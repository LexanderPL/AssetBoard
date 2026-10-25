from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, time
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.db.models.identity import AuditEvent, User, Workspace
from assetboard.db.models.income import IncomeSource, WorkLog
from assetboard.db.models.notifications import NotificationChannel, NotificationLog
from assetboard.db.models.scheduled import ScheduledItem, ScheduledOccurrence
from assetboard.db.repositories import NotFound
from assetboard.services import notifications
from assetboard.services.context import PermissionDenied
from assetboard.services.notifications import ChannelInput, NotificationError
from assetboard.services.workspaces import remove_member
from tests.fake_notifier import FakeNotifier
from tests.tenant.factories import make_category
from tests.tenant.workspace_db import WorkspaceDb, workspace_db

URL = "ntfys://ntfy.example.com/reminders-secret-topic"


@pytest.fixture
def db(tmp_path):
    yield from workspace_db(tmp_path)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path, secret_key="notification-test-secret")


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


def add_channel(
    db: WorkspaceDb, settings: Settings, notifier: FakeNotifier, key: str = "ana", **values: object
) -> UUID:
    if key not in db.users:
        db.add_member(key, "viewer")
    fields: dict[str, Any] = {"name": "Phone", "url": URL} | values
    return notifications.create_channel(
        db.factory, db.ctx(key, "viewer"), settings, notifier, ChannelInput(**fields)
    )


def stored(db: WorkspaceDb, channel_id: UUID) -> NotificationChannel:
    with db.factory() as session:
        row = session.get(NotificationChannel, channel_id)
        assert row is not None
        return row


def log_rows(db: WorkspaceDb) -> list[NotificationLog]:
    with db.factory() as session:
        return list(session.scalars(select(NotificationLog).order_by(NotificationLog.sent_on)))


def add_occurrence(db: WorkspaceDb, due: date, *, lead: int | None = None, label="Rent") -> UUID:
    with db.factory.begin() as session:
        category = make_category(session, db.workspace_id)
        category.kind = "expense"
        session.add(category)
        session.flush()
        item = ScheduledItem(
            workspace_id=db.workspace_id,
            name=label,
            direction="expense",
            category_id=category.id,
            amount_mode="fixed",
            amount_minor=123456,
            currency="EUR",
            start_date=date(2026, 1, 1),
            freq="monthly",
            interval=1,
            weekdays=[],
            month_day="1",
            month=1,
            weekend_policy="none",
            notes="",
            reminder_lead_days=lead,
        )
        session.add(item)
        session.flush()
        occurrence = ScheduledOccurrence(
            workspace_id=db.workspace_id,
            scheduled_item_id=item.id,
            category_id=category.id,
            direction="expense",
            nominal_date=due,
            due_date=due,
            expected_minor=123456,
            currency="EUR",
            amount_state="confirmed",
            status="pending",
            label=label,
        )
        session.add(occurrence)
        session.flush()
        return occurrence.id


@dataclass(frozen=True)
class _WorkspaceRef:
    id: UUID


@dataclass(frozen=True)
class TrustedScope:
    """The workspace scope the scheduler passes to the run."""

    workspace: _WorkspaceRef


def run(
    db: WorkspaceDb, settings: Settings, notifier: FakeNotifier, at: str, **kwargs: Any
) -> notifications.RunStats:
    return notifications.run_workspace(
        db.factory,
        TrustedScope(_WorkspaceRef(db.workspace_id)),
        FixedClock(at),
        settings,
        notifier,
        **kwargs,
    )


# Channels: storage, masking, validation, ownership


def test_url_is_encrypted_bound_to_its_channel_and_never_audited(db, settings, notifier) -> None:
    channel_id = add_channel(db, settings, notifier)
    row = stored(db, channel_id)
    assert URL not in row.url_encrypted and "secret" not in row.url_encrypted
    assert notifications.decrypt_url(settings, channel_id, row.url_encrypted) == URL
    # A ciphertext copied to another row is refused.
    other = add_channel(db, settings, notifier, name="Laptop")
    assert notifications.decrypt_url(settings, other, row.url_encrypted) is None
    # Another instance secret cannot read it.
    changed = Settings(_env_file=None, data_dir=settings.data_dir, secret_key="another-secret")
    assert notifications.decrypt_url(changed, channel_id, row.url_encrypted) is None
    view = notifications.channel(db.factory, db.ctx("ana", "viewer"), channel_id)
    assert view.masked_url == "ntfys://••••••" and view.service_name == "Fake ntfys"
    with db.factory() as session:
        events = session.scalars(select(AuditEvent).where(AuditEvent.entity_id == str(channel_id)))
        [event] = list(events)
    assert event.action == "notification_channel.created"
    assert event.data == {"scheme": "ntfys"}
    assert "secret" not in str(event.data)


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("", "invalid_url"),
        ("ntfys://a ntfys://b", "invalid_url"),
        ("no-scheme", "invalid_url"),
        ("ntfys://", "invalid_url"),
        ("ntfys://invalid", "invalid_url"),
        ("json://internal.example/hook", "scheme_not_allowed"),
        ("windows://", "invalid_url"),
        ("syslog://localhost", "scheme_not_allowed"),
        ("ntfys://" + "x" * 2050, "invalid_url"),
    ],
)
def test_urls_are_validated_and_schemes_allowlisted(db, settings, notifier, url, code) -> None:
    with pytest.raises(NotificationError) as error:
        add_channel(db, settings, notifier, url=url)
    assert error.value.code == code


