"""Standalone ``reference_rate_values.csv`` import (F7.8).

Reference-rate values are the only loan archive file that can be merged alone. A row names its
series, so a hand-made file does not need ids: the series is matched by its case-folded name and
created when absent. The same series and effective date makes an identical value a duplicate
and a different value a conflict; both leave the stored value untouched.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from assetboard.db.models.import_batch import ImportBatch
from assetboard.db.repositories import ReferenceRateRepository, ReferenceRateValueRepository
from assetboard.domain.import_csv import parse_date
from assetboard.domain.import_upload import DEADLINE_CHECK_ROWS, ParsedUpload, ParseTimeout
from assetboard.domain.loans import clean_loan_name
from assetboard.domain.text import text_key
from assetboard.domain.transactions import EntryError
from assetboard.services.context import Context
from assetboard.services.imports import ImportAdapter

FILE = "reference_rate_values.csv"
MAX_RATE_BP = 10_000


def _entry(code: str, line: int | None = None, column: str = "") -> dict[str, object]:
    return {"file": FILE, "line": line or "", "column": column, "code": code, "message": code}


@dataclass(frozen=True, slots=True)
class _Row:
    line: int
    name: str
    key: str
    effective_from: date
    value_bp: int


@dataclass(frozen=True, slots=True)
class _Match:
    row: _Row
    action: str  # create, duplicate or conflict
    code: str | None
    create_rate: bool


def _parse_bp(value: str) -> int:
    text = value.strip()
    if not re.fullmatch(r"[0-9]{1,5}", text):
        raise EntryError("invalid_rate")
    number = int(text)
    if not 0 <= number <= MAX_RATE_BP:
        raise EntryError("invalid_rate")
    return number


def _parse_rows(
    parsed: ParsedUpload, *, out_of_time: Callable[[], bool]
) -> tuple[list[_Row], list[dict[str, object]]]:
    rows: list[_Row] = []
    errors: list[dict[str, object]] = []
    for index, cells in enumerate(parsed.csv_rows):
        line = index + 2
        if index % DEADLINE_CHECK_ROWS == 0 and out_of_time():
            raise ParseTimeout
        record = dict(zip(parsed.csv_header, cells, strict=True))
        try:
            name = clean_loan_name(record.get("reference_rate", ""))
        except (ValueError, TypeError):
            errors.append(_entry("invalid_name", line, "reference_rate"))
            continue
        try:
            effective_from = parse_date(record.get("effective_from", ""))
        except EntryError as error:
            errors.append(_entry(error.code, line, "effective_from"))
            continue
        try:
            value_bp = _parse_bp(record.get("value_bp", ""))
        except EntryError as error:
            errors.append(_entry(error.code, line, "value_bp"))
            continue
        rows.append(_Row(line, name, text_key(name), effective_from, value_bp))
    return rows, errors


def _plan(
    session: Session,
    ctx: Context,
    parsed: ParsedUpload,
    *,
    out_of_time: Callable[[], bool],
) -> tuple[list[_Match], list[dict[str, object]], int]:
    rows, errors = _parse_rows(parsed, out_of_time=out_of_time)
    rates = ReferenceRateRepository(session, ctx)
    values = ReferenceRateValueRepository(session, ctx)
    existing = {rate.name_key: rate for rate in rates.ordered()}
    stored: dict[str, dict[date, int]] = {
        key: {value.effective_from: value.value_bp for value in values.for_rate(rate.id)}
        for key, rate in existing.items()
    }
    planned: dict[tuple[str, date], int] = {}
    planned_rates: set[str] = set()
    matches: list[_Match] = []
    skipped: list[dict[str, object]] = []
    for row in rows:
        key = (row.key, row.effective_from)
        if key in planned:
            action = "duplicate" if planned[key] == row.value_bp else "conflict"
            code = "duplicate_in_file" if action == "duplicate" else "conflict_in_file"
        elif row.key in existing:
            if row.effective_from in stored[row.key]:
                action = (
                    "duplicate"
                    if stored[row.key][row.effective_from] == row.value_bp
                    else "conflict"
                )
                code = (
                    "already_imported" if action == "duplicate" else "reference_rate_value_differs"
                )
            else:
                action, code = "create", None
        else:
            action, code = "create", None
        create_rate = (
            action == "create" and row.key not in existing and row.key not in planned_rates
        )
        if action == "create":
            planned[key] = row.value_bp
            planned_rates.add(row.key)
        else:
            skipped.append(_entry(str(code), row.line, "value_bp"))
        matches.append(_Match(row, action, code, create_rate))
    return matches, [*errors, *skipped], len(planned_rates - set(existing))


class ReferenceRateValuesCsvAdapter(ImportAdapter):
    """``reference_rate_values.csv`` alone, merged into the existing series (F7.8)."""

    source = "reference_rate_values_csv"

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
        del base_currency, batch
        matches, skipped, new_rates = _plan(session, ctx, parsed, out_of_time=out_of_time)
        duplicates = sum(match.action == "duplicate" for match in matches)
        conflicts = sum(match.action == "conflict" for match in matches)
        return {
            "counts": {
                "to_create": sum(match.action == "create" for match in matches),
                "duplicates_skipped": duplicates,
                "conflicts_skipped": conflicts,
                "invalid": sum(
                    entry["code"] in {"invalid_name", "invalid_date", "invalid_rate"}
                    for entry in skipped
                ),
                "mapping_invalid": 0,
                "reference_rates_created": new_rates,
            },
            "months": [],
            "categories": [],
            "mapping": [],
            "mapping_errors": [],
            "closed_months": [],
            "warnings": [],
            "skipped": skipped,
            "errors": [
                entry
                for entry in skipped
                if entry["code"] in {"invalid_name", "invalid_date", "invalid_rate"}
            ],
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
        del base_currency, skip_invalid, batch
        matches, skipped, _new_rates = _plan(session, ctx, parsed, out_of_time=lambda: False)
        rates = ReferenceRateRepository(session, ctx)
        values = ReferenceRateValueRepository(session, ctx)
        created_rates: dict[str, UUID] = {}
        created = 0
        for match in matches:
            if match.action != "create":
                continue
            row = match.row
            existing = rates.by_name_key(row.key)
            rate_id = existing.id if existing is not None else created_rates.get(row.key)
            if rate_id is None:
                rate_id = rates.create(name=row.name, name_key=row.key, notes="").id
                created_rates[row.key] = rate_id
            values.create(
                reference_rate_id=rate_id,
                effective_from=row.effective_from,
                value_bp=row.value_bp,
            )
            created += 1
        return {
            "created": created,
            "duplicates": sum(match.action == "duplicate" for match in matches),
            "conflicts": sum(match.action == "conflict" for match in matches),
            "skipped": len(skipped),
            "reference_rates_created": len(created_rates),
            "reference_rate_values_created": created,
        }
