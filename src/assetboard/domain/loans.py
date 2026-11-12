"""Loan configuration rules (F7.1). Pure: no I/O, no database, no clock.

F7.1 stores terms only. The schedule engine arrives in F7.2; the one finance helper here,
``first_installment``, exists solely so the target-total validation can compare the configured
monthly target with the first installment. F7.2 reuses it.
"""

from __future__ import annotations

import calendar
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, localcontext
from enum import StrEnum

from assetboard.domain.money import Money, minor_unit_for
from assetboard.domain.months import MAX_YEAR, MIN_YEAR
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


class RateMode(StrEnum):
    FIXED = "fixed"
    VARIABLE = "variable"


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
    minor_unit = minor_unit_for(principal.currency)
    with localcontext() as context:
        context.prec = max(context.prec, 40)
        amount = principal.to_decimal()
        monthly_rate = Decimal(annual_rate_bp).scaleb(-4) / Decimal(12)
        if kind is LoanKind.EQUAL_PRINCIPAL:
            payment = amount / Decimal(term_months) + amount * monthly_rate
        elif monthly_rate == 0:
            payment = amount / Decimal(term_months)
        else:
            growth = (Decimal(1) + monthly_rate) ** (-term_months)
            payment = amount * monthly_rate / (Decimal(1) - growth)
        rounded = payment.quantize(Decimal(1).scaleb(-minor_unit), rounding=ROUND_HALF_UP)
        minor = int(rounded.scaleb(minor_unit))
    if not 0 < minor <= MAX_AMOUNT_MINOR:
        raise LoanValidationError("principal", "invalid_principal")
    return Money(minor, principal.currency)
