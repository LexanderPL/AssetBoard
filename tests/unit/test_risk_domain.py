"""Pure risk targets and balance (F6.11)."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from assetboard.domain.instruments import RiskTier
from assetboard.domain.money import Money
from assetboard.domain.risk import (
    InvalidRiskPolicy,
    RiskPolicy,
    RiskPolicyMode,
    RiskTargetsUnavailable,
    default_policy,
    risk_balance,
    risk_targets,
)


def _policy(
    mode: RiskPolicyMode = RiskPolicyMode.FIXED,
    safe: int = 5000,
    growth: int = 4000,
    speculative: int = 1000,
    tolerance: int = 500,
) -> RiskPolicy:
    return RiskPolicy(mode, safe, growth, speculative, tolerance)


def test_fixed_targets_are_the_stored_values() -> None:
    assert risk_targets(_policy(), None, 90) == {
        RiskTier.SAFE: 5000,
        RiskTier.GROWTH: 4000,
        RiskTier.SPECULATIVE: 1000,
    }


def test_age_scaled_golden_example_is_45_45_10() -> None:
    assert risk_targets(_policy(RiskPolicyMode.AGE_SCALED), 40, 80) == {
        RiskTier.SAFE: 4500,
        RiskTier.GROWTH: 4500,
        RiskTier.SPECULATIVE: 1000,
    }


def test_age_in_bonds_golden_example_is_35_55_10() -> None:
    assert risk_targets(_policy(RiskPolicyMode.AGE_IN_BONDS), 35, 90) == {
        RiskTier.SAFE: 3500,
        RiskTier.GROWTH: 5500,
        RiskTier.SPECULATIVE: 1000,
    }


@pytest.mark.parametrize(
    ("age", "life", "safe"),
    [(80, 80, 9000), (95, 80, 9000), (0, 80, 0)],
)
def test_age_targets_cap_safe_at_the_remaining_share(age: int, life: int, safe: int) -> None:
    targets = risk_targets(_policy(RiskPolicyMode.AGE_SCALED), age, life)
    assert targets[RiskTier.SAFE] == safe
    assert sum(targets.values()) == 10_000


def test_age_targets_round_half_up_and_put_the_remainder_on_growth() -> None:
    targets = risk_targets(_policy(RiskPolicyMode.AGE_SCALED), 1, 80)
    assert targets[RiskTier.SAFE] == 113
    assert targets[RiskTier.GROWTH] == 8887
    assert sum(targets.values()) == 10_000


def test_age_modes_need_a_birth_year() -> None:
    with pytest.raises(RiskTargetsUnavailable):
        risk_targets(_policy(RiskPolicyMode.AGE_SCALED), None, 90)


def test_fixed_targets_must_total_100_percent() -> None:
    with pytest.raises(InvalidRiskPolicy):
        risk_targets(_policy(safe=5000, growth=4000, speculative=2000), None, 90)


@given(
    speculative=st.integers(min_value=0, max_value=10_000),
    age=st.integers(min_value=0, max_value=120),
)
def test_targets_always_total_100_percent(speculative: int, age: int) -> None:
    for mode in (RiskPolicyMode.AGE_SCALED, RiskPolicyMode.AGE_IN_BONDS):
        targets = risk_targets(_policy(mode, speculative=speculative), age, 90)
        assert sum(targets.values()) == 10_000
        assert all(value >= 0 for value in targets.values())


@given(
    age=st.integers(min_value=0, max_value=120),
    older=st.integers(min_value=0, max_value=120),
)
def test_older_age_never_lowers_safe(age: int, older: int) -> None:
    young = min(age, older)
    old = max(age, older)
    for mode in (RiskPolicyMode.AGE_SCALED, RiskPolicyMode.AGE_IN_BONDS):
        policy = _policy(mode)
        assert (
            risk_targets(policy, old, 90)[RiskTier.SAFE]
            >= risk_targets(policy, young, 90)[RiskTier.SAFE]
        )


def _parts(
    values: list[tuple[UUID, UUID | None, int]],
) -> dict[tuple[UUID, UUID | None], Money]:
    return {(instrument, class_id): Money(amount, "EUR") for instrument, class_id, amount in values}


def test_balance_uses_class_look_through_and_flags_the_largest_gap() -> None:
    safe = uuid4()
    growth = uuid4()
    instrument = uuid4()
    balance = risk_balance(
        _parts([(instrument, safe, 2000), (instrument, growth, 8000)]),
        {safe: RiskTier.SAFE, growth: RiskTier.GROWTH},
        {},
        {RiskTier.SAFE: 5000, RiskTier.GROWTH: 4000, RiskTier.SPECULATIVE: 1000},
        500,
        currency="EUR",
    )
    rows = {row.tier: row for row in balance.tiers}
    assert rows[RiskTier.SAFE].current_bp == 2000
    assert rows[RiskTier.GROWTH].current_bp == 8000
    assert rows[RiskTier.SAFE].difference_bp == -3000
    assert rows[RiskTier.GROWTH].difference_bp == 4000
    assert rows[RiskTier.SAFE].flagged and rows[RiskTier.GROWTH].flagged
    assert balance.hint is not None
    assert balance.hint.from_tier is RiskTier.GROWTH
    assert balance.hint.to_tier is RiskTier.SAFE
    assert balance.hint.amount == Money(3000, "EUR")


def test_instrument_override_moves_every_part_to_one_tier() -> None:
    safe = uuid4()
    growth = uuid4()
    instrument = uuid4()
    balance = risk_balance(
        _parts([(instrument, safe, 4000), (instrument, growth, 6000)]),
        {safe: RiskTier.SAFE, growth: RiskTier.GROWTH},
        {instrument: RiskTier.SPECULATIVE},
        None,
        500,
        currency="EUR",
    )
    rows = {row.tier: row for row in balance.tiers}
    assert rows[RiskTier.SPECULATIVE].value == Money(10_000, "EUR")
    assert rows[RiskTier.SAFE].value == Money(0, "EUR")
    assert rows[RiskTier.GROWTH].value == Money(0, "EUR")


def test_classes_without_a_tier_are_unassigned_and_excluded_from_percentages() -> None:
    safe = uuid4()
    unassigned = uuid4()
    instrument_a, instrument_b = uuid4(), uuid4()
    balance = risk_balance(
        _parts(
            [
                (instrument_a, safe, 6000),
                (instrument_b, unassigned, 4000),
            ]
        ),
        {safe: RiskTier.SAFE},
        {},
        {RiskTier.SAFE: 5000, RiskTier.GROWTH: 4000, RiskTier.SPECULATIVE: 1000},
        500,
        currency="EUR",
    )
    rows = {row.tier: row for row in balance.tiers}
    assert rows[RiskTier.SAFE].current_bp == 10_000
    assert balance.unassigned == Money(4000, "EUR")
    assert balance.unassigned_bp == 4000
    assert sum(row.current_bp or 0 for row in balance.tiers) == 10_000


def test_within_tolerance_shows_no_hint() -> None:
    safe, growth, spec = uuid4(), uuid4(), uuid4()
    i1, i2, i3 = uuid4(), uuid4(), uuid4()
    balance = risk_balance(
        _parts([(i1, safe, 5300), (i2, growth, 4000), (i3, spec, 700)]),
        {safe: RiskTier.SAFE, growth: RiskTier.GROWTH, spec: RiskTier.SPECULATIVE},
        {},
        {RiskTier.SAFE: 5000, RiskTier.GROWTH: 4000, RiskTier.SPECULATIVE: 1000},
        500,
        currency="EUR",
    )
    assert balance.hint is None
    assert not any(row.flagged for row in balance.tiers)


def test_balance_percentages_always_total_100_percent() -> None:
    safe, growth, spec = uuid4(), uuid4(), uuid4()
    i1, i2, i3 = uuid4(), uuid4(), uuid4()
    balance = risk_balance(
        _parts([(i1, safe, 1), (i2, growth, 1), (i3, spec, 1)]),
        {safe: RiskTier.SAFE, growth: RiskTier.GROWTH, spec: RiskTier.SPECULATIVE},
        {},
        None,
        500,
        currency="EUR",
    )
    assert sum(row.current_bp or 0 for row in balance.tiers) == 10_000


def test_default_policy_is_the_owner_agreed_fixed_mix() -> None:
    policy = default_policy()
    assert policy.mode is RiskPolicyMode.FIXED
    assert (policy.safe_bp, policy.growth_bp, policy.speculative_bp) == (5000, 4000, 1000)
    assert policy.tolerance_bp == 500
