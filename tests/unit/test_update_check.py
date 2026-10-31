"""The opt-in update check (ADR-0023) against a fake release source: never the network."""

from __future__ import annotations

import logging
import urllib.request
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.db.models.identity import AuditEvent, InstanceSettings, User
from assetboard.recurring_jobs import RecurringJobs
from assetboard.services import update_check
from assetboard.services.instance_settings import update_settings
from assetboard.services.update_check import (
    UpdateCheckError,
    is_newer,
    parse_release,
    run_update_check,
    update_notice,
    version_key,
)
from tests.fake_release_source import FakeReleaseSource, release_body
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def db(tmp_path):
    yield from workspace_db(tmp_path)


def _enable(db: WorkspaceDb, enabled: bool = True) -> None:
    with db.factory.begin() as session:
        row = session.get(InstanceSettings, 1)
        assert row is not None
        row.check_for_updates = enabled


def _stored(db: WorkspaceDb) -> tuple[str | None, datetime | None]:
    with db.factory() as session:
        row = session.get(InstanceSettings, 1)
        assert row is not None
        return row.latest_release_tag, row.update_checked_at


# Versions


@pytest.mark.parametrize(
    ("latest", "current", "newer"),
    [
        ("v0.3.7", "0.3.6", True),
        ("0.4.0", "0.3.10", True),
        ("v0.3.10", "0.3.9", True),  # numeric, not string, comparison
        ("v1.0", "0.9.9", True),
        ("v0.3.6", "0.3.6", False),
        ("v0.3.6", "0.3.6.0", False),
        ("v0.3.5", "0.3.6", False),
        ("v0.3.6", "0.3.6.dev2+gabc", False),
        ("v0.3.7", "0.3.6.dev2+gabc", True),
        ("nightly", "0.3.6", False),
        ("v0.3.7", "unknown", False),
    ],
)
def test_is_newer_compares_numeric_parts(latest: str, current: str, newer: bool) -> None:
    assert is_newer(latest, current) is newer


def test_version_key_reads_the_leading_release_number() -> None:
    assert version_key("v1.2.3") == (1, 2, 3)
    assert version_key("0.3.6.dev1+g123") == (0, 3, 6)
    assert version_key("release") is None


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"{not json", "malformed_json"),
        (b"\xff\xfe", "malformed_json"),
        (b"[]", "malformed_release"),
        (b"{}", "malformed_tag"),
        (b'{"tag_name": 7}', "malformed_tag"),
        (b'{"tag_name": "v1.0.0-rc1"}', "malformed_tag"),
        (b'{"tag_name": "<script>"}', "malformed_tag"),
        (release_body("v0.4.0", draft=True), "not_a_release"),
        (release_body("v0.4.0", prerelease=True), "not_a_release"),
    ],
)
def test_parse_release_refuses_what_is_not_a_plain_release(body: bytes, reason: str) -> None:
    with pytest.raises(UpdateCheckError) as error:
        parse_release(body)
    assert error.value.reason == reason


def test_parse_release_returns_the_tag() -> None:
    assert parse_release(release_body("v0.3.7")) == "v0.3.7"


# The check


def test_a_disabled_check_sends_nothing(db: WorkspaceDb) -> None:
    source = FakeReleaseSource(body=release_body("v9.0.0"), fail_with=None)
    assert run_update_check(db.factory, source, db.clock, Mock()) == "disabled"
    assert source.calls == 0
    assert _stored(db) == (None, None)


@pytest.mark.parametrize(
    ("tag", "notice"), [("v0.3.7", "0.3.7"), ("v0.3.6", None), ("v0.3.5", None)]
)
def test_an_enabled_check_stores_the_release_and_notices_only_newer(
    db: WorkspaceDb, tag: str, notice: str | None
) -> None:
    _enable(db)
    source = FakeReleaseSource(body=release_body(tag), fail_with=None)
    assert run_update_check(db.factory, source, db.clock, Mock()) == "checked"
    assert source.calls == 1
    assert _stored(db) == (tag, db.clock.now())
    found = update_notice(db.factory, "0.3.6")
    if notice is None:
        assert found is None
    else:
        assert found is not None and found.version == notice
        assert found.url == f"https://github.com/LexanderPL/AssetBoard/releases/tag/{tag}"


@pytest.mark.parametrize(
    "source",
    [
        FakeReleaseSource(fail_with="URLError"),
        FakeReleaseSource(fail_with="http_403"),
        FakeReleaseSource(body=b"<html>rate limited</html>", fail_with=None),
        FakeReleaseSource(body=b'{"tag_name": null}', fail_with=None),
    ],
    ids=["network", "http-error", "malformed-json", "no-tag"],
)
def test_a_failed_check_keeps_the_last_result_and_logs_only_a_reason(
    db: WorkspaceDb, source: FakeReleaseSource, caplog: pytest.LogCaptureFixture
) -> None:
    _enable(db)
    earlier = datetime(2026, 2, 27, 12, tzinfo=UTC)
    with db.factory.begin() as session:
        row = session.get(InstanceSettings, 1)
        assert row is not None
        row.latest_release_tag, row.update_checked_at = "v0.3.5", earlier
    logger = logging.getLogger("test.update_check")
    with caplog.at_level(logging.DEBUG, logger="test.update_check"):
        assert run_update_check(db.factory, source, db.clock, logger) == "failed"
    assert _stored(db) == ("v0.3.5", earlier)
    [record] = caplog.records
    assert record.levelno == logging.INFO
    assert record.getMessage().startswith("update_check_failed reason=")
    assert "rate limited" not in record.getMessage()


