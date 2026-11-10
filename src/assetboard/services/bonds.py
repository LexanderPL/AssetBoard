"""Bond model terms and valuation use cases (F6.5).

Terms are editor-owned, validated with the pure domain rules and stored once per bond-model
instrument. Valuation always uses FIFO lots regardless of the workspace cost-basis setting:
that setting is about cost, not interest entitlement. Undated legacy lots are included only in
a current (undated) valuation and are treated as accruing from the schedule's first date.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.engine import run_immediate
from assetboard.db.models.investments import BondTerms as BondTermsRow
from assetboard.db.models.investments import Instrument, InvestmentTransaction
from assetboard.db.repositories import (
    AuditEventRepository,
    BondTermsRepository,
    CurrentWorkspaceRepository,
    InstrumentRepository,
    InvestmentTransactionRepository,
)
from assetboard.domain.bonds import (
    MAX_ANNUAL_RATE_BP,
    BondLot,
    BondRatePeriod,
    BondTerms,
    BondValuation,
    InterestMode,
    InvalidBondTerms,
    bond_value,
    clean_series_code,
)
from assetboard.domain.money import Money
from assetboard.domain.percent_or_fixed import PercentOrFixed
from assetboard.domain.transactions import MAX_AMOUNT_MINOR, EntryError, amount_minor
from assetboard.i18n.numbers import parse_amount
from assetboard.services.context import Context, Role, require_role, require_role_now

_DECIMAL_PRECISION = 80
_UNIT_PRICE_QUANTUM = Decimal("1e-18")


class _RateScheduleEntry(BaseModel):
    """The JSON shape of one schedule entry; the domain checks anniversaries and order."""

    model_config = ConfigDict(extra="forbid")

    from_date: date
    annual_rate_bp: int = Field(ge=0, le=MAX_ANNUAL_RATE_BP)


class BondTermsError(Exception):
    """Bond terms cannot be saved; ``field`` and ``code`` are form-friendly."""

    def __init__(self, field: str, code: str) -> None:
        super().__init__(code)
        self.field = field
        self.code = code


@dataclass(frozen=True, slots=True)
class BondTermsInput:
    """Raw form values; the service parses them with the interface locale."""

    series_code: str = ""
    face_value: str = ""
    maturity_date: str = ""
    interest_mode: str = ""
    rate_periods: tuple[tuple[str, str], ...] = ()
    tax_rate: str = ""
    fee_mode: str = ""
    fee_value: str = ""


@dataclass(frozen=True, slots=True)
class BondTermsView:
    instrument_id: UUID
    series_code: str | None
    face_value: Money
    maturity_date: date
    interest_mode: InterestMode
    rate_schedule: tuple[BondRatePeriod, ...]
    tax_rate_bp: int
    early_redemption_fee: PercentOrFixed | None


@dataclass(frozen=True, slots=True)
class BondPanel:
    terms: BondTermsView
    valuation: BondValuation
    quantity: Decimal
    as_of: date
    unit_price: Decimal | None


def bond_terms(
    factory: sessionmaker[Session], ctx: Context, instrument_id: UUID
) -> BondTermsView | None:
    """The terms of one bond-model instrument, or ``None`` when it has none."""
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        instrument = InstrumentRepository(session, ctx).get_or_raise(instrument_id)
        if instrument.type != "bond_model":
            return None
        row = BondTermsRepository(session, ctx).for_instrument(instrument_id)
        return _view(row, instrument) if row is not None else None


def bond_panel(
    factory: sessionmaker[Session],
    ctx: Context,
    instrument_id: UUID,
    *,
    on: date | None = None,
) -> BondPanel | None:
    """The current (``on=None``) or historical model valuation of a bond-model instrument."""
    require_role(ctx, Role.VIEWER)
    today = ctx.clock.today(ctx.workspace.timezone)
    with factory() as session:
        instrument = InstrumentRepository(session, ctx).get_or_raise(instrument_id)
        if instrument.type != "bond_model":
            return None
        row = BondTermsRepository(session, ctx).for_instrument(instrument_id)
        if row is None:
            return None
        terms = _terms(row, instrument)
        valuation_date = today if on is None else on
        current = on is None or on == today
        lots = _lots(session, ctx, instrument, terms, valuation_date, current)
        valuation = bond_value(terms, lots, valuation_date)
        quantity = sum((lot.quantity for lot in lots), Decimal(0))
        unit_price = bond_unit_price(valuation, quantity)
        return BondPanel(
            terms=_view(row, instrument),
            valuation=valuation,
            quantity=quantity,
            as_of=valuation_date,
            unit_price=unit_price,
        )


def save_bond_terms(
    factory: sessionmaker[Session],
    ctx: Context,
    instrument_id: UUID,
    *,
    values: BondTermsInput,
    locale: str,
) -> BondTermsView:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> BondTermsView:
        return _save_bond_terms_in_session(
            session, ctx, instrument_id, values=values, locale=locale
        )

    return run_immediate(factory, write)


def _save_bond_terms_in_session(
    session: Session,
    ctx: Context,
    instrument_id: UUID,
    *,
    values: BondTermsInput,
    locale: str,
) -> BondTermsView:
    """``save_bond_terms`` inside the caller's write transaction.

    Role, lock, instrument checks, parsing, audit and the view are the public write's.
    """
    require_role_now(session, ctx, Role.EDITOR)
    CurrentWorkspaceRepository(session, ctx).lock(exclusive=False)
    instrument = InstrumentRepository(session, ctx).get_or_raise(instrument_id)
    if instrument.type != "bond_model":
        raise BondTermsError("instrument", "not_bond_model")
    if instrument.archived_at is not None:
        raise BondTermsError("instrument", "instrument_archived")
    parsed = _parse(values, locale, instrument.quote_currency)
    repository = BondTermsRepository(session, ctx)
    row = repository.for_instrument(instrument_id, for_update=True)
    data: dict[str, Any] = {
        "instrument_id": instrument_id,
        "series_code": parsed.series_code,
        "face_value_minor": parsed.face_value_minor,
        "maturity_date": parsed.maturity_date,
        "interest_mode": parsed.interest_mode.value,
        "rate_schedule": parsed.rate_schedule,
        "tax_rate_bp": parsed.tax_rate_bp,
        "early_redemption_fee_mode": parsed.fee_mode,
        "early_redemption_fee_rate_bp": parsed.fee_rate_bp,
        "early_redemption_fee_minor": parsed.fee_minor,
    }
    fields = {field: value for field, value in data.items() if field != "instrument_id"}
    changed: list[str]
    if row is None:
        row = repository.create(**data)
        changed = sorted(fields)
    else:
        changed = [field for field, value in fields.items() if getattr(row, field) != value]
        row = repository.update(row.id, **fields)
    AuditEventRepository(session, ctx).record(
        actor_user_id=ctx.user.id,
        action="bond_terms.saved",
        entity_type="instrument",
        entity_id=str(instrument.id),
        ts=ctx.clock.now(),
        data={
            "instrument_id": str(instrument.id),
            "fields": ",".join(sorted(changed)),
        },
    )
    return _view(row, instrument)


@dataclass(slots=True)
class _OpenLot:
    date: date
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class _Parsed:
    series_code: str | None
    face_value_minor: int
    maturity_date: date
    interest_mode: InterestMode
    rate_schedule: list[dict[str, object]]
    tax_rate_bp: int
    fee_mode: str | None
    fee_rate_bp: int | None
    fee_minor: int | None


def _parse(values: BondTermsInput, locale: str, currency: str) -> _Parsed:
    try:
        series = clean_series_code(values.series_code)
    except InvalidBondTerms:
        raise BondTermsError("series_code", "invalid_series") from None
    face_minor = _positive_amount(values.face_value, locale, "face_value", currency)
    maturity = _date(values.maturity_date, "maturity_date")
    try:
        mode = InterestMode(values.interest_mode)
    except ValueError:
        raise BondTermsError("interest_mode", "invalid_interest_mode") from None
    schedule = _schedule(values.rate_periods, locale)
    tax_bp = _percent_bp(values.tax_rate, locale, "tax_rate", maximum=10_000, blank_zero=True)
    fee_mode, fee_rate_bp, fee_minor = _fee(values.fee_mode, values.fee_value, locale, currency)
    try:
        bond_terms_value = BondTerms(
            face_value=Money(face_minor, currency),
            maturity_date=maturity,
            interest_mode=mode,
            rate_schedule=tuple(
                BondRatePeriod(
                    date.fromisoformat(str(item["from_date"])), int(str(item["annual_rate_bp"]))
                )
                for item in schedule
            ),
            tax_rate_bp=tax_bp,
            early_redemption_fee=(
                PercentOrFixed.percent(fee_rate_bp)
                if fee_mode == "percent" and fee_rate_bp is not None
                else PercentOrFixed.fixed_amount(Money(fee_minor, currency))
                if fee_mode == "fixed" and fee_minor is not None
                else None
            ),
        )
    except InvalidBondTerms as error:
        raise BondTermsError(_field_for(error.code), error.code) from None
    return _Parsed(
        series_code=series,
        face_value_minor=face_minor,
        maturity_date=maturity,
        interest_mode=bond_terms_value.interest_mode,
        rate_schedule=schedule,
        tax_rate_bp=tax_bp,
        fee_mode=fee_mode,
        fee_rate_bp=fee_rate_bp,
        fee_minor=fee_minor,
    )


def _field_for(code: str) -> str:
    if code in {"invalid_face_value"}:
        return "face_value"
    if code in {"invalid_maturity"}:
        return "maturity_date"
    if code in {"invalid_interest_mode"}:
        return "interest_mode"
    if code in {"invalid_tax"}:
        return "tax_rate"
    if code in {"invalid_fee"}:
        return "fee_value"
    if code in {"invalid_series"}:
        return "series_code"
    return "schedule"


def _date(value: str, field: str) -> date:
    try:
        day = date.fromisoformat(value.strip())
    except ValueError:
        raise BondTermsError(
            field, "invalid_maturity" if field == "maturity_date" else "invalid_schedule"
        ) from None
    if not 1900 <= day.year <= 2199:
        raise BondTermsError(
            field, "invalid_maturity" if field == "maturity_date" else "invalid_schedule"
        )
    return day


def _positive_amount(value: str, locale: str, field: str, currency: str) -> int:
    try:
        amount = parse_amount(value, locale)
        if not amount.is_finite() or amount <= 0:
            raise ValueError
        minor = amount_minor(amount, currency)
    except (ValueError, EntryError):
        raise BondTermsError(field, "invalid_face_value") from None
    if minor > MAX_AMOUNT_MINOR:
        raise BondTermsError(field, "invalid_face_value")
    return minor


def _percent_bp(
    value: str,
    locale: str,
    field: str,
    *,
    maximum: int,
    blank_zero: bool = False,
) -> int:
    text = value.strip()
    if not text:
        if blank_zero:
            return 0
        raise BondTermsError(field, "invalid_rate")
    try:
        percent = parse_amount(text, locale)
        if not percent.is_finite() or percent < 0:
            raise ValueError
        basis_points = percent.scaleb(2)
        if basis_points != basis_points.to_integral_value():
            raise ValueError
        result = int(basis_points)
        if result > maximum:
            raise ValueError
    except ValueError:
        raise BondTermsError(field, "invalid_rate") from None
    return result


def _schedule(periods: Sequence[tuple[str, str]], locale: str) -> list[dict[str, object]]:
    if not periods:
        raise BondTermsError("schedule", "empty_schedule")
    rows: list[dict[str, object]] = []
    for raw_date, raw_rate in periods:
        day = _date(raw_date, "schedule")
        rate = _percent_bp(raw_rate, locale, "schedule", maximum=MAX_ANNUAL_RATE_BP)
        try:
            entry = _RateScheduleEntry(from_date=day, annual_rate_bp=rate)
        except ValidationError:
            raise BondTermsError("schedule", "invalid_rate") from None
        rows.append(
            {
                "from_date": entry.from_date.isoformat(),
                "annual_rate_bp": entry.annual_rate_bp,
            }
        )
    return rows


def _fee(
    mode_value: str, value: str, locale: str, currency: str
) -> tuple[str | None, int | None, int | None]:
    mode = mode_value.strip()
    if not mode or mode == "none":
        return None, None, None
    if mode == "percent":
        rate = _percent_bp(value, locale, "fee_value", maximum=10_000)
        return "percent", rate, None
    if mode == "fixed":
        text = value.strip()
        if not text:
            raise BondTermsError("fee_value", "invalid_fee")
        try:
            amount = parse_amount(text, locale)
            if not amount.is_finite() or amount < 0:
                raise ValueError
            minor = 0 if amount == 0 else amount_minor(amount, currency)
        except (ValueError, EntryError):
            raise BondTermsError("fee_value", "invalid_fee") from None
        if minor > MAX_AMOUNT_MINOR:
            raise BondTermsError("fee_value", "invalid_fee")
        return "fixed", None, minor
    raise BondTermsError("fee_mode", "invalid_fee")


def terms_from_row(row: BondTermsRow, instrument: Instrument) -> BondTerms:
    """Build the domain terms from a stored row and the instrument's quote currency."""
    return _terms(row, instrument)


