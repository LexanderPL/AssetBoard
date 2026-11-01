"""F3.7 work calendar: day entries, the derived month work log and F3.4 recalculation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from assetboard.db.repositories import CurrentWorkspaceRepository, WorkLogRepository
from assetboard.domain.months import YearMonth
from assetboard.services import income
from assetboard.services.context import PermissionDenied
from assetboard.services.months import close_month
from tests.tenant.workspace_db import WorkspaceDb, workspace_db
from tests.unit.test_income_service import _occurrence, _source, categories

MARCH = date(2026, 3, 1)
MAY = date(2026, 5, 1)


@pytest.fixture
def team(tmp_path):
    for db in workspace_db(tmp_path):
        db.add_member("owner", "owner")
        db.add_member("viewer", "viewer")
        yield db


def _setup(team: WorkspaceDb, *, country: str | None = None):
    ctx = team.ctx("owner", "owner")
    income_category, _expense = categories(team, ctx)
    if country is not None:
        with team.factory.begin() as session:
            CurrentWorkspaceRepository(session, ctx).update(country_code=country)
    return ctx, _source(team, ctx, income_category)


def _log(team: WorkspaceDb, ctx, source_id, month: date):
    with team.factory() as session:
        return next(
            (
                row
                for row in WorkLogRepository(session, ctx).for_source(source_id)
                if row.work_month == month
            ),
            None,
        )


def test_toggle_marks_a_day_worked_and_recalculates_the_month(team: WorkspaceDb) -> None:
    ctx, source_id = _setup(team)
    assert not income.toggle_work_day(team.factory, ctx, source_id, date(2026, 3, 2))
    assert not income.toggle_work_day(team.factory, ctx, source_id, date(2026, 3, 3))

    calendar = income.work_calendar(team.factory, ctx, source_id, MARCH)
    worked = {day.day: day.units for day in calendar.days if day.units is not None}
    assert worked == {date(2026, 3, 2): Decimal(1), date(2026, 3, 3): Decimal(1)}
    assert calendar.total == Decimal(2) and calendar.derived
    log = _log(team, ctx, source_id, MARCH)
    assert log is not None and log.derived_from_days and log.units == Decimal(2)
    occurrence = _occurrence(team, ctx, source_id, MARCH)
    assert occurrence is not None
    assert (occurrence.expected_minor, occurrence.amount_state) == (20_000, "confirmed")

    # Toggling a worked day again removes it, and the occurrence follows.
    income.toggle_work_day(team.factory, ctx, source_id, date(2026, 3, 3))
    assert income.work_calendar(team.factory, ctx, source_id, MARCH).total == Decimal(1)
    occurrence = _occurrence(team, ctx, source_id, MARCH)
    assert occurrence is not None and occurrence.expected_minor == 10_000


def test_a_day_value_is_editable_and_the_month_is_their_sum(team: WorkspaceDb) -> None:
    ctx, source_id = _setup(team)
    income.toggle_work_day(team.factory, ctx, source_id, date(2026, 3, 2))
    income.set_work_day(team.factory, ctx, source_id, date(2026, 3, 2), Decimal("0.5"))
    income.set_work_day(team.factory, ctx, source_id, date(2026, 3, 4), Decimal("7.25"))
    log = _log(team, ctx, source_id, MARCH)
    assert log is not None and log.units == Decimal("7.75")
    occurrence = _occurrence(team, ctx, source_id, MARCH)
    assert occurrence is not None and occurrence.expected_minor == 77_500


@pytest.mark.parametrize("units", ["0", "-1", "24.01", "NaN", "0.0000000000000000001"])
def test_day_values_must_be_above_zero_and_at_most_24(team: WorkspaceDb, units: str) -> None:
    ctx, source_id = _setup(team)
    with pytest.raises(income.IncomeError) as error:
        income.set_work_day(team.factory, ctx, source_id, date(2026, 3, 2), Decimal(units))
    assert error.value.code == "invalid_day_units"


def test_the_derived_month_log_is_read_only_and_replaces_a_manual_one(team: WorkspaceDb) -> None:
    ctx, source_id = _setup(team)
    income.log_work(team.factory, ctx, source_id, MARCH, Decimal("10"))
    calendar = income.work_calendar(team.factory, ctx, source_id, MARCH)
    assert calendar.manual_units == Decimal("10") and not calendar.derived

    income.toggle_work_day(team.factory, ctx, source_id, date(2026, 3, 2))
    log = _log(team, ctx, source_id, MARCH)
    assert log is not None and log.derived_from_days and log.units == Decimal(1)
    with pytest.raises(income.IncomeError) as error:
        income.log_work(team.factory, ctx, source_id, MARCH, Decimal("12"))
    assert error.value.code == "derived_work_log"
    # Other months keep the month-level form.
    income.log_work(team.factory, ctx, source_id, MAY, Decimal("12"))


def test_removing_the_last_day_deletes_the_derived_log(team: WorkspaceDb) -> None:
    ctx, source_id = _setup(team)
    income.toggle_work_day(team.factory, ctx, source_id, date(2026, 3, 2))
    income.toggle_work_day(team.factory, ctx, source_id, date(2026, 3, 2))
    assert _log(team, ctx, source_id, MARCH) is None
    occurrence = _occurrence(team, ctx, source_id, MARCH)
    assert occurrence is not None and occurrence.amount_state == "estimated"


def test_fill_working_days_skips_weekends_and_holidays_and_never_overwrites(
    team: WorkspaceDb,
) -> None:
    ctx, source_id = _setup(team, country="PL")
    income.set_work_day(team.factory, ctx, source_id, date(2026, 5, 4), Decimal("0.5"))
    income.fill_working_days(team.factory, ctx, source_id, MAY)

    calendar = income.work_calendar(team.factory, ctx, source_id, MAY)
    by_day = {day.day: day for day in calendar.days}
    # 1 May 2026 (Friday) is a Polish public holiday; 2-3 May is a weekend.
    assert by_day[date(2026, 5, 1)].holiday and by_day[date(2026, 5, 1)].units is None
    assert by_day[date(2026, 5, 2)].weekend and by_day[date(2026, 5, 2)].units is None
    assert by_day[date(2026, 5, 4)].units == Decimal("0.5")
    assert by_day[date(2026, 5, 5)].units == Decimal(1)
    # May 2026 has 21 weekdays; without 1 May and the pre-set 4 May: 19 x 1 + 0.5.
    assert calendar.total == Decimal("19.5")
    log = _log(team, ctx, source_id, MAY)
    assert log is not None and log.units == Decimal("19.5")
    occurrence = _occurrence(team, ctx, source_id, MAY)
    assert occurrence is not None and occurrence.expected_minor == 195_000


def test_without_a_country_only_weekends_are_skipped(team: WorkspaceDb) -> None:
    ctx, source_id = _setup(team)
    income.fill_working_days(team.factory, ctx, source_id, MAY)
    calendar = income.work_calendar(team.factory, ctx, source_id, MAY)
    assert not any(day.holiday for day in calendar.days)
    assert calendar.total == Decimal(21)


def test_clear_month_removes_every_day_and_the_derived_log(team: WorkspaceDb) -> None:
    ctx, source_id = _setup(team)
    income.fill_working_days(team.factory, ctx, source_id, MARCH)
    income.toggle_work_day(team.factory, ctx, source_id, date(2026, 4, 1))
    income.clear_work_month(team.factory, ctx, source_id, MARCH)
    assert income.work_calendar(team.factory, ctx, source_id, MARCH).total == Decimal(0)
    assert _log(team, ctx, source_id, MARCH) is None
    occurrence = _occurrence(team, ctx, source_id, MARCH)
    assert occurrence is not None and occurrence.amount_state == "estimated"
    # Other months are untouched.
    assert income.work_calendar(team.factory, ctx, source_id, date(2026, 4, 1)).total == 1


def test_viewers_read_but_cannot_change_the_calendar(team: WorkspaceDb) -> None:
    _owner, source_id = _setup(team)
    viewer = team.ctx("viewer", "viewer")
    assert income.work_calendar(team.factory, viewer, source_id, MARCH).total == 0
    day = date(2026, 3, 2)
    for call in (
        lambda: income.toggle_work_day(team.factory, viewer, source_id, day),
        lambda: income.set_work_day(team.factory, viewer, source_id, day, Decimal(1)),
        lambda: income.fill_working_days(team.factory, viewer, source_id, MARCH),
        lambda: income.clear_work_month(team.factory, viewer, source_id, MARCH),
    ):
        with pytest.raises(PermissionDenied):
            call()


def test_a_closed_pay_month_refuses_day_changes(team: WorkspaceDb) -> None:
    ctx, source_id = _setup(team)
    january = date(2026, 1, 1)  # paid on 10 February (lag 1)
    income.toggle_work_day(team.factory, ctx, source_id, date(2026, 1, 5))
    close_month(team.factory, ctx, YearMonth(2026, 2))
    for call in (
        lambda: income.toggle_work_day(team.factory, ctx, source_id, date(2026, 1, 6)),
        lambda: income.set_work_day(team.factory, ctx, source_id, date(2026, 1, 5), Decimal(2)),
        lambda: income.fill_working_days(team.factory, ctx, source_id, january),
        lambda: income.clear_work_month(team.factory, ctx, source_id, january),
    ):
        with pytest.raises(income.IncomeError) as error:
            call()
        assert error.value.code == "month_closed"
    assert income.work_calendar(team.factory, ctx, source_id, january).total == Decimal(1)


def test_fixed_and_archived_sources_have_no_editable_calendar(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    income_category, _expense = categories(team, ctx)
    source_id = _source(team, ctx, income_category)
    fixed_id = _source(team, ctx, income_category, kind="fixed", fixed_minor=500_000)
    with pytest.raises(income.IncomeError):
        income.work_calendar(team.factory, ctx, fixed_id, MARCH)
    with pytest.raises(income.IncomeError):
        income.toggle_work_day(team.factory, ctx, fixed_id, date(2026, 3, 2))
    income.archive_source(team.factory, ctx, source_id)
    assert not income.work_calendar(team.factory, ctx, source_id, MARCH).editable
    with pytest.raises(income.IncomeError):
        income.toggle_work_day(team.factory, ctx, source_id, date(2026, 3, 2))
