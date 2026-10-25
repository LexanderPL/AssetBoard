from __future__ import annotations

from datetime import date, datetime, time, timedelta
from uuid import UUID, uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from assetboard.domain.money import Money
from assetboard.domain.notifications import (
    OVERDUE_MAX_REMINDERS,
    PER_ITEM_CAP,
    ChannelRules,
    DeliveryMode,
    LogStatus,
    PendingOccurrence,
    Reminder,
    ReminderKind,
    WorkSource,
    batches,
    channel_is_due,
    plan_reminders,
    work_log_key,
)

TODAY = date(2026, 3, 10)
EUR = Money(12345, "EUR")


def occurrence(due: date, *, lead: int | None = None, **values: object) -> PendingOccurrence:
    return PendingOccurrence(
        id=uuid4(),
        label="Rent",
        due_date=due,
        amount=EUR,
        lead_days=lead,
        **values,  # type: ignore[arg-type]
    )


def kinds(reminders: list[Reminder]) -> list[str]:
    return [reminder.kind.value for reminder in reminders]


@pytest.mark.parametrize(
    ("due", "lead", "default", "expected"),
    [
        (TODAY, None, 3, ["due_soon"]),  # due today is "due soon"
        (TODAY + timedelta(days=3), None, 3, ["due_soon"]),  # first day of the window
        (TODAY + timedelta(days=4), None, 3, []),  # not yet
        (TODAY + timedelta(days=4), 5, 3, ["due_soon"]),  # the item's own lead wins
        (TODAY + timedelta(days=2), 0, 3, []),  # lead 0: only on the due date
        (TODAY, 0, 3, ["due_soon"]),
        (TODAY + timedelta(days=10), None, 10, ["due_soon"]),  # channel default
        (TODAY - timedelta(days=1), None, 3, ["overdue"]),
    ],
)
def test_due_soon_window_and_lead_precedence(
    due: date, lead: int | None, default: int, expected: list[str]
) -> None:
    plan = plan_reminders(
        TODAY, [occurrence(due, lead=lead)], [], ChannelRules(default_lead_days=default), {}, {}
    )
    assert kinds(plan) == expected


def test_due_soon_slot_is_the_due_date_so_a_due_date_is_reminded_once() -> None:
    row = occurrence(TODAY + timedelta(days=2))
    [first] = plan_reminders(TODAY, [row], [], ChannelRules(), {}, {})
    assert first.slot == row.due_date and first.subject_key == str(row.id)
    logged = {first.key: LogStatus.SENT}
    tomorrow = TODAY + timedelta(days=1)
    assert plan_reminders(tomorrow, [row], [], ChannelRules(), logged, {}) == []
    # Claimed by a run that has not finished: not planned again either.
    assert (
        plan_reminders(tomorrow, [row], [], ChannelRules(), {first.key: LogStatus.SENDING}, {})
        == []
    )
    # A failed attempt is retried by the next run.
    retried = plan_reminders(tomorrow, [row], [], ChannelRules(), {first.key: LogStatus.FAILED}, {})
    assert [reminder.key for reminder in retried] == [first.key]


def test_a_rescheduled_occurrence_is_reminded_for_its_new_date() -> None:
    row = occurrence(TODAY + timedelta(days=1))
    [first] = plan_reminders(TODAY, [row], [], ChannelRules(), {}, {})
    moved = PendingOccurrence(row.id, row.label, TODAY + timedelta(days=2), row.amount)
    plan = plan_reminders(TODAY, [moved], [], ChannelRules(), {first.key: LogStatus.SENT}, {})
    assert [reminder.slot for reminder in plan] == [moved.due_date]


def test_overdue_once_a_day_and_at_most_seven_times() -> None:
    row = occurrence(TODAY - timedelta(days=20))
    [reminder] = plan_reminders(TODAY, [row], [], ChannelRules(), {}, {})
    assert reminder.kind is ReminderKind.OVERDUE and reminder.slot == TODAY
    assert (
        plan_reminders(TODAY, [row], [], ChannelRules(), {reminder.key: LogStatus.SENT}, {}) == []
    )
    key = str(row.id)
    almost = plan_reminders(TODAY, [row], [], ChannelRules(), {}, {key: OVERDUE_MAX_REMINDERS - 1})
    assert kinds(almost) == ["overdue"]
    assert plan_reminders(TODAY, [row], [], ChannelRules(), {}, {key: OVERDUE_MAX_REMINDERS}) == []


