"""Income sources, month-level work logs, and tax-payment occurrence reconciliation."""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

import holidays as holiday_calendars
from sqlalchemy.orm import Session, sessionmaker

from assetboard.clock import Clock
from assetboard.config import Settings
from assetboard.db.engine import run_immediate
from assetboard.db.models.income import (
    MAX_WORK_DAY_UNITS,
    IncomeSource,
    IncomeSourceTaxRule,
    WorkDay,
    WorkLog,
)
from assetboard.db.models.scheduled import ScheduledOccurrence
from assetboard.db.repositories import (
    AuditEventRepository,
    CategoryRepository,
    CurrentWorkspaceRepository,
    IncomeSourceRepository,
    IncomeSourceTaxRuleRepository,
    MonthStateRepository,
    ScheduledOccurrenceRepository,
    TaxRuleRepository,
    WorkDayRepository,
    WorkLogRepository,
)
from assetboard.db.repositories.base import WorkspaceScope
from assetboard.domain.income import (
    ExpectedUnitsMode,
    IncomeEstimate,
    IncomeKind,
    estimate_income,
    expected_units,
    pay_date,
)
from assetboard.domain.money import Money
from assetboard.domain.months import MAX_YEAR, MIN_YEAR, YearMonth
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
from assetboard.i18n import gettext
from assetboard.services.category_usage import CategoryUsage
from assetboard.services.context import Context, Role, require_role, require_role_now


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when used."""
    return message


# Generated tax payments are named in the workspace language, as the seeded categories are.
TAX_PAYMENT_LABEL = _("%(name)s tax payment")


class IncomeError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class IncomePreview:
    work_month: date
    due_date: date
    amount_state: str
    units: Decimal | None
    breakdown: TaxBreakdown
    tax_names: dict[str, str]


@dataclass(frozen=True, slots=True)
class IncomeSourceView:
    id: UUID
    name: str
    kind: IncomeKind
    category_id: UUID
    currency: str
    fixed_minor: int | None
    rate_minor: int | None
    unit_label: str
    pay_day: int
    pay_lag_months: int
    expected_mode: ExpectedUnitsMode
    expected_units: Decimal | None
    average_months: int
    record_as: RecordAs
    active_from: date
    active_to: date | None
    archived: bool
    attachments: tuple[IncomeSourceTaxRule, ...]
    work_logs: tuple[WorkLog, ...]
    preview: tuple[IncomePreview, ...]


@dataclass(frozen=True, slots=True)
class _IncomeSourceValues:
    name: str
    kind: str
    category_id: UUID
    fixed_minor: int | None
    rate_minor: int | None
    unit_label: str
    pay_day: int
    pay_lag_months: int
    expected_mode: str
    expected_units: Decimal | None
    average_months: int
    record_as: str
    active_from: date
    active_to: date | None

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "category_id": self.category_id,
            "fixed_minor": self.fixed_minor,
            "rate_minor": self.rate_minor,
            "unit_label": self.unit_label,
            "pay_day": self.pay_day,
            "pay_lag_months": self.pay_lag_months,
            "expected_mode": self.expected_mode,
            "expected_units": self.expected_units,
            "average_months": self.average_months,
            "record_as": self.record_as,
            "active_from": self.active_from,
            "active_to": self.active_to,
        }


def _month_start(day: date) -> date:
    return date(day.year, day.month, 1)


def _valid_month(month: date) -> None:
    if month.day != 1 or not MIN_YEAR <= month.year <= MAX_YEAR:
        raise IncomeError("invalid_work_month")


def _month_shift(month: date, offset: int) -> date:
    index = month.year * 12 + month.month - 1 + offset
    year, month_index = divmod(index, 12)
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise IncomeError("invalid_date")
    return date(year, month_index + 1, 1)


def _month_range(first: date, last: date) -> list[date]:
    result: list[date] = []
    current = first
    while current <= last:
        result.append(current)
        current = _month_shift(current, 1)
    return result


def _holidays(country: str | None, year: int) -> set[date]:
    if country is None:
        return set()
    try:
        return set(holiday_calendars.country_holidays(country, years=[year]))
    except NotImplementedError:
        raise IncomeError("unsupported_holidays") from None


def _working_days(country: str | None, month: date) -> int:
    holidays = _holidays(country, month.year)
    return sum(
        1
        for day_number in range(1, monthrange(month.year, month.month)[1] + 1)
        if (day := date(month.year, month.month, day_number)).weekday() < 5 and day not in holidays
    )


def _check_working_days(fields: _IncomeSourceValues, country: str | None) -> None:
    """Refuse working-day estimates the workspace country cannot compute (generation would fail)."""
    if (
        fields.kind == IncomeKind.VARIABLE.value
        and fields.expected_mode == ExpectedUnitsMode.WORKING_DAYS.value
    ):
        _holidays(country, fields.active_from.year)


def _average(logs: list[WorkLog], months: int) -> Decimal | None:
    selected = [row.units for row in logs[-months:]]
    if not selected:
        return None
    return sum(selected, Decimal(0)) / Decimal(len(selected))


def _source_estimate(
    source: IncomeSource, month: date, logs: list[WorkLog], country: str | None
) -> IncomeEstimate:
    matching = next((row for row in logs if row.work_month == month), None)
    if source.kind == IncomeKind.FIXED.value:
        return estimate_income(
            kind=IncomeKind.FIXED,
            currency=source.currency,
            fixed_minor=source.fixed_minor,
            rate_minor=None,
            units=None,
            confirmed=True,
        )
    units = (
        matching.units
        if matching is not None
        else expected_units(
            ExpectedUnitsMode(source.expected_mode),
            work_month=month,
            fixed=source.expected_units if isinstance(source.expected_units, Decimal) else None,
            average=_average(
                [row for row in logs if row.work_month < month], source.average_months
            ),
            working_days=_working_days(country, month),
        )
    )
    try:
        return estimate_income(
            kind=IncomeKind.VARIABLE,
            currency=source.currency,
            fixed_minor=None,
            rate_minor=source.rate_minor,
            units=units,
            confirmed=matching is not None,
        )
    except ValueError as exc:
        raise IncomeError(str(exc)) from None


def _calculate_breakdown(net: Money, taxes: list[TaxInput], record_as: RecordAs) -> TaxBreakdown:
    breakdown = calculate_taxes(net, taxes, record_as)
    amounts = (
        breakdown.gross.amount_minor,
        breakdown.take_home.amount_minor,
        breakdown.ledger_amount.amount_minor,
        *(line.amount.amount_minor for line in (*breakdown.added, *breakdown.withheld)),
    )
    if any(amount > MAX_AMOUNT_MINOR for amount in amounts):
        raise IncomeError("amount_too_large")
    if breakdown.ledger_amount.amount_minor < 0:
        raise IncomeError("negative_income_after_tax")
    return breakdown


def _tax_lines(
    session: Session,
    scope: WorkspaceScope,
    source: IncomeSource,
    day: date,
) -> tuple[list[tuple[IncomeSourceTaxRule, TaxInput, str]], set[UUID]]:
    attachments = IncomeSourceTaxRuleRepository(session, scope).for_source(source.id)
    effective = TaxRuleRepository(session, scope).effective_for_all(day)
    versions = {rule.id: (rule, version) for rule, version in effective}
    selected: list[tuple[IncomeSourceTaxRule, TaxInput, str]] = []
    missing: set[UUID] = set()
    for attachment in attachments:
        pair = versions.get(attachment.tax_rule_id)
        if pair is None:
            missing.add(attachment.id)
            continue
        rule, version = pair
        if version.mode == "percent":
            if version.rate_bp is None:
                raise RuntimeError("percent tax version has no rate")
            value = PercentOrFixed.percent(version.rate_bp)
        else:
            if version.fixed_minor is None:
                raise RuntimeError("fixed tax version has no amount")
            value = PercentOrFixed.fixed_amount(Money(version.fixed_minor, source.currency))
        selected.append(
            (
                attachment,
                TaxInput(
                    name=str(attachment.id),
                    effect=TaxEffect(rule.effect),
                    base=TaxBase(rule.base),
                    value=value,
                    generate_payment=attachment.generate_payment,
                ),
                rule.name,
            )
        )
    return selected, missing


def _sync_children(
    session: Session,
    scope: WorkspaceScope,
    parent: ScheduledOccurrence,
    source: IncomeSource,
    *,
    net: Money,
    due: date,
) -> None:
    selected, _missing = _tax_lines(session, scope, source, due)
    locale = CurrentWorkspaceRepository(session, scope).get().locale
    label = gettext(TAX_PAYMENT_LABEL, locale) % {"name": source.name}
    breakdown = _calculate_breakdown(
        net, [tax for _, tax, _ in selected], RecordAs(source.record_as)
    )
    amounts: dict[str, int] = {}
    for line in (*breakdown.added, *breakdown.withheld):
        if line.amount.amount_minor > MAX_AMOUNT_MINOR:
            raise IncomeError("amount_too_large")
        amounts[line.name] = line.amount.amount_minor
    payable = {line.name for line in breakdown.payments}
    wanted: dict[UUID, tuple[IncomeSourceTaxRule, int, date]] = {}
    for attachment, tax, _tax_name in selected:
        if tax.name not in payable:
            continue
        amount = amounts[tax.name]
        if amount <= 0:
            continue
        payment_day = pay_date(
            _month_shift(_month_start(due), attachment.due_month_offset), attachment.due_day, 0
        )
        wanted[attachment.id] = (attachment, amount, payment_day)

    repository = ScheduledOccurrenceRepository(session, scope)
    closed_months = MonthStateRepository(session, scope).closed_months()
    existing = {
        row.income_source_tax_rule_id: row
        for row in repository.children(parent.id)
        if row.income_source_tax_rule_id is not None
    }
    for attachment_id, child in existing.items():
        target = wanted.pop(attachment_id, None)
        if child.status != "pending":
            continue
        if YearMonth.from_date(child.due_date) in closed_months:
            continue
        if target is not None and YearMonth.from_date(target[2]) in closed_months:
            continue
        if target is None:
            repository.delete(child)
            continue
        attachment, amount, due_date = target
        child.category_id = attachment.payment_category_id
        child.due_date = due_date
        child.nominal_date = due_date
        child.expected_minor = amount
        child.currency = source.currency
        child.amount_state = "confirmed"
        child.label = label
    for attachment_id, (attachment, amount, due_date) in wanted.items():
        if YearMonth.from_date(due_date) in closed_months:
            continue
        repository.create(
            scheduled_item_id=None,
            income_source_id=None,
            work_month=None,
            parent_occurrence_id=parent.id,
            income_source_tax_rule_id=attachment_id,
            direction="expense",
            category_id=attachment.payment_category_id,
            nominal_date=due_date,
            due_date=due_date,
            expected_minor=amount,
            currency=source.currency,
            amount_state="confirmed",
            status="pending",
            generated_by="tax",
            label=label,
        )


def _sync_work_month(
    session: Session,
    scope: WorkspaceScope,
    clock: Clock,
    source: IncomeSource,
    month: date,
    *,
    allow_past: bool,
) -> ScheduledOccurrence | None:
    _valid_month(month)
    workspace = CurrentWorkspaceRepository(session, scope).get()
    due = pay_date(month, source.pay_day, source.pay_lag_months)
    repository = ScheduledOccurrenceRepository(session, scope)
    occurrence = repository.for_income_work_month(source.id, month)
    closed_months = MonthStateRepository(session, scope).closed_months()
    if YearMonth.from_date(due) in closed_months or (
        occurrence is not None and YearMonth.from_date(occurrence.due_date) in closed_months
    ):
        return occurrence
    if month < source.active_from or (source.active_to is not None and month > source.active_to):
        if occurrence is not None and occurrence.status == "pending":
            children = repository.children(occurrence.id)
            if not any(child.status != "pending" for child in children):
                for child in children:
                    repository.delete(child)
                repository.delete(occurrence)
        return None
    if not allow_past and due < clock.today(workspace.timezone):
        return None
    category = CategoryRepository(session, scope).get_or_raise(source.category_id)
    if category.archived_at is not None or category.kind != "income":
        if occurrence is not None and occurrence.status == "pending":
            children = repository.children(occurrence.id)
            if not any(child.status != "pending" for child in children):
                for child in children:
                    repository.delete(child)
                repository.delete(occurrence)
        return None
    logs = WorkLogRepository(session, scope).for_source(source.id)
    estimate = _source_estimate(source, month, logs, workspace.country_code)
    tax_rules, _missing = _tax_lines(session, scope, source, due)
    breakdown = _calculate_breakdown(
        estimate.net,
        [tax for _, tax, _ in tax_rules],
        RecordAs(source.record_as),
    )
    if occurrence is None:
        occurrence = repository.create(
            scheduled_item_id=None,
            income_source_id=source.id,
            work_month=month,
            parent_occurrence_id=None,
            income_source_tax_rule_id=None,
            direction="income",
            category_id=source.category_id,
            nominal_date=due,
            due_date=due,
            expected_minor=breakdown.ledger_amount.amount_minor,
            currency=source.currency,
            amount_state=estimate.amount_state,
            status="pending",
            generated_by="income",
            label=source.name,
        )
    elif occurrence.status == "pending":
        occurrence.category_id = source.category_id
        occurrence.direction = "income"
        occurrence.currency = source.currency
        occurrence.nominal_date = due
        if occurrence.rescheduled_at is None:
            # A user-rescheduled occurrence keeps its chosen date (as F3.1 rule occurrences do).
            occurrence.due_date = due
        occurrence.label = source.name
        if not occurrence.amount_overridden:
            occurrence.expected_minor = breakdown.ledger_amount.amount_minor
            occurrence.amount_state = estimate.amount_state
    else:
        return occurrence
    _sync_children(session, scope, occurrence, source, net=estimate.net, due=due)
    return occurrence


def generate_workspace_in_session(
    session: Session,
    scope: WorkspaceScope,
    clock: Clock,
    settings: Settings,
) -> int:
    return _generate_workspace_in_session(session, scope, clock, settings.recurrence_horizon_months)


def _generate_workspace_in_session(
    session: Session,
    scope: WorkspaceScope,
    clock: Clock,
    horizon_months: int,
) -> int:
    workspace = CurrentWorkspaceRepository(session, scope).get()
    today = _month_start(clock.today(workspace.timezone))
    through = _month_shift(today, horizon_months)
    created = 0
    for source in IncomeSourceRepository(session, scope).ordered(active_only=True):
        last_work_month = _month_shift(through, -source.pay_lag_months)
        if source.active_to is not None:
            last_work_month = min(last_work_month, source.active_to)
        # Work months before this one can still be paid in the future (pay lag); the pay-date
        # check in _sync_work_month skips the ones already due in the past.
        first_work_month = max(
            _month_shift(today, -source.pay_lag_months), _month_start(source.active_from)
        )
        if last_work_month < first_work_month:
            continue
        for month in _month_range(first_work_month, last_work_month):
            prior = ScheduledOccurrenceRepository(session, scope).for_income_work_month(
                source.id, month
            )
            if prior is None and (
                _sync_work_month(session, scope, clock, source, month, allow_past=False) is not None
            ):
                created += 1
    return created


def has_working_day_income_sources(session: Session, ctx: Context) -> bool:
    return any(
        source.archived_at is None and source.expected_mode == ExpectedUnitsMode.WORKING_DAYS.value
        for source in IncomeSourceRepository(session, ctx).ordered()
    )


def regenerate_working_day_income(
    session: Session, ctx: Context, clock: Clock, horizon_months: int
) -> int:
    for source in IncomeSourceRepository(session, ctx).ordered(active_only=True):
        if source.expected_mode != ExpectedUnitsMode.WORKING_DAYS.value:
            continue
        for occurrence in ScheduledOccurrenceRepository(session, ctx).for_income_source(source.id):
            if occurrence.status == "pending" and occurrence.work_month is not None:
                _sync_work_month(
                    session, ctx, clock, source, occurrence.work_month, allow_past=True
                )
    return _generate_workspace_in_session(session, ctx, clock, horizon_months)


def generate_workspace(
    factory: sessionmaker[Session],
    scope: WorkspaceScope,
    clock: Clock,
    settings: Settings,
) -> int:
    def write(session: Session) -> int:
        CurrentWorkspaceRepository(session, scope).lock(exclusive=True)
        return generate_workspace_in_session(session, scope, clock, settings)

    return run_immediate(factory, write)


def list_sources(factory: sessionmaker[Session], ctx: Context) -> list[IncomeSourceView]:
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        sources = IncomeSourceRepository(session, ctx).ordered()
        result: list[IncomeSourceView] = []
        attachment_repository = IncomeSourceTaxRuleRepository(session, ctx)
        log_repository = WorkLogRepository(session, ctx)
        workspace = CurrentWorkspaceRepository(session, ctx).get()
        today = ctx.clock.today(workspace.timezone)
        for source in sources:
            logs = log_repository.for_source(source.id)
            preview_rows: list[IncomePreview] = []
            first = _month_shift(_month_start(today), -source.pay_lag_months)
            work_months = (
                _month_range(first, _month_shift(first, 5)) if source.archived_at is None else []
            )
            for work_month in work_months:
                due = pay_date(work_month, source.pay_day, source.pay_lag_months)
                if due < today or work_month < source.active_from:
                    continue
                if source.active_to is not None and work_month > source.active_to:
                    continue
                estimate = _source_estimate(source, work_month, logs, workspace.country_code)
                selected, _missing = _tax_lines(session, ctx, source, due)
                breakdown = _calculate_breakdown(
                    estimate.net,
                    [tax for _, tax, _ in selected],
                    RecordAs(source.record_as),
                )
                preview_rows.append(
                    IncomePreview(
                        work_month=work_month,
                        due_date=due,
                        amount_state=estimate.amount_state,
                        units=estimate.units,
                        breakdown=breakdown,
                        tax_names={str(attachment.id): name for attachment, _, name in selected},
                    )
                )
            result.append(
                IncomeSourceView(
                    id=source.id,
                    name=source.name,
                    kind=IncomeKind(source.kind),
                    category_id=source.category_id,
                    currency=source.currency,
                    fixed_minor=source.fixed_minor,
                    rate_minor=source.rate_minor,
                    unit_label=source.unit_label,
                    pay_day=source.pay_day,
                    pay_lag_months=source.pay_lag_months,
                    expected_mode=ExpectedUnitsMode(source.expected_mode),
                    expected_units=source.expected_units,
                    average_months=source.average_months,
                    record_as=RecordAs(source.record_as),
                    active_from=source.active_from,
                    active_to=source.active_to,
                    archived=source.archived_at is not None,
                    attachments=tuple(attachment_repository.for_source(source.id)),
                    work_logs=tuple(logs),
                    preview=tuple(preview_rows),
                )
            )
        return result


def category_usage(session: Session, ctx: Context) -> dict[UUID, CategoryUsage]:
    counts: dict[UUID, int] = {}
    source_counts = IncomeSourceRepository(session, ctx).category_counts()
    payment_counts = IncomeSourceTaxRuleRepository(session, ctx).payment_category_counts()
    for category_id, count in (*source_counts.items(), *payment_counts.items()):
        counts[category_id] = counts.get(category_id, 0) + count
    return {category_id: CategoryUsage(references=count) for category_id, count in counts.items()}


def workspace_has_income_amounts(session: Session, ctx: Context) -> bool:
    return IncomeSourceRepository(session, ctx).any_exists()


def _source_fields(
    *,
    name: str,
    kind: str,
    category_id: UUID,
    fixed_minor: int | None,
    rate_minor: int | None,
    unit_label: str,
    pay_day_value: int,
    pay_lag_months: int,
    expected_mode: str,
    expected_units_value: Decimal | None,
    average_months: int,
    record_as: str,
    active_from: date,
    active_to: date | None,
) -> _IncomeSourceValues:
    clean_name = clean_description(name)
    if (
        not clean_name
        or not 1 <= pay_day_value <= 31
        or not 0 <= pay_lag_months <= 24
        or not 1 <= average_months <= 24
        or active_from.day != 1
        or (active_to is not None and active_to.day != 1)
        or (active_to is not None and active_to < active_from)
    ):
        raise IncomeError("invalid_source")
    try:
        source_kind = IncomeKind(kind)
        expected_mode_value = ExpectedUnitsMode(expected_mode)
        record_mode = RecordAs(record_as)
    except ValueError:
        raise IncomeError("invalid_source") from None
    if source_kind == IncomeKind.FIXED:
        if fixed_minor is None or not 0 < fixed_minor <= MAX_AMOUNT_MINOR:
            raise IncomeError("invalid_amount")
        rate_minor = None
    else:
        if rate_minor is None or not 0 < rate_minor <= MAX_AMOUNT_MINOR:
            raise IncomeError("invalid_amount")
        fixed_minor = None
    if expected_units_value is not None:
        if not expected_units_value.is_finite() or expected_units_value < 0:
            raise IncomeError("invalid_units")
        exponent = expected_units_value.as_tuple().exponent
        integer_digits = max(expected_units_value.adjusted() + 1, 0)
        scale = max(-exponent, 0) if isinstance(exponent, int) else 19
        if integer_digits > 20 or scale > 18 or integer_digits + scale > 38:
            raise IncomeError("invalid_units")
    if not unit_label.strip() or len(unit_label.strip()) > 40:
        raise IncomeError("invalid_source")
    if not MIN_YEAR <= active_from.year <= MAX_YEAR or (
        active_to is not None and not MIN_YEAR <= active_to.year <= MAX_YEAR
    ):
        raise IncomeError("invalid_date")
    return _IncomeSourceValues(
        name=clean_name,
        kind=source_kind.value,
        category_id=category_id,
        fixed_minor=fixed_minor,
        rate_minor=rate_minor,
        unit_label=unit_label.strip(),
        pay_day=pay_day_value,
        pay_lag_months=pay_lag_months,
        expected_mode=expected_mode_value.value,
        expected_units=expected_units_value,
        average_months=average_months,
        record_as=record_mode.value,
        active_from=active_from,
        active_to=active_to,
    )


def create_source(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    name: str,
    kind: str,
    category_id: UUID,
    fixed_minor: int | None,
    rate_minor: int | None,
    unit_label: str,
    pay_day_value: int,
    pay_lag_months: int,
    expected_mode: str,
    expected_units_value: Decimal | None,
    average_months: int,
    record_as: str,
    active_from: date,
    active_to: date | None,
) -> UUID:
    require_role(ctx, Role.EDITOR)
    fields = _source_fields(
        name=name,
        kind=kind,
        category_id=category_id,
        fixed_minor=fixed_minor,
        rate_minor=rate_minor,
        unit_label=unit_label,
        pay_day_value=pay_day_value,
        pay_lag_months=pay_lag_months,
        expected_mode=expected_mode,
        expected_units_value=expected_units_value,
        average_months=average_months,
        record_as=record_as,
        active_from=active_from,
        active_to=active_to,
    )

    def write(session: Session) -> UUID:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        workspace = CurrentWorkspaceRepository(session, ctx).get()
        category = CategoryRepository(session, ctx).get_or_raise(category_id)
        if category.kind != "income" or category.archived_at is not None:
            raise IncomeError("invalid_category")
        _check_working_days(fields, workspace.country_code)
        row = IncomeSourceRepository(session, ctx).create(
            **fields.as_dict(), currency=workspace.base_currency
        )
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="income_source.created",
            entity_type="income_source",
            entity_id=str(row.id),
            ts=ctx.clock.now(),
            data={"name": fields.name, "kind": fields.kind},
        )
        return row.id

    return run_immediate(factory, write)


def update_source(
    factory: sessionmaker[Session],
    ctx: Context,
    source_id: UUID,
    *,
    name: str,
    kind: str,
    category_id: UUID,
    fixed_minor: int | None,
    rate_minor: int | None,
    unit_label: str,
    pay_day_value: int,
    pay_lag_months: int,
    expected_mode: str,
    expected_units_value: Decimal | None,
    average_months: int,
    record_as: str,
    active_from: date,
    active_to: date | None,
) -> None:
    require_role(ctx, Role.EDITOR)
    fields = _source_fields(
        name=name,
        kind=kind,
        category_id=category_id,
        fixed_minor=fixed_minor,
        rate_minor=rate_minor,
        unit_label=unit_label,
        pay_day_value=pay_day_value,
        pay_lag_months=pay_lag_months,
        expected_mode=expected_mode,
        expected_units_value=expected_units_value,
        average_months=average_months,
        record_as=record_as,
        active_from=active_from,
        active_to=active_to,
    )

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        source = IncomeSourceRepository(session, ctx).get_or_raise(source_id)
        if source.archived_at is not None:
            raise IncomeError("archived")
        category = CategoryRepository(session, ctx).get_or_raise(fields.category_id)
        if category.kind != "income" or category.archived_at is not None:
            raise IncomeError("invalid_category")
        _check_working_days(fields, CurrentWorkspaceRepository(session, ctx).get().country_code)
        IncomeSourceRepository(session, ctx).update(source_id, **fields.as_dict())
        for occurrence in ScheduledOccurrenceRepository(session, ctx).for_income_source(source_id):
            if occurrence.status == "pending" and occurrence.work_month is not None:
                _sync_work_month(
                    session, ctx, ctx.clock, source, occurrence.work_month, allow_past=True
                )
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="income_source.updated",
            entity_type="income_source",
            entity_id=str(source_id),
            ts=ctx.clock.now(),
        )

    run_immediate(factory, write)


def attach_tax_rule(
    factory: sessionmaker[Session],
    ctx: Context,
    source_id: UUID,
    *,
    tax_rule_id: UUID,
    payment_category_id: UUID,
    due_day: int,
    due_month_offset: int,
    generate_payment: bool,
) -> UUID:
    require_role(ctx, Role.EDITOR)
    if not 1 <= due_day <= 31 or not 0 <= due_month_offset <= 24:
        raise IncomeError("invalid_tax_attachment")

    def write(session: Session) -> UUID:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        sources = IncomeSourceRepository(session, ctx)
        source = sources.get_or_raise(source_id)
        if source.archived_at is not None:
            raise IncomeError("archived")
        tax_rule = TaxRuleRepository(session, ctx).get_or_raise(tax_rule_id)
        category = CategoryRepository(session, ctx).get_or_raise(payment_category_id)
        if tax_rule.archived_at is not None or category.archived_at is not None:
            raise IncomeError("invalid_tax_attachment")
        if category.kind != "expense" or category.treatment == "transfer":
            raise IncomeError("invalid_tax_category")
        repo = IncomeSourceTaxRuleRepository(session, ctx)
        if repo.for_source_and_rule(source_id, tax_rule_id) is not None:
            raise IncomeError("duplicate_tax_rule")
        order = max((row.sort_order for row in repo.for_source(source_id)), default=-1) + 1
        attachment = repo.create(
            income_source_id=source_id,
            tax_rule_id=tax_rule_id,
            sort_order=order,
            generate_payment=generate_payment,
            payment_category_id=payment_category_id,
            due_day=due_day,
            due_month_offset=due_month_offset,
        )
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="income_source_tax_rule.attached",
            entity_type="income_source",
            entity_id=str(source_id),
            ts=ctx.clock.now(),
            data={"tax_rule_id": str(tax_rule_id)},
        )
        _resync_tax_months(session, ctx, source)
        return attachment.id

    return run_immediate(factory, write)


def _resync_tax_months(session: Session, ctx: Context, source: IncomeSource) -> None:
    """Recalculate every logged or pending work month of ``source`` after its tax attachments
    changed, so pending tax payments follow the new settings."""
    months = {row.work_month for row in WorkLogRepository(session, ctx).for_source(source.id)}
    months.update(
        row.work_month
        for row in ScheduledOccurrenceRepository(session, ctx).for_income_source(source.id)
        if row.work_month is not None and row.status == "pending"
    )
    for work_month in sorted(months):
        _sync_work_month(session, ctx, ctx.clock, source, work_month, allow_past=True)


def update_tax_attachment(
    factory: sessionmaker[Session],
    ctx: Context,
    source_id: UUID,
    attachment_id: UUID,
    *,
    payment_category_id: UUID,
    due_day: int,
    due_month_offset: int,
    generate_payment: bool,
) -> None:
    """Change how an attached tax rule's payment is planned; pending payments are re-synced."""
    require_role(ctx, Role.EDITOR)
    if not 1 <= due_day <= 31 or not 0 <= due_month_offset <= 24:
        raise IncomeError("invalid_tax_attachment")

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        source = IncomeSourceRepository(session, ctx).get_or_raise(source_id)
        if source.archived_at is not None:
            raise IncomeError("archived")
        repository = IncomeSourceTaxRuleRepository(session, ctx)
        attachment = repository.get_or_raise(attachment_id)
        if attachment.income_source_id != source_id:
            raise IncomeError("invalid_tax_attachment")
        category = CategoryRepository(session, ctx).get_or_raise(payment_category_id)
        if category.archived_at is not None:
            raise IncomeError("invalid_tax_attachment")
        if category.kind != "expense" or category.treatment == "transfer":
            raise IncomeError("invalid_tax_category")
        repository.update(
            attachment_id,
            payment_category_id=payment_category_id,
            due_day=due_day,
            due_month_offset=due_month_offset,
            generate_payment=generate_payment,
        )
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="income_source_tax_rule.updated",
            entity_type="income_source",
            entity_id=str(source_id),
            ts=ctx.clock.now(),
            data={"attachment_id": str(attachment_id), "tax_rule_id": str(attachment.tax_rule_id)},
        )
        _resync_tax_months(session, ctx, source)

    run_immediate(factory, write)