def test_operator_can_change_the_scheme_allowlist(db, settings, notifier) -> None:
    narrow = Settings(
        _env_file=None, data_dir=settings.data_dir, secret_key="x", notification_schemes="gotifys"
    )
    with pytest.raises(NotificationError, match="scheme_not_allowed"):
        add_channel(db, narrow, notifier)
    assert add_channel(db, narrow, notifier, url="gotifys://gotify.example/token")


@pytest.mark.parametrize(
    ("values", "code"),
    [
        ({"name": " "}, "invalid_name"),
        ({"name": "x" * 101}, "invalid_name"),
        ({"mode": "hourly"}, "invalid_mode"),
        ({"default_lead_days": 61}, "invalid_lead_days"),
        ({"default_lead_days": -1}, "invalid_lead_days"),
    ],
)
def test_channel_fields_are_validated(db, settings, notifier, values, code) -> None:
    with pytest.raises(NotificationError) as error:
        add_channel(db, settings, notifier, **values)
    assert error.value.code == code


def test_members_see_and_change_only_their_own_channels(db, settings, notifier) -> None:
    ana = add_channel(db, settings, notifier, "ana")
    add_channel(db, settings, notifier, "ben")
    page = notifications.page(db.factory, db.ctx("ben", "viewer"), settings)
    assert [channel.name for channel in page.channels] == ["Phone"]
    assert page.channels[0].id != ana
    ben = db.ctx("ben", "viewer")
    with pytest.raises(NotFound):
        notifications.channel(db.factory, ben, ana)
    with pytest.raises(NotFound):
        notifications.update_channel(
            db.factory, ben, settings, notifier, ana, ChannelInput(name="Mine", url="")
        )
    with pytest.raises(NotFound):
        notifications.delete_channel(db.factory, ben, ana)
    with pytest.raises(NotFound):
        notifications.send_test(db.factory, ben, settings, notifier, ana)
    assert stored(db, ana).name == "Phone"


def test_update_keeps_the_url_unless_a_new_one_is_typed(db, settings, notifier) -> None:
    channel_id = add_channel(db, settings, notifier)
    ctx = db.ctx("ana", "viewer")
    notifications.update_channel(
        db.factory,
        ctx,
        settings,
        notifier,
        channel_id,
        ChannelInput(name="Phone 2", url="", mode="per_item", digest_time=time(7, 30)),
    )
    row = stored(db, channel_id)
    assert (row.name, row.mode, row.digest_time) == ("Phone 2", "per_item", time(7, 30))
    assert notifications.decrypt_url(settings, channel_id, row.url_encrypted) == URL
    notifications.update_channel(
        db.factory,
        ctx,
        settings,
        notifier,
        channel_id,
        ChannelInput(name="Phone 2", url="tgram://bot-token/12345"),
    )
    row = stored(db, channel_id)
    assert row.scheme == "tgram"
    assert notifications.decrypt_url(settings, channel_id, row.url_encrypted) == (
        "tgram://bot-token/12345"
    )


def test_channel_limit_per_member(db, settings, notifier) -> None:
    for index in range(notifications.MAX_CHANNELS_PER_MEMBER):
        add_channel(db, settings, notifier, name=f"Channel {index}")
    assert not notifications.page(db.factory, db.ctx("ana", "viewer"), settings).can_add
    with pytest.raises(NotificationError, match="too_many_channels"):
        add_channel(db, settings, notifier, name="One more")


