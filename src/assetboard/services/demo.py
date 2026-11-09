"""Demo workspace writer (F9.11a).

Applies a pure :mod:`assetboard.domain.demo` plan to an **empty** workspace through the normal
services, inside one ``run_immediate`` transaction. Every write therefore runs the service's own
role check, workspace lock, month guard, validation and audit; the only thing skipped is the
per-call transaction. Historical rows are written with a ``Context`` whose fixed clock is at the
row's date (and the same clock is put in ``Session.info``), so ``created_at`` timestamps look
realistic.

The writer seeds the suggested categories, net-worth categories and asset classes when they are
missing (a new workspace normally has them already), writes the ledger, recurring items,
variable income and taxes, net worth, investments, snapshots and one real CSV import, and closes
``M-3`` **last**: closed months refuse ledger writes, including linked dividend income.
"""

# This module is the one place that composes the other services' in-session seams; they are
# intentionally private to each service and public only to this writer (the public wrappers keep
# the per-call transaction). Keeping the composition here avoids duplicating their validation.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import csv
import hashlib
import io
import os
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session, sessionmaker

from assetboard.clock import FixedClock
from assetboard.config import DEFAULT_RECURRENCE_HORIZON_MONTHS, Settings
from assetboard.db.engine import run_immediate
from assetboard.db.models.identity import User, Workspace
from assetboard.db.models.income import WorkDay
from assetboard.db.repositories import (
    AssetClassRepository,
    BondTermsRepository,
    BudgetLineRepository,
    CategoryRepository,
    CurrentWorkspaceRepository,
    FxRateRepository,
    ImportBatchRepository,
    ImportCategoryMappingRepository,
    IncomeSourceRepository,
    IncomeSourceTaxRuleRepository,
    InstrumentAllocationRepository,
    InstrumentRepository,
    InvestmentTransactionRepository,
    ItemValuationRepository,
    MembershipRepository,
    MonthStateRepository,
    NetWorthCategoryRepository,
    NetWorthItemRepository,
    NetWorthSnapshotRepository,
    PricePointRepository,
    RiskTargetPolicyRepository,
    ScheduledItemRepository,
    ScheduledOccurrenceRepository,
    ScopedRepository,
    TaxRuleRepository,
    TaxRuleVersionRepository,
    TransactionRepository,
    WorkDayRepository,
    WorkLogRepository,
)
from assetboard.domain.categories import name_key
from assetboard.domain.demo import (
    DemoBondTerms,
    DemoIncomeSource,
    DemoPlan,
    DemoRecurring,
    build_plan,
)
from assetboard.domain.income import IncomeKind
from assetboard.domain.months import YearMonth
from assetboard.domain.percent_or_fixed import PercentOrFixed
from assetboard.domain.recurrence import Frequency, RecurrenceRule, WeekendPolicy
from assetboard.domain.risk import RiskPolicy, RiskPolicyMode
from assetboard.domain.scheduled import AmountMode
from assetboard.domain.taxes import PaymentTiming, TaxBase, TaxKind
from assetboard.domain.transactions import TransactionDirection, description_key
from assetboard.i18n import gettext
from assetboard.services import (
    archive,
    bonds,
    budget,
    categories,
    imports,
    income,
    investment_transactions,
    investments,
    months,
    networth,
    networth_snapshots,
    occurrences,
    portfolio_links,
    prices,
    risk,
    scheduled,
    taxes,
    transactions,
)
from assetboard.services.context import Context, Role, require_role, require_role_now
from assetboard.services.import_csv import TransactionsCsvAdapter, default_import_adapters
from assetboard.services.imports import ImportAdapterRegistry
from assetboard.services.scheduled import ScheduledInput
from assetboard.services.taxes import _RuleFields, _validated_rule

__all__ = [
    "DemoBuildResult",
    "DemoBuildError",
    "DemoPlanMismatch",
    "DemoWorkspaceNotEmpty",
    "build_demo_workspace",
    "plan_for_workspace",
    "workspace_owner",
]

# The demo generates the same horizon the scheduler would use, so an occurrence is pending for
# every month the reset job will skip (F9.11b). The CLI/writer passes the instance value; this is
# only the fallback for tests and direct callers.
DEMO_RECURRENCE_HORIZON_MONTHS = DEFAULT_RECURRENCE_HORIZON_MONTHS


