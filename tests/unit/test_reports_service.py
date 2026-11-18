"""The Reports service (F10.1): golden month-by-month totals against F2.4's summary, filters and
the one streamed read. Every amount is synthetic."""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert

from assetboard.clock import FixedClock
from assetboard.db.models.ledger import Transaction
from assetboard.db.repositories import CategoryRepository, TransactionRepository
from assetboard.domain.money import Money
from assetboard.domain.months import YearMonth
from assetboard.domain.reports import ReportStep
from assetboard.services.categories import create_category
from assetboard.services.context import Context
from assetboard.services.months import close_month
from assetboard.services.reports import FilterError, reports_data
from assetboard.services.summary import month_summary
from assetboard.services.transactions import create_transaction, delete_transaction
from tests.tenant.workspace_db import WorkspaceDb, workspace_db

TODAY = datetime(2026, 10, 14, 12, tzinfo=UTC)
FIRST = YearMonth(2025, 9)


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


def _months(count: int, first: YearMonth = FIRST) -> list[YearMonth]:
    result = [first]
    for _ in range(count - 1):
        following = result[-1].next()
        assert following is not None
        result.append(following)
    return result


def _golden(db: WorkspaceDb) -> dict[str, UUID]:
    categories = {
        "salary": _category(db, "Golden Salary", "income"),
        "food": _category(db, "Golden Food"),
        "rent": _category(db, "Golden Rent"),
        "savings": _category(db, "Golden Savings", "expense", "savings"),
        "withdrawal": _category(db, "Golden Withdrawal", "income", "savings"),
        "transfer": _category(db, "Golden Transfer", "expense", "transfer"),
    }
    for index, month in enumerate(_months(14)):
        _entry(db, month, categories["salary"], f"{3000 + index}.00", direction="income")
        _entry(db, month, categories["food"], f"{120 + index}.00")
        _entry(db, month, categories["rent"], "900.00")
        _entry(db, month, categories["savings"], "500.00")
        _entry(db, month, categories["transfer"], "750.00")
        if index == 3:
            _entry(db, month, categories["withdrawal"], "200.00", direction="income")
    gone = _entry(db, YearMonth(2026, 2), categories["food"], "999.00")
    delete_transaction(db.factory, _editor(db), gone.id)
    close_month(db.factory, _editor(db), YearMonth(2026, 8))
    return categories


def _report(db: WorkspaceDb, *, from_year: str, to_year: str, step: str = "", currency: str = ""):
    return reports_data(
        db.factory,
        db.ctx("vic", "viewer"),
        from_year=from_year,
        to_year=to_year,
        step=step,
        currency=currency,
    )


def test_the_golden_months_match_the_month_summary(team: WorkspaceDb) -> None:
    _golden(team)
    data = _report(team, from_year="2025", to_year="2026", step="month")
    assert data.filters.start_year == 2025 and data.filters.end_year == 2026
    assert data.filters.step is ReportStep.MONTH
    by_month = {period.period: period for period in data.series}
    for month in _months(14):
        summary = month_summary(team.factory, team.ctx("vic", "viewer"), month).summary.kpis
        report = by_month[month]
        assert (report.earned, report.spent, report.saved, report.net) == (
            summary.earned,
            summary.spent,
            summary.saved,
            summary.net,
        ), month
    # The transfer and withdrawal are in no figure; the soft-deleted row never counts.
    assert by_month[YearMonth(2025, 12)].earned == Money(300_300, "EUR")
    assert by_month[YearMonth(2026, 2)].spent == Money(102_500, "EUR")
    # Closed August counts like an open month.
    assert by_month[YearMonth(2026, 8)].earned == Money(301_100, "EUR")
    assert data.has_data and data.future_count == 0 and data.other_currency_count == 0


def test_the_golden_years_are_the_sums_of_the_months(team: WorkspaceDb) -> None:
    _golden(team)
    monthly = _report(team, from_year="2025", to_year="2026", step="month")
    yearly = _report(team, from_year="2025", to_year="2026", step="year")
    assert [period.period for period in yearly.series] == [2025, 2026]
    for year in yearly.series:
        months = [
            period for period in monthly.series if isinstance(period.period, YearMonth)
            and period.period.year == year.period
        ]  # fmt: skip
        assert year.earned.amount_minor == sum(p.earned.amount_minor for p in months)
        assert year.spent.amount_minor == sum(p.spent.amount_minor for p in months)
        assert year.saved.amount_minor == sum(p.saved.amount_minor for p in months)
        assert year.net.amount_minor == sum(p.net.amount_minor for p in months)


