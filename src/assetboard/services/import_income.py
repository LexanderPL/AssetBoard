"""Standalone ``work_logs.csv`` import (F3.8).

A work-log history kept outside AssetBoard, or the ``work_logs.csv`` file of an archive, can be
merged into an existing workspace. Rows match an income source by its lineage id first and by
name otherwise. A month that already has a log is never overwritten: an identical row is a
duplicate and a different one a conflict, both listed in the report.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy.orm import Session

from assetboard.config import Settings
from assetboard.db.models.import_batch import ImportBatch
from assetboard.db.models.income import IncomeSource
from assetboard.db.repositories import (
    IncomeSourceRepository,
    MonthStateRepository,
    ScheduledOccurrenceRepository,
    WorkLogRepository,
)
from assetboard.domain.import_csv import parse_decimal, parse_month, parse_uuid
from assetboard.domain.import_upload import DEADLINE_CHECK_ROWS, ParsedUpload, ParseTimeout
from assetboard.domain.income import pay_date
from assetboard.domain.months import YearMonth
from assetboard.domain.text import text_key
from assetboard.domain.transactions import EntryError, clean_description
from assetboard.services.context import Context
from assetboard.services.imports import ImportAdapter
from assetboard.services.income import resync_work_month

FILE = "work_logs.csv"
SOURCE = "work_logs_csv"


def _entry(code: str, line: int | None = None, column: str = "") -> dict[str, object]:
    return {"file": FILE, "line": line or "", "column": column, "code": code, "message": code}


@dataclass(frozen=True, slots=True)
class _Row:
    line: int
    source_name: str
    source_key: str
    source_id: UUID | None
    work_month: date
    units: Decimal
    derived_from_days: bool


@dataclass(frozen=True, slots=True)
class _Match:
    row: _Row
    source: IncomeSource
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
            source_name = clean_description(record.get("income_source", ""))
        except ValueError:
            row_errors.append(_entry("invalid_name", line, "income_source"))
            source_name = ""
        if not source_name:
            row_errors.append(_entry("invalid_name", line, "income_source"))
        raw_source_id = record.get("income_source_id", "").strip()
        source_id: UUID | None = None
        if raw_source_id:
            try:
                source_id = parse_uuid(raw_source_id)
            except EntryError as error:
                row_errors.append(_entry(error.code, line, "income_source_id"))
        try:
            work_month = parse_month(record.get("work_month", "")).first_day
        except EntryError as error:
            row_errors.append(_entry(error.code, line, "work_month"))
            work_month = date(1900, 1, 1)
        try:
            units = parse_decimal(record.get("units", ""), decimal=decimal)
        except EntryError as error:
            row_errors.append(_entry(error.code, line, "units"))
            units = Decimal(0)
        if units.is_finite() is False or units < 0:
            row_errors.append(_entry("invalid_units", line, "units"))
        else:
            exponent = units.as_tuple().exponent
            integer_digits = max(units.adjusted() + 1, 0)
            scale = max(-exponent, 0) if isinstance(exponent, int) else 19
            if integer_digits > 20 or scale > 18 or integer_digits + scale > 38:
                row_errors.append(_entry("invalid_units", line, "units"))
        raw_derived = record.get("derived_from_days", "").strip().lower()
        if raw_derived not in {"", "true", "false"}:
            row_errors.append(_entry("invalid_boolean", line, "derived_from_days"))
        if row_errors:
            errors.extend(row_errors)
            continue
        rows.append(
            _Row(
                line=line,
                source_name=source_name,
                source_key=text_key(source_name),
                source_id=source_id,
                work_month=work_month,
                units=units,
                derived_from_days=raw_derived == "true",
            )
        )
    return rows, errors


def _source_index(
    sources: list[IncomeSource],
) -> tuple[dict[UUID, IncomeSource], dict[str, list[IncomeSource]]]:
    by_id = {source.id: source for source in sources}
    by_name: dict[str, list[IncomeSource]] = {}
    for source in sources:
        by_name.setdefault(text_key(source.name), []).append(source)
    return by_id, by_name


def _resolve_source(
    row: _Row,
    by_id: dict[UUID, IncomeSource],
    by_name: dict[str, list[IncomeSource]],
) -> tuple[IncomeSource | None, str | None]:
    candidates: list[IncomeSource] = []
    if row.source_id is not None and row.source_id in by_id:
        candidates = [by_id[row.source_id]]
    else:
        candidates = by_name.get(row.source_key, [])
    if not candidates:
        return None, "income_source_not_found"
    active = [source for source in candidates if source.archived_at is None]
    if not active:
        return None, "income_source_archived"
    if len(active) > 1:
        return None, "income_source_ambiguous"
    if active[0].kind != "variable":
        return None, "income_source_not_variable"
    return active[0], None


def create_work_log(
    session: Session,
    ctx: Context,
    source: IncomeSource,
    work_month: date,
    units: Decimal,
) -> tuple[UUID, list[UUID]]:
    """Create one merged work log and recalculate its month.

    A merged log is always stored as directly entered: work calendars are restore-only, so a
    ``derived_from_days`` flag would point at days the merge did not carry. Returns the log id
    and the ids of the income/tax occurrences the recalculation created (not the ones it
    updated). A rollback recalculates the month again if its occurrence still exists.
    """
    occurrences = ScheduledOccurrenceRepository(session, ctx)
    existing = occurrences.for_income_work_month(source.id, work_month)
    old_children: set[UUID] = (
        {child.id for child in occurrences.children(existing.id)} if existing else set()
    )
    log = WorkLogRepository(session, ctx).create(
        income_source_id=source.id,
        work_month=work_month,
        units=units,
        derived_from_days=False,
    )
    resync_work_month(session, ctx, source.id, work_month)
    current = occurrences.for_income_work_month(source.id, work_month)
    created: list[UUID] = []
    if current is not None:
        if existing is None or current.id != existing.id:
            created.append(current.id)
        created.extend(
            child.id for child in occurrences.children(current.id) if child.id not in old_children
        )
    return log.id, created


def record_work_log_rollback(
    batch: ImportBatch,
    work_log_ids: list[str],
    occurrence_ids: list[str],
    resync_months: list[tuple[UUID, date]],
) -> None:
    """Remember the rows this merge created so the generic rollback can remove them."""
    report = dict(batch.report or {})
    raw = report.get("work_log_rollback")
    existing = dict(cast("dict[str, object]", raw)) if isinstance(raw, dict) else {}
    logs = list(cast("list[object]", existing.get("work_logs", [])))
    occurrences = list(cast("list[object]", existing.get("occurrences", [])))
    resync = list(cast("list[object]", existing.get("resync", [])))
    logs.extend(work_log_ids)
    occurrences.extend(occurrence_ids)
    resync.extend(
        {"income_source_id": str(source_id), "work_month": work_month.isoformat()}
        for source_id, work_month in resync_months
    )
    report["work_log_rollback"] = {
        "work_logs": logs,
        "occurrences": occurrences,
        "resync": resync,
    }
    batch.report = report


def _plan(
    session: Session,
    ctx: Context,
    batch: ImportBatch,
    parsed: ParsedUpload,
    *,
    out_of_time: Callable[[], bool],
) -> tuple[list[_Match], list[dict[str, object]], list[dict[str, object]]]:
    rows, errors = _parse_rows(parsed, batch, out_of_time=out_of_time)
    sources = IncomeSourceRepository(session, ctx).ordered()
    by_id, by_name = _source_index(sources)
    logs = WorkLogRepository(session, ctx)
    stored = {
        (row.income_source_id, row.work_month): row
        for source in sources
        for row in logs.for_source(source.id)
    }
    closed_months = MonthStateRepository(session, ctx).closed_months()
    occurrences = ScheduledOccurrenceRepository(session, ctx)
    matches: list[_Match] = []
    skipped: list[dict[str, object]] = []
    seen: set[tuple[UUID, date]] = set()
    for row in rows:
        source, problem = _resolve_source(row, by_id, by_name)
        if source is None:
            skipped.append(_entry(problem or "income_source_not_found", row.line, "income_source"))
            continue
        due = pay_date(row.work_month, source.pay_day, source.pay_lag_months)
        occurrence = occurrences.for_income_work_month(source.id, row.work_month)
        locked = {YearMonth.from_date(row.work_month), YearMonth.from_date(due)}
        if occurrence is not None:
            locked.add(YearMonth.from_date(occurrence.due_date))
        if locked & closed_months:
            skipped.append(_entry("closed_month", row.line, "work_month"))
            continue
        if occurrence is not None and occurrence.status == "posted":
            matches.append(_Match(row, source, "conflict", "income_received"))
            skipped.append(_entry("income_received", row.line, "work_month"))
            continue
        key = (source.id, row.work_month)
        if key in seen:
            matches.append(_Match(row, source, "duplicate", "duplicate_in_file"))
            skipped.append(_entry("duplicate_in_file", row.line, "work_month"))
            continue
        seen.add(key)
        existing = stored.get(key)
        if existing is not None:
            if existing.units == row.units:
                matches.append(_Match(row, source, "duplicate", "already_logged"))
                skipped.append(_entry("already_logged", row.line, "work_month"))
            else:
                matches.append(_Match(row, source, "conflict", "work_log_changed"))
                skipped.append(_entry("work_log_changed", row.line, "work_month"))
            continue
        matches.append(_Match(row, source, "create", None))
    return matches, skipped, errors


class WorkLogsCsvAdapter(ImportAdapter):
    """``work_logs.csv`` alone, merged into the existing work-log history."""

    source = SOURCE

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
        secrets_passphrase: str | None = None,
        instance_settings: Settings | None = None,
    ) -> dict[str, int]:
        del base_currency, skip_invalid, secrets_passphrase, instance_settings
        matches, skipped, errors = _plan(session, ctx, batch, parsed, out_of_time=lambda: False)
        created = 0
        work_log_ids: list[str] = []
        occurrence_ids: list[str] = []
        resync_months: list[tuple[UUID, date]] = []
        for match in matches:
            if match.action != "create":
                continue
            log_id, new_occurrences = create_work_log(
                session, ctx, match.source, match.row.work_month, match.row.units
            )
            work_log_ids.append(str(log_id))
            occurrence_ids.extend(str(value) for value in new_occurrences)
            resync_months.append((match.source.id, match.row.work_month))
            created += 1
        if work_log_ids:
            record_work_log_rollback(batch, work_log_ids, occurrence_ids, resync_months)
        return {
            "created": created,
            "duplicates": sum(match.action == "duplicate" for match in matches),
            "conflicts": sum(match.action == "conflict" for match in matches),
            "skipped": len(skipped) + len(errors),
            "work_logs_created": created,
        }