class DemoBuildError(Exception):
    """The demo dataset cannot be written."""


class DemoWorkspaceNotEmpty(DemoBuildError):
    """The builder only fills a workspace ``services.archive.workspace_is_empty`` accepts."""


class DemoPlanMismatch(DemoBuildError):
    """The plan's locale or base currency does not match the workspace."""


@dataclass(frozen=True, slots=True)
class DemoBuildResult:
    """What the builder created; counts only (the CLI never prints amounts)."""

    reference_date: date
    version: int
    counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class _Lookup:
    """The translated names the writer resolves through repositories."""

    categories: dict[tuple[str, str], UUID]
    networth: dict[tuple[str, str], UUID]
    asset_classes: dict[str, UUID]
    income_categories: dict[str, UUID]


def plan_for_workspace(ctx: Context, reference_date: date) -> DemoPlan:
    """Build the demo plan in the workspace's locale and base currency."""
    locale = ctx.workspace.locale
    return build_plan(
        reference_date=reference_date,
        locale=locale,
        base_currency=ctx.workspace.base_currency,
        translate=lambda message: gettext(message, locale),
    )


def _fixed_clock(day: date, timezone: str) -> FixedClock:
    """Noon in the workspace time zone: the date is stable, the timestamp deterministic."""
    value = datetime(day.year, day.month, day.day, 12, tzinfo=ZoneInfo(timezone))
    return FixedClock(value.isoformat())


def _ctx_at(ctx: Context, day: date) -> Context:
    return replace(ctx, clock=_fixed_clock(day, ctx.workspace.timezone))


def _use(session: Session, ctx: Context) -> None:
    """Make the timestamp listener stamp the same fixed clock the service audits with."""
    session.info["clock"] = ctx.clock


def _lookup(session: Session, ctx: Context) -> _Lookup:
    category_rows = CategoryRepository(session, ctx).ordered()
    return _Lookup(
        categories={
            (row.kind, row.name_key): row.id for row in category_rows if row.archived_at is None
        },
        networth={
            (row.side, row.name_key): row.id
            for row in NetWorthCategoryRepository(session, ctx).ordered()
            if row.archived_at is None
        },
        asset_classes={
            row.name_key: row.id
            for row in AssetClassRepository(session, ctx).ordered()
            if row.archived_at is None
        },
        income_categories={
            row.name_key: row.id
            for row in category_rows
            if row.kind == "income" and row.archived_at is None
        },
    )


def _category(lookup: _Lookup, kind: str, name: str) -> UUID:
    key = (kind, name_key(name))
    try:
        return lookup.categories[key]
    except KeyError:
        raise DemoBuildError(f"the plan names an unknown {kind} category: {name}") from None


def _category_any(lookup: _Lookup, name: str) -> UUID:
    key = name_key(name)
    for kind in ("expense", "income"):
        category_id = lookup.categories.get((kind, key))
        if category_id is not None:
            return category_id
    raise DemoBuildError(f"the plan names an unknown category: {name}")


def _seed_suggested_sets(session: Session, ctx: Context, plan: DemoPlan) -> None:
    if CategoryRepository(session, ctx).count() == 0:
        categories.seed_into(
            session,
            ctx,
            locale=plan.locale,
            actor_user_id=ctx.user.id,
            actor_type="user",
            now=ctx.clock.now(),
            automatic=False,
        )
    if not NetWorthCategoryRepository(session, ctx).ordered():
        networth.seed_into(
            session,
            ctx,
            locale=plan.locale,
            actor_user_id=ctx.user.id,
            actor_type="user",
            now=ctx.clock.now(),
            automatic=False,
        )
    if AssetClassRepository(session, ctx).active_count() == 0:
        investments.seed_into(
            session,
            ctx,
            locale=plan.locale,
            actor_user_id=ctx.user.id,
            actor_type="user",
            now=ctx.clock.now(),
            automatic=False,
        )