def test_the_default_range_is_the_current_year_and_the_two_before(team: WorkspaceDb) -> None:
    data = reports_data(team.factory, team.ctx("vic", "viewer"))
    assert (data.filters.start_year, data.filters.end_year) == (2024, 2026)
    assert data.filters.step is ReportStep.MONTH
    assert data.filters.from_text == "2024" and data.filters.to_text == "2026"
    assert data.filters.currency == "EUR"
    assert data.show_currency_selector is False
    assert [period.period for period in data.series][0] == YearMonth(2024, 1)
    assert [period.period for period in data.series][-1] == YearMonth(2026, 10)


def test_a_range_over_three_years_defaults_to_years(team: WorkspaceDb) -> None:
    data = _report(team, from_year="2020", to_year="2026")
    assert data.filters.step is ReportStep.YEAR
    assert data.filters.from_text == "2020"


def test_an_explicit_step_wins_and_unknown_values_are_ignored(team: WorkspaceDb) -> None:
    assert _report(team, from_year="2024", to_year="2026", step="year").filters.step is (
        ReportStep.YEAR
    )
    assert _report(team, from_year="2024", to_year="2026", step="week").filters.step is (
        ReportStep.MONTH
    )


def test_thirty_years_is_allowed_and_thirty_one_is_not(team: WorkspaceDb) -> None:
    allowed = _report(team, from_year="1997", to_year="2026", step="year")
    assert allowed.filters.errors == {}
    assert len(allowed.series) == 30
    too_long = _report(team, from_year="1996", to_year="2026", step="year")
    assert too_long.filters.errors == {"to": FilterError.TOO_MANY_YEARS}


def test_malformed_and_out_of_range_filters_show_no_series(team: WorkspaceDb) -> None:
    data = _report(team, from_year="abcd", to_year="2026")
    assert data.filters.errors == {"from": FilterError.INVALID_YEAR}
    assert data.series == () and data.has_data is False
    assert data.filters.from_text == "abcd"

    data = _report(team, from_year="2026", to_year="2025")
    assert data.filters.errors == {"to": FilterError.FROM_AFTER_TO}
    assert data.series == ()

    data = _report(team, from_year="1996", to_year="2026")
    assert data.filters.errors == {"to": FilterError.TOO_MANY_YEARS}
    assert data.series == ()

    data = _report(team, from_year="1899", to_year="2026")
    assert data.filters.errors == {"from": FilterError.INVALID_YEAR}
    assert data.series == ()


def test_future_rows_are_counted_but_never_charted(team: WorkspaceDb) -> None:
    food = _category(team, "Golden Food")
    with team.factory.begin() as session:
        repository = TransactionRepository(session, _editor(team))
        repository.create(
            direction="expense",
            day=date(2026, 10, 20),
            amount_minor=10_000,
            currency="EUR",
            category_id=food,
            description="Later this month",
            description_key="later this month",
            source="manual",
            created_by_user_id=None,
        )
        repository.create(
            direction="expense",
            day=date(2026, 11, 2),
            amount_minor=20_000,
            currency="EUR",
            category_id=food,
            description="Next month",
            description_key="next month",
            source="manual",
            created_by_user_id=None,
        )
    data = _report(team, from_year="2026", to_year="2026", step="month")
    assert data.future_count == 2
    assert data.has_data is False
    assert data.series[-1].period == YearMonth(2026, 10)
    assert all(period.spent.amount_minor == 0 for period in data.series)


def test_other_currencies_are_left_out_and_counted(team: WorkspaceDb) -> None:
    food = _category(team, "Golden Food")
    with team.factory.begin() as session:
        repository = TransactionRepository(session, _editor(team))
        repository.create(
            direction="expense",
            day=date(2026, 3, 5),
            amount_minor=1_000,
            currency="EUR",
            category_id=food,
            description="Euros",
            description_key="euros",
            source="manual",
            created_by_user_id=None,
        )
        repository.create(
            direction="expense",
            day=date(2026, 3, 6),
            amount_minor=2_000,
            currency="USD",
            category_id=food,
            description="Dollars",
            description_key="dollars",
            source="manual",
            created_by_user_id=None,
        )
    data = _report(team, from_year="2026", to_year="2026", step="month")
    assert data.available_currencies == ("EUR", "USD")
    assert data.show_currency_selector is True
    assert data.filters.currency == "EUR"
    assert data.other_currency_count == 1
    assert data.totals.spent == Money(1_000, "EUR")

    dollars = _report(team, from_year="2026", to_year="2026", step="month", currency="USD")
    assert dollars.filters.currency == "USD"
    assert dollars.other_currency_count == 1
    assert dollars.totals.spent == Money(2_000, "USD")

    unknown = _report(team, from_year="2026", to_year="2026", step="month", currency="ZZZ")
    assert unknown.filters.currency == "EUR"
    assert unknown.filters.errors == {}

    junk = _report(team, from_year="2026", to_year="2026", step="month", currency="usd")
    assert junk.filters.currency == "USD"