def test_a_removed_member_is_refused_inside_the_write(db, settings, notifier) -> None:
    channel_id = add_channel(db, settings, notifier)
    db.add_member("olga", "owner")
    stale = db.ctx("ana", "viewer")
    remove_member(db.factory, db.ctx("olga", "owner"), membership_id=db.memberships["ana"])
    with pytest.raises(PermissionDenied):
        notifications.create_channel(
            db.factory, stale, settings, notifier, ChannelInput(name="x", url=URL)
        )
    # Removing the member deleted their channels (and their log rows with them).
    with db.factory() as session:
        assert session.get(NotificationChannel, channel_id) is None


def test_send_test_reaches_the_channel_and_is_throttled(db, settings, notifier) -> None:
    channel_id = add_channel(db, settings, notifier)
    ctx = db.ctx("ana", "viewer")
    outcome = notifications.send_test(db.factory, ctx, settings, notifier, channel_id)
    assert outcome.ok
    [message] = notifier.sent
    assert message.url == URL and "test" in message.title
    for _ in range(notifications.TEST_SEND_LIMIT - 1):
        notifications.send_test(db.factory, ctx, settings, notifier, channel_id)
    throttled = notifications.send_test(db.factory, ctx, settings, notifier, channel_id)
    assert throttled == notifications.TestOutcome(False, "throttled")
    assert len(notifier.sent) == notifications.TEST_SEND_LIMIT


def test_send_test_reports_a_failure_code_only(db, settings, notifier, caplog) -> None:
    channel_id = add_channel(db, settings, notifier)
    notifier.fail_with = "timeout"
    with caplog.at_level(logging.DEBUG):
        outcome = notifications.send_test(
            db.factory, db.ctx("ana", "viewer"), settings, notifier, channel_id
        )
    assert outcome == notifications.TestOutcome(False, "timeout")
    assert stored(db, channel_id).last_error == "timeout"
    assert "secret" not in caplog.text and URL not in caplog.text


def test_send_test_when_delivery_is_off(db, settings, notifier) -> None:
    channel_id = add_channel(db, settings, notifier)
    off = Settings(
        _env_file=None, data_dir=settings.data_dir, secret_key="x", notifications_enabled=False
    )
    outcome = notifications.send_test(
        db.factory, db.ctx("ana", "viewer"), off, notifier, channel_id
    )
    assert outcome.error == "disabled" and notifier.sent == []


# The daily run


def test_runs_at_the_digest_time_once_per_day_and_dedupes_across_restarts(
    db, settings, notifier
) -> None:
    add_channel(db, settings, notifier)
    add_occurrence(db, date(2026, 3, 12))
    assert run(db, settings, notifier, "2026-03-10T07:59:00+00:00").channels == 0
    stats = run(db, settings, notifier, "2026-03-10T08:00:00+00:00")
    assert (stats.channels, stats.sent) == (1, 1)
    [message] = notifier.sent
    assert "Rent" in message.body and "Due" in message.body
    assert "1,234.56" not in message.body  # amounts are off by default
    # Later ticks the same day, and a "restart" (a new call with the same database): nothing.
    run(db, settings, notifier, "2026-03-10T08:01:00+00:00")
    run(db, settings, notifier, "2026-03-10T22:00:00+00:00")
    assert len(notifier.sent) == 1
    # Next day: the due date was already reminded.
    run(db, settings, notifier, "2026-03-11T09:00:00+00:00")
    assert len(notifier.sent) == 1
    assert [row.status for row in log_rows(db)] == ["sent"]


def test_digest_time_follows_the_workspace_time_zone(db, settings, notifier) -> None:
    with db.factory.begin() as session:
        workspace = session.get(Workspace, db.workspace_id)
        assert workspace is not None
        workspace.timezone = "Europe/Warsaw"
    add_channel(db, settings, notifier)
    add_occurrence(db, date(2026, 3, 12))
    # 08:00 in Warsaw (UTC+1 in March, before the change to summer time) is 07:00 UTC.
    assert run(db, settings, notifier, "2026-03-10T06:59:00+00:00").channels == 0
    assert run(db, settings, notifier, "2026-03-10T07:00:00+00:00").channels == 1
    add_occurrence(db, date(2026, 3, 13), label="Phone bill")
    # 23:30 UTC is already 11 March in Warsaw, but before 08:00 there.
    assert run(db, settings, notifier, "2026-03-10T23:30:00+00:00").channels == 0
    assert run(db, settings, notifier, "2026-03-11T07:00:00+00:00").channels == 1
    assert "Phone bill" in notifier.sent[-1].body