def remaining_lots(
    rows: Sequence[InvestmentTransaction], terms: BondTerms, *, current: bool
) -> list[BondLot]:
    """Consume sells FIFO and return the remaining dated lots for valuation.

    ``current`` includes undated legacy lots and treats them as accruing from the schedule's
    first date; a dated valuation excludes them, matching F6.2 holdings.
    """
    first = terms.rate_schedule[0].from_date
    open_lots: list[_OpenLot] = []
    for row in rows:
        if row.type == "buy":
            day = row.date
            if day is None:
                if not current:
                    continue
                day = first
            open_lots.append(_OpenLot(day, row.quantity))
        elif row.type == "sell":
            remaining = row.quantity
            while remaining > 0 and open_lots:
                lot = open_lots[0]
                take = min(lot.quantity, remaining)
                lot.quantity -= take
                remaining -= take
                if lot.quantity == 0:
                    open_lots.pop(0)
    return [BondLot(lot.date, lot.quantity) for lot in open_lots if lot.quantity > 0]


def bond_unit_price(valuation: BondValuation, quantity: Decimal) -> Decimal | None:
    """The exact numeric unit price at DecimalType scale, not a rounded Money per unit."""
    if quantity <= 0:
        return None
    with localcontext() as context:
        context.prec = max(context.prec, _DECIMAL_PRECISION)
        return (valuation.net.to_decimal() / quantity).quantize(
            _UNIT_PRICE_QUANTUM, rounding=ROUND_HALF_UP
        )