def test_nothing_crosses_workspaces(team: WorkspaceDb) -> None:
    food = _category(team, "Golden Food")
    _entry(team, YearMonth(2026, 3), food, "10.00")
    other = team.add_workspace("Other")
    team.add_member("ed", "editor", other)
    data = reports_data(
        team.factory,
        team.ctx("ed", "editor", other),
        from_year="2026",
        to_year="2026",
        step="month",
    )
    assert all(period.spent.amount_minor == 0 for period in data.series)
    assert data.totals.spent.amount_minor == 0


def test_viewers_may_read(team: WorkspaceDb) -> None:
    data = reports_data(
        team.factory,
        team.ctx("vic", "viewer"),
        from_year="2026",
        to_year="2026",
        step="month",
    )
    assert data.filters.start_year == 2026


def test_the_read_stays_one_stream_and_meets_the_performance_budget(team: WorkspaceDb) -> None:
    food = _category(team, "Golden Food")
    with team.factory.begin() as session:
        category_id = food
        rows = []
        start = date(2016, 1, 1)
        for index in range(50_000):
            year = start.year + index // 5_000
            month = (index // 400) % 12 + 1
            day = index % 28 + 1
            rows.append(
                {
                    "id": uuid4(),
                    "workspace_id": team.workspace_id,
                    "direction": "expense",
                    "date": date(year, month, day),
                    "amount_minor": 100 + index % 1_000,
                    "currency": "EUR",
                    "category_id": category_id,
                    "description": "Benchmark",
                    "description_key": "benchmark",
                    "import_key": None,
                    "import_batch_id": None,
                    "source": "manual",
                    "created_by_user_id": None,
                    "created_at": datetime(2016, 1, 1, tzinfo=UTC),
                    "updated_at": datetime(2016, 1, 1, tzinfo=UTC),
                    "deleted_at": None,
                }
            )
        for offset in range(0, len(rows), 1_000):
            session.execute(insert(Transaction), rows[offset : offset + 1_000])

    began = time.perf_counter()
    data = _report(team, from_year="2016", to_year="2025", step="month")
    elapsed = time.perf_counter() - began
    assert len(data.series) == 120
    assert data.totals.spent.amount_minor > 0
    # The target is 500 ms on CI; 2 s leaves a 4x margin for busy shared runners.
    assert elapsed < 2.0, f"reports read took {elapsed:.3f}s"


def test_only_column_reads_are_used(team: WorkspaceDb) -> None:
    from typing import Any

    from sqlalchemy import event

    statements: list[str] = []

    def capture(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        del connection, cursor, parameters, context, executemany
        statements.append(statement)

    event.listen(team.engine, "before_cursor_execute", capture)
    try:
        food = _category(team, "Golden Food")
        with team.factory.begin() as session:
            CategoryRepository(session, _editor(team)).get(food)
        _report(team, from_year="2026", to_year="2026", step="month")
    finally:
        event.remove(team.engine, "before_cursor_execute", capture)
    transaction_statements = [line for line in statements if "FROM transactions" in line]
    assert len(transaction_statements) == 2  # the currency lookup and the one column stream
    assert any(
        "SELECT DISTINCT transactions.currency" in statement for statement in transaction_statements
    )
    assert any("transactions.amount_minor" in statement for statement in transaction_statements)
    assert not any(
        "sum(" in statement.lower() or "count(" in statement.lower()
        for statement in transaction_statements
    )


def test_a_future_only_range_still_counts_the_excluded_rows(team: WorkspaceDb) -> None:
    food = _category(team, "Golden Food")
    with team.factory.begin() as session:
        TransactionRepository(session, _editor(team)).create(
            direction="expense",
            day=date(2027, 1, 5),
            amount_minor=10_000,
            currency="EUR",
            category_id=food,
            description="Next year",
            description_key="next year",
            source="manual",
            created_by_user_id=None,
        )
    data = _report(team, from_year="2027", to_year="2028", step="year")
    assert data.series == ()
    assert data.future_count == 1
    assert data.has_data is False


def test_the_default_step_uses_the_requested_range_not_the_future_end(team: WorkspaceDb) -> None:
    data = _report(team, from_year="2024", to_year="2030")
    assert data.filters.errors == {}
    assert data.filters.step is ReportStep.YEAR


def test_a_currency_that_only_occurs_after_today_is_not_offered(team: WorkspaceDb) -> None:
    food = _category(team, "Golden Food")
    with team.factory.begin() as session:
        TransactionRepository(session, _editor(team)).create(
            direction="expense",
            day=date(2026, 12, 1),
            amount_minor=10_000,
            currency="USD",
            category_id=food,
            description="Future dollars",
            description_key="future dollars",
            source="manual",
            created_by_user_id=None,
        )
    data = _report(team, from_year="2026", to_year="2026", step="year")
    assert data.available_currencies == ("EUR",)
    assert data.show_currency_selector is False
