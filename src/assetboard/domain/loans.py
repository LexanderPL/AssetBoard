"""Loan configuration and schedule rules (F7.1-F7.2). Pure: no I/O, no database, no clock.

F7.1 stores terms and validates them. F7.2 builds the monthly schedule from an anchor
(origination or a balance checkpoint), the rate timeline, costs and planned overpayments, and
summarizes it at an ``as_of`` date. All amounts stay in integer minor units; the only Decimal
work is the annuity payment formula, rounded once per component.
"""

from __future__ import annotations

import calendar
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from enum import StrEnum

from assetboard.domain.money import Money, minor_unit_for
from assetboard.domain.months import MAX_YEAR, MIN_YEAR
from assetboard.domain.percent_or_fixed import PercentOrFixed
from assetboard.domain.transactions import MAX_AMOUNT_MINOR, EntryError, clean_description

MAX_LOAN_NAME_LENGTH = 200
MAX_NOTE_LENGTH = 200
# 50 years: long enough for every household mortgage, short enough to keep a schedule finite.
MAX_TERM_MONTHS = 600
# 100% annual interest: above every plausible household loan and still far below MAX_RATE_BP.
MAX_LOAN_RATE_BP = 10_000
# Above this rate the form asks for an explicit confirmation (25.00% itself is allowed).
HIGH_RATE_CONFIRMATION_BP = 2_500
MAX_COST_RATE_BP = 10_000


class LoanKind(StrEnum):
    ANNUITY = "annuity"
    EQUAL_PRINCIPAL = "equal_principal"
    INSTALLMENT_PLAN = "installment_plan"


class RateMode(StrEnum):
    FIXED = "fixed"
    VARIABLE = "variable"


class DrawdownMode(StrEnum):
    FULL_AT_ORIGINATION = "full_at_origination"
    TRANCHES = "tranches"


class OverpaymentMode(StrEnum):
    NONE = "none"
    FIXED_EXTRA = "fixed_extra"
    TARGET_TOTAL = "target_total"


class OverpaymentStrategy(StrEnum):
    REDUCE_TERM = "reduce_term"
    REDUCE_INSTALLMENT = "reduce_installment"


class CostBasis(StrEnum):
    OPENING_BALANCE = "opening_balance"


class LoanValidationError(ValueError):
    """Invalid loan input; ``field`` and ``code`` name the problem for the form."""

    def __init__(self, field: str, code: str) -> None:
        super().__init__(code)
        self.field = field
        self.code = code


def clean_loan_name(value: str) -> str:
    try:
        cleaned = clean_description(value)
    except EntryError:
        raise LoanValidationError("name", "invalid_name") from None
    if not cleaned or len(cleaned) > MAX_LOAN_NAME_LENGTH:
        raise LoanValidationError("name", "invalid_name")
    return cleaned


def clean_note(value: str) -> str:
    try:
        cleaned = clean_description(value)
    except EntryError:
        raise LoanValidationError("note", "invalid_note") from None
    if len(cleaned) > MAX_NOTE_LENGTH:
        raise LoanValidationError("note", "invalid_note")
    return cleaned


def valid_date(value: date, field: str) -> date:
    if not MIN_YEAR <= value.year <= MAX_YEAR:
        raise LoanValidationError(field, "invalid_date")
    return value


def validate_term_months(value: int) -> int:
    if type(value) is not int or not 1 <= value <= MAX_TERM_MONTHS:
        raise LoanValidationError("term_months", "invalid_term")
    return value