def archive_source(factory: sessionmaker[Session], ctx: Context, source_id: UUID) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        repository = IncomeSourceRepository(session, ctx)
        source = repository.get_or_raise(source_id)
        if source.archived_at is not None:
            return
        repository.update(source_id, archived_at=ctx.clock.now())
        occurrence_repository = ScheduledOccurrenceRepository(session, ctx)
        closed_months = MonthStateRepository(session, ctx).closed_months()
        for occurrence in occurrence_repository.for_income_source(source_id):
            if occurrence.status == "pending":
                if YearMonth.from_date(occurrence.due_date) in closed_months:
                    continue
                children = occurrence_repository.children(occurrence.id)
                retained_child = False
                for child in children:
                    if (
                        child.status != "pending"
                        or YearMonth.from_date(child.due_date) in closed_months
                    ):
                        retained_child = True
                    else:
                        occurrence_repository.delete(child)
                if retained_child:
                    occurrence_repository.update(occurrence.id, status="skipped")
                else:
                    occurrence_repository.delete(occurrence)
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="income_source.archived",
            entity_type="income_source",
            entity_id=str(source_id),
            ts=ctx.clock.now(),
        )

    run_immediate(factory, write)


def remove_tax_attachment(
    factory: sessionmaker[Session], ctx: Context, source_id: UUID, attachment_id: UUID
) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        repository = IncomeSourceTaxRuleRepository(session, ctx)
        attachment = repository.get_or_raise(attachment_id)
        if attachment.income_source_id != source_id:
            raise IncomeError("invalid_tax_attachment")
        occurrences = ScheduledOccurrenceRepository(session, ctx)
        closed_months = MonthStateRepository(session, ctx).closed_months()
        for child in occurrences.for_tax_attachment(attachment_id):
            if child.status != "pending":
                raise IncomeError("attachment_in_use")
            if YearMonth.from_date(child.due_date) in closed_months:
                raise IncomeError("month_closed")
            occurrences.delete(child)
        repository.delete(attachment)
        source = IncomeSourceRepository(session, ctx).get_or_raise(source_id)
        for occurrence in occurrences.for_income_source(source_id):
            if occurrence.status == "pending" and occurrence.work_month is not None:
                _sync_work_month(
                    session, ctx, ctx.clock, source, occurrence.work_month, allow_past=True
                )
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="income_source_tax_rule.removed",
            entity_type="income_source",
            entity_id=str(source_id),
            ts=ctx.clock.now(),
        )

    run_immediate(factory, write)