def _write_transactions(session: Session, ctx: Context, plan: DemoPlan, lookup: _Lookup) -> int:
    written = 0
    for row in plan.transactions:
        sctx = _ctx_at(ctx, row.day)
        _use(session, sctx)
        category_id = _category(lookup, row.direction, row.category)
        entry = transactions._Entry(
            day=row.day,
            amount=row.amount.to_decimal(),
            description=row.description,
            key=description_key(row.description),
        )
        transactions._create_transaction_in_session(
            session,
            sctx,
            entry=entry,
            wanted=TransactionDirection(row.direction),
            category_id=category_id,
        )
        written += 1
    return written


def _write_planned_values(session: Session, ctx: Context, plan: DemoPlan, lookup: _Lookup) -> int:
    written = 0
    for row in plan.planned_values:
        if row.amount.amount_minor == 0:
            continue
        month = YearMonth.from_date(row.month)
        day = min(month.last_day, plan.reference_date)
        sctx = _ctx_at(ctx, day)
        _use(session, sctx)
        budget._set_planned_in_session(
            session,
            sctx,
            month=month,
            category_id=_category_any(lookup, row.category),
            value=row.amount.to_decimal(),
        )
        written += 1
    return written


def _scheduled_input(row: DemoRecurring, category_id: UUID) -> ScheduledInput:
    direction = TransactionDirection("income" if row.key == "salary" else "expense")
    rule = RecurrenceRule(
        start_date=row.start_date,
        freq=Frequency(row.freq),
        interval=row.interval,
        weekdays=row.weekdays,
        month_day=int(row.month_day),
        month=row.month,
        end_date=row.end_date,
        max_count=row.max_count,
        weekend_policy=WeekendPolicy(row.weekend_policy),
    )
    return ScheduledInput(
        name=row.name,
        direction=direction,
        category_id=category_id,
        amount=row.amount,
        amount_mode=AmountMode(row.amount_mode),
        rule=rule,
        auto_post=row.auto_post,
    )


def _write_recurring(
    session: Session,
    ctx: Context,
    plan: DemoPlan,
    lookup: _Lookup,
    *,
    horizon_months: int,
) -> tuple[int, int]:
    settings = Settings(_env_file=None, recurrence_horizon_months=horizon_months)
    items = 0
    occurrences_written = 0
    for row in plan.recurring:
        _use(session, ctx)
        direction = TransactionDirection("income" if row.key == "salary" else "expense")
        category_id = _category(lookup, direction, row.category)
        values = _scheduled_input(row, category_id)
        item_id = scheduled.save_item_in_session(
            session,
            ctx,
            values,
            settings,
            include_past=True,
        )
        items += 1
        repository = ScheduledOccurrenceRepository(session, ctx)
        rows = repository.for_item(item_id)
        occurrences_written += len(rows)
        for occurrence in rows:
            if row.skip_nominal is not None and occurrence.nominal_date == row.skip_nominal:
                sctx = _ctx_at(ctx, occurrence.due_date)
                _use(session, sctx)
                occurrences._skip_in_session(session, sctx, occurrence.id)
                continue
            if occurrence.due_date >= plan.reference_date:
                continue
            if row.overdue_nominal is not None and occurrence.nominal_date == row.overdue_nominal:
                continue
            if occurrence.status != "pending":
                continue
            sctx = _ctx_at(ctx, occurrence.due_date)
            _use(session, sctx)
            occurrences._post_in_session(
                session,
                sctx,
                sctx.clock,
                occurrence.id,
                amount_minor=occurrence.expected_minor,
                day=occurrence.due_date,
                today=occurrence.due_date,
                source=occurrences.Source.RECURRING,
                actor_user_id=ctx.user.id,
            )
    return items, occurrences_written


