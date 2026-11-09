"""Risk tiers, target policies and the portfolio risk balance (F6.11).

Pure: no I/O and no clock. Targets are integer basis points and always total 10 000. The
balance uses the same look-through parts as the portfolio page: an instrument override moves
all of its value to one tier, otherwise each class part follows its class's tier. Classes
without a tier are Unassigned and stay out of the tier percentages.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from assetboard.domain.instruments import RiskTier
from assetboard.domain.money import Money

RISK_TIERS: tuple[RiskTier, ...] = (
    RiskTier.SAFE,
    RiskTier.GROWTH,
    RiskTier.SPECULATIVE,
)
DEFAULT_SAFE_BP = 5000
DEFAULT_GROWTH_BP = 4000
DEFAULT_SPECULATIVE_BP = 1000
DEFAULT_TOLERANCE_BP = 500
MIN_LIFE_EXPECTANCY = 50
MAX_LIFE_EXPECTANCY = 120


def _(message: str) -> str:
    """Marks a message for extraction; it is translated where it is rendered."""
    return message


class RiskPolicyMode(StrEnum):
    FIXED = "fixed"
    AGE_SCALED = "age_scaled"
    AGE_IN_BONDS = "age_in_bonds"


RISK_POLICY_MODE_LABELS: dict[RiskPolicyMode, str] = {
    RiskPolicyMode.FIXED: _("Fixed targets"),
    RiskPolicyMode.AGE_SCALED: _("Age-scaled"),
    RiskPolicyMode.AGE_IN_BONDS: _("Age in bonds"),
}


class InvalidRiskPolicy(ValueError):
    """A policy value is invalid; ``field`` and ``code`` are form-friendly."""

    def __init__(self, field: str, code: str) -> None:
        super().__init__(code)
        self.field = field
        self.code = code


class RiskTargetsUnavailable(ValueError):
    """An age-based policy needs the viewer's birth year."""


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    mode: RiskPolicyMode
    safe_bp: int
    growth_bp: int
    speculative_bp: int
    tolerance_bp: int


@dataclass(frozen=True, slots=True)
class TierBalance:
    tier: RiskTier
    value: Money
    current_bp: int | None
    target_bp: int | None
    target_value: Money | None
    difference_bp: int | None
    difference: Money | None
    flagged: bool


@dataclass(frozen=True, slots=True)
class RiskHint:
    from_tier: RiskTier
    to_tier: RiskTier
    amount: Money


@dataclass(frozen=True, slots=True)
class RiskBalance:
    currency: str
    tiers: tuple[TierBalance, ...]
    assigned: Money
    unassigned: Money
    unassigned_bp: int | None
    hint: RiskHint | None
    targets_available: bool


def default_policy() -> RiskPolicy:
    return RiskPolicy(
        RiskPolicyMode.FIXED,
        DEFAULT_SAFE_BP,
        DEFAULT_GROWTH_BP,
        DEFAULT_SPECULATIVE_BP,
        DEFAULT_TOLERANCE_BP,
    )


def _validate_bp(value: object, field: str) -> int:
    if type(value) is not int or not 0 <= value <= 10_000:
        raise InvalidRiskPolicy(field, "invalid_percent")
    return value


def validate_policy(policy: RiskPolicy) -> RiskPolicy:
    """Check the stored values; the fixed mode must total exactly 100%."""
    try:
        mode = RiskPolicyMode(policy.mode)
    except ValueError:
        raise InvalidRiskPolicy("mode", "invalid_mode") from None
    safe = _validate_bp(policy.safe_bp, "safe")
    growth = _validate_bp(policy.growth_bp, "growth")
    speculative = _validate_bp(policy.speculative_bp, "speculative")
    tolerance = _validate_bp(policy.tolerance_bp, "tolerance")
    if mode is RiskPolicyMode.FIXED and safe + growth + speculative != 10_000:
        raise InvalidRiskPolicy("targets", "targets_total")
    return RiskPolicy(mode, safe, growth, speculative, tolerance)


def _half_up_div(numerator: int, denominator: int) -> int:
    quotient, remainder = divmod(numerator, denominator)
    return quotient + (1 if remainder * 2 >= denominator else 0)


def _tier_order(tier: RiskTier) -> int:
    return RISK_TIERS.index(tier)


def _difference_minor(row: TierBalance) -> int:
    return row.difference.amount_minor if row.difference is not None else 0


def risk_targets(
    policy: RiskPolicy,
    age: int | None,
    life_expectancy: int,
) -> dict[RiskTier, int]:
    """Target basis points for the policy; age modes need a real age."""
    policy = validate_policy(policy)
    if policy.mode is RiskPolicyMode.FIXED:
        return {
            RiskTier.SAFE: policy.safe_bp,
            RiskTier.GROWTH: policy.growth_bp,
            RiskTier.SPECULATIVE: policy.speculative_bp,
        }
    if age is None:
        raise RiskTargetsUnavailable
    if type(age) is not int or age < 0:
        raise InvalidRiskPolicy("age", "invalid_age")
    if not MIN_LIFE_EXPECTANCY <= life_expectancy <= MAX_LIFE_EXPECTANCY:
        raise InvalidRiskPolicy("life_expectancy", "invalid_life_expectancy")
    speculative = policy.speculative_bp
    remaining = 10_000 - speculative
    if policy.mode is RiskPolicyMode.AGE_SCALED:
        safe = min(_half_up_div(remaining * age, life_expectancy), remaining)
    else:
        safe = min(age * 100, remaining)
    return {
        RiskTier.SAFE: safe,
        RiskTier.GROWTH: remaining - safe,
        RiskTier.SPECULATIVE: speculative,
    }