def log_work(
    factory: sessionmaker[Session],
    ctx: Context,
    source_id: UUID,
    work_month: date,
    units: Decimal,
) -> bool:
    """Save a month-level work log and recalculate only its pending occurrence.

    Returns ``True`` when a posted occurrence exists for the month; callers can offer an explicit
    transaction adjustment without changing the posted ledger row.
    """
    require_role(ctx, Role.EDITOR)
    _valid_month(work_month)
    if not units.is_finite() or units < 0:
        raise IncomeError("invalid_units")

    def write(session: Session) -> bool:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        sources = IncomeSourceRepository(session, ctx)
        source = sources.get_or_raise(source_id)
        if source.archived_at is not None or source.kind != IncomeKind.VARIABLE.value:
            raise IncomeError("invalid_source")
        occurrence_repo = ScheduledOccurrenceRepository(session, ctx)
        occurrence = occurrence_repo.for_income_work_month(source_id, work_month)
        if (
            occurrence is not None
            and occurrence.status == "pending"
            and YearMonth.from_date(occurrence.due_date)
            in MonthStateRepository(session, ctx).closed_months()
        ):
            raise IncomeError("month_closed")
        _check_units_size(units)
        repo = WorkLogRepository(session, ctx)
        row = next(
            (log for log in repo.for_source(source_id) if log.work_month == work_month), None
        )
        if row is not None and row.derived_from_days:
            # F3.7: the month is the sum of its calendar days; change the days instead.
            raise IncomeError("derived_work_log")
        if row is None:
            row = repo.create(income_source_id=source_id, work_month=work_month, units=units)
        else:
            repo.update(row.id, units=units)
        if occurrence is not None and occurrence.status == "posted":
            AuditEventRepository(session, ctx).record(
                actor_user_id=ctx.user.id,
                action="work_log.updated_after_posting",
                entity_type="work_log",
                entity_id=str(row.id),
                ts=ctx.clock.now(),
                data={"work_month": work_month.isoformat()},
            )
            return True
        _sync_work_month(session, ctx, ctx.clock, source, work_month, allow_past=True)
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="work_log.saved",
            entity_type="income_source",
            entity_id=str(source_id),
            ts=ctx.clock.now(),
            data={"work_month": work_month.isoformat()},
        )
        return False

    return run_immediate(factory, write)