def test_catch_up_after_downtime_and_overdue_cap(db, settings, notifier) -> None:
    add_channel(db, settings, notifier, notify_due_soon=False)
    add_occurrence(db, date(2026, 3, 1))
    for day in range(2, 20):  # the server was down until 23:00 on some days: still runs
        run(db, settings, notifier, f"2026-03-{day:02d}T23:00:00+00:00")
    assert len(notifier.sent) == 7
    assert all("Overdue since" in message.body for message in notifier.sent)


def test_failures_are_retried_by_the_next_daily_run(db, settings, notifier) -> None:
    channel_id = add_channel(db, settings, notifier)
    add_occurrence(db, date(2026, 3, 12))
    notifier.fail_with = "delivery_failed"
    stats = run(db, settings, notifier, "2026-03-10T08:00:00+00:00")
    assert (stats.sent, stats.failed) == (0, 1)
    assert [(row.status, row.error) for row in log_rows(db)] == [("failed", "delivery_failed")]
    assert stored(db, channel_id).last_error == "delivery_failed"
    notifier.fail_with = None
    run(db, settings, notifier, "2026-03-10T12:00:00+00:00")  # same day: no retry yet
    assert notifier.sent == []
    run(db, settings, notifier, "2026-03-11T08:00:00+00:00")
    assert len(notifier.sent) == 1
    assert [row.status for row in log_rows(db)] == ["sent"]  # the same slot row, updated
    assert stored(db, channel_id).last_error is None


def test_one_failing_channel_does_not_stop_the_others(db, settings, notifier, caplog) -> None:
    add_channel(db, settings, notifier, name="Broken", url="ntfys://broken.example/x")
    add_channel(db, settings, notifier, name="Working")
    add_occurrence(db, date(2026, 3, 12))
    notifier.fail_with = "timeout"
    notifier.failing_urls = {"ntfys://broken.example/x"}
    with caplog.at_level(logging.WARNING):
        stats = run(db, settings, notifier, "2026-03-10T08:00:00+00:00")
    assert (stats.channels, stats.sent, stats.failed) == (2, 1, 1)
    assert [message.url for message in notifier.sent] == [URL]
    assert "broken.example" not in caplog.text


def test_an_exception_in_one_channel_is_logged_and_the_run_continues(
    db, settings, notifier, monkeypatch, caplog
) -> None:
    first = add_channel(db, settings, notifier, name="First")
    add_channel(db, settings, notifier, name="Second")
    add_occurrence(db, date(2026, 3, 12))
    original = notifications._claim

    def flaky(factory, scope, clock, settings, channel_id):
        if channel_id == first:
            raise RuntimeError("boom")
        return original(factory, scope, clock, settings, channel_id)

    monkeypatch.setattr(notifications, "_claim", flaky)
    with caplog.at_level(logging.ERROR):
        stats = run(db, settings, notifier, "2026-03-10T08:00:00+00:00")
    assert stats.sent == 1
    assert "notification_run_failed" in caplog.text and "RuntimeError" in caplog.text


def test_an_interrupted_send_counts_as_failed_and_is_retried(db, settings, notifier) -> None:
    channel_id = add_channel(db, settings, notifier)
    occurrence = add_occurrence(db, date(2026, 3, 12))
    with db.factory.begin() as session:
        session.add(
            NotificationLog(
                workspace_id=db.workspace_id,
                channel_id=channel_id,
                kind="due_soon",
                subject_key=str(occurrence),
                occurrence_id=occurrence,
                sent_on=date(2026, 3, 12),
                status="sending",
                attempted_at=FixedClock("2026-03-09T08:00:00+00:00").now(),
            )
        )
    run(db, settings, notifier, "2026-03-10T08:00:00+00:00")
    assert len(notifier.sent) == 1
    assert [row.status for row in log_rows(db)] == ["sent"]


def test_digest_versus_per_item_and_amount_opt_in(db, settings, notifier) -> None:
    add_channel(db, settings, notifier, name="Digest", include_amounts=True)
    add_channel(
        db, settings, notifier, name="Items", url="gotifys://g.example/token", mode="per_item"
    )
    add_occurrence(db, date(2026, 3, 11), label="Rent")
    add_occurrence(db, date(2026, 3, 12), label="Insurance")
    run(db, settings, notifier, "2026-03-10T08:00:00+00:00")
    digest = [message for message in notifier.sent if message.url == URL]
    items = [message for message in notifier.sent if message.url != URL]
    assert len(digest) == 1 and "2 reminders" in digest[0].title
    assert "€1,234.56" in digest[0].body
    assert len(items) == 2 and all("1 reminder" in message.title for message in items)
    assert all("1,234.56" not in message.body for message in items)


