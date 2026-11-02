"""CSV import adapters for lone ledger files (F4.3).

Each adapter plans a file against the workspace (read only) and applies it inside the caller's
write transaction. Category mapping (``domain.category_mapping``) is shared: the dry-run report
lists every source category with its default or chosen action, and apply re-resolves the choices
after creating the categories the batch needs.
"""

from __future__ import annotations

import contextlib
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid5

from sqlalchemy.orm import Session
from sqlalchemy.orm.session import sessionmaker

from assetboard.db.models.import_batch import ImportBatch
from assetboard.db.repositories import (
    BudgetLineRepository,
    CategoryRepository,
    ImportCategoryMappingRepository,
    MonthStateRepository,
    TransactionRepository,
)
from assetboard.domain.categories import Kind, Treatment, name_key
from assetboard.domain.category_mapping import (
    CategorySnapshot,
    MappingChoice,
    MappingError,
    ResolvedMapping,
    SavedMapping,
    next_color,
    resolve_mapping,
    saved_kind_for_name,
)
from assetboard.domain.import_csv import (
    parse_category_name,
    parse_currency,
    parse_date,
    parse_description,
    parse_kind,
    parse_month,
    parse_planned_amount,
    parse_sort_order,
    parse_timestamp,
    parse_transaction_amount,
    parse_treatment,
    parse_uuid,
)
from assetboard.domain.import_upload import DEADLINE_CHECK_ROWS, ParsedUpload, ParseTimeout
from assetboard.domain.money import minor_unit_for
from assetboard.domain.months import YearMonth
from assetboard.domain.transactions import EntryError, description_key
from assetboard.services.context import Context, Role, require_role
from assetboard.services.import_dedupe import (
    IncomingBudgetLine,
    IncomingTransaction,
    match_budget_lines,
    match_transactions,
)
from assetboard.services.imports import ImportAdapter
from assetboard.services.imports import ImportError as ImportServiceError

FILE = "upload.csv"
_SENTINEL_NAMESPACE = UUID("6b1f0c1e-4f6a-5e7b-8c9d-0a1b2c3d4e5f")


def _entry(code: str, line: int | None = None, column: str = "") -> dict[str, object]:
    return {"file": FILE, "line": line or "", "column": column, "code": code, "message": code}


def _error(error: EntryError, line: int, column: str) -> dict[str, object]:
    return _entry(error.code, line, column)


def _records(parsed: ParsedUpload) -> list[tuple[int, dict[str, str]]]:
    return [
        (number, dict(zip(parsed.csv_header, row, strict=True)))
        for number, row in enumerate(parsed.csv_rows, start=2)
    ]


def _text(minor: int, currency: str) -> str:
    return str(Decimal(minor).scaleb(-minor_unit_for(currency)))


def _decimal_option(batch: ImportBatch) -> str:
    return "comma" if batch.options.get("decimal") == "comma" else "dot"


def _snapshots(session: Session, ctx: Context) -> list[CategorySnapshot]:
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
        for category in CategoryRepository(session, ctx).ordered()
    ]


def _saved_mappings(session: Session, ctx: Context) -> list[SavedMapping]:
    return [
        SavedMapping(
            kind=mapping.kind,
            source_name_key=mapping.source_name_key,
            action=mapping.action,
            category_id=mapping.category_id,
        )
        for mapping in ImportCategoryMappingRepository(session, ctx).list()
    ]


def _mapping_choices(batch: ImportBatch) -> dict[tuple[str, str], MappingChoice]:
    raw = batch.options.get("mappings")
    if not isinstance(raw, list):
        return {}
    choices: dict[tuple[str, str], MappingChoice] = {}
    for raw_item in cast("list[object]", raw):
        if not isinstance(raw_item, dict):
            continue
        item = cast("dict[str, object]", raw_item)
        kind = str(item.get("kind", ""))
        source_name_key = str(item.get("source_name_key", ""))
        action = str(item.get("action", ""))
        if (
            kind not in {Kind.EXPENSE.value, Kind.INCOME.value}
            or not source_name_key
            or action not in {"map", "create", "ignore"}
        ):
            continue
        category_id: UUID | None = None
        raw_id = item.get("category_id")
        if isinstance(raw_id, str) and raw_id:
            try:
                category_id = UUID(raw_id)
            except ValueError:
                category_id = None
        choices[(kind, source_name_key)] = MappingChoice(
            kind=kind,
            source_name_key=source_name_key,
            action=action,  # type: ignore[arg-type]
            category_id=category_id,
            create_name=str(item.get("create_name", "")),
            create_treatment=str(item.get("create_treatment", "regular")),
            create_color=str(item.get("create_color", "")),
        )
    return choices


def _sentinel(batch: ImportBatch, kind: str, source_name_key: str) -> UUID:
    return uuid5(_SENTINEL_NAMESPACE, f"{batch.id}\x1f{kind}\x1f{source_name_key}")


@dataclass
class _Source:
    kind: str
    name: str
    name_key: str
    rows: int = 0
    total_minor: int = 0


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


