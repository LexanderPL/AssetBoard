"""Price and FX service (F6.3): carry-forward, priority, staleness, manual writes and batching."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError

from assetboard.clock import FixedClock
from assetboard.db.models.identity import AuditEvent
from assetboard.db.models.investments import InvestmentTransaction
from assetboard.db.models.prices import PricePoint
from assetboard.db.repositories import (
    AssetClassRepository,
    FxRateRepository,
    InvestmentTransactionRepository,
    NotFound,
    PricePointRepository,
)
from assetboard.services.context import Context, PermissionDenied
from assetboard.services.investment_transactions import create_transaction, delete_transaction
from assetboard.services.investments import (
    archive_instrument,
    create_asset_class,
    create_instrument,
)
from assetboard.services.prices import (
    PriceError,
    delete_manual_price,
    fx_rate,
    instrument_prices,
    latest_fx_rates,
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
        existing = AssetClassRepository(session, ctx).active_by_name_key("stocks")
        if existing is not None:
            return existing.id
    return create_asset_class(db.factory, ctx, name="Stocks", color="#2563eb", investable=True).id


def _instrument(
    db: WorkspaceDb,
    ctx: Context,
    *,
    name: str = "World ETF",
    type_value: str = "etf",
    quote_currency: str = "EUR",
):
    class_id = _class_id(db, ctx)
    return create_instrument(
        db.factory,
        ctx,
        name=name,
        symbol="",
        type=type_value,
        quote_currency=quote_currency,
        unit_label="share" if type_value != "currency" else quote_currency,
        allocations=[(class_id, 10_000)],
        locale="en",
    )


def _tx(
    db: WorkspaceDb,
    ctx: Context,
    instrument_id: UUID,
    *,
    date_value: str = "2026-03-01",
    unit_price: str = "100",
    fx: str = "1",
):
    return create_transaction(
        db.factory,
        ctx,
        instrument_id=instrument_id,
        type_value="buy",
        date_value=date_value,
        quantity_value="1",
        unit_price_value=unit_price,
        fee_value="0",
        fx_value=fx,
        note_value="",
        locale="en",
    )


def _manual(
    db: WorkspaceDb,
    ctx: Context,
    instrument_id: UUID,
    *,
    date_value: str = "2026-03-01",
    price_value: str = "100",
    price_id: UUID | None = None,
    locale: str = "en",
):
    return save_manual_price(
        db.factory,
        ctx,
        instrument_id,
        price_id=price_id,
        date_value=date_value,
        price_value=price_value,
        locale=locale,
    )


def _stored_price(
    db: WorkspaceDb,
    ctx: Context,
    instrument_id: UUID,
    *,
    date_value: date,
    price: str,
    source: str = "provider",
) -> PricePoint:
    with db.factory.begin() as session:
        return PricePointRepository(session, ctx).create(
            instrument_id=instrument_id,
            date=date_value,
            price=Decimal(price),
            source=source,
        )


def _stored_rate(
    db: WorkspaceDb,
    ctx: Context,
    *,
    quote_currency: str,
    date_value: date,
    rate: str,
    source: str = "nbp",
):
    with db.factory.begin() as session:
        return FxRateRepository(session, ctx).create(
            base_currency="EUR",
            quote_currency=quote_currency,
            date=date_value,
            rate=Decimal(rate),
            source=source,
        )


def _events(db: WorkspaceDb, prefix: str) -> list[AuditEvent]:
    with db.factory() as session:
        return list(
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.action.startswith(prefix))
                .order_by(AuditEvent.ts, AuditEvent.id)
            )
        )


def _price_rows(db: WorkspaceDb) -> list[PricePoint]:
    with db.factory() as session:
        return list(session.scalars(select(PricePoint).order_by(PricePoint.date)))


def test_price_at_is_missing_without_an_observation(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx)
    result = price_at(db.factory, ctx, instrument.id, date(2026, 3, 1))
    assert (result.price, result.as_of, result.source, result.stale, result.missing) == (
        None,
        None,
        None,
        False,
        True,
    )


def test_a_manual_price_is_carried_forward_and_stales_after_five_days(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx)
    _manual(db, ctx, instrument.id, date_value="2026-03-01", price_value="1234.56")

    same_day = price_at(db.factory, ctx, instrument.id, date(2026, 3, 1))
    assert (same_day.price, same_day.as_of, same_day.source) == (
        Decimal("1234.56"),
        date(2026, 3, 1),
        "manual",
    )
    assert not same_day.stale

    forward = price_at(db.factory, ctx, instrument.id, date(2026, 3, 6))
    assert (forward.price, forward.as_of) == (Decimal("1234.56"), date(2026, 3, 1))
    assert not forward.stale

    stale = price_at(db.factory, ctx, instrument.id, date(2026, 3, 7))
    assert stale.price == Decimal("1234.56")
    assert stale.stale

    before = price_at(db.factory, ctx, instrument.id, date(2026, 2, 28))
    assert before.missing

    panel = instrument_prices(db.factory, ctx, instrument.id, on=date(2026, 3, 7))
    assert [point.source for point in panel.points] == ["manual"]
    assert panel.latest.stale
    assert panel.effective[0].price == Decimal("1234.56")


def test_transaction_prices_are_derived_and_deleted_transactions_stop_contributing(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx)
    first = _tx(db, ctx, instrument.id, unit_price="100")
    second = _tx(db, ctx, instrument.id, unit_price="101")

    derived = price_at(db.factory, ctx, instrument.id, date(2026, 3, 1))
    assert (derived.price, derived.as_of, derived.source) == (
        Decimal("101"),
        date(2026, 3, 1),
        "transaction",
    )
    panel = instrument_prices(db.factory, ctx, instrument.id, on=date(2026, 3, 1))
    assert [point.source for point in panel.points] == ["transaction"]
    assert panel.points[0].price == Decimal("101")
    assert _price_rows(db) == []

    delete_transaction(db.factory, ctx, second.id)
    after_second = price_at(db.factory, ctx, instrument.id, date(2026, 3, 1))
    assert (after_second.price, after_second.source) == (Decimal("100"), "transaction")

    delete_transaction(db.factory, ctx, first.id)
    assert price_at(db.factory, ctx, instrument.id, date(2026, 3, 1)).missing


def test_the_latest_transaction_on_a_date_wins_by_created_at_then_id(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx)
    first = _tx(db, ctx, instrument.id, unit_price="100")
    second = _tx(db, ctx, instrument.id, unit_price="101")

    # Force the same creation time: the id is then the total-order tie-break.
    stamp = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    with db.factory.begin() as session:
        repository = InvestmentTransactionRepository(session, ctx)
        repository.update(first.id, created_at=stamp)
        repository.update(second.id, created_at=stamp)
    with db.factory() as session:
        rows = list(session.scalars(select(InvestmentTransaction)))
        expected = max(rows, key=lambda row: str(row.id)).unit_price

    result = price_at(db.factory, ctx, instrument.id, date(2026, 3, 1))
    assert result.price == expected


def test_price_priority_on_a_date_and_newer_dates(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx)
    _stored_price(db, ctx, instrument.id, date_value=date(2026, 3, 1), price="99")
    _stored_price(db, ctx, instrument.id, date_value=date(2026, 3, 1), price="100", source="manual")
    _tx(db, ctx, instrument.id, unit_price="98")

    same_date = price_at(db.factory, ctx, instrument.id, date(2026, 3, 1))
    assert (same_date.price, same_date.source) == (Decimal("100"), "manual")

    _tx(db, ctx, instrument.id, date_value="2026-03-02", unit_price="101")
    newer_transaction = price_at(db.factory, ctx, instrument.id, date(2026, 3, 2))
    assert (newer_transaction.price, newer_transaction.source) == (
        Decimal("101"),
        "transaction",
    )

    _stored_price(db, ctx, instrument.id, date_value=date(2026, 3, 3), price="102")
    newer_provider = price_at(db.factory, ctx, instrument.id, date(2026, 3, 3))
    assert (newer_provider.price, newer_provider.source) == (Decimal("102"), "provider")

    _stored_price(db, ctx, instrument.id, date_value=date(2026, 3, 4), price="103", source="manual")
    _stored_price(db, ctx, instrument.id, date_value=date(2026, 3, 4), price="104")
    newest = price_at(db.factory, ctx, instrument.id, date(2026, 3, 4))
    assert (newest.price, newest.source) == (Decimal("103"), "manual")


def test_instrument_prices_lists_points_newest_first_and_one_effective_point_per_date(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx)
    _stored_price(db, ctx, instrument.id, date_value=date(2026, 3, 1), price="99")
    _stored_price(db, ctx, instrument.id, date_value=date(2026, 3, 1), price="100", source="manual")
    _tx(db, ctx, instrument.id, unit_price="98")
    _tx(db, ctx, instrument.id, date_value="2026-03-02", unit_price="101")

    panel = instrument_prices(db.factory, ctx, instrument.id, on=date(2026, 3, 2))
    assert [(point.date, point.price, point.source) for point in panel.points] == [
        (date(2026, 3, 2), Decimal("101"), "transaction"),
        (date(2026, 3, 1), Decimal("100"), "manual"),
        (date(2026, 3, 1), Decimal("99"), "provider"),
        (date(2026, 3, 1), Decimal("98"), "transaction"),
    ]
    assert [(point.date, point.price, point.source) for point in panel.effective] == [
        (date(2026, 3, 1), Decimal("100"), "manual"),
        (date(2026, 3, 2), Decimal("101"), "transaction"),
    ]
    assert (panel.latest.price, panel.latest.as_of, panel.latest.source) == (
        Decimal("101"),
        date(2026, 3, 2),
        "transaction",
    )
    derived = [point for point in panel.points if point.source == "transaction"]
    assert all(point.id is None and not point.editable for point in derived)


def test_a_currency_instrument_is_always_priced_at_one_without_a_point(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    instrument = _instrument(
        db, ctx, name="US dollars", type_value="currency", quote_currency="USD"
    )
    result = price_at(db.factory, ctx, instrument.id, date(2026, 3, 1))
    assert (result.price, result.as_of, result.source, result.stale) == (
        Decimal(1),
        date(2026, 3, 1),
        None,
        False,
    )
    panel = instrument_prices(db.factory, ctx, instrument.id, on=date(2026, 3, 1))
    assert panel.points == ()
    assert panel.effective == ()
    assert panel.latest.price == 1
    assert _price_rows(db) == []


def test_latest_prices_batches_instruments_points_and_transactions(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    first = _instrument(db, ctx, name="First")
    second = _instrument(db, ctx, name="Second")
    third = _instrument(db, ctx, name="Cash", type_value="currency", quote_currency="USD")
    _stored_price(db, ctx, first.id, date_value=date(2026, 3, 1), price="10")
    _tx(db, ctx, second.id, unit_price="20")

    statements: list[str] = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", record)
    try:
        result = latest_prices(db.factory, ctx, (first.id, second.id, third.id), date(2026, 3, 1))
    finally:
        event.remove(db.engine, "before_cursor_execute", record)

    assert result[first.id].price == Decimal("10")
    assert result[first.id].source == "provider"
    assert result[second.id].price == Decimal("20")
    assert result[second.id].source == "transaction"
    assert result[third.id].price == 1
    assert sum("FROM price_points" in statement for statement in statements) == 1
    assert sum("FROM investment_transactions" in statement for statement in statements) == 1
    assert sum("FROM instruments" in statement for statement in statements) == 1


def test_stored_price_sources_are_only_manual_or_provider(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx)
    for source in ("manual", "provider"):
        _stored_price(db, ctx, instrument.id, date_value=date(2026, 3, 1), price="1", source=source)
    with pytest.raises(IntegrityError), db.factory.begin() as session:
        session.add(
            PricePoint(
                workspace_id=ctx.workspace.id,
                instrument_id=instrument.id,
                date=date(2026, 3, 2),
                price=Decimal("1"),
                source="transaction",
            )
        )


def test_fx_repository_refuses_non_positive_rates(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    with pytest.raises(ValueError, match="positive finite"):
        _stored_rate(db, ctx, quote_currency="USD", date_value=date(2026, 3, 1), rate="0")
    with pytest.raises(ValueError, match="positive finite"):
        _stored_rate(db, ctx, quote_currency="USD", date_value=date(2026, 3, 1), rate="-1")


def test_fx_rate_triangulates_through_the_base_currency(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    _stored_rate(db, ctx, quote_currency="USD", date_value=date(2026, 3, 1), rate="0.92")
    _stored_rate(db, ctx, quote_currency="GBP", date_value=date(2026, 3, 1), rate="1.17")

    usd_to_eur = fx_rate(db.factory, ctx, "USD", "EUR", date(2026, 3, 1))
    assert usd_to_eur.rate == Decimal("0.92")
    assert (usd_to_eur.as_of, usd_to_eur.source) == (date(2026, 3, 1), "nbp")

    with localcontext() as context:
        context.prec = 80
        expected_eur_to_usd = Decimal(1) / Decimal("0.92")
        expected_usd_to_gbp = Decimal("0.92") / Decimal("1.17")
    assert fx_rate(db.factory, ctx, "EUR", "USD", date(2026, 3, 1)).rate == expected_eur_to_usd
    assert fx_rate(db.factory, ctx, "USD", "GBP", date(2026, 3, 1)).rate == expected_usd_to_gbp

    identity = fx_rate(db.factory, ctx, "EUR", "EUR", date(2026, 3, 1))
    assert (identity.rate, identity.as_of, identity.source, identity.stale) == (
        Decimal(1),
        date(2026, 3, 1),
        None,
        False,
    )
    assert fx_rate(db.factory, ctx, "USD", "JPY", date(2026, 3, 1)).missing
    with pytest.raises(ValueError, match="unsupported currency"):
        fx_rate(db.factory, ctx, "USD", "ZZZ", date(2026, 3, 1))


def test_fx_rate_priority_and_transaction_fallback(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx, name="US fund", quote_currency="USD")
    _stored_rate(db, ctx, quote_currency="USD", date_value=date(2026, 3, 1), rate="0.90")
    _stored_rate(
        db,
        ctx,
        quote_currency="USD",
        date_value=date(2026, 3, 1),
        rate="0.91",
        source="manual",
    )
    _tx(db, ctx, instrument.id, fx="0.92")

    same_date = fx_rate(db.factory, ctx, "USD", "EUR", date(2026, 3, 1))
    assert (same_date.rate, same_date.source) == (Decimal("0.91"), "manual")

    _tx(db, ctx, instrument.id, date_value="2026-03-02", fx="0.93")
    newer = fx_rate(db.factory, ctx, "USD", "EUR", date(2026, 3, 2))
    assert (newer.rate, newer.source) == (Decimal("0.93"), "transaction")

    _stored_rate(db, ctx, quote_currency="USD", date_value=date(2026, 3, 3), rate="0.94")
    provider = fx_rate(db.factory, ctx, "USD", "EUR", date(2026, 3, 3))
    assert (provider.rate, provider.source) == (Decimal("0.94"), "nbp")

    # The derived rows are not stored, so deleting the transaction changes the rate immediately.
    with db.factory() as session:
        row_id = session.scalars(
            select(InvestmentTransaction.id)
            .where(InvestmentTransaction.date == date(2026, 3, 2))
            .order_by(InvestmentTransaction.created_at.desc(), InvestmentTransaction.id)
        ).first()
    assert row_id is not None
    delete_transaction(db.factory, ctx, row_id)
    after_delete = fx_rate(db.factory, ctx, "USD", "EUR", date(2026, 3, 2))
    assert (after_delete.rate, after_delete.source) == (Decimal("0.91"), "manual")


def test_fx_from_transactions_alone_is_the_lowest_priority_source(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx, name="US fund", quote_currency="USD")
    _tx(db, ctx, instrument.id, fx="1.10")
    _tx(db, ctx, instrument.id, fx="1.20")
    result = fx_rate(db.factory, ctx, "USD", "EUR", date(2026, 3, 1))
    assert (result.rate, result.as_of, result.source) == (
        Decimal("1.20"),
        date(2026, 3, 1),
        "transaction",
    )

    _stored_rate(db, ctx, quote_currency="USD", date_value=date(2026, 3, 1), rate="1.11")
    wins = fx_rate(db.factory, ctx, "USD", "EUR", date(2026, 3, 1))
    assert (wins.rate, wins.source) == (Decimal("1.11"), "nbp")


def test_fx_staleness_uses_the_oldest_leg_and_the_requested_date(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    _stored_rate(db, ctx, quote_currency="USD", date_value=date(2026, 3, 1), rate="0.92")
    _stored_rate(
        db, ctx, quote_currency="GBP", date_value=date(2026, 3, 3), rate="1.17", source="ecb"
    )

    exact = fx_rate(db.factory, ctx, "USD", "EUR", date(2026, 3, 6))
    assert not exact.stale
    stale = fx_rate(db.factory, ctx, "USD", "EUR", date(2026, 3, 7))
    assert stale.stale

    triangulated = fx_rate(db.factory, ctx, "USD", "GBP", date(2026, 3, 7))
    assert triangulated.as_of == date(2026, 3, 1)
    assert triangulated.stale
    assert triangulated.source is None


def test_latest_fx_rates_batches_and_includes_the_base_currency(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    _stored_rate(db, ctx, quote_currency="USD", date_value=date(2026, 3, 1), rate="0.92")
    _stored_rate(db, ctx, quote_currency="GBP", date_value=date(2026, 3, 2), rate="1.17")

    statements: list[str] = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", record)
    try:
        result = latest_fx_rates(db.factory, ctx, ("eur", "usd", "gbp", "jpy"), date(2026, 3, 2))
    finally:
        event.remove(db.engine, "before_cursor_execute", record)

    assert result["EUR"].rate == 1
    assert result["EUR"].as_of == date(2026, 3, 2)
    assert result["USD"].rate == Decimal("0.92")
    assert result["GBP"].rate == Decimal("1.17")
    assert result["JPY"].missing
    assert sum("FROM fx_rates" in statement for statement in statements) == 1
    assert sum("FROM investment_transactions" in statement for statement in statements) == 1


def test_manual_price_create_update_edit_and_delete_are_audited(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx)

    created = _manual(db, ctx, instrument.id, date_value="2026-03-01", price_value="100")
    assert (created.date, created.price, created.source, created.editable) == (
        date(2026, 3, 1),
        Decimal("100"),
        "manual",
        True,
    )
    assert len(_price_rows(db)) == 1

    updated = _manual(db, ctx, instrument.id, date_value="2026-03-01", price_value="101")
    assert (updated.id, updated.price) == (created.id, Decimal("101"))
    assert len(_price_rows(db)) == 1

    moved = _manual(
        db,
        ctx,
        instrument.id,
        date_value="2026-03-02",
        price_value="102",
        price_id=created.id,
    )
    assert (moved.id, moved.date, moved.price) == (
        created.id,
        date(2026, 3, 2),
        Decimal("102"),
    )

    assert created.id is not None
    delete_manual_price(db.factory, ctx, instrument.id, created.id)
    assert _price_rows(db) == []
    assert price_at(db.factory, ctx, instrument.id, date(2026, 3, 2)).missing

    actions = [event.action for event in _events(db, "price_point.")]
    assert actions == [
        "price_point.manual_created",
        "price_point.manual_updated",
        "price_point.manual_updated",
        "price_point.manual_deleted",
    ]
    created_event = _events(db, "price_point.manual_created")[0]
    assert created_event.entity_type == "price_point"
    assert created_event.data == {
        "instrument": instrument.name,
        "date": "2026-03-01",
        "price": "100",
        "currency": "EUR",
    }


@pytest.mark.parametrize(
    ("price", "code"),
    [
        ("-1", "invalid_price"),
        ("abc", "invalid_price"),
        ("", "invalid_price"),
        ("0.0000000000000000001", "invalid_price"),
        ("1" * 21, "invalid_price"),
    ],
)
def test_manual_price_validation(db: WorkspaceDb, team: WorkspaceDb, price: str, code: str) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx)
    with pytest.raises(PriceError) as caught:
        _manual(db, ctx, instrument.id, price_value=price)
    assert (caught.value.field, caught.value.code) == ("price", code)
    assert _price_rows(db) == []


@pytest.mark.parametrize(
    ("date_value", "code"),
    [
        ("2026-13-01", "invalid_date"),
        ("2026-02-30", "invalid_date"),
        ("1899-12-31", "invalid_date"),
        ("2200-01-01", "invalid_date"),
        ("2026-03-11", "future_date"),
    ],
)
def test_manual_price_date_validation(
    db: WorkspaceDb, team: WorkspaceDb, date_value: str, code: str
) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx)
    with pytest.raises(PriceError) as caught:
        _manual(db, ctx, instrument.id, date_value=date_value, price_value="1")
    assert caught.value.field == "date"
    assert caught.value.code == code


def test_manual_prices_accept_zero_and_locale_numbers(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx)
    zero = _manual(db, ctx, instrument.id, date_value="2026-03-01", price_value="0")
    assert zero.price == 0

    polish = _manual(
        db,
        ctx,
        instrument.id,
        date_value="2026-03-02",
        price_value="1 234,56",
        locale="pl",
    )
    assert polish.price == Decimal("1234.56")

    english = _manual(
        db,
        ctx,
        instrument.id,
        date_value="2026-03-03",
        price_value="1,234.56",
        locale="en",
    )
    assert english.price == Decimal("1234.56")


def test_manual_price_cannot_move_onto_another_manual_date(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx)
    first = _manual(db, ctx, instrument.id, date_value="2026-03-01", price_value="1")
    _manual(db, ctx, instrument.id, date_value="2026-03-02", price_value="2")

    with pytest.raises(PriceError) as caught:
        _manual(
            db,
            ctx,
            instrument.id,
            date_value="2026-03-02",
            price_value="3",
            price_id=first.id,
        )
    assert (caught.value.field, caught.value.code) == ("date", "duplicate_date")


def test_manual_price_rejects_provider_rows_and_other_instruments(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    first = _instrument(db, ctx, name="First")
    second = _instrument(db, ctx, name="Second")
    provider = _stored_price(
        db, ctx, first.id, date_value=date(2026, 3, 1), price="1", source="provider"
    )
    other_manual = _manual(db, ctx, second.id, date_value="2026-03-01", price_value="2")

    with pytest.raises(PriceError) as provider_error:
        _manual(
            db,
            ctx,
            first.id,
            date_value="2026-03-02",
            price_value="3",
            price_id=provider.id,
        )
    assert provider_error.value.code == "invalid_price"

    with pytest.raises(PriceError) as foreign_error:
        _manual(
            db,
            ctx,
            first.id,
            date_value="2026-03-02",
            price_value="3",
            price_id=other_manual.id,
        )
    assert foreign_error.value.code == "invalid_price"

    assert provider.id is not None
    with pytest.raises(PriceError) as delete_error:
        delete_manual_price(db.factory, ctx, first.id, provider.id)
    assert delete_error.value.code == "not_manual"

    assert other_manual.id is not None
    with pytest.raises(PriceError) as wrong_instrument:
        delete_manual_price(db.factory, ctx, first.id, other_manual.id)
    assert wrong_instrument.value.code == "invalid_price"


def test_manual_prices_are_refused_for_archived_instruments(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx)
    point = _manual(db, ctx, instrument.id, date_value="2026-03-01", price_value="1")
    archive_instrument(db.factory, ctx, instrument.id)

    with pytest.raises(PriceError) as save_error:
        _manual(db, ctx, instrument.id, date_value="2026-03-02", price_value="2")
    assert (save_error.value.field, save_error.value.code) == (
        "instrument",
        "instrument_archived",
    )

    assert point.id is not None
    with pytest.raises(PriceError) as delete_error:
        delete_manual_price(db.factory, ctx, instrument.id, point.id)
    assert delete_error.value.code == "instrument_archived"


def test_viewers_cannot_write_manual_prices(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = db.ctx("vic", "viewer")
    instrument = _instrument(db, team.ctx("olga", "owner"))
    with pytest.raises(PermissionDenied):
        _manual(db, ctx, instrument.id, date_value="2026-03-01", price_value="1")


def test_rows_of_another_workspace_are_not_found(db: WorkspaceDb, team: WorkspaceDb) -> None:
    other_workspace = db.add_workspace("Other")
    db.add_member("bob", "owner", workspace_id=other_workspace)
    other = db.ctx("bob", "owner", other_workspace)
    other_instrument = _instrument(db, other, name="Elsewhere")
    other_point = _manual(db, other, other_instrument.id, date_value="2026-03-01", price_value="1")
    mine = _owner(db)
    my_instrument = _instrument(db, mine, name="Mine")

    with pytest.raises(NotFound):
        _manual(
            db,
            mine,
            my_instrument.id,
            date_value="2026-03-02",
            price_value="2",
            price_id=other_point.id,
        )
    assert other_point.id is not None
    with pytest.raises(NotFound):
        delete_manual_price(db.factory, mine, my_instrument.id, other_point.id)