def _check_units_size(units: Decimal) -> None:
    """Refuse values the 38-digit, 18-decimal ``DecimalType`` cannot store exactly."""
    exponent = units.as_tuple().exponent
    integer_digits = max(units.adjusted() + 1, 0)
    scale = max(-exponent, 0) if isinstance(exponent, int) else 19
    if integer_digits > 20 or scale > 18 or integer_digits + scale > 38:
        raise IncomeError("invalid_units")


# F3.7 work calendar


@dataclass(frozen=True, slots=True)
class CalendarDay:
    day: date
    units: Decimal | None
    weekend: bool
    holiday: bool

    @property
    def working_day(self) -> bool:
        return not self.weekend and not self.holiday


@dataclass(frozen=True, slots=True)
class WorkCalendar:
    source_id: UUID
    source_name: str
    unit_label: str
    month: date
    days: tuple[CalendarDay, ...]
    total: Decimal
    derived: bool
    manual_units: Decimal | None
    editable: bool


def _calendar_holidays(country: str | None, year: int) -> set[date]:
    """Holidays to show and skip in the calendar; a country without data shows weekends only."""
    try:
        return _holidays(country, year)
    except IncomeError:
        return set()


def _month_days(month: date) -> list[date]:
    return [
        date(month.year, month.month, number)
        for number in range(1, monthrange(month.year, month.month)[1] + 1)
    ]