def _add_source(
    sources: OrderedDict[tuple[str, str], _Source],
    kind: str,
    name: str,
    key: str,
    amount_minor: int,
) -> None:
    source = sources.setdefault((kind, key), _Source(kind, name, key))
    source.rows += 1
    source.total_minor += amount_minor


def _find_category(
    categories: list[CategorySnapshot], category_id: UUID | None
) -> CategorySnapshot | None:
    if category_id is None:
        return None
    return next((category for category in categories if category.id == category_id), None)


def _one_existing_kind(categories: list[CategorySnapshot], key: str) -> str | None:
    kinds = {category.kind for category in categories if category.name_key == key}
    return next(iter(kinds)) if len(kinds) == 1 else None


def _resolve_sources(
    sources: list[_Source],
    categories: list[CategorySnapshot],
    saved: list[SavedMapping],
    choices: dict[tuple[str, str], MappingChoice],
    *,
    currency: str,
    created_ids: dict[tuple[str, str], UUID] | None = None,
) -> tuple[
    dict[tuple[str, str], ResolvedMapping],
    list[dict[str, object]],
    list[dict[str, object]],
    int,
]:
    """Resolve every source category; invalid choices become mapping errors, not exceptions."""
    saved_by_key = {(mapping.kind, mapping.source_name_key): mapping for mapping in saved}
    resolved: dict[tuple[str, str], ResolvedMapping] = {}
    entries: list[dict[str, object]] = []
    errors: list[dict[str, object]] = []
    invalid = 0
    create_offsets = {Kind.EXPENSE.value: 0, Kind.INCOME.value: 0}
    seen_creates: set[tuple[str, str]] = set()

    for source in sources:
        key = (source.kind, source.name_key)
        error_code = ""
        try:
            mapping = resolve_mapping(
                kind=source.kind,
                source_name=source.name,
                source_name_key=source.name_key,
                categories=categories,
                saved=saved_by_key.get(key),
                choice=choices.get(key),
                created_ids=created_ids,
                create_offset=create_offsets[source.kind],
            )
        except MappingError as exc:
            invalid += 1
            error_code = exc.code
            errors.append(_entry(exc.code, column="category"))
            mapping = ResolvedMapping(source.kind, source.name, source.name_key, "ignore", None)
        else:
            if mapping.action == "create":
                create_key = (mapping.kind, name_key(mapping.create_name))
                if create_key in seen_creates:
                    invalid += 1
                    error_code = "duplicate_create_name"
                    errors.append(_entry(error_code, column="category"))
                    mapping = ResolvedMapping(
                        source.kind, source.name, source.name_key, "ignore", None
                    )
                else:
                    seen_creates.add(create_key)
                    create_offsets[source.kind] += 1
        resolved[key] = mapping
        entries.append(
            {
                "kind": source.kind,
                "source_name": source.name,
                "source_name_key": source.name_key,
                "rows": source.rows,
                "total": _text(source.total_minor, currency),
                "action": mapping.action,
                "category_id": str(mapping.category_id) if mapping.category_id else "",
                "create_name": mapping.create_name or source.name,
                "create_treatment": mapping.create_treatment or Treatment.REGULAR.value,
                "create_color": mapping.create_color
                or next_color(source.kind, categories, create_offsets[source.kind]),
                "error": error_code,
            }
        )
    return resolved, entries, errors, invalid


def _create_categories(
    session: Session,
    ctx: Context,
    batch: ImportBatch,
    mappings: list[ResolvedMapping],
    categories: list[CategorySnapshot],
) -> dict[tuple[str, str], UUID]:
    repository = CategoryRepository(session, ctx)
    next_orders: dict[str, int] = {}
    created_ids: dict[tuple[str, str], UUID] = {}
    for mapping in mappings:
        if mapping.action != "create":
            continue
        kind = mapping.kind
        if kind not in next_orders:
            next_orders[kind] = repository.next_sort_order(kind)
        color = mapping.create_color or next_color(kind, categories)
        category = repository.create(
            name=mapping.create_name,
            name_key=name_key(mapping.create_name),
            kind=kind,
            treatment=mapping.create_treatment or Treatment.REGULAR.value,
            color=color,
            sort_order=next_orders[kind],
            import_batch_id=batch.id,
        )
        next_orders[kind] += 1
        categories.append(
            CategorySnapshot(
                id=category.id,
                kind=category.kind,
                name=category.name,
                name_key=category.name_key,
                treatment=category.treatment,
                color=category.color,
                sort_order=category.sort_order,
                archived=False,
            )
        )
        created_ids[(kind, mapping.source_name_key)] = category.id
    return created_ids


def _save_mappings(
    session: Session,
    ctx: Context,
    resolved: Mapping[tuple[str, str], ResolvedMapping],
    sources: list[_Source],
) -> None:
    repository = ImportCategoryMappingRepository(session, ctx)
    for source in sources:
        mapping = resolved[(source.kind, source.name_key)]
        if mapping.action == "map" and mapping.category_id is not None:
            repository.upsert(
                kind=source.kind,
                source_name_key=source.name_key,
                action="map",
                category_id=mapping.category_id,
            )
        elif mapping.action == "ignore":
            repository.upsert(
                kind=source.kind,
                source_name_key=source.name_key,
                action="ignore",
                category_id=None,
            )


