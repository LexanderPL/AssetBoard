"""Standalone ``item_valuations.csv`` import (F5.5).

A valuation history kept outside AssetBoard can be merged into an existing workspace. The file
is the archive's ``item_valuations.csv`` or a hand-written ``item,as_of,value`` CSV; the
optional ``currency``, ``note``, ``item_id`` and ``created_at`` columns carry the rest.

Rows match an item by its lineage ``item_id`` first (an exported archive writes it) and by
name otherwise, because item names are not unique (F5.1). A stored value for the same item and
date makes an identical row a duplicate and a different value a conflict; neither changes the
stored history. ``created_at`` keeps same-day entry order, and a file without it is stamped in
file order from the import time.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from assetboard.db.models.import_batch import ImportBatch
from assetboard.db.models.networth import NetWorthItem
from assetboard.db.repositories import ItemValuationRepository, NetWorthItemRepository
from assetboard.domain.budget import planned_minor
from assetboard.domain.import_csv import (
    parse_date,
    parse_decimal,
    parse_timestamp,
    parse_uuid,
)
from assetboard.domain.import_upload import DEADLINE_CHECK_ROWS, ParsedUpload, ParseTimeout
from assetboard.domain.money import minor_unit_for
from assetboard.domain.networth import (
    InvalidItemName,
    InvalidNotes,
    clean_item_name,
    clean_valuation_note,
)
from assetboard.domain.text import text_key
from assetboard.domain.transactions import EntryError
from assetboard.services.context import Context
from assetboard.services.imports import ImportAdapter

FILE = "item_valuations.csv"


def _entry(code: str, line: int | None = None, column: str = "") -> dict[str, object]:
    return {"file": FILE, "line": line or "", "column": column, "code": code, "message": code}


@dataclass(frozen=True, slots=True)
class _Row:
    line: int
    item_name: str
    item_key: str
    item_id: UUID | None
    as_of: date
    value: Decimal
    currency: str | None
    note: str
    created_at: datetime | None
    source_id: UUID | None


@dataclass(frozen=True, slots=True)
class _Match:
    row: _Row
    item: NetWorthItem
    value_minor: int
    created_at: datetime
    action: str  # create, duplicate or conflict
    code: str | None


def _decimal_option(batch: ImportBatch) -> str:
    return "comma" if batch.options.get("decimal") == "comma" else "dot"


def _parse_rows(
    parsed: ParsedUpload,
    batch: ImportBatch,
    *,
    out_of_time: Callable[[], bool],
) -> tuple[list[_Row], list[dict[str, object]]]:
    header = parsed.csv_header
    decimal = _decimal_option(batch)
    rows: list[_Row] = []
    errors: list[dict[str, object]] = []
    for index, cells in enumerate(parsed.csv_rows):
        line = index + 2
        if index % DEADLINE_CHECK_ROWS == 0 and out_of_time():
            raise ParseTimeout
        record = dict(zip(header, cells, strict=True))
        row_errors: list[dict[str, object]] = []
        try:
            item_name = clean_item_name(record.get("item", ""))
        except (InvalidItemName, ValueError):
            row_errors.append(_entry("invalid_name", line, "item"))
            item_name = ""
        item_key = text_key(item_name)
        raw_item_id = record.get("item_id", "").strip()
        item_id: UUID | None = None
        if raw_item_id:
            try:
                item_id = parse_uuid(raw_item_id)
            except EntryError as error:
                row_errors.append(_entry(error.code, line, "item_id"))
        try:
            as_of = parse_date(record.get("as_of", ""))
        except EntryError as error:
            row_errors.append(_entry(error.code, line, "as_of"))
            as_of = date(1900, 1, 1)
        try:
            value = parse_decimal(record.get("value", ""), decimal=decimal)
        except EntryError as error:
            row_errors.append(_entry(error.code, line, "value"))
            value = Decimal(0)
        raw_currency = record.get("currency", "").strip().upper()
        currency = raw_currency or None
        if currency is not None:
            try:
                minor_unit_for(currency)
            except ValueError:
                row_errors.append(_entry("invalid_currency", line, "currency"))
                currency = None
        try:
            note = clean_valuation_note(record.get("note", ""))
        except (InvalidNotes, ValueError):
            row_errors.append(_entry("invalid_notes", line, "note"))
            note = ""
        raw_created = record.get("created_at", "").strip()
        created_at: datetime | None = None
        if raw_created:
            try:
                created_at = parse_timestamp(raw_created)
            except EntryError as error:
                row_errors.append(_entry(error.code, line, "created_at"))
        raw_id = record.get("id", "").strip()
        source_id: UUID | None = None
        if raw_id:
            try:
                source_id = parse_uuid(raw_id)
            except EntryError as error:
                row_errors.append(_entry(error.code, line, "id"))
        if row_errors:
            errors.extend(row_errors)
            continue
        rows.append(
            _Row(
                line=line,
                item_name=item_name,
                item_key=item_key,
                item_id=item_id,
                as_of=as_of,
                value=value,
                currency=currency,
                note=note,
                created_at=created_at,
                source_id=source_id,
            )
        )
    return rows, errors


def _item_index(
    items: list[NetWorthItem],
) -> tuple[dict[UUID, NetWorthItem], dict[str, list[NetWorthItem]]]:
    by_lineage: dict[UUID, NetWorthItem] = {}
    by_name: dict[str, list[NetWorthItem]] = {}
    for item in items:
        by_lineage[item.id] = item
        if item.import_key and item.import_key.startswith("id:"):
            with contextlib.suppress(ValueError):
                by_lineage[UUID(item.import_key[3:])] = item
        by_name.setdefault(text_key(item.name), []).append(item)
    return by_lineage, by_name


def _resolve_item(
    row: _Row,
    by_lineage: dict[UUID, NetWorthItem],
    by_name: dict[str, list[NetWorthItem]],
) -> tuple[NetWorthItem | None, str | None]:
    if row.item_id is not None and row.item_id in by_lineage:
        candidates = [by_lineage[row.item_id]]
    else:
        candidates = by_name.get(row.item_key, [])
    if not candidates:
        return None, "item_not_found"
    active = [item for item in candidates if item.archived_at is None]
    if not active:
        return None, "item_archived"
    if len(active) > 1:
        return None, "item_ambiguous"
    return active[0], None


def _plan(
    session: Session,
    ctx: Context,
    batch: ImportBatch,
    parsed: ParsedUpload,
    *,
    out_of_time: Callable[[], bool],
) -> tuple[list[_Match], list[dict[str, object]], list[dict[str, object]]]:
    rows, errors = _parse_rows(parsed, batch, out_of_time=out_of_time)
    items = NetWorthItemRepository(session, ctx).ordered()
    by_lineage, by_name = _item_index(items)
    valuations = ItemValuationRepository(session, ctx).for_items([item.id for item in items])
    stored: dict[tuple[UUID, date], list[int]] = {}
    stored_ids: set[UUID] = set()
    for valuation in valuations:
        stored.setdefault((valuation.item_id, valuation.as_of), []).append(valuation.value_minor)
        stored_ids.add(valuation.id)
        if valuation.import_key and valuation.import_key.startswith("id:"):
            with contextlib.suppress(ValueError):
                stored_ids.add(UUID(valuation.import_key[3:]))
    base = ctx.clock.now()
    matches: list[_Match] = []
    skipped: list[dict[str, object]] = []
    seen_ids: set[UUID] = set()
    seen_rows: set[tuple[UUID, date, int, str, datetime | None]] = set()
    for index, row in enumerate(rows):
        item, problem = _resolve_item(row, by_lineage, by_name)
        if item is None:
            skipped.append(_entry(problem or "item_not_found", row.line, "item"))
            continue
        if item.valuation_mode != "manual":
            skipped.append(_entry("item_linked", row.line, "item"))
            continue
        if row.currency is not None and row.currency != item.currency:
            skipped.append(_entry("valuation_currency_mismatch", row.line, "currency"))
            continue
        try:
            value_minor = planned_minor(row.value, item.currency)
        except EntryError as error:
            errors.append(_entry(error.code, row.line, "value"))
            continue
        created_at = row.created_at or base + timedelta(microseconds=index)
        if row.source_id is not None:
            if row.source_id in seen_ids:
                matches.append(
                    _Match(row, item, value_minor, created_at, "duplicate", "duplicate_in_file")
                )
                skipped.append(_entry("duplicate_in_file", row.line, "id"))
                continue
            seen_ids.add(row.source_id)
            if row.source_id in stored_ids:
                matches.append(
                    _Match(row, item, value_minor, created_at, "duplicate", "already_imported")
                )
                skipped.append(_entry("already_imported", row.line, "id"))
                continue
        identity = (item.id, row.as_of, value_minor, row.note, row.created_at)
        if identity in seen_rows:
            matches.append(
                _Match(row, item, value_minor, created_at, "duplicate", "duplicate_in_file")
            )
            skipped.append(_entry("duplicate_in_file", row.line, "value"))
            continue
        seen_rows.add(identity)
        existing = stored.get((item.id, row.as_of), [])
        if existing:
            action = "duplicate" if value_minor in existing else "conflict"
            code = "already_imported" if action == "duplicate" else "changed_since_export"
            skipped.append(_entry(code, row.line, "value"))
        else:
            action = "create"
            code = None
        matches.append(
            _Match(
                row=row,
                item=item,
                value_minor=value_minor,
                created_at=created_at,
                action=action,
                code=code,
            )
        )
    return matches, skipped, errors


class ItemValuationsCsvAdapter(ImportAdapter):
    """``item_valuations.csv`` alone, merged into the existing history (F5.5)."""

    source = "item_valuations_csv"

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
        matches, skipped, errors = _plan(session, ctx, batch, parsed, out_of_time=out_of_time)
        duplicates = sum(match.action == "duplicate" for match in matches)
        conflicts = sum(match.action == "conflict" for match in matches)
        return {
            "counts": {
                "to_create": sum(match.action == "create" for match in matches),
                "duplicates_skipped": duplicates,
                "conflicts_skipped": conflicts,
                "invalid": len(errors),
                "mapping_invalid": 0,
            },
            "months": [],
            "categories": [],
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
        matches, skipped, errors = _plan(session, ctx, batch, parsed, out_of_time=lambda: False)
        repository = ItemValuationRepository(session, ctx)
        created = 0
        for match in matches:
            if match.action != "create":
                continue
            repository.create(
                item_id=match.item.id,
                as_of=match.row.as_of,
                value_minor=match.value_minor,
                currency=match.item.currency,
                note=match.row.note,
                import_batch_id=batch.id,
                import_key=(
                    f"id:{match.row.source_id}" if match.row.source_id is not None else None
                ),
                created_at=match.created_at,
            )
            created += 1
        return {
            "created": created,
            "duplicates": sum(match.action == "duplicate" for match in matches),
            "conflicts": sum(match.action == "conflict" for match in matches),
            "skipped": len(skipped) + len(errors),
            "item_valuations_created": created,
        }