def test_overdue_cap_counts_only_sent_reminders_so_failures_are_retried() -> None:
    """The cap is fed with sent counts only: a channel that failed for a week still sends."""
    row = occurrence(TODAY - timedelta(days=9))
    logged = {
        (ReminderKind.OVERDUE.value, str(row.id), TODAY - timedelta(days=day)): LogStatus.FAILED
        for day in range(1, 8)
    }
    assert kinds(plan_reminders(TODAY, [row], [], ChannelRules(), logged, {})) == ["overdue"]


def test_toggles_switch_kinds_off() -> None:
    rows = [occurrence(TODAY - timedelta(days=1)), occurrence(TODAY + timedelta(days=1))]
    assert kinds(plan_reminders(TODAY, rows, [], ChannelRules(overdue=False), {}, {})) == [
        "due_soon"
    ]
    assert kinds(plan_reminders(TODAY, rows, [], ChannelRules(due_soon=False), {}, {})) == [
        "overdue"
    ]


def source(*logged: date, active_to: date | None = None) -> WorkSource:
    return WorkSource(uuid4(), "Consulting", date(2025, 1, 1), active_to, frozenset(logged))


def test_work_log_reminder_on_the_first_day_of_the_month_for_the_month_just_ended() -> None:
    first = date(2026, 3, 1)
    consulting = source()
    [reminder] = plan_reminders(first, [], [consulting], ChannelRules(), {}, {})
    assert reminder.kind is ReminderKind.WORK_LOG
    assert reminder.work_month == date(2026, 2, 1)
    assert reminder.slot == first
    assert reminder.subject_key == work_log_key(consulting.id, date(2026, 2, 1))
    assert reminder.amount is None
    # Not on other days, and not once logged or when the source ended before that month.
    assert plan_reminders(date(2026, 3, 2), [], [consulting], ChannelRules(), {}, {}) == []
    logged = source(date(2026, 2, 1))
    assert plan_reminders(first, [], [logged], ChannelRules(), {}, {}) == []
    ended = source(active_to=date(2026, 1, 31))
    assert plan_reminders(first, [], [ended], ChannelRules(), {}, {}) == []
    assert plan_reminders(first, [], [consulting], ChannelRules(work_logs=False), {}, {}) == []


def test_a_failed_first_of_month_reminder_is_retried_on_a_later_day() -> None:
    consulting = source()
    [reminder] = plan_reminders(date(2026, 3, 1), [], [consulting], ChannelRules(), {}, {})
    later = date(2026, 3, 2)
    retry = plan_reminders(
        later, [], [consulting], ChannelRules(), {reminder.key: LogStatus.FAILED}, {}
    )
    assert [row.key for row in retry] == [reminder.key]
    sent = {reminder.key: LogStatus.SENT}
    assert plan_reminders(later, [], [consulting], ChannelRules(), sent, {}) == []


def test_work_log_reminder_three_days_before_an_estimated_income_occurrence() -> None:
    consulting = source()
    due = TODAY + timedelta(days=3)
    income = occurrence(
        due,
        income_source_id=consulting.id,
        work_month=date(2026, 2, 1),
        estimated=True,
    )
    rules = ChannelRules(due_soon=False)
    [reminder] = plan_reminders(TODAY, [income], [consulting], rules, {}, {})
    assert reminder.kind is ReminderKind.WORK_LOG
    assert reminder.slot == TODAY and reminder.due_date == due
    assert reminder.work_month == date(2026, 2, 1)
    # Four days before: not yet. Confirmed (logged): never.
    early = plan_reminders(TODAY - timedelta(days=1), [income], [consulting], rules, {}, {})
    assert early == []
    logged = source(date(2026, 2, 1))
    confirmed = occurrence(due, income_source_id=logged.id, work_month=date(2026, 2, 1))
    assert plan_reminders(TODAY, [confirmed], [logged], rules, {}, {}) == []
    # Once sent, not again.
    assert (
        plan_reminders(
            TODAY + timedelta(days=1),
            [income],
            [consulting],
            rules,
            {reminder.key: LogStatus.SENT},
            {},
        )
        == []
    )


def test_two_work_log_triggers_on_the_same_day_send_one_reminder() -> None:
    first = date(2026, 4, 1)
    consulting = source()
    income = occurrence(
        date(2026, 4, 4),
        income_source_id=consulting.id,
        work_month=date(2026, 3, 1),
        estimated=True,
    )
    plan = plan_reminders(first, [income], [consulting], ChannelRules(due_soon=False), {}, {})
    assert kinds(plan) == ["work_log"]


def test_order_is_overdue_then_due_soon_then_work_logs() -> None:
    consulting = source()
    first = date(2026, 3, 1)
    rows = [occurrence(first + timedelta(days=1)), occurrence(first - timedelta(days=2))]
    plan = plan_reminders(first, rows, [consulting], ChannelRules(), {}, {})
    assert kinds(plan) == ["overdue", "due_soon", "work_log"]


