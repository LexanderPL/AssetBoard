"""Prices, FX and manual price entry (F6.3).

Stored points are manual or provider observations. Transaction prices and FX rates are derived
from live investment transactions at read time and never stored. On one date the priority is
manual > provider > transaction; a newer date always wins. FX rates are base-currency units per
one quote-currency unit, so ``rate(from, to) = rate(base, from) / rate(base, to)``.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, localcontext
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.engine import run_immediate
from assetboard.db.models.investments import Instrument, InvestmentTransaction
from assetboard.db.models.prices import PricePoint
from assetboard.db.repositories import (
    AuditEventRepository,
    BondTermsRepository,
    CurrentWorkspaceRepository,
    FxRateRepository,
    InstrumentRepository,
    InvestmentTransactionRepository,
    PricePointRepository,
)
from assetboard.domain.bonds import bond_value
from assetboard.domain.money import minor_unit_for
from assetboard.domain.prices import (
    DEFAULT_STALE_AFTER_DAYS,
    FxProvider,
    PriceProvider,
    ProviderInstrument,
    ProviderQuote,
    ProviderRateLimit,
    is_stale,
    source_rank,
)
from assetboard.i18n.numbers import parse_amount
from assetboard.services.bonds import bond_unit_price, remaining_lots, terms_from_row
from assetboard.services.context import Context, Role, require_role, require_role_now

__all__ = [
    "DEFAULT_STALE_AFTER_DAYS",
    "FakeProvider",
    "FxAt",
    "ManualProvider",
    "PriceAt",
    "PricePanel",
    "PricePointView",
    "PriceProvider",
    "ProviderInstrument",
    "ProviderQuote",
    "ProviderRateLimit",
    "FxProvider",
    "delete_manual_price",
    "fx_rate",
    "instrument_prices",
    "latest_fx_rates",
    "latest_fx_rates_in_session",
    "latest_prices",
    "latest_prices_in_session",
    "price_at",
    "save_manual_price",
]

_DECIMAL_PRECISION = 80


class ManualProvider:
    """The manual path is written directly by the UI; it never fetches remote quotes."""

    key = "manual"
    rate_limit = ProviderRateLimit(0, 0)

    def supports(self, instrument: ProviderInstrument) -> bool:
        del instrument
        return True

    def fetch_latest(
        self,
        symbols: Sequence[str],
        quote_currency: str = "",
        *,
        wait: bool = True,
        settings: Mapping[str, str] | None = None,
    ) -> Mapping[str, ProviderQuote]:
        del symbols, quote_currency, wait, settings
        return {}

    def fetch_history(
        self,
        symbol: str,
        start: date,
        end: date,
        quote_currency: str = "",
        *,
        wait: bool = True,
        settings: Mapping[str, str] | None = None,
    ) -> Sequence[ProviderQuote]:
        del symbol, start, end, quote_currency, wait, settings
        return ()


class FakeProvider:
    """A deterministic provider for tests and F6.4 adapters."""

    rate_limit = ProviderRateLimit(1, 60)

    def __init__(
        self,
        *,
        key: str = "fake",
        latest: Mapping[str, ProviderQuote] | None = None,
        history: Mapping[str, tuple[ProviderQuote, ...]] | None = None,
        supported_types: Collection[str] | None = None,
    ) -> None:
        self.key = key
        self._latest = dict(latest or {})
        self._history = dict(history or {})
        self._supported_types = set(supported_types or ())

    def supports(self, instrument: ProviderInstrument) -> bool:
        return not self._supported_types or instrument.type in self._supported_types

    def fetch_latest(
        self,
        symbols: Sequence[str],
        quote_currency: str = "",
        *,
        wait: bool = True,
        settings: Mapping[str, str] | None = None,
    ) -> Mapping[str, ProviderQuote]:
        del quote_currency, wait, settings
        return {symbol: self._latest[symbol] for symbol in symbols if symbol in self._latest}

    def fetch_history(
        self,
        symbol: str,
        start: date,
        end: date,
        quote_currency: str = "",
        *,
        wait: bool = True,
        settings: Mapping[str, str] | None = None,
    ) -> Sequence[ProviderQuote]:
        del quote_currency, wait, settings
        return tuple(quote for quote in self._history.get(symbol, ()) if start <= quote.date <= end)


@dataclass(frozen=True, slots=True)
class PriceAt:
    price: Decimal | None
    as_of: date | None
    source: str | None
    stale: bool
    provider_key: str | None = None
    terms_missing: bool = False

    @property
    def missing(self) -> bool:
        return self.price is None


@dataclass(frozen=True, slots=True)
class FxAt:
    rate: Decimal | None
    as_of: date | None
    source: str | None
    stale: bool

    @property
    def missing(self) -> bool:
        return self.rate is None


@dataclass(frozen=True, slots=True)
class PricePointView:
    id: UUID | None
    date: date
    price: Decimal
    source: str
    editable: bool
    provider_key: str | None = None


@dataclass(frozen=True, slots=True)
class PricePanel:
    points: tuple[PricePointView, ...]
    effective: tuple[PricePointView, ...]
    latest: PriceAt


@dataclass(frozen=True, slots=True)
class _Candidate:
    date: date
    source: str
    created_at: datetime
    id: UUID
    value: Decimal
    provider_key: str | None = None


def _candidate_key(candidate: _Candidate) -> tuple[date, int, datetime, str]:
    return (
        candidate.date,
        -source_rank(candidate.source),
        candidate.created_at,
        str(candidate.id),
    )


def _best(candidates: Sequence[_Candidate]) -> _Candidate | None:
    return max(candidates, key=_candidate_key) if candidates else None


def _price_at(candidate: _Candidate | None, on: date) -> PriceAt:
    if candidate is None:
        return PriceAt(None, None, None, False)
    return PriceAt(
        candidate.value,
        candidate.date,
        candidate.source,
        is_stale(candidate.date, on),
        candidate.provider_key,
    )


def _fx_at(candidate: _Candidate | None, on: date) -> FxAt:
    if candidate is None:
        return FxAt(None, None, None, False)
    return FxAt(
        candidate.value,
        candidate.date,
        candidate.source,
        is_stale(candidate.date, on),
    )


def _latest_transaction_price(
    transactions: Sequence[InvestmentTransaction],
) -> dict[tuple[UUID, date], InvestmentTransaction]:
    latest: dict[tuple[UUID, date], InvestmentTransaction] = {}
    for row in transactions:
        if row.date is None:
            continue
        key = (row.instrument_id, row.date)
        previous = latest.get(key)
        if previous is None or (row.created_at, str(row.id)) > (
            previous.created_at,
            str(previous.id),
        ):
            latest[key] = row
    return latest


def _latest_transaction_fx(
    transactions: Sequence[InvestmentTransaction], base_currency: str
) -> dict[tuple[str, date], InvestmentTransaction]:
    latest: dict[tuple[str, date], InvestmentTransaction] = {}
    for row in transactions:
        if row.date is None or row.fx_to_base is None or row.quote_currency == base_currency:
            continue
        key = (row.quote_currency, row.date)
        previous = latest.get(key)
        if previous is None or (row.created_at, str(row.id)) > (
            previous.created_at,
            str(previous.id),
        ):
            latest[key] = row
    return latest


def _latest_prices_in_session(
    session: Session,
    ctx: Context,
    instrument_ids: Collection[UUID],
    on: date,
) -> dict[UUID, PriceAt]:
    ids = tuple(dict.fromkeys(instrument_ids))
    if not ids:
        return {}
    instruments = {
        row.id: row for row in InstrumentRepository(session, ctx).ordered() if row.id in ids
    }
    stored = PricePointRepository(session, ctx).for_instruments_on_or_before(ids, on)
    transactions = InvestmentTransactionRepository(session, ctx).live_dated(on, ids)
    transaction_prices = _latest_transaction_price(transactions)
    bond_rows = {
        row.instrument_id: row for row in BondTermsRepository(session, ctx).for_instruments(ids)
    }
    today = ctx.clock.today(ctx.workspace.timezone)
    current = on == today
    if current and bond_rows:
        transactions = InvestmentTransactionRepository(session, ctx).live_for_instruments(bond_rows)

    candidates: dict[UUID, list[_Candidate]] = {instrument_id: [] for instrument_id in ids}
    for row in stored:
        candidates[row.instrument_id].append(
            _Candidate(
                row.date,
                row.source,
                row.created_at,
                row.id,
                row.price,
                row.provider_key,
            )
        )
    for (instrument_id, day), row in transaction_prices.items():
        candidates[instrument_id].append(
            _Candidate(
                day,
                "transaction",
                row.created_at,
                row.id,
                row.unit_price,
            )
        )
    result: dict[UUID, PriceAt] = {}
    for instrument_id in ids:
        instrument = instruments.get(instrument_id)
        if instrument is None:
            continue
        if instrument.type == "currency":
            result[instrument_id] = PriceAt(Decimal(1), on, None, False)
        elif instrument.type == "bond_model":
            bond_row = bond_rows.get(instrument_id)
            if bond_row is None:
                transaction_only = [
                    candidate
                    for candidate in candidates[instrument_id]
                    if candidate.source == "transaction"
                ]
                result[instrument_id] = replace(
                    _price_at(_best(transaction_only), on), terms_missing=True
                )
            else:
                terms = terms_from_row(bond_row, instrument)
                lots = remaining_lots(
                    [row for row in transactions if row.instrument_id == instrument_id],
                    terms,
                    current=current,
                )
                valuation = bond_value(terms, lots, on)
                unit_price = bond_unit_price(
                    valuation, sum((lot.quantity for lot in lots), Decimal(0))
                )
                result[instrument_id] = PriceAt(
                    unit_price,
                    on if unit_price is not None else None,
                    "bond_model",
                    False,
                )
        else:
            result[instrument_id] = _price_at(_best(candidates[instrument_id]), on)
    return result


def latest_prices_in_session(
    session: Session,
    ctx: Context,
    instrument_ids: Collection[UUID],
    on: date,
) -> dict[UUID, PriceAt]:
    """Batch prices in an existing session; the shared F6.6 current path uses this."""
    return _latest_prices_in_session(session, ctx, instrument_ids, on)


def latest_prices(
    factory: sessionmaker[Session],
    ctx: Context,
    instrument_ids: Collection[UUID],
    on: date,
) -> dict[UUID, PriceAt]:
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        return latest_prices_in_session(session, ctx, instrument_ids, on)


def price_at(
    factory: sessionmaker[Session],
    ctx: Context,
    instrument_id: UUID,
    on: date,
) -> PriceAt:
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        instrument = InstrumentRepository(session, ctx).get_or_raise(instrument_id)
        if instrument.type == "currency":
            return PriceAt(Decimal(1), on, None, False)
        return _latest_prices_in_session(session, ctx, (instrument_id,), on)[instrument_id]


def _latest_fx_in_session(
    session: Session,
    ctx: Context,
    base_currency: str,
    currencies: Collection[str],
    on: date,
) -> dict[str, FxAt]:
    wanted = {currency for currency in currencies if currency != base_currency}
    if not wanted:
        return {}
    stored = FxRateRepository(session, ctx).for_base_on_or_before(base_currency, on)
    transactions = InvestmentTransactionRepository(session, ctx).live_dated(on)
    transaction_rates = _latest_transaction_fx(transactions, base_currency)
    candidates: dict[str, list[_Candidate]] = {currency: [] for currency in wanted}
    for row in stored:
        if row.quote_currency in wanted:
            candidates[row.quote_currency].append(
                _Candidate(row.date, row.source, row.created_at, row.id, row.rate)
            )
    for (currency, day), row in transaction_rates.items():
        if currency in wanted and row.fx_to_base is not None:
            candidates[currency].append(
                _Candidate(day, "transaction", row.created_at, row.id, row.fx_to_base)
            )
    return {currency: _fx_at(_best(candidates[currency]), on) for currency in wanted}


def latest_fx_rates_in_session(
    session: Session,
    ctx: Context,
    currencies: Collection[str],
    on: date,
) -> dict[str, FxAt]:
    """Batch FX rates in an existing session; the shared F6.6 current path uses this."""
    base = CurrentWorkspaceRepository(session, ctx).get().base_currency
    return _latest_fx_in_session(session, ctx, base, currencies, on)


def latest_fx_rates(
    factory: sessionmaker[Session],
    ctx: Context,
    currencies: Collection[str],
    on: date,
) -> dict[str, FxAt]:
    """Base-currency units per one unit of each requested currency.

    The base currency itself is always 1 without a stored observation.
    """
    require_role(ctx, Role.VIEWER)
    requested = {currency.upper() for currency in currencies}
    with factory() as session:
        base = CurrentWorkspaceRepository(session, ctx).get().base_currency
        result = latest_fx_rates_in_session(session, ctx, requested, on)
        if base in requested:
            result[base] = FxAt(Decimal(1), on, None, False)
        return result


def stored_fx_rates_in_session(
    session: Session,
    ctx: Context,
    currencies: Collection[str],
    on: date,
) -> dict[str, Decimal]:
    """Stored FX rates in an existing session (manual or provider, never transaction-derived)."""
    base = CurrentWorkspaceRepository(session, ctx).get().base_currency
    wanted = {currency.upper() for currency in currencies}
    wanted.discard(base)
    if not wanted:
        return {}
    candidates: dict[str, list[_Candidate]] = {}
    for row in FxRateRepository(session, ctx).for_base_on_or_before(base, on):
        if row.quote_currency in wanted and row.source != "transaction":
            candidates.setdefault(row.quote_currency, []).append(
                _Candidate(row.date, row.source, row.created_at, row.id, row.rate)
            )
    result: dict[str, Decimal] = {}
    for currency, rows in candidates.items():
        best = _best(rows)
        if best is not None:
            result[currency] = best.value
    return result


def stored_fx_rates(
    factory: sessionmaker[Session],
    ctx: Context,
    currencies: Collection[str],
    on: date,
) -> dict[str, Decimal]:
    """Stored FX (manual or provider rows, never transaction-derived) per currency at ``on``.

    Base-currency units per one unit: the latest stored observation on or before ``on``, with the
    usual source priority on one date. Used to pre-fill a new transaction's FX field (F6.2 spec:
    "F6.3 will pre-fill it from stored FX"); the user can still change it. A currency without a
    stored rate is absent, and the base currency itself is never listed.
    """
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        return stored_fx_rates_in_session(session, ctx, currencies, on)


def fx_rate(
    factory: sessionmaker[Session],
    ctx: Context,
    from_currency: str,
    to_currency: str,
    on: date,
) -> FxAt:
    """Units of ``to_currency`` per one unit of ``from_currency``."""
    require_role(ctx, Role.VIEWER)
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()
    minor_unit_for(from_currency)
    minor_unit_for(to_currency)
    if from_currency == to_currency:
        return FxAt(Decimal(1), on, None, False)
    with factory() as session:
        base = CurrentWorkspaceRepository(session, ctx).get().base_currency
        rates = _latest_fx_in_session(
            session,
            ctx,
            base,
            {from_currency, to_currency} - {base},
            on,
        )
        if from_currency == base:
            left = FxAt(Decimal(1), on, None, False)
        else:
            left = rates.get(from_currency, FxAt(None, None, None, False))
        if to_currency == base:
            right = FxAt(Decimal(1), on, None, False)
        else:
            right = rates.get(to_currency, FxAt(None, None, None, False))
        if left.missing or right.missing:
            return FxAt(None, None, None, False)
        assert left.rate is not None and right.rate is not None
        with localcontext() as context:
            context.prec = max(context.prec, _DECIMAL_PRECISION)
            rate = left.rate / right.rate
        used = [item for item in (left, right) if item.as_of is not None]
        as_of = min(item.as_of for item in used if item.as_of is not None)
        stale = any(item.stale for item in used)
        if from_currency == base:
            source = right.source
        elif to_currency == base or left.source == right.source:
            source = left.source
        else:
            source = None
        return FxAt(rate, as_of, source, stale)


def _decimal(value: str, locale: str, field: str) -> Decimal:
    try:
        amount = parse_amount(value, locale)
    except ValueError:
        raise PriceError(field, f"invalid_{field}") from None
    if not amount.is_finite() or amount < 0:
        raise PriceError(field, f"invalid_{field}")
    exponent = amount.as_tuple().exponent
    assert isinstance(exponent, int)
    scale = max(-exponent, 0)
    integer_digits = 0 if amount.is_zero() else max(amount.adjusted() + 1, 0)
    if scale > 18 or integer_digits > 20 or integer_digits + scale > 38:
        raise PriceError(field, f"invalid_{field}")
    return amount


class PriceError(Exception):
    """A manual price cannot be saved; ``field`` and ``code`` are form-friendly."""

    def __init__(self, field: str, code: str) -> None:
        super().__init__(code)
        self.field = field
        self.code = code


def _audit(
    session: Session,
    ctx: Context,
    action: str,
    instrument: Instrument,
    point: PricePoint,
) -> None:
    AuditEventRepository(session, ctx).record(
        actor_user_id=ctx.user.id,
        action=action,
        entity_type="price_point",
        entity_id=str(point.id),
        ts=ctx.clock.now(),
        data={
            "instrument": instrument.name,
            "date": point.date.isoformat(),
            "price": format(point.price, "f"),
            "currency": instrument.quote_currency,
        },
    )


def save_manual_price(
    factory: sessionmaker[Session],
    ctx: Context,
    instrument_id: UUID,
    *,
    price_id: UUID | None,
    date_value: str,
    price_value: str,
    locale: str,
) -> PricePointView:
    require_role(ctx, Role.EDITOR)
    try:
        day = date.fromisoformat(date_value)
    except ValueError:
        raise PriceError("date", "invalid_date") from None
    if not 1900 <= day.year <= 2199:
        raise PriceError("date", "invalid_date")
    price = _decimal(price_value, locale, "price")
    today = ctx.clock.today(ctx.workspace.timezone)
    if day > today:
        raise PriceError("date", "future_date")

    def write(session: Session) -> PricePointView:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=False)
        instrument = InstrumentRepository(session, ctx).get_or_raise(instrument_id)
        if instrument.archived_at is not None:
            raise PriceError("instrument", "instrument_archived")
        if instrument.type == "bond_model":
            raise PriceError("instrument", "bond_model_priced")
        repository = PricePointRepository(session, ctx)
        existing = repository.manual_on(instrument_id, day)
        if price_id is not None:
            row = repository.get_or_raise(price_id)
            if row.instrument_id != instrument_id or row.source != "manual":
                raise PriceError("price", "invalid_price")
            if existing is not None and existing.id != row.id:
                raise PriceError("date", "duplicate_date")
            row.date = day
            row.price = price
            session.flush()
            _audit(session, ctx, "price_point.manual_updated", instrument, row)
            return _view(row)
        if existing is not None:
            existing.price = price
            session.flush()
            _audit(session, ctx, "price_point.manual_updated", instrument, existing)
            return _view(existing)
        row = repository.create(
            instrument_id=instrument_id,
            date=day,
            price=price,
            source="manual",
        )
        _audit(session, ctx, "price_point.manual_created", instrument, row)
        return _view(row)

    return run_immediate(factory, write)


def delete_manual_price(
    factory: sessionmaker[Session],
    ctx: Context,
    instrument_id: UUID,
    price_id: UUID,
) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=False)
        repository = PricePointRepository(session, ctx)
        row = repository.get_or_raise(price_id)
        if row.instrument_id != instrument_id:
            raise PriceError("price", "invalid_price")
        if row.source != "manual":
            raise PriceError("price", "not_manual")
        instrument = InstrumentRepository(session, ctx).get_or_raise(row.instrument_id)
        if instrument.archived_at is not None:
            raise PriceError("instrument", "instrument_archived")
        _audit(session, ctx, "price_point.manual_deleted", instrument, row)
        repository.delete(row)

    run_immediate(factory, write)


def _view(row: PricePoint) -> PricePointView:
    return PricePointView(
        id=row.id,
        date=row.date,
        price=row.price,
        source=row.source,
        editable=row.source == "manual",
        provider_key=row.provider_key,
    )


def instrument_prices(
    factory: sessionmaker[Session],
    ctx: Context,
    instrument_id: UUID,
    *,
    on: date,
) -> PricePanel:
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        instrument = InstrumentRepository(session, ctx).get_or_raise(instrument_id)
        if instrument.type == "currency":
            return PricePanel(
                points=(),
                effective=(),
                latest=PriceAt(Decimal(1), on, None, False),
            )
        bond_row = (
            BondTermsRepository(session, ctx).for_instrument(instrument_id)
            if instrument.type == "bond_model"
            else None
        )
        if bond_row is not None:
            today = ctx.clock.today(ctx.workspace.timezone)
            current = on == today
            transaction_repository = InvestmentTransactionRepository(session, ctx)
            transactions = (
                transaction_repository.live_for_instruments((instrument_id,))
                if current
                else transaction_repository.live_dated(on, (instrument_id,))
            )
            terms = terms_from_row(bond_row, instrument)
            lots = remaining_lots(transactions, terms, current=current)
            valuation = bond_value(terms, lots, on)
            unit_price = bond_unit_price(valuation, sum((lot.quantity for lot in lots), Decimal(0)))
            return PricePanel(
                points=(),
                effective=(),
                latest=PriceAt(
                    unit_price,
                    on if unit_price is not None else None,
                    "bond_model",
                    False,
                ),
            )
        stored = (
            []
            if instrument.type == "bond_model"
            else PricePointRepository(session, ctx).for_instruments_on_or_before(
                (instrument_id,), on
            )
        )
        transactions = InvestmentTransactionRepository(session, ctx).live_dated(
            on, (instrument_id,)
        )
        derived = _latest_transaction_price(transactions)
        candidates = [
            _Candidate(row.date, row.source, row.created_at, row.id, row.price) for row in stored
        ]
        candidates += [
            _Candidate(
                day,
                "transaction",
                row.created_at,
                row.id,
                row.unit_price,
            )
            for (_instrument_id, day), row in derived.items()
        ]
        price = _price_at(_best(candidates), on)
        if instrument.type == "bond_model":
            price = replace(price, terms_missing=True)
        points = [_view(row) for row in stored]
        points += [
            PricePointView(
                id=None,
                date=day,
                price=row.unit_price,
                source="transaction",
                editable=False,
            )
            for (_instrument_id, day), row in derived.items()
        ]
        points.sort(key=lambda point: (point.date, -source_rank(point.source), str(point.id or "")))
        effective_by_date: dict[date, PricePointView] = {}
        for point in points:
            previous = effective_by_date.get(point.date)
            if previous is None or _view_key(point) > _view_key(previous):
                effective_by_date[point.date] = point
        effective = tuple(sorted(effective_by_date.values(), key=lambda point: point.date))
        return PricePanel(
            points=tuple(reversed(points)),
            effective=effective,
            latest=price,
        )


def _view_key(point: PricePointView) -> tuple[int, str]:
    return (-source_rank(point.source), str(point.id or ""))
