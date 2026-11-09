"""Portfolio page service (F6.6): valuation inputs, exclusions, realized P&L and scope."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from assetboard.db.models.identity import Workspace
from assetboard.db.repositories import AssetClassRepository, InvestmentTransactionRepository
from assetboard.domain.money import Money
from assetboard.domain.portfolio import Missing
from assetboard.services import portfolio as portfolio_service
from assetboard.services.investment_transactions import create_transaction
from assetboard.services.investments import (
    archive_asset_class,
    archive_instrument,
    create_asset_class,
    create_instrument,
    seed_asset_classes,
    update_asset_class,
)
from assetboard.services.portfolio import default_scope_keys, portfolio_page
from assetboard.services.prices import save_manual_price
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    yield from workspace_db(tmp_path)  # base EUR, today 2026-03-01 (UTC)


@pytest.fixture
def team(db: WorkspaceDb) -> WorkspaceDb:
    db.add_member("olga", "owner")
    db.add_member("vic", "viewer")
    return db


def _owner(db: WorkspaceDb):
    return db.ctx("olga", "owner")


def _classes(db: WorkspaceDb, locale: str = "en") -> dict[str, UUID]:
    ctx = _owner(db)
    seed_asset_classes(db.factory, ctx, locale=locale)
    with db.factory() as session:
        return {row.name: row.id for row in AssetClassRepository(session, ctx).ordered()}


def _instrument(
    db: WorkspaceDb,
    name: str,
    allocations: list[tuple[UUID, int]],
    *,
    type_value: str = "etf",
    currency: str = "EUR",
    unit: str = "share",
) -> UUID:
    return create_instrument(
        db.factory,
        _owner(db),
        name=name,
        symbol="",
        type=type_value,
        quote_currency=currency,
        unit_label=unit,
        allocations=allocations,
        locale="en",
    ).id


def _trade(
    db: WorkspaceDb,
    instrument_id: UUID,
    kind: str,
    day: str,
    quantity: str,
    price: str,
    *,
    fx: str = "1",
) -> None:
    create_transaction(
        db.factory,
        _owner(db),
        instrument_id=instrument_id,
        type_value=kind,
        date_value=day,
        quantity_value=quantity,
        unit_price_value=price,
        fee_value="0",
        fx_value=fx,
        note_value="",
        locale="en",
    )


def _undated_buy(
    db: WorkspaceDb, instrument_id: UUID, quantity: str, price: str, currency: str = "EUR"
) -> None:
    """An imported legacy lot (F6.9): no date, so it gives neither a price nor an FX rate."""
    with db.factory.begin() as session:
        InvestmentTransactionRepository(session, _owner(db)).create(
            instrument_id=instrument_id,
            date=None,
            type="buy",
            quantity=Decimal(quantity),
            unit_price=Decimal(price),
            quote_currency=currency,
            fee_minor=0,
            fx_to_base=Decimal("0.9") if currency != "EUR" else None,
        )


def _price(db: WorkspaceDb, instrument_id: UUID, day: str, price: str) -> None:
    save_manual_price(
        db.factory,
        _owner(db),
        instrument_id,
        price_id=None,
        date_value=day,
        price_value=price,
        locale="en",
    )


def test_values_holdings_classes_and_totals(db: WorkspaceDb, team: WorkspaceDb) -> None:
    classes = _classes(db)
    fund = _instrument(db, "Balanced fund", [(classes["Stocks"], 8000), (classes["Bonds"], 2000)])
    _trade(db, fund, "buy", "2026-02-27", "10", "100")
    _price(db, fund, "2026-03-01", "110")

    page = portfolio_page(db.factory, _owner(db))

    valuation = page.valuation
    assert (valuation.value, valuation.cost, valuation.gain) == (
        Money(110_000, "EUR"),
        Money(100_000, "EUR"),
        Money(10_000, "EUR"),
    )
    assert valuation.gain_tenths == 100
    assert [
        (row.asset_class.name if row.asset_class else None, row.total.value)
        for row in (page.class_rows)
    ] == [("Stocks", Money(88_000, "EUR")), ("Bonds", Money(22_000, "EUR"))]
    assert [row.total.cost for row in page.class_rows] == [
        Money(80_000, "EUR"),
        Money(20_000, "EUR"),
    ]
    assert page.today == date(2026, 3, 1)
    assert page.scope is not None and page.scope.name == "Stocks"  # the seeded default
    assert [(s.category_id, s.amount) for s in page.slices] == [(fund, Money(88_000, "EUR"))]
    assert page.prices[fund].source == "manual"


def test_realized_pnl_is_split_by_year_and_sold_out_instruments_count(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    classes = _classes(db)
    sold = _instrument(db, "Sold out", [(classes["Stocks"], 10_000)])
    _trade(db, sold, "buy", "2025-06-01", "10", "10")
    _trade(db, sold, "sell", "2025-12-31", "5", "12")  # +10.00 last year
    _trade(db, sold, "sell", "2026-01-02", "5", "9")  # -5.00 this year
    legacy = _instrument(db, "Legacy", [(classes["Stocks"], 10_000)])
    _undated_buy(db, legacy, "4", "20")
    _trade(db, legacy, "sell", "2026-02-01", "1", "26")  # +6.00 from an undated lot
    archive_instrument(db.factory, _owner(db), sold)

    page = portfolio_page(db.factory, _owner(db))

    assert (page.realized.year_to_date, page.realized.all_time) == (
        Money(100, "EUR"),  # -5.00 + 6.00
        Money(1_100, "EUR"),  # +10.00 - 5.00 + 6.00
    )
    assert [row.instrument_id for row in page.valuation.rows] == [legacy]  # sold out: no row


@pytest.mark.parametrize("missing", [Missing.PRICE, Missing.FX])
def test_missing_price_or_fx_keeps_the_row_and_excludes_value_and_cost(
    db: WorkspaceDb, team: WorkspaceDb, missing: Missing
) -> None:
    classes = _classes(db)
    priced = _instrument(db, "Priced", [(classes["Stocks"], 10_000)])
    _trade(db, priced, "buy", "2026-02-27", "1", "50")
    if missing is Missing.PRICE:
        gap = _instrument(db, "No price", [(classes["Stocks"], 10_000)])
        _undated_buy(db, gap, "2", "30")
    else:
        gap = _instrument(db, "US fund", [(classes["Stocks"], 10_000)], currency="USD")
        _undated_buy(db, gap, "2", "30", currency="USD")
        _price(db, gap, "2026-03-01", "31")

    page = portfolio_page(db.factory, _owner(db))

    rows = {row.instrument_id: row for row in page.valuation.rows}
    assert rows[gap].missing is missing
    assert rows[gap].value is None and rows[gap].cost_basis.amount_minor > 0
    assert (page.valuation.value, page.valuation.cost) == (
        Money(5_000, "EUR"),
        Money(5_000, "EUR"),
    )
    assert page.valuation.excluded == 1
    assert [row.total.cost for row in page.class_rows] == [Money(5_000, "EUR")]
    assert [s.category_id for s in page.slices] == [priced]


def test_stale_price_and_stale_fx_are_still_valued(db: WorkspaceDb, team: WorkspaceDb) -> None:
    classes = _classes(db)
    old_price = _instrument(db, "Old price", [(classes["Stocks"], 10_000)])
    _trade(db, old_price, "buy", "2026-02-01", "1", "40")
    _price(db, old_price, "2026-02-20", "45")
    us = _instrument(db, "US fund", [(classes["Stocks"], 10_000)], currency="USD")
    _trade(db, us, "buy", "2026-02-01", "2", "50", fx="0.9")
    _price(db, us, "2026-03-01", "55")

    page = portfolio_page(db.factory, _owner(db))

    assert page.prices[old_price].stale and not page.prices[us].stale
    assert page.fx["USD"].stale
    rows = {row.instrument_id: row for row in page.valuation.rows}
    assert rows[old_price].value == Money(4_500, "EUR")
    assert rows[us].value == Money(9_900, "EUR")  # 2 × 55 × 0.9
    assert page.valuation.excluded == 0


def test_bond_model_without_terms_uses_the_transaction_price(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    classes = _classes(db)
    bond = _instrument(
        db, "Savings bond", [(classes["Bonds"], 10_000)], type_value="bond_model", unit="bond"
    )
    _trade(db, bond, "buy", "2026-02-27", "3", "100")

    page = portfolio_page(db.factory, _owner(db))

    assert page.prices[bond].terms_missing
    assert page.valuation.rows[0].value == Money(30_000, "EUR")


@pytest.mark.parametrize("locale", ["en", "pl"])
def test_default_scope_is_the_seeded_stocks_class_in_either_language(
    db: WorkspaceDb, team: WorkspaceDb, locale: str
) -> None:
    classes = _classes(db, locale)
    stocks = next(class_id for name, class_id in classes.items() if name in {"Stocks", "Akcje"})
    other = next(class_id for class_id in classes.values() if class_id != stocks)
    fund = _instrument(db, "World", [(stocks, 6000), (other, 4000)])
    _trade(db, fund, "buy", "2026-02-27", "1", "100")

    page = portfolio_page(db.factory, _owner(db))

    assert page.scope is not None and page.scope.id == stocks
    assert [s.amount for s in page.slices] == [Money(6_000, "EUR")]  # the look-through part
    assert {"stocks", "akcje"} <= default_scope_keys()


def test_explicit_all_and_unknown_scopes(db: WorkspaceDb, team: WorkspaceDb) -> None:
    classes = _classes(db)
    fund = _instrument(db, "World", [(classes["Stocks"], 6000), (classes["Bonds"], 4000)])
    _trade(db, fund, "buy", "2026-02-27", "1", "100")
    ctx = _owner(db)

    bonds = portfolio_page(db.factory, ctx, scope=str(classes["Bonds"]))
    assert bonds.scope is not None and bonds.scope.id == classes["Bonds"]
    assert [s.amount for s in bonds.slices] == [Money(4_000, "EUR")]
    whole = portfolio_page(db.factory, ctx, scope="all")
    assert whole.scope is None and [s.amount for s in whole.slices] == [Money(10_000, "EUR")]
    for unknown in (str(uuid4()), "not-a-uuid", ""):
        fallback = portfolio_page(db.factory, ctx, scope=unknown)
        assert fallback.scope is not None and fallback.scope.id == classes["Stocks"]
    assert [option.name for option in whole.scope_options] == ["Stocks", "Bonds"]


def test_scope_falls_back_to_the_whole_portfolio_without_stocks(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    classes = _classes(db)
    gold = _instrument(db, "Gold", [(classes["Metals"], 10_000)], type_value="metal", unit="oz")
    _trade(db, gold, "buy", "2026-02-27", "1", "2000")
    ctx = _owner(db)
    assert portfolio_page(db.factory, ctx).scope is None  # Stocks holds nothing

    update_asset_class(
        db.factory, ctx, classes["Stocks"], name="Equities", color="#2563eb", investable=True
    )
    archive_asset_class(db.factory, ctx, classes["Stocks"])
    page = portfolio_page(db.factory, ctx)
    assert page.scope is None
    assert [s.category_id for s in page.slices] == [gold]


def test_renamed_stocks_class_is_not_the_default(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    equities = create_asset_class(
        db.factory, ctx, name="Equities", color="#2563eb", investable=True
    ).id
    fund = _instrument(db, "World", [(equities, 10_000)])
    _trade(db, fund, "buy", "2026-02-27", "1", "100")
    assert portfolio_page(db.factory, ctx).scope is None


def test_archived_class_with_allocations_is_listed(db: WorkspaceDb, team: WorkspaceDb) -> None:
    classes = _classes(db)
    fund = _instrument(db, "World", [(classes["Stocks"], 5000), (classes["Crypto"], 5000)])
    _trade(db, fund, "buy", "2026-02-27", "1", "100")
    archive_asset_class(db.factory, _owner(db), classes["Crypto"])

    page = portfolio_page(db.factory, _owner(db))

    rows = [
        (row.asset_class.name, row.asset_class.archived)
        for row in page.class_rows
        if row.asset_class
    ]
    assert rows == [("Stocks", False), ("Crypto", True)]


def test_one_price_and_one_fx_lookup_for_all_holdings(
    db: WorkspaceDb, team: WorkspaceDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    classes = _classes(db)
    for index in range(4):
        instrument = _instrument(
            db,
            f"Fund {index}",
            [(classes["Stocks"], 10_000)],
            currency="USD" if index % 2 else "EUR",
        )
        _trade(db, instrument, "buy", "2026-02-27", "1", "10", fx="0.9")
    calls = {"prices": 0, "fx": 0}
    real_prices = portfolio_service.latest_prices_in_session
    real_fx = portfolio_service.latest_fx_rates_in_session

    def prices(*args, **kwargs):
        calls["prices"] += 1
        return real_prices(*args, **kwargs)

    def fx(*args, **kwargs):
        calls["fx"] += 1
        return real_fx(*args, **kwargs)

    monkeypatch.setattr(portfolio_service, "latest_prices_in_session", prices)
    monkeypatch.setattr(portfolio_service, "latest_fx_rates_in_session", fx)

    page = portfolio_page(db.factory, _owner(db))

    assert calls == {"prices": 1, "fx": 1}
    assert len(page.valuation.rows) == 4


def test_viewers_can_read_and_an_empty_workspace_is_empty(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    page = portfolio_page(db.factory, db.ctx("vic", "viewer"))
    assert page.valuation.rows == () and page.class_rows == () and page.slices == ()
    assert page.scope is None
    assert page.realized.all_time == Money(0, "EUR")


def test_the_workspace_base_currency_is_used(db: WorkspaceDb, team: WorkspaceDb) -> None:
    with db.factory.begin() as session:
        workspace = session.get(Workspace, db.workspace_id)
        assert workspace is not None
        workspace.base_currency = "PLN"
    assert portfolio_page(db.factory, _owner(db)).currency == "PLN"
