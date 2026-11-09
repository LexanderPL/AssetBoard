"""Risk policy storage and the portfolio risk balance (F6.11)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from assetboard.db.models.identity import AuditEvent, User
from assetboard.db.models.investments import RiskTargetPolicy
from assetboard.domain.instruments import RiskTier
from assetboard.domain.money import Money
from assetboard.domain.risk import InvalidRiskPolicy, RiskPolicyMode
from assetboard.services.context import Context, PermissionDenied
from assetboard.services.investment_transactions import create_transaction
from assetboard.services.investments import create_asset_class, create_instrument
from assetboard.services.portfolio import portfolio_page
from assetboard.services.profile import update_profile
from assetboard.services.risk import load_policy, save_risk_policy
from tests.tenant.workspace_db import WorkspaceDb, workspace_db

TODAY = date(2026, 3, 1)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    yield from workspace_db(tmp_path)


@pytest.fixture
def team(db: WorkspaceDb) -> WorkspaceDb:
    db.add_member("olga", "owner")
    db.add_member("ed", "editor")
    db.add_member("vic", "viewer")
    return db


def _ctx(team: WorkspaceDb) -> Context:
    return team.ctx("ed", "editor")


def _class(team: WorkspaceDb, name: str, tier: RiskTier | None) -> UUID:
    return create_asset_class(
        team.factory,
        _ctx(team),
        name=name,
        color="#2563eb",
        investable=True,
        risk_tier=tier.value if tier is not None else "",
    ).id


def _instrument(
    team: WorkspaceDb,
    allocations: list[tuple[UUID, int]],
    *,
    name: str = "Fund",
    override: RiskTier | None = None,
) -> UUID:
    return create_instrument(
        team.factory,
        _ctx(team),
        name=name,
        symbol="",
        type="etf",
        quote_currency="EUR",
        unit_label="share",
        allocations=allocations,
        locale="en",
        risk_tier_override=override.value if override is not None else "",
    ).id


def _buy(team: WorkspaceDb, instrument_id: UUID) -> None:
    create_transaction(
        team.factory,
        _ctx(team),
        instrument_id=instrument_id,
        type_value="buy",
        date_value="2026-01-10",
        quantity_value="10",
        unit_price_value="100",
        fee_value="0",
        fx_value="1",
        note_value="",
        locale="en",
    )


def _set_profile(
    team: WorkspaceDb,
    *,
    birth_year: str = "1986",
    life_expectancy: str = "80",
) -> None:
    update_profile(
        team.factory,
        user_id=team.users["ed"],
        display_name="Ed",
        locale="en",
        theme="system",
        birth_year=birth_year,
        life_expectancy=life_expectancy,
        today=TODAY,
    )


def _save(
    team: WorkspaceDb,
    *,
    mode: str = "fixed",
    safe: str = "50",
    growth: str = "40",
    speculative: str = "10",
    tolerance: str = "5",
) -> None:
    save_risk_policy(
        team.factory,
        _ctx(team),
        mode=mode,
        safe=safe,
        growth=growth,
        speculative=speculative,
        tolerance=tolerance,
        locale="en",
    )


def test_a_missing_policy_means_the_fixed_defaults(team: WorkspaceDb) -> None:
    with team.factory() as session:
        policy, saved = load_policy(session, _ctx(team))
    assert not saved
    assert policy.mode is RiskPolicyMode.FIXED
    assert (policy.safe_bp, policy.growth_bp, policy.speculative_bp) == (5000, 4000, 1000)
    assert policy.tolerance_bp == 500


def test_save_policy_persists_values_and_audits(team: WorkspaceDb) -> None:
    _save(team, safe="45", growth="40", speculative="15", tolerance="3")

    with team.factory() as session:
        row = session.scalars(select(RiskTargetPolicy)).one()
        policy, saved = load_policy(session, _ctx(team))
    assert saved and policy.safe_bp == 4500 and policy.growth_bp == 4000
    assert policy.speculative_bp == 1500 and policy.tolerance_bp == 300
    with team.factory() as session:
        event = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "risk_policy.saved")
        ).one()
    assert row.mode == "fixed"
    assert event.data == {
        "mode": "fixed",
        "safe_bp": 4500,
        "growth_bp": 4000,
        "speculative_bp": 1500,
        "tolerance_bp": 300,
    }


def test_save_age_mode_requires_the_users_birth_year(team: WorkspaceDb) -> None:
    with pytest.raises(InvalidRiskPolicy) as caught:
        _save(team, mode="age_scaled", speculative="10")
    assert (caught.value.field, caught.value.code) == ("mode", "birth_year_required")

    _set_profile(team)
    _save(team, mode="age_scaled", safe="50", growth="40", speculative="10")
    with team.factory() as session:
        policy, saved = load_policy(session, _ctx(team))
    assert saved and policy.mode is RiskPolicyMode.AGE_SCALED
    assert policy.speculative_bp == 1000


def test_invalid_policy_values_are_refused(team: WorkspaceDb) -> None:
    with pytest.raises(InvalidRiskPolicy) as total:
        _save(team, safe="50", growth="40", speculative="20")
    assert (total.value.field, total.value.code) == ("targets", "targets_total")
    with pytest.raises(InvalidRiskPolicy):
        _save(team, safe="not-a-number")
    with pytest.raises(InvalidRiskPolicy):
        _save(team, tolerance="101")
    with pytest.raises(InvalidRiskPolicy):
        _save(team, mode="unknown")


def test_only_editors_can_save_the_policy(team: WorkspaceDb) -> None:
    with pytest.raises(PermissionDenied):
        save_risk_policy(
            team.factory,
            team.ctx("vic", "viewer"),
            mode="fixed",
            safe="50",
            growth="40",
            speculative="10",
            tolerance="5",
            locale="en",
        )


def test_portfolio_risk_uses_class_tiers_look_through_and_override(
    team: WorkspaceDb,
) -> None:
    safe = _class(team, "Bonds", RiskTier.SAFE)
    growth = _class(team, "Stocks", RiskTier.GROWTH)
    instrument = _instrument(team, [(safe, 5000), (growth, 5000)])
    _buy(team, instrument)

    page = portfolio_page(team.factory, _ctx(team))
    rows = {row.tier: row for row in page.risk.balance.tiers}
    assert rows[RiskTier.SAFE].value == Money(50000, "EUR")
    assert rows[RiskTier.GROWTH].value == Money(50000, "EUR")
    assert page.risk.balance.hint is not None

    override = _instrument(team, [(safe, 10_000)], name="Special", override=RiskTier.SPECULATIVE)
    _buy(team, override)
    page = portfolio_page(team.factory, _ctx(team))
    rows = {row.tier: row for row in page.risk.balance.tiers}
    assert rows[RiskTier.SAFE].value == Money(50_000, "EUR")
    assert rows[RiskTier.SPECULATIVE].value == Money(100_000, "EUR")


def test_portfolio_risk_keeps_untiered_classes_unassigned(team: WorkspaceDb) -> None:
    safe = _class(team, "Bonds", RiskTier.SAFE)
    other = _class(team, "Other", None)
    _buy(team, _instrument(team, [(safe, 5000), (other, 5000)]))

    page = portfolio_page(team.factory, _ctx(team))

    assert page.risk.balance.unassigned == Money(50_000, "EUR")
    assert page.risk.balance.unassigned_bp == 5000
    assert sum(row.current_bp or 0 for row in page.risk.balance.tiers) == 10_000


def test_age_targets_use_the_viewers_profile(team: WorkspaceDb) -> None:
    _set_profile(team, birth_year="1986", life_expectancy="80")
    _save(team, mode="age_scaled", safe="50", growth="40", speculative="10")
    safe = _class(team, "Bonds", RiskTier.SAFE)
    _buy(team, _instrument(team, [(safe, 10_000)]))

    page = portfolio_page(team.factory, _ctx(team))

    assert page.risk.age == 40
    assert page.risk.balance.targets_available
    rows = {row.tier: row for row in page.risk.balance.tiers}
    assert rows[RiskTier.SAFE].target_bp == 4500
    assert rows[RiskTier.GROWTH].target_bp == 4500
    assert rows[RiskTier.SPECULATIVE].target_bp == 1000


def test_age_mode_without_a_profile_birth_year_has_no_targets(team: WorkspaceDb) -> None:
    _set_profile(team)
    _save(team, mode="age_in_bonds", safe="50", growth="40", speculative="0")
    with team.factory.begin() as session:
        user = session.get(User, team.users["ed"])
        assert user is not None
        user.birth_year = None

    page = portfolio_page(team.factory, _ctx(team))

    assert page.risk.age is None
    assert not page.risk.balance.targets_available
    assert all(row.target_bp is None for row in page.risk.balance.tiers)
