"""The scheduler skips demo workspaces and schedules the demo reset (F9.11b)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import UUID

import pytest

from assetboard.config import Settings
from assetboard.db.models.identity import Workspace
from assetboard.db.repositories.scheduled import RecurringWorkspaceReader
from assetboard.recurring_jobs import RecurringJobs
from tests.fake_notifier import FakeNotifier
from tests.tenant.workspace_db import workspace_db


@pytest.fixture
def db(tmp_path):
    yield from workspace_db(tmp_path)


def _add_demo_workspace(db) -> UUID:
    with db.factory.begin() as session:
        workspace = Workspace(
            name="Demo",
            base_currency="EUR",
            locale="en",
            timezone="UTC",
            is_demo=True,
        )
        session.add(workspace)
        session.flush()
        return workspace.id


def test_the_reader_and_recurring_jobs_skip_the_demo_workspace(db, monkeypatch):
    demo_id = _add_demo_workspace(db)
    with db.factory() as session:
        reader = RecurringWorkspaceReader(session)
        assert demo_id not in {row[0] for row in reader.workspaces()}
        assert demo_id in {row[0] for row in reader.workspaces(include_demo=True)}
        assert reader.is_demo(demo_id) is True
        assert reader.is_demo(db.workspace_id) is False

    calls: list[str] = []
    monkeypatch.setattr(
        "assetboard.recurring_jobs.generate_workspace",
        lambda *_args, **_kwargs: calls.append("generation"),
    )
    monkeypatch.setattr(
        "assetboard.recurring_jobs.auto_post_workspace",
        lambda *_args, **_kwargs: calls.append("auto_post"),
    )
    monkeypatch.setattr(
        "assetboard.recurring_jobs.take_auto_snapshot",
        lambda *_args, **_kwargs: calls.append("snapshot"),
    )
    monkeypatch.setattr(
        "assetboard.recurring_jobs.resync_pending_income",
        lambda *_args, **_kwargs: calls.append("income"),
    )

    jobs = RecurringJobs(db.factory, Settings(_env_file=None), lambda: db.clock, Mock())
    jobs.run(demo_id)
    jobs.auto_post(demo_id)
    jobs.networth_snapshot(demo_id, "UTC")
    jobs.resync_income(demo_id)
    assert calls == []

    jobs.run(db.workspace_id)
    jobs.auto_post(db.workspace_id)
    jobs.networth_snapshot(db.workspace_id, "UTC")
    jobs.resync_income(db.workspace_id)
    assert calls == ["generation", "auto_post", "snapshot", "income"]


def test_the_reminder_tick_skips_the_demo_workspace(db, monkeypatch):
    demo_id = _add_demo_workspace(db)
    called: list[object] = []

    def fake_run(_factory, scope, _clock, _settings, notifier, *, out_of_time):
        called.append(scope.workspace.id)

    monkeypatch.setattr("assetboard.recurring_jobs.run_notifications", fake_run)
    jobs = RecurringJobs(
        db.factory, Settings(_env_file=None), lambda: db.clock, Mock(), notifier=FakeNotifier
    )
    jobs.reconcile()
    assert demo_id not in jobs.zones
    jobs.notify()
    assert called == [db.workspace_id]


def test_the_reset_job_is_scheduled_in_the_demo_time_zone(db):
    settings = Settings(
        _env_file=None,
        demo_enabled=True,
        demo_reset_time="04:30",
        demo_timezone="Europe/Warsaw",
    )
    jobs = RecurringJobs(db.factory, settings, lambda: db.clock, Mock())
    jobs.start()
    try:
        assert jobs.caught_up.wait(timeout=30)
        job = jobs.scheduler.get_job("demo-reset")
        assert job is not None
        assert str(job.trigger.timezone) == "Europe/Warsaw"
        fire = job.trigger.get_next_fire_time(None, datetime(2026, 3, 28, 23, tzinfo=UTC))
        assert fire.isoformat() == "2026-03-29T04:30:00+02:00"
    finally:
        jobs.stop()

    off = RecurringJobs(db.factory, Settings(_env_file=None), lambda: db.clock, Mock())
    off.start()
    try:
        assert off.caught_up.wait(timeout=30)
        assert off.scheduler.get_job("demo-reset") is None
    finally:
        off.stop()


def test_a_failing_demo_reset_is_logged_without_stopping_the_scheduler(db, monkeypatch):
    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("sensitive detail")

    monkeypatch.setattr("assetboard.recurring_jobs.reset_demo_instance", broken)
    logger = Mock()
    jobs = RecurringJobs(db.factory, Settings(_env_file=None), lambda: db.clock, logger)
    jobs.demo_reset()
    assert logger.error.call_count == 1
    message, *args = logger.error.call_args.args
    rendered = message % tuple(args)
    assert "demo_reset_failed" in rendered
    assert "RuntimeError" in rendered and "sensitive detail" not in rendered