@pytest.mark.parametrize(
    ("now", "digest", "last_run", "expected"),
    [
        (datetime(2026, 3, 10, 7, 59), time(8, 0), None, False),
        (datetime(2026, 3, 10, 8, 0), time(8, 0), None, True),
        (datetime(2026, 3, 10, 23, 0), time(8, 0), date(2026, 3, 9), True),  # catch-up
        (datetime(2026, 3, 10, 9, 0), time(8, 0), date(2026, 3, 10), False),  # ran today
        # Spring forward in Europe/Warsaw: 02:30 does not exist; the first tick after it runs.
        (datetime(2026, 3, 29, 3, 0), time(2, 30), date(2026, 3, 28), True),
    ],
)
def test_channel_is_due_once_per_local_day(
    now: datetime, digest: time, last_run: date | None, expected: bool
) -> None:
    assert channel_is_due(now, digest, last_run) is expected


def test_batches_digest_and_per_item_with_a_cap() -> None:
    reminders = [
        Reminder(ReminderKind.OVERDUE, str(index), TODAY, f"Item {index}")
        for index in range(PER_ITEM_CAP + 3)
    ]
    assert batches([], DeliveryMode.DIGEST) == []
    assert batches(reminders, DeliveryMode.DIGEST) == [tuple(reminders)]
    per_item = batches(reminders, DeliveryMode.PER_ITEM)
    assert len(per_item) == PER_ITEM_CAP + 1
    assert all(len(group) == 1 for group in per_item[:PER_ITEM_CAP])
    assert per_item[-1] == tuple(reminders[PER_ITEM_CAP:])
    assert batches(reminders[:2], DeliveryMode.PER_ITEM) == [(reminders[0],), (reminders[1],)]


# Properties


def _ids() -> st.SearchStrategy[UUID]:
    return st.uuids()


occurrences_strategy = st.lists(
    st.builds(
        PendingOccurrence,
        id=_ids(),
        label=st.just("Item"),
        due_date=st.dates(date(2026, 1, 1), date(2026, 6, 30)),
        amount=st.just(EUR),
        lead_days=st.one_of(st.none(), st.integers(0, 60)),
    ),
    max_size=8,
    unique_by=lambda row: row.id,
)


@settings(max_examples=150, deadline=None)
@given(
    rows=occurrences_strategy,
    start=st.dates(date(2026, 1, 1), date(2026, 6, 30)),
    days=st.integers(1, 40),
    default=st.integers(0, 60),
)
def test_replaying_daily_runs_never_repeats_a_slot_and_caps_overdue(
    rows: list[PendingOccurrence], start: date, days: int, default: int
) -> None:
    """Simulate one run per day with every send succeeding: each slot is sent at most once,
    every occurrence gets at most one due-soon reminder per due date and at most seven overdue
    reminders."""
    logged: dict[tuple[str, str, date], LogStatus] = {}
    overdue_sent: dict[str, int] = {}
    sent: list[Reminder] = []
    rules = ChannelRules(default_lead_days=default)
    for offset in range(days):
        today = start + timedelta(days=offset)
        plan = plan_reminders(today, rows, [], rules, logged, overdue_sent)
        assert len({reminder.key for reminder in plan}) == len(plan)
        for reminder in plan:
            assert reminder.key not in logged
            logged[reminder.key] = LogStatus.SENT
            if reminder.kind is ReminderKind.OVERDUE:
                overdue_sent[reminder.subject_key] = overdue_sent.get(reminder.subject_key, 0) + 1
            sent.append(reminder)
    for row in rows:
        mine = [reminder for reminder in sent if reminder.occurrence_id == row.id]
        assert sum(reminder.kind is ReminderKind.DUE_SOON for reminder in mine) <= 1
        assert sum(reminder.kind is ReminderKind.OVERDUE for reminder in mine) <= 7
        for reminder in mine:
            if reminder.kind is ReminderKind.DUE_SOON:
                assert reminder.slot == row.due_date
            else:
                assert reminder.slot > row.due_date


@settings(max_examples=100, deadline=None)
@given(count=st.integers(0, 40), mode=st.sampled_from(list(DeliveryMode)))
def test_batches_keep_every_reminder_once_in_order(count: int, mode: DeliveryMode) -> None:
    reminders = [Reminder(ReminderKind.OVERDUE, str(index), TODAY, "x") for index in range(count)]
    groups = batches(reminders, mode)
    assert [reminder for group in groups for reminder in group] == reminders
    assert len(groups) <= (1 if mode is DeliveryMode.DIGEST else PER_ITEM_CAP + 1)