def _month_rows(months: Mapping[str, _MonthTotals], currency: str) -> list[dict[str, object]]:
    return [
        {
            "month": month,
            "expense_total": _text(totals.expense_minor, currency),
            "income_total": _text(totals.income_minor, currency),
            "planned_total": _text(totals.planned_minor, currency),
            "rows_to_create": totals.rows_to_create,
            "rows_skipped": totals.rows_skipped,
        }
        for month, totals in sorted(months.items())
    ]


def _category_rows(
    categories: Mapping[tuple[str, str], _CategoryTotals], currency: str
) -> list[dict[str, object]]:
    return [
        {
            "kind": totals.kind,
            "category": totals.name,
            "total": _text(totals.total_minor, currency),
            "rows_to_create": totals.rows_to_create,
        }
        for _, totals in sorted(categories.items())
    ]


@dataclass(frozen=True, slots=True)
class _TransactionRow:
    line: int
    day: date
    direction: str
    amount_minor: int
    currency: str
    category_name: str
    category_key: str
    description: str
    description_key: str
    source_id: UUID | None


def _parse_transactions(
    parsed: ParsedUpload,
    *,
    base_currency: str,
    decimal: str,
    closed: frozenset[YearMonth],
    today: date,
    out_of_time: Callable[[], bool] = lambda: False,
) -> tuple[
    list[_TransactionRow],
    list[dict[str, object]],
    list[dict[str, object]],
    OrderedDict[tuple[str, str], _Source],
    dict[str, int],
    dict[str, int],
    set[str],
]:
    rows: list[_TransactionRow] = []
    errors: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    sources: OrderedDict[tuple[str, str], _Source] = OrderedDict()
    counts = {"deleted": 0, "future": 0, "closed": 0}
    month_skipped: dict[str, int] = {}
    closed_months: set[str] = set()

    for number, (line, record) in enumerate(_records(parsed), start=1):
        if number % DEADLINE_CHECK_ROWS == 0 and out_of_time():
            raise ParseTimeout
        try:
            day = parse_date(record.get("date", ""))
        except EntryError as error:
            errors.append(_error(error, line, "date"))
            continue
        try:
            direction = parse_kind(record.get("direction", ""))
        except EntryError as error:
            errors.append(_error(error, line, "direction"))
            continue
        try:
            currency = parse_currency(record.get("currency", ""), base_currency)
        except EntryError as error:
            errors.append(_error(error, line, "currency"))
            continue
        try:
            amount_minor = parse_transaction_amount(
                record.get("amount", ""), currency, decimal=decimal
            )
        except EntryError as error:
            errors.append(_error(error, line, "amount"))
            continue
        try:
            category_name = parse_category_name(record.get("category", ""))
        except EntryError as error:
            errors.append(_error(error, line, "category"))
            continue
        try:
            description = parse_description(record.get("description", ""))
        except EntryError as error:
            errors.append(_error(error, line, "description"))
            continue
        source_id: UUID | None = None
        raw_id = record.get("id", "").strip()
        if raw_id:
            try:
                source_id = parse_uuid(raw_id)
            except EntryError as error:
                errors.append(_error(error, line, "id"))
                continue
        raw_deleted = record.get("deleted_at", "").strip()
        if raw_deleted:
            try:
                parse_timestamp(raw_deleted)
            except EntryError as error:
                errors.append(_error(error, line, "deleted_at"))
                continue
            counts["deleted"] += 1
            skipped.append(_entry("deleted_in_source", line, "deleted_at"))
            month_skipped[str(YearMonth.from_date(day))] = (
                month_skipped.get(str(YearMonth.from_date(day)), 0) + 1
            )
            continue
        month = YearMonth.from_date(day)
        if day > today:
            counts["future"] += 1
            skipped.append(_entry("future_date", line, "date"))
            month_skipped[str(month)] = month_skipped.get(str(month), 0) + 1
            continue
        if month in closed:
            counts["closed"] += 1
            closed_months.add(str(month))
            skipped.append(_entry("closed_month", line, "date"))
            month_skipped[str(month)] = month_skipped.get(str(month), 0) + 1
            continue
        category_key = name_key(category_name)
        _add_source(sources, direction, category_name, category_key, amount_minor)
        rows.append(
            _TransactionRow(
                line=line,
                day=day,
                direction=direction,
                amount_minor=amount_minor,
                currency=currency,
                category_name=category_name,
                category_key=category_key,
                description=description,
                description_key=description_key(description),
                source_id=source_id,
            )
        )
    return rows, errors, skipped, sources, counts, month_skipped, closed_months