def work_calendar(
    factory: sessionmaker[Session], ctx: Context, source_id: UUID, month: date
) -> WorkCalendar:
    """The day-level calendar of one variable income source for ``month`` (first day)."""
    require_role(ctx, Role.VIEWER)
    _valid_month(month)
    with factory() as session:
        source = IncomeSourceRepository(session, ctx).get_or_raise(source_id)
        if source.kind != IncomeKind.VARIABLE.value:
            raise IncomeError("invalid_source")
        workspace = CurrentWorkspaceRepository(session, ctx).get()
        holidays = _calendar_holidays(workspace.country_code, month.year)
        days = _month_days(month)
        entries = {
            row.work_date: row.units
            for row in WorkDayRepository(session, ctx).for_month(source_id, days[0], days[-1])
        }
        log = next(
            (
                row
                for row in WorkLogRepository(session, ctx).for_source(source_id)
                if row.work_month == month
            ),
            None,
        )
        return WorkCalendar(
            source_id=source.id,
            source_name=source.name,
            unit_label=source.unit_label,
            month=month,
            days=tuple(
                CalendarDay(
                    day=day,
                    units=entries.get(day),
                    weekend=day.weekday() >= 5,
                    holiday=day in holidays,
                )
                for day in days
            ),
            total=sum(entries.values(), Decimal(0)),
            derived=bool(entries),
            manual_units=log.units if log is not None and not log.derived_from_days else None,
            editable=source.archived_at is None,
        )