def _write_income(
    session: Session, ctx: Context, plan: DemoPlan, lookup: _Lookup
) -> tuple[UUID, int]:
    source_plan: DemoIncomeSource = plan.income_source
    category_id = _category(lookup, "income", source_plan.category)
    _use(session, ctx)
    fields = income._source_fields(
        name=source_plan.name,
        kind=IncomeKind.VARIABLE.value,
        category_id=category_id,
        fixed_minor=None,
        rate_minor=source_plan.rate.amount_minor,
        unit_label=source_plan.unit_label,
        pay_day_value=source_plan.pay_day,
        pay_lag_months=source_plan.pay_lag_months,
        expected_mode="fixed",
        expected_units_value=source_plan.expected_units,
        average_months=source_plan.average_months,
        active_from=source_plan.active_from,
        active_to=None,
    )
    source_id = income._create_source_in_session(session, ctx, fields=fields)

    rule_fields: _RuleFields = _validated_rule(
        name=plan.tax_rule.name,
        notes="",
        kind=TaxKind.SELF_PAID.value,
        base=TaxBase.GROSS.value,
        payment_day=plan.tax_rule.payment_day,
        payment_timing=PaymentTiming.NEXT_MONTH.value,
        payment_category_id=_category(lookup, "expense", plan.tax_rule.payment_category),
    )
    tax_rule_id = taxes._create_rule_in_session(
        session,
        ctx,
        fields=rule_fields,
        value=PercentOrFixed.percent(plan.tax_rule.percent_bp),
        effective_from=source_plan.active_from,
    )
    income._attach_tax_rule_in_session(
        session,
        ctx,
        source_id,
        tax_rule_id=tax_rule_id,
        payment_category_id=None,
        due_day=None,
        due_month_offset=None,
        generate_payment=True,
    )

    # The work calendar is the source of the work logs: one write per month creates every day
    # and rebuilds the month's derived log and pending income/tax occurrences.
    by_month: dict[date, list[tuple[date, Decimal]]] = {}
    for work_day in source_plan.work_days:
        by_month.setdefault(work_day.day.replace(day=1), []).append((work_day.day, work_day.units))
    for month, days in sorted(by_month.items()):
        latest = max(day for day, _units in days)
        sctx = _ctx_at(ctx, min(latest, plan.reference_date))
        _use(session, sctx)

        def change(
            repository: WorkDayRepository,
            entries: dict[date, WorkDay],
            holidays: set[date],
            days: list[tuple[date, Decimal]] = days,
        ) -> None:
            del holidays
            for day, units in days:
                if day in entries:
                    continue
                value = income._valid_day_units(units)
                repository.create(
                    income_source_id=source_id,
                    work_date=day,
                    units=value,
                )

        income._change_work_days_in_session(
            session,
            sctx,
            source_id,
            month,
            change,
            "work_days.filled",
            False,
        )

    # Post every received month and its tax payments; keep the current pending one pending.
    repository = ScheduledOccurrenceRepository(session, ctx)
    occurrences_written = 0
    for occurrence in sorted(
        repository.for_income_source(source_id), key=lambda row: (row.due_date, str(row.id))
    ):
        if occurrence.generated_by != "income":
            continue
        occurrences_written += 1
        if occurrence.due_date >= plan.reference_date or occurrence.status != "pending":
            continue
        sctx = _ctx_at(ctx, occurrence.due_date)
        _use(session, sctx)
        occurrences._post_in_session(
            session,
            sctx,
            sctx.clock,
            occurrence.id,
            amount_minor=occurrence.expected_minor,
            day=occurrence.due_date,
            today=occurrence.due_date,
            source=occurrences.Source.RECURRING,
            actor_user_id=ctx.user.id,
        )
        for child in repository.children(occurrence.id):
            occurrences_written += 1
            if child.status != "pending" or child.due_date >= plan.reference_date:
                continue
            child_ctx = _ctx_at(ctx, child.due_date)
            _use(session, child_ctx)
            occurrences._post_in_session(
                session,
                child_ctx,
                child_ctx.clock,
                child.id,
                amount_minor=child.expected_minor,
                day=child.due_date,
                today=child.due_date,
                source=occurrences.Source.RECURRING,
                actor_user_id=ctx.user.id,
            )
    return source_id, occurrences_written


def _write_networth(
    session: Session, ctx: Context, plan: DemoPlan, lookup: _Lookup
) -> tuple[int, int]:
    items = 0
    valuations = 0
    for row in plan.networth_items:
        if not row.values:
            continue
        first = row.values[0]
        sctx = _ctx_at(ctx, first.as_of)
        _use(session, sctx)
        category_id = lookup.networth[(row.side, name_key(row.category))]
        item = networth._create_item_in_session(
            session,
            sctx,
            side=networth.Side(row.side),
            category_id=category_id,
            name=row.name,
            notes="",
            acquired_on=None,
            link_type=None,
            link_id=None,
            parsed=first.value.to_decimal(),
            requested_currency=first.value.currency,
            as_of=first.as_of,
            valuation_note="",
            include_in_net_worth=True,
            investable=False,
            estimate_between=row.side == "asset",
        )
        items += 1
        valuations += 1
        for value in row.values[1:]:
            sctx = _ctx_at(ctx, value.as_of)
            _use(session, sctx)
            networth._add_valuation_in_session(
                session,
                sctx,
                item.id,
                value=value.value.to_decimal(),
                as_of=value.as_of,
                note="",
            )
            valuations += 1
    return items, valuations