def _validate_targets(targets: Mapping[RiskTier, int] | None) -> None:
    if targets is None:
        return
    if set(targets) != set(RISK_TIERS):
        raise ValueError("targets must name every risk tier")
    total = 0
    for tier in RISK_TIERS:
        value = targets[tier]
        if type(value) is not int or not 0 <= value <= 10_000:
            raise ValueError("target basis points are invalid")
        total += value
    if total != 10_000:
        raise ValueError("targets must total 10 000 basis points")


def _current_percentages(
    values: Mapping[RiskTier, int], assigned: int
) -> dict[RiskTier, int | None]:
    if assigned <= 0:
        return dict.fromkeys(RISK_TIERS)
    raw = {tier: values[tier] * 10_000 for tier in RISK_TIERS}
    floors = {tier: raw[tier] // assigned for tier in RISK_TIERS}
    remainder = 10_000 - sum(floors.values())
    ranked = sorted(
        RISK_TIERS,
        key=lambda tier: (-(raw[tier] % assigned), _tier_order(tier)),
    )
    for tier in ranked[:remainder]:
        floors[tier] += 1
    result: dict[RiskTier, int | None] = dict(floors)
    return result


def risk_balance(
    class_values: Mapping[tuple[UUID, UUID | None], Money],
    class_tiers: Mapping[UUID, RiskTier | None],
    overrides: Mapping[UUID, RiskTier | None],
    targets: Mapping[RiskTier, int] | None,
    tolerance_bp: int,
    *,
    currency: str,
) -> RiskBalance:
    """Split the look-through parts into tiers and compare them with ``targets``."""
    _validate_targets(targets)
    if type(tolerance_bp) is not int or not 0 <= tolerance_bp <= 10_000:
        raise ValueError("tolerance basis points are invalid")
    amounts = dict.fromkeys(RISK_TIERS, 0)
    unassigned = 0
    for (instrument_id, class_id), amount in class_values.items():
        if amount.currency != currency:
            raise ValueError("risk values must use one currency")
        override = overrides.get(instrument_id)
        tier = override
        if tier is None and class_id is not None:
            tier = class_tiers.get(class_id)
        if tier is None:
            unassigned += amount.amount_minor
        else:
            amounts[tier] += amount.amount_minor

    assigned = sum(amounts.values())
    total = assigned + unassigned
    current = _current_percentages(amounts, assigned)
    rows: list[TierBalance] = []
    for tier in RISK_TIERS:
        value = Money(amounts[tier], currency)
        target_bp: int | None = targets[tier] if targets is not None else None
        target_value = (
            Money(_half_up_div(assigned * target_bp, 10_000), currency)
            if target_bp is not None
            else None
        )
        current_bp = current[tier]
        difference_bp: int | None = None
        if current_bp is not None and target_bp is not None:
            difference_bp = current_bp - target_bp
        difference = (
            Money(value.amount_minor - target_value.amount_minor, currency)
            if target_value is not None
            else None
        )
        rows.append(
            TierBalance(
                tier=tier,
                value=value,
                current_bp=current[tier],
                target_bp=target_bp,
                target_value=target_value,
                difference_bp=difference_bp,
                difference=difference,
                flagged=(difference_bp is not None and abs(difference_bp) > tolerance_bp),
            )
        )

    hint: RiskHint | None = None
    if targets is not None and assigned > 0:
        gaps = [row for row in rows if row.difference_bp is not None]
        largest = max(gaps, key=lambda row: (abs(row.difference_bp or 0), -_tier_order(row.tier)))
        if abs(largest.difference_bp or 0) > tolerance_bp:
            overweight = [row for row in rows if _difference_minor(row) > 0]
            underweight = [row for row in rows if _difference_minor(row) < 0]
            if overweight and underweight:
                most_over = max(
                    overweight,
                    key=lambda row: (_difference_minor(row), -_tier_order(row.tier)),
                )
                most_under = min(
                    underweight,
                    key=lambda row: (_difference_minor(row), _tier_order(row.tier)),
                )
                amount = min(_difference_minor(most_over), -_difference_minor(most_under))
                if amount > 0:
                    hint = RiskHint(most_over.tier, most_under.tier, Money(amount, currency))

    return RiskBalance(
        currency=currency,
        tiers=tuple(rows),
        assigned=Money(assigned, currency),
        unassigned=Money(unassigned, currency),
        unassigned_bp=(_half_up_div(unassigned * 10_000, total) if total > 0 else None),
        hint=hint,
        targets_available=targets is not None,
    )
