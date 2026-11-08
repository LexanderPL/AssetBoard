import threading
from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest

from assetboard.config import Settings
from assetboard.db.models.identity import Workspace
from assetboard.db.repositories.scheduled import RecurringWorkspaceReader
from assetboard.recurring_jobs import RecurringJobs
from assetboard.services.providers.registry import build_provider_runtime
from tests.fake_notifier import FakeNotifier
from tests.fake_provider_http import FailingProviderHttpClient
from tests.tenant.workspace_db import workspace_db


@pytest.fixture
def db(tmp_path):
    yield from workspace_db(tmp_path)


def test_jobs_follow_workspace_timezones_refresh_and_shutdown(db):
    with db.factory.begin() as session:
        workspace = session.get(Workspace, db.workspace_id)
        assert workspace is not None
        workspace.timezone = "Europe/Warsaw"
    logger = Mock()
    jobs = RecurringJobs(db.factory, Settings(_env_file=None), lambda: db.clock, logger)
    jobs.start()
    try:
        assert jobs.caught_up.wait(timeout=30)  # the catch-up runs as a background job
        job = jobs.scheduler.get_job(str(db.workspace_id))
        assert job is not None
        fire = job.trigger.get_next_fire_time(None, datetime(2026, 3, 28, 23, tzinfo=UTC))
        assert fire.isoformat() == "2026-03-29T03:00:00+02:00"
        auto_post = jobs.scheduler.get_job(f"auto-post-{db.workspace_id}")
        assert auto_post is not None
        auto_fire = auto_post.trigger.get_next_fire_time(
            None, datetime(2026, 3, 28, 23, tzinfo=UTC)
        )
        assert auto_fire.isoformat() == "2026-03-29T00:05:00+01:00"
        snapshot = jobs.scheduler.get_job(f"networth-snapshot-{db.workspace_id}")
        assert snapshot is not None
        snapshot_fire = snapshot.trigger.get_next_fire_time(
            None, datetime(2026, 3, 28, 23, tzinfo=UTC)
        )
        assert snapshot_fire.isoformat() == "2026-04-01T04:00:00+02:00"
        other = db.add_workspace("Other")
        with db.factory.begin() as session:
            workspace = session.get(Workspace, db.workspace_id)
            assert workspace is not None
            workspace.timezone = "America/New_York"
        jobs.reconcile()
        assert jobs.scheduler.get_job(str(other)) is not None
        updated = jobs.scheduler.get_job(str(db.workspace_id))
        assert updated is not None and str(updated.trigger.timezone) == "America/New_York"
        updated_snapshot = jobs.scheduler.get_job(f"networth-snapshot-{db.workspace_id}")
        assert updated_snapshot is not None
        assert str(updated_snapshot.trigger.timezone) == "America/New_York"
        with db.factory.begin() as session:
            assert set(RecurringWorkspaceReader(session).workspaces()) == {
                (db.workspace_id, "America/New_York"),
                (other, "UTC"),
            }
            session.delete(session.get(Workspace, other))
        jobs.reconcile()
        assert jobs.scheduler.get_job(str(other)) is None
        assert jobs.scheduler.get_job(f"auto-post-{other}") is None
        assert jobs.scheduler.get_job(f"networth-snapshot-{other}") is None
        logger.error.assert_not_called()
    finally:
        jobs.stop()
    assert not jobs.scheduler.running


