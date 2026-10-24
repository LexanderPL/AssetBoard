from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from assetboard.domain.money import Money
from assetboard.domain.percent_or_fixed import PercentOrFixed


class TaxEffect(StrEnum):
    ADDED = "added"
    WITHHELD = "withheld"


class TaxBase(StrEnum):
    NET = "net"
    GROSS = "gross"


class RecordAs(StrEnum):
    CASH_RECEIVED = "cash_received"
    NET = "net"
    TAKE_HOME = "take_home"


@dataclass(frozen=True, slots=True)
class TaxInput:
    name: str
    effect: TaxEffect
    base: TaxBase
    value: PercentOrFixed
    generate_payment: bool = True

    def __post_init__(self) -> None:
        effect = TaxEffect(self.effect)
        base = TaxBase(self.base)
        object.__setattr__(self, "effect", effect)
        object.__setattr__(self, "base", base)
        if effect == TaxEffect.ADDED and base != TaxBase.NET:
            raise ValueError("added_tax_base_must_be_net")


@dataclass(frozen=True, slots=True)
class TaxLine:
    name: str
    effect: TaxEffect
    amount: Money
    planned_payment: bool


@dataclass(frozen=True, slots=True)
class TaxBreakdown:
    net: Money
    added: tuple[TaxLine, ...]
    gross: Money
    withheld: tuple[TaxLine, ...]
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
    minor = int(
        (Decimal(base.amount_minor) * Decimal(value.rate_bp) / Decimal(10_000)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    return Money(minor, base.currency)


def calculate_taxes(
    net: Money, rules: tuple[TaxInput, ...] | list[TaxInput], record_as: RecordAs
) -> TaxBreakdown:
    """Calculate each tax independently, rounding every percentage to a minor unit."""
    record_as = RecordAs(record_as)
    if net.amount_minor < 0:
        raise ValueError("net_must_not_be_negative")

    added = tuple(
        TaxLine(
            rule.name,
            rule.effect,
            _tax_amount(net, rule.value),
            rule.generate_payment,
        )
        for rule in rules
        if rule.effect == TaxEffect.ADDED
    )
    gross = Money(net.amount_minor + sum(line.amount.amount_minor for line in added), net.currency)
    withheld = tuple(
        TaxLine(
            rule.name,
            rule.effect,
            _tax_amount(net if rule.base == TaxBase.NET else gross, rule.value),
            rule.generate_payment,
        )
        for rule in rules
        if rule.effect == TaxEffect.WITHHELD
    )
    take_home = Money(
        net.amount_minor - sum(line.amount.amount_minor for line in withheld), net.currency
    )
    if record_as == RecordAs.CASH_RECEIVED:
        ledger_amount = gross
        eligible = (*added, *withheld)
    elif record_as == RecordAs.NET:
        ledger_amount = net
        eligible = withheld
    else:
        ledger_amount = take_home
        eligible = ()
    payments = tuple(line for line in eligible if line.planned_payment)
    return TaxBreakdown(net, added, gross, withheld, take_home, ledger_amount, payments)