def _terms(row: BondTermsRow, instrument: Instrument) -> BondTerms:
    schedule = tuple(
        BondRatePeriod(date.fromisoformat(str(item["from_date"])), int(str(item["annual_rate_bp"])))
        for item in row.rate_schedule
    )
    if row.early_redemption_fee_mode == "percent":
        assert row.early_redemption_fee_rate_bp is not None
        fee: PercentOrFixed | None = PercentOrFixed.percent(row.early_redemption_fee_rate_bp)
    elif row.early_redemption_fee_mode == "fixed":
        assert row.early_redemption_fee_minor is not None
        fee = PercentOrFixed.fixed_amount(
            Money(row.early_redemption_fee_minor, instrument.quote_currency)
        )
    else:
        fee = None
    return BondTerms(
        face_value=Money(row.face_value_minor, instrument.quote_currency),
        maturity_date=row.maturity_date,
        interest_mode=InterestMode(row.interest_mode),
        rate_schedule=schedule,
        tax_rate_bp=row.tax_rate_bp,
        early_redemption_fee=fee,
    )


def _view(row: BondTermsRow, instrument: Instrument) -> BondTermsView:
    terms = _terms(row, instrument)
    return BondTermsView(
        instrument_id=row.instrument_id,
        series_code=row.series_code,
        face_value=terms.face_value,
        maturity_date=terms.maturity_date,
        interest_mode=terms.interest_mode,
        rate_schedule=terms.rate_schedule,
        tax_rate_bp=terms.tax_rate_bp,
        early_redemption_fee=terms.early_redemption_fee,
    )


def _lots(
    session: Session,
    ctx: Context,
    instrument: Instrument,
    terms: BondTerms,
    on: date,
    current: bool,
) -> list[BondLot]:
    repository = InvestmentTransactionRepository(session, ctx)
    rows = (
        repository.for_instrument(instrument.id)
        if current
        else repository.live_dated(on, (instrument.id,))
    )
    return remaining_lots(rows, terms, current=current)