def test_start_returns_before_the_catch_up_generates(db, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def slow(*_args):
        started.set()
        release.wait(timeout=30)
        return 0

    monkeypatch.setattr("assetboard.recurring_jobs.generate_workspace", slow)
    jobs = RecurringJobs(db.factory, Settings(_env_file=None), lambda: db.clock, Mock())
    jobs.start()
    try:
        assert started.wait(timeout=30)
        assert not jobs.caught_up.is_set()  # start() did not wait for generation
        release.set()
        assert jobs.caught_up.wait(timeout=30)
    finally:
        release.set()
        jobs.stop()


def test_failed_generation_is_logged_without_data_and_later_runs_can_retry(db, monkeypatch):
    logger = Mock()
    jobs = RecurringJobs(db.factory, Settings(_env_file=None), lambda: db.clock, logger)
    generate = Mock(side_effect=[RuntimeError("sensitive SQL parameters"), 0])
    monkeypatch.setattr("assetboard.recurring_jobs.generate_workspace", generate)
    jobs.run(db.workspace_id)
    jobs.run(db.workspace_id)
    assert generate.call_count == 2
    assert logger.error.call_count == 1
    message = logger.error.call_args.args[0] % logger.error.call_args.args[1:]
    assert "sensitive" not in message and "RuntimeError" in message
    assert logger.error.call_args.kwargs["exc_info"] is True


def test_catch_up_takes_one_snapshot_per_workspace_with_its_timezone(db, monkeypatch):
    calls = []

    def fake_snapshot(_factory, scope, _clock, timezone):
        calls.append((scope.workspace.id, timezone))
        return False

    monkeypatch.setattr("assetboard.recurring_jobs.take_auto_snapshot", fake_snapshot)
    jobs = RecurringJobs(db.factory, Settings(_env_file=None), lambda: db.clock, Mock())
    jobs.reconcile()
    jobs.catch_up()
    assert calls == [(db.workspace_id, "UTC")]


def test_a_failing_snapshot_is_logged_without_data(db, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("secret detail")

    monkeypatch.setattr("assetboard.recurring_jobs.take_auto_snapshot", broken)
    logger = Mock()
    jobs = RecurringJobs(db.factory, Settings(_env_file=None), lambda: db.clock, logger)
    jobs.networth_snapshot(db.workspace_id, "UTC")
    message, *args = logger.error.call_args.args
    rendered = message % tuple(args)
    assert "networth_snapshot_failed" in message
    assert str(db.workspace_id) in rendered
    assert "RuntimeError" in rendered and "secret detail" not in rendered
    assert logger.error.call_args.kwargs["exc_info"] is True


def test_a_deleted_workspace_is_skipped_quietly(db):
    """A job that runs after its workspace was deleted (ADR-0020) logs at DEBUG only."""
    logger = Mock()
    jobs = RecurringJobs(db.factory, Settings(_env_file=None), lambda: db.clock, logger)
    gone = uuid4()  # no such workspace: the generation's lock raises NotFound
    jobs.run(gone)
    assert logger.error.call_count == 0
    assert logger.debug.call_count == 1
    assert logger.debug.call_args.args == (
        "recurring_generation_skipped workspace=%s reason=deleted",
        gone,
    )
    assert "exc_info" not in logger.debug.call_args.kwargs


def test_the_reminder_tick_is_scheduled_and_runs_every_workspace(db, monkeypatch):
    other = db.add_workspace("Other")
    calls = []

    def fake_run(_factory, scope, _clock, _settings, notifier, *, out_of_time):
        calls.append((scope.workspace.id, notifier))

    monkeypatch.setattr("assetboard.recurring_jobs.run_notifications", fake_run)
    notifier = FakeNotifier()
    jobs = RecurringJobs(
        db.factory, Settings(_env_file=None), lambda: db.clock, Mock(), notifier=lambda: notifier
    )
    jobs.reconcile()
    jobs.notify()
    jobs.notify()
    assert {workspace_id for workspace_id, _ in calls} == {db.workspace_id, other}
    assert all(passed is notifier for _, passed in calls)
    # The starting workspace rotates from one tick to the next.
    assert calls[0][0] != calls[2][0]
    jobs.start()
    try:
        job = jobs.scheduler.get_job("notifications")
        assert job is not None and job.max_instances == 1
        cleanup = jobs.scheduler.get_job("import-upload-cleanup")
        assert cleanup is not None and cleanup.max_instances == 1
    finally:
        jobs.stop()


def test_the_reminder_tick_stops_at_its_time_budget_and_obeys_the_kill_switch(db, monkeypatch):
    db.add_workspace("Other")
    calls = []
    now = iter([0.0, 0.0, 100.0, 100.0, 100.0])

    def fake_run(_factory, scope, _clock, _settings, _notifier, *, out_of_time):
        calls.append(scope.workspace.id)

    monkeypatch.setattr("assetboard.recurring_jobs.run_notifications", fake_run)
    settings = Settings(_env_file=None, notification_tick_seconds=45)
    jobs = RecurringJobs(
        db.factory,
        settings,
        lambda: db.clock,
        Mock(),
        notifier=FakeNotifier,
        monotonic=lambda: next(now),
    )
    jobs.reconcile()
    jobs.notify()
    assert len(calls) == 1  # the second workspace waits for the next tick
    off = RecurringJobs(
        db.factory,
        Settings(_env_file=None, notifications_enabled=False),
        lambda: db.clock,
        Mock(),
        notifier=FakeNotifier,
    )
    off.reconcile()
    off.notify()
    assert len(calls) == 1


def test_a_failing_reminder_run_is_logged_without_data(db, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("secret detail")

    monkeypatch.setattr("assetboard.recurring_jobs.run_notifications", broken)
    logger = Mock()
    jobs = RecurringJobs(
        db.factory, Settings(_env_file=None), lambda: db.clock, logger, notifier=FakeNotifier
    )
    jobs.reconcile()
    jobs.notify()
    message, *args = logger.error.call_args.args
    assert "notification_run_failed" in message
    assert "secret detail" not in message % tuple(args)


def _mock_scheduler(jobs: RecurringJobs) -> Mock:
    scheduler = Mock()
    scheduler.running = False
    jobs.scheduler = scheduler
    return scheduler


def test_provider_jobs_are_registered_only_with_a_runtime(db):
    runtime = build_provider_runtime(
        Settings(_env_file=None), db.clock, http=FailingProviderHttpClient()
    )
    jobs = RecurringJobs(
        db.factory,
        Settings(_env_file=None),
        lambda: db.clock,
        Mock(),
        provider_runtime=runtime,
    )
    scheduler = _mock_scheduler(jobs)
    jobs.start()
    calls = {call.kwargs.get("id"): call for call in scheduler.add_job.call_args_list}
    assert calls["price-refresh-nbp"].kwargs["max_instances"] == 1
    assert calls["price-refresh-ecb"].kwargs["max_instances"] == 1
    assert calls["price-refresh-coingecko"].kwargs["max_instances"] == 1
    assert calls["price-refresh-eodhd"].kwargs["max_instances"] == 1
    assert calls["price-refresh-goldapi"].kwargs["max_instances"] == 1
    fire = (
        calls["price-refresh-nbp"]
        .args[1]
        .get_next_fire_time(None, datetime(2026, 3, 2, 0, tzinfo=UTC))
    )
    assert fire.isoformat() == "2026-03-02T18:10:00+00:00"
    goldapi = calls["price-refresh-goldapi"]
    assert goldapi.args[0] == jobs.refresh_prices
    assert goldapi.kwargs["args"] == ["goldapi"]
    assert goldapi.kwargs["coalesce"] is True
    assert goldapi.kwargs["misfire_grace_time"] is None
    goldapi_fire = goldapi.args[1].get_next_fire_time(None, datetime(2026, 3, 2, 0, tzinfo=UTC))
    assert goldapi_fire.isoformat() == "2026-03-02T22:50:00+00:00"


def test_request_backfill_adds_one_immediate_job(db, monkeypatch):
    runtime = build_provider_runtime(
        Settings(_env_file=None), db.clock, http=FailingProviderHttpClient()
    )
    jobs = RecurringJobs(
        db.factory,
        Settings(_env_file=None),
        lambda: db.clock,
        Mock(),
        provider_runtime=runtime,
    )
    scheduler = Mock()
    scheduler.running = True
    monkeypatch.setattr(jobs, "scheduler", scheduler)
    jobs.request_backfill(db.workspace_id, "nbp")
    scheduler.add_job.assert_called_once()
    kwargs = scheduler.add_job.call_args.kwargs
    assert kwargs["id"] == f"price-backfill-{db.workspace_id}-nbp"
    assert kwargs["max_instances"] == 1
    assert "trigger" not in kwargs


def test_provider_jobs_are_absent_without_a_runtime(db):
    jobs = RecurringJobs(db.factory, Settings(_env_file=None), lambda: db.clock, Mock())
    scheduler = _mock_scheduler(jobs)
    jobs.start()
    assert "price-refresh-nbp" not in {
        call.kwargs.get("id") for call in scheduler.add_job.call_args_list
    }


def test_refresh_prices_with_no_config_does_not_call_the_network(db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "assetboard.recurring_jobs.refresh_all",
        lambda *_args, **_kwargs: calls.append(True) or [],
    )
    runtime = build_provider_runtime(
        Settings(_env_file=None), db.clock, http=FailingProviderHttpClient()
    )
    jobs = RecurringJobs(
        db.factory,
        Settings(_env_file=None),
        lambda: db.clock,
        Mock(),
        provider_runtime=runtime,
    )
    jobs.refresh_prices("nbp")
    assert calls == [True]