class TransactionsCsvAdapter(ImportAdapter):
    """``transactions.csv`` alone (F4.3)."""

    source = "transactions_csv"

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
        today = ctx.clock.today(ctx.workspace.timezone)
        closed = MonthStateRepository(session, ctx).closed_months()
        rows, errors, skipped, sources, skipped_counts, month_skipped, closed_months = (
            _parse_transactions(
                parsed,
                base_currency=base_currency,
                decimal=_decimal_option(batch),
                closed=closed,
                today=today,
                out_of_time=out_of_time,
            )
        )
        categories = _snapshots(session, ctx)
        saved = _saved_mappings(session, ctx)
        choices = _mapping_choices(batch)
        resolved, mapping_entries, mapping_errors, mapping_invalid = _resolve_sources(
            list(sources.values()),
            categories,
            saved,
            choices,
            currency=base_currency,
        )
        incoming: list[IncomingTransaction] = []
        for row in rows:
            mapping = resolved[(row.direction, row.category_key)]
            if mapping.action == "ignore":
                skipped.append(_entry("ignored_category", row.line, "category"))
                skipped_counts["ignored"] = skipped_counts.get("ignored", 0) + 1
                month = str(YearMonth.from_date(row.day))
                month_skipped[month] = month_skipped.get(month, 0) + 1
                continue
            incoming.append(
                IncomingTransaction(
                    line=row.line,
                    source_id=row.source_id,
                    day=row.day,
                    direction=row.direction,
                    amount_minor=row.amount_minor,
                    currency=row.currency,
                    category_id=mapping.category_id
                    or _sentinel(batch, row.direction, row.category_key),
                    source_category_name_key=row.category_key,
                    description=row.description,
                    description_key=row.description_key,
                )
            )
        matches = match_transactions(session, ctx, incoming)
        months: dict[str, _MonthTotals] = {
            month: _MonthTotals(rows_skipped=count) for month, count in month_skipped.items()
        }
        category_totals: dict[tuple[str, str], _CategoryTotals] = {}
        duplicates = conflicts = 0
        for match in matches:
            month = str(YearMonth.from_date(match.row.day))
            totals = months.setdefault(month, _MonthTotals())
            if match.action == "create":
                totals.rows_to_create += 1
                if match.row.direction == Kind.EXPENSE.value:
                    totals.expense_minor += match.row.amount_minor
                else:
                    totals.income_minor += match.row.amount_minor
                mapping = resolved[(match.row.direction, match.row.source_category_name_key)]
                target = _find_category(categories, mapping.category_id)
                target_name = (
                    mapping.create_name
                    if mapping.action == "create"
                    else (target.name if target is not None else match.row.source_category_name_key)
                )
                key = (match.row.direction, name_key(target_name))
                category = category_totals.setdefault(
                    key, _CategoryTotals(match.row.direction, target_name)
                )
                category.total_minor += match.row.amount_minor
                category.rows_to_create += 1
            else:
                totals.rows_skipped += 1
                if match.action == "duplicate":
                    duplicates += 1
                else:
                    conflicts += 1
                skipped.append(_entry(match.code or "duplicate", match.row.line, "category"))
        counts = {
            "to_create": sum(match.action == "create" for match in matches),
            "duplicates_skipped": duplicates,
            "conflicts_skipped": conflicts,
            "closed_months_skipped": skipped_counts["closed"],
            "future_dates_skipped": skipped_counts["future"],
            "deleted_skipped": skipped_counts["deleted"],
            "ignored_categories_skipped": skipped_counts.get("ignored", 0),
            "invalid": len(errors),
            "mapping_invalid": mapping_invalid,
        }
        return {
            "counts": counts,
            "months": _month_rows(months, base_currency),
            "categories": _category_rows(category_totals, base_currency),
            "mapping": mapping_entries,
            "mapping_errors": mapping_errors,
            "closed_months": sorted(closed_months),
            "warnings": [],
            "skipped": skipped,
            "errors": [*errors, *mapping_errors],
        }

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
        today = ctx.clock.today(ctx.workspace.timezone)
        closed = MonthStateRepository(session, ctx).closed_months()
        rows, errors, skipped, sources, _, _, _ = _parse_transactions(
            parsed,
            base_currency=base_currency,
            decimal=_decimal_option(batch),
            closed=closed,
            today=today,
        )
        categories = _snapshots(session, ctx)
        saved = _saved_mappings(session, ctx)
        choices = _mapping_choices(batch)
        resolved, _, _, mapping_invalid = _resolve_sources(
            list(sources.values()),
            categories,
            saved,
            choices,
            currency=base_currency,
        )
        if mapping_invalid:
            raise ImportServiceError("mapping_invalid")
        created_ids = _create_categories(
            session,
            ctx,
            batch,
            [mapping for mapping in resolved.values() if mapping.action == "create"],
            categories,
        )
        resolved, _, _, _ = _resolve_sources(
            list(sources.values()),
            categories,
            saved,
            choices,
            currency=base_currency,
            created_ids=created_ids,
        )
        incoming: list[IncomingTransaction] = []
        for row in rows:
            mapping = resolved[(row.direction, row.category_key)]
            if mapping.action == "ignore":
                continue
            incoming.append(
                IncomingTransaction(
                    line=row.line,
                    source_id=row.source_id,
                    day=row.day,
                    direction=row.direction,
                    amount_minor=row.amount_minor,
                    currency=row.currency,
                    category_id=mapping.category_id
                    or _sentinel(batch, row.direction, row.category_key),
                    source_category_name_key=row.category_key,
                    description=row.description,
                    description_key=row.description_key,
                )
            )
        matches = match_transactions(session, ctx, incoming)
        repository = TransactionRepository(session, ctx)
        created = duplicates = conflicts = 0
        for match in matches:
            if match.action == "create":
                row = match.row
                repository.create_imported(
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
                created += 1
            elif match.action == "duplicate":
                duplicates += 1
            else:
                conflicts += 1
        if batch.options.get("remember_mappings"):
            _save_mappings(session, ctx, resolved, list(sources.values()))
        return {
            "created": created,
            "duplicates": duplicates,
            "conflicts": conflicts,
            "skipped": len(skipped) + len(errors),
        }


@dataclass(frozen=True, slots=True)
class _BudgetRow:
    line: int
    month: YearMonth
    kind: str
    category_name: str
    category_key: str
    planned_minor: int


def _parse_budget_lines(
    parsed: ParsedUpload,
    *,
    base_currency: str,
    decimal: str,
    closed: frozenset[YearMonth],
    categories: list[CategorySnapshot],
    saved: list[SavedMapping],
    out_of_time: Callable[[], bool] = lambda: False,
) -> tuple[
    list[_BudgetRow],
    list[dict[str, object]],
    list[dict[str, object]],
    OrderedDict[tuple[str, str], _Source],
    dict[str, int],
    dict[str, int],
    set[str],
]:
    rows: list[_BudgetRow] = []
    errors: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    sources: OrderedDict[tuple[str, str], _Source] = OrderedDict()
    counts = {"closed": 0}
    month_skipped: dict[str, int] = {}
    closed_months: set[str] = set()

    for number, (line, record) in enumerate(_records(parsed), start=1):
        if number % DEADLINE_CHECK_ROWS == 0 and out_of_time():
            raise ParseTimeout
        try:
            month = parse_month(record.get("month", ""))
        except EntryError as error:
            errors.append(_error(error, line, "month"))
            continue
        try:
            category_name = parse_category_name(record.get("category", ""))
        except EntryError as error:
            errors.append(_error(error, line, "category"))
            continue
        try:
            currency = parse_currency(record.get("currency", ""), base_currency)
        except EntryError as error:
            errors.append(_error(error, line, "currency"))
            continue
        try:
            planned_minor = parse_planned_amount(
                record.get("planned", ""), currency, decimal=decimal
            )
        except EntryError as error:
            errors.append(_error(error, line, "planned"))
            continue
        if planned_minor == 0:
            continue
        if month in closed:
            counts["closed"] += 1
            closed_months.add(str(month))
            skipped.append(_entry("closed_month", line, "month"))
            month_skipped[str(month)] = month_skipped.get(str(month), 0) + 1
            continue
        raw_kind = record.get("kind", "").strip()
        if raw_kind:
            try:
                kind = parse_kind(raw_kind)
            except EntryError as error:
                errors.append(_error(error, line, "kind"))
                continue
        else:
            category_key = name_key(category_name)
            saved_kind = saved_kind_for_name(category_key, saved)
            kind = saved_kind or _one_existing_kind(categories, category_key)
            if kind is None:
                errors.append(_entry("kind_required", line, "kind"))
                continue
        category_key = name_key(category_name)
        _add_source(sources, kind, category_name, category_key, planned_minor)
        rows.append(
            _BudgetRow(
                line=line,
                month=month,
                kind=kind,
                category_name=category_name,
                category_key=category_key,
                planned_minor=planned_minor,
            )
        )
    return rows, errors, skipped, sources, counts, month_skipped, closed_months


class BudgetLinesCsvAdapter(ImportAdapter):
    """``budget_lines.csv`` alone (F4.3)."""

    source = "budget_lines_csv"

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
        closed = MonthStateRepository(session, ctx).closed_months()
        categories = _snapshots(session, ctx)
        saved = _saved_mappings(session, ctx)
        rows, errors, skipped, sources, skipped_counts, month_skipped, closed_months = (
            _parse_budget_lines(
                parsed,
                base_currency=base_currency,
                decimal=_decimal_option(batch),
                closed=closed,
                categories=categories,
                saved=saved,
                out_of_time=out_of_time,
            )
        )
        resolved, mapping_entries, mapping_errors, mapping_invalid = _resolve_sources(
            list(sources.values()),
            categories,
            saved,
            _mapping_choices(batch),
            currency=base_currency,
        )
        incoming: list[IncomingBudgetLine] = []
        source_by_line: dict[int, _Source] = {}
        for row in rows:
            mapping = resolved[(row.kind, row.category_key)]
            if mapping.action == "ignore":
                skipped.append(_entry("ignored_category", row.line, "category"))
                skipped_counts["ignored"] = skipped_counts.get("ignored", 0) + 1
                month_skipped[str(row.month)] = month_skipped.get(str(row.month), 0) + 1
                continue
            target = _find_category(categories, mapping.category_id)
            if target is not None and target.archived:
                skipped.append(_entry("category_archived", row.line, "category"))
                skipped_counts["archived"] = skipped_counts.get("archived", 0) + 1
                month_skipped[str(row.month)] = month_skipped.get(str(row.month), 0) + 1
                continue
            treatment = target.treatment if target is not None else mapping.create_treatment
            if treatment == Treatment.TRANSFER.value:
                skipped.append(_entry("category_transfer", row.line, "category"))
                skipped_counts["transfer"] = skipped_counts.get("transfer", 0) + 1
                month_skipped[str(row.month)] = month_skipped.get(str(row.month), 0) + 1
                continue
            category_id = mapping.category_id or _sentinel(batch, row.kind, row.category_key)
            source_by_line[row.line] = sources[(row.kind, row.category_key)]
            incoming.append(
                IncomingBudgetLine(
                    line=row.line,
                    month=row.month,
                    category_id=category_id,
                    planned_minor=row.planned_minor,
                )
            )
        matches = match_budget_lines(session, ctx, incoming)
        months: dict[str, _MonthTotals] = {
            month: _MonthTotals(rows_skipped=count) for month, count in month_skipped.items()
        }
        category_totals: dict[tuple[str, str], _CategoryTotals] = {}
        duplicates = conflicts = 0
        for match in matches:
            month = str(match.row.month)
            totals = months.setdefault(month, _MonthTotals())
            if match.action == "create":
                totals.rows_to_create += 1
                totals.planned_minor += match.row.planned_minor
                source = source_by_line[match.row.line]
                mapping = resolved[(source.kind, source.name_key)]
                target = _find_category(categories, mapping.category_id)
                target_name = (
                    mapping.create_name
                    if mapping.action == "create"
                    else (target.name if target is not None else source.name)
                )
                key = (source.kind, name_key(target_name))
                category = category_totals.setdefault(
                    key, _CategoryTotals(source.kind, target_name)
                )
                category.total_minor += match.row.planned_minor
                category.rows_to_create += 1
            else:
                totals.rows_skipped += 1
                if match.action == "duplicate":
                    duplicates += 1
                else:
                    conflicts += 1
                skipped.append(_entry(match.code or "duplicate", match.row.line, "category"))
        counts = {
            "to_create": sum(match.action == "create" for match in matches),
            "duplicates_skipped": duplicates,
            "conflicts_skipped": conflicts,
            "closed_months_skipped": skipped_counts["closed"],
            "ignored_categories_skipped": skipped_counts.get("ignored", 0),
            "archived_categories_skipped": skipped_counts.get("archived", 0),
            "transfer_categories_skipped": skipped_counts.get("transfer", 0),
            "invalid": len(errors),
            "mapping_invalid": mapping_invalid,
        }
        return {
            "counts": counts,
            "months": _month_rows(months, base_currency),
            "categories": _category_rows(category_totals, base_currency),
            "mapping": mapping_entries,
            "mapping_errors": mapping_errors,
            "closed_months": sorted(closed_months),
            "warnings": [],
            "skipped": skipped,
            "errors": [*errors, *mapping_errors],
        }

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
        closed = MonthStateRepository(session, ctx).closed_months()
        categories = _snapshots(session, ctx)
        saved = _saved_mappings(session, ctx)
        rows, errors, skipped, sources, _, _, _ = _parse_budget_lines(
            parsed,
            base_currency=base_currency,
            decimal=_decimal_option(batch),
            closed=closed,
            categories=categories,
            saved=saved,
        )
        resolved, _, _, mapping_invalid = _resolve_sources(
            list(sources.values()),
            categories,
            saved,
            _mapping_choices(batch),
            currency=base_currency,
        )
        if mapping_invalid:
            raise ImportServiceError("mapping_invalid")
        created_ids = _create_categories(
            session,
            ctx,
            batch,
            [mapping for mapping in resolved.values() if mapping.action == "create"],
            categories,
        )
        resolved, _, _, _ = _resolve_sources(
            list(sources.values()),
            categories,
            saved,
            _mapping_choices(batch),
            currency=base_currency,
            created_ids=created_ids,
        )
        incoming: list[IncomingBudgetLine] = []
        for row in rows:
            mapping = resolved[(row.kind, row.category_key)]
            if mapping.action == "ignore":
                continue
            target = _find_category(categories, mapping.category_id)
            if target is not None and target.archived:
                continue
            treatment = target.treatment if target is not None else mapping.create_treatment
            if treatment == Treatment.TRANSFER.value:
                continue
            incoming.append(
                IncomingBudgetLine(
                    line=row.line,
                    month=row.month,
                    category_id=mapping.category_id or _sentinel(batch, row.kind, row.category_key),
                    planned_minor=row.planned_minor,
                )
            )
        matches = match_budget_lines(session, ctx, incoming)
        repository = BudgetLineRepository(session, ctx)
        created = duplicates = conflicts = 0
        affected_months: set[YearMonth] = set()
        for match in matches:
            if match.action == "create":
                repository.create(
                    month=match.row.month,
                    category_id=match.row.category_id,
                    planned_minor=match.row.planned_minor,
                    currency=base_currency,
                    import_batch_id=batch.id,
                )
                affected_months.add(match.row.month)
                created += 1
            elif match.action == "duplicate":
                duplicates += 1
            else:
                conflicts += 1
        month_states = MonthStateRepository(session, ctx)
        now = ctx.clock.now()
        for month in sorted(affected_months):
            state = month_states.for_month(month) or month_states.create(month)
            if state.plan_started_at is None:
                month_states.update(state.id, plan_started_at=now)
        if batch.options.get("remember_mappings"):
            _save_mappings(session, ctx, resolved, list(sources.values()))
        return {
            "created": created,
            "duplicates": duplicates,
            "conflicts": conflicts,
            "skipped": len(skipped) + len(errors),
        }


@dataclass(frozen=True, slots=True)
class _CategoryRow:
    line: int
    source_id: UUID | None
    kind: str
    name: str
    name_key: str
    treatment: str
    color: str
    archived_at: datetime | None


def _parse_category_rows(
    parsed: ParsedUpload,
    *,
    out_of_time: Callable[[], bool] = lambda: False,
) -> tuple[list[_CategoryRow], list[dict[str, object]]]:
    rows: list[_CategoryRow] = []
    errors: list[dict[str, object]] = []
    for number, (line, record) in enumerate(_records(parsed), start=1):
        if number % DEADLINE_CHECK_ROWS == 0 and out_of_time():
            raise ParseTimeout
        raw_kind = record.get("kind", "").strip()
        if not raw_kind:
            errors.append(_entry("kind_required", line, "kind"))
            continue
        try:
            kind = parse_kind(raw_kind)
        except EntryError as error:
            errors.append(_error(error, line, "kind"))
            continue
        try:
            name = parse_category_name(record.get("name", ""))
        except EntryError as error:
            errors.append(_error(error, line, "name"))
            continue
        try:
            treatment = parse_treatment(record.get("treatment", "")).value
        except EntryError as error:
            errors.append(_error(error, line, "treatment"))
            continue
        color = ""
        raw_color = record.get("color", "").strip()
        if raw_color:
            try:
                from assetboard.domain.categories import clean_color

                color = clean_color(raw_color)
            except ValueError:
                errors.append(_entry("invalid_color", line, "color"))
                continue
        raw_sort_order = record.get("sort_order", "").strip()
        if raw_sort_order:
            try:
                parse_sort_order(raw_sort_order)
            except EntryError as error:
                errors.append(_error(error, line, "sort_order"))
                continue
        archived_at: datetime | None = None
        raw_archived = record.get("archived_at", "").strip()
        if raw_archived:
            try:
                archived_at = parse_timestamp(raw_archived)
            except EntryError as error:
                errors.append(_error(error, line, "archived_at"))
                continue
        source_id: UUID | None = None
        raw_id = record.get("id", "").strip()
        if raw_id:
            try:
                source_id = parse_uuid(raw_id)
            except EntryError as error:
                errors.append(_error(error, line, "id"))
                continue
        rows.append(
            _CategoryRow(
                line=line,
                source_id=source_id,
                kind=kind,
                name=name,
                name_key=name_key(name),
                treatment=treatment,
                color=color,
                archived_at=archived_at,
            )
        )
    return rows, errors


def _category_values_match(row: _CategoryRow, category: Any) -> bool:
    return (
        category.kind == row.kind
        and category.name_key == row.name_key
        and category.treatment == row.treatment
        # An omitted colour means the palette default, so it cannot be a difference.
        and (not row.color or category.color == row.color)
        and category.archived_at == row.archived_at
    )


class CategoriesCsvAdapter(ImportAdapter):
    """``categories.csv`` alone (F4.3)."""

    source = "categories_csv"

    def _plan(
        self,
        session: Session,
        ctx: Context,
        parsed: ParsedUpload,
        *,
        out_of_time: Callable[[], bool] = lambda: False,
    ) -> tuple[
        list[_CategoryRow],
        list[dict[str, object]],
        list[dict[str, object]],
        list[dict[str, object]],
        int,
    ]:
        rows, errors = _parse_category_rows(parsed, out_of_time=out_of_time)
        repository = CategoryRepository(session, ctx)
        lineage = repository.by_lineage_ids(
            [row.source_id for row in rows if row.source_id is not None]
        )
        by_id: dict[UUID, Any] = {}
        for category in lineage:
            if category.import_key and category.import_key.startswith("id:"):
                with contextlib.suppress(ValueError):
                    by_id[UUID(category.import_key[3:])] = category
            by_id[category.id] = category
        active = {
            (category.kind, category.name_key): category
            for category in repository.ordered()
            if category.archived_at is None
        }
        planned: list[_CategoryRow] = []
        skipped: list[dict[str, object]] = []
        categories: list[dict[str, object]] = []
        seen_ids: set[UUID] = set()
        seen_names: dict[tuple[str, str], _CategoryRow] = {}
        for row in rows:
            if row.source_id is not None:
                if row.source_id in seen_ids:
                    skipped.append(_entry("duplicate_in_file", row.line, "id"))
                    categories.append(
                        {
                            "kind": row.kind,
                            "category": row.name,
                            "total": "",
                            "rows_to_create": 0,
                        }
                    )
                    continue
                seen_ids.add(row.source_id)
            name_key_value = (row.kind, row.name_key)
            earlier = seen_names.get(name_key_value)
            if earlier is not None:
                code = (
                    "duplicate_in_file"
                    if (
                        earlier.treatment == row.treatment
                        and earlier.color == row.color
                        and earlier.archived_at == row.archived_at
                    )
                    else "conflict_in_file"
                )
                skipped.append(_entry(code, row.line, "name"))
                categories.append(
                    {
                        "kind": row.kind,
                        "category": row.name,
                        "total": "",
                        "rows_to_create": 0,
                    }
                )
                continue
            seen_names[name_key_value] = row
            existing = by_id.get(row.source_id) if row.source_id is not None else None
            if existing is None:
                existing = active.get((row.kind, row.name_key))
            if existing is not None:
                code = (
                    "already_imported"
                    if _category_values_match(row, existing)
                    else "category_differs"
                )
                skipped.append(_entry(code, row.line, "name"))
                categories.append(
                    {
                        "kind": row.kind,
                        "category": row.name,
                        "total": "",
                        "rows_to_create": 0,
                    }
                )
                continue
            planned.append(row)
            categories.append(
                {
                    "kind": row.kind,
                    "category": row.name,
                    "total": "",
                    "rows_to_create": 1,
                }
            )
        return planned, errors, skipped, categories, len(errors)

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
        del base_currency
        planned, errors, skipped, categories, invalid = self._plan(
            session, ctx, parsed, out_of_time=out_of_time
        )
        duplicates = sum(
            1 for entry in skipped if entry["code"] in {"already_imported", "duplicate_in_file"}
        )
        conflicts = sum(
            1 for entry in skipped if entry["code"] in {"category_differs", "conflict_in_file"}
        )
        return {
            "counts": {
                "to_create": len(planned),
                "duplicates_skipped": duplicates,
                "conflicts_skipped": conflicts,
                "invalid": invalid,
                "mapping_invalid": 0,
            },
            "months": [],
            "categories": categories,
            "mapping": [],
            "mapping_errors": [],
            "closed_months": [],
            "warnings": [],
            "skipped": skipped,
            "errors": errors,
        }

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
        del base_currency, skip_invalid
        planned, errors, skipped, _, _ = self._plan(session, ctx, parsed)
        repository = CategoryRepository(session, ctx)
        next_orders: dict[str, int] = {}
        categories = _snapshots(session, ctx)
        created = 0
        for row in planned:
            if row.kind not in next_orders:
                next_orders[row.kind] = repository.next_sort_order(row.kind)
            color = row.color or next_color(row.kind, categories)
            category = repository.create(
                name=row.name,
                name_key=row.name_key,
                kind=row.kind,
                treatment=row.treatment,
                color=color,
                sort_order=next_orders[row.kind],
                import_batch_id=batch.id,
                import_key=f"id:{row.source_id}" if row.source_id is not None else None,
                archived_at=row.archived_at,
            )
            next_orders[row.kind] += 1
            categories.append(
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
            )
            created += 1
        return {
            "created": created,
            "duplicates": sum(
                1 for entry in skipped if entry["code"] in {"already_imported", "duplicate_in_file"}
            ),
            "conflicts": sum(
                1 for entry in skipped if entry["code"] in {"category_differs", "conflict_in_file"}
            ),
            "skipped": len(skipped) + len(errors),
        }


def default_import_adapters() -> dict[str, ImportAdapter]:
    """The F4.3 adapters registered by the application."""
    return {
        "transactions_csv": TransactionsCsvAdapter(),
        "budget_lines_csv": BudgetLinesCsvAdapter(),
        "categories_csv": CategoriesCsvAdapter(),
    }


@dataclass(frozen=True, slots=True)
class MappingCategoryView:
    id: str
    kind: str
    name: str
    archived: bool


def mapping_category_options(
    factory: sessionmaker[Session], ctx: Context
) -> list[MappingCategoryView]:
    """Categories a mapping row may target (active and archived, both kinds)."""
    require_role(ctx, Role.EDITOR)
    with factory() as session:
        return [
            MappingCategoryView(
                id=str(category.id),
                kind=category.kind,
                name=category.name,
                archived=category.archived_at is not None,
            )
            for category in CategoryRepository(session, ctx).ordered()
        ]