def _write_investments(
    session: Session, ctx: Context, plan: DemoPlan, lookup: _Lookup
) -> tuple[int, int, int, int, int]:
    instrument_ids: dict[str, UUID] = {}
    instruments = 0
    for row in plan.instruments:
        _use(session, ctx)
        allocations = tuple(
            (lookup.asset_classes[name_key(allocation.asset_class)], allocation.weight_bp)
            for allocation in row.allocations
        )
        values = investments._instrument_values(
            name=row.name,
            symbol=row.symbol,
            type_value=row.type,
            quote_currency=row.quote_currency,
            unit_label=row.unit_label,
            locale=plan.locale,
        )
        (
            clean,
            key,
            symbol,
            instrument_type,
            currency,
            unit,
            source,
            provider_key,
            provider_symbol,
        ) = values
        view = investments._create_instrument_in_session(
            session,
            ctx,
            name=clean,
            key=key,
            symbol=symbol,
            instrument_type=instrument_type,
            currency=currency,
            unit=unit,
            source=source,
            provider_key=provider_key,
            provider_symbol=provider_symbol,
            allocations=allocations,
            locale=plan.locale,
        )
        instrument_ids[row.name] = view.id
        instruments += 1

    fx_written = 0
    for fx in plan.fx_points:
        sctx = _ctx_at(ctx, fx.day)
        _use(session, sctx)
        prices._save_fx_rate_in_session(
            session, sctx, quote_currency=fx.quote_currency, day=fx.day, rate=fx.rate
        )
        fx_written += 1

    price_written = 0
    for point in plan.price_points:
        sctx = _ctx_at(ctx, point.day)
        _use(session, sctx)
        prices._save_manual_price_in_session(
            session,
            sctx,
            instrument_ids[point.instrument],
            day=point.day,
            price=point.price,
        )
        price_written += 1

    trade_written = 0
    for trade in plan.investment_transactions:
        sctx = _ctx_at(ctx, trade.day)
        _use(session, sctx)
        income_category_id = (
            lookup.income_categories[name_key(trade.income_category)]
            if trade.income_category is not None
            else None
        )
        investment_transactions._create_transaction_in_session(
            session,
            sctx,
            instrument_id=instrument_ids[trade.instrument],
            type_value=trade.type,
            date_value=trade.day.isoformat(),
            quantity_value=format(trade.quantity, "f"),
            unit_price_value=format(trade.unit_price, "f"),
            fee_value=format(trade.fee.to_decimal(), "f"),
            fx_value=format(trade.fx_to_base, "f") if trade.fx_to_base is not None else "",
            note_value=trade.note,
            locale="en",
            amounts_value=trade.amounts,
            base_unit_price_value=(
                format(trade.base_unit_price, "f") if trade.base_unit_price is not None else ""
            ),
            base_fee_value=(
                format(trade.base_fee.to_decimal(), "f") if trade.base_fee is not None else ""
            ),
            record_income=trade.record_income,
            income_category_id=income_category_id,
        )
        trade_written += 1

    _write_bond_terms(session, ctx, plan.bond_terms, instrument_ids[plan.bond_terms.instrument])
    _use(session, ctx)
    policy = plan.risk_policy
    risk._save_risk_policy_in_session(
        session,
        ctx,
        RiskPolicy(
            mode=RiskPolicyMode.FIXED,
            safe_bp=policy.safe_bp,
            growth_bp=policy.growth_bp,
            speculative_bp=policy.speculative_bp,
            tolerance_bp=policy.tolerance_bp,
        ),
    )
    _use(session, ctx)
    portfolio_links._create_missing_links_in_session(session, ctx)
    return instruments, trade_written, price_written, fx_written, 1


