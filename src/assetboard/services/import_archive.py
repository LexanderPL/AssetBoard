"""Archive restore and merge adapters (F4.4).

Restore replaces a workspace that holds no data (the untouched suggested category set aside)
with the archive's settings, categories, budget lines, transactions and month states. Merge
applies only the ledger data through the archive-category mapping step; settings and month
states are reported but never written.

The archive reader (``domain.archive``) has already validated the bytes and row values. This
module turns those rows into ORM writes through the scoped repositories, reuses the shared
import dedupe helpers, and keeps every write inside the caller's transaction.
"""

from __future__ import annotations

import contextlib
import json
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy.orm import Session

from assetboard.db.models.identity import Workspace
from assetboard.db.models.import_batch import ImportBatch
from assetboard.db.models.investments import AssetClass, RiskTargetPolicy
from assetboard.db.models.ledger import Category
from assetboard.db.models.networth import NetWorthCategory
from assetboard.db.repositories import (
    AssetClassRepository,
    AuditEventRepository,
    BondTermsRepository,
    BudgetLineRepository,
    CategoryRepository,
    CurrentWorkspaceRepository,
    FxRateRepository,
    ImportCategoryMappingRepository,
    InstrumentAllocationRepository,
    InstrumentRepository,
    InvestmentTransactionRepository,
    ItemValuationRepository,
    MonthStateRepository,
    NetWorthCategoryRepository,
    NetWorthItemRepository,
    NetWorthSnapshotRepository,
    PricePointRepository,
    PriceSourceConfigRepository,
    RiskTargetPolicyRepository,
    TransactionRepository,
)
from assetboard.domain.archive import (
    INVESTMENT_CSV_FILES,
    NETWORTH_CSV_FILES,
    ArchiveRows,
    BudgetLineRow,
    CategoryRow,
    NetWorthItemRow,
    ParsedArchive,
    TransactionRow,
)
from assetboard.domain.categories import Kind, Treatment, clean_color, clean_name, name_key
from assetboard.domain.category_mapping import CategorySnapshot, next_color
from assetboard.domain.import_csv import (
    parse_date,
    parse_month,
    parse_planned_amount,
    parse_timestamp,
    parse_transaction_amount,
)
from assetboard.domain.import_upload import DEADLINE_CHECK_ROWS, ParsedUpload, ParseTimeout
from assetboard.domain.ledger_csv import plain_amount
from assetboard.domain.money import minor_unit_for
from assetboard.domain.months import YearMonth
from assetboard.domain.providers import PROVIDERS
from assetboard.domain.transactions import EntryError, description_key
from assetboard.services.archive import (
    suggested_asset_classes_to_remove,
    suggested_categories_to_remove,
    suggested_networth_categories_to_remove,
    workspace_is_empty,
)
from assetboard.services.context import Context
from assetboard.services.import_dedupe import (
    IncomingBudgetLine,
    IncomingTransaction,
    match_budget_lines,
    match_transactions,
)
from assetboard.services.imports import ImportAdapter, ImportRestoreUnavailable
from assetboard.services.imports import ImportError as ImportServiceError
from assetboard.services.workspace_options import WorkspaceOptionError, validate_workspace_options
from assetboard.services.workspaces import MAX_WORKSPACE_NAME_LENGTH

FILE = "archive"
RESTORE_SETTINGS_FIELDS = (
    "name",
    "base_currency",
    "country_code",
    "timezone",
    "locale",
    "auto_copy_plan",
    "taxes_enabled",
    "upcoming_days",
    "investment_cost_basis",
)


def _entry(
    code: str, *, file: str = FILE, line: object = "", column: object = ""
) -> dict[str, object]:
    return {"file": file, "line": line, "column": column, "code": code, "message": code}


def _money(minor: int, currency: str) -> str:
    return plain_amount(minor, currency)


def _restore_counts(**values: int) -> dict[str, int]:
    return {
        "to_create": 0,
        "duplicates_skipped": 0,
        "conflicts_skipped": 0,
        "closed_months_skipped": 0,
        "ignored_categories_skipped": 0,
        "future_dates_skipped": 0,
        "deleted_skipped": 0,
        "archived_categories_skipped": 0,
        "transfer_categories_skipped": 0,
        "invalid": 0,
        "mapping_invalid": 0,
        "categories_created": 0,
        "transactions_created": 0,
        "budget_lines_created": 0,
        "months_created": 0,
        "deleted_transactions": 0,
        "suggested_categories_removed": 0,
        "suggested_networth_categories_removed": 0,
        "networth_categories_created": 0,
        "networth_items_created": 0,
        "item_valuations_created": 0,
        "networth_snapshots_created": 0,
        "linked_items_unlinked": 0,
        "asset_classes_created": 0,
        "instruments_created": 0,
        "instrument_allocations_created": 0,
        "investment_transactions_created": 0,
        "bond_terms_created": 0,
        "price_points_created": 0,
        "fx_rates_created": 0,
        "price_source_configs_created": 0,
        "risk_policy_restored": 0,
        "suggested_asset_classes_removed": 0,
        **values,
    }


def _empty_report(**values: object) -> dict[str, Any]:
    report: dict[str, Any] = {
        "counts": _restore_counts(),
        "months": [],
        "categories": [],
        "mapping": [],
        "mapping_errors": [],
        "closed_months": [],
        "warnings": [],
        "skipped": [],
        "errors": [],
        "refused": False,
    }
    report.update(values)
    return report


def _archive(parsed: ParsedUpload) -> ParsedArchive:
    if parsed.archive is None:  # defensive: the upload route only sends ZIPs to this adapter
        raise ImportServiceError("the upload is not a complete archive")
    return parsed.archive


@dataclass(frozen=True, slots=True)
class _RestoreSettings:
    name: str
    base_currency: str
    country_code: str | None
    timezone: str
    locale: str
    auto_copy_plan: bool
    taxes_enabled: bool
    upcoming_days: int | None
    investment_cost_basis: str

    def values(self) -> dict[str, object]:
        return {field: getattr(self, field) for field in RESTORE_SETTINGS_FIELDS}


def _restore_settings(
    archive: ParsedArchive,
) -> tuple[_RestoreSettings | None, list[dict[str, object]]]:
    workspace = archive.rows.workspace
    errors: list[dict[str, object]] = []
    name = str(workspace.get("name", "")).strip()
    if not name or len(name) > MAX_WORKSPACE_NAME_LENGTH:
        errors.append(_entry("invalid_workspace_settings", file="workspace.json", column="name"))
    try:
        options = validate_workspace_options(
            base_currency=str(workspace.get("base_currency", "")),
            country_code=cast("str | None", workspace.get("country_code")),
            timezone=str(workspace.get("timezone", "")),
            locale=str(workspace.get("locale", "")),
        )
    except WorkspaceOptionError as error:
        errors.append(
            _entry("invalid_workspace_settings", file="workspace.json", column=error.field)
        )
        return None, errors
    upcoming = workspace.get("upcoming_days")
    if upcoming is not None and not isinstance(upcoming, int):
        errors.append(
            _entry("invalid_workspace_settings", file="workspace.json", column="upcoming_days")
        )
    taxes = workspace.get("taxes_enabled", False)
    if not isinstance(taxes, bool):
        errors.append(
            _entry("invalid_workspace_settings", file="workspace.json", column="taxes_enabled")
        )
    cost_basis = workspace.get("investment_cost_basis", "average")
    if cost_basis not in {"average", "fifo"}:
        errors.append(
            _entry(
                "invalid_workspace_settings",
                file="workspace.json",
                column="investment_cost_basis",
            )
        )
    if errors:
        return None, errors
    return (
        _RestoreSettings(
            name=name,
            base_currency=options.base_currency,
            country_code=options.country_code,
            timezone=options.timezone,
            locale=options.locale,
            auto_copy_plan=bool(workspace.get("auto_copy_plan", False)),
            taxes_enabled=cast("bool", taxes),
            upcoming_days=cast("int | None", upcoming),
            investment_cost_basis=cast("str", cost_basis),
        ),
        [],
    )


def _settings_diff(workspace: Workspace, wanted: Mapping[str, object]) -> list[str]:
    return [
        field for field in RESTORE_SETTINGS_FIELDS if getattr(workspace, field) != wanted[field]
    ]


def _archive_setting_values(workspace: Mapping[str, object]) -> dict[str, object]:
    """Archive settings with the 1.0 defaults applied, for the merge information report."""
    return {
        "name": str(workspace.get("name", "")),
        "base_currency": str(workspace.get("base_currency", "")),
        "country_code": workspace.get("country_code"),
        "timezone": str(workspace.get("timezone", "")),
        "locale": str(workspace.get("locale", "")),
        "auto_copy_plan": bool(workspace.get("auto_copy_plan", False)),
        "taxes_enabled": workspace.get("taxes_enabled", False),
        "upcoming_days": workspace.get("upcoming_days"),
        "investment_cost_basis": workspace.get("investment_cost_basis", "average"),
    }