def test_turning_the_check_off_hides_the_notice_and_forgets_the_result(
    db: WorkspaceDb,
) -> None:
    _enable(db)
    source = FakeReleaseSource(body=release_body("v0.4.0"), fail_with=None)
    run_update_check(db.factory, source, db.clock, Mock())
    assert update_notice(db.factory, "0.3.6") is not None
    with db.factory.begin() as session:
        admin = User(email="root@example.com", display_name="Root", is_instance_admin=True)
        session.add(admin)
        session.flush()
        status = update_settings(
            session,
            admin,
            allow_workspace_creation=True,
            allow_registration=False,
            check_for_updates=False,
            settings=Settings(_env_file=None),
            clock=db.clock,
            host=None,
            user_agent=None,
        )
    assert status == "done"
    assert update_notice(db.factory, "0.3.6") is None
    assert _stored(db) == (None, None)


def test_the_setting_change_is_audited(db: WorkspaceDb) -> None:
    with db.factory.begin() as session:
        admin = User(email="root@example.com", display_name="Root", is_instance_admin=True)
        session.add(admin)
        session.flush()
        arguments = {
            "allow_workspace_creation": True,
            "allow_registration": False,
            "settings": Settings(_env_file=None),
            "clock": db.clock,
            "host": None,
            "user_agent": None,
        }
        assert update_settings(session, admin, check_for_updates=True, **arguments) == "done"
        # ``None`` keeps the stored value: nothing to change.
        assert update_settings(session, admin, **arguments) == "unchanged"
    with db.factory() as session:
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "instance.settings_updated")
        ).all()
        assert [event.data for event in events] == [{"check_for_updates": True}]


# The scheduler job


def test_the_job_checks_once_a_day_and_right_after_enabling(db: WorkspaceDb) -> None:
    now = [datetime(2026, 3, 1, 12, tzinfo=UTC)]

    class Clock(FixedClock):
        def now(self) -> datetime:
            return now[0]

    source = FakeReleaseSource(body=release_body("v0.4.0"), fail_with=None)
    jobs = RecurringJobs(
        db.factory,
        Settings(_env_file=None),
        lambda: Clock("2026-03-01T12:00:00+00:00"),
        Mock(),
        release_source=lambda: source,
    )
    assert jobs.check_updates() == "disabled" and source.calls == 0
    _enable(db)
    assert jobs.check_updates() == "checked" and source.calls == 1
    now[0] += timedelta(hours=23)
    assert jobs.check_updates() == "not_due" and source.calls == 1
    now[0] += timedelta(hours=1)
    assert jobs.check_updates() == "checked" and source.calls == 2
    # A failure waits a day too, instead of retrying every hour.
    source.fail_with = "offline"
    now[0] += timedelta(days=1)
    assert jobs.check_updates() == "failed" and source.calls == 3
    now[0] += timedelta(hours=1)
    assert jobs.check_updates() == "not_due" and source.calls == 3
    # Turning it on again asks right away.
    assert jobs.check_updates(force=True) == "failed" and source.calls == 4


def test_the_job_survives_an_unexpected_error(db: WorkspaceDb) -> None:
    _enable(db)

    class Broken:
        def fetch_latest_release(self) -> bytes:
            raise RuntimeError("boom")

    logger = Mock()
    jobs = RecurringJobs(
        db.factory, Settings(_env_file=None), lambda: db.clock, logger, release_source=Broken
    )
    assert jobs.check_updates() == "failed"
    logger.error.assert_called_once()
    assert "boom" not in str(logger.error.call_args)


def test_the_github_source_sends_only_a_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real client, with the opener replaced: the request it would send, no network."""
    sent: list[tuple[urllib.request.Request, float]] = []

    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return release_body("v0.3.7")

    class Opener:
        def open(self, request: urllib.request.Request, timeout: float) -> Response:
            sent.append((request, timeout))
            return Response()

    source = update_check.GitHubReleaseSource()
    monkeypatch.setattr(source, "_opener", Opener())
    assert parse_release(source.fetch_latest_release()) == "v0.3.7"
    [(request, timeout)] = sent
    assert timeout == 5.0
    assert request.full_url == (
        "https://api.github.com/repos/LexanderPL/AssetBoard/releases/latest"
    )
    headers = {k.lower(): v for k, v in request.header_items()}
    assert headers["user-agent"] == "AssetBoard"
    assert "cookie" not in headers and "authorization" not in headers
    assert request.data is None
