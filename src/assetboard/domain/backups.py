"""Pure backup retention and schedule rules (F9.4).

No I/O and no clock access: callers pass the timestamps they read from the injected ``Clock``.
Retention is grandfather-father-son, counted rather than age-based: the newest backup of each
day, ISO week and month is kept, up to the configured counts. One file can be the representative
of several buckets, so 7 daily, 4 weekly and 6 monthly backups can share files where the same
backup is the newest of its day, week and month. Manual and pre-restore copies are never pruned
automatically.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

DEFAULT_DAILY_RETENTION = 7
DEFAULT_WEEKLY_RETENTION = 4
DEFAULT_MONTHLY_RETENTION = 6
MAX_RETENTION = 365
# The daily scheduled run. UTC keeps it independent of every workspace's time zone.
BACKUP_HOUR_UTC = 3
BACKUP_MINUTE_UTC = 30
SCHEDULED_INTERVAL = timedelta(hours=24)

BackupTrigger = Literal["scheduled", "manual", "pre_restore"]
SCHEDULED = "scheduled"


def validate_retention(value: int) -> int:
    """A retention count: zero to ``MAX_RETENTION`` inclusive."""
    if isinstance(value, bool):
        raise ValueError("retention must not be a boolean")
    if not 0 <= value <= MAX_RETENTION:
        raise ValueError(f"retention must be between 0 and {MAX_RETENTION}")
    return value


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    daily: int = DEFAULT_DAILY_RETENTION
    weekly: int = DEFAULT_WEEKLY_RETENTION
    monthly: int = DEFAULT_MONTHLY_RETENTION

    def __post_init__(self) -> None:
        for name in ("daily", "weekly", "monthly"):
            object.__setattr__(self, name, validate_retention(getattr(self, name)))


@dataclass(frozen=True, slots=True)
class BackupRef:
    """One finished backup that may take part in retention."""

    key: str
    started_at: datetime
    trigger: str = SCHEDULED
    path: str | None = None


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    keep: frozenset[str]
    delete: frozenset[str]


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("backup timestamps must be timezone-aware")
    return moment.astimezone(UTC)


def _day_bucket(moment: datetime) -> date:
    return _aware(moment).date()


def _week_bucket(moment: datetime) -> tuple[int, int]:
    iso = _aware(moment).isocalendar()
    return iso.year, iso.week


def _month_bucket(moment: datetime) -> tuple[int, int]:
    local = _aware(moment)
    return local.year, local.month


def plan_retention(runs: Iterable[BackupRef], policy: RetentionPolicy) -> RetentionPlan:
    """Which scheduled backups to keep and which to delete.

    Manual and pre-restore backups, and runs without a file, are left alone. Sorting is stable
    and descending so the newest row of a bucket wins; equal timestamps fall back to the key so
    the result is deterministic.
    """
    eligible = [
        run for run in runs if run.trigger == SCHEDULED and run.path is not None and run.path
    ]
    ordered = sorted(eligible, key=lambda run: (_aware(run.started_at), run.key), reverse=True)
    keep: set[str] = set()
    for limit, bucket in (
        (policy.daily, _day_bucket),
        (policy.weekly, _week_bucket),
        (policy.monthly, _month_bucket),
    ):
        if limit <= 0:
            continue
        seen: set[object] = set()
        for run in ordered:
            marker = bucket(run.started_at)
            if marker in seen:
                continue
            seen.add(marker)
            keep.add(run.key)
            if len(seen) >= limit:
                break
    delete = {run.key for run in eligible if run.key not in keep}
    return RetentionPlan(frozenset(keep), frozenset(delete))


def backup_due(
    last_started_at: datetime | None,
    now: datetime,
    *,
    interval: timedelta = SCHEDULED_INTERVAL,
) -> bool:
    """Whether the scheduled backup should run: none yet, or the last one is a day old."""
    moment = _aware(now)
    if last_started_at is None:
        return True
    elapsed = moment - _aware(last_started_at)
    return elapsed >= interval


def next_scheduled_at(now: datetime) -> datetime:
    """The next 03:30 UTC backup time after ``now``."""
    moment = _aware(now)
    candidate = moment.replace(
        hour=BACKUP_HOUR_UTC,
        minute=BACKUP_MINUTE_UTC,
        second=0,
        microsecond=0,
    )
    if candidate <= moment:
        candidate += timedelta(days=1)
    return candidate
