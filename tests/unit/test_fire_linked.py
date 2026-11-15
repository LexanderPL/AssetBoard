"""F8.4 linked FIRE inputs, expense exclusions and loan payoff events."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from assetboard.db.repositories import (
    CategoryRepository,
    FxRateRepository,
    ItemValuationRepository,
    LoanPaymentRepository,
    LoanRepository,
    NetWorthCategoryRepository,
    NetWorthItemRepository,
    TransactionRepository,
)
from assetboard.domain.fire import annualize_minor, trailing_window
from assetboard.domain.money import Money
from assetboard.services import fire_linked
from assetboard.services.investment_transactions import create_transaction
from assetboard.services.investments import (
    create_asset_class,
    create_instrument,
)
from assetboard.services.prices import save_manual_price
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def team(tmp_path):
    for db in workspace_db(tmp_path):
        db.add_member("owner", "owner")
        db.add_member("editor", "editor")
        db.add_member("viewer", "viewer")
        yield db


def _ctx(team: WorkspaceDb):
    return team.ctx("owner", "owner")


def _money(minor: int, currency: str = "EUR") -> Money:
    return Money(minor, currency)


def _category(
    team: WorkspaceDb,
    name: str,
    *,
    kind: str = "expense",
    treatment: str = "regular",
) -> UUID:
    with team.factory.begin() as session:
        row = CategoryRepository(session, _ctx(team)).create(
            name=name,
            name_key=name.casefold(),
            kind=kind,
            treatment=treatment,
            color="#16a34a",
            sort_order=0,
        )
        return row.id


def _transaction(
    team: WorkspaceDb,
    category_id: UUID,
    day: date,
    amount_minor: int,
    *,
    direction: str = "expense",
    currency: str = "EUR",
) -> UUID:
    with team.factory.begin() as session:
        row = TransactionRepository(session, _ctx(team)).create(
            direction=direction,
            day=day,
            amount_minor=amount_minor,
            currency=currency,
            category_id=category_id,
            description="Synthetic",
            description_key="synthetic",
            source="manual",
            created_by_user_id=None,
        )
        return row.id


def _series(team: WorkspaceDb, **overrides: object):
    values = {
        "currency": "EUR",
        "start_year": 2026,
        "income_mode": "manual",
        "expenses_mode": "manual",
        "portfolio_mode": "manual",
        "expense_exclusion_ids": (),
    }
    values.update(overrides)
    with team.factory() as session:
        return fire_linked.linked_inputs_in_session(session, _ctx(team), **values)  # type: ignore[arg-type]


def _portfolio_inputs(team: WorkspaceDb) -> Any:
    return _series(team, portfolio_mode="linked_investable")


def _networth_category(team: WorkspaceDb, name: str | None = None) -> UUID:
    name = name or f"Savings {uuid4().hex[:8]}"
    with team.factory.begin() as session:
        row = NetWorthCategoryRepository(session, _ctx(team)).create(
            name=name,
            name_key=name.casefold(),
            side="asset",
            color="#0ea5e9",
            sort_order=0,
        )
        return row.id


def _networth_item(
    team: WorkspaceDb,
    *,
    name: str = "Cash savings",
    currency: str = "EUR",
    value_minor: int | None = 50_000,
    as_of: date = date(2026, 2, 15),
    investable: bool = True,
    include_in_net_worth: bool = True,
    valuation_mode: str = "manual",
    link_type: str | None = None,
    link_id: UUID | None = None,
) -> UUID:
    category_id = _networth_category(team)
    with team.factory.begin() as session:
        item = NetWorthItemRepository(session, _ctx(team)).create(
            side="asset",
            category_id=category_id,
            name=name,
            currency=currency,
            valuation_mode=valuation_mode,
            link_type=link_type,
            link_id=link_id,
            include_in_net_worth=include_in_net_worth,
            investable=investable,
            estimate_between=False,
        )
        if value_minor is not None:
            ItemValuationRepository(session, _ctx(team)).create(
                item_id=item.id,
                as_of=as_of,
                value_minor=value_minor,
                currency=currency,
            )
        return item.id


def _asset_class(team: WorkspaceDb, name: str, *, investable: bool) -> UUID:
    return create_asset_class(
        team.factory,
        _ctx(team),
        name=name,
        color="#2563eb" if investable else "#64748b",
        investable=investable,
    ).id


def _holding(
    team: WorkspaceDb,
    *,
    name: str,
    class_id: UUID,
    quantity: str = "10",
    price: str = "100",
    quote_currency: str = "EUR",
) -> UUID:
    instrument_id = create_instrument(
        team.factory,
        _ctx(team),
        name=name,
        symbol="",
        type="etf",
        quote_currency=quote_currency,
        unit_label="share",
        allocations=[(class_id, 10_000)],
        locale="en",
    ).id
    create_transaction(
        team.factory,
        _ctx(team),
        instrument_id=instrument_id,
        type_value="buy",
        date_value="2026-02-27",
        quantity_value=quantity,
        unit_price_value=price,
        fee_value="0",
        fx_value="1",
        note_value="",
        locale="en",
    )
    save_manual_price(
        team.factory,
        _ctx(team),
        instrument_id,
        price_id=None,
        date_value="2026-03-01",
        price_value=price,
        locale="en",
    )
    return instrument_id


# Window and annualization


def test_trailing_window_is_twelve_complete_months_before_the_current_month() -> None:
    window = trailing_window(date(2026, 3, 15))
    assert (window.start.year, window.start.month) == (2025, 3)
    assert (window.end.year, window.end.month) == (2026, 2)
    assert window.first_day == date(2025, 3, 1)
    assert window.last_day == date(2026, 2, 28)
    assert window.months == 12
    assert window.contains(date(2025, 3, 1))
    assert window.contains(date(2026, 2, 28))
    assert not window.contains(date(2025, 2, 28))
    assert not window.contains(date(2026, 3, 1))


def test_annualization_rounds_half_up_once_and_refuses_zero_months() -> None:
    assert annualize_minor(1_000_000, 5) == 2_400_000
    assert annualize_minor(100_000, 7) == 171_429
    assert annualize_minor(1, 2) == 6
    # One exact half-up step: 12 / 8 = 1.5 -> 2 and 36 / 8 = 4.5 -> 5.
    assert annualize_minor(1, 8) == 2
    assert annualize_minor(3, 8) == 5
    with pytest.raises(ValueError):
        annualize_minor(1_000, 0)


# Trailing income and expenses


def test_trailing_series_uses_regular_rows_distinct_months_and_exclusions(
    team: WorkspaceDb,
) -> None:
    food = _category(team, "Food")
    savings = _category(team, "Savings", treatment="savings")
    transfer = _category(team, "Transfer", treatment="transfer")
    salary = _category(team, "Salary", kind="income")
    for month in (10, 11, 12):
        _transaction(team, food, date(2025, month, 10), 200_000)
    for month in (1, 2):
        _transaction(team, food, date(2026, month, 10), 200_000)
    _transaction(team, savings, date(2025, 9, 10), 900_000)
    _transaction(team, transfer, date(2025, 9, 11), 800_000)
    for month in (8, 9, 10, 11, 12):
        _transaction(team, salary, date(2025, month, 1), 14_286, direction="income")
    for month in (1, 2):
        _transaction(team, salary, date(2026, month, 1), 14_285, direction="income")

    linked = _series(
        team,
        income_mode="trailing_12m",
        expenses_mode="trailing_12m",
    )
    assert linked.blocks == ()
    assert linked.income is not None and linked.expenses is not None
    assert linked.income.months_available == 7
    assert linked.income.raw_total == _money(100_000)
    assert linked.income.annual_total == _money(171_429)
    assert linked.expenses.months_available == 5
    assert linked.expenses.raw_total == _money(1_000_000)
    assert linked.expenses.annual_total == _money(2_400_000)
    assert [row.category_id for row in linked.expenses.categories] == [food]
    assert [(row.category_id, row.amount.amount_minor) for row in linked.income.categories] == [
        (salary, 100_000)
    ]
    assert {warning.code for warning in linked.warnings} == {"scaled_months"}

    excluded = _series(
        team,
        expenses_mode="trailing_12m",
        expense_exclusion_ids=(food,),
    )
    assert excluded.expenses is not None
    assert excluded.expenses.annual_total == _money(0)
    assert excluded.expenses.months_available == 0
    assert excluded.expenses.excluded_categories[0].category_id == food
    assert [block.code for block in excluded.blocks] == ["expenses_all_excluded"]


def test_trailing_series_blocks_empty_data_and_other_currencies(team: WorkspaceDb) -> None:
    linked = _series(team, income_mode="trailing_12m", expenses_mode="trailing_12m")
    assert {block.code for block in linked.blocks} == {"no_income_data", "no_expense_data"}

    food = _category(team, "Food")
    _transaction(team, food, date(2025, 4, 10), 10_000)
    _transaction(team, food, date(2025, 5, 10), 10_000, currency="USD")
    linked = _series(team, expenses_mode="trailing_12m")
    assert [block.code for block in linked.blocks] == ["ledger_currency_mismatch"]
    assert linked.blocks[0].detail == "USD"


def test_trailing_series_warns_when_the_latest_data_is_older_than_the_window(
    team: WorkspaceDb,
) -> None:
    food = _category(team, "Food")
    _transaction(team, food, date(2025, 4, 10), 100_000)
    linked = _series(team, expenses_mode="trailing_12m")
    assert linked.expenses is not None
    assert {warning.code for warning in linked.warnings} == {
        "scaled_months",
        "linked_data_older",
    }


def test_archived_categories_still_count_in_the_trailing_window(team: WorkspaceDb) -> None:
    from assetboard.services.categories import archive_category

    food = _category(team, "Food")
    _transaction(team, food, date(2026, 1, 10), 100_000)
    archive_category(team.factory, _ctx(team), food)
    linked = _series(team, expenses_mode="trailing_12m")
    assert linked.blocks == ()
    assert linked.expenses is not None
    assert linked.expenses.months_available == 1
    assert linked.expenses.raw_total == _money(100_000)
    assert linked.expenses.annual_total == _money(1_200_000)
    assert [(row.category_id, row.archived) for row in linked.expenses.categories] == [(food, True)]


def test_exclusions_remove_rows_and_the_months_they_covered(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    unusual = _category(team, "Unusual")
    for month in (12, 1, 2):
        year = 2025 if month == 12 else 2026
        _transaction(team, food, date(year, month, 10), 100_000)
    for month in (10, 11):
        _transaction(team, unusual, date(2025, month, 10), 900_000)

    full = _series(team, expenses_mode="trailing_12m")
    assert full.expenses is not None
    assert full.expenses.months_available == 5
    assert full.expenses.raw_total == _money(2_100_000)

    without_unusual = _series(
        team,
        expenses_mode="trailing_12m",
        expense_exclusion_ids=(unusual,),
    )
    assert without_unusual.expenses is not None
    assert without_unusual.expenses.months_available == 3
    assert without_unusual.expenses.raw_total == _money(300_000)
    assert without_unusual.expenses.annual_total == _money(1_200_000)
    assert without_unusual.expenses.excluded_total == _money(1_800_000)
    assert [row.category_id for row in without_unusual.expenses.categories] == [food]

    without_all = _series(
        team,
        expenses_mode="trailing_12m",
        expense_exclusion_ids=(food, unusual),
    )
    assert without_all.expenses is not None
    assert without_all.expenses.months_available == 0
    assert [block.code for block in without_all.blocks] == ["expenses_all_excluded"]


# Portfolio


def test_linked_portfolio_sums_investable_lookthrough_and_manual_investable_items(
    team: WorkspaceDb,
) -> None:
    stocks = _asset_class(team, "Stocks", investable=True)
    home = _asset_class(team, "Home", investable=False)
    _holding(team, name="Fund", class_id=stocks)
    _holding(team, name="House", class_id=home, quantity="1", price="250000")
    item_id = _networth_item(team, name="Cash savings", value_minor=50_000)
    skipped_item_id = _networth_item(
        team,
        name="Skipped linked item",
        value_minor=None,
        valuation_mode="linked",
        link_type="portfolio_asset_class",
        link_id=stocks,
    )
    _networth_item(
        team,
        name="Not investable",
        value_minor=90_000,
        investable=False,
    )

    linked = _portfolio_inputs(team)
    assert linked.blocks == ()
    assert linked.portfolio is not None
    assert linked.portfolio.total == _money(150_000)
    assert [row.name for row in linked.portfolio.rows] == ["Stocks", "Cash savings"]
    assert linked.portfolio.rows[0].source_kind == "portfolio_class"
    assert linked.portfolio.rows[0].source_id == stocks
    assert linked.portfolio.rows[1].source_kind == "networth_item"
    assert linked.portfolio.rows[1].source_id == item_id
    assert linked.portfolio.rows[1].as_of == date(2026, 2, 15)
    assert linked.portfolio.non_investable_classes == 1
    assert linked.portfolio.skipped_linked_items == 1
    assert [
        (row.name, row.source_kind, row.source_id, row.reason, row.color)
        for row in linked.portfolio.skipped
    ] == [
        ("Home", "portfolio_class", home, "non_investable_class", "#64748b"),
        (
            "Skipped linked item",
            "networth_item",
            skipped_item_id,
            "linked_item_double_count",
            None,
        ),
    ]
    counts = {
        warning.code: warning.count for warning in linked.warnings if warning.count is not None
    }
    assert counts["non_investable_classes"] == 1
    assert counts["linked_items_skipped"] == 1


def test_skipped_portfolio_rows_do_not_add_queries(team: WorkspaceDb) -> None:
    from sqlalchemy import event

    stocks = _asset_class(team, "Stocks", investable=True)
    _holding(team, name="Fund", class_id=stocks)
    _networth_item(team, name="Cash savings", value_minor=50_000)

    def count(operation) -> int:
        seen = 0

        def before(*_args: object) -> None:
            nonlocal seen
            seen += 1

        event.listen(team.engine, "before_cursor_execute", before)
        try:
            operation()
        finally:
            event.remove(team.engine, "before_cursor_execute", before)
        return seen

    quiet = count(lambda: _portfolio_inputs(team))
    # Seed both groups in reverse name order. The linked names sort before the class names, so
    # this also proves the reason is the primary key, not the display name.
    for index in range(20):
        suffix = 19 - index
        skipped_class = _asset_class(team, f"Skipped class {suffix:02d}", investable=False)
        _holding(
            team,
            name=f"Skipped holding {suffix:02d}",
            class_id=skipped_class,
            quantity="1",
            price="1000",
        )
        linked_source = _asset_class(team, f"Linked source {suffix:02d}", investable=True)
        _networth_item(
            team,
            name=f"a linked item {suffix:02d}",
            value_minor=None,
            valuation_mode="linked",
            link_type="portfolio_asset_class",
            link_id=linked_source,
        )
    loud = count(lambda: _portfolio_inputs(team))
    assert loud == quiet
    linked = _portfolio_inputs(team)
    assert linked.portfolio is not None
    assert len(linked.portfolio.skipped) == 40
    assert linked.portfolio.total == _money(150_000)
    reasons = [row.reason for row in linked.portfolio.skipped]
    assert reasons == ["non_investable_class"] * 20 + ["linked_item_double_count"] * 20
    assert [row.name for row in linked.portfolio.skipped] == [
        *(f"Skipped class {index:02d}" for index in range(20)),
        *(f"a linked item {index:02d}" for index in range(20)),
    ]
    assert all(row.color == "#64748b" for row in linked.portfolio.skipped[:20])
    assert all(row.color is None for row in linked.portfolio.skipped[20:])
    counts = {
        warning.code: warning.count for warning in linked.warnings if warning.count is not None
    }
    assert counts["non_investable_classes"] == 20
    assert counts["linked_items_skipped"] == 20


def test_linked_portfolio_converts_a_manual_item_with_fx_once(team: WorkspaceDb) -> None:
    _networth_item(
        team,
        name="USD cash",
        currency="USD",
        value_minor=10_000,
    )
    with team.factory.begin() as session:
        FxRateRepository(session, _ctx(team)).create(
            base_currency="EUR",
            quote_currency="USD",
            date=date(2026, 2, 20),
            rate=Decimal("0.9"),
            source="manual",
        )
    linked = _portfolio_inputs(team)
    assert linked.blocks == ()
    assert linked.portfolio is not None
    assert linked.portfolio.total == _money(9_000)


def test_linked_portfolio_blocks_missing_item_value_and_fx(team: WorkspaceDb) -> None:
    _networth_item(team, name="No value", value_minor=None)
    linked = _portfolio_inputs(team)
    assert [block.code for block in linked.blocks] == ["item_value_missing"]
    assert linked.blocks[0].detail == "No value"

    _networth_item(team, name="USD cash", currency="USD", value_minor=10_000)
    linked = _portfolio_inputs(team)
    assert {block.code for block in linked.blocks} == {"item_value_missing", "item_fx_missing"}
    assert any(block.detail == "USD cash" for block in linked.blocks)


def test_linked_portfolio_blocks_no_sources_and_warns_on_zero_with_sources(
    team: WorkspaceDb,
) -> None:
    linked = _portfolio_inputs(team)
    assert [block.code for block in linked.blocks] == ["no_linked_portfolio_data"]

    home = _asset_class(team, "Home", investable=False)
    _holding(team, name="House", class_id=home, quantity="1", price="250000")
    linked = _portfolio_inputs(team)
    assert [block.code for block in linked.blocks] == ["no_linked_portfolio_data"]

    _networth_item(team, name="Zero cash", value_minor=0)
    linked = _portfolio_inputs(team)
    assert linked.blocks == ()
    assert linked.portfolio is not None
    assert linked.portfolio.total == _money(0)
    assert "linked_portfolio_zero" in {warning.code for warning in linked.warnings}


@pytest.mark.parametrize(
    ("missing", "code"),
    [("price", "portfolio_price_missing"), ("fx", "portfolio_fx_missing")],
)
def test_linked_portfolio_blocks_a_missing_holding_price_or_fx(
    team: WorkspaceDb, monkeypatch: pytest.MonkeyPatch, missing: str, code: str
) -> None:
    from types import SimpleNamespace

    from assetboard.domain.portfolio import Missing

    instrument_id = uuid4()
    row = SimpleNamespace(
        instrument_id=instrument_id,
        missing=Missing(missing),
    )
    data = SimpleNamespace(
        currency="EUR",
        today=date(2026, 3, 1),
        classes=(),
        instruments=(
            SimpleNamespace(
                id=instrument_id,
                name="No price fund",
                quote_currency="EUR",
            ),
        ),
        holdings={},
        allocations=(),
        prices={},
        fx={},
        valuation=SimpleNamespace(rows=(row,), classes=()),
    )
    monkeypatch.setattr(
        fire_linked, "current_portfolio_data_in_session", lambda _session, _ctx, *, today=None: data
    )
    linked = _portfolio_inputs(team)
    assert [block.code for block in linked.blocks] == [code]
    assert linked.blocks[0].detail == "No price fund"


def test_a_plan_start_year_before_today_warns_for_linked_values(
    team: WorkspaceDb,
) -> None:
    food = _category(team, "Food")
    _transaction(team, food, date(2025, 10, 1), 100_000)
    linked = _series(team, start_year=2025, expenses_mode="trailing_12m")
    assert "start_year_not_current" in {warning.code for warning in linked.warnings}


def test_scenario_currency_mismatch_blocks_all_linked_modes(team: WorkspaceDb) -> None:
    from assetboard.db.models.identity import Workspace

    with team.factory.begin() as session:
        workspace = session.get_one(Workspace, team.workspace_id)
        workspace.base_currency = "PLN"
    linked = _series(
        team,
        income_mode="trailing_12m",
        expenses_mode="trailing_12m",
        portfolio_mode="linked_investable",
    )
    assert [block.code for block in linked.blocks] == ["scenario_currency_mismatch"]


# Exclusions

# Setup wizard suggestions (F8.9)


def _setup(team: WorkspaceDb, **overrides: object):
    values = {
        "currency": "EUR",
        "include_income": True,
        "include_expenses": True,
        "include_portfolio": True,
    }
    values.update(overrides)
    with team.factory() as session:
        return fire_linked.setup_suggestions_in_session(
            session,
            _ctx(team),
            **values,  # type: ignore[arg-type]
        )


def _linked_loan_payment(team: WorkspaceDb, transaction_id: UUID, total_minor: int) -> UUID:
    with team.factory.begin() as session:
        loan = LoanRepository(session, _ctx(team)).create(
            name="Mortgage",
            kind="annuity",
            currency="EUR",
            principal_original_minor=None,
            origination_date=None,
            first_payment_date=date(2026, 1, 1),
            term_months=12,
            payment_day=1,
            rate_mode="fixed",
            day_count="monthly",
            margin_bp=None,
            reference_rate_id=None,
            installment_override_minor=None,
            overpayment_mode="none",
            overpayment_amount_minor=None,
            overpayment_strategy="reduce_term",
            archived_at=None,
        )
        payment = LoanPaymentRepository(session, _ctx(team)).create(
            loan_id=loan.id,
            date=date(2026, 1, 1),
            kind="installment",
            total_minor=total_minor,
            principal_minor=total_minor,
            interest_minor=0,
            costs_minor=0,
            overpayment_minor=0,
            note="",
            source="manual",
            transaction_id=transaction_id,
        )
        return payment.id


def test_setup_suggestions_resolve_the_requested_cards(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    salary = _category(team, "Salary", kind="income")
    _transaction(team, food, date(2025, 12, 10), 100_000)
    _transaction(team, food, date(2026, 2, 10), 200_000)
    _transaction(team, salary, date(2026, 1, 1), 500_000, direction="income")
    _networth_item(team, name="Cash savings", value_minor=50_000)

    suggestions = _setup(team)
    assert suggestions.blocks == ()
    assert suggestions.as_of == date(2026, 3, 1)
    assert suggestions.window.first_day == date(2025, 3, 1)
    assert suggestions.window.last_day == date(2026, 2, 28)
    assert suggestions.income is not None and suggestions.expenses is not None
    assert suggestions.portfolio is not None
    assert suggestions.income.annual_total == _money(6_000_000)
    assert suggestions.expenses.annual_total == _money(1_800_000)
    assert suggestions.portfolio.total == _money(50_000)
    assert suggestions.loan_portion is not None
    assert suggestions.loan_portion.has_data is False
    assert suggestions.loan_portion.window_total == _money(0)
    assert {warning.code for warning in suggestions.warnings} == {
        "linked_data_older",
        "scaled_months",
    }

    only_income = _setup(team, include_expenses=False, include_portfolio=False)
    assert only_income.income is not None
    assert only_income.expenses is None and only_income.portfolio is None
    assert only_income.loan_portion is None
    assert only_income.blocks == ()


def test_setup_suggestions_loan_portion_is_the_linked_ledger_subset(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    first = _transaction(team, food, date(2026, 1, 10), 100_000)
    _transaction(team, food, date(2026, 2, 10), 250_000)
    _linked_loan_payment(team, first, 90_000)

    suggestions = _setup(team)
    assert suggestions.expenses is not None
    assert suggestions.expenses.raw_total == _money(350_000)
    assert suggestions.loan_portion is not None
    assert suggestions.loan_portion.has_data is True
    assert suggestions.loan_portion.window_total == _money(100_000)
    assert suggestions.loan_portion.months_available == 1


def test_setup_suggestions_drop_a_blocked_card_instead_of_showing_zero(
    team: WorkspaceDb,
) -> None:
    food = _category(team, "Food")
    _transaction(team, food, date(2026, 1, 10), 100_000, currency="USD")

    suggestions = _setup(team)
    assert "ledger_currency_mismatch" in {block.code for block in suggestions.blocks}
    assert suggestions.expenses is None
    assert suggestions.loan_portion is None
    assert suggestions.income is None
    assert suggestions.portfolio is None


def test_setup_suggestions_never_write(team: WorkspaceDb) -> None:
    from sqlalchemy import func, select

    from assetboard.db.models.fire import FireEvent, FireScenario
    from assetboard.db.models.identity import AuditEvent
    from assetboard.db.models.ledger import Transaction
    from assetboard.db.models.loans import LoanPayment

    food = _category(team, "Food")
    transaction_id = _transaction(team, food, date(2026, 1, 10), 100_000)
    _linked_loan_payment(team, transaction_id, 90_000)

    def counts() -> tuple[int, int, int, int, int]:
        with team.factory() as session:
            return (
                session.scalar(select(func.count()).select_from(FireScenario)) or 0,
                session.scalar(select(func.count()).select_from(FireEvent)) or 0,
                session.scalar(select(func.count()).select_from(AuditEvent)) or 0,
                session.scalar(select(func.count()).select_from(Transaction)) or 0,
                session.scalar(select(func.count()).select_from(LoanPayment)) or 0,
            )

    before = counts()
    _setup(team)
    assert counts() == before


def test_setup_suggestions_query_budget_does_not_grow_with_data(team: WorkspaceDb) -> None:
    from sqlalchemy import event

    food = _category(team, "Food")
    salary = _category(team, "Salary", kind="income")
    _transaction(team, food, date(2026, 1, 10), 100_000)
    _transaction(team, salary, date(2026, 1, 1), 500_000, direction="income")
    _networth_item(team, name="Cash", value_minor=50_000)

    def count(operation) -> int:
        seen = 0

        def before(*_args: object) -> None:
            nonlocal seen
            seen += 1

        event.listen(team.engine, "before_cursor_execute", before)
        try:
            operation()
        finally:
            event.remove(team.engine, "before_cursor_execute", before)
        return seen

    quiet = count(lambda: _setup(team))
    for index in range(40):
        _transaction(team, food, date(2025, 4 + (index % 4), 1 + (index % 20)), 1_000 + index)
    food_categories = [_category(team, f"Food {index}") for index in range(20)]
    for index, category in enumerate(food_categories):
        _transaction(team, category, date(2025, 6, 1 + (index % 20)), 2_000)
    loud = count(lambda: _setup(team))
    assert loud == quiet
    assert quiet <= 24
