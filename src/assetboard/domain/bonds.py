"""Bond model valuation (F6.5). Pure: no I/O, no database and no clock.

A bond-model instrument is valued from its user-entered terms and the dated buy lots that are
still held. Interest accrues linearly within each schedule period; capitalized bonds compound
at period ends, paid-out bonds keep only the current period's accrual, and at-maturity-simple
bonds add non-compounded interest. Each output line is rounded half-up once, from exact Decimal
values, to the instrument currency's minor unit.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from enum import StrEnum

from assetboard.domain.money import Money
from assetboard.domain.percent_or_fixed import PercentOrFixed
from assetboard.domain.text import collapse_whitespace, has_rejected_characters

_DECIMAL_PRECISION = 80
MAX_ANNUAL_RATE_BP = 100_000
MAX_TAX_RATE_BP = 10_000
MAX_FEE_RATE_BP = 10_000
MAX_SERIES_CODE_LENGTH = 64
MIN_YEAR = 1900
MAX_YEAR = 2199


class InterestMode(StrEnum):
    CAPITALIZED_ANNUALLY = "capitalized_annually"
    PAID_OUT_ANNUALLY = "paid_out_annually"
    AT_MATURITY_SIMPLE = "at_maturity_simple"


class InvalidBondTerms(ValueError):
    """Bond terms are unusable; ``code`` is a stable token for the form."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class InvalidBondLot(ValueError):
    """A lot cannot be used in valuation; ``code`` is a stable token for the caller."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class BondRatePeriod:
    """One schedule entry: the annual rate that applies from ``from_date``."""

    from_date: date
    annual_rate_bp: int

    def __post_init__(self) -> None:
        if type(self.from_date) is not date or not MIN_YEAR <= self.from_date.year <= MAX_YEAR:
            raise InvalidBondTerms("invalid_schedule")
        if (
            type(self.annual_rate_bp) is not int
            or not 0 <= self.annual_rate_bp <= MAX_ANNUAL_RATE_BP
        ):
            raise InvalidBondTerms("invalid_rate")


@dataclass(frozen=True, slots=True)
class BondTerms:
    """The domain shape of a bond-model instrument's terms."""

    face_value: Money
    maturity_date: date
    interest_mode: InterestMode
    rate_schedule: tuple[BondRatePeriod, ...]
    tax_rate_bp: int = 0
    early_redemption_fee: PercentOrFixed | None = None

    def __post_init__(self) -> None:
        if self.face_value.amount_minor <= 0:
            raise InvalidBondTerms("invalid_face_value")
        if (
            type(self.maturity_date) is not date
            or not MIN_YEAR <= self.maturity_date.year <= MAX_YEAR
        ):
            raise InvalidBondTerms("invalid_maturity")
        schedule = tuple(self.rate_schedule)
        object.__setattr__(self, "rate_schedule", schedule)
        if type(self.tax_rate_bp) is not int or not 0 <= self.tax_rate_bp <= MAX_TAX_RATE_BP:
            raise InvalidBondTerms("invalid_tax")
        validate_rate_schedule(schedule, self.maturity_date)
        fee = self.early_redemption_fee
        if fee is not None:
            if fee.mode == "percent" and (fee.rate_bp is None or fee.rate_bp > MAX_FEE_RATE_BP):
                raise InvalidBondTerms("invalid_fee")
            if (
                fee.mode == "fixed"
                and fee.fixed is not None
                and (fee.fixed.currency != self.face_value.currency)
            ):
                raise InvalidBondTerms("invalid_fee")


@dataclass(frozen=True, slots=True)
class BondLot:
    """A dated quantity still held; the service consumes sells FIFO before calling."""

    date: date
    quantity: Decimal

    def __post_init__(self) -> None:
        if type(self.date) is not date or not MIN_YEAR <= self.date.year <= MAX_YEAR:
            raise InvalidBondLot("invalid_lot_date")
        if (
            type(self.quantity) is not Decimal
            or not self.quantity.is_finite()
            or self.quantity <= 0
        ):
            raise InvalidBondLot("invalid_lot_quantity")


@dataclass(frozen=True, slots=True)
class BondValuation:
    """Per-currency totals across all lots at one valuation date."""

    gross_accrued: Money
    tax: Money
    fee: Money
    net: Money
    projected_net_at_maturity: Money


@dataclass(frozen=True, slots=True)
class _Period:
    start: date
    end: date
    full_end: date
    annual_rate_bp: int


def anniversary(start: date, years: int) -> date:
    """The same calendar day ``years`` later; 29 February falls on 28 February if needed."""
    try:
        return start.replace(year=start.year + years)
    except ValueError:
        return date(start.year + years, 2, 28)


def validate_rate_schedule(schedule: Sequence[BondRatePeriod], maturity_date: date) -> None:
    """Validate the ordered anniversary schedule shared by the form and the domain."""
    if not schedule:
        raise InvalidBondTerms("empty_schedule")
    first = schedule[0].from_date
    if maturity_date <= first:
        raise InvalidBondTerms("invalid_maturity")
    for index, period in enumerate(schedule):
        if period.from_date >= maturity_date:
            raise InvalidBondTerms("invalid_schedule")
        if index == 0:
            continue
        if period.from_date <= schedule[index - 1].from_date:
            raise InvalidBondTerms("invalid_schedule")
        if period.from_date != anniversary(first, index):
            raise InvalidBondTerms("invalid_schedule")


def clean_series_code(value: str) -> str | None:
    """Trim a series code; blank means none."""
    if has_rejected_characters(value.strip()):
        raise InvalidBondTerms("invalid_series")
    cleaned = collapse_whitespace(value)
    if not cleaned:
        return None
    if len(cleaned) > MAX_SERIES_CODE_LENGTH:
        raise InvalidBondTerms("invalid_series")
    return cleaned


