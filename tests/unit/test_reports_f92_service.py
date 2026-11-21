"""The F9.2 Reports service: the year grid, category trends, year-over-year and plan accuracy
from one scoped read, with the shared filters and URL-resolved section options."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event

from assetboard.clock import FixedClock
from assetboard.db.repositories import BudgetLineRepository
from assetboard.domain.money import Money
from assetboard.domain.months import YearMonth
from assetboard.domain.reports import ReportStep
from assetboard.services.budget import set_planned
from assetboard.services.categories import archive_category, create_category
from assetboard.services.context import Context
from assetboard.services.months import close_month
from assetboard.services.reports import reports_data
from assetboard.services.summary import month_summary
from assetboard.services.transactions import create_transaction, delete_transaction
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


def _editor(db: WorkspaceDb) -> Context:
    return db.ctx("ed", "editor")


def _category(db: WorkspaceDb, name: str, kind: str = "expense", treatment: str = "regular"):
    return create_category(
        db.factory, _editor(db), name=name, kind=kind, treatment=treatment, color="#16a34a"
    ).id


def _entry(
    db: WorkspaceDb,
    month: YearMonth,
    category: UUID,
    amount: str,
    *,
    direction: str = "expense",
    day: str = "7",
):
    return create_transaction(
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


def _plan(db: WorkspaceDb, month: YearMonth, category: UUID, amount: str) -> None:
    set_planned(
        db.factory,
        _editor(db),
        month=month,
        category_id=category,
        amount=amount,
        locale="en",
    )


def _data(
    db: WorkspaceDb,
    *,
    from_year: str = "2026",
    to_year: str = "2026",
    step: str = "month",
    year: str = "",
    trend: str = "",
    category: tuple[str, ...] = (),
    savings: str = "",
):
    return reports_data(
        db.factory,
        db.ctx("vic", "viewer"),
        from_year=from_year,
        to_year=to_year,
        step=step,
        year=year,
        trend=trend,
        category=category,
        savings=savings,
    )


def _seed_year(db: WorkspaceDb) -> dict[str, UUID]:
    categories = {
        "food": _category(db, "Grid Food"),
        "rent": _category(db, "Grid Rent"),
        "salary": _category(db, "Grid Salary", "income"),
        "savings": _category(db, "Grid Savings", "expense", "savings"),
        "transfer": _category(db, "Grid Transfer", "expense", "transfer"),
    }
    for month in range(1, 11):
        _entry(db, YearMonth(2026, month), categories["salary"], "3000.00", direction="income")
        _entry(db, YearMonth(2026, month), categories["food"], f"{100 + month}.00")
        _entry(db, YearMonth(2026, month), categories["rent"], "900.00")
        _entry(db, YearMonth(2026, month), categories["savings"], "500.00")
        _entry(db, YearMonth(2026, month), categories["transfer"], "750.00")
    return categories


def test_the_year_grid_matches_the_month_summaries(team: WorkspaceDb) -> None:
    categories = _seed_year(team)
    data = _data(team, year="2026")
    grid = data.year_grid
    assert len(grid.periods) == 12
    assert grid.periods[0] == YearMonth(2026, 1) and grid.periods[-1] == YearMonth(2026, 12)
    for index, month in enumerate(range(1, 11)):
        summary = month_summary(team.factory, team.ctx("vic", "viewer"), YearMonth(2026, month))
        assert grid.totals[index] == summary.summary.kpis.spent, month
    assert grid.totals[10] == Money(0, "EUR") and grid.totals[11] == Money(0, "EUR")
    # Transfers, savings and income never appear; two regular expense categories do.
    assert {row.category_id for row in grid.rows} == {categories["food"], categories["rent"]}
    food = next(row for row in grid.rows if row.category_id == categories["food"])
    assert food.total == Money(105_500, "EUR")  # 101 + ... + 110
    assert grid.total == Money(1_005_500, "EUR")


def test_the_year_grid_excludes_soft_deleted_rows_and_counts_closed_months(
    team: WorkspaceDb,
) -> None:
    food = _category(team, "Grid Deleted")
    _entry(team, YearMonth(2026, 1), food, "100.00")
    gone = _entry(team, YearMonth(2026, 2), food, "999.00")
    delete_transaction(team.factory, _editor(team), gone.id)
    _entry(team, YearMonth(2026, 3), food, "70.00")
    close_month(team.factory, _editor(team), YearMonth(2026, 3))
    grid = _data(team, year="2026").year_grid
    assert [grid.totals[index].amount_minor for index in range(3)] == [10_000, 0, 7_000]


def test_the_grid_can_include_savings_when_the_option_is_on(team: WorkspaceDb) -> None:
    categories = _seed_year(team)
    data = _data(team, year="2026", savings="1")
    assert categories["savings"] in {row.category_id for row in data.year_grid.rows}
    assert data.year_grid.totals[0] == Money(150_100, "EUR")


def test_the_grid_honours_the_category_filter_and_ignores_unknown_ids(
    team: WorkspaceDb,
) -> None:
    categories = _seed_year(team)
    data = _data(
        team,
        year="2026",
        category=(str(categories["rent"]), "junk", str(uuid4())),
    )
    assert [row.category_id for row in data.year_grid.rows] == [categories["rent"]]
    assert data.year_grid.total == Money(900_000, "EUR")
    assert data.year_grid.totals[0] == Money(90_000, "EUR")


def test_the_section_year_defaults_to_the_report_end_or_current_year(team: WorkspaceDb) -> None:
    assert _data(team, from_year="2024", to_year="2026").filters.year == 2026
    assert _data(team, from_year="2020", to_year="2030").filters.year == 2026
    assert _data(team, from_year="2024", to_year="2026", year="2025").filters.year == 2025
    assert _data(team, from_year="2024", to_year="2026", year="20x5").filters.year == 2026
    assert _data(team, from_year="2024", to_year="2026", year="1899").filters.year == 2026
    assert _data(team, from_year="2024", to_year="2026", year="2031").filters.year == 2031


def test_the_year_options_only_offer_years_with_transactions(team: WorkspaceDb) -> None:
    _seed_year(team)
    _entry(team, YearMonth(2024, 3), _category(team, "Old Option"), "10.00")
    data = _data(team, from_year="2024", to_year="2026", year="2019")
    assert data.range_year_options == (2024, 2026)
    assert data.year_options == (2019, 2024, 2026)
    assert all(1900 <= year <= 2199 for year in data.range_year_options)


def test_year_options_are_workspace_scoped(team: WorkspaceDb) -> None:
    other = team.add_workspace("Other Years")
    team.add_member("ed", "editor", other)
    category = create_category(
        team.factory,
        team.ctx("ed", "editor", other),
        name="Foreign Year",
        kind="expense",
        treatment="regular",
        color="#16a34a",
    ).id
    create_transaction(
        team.factory,
        team.ctx("ed", "editor", other),
        month=YearMonth(2018, 1),
        direction="expense",
        day="7",
        amount="10.00",
        description="",
        category_id=category,
        locale="en",
    )
    data = _data(team)
    assert 2018 not in data.range_year_options


def test_an_empty_workspace_still_offers_the_current_year(team: WorkspaceDb) -> None:
    data = _data(team)
    assert data.range_year_options == (2026,)
    assert data.year_options == (2026,)
    assert data.filters.start_year == 2026 and data.filters.end_year == 2026


def test_the_default_range_never_exceeds_thirty_years(team: WorkspaceDb) -> None:
    for year in (1990, 2000, 2025):
        _entry(team, YearMonth(year, 1), _category(team, f"Span {year}"), "10.00")
    data = _data(team, from_year="", to_year="")
    assert (data.filters.start_year, data.filters.end_year) == (2000, 2025)
    assert data.range_year_options == (1990, 2000, 2025)


def test_the_default_range_uses_the_last_three_data_years(team: WorkspaceDb) -> None:
    for year in (2019, 2021, 2022, 2023, 2025):
        _entry(team, YearMonth(year, 1), _category(team, f"Year {year}"), "10.00")
    data = _data(team, from_year="", to_year="")
    assert (data.filters.start_year, data.filters.end_year) == (2022, 2025)
    assert data.range_year_options == (2019, 2021, 2022, 2023, 2025)
    assert data.year_options == (2022, 2023, 2025)


def test_category_trends_use_the_displayed_categories_and_the_window(team: WorkspaceDb) -> None:
    categories = _seed_year(team)
    default = _data(team, year="2026")
    assert default.filters.trend_months == 3
    assert default.category_trends.months == 3
    assert {row.category_id for row in default.category_trends.rows} == {
        categories["food"],
        categories["rent"],
    }
    assert default.category_trends.rows[0].averages[0] is None
    assert default.category_trends.rows[0].averages[2] is not None

    twelve = _data(team, from_year="2025", year="2026", trend="12")
    assert twelve.filters.trend_months == 12
    assert twelve.category_trends.months == 12
    assert all(amount is None for amount in twelve.category_trends.rows[0].averages[:11])
    assert twelve.category_trends.rows[0].averages[11] is not None

    selected = _data(team, year="2026", category=(str(categories["food"]),))
    assert [row.category_id for row in selected.category_trends.rows] == [categories["food"]]


def test_category_trends_are_empty_for_a_yearly_step(team: WorkspaceDb) -> None:
    data = _data(team, year="2026", step="year")
    assert data.filters.step is ReportStep.YEAR
    assert data.category_trends.periods == () and data.category_trends.rows == ()


def test_year_over_year_table_has_a_row_per_data_year(team: WorkspaceDb) -> None:
    categories = _seed_year(team)
    _entry(team, YearMonth(2025, 1), categories["food"], "500.00")
    _entry(team, YearMonth(2025, 2), categories["food"], "500.00")
    _entry(team, YearMonth(2025, 1), categories["rent"], "800.00")

    table = _data(team, from_year="2025", to_year="2026").yoy
    assert [row.year for row in table.rows] == [2025, 2026]
    first, current = table.rows
    assert first.earned == Money(0, "EUR")
    assert first.spent == Money(180_000, "EUR")
    assert first.has_previous is False
    assert current.earned == Money(3_000_000, "EUR")
    assert current.spent == Money(1_005_500, "EUR")
    assert current.saved == Money(500_000, "EUR")
    assert current.earned_change == Money(3_000_000, "EUR")
    assert current.spent_change == Money(825_500, "EUR")
    assert current.saved_change == Money(500_000, "EUR")
    assert current.earned_change_tenths is None
    assert current.spent_change_tenths == 4_586  # +458.6 %
    assert current.saved_change_tenths is None
    assert current.has_previous is True
    assert table.earned == Money(3_000_000, "EUR")
    assert table.spent == Money(1_185_500, "EUR")
    assert table.saved == Money(500_000, "EUR")


def test_the_current_year_yoy_compares_full_calendar_years(team: WorkspaceDb) -> None:
    food = _category(team, "YTD Food")
    for month in range(1, 13):
        _entry(team, YearMonth(2025, month), food, "100.00")
    for month in range(1, 11):
        _entry(team, YearMonth(2026, month), food, "100.00")

    table = _data(team, from_year="2026", to_year="2026").yoy
    row = table.rows[0]
    assert row.has_previous is True
    assert (row.earned, row.spent) == (Money(0, "EUR"), Money(100_000, "EUR"))
    # The current year has ten recorded months; 2025 is compared in full, not Jan-Oct.
    assert row.spent_change == Money(-20_000, "EUR")
    assert row.spent_change_tenths == -167  # -16.7 %


def test_yoy_percent_is_none_without_previous_data(team: WorkspaceDb) -> None:
    _seed_year(team)
    table = _data(team, from_year="2026", to_year="2026").yoy
    row = table.rows[0]
    assert row.has_previous is False
    assert row.earned_change == Money(0, "EUR")
    assert row.spent_change == Money(0, "EUR")
    assert row.saved_change == Money(0, "EUR")
    assert row.earned_change_tenths is None
    assert row.spent_change_tenths is None
    assert row.saved_change_tenths is None


def test_year_over_year_first_range_year_keeps_changes_when_a_prior_year_exists(
    team: WorkspaceDb,
) -> None:
    food = _category(team, "Prior Year Food")
    _entry(team, YearMonth(2025, 1), food, "100.00")
    _entry(team, YearMonth(2026, 1), food, "200.00")

    row = _data(team, from_year="2026", to_year="2026").yoy.rows[0]
    assert row.has_previous is True
    assert row.spent_change == Money(10_000, "EUR")
    assert row.spent_change_tenths == 1_000


def test_year_over_year_savings_are_separate_from_expenses(team: WorkspaceDb) -> None:
    salary = _category(team, "YoY Salary", "income")
    food = _category(team, "YoY Food")
    savings = _category(team, "YoY Savings", "expense", "savings")
    _entry(team, YearMonth(2025, 1), salary, "3000.00", direction="income")
    _entry(team, YearMonth(2025, 1), food, "1000.00")
    _entry(team, YearMonth(2025, 1), savings, "250.00")
    _entry(team, YearMonth(2026, 1), salary, "3000.00", direction="income")
    _entry(team, YearMonth(2026, 1), food, "1000.00")
    _entry(team, YearMonth(2026, 1), savings, "500.00")

    data = _data(team, from_year="2025", to_year="2026")
    row = data.yoy.rows[1]
    assert (row.earned, row.spent, row.saved) == (
        Money(300_000, "EUR"),
        Money(100_000, "EUR"),
        Money(50_000, "EUR"),
    )
    assert row.saved_change == Money(25_000, "EUR")
    assert row.saved_change_tenths == 1_000
    assert data.yoy.saved == Money(75_000, "EUR")

    with_savings = _data(team, from_year="2025", to_year="2026", savings="1").yoy
    assert with_savings == data.yoy


def test_plan_accuracy_counts_planned_months_and_strictly_over_months(
    team: WorkspaceDb,
) -> None:
    food = _category(team, "Accuracy Food")
    rent = _category(team, "Accuracy Rent")
    savings = _category(team, "Accuracy Savings", "expense", "savings")
    salary = _category(team, "Accuracy Salary", "income")
    transfer = _category(team, "Accuracy Transfer", "expense", "transfer")
    _entry(team, YearMonth(2026, 1), food, "100.00")
    _entry(team, YearMonth(2026, 2), food, "200.00")
    _entry(team, YearMonth(2026, 3), food, "50.00")
    _entry(team, YearMonth(2026, 1), rent, "300.00")
    _entry(team, YearMonth(2026, 1), savings, "400.00")
    _entry(team, YearMonth(2026, 1), salary, "500.00", direction="income")
    _entry(team, YearMonth(2026, 1), transfer, "600.00")
    _plan(team, YearMonth(2026, 1), food, "100.00")  # equal is not over
    _plan(team, YearMonth(2026, 2), food, "150.00")  # over
    _plan(team, YearMonth(2026, 3), food, "100.00")  # under
    _plan(team, YearMonth(2026, 1), rent, "300.00")  # equal
    _plan(team, YearMonth(2026, 1), savings, "1.00")
    _plan(team, YearMonth(2026, 1), salary, "1.00")
    # The budget service refuses a new transfer plan, so seed one raw line: the report must
    # still ignore it.
    with team.factory.begin() as session:
        BudgetLineRepository(session, _editor(team)).create(
            month=YearMonth(2026, 1),
            category_id=transfer,
            planned_minor=100,
            currency="EUR",
        )
    close_month(team.factory, _editor(team), YearMonth(2026, 2))

    data = _data(team, year="2026")
    accuracy = data.plan_accuracy
    assert [row.category_id for row in accuracy.rows] == [food, rent]
    assert (accuracy.rows[0].months_planned, accuracy.rows[0].months_over) == (3, 1)
    assert accuracy.rows[0].over_tenths == 333
    assert (accuracy.rows[1].months_planned, accuracy.rows[1].months_over) == (1, 0)
    assert (accuracy.months_planned, accuracy.months_over) == (4, 1)
    assert accuracy.over_tenths == 250
    assert accuracy.planned == Money(65_000, "EUR")
    assert accuracy.deviation == Money(10_000, "EUR")
    assert accuracy.score_tenths == 846

    with_savings = _data(team, year="2026", savings="1")
    assert [row.category_id for row in with_savings.plan_accuracy.rows] == [food, rent]
    assert with_savings.plan_accuracy.score_tenths == 846


def test_plan_accuracy_includes_a_plan_without_any_spending(team: WorkspaceDb) -> None:
    food = _category(team, "Planned Empty")
    _plan(team, YearMonth(2026, 1), food, "100.00")
    accuracy = _data(team, year="2026").plan_accuracy
    assert len(accuracy.rows) == 1
    assert (accuracy.rows[0].months_planned, accuracy.rows[0].months_over) == (1, 0)
    assert accuracy.rows[0].over_tenths == 0
    assert accuracy.planned == Money(10_000, "EUR")
    assert accuracy.deviation == Money(10_000, "EUR")
    assert accuracy.score_tenths == 0


def test_plan_accuracy_is_empty_without_plans(team: WorkspaceDb) -> None:
    _seed_year(team)
    data = _data(team, year="2026")
    assert data.plan_accuracy.empty
    assert data.plan_accuracy.over_tenths is None
    assert data.plan_accuracy.score_tenths is None


def test_one_transaction_read_covers_section_years_outside_the_range(team: WorkspaceDb) -> None:
    _seed_year(team)
    _entry(team, YearMonth(2024, 3), _category(team, "Older Food"), "10.00")
    statements: list[str] = []

    def capture(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: object,
    ) -> None:
        del connection, cursor, parameters, context, executemany
        statements.append(statement)

    event.listen(team.engine, "before_cursor_execute", capture)
    try:
        data = _data(team, from_year="2026", to_year="2026", year="2024")
    finally:
        event.remove(team.engine, "before_cursor_execute", capture)
    transaction_statements = [line for line in statements if "FROM transactions" in line]
    # The year selector, the currency lookup and the one column stream.
    assert len(transaction_statements) == 3
    assert data.year_grid.total == Money(1_000, "EUR")
    assert data.filters.year == 2024


def test_an_archived_category_that_only_spent_in_the_grid_year_is_still_offered(
    team: WorkspaceDb,
) -> None:
    archived = _category(team, "Old Grid")
    _entry(team, YearMonth(2024, 3), archived, "10.00")
    archive_category(team.factory, _editor(team), archived)

    data = _data(team, from_year="2026", to_year="2026", year="2024")
    assert [row.category_id for row in data.year_grid.rows] == [archived]
    assert archived in {option.id for option in data.category_options}


def test_section_years_outside_the_report_range_do_not_skew_the_notes(
    team: WorkspaceDb,
) -> None:
    categories = _seed_year(team)
    # A future report row in the report range is noted.
    _entry(team, YearMonth(2026, 11), categories["food"], "10.00")
    # A USD row in a grid year outside the report range must not be counted in the notes.
    with team.factory.begin() as session:
        from assetboard.db.repositories import TransactionRepository

        TransactionRepository(session, _editor(team)).create(
            direction="expense",
            day=date(2024, 10, 5),
            amount_minor=1_000,
            currency="USD",
            category_id=categories["food"],
            description="Old dollars",
            description_key="old dollars",
            source="manual",
            created_by_user_id=None,
        )
    data = _data(team, from_year="2026", to_year="2026", year="2024")
    assert data.future_count == 1  # only the 2026-11 row counts
    assert data.other_currency_count == 0  # the USD row is outside the report range


def test_budget_line_range_query_uses_the_month_index(team: WorkspaceDb) -> None:
    from sqlalchemy import text

    with team.factory() as session:
        plan = session.execute(
            text(
                "EXPLAIN QUERY PLAN SELECT id FROM budget_lines "
                "WHERE workspace_id = :workspace AND month >= :start AND month <= :end "
                "ORDER BY month, category_id"
            ),
            {
                "workspace": str(team.workspace_id),
                "start": "2026-01-01",
                "end": "2026-12-31",
            },
        ).all()
    detail = " ".join(str(row[-1]) for row in plan)
    assert "budget_lines" in detail and "USING INDEX" in detail