def _valid_day_units(units: Decimal) -> Decimal:
    if not units.is_finite() or not 0 < units <= MAX_WORK_DAY_UNITS:
        raise IncomeError("invalid_day_units")
    try:
        _check_units_size(units)
    except IncomeError:
        raise IncomeError("invalid_day_units") from None
    return units


def _change_work_days(
    factory: sessionmaker[Session],
    ctx: Context,
    source_id: UUID,
    month: date,
    change: Callable[[WorkDayRepository, dict[date, WorkDay], set[date]], None],
    action: str,
) -> bool:
    """Apply ``change`` to one month's day entries, then derive the month's work log and
    recalculate its income occurrence exactly as a month-level work log save does.

    Returns ``True`` when the month's income is already posted (the ledger row is not changed).
    """
    require_role(ctx, Role.EDITOR)
    _valid_month(month)

    def write(session: Session) -> bool:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        source = IncomeSourceRepository(session, ctx).get_or_raise(source_id)
        if source.archived_at is not None or source.kind != IncomeKind.VARIABLE.value:
            raise IncomeError("invalid_source")
        occurrence = ScheduledOccurrenceRepository(session, ctx).for_income_work_month(
            source_id, month
        )
        if (
            occurrence is not None
            and occurrence.status == "pending"
            and YearMonth.from_date(occurrence.due_date)
            in MonthStateRepository(session, ctx).closed_months()
        ):
            raise IncomeError("month_closed")
        days = _month_days(month)
        repository = WorkDayRepository(session, ctx)
        entries = {row.work_date: row for row in repository.for_month(source_id, days[0], days[-1])}
        workspace = CurrentWorkspaceRepository(session, ctx).get()
        change(repository, entries, _calendar_holidays(workspace.country_code, month.year))
        session.flush()
        remaining = repository.for_month(source_id, days[0], days[-1])
        logs = WorkLogRepository(session, ctx)
        log = next((row for row in logs.for_source(source_id) if row.work_month == month), None)
        if remaining:
            total = sum((row.units for row in remaining), Decimal(0))
            _check_units_size(total)
            if log is None:
                log = logs.create(
                    income_source_id=source_id,
                    work_month=month,
                    units=total,
                    derived_from_days=True,
                )
            else:
                logs.update(log.id, units=total, derived_from_days=True)
        elif log is not None and log.derived_from_days:
            logs.delete(log)
        audit = AuditEventRepository(session, ctx)
        if occurrence is not None and occurrence.status == "posted":
            audit.record(
                actor_user_id=ctx.user.id,
                action="work_log.updated_after_posting",
                entity_type="income_source",
                entity_id=str(source_id),
                ts=ctx.clock.now(),
                data={"work_month": month.isoformat()},
            )
            return True
        _sync_work_month(session, ctx, ctx.clock, source, month, allow_past=True)
        audit.record(
            actor_user_id=ctx.user.id,
            action=action,
            entity_type="income_source",
            entity_id=str(source_id),
            ts=ctx.clock.now(),
            data={"work_month": month.isoformat()},
        )
        return False

    return run_immediate(factory, write)


