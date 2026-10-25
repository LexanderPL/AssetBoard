"""Which reminders a channel sends today (F3.6). Pure: no I/O, no clock, no database.

Three kinds of reminder, each identified by ``(kind, subject_key, slot)``:

- ``due_soon``: a pending occurrence whose due date is within its lead days (the item's own
  lead, else the channel default). The slot is the due date, so a due date is reminded once
  and a rescheduled occurrence once more.
- ``overdue``: a pending occurrence due before today. The slot is today (at most once a day),
  and only while fewer than ``OVERDUE_MAX_REMINDERS`` were sent for it.
- ``work_log``: a variable income source without a work log. On the first day of a month for
  the month just ended (slot: that first day), and from three days before an estimated income
  occurrence until its due date for that occurrence's work month (slot: due date minus three).

A slot already ``sent`` or being sent is never planned again; a ``failed`` slot is planned
again (the next daily run retries it).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from uuid import UUID

from assetboard.domain.money import Money

MAX_LEAD_DAYS = 60
DEFAULT_LEAD_DAYS = 3
DEFAULT_DIGEST_TIME = time(8, 0)
OVERDUE_MAX_REMINDERS = 7
WORK_LOG_LEAD_DAYS = 3
PER_ITEM_CAP = 10
MAX_CHANNEL_NAME_LENGTH = 100


class ReminderKind(StrEnum):
    DUE_SOON = "due_soon"
    OVERDUE = "overdue"
    WORK_LOG = "work_log"


class DeliveryMode(StrEnum):
    DIGEST = "digest"
    PER_ITEM = "per_item"


class LogStatus(StrEnum):
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PendingOccurrence:
    id: UUID
    label: str
    due_date: date
    amount: Money
    lead_days: int | None = None  # the recurring item's own lead; None: channel default
    income_source_id: UUID | None = None
    work_month: date | None = None
    estimated: bool = False


@dataclass(frozen=True, slots=True)
class WorkSource:
    """An active variable income source and the work months it has logs for."""

    id: UUID
    name: str
    active_from: date
    active_to: date | None
    logged_months: frozenset[date]


@dataclass(frozen=True, slots=True)
class ChannelRules:
    default_lead_days: int = DEFAULT_LEAD_DAYS
    due_soon: bool = True
    overdue: bool = True
    work_logs: bool = True


@dataclass(frozen=True, slots=True)
class Reminder:
    kind: ReminderKind
    subject_key: str
    slot: date
    label: str
    due_date: date | None = None
    work_month: date | None = None
    occurrence_id: UUID | None = None
    amount: Money | None = None

    @property
    def key(self) -> tuple[str, str, date]:
        return (self.kind.value, self.subject_key, self.slot)


LogKey = tuple[str, str, date]


def work_log_key(source_id: UUID, work_month: date) -> str:
    return f"worklog:{source_id}:{work_month:%Y-%m}"


def _previous_month(day: date) -> date:
    return (day.replace(day=1) - timedelta(days=1)).replace(day=1)


def _month_end(first: date) -> date:
    return (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def _active_in(source: WorkSource, work_month: date) -> bool:
    return source.active_from <= _month_end(work_month) and (
        source.active_to is None or source.active_to >= work_month
    )


def _open(logged: Mapping[LogKey, LogStatus], key: LogKey) -> bool:
    """Not yet sent or claimed (a failed attempt is retried)."""
    return logged.get(key) in (None, LogStatus.FAILED)


def channel_is_due(local_now: datetime, digest_time: time, last_run_on: date | None) -> bool:
    """Run once per local day, at or after the digest time (a missed time catches up).

    ``local_now`` is the wall-clock time in the workspace time zone. On a day when the clock
    skips the digest time (daylight saving), the first tick after it still runs.
    """
    today = local_now.date()
    if last_run_on is not None and last_run_on >= today:
        return False
    return local_now.time().replace(tzinfo=None) >= digest_time


def plan_reminders(
    today: date,
    occurrences: Iterable[PendingOccurrence],
    sources: Iterable[WorkSource],
    rules: ChannelRules,
    logged: Mapping[LogKey, LogStatus],
    overdue_sent: Mapping[str, int],
) -> list[Reminder]:
    """The reminders to send today: overdue first (oldest first), then due soon, then work logs.

    ``logged`` holds the status of every log row of the channel that can still matter;
    ``overdue_sent`` the number of overdue reminders already sent per occurrence key.
    """
    pending = sorted(occurrences, key=lambda row: (row.due_date, str(row.id)))
    work_sources = {source.id: source for source in sources}
    overdue: list[Reminder] = []
    due_soon: list[Reminder] = []
    work_logs: dict[LogKey, Reminder] = {}

    for occurrence in pending:
        subject = str(occurrence.id)
        if occurrence.due_date < today:
            if not rules.overdue or overdue_sent.get(subject, 0) >= OVERDUE_MAX_REMINDERS:
                continue
            reminder = Reminder(
                ReminderKind.OVERDUE,
                subject,
                today,
                occurrence.label,
                due_date=occurrence.due_date,
                occurrence_id=occurrence.id,
                amount=occurrence.amount,
            )
            if _open(logged, reminder.key):
                overdue.append(reminder)
            continue
        lead = occurrence.lead_days if occurrence.lead_days is not None else rules.default_lead_days
        if rules.due_soon and occurrence.due_date - timedelta(days=lead) <= today:
            reminder = Reminder(
                ReminderKind.DUE_SOON,
                subject,
                occurrence.due_date,
                occurrence.label,
                due_date=occurrence.due_date,
                occurrence_id=occurrence.id,
                amount=occurrence.amount,
            )
            if _open(logged, reminder.key):
                due_soon.append(reminder)
        if (
            rules.work_logs
            and occurrence.estimated
            and occurrence.income_source_id is not None
            and occurrence.work_month is not None
            and occurrence.due_date - timedelta(days=WORK_LOG_LEAD_DAYS) <= today
        ):
            source = work_sources.get(occurrence.income_source_id)
            if source is None or occurrence.work_month in source.logged_months:
                continue
            reminder = Reminder(
                ReminderKind.WORK_LOG,
                work_log_key(source.id, occurrence.work_month),
                occurrence.due_date - timedelta(days=WORK_LOG_LEAD_DAYS),
                source.name,
                due_date=occurrence.due_date,
                work_month=occurrence.work_month,
            )
            if _open(logged, reminder.key):
                work_logs.setdefault(reminder.key, reminder)

    if rules.work_logs:
        first = today.replace(day=1)
        work_month = _previous_month(today)
        for source in sorted(work_sources.values(), key=lambda row: (row.name, str(row.id))):
            if work_month in source.logged_months or not _active_in(source, work_month):
                continue
            key: LogKey = (ReminderKind.WORK_LOG.value, work_log_key(source.id, work_month), first)
            # On the first day of the month; a failed attempt is retried on later days.
            if today != first and logged.get(key) != LogStatus.FAILED:
                continue
            if not _open(logged, key):
                continue
            work_logs.setdefault(
                key,
                Reminder(
                    ReminderKind.WORK_LOG,
                    key[1],
                    first,
                    source.name,
                    work_month=work_month,
                ),
            )

    return overdue + due_soon + sorted(work_logs.values(), key=lambda row: (row.slot, row.label))


def batches(
    reminders: Sequence[Reminder], mode: DeliveryMode, cap: int = PER_ITEM_CAP
) -> list[tuple[Reminder, ...]]:
    """Group reminders into messages: one digest, or one message each up to ``cap`` and the
    rest together in one more message."""
    if not reminders:
        return []
    if mode is DeliveryMode.DIGEST:
        return [tuple(reminders)]
    singles = [(reminder,) for reminder in reminders[:cap]]
    rest = tuple(reminders[cap:])
    return singles + ([rest] if rest else [])
