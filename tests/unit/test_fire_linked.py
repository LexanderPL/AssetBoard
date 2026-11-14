"""F8.4 linked FIRE inputs, expense exclusions and loan payoff events."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from assetboard.db.models.fire import FireEvent, FireScenario
from assetboard.db.models.identity import AuditEvent
from assetboard.db.repositories import (
    CategoryRepository,
    FxRateRepository,
    ItemValuationRepository,
    NetWorthCategoryRepository,
    NetWorthItemRepository,
    TransactionRepository,
)
from assetboard.domain.fire import (
    annualize_minor,
    trailing_window,
)
from assetboard.domain.money import Money
from assetboard.services import fire_linked, fire_projection, fire_scenarios, loans
from assetboard.services.context import PermissionDenied
from assetboard.services.fire_scenarios import (
    FireError,
    FireScenarioInput,
)
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


def _scenario_input(**overrides: object) -> FireScenarioInput:
    values: dict[str, object] = {
        "name": "Linked plan",
        "birth_year": 1985,
        "inflation_bp": 250,
        "real_return_bp": 500,
        "income_growth_bp": 300,
        "stop_working_age": 55,
        "horizon_age": 90,
        "rule": "returns_cover_expenses",
        "income_mode": "manual",
        "income_manual": _money(5_000_000),
        "expenses_mode": "manual",
        "expenses_manual": _money(3_000_000),
        "portfolio_mode": "manual",
        "portfolio_manual": _money(100_000_000),
    }
    values.update(overrides)
    return FireScenarioInput(**values)  # type: ignore[arg-type]


def _create_scenario(team: WorkspaceDb, **overrides: object) -> UUID:
    return fire_scenarios.create_fire_scenario(
        team.factory, _ctx(team), _scenario_input(**overrides)
    )


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
) -> None:
    with team.factory.begin() as session:
        TransactionRepository(session, _ctx(team)).create(
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


def _loan(team: WorkspaceDb, **overrides: Any) -> UUID:
    values: dict[str, Any] = {
        "entry_path": "original",
        "name": "Mortgage",
        "kind": "annuity",
        "principal_original": _money(24_000_000),
        "current_balance": None,
        "balance_date": None,
        "origination_date": date(2026, 1, 1),
        "first_payment_date": date(2026, 2, 1),
        "term_months": 24,
        "payment_day": 1,
        "rate_mode": "fixed",
        "fixed_rate_bp": 0,
        "reference_rate_id": None,
        "margin_bp": None,
        "high_rate_confirmed": False,
        "installment_override": None,
        "overpayment_mode": "none",
        "overpayment_amount": None,
        "overpayment_strategy": "reduce_term",
    }
    values.update(overrides)
    return loans.create_loan(team.factory, _ctx(team), **values)


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


# Portfolio


def test_linked_portfolio_sums_investable_lookthrough_and_manual_investable_items(
    team: WorkspaceDb,
) -> None:
    stocks = _asset_class(team, "Stocks", investable=True)
    home = _asset_class(team, "Home", investable=False)
    _holding(team, name="Fund", class_id=stocks)
    _holding(team, name="House", class_id=home, quantity="1", price="250000")
    item_id = _networth_item(team, name="Cash savings", value_minor=50_000)
    _networth_item(
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
    assert {warning.code for warning in linked.warnings} >= {
        "non_investable_classes",
        "linked_items_skipped",
    }


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
        fire_linked, "current_portfolio_data_in_session", lambda _session, _ctx: data
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


def test_exclusions_reconcile_by_category_and_duplicate(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    travel = _category(team, "Travel")
    scenario_id = _create_scenario(
        team, expenses_mode="trailing_12m", expense_exclusion_ids=(food, travel)
    )
    detail = fire_scenarios.get_fire_scenario(team.factory, _ctx(team), scenario_id)
    assert set(detail.expense_exclusion_ids) == {food, travel}
    first_ids = set(detail.expense_exclusion_ids)

    fire_scenarios.update_fire_scenario(
        team.factory,
        _ctx(team),
        scenario_id,
        _scenario_input(
            expenses_mode="trailing_12m",
            expense_exclusion_ids=(travel,),
        ),
    )
    updated = fire_scenarios.get_fire_scenario(team.factory, _ctx(team), scenario_id)
    assert updated.expense_exclusion_ids == (travel,)

    duplicate = fire_scenarios.duplicate_fire_scenario(team.factory, _ctx(team), scenario_id)
    copied = fire_scenarios.get_fire_scenario(team.factory, _ctx(team), duplicate)
    assert copied.expense_exclusion_ids == (travel,)
    assert first_ids != {travel}


def test_exclusions_reject_income_savings_and_cross_workspace_categories(
    team: WorkspaceDb,
) -> None:
    income = _category(team, "Salary", kind="income")
    savings = _category(team, "Savings", treatment="savings")
    for category_id in (income, savings):
        with pytest.raises(FireError) as exc:
            _create_scenario(
                team,
                expenses_mode="trailing_12m",
                expense_exclusion_ids=(category_id,),
            )
        assert (exc.value.field, exc.value.code) == (
            "expense_exclusions",
            "invalid_expense_exclusion",
        )

    other = team.add_workspace("Other")
    with team.factory.begin() as session:
        foreign = CategoryRepository(session, team.ctx("owner", "owner", other)).create(
            name="Foreign",
            name_key="foreign",
            kind="expense",
            treatment="regular",
            color="#16a34a",
            sort_order=0,
        )
    with pytest.raises(FireError) as exc:
        _create_scenario(
            team,
            expenses_mode="trailing_12m",
            expense_exclusion_ids=(foreign.id,),
        )
    assert (exc.value.field, exc.value.code) == (
        "expense_exclusions",
        "invalid_expense_exclusion",
    )


def test_archived_categories_remain_valid_exclusions(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    from assetboard.services.categories import archive_category

    archive_category(team.factory, _ctx(team), food)
    scenario_id = _create_scenario(
        team, expenses_mode="trailing_12m", expense_exclusion_ids=(food,)
    )
    detail = fire_scenarios.get_fire_scenario(team.factory, _ctx(team), scenario_id)
    assert detail.expense_exclusion_ids == (food,)
    options = fire_linked.expense_exclusion_options(team.factory, _ctx(team), selected_ids=(food,))
    assert [(option.category_id, option.archived, option.selected) for option in options] == [
        (food, True, True)
    ]


# Projection integration and preview


def test_saved_linked_inputs_select_the_resolved_amounts(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    salary = _category(team, "Salary", kind="income")
    _transaction(team, food, date(2025, 10, 10), 100_000)
    _transaction(team, food, date(2025, 11, 10), 100_000)
    _transaction(team, salary, date(2025, 10, 1), 500_000, direction="income")
    _transaction(team, salary, date(2025, 11, 1), 500_000, direction="income")
    _networth_item(team, name="Cash", value_minor=500_000)
    scenario_id = _create_scenario(
        team,
        income_mode="trailing_12m",
        expenses_mode="trailing_12m",
        portfolio_mode="linked_investable",
    )
    results = fire_projection.scenario_results(team.factory, _ctx(team), scenario_id)
    assert results.blocks == ()
    assert results.projection is not None
    assert results.projection.inputs.start_expenses == _money(1_200_000)
    assert results.projection.inputs.start_income == _money(6_000_000)
    assert results.projection.inputs.start_portfolio == _money(500_000)
    assert results.linked is not None
    assert results.linked.expenses is not None


def test_preview_resolves_posted_modes_and_exclusions_without_writing(
    team: WorkspaceDb,
) -> None:
    food = _category(team, "Food")
    unusual = _category(team, "Unusual")
    for month in (10, 11, 12, 1, 2):
        year = 2025 if month >= 10 else 2026
        _transaction(team, food, date(year, month, 10), 100_000)
        _transaction(team, unusual, date(year, month, 11), 900_000)
    scenario_id = _create_scenario(team)
    detail = fire_scenarios.get_fire_scenario(team.factory, _ctx(team), scenario_id)
    with team.factory() as session:
        before_updated = session.get_one(FireScenario, scenario_id).updated_at
    preview = fire_projection.preview_scenario_results(
        _ctx(team),
        detail,
        _scenario_input(
            expenses_mode="trailing_12m",
            expense_exclusion_ids=(unusual,),
        ),
        factory=team.factory,
    )
    assert preview.preview is True
    assert preview.blocks == ()
    assert preview.projection is not None
    assert preview.projection.inputs.start_expenses == _money(1_200_000)
    assert preview.linked is not None and preview.linked.expenses is not None
    assert [row.category_id for row in preview.linked.expenses.categories] == [food]
    assert preview.linked.expenses.excluded_categories[0].category_id == unusual

    after = fire_scenarios.get_fire_scenario(team.factory, _ctx(team), scenario_id)
    with team.factory() as session:
        assert session.get_one(FireScenario, scenario_id).updated_at == before_updated
    assert after.expense_exclusion_ids == ()


def test_preview_reports_every_block_for_fully_linked_plan(team: WorkspaceDb) -> None:
    scenario_id = _create_scenario(team)
    detail = fire_scenarios.get_fire_scenario(team.factory, _ctx(team), scenario_id)
    preview = fire_projection.preview_scenario_results(
        _ctx(team),
        detail,
        _scenario_input(
            income_mode="trailing_12m",
            expenses_mode="trailing_12m",
            portfolio_mode="linked_investable",
        ),
        factory=team.factory,
    )
    assert preview.projection is None
    assert {block.code for block in preview.blocks} == {
        "no_income_data",
        "no_expense_data",
        "no_linked_portfolio_data",
    }


# Loan payoff suggestions and one-click events


def _set_base_currency(team: WorkspaceDb, currency: str) -> None:
    from assetboard.db.models.identity import Workspace

    with team.factory.begin() as session:
        workspace = session.get_one(Workspace, team.workspace_id)
        workspace.base_currency = currency


def _suggestions(team: WorkspaceDb, scenario_id: UUID | None = None):
    scenario_id = scenario_id or _create_scenario(team)
    detail = fire_scenarios.get_fire_scenario(team.factory, _ctx(team), scenario_id)
    with team.factory() as session:
        return fire_linked.loan_payoff_suggestions_in_session(
            session,
            _ctx(team),
            currency="EUR",
            start_year=detail.scenario.start_year,
            last_year=detail.scenario.birth_year + detail.scenario.horizon_age,
            existing_events=tuple((event.year, event.kind, event.label) for event in detail.events),
        )


def test_loan_payoff_suggestion_uses_contractual_rows_not_installment_times_twelve(
    team: WorkspaceDb,
) -> None:
    _loan(team, term_months=24, principal_original=_money(24_000_000))
    suggestions = _suggestions(team)
    assert len(suggestions) == 1
    suggestion = suggestions[0]
    assert suggestion.state == "available"
    assert suggestion.payoff_year == 2028
    assert suggestion.months_covered == 12
    assert suggestion.annual_amount == _money(12_000_000)  # €120,000.00


def test_loan_payoff_states_for_archived_paid_off_currency_and_window(
    team: WorkspaceDb,
) -> None:
    scenario_id = _create_scenario(team)
    _loan(team, name="Paid", origination_date=date(2025, 1, 1), first_payment_date=date(2025, 2, 1),
          term_months=2, principal_original=_money(2_000_000))  # fmt: skip
    _set_base_currency(team, "USD")
    _loan(
        team,
        name="USD loan",
        principal_original=_money(24_000_000, "USD"),
    )
    _set_base_currency(team, "EUR")
    _loan(team, name="Long loan")
    archived_loan = _loan(team, name="Archived loan")
    loans.archive_loan(team.factory, _ctx(team), archived_loan)

    suggestions = {item.loan_name: item for item in _suggestions(team, scenario_id)}
    assert "Archived loan" not in suggestions
    assert suggestions["Paid"].state == "already_paid_off"
    assert suggestions["USD loan"].state == "currency_mismatch"

    detail = fire_scenarios.get_fire_scenario(team.factory, _ctx(team), scenario_id)
    with team.factory() as session:
        windowed = fire_linked.loan_payoff_suggestions_in_session(
            session,
            _ctx(team),
            currency="EUR",
            start_year=2026,
            last_year=2026,
            existing_events=(),
        )
    assert {item.state for item in windowed if item.loan_name == "Long loan"} == {"out_of_window"}
    assert detail.scenario.start_year == 2026


def test_loan_payoff_states_for_a_broken_schedule_and_an_unusable_amount(
    team: WorkspaceDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from assetboard.domain.loans import ScheduleError

    _loan(team, name="Broken")
    scenario_id = _create_scenario(team)
    detail = fire_scenarios.get_fire_scenario(team.factory, _ctx(team), scenario_id)

    def suggestions():
        with team.factory() as session:
            return fire_linked.loan_payoff_suggestions_in_session(
                session,
                _ctx(team),
                currency="EUR",
                start_year=detail.scenario.start_year,
                last_year=detail.scenario.birth_year + detail.scenario.horizon_age,
                existing_events=(),
            )

    def broken(_inputs):
        raise ScheduleError("invalid_terms")

    monkeypatch.setattr(fire_linked, "build_inputs_schedule", broken)
    assert suggestions()[0].state == "schedule_error"
    assert suggestions()[0].schedule_error == "invalid_terms"

    row = SimpleNamespace(
        date=date(2026, 4, 1),
        interest=Money(0, "EUR"),
        principal=Money(0, "EUR"),
        costs=Money(0, "EUR"),
    )
    summary = SimpleNamespace(
        remaining_principal=Money(1_000_000, "EUR"),
        payoff_date=date(2028, 1, 1),
    )
    monkeypatch.setattr(
        fire_linked,
        "build_inputs_schedule",
        lambda _inputs: SimpleNamespace(rows=(row,)),
    )
    monkeypatch.setattr(fire_linked, "summarize_schedule", lambda _schedule, _today: summary)
    assert suggestions()[0].state == "amount_invalid"


def test_one_click_creates_the_event_and_audit_without_the_amount(
    team: WorkspaceDb,
) -> None:
    loan_id = _loan(team)
    scenario_id = _create_scenario(team)
    outcome = fire_linked.add_loan_payoff_event(team.factory, _ctx(team), scenario_id, loan_id)
    assert outcome == "added"
    with team.factory() as session:
        event = session.scalars(select(FireEvent).where(FireEvent.scenario_id == scenario_id)).one()
        assert (event.year, event.kind, event.amount_minor) == (2028, "expense_change", -12_000_000)
        assert event.label == "Loan repaid: Mortgage"
        audit = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "fire_scenario.loan_payoff_event")
        ).one()
        assert audit.data == {"loan": "Mortgage", "year": 2028}

    assert (
        fire_linked.add_loan_payoff_event(team.factory, _ctx(team), scenario_id, loan_id)
        == "exists"
    )
    suggestion = _suggestions(team, scenario_id)[0]
    assert suggestion.state == "already_added"

    results = fire_projection.scenario_results(team.factory, _ctx(team), scenario_id)
    assert results.projection is not None
    row = next(row for row in results.projection.rows if row.year == 2028)
    # 3,000,000.00 inflated by 2.5% for two years, less the event inflated the same way.
    assert row.expenses.amount_minor == 3_151_875 - 12_607_500


def test_loan_suggestion_queries_do_not_grow_with_the_loan_count(team: WorkspaceDb) -> None:
    from collections.abc import Callable

    from sqlalchemy import event
    from sqlalchemy.orm import ORMExecuteState, Session

    _loan(team, name="First")
    scenario_id = _create_scenario(team)
    detail = fire_scenarios.get_fire_scenario(team.factory, _ctx(team), scenario_id)

    def statements(operation: Callable[[], Any]) -> int:
        seen = 0

        def record(_state: ORMExecuteState) -> None:
            nonlocal seen
            seen += 1

        event.listen(Session, "do_orm_execute", record)
        try:
            operation()
        finally:
            event.remove(Session, "do_orm_execute", record)
        return seen

    def run() -> None:
        with team.factory() as session:
            fire_linked.loan_payoff_suggestions_in_session(
                session,
                _ctx(team),
                currency="EUR",
                start_year=detail.scenario.start_year,
                last_year=detail.scenario.birth_year + detail.scenario.horizon_age,
                existing_events=(),
            )

    with_one = statements(run)
    _loan(team, name="Second")
    _loan(team, name="Third")
    with_three = statements(run)
    assert with_one == with_three <= 6


def test_workspace_emptiness_sees_fire_expense_exclusions(team: WorkspaceDb) -> None:
    from assetboard.services.archive import workspace_is_empty

    food = _category(team, "Food")
    scenario_id = _create_scenario(
        team, expenses_mode="trailing_12m", expense_exclusion_ids=(food,)
    )
    with team.factory() as session:
        assert workspace_is_empty(session, _ctx(team)) is False
    with team.factory.begin() as session:
        session.delete(session.get_one(FireScenario, scenario_id))
    with team.factory() as session:
        from assetboard.db.repositories import FireExpenseExclusionRepository

        assert FireExpenseExclusionRepository(session, _ctx(team)).any_exists() is False
    with team.factory.begin() as session:
        category = CategoryRepository(session, _ctx(team)).get_or_raise(food)
        CategoryRepository(session, _ctx(team)).delete(category)
    with team.factory() as session:
        assert workspace_is_empty(session, _ctx(team)) is True


def test_one_click_requires_an_editor_and_an_active_scenario(team: WorkspaceDb) -> None:
    loan_id = _loan(team)
    scenario_id = _create_scenario(team)
    with pytest.raises(PermissionDenied):
        fire_linked.add_loan_payoff_event(
            team.factory, team.ctx("viewer", "viewer"), scenario_id, loan_id
        )
    fire_scenarios.archive_fire_scenario(team.factory, _ctx(team), scenario_id)
    with pytest.raises(fire_linked.LinkedError) as exc:
        fire_linked.add_loan_payoff_event(team.factory, _ctx(team), scenario_id, loan_id)
    assert (exc.value.field, exc.value.code) == ("scenario", "archived")


def test_archived_and_investable_category_checks_keep_exclusions_valid(
    team: WorkspaceDb,
) -> None:
    from assetboard.services import categories

    food = _category(team, "Food")
    _create_scenario(team, expenses_mode="trailing_12m", expense_exclusion_ids=(food,))
    assert categories.delete_category(team.factory, _ctx(team), food) == "archived"
    categories.restore_category(team.factory, _ctx(team), food)
    with pytest.raises(categories.TreatmentChangeBlocked):
        categories.update_category(
            team.factory,
            _ctx(team),
            food,
            name="Food",
            kind="expense",
            treatment="savings",
            color="#16a34a",
        )
    with pytest.raises(categories.KindChangeBlocked):
        categories.update_category(
            team.factory,
            _ctx(team),
            food,
            name="Food",
            kind="income",
            treatment="regular",
            color="#16a34a",
        )
