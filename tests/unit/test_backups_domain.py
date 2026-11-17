"""Pure backup retention and schedule rules (F9.4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from assetboard.domain.backups import (
    BackupRef,
    RetentionPolicy,
    backup_due,
    next_scheduled_at,
    plan_retention,
    validate_retention,
)


def _run(key: str, moment: datetime, *, trigger: str = "scheduled", path: str | None = None):
    return BackupRef(key=key, started_at=moment, trigger=trigger, path=path or f"{key}.db")


def test_retention_defaults_are_seven_daily_four_weekly_six_monthly() -> None:
    policy = RetentionPolicy()
    assert (policy.daily, policy.weekly, policy.monthly) == (7, 4, 6)


def test_retention_counts_are_validated() -> None:
    assert validate_retention(0) == 0
    assert validate_retention(365) == 365
    for invalid in (-1, 366, True):
        with pytest.raises(ValueError):
            validate_retention(invalid)


def test_daily_week_and_month_buckets_keep_the_newest_run_of_each() -> None:
    start = datetime(2026, 1, 1, 3, 30, tzinfo=UTC)
    runs = [_run(f"day-{index}", start + timedelta(days=index)) for index in range(20)]
    plan = plan_retention(runs, RetentionPolicy(daily=7, weekly=4, monthly=6))
    # Seven daily, then the older representative of each of the four weeks in range: the
    # newest weekly entries are already kept by the daily rule.
    assert plan.keep == {
        "day-3",
        "day-10",
        "day-13",
        "day-14",
        "day-15",
        "day-16",
        "day-17",
        "day-18",
        "day-19",
    }
    assert plan.delete == {
        "day-0",
        "day-1",
        "day-2",
        "day-4",
        "day-5",
        "day-6",
        "day-7",
        "day-8",
        "day-9",
        "day-11",
        "day-12",
    }


def test_one_file_can_represent_a_day_and_a_week_and_a_month() -> None:
    moment = datetime(2026, 1, 1, 3, 30, tzinfo=UTC)
    plan = plan_retention([_run("only", moment)], RetentionPolicy())
    assert plan.keep == {"only"}
    assert plan.delete == frozenset()


def test_weekly_and_monthly_rules_each_keep_their_own_newest_entries() -> None:
    start = datetime(2025, 12, 1, 3, 30, tzinfo=UTC)
    weeks = [_run(f"week-{index}", start + timedelta(weeks=index)) for index in range(8)]
    weekly = plan_retention(weeks, RetentionPolicy(daily=0, weekly=4, monthly=0))
    assert weekly.keep == {"week-4", "week-5", "week-6", "week-7"}
    assert weekly.delete == {f"week-{index}" for index in range(4)}

    months = [_run(f"month-{index}", start + timedelta(days=31 * index)) for index in range(8)]
    monthly = plan_retention(months, RetentionPolicy(daily=0, weekly=0, monthly=6))
    assert monthly.keep == {f"month-{index}" for index in range(2, 8)}
    assert monthly.delete == {"month-0", "month-1"}


def test_zero_counts_delete_every_scheduled_copy() -> None:
    moment = datetime(2026, 1, 1, 3, 30, tzinfo=UTC)
    plan = plan_retention([_run("a", moment)], RetentionPolicy(0, 0, 0))
    assert plan.keep == frozenset()
    assert plan.delete == {"a"}


def test_manual_and_pre_restore_copies_are_never_pruned() -> None:
    moment = datetime(2026, 1, 1, 3, 30, tzinfo=UTC)
    runs = [
        _run("scheduled", moment),
        _run("manual", moment - timedelta(days=100), trigger="manual"),
        _run("pre-restore", moment - timedelta(days=200), trigger="pre_restore"),
    ]
    plan = plan_retention(runs, RetentionPolicy(1, 0, 0))
    assert plan.keep == {"scheduled"}
    assert plan.delete == frozenset()


def test_runs_without_a_file_are_ignored() -> None:
    moment = datetime(2026, 1, 1, 3, 30, tzinfo=UTC)
    plan = plan_retention(
        [
            BackupRef(key="failed", started_at=moment, path=None),
            BackupRef(key="no-path", started_at=moment, path=""),
            _run("ok", moment),
        ],
        RetentionPolicy(1, 0, 0),
    )
    assert plan.keep == {"ok"}
    assert plan.delete == frozenset()


def test_equal_timestamps_are_deterministic_and_keep_the_newest_key() -> None:
    moment = datetime(2026, 1, 1, 3, 30, tzinfo=UTC)
    plan = plan_retention(
        [_run("a", moment), _run("b", moment), _run("c", moment)],
        RetentionPolicy(1, 0, 0),
    )
    assert plan.keep == {"c"}
    assert plan.delete == {"a", "b"}


def test_backup_due_needs_a_day_or_no_previous_run() -> None:
    now = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    assert backup_due(None, now) is True
    assert backup_due(now - timedelta(hours=23), now) is False
    assert backup_due(now - timedelta(hours=24), now) is True
    assert backup_due(now + timedelta(hours=1), now) is False


def test_next_scheduled_time_is_the_next_0330_utc() -> None:
    before = datetime(2026, 3, 1, 3, 29, tzinfo=UTC)
    after = datetime(2026, 3, 1, 3, 30, tzinfo=UTC)
    assert next_scheduled_at(before).isoformat() == "2026-03-01T03:30:00+00:00"
    assert next_scheduled_at(after).isoformat() == "2026-03-02T03:30:00+00:00"
    # Another zone: the result is still the same UTC instant.
    warsaw = datetime(2026, 3, 1, 12, 0, tzinfo=UTC).astimezone()
    assert next_scheduled_at(warsaw).isoformat() == "2026-03-02T03:30:00+00:00"


def test_naive_timestamps_are_refused() -> None:
    with pytest.raises(ValueError):
        backup_due(None, datetime(2026, 1, 1, 0, 0))
    with pytest.raises(ValueError):
        plan_retention(
            [BackupRef("x", datetime(2026, 1, 1, 0, 0), "scheduled", "x.db")],
            RetentionPolicy(),
        )
