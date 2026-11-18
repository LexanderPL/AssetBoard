"""The category report in the Reports service (F10.3): the shared filters, top-8 + Other,
archived categories, the category multi-select and the savings option. Synthetic data only."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from assetboard.clock import FixedClock
from assetboard.domain.months import YearMonth
from assetboard.services.categories import archive_category, create_category
from assetboard.services.context import Context
from assetboard.services.reports import reports_data
from assetboard.services.transactions import create_transaction
from tests.tenant.workspace_db import WorkspaceDb, workspace_db

TODAY = datetime(2026, 10, 14, 12, tzinfo=UTC)


@pytest.fixture
def team(tmp_path: Path) -> Iterator[WorkspaceDb]:
    for prepared in workspace_db(tmp_path):
        clock = FixedClock(TODAY.isoformat())
        prepared.clock = clock
        prepared.factory.configure(info={"clock": clock})
        prepared.add_member("ed", "editor")
        prepared.add_member("vic", "viewer")
        yield prepared


def _editor(db: WorkspaceDb, workspace_id: UUID | None = None) -> Context:
    return db.ctx("ed", "editor", workspace_id)


def _category(
    db: WorkspaceDb,
    name: str,
    kind: str = "expense",
    treatment: str = "regular",
    *,
    workspace_id: UUID | None = None,
) -> UUID:
    return create_category(
        db.factory,
        _editor(db, workspace_id),
        name=name,
        kind=kind,
        treatment=treatment,
        color="#16a34a",
    ).id


def _entry(
    db: WorkspaceDb,
    month: YearMonth,
    category: UUID,
    amount: str,
    *,
    direction: str = "expense",
    day: str = "7",
) -> None:
    create_transaction(
        db.factory,
        _editor(db),
        month=month,
        direction=direction,
        day=day,
        amount=amount,
        description="",
        category_id=category,
        locale="en",
    )


def _report(
    db: WorkspaceDb,
    *,
    from_year: str = "2026",
    to_year: str = "2026",
    step: str = "month",
    category: list[str] | None = None,
    savings: str = "",
):
    return reports_data(
        db.factory,
        db.ctx("vic", "viewer"),
        from_year=from_year,
        to_year=to_year,
        step=step,
        category=category or [],
        savings=savings,
    )


def test_column_totals_match_the_income_expense_series(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    rent = _category(team, "Rent")
    salary = _category(team, "Salary", "income")
    savings = _category(team, "Savings", "expense", "savings")
    transfer = _category(team, "Transfer", "expense", "transfer")
    for month in (YearMonth(2026, 1), YearMonth(2026, 2), YearMonth(2026, 3)):
        _entry(team, month, salary, "3000.00", direction="income")
        _entry(team, month, food, "120.00")
        _entry(team, month, rent, "900.00")
        _entry(team, month, savings, "500.00")
        _entry(team, month, transfer, "750.00")

    data = _report(team, from_year="2026", to_year="2026")
    assert [total.amount_minor for total in data.category_series.totals] == [
        period.spent.amount_minor for period in data.series
    ]
    assert data.category_series.total == data.totals.spent
    assert {row.name for row in data.category_series.rows} == {"Food", "Rent"}

    with_savings = _report(team, from_year="2026", to_year="2026", savings="1")
    assert with_savings.filters.savings is True
    assert [total.amount_minor for total in with_savings.category_series.totals] == [
        period.spent.amount_minor + period.saved.amount_minor for period in with_savings.series
    ]
    assert with_savings.category_series.total == (
        with_savings.totals.spent + with_savings.totals.saved
    )
    assert {row.name for row in with_savings.category_series.rows} == {"Food", "Rent", "Savings"}


def test_an_archived_category_keeps_its_name_and_spending(team: WorkspaceDb) -> None:
    old = _category(team, "Old Food")
    empty = _category(team, "Old Empty")
    _entry(team, YearMonth(2026, 5), old, "42.00")
    archive_category(team.factory, _editor(team), old)
    archive_category(team.factory, _editor(team), empty)

    data = _report(team, from_year="2026", to_year="2026")
    row = next(row for row in data.category_series.rows if row.category_id == old)
    assert row.name == "Old Food"
    assert row.total.amount_minor == 4_200
    assert [option.name for option in data.category_options] == ["Old Food"]
    assert data.category_options[0].archived is True


def test_the_default_chart_shows_the_eight_largest_and_other(team: WorkspaceDb) -> None:
    for index in range(1, 11):
        category = _category(team, f"Category {index:02d}")
        _entry(team, YearMonth(2026, 1), category, f"{index}.00")

    data = _report(team, from_year="2026", to_year="2026")
    assert len(data.category_series.rows) == 9
    assert [row.total.amount_minor for row in data.category_series.rows[:8]] == [
        1_000,
        900,
        800,
        700,
        600,
        500,
        400,
        300,
    ]
    other = data.category_series.rows[-1]
    assert other.other and other.total.amount_minor == 300
    assert data.category_series.total.amount_minor == 5_500


def test_a_category_filter_shows_exactly_the_chosen_categories(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    rent = _category(team, "Rent")
    travel = _category(team, "Travel")
    for category, amount in ((food, "10.00"), (rent, "20.00"), (travel, "30.00")):
        _entry(team, YearMonth(2026, 1), category, amount)

    data = _report(
        team,
        from_year="2026",
        to_year="2026",
        category=[str(food), str(travel)],
    )
    assert data.filters.category_ids == (food, travel)
    assert data.category_filter_active is True
    assert {row.category_id for row in data.category_series.rows} == {food, travel}
    assert not any(row.other for row in data.category_series.rows)
    assert data.category_series.total.amount_minor == 4_000


def test_unknown_and_other_workspace_category_ids_are_ignored(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    _entry(team, YearMonth(2026, 1), food, "10.00")
    other_workspace = team.add_workspace("Other")
    team.add_member("ed", "editor", other_workspace)
    foreign = _category(team, "Foreign", workspace_id=other_workspace)

    mixed = _report(
        team,
        from_year="2026",
        to_year="2026",
        category=["not-a-uuid", str(uuid4()), str(foreign), str(food)],
    )
    assert mixed.filters.category_ids == (food,)
    assert [row.category_id for row in mixed.category_series.rows] == [food]

    ignored = _report(
        team,
        from_year="2026",
        to_year="2026",
        category=["not-a-uuid", str(uuid4()), str(foreign)],
    )
    assert ignored.filters.category_ids == ()
    assert ignored.category_filter_active is False
    assert [row.category_id for row in ignored.category_series.rows] == [food]


def test_savings_categories_join_only_with_the_option(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    savings = _category(team, "Investments", "expense", "savings")
    _entry(team, YearMonth(2026, 1), food, "10.00")
    _entry(team, YearMonth(2026, 1), savings, "50.00")

    default = _report(team, from_year="2026", to_year="2026")
    assert [option.name for option in default.category_options] == ["Food"]
    assert {row.category_id for row in default.category_series.rows} == {food}

    with_savings = _report(team, from_year="2026", to_year="2026", savings="1")
    assert with_savings.filters.savings is True
    assert [option.name for option in with_savings.category_options] == ["Food", "Investments"]
    assert {row.category_id for row in with_savings.category_series.rows} == {food, savings}

    # A savings id in the filter is ignored while the option is off.
    ignored = _report(
        team,
        from_year="2026",
        to_year="2026",
        category=[str(savings)],
    )
    assert ignored.filters.category_ids == ()
    assert {row.category_id for row in ignored.category_series.rows} == {food}


def test_only_expense_categories_are_offered(team: WorkspaceDb) -> None:
    _category(team, "Food")
    _category(team, "Salary", "income")
    _category(team, "Transfer", "expense", "transfer")
    _category(team, "Investments", "expense", "savings")

    data = _report(team, from_year="2026", to_year="2026")
    assert [option.name for option in data.category_options] == ["Food"]
    assert data.category_series.rows == ()


def test_viewers_may_read_the_category_report(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    _entry(team, YearMonth(2026, 1), food, "15.00")
    data = reports_data(
        team.factory,
        team.ctx("vic", "viewer"),
        from_year="2026",
        to_year="2026",
        step="month",
    )
    assert data.category_series.total.amount_minor == 1_500