def bond_value(terms: BondTerms, lots: Sequence[BondLot], as_of: date) -> BondValuation:
    """Value the given held lots at ``as_of``, rounded once per output line.

    The lots must be the remaining lots at the valuation date; the service applies FIFO to the
    buy/sell history. At or after maturity interest is frozen, the early-redemption fee is
    zero, and ``net`` equals ``projected_net_at_maturity``.
    """
    if type(as_of) is not date or not MIN_YEAR <= as_of.year <= MAX_YEAR:
        raise InvalidBondLot("invalid_valuation_date")
    periods = _periods(terms)
    with localcontext() as context:
        context.prec = max(context.prec, _DECIMAL_PRECISION)
        face_per_unit = terms.face_value.to_decimal()
        face_total = Decimal(0)
        quantity_total = Decimal(0)
        interest = Decimal(0)
        projected_interest = Decimal(0)
        for lot in lots:
            face_total += face_per_unit * lot.quantity
            quantity_total += lot.quantity
            interest += _lot_interest(terms, periods, lot, min(as_of, terms.maturity_date))
            projected_interest += _lot_interest(terms, periods, lot, terms.maturity_date)

        projected_tax = _tax(terms, projected_interest)
        projected_net = face_total + projected_interest - projected_tax
        if as_of >= terms.maturity_date:
            return BondValuation(
                gross_accrued=_money(face_total + projected_interest, terms),
                tax=_money(projected_tax, terms),
                fee=_money(Decimal(0), terms),
                net=_money(projected_net, terms),
                projected_net_at_maturity=_money(projected_net, terms),
            )
        fee = _fee(terms, face_total, quantity_total, interest)
        tax = _tax(terms, max(Decimal(0), interest - fee))
        net = face_total + interest - fee - tax
        return BondValuation(
            gross_accrued=_money(face_total + interest, terms),
            tax=_money(tax, terms),
            fee=_money(fee, terms),
            net=_money(net, terms),
            projected_net_at_maturity=_money(projected_net, terms),
        )


def _periods(terms: BondTerms) -> list[_Period]:
    first = terms.rate_schedule[0].from_date
    last_rate = terms.rate_schedule[-1].annual_rate_bp
    by_start = {period.from_date: period.annual_rate_bp for period in terms.rate_schedule}
    periods: list[_Period] = []
    index = 0
    while True:
        start = anniversary(first, index)
        if start >= terms.maturity_date:
            break
        full_end = anniversary(first, index + 1)
        end = min(full_end, terms.maturity_date)
        periods.append(
            _Period(
                start=start,
                end=end,
                full_end=full_end,
                annual_rate_bp=by_start.get(start, last_rate),
            )
        )
        index += 1
    return periods


def _lot_interest(
    terms: BondTerms, periods: Sequence[_Period], lot: BondLot, stop: date
) -> Decimal:
    start = max(lot.date, periods[0].start)
    stop = min(stop, terms.maturity_date)
    if stop <= start:
        return Decimal(0)
    face = terms.face_value.to_decimal()
    mode = terms.interest_mode
    if mode is InterestMode.PAID_OUT_ANNUALLY:
        # A coupon is paid on the period-end day, so the bond resets to face then; at maturity
        # the same reset applies to the final coupon.
        current = _period_at(periods, stop)
        if current is None:
            return Decimal(0)
        accrual_start = max(start, current.start)
        if stop <= accrual_start:
            return Decimal(0)
        return lot.quantity * face * _rate(current) * _fraction(accrual_start, stop, current)
    if mode is InterestMode.AT_MATURITY_SIMPLE:
        total = Decimal(0)
        for period in periods:
            segment_start = max(start, period.start)
            segment_end = min(stop, period.end)
            if segment_end > segment_start:
                total += face * _rate(period) * _fraction(segment_start, segment_end, period)
        return lot.quantity * total
    principal = face
    for period in periods:
        if period.start >= stop:
            break
        if period.end <= start:
            continue
        segment_start = max(start, period.start)
        segment_end = min(stop, period.end)
        if segment_end <= segment_start:
            continue
        principal *= 1 + _rate(period) * _fraction(segment_start, segment_end, period)
    return lot.quantity * (principal - face)


def _period_at(periods: Sequence[_Period], on: date) -> _Period | None:
    for period in periods:
        if period.start <= on < period.end:
            return period
    return None


def _rate(period: _Period) -> Decimal:
    return Decimal(period.annual_rate_bp) / Decimal(10_000)


def _fraction(start: date, end: date, period: _Period) -> Decimal:
    # Actual/actual: a partial final period is prorated over the full annual period.
    period_days = (period.full_end - period.start).days
    if period_days <= 0:
        return Decimal(0)
    return Decimal((end - start).days) / Decimal(period_days)


def _fee(
    terms: BondTerms,
    face_total: Decimal,
    quantity_total: Decimal,
    interest: Decimal,
) -> Decimal:
    if terms.early_redemption_fee is None or interest <= 0:
        return Decimal(0)
    fee = terms.early_redemption_fee
    if fee.mode == "percent":
        assert fee.rate_bp is not None
        raw = face_total * Decimal(fee.rate_bp) / Decimal(10_000)
    else:
        assert fee.fixed is not None
        raw = fee.fixed.to_decimal() * quantity_total
    return min(max(raw, Decimal(0)), interest)


def _tax(terms: BondTerms, taxable: Decimal) -> Decimal:
    return max(taxable, Decimal(0)) * Decimal(terms.tax_rate_bp) / Decimal(10_000)


def _money(amount: Decimal, terms: BondTerms) -> Money:
    return Money.from_decimal(amount, terms.face_value.currency)
