"""User-defined taxes on income (kinds, schedules, immutable rate versions), the Taxes switch
and the example panel (F3.3, redesigned by ADR-0022)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.engine import run_immediate
from assetboard.db.models.tax import TaxRule, TaxRuleVersion
from assetboard.db.repositories import (
    AuditEventRepository,
    CategoryRepository,
    CurrentWorkspaceRepository,
    IncomeSourceRepository,
    IncomeSourceTaxRuleRepository,
    TaxRuleRepository,
    TaxRuleVersionRepository,
)
from assetboard.domain.money import Money, minor_unit_for
from assetboard.domain.months import MAX_YEAR, MIN_YEAR
from assetboard.domain.percent_or_fixed import PercentOrFixed
from assetboard.domain.taxes import (
    PaymentTiming,
    TaxBase,
    TaxBreakdown,
    TaxInput,
    TaxKind,
    calculate_taxes,
    tax_payment_date,
)
from assetboard.domain.transactions import MAX_AMOUNT_MINOR, clean_description
from assetboard.services.context import Context, Role, require_role, require_role_now
from assetboard.services.income import resync_sources_with_rule

# The first rate version of a tax is valid "from the start" (the app's earliest date).
FIRST_VERSION_DATE = date(MIN_YEAR, 1, 1)
# The example panel's income: 10 000.00 in the workspace currency's major unit.
EXAMPLE_NET_MAJOR = 10_000


class TaxRuleError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TaxesInUse(Exception):
    """Taxes cannot be turned off: these non-archived income sources have taxes attached."""

    def __init__(self, sources: tuple[str, ...]) -> None:
        super().__init__("taxes_in_use")
        self.sources = sources


@dataclass(frozen=True, slots=True)
class TaxRuleVersionView:
    id: UUID
    effective_from: date
    value: PercentOrFixed


@dataclass(frozen=True, slots=True)
class TaxRuleView:
    id: UUID
    name: str
    kind: TaxKind
    base: TaxBase
    notes: str
    archived: bool
    versions: tuple[TaxRuleVersionView, ...]
    payment_day: int
    payment_timing: PaymentTiming
    payment_category_id: UUID | None
    # Names of the non-archived income sources the tax is attached to.
    used_by: tuple[str, ...] = ()
    # Kind and base are locked once posted income was calculated with the tax.
    locked: bool = False

    @property
    def current(self) -> PercentOrFixed | None:
        return self.versions[-1].value if self.versions else None


@dataclass(frozen=True, slots=True)
class TaxExample:
    paid_on: date
    payment_date: date
    breakdown: TaxBreakdown


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


def _view(
    session: Session,
    ctx: Context,
    rule: TaxRule,
    versions: list[TaxRuleVersion],
    source_names: dict[UUID, str],
) -> TaxRuleView:
    attachments = IncomeSourceTaxRuleRepository(session, ctx).for_rule(rule.id)
    return TaxRuleView(
        id=rule.id,
        name=rule.name,
        kind=TaxKind(rule.kind),
        base=TaxBase(rule.base),
        notes=rule.notes,
        archived=rule.archived_at is not None,
        versions=tuple(
            TaxRuleVersionView(
                id=version.id,
                effective_from=version.effective_from,
                value=_value_from_row(version, ctx.workspace.base_currency),
            )
            for version in versions
        ),
        payment_day=rule.payment_day,
        payment_timing=PaymentTiming(rule.payment_timing),
        payment_category_id=rule.payment_category_id,
        used_by=tuple(
            sorted(
                source_names[row.income_source_id]
                for row in attachments
                if row.income_source_id in source_names
            )
        ),
        locked=TaxRuleRepository(session, ctx).has_posted_income(rule.id),
    )


def _active_source_names(session: Session, ctx: Context) -> dict[UUID, str]:
    return {
        source.id: source.name
        for source in IncomeSourceRepository(session, ctx).ordered(active_only=True)
    }


def list_rules(factory: sessionmaker[Session], ctx: Context) -> list[TaxRuleView]:
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        rules = TaxRuleRepository(session, ctx).ordered()
        versions = TaxRuleVersionRepository(session, ctx).all_for_rules()
        names = _active_source_names(session, ctx)
        by_rule: dict[UUID, list[TaxRuleVersion]] = {}
        for version in versions:
            by_rule.setdefault(version.tax_rule_id, []).append(version)
        return [_view(session, ctx, rule, by_rule.get(rule.id, []), names) for rule in rules]


def get_rule(factory: sessionmaker[Session], ctx: Context, rule_id: UUID) -> TaxRuleView:
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        rule = TaxRuleRepository(session, ctx).get_or_raise(rule_id)
        versions = TaxRuleVersionRepository(session, ctx).for_rule(rule_id)
        return _view(session, ctx, rule, versions, _active_source_names(session, ctx))


def example(
    ctx: Context,
    *,
    name: str,
    kind: TaxKind,
    base: TaxBase,
    value: PercentOrFixed,
    payment_day: int,
    payment_timing: PaymentTiming,
) -> TaxExample:
    """The example panel: income of 10 000.00 net paid on the 10th of next month."""
    require_role(ctx, Role.VIEWER)
    currency = ctx.workspace.base_currency
    net = Money(EXAMPLE_NET_MAJOR * 10 ** minor_unit_for(currency), currency)
    today = ctx.clock.today(ctx.workspace.timezone)
    year, month = divmod(today.year * 12 + today.month, 12)
    paid_on = date(min(year, MAX_YEAR), month + 1, 10)
    if value.mode == "fixed" and (value.fixed is None or value.fixed.currency != currency):
        raise TaxRuleError("invalid_value")
    if kind == TaxKind.ADDED:
        base = TaxBase.NET
    breakdown = calculate_taxes(net, [TaxInput(name, kind, base, value)])
    return TaxExample(
        paid_on=paid_on,
        payment_date=tax_payment_date(paid_on, payment_day, payment_timing),
        breakdown=breakdown,
    )


@dataclass(frozen=True, slots=True)
class _RuleFields:
    name: str
    notes: str
    kind: TaxKind
    base: TaxBase
    payment_day: int
    payment_timing: PaymentTiming
    payment_category_id: UUID | None


def _validated_rule(
    *,
    name: str,
    notes: str,
    kind: str,
    base: str,
    payment_day: int,
    payment_timing: str,
    payment_category_id: UUID | None,
) -> _RuleFields:
    clean_name = clean_description(name)
    if not clean_name:
        raise TaxRuleError("invalid_name")
    try:
        tax_kind = TaxKind(kind)
        tax_base = TaxBase(base)
    except ValueError:
        raise TaxRuleError("invalid_rule") from None
    if tax_kind == TaxKind.ADDED:
        tax_base = TaxBase.NET  # the UI has no base for added taxes
    try:
        timing = PaymentTiming(payment_timing)
    except ValueError:
        raise TaxRuleError("invalid_schedule") from None
    if not 1 <= payment_day <= 31:
        raise TaxRuleError("invalid_schedule")
    if not tax_kind.plans_payment:
        payment_category_id = None  # nothing is planned: no category
    elif payment_category_id is None:
        raise TaxRuleError("invalid_category")
    return _RuleFields(
        name=clean_name,
        notes=clean_description(notes),
        kind=tax_kind,
        base=tax_base,
        payment_day=payment_day,
        payment_timing=timing,
        payment_category_id=payment_category_id,
    )


def _check_category(session: Session, ctx: Context, category_id: UUID | None) -> None:
    if category_id is None:
        return
    category = CategoryRepository(session, ctx).get(category_id)
    if (
        category is None
        or category.archived_at is not None
        or category.kind != "expense"
        or category.treatment == "transfer"
    ):
        raise TaxRuleError("invalid_category")


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
    kind: str,
    base: str = TaxBase.NET.value,
    notes: str = "",
    value: PercentOrFixed,
    payment_day: int = 10,
    payment_timing: str = PaymentTiming.NEXT_MONTH.value,
    payment_category_id: UUID | None = None,
    effective_from: date = FIRST_VERSION_DATE,
) -> UUID:
    require_role(ctx, Role.EDITOR)
    _validate_day(effective_from)
    fields = _validated_rule(
        name=name,
        notes=notes,
        kind=kind,
        base=base,
        payment_day=payment_day,
        payment_timing=payment_timing,
        payment_category_id=payment_category_id,
    )

    def write(session: Session) -> UUID:
        require_role_now(session, ctx, Role.EDITOR)
        workspace = CurrentWorkspaceRepository(session, ctx)
        workspace.lock(exclusive=True)
        version_values = _version_fields(value, workspace.get().base_currency)
        _check_category(session, ctx, fields.payment_category_id)
        rule = TaxRuleRepository(session, ctx).create(
            name=fields.name,
            kind=fields.kind.value,
            base=fields.base.value,
            notes=fields.notes,
            payment_day=fields.payment_day,
            payment_timing=fields.payment_timing.value,
            payment_category_id=fields.payment_category_id,
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
            data={"name": fields.name, "kind": fields.kind.value, "base": fields.base.value},
        )
        return rule.id

    return run_immediate(factory, write)


def update_rule(
    factory: sessionmaker[Session],
    ctx: Context,
    rule_id: UUID,
    *,
    name: str,
    kind: str,
    base: str = TaxBase.NET.value,
    notes: str = "",
    payment_day: int = 10,
    payment_timing: str = PaymentTiming.NEXT_MONTH.value,
    payment_category_id: UUID | None = None,
) -> None:
    """Change a tax's name, kind, base and payment schedule; pending income follows.

    Kind and base cannot change once posted income was calculated with the tax (ADR-0022).
    """
    require_role(ctx, Role.EDITOR)
    fields = _validated_rule(
        name=name,
        notes=notes,
        kind=kind,
        base=base,
        payment_day=payment_day,
        payment_timing=payment_timing,
        payment_category_id=payment_category_id,
    )

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        repository = TaxRuleRepository(session, ctx)
        rule = repository.get_or_raise(rule_id)
        if rule.archived_at is not None:
            raise TaxRuleError("archived")
        meaning_changed = rule.kind != fields.kind.value or rule.base != fields.base.value
        if meaning_changed and repository.has_posted_income(rule_id):
            raise TaxRuleError("kind_locked")
        if fields.payment_category_id != rule.payment_category_id:
            _check_category(session, ctx, fields.payment_category_id)
        if fields.kind.plans_payment and fields.payment_category_id is None:
            raise TaxRuleError("invalid_category")
        repository.update(
            rule_id,
            name=fields.name,
            notes=fields.notes,
            kind=fields.kind.value,
            base=fields.base.value,
            payment_day=fields.payment_day,
            payment_timing=fields.payment_timing.value,
            payment_category_id=fields.payment_category_id,
        )
        resync_sources_with_rule(session, ctx, rule_id)
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="tax_rule.updated",
            entity_type="tax_rule",
            entity_id=str(rule_id),
            ts=ctx.clock.now(),
            data={"kind": fields.kind.value, "base": fields.base.value},
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
        resync_sources_with_rule(session, ctx, rule_id)
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


def _sources_with_taxes(session: Session, ctx: Context) -> tuple[str, ...]:
    names = _active_source_names(session, ctx)
    attached = {row.income_source_id for row in IncomeSourceTaxRuleRepository(session, ctx).list()}
    return tuple(sorted(names[source_id] for source_id in attached if source_id in names))


def sources_with_taxes(factory: sessionmaker[Session], ctx: Context) -> tuple[str, ...]:
    """Non-archived income sources with a tax attached (they keep the Taxes switch on)."""
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        return _sources_with_taxes(session, ctx)


def set_taxes_enabled(factory: sessionmaker[Session], ctx: Context, enabled: bool) -> None:
    """The Workspace settings "Menu" switch; owners only, as the other workspace settings.

    Turning it off is refused while a non-archived income source has a tax attached.
    """
    require_role(ctx, Role.OWNER)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.OWNER)
        repository = CurrentWorkspaceRepository(session, ctx)
        repository.lock(exclusive=True)
        if repository.get().taxes_enabled == enabled:
            return
        if not enabled:
            in_use = _sources_with_taxes(session, ctx)
            if in_use:
                raise TaxesInUse(in_use)
        repository.update(taxes_enabled=enabled)
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="workspace.settings_updated",
            entity_type="workspace",
            entity_id=str(ctx.workspace.id),
            ts=ctx.clock.now(),
            data={"taxes_enabled": enabled},
        )

    run_immediate(factory, write)


@dataclass(frozen=True, slots=True)
class IncomeTaxExample:
    """The example on the last step of "Add income": the ticked taxes on a sample amount.
    ``names`` and ``payment_dates`` are keyed by the breakdown line names (rule ids)."""

    breakdown: TaxBreakdown
    names: dict[str, str]
    payment_dates: dict[str, date]


def income_example(
    factory: sessionmaker[Session],
    ctx: Context,
    rule_ids: list[UUID],
    *,
    net: Money,
    paid_on: date,
) -> IncomeTaxExample:
    """``net`` paid on ``paid_on`` with the ticked taxes, at the rates valid on that day."""
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        effective = {
            rule.id: (rule, version)
            for rule, version in TaxRuleRepository(session, ctx).effective_for_all(paid_on)
        }
    inputs: list[TaxInput] = []
    names: dict[str, str] = {}
    dates: dict[str, date] = {}
    for rule_id in rule_ids:
        pair = effective.get(rule_id)
        if pair is None or pair[0].archived_at is not None:
            continue
        rule, version = pair
        key = str(rule.id)
        inputs.append(
            TaxInput(
                key, TaxKind(rule.kind), TaxBase(rule.base), _value_from_row(version, net.currency)
            )
        )
        names[key] = rule.name
        dates[key] = tax_payment_date(paid_on, rule.payment_day, PaymentTiming(rule.payment_timing))
    return IncomeTaxExample(calculate_taxes(net, inputs), names, dates)