def _risk_policy_tuple(values: object) -> tuple[object, ...] | None:
    if values is None:
        return None
    return (
        getattr(values, "mode", None),
        getattr(values, "safe_bp", None),
        getattr(values, "growth_bp", None),
        getattr(values, "speculative_bp", None),
        getattr(values, "tolerance_bp", None),
    )


def _current_risk_policy(session: Session, ctx: Context) -> tuple[object, ...] | None:
    row = RiskTargetPolicyRepository(session, ctx).for_workspace()
    return _risk_policy_tuple(row)


def _duplicate_active_categories(rows: tuple[CategoryRow, ...]) -> set[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    duplicates: set[tuple[str, str]] = set()
    for row in rows:
        if row.archived_at is not None:
            continue
        key = (row.kind, name_key(row.name))
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    return duplicates


def _restore_counts_for(
    rows: ArchiveRows, *, removed: int, removed_networth: int, removed_assets: int
) -> dict[str, int]:
    budget_lines = sum(1 for row in rows.budget_lines if _planned_minor(row))
    deleted = sum(1 for row in rows.transactions if row.deleted_at)
    asset_class_ids = {UUID(row.id) for row in rows.asset_classes}
    instrument_ids = {UUID(row.id) for row in rows.instruments}
    linked = sum(
        _link_target_missing(row, asset_class_ids, instrument_ids) for row in rows.networth_items
    )
    return _restore_counts(
        to_create=(
            len(rows.categories)
            + budget_lines
            + len(rows.transactions)
            + len(rows.months)
            + len(rows.networth_categories)
            + len(rows.networth_items)
            + len(rows.item_valuations)
            + len(rows.networth_snapshots)
            + len(rows.asset_classes)
            + len(rows.instruments)
            + len(rows.instrument_allocations)
            + len(rows.investment_transactions)
            + len(rows.bond_terms)
            + len(rows.price_points)
            + len(rows.fx_rates)
            + len(rows.price_source_configs)
            + (1 if rows.risk_target_policy is not None else 0)
        ),
        categories_created=len(rows.categories),
        transactions_created=len(rows.transactions),
        budget_lines_created=budget_lines,
        months_created=len(rows.months),
        deleted_transactions=deleted,
        suggested_categories_removed=removed,
        suggested_networth_categories_removed=removed_networth,
        networth_categories_created=len(rows.networth_categories),
        networth_items_created=len(rows.networth_items),
        item_valuations_created=len(rows.item_valuations),
        networth_snapshots_created=len(rows.networth_snapshots),
        linked_items_unlinked=linked,
        asset_classes_created=len(rows.asset_classes),
        instruments_created=len(rows.instruments),
        instrument_allocations_created=len(rows.instrument_allocations),
        investment_transactions_created=len(rows.investment_transactions),
        bond_terms_created=len(rows.bond_terms),
        price_points_created=len(rows.price_points),
        fx_rates_created=len(rows.fx_rates),
        price_source_configs_created=len(rows.price_source_configs),
        risk_policy_restored=1 if rows.risk_target_policy is not None else 0,
        suggested_asset_classes_removed=removed_assets,
    )


def _planned_minor(row: BudgetLineRow) -> int:
    try:
        return parse_planned_amount(row.planned, row.currency, decimal="dot")
    except EntryError:
        return 0


# --- archive merge -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Choice:
    source_id: UUID
    action: str
    category_id: UUID | None
    create_name: str
    create_treatment: str
    create_color: str


@dataclass
class _SourceCategory:
    row: CategoryRow
    source_id: UUID
    line: int
    kind: str
    name: str
    name_key: str
    treatment: str
    color: str
    archived_at: datetime | None
    rows: int = 0
    total_minor: int = 0


@dataclass
class _Resolution:
    source: _SourceCategory
    action: str
    target_id: UUID | None
    target_name: str
    target_archived: bool
    target_treatment: str
    create_name: str
    create_treatment: str
    create_color: str
    error: str = ""
    created: bool = False


@dataclass
class _CategoryResolution:
    resolutions: dict[UUID, _Resolution]
    report_rows: list[dict[str, object]]
    skipped: list[dict[str, object]]
    mapping: list[dict[str, object]]
    mapping_errors: list[dict[str, object]]
    mapping_invalid: int
    created: int
    duplicates: int
    conflicts: int


@dataclass
class _ParsedTransaction:
    row: TransactionRow
    source: _SourceCategory
    line: int
    day: date
    amount_minor: int


@dataclass
class _ParsedBudget:
    row: BudgetLineRow
    source: _SourceCategory
    line: int
    month: YearMonth
    planned_minor: int


@dataclass
class _MonthTotals:
    expense_minor: int = 0
    income_minor: int = 0
    planned_minor: int = 0
    rows_to_create: int = 0
    rows_skipped: int = 0


@dataclass
class _CategoryTotals:
    kind: str
    name: str
    total_minor: int = 0
    rows_to_create: int = 0


@dataclass
class _Collected:
    sources: OrderedDict[UUID, _SourceCategory]
    transactions: list[_ParsedTransaction]
    budget_lines: list[_ParsedBudget]
    skipped: list[dict[str, object]]
    errors: list[dict[str, object]]
    month_skipped: dict[str, int]
    closed_months: set[str]
    deleted: int = 0
    future: int = 0
    closed: int = 0
    invalid: int = 0


def _source_categories(rows: tuple[CategoryRow, ...]) -> OrderedDict[UUID, _SourceCategory]:
    sources: OrderedDict[UUID, _SourceCategory] = OrderedDict()
    for line, row in enumerate(rows, start=2):
        source_id = UUID(row.id)
        sources[source_id] = _SourceCategory(
            row=row,
            source_id=source_id,
            line=line,
            kind=row.kind,
            name=row.name,
            name_key=name_key(row.name),
            treatment=row.treatment,
            color=row.color,
            archived_at=parse_timestamp(row.archived_at) if row.archived_at else None,
        )
    return sources


def _choices(options: Mapping[str, object]) -> dict[UUID, _Choice]:
    raw = options.get("mappings")
    if not isinstance(raw, list):
        return {}
    choices: dict[UUID, _Choice] = {}
    for item in cast("list[object]", raw):
        if not isinstance(item, dict):
            continue
        mapping = cast("dict[str, object]", item)
        raw_source = mapping.get("source_id")
        if not isinstance(raw_source, str) or not raw_source:
            continue
        try:
            source_id = UUID(raw_source)
        except ValueError:
            continue
        action = str(mapping.get("action", ""))
        if action not in {"map", "create", "ignore"}:
            continue
        raw_category = mapping.get("category_id")
        category_id: UUID | None = None
        if isinstance(raw_category, str) and raw_category:
            try:
                category_id = UUID(raw_category)
            except ValueError:
                category_id = None
        choices[source_id] = _Choice(
            source_id=source_id,
            action=action,
            category_id=category_id,
            create_name=str(mapping.get("create_name", "")),
            create_treatment=str(mapping.get("create_treatment", "regular")),
            create_color=str(mapping.get("create_color", "")),
        )
    return choices


def _category_snapshots(categories: list[Category]) -> list[CategorySnapshot]:
    return [
        CategorySnapshot(
            id=category.id,
            kind=category.kind,
            name=category.name,
            name_key=category.name_key,
            treatment=category.treatment,
            color=category.color,
            sort_order=category.sort_order,
            archived=category.archived_at is not None,
        )
        for category in categories
    ]


def _category_values_match(source: _SourceCategory, category: Category) -> bool:
    return (
        category.kind == source.kind
        and category.name_key == source.name_key
        and category.treatment == source.treatment
        and (not source.color or category.color == source.color)
        and category.archived_at == source.archived_at
    )


def _resolve_categories(
    session: Session,
    ctx: Context,
    batch: ImportBatch,
    archive: ParsedArchive,
    *,
    sources: OrderedDict[UUID, _SourceCategory],
    apply: bool,
) -> _CategoryResolution:
    repository = CategoryRepository(session, ctx)
    existing = repository.ordered()
    lineage = repository.by_lineage_ids(list(sources))
    by_lineage: dict[UUID, Category] = {}
    for category in lineage:
        if category.import_key and category.import_key.startswith("id:"):
            with contextlib.suppress(ValueError):
                by_lineage[UUID(category.import_key[3:])] = category
        by_lineage[category.id] = category
    active = {
        (category.kind, category.name_key): category
        for category in existing
        if category.archived_at is None
    }
    saved = {
        (mapping.kind, mapping.source_name_key): mapping
        for mapping in ImportCategoryMappingRepository(session, ctx).list()
    }
    choices = _choices(batch.options)
    archive_ids = set(sources)
    snapshots = _category_snapshots(existing)
    next_orders: dict[str, int] = {}
    active_created: set[tuple[str, str]] = set()
    resolutions: dict[UUID, _Resolution] = {}
    report_rows: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    mapping_entries: list[dict[str, object]] = []
    mapping_errors: list[dict[str, object]] = []
    created = duplicates = conflicts = 0
    mapping_invalid = 0

    def create_category(
        source: _SourceCategory,
        *,
        name: str,
        treatment: str,
        color: str,
        error: str = "",
    ) -> _Resolution:
        nonlocal created
        archived = source.archived_at is not None
        resolution = _Resolution(
            source=source,
            action="create",
            target_id=None,
            target_name=name,
            target_archived=archived,
            target_treatment=treatment,
            create_name=name,
            create_treatment=treatment,
            create_color=color,
            error=error,
        )
        if error:
            resolution.action = "ignore"
            return resolution
        key = (source.kind, name_key(name))
        if not archived and key in active:
            resolution.action = "ignore"
            resolution.error = "category_exists"
            return resolution
        if not archived and key in active_created:
            resolution.action = "ignore"
            resolution.error = "duplicate_create_name"
            return resolution
        if apply:
            if source.kind not in next_orders:
                next_orders[source.kind] = repository.next_sort_order(source.kind)
            category = repository.create(
                name=name,
                name_key=name_key(name),
                kind=source.kind,
                treatment=treatment,
                color=color,
                sort_order=next_orders[source.kind],
                import_batch_id=batch.id,
                import_key=f"id:{source.source_id}",
                archived_at=source.archived_at,
            )
            next_orders[source.kind] += 1
            resolution.target_id = category.id
            resolution.created = True
        else:
            resolution.target_id = source.source_id
        created += 1
        if not archived:
            active_created.add(key)
        snapshots.append(
            CategorySnapshot(
                id=resolution.target_id or source.source_id,
                kind=source.kind,
                name=name,
                name_key=name_key(name),
                treatment=treatment,
                color=color,
                sort_order=0,
                archived=archived,
            )
        )
        return resolution

    for source in sources.values():
        choice = choices.get(source.source_id)
        if choice is not None and choice.action == "ignore":
            resolutions[source.source_id] = _Resolution(
                source=source,
                action="ignore",
                target_id=None,
                target_name="",
                target_archived=False,
                target_treatment="",
                create_name="",
                create_treatment="",
                create_color="",
            )
            continue
        if choice is not None and choice.action == "map":
            if choice.category_id is not None and choice.category_id in archive_ids:
                # Only a row's own archive id is accepted as "use the default target"; other
                # archive ids are not workspace categories until this batch creates them.
                if choice.category_id != source.source_id:
                    resolutions[source.source_id] = _Resolution(
                        source=source,
                        action="ignore",
                        target_id=None,
                        target_name="",
                        target_archived=False,
                        target_treatment="",
                        create_name="",
                        create_treatment="",
                        create_color="",
                        error="mapping_category_missing",
                    )
                    mapping_invalid += 1
                    mapping_errors.append(_entry("mapping_category_missing", column="category"))
                    continue
                choice = None
            else:
                target = (
                    next(
                        (category for category in existing if category.id == choice.category_id),
                        None,
                    )
                    if choice.category_id is not None
                    else None
                )
                if target is None:
                    resolutions[source.source_id] = _Resolution(
                        source=source,
                        action="ignore",
                        target_id=None,
                        target_name="",
                        target_archived=False,
                        target_treatment="",
                        create_name="",
                        create_treatment="",
                        create_color="",
                        error="mapping_category_missing",
                    )
                    mapping_invalid += 1
                    mapping_errors.append(_entry("mapping_category_missing", column="category"))
                    continue
                if target.kind != source.kind:
                    resolutions[source.source_id] = _Resolution(
                        source=source,
                        action="ignore",
                        target_id=None,
                        target_name="",
                        target_archived=False,
                        target_treatment="",
                        create_name="",
                        create_treatment="",
                        create_color="",
                        error="mapping_kind_mismatch",
                    )
                    mapping_invalid += 1
                    mapping_errors.append(_entry("mapping_kind_mismatch", column="category"))
                    continue
                resolutions[source.source_id] = _Resolution(
                    source=source,
                    action="map",
                    target_id=target.id,
                    target_name=target.name,
                    target_archived=target.archived_at is not None,
                    target_treatment=target.treatment,
                    create_name=source.name,
                    create_treatment=source.treatment,
                    create_color=source.color,
                )
                continue
        if choice is not None and choice.action == "create":
            error = ""
            try:
                name = clean_name(choice.create_name or source.name)
            except ValueError:
                name = source.name
                error = "invalid_category_name"
            try:
                treatment = Treatment(choice.create_treatment or source.treatment).value
            except ValueError:
                treatment = source.treatment
                error = error or "invalid_treatment"
            try:
                color = clean_color(choice.create_color) if choice.create_color else source.color
            except ValueError:
                color = source.color
                error = error or "invalid_color"
            resolution = create_category(
                source, name=name, treatment=treatment, color=color, error=error
            )
            resolutions[source.source_id] = resolution
            if resolution.error:
                mapping_invalid += 1
                mapping_errors.append(_entry(resolution.error, column="category"))
            continue

        target = by_lineage.get(source.source_id)
        if target is not None:
            resolution = _Resolution(
                source=source,
                action="map",
                target_id=target.id,
                target_name=target.name,
                target_archived=target.archived_at is not None,
                target_treatment=target.treatment,
                create_name=source.name,
                create_treatment=source.treatment,
                create_color=source.color,
            )
            if target.kind != source.kind:
                resolution.action = "ignore"
                resolution.error = "mapping_kind_mismatch"
                mapping_invalid += 1
                mapping_errors.append(
                    _entry(
                        "mapping_kind_mismatch",
                        file="categories.csv",
                        line=source.line,
                        column="category",
                    )
                )
            elif _category_values_match(source, target):
                duplicates += 1
                skipped.append(
                    _entry(
                        "already_imported",
                        file="categories.csv",
                        line=source.line,
                        column="category",
                    )
                )
            else:
                conflicts += 1
                skipped.append(
                    _entry(
                        "category_differs",
                        file="categories.csv",
                        line=source.line,
                        column="category",
                    )
                )
            resolutions[source.source_id] = resolution
            continue

        if source.archived_at is None:
            remembered = saved.get((source.kind, source.name_key))
            if remembered is not None:
                if remembered.action == "ignore":
                    resolutions[source.source_id] = _Resolution(
                        source=source,
                        action="ignore",
                        target_id=None,
                        target_name="",
                        target_archived=False,
                        target_treatment="",
                        create_name="",
                        create_treatment="",
                        create_color="",
                    )
                    continue
                target = next(
                    (category for category in existing if category.id == remembered.category_id),
                    None,
                )
                if target is None or target.kind != source.kind:
                    resolutions[source.source_id] = _Resolution(
                        source=source,
                        action="ignore",
                        target_id=None,
                        target_name="",
                        target_archived=False,
                        target_treatment="",
                        create_name="",
                        create_treatment="",
                        create_color="",
                        error=(
                            "mapping_kind_mismatch"
                            if target is not None
                            else "mapping_category_missing"
                        ),
                    )
                    mapping_invalid += 1
                    mapping_errors.append(
                        _entry(
                            "mapping_kind_mismatch"
                            if target is not None
                            else "mapping_category_missing",
                            column="category",
                        )
                    )
                    continue
                resolutions[source.source_id] = _Resolution(
                    source=source,
                    action="map",
                    target_id=target.id,
                    target_name=target.name,
                    target_archived=target.archived_at is not None,
                    target_treatment=target.treatment,
                    create_name=source.name,
                    create_treatment=source.treatment,
                    create_color=source.color,
                )
                continue
            target = active.get((source.kind, source.name_key))
            if target is not None:
                resolutions[source.source_id] = _Resolution(
                    source=source,
                    action="map",
                    target_id=target.id,
                    target_name=target.name,
                    target_archived=False,
                    target_treatment=target.treatment,
                    create_name=source.name,
                    create_treatment=source.treatment,
                    create_color=source.color,
                )
                if _category_values_match(source, target):
                    duplicates += 1
                    skipped.append(
                        _entry(
                            "already_imported",
                            file="categories.csv",
                            line=source.line,
                            column="category",
                        )
                    )
                else:
                    conflicts += 1
                    skipped.append(
                        _entry(
                            "category_differs",
                            file="categories.csv",
                            line=source.line,
                            column="category",
                        )
                    )
                continue

        resolution = create_category(
            source,
            name=source.name,
            treatment=source.treatment,
            color=source.color or next_color(source.kind, snapshots),
        )
        resolutions[source.source_id] = resolution
        if resolution.error:
            mapping_invalid += 1
            mapping_errors.append(_entry(resolution.error, column="category"))

    for source in sources.values():
        resolution = resolutions[source.source_id]
        action = resolution.action
        error = resolution.error
        if error:
            action = "ignore"
        if action != "ignore":
            report_rows.append(
                {
                    "kind": source.kind,
                    "category": resolution.target_name or source.name,
                    "total": "",
                    "rows_to_create": 1 if resolution.created else 0,
                }
            )
        mapping_entries.append(
            {
                "source_id": str(source.source_id),
                "kind": source.kind,
                "source_name": source.name,
                "source_name_key": source.name_key,
                "source_archived": source.archived_at is not None,
                "rows": source.rows,
                "total": _money(source.total_minor, str(archive.rows.workspace["base_currency"])),
                "action": action,
                "category_id": (
                    str(resolution.target_id) if action == "map" and resolution.target_id else ""
                ),
                "create_name": resolution.create_name or source.name,
                "create_treatment": resolution.create_treatment or source.treatment,
                "create_color": resolution.create_color or source.color,
                "target_name": resolution.target_name,
                "target_archived": resolution.target_archived,
                "error": error,
            }
        )

    # Never let a malformed archive (two active rows with one name) create a partial mapping:
    # the create helper reports the second as a duplicate. The first remains valid.
    return _CategoryResolution(
        resolutions=resolutions,
        report_rows=report_rows,
        skipped=skipped,
        mapping=mapping_entries,
        mapping_errors=mapping_errors,
        mapping_invalid=mapping_invalid,
        created=created,
        duplicates=duplicates,
        conflicts=conflicts,
    )


def _collect_sources(
    archive: ParsedArchive,
    *,
    sources: OrderedDict[UUID, _SourceCategory],
    currency: str,
    today: date,
    closed: frozenset[YearMonth],
    out_of_time: Callable[[], bool],
) -> _Collected:
    collected = _Collected(
        sources=sources,
        transactions=[],
        budget_lines=[],
        skipped=[],
        errors=[],
        month_skipped={},
        closed_months=set(),
    )
    for line, row in enumerate(archive.rows.transactions, start=2):
        if (line - 1) % DEADLINE_CHECK_ROWS == 0 and out_of_time():
            raise ParseTimeout
        try:
            source = sources[UUID(row.category_id)]
            day = parse_date(row.date)
            amount = parse_transaction_amount(row.amount, row.currency, decimal="dot")
        except EntryError as error:
            collected.invalid += 1
            collected.errors.append(_entry(error.code, file="transactions.csv", line=line))
            continue
        except (KeyError, ValueError):
            collected.invalid += 1
            collected.errors.append(_entry("invalid_row", file="transactions.csv", line=line))
            continue
        month = YearMonth.from_date(day)
        if row.deleted_at:
            collected.deleted += 1
            collected.skipped.append(
                _entry("deleted_in_source", file="transactions.csv", line=line)
            )
            collected.month_skipped[str(month)] = collected.month_skipped.get(str(month), 0) + 1
            continue
        if day > today:
            collected.future += 1
            collected.skipped.append(_entry("future_date", file="transactions.csv", line=line))
            collected.month_skipped[str(month)] = collected.month_skipped.get(str(month), 0) + 1
            continue
        if month in closed:
            collected.closed += 1
            collected.closed_months.add(str(month))
            collected.skipped.append(_entry("closed_month", file="transactions.csv", line=line))
            collected.month_skipped[str(month)] = collected.month_skipped.get(str(month), 0) + 1
            continue
        source.rows += 1
        source.total_minor += amount
        collected.transactions.append(_ParsedTransaction(row, source, line, day, amount))

    for line, row in enumerate(archive.rows.budget_lines, start=2):
        if (line - 1) % DEADLINE_CHECK_ROWS == 0 and out_of_time():
            raise ParseTimeout
        try:
            source = sources[UUID(row.category_id)]
            month = parse_month(row.month)
            planned = parse_planned_amount(row.planned, row.currency, decimal="dot")
        except EntryError as error:
            collected.invalid += 1
            collected.errors.append(_entry(error.code, file="budget_lines.csv", line=line))
            continue
        except (KeyError, ValueError):
            collected.invalid += 1
            collected.errors.append(_entry("invalid_row", file="budget_lines.csv", line=line))
            continue
        if planned == 0:
            continue
        if month in closed:
            collected.closed += 1
            collected.closed_months.add(str(month))
            collected.skipped.append(_entry("closed_month", file="budget_lines.csv", line=line))
            collected.month_skipped[str(month)] = collected.month_skipped.get(str(month), 0) + 1
            continue
        source.rows += 1
        source.total_minor += planned
        collected.budget_lines.append(_ParsedBudget(row, source, line, month, planned))
    return collected


def _merge_months(
    collected: _Collected,
    transactions: list[IncomingTransaction],
    transaction_matches: list[Any],
    budget_lines: list[IncomingBudgetLine],
    budget_matches: list[Any],
    currency: str,
) -> list[dict[str, object]]:
    months: dict[str, _MonthTotals] = {
        month: _MonthTotals(rows_skipped=count) for month, count in collected.month_skipped.items()
    }
    for incoming, match in zip(transactions, transaction_matches, strict=True):
        month = str(YearMonth.from_date(incoming.day))
        totals = months.setdefault(month, _MonthTotals())
        if match.action != "create":
            totals.rows_skipped += 1
            continue
        totals.rows_to_create += 1
        if incoming.direction == Kind.EXPENSE.value:
            totals.expense_minor += incoming.amount_minor
        else:
            totals.income_minor += incoming.amount_minor
    for incoming, match in zip(budget_lines, budget_matches, strict=True):
        month = str(incoming.month)
        totals = months.setdefault(month, _MonthTotals())
        if match.action != "create":
            totals.rows_skipped += 1
            continue
        totals.rows_to_create += 1
        totals.planned_minor += incoming.planned_minor
    return [
        {
            "month": month,
            "expense_total": _money(totals.expense_minor, currency),
            "income_total": _money(totals.income_minor, currency),
            "planned_total": _money(totals.planned_minor, currency),
            "rows_to_create": totals.rows_to_create,
            "rows_skipped": totals.rows_skipped,
        }
        for month, totals in sorted(months.items())
    ]


def _merge_categories(
    resolution: _CategoryResolution,
    transactions: list[IncomingTransaction],
    transaction_matches: list[Any],
    budget_lines: list[IncomingBudgetLine],
    budget_matches: list[Any],
    currency: str,
) -> list[dict[str, object]]:
    targets = {
        item.target_id: (item.source.kind, item.target_name)
        for item in resolution.resolutions.values()
        if item.target_id is not None
    }
    totals: dict[tuple[str, str], _CategoryTotals] = {}
    for row in resolution.report_rows:
        kind = str(row["kind"])
        name = str(row["category"])
        key = (kind, name_key(name))
        entry = totals.setdefault(key, _CategoryTotals(kind, name))
        entry.rows_to_create += int(cast("int", row["rows_to_create"]))
    for incoming, match in zip(transactions, transaction_matches, strict=True):
        if match.action != "create":
            continue
        kind, target_name = targets.get(
            incoming.category_id, (incoming.direction, incoming.source_category_name_key)
        )
        key = (kind, name_key(target_name))
        entry = totals.setdefault(key, _CategoryTotals(kind, target_name))
        entry.total_minor += incoming.amount_minor
        entry.rows_to_create += 1
    for incoming, match in zip(budget_lines, budget_matches, strict=True):
        if match.action != "create":
            continue
        kind, target_name = targets.get(incoming.category_id, ("expense", ""))
        if not target_name:
            continue
        key = (kind, name_key(target_name))
        entry = totals.setdefault(key, _CategoryTotals(kind, target_name))
        entry.total_minor += incoming.planned_minor
        entry.rows_to_create += 1
    return [
        {
            "kind": totals[key].kind,
            "category": totals[key].name,
            "total": _money(totals[key].total_minor, currency),
            "rows_to_create": totals[key].rows_to_create,
        }
        for key in sorted(totals)
    ]


def _merge(
    session: Session,
    ctx: Context,
    batch: ImportBatch,
    archive: ParsedArchive,
    *,
    base_currency: str,
    apply: bool,
    out_of_time: Callable[[], bool],
) -> tuple[dict[str, Any], dict[str, int]]:
    archive_currency = str(archive.rows.workspace["base_currency"])
    currency = archive_currency
    if archive_currency != base_currency:
        report = _empty_report(
            counts=_restore_counts(invalid=1),
            refused=True,
            errors=[_entry("currency_mismatch", file="manifest.json")],
        )
        return report, {"created": 0, "duplicates": 0, "conflicts": 0, "skipped": 1}
    closed = MonthStateRepository(session, ctx).closed_months()
    today = ctx.clock.today(ctx.workspace.timezone)
    sources = _source_categories(archive.rows.categories)
    collected = _collect_sources(
        archive,
        sources=sources,
        currency=currency,
        today=today,
        closed=closed,
        out_of_time=out_of_time,
    )
    category_resolution = _resolve_categories(
        session, ctx, batch, archive, sources=sources, apply=apply
    )
    incoming_transactions: list[IncomingTransaction] = []
    ignored = 0
    for parsed in collected.transactions:
        resolution = category_resolution.resolutions[parsed.source.source_id]
        if resolution.action == "ignore" or resolution.error or resolution.target_id is None:
            ignored += 1
            month = str(YearMonth.from_date(parsed.day))
            collected.month_skipped[month] = collected.month_skipped.get(month, 0) + 1
            collected.skipped.append(
                _entry("ignored_category", file="transactions.csv", line=parsed.line)
            )
            continue
        incoming_transactions.append(
            IncomingTransaction(
                line=parsed.line,
                source_id=UUID(parsed.row.id) if parsed.row.id else None,
                day=parsed.day,
                direction=parsed.row.direction,
                amount_minor=parsed.amount_minor,
                currency=parsed.row.currency,
                category_id=resolution.target_id,
                source_category_name_key=parsed.source.name_key,
                description=parsed.row.description,
                description_key=description_key(parsed.row.description),
            )
        )
    transaction_matches = match_transactions(session, ctx, incoming_transactions)

    incoming_lines: list[IncomingBudgetLine] = []
    for parsed in collected.budget_lines:
        resolution = category_resolution.resolutions[parsed.source.source_id]
        if resolution.action == "ignore" or resolution.error or resolution.target_id is None:
            ignored += 1
            month = str(parsed.month)
            collected.month_skipped[month] = collected.month_skipped.get(month, 0) + 1
            collected.skipped.append(
                _entry("ignored_category", file="budget_lines.csv", line=parsed.line)
            )
            continue
        if resolution.target_archived:
            collected.skipped.append(
                _entry("category_archived", file="budget_lines.csv", line=parsed.line)
            )
            month = str(parsed.month)
            collected.month_skipped[month] = collected.month_skipped.get(month, 0) + 1
            continue
        if resolution.target_treatment == Treatment.TRANSFER.value:
            collected.skipped.append(
                _entry("category_transfer", file="budget_lines.csv", line=parsed.line)
            )
            month = str(parsed.month)
            collected.month_skipped[month] = collected.month_skipped.get(month, 0) + 1
            continue
        incoming_lines.append(
            IncomingBudgetLine(
                line=parsed.line,
                month=parsed.month,
                category_id=resolution.target_id,
                planned_minor=parsed.planned_minor,
            )
        )
    budget_matches = match_budget_lines(session, ctx, incoming_lines)

    tx_created = sum(match.action == "create" for match in transaction_matches)
    tx_duplicates = sum(match.action == "duplicate" for match in transaction_matches)
    tx_conflicts = sum(match.action == "conflict" for match in transaction_matches)
    line_created = sum(match.action == "create" for match in budget_matches)
    line_duplicates = sum(match.action == "duplicate" for match in budget_matches)
    line_conflicts = sum(match.action == "conflict" for match in budget_matches)
    if apply:
        transactions = TransactionRepository(session, ctx)
        for match in transaction_matches:
            if match.action != "create":
                continue
            row = match.row
            transactions.create_imported(
                direction=row.direction,
                day=row.day,
                amount_minor=row.amount_minor,
                currency=row.currency,
                category_id=row.category_id,
                description=row.description,
                description_key=row.description_key,
                import_batch_id=batch.id,
                import_key=match.import_key,
                created_by_user_id=ctx.user.id,
            )
        lines = BudgetLineRepository(session, ctx)
        affected: set[YearMonth] = set()
        for match in budget_matches:
            if match.action != "create":
                continue
            lines.create(
                month=match.row.month,
                category_id=match.row.category_id,
                planned_minor=match.row.planned_minor,
                currency=currency,
                import_batch_id=batch.id,
            )
            affected.add(match.row.month)
        if affected:
            months = MonthStateRepository(session, ctx)
            now = ctx.clock.now()
            stamped: list[str] = []
            for month in sorted(affected):
                state = months.for_month(month) or months.create(month)
                if state.plan_started_at is None:
                    months.update(state.id, plan_started_at=now)
                    stamped.append(str(month))
            if stamped:
                batch.report = {**(batch.report or {}), "plan_started_months": stamped}
        if batch.options.get("remember_mappings"):
            mappings = ImportCategoryMappingRepository(session, ctx)
            for resolution in category_resolution.resolutions.values():
                if resolution.source.archived_at is not None:
                    continue
                if resolution.action == "map" and resolution.target_id is not None:
                    mappings.upsert(
                        kind=resolution.source.kind,
                        source_name_key=resolution.source.name_key,
                        action="map",
                        category_id=resolution.target_id,
                    )
                elif resolution.action == "ignore" and not resolution.error:
                    mappings.upsert(
                        kind=resolution.source.kind,
                        source_name_key=resolution.source.name_key,
                        action="ignore",
                        category_id=None,
                    )
        session.flush()

    months_report = _merge_months(
        collected,
        incoming_transactions,
        transaction_matches,
        incoming_lines,
        budget_matches,
        currency,
    )
    category_report = _merge_categories(
        category_resolution,
        incoming_transactions,
        transaction_matches,
        incoming_lines,
        budget_matches,
        currency,
    )
    skipped = [
        *collected.skipped,
        *category_resolution.skipped,
        *[
            _entry(match.code or "duplicate", file="transactions.csv", line=match.row.line)
            for match in transaction_matches
            if match.action != "create"
        ],
        *[
            _entry(match.code or "duplicate", file="budget_lines.csv", line=match.row.line)
            for match in budget_matches
            if match.action != "create"
        ],
    ]
    closed_months = sorted(collected.closed_months)
    errors = [*collected.errors, *category_resolution.mapping_errors]
    counts = _restore_counts(
        to_create=category_resolution.created + tx_created + line_created,
        duplicates_skipped=category_resolution.duplicates + tx_duplicates + line_duplicates,
        conflicts_skipped=category_resolution.conflicts + tx_conflicts + line_conflicts,
        closed_months_skipped=collected.closed,
        future_dates_skipped=collected.future,
        deleted_skipped=collected.deleted,
        ignored_categories_skipped=ignored,
        archived_categories_skipped=sum(
            entry["code"] == "category_archived" for entry in collected.skipped
        ),
        transfer_categories_skipped=sum(
            entry["code"] == "category_transfer" for entry in collected.skipped
        ),
        invalid=collected.invalid,
        mapping_invalid=category_resolution.mapping_invalid,
        categories_created=category_resolution.created,
        transactions_created=tx_created,
        budget_lines_created=line_created,
    )
    workspace = CurrentWorkspaceRepository(session, ctx).get()
    settings_diff = _settings_diff(workspace, _archive_setting_values(archive.rows.workspace))
    if _risk_policy_tuple(archive.rows.risk_target_policy) != _current_risk_policy(session, ctx):
        settings_diff.append("risk_target_policy")
    archive_closed = sorted(row.month for row in archive.rows.months if row.closed_at)
    warnings: list[dict[str, object]] = []
    if any(name in archive.files for name in NETWORTH_CSV_FILES):
        warnings.append(_entry("networth_restore_only", file="archive"))
    if any(name in archive.files for name in INVESTMENT_CSV_FILES):
        warnings.append(_entry("investment_restore_only", file="archive"))
    report = _empty_report(
        counts=counts,
        months=months_report,
        categories=category_report,
        mapping=category_resolution.mapping,
        mapping_errors=category_resolution.mapping_errors,
        closed_months=closed_months,
        warnings=warnings,
        skipped=skipped,
        errors=errors,
        archive_closed_months=archive_closed,
        settings_diff=settings_diff,
    )
    return report, {
        "created": tx_created,
        "duplicates": tx_duplicates,
        "conflicts": tx_conflicts,
        "skipped": len(skipped) + len(errors),
        "categories_created": category_resolution.created,
        "budget_lines_created": line_created,
    }


# --- restore ----------------------------------------------------------------


def _restore_report(
    session: Session,
    ctx: Context,
    archive: ParsedArchive,
    *,
    settings: _RestoreSettings | None,
    errors: list[dict[str, object]],
) -> dict[str, Any]:
    removed = (
        suggested_categories_to_remove(session, ctx) if workspace_is_empty(session, ctx) else []
    )
    removed_networth = (
        suggested_networth_categories_to_remove(session, ctx)
        if workspace_is_empty(session, ctx)
        else []
    )
    removed_assets = (
        suggested_asset_classes_to_remove(session, ctx) if workspace_is_empty(session, ctx) else []
    )
    counts = _restore_counts_for(
        archive.rows,
        removed=len(removed),
        removed_networth=len(removed_networth),
        removed_assets=len(removed_assets),
    )
    counts["invalid"] = len(errors)
    asset_class_ids, instrument_ids = _archived_link_targets(archive)
    warnings = [
        _entry(
            "link_target_missing",
            file="networth_items.csv",
            line=line,
            column="link_id",
        )
        for line, row in enumerate(archive.rows.networth_items, start=2)
        if _link_target_missing(row, asset_class_ids, instrument_ids)
    ]
    warnings.extend(
        _provider_key_warning(row.provider_key, line)
        for line, row in enumerate(archive.rows.price_source_configs, start=2)
        if row.enabled == "true"
        and (definition := PROVIDERS.get(row.provider_key)) is not None
        and definition.requires_key
    )
    report = _empty_report(
        counts=counts,
        errors=errors,
        warnings=warnings,
        restore_months=[row.month for row in archive.rows.months],
        archive_closed_months=sorted(row.month for row in archive.rows.months if row.closed_at),
        suggested_categories_removed=[category.name for category in removed],
        suggested_networth_categories_removed=[category.name for category in removed_networth],
        suggested_asset_classes_removed=[asset_class.name for asset_class in removed_assets],
        settings_diff=(
            _settings_diff(CurrentWorkspaceRepository(session, ctx).get(), settings.values())
            if settings is not None
            else []
        ),
    )
    return report


def _provider_key_warning(provider_key: str, line: int) -> dict[str, object]:
    entry = _entry(
        "provider_key_required",
        file="price_source_configs.csv",
        line=line,
        column="provider_key",
    )
    entry["provider"] = provider_key
    return entry


def _plan_restore(
    session: Session,
    ctx: Context,
    parsed: ParsedUpload,
    *,
    out_of_time: Callable[[], bool],
) -> dict[str, Any]:
    archive = _archive(parsed)
    errors: list[dict[str, object]] = []
    if not workspace_is_empty(session, ctx):
        errors.append(_entry("workspace_not_empty", file="archive"))
    settings, settings_errors = _restore_settings(archive)
    errors.extend(settings_errors)
    duplicates = _duplicate_active_categories(archive.rows.categories)
    for _duplicate in sorted(duplicates):
        errors.append(_entry("duplicate_category_name", file="categories.csv", column="name"))
    for number, _row in enumerate(archive.rows.categories, start=1):
        if number % DEADLINE_CHECK_ROWS == 0 and out_of_time():
            raise ParseTimeout
    for number, _row in enumerate(archive.rows.transactions, start=1):
        if number % DEADLINE_CHECK_ROWS == 0 and out_of_time():
            raise ParseTimeout
    for number, _row in enumerate(archive.rows.budget_lines, start=1):
        if number % DEADLINE_CHECK_ROWS == 0 and out_of_time():
            raise ParseTimeout
    for rows in (
        archive.rows.asset_classes,
        archive.rows.instruments,
        archive.rows.instrument_allocations,
        archive.rows.investment_transactions,
        archive.rows.bond_terms,
        archive.rows.price_points,
        archive.rows.fx_rates,
        archive.rows.price_source_configs,
    ):
        for number, _row in enumerate(rows, start=1):
            if number % DEADLINE_CHECK_ROWS == 0 and out_of_time():
                raise ParseTimeout
    return _restore_report(session, ctx, archive, settings=settings, errors=errors)


def _restore_breakdown(raw: str, category_ids: Mapping[UUID, UUID]) -> dict[str, object]:
    """The archived breakdown with lineage category ids mapped to the restored ones."""
    loaded: object = json.loads(raw)
    data: dict[str, object] = {}
    if isinstance(loaded, dict):
        source = cast("Mapping[object, object]", loaded)
        data = {str(key): value for key, value in source.items()}
    categories = data.get("categories")
    if isinstance(categories, list):
        converted: list[object] = []
        for entry in cast("list[object]", categories):
            if not isinstance(entry, Mapping):
                converted.append(entry)
                continue
            row = dict(cast("Mapping[str, object]", entry))
            raw_id = row.get("category_id")
            if isinstance(raw_id, str):
                with contextlib.suppress(ValueError):
                    source_id = UUID(raw_id)
                    row["category_id"] = str(category_ids.get(source_id, source_id))
            converted.append(row)
        data["categories"] = converted
    return data


def _archived_link_targets(archive: ParsedArchive) -> tuple[set[UUID], set[UUID]]:
    """Lineage ids of the portfolio targets this archive can resolve (F6.9)."""
    return (
        {UUID(row.id) for row in archive.rows.asset_classes},
        {UUID(row.id) for row in archive.rows.instruments},
    )


def _link_target_missing(
    row: NetWorthItemRow,
    asset_class_ids: set[UUID],
    instrument_ids: set[UUID],
) -> bool:
    if row.valuation_mode != "linked":
        return False
    link_type = row.link_type
    raw_id = row.link_id
    if not isinstance(raw_id, str):
        return True
    try:
        target = UUID(raw_id)
    except ValueError:
        return True
    if link_type == "portfolio_asset_class":
        return target not in asset_class_ids
    if link_type == "portfolio_holding":
        return target not in instrument_ids
    return True


def _resolve_networth_links(
    session: Session,
    ctx: Context,
    archive: ParsedArchive,
    item_ids: Mapping[UUID, UUID],
    asset_class_ids: Mapping[UUID, UUID],
    instrument_ids: Mapping[UUID, UUID],
) -> int:
    """Point restored net-worth items at the portfolio rows; count missing targets."""
    repository = NetWorthItemRepository(session, ctx)
    missing = 0
    for row in archive.rows.networth_items:
        if row.valuation_mode != "linked":
            continue
        target: UUID | None = None
        if row.link_type == "portfolio_asset_class" and row.link_id:
            target = asset_class_ids.get(UUID(row.link_id))
        elif row.link_type == "portfolio_holding" and row.link_id:
            target = instrument_ids.get(UUID(row.link_id))
        if target is None:
            missing += 1
            continue
        repository.update(
            item_ids[UUID(row.id)],
            valuation_mode="linked",
            link_type=row.link_type,
            link_id=target,
        )
    return missing


def _restore_networth(
    session: Session,
    ctx: Context,
    batch: ImportBatch,
    archive: ParsedArchive,
    *,
    restore_now: datetime,
    removed: list[NetWorthCategory],
) -> dict[UUID, UUID]:
    """Recreate categories, items, valuations and snapshots after the ledger rows."""
    categories_repository = NetWorthCategoryRepository(session, ctx)
    for category in removed:
        categories_repository.delete(category)
    category_ids: dict[UUID, UUID] = {}
    for index, row in enumerate(archive.rows.networth_categories):
        category = categories_repository.create(
            name=row.name,
            name_key=name_key(row.name),
            side=row.side,
            color=row.color,
            sort_order=row.sort_order,
            import_batch_id=batch.id,
            import_key=f"id:{row.id}",
            archived_at=parse_timestamp(row.archived_at) if row.archived_at else None,
            created_at=restore_now + timedelta(microseconds=index),
        )
        category_ids[UUID(row.id)] = category.id

    items_repository = NetWorthItemRepository(session, ctx)
    item_ids: dict[UUID, UUID] = {}
    for index, row in enumerate(archive.rows.networth_items):
        is_linked = row.valuation_mode == "linked"
        item = items_repository.create(
            side=row.side,
            category_id=category_ids[UUID(row.category_id)],
            name=row.name,
            currency=row.currency,
            notes=row.notes,
            acquired_on=parse_date(row.acquired_on) if row.acquired_on else None,
            valuation_mode="manual" if is_linked else row.valuation_mode,
            link_type=None,
            link_id=None,
            include_in_net_worth=row.include_in_net_worth == "true",
            investable=row.investable == "true",
            estimate_between=(
                row.estimate_between == "true" if row.estimate_between else row.side == "asset"
            ),
            archived_at=parse_timestamp(row.archived_at) if row.archived_at else None,
            import_batch_id=batch.id,
            import_key=f"id:{row.id}",
            created_at=restore_now + timedelta(microseconds=index),
        )
        item_ids[UUID(row.id)] = item.id

    valuations = ItemValuationRepository(session, ctx)
    for index, row in enumerate(archive.rows.item_valuations):
        created_at = (
            parse_timestamp(row.created_at)
            if row.created_at
            else restore_now + timedelta(microseconds=index)
        )
        valuations.create(
            item_id=item_ids[UUID(row.item_id)],
            as_of=parse_date(row.as_of),
            value_minor=parse_planned_amount(row.value, row.currency, decimal="dot"),
            currency=row.currency,
            note=row.note,
            import_batch_id=batch.id,
            import_key=f"id:{row.id}",
            created_at=created_at,
        )

    snapshots = NetWorthSnapshotRepository(session, ctx)
    for row in archive.rows.networth_snapshots:
        snapshots.create(
            as_of=parse_date(row.as_of),
            assets_minor=parse_planned_amount(row.assets, row.currency, decimal="dot"),
            liabilities_minor=parse_planned_amount(row.liabilities, row.currency, decimal="dot"),
            currency=row.currency,
            breakdown=_restore_breakdown(row.breakdown, category_ids),
            source=row.source,
            import_batch_id=batch.id,
            import_key=f"id:{row.id}",
        )
    return item_ids


def _money_minor(value: str, currency: str, *, allow_zero: bool) -> int:
    text = value or "0"
    number = Decimal(text)
    minor = number.scaleb(minor_unit_for(currency))
    if minor != minor.to_integral_value():
        raise ImportServiceError("the archive has an amount with too many decimals")
    result = int(minor)
    if result == 0 and not allow_zero:
        raise ImportServiceError("the archive has a non-positive amount")
    return result


def _restore_investments(
    session: Session,
    ctx: Context,
    batch: ImportBatch,
    archive: ParsedArchive,
    *,
    restore_now: datetime,
    removed: list[AssetClass],
) -> tuple[dict[str, int], dict[UUID, UUID], dict[UUID, UUID]]:
    """Recreate classes, instruments, holdings, prices, FX and provider settings."""
    classes = AssetClassRepository(session, ctx)
    for asset_class in removed:
        classes.delete(asset_class)
    asset_class_ids: dict[UUID, UUID] = {}
    for index, row in enumerate(archive.rows.asset_classes):
        asset_class = classes.create(
            name=row.name,
            name_key=name_key(row.name),
            investable=row.investable == "true",
            color=row.color,
            sort_order=row.sort_order,
            risk_tier=row.risk_tier or None,
            archived_at=parse_timestamp(row.archived_at) if row.archived_at else None,
            import_key=f"id:{row.id}",
            import_batch_id=batch.id,
            created_at=restore_now + timedelta(microseconds=index),
        )
        asset_class_ids[UUID(row.id)] = asset_class.id

    instruments_repo = InstrumentRepository(session, ctx)
    instrument_ids: dict[UUID, UUID] = {}
    for index, row in enumerate(archive.rows.instruments):
        instrument = instruments_repo.create(
            name=row.name,
            name_key=name_key(row.name),
            symbol=row.symbol or None,
            type=row.type,
            quote_currency=row.quote_currency,
            unit_label=row.unit_label,
            valuation_source=row.valuation_source,
            provider_key=row.provider_key or None,
            provider_symbol=row.provider_symbol or None,
            provider_backfilled_at=(
                parse_timestamp(row.provider_backfilled_at) if row.provider_backfilled_at else None
            ),
            risk_tier_override=row.risk_tier_override or None,
            archived_at=parse_timestamp(row.archived_at) if row.archived_at else None,
            import_key=f"id:{row.id}",
            import_batch_id=batch.id,
            created_at=restore_now + timedelta(microseconds=index),
        )
        instrument_ids[UUID(row.id)] = instrument.id

    allocations = InstrumentAllocationRepository(session, ctx)
    for index, row in enumerate(archive.rows.instrument_allocations):
        allocations.create(
            instrument_id=instrument_ids[UUID(row.instrument_id)],
            asset_class_id=asset_class_ids[UUID(row.asset_class_id)],
            weight_bp=row.weight_bp,
            import_key=f"id:{row.id}",
            import_batch_id=batch.id,
            created_at=restore_now + timedelta(microseconds=index),
        )

    transactions = InvestmentTransactionRepository(session, ctx)
    for index, row in enumerate(archive.rows.investment_transactions):
        transactions.create(
            instrument_id=instrument_ids[UUID(row.instrument_id)],
            date=parse_date(row.date) if row.date else None,
            type=row.type,
            quantity=Decimal(row.quantity),
            unit_price=Decimal(row.unit_price),
            quote_currency=row.quote_currency,
            fee_minor=_money_minor(row.fee, row.quote_currency, allow_zero=True),
            fx_to_base=Decimal(row.fx_to_base) if row.fx_to_base else None,
            note=row.note,
            settlement_currency=row.settlement_currency or None,
            settlement_unit_price=(
                Decimal(row.settlement_unit_price) if row.settlement_unit_price else None
            ),
            settlement_fee_minor=(
                _money_minor(row.settlement_fee, row.settlement_currency, allow_zero=True)
                if row.settlement_currency
                else None
            ),
            import_batch_id=batch.id,
            import_key=f"id:{row.id}",
            created_at=restore_now + timedelta(microseconds=index),
        )

    bonds = BondTermsRepository(session, ctx)
    for index, row in enumerate(archive.rows.bond_terms):
        instrument = next(item for item in archive.rows.instruments if item.id == row.instrument_id)
        bonds.create(
            instrument_id=instrument_ids[UUID(row.instrument_id)],
            series_code=row.series_code or None,
            face_value_minor=_money_minor(
                row.face_value, instrument.quote_currency, allow_zero=False
            ),
            maturity_date=parse_date(row.maturity_date),
            interest_mode=row.interest_mode,
            rate_schedule=json.loads(row.rate_schedule),
            tax_rate_bp=row.tax_rate_bp,
            early_redemption_fee_mode=row.early_redemption_fee_mode or None,
            early_redemption_fee_rate_bp=row.early_redemption_fee_rate_bp,
            early_redemption_fee_minor=(
                _money_minor(
                    row.early_redemption_fee,
                    instrument.quote_currency,
                    allow_zero=True,
                )
                if row.early_redemption_fee_mode == "fixed"
                else None
            ),
            import_key=f"id:{row.id}",
            import_batch_id=batch.id,
            created_at=restore_now + timedelta(microseconds=index),
        )

    prices = PricePointRepository(session, ctx)
    for index, row in enumerate(archive.rows.price_points):
        prices.create(
            instrument_id=instrument_ids[UUID(row.instrument_id)],
            date=parse_date(row.date),
            price=Decimal(row.price),
            source=row.source,
            provider_key=row.provider_key or None,
            import_key=f"id:{row.id}",
            import_batch_id=batch.id,
            created_at=restore_now + timedelta(microseconds=index),
        )

    rates = FxRateRepository(session, ctx)
    for index, row in enumerate(archive.rows.fx_rates):
        rates.create(
            base_currency=row.base_currency,
            quote_currency=row.quote_currency,
            date=parse_date(row.date),
            rate=Decimal(row.rate),
            source=row.source,
            import_key=f"id:{row.id}",
            import_batch_id=batch.id,
            created_at=restore_now + timedelta(microseconds=index),
        )

    configs = PriceSourceConfigRepository(session, ctx)
    for index, row in enumerate(archive.rows.price_source_configs):
        definition = PROVIDERS.get(row.provider_key)
        requires_key = definition is not None and definition.requires_key
        configs.create(
            provider_key=row.provider_key,
            enabled=row.enabled == "true" and not requires_key,
            settings_encrypted=None,
            fx_backfilled=row.fx_backfilled or None,
            daily_call_budget=row.daily_call_budget,
            import_key=f"id:{row.id}",
            import_batch_id=batch.id,
            created_at=restore_now + timedelta(microseconds=index),
        )

    return (
        {
            "asset_classes_created": len(archive.rows.asset_classes),
            "instruments_created": len(archive.rows.instruments),
            "instrument_allocations_created": len(archive.rows.instrument_allocations),
            "investment_transactions_created": len(archive.rows.investment_transactions),
            "bond_terms_created": len(archive.rows.bond_terms),
            "price_points_created": len(archive.rows.price_points),
            "fx_rates_created": len(archive.rows.fx_rates),
            "price_source_configs_created": len(archive.rows.price_source_configs),
        },
        asset_class_ids,
        instrument_ids,
    )


def _policy_values(row: RiskTargetPolicy) -> dict[str, object]:
    return {
        "mode": row.mode,
        "safe_bp": row.safe_bp,
        "growth_bp": row.growth_bp,
        "speculative_bp": row.speculative_bp,
        "tolerance_bp": row.tolerance_bp,
    }


def _apply_restore_risk_policy(
    session: Session,
    ctx: Context,
    batch: ImportBatch,
    archive: ParsedArchive,
    *,
    restore_now: datetime,
) -> int:
    """Replace the workspace's policy and remember the previous one for rollback."""
    repository = RiskTargetPolicyRepository(session, ctx)
    current = repository.for_workspace(for_update=True)
    previous = _policy_values(current) if current is not None else None
    batch.previous_settings = {
        **(batch.previous_settings or {}),
        "risk_target_policy": previous,
    }
    policy = archive.rows.risk_target_policy
    if policy is None:
        if current is not None:
            repository.delete(current)
        return 0
    if current is None:
        repository.create(
            mode=policy.mode,
            safe_bp=policy.safe_bp,
            growth_bp=policy.growth_bp,
            speculative_bp=policy.speculative_bp,
            tolerance_bp=policy.tolerance_bp,
            import_batch_id=batch.id,
            created_at=restore_now,
        )
    else:
        repository.update(
            current.id,
            mode=policy.mode,
            safe_bp=policy.safe_bp,
            growth_bp=policy.growth_bp,
            speculative_bp=policy.speculative_bp,
            tolerance_bp=policy.tolerance_bp,
            import_batch_id=batch.id,
        )
    return 1


def _apply_restore(
    session: Session,
    ctx: Context,
    batch: ImportBatch,
    parsed: ParsedUpload,
) -> dict[str, int]:
    archive = _archive(parsed)
    if not workspace_is_empty(session, ctx):
        raise ImportRestoreUnavailable
    settings, errors = _restore_settings(archive)
    if settings is None or errors:
        raise ImportServiceError("the archive's workspace settings are invalid")
    duplicates = _duplicate_active_categories(archive.rows.categories)
    if duplicates:
        raise ImportServiceError("the archive has duplicate active category names")
    currency = str(archive.rows.workspace["base_currency"])
    workspace = CurrentWorkspaceRepository(session, ctx).get()
    settings_diff = _settings_diff(workspace, settings.values())
    batch.previous_settings = {
        field: getattr(workspace, field) for field in RESTORE_SETTINGS_FIELDS
    }
    CurrentWorkspaceRepository(session, ctx).update(**settings.values())
    removed = suggested_categories_to_remove(session, ctx)
    removed_networth = suggested_networth_categories_to_remove(session, ctx)
    removed_assets = suggested_asset_classes_to_remove(session, ctx)
    categories = CategoryRepository(session, ctx)
    for category in removed:
        categories.delete(category)
    restore_now = ctx.clock.now()
    category_ids: dict[UUID, UUID] = {}
    for row in archive.rows.categories:
        category = categories.create(
            name=row.name,
            name_key=name_key(row.name),
            kind=row.kind,
            treatment=row.treatment,
            color=row.color,
            sort_order=int(row.sort_order),
            import_batch_id=batch.id,
            import_key=f"id:{row.id}",
            archived_at=parse_timestamp(row.archived_at) if row.archived_at else None,
        )
        category_ids[UUID(row.id)] = category.id
    lines = BudgetLineRepository(session, ctx)
    created_lines = 0
    for row in archive.rows.budget_lines:
        planned = parse_planned_amount(row.planned, row.currency, decimal="dot")
        if planned == 0:
            continue
        lines.create(
            month=parse_month(row.month),
            category_id=category_ids[UUID(row.category_id)],
            planned_minor=planned,
            currency=currency,
            import_batch_id=batch.id,
        )
        created_lines += 1
    transactions = TransactionRepository(session, ctx)
    total = len(archive.rows.transactions)
    base = restore_now - timedelta(microseconds=total)
    deleted = 0
    for index, row in enumerate(archive.rows.transactions):
        if row.deleted_at:
            deleted += 1
        transactions.create_restored(
            direction=row.direction,
            day=parse_date(row.date),
            amount_minor=parse_transaction_amount(row.amount, row.currency, decimal="dot"),
            currency=currency,
            category_id=category_ids[UUID(row.category_id)],
            description=row.description,
            description_key=description_key(row.description),
            import_batch_id=batch.id,
            import_key=f"id:{row.id}" if row.id else None,
            created_by_user_id=ctx.user.id,
            created_at=base + timedelta(microseconds=index),
            deleted_at=parse_timestamp(row.deleted_at) if row.deleted_at else None,
        )
    months = MonthStateRepository(session, ctx)
    for row in archive.rows.months:
        months.create_restored(
            month=parse_month(row.month),
            closed_at=parse_timestamp(row.closed_at) if row.closed_at else None,
            closed_by_user_id=ctx.user.id if row.closed_at else None,
            plan_started_at=parse_timestamp(row.plan_started_at) if row.plan_started_at else None,
            created_at=restore_now,
            updated_at=restore_now,
        )
    networth_item_ids = _restore_networth(
        session,
        ctx,
        batch,
        archive,
        restore_now=restore_now,
        removed=removed_networth,
    )
    investment_counts, asset_class_ids, instrument_ids = _restore_investments(
        session,
        ctx,
        batch,
        archive,
        restore_now=restore_now,
        removed=removed_assets,
    )
    investment_counts["risk_policy_restored"] = _apply_restore_risk_policy(
        session, ctx, batch, archive, restore_now=restore_now
    )
    linked_items = _resolve_networth_links(
        session,
        ctx,
        archive,
        networth_item_ids,
        asset_class_ids,
        instrument_ids,
    )
    session.flush()
    AuditEventRepository(session, ctx).record(
        actor_user_id=ctx.user.id,
        action="workspace.restored",
        entity_type="workspace",
        entity_id=str(ctx.workspace.id),
        ts=restore_now,
        data={
            "batch_id": str(batch.id),
            "categories": len(archive.rows.categories),
            "transactions": len(archive.rows.transactions),
            "budget_lines": created_lines,
            "months": len(archive.rows.months),
            "networth_categories": len(archive.rows.networth_categories),
            "networth_items": len(archive.rows.networth_items),
            "item_valuations": len(archive.rows.item_valuations),
            "networth_snapshots": len(archive.rows.networth_snapshots),
            "linked_items_unlinked": linked_items,
            "asset_classes": len(archive.rows.asset_classes),
            "instruments": len(archive.rows.instruments),
            "instrument_allocations": len(archive.rows.instrument_allocations),
            "investment_transactions": len(archive.rows.investment_transactions),
            "bond_terms": len(archive.rows.bond_terms),
            "price_points": len(archive.rows.price_points),
            "fx_rates": len(archive.rows.fx_rates),
            "price_source_configs": len(archive.rows.price_source_configs),
            "risk_policy": archive.rows.risk_target_policy is not None,
            "settings_fields": ",".join(settings_diff),
            "suggested_categories_removed": len(removed),
            "suggested_networth_categories_removed": len(removed_networth),
            "suggested_asset_classes_removed": len(removed_assets),
        },
    )
    return {
        "created": len(archive.rows.transactions),
        "duplicates": 0,
        "conflicts": 0,
        "skipped": 0,
        "categories_created": len(archive.rows.categories),
        "transactions_created": len(archive.rows.transactions),
        "budget_lines_created": created_lines,
        "months_created": len(archive.rows.months),
        "deleted_transactions": deleted,
        "suggested_categories_removed": len(removed),
        "suggested_networth_categories_removed": len(removed_networth),
        "suggested_asset_classes_removed": len(removed_assets),
        "networth_categories_created": len(archive.rows.networth_categories),
        "networth_items_created": len(archive.rows.networth_items),
        "item_valuations_created": len(archive.rows.item_valuations),
        "networth_snapshots_created": len(archive.rows.networth_snapshots),
        "linked_items_unlinked": linked_items,
        **investment_counts,
    }


class ArchiveImportAdapter(ImportAdapter):
    """Restore or merge a complete ``.zip`` workspace archive (F4.4)."""

    def plan(
        self,
        session: Session,
        ctx: Context,
        batch: ImportBatch,
        parsed: ParsedUpload,
        *,
        base_currency: str,
        out_of_time: Callable[[], bool] = lambda: False,
    ) -> dict[str, Any]:
        if batch.mode == "restore":
            return _plan_restore(session, ctx, parsed, out_of_time=out_of_time)
        report, _counts = _merge(
            session,
            ctx,
            batch,
            _archive(parsed),
            base_currency=base_currency,
            apply=False,
            out_of_time=out_of_time,
        )
        return report

    def apply(
        self,
        session: Session,
        ctx: Context,
        batch: ImportBatch,
        parsed: ParsedUpload,
        *,
        base_currency: str,
        skip_invalid: bool,
    ) -> dict[str, int]:
        del skip_invalid
        if batch.mode == "restore":
            return _apply_restore(session, ctx, batch, parsed)
        _report, counts = _merge(
            session,
            ctx,
            batch,
            _archive(parsed),
            base_currency=base_currency,
            apply=True,
            out_of_time=lambda: False,
        )
        return counts