def validate_payment_day(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 31:
        raise LoanValidationError("payment_day", "invalid_payment_day")
    return value


def payment_date_matches_day(first_payment_date: date, payment_day: int) -> bool:
    """Whether the first due date is the nominal day, clamped to the end of its month."""
    last_day = calendar.monthrange(first_payment_date.year, first_payment_date.month)[1]
    return first_payment_date.day == min(payment_day, last_day)


def validate_rate_bp(value: int, field: str = "rate_bp") -> int:
    if type(value) is not int or not 0 <= value <= MAX_LOAN_RATE_BP:
        raise LoanValidationError(field, "invalid_rate")
    return value


def validate_margin_bp(value: int) -> int:
    return validate_rate_bp(value, "margin_bp")


def validate_cost_rate_bp(value: int) -> int:
    if type(value) is not int or not 0 <= value <= MAX_COST_RATE_BP:
        raise LoanValidationError("rate_bp", "invalid_rate")
    return value


def validate_cost_dates(effective_from: date, effective_to: date | None) -> None:
    valid_date(effective_from, "effective_from")
    if effective_to is not None:
        valid_date(effective_to, "effective_to")
        if effective_to < effective_from:
            raise LoanValidationError("effective_to", "invalid_effective_range")


def cost_active_on(effective_from: date, effective_to: date | None, day: date) -> bool:
    """Cost components apply from and including their first day until and including their last."""
    return effective_from <= day and (effective_to is None or day <= effective_to)


def is_high_rate(rate_bp: int) -> bool:
    return rate_bp > HIGH_RATE_CONFIRMATION_BP


def requires_high_rate_confirmation(*rates: int | None) -> bool:
    return any(rate is not None and is_high_rate(rate) for rate in rates)


def latest_value_on_or_before(values: list[tuple[date, int]], day: date) -> int | None:
    """The value of the latest entry whose date is at or before ``day``; None if there is none."""
    chosen: int | None = None
    for effective, value in values:
        if effective <= day:
            chosen = value
    return chosen


def _monthly_rate(annual_rate_bp: int) -> Decimal:
    return Decimal(annual_rate_bp).scaleb(-4) / Decimal(12)


def _annuity_payment_decimal(amount: Decimal, annual_rate_bp: int, term_months: int) -> Decimal:
    """The unrounded annuity payment, ``P * r / (1 - (1 + r)^-n)``."""
    monthly_rate = _monthly_rate(annual_rate_bp)
    if monthly_rate == 0:
        return amount / Decimal(term_months)
    growth = (Decimal(1) + monthly_rate) ** (-term_months)
    return amount * monthly_rate / (Decimal(1) - growth)


def _round_minor(value: Decimal, currency: str, minor_unit: int | None = None) -> int:
    if minor_unit is None:
        minor_unit = minor_unit_for(currency)
    return int(value.scaleb(minor_unit).to_integral_value(rounding=ROUND_HALF_UP))


def first_installment(
    principal: Money,
    annual_rate_bp: int,
    term_months: int,
    kind: LoanKind,
) -> Money:
    """The first monthly installment for an annuity or equal-principal loan.

    Annuity: ``P * r / (1 - (1 + r)^-n)`` with ``r = annual_rate / 12``. Equal principal:
    ``P / n + P * r``. Each result is rounded half up once to the currency's minor unit, matching
    the schedule engine's per-component rounding. This is the only finance calculation in F7.1.
    """
    validate_rate_bp(annual_rate_bp)
    validate_term_months(term_months)
    if principal.amount_minor <= 0:
        raise LoanValidationError("principal", "invalid_principal")
    with localcontext() as context:
        context.prec = max(context.prec, 40)
        amount = principal.to_decimal()
        if kind is LoanKind.EQUAL_PRINCIPAL:
            payment = amount / Decimal(term_months) + amount * _monthly_rate(annual_rate_bp)
        else:
            payment = _annuity_payment_decimal(amount, annual_rate_bp, term_months)
        minor = _round_minor(payment, principal.currency)
    if not 0 < minor <= MAX_AMOUNT_MINOR:
        raise LoanValidationError("principal", "invalid_principal")
    return Money(minor, principal.currency)


def _annuity_principal_minor(
    amount: Decimal,
    monthly_rate: Decimal,
    payment: Decimal,
    currency: str,
    minor_unit: int,
) -> int:
    """The rounded principal component of one annuity payment.

    The unrounded payment minus the unrounded interest, rounded half up once, is the same
    component ``numpy_financial.ppmt`` reports. Using the rounded payment first would drift by a
    minor unit on many rows.
    """
    interest = amount * monthly_rate
    return _round_minor(payment - interest, currency, minor_unit)


class DayCount(StrEnum):
    MONTHLY = "monthly"
    ACTUAL_365 = "actual_365"


class LoanPaymentKind(StrEnum):
    INSTALLMENT = "installment"
    OVERPAYMENT = "overpayment"


class LoanPaymentSource(StrEnum):
    MANUAL = "manual"
    UPCOMING = "upcoming"


def validate_day_count(value: DayCount | str) -> DayCount:
    try:
        return DayCount(value)
    except ValueError:
        raise LoanValidationError("day_count", "invalid_day_count") from None


class ScheduleError(ValueError):
    """A schedule that cannot be built or has no valid answer; ``code`` names the problem."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class RatePeriod:
    effective_from: date
    annual_rate_bp: int


@dataclass(frozen=True, slots=True)
class RateSegment:
    """One part of a loan's rate life: a fixed rate or a reference rate plus margin.

    Each segment starts on ``effective_from`` and lasts until the next segment starts. A variable
    segment carries the reference values that may reprice it; the value in force on the segment's
    first day always applies, even when it was published earlier.
    """

    effective_from: date
    mode: RateMode
    rate_bp: int | None = None
    margin_bp: int | None = None
    reference_values: tuple[tuple[date, int], ...] = ()


def build_rate_timeline(segments: Sequence[RateSegment], start: date) -> list[RatePeriod]:
    """Resolve mixed fixed/reference segments into the flat timeline the schedule engine uses.

    Segments must start on or after the loan start and may not share a date. A variable segment
    contributes its opening value and then every later reference value up to (but not including)
    the next segment's start, so a fixed segment always wins on its own effective date.
    """
    ordered = sorted(segments, key=lambda segment: segment.effective_from)
    previous: date | None = None
    timeline: list[RatePeriod] = []
    for index, segment in enumerate(ordered):
        if segment.effective_from < start:
            raise LoanValidationError("effective_from", "rate_change_before_start")
        if previous == segment.effective_from:
            raise LoanValidationError("effective_from", "rate_change_exists")
        previous = segment.effective_from
        end = ordered[index + 1].effective_from if index + 1 < len(ordered) else None
        if segment.mode is RateMode.FIXED:
            if segment.rate_bp is None:
                raise LoanValidationError("rate_bp", "invalid_rate")
            timeline.append(RatePeriod(segment.effective_from, validate_rate_bp(segment.rate_bp)))
            continue
        if segment.margin_bp is None:
            raise LoanValidationError("margin_bp", "invalid_margin")
        margin = validate_margin_bp(segment.margin_bp)
        values = sorted(segment.reference_values, key=lambda value: value[0])
        opening = latest_value_on_or_before(values, segment.effective_from)
        if opening is None:
            raise LoanValidationError("reference_rate_id", "missing_reference_value")
        timeline.append(RatePeriod(segment.effective_from, validate_rate_bp(opening + margin)))
        for effective_from, value_bp in values:
            if effective_from <= segment.effective_from:
                continue
            if end is not None and effective_from >= end:
                break
            timeline.append(RatePeriod(effective_from, validate_rate_bp(value_bp + margin)))
    return timeline


@dataclass(frozen=True, slots=True)
class CostPeriod:
    value: PercentOrFixed
    effective_from: date
    effective_to: date | None = None


@dataclass(frozen=True, slots=True)
class Drawdown:
    """A transfer of loan money to the target account (a positive increase in the debt)."""

    date: date
    amount: Money


@dataclass(frozen=True, slots=True)
class OneOffOverpayment:
    date: date
    amount: Money


@dataclass(frozen=True, slots=True)
class BalanceCheckpoint:
    as_of: date
    remaining_principal: Money


@dataclass(frozen=True, slots=True)
class ScheduleAnchor:
    date: date
    balance: Money


@dataclass(frozen=True, slots=True)
class LoanTerms:
    kind: LoanKind | str
    currency: str
    principal_original: Money | None
    origination_date: date | None
    first_payment_date: date
    term_months: int
    payment_day: int
    installment_override: Money | None
    overpayment_mode: OverpaymentMode | str
    overpayment_amount: Money | None
    drawdown_mode: DrawdownMode | str = DrawdownMode.FULL_AT_ORIGINATION
    interest_only_months: int = 0


@dataclass(frozen=True, slots=True)
class ScheduleRow:
    n: int
    date: date
    annual_rate_bp: int
    opening: Money
    interest: Money
    principal: Money
    overpayment: Money
    transfer: Money
    costs: Money
    installment_total: Money
    closing: Money


@dataclass(frozen=True, slots=True)
class Schedule:
    rows: tuple[ScheduleRow, ...]
    starting_balance: Money
    drawn_principal: Money
    anchor_date: date
    as_of: date
    original_principal: Money | None
    currency: str
    drawdowns: tuple[Drawdown, ...] = ()


@dataclass(frozen=True, slots=True)
class LoanPaymentSplit:
    principal: Money
    interest: Money
    costs: Money
    overpayment: Money

    @property
    def total(self) -> Money:
        return Money(
            self.principal.amount_minor
            + self.interest.amount_minor
            + self.costs.amount_minor
            + self.overpayment.amount_minor,
            self.principal.currency,
        )


def split_loan_payment(
    total: Money, kind: LoanPaymentKind | str, row: ScheduleRow | None
) -> LoanPaymentSplit:
    """Split an actual payment against its projected installment row.

    An overpayment goes entirely to the overpayment component. For an installment, an equal
    total keeps the row's components, a larger total adds the excess to the overpayment, and a
    smaller total fills interest, costs, principal and overpayment in that order.
    """
    try:
        wanted_kind = LoanPaymentKind(kind)
    except ValueError:
        raise LoanValidationError("kind", "invalid_payment_kind") from None
    if total.amount_minor <= 0 or total.amount_minor > MAX_AMOUNT_MINOR:
        raise LoanValidationError("total", "invalid_payment_total")
    if wanted_kind is LoanPaymentKind.OVERPAYMENT:
        return LoanPaymentSplit(
            principal=Money(0, total.currency),
            interest=Money(0, total.currency),
            costs=Money(0, total.currency),
            overpayment=total,
        )
    if row is None:
        raise LoanValidationError("date", "no_scheduled_payment")
    if row.opening.currency != total.currency:
        raise LoanValidationError("total", "invalid_currency")
    row_total = row.installment_total.amount_minor
    if total.amount_minor == row_total:
        return LoanPaymentSplit(
            principal=row.principal,
            interest=row.interest,
            costs=row.costs,
            overpayment=row.overpayment,
        )
    if total.amount_minor > row_total:
        return LoanPaymentSplit(
            principal=row.principal,
            interest=row.interest,
            costs=row.costs,
            overpayment=Money(
                row.overpayment.amount_minor + total.amount_minor - row_total,
                total.currency,
            ),
        )
    remaining = total.amount_minor
    interest = min(remaining, row.interest.amount_minor)
    remaining -= interest
    costs = min(remaining, row.costs.amount_minor)
    remaining -= costs
    principal = min(remaining, row.principal.amount_minor)
    remaining -= principal
    overpayment = min(remaining, row.overpayment.amount_minor)
    return LoanPaymentSplit(
        principal=Money(principal, total.currency),
        interest=Money(interest, total.currency),
        costs=Money(costs, total.currency),
        overpayment=Money(overpayment, total.currency),
    )


def validate_payment_breakdown(
    total: Money,
    principal: Money,
    interest: Money,
    costs: Money,
    overpayment: Money,
) -> LoanPaymentSplit:
    """Check an explicit breakdown: non-negative, same currency and exactly the total."""
    parts = (principal, interest, costs, overpayment)
    if any(part.currency != total.currency for part in parts):
        raise LoanValidationError("total", "invalid_currency")
    if any(part.amount_minor < 0 or part.amount_minor > MAX_AMOUNT_MINOR for part in parts):
        raise LoanValidationError("principal", "invalid_payment_breakdown")
    if sum(part.amount_minor for part in parts) != total.amount_minor:
        raise LoanValidationError("total", "payment_breakdown_mismatch")
    return LoanPaymentSplit(
        principal=principal,
        interest=interest,
        costs=costs,
        overpayment=overpayment,
    )


@dataclass(frozen=True, slots=True)
class LoanScheduleSummary:
    as_of: date
    original_principal: Money
    drawn_principal: Money
    undrawn_principal: Money
    paid_principal: Money
    remaining_principal: Money
    next_payment_date: date | None
    next_installment: Money | None
    remaining_interest: Money
    remaining_costs: Money
    payoff_date: date | None
    months_remaining: int
    percent_repaid_bp: int
    percent_remaining_bp: int
    percent_undrawn_bp: int
    total_interest: Money
    total_costs: Money
    total_overpayments: Money
    total_paid: Money


def _half_up(value: int, multiplier: int, divisor: int) -> int:
    """``value * multiplier / divisor`` rounded half up, with non-negative inputs."""
    if value < 0 or multiplier < 0 or divisor <= 0:
        raise ValueError("half-up inputs must be non-negative")
    return (value * multiplier * 2 + divisor) // (2 * divisor)


_DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _month_last_day(year: int, month: int) -> int:
    if month == 2 and year % 4 == 0 and (year % 100 != 0 or year % 400 == 0):
        return 29
    return _DAYS_IN_MONTH[month - 1]


def _clamped_date(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, _month_last_day(year, month)))


def _add_months(day: date, months: int, payment_day: int) -> date:
    index = day.year * 12 + (day.month - 1) + months
    return _clamped_date(index // 12, index % 12 + 1, payment_day)


def _next_due_date(anchor: date, first_payment_date: date, payment_day: int) -> date:
    """The first contractual due date strictly after the anchor.

    Before the first payment, the contract's first payment date wins. Once that date has passed
    (a checkpoint mid-term), the next nominal payment day after the anchor is used.
    """
    if first_payment_date > anchor:
        return first_payment_date
    return nominal_payment_after(anchor, payment_day)


def _payment_dates(
    terms: LoanTerms, anchor_date: date, first_draw_date: date | None = None
) -> tuple[date, ...]:
    first = _next_due_date(anchor_date, terms.first_payment_date, terms.payment_day)
    if first_draw_date is not None and first_draw_date >= first:
        first = nominal_payment_after(first_draw_date, terms.payment_day)
    return tuple(
        [first]
        + [_add_months(first, index, terms.payment_day) for index in range(1, terms.term_months)]
    )


_ZERO_MONEY: dict[str, Money] = {}


def nominal_payment_after(day: date, payment_day: int) -> date:
    """The next contractual payment day strictly after ``day`` (month-end clamped)."""
    candidate = _clamped_date(day.year, day.month, payment_day)
    if candidate <= day:
        candidate = _add_months(day, 1, payment_day)
    return candidate


def payment_delayed_after_transfer(
    first_transfer_date: date, first_payment_date: date, payment_day: int
) -> bool:
    """Whether the first payment is later than the next nominal day after the first transfer."""
    return nominal_payment_after(first_transfer_date, payment_day) < first_payment_date


def _money(value: int, currency: str) -> Money:
    if value == 0:
        zero = _ZERO_MONEY.get(currency)
        if zero is None:
            zero = _ZERO_MONEY[currency] = Money.from_minor_unchecked(0, currency)
        return zero
    return Money.from_minor_unchecked(value, currency)


def _require_currency(money: Money | None, currency: str, field: str) -> None:
    if money is not None and money.currency != currency:
        raise LoanValidationError(field, "invalid_currency")


def _validate_terms(terms: LoanTerms) -> LoanKind:
    try:
        minor_unit_for(terms.currency)
    except ValueError:
        raise LoanValidationError("currency", "invalid_currency") from None
    try:
        kind = LoanKind(terms.kind)
    except ValueError:
        raise LoanValidationError("kind", "invalid_kind") from None
    validate_term_months(terms.term_months)
    validate_payment_day(terms.payment_day)
    valid_date(terms.first_payment_date, "first_payment_date")
    if terms.origination_date is not None:
        valid_date(terms.origination_date, "origination_date")
    _require_currency(terms.principal_original, terms.currency, "principal_original")
    if terms.principal_original is not None and terms.principal_original.amount_minor <= 0:
        raise LoanValidationError("principal_original", "invalid_principal")
    _require_currency(terms.installment_override, terms.currency, "installment_override")
    if terms.installment_override is not None and terms.installment_override.amount_minor <= 0:
        raise LoanValidationError("installment_override", "invalid_override")
    if kind is LoanKind.EQUAL_PRINCIPAL and terms.installment_override is not None:
        raise LoanValidationError("installment_override", "override_annuity_only")
    try:
        mode = OverpaymentMode(terms.overpayment_mode)
    except ValueError:
        raise LoanValidationError("overpayment_mode", "invalid_overpayment") from None
    _require_currency(terms.overpayment_amount, terms.currency, "overpayment_amount")
    if mode is OverpaymentMode.NONE:
        if terms.overpayment_amount is not None:
            raise LoanValidationError("overpayment_amount", "invalid_overpayment")
    elif terms.overpayment_amount is None or terms.overpayment_amount.amount_minor <= 0:
        raise LoanValidationError("overpayment_amount", "invalid_overpayment")
    try:
        drawdown_mode = DrawdownMode(terms.drawdown_mode)
    except ValueError:
        raise LoanValidationError("drawdown_mode", "invalid_drawdown_mode") from None
    if kind is LoanKind.INSTALLMENT_PLAN and drawdown_mode is not DrawdownMode.FULL_AT_ORIGINATION:
        raise LoanValidationError("drawdown_mode", "invalid_drawdown_mode")
    if type(terms.interest_only_months) is not int or terms.interest_only_months < 0:
        raise LoanValidationError("interest_only_months", "invalid_interest_only")
    if terms.term_months == 0:
        if terms.interest_only_months != 0:
            raise LoanValidationError("interest_only_months", "invalid_interest_only")
    elif terms.interest_only_months >= terms.term_months:
        raise LoanValidationError("interest_only_months", "invalid_interest_only")
    return kind


def _validate_rate_timeline(rate_timeline: list[RatePeriod]) -> list[RatePeriod]:
    ordered = sorted(rate_timeline, key=lambda row: row.effective_from)
    previous: date | None = None
    for period in ordered:
        valid_date(period.effective_from, "effective_from")
        if previous == period.effective_from:
            raise ScheduleError("duplicate_rate")
        previous = period.effective_from
        validate_rate_bp(period.annual_rate_bp)
    return ordered


def shift_rate_timeline(
    rate_timeline: list[RatePeriod], shift_bp: int, effective_from: date
) -> list[RatePeriod]:
    """Shift the annual rate by ``shift_bp`` from ``effective_from`` onwards.

    Periods before the date keep their rate. When no period starts on the date, a period is
    inserted with the rate then in force, shifted; a date after the last period appends one.
    A date before the first period shifts every period. The result is ordered and validated.
    """
    valid_date(effective_from, "rate_shift_from")
    if type(shift_bp) is not int:
        raise LoanValidationError("rate_shift_bp", "invalid_rate_shift")
    ordered = _validate_rate_timeline(rate_timeline)
    if not ordered:
        raise ScheduleError("missing_rate")
    if not -MAX_LOAN_RATE_BP <= shift_bp <= MAX_LOAN_RATE_BP:
        raise LoanValidationError("rate_shift_bp", "invalid_rate_shift")
    if shift_bp == 0:
        return ordered

    shifted: list[RatePeriod] = []
    current: int | None = None
    for period in ordered:
        if period.effective_from < effective_from:
            current = period.annual_rate_bp
            shifted.append(period)
            continue
        if current is None:
            current = period.annual_rate_bp
        elif period.effective_from > effective_from and shifted[-1].effective_from < effective_from:
            shifted.append(RatePeriod(effective_from, current + shift_bp))
        shifted.append(RatePeriod(period.effective_from, period.annual_rate_bp + shift_bp))
    if ordered[-1].effective_from < effective_from:
        shifted.append(RatePeriod(effective_from, ordered[-1].annual_rate_bp + shift_bp))
    for period in shifted:
        if not 0 <= period.annual_rate_bp <= MAX_LOAN_RATE_BP:
            raise LoanValidationError("rate_shift_bp", "invalid_rate_shift")
    return shifted


def _validate_costs(costs: list[CostPeriod], currency: str) -> None:
    for cost in costs:
        valid_date(cost.effective_from, "effective_from")
        if cost.effective_to is not None:
            valid_date(cost.effective_to, "effective_to")
            if cost.effective_to < cost.effective_from:
                raise LoanValidationError("effective_to", "invalid_effective_range")
        if cost.value.mode == "percent":
            if cost.value.rate_bp is None:
                raise LoanValidationError("costs", "invalid_cost")
            validate_cost_rate_bp(cost.value.rate_bp)
        elif cost.value.mode == "fixed":
            if cost.value.fixed is None:
                raise LoanValidationError("costs", "invalid_cost")
            _require_currency(cost.value.fixed, currency, "costs")
        else:
            raise LoanValidationError("costs", "invalid_cost")


def _costs_on(costs: list[CostPeriod], day: date, opening_minor: int, currency: str) -> int:
    total = 0
    for cost in costs:
        if not cost_active_on(cost.effective_from, cost.effective_to, day):
            continue
        if cost.value.mode == "percent":
            assert cost.value.rate_bp is not None
            total += _half_up(opening_minor, cost.value.rate_bp, 10_000)
        else:
            assert cost.value.fixed is not None
            if cost.value.fixed.currency != currency:
                raise LoanValidationError("costs", "invalid_currency")
            total += cost.value.fixed.amount_minor
    return total


def _fixed_costs_on(costs: list[CostPeriod], day: date, currency: str) -> int:
    total = 0
    for cost in costs:
        if not cost_active_on(cost.effective_from, cost.effective_to, day):
            continue
        if cost.value.mode == "fixed":
            assert cost.value.fixed is not None
            if cost.value.fixed.currency != currency:
                raise LoanValidationError("costs", "invalid_currency")
            total += cost.value.fixed.amount_minor
    return total


def _accrued_costs(
    costs: list[CostPeriod],
    start: date,
    end: date,
    opening_minor: int,
    transfers: Sequence[Drawdown],
    currency: str,
    minor_unit: int,
) -> int:
    """Percent costs follow the drawn balance by calendar month; fixed costs stay per row."""
    if end <= start:
        raise ScheduleError("invalid_payment_date")
    events: set[date] = {end}
    transfer_by_date: dict[date, int] = {}
    for transfer in transfers:
        if start < transfer.date < end:
            events.add(transfer.date)
            transfer_by_date[transfer.date] = (
                transfer_by_date.get(transfer.date, 0) + transfer.amount.amount_minor
            )
    month = _first_of_next_month(start)
    while month < end:
        events.add(month)
        month = _first_of_next_month(month)
    for cost in costs:
        if start < cost.effective_from < end:
            events.add(cost.effective_from)
        if cost.effective_to is not None:
            day_after = cost.effective_to + timedelta(days=1)
            if start < day_after < end:
                events.add(day_after)
    percentage: list[tuple[CostPeriod, int]] = []
    for cost in costs:
        if cost.value.mode == "percent":
            rate_bp = cost.value.rate_bp
            assert rate_bp is not None
            percentage.append((cost, rate_bp))
    totals = [Decimal(0) for _ in percentage]
    balance = opening_minor
    cursor = start
    with localcontext() as context:
        context.prec = max(context.prec, 40)
        for event in sorted(events):
            if event <= start:
                continue
            if event >= end:
                break
            days = (event - cursor).days
            if days > 0:
                for index, (cost, rate_bp) in enumerate(percentage):
                    if cost_active_on(cost.effective_from, cost.effective_to, cursor):
                        totals[index] += (
                            Decimal(balance).scaleb(-minor_unit)
                            * Decimal(rate_bp)
                            / Decimal(10_000)
                            * Decimal(days)
                            / Decimal(_month_last_day(cursor.year, cursor.month))
                        )
            balance += transfer_by_date.get(event, 0)
            cursor = event
        days = (end - cursor).days
        for index, (cost, rate_bp) in enumerate(percentage):
            if cost_active_on(cost.effective_from, cost.effective_to, cursor):
                totals[index] += (
                    Decimal(balance).scaleb(-minor_unit)
                    * Decimal(rate_bp)
                    / Decimal(10_000)
                    * Decimal(days)
                    / Decimal(_month_last_day(cursor.year, cursor.month))
                )
    rounded = sum(_round_minor(value, currency, minor_unit) for value in totals)
    return rounded + _fixed_costs_on(costs, end, currency)


def _validate_one_off_overpayments(
    overpayments: list[OneOffOverpayment], currency: str
) -> list[OneOffOverpayment]:
    for row in overpayments:
        valid_date(row.date, "date")
        _require_currency(row.amount, currency, "overpayment")
        if row.amount.amount_minor <= 0:
            raise LoanValidationError("overpayment", "invalid_overpayment")
    return sorted(overpayments, key=lambda row: row.date)


def _regular_overpayment(
    terms: LoanTerms,
    mode: OverpaymentMode,
    *,
    principal_minor: int,
    interest_minor: int,
    costs_minor: int,
) -> int:
    if mode is OverpaymentMode.NONE:
        return 0
    assert terms.overpayment_amount is not None
    if mode is OverpaymentMode.FIXED_EXTRA:
        return terms.overpayment_amount.amount_minor
    target = terms.overpayment_amount.amount_minor
    return max(0, target - principal_minor - interest_minor - costs_minor)


def select_anchor(
    terms: LoanTerms,
    checkpoints: list[BalanceCheckpoint],
    as_of: date,
    drawdowns: Sequence[Drawdown] = (),
) -> ScheduleAnchor:
    """The latest checkpoint at or before ``as_of``, else the first transfer or origination."""
    valid_date(as_of, "as_of")
    _require_currency(terms.principal_original, terms.currency, "principal_original")
    if terms.principal_original is not None and terms.principal_original.amount_minor <= 0:
        raise LoanValidationError("principal_original", "invalid_principal")
    usable: list[BalanceCheckpoint] = []
    for checkpoint in checkpoints:
        valid_date(checkpoint.as_of, "as_of")
        _require_currency(checkpoint.remaining_principal, terms.currency, "checkpoint")
        if checkpoint.remaining_principal.amount_minor < 0:
            raise LoanValidationError("checkpoint", "invalid_balance")
        if checkpoint.as_of <= as_of:
            usable.append(checkpoint)
    if usable:
        latest = max(usable, key=lambda row: row.as_of)
        return ScheduleAnchor(date=latest.as_of, balance=latest.remaining_principal)
    if DrawdownMode(terms.drawdown_mode) is DrawdownMode.TRANCHES:
        ordered = sorted(drawdowns, key=lambda row: row.date)
        if not ordered:
            return ScheduleAnchor(date=as_of, balance=Money(0, terms.currency))
        first_date = ordered[0].date
        if first_date > as_of:
            return ScheduleAnchor(date=as_of, balance=Money(0, terms.currency))
        first_total = sum(
            drawdown.amount.amount_minor for drawdown in ordered if drawdown.date == first_date
        )
        return ScheduleAnchor(date=first_date, balance=Money(first_total, terms.currency))
    if terms.origination_date is None or terms.principal_original is None:
        raise ScheduleError("no_anchor")
    if terms.principal_original.amount_minor <= 0:
        raise LoanValidationError("principal_original", "invalid_principal")
    return ScheduleAnchor(date=terms.origination_date, balance=terms.principal_original)


def _rate_on_day(rate_timeline: Sequence[RatePeriod], day: date) -> int:
    chosen: int | None = None
    for period in rate_timeline:
        if period.effective_from <= day:
            chosen = period.annual_rate_bp
    if chosen is None:
        raise ScheduleError("missing_rate")
    return chosen


def _first_of_next_month(day: date) -> date:
    if day.month == 12:
        return date(day.year + 1, 1, 1)
    return date(day.year, day.month + 1, 1)


def _interest_slice(
    start: date,
    end: date,
    balance_minor: int,
    annual_rate_bp: int,
    day_count: DayCount,
    currency: str,
    minor_unit: int,
) -> Decimal:
    days = (end - start).days
    if days <= 0:
        return Decimal(0)
    balance = Decimal(balance_minor).scaleb(-minor_unit)
    annual = Decimal(annual_rate_bp) / Decimal(10_000)
    if day_count is DayCount.ACTUAL_365:
        return balance * annual * Decimal(days) / Decimal(365)
    return (
        balance
        * annual
        / Decimal(12)
        * Decimal(days)
        / Decimal(_month_last_day(start.year, start.month))
    )


def _accrued_interest(
    start: date,
    end: date,
    opening_minor: int,
    transfers: Sequence[Drawdown],
    rate_timeline: Sequence[RatePeriod],
    day_count: DayCount,
    currency: str,
    minor_unit: int,
) -> int:
    """Interest for one payment period, split at transfers, rate changes and month boundaries."""
    if end <= start:
        raise ScheduleError("invalid_payment_date")
    events: set[date] = {end}
    transfer_by_date: dict[date, int] = {}
    for transfer in transfers:
        if start < transfer.date < end:
            events.add(transfer.date)
            transfer_by_date[transfer.date] = (
                transfer_by_date.get(transfer.date, 0) + transfer.amount.amount_minor
            )
    for period in rate_timeline:
        if start < period.effective_from < end:
            events.add(period.effective_from)
    if day_count is DayCount.MONTHLY:
        month = _first_of_next_month(start)
        while month < end:
            events.add(month)
            month = _first_of_next_month(month)
    total = Decimal(0)
    balance = opening_minor
    cursor = start
    with localcontext() as context:
        context.prec = max(context.prec, 40)
        for event in sorted(events):
            if event <= start:
                continue
            if event >= end:
                break
            total += _interest_slice(
                cursor,
                event,
                balance,
                _rate_on_day(rate_timeline, cursor),
                day_count,
                currency,
                minor_unit,
            )
            balance += transfer_by_date.get(event, 0)
            cursor = event
        total += _interest_slice(
            cursor,
            end,
            balance,
            _rate_on_day(rate_timeline, cursor),
            day_count,
            currency,
            minor_unit,
        )
    return _round_minor(total, currency, minor_unit)


def _validate_drawdowns(drawdowns: Sequence[Drawdown], terms: LoanTerms) -> list[Drawdown]:
    ordered: list[Drawdown] = []
    total = 0
    for drawdown in drawdowns:
        valid_date(drawdown.date, "date")
        if terms.origination_date is not None and drawdown.date < terms.origination_date:
            raise LoanValidationError("date", "transfer_before_start")
        _require_currency(drawdown.amount, terms.currency, "drawdown")
        if drawdown.amount.amount_minor <= 0:
            raise LoanValidationError("amount", "invalid_drawdown")
        total += drawdown.amount.amount_minor
        ordered.append(drawdown)
    if terms.principal_original is not None and total > terms.principal_original.amount_minor:
        raise LoanValidationError("amount", "drawdown_exceeds_principal")
    return sorted(ordered, key=lambda row: row.date)


def build_schedule(
    terms: LoanTerms,
    rate_timeline: list[RatePeriod],
    costs: list[CostPeriod],
    overpayments: list[OneOffOverpayment],
    anchor: ScheduleAnchor,
    as_of: date,
    strategy: OverpaymentStrategy | str,
    day_count: DayCount | str,
    drawdowns: Sequence[Drawdown] = (),
) -> Schedule:
    """Build a pure monthly loan schedule from ``anchor``.

    ``terms.term_months`` is the number of scheduled payments from the anchor. F7.3/F7.5 must
    pass the remaining months when re-anchoring at a checkpoint. Overpayments after the payoff
    are ignored; one dated on or before the anchor is already reflected in the checkpoint.

    In ``tranches`` mode the anchor normally starts at zero; transfers during a period increase
    the debt and interest is accrued on the balance in force for each day/sub-period.
    """
    kind = _validate_terms(terms)
    wanted_mode = DrawdownMode(terms.drawdown_mode)
    try:
        wanted_strategy = OverpaymentStrategy(strategy)
    except ValueError:
        raise LoanValidationError("strategy", "invalid_strategy") from None
    overpayment_mode = OverpaymentMode(terms.overpayment_mode)
    try:
        wanted_day_count = DayCount(day_count)
    except ValueError:
        raise LoanValidationError("day_count", "invalid_day_count") from None
    valid_date(anchor.date, "anchor_date")
    valid_date(as_of, "as_of")
    _require_currency(anchor.balance, terms.currency, "anchor")
    if anchor.balance.amount_minor < 0:
        raise LoanValidationError("anchor", "invalid_balance")
    if as_of < anchor.date:
        raise LoanValidationError("as_of", "invalid_date")
    _validate_costs(costs, terms.currency)
    one_off = _validate_one_off_overpayments(overpayments, terms.currency)
    tranched = wanted_mode is DrawdownMode.TRANCHES
    draws = _validate_drawdowns(drawdowns, terms) if tranched else []
    future_draws = [drawdown for drawdown in draws if drawdown.date > anchor.date]

    if anchor.balance.amount_minor == 0 and not future_draws:
        drawn = (
            sum(drawdown.amount.amount_minor for drawdown in draws)
            if tranched
            else (terms.principal_original or anchor.balance).amount_minor
        )
        return Schedule(
            rows=(),
            starting_balance=anchor.balance,
            drawn_principal=_money(drawn, terms.currency),
            anchor_date=anchor.date,
            as_of=as_of,
            original_principal=terms.principal_original,
            currency=terms.currency,
            drawdowns=tuple(draws),
        )

    rates = _validate_rate_timeline(rate_timeline)
    first_draw = future_draws[0].date if anchor.balance.amount_minor == 0 and future_draws else None
    dates = _payment_dates(terms, anchor.date, first_draw)
    rows: list[ScheduleRow] = []
    balance = anchor.balance.amount_minor
    currency = terms.currency
    minor_unit = minor_unit_for(currency)
    base_equal_principal = (
        None
        if tranched or terms.interest_only_months > 0
        else _half_up(balance, 1, terms.term_months)
    )
    annuity_payment: Decimal | None = None
    annuity_growth: dict[int, tuple[int, Decimal]] = {}
    monthly_rates: dict[int, Decimal] = {}
    hold_payment = False
    previous_rate: int | None = None
    recompute_next = True
    one_off_index = 0
    draw_index = 0
    previous_date = anchor.date
    debt_start = (
        min(drawdown.date for drawdown in draws)
        if tranched and draws
        else (terms.origination_date or anchor.date)
    )
    interest_only_until = (
        _add_months(debt_start, terms.interest_only_months, debt_start.day)
        if terms.interest_only_months > 0
        else None
    )

    for index, due in enumerate(dates, start=1):
        period_draws = [
            drawdown for drawdown in future_draws if previous_date < drawdown.date <= due
        ]
        transfer_minor = sum(drawdown.amount.amount_minor for drawdown in period_draws)
        while draw_index < len(future_draws) and future_draws[draw_index].date <= due:
            draw_index += 1
        pending_draw = draw_index < len(future_draws)
        if balance <= 0 and transfer_minor == 0 and not pending_draw:
            break
        rate = _rate_on_day(rates, due)
        days = (due - previous_date).days
        if days <= 0:
            raise ScheduleError("invalid_payment_date")
        payment_balance = balance + transfer_minor
        interest_only = interest_only_until is not None and due <= interest_only_until

        if tranched:
            interest = _accrued_interest(
                previous_date,
                due,
                balance,
                period_draws,
                rates,
                wanted_day_count,
                currency,
                minor_unit,
            )
        elif wanted_day_count is DayCount.MONTHLY:
            interest = _half_up(balance, rate, 120_000)
        else:
            interest = _half_up(balance, rate * days, 3_650_000)
        costs_minor = (
            _accrued_costs(
                costs,
                previous_date,
                due,
                balance,
                period_draws,
                currency,
                minor_unit,
            )
            if tranched
            else _costs_on(costs, due, payment_balance, currency)
        )

        applying: list[OneOffOverpayment] = []
        while one_off_index < len(one_off) and one_off[one_off_index].date <= due:
            candidate = one_off[one_off_index]
            if candidate.date > anchor.date:
                applying.append(candidate)
            one_off_index += 1
        one_off_minor = sum(item.amount.amount_minor for item in applying)

        last_contractual = index == terms.term_months
        if last_contractual:
            principal = payment_balance
            overpayment = 0
        elif interest_only:
            principal = 0
            regular = _regular_overpayment(
                terms,
                overpayment_mode,
                principal_minor=0,
                interest_minor=interest,
                costs_minor=costs_minor,
            )
            overpayment = min(regular + one_off_minor, payment_balance)
        else:
            remaining = max(1, terms.term_months - (index - 1))
            payment_decimal = Decimal(payment_balance).scaleb(-minor_unit)
            if kind is LoanKind.EQUAL_PRINCIPAL:
                if base_equal_principal is None or transfer_minor > 0:
                    base_equal_principal = _half_up(payment_balance, 1, remaining)
                principal = min(base_equal_principal, payment_balance)
            else:
                if terms.installment_override is not None:
                    principal = terms.installment_override.amount_minor - interest
                    recompute_next = False
                else:
                    rate_changed = previous_rate is not None and previous_rate != rate
                    if recompute_next or rate_changed or not hold_payment or transfer_minor > 0:
                        monthly_rate = monthly_rates.get(rate)
                        if monthly_rate is None:
                            monthly_rate = monthly_rates[rate] = _monthly_rate(rate)
                        with localcontext() as context:
                            context.prec = max(context.prec, 40)
                            if monthly_rate == 0:
                                annuity_payment = payment_decimal / Decimal(remaining)
                            else:
                                factor = Decimal(1) + monthly_rate
                                cached = annuity_growth.get(rate)
                                if cached is None:
                                    growth = factor ** (-remaining)
                                else:
                                    cached_remaining, growth = cached
                                    if cached_remaining > remaining:
                                        for _ in range(cached_remaining - remaining):
                                            growth *= factor
                                    elif cached_remaining < remaining:
                                        growth = factor ** (-remaining)
                                annuity_growth[rate] = (remaining, growth)
                                annuity_payment = (
                                    payment_decimal * monthly_rate / (Decimal(1) - growth)
                                )
                        recompute_next = False
                        if rate_changed:
                            hold_payment = False
                    assert annuity_payment is not None
                    if tranched:
                        interest_decimal = Decimal(interest).scaleb(-minor_unit)
                        principal = _round_minor(
                            annuity_payment - interest_decimal, currency, minor_unit
                        )
                    else:
                        principal = _annuity_principal_minor(
                            payment_decimal,
                            monthly_rates[rate],
                            annuity_payment,
                            currency,
                            minor_unit,
                        )
                if principal < 0:
                    if tranched and index == 1:
                        # A first payment later than the next nominal day covers a longer
                        # period; its extra interest is paid instead of amortized.
                        principal = 0
                    else:
                        raise ScheduleError("negative_amortization")
                principal = min(principal, payment_balance)
            regular = _regular_overpayment(
                terms,
                overpayment_mode,
                principal_minor=principal,
                interest_minor=interest,
                costs_minor=costs_minor,
            )
            overpayment = min(regular + one_off_minor, payment_balance - principal)

        closing = payment_balance - principal - overpayment
        if closing < 0:
            raise ScheduleError("negative_closing")
        if not last_contractual and overpayment > 0 and kind is LoanKind.ANNUITY:
            if wanted_strategy is OverpaymentStrategy.REDUCE_INSTALLMENT:
                recompute_next = True
                hold_payment = False
            else:
                hold_payment = True
        rows.append(
            ScheduleRow(
                n=index,
                date=due,
                annual_rate_bp=rate,
                opening=_money(balance, currency),
                interest=_money(interest, currency),
                principal=_money(principal, currency),
                overpayment=_money(overpayment, currency),
                transfer=_money(transfer_minor, currency),
                costs=_money(costs_minor, currency),
                installment_total=_money(
                    interest + principal + overpayment + costs_minor, currency
                ),
                closing=_money(closing, currency),
            )
        )
        balance = closing
        previous_rate = rate
        previous_date = due

    if draw_index < len(future_draws):
        raise ScheduleError("transfer_after_final_payment")
    drawn = (
        sum(drawdown.amount.amount_minor for drawdown in draws)
        if tranched
        else (terms.principal_original or anchor.balance).amount_minor
    )
    return Schedule(
        rows=tuple(rows),
        starting_balance=anchor.balance,
        drawn_principal=_money(drawn, currency),
        anchor_date=anchor.date,
        as_of=as_of,
        original_principal=terms.principal_original,
        currency=currency,
        drawdowns=tuple(draws),
    )


def balance_on(schedule: Schedule, as_of: date) -> Money:
    """The projected balance at ``as_of`` including transfers after the last payment row."""
    valid_date(as_of, "as_of")
    if as_of < schedule.anchor_date:
        raise LoanValidationError("as_of", "invalid_date")
    passed = [row for row in schedule.rows if row.date <= as_of]
    last_date = passed[-1].date if passed else schedule.anchor_date
    value = passed[-1].closing.amount_minor if passed else schedule.starting_balance.amount_minor
    value += sum(
        drawdown.amount.amount_minor
        for drawdown in schedule.drawdowns
        if last_date < drawdown.date <= as_of
    )
    return _money(value, schedule.currency)


def summarize_schedule(schedule: Schedule, as_of: date) -> LoanScheduleSummary:
    """Lifetime and as-of totals for a built schedule (a payment on ``as_of`` is made)."""
    valid_date(as_of, "as_of")
    if as_of < schedule.anchor_date:
        raise LoanValidationError("as_of", "invalid_date")
    currency = schedule.currency
    future = [row for row in schedule.rows if row.date > as_of]
    remaining = balance_on(schedule, as_of)
    original = schedule.original_principal or schedule.starting_balance
    if schedule.drawdowns:
        drawn = _money(
            sum(
                drawdown.amount.amount_minor
                for drawdown in schedule.drawdowns
                if drawdown.date <= as_of
            ),
            currency,
        )
    else:
        drawn = schedule.drawn_principal
    paid_minor = max(0, drawn.amount_minor - remaining.amount_minor)
    undrawn_minor = max(0, original.amount_minor - drawn.amount_minor)
    if original.amount_minor > 0:
        percent = min(10_000, _half_up(paid_minor, 10_000, original.amount_minor))
        remaining_percent = min(
            10_000, _half_up(remaining.amount_minor, 10_000, original.amount_minor)
        )
        undrawn_percent = min(10_000, _half_up(undrawn_minor, 10_000, original.amount_minor))
    else:
        percent = 0
        remaining_percent = 0
        undrawn_percent = 0
    total_interest = sum(row.interest.amount_minor for row in schedule.rows)
    total_costs = sum(row.costs.amount_minor for row in schedule.rows)
    total_overpayments = sum(row.overpayment.amount_minor for row in schedule.rows)
    total_paid = sum(row.installment_total.amount_minor for row in schedule.rows)
    next_row = future[0] if future else None
    return LoanScheduleSummary(
        as_of=as_of,
        original_principal=original,
        drawn_principal=drawn,
        undrawn_principal=_money(undrawn_minor, currency),
        paid_principal=_money(paid_minor, currency),
        remaining_principal=remaining,
        next_payment_date=next_row.date if next_row is not None else None,
        next_installment=next_row.installment_total if next_row is not None else None,
        remaining_interest=_money(sum(row.interest.amount_minor for row in future), currency),
        remaining_costs=_money(sum(row.costs.amount_minor for row in future), currency),
        payoff_date=schedule.rows[-1].date if schedule.rows else None,
        months_remaining=len(future),
        percent_repaid_bp=percent,
        percent_remaining_bp=remaining_percent,
        percent_undrawn_bp=undrawn_percent,
        total_interest=_money(total_interest, currency),
        total_costs=_money(total_costs, currency),
        total_overpayments=_money(total_overpayments, currency),
        total_paid=_money(total_paid, currency),
    )
