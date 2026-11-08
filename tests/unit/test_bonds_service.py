"""Bond terms use cases and bond-model pricing (F6.5)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.models.identity import AuditEvent
from assetboard.db.repositories import (
    AssetClassRepository,
    BondTermsRepository,
    InstrumentRepository,
    InvestmentTransactionRepository,
)
from assetboard.services.bonds import (
    BondTermsError,
    BondTermsInput,
    bond_panel,
    bond_terms,
    save_bond_terms,
)
from assetboard.services.context import Context, PermissionDenied
from assetboard.services.investment_transactions import create_transaction
from assetboard.services.investments import (
    archive_instrument,
    create_asset_class,
    create_instrument,
    investment_page,
    update_instrument,
)
from assetboard.services.prices import (
    PriceError,
    instrument_prices,
    latest_prices,
    price_at,
    save_manual_price,
)
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


class TickingClock(FixedClock):
    def __init__(self) -> None:
        super().__init__("2026-03-10T12:00:00+00:00")
        self._ticks = 0

    def now(self) -> datetime:
        self._ticks += 1
        return super().now() + timedelta(seconds=self._ticks)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    for prepared in workspace_db(tmp_path):
        clock = TickingClock()
        prepared.clock = clock
        prepared.factory.kw["info"]["clock"] = clock
        yield prepared


@pytest.fixture
def team(db: WorkspaceDb) -> WorkspaceDb:
    db.add_member("olga", "owner")
    db.add_member("ed", "editor")
    db.add_member("vic", "viewer")
    return db


def _owner(db: WorkspaceDb) -> Context:
    return db.ctx("olga", "owner")


def _class_id(db: WorkspaceDb, ctx: Context) -> UUID:
    with db.factory() as session:
        existing = AssetClassRepository(session, ctx).active_by_name_key("bonds")
        if existing is not None:
            return existing.id
    return create_asset_class(db.factory, ctx, name="Bonds", color="#16a34a", investable=True).id


def _instrument(
    db: WorkspaceDb,
    ctx: Context,
    *,
    name: str = "Treasury bonds",
    type_value: str = "bond_model",
):
    return create_instrument(
        db.factory,
        ctx,
        name=name,
        symbol="",
        type=type_value,
        quote_currency="PLN",
        unit_label="bond",
        allocations=[(_class_id(db, ctx), 10_000)],
        locale="en",
    )


def _buy(
    db: WorkspaceDb,
    ctx: Context,
    instrument_id: UUID,
    *,
    day: str = "2026-01-01",
    quantity: str = "1",
    unit_price: str = "100",
):
    return create_transaction(
        db.factory,
        ctx,
        instrument_id=instrument_id,
        type_value="buy",
        date_value=day,
        quantity_value=quantity,
        unit_price_value=unit_price,
        fee_value="0",
        fx_value="1",
        note_value="",
        locale="en",
    )


def _sell(
    db: WorkspaceDb,
    ctx: Context,
    instrument_id: UUID,
    *,
    day: str = "2026-07-01",
    quantity: str = "1",
):
    return create_transaction(
        db.factory,
        ctx,
        instrument_id=instrument_id,
        type_value="sell",
        date_value=day,
        quantity_value=quantity,
        unit_price_value="100",
        fee_value="0",
        fx_value="1",
        note_value="",
        locale="en",
    )


def _values(**overrides: object) -> BondTermsInput:
    defaults: dict[str, object] = {
        "series_code": "OTS",
        "face_value": "100",
        "maturity_date": "2028-01-01",
        "interest_mode": "capitalized_annually",
        "rate_periods": (("2026-01-01", "5"),),
        "tax_rate": "19",
        "fee_mode": "",
        "fee_value": "",
    }
    defaults.update(overrides)
    return BondTermsInput(**defaults)  # type: ignore[arg-type]


def test_save_read_and_update_terms_without_amounts_in_the_audit(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx)
    view = save_bond_terms(db.factory, ctx, instrument.id, values=_values(), locale="en")
    assert view.series_code == "OTS"
    assert view.face_value.amount_minor == 10_000
    assert view.tax_rate_bp == 1_900
    assert view.rate_schedule[0].annual_rate_bp == 500
    assert bond_terms(db.factory, ctx, instrument.id) == view

    updated = save_bond_terms(
        db.factory,
        ctx,
        instrument.id,
        values=_values(series_code="EDO", tax_rate="0"),
        locale="en",
    )
    assert (updated.series_code, updated.tax_rate_bp) == ("EDO", 0)
    with db.factory() as session:
        events = list(
            session.scalars(select(AuditEvent).where(AuditEvent.action == "bond_terms.saved"))
        )
    assert len(events) == 2
    assert set(events[-1].data) == {"instrument_id", "fields"}
    assert "10000" not in str(events[-1].data)
    assert "series_code" in str(events[-1].data["fields"])

    page = investment_page(db.factory, ctx, locale="en")
    row = next(item for item in page.instruments if item.id == instrument.id)
    assert row.terms_required is False


def test_currency_change_before_transactions_relabels_the_terms(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx)
    save_bond_terms(db.factory, ctx, instrument.id, values=_values(), locale="en")
    updated = update_instrument(
        db.factory,
        ctx,
        instrument.id,
        name=instrument.name,
        symbol="",
        type="bond_model",
        quote_currency="EUR",
        unit_label="bond",
        allocations=[(instrument.allocations[0].asset_class_id, 10_000)],
        locale="en",
    )
    assert updated.quote_currency == "EUR"
    view = bond_terms(db.factory, ctx, instrument.id)
    assert view is not None and view.face_value.currency == "EUR"


def test_terms_are_required_until_they_exist(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx)
    page = investment_page(db.factory, ctx, locale="en")
    row = next(item for item in page.instruments if item.id == instrument.id)
    assert row.terms_required is True
    assert bond_terms(db.factory, ctx, instrument.id) is None
    assert bond_panel(db.factory, ctx, instrument.id) is None


@pytest.mark.parametrize(
    ("values", "field"),
    [
        (_values(rate_periods=()), "schedule"),
        (_values(rate_periods=(("2026-01-01", "5"), ("2027-01-02", "5"))), "schedule"),
        (_values(maturity_date="2026-01-01"), "maturity_date"),
        (_values(rate_periods=(("2026-01-01", "1001"),)), "schedule"),
        (_values(tax_rate="-1"), "tax_rate"),
        (_values(fee_mode="percent", fee_value="101"), "fee_value"),
        (_values(fee_mode="fixed", fee_value="-1"), "fee_value"),
        (_values(face_value="0"), "face_value"),
        (_values(interest_mode="unknown"), "interest_mode"),
    ],
)
def test_invalid_terms_are_refused(
    db: WorkspaceDb, team: WorkspaceDb, values: BondTermsInput, field: str
) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx)
    with pytest.raises(BondTermsError) as caught:
        save_bond_terms(db.factory, ctx, instrument.id, values=values, locale="en")
    assert caught.value.field == field


def test_only_bond_models_with_editor_rights_can_have_terms(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    other = _instrument(db, ctx, name="World ETF", type_value="etf")
    with pytest.raises(BondTermsError) as caught:
        save_bond_terms(db.factory, ctx, other.id, values=_values(), locale="en")
    assert caught.value.code == "not_bond_model"

    bonds = _instrument(db, ctx)
    archive_instrument(db.factory, ctx, bonds.id)
    with pytest.raises(BondTermsError) as caught:
        save_bond_terms(db.factory, ctx, bonds.id, values=_values(), locale="en")
    assert caught.value.code == "instrument_archived"

    with pytest.raises(PermissionDenied):
        save_bond_terms(
            db.factory, db.ctx("vic", "viewer"), bonds.id, values=_values(), locale="en"
        )


def test_panel_uses_fifo_lots_and_totals_the_net_value(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx)
    save_bond_terms(
        db.factory,
        ctx,
        instrument.id,
        values=_values(tax_rate="0", maturity_date="2028-01-01"),
        locale="en",
    )
    _buy(db, ctx, instrument.id, day="2026-01-01", quantity="2")
    _buy(db, ctx, instrument.id, day="2026-02-01", quantity="1")
    _sell(db, ctx, instrument.id, day="2026-02-01", quantity="1")

    panel = bond_panel(db.factory, ctx, instrument.id, on=date(2026, 3, 10))
    assert panel is not None
    assert panel.quantity == Decimal("2")
    assert panel.valuation.net.amount_minor == 20_144  # 200 + 68/365 + 37/365 of 5%
    assert panel.unit_price == Decimal("100.72")
    assert panel.valuation.fee.amount_minor == 0

    current = bond_panel(db.factory, ctx, instrument.id)
    assert current is not None and current.as_of == date(2026, 3, 10)
    assert current.quantity == Decimal("2")


def test_unit_price_keeps_decimal_type_scale_not_minor_units(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx)
    save_bond_terms(
        db.factory,
        ctx,
        instrument.id,
        values=_values(
            maturity_date="2026-04-01",
            rate_periods=(("2026-01-01", "5"),),
            tax_rate="0",
        ),
        locale="en",
    )
    _buy(db, ctx, instrument.id, day="2026-01-01", quantity="3")

    panel = bond_panel(db.factory, ctx, instrument.id, on=date(2026, 3, 10))
    assert panel is not None
    assert panel.valuation.net.amount_minor == 31_133
    expected = (Decimal("311.33") / Decimal(3)).quantize(Decimal("1e-18"))
    assert panel.unit_price == expected
    assert panel.unit_price is not None and panel.unit_price.as_tuple().exponent == -18


def test_undated_lots_count_only_in_current_valuation(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx)
    save_bond_terms(db.factory, ctx, instrument.id, values=_values(), locale="en")
    _buy(db, ctx, instrument.id, day="2026-02-01")
    with db.factory.begin() as session:
        InvestmentTransactionRepository(session, ctx).create(
            instrument_id=instrument.id,
            date=None,
            type="buy",
            quantity=Decimal("1"),
            unit_price=Decimal("100"),
            quote_currency="PLN",
            fee_minor=0,
            fx_to_base=Decimal("1"),
        )

    current = bond_panel(db.factory, ctx, instrument.id)
    historical = bond_panel(db.factory, ctx, instrument.id, on=date(2026, 3, 1))
    assert current is not None and current.quantity == Decimal("2")
    assert historical is not None and historical.quantity == Decimal("1")


def test_price_at_uses_the_model_and_refuses_manual_prices(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx)
    save_bond_terms(db.factory, ctx, instrument.id, values=_values(), locale="en")
    _buy(db, ctx, instrument.id, day="2026-01-01")

    price = price_at(db.factory, ctx, instrument.id, date(2026, 3, 10))
    assert price.source == "bond_model"
    assert price.price == Decimal("100.75")  # 100 + 5% x 68/365 less 19% tax, rounded once
    assert price.terms_missing is False
    assert instrument_prices(db.factory, ctx, instrument.id, on=date(2026, 3, 10)).latest == price
    batch = latest_prices(db.factory, ctx, (instrument.id,), date(2026, 3, 10))
    assert batch[instrument.id] == price

    with pytest.raises(PriceError) as caught:
        save_manual_price(
            db.factory,
            ctx,
            instrument.id,
            price_id=None,
            date_value="2026-03-10",
            price_value="101",
            locale="en",
        )
    assert caught.value.code == "bond_model_priced"


def test_missing_terms_fall_back_to_the_transaction_price(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx)
    _buy(db, ctx, instrument.id, day="2026-03-01", unit_price="100")

    price = price_at(db.factory, ctx, instrument.id, date(2026, 3, 10))
    assert (price.price, price.source, price.terms_missing) == (
        Decimal("100"),
        "transaction",
        True,
    )
    panel = instrument_prices(db.factory, ctx, instrument.id, on=date(2026, 3, 10))
    assert panel.latest.terms_missing is True


def test_every_bond_valuation_is_tenant_scoped(db: WorkspaceDb, team: WorkspaceDb) -> None:
    other = db.add_workspace("Other")
    db.add_member("bea", "owner", workspace_id=other)
    ctx = _owner(db)
    other_ctx = db.ctx("bea", "owner", workspace_id=other)
    instrument = _instrument(db, ctx)
    save_bond_terms(db.factory, ctx, instrument.id, values=_values(), locale="en")
    with db.factory() as session:
        repository = BondTermsRepository(session, other_ctx)
        assert repository.for_instrument(instrument.id) is None
        assert repository.instrument_ids() == set()
        assert InstrumentRepository(session, other_ctx).ordered() == []
