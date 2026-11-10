"""Risk target policy and portfolio risk balance (F6.11)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.engine import run_immediate
from assetboard.db.models.identity import User
from assetboard.db.models.investments import RiskTargetPolicy
from assetboard.db.repositories import (
    AuditEventRepository,
    CurrentWorkspaceRepository,
    RiskTargetPolicyRepository,
)
from assetboard.domain.instruments import InvalidAllocation, RiskTier, percent_to_bp
from assetboard.domain.money import Money
from assetboard.domain.risk import (
    InvalidRiskPolicy,
    RiskBalance,
    RiskPolicy,
    RiskPolicyMode,
    RiskTargetsUnavailable,
    default_policy,
    risk_balance,
    risk_targets,
    validate_policy,
)
from assetboard.i18n.numbers import parse_amount
from assetboard.services.context import Context, Role, require_role, require_role_now
from assetboard.services.investments import suggested_risk_tier_changes

if TYPE_CHECKING:
    from assetboard.services.portfolio import CurrentPortfolioData


@dataclass(frozen=True, slots=True)
class PortfolioRisk:
    """The policy and computed balance shown on the portfolio page."""

    policy: RiskPolicy
    saved: bool
    age: int | None
    birth_year: int | None
    life_expectancy: int
    balance: RiskBalance
    can_apply_risk_tiers: bool = False


def load_policy(session: Session, ctx: Context) -> tuple[RiskPolicy, bool]:
    row = RiskTargetPolicyRepository(session, ctx).for_workspace()
    if row is None:
        return default_policy(), False
    return (
        RiskPolicy(
            mode=RiskPolicyMode(row.mode),
            safe_bp=row.safe_bp,
            growth_bp=row.growth_bp,
            speculative_bp=row.speculative_bp,
            tolerance_bp=row.tolerance_bp,
        ),
        True,
    )


def _profile(session: Session, ctx: Context) -> tuple[int | None, int]:
    user = session.get(User, ctx.user.id)
    if user is None:
        return None, 90
    return user.birth_year, user.life_expectancy


def _age(today: date, birth_year: int | None) -> int | None:
    if birth_year is None:
        return None
    age = today.year - birth_year
    return age if age >= 0 else None


def risk_for_portfolio(
    session: Session,
    ctx: Context,
    data: CurrentPortfolioData,
) -> PortfolioRisk:
    """Compute the current viewer's risk balance from an already-loaded portfolio."""
    policy, saved = load_policy(session, ctx)
    birth_year, life_expectancy = _profile(session, ctx)
    age = _age(ctx.clock.today(ctx.workspace.timezone), birth_year)
    targets: Mapping[RiskTier, int] | None
    try:
        targets = risk_targets(policy, age, life_expectancy)
    except RiskTargetsUnavailable:
        targets = None

    class_values: dict[tuple[UUID, UUID | None], Money] = {}
    for row in data.valuation.rows:
        if row.value is None:
            continue
        for part in row.parts:
            class_values[(row.instrument_id, part.asset_class_id)] = Money(
                part.value_minor, data.currency
            )
    class_tiers = {
        row.id: RiskTier(row.risk_tier) if row.risk_tier else None for row in data.classes
    }
    overrides = {
        row.id: RiskTier(row.risk_tier_override) if row.risk_tier_override else None
        for row in data.instruments
    }
    balance = risk_balance(
        class_values,
        class_tiers,
        overrides,
        targets,
        policy.tolerance_bp,
        currency=data.currency,
    )
    return PortfolioRisk(
        policy=policy,
        saved=saved,
        age=age,
        birth_year=birth_year,
        life_expectancy=life_expectancy,
        balance=balance,
        can_apply_risk_tiers=bool(suggested_risk_tier_changes(session, ctx)),
    )


def _percent(value: str, locale: str, field: str) -> int:
    text = value.strip()
    if not text:
        raise InvalidRiskPolicy(field, "invalid_percent")
    try:
        amount = parse_amount(text, locale)
    except ValueError:
        raise InvalidRiskPolicy(field, "invalid_percent") from None
    try:
        return percent_to_bp(amount)
    except InvalidAllocation:
        raise InvalidRiskPolicy(field, "invalid_percent") from None


def _upsert_policy(
    repository: RiskTargetPolicyRepository,
    row: RiskTargetPolicy | None,
    policy: RiskPolicy,
) -> RiskTargetPolicy:
    if row is None:
        return repository.create(
            mode=policy.mode.value,
            safe_bp=policy.safe_bp,
            growth_bp=policy.growth_bp,
            speculative_bp=policy.speculative_bp,
            tolerance_bp=policy.tolerance_bp,
        )
    return repository.update(
        row.id,
        mode=policy.mode.value,
        safe_bp=policy.safe_bp,
        growth_bp=policy.growth_bp,
        speculative_bp=policy.speculative_bp,
        tolerance_bp=policy.tolerance_bp,
    )


def save_risk_policy(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    mode: str,
    safe: str,
    growth: str,
    speculative: str,
    tolerance: str,
    locale: str,
) -> RiskPolicy:
    """Validate and save this workspace's singleton target policy."""
    require_role(ctx, Role.EDITOR)
    try:
        wanted_mode = RiskPolicyMode(mode)
    except ValueError:
        raise InvalidRiskPolicy("mode", "invalid_mode") from None
    policy = validate_policy(
        RiskPolicy(
            mode=wanted_mode,
            safe_bp=_percent(safe, locale, "safe"),
            growth_bp=_percent(growth, locale, "growth"),
            speculative_bp=_percent(speculative, locale, "speculative"),
            tolerance_bp=_percent(tolerance, locale, "tolerance"),
        )
    )

    def write(session: Session) -> RiskPolicy:
        return _save_risk_policy_in_session(session, ctx, policy)

    return run_immediate(factory, write)


def _save_risk_policy_in_session(session: Session, ctx: Context, policy: RiskPolicy) -> RiskPolicy:
    """``save_risk_policy`` inside the caller's write transaction.

    The public path parsed and validated the percentages and mode; role, lock, profile check,
    upsert and audit are this function's.
    """
    require_role_now(session, ctx, Role.EDITOR)
    CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
    if policy.mode is not RiskPolicyMode.FIXED:
        user = session.get(User, ctx.user.id)
        if user is None or user.birth_year is None:
            raise InvalidRiskPolicy("mode", "birth_year_required")
    repository = RiskTargetPolicyRepository(session, ctx)
    row = _upsert_policy(
        repository,
        repository.for_workspace(for_update=True),
        policy,
    )
    AuditEventRepository(session, ctx).record(
        actor_user_id=ctx.user.id,
        action="risk_policy.saved",
        entity_type="risk_target_policy",
        entity_id=str(row.id),
        ts=ctx.clock.now(),
        data={
            "mode": policy.mode.value,
            "safe_bp": policy.safe_bp,
            "growth_bp": policy.growth_bp,
            "speculative_bp": policy.speculative_bp,
            "tolerance_bp": policy.tolerance_bp,
        },
    )
    # Local import: portfolio_links reads the portfolio, which imports this module.
    from assetboard.services.portfolio_links import ensure_networth_items_for_asset_classes

    ensure_networth_items_for_asset_classes(session, ctx)
    return policy