def _write_bond_terms(
    session: Session, ctx: Context, terms: DemoBondTerms, instrument_id: UUID
) -> None:
    _use(session, ctx)
    values = bonds.BondTermsInput(
        series_code=terms.series_code,
        face_value=format(terms.face_value.to_decimal(), "f"),
        maturity_date=terms.maturity_date.isoformat(),
        interest_mode=terms.interest_mode,
        rate_periods=tuple(
            (from_date.isoformat(), format(Decimal(rate_bp) / 100, "f"))
            for from_date, rate_bp in terms.rate_schedule
        ),
        tax_rate=format(Decimal(terms.tax_rate_bp) / 100, "f"),
    )
    bonds._save_bond_terms_in_session(session, ctx, instrument_id, values=values, locale="en")


def _write_snapshots(session: Session, ctx: Context, plan: DemoPlan) -> int:
    written = 0
    for day in plan.snapshot_dates:
        sctx = _ctx_at(ctx, day)
        _use(session, sctx)
        created = networth_snapshots._auto_write(
            session,
            sctx,
            as_of=day,
            clock=sctx.clock,
            timezone=ctx.workspace.timezone,
        )
        if created:
            written += 1
    return written


def _csv_content(plan: DemoPlan) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["date", "direction", "amount", "category", "description"])
    for row in plan.csv_rows:
        writer.writerow(
            [
                row.day.isoformat(),
                row.direction,
                format(row.amount.to_decimal(), "f"),
                row.category,
                row.description,
            ]
        )
    return buffer.getvalue().encode("utf-8")


def _write_import(
    session: Session,
    ctx: Context,
    plan: DemoPlan,
    data_dir: Path,
) -> tuple[int, Path | None]:
    content = _csv_content(plan)
    _use(session, ctx)
    batch = imports.ImportBatchRepository(session, ctx).create(
        source="transactions_csv",
        mode="merge",
        filename="demo-ledger.csv",
        file_sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        options={"delimiter": "comma", "decimal": "dot", "remember_mappings": True},
        status="planned",
        started_by_user_id=ctx.user.id,
    )
    path = imports._upload_path(data_dir, ctx.workspace.id, batch.id)
    imports_root = data_dir / "imports"
    imports_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(imports_root, 0o700)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    path.write_bytes(content)
    os.chmod(path, 0o600)

    try:
        status, base_report, parsed = imports._plan_file(
            content, delimiter=imports._delimiter_option(batch.options)
        )
        if status == "failed" or parsed is None:
            raise DemoBuildError("the generated demo CSV could not be parsed")
        adapter = TransactionsCsvAdapter()
        report = imports._merge_adapter_report(
            base_report,
            adapter.plan(
                session,
                ctx,
                batch,
                parsed,
                base_currency=ctx.workspace.base_currency,
            ),
        )
        report["source"] = parsed.source
        batch.report = report

        registry = ImportAdapterRegistry(default_import_adapters())
        imports._apply_import_in_session(
            session,
            ctx,
            stored_path=path,
            batch_id=batch.id,
            adapters=registry,
            skip_invalid=False,
        )
    except BaseException:
        imports._delete_upload(path)
        raise
    return 1, path


_COUNT_REPOSITORIES: tuple[tuple[str, type[ScopedRepository[Any]]], ...] = (
    ("categories", CategoryRepository),
    ("transactions", TransactionRepository),
    ("planned_values", BudgetLineRepository),
    ("month_states", MonthStateRepository),
    ("scheduled_items", ScheduledItemRepository),
    ("scheduled_occurrences", ScheduledOccurrenceRepository),
    ("income_sources", IncomeSourceRepository),
    ("income_tax_rules", IncomeSourceTaxRuleRepository),
    ("work_logs", WorkLogRepository),
    ("work_days", WorkDayRepository),
    ("tax_rules", TaxRuleRepository),
    ("tax_rule_versions", TaxRuleVersionRepository),
    ("networth_categories", NetWorthCategoryRepository),
    ("networth_items", NetWorthItemRepository),
    ("item_valuations", ItemValuationRepository),
    ("networth_snapshots", NetWorthSnapshotRepository),
    ("asset_classes", AssetClassRepository),
    ("instruments", InstrumentRepository),
    ("instrument_allocations", InstrumentAllocationRepository),
    ("investment_transactions", InvestmentTransactionRepository),
    ("bond_terms", BondTermsRepository),
    ("price_points", PricePointRepository),
    ("fx_rates", FxRateRepository),
    ("risk_target_policies", RiskTargetPolicyRepository),
    ("import_batches", ImportBatchRepository),
    ("import_category_mappings", ImportCategoryMappingRepository),
)


