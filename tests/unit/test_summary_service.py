"""The month summary service (F2.4): the golden example end to end, only the month's live
transactions count, every member may read, and nothing crosses workspaces."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.domain.money import Money
from assetboard.domain.months import YearMonth
from assetboard.domain.recurrence import Frequency, RecurrenceRule
from assetboard.domain.transactions import TransactionDirection
from assetboard.services import income, scheduled
from assetboard.services.budget import set_planned
from assetboard.services.categories import archive_category, create_category
from assetboard.services.context import Context
from assetboard.services.summary import month_summary
from assetboard.services.transactions import create_transaction, delete_transaction
from tests.tenant.workspace_db import WorkspaceDb, workspace_db

OCTOBER = YearMonth(2026, 10)


@pytest.fixture
def team(tmp_path: Path) -> Iterator[WorkspaceDb]:
    for prepared in workspace_db(tmp_path):
        clock = FixedClock(datetime(2026, 10, 14, 12, tzinfo=UTC).isoformat())
        prepared.clock = clock
        prepared.factory.configure(info={"clock": clock})
        prepared.add_member("ed", "editor")
        prepared.add_member("vic", "viewer")
        yield prepared


def _editor(db: WorkspaceDb) -> Context:
    return db.ctx("ed", "editor")


def _category(db: WorkspaceDb, name: str, kind: str = "expense", treatment: str = "regular"):
    return create_category(
        db.factory, _editor(db), name=name, kind=kind, treatment=treatment, color="#16a34a"
    ).id


def _spend(db: WorkspaceDb, category: UUID, amount: str, *, day: str = "7", kind: str = "expense"):
    return create_transaction(
        db.factory, _editor(db), month=OCTOBER, direction=kind, day=day, amount=amount,
        description="", category_id=category, locale="en",
    )  # fmt: skip


def _plan(db: WorkspaceDb, category: UUID, amount: str) -> None:
    set_planned(
        db.factory, _editor(db), month=OCTOBER, category_id=category, amount=amount, locale="en"
    )


def test_the_golden_example_end_to_end(team: WorkspaceDb) -> None:
    food, travel = _category(team, "Golden Food"), _category(team, "Golden Travel")
    salary = _category(team, "Golden Salary", "income")
    _plan(team, food, "550")
    _plan(team, travel, "5350")
    _plan(team, salary, "3000")
    _spend(team, food, "50")
    _spend(team, food, "500")
    _spend(team, travel, "4323")
    _spend(team, salary, "3400", kind="income")
    data = month_summary(team.factory, team.ctx("vic", "viewer"), OCTOBER)
    summary = data.summary
    eur = lambda minor: Money(minor, "EUR")  # noqa: E731
    rows = {r.category_id: r for b in summary.blocks for r in b.rows}
    assert (rows[food].actual, rows[food].difference) == (eur(55000), eur(0))
    assert rows[travel].difference == eur(102700)
    assert rows[salary].difference == eur(40000)
    assert summary.kpis.net == eur(-147300) and summary.kpis.savings_rate_bp == 0
    assert data.categories[food].name == "Golden Food"


def test_only_the_months_live_transactions_count(team: WorkspaceDb) -> None:
    food = _category(team, "Golden Food")
    _spend(team, food, "10")
    gone = _spend(team, food, "99")
    delete_transaction(team.factory, _editor(team), gone.id)
    _spend(team, food, "1000", day="2026-09-30")  # September
    summary = month_summary(team.factory, _editor(team), OCTOBER).summary
    assert summary.block("expense").actual == Money(1000, "EUR")


def test_pending_income_occurrence_contributes_to_month_forecast(team: WorkspaceDb) -> None:
    salary = _category(team, "Golden Pending Salary", "income")
    source_id = income.create_source(
        team.factory,
        _editor(team),
        name="Contract",
        kind="variable",
        category_id=salary,
        fixed_minor=None,
        rate_minor=1_000,
        unit_label="day",
        pay_day_value=10,
        pay_lag_months=1,
        expected_mode="manual",
        expected_units_value=None,
        average_months=3,
        record_as="net",
        active_from=date(2026, 1, 1),
        active_to=None,
    )
    income.log_work(team.factory, _editor(team), source_id, date(2026, 9, 1), Decimal("5"))

    row = month_summary(team.factory, _editor(team), OCTOBER).summary.row(salary)
    assert row is not None
    assert (row.actual, row.expected, row.forecast) == (
        Money(0, "EUR"),
        Money(5_000, "EUR"),
        Money(5_000, "EUR"),
    )


def test_archived_categories_appear_with_actuals_only(team: WorkspaceDb) -> None:
    used, idle = _category(team, "Golden Old"), _category(team, "Golden Idle")
    _spend(team, used, "5")
    for category in (used, idle):
        archive_category(team.factory, _editor(team), category)
    rows = month_summary(team.factory, _editor(team), OCTOBER).summary.block("expense").rows
    assert [r.category_id for r in rows] == [used]


def test_the_sort_order_is_passed_through(team: WorkspaceDb) -> None:
    first, second = _category(team, "Golden A"), _category(team, "Golden B")
    _spend(team, second, "5")
    ids = lambda sort: [  # noqa: E731
        r.category_id
        for r in month_summary(team.factory, _editor(team), OCTOBER, sort=sort)
        .summary.block("expense")
        .rows
    ]
    assert ids("actual") == [second, first]
    assert ids("manual") == [first, second]


def test_nothing_crosses_workspaces(team: WorkspaceDb) -> None:
    _spend(team, _category(team, "Golden Food"), "10")
    other = team.add_workspace("Other")
    team.add_member("ed", "editor", other)
    data = month_summary(team.factory, team.ctx("ed", "editor", other), OCTOBER)
    assert all(not b.rows for b in data.summary.blocks) and data.categories == {}


def test_one_time_item_feeds_expected_and_forecast_but_not_planned(team: WorkspaceDb) -> None:
    gifts = _category(team, "Golden Gifts")
    _plan(team, gifts, "50")
    _spend(team, gifts, "10")
    scheduled.save_item(
        team.factory,
        _editor(team),
        scheduled.ScheduledInput(
            "Wedding gift",
            TransactionDirection.EXPENSE,
            gifts,
            Money(12_000, "EUR"),
            RecurrenceRule(date(2026, 10, 20), Frequency.ONCE),
        ),
        Settings(_env_file=None),
    )
    row = month_summary(team.factory, _editor(team), OCTOBER).summary.row(gifts)
    assert row is not None
    assert (row.planned, row.actual, row.expected, row.forecast) == (
        Money(5_000, "EUR"),
        Money(1_000, "EUR"),
        Money(12_000, "EUR"),
        Money(13_000, "EUR"),
    )
    november = month_summary(team.factory, _editor(team), YearMonth(2026, 11)).summary.row(gifts)
    assert november is None or november.expected == Money(0, "EUR")