def test_item_lead_days_win_over_the_channel_default(db, settings, notifier) -> None:
    add_channel(db, settings, notifier, default_lead_days=1)
    add_occurrence(db, date(2026, 3, 20), lead=10, label="Insurance")
    add_occurrence(db, date(2026, 3, 20), label="Rent")
    run(db, settings, notifier, "2026-03-10T08:00:00+00:00")
    [message] = notifier.sent
    assert "Insurance" in message.body and "Rent" not in message.body


def test_work_log_reminder_on_the_first_of_the_month(db, settings, notifier) -> None:
    add_channel(db, settings, notifier)
    with db.factory.begin() as session:
        category = make_category(session, db.workspace_id)
        session.add(category)
        session.flush()
        source = IncomeSource(
            workspace_id=db.workspace_id,
            name="Consulting",
            kind="variable",
            category_id=category.id,
            currency="EUR",
            rate_minor=100000,
            unit_label="day",
            pay_day=10,
            pay_lag_months=1,
            expected_mode="manual",
            average_months=3,
            record_as="net",
            active_from=date(2026, 1, 1),
        )
        session.add(source)
        session.flush()
        session.add(
            WorkLog(
                workspace_id=db.workspace_id,
                income_source_id=source.id,
                work_month=date(2026, 3, 1),
                units=Decimal("20"),
            )
        )
    run(db, settings, notifier, "2026-04-02T08:00:00+00:00")
    assert notifier.sent == []  # not the first: no work-log reminder
    run(db, settings, notifier, "2026-05-01T08:00:00+00:00")
    [message] = notifier.sent
    assert "Enter days worked for April 2026 for Consulting." in message.body


def test_inactive_users_and_disabled_channels_get_nothing(db, settings, notifier) -> None:
    add_channel(db, settings, notifier, "ana")
    add_channel(db, settings, notifier, "ben", enabled=False)
    add_occurrence(db, date(2026, 3, 12))
    with db.factory.begin() as session:
        user = session.get(User, db.users["ana"])
        assert user is not None
        user.is_active = False
    stats = run(db, settings, notifier, "2026-03-10T08:00:00+00:00")
    assert stats.sent == 0 and notifier.sent == []


def test_kill_switch_and_time_budget(db, settings, notifier) -> None:
    add_channel(db, settings, notifier, name="One")
    add_channel(db, settings, notifier, name="Two")
    add_occurrence(db, date(2026, 3, 12))
    off = Settings(
        _env_file=None, data_dir=settings.data_dir, secret_key="x", notifications_enabled=False
    )
    assert run(db, off, notifier, "2026-03-10T08:00:00+00:00").channels == 0
    calls = iter([False, True])
    stats = run(
        db, settings, notifier, "2026-03-10T08:00:00+00:00", out_of_time=lambda: next(calls)
    )
    assert (stats.channels, stats.deferred) == (1, 1)
    # The deferred channel runs on the next tick.
    assert run(db, settings, notifier, "2026-03-10T08:01:00+00:00").channels == 1
    assert len(notifier.sent) == 2


def test_a_scheme_removed_from_the_allowlist_is_not_sent(db, settings, notifier) -> None:
    channel_id = add_channel(db, settings, notifier)
    add_occurrence(db, date(2026, 3, 12))
    narrow = Settings(
        _env_file=None,
        data_dir=settings.data_dir,
        secret_key=settings.secret_key,
        notification_schemes="gotifys",
    )
    stats = run(db, narrow, notifier, "2026-03-10T08:00:00+00:00")
    assert stats.failed == 1 and notifier.sent == []
    assert stored(db, channel_id).last_error == "scheme_not_allowed"


def test_log_pruning_keeps_sent_overdue_rows(db, settings, notifier) -> None:
    channel_id = add_channel(db, settings, notifier)
    attempted = FixedClock("2026-01-01T08:00:00+00:00").now()
    with db.factory.begin() as session:
        for kind, status in (("overdue", "sent"), ("overdue", "failed"), ("due_soon", "sent")):
            session.add(
                NotificationLog(
                    workspace_id=db.workspace_id,
                    channel_id=channel_id,
                    kind=kind,
                    subject_key=f"{kind}-{status}",
                    sent_on=date(2026, 1, 1),
                    status=status,
                    attempted_at=attempted,
                )
            )
    run(db, settings, notifier, "2026-03-10T08:00:00+00:00")
    assert [(row.kind, row.status) for row in log_rows(db)] == [("overdue", "sent")]