def _counts(session: Session, ctx: Context) -> dict[str, int]:
    return {name: len(repository(session, ctx).list()) for name, repository in _COUNT_REPOSITORIES}


def build_demo_workspace(
    factory: sessionmaker[Session],
    ctx: Context,
    plan: DemoPlan,
    *,
    data_dir: Path,
    horizon_months: int = DEMO_RECURRENCE_HORIZON_MONTHS,
) -> DemoBuildResult:
    """Fill an empty workspace with ``plan`` in one write; returns counts only."""
    require_role(ctx, Role.OWNER)
    if plan.locale != ctx.workspace.locale or plan.base_currency != ctx.workspace.base_currency:
        raise DemoPlanMismatch("the plan was built for another locale or base currency")

    upload_path: Path | None = None

    def write(session: Session) -> DemoBuildResult:
        nonlocal upload_path
        require_role_now(session, ctx, Role.OWNER)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        if not archive.workspace_is_empty(session, ctx):
            raise DemoWorkspaceNotEmpty("the workspace is not empty")
        tctx = _ctx_at(ctx, plan.reference_date)
        _use(session, tctx)

        _seed_suggested_sets(session, tctx, plan)
        lookup = _lookup(session, tctx)

        _write_transactions(session, tctx, plan, lookup)
        _write_planned_values(session, tctx, plan, lookup)
        _write_recurring(session, tctx, plan, lookup, horizon_months=horizon_months)
        _write_income(session, tctx, plan, lookup)
        _write_networth(session, tctx, plan, lookup)
        _write_investments(session, tctx, plan, lookup)
        _write_snapshots(session, tctx, plan)
        _import_batches, upload_path = _write_import(session, tctx, plan, data_dir)

        # Closed months refuse ledger writes (linked dividend income included), so M-3 is the
        # very last thing the builder does.
        months.close_month_state(session, tctx, YearMonth.from_date(plan.closed_month))
        return DemoBuildResult(
            reference_date=plan.reference_date,
            version=plan.version,
            counts=_counts(session, tctx),
        )

    try:
        result = run_immediate(factory, write)
    except BaseException:
        if upload_path is not None:
            imports._delete_upload(upload_path)
        raise
    if upload_path is not None:
        imports._delete_upload(upload_path)
    return result


@dataclass(frozen=True, slots=True)
class _WorkspaceId:
    id: UUID


@dataclass(frozen=True, slots=True)
class _WorkspaceScope:
    """The minimum a repository needs: the workspace whose rows it may touch."""

    workspace: _WorkspaceId


def workspace_owner(session: Session, workspace_id: UUID) -> User | None:
    """The workspace's oldest owner, through the scoped membership repository."""
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        return None
    scope = _WorkspaceScope(_WorkspaceId(workspace.id))
    members = MembershipRepository(session, scope).members()
    owner = next((member for member in members if member.role == "owner"), None)
    if owner is None:
        return None
    return session.get(User, owner.user_id)


# Kept importable for F9.11b, which needs the owner's profile fields for age-based risk modes.
def ensure_demo_profile(
    session: Session, workspace_id: UUID, *, reference_date: date
) -> tuple[int, int] | None:
    """Set the workspace owner's ``birth_year``/``life_expectancy`` when the profile is empty.

    Never overwrites a real profile: it only fills a missing ``birth_year`` and leaves a custom
    life expectancy alone. Returns the values now set, or ``None`` when there is no owner.
    """
    owner = workspace_owner(session, workspace_id)
    if owner is None:
        return None
    if owner.birth_year is None:
        owner.birth_year = reference_date.year - 38
        if owner.life_expectancy == 90:
            owner.life_expectancy = 88
    return owner.birth_year, owner.life_expectancy