def toggle_work_day(
    factory: sessionmaker[Session], ctx: Context, source_id: UUID, day: date
) -> bool:
    """Mark ``day`` worked (1 unit) or, when it already has an entry, remove it."""

    def change(repository: WorkDayRepository, entries: dict[date, WorkDay], _: set[date]) -> None:
        existing = entries.get(day)
        if existing is None:
            repository.create(income_source_id=source_id, work_date=day, units=Decimal(1))
        else:
            repository.delete(existing)

    return _change_work_days(factory, ctx, source_id, _month_start(day), change, "work_day.toggled")


def set_work_day(
    factory: sessionmaker[Session], ctx: Context, source_id: UUID, day: date, units: Decimal
) -> bool:
    """Set the units of ``day`` (0 < units <= 24), creating its entry when needed."""
    value = _valid_day_units(units)

    def change(repository: WorkDayRepository, entries: dict[date, WorkDay], _: set[date]) -> None:
        existing = entries.get(day)
        if existing is None:
            repository.create(income_source_id=source_id, work_date=day, units=value)
        else:
            repository.update(existing.id, units=value)

    return _change_work_days(factory, ctx, source_id, _month_start(day), change, "work_day.saved")


def fill_working_days(
    factory: sessionmaker[Session], ctx: Context, source_id: UUID, month: date
) -> bool:
    """Add 1 unit for every working day of ``month`` without an entry; never overwrite."""

    def change(
        repository: WorkDayRepository, entries: dict[date, WorkDay], holidays: set[date]
    ) -> None:
        for day in _month_days(month):
            if day.weekday() < 5 and day not in holidays and day not in entries:
                repository.create(income_source_id=source_id, work_date=day, units=Decimal(1))

    return _change_work_days(factory, ctx, source_id, month, change, "work_days.filled")


