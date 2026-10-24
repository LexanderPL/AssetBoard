import threading
from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest

from assetboard.config import Settings
from assetboard.db.models.identity import Workspace
from assetboard.db.repositories.scheduled import RecurringWorkspaceReader
from assetboard.recurring_jobs import RecurringJobs
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
        other = db.add_workspace("Other")
        with db.factory.begin() as session:
            workspace = session.get(Workspace, db.workspace_id)
            assert workspace is not None
            workspace.timezone = "America/New_York"
        jobs.reconcile()
        assert jobs.scheduler.get_job(str(other)) is not None
        updated = jobs.scheduler.get_job(str(db.workspace_id))
        assert updated is not None and str(updated.trigger.timezone) == "America/New_York"
        with db.factory.begin() as session:
            assert set(RecurringWorkspaceReader(session).workspaces()) == {
                (db.workspace_id, "America/New_York"),
                (other, "UTC"),
            }
            session.delete(session.get(Workspace, other))
        jobs.reconcile()
        assert jobs.scheduler.get_job(str(other)) is None
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
