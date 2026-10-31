"""Tax calculation and tax-payment dates (F3.3, redesigned by ADR-0022).

Each tax rule has one ``kind`` that says who pays it to the tax office:

- ``added``: the user charges it on top of the net amount (VAT-style). It increases what the
  user receives and a payment is planned.
- ``self_paid``: the user pays it from the income. What the user receives is unchanged and a
  payment is planned.
- ``payer_withheld``: the payer takes it off before paying. It reduces what the user receives
  and no payment is planned.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from assetboard.domain.money import Money
from assetboard.domain.percent_or_fixed import PercentOrFixed


class TaxKind(StrEnum):
    ADDED = "added"
    SELF_PAID = "self_paid"
    PAYER_WITHHELD = "payer_withheld"

    @property
    def plans_payment(self) -> bool:
        return self != TaxKind.PAYER_WITHHELD


class TaxBase(StrEnum):
    NET = "net"
    GROSS = "gross"


class PaymentTiming(StrEnum):
    NEXT_MONTH = "next_month"
    SAME_MONTH = "same_month"
    AFTER_QUARTER = "after_quarter"


@dataclass(frozen=True, slots=True)
class TaxInput:
    name: str
    kind: TaxKind
    base: TaxBase
    value: PercentOrFixed
    generate_payment: bool = True

    def __post_init__(self) -> None:
        kind = TaxKind(self.kind)
        base = TaxBase(self.base)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "base", base)
        if kind == TaxKind.ADDED and base != TaxBase.NET:
            raise ValueError("added_tax_base_must_be_net")


@dataclass(frozen=True, slots=True)
class TaxLine:
    name: str
    kind: TaxKind
    amount: Money
    planned_payment: bool


@dataclass(frozen=True, slots=True)
class TaxBreakdown:
    """``ledger_amount`` is what the user receives: the income occurrence's expected amount."""

    net: Money
    added: tuple[TaxLine, ...]
    gross: Money
    # Taxes on the income: self-paid and payer-withheld lines, in rule order.
    deducted: tuple[TaxLine, ...]
    take_home: Money
    ledger_amount: Money
    payments: tuple[TaxLine, ...]


def _tax_amount(base: Money, value: PercentOrFixed) -> Money:
    if value.mode == "fixed":
        assert value.fixed is not None
        if value.fixed.currency != base.currency:
            raise ValueError("currency_mismatch")
        return value.fixed
    assert value.rate_bp is not None
    # Exact integer arithmetic at any magnitude (Decimal's default 28-digit context would
    # round before the half-up step for very large products). Bases are never negative here.
    quotient, remainder = divmod(base.amount_minor * value.rate_bp, 10_000)
    if remainder * 2 >= 10_000:
        quotient += 1
    return Money(quotient, base.currency)


def _sum(lines: tuple[TaxLine, ...]) -> int:
    return sum(line.amount.amount_minor for line in lines)


def calculate_taxes(net: Money, rules: tuple[TaxInput, ...] | list[TaxInput]) -> TaxBreakdown:
    """Calculate each tax independently, rounding every percentage to a minor unit.

    received (ledger amount) = net + added - payer_withheld
    payments                 = added + self_paid (where generate_payment)
    take_home                = net - self_paid - payer_withheld
    """
    if net.amount_minor < 0:
        raise ValueError("net_must_not_be_negative")
    added = tuple(
        TaxLine(rule.name, rule.kind, _tax_amount(net, rule.value), rule.generate_payment)
        for rule in rules
        if rule.kind == TaxKind.ADDED
    )
    gross = Money(net.amount_minor + _sum(added), net.currency)
    deducted = tuple(
        TaxLine(
            rule.name,
            rule.kind,
            _tax_amount(net if rule.base == TaxBase.NET else gross, rule.value),
            rule.generate_payment and rule.kind.plans_payment,
        )
        for rule in rules
        if rule.kind != TaxKind.ADDED
    )
    take_home = Money(net.amount_minor - _sum(deducted), net.currency)
    withheld = _sum(tuple(line for line in deducted if line.kind == TaxKind.PAYER_WITHHELD))
    ledger_amount = Money(gross.amount_minor - withheld, net.currency)
    payments = tuple(line for line in (*added, *deducted) if line.planned_payment)
    return TaxBreakdown(net, added, gross, deducted, take_home, ledger_amount, payments)


def _clamped(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, monthrange(year, month)[1]))


def _shift(year: int, month: int, offset: int) -> tuple[int, int]:
    index = year * 12 + month - 1 + offset
    shifted_year, month_index = divmod(index, 12)
    return shifted_year, month_index + 1


def timing_month_offset(paid_on: date, timing: PaymentTiming) -> int:
    """Months between the pay date's month and the tax payment's month."""
    timing = PaymentTiming(timing)
    if timing == PaymentTiming.SAME_MONTH:
        return 0
    if timing == PaymentTiming.NEXT_MONTH:
        return 1
    # The month after the end of the pay date's calendar quarter (Mar, Jun, Sep, Dec).
    quarter_end = (paid_on.month - 1) // 3 * 3 + 3
    return quarter_end - paid_on.month + 1


def tax_payment_date(paid_on: date, day: int, timing: PaymentTiming) -> date:
    """The tax payment date for income paid on ``paid_on``: ``day`` (clamped to the month's
    length) of the month that ``timing`` names."""
    return tax_payment_date_with_offset(paid_on, day, timing_month_offset(paid_on, timing))


def tax_payment_date_with_offset(paid_on: date, day: int, month_offset: int) -> date:
    """``day`` (clamped) of the month ``month_offset`` months after the pay date's month."""
    if not 1 <= day <= 31 or month_offset < 0:
        raise ValueError("invalid_payment_schedule")
    year, month = _shift(paid_on.year, paid_on.month, month_offset)
    return _clamped(year, month, day)
