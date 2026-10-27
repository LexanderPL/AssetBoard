"""User-defined tax rules, immutable versions, and calculator previews (F3.3)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.engine import run_immediate
from assetboard.db.models.identity import User
from assetboard.db.models.tax import TaxRule, TaxRuleVersion
from assetboard.db.repositories import (
    AuditEventRepository,
    CurrentWorkspaceRepository,
    TaxRuleRepository,
    TaxRuleVersionRepository,
)
from assetboard.domain.money import Money
from assetboard.domain.months import MAX_YEAR, MIN_YEAR
from assetboard.domain.percent_or_fixed import PercentOrFixed
from assetboard.domain.taxes import (
    RecordAs,
    TaxBase,
    TaxBreakdown,
    TaxEffect,
    TaxInput,
    calculate_taxes,
)
from assetboard.domain.transactions import MAX_AMOUNT_MINOR, clean_description
from assetboard.services.context import Context, Role, require_role, require_role_now


def rate_warning_dismissed(factory: sessionmaker[Session], ctx: Context) -> bool:
    """Whether this account dismissed the user-entered tax rates warning."""
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        user = session.get(User, ctx.user.id)
        return bool(user and user.tax_warning_dismissed)


def dismiss_rate_warning(factory: sessionmaker[Session], ctx: Context) -> None:
    """Persist dismissal on the account, across its workspaces."""
    require_role(ctx, Role.VIEWER)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.VIEWER)
        user = session.get(User, ctx.user.id)
        if user is not None:
            user.tax_warning_dismissed = True

    run_immediate(factory, write)


class TaxRuleError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class TaxRuleVersionView:
    id: UUID
    effective_from: date
    value: PercentOrFixed


@dataclass(frozen=True, slots=True)
class TaxRuleView:
    id: UUID
    name: str
    effect: TaxEffect
    base: TaxBase
    notes: str
    archived: bool
    versions: tuple[TaxRuleVersionView, ...]


@dataclass(frozen=True, slots=True)
class TaxPreview:
    day: date
    calculation: TaxBreakdown
    missing_rules: tuple[str, ...]


def _validate_day(day: date) -> None:
    if not MIN_YEAR <= day.year <= MAX_YEAR:
        raise TaxRuleError("invalid_date")


def _value_from_row(row: TaxRuleVersion, currency: str) -> PercentOrFixed:
    if row.mode == "percent":
        if row.rate_bp is None:
            raise RuntimeError("percent tax version has no rate")
        return PercentOrFixed.percent(row.rate_bp)
    if row.fixed_minor is None:
        raise RuntimeError("fixed tax version has no amount")
    return PercentOrFixed.fixed_amount(Money(row.fixed_minor, currency))


def _view(rule: TaxRule, versions: list[TaxRuleVersion], currency: str) -> TaxRuleView:
    return TaxRuleView(
        id=rule.id,
        name=rule.name,
        effect=TaxEffect(rule.effect),
        base=TaxBase(rule.base),
        notes=rule.notes,
        archived=rule.archived_at is not None,
        versions=tuple(
            TaxRuleVersionView(
                id=version.id,
                effective_from=version.effective_from,
                value=_value_from_row(version, currency),
            )
            for version in versions
        ),
    )


def list_rules(factory: sessionmaker[Session], ctx: Context) -> list[TaxRuleView]:
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        rules = TaxRuleRepository(session, ctx).ordered()
        versions = TaxRuleVersionRepository(session, ctx).all_for_rules()
        by_rule: dict[UUID, list[TaxRuleVersion]] = {}
        for version in versions:
            by_rule.setdefault(version.tax_rule_id, []).append(version)
        return [
            _view(rule, by_rule.get(rule.id, []), ctx.workspace.base_currency) for rule in rules
        ]


def preview(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    net: Money,
    day: date,
    record_as: RecordAs,
) -> TaxPreview:
    require_role(ctx, Role.VIEWER)
    _validate_day(day)
    if net.currency != ctx.workspace.base_currency or not 0 <= net.amount_minor <= MAX_AMOUNT_MINOR:
        raise TaxRuleError("invalid_amount")
    with factory() as session:
        rules_repository = TaxRuleRepository(session, ctx)
        rules = rules_repository.ordered(include_archived=False)
        effective = rules_repository.effective_for_all(day)
    selected = {rule.id: version for rule, version in effective}
    inputs: list[TaxInput] = []
    missing: list[str] = []
    for rule in rules:
        version = selected.get(rule.id)
        if version is None:
            missing.append(rule.name)
            continue
        inputs.append(
            TaxInput(
                name=rule.name,
                effect=TaxEffect(rule.effect),
                base=TaxBase(rule.base),
                value=_value_from_row(version, net.currency),
                generate_payment=True,
            )
        )
    return TaxPreview(
        day=day,
        calculation=calculate_taxes(net, inputs, record_as),
        missing_rules=tuple(missing),
    )


def _validated_rule(
    name: str, effect: str, base: str, notes: str
) -> tuple[str, TaxEffect, TaxBase, str]:
    clean_name = clean_description(name)
    clean_notes = clean_description(notes)
    if not clean_name:
        raise TaxRuleError("invalid_name")
    try:
        tax_effect = TaxEffect(effect)
        tax_base = TaxBase(base)
    except ValueError:
        raise TaxRuleError("invalid_rule") from None
    if tax_effect == TaxEffect.ADDED and tax_base != TaxBase.NET:
        raise TaxRuleError("invalid_rule")
    return clean_name, tax_effect, tax_base, clean_notes


def _version_fields(value: PercentOrFixed, currency: str) -> dict[str, object]:
    if value.mode == "percent":
        assert value.rate_bp is not None
        return {"mode": "percent", "rate_bp": value.rate_bp, "fixed_minor": None}
    assert value.fixed is not None
    if value.fixed.currency != currency or value.fixed.amount_minor > MAX_AMOUNT_MINOR:
        raise TaxRuleError("invalid_value")
    return {"mode": "fixed", "rate_bp": None, "fixed_minor": value.fixed.amount_minor}


def create_rule(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    name: str,
    effect: str,
    base: str,
    notes: str,
    effective_from: date,
    value: PercentOrFixed,
) -> UUID:
    require_role(ctx, Role.EDITOR)
    _validate_day(effective_from)
    clean_name, tax_effect, tax_base, clean_notes = _validated_rule(name, effect, base, notes)

    def write(session: Session) -> UUID:
        require_role_now(session, ctx, Role.EDITOR)
        workspace = CurrentWorkspaceRepository(session, ctx)
        workspace.lock(exclusive=True)
        version_values = _version_fields(value, workspace.get().base_currency)
        rules = TaxRuleRepository(session, ctx)
        rule = rules.create(
            name=clean_name,
            effect=tax_effect.value,
            base=tax_base.value,
            notes=clean_notes,
        )
        TaxRuleVersionRepository(session, ctx).create(
            tax_rule_id=rule.id,
            effective_from=effective_from,
            **version_values,
        )
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="tax_rule.created",
            entity_type="tax_rule",
            entity_id=str(rule.id),
            ts=ctx.clock.now(),
            data={
                "name": clean_name,
                "effect": tax_effect.value,
                "base": tax_base.value,
            },
        )
        return rule.id

    return run_immediate(factory, write)


def update_rule(
    factory: sessionmaker[Session],
    ctx: Context,
    rule_id: UUID,
    *,
    name: str,
    notes: str,
) -> None:
    require_role(ctx, Role.EDITOR)
    clean_name = clean_description(name)
    clean_notes = clean_description(notes)
    if not clean_name:
        raise TaxRuleError("invalid_name")

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        repository = TaxRuleRepository(session, ctx)
        rule = repository.get_or_raise(rule_id)
        if rule.archived_at is not None:
            raise TaxRuleError("archived")
        repository.update(rule_id, name=clean_name, notes=clean_notes)
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="tax_rule.updated",
            entity_type="tax_rule",
            entity_id=str(rule_id),
            ts=ctx.clock.now(),
        )

    run_immediate(factory, write)


def add_version(
    factory: sessionmaker[Session],
    ctx: Context,
    rule_id: UUID,
    *,
    effective_from: date,
    value: PercentOrFixed,
) -> UUID:
    require_role(ctx, Role.EDITOR)
    _validate_day(effective_from)

    def write(session: Session) -> UUID:
        require_role_now(session, ctx, Role.EDITOR)
        workspace = CurrentWorkspaceRepository(session, ctx)
        workspace.lock(exclusive=True)
        version_values = _version_fields(value, workspace.get().base_currency)
        rule = TaxRuleRepository(session, ctx).get_or_raise(rule_id)
        if rule.archived_at is not None:
            raise TaxRuleError("archived")
        versions = TaxRuleVersionRepository(session, ctx)
        current = versions.for_rule(rule_id)
        if current and effective_from <= current[-1].effective_from:
            raise TaxRuleError("version_not_newer")
        version = versions.create(
            tax_rule_id=rule_id,
            effective_from=effective_from,
            **version_values,
        )
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="tax_rule.version_added",
            entity_type="tax_rule",
            entity_id=str(rule_id),
            ts=ctx.clock.now(),
            data={"effective_from": effective_from.isoformat(), "mode": str(value.mode)},
        )
        return version.id

    return run_immediate(factory, write)


def archive_rule(factory: sessionmaker[Session], ctx: Context, rule_id: UUID) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        repository = TaxRuleRepository(session, ctx)
        rule = repository.get_or_raise(rule_id)
        if rule.archived_at is None:
            now: datetime = ctx.clock.now()
            repository.update(rule_id, archived_at=now)
            AuditEventRepository(session, ctx).record(
                actor_user_id=ctx.user.id,
                action="tax_rule.archived",
                entity_type="tax_rule",
                entity_id=str(rule_id),
                ts=now,
            )

    run_immediate(factory, write)


def workspace_has_fixed_tax_amounts(session: Session, ctx: Context) -> bool:
    return TaxRuleVersionRepository(session, ctx).any_fixed_amount()