def clear_work_month(
    factory: sessionmaker[Session], ctx: Context, source_id: UUID, month: date
) -> bool:
    """Remove every day entry of ``month``; the derived work log goes with them."""

    def change(repository: WorkDayRepository, entries: dict[date, WorkDay], _: set[date]) -> None:
        for row in entries.values():
            repository.delete(row)

    return _change_work_days(factory, ctx, source_id, month, change, "work_days.cleared")


def missing_work_logs(
    factory: sessionmaker[Session], ctx: Context, month: date, *, within_days: int = 10
) -> list[tuple[UUID, str, date]]:
    require_role(ctx, Role.VIEWER)
    _valid_month(month)
    today = ctx.clock.today(ctx.workspace.timezone)
    with factory() as session:
        sources = IncomeSourceRepository(session, ctx).ordered(active_only=True)
        logs = WorkLogRepository(session, ctx)
        logged_months = logs.work_months_for_sources([source.id for source in sources])
        result: list[tuple[UUID, str, date]] = []
        for source in sources:
            if source.kind != "variable":
                continue
            candidate = _month_shift(month, -source.pay_lag_months)
            if candidate < source.active_from or (
                source.active_to is not None and candidate > source.active_to
            ):
                continue
            due = pay_date(candidate, source.pay_day, source.pay_lag_months)
            if due > today + timedelta(days=within_days):
                continue
            if candidate not in logged_months.get(source.id, set()):
                result.append((source.id, source.name, candidate))
        return result
