"""Import upload and batch lifecycle (F4.2).

Upload stores the file and an ``uploaded`` batch; planning parses it in a worker thread with a
time limit and stores the dry-run report (``planned``); apply re-parses and re-plans inside the
write transaction; rollback and cancel undo a batch. The adapter of the batch's ``source``
(F4.3, F4.4; see ``ImportAdapterRegistry``) writes the rows; dedupe helpers for adapters live
in ``services.import_dedupe``, and rollback is done here for every source alike.

**Time limit.** ``MAX_IMPORT_SECONDS`` counts from when the worker starts the job, not from when
it was queued. The worker passes ``out_of_time`` to the parser (checked every few thousand rows)
and to the adapter's ``plan``, so a job over the limit stops itself and frees its worker. A job
that cannot start within ``MAX_IMPORT_QUEUE_SECONDS`` (both workers busy) fails as "busy".

**Report entries** (``errors``, ``warnings``, ``skipped``) are ``{file, line, column, code,
message}``. ``code`` is stable; ``report_message`` translates known codes and otherwise shows
the original ``message`` (adapters may add their own codes).
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import threading
import time
import unicodedata
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.clock import Clock
from assetboard.db.engine import run_immediate
from assetboard.db.models.import_batch import ImportBatch
from assetboard.db.models.ledger import BudgetLine, Category, Transaction
from assetboard.db.repositories import (
    AuditEventRepository,
    BudgetLineRepository,
    CategoryRepository,
    CurrentWorkspaceRepository,
    ImportBatchRepository,
    ImportCategoryMappingRepository,
    IncomeSourceRepository,
    IncomeSourceTaxRuleRepository,
    MonthStateRepository,
    ScheduledItemRepository,
    ScheduledOccurrenceRepository,
    TaxRuleRepository,
    TaxRuleVersionRepository,
    TransactionRepository,
    WorkDayRepository,
    WorkLogRepository,
)
from assetboard.db.repositories.base import WorkspaceScope
from assetboard.domain.archive import ArchiveError
from assetboard.domain.import_upload import (
    ZIP_LOCAL_FILE_SIGNATURE,
    ParsedUpload,
    ParseTimeout,
    parse_upload,
)
from assetboard.domain.ledger_csv import guard_formula
from assetboard.domain.months import YearMonth
from assetboard.i18n import gettext
from assetboard.services.archive import workspace_is_empty
from assetboard.services.categories import usage_by_category
from assetboard.services.context import Context, Role, require_role, require_role_now
from assetboard.services.months import lock_month_writes

MAX_IMPORT_SECONDS = 60
MAX_IMPORT_QUEUE_SECONDS = 60
IMPORT_FILE_RETENTION = timedelta(hours=24)
# A file with no uploaded/planned batch is removed once it is this old (it may belong to an
# upload whose transaction has not committed yet).
ORPHAN_FILE_GRACE = timedelta(hours=1)
_WORKER = ThreadPoolExecutor(max_workers=2, thread_name_prefix="assetboard-import")
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._ -]")
# Letters that NFKD does not decompose into an ASCII base letter.
_ASCII_LETTERS = str.maketrans(
    {"Ł": "L", "ł": "l", "Đ": "D", "đ": "d", "Ø": "O", "ø": "o", "ß": "ss",
     "Æ": "AE", "æ": "ae", "Œ": "OE", "œ": "oe", "Þ": "Th", "þ": "th"}
)  # fmt: skip
_monotonic: Callable[[], float] = time.monotonic  # replaced in tests


def _(message: str) -> str:
    return message


# Report codes -> translatable messages (shown on the page and in the errors CSV).
REPORT_MESSAGES: dict[str, str] = {
    "invalid_utf8": _("Invalid UTF-8 encoding."),
    "invalid_csv": _("The CSV data is invalid."),
    "empty_file": _("The file is empty."),
    "invalid_column_count": _("The CSV column count is invalid."),
    "row_column_count": _("The row has a different number of columns than the header."),
    "too_many_rows": _("The upload contains too many data rows."),
    "unknown_file": _("An unknown file in the archive was ignored."),
    "unknown_column": _("An unknown CSV column was ignored."),
    "duplicate_members": _("The archive contains duplicate file names."),
    "missing_members": _("The archive is missing required files."),
    "too_many_members": _("The archive contains too many files."),
    "unsafe_member": _("The archive contains an unsafe file."),
    "size_limit": _("The archive exceeds an uncompressed size limit."),
    "unsupported_format": _("The archive format is not supported."),
    "unsupported_version": _("The archive version is not supported."),
    "invalid_json": _("The archive contains invalid JSON."),
    "invalid_archive_csv": _("The archive contains invalid UTF-8 or CSV data."),
    "checksum_mismatch": _("The file checksum does not match the manifest."),
    "byte_count_mismatch": _("The file size does not match the manifest."),
    "row_count_mismatch": _("The file row count does not match the manifest."),
    "invalid_delimiter": _("The CSV delimiter option is invalid."),
    "stored_checksum_mismatch": _("The stored upload no longer matches its checksum."),
    "stored_file_missing": _("The stored upload is missing; upload the file again."),
    "expired": _("The upload expired after 24 hours and was removed."),
    "timeout": _("Import planning exceeded 60 seconds."),
    "busy": _("Other imports are being checked; try again in a minute."),
    "planning_failed": _("Import planning failed."),
    # Dedupe (services.import_dedupe) and closed months.
    "already_imported": _("Already imported."),
    "imported_before_deleted": _("Imported before and deleted; it is not brought back."),
    "changed_since_export": _("Changed since the export; the stored row is kept."),
    "duplicate_in_file": _("The same row appears earlier in this file."),
    "conflict_in_file": _("An earlier row of this file sets another value; it is kept."),
    "planned_value_exists": _("The same planned value already exists."),
    "planned_value_differs": _("A different planned value exists; the stored value is kept."),
    "closed_month": _("The month is closed."),
    # F4.3 lone CSV imports and category mapping.
    "partial_restore": _("This file is only part of a full restore."),
    "missing_column": _("A required column is missing."),
    "duplicate_column": _("The header contains a duplicate column."),
    "deleted_in_source": _("Deleted in the source; it is not imported in merge mode."),
    "future_date": _("The date is in the future; it is not imported as an actual entry."),
    "ignored_category": _("The category is ignored for this import."),
    "category_archived": _("The category is archived; no planned value can be added."),
    "category_transfer": _("Transfer categories cannot have planned values."),
    "category_differs": _("The category already exists with different values; it is kept."),
    "category_exists": _("A category with this name already exists."),
    "kind_required": _(
        "The category kind is required: the name is unknown or exists as both an expense and "
        "an income category."
    ),
    "invalid_direction": _("The direction must be expense or income."),
    "invalid_kind": _("The kind must be expense or income."),
    "invalid_date": _("Enter a date as YYYY-MM-DD."),
    "date_out_of_range": _("The date is outside 1900-01-01 to 2199-12-31."),
    "invalid_month": _("Enter a month as YYYY-MM."),
    "invalid_amount": _("Enter a plain positive amount."),
    "amount_not_positive": _("The amount must be greater than zero."),
    "amount_too_large": _("The amount is too large."),
    "too_many_decimals": _("The amount has too many decimal places."),
    "amount_negative": _("The amount cannot be negative."),
    "invalid_currency": _("The currency must be the workspace base currency."),
    "invalid_description": _("The description contains characters that are not allowed."),
    "description_too_long": _("The description is too long."),
    "invalid_category_name": _("Enter a category name of 1 to 80 characters."),
    "invalid_treatment": _("Choose a valid category treatment."),
    "invalid_color": _("Choose a valid colour."),
    "invalid_sort_order": _("The sort order must be a whole number."),
    "invalid_id": _("The id is not a valid UUID."),
    "invalid_timestamp": _("The timestamp is invalid."),
    "invalid_row": _("The row is invalid."),
    "mapping_category_missing": _("The mapped category does not exist."),
    "mapping_kind_mismatch": _("The mapped category has the wrong kind."),
    "duplicate_create_name": _("Two new categories have the same kind and name."),
    # F4.4 archive restore and merge.
    "currency_mismatch": _(
        "The archive's base currency differs from this workspace's base currency."
    ),
    "workspace_not_empty": _(
        "Restore is available only in an empty workspace (the untouched suggested categories "
        "are fine)."
    ),
    "duplicate_category_name": _(
        "Two active categories in the archive have the same kind and name."
    ),
    "invalid_workspace_settings": _("The archive's workspace settings are not supported."),
    "newer_major_version": _("The archive was made by a newer version of the app."),
    "newer_minor_version": _(
        "The archive was made by a newer minor version; unknown data was ignored."
    ),
    # Restore rollback reasons.
    "restore_rows_added": _("The workspace has rows that this restore did not write."),
    "restore_rows_changed": _("Rows written by this restore were changed after it."),
    "restore_months_changed": _("Month states written by this restore were changed after it."),
    "restore_settings_changed": _("Workspace settings changed since the restore."),
    "restore_m3_rows_added": _(
        "Recurring items, income sources, work logs or tax rules were added after the restore."
    ),
    "restore_m3_references": _(
        "Recurring items, income sources, work logs or tax rules reference rows written by "
        "this restore."
    ),
    "restore_previous_settings_missing": _(
        "This restore did not record the workspace settings it replaced."
    ),
}

# ``domain.archive`` / ``domain.import_upload`` messages -> report codes.
_PARSER_CODES: dict[str, str] = {
    "invalid UTF-8": "invalid_utf8",
    "invalid CSV": "invalid_csv",
    "file is empty": "empty_file",
    "invalid column count": "invalid_column_count",
    "row has a different number of columns": "row_column_count",
    "too many data rows": "too_many_rows",
    "unknown file ignored": "unknown_file",
    "unknown column ignored": "unknown_column",
    "duplicate member names": "duplicate_members",
    "missing required members": "missing_members",
    "too many members": "too_many_members",
    "unsafe member": "unsafe_member",
    "uncompressed size or ratio limit": "size_limit",
    "unsupported archive format": "unsupported_format",
    "unsupported archive version": "unsupported_version",
    "made by a newer version of the app": "newer_major_version",
    "invalid JSON": "invalid_json",
    "invalid UTF-8 or CSV": "invalid_archive_csv",
    "checksum mismatch": "checksum_mismatch",
    "byte count mismatch": "byte_count_mismatch",
    "row count mismatch": "row_count_mismatch",
    "invalid delimiter option": "invalid_delimiter",
    "duplicate column name": "duplicate_column",
    "only part of a full restore": "partial_restore",
    "unknown file": "unknown_file",
}


def report_message(entry: Mapping[str, Any], locale: str) -> str:
    """The text of a report entry in ``locale``: its code's message, else its own message."""
    known = REPORT_MESSAGES.get(str(entry.get("code", "")))
    if known is not None:
        return gettext(known, locale)
    return str(entry.get("message", ""))


class ImportError(Exception):
    pass


class ImportUnavailable(ImportError):
    """The validated upload has no importer until its owning feature is installed."""


class ImportConfirmationRequired(ImportError):
    def __init__(self, report: dict[str, Any]) -> None:
        super().__init__("the import plan changed and needs confirmation")
        self.report = report


class ImportClosedMonths(ImportError):
    def __init__(self, months: tuple[str, ...]) -> None:
        super().__init__("the batch affects months that are now closed")
        self.months = months


class ImportMappingInvalid(ImportError):
    def __init__(self, report: dict[str, Any]) -> None:
        super().__init__("the import has unresolved or invalid category mappings")
        self.report = report


class ImportRefused(ImportError):
    """The whole upload is refused (a merge archive's base currency differs, for example)."""


class ImportRestoreUnavailable(ImportError):
    """Restore was requested for a workspace that is not empty."""


class ImportRestoreRequiresArchive(ImportError):
    """Restore was requested for an upload that is not a complete ZIP archive."""


class ImportRestoreRollbackBlocked(ImportError):
    """The workspace no longer holds exactly what the restore wrote."""

    def __init__(self, reasons: tuple[str, ...]) -> None:
        super().__init__("the workspace changed after the restore")
        self.reasons = reasons


@dataclass(frozen=True, slots=True)
class RollbackPreview:
    """The rollback confirmation page, for a merge or a restore batch."""

    batch: ImportBatchView
    mode: str
    counts: dict[str, int]
    closed_months: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()
    settings_restored: tuple[str, ...] = ()


def _never() -> bool:
    return False


class ImportAdapter:
    """Feature-specific planner and row writer; F4.3/F4.4 provide the production adapters.

    ``plan`` reads only and returns the report (``counts`` and the per-month and per-category
    tables); it should ask ``out_of_time`` now and then and raise ``ParseTimeout`` when it
    answers true. ``apply`` writes the rows with ``source=import``, ``import_batch_id`` and
    ``import_key`` (``services.import_dedupe`` decides which rows). Rollback is not part of the
    contract: it works from ``import_batch_id`` for every source (``rollback_import``).
    """

    def plan(
        self,
        session: Session,
        ctx: Context,
        batch: ImportBatch,
        parsed: ParsedUpload,
        *,
        base_currency: str,
        out_of_time: Callable[[], bool] = _never,
    ) -> dict[str, Any]:
        raise NotImplementedError

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
        raise NotImplementedError


class ImportAdapterRegistry:
    """The adapter of each batch ``source`` (``transactions_csv``, ``archive``, ...)."""

    def __init__(self, adapters: Mapping[str, ImportAdapter] | None = None) -> None:
        self._adapters = dict(adapters or {})

    @property
    def sources(self) -> frozenset[str]:
        return frozenset(self._adapters)

    def for_source(self, source: str) -> ImportAdapter | None:
        return self._adapters.get(source)


@dataclass(frozen=True, slots=True)
class ImportBatchView:
    id: UUID
    source: str
    mode: str
    filename: str
    status: str
    created_at: datetime
    report: dict[str, Any] | None
    options: dict[str, Any]


def _batch_view(batch: ImportBatch) -> ImportBatchView:
    return ImportBatchView(
        id=batch.id,
        source=batch.source,
        mode=batch.mode,
        filename=batch.filename,
        status=batch.status,
        created_at=batch.created_at,
        report=batch.report,
        options=batch.options,
    )


def _upload_path(data_dir: Path, workspace_id: UUID, batch_id: UUID) -> Path:
    return data_dir / "imports" / str(workspace_id) / str(batch_id)


def _safe_filename(filename: str) -> str:
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    # Keep base letters ("Łódź wydatki.csv" -> "Lodz wydatki.csv") instead of "_" for each.
    decomposed = "".join(
        char
        for char in unicodedata.normalize("NFKD", basename.translate(_ASCII_LETTERS))
        if not unicodedata.combining(char)
    )
    cleaned = _SAFE_FILENAME.sub("_", decomposed).strip(" .") or "upload.csv"
    return cleaned[:255]


def _counts(**values: int) -> dict[str, int]:
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
        **values,
    }


def _entry(
    file: str, code: str, message: str, line: object = "", column: object = ""
) -> dict[str, object]:
    return {"file": file, "line": line, "column": column, "code": code, "message": message}


def _report(
    *,
    counts: dict[str, int] | None = None,
    errors: list[dict[str, object]] | None = None,
    warnings: list[dict[str, object]] | None = None,
) -> dict[str, Any]:
    return {
        "counts": counts or _counts(),
        "months": [],
        "categories": [],
        "mapping": [],
        "mapping_errors": [],
        "closed_months": [],
        "warnings": warnings or [],
        "skipped": [],
        "errors": errors or [],
    }


def _failure(code: str, message: str) -> dict[str, Any]:
    return _report(counts=_counts(invalid=1), errors=[_entry("upload", code, message)])


def upload_import(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    data_dir: Path,
    filename: str,
    content: bytes,
    options: dict[str, object] | None = None,
    mode: Literal["restore", "merge"] = "merge",
) -> UUID:
    if mode not in {"restore", "merge"}:
        raise ImportError("invalid import mode")
    required_role = Role.OWNER if mode == "restore" else Role.EDITOR
    require_role(ctx, required_role)
    is_archive = content.startswith(ZIP_LOCAL_FILE_SIGNATURE)
    if mode == "restore" and not is_archive:
        raise ImportRestoreRequiresArchive
    digest = hashlib.sha256(content).hexdigest()
    path: Path | None = None

    def create(session: Session) -> UUID:
        nonlocal path
        require_role_now(session, ctx, required_role)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        if mode == "restore" and not workspace_is_empty(session, ctx):
            raise ImportRestoreUnavailable
        repository = ImportBatchRepository(session, ctx)
        batch = repository.create(
            source="archive" if is_archive else "transactions_csv",
            mode=mode,
            filename=_safe_filename(filename),
            file_sha256=digest,
            size_bytes=len(content),
            options=options or {},
            status="uploaded",
            started_by_user_id=ctx.user.id,
        )
        path = _upload_path(data_dir, ctx.workspace.id, batch.id)
        imports_root = data_dir / "imports"
        imports_root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(imports_root, 0o700)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        os.chmod(path.parent, 0o700)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stored:
            stored.write(content)
        return batch.id

    try:
        return run_immediate(factory, create)
    except BaseException:
        if path is not None:
            path.unlink(missing_ok=True)
        raise


def _error_dict(error: ArchiveError) -> dict[str, object]:
    code = _PARSER_CODES.get(error.message, "invalid_upload")
    if code == "invalid_upload" and error.message.startswith("missing required column:"):
        code = "missing_column"
    if code == "invalid_upload" and error.message.startswith("newer minor version:"):
        code = "newer_minor_version"
    return _entry(
        error.file,
        code,
        error.message,
        error.line or "",
        error.column or "",
    )


def _delimiter_option(options: Mapping[str, object]) -> str:
    value = options.get("delimiter", "detect")
    return {"comma": ",", "semicolon": ";"}.get(str(value), "detect")


def _plan_file(
    content: bytes, *, delimiter: str = "detect", out_of_time: Callable[[], bool] = _never
) -> tuple[str, dict[str, Any], ParsedUpload | None]:
    parsed, errors = parse_upload(content, delimiter=delimiter, out_of_time=out_of_time)
    if parsed is None:
        return (
            "failed",
            _report(
                counts=_counts(invalid=len(errors)),
                errors=[_error_dict(error) for error in errors],
            ),
            None,
        )

    warnings = [_error_dict(error) for error in parsed.warnings]
    if parsed.archive is None:
        row_count = len(parsed.csv_rows)
    else:
        rows = parsed.archive.rows
        row_count = sum(
            len(part)
            for part in (rows.categories, rows.budget_lines, rows.transactions, rows.months)
        )
    report = _report(
        counts=_counts(
            invalid=parsed.invalid_row_count,
            rows_received=row_count + parsed.invalid_row_count,
        ),
        errors=[_error_dict(error) for error in parsed.row_errors],
        warnings=warnings,
    )
    report["source"] = parsed.source
    return "planned", report, parsed


def _merge_adapter_report(base: dict[str, Any], adapter_report: dict[str, Any]) -> dict[str, Any]:
    """Combine the parser's report with the adapter's (warnings, errors and invalid counts)."""
    counts = _counts(**adapter_report.get("counts", {}))
    counts["invalid"] += base.get("counts", {}).get("invalid", 0)
    counts["rows_received"] = base.get("counts", {}).get("rows_received", 0)
    merged = {**base, **adapter_report}
    merged["counts"] = counts
    merged["warnings"] = [*base.get("warnings", []), *adapter_report.get("warnings", [])]
    merged["errors"] = [*base.get("errors", []), *adapter_report.get("errors", [])]
    for key in ("months", "categories", "mapping", "mapping_errors", "closed_months", "skipped"):
        merged.setdefault(key, [])
    return merged


def _plan_worker(
    path: Path,
    factory: sessionmaker[Session],
    ctx: Context,
    batch_id: UUID,
    adapters: ImportAdapterRegistry,
    out_of_time: Callable[[], bool],
) -> tuple[str, dict[str, Any]]:
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return "failed", _failure("stored_file_missing", "stored upload is missing")
    with factory() as session:
        batch = ImportBatchRepository(session, ctx).get(batch_id)
        if batch is None:
            raise ImportError("batch is not in this workspace")
        delimiter = _delimiter_option(batch.options)
        status, base_report, parsed = _plan_file(
            content, delimiter=delimiter, out_of_time=out_of_time
        )
        if status != "planned" or parsed is None:
            return status, base_report
        adapter = adapters.for_source(parsed.source)
        if adapter is None:
            return status, base_report
        base_currency = CurrentWorkspaceRepository(session, ctx).get().base_currency
        report = _merge_adapter_report(
            base_report,
            adapter.plan(
                session,
                ctx,
                batch,
                parsed,
                base_currency=base_currency,
                out_of_time=out_of_time,
            ),
        )
        report["source"] = parsed.source
    for key in ("warnings", "skipped", "errors", "months", "categories", "closed_months"):
        report.setdefault(key, [])
    return status, report


def _run_plan_job(
    path: Path,
    factory: sessionmaker[Session],
    ctx: Context,
    batch_id: UUID,
    adapters: ImportAdapterRegistry,
) -> tuple[str, dict[str, Any]]:
    """Run ``_plan_worker`` with the time limit counted from the job's start."""
    started = threading.Event()
    stop = threading.Event()

    def job() -> tuple[str, dict[str, Any]]:
        started.set()
        deadline = _monotonic() + MAX_IMPORT_SECONDS

        def out_of_time() -> bool:
            return stop.is_set() or _monotonic() >= deadline

        try:
            return _plan_worker(path, factory, ctx, batch_id, adapters, out_of_time)
        except ParseTimeout:
            return "failed", _failure("timeout", "Import planning exceeded 60 seconds.")

    future = _WORKER.submit(job)
    if not started.wait(MAX_IMPORT_QUEUE_SECONDS):
        if future.cancel():
            return "failed", _failure("busy", "other imports are being planned")
        started.wait()
    try:
        # A little grace beyond the limit: the worker notices the deadline itself.
        return future.result(timeout=MAX_IMPORT_SECONDS + 5)
    except FutureTimeout:
        stop.set()
        return "failed", _failure("timeout", "Import planning exceeded 60 seconds.")
    except Exception:
        return "failed", _failure("planning_failed", "Import planning failed.")


def plan_import(
    factory: sessionmaker[Session],
    ctx: Context,
    data_dir: Path,
    batch_id: UUID,
    adapters: ImportAdapterRegistry | None = None,
) -> dict[str, Any]:
    require_role(ctx, Role.EDITOR)

    def locate(session: Session) -> Path:
        require_role_now(session, ctx, Role.EDITOR)
        batch = ImportBatchRepository(session, ctx).get_for_update(batch_id)
        if batch is None or batch.status != "uploaded":
            raise ImportError("batch is not awaiting a plan")
        return _upload_path(data_dir, ctx.workspace.id, batch.id)

    path = run_immediate(factory, locate)
    status, report = _run_plan_job(
        path, factory, ctx, batch_id, adapters or ImportAdapterRegistry()
    )

    def save(session: Session) -> dict[str, Any]:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        batch = ImportBatchRepository(session, ctx).get_for_update(batch_id)
        if batch is None or batch.status != "uploaded":
            raise ImportError("batch changed while planning")
        batch.status = status
        batch.source = str(report.get("source", batch.source))
        batch.report = report
        return report

    try:
        result = run_immediate(factory, save)
    except BaseException:
        if status == "failed":
            _delete_upload(path)
        raise
    if status == "failed":
        _delete_upload(path)
    return result


def _delete_upload(path: Path) -> None:
    path.unlink(missing_ok=True)
    for parent in (path.parent, path.parent.parent):
        try:
            parent.rmdir()
        except OSError:
            break


def cancel_import(
    factory: sessionmaker[Session], ctx: Context, data_dir: Path, batch_id: UUID
) -> None:
    require_role(ctx, Role.EDITOR)

    def cancel(session: Session) -> Path:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        batch = ImportBatchRepository(session, ctx).get_for_update(batch_id)
        if batch is None or batch.status not in {"uploaded", "planned", "failed"}:
            raise ImportError("batch cannot be cancelled")
        batch.status = "cancelled"
        return _upload_path(data_dir, ctx.workspace.id, batch.id)

    _delete_upload(run_immediate(factory, cancel))


def _mapping_signature(options: Mapping[str, object]) -> tuple[tuple[str, ...], ...]:
    """A stable comparison of the posted mapping choices (order does not matter)."""
    raw = options.get("mappings")
    if not isinstance(raw, list):
        return ()
    fields = (
        "source_id",
        "kind",
        "source_name_key",
        "action",
        "category_id",
        "create_name",
        "create_treatment",
        "create_color",
    )
    entries: list[tuple[str, ...]] = []
    for raw_item in cast("list[object]", raw):
        if not isinstance(raw_item, dict):
            continue
        item = cast("dict[str, object]", raw_item)
        entries.append(tuple(str(item.get(field, "")) for field in fields))
    return tuple(sorted(entries))


def _merged_options(
    current: Mapping[str, object] | None, changes: Mapping[str, object] | None
) -> dict[str, object]:
    merged = dict(current or {})
    if changes:
        merged.update(changes)
    return merged


def apply_import(
    factory: sessionmaker[Session],
    ctx: Context,
    data_dir: Path,
    batch_id: UUID,
    adapters: ImportAdapterRegistry,
    *,
    skip_invalid: bool = False,
    options: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    require_role(ctx, Role.EDITOR)
    stored_path = _upload_path(data_dir, ctx.workspace.id, batch_id)

    def apply(session: Session) -> dict[str, Any]:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        lock_month_writes(session, ctx)
        batch = ImportBatchRepository(session, ctx).get_for_update(batch_id)
        if batch is None or batch.status != "planned":
            raise ImportError("batch is not ready to apply")
        adapter = adapters.for_source(batch.source)
        if adapter is None:
            raise ImportUnavailable("No importer is registered for this upload type.")
        if batch.mode == "restore":
            require_role_now(session, ctx, Role.OWNER)
        previous_mapping = (batch.report or {}).get("mapping", [])
        choices_changed = (
            isinstance(options, Mapping)
            and "mappings" in options
            and _mapping_signature(options) != _mapping_signature({"mappings": previous_mapping})
        )
        new_options = _merged_options(batch.options, options)
        batch.options = new_options
        try:
            content = stored_path.read_bytes()
        except FileNotFoundError:
            batch.status = "failed"
            batch.report = _failure("stored_file_missing", "stored upload is missing")
            return {"_failed": True, "report": batch.report}
        if hashlib.sha256(content).hexdigest() != batch.file_sha256:
            batch.status = "failed"
            batch.report = _failure("stored_checksum_mismatch", "stored upload checksum mismatch")
            return {"_failed": True, "report": batch.report}
        status, base_report, parsed = _plan_file(
            content, delimiter=_delimiter_option(batch.options)
        )
        if status == "failed" or parsed is None:
            batch.status = "failed"
            batch.report = base_report
            return {"_failed": True, "report": base_report}
        base_currency = CurrentWorkspaceRepository(session, ctx).get().base_currency
        report = _merge_adapter_report(
            base_report,
            adapter.plan(session, ctx, batch, parsed, base_currency=base_currency),
        )
        report["source"] = parsed.source
        if report.get("refused"):
            batch.report = report
            return {"_refused": True, "report": report}
        if report["counts"].get("mapping_invalid", 0):
            batch.report = report
            return {"_mapping_invalid": True, "report": report}
        if choices_changed or report["counts"] != (batch.report or {}).get("counts", {}):
            batch.report = report
            return {"_confirmation_required": True, "report": report}
        if batch.mode == "restore" and report["counts"].get("invalid", 0):
            batch.report = report
            errors = cast("list[dict[str, object]]", report.get("errors", []))
            if any(entry.get("code") == "workspace_not_empty" for entry in errors):
                return {"_restore_unavailable": True, "report": report}
            raise ImportError("restore requires a completely valid upload")
        if report["counts"].get("invalid", 0) and not skip_invalid:
            raise ImportError("confirm skipping invalid rows before applying")
        counts = adapter.apply(
            session,
            ctx,
            batch,
            parsed,
            base_currency=base_currency,
            skip_invalid=skip_invalid,
        )
        now = ctx.clock.now()
        batch.status = "applied"
        batch.applied_at = now
        if batch.mode != "restore":
            AuditEventRepository(session, ctx).record(
                actor_user_id=ctx.user.id,
                action="import.applied",
                entity_type="import_batch",
                entity_id=str(batch.id),
                ts=now,
                data={
                    "source": batch.source,
                    "mode": batch.mode,
                    "transactions_created": counts.get("created", 0),
                    "duplicates": counts.get("duplicates", 0),
                    "conflicts": counts.get("conflicts", 0),
                    "rows_skipped": counts.get("skipped", 0),
                },
            )
        return {"counts": counts, "report": report}

    result = run_immediate(factory, apply)
    if result.get("_failed"):
        _delete_upload(stored_path)
        raise ImportError("stored upload failed validation")
    if result.get("_refused"):
        raise ImportRefused("the upload was refused as a whole")
    if result.get("_restore_unavailable"):
        raise ImportRestoreUnavailable
    if result.get("_mapping_invalid"):
        raise ImportMappingInvalid(result["report"])
    if result.get("_confirmation_required"):
        raise ImportConfirmationRequired(result["report"])
    _delete_upload(stored_path)
    return result


def update_import_preview(
    factory: sessionmaker[Session],
    ctx: Context,
    data_dir: Path,
    batch_id: UUID,
    adapters: ImportAdapterRegistry,
    *,
    options: Mapping[str, object],
) -> dict[str, Any]:
    """Save the mapping choices on the batch and re-run the dry-run (no rows are written)."""
    require_role(ctx, Role.EDITOR)

    def prepare(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        batch = ImportBatchRepository(session, ctx).get_for_update(batch_id)
        if batch is None or batch.status != "planned":
            raise ImportError("batch is not ready to preview")
        batch.options = _merged_options(batch.options, options)
        batch.status = "uploaded"

    run_immediate(factory, prepare)
    return plan_import(factory, ctx, data_dir, batch_id, adapters)


@dataclass(frozen=True, slots=True)
class _RollbackPlan:
    """What rolling a batch back does, decided once for the preview and the rollback."""

    transactions: tuple[Transaction, ...]  # the batch's rows that are not deleted yet
    edited: int  # of those, changed after the import
    lines_to_remove: tuple[BudgetLine, ...]
    lines_kept: int  # changed after the import (``updated_at > applied_at``)
    categories_created: tuple[Category, ...]  # every category the batch created
    categories_to_delete: tuple[Category, ...]
    categories_to_archive: tuple[Category, ...]
    closed_months: tuple[str, ...]

    def counts(self) -> dict[str, int]:
        return {
            "deleted": len(self.transactions),
            "edited": self.edited,
            "budget_lines": len(self.lines_to_remove),
            "budget_lines_kept": self.lines_kept,
            "categories_to_delete": len(self.categories_to_delete),
            "categories_to_archive": len(self.categories_to_archive),
        }


def _rollback_plan(
    session: Session, ctx: Context, batch: ImportBatch, *, lock: bool
) -> _RollbackPlan:
    applied_at = batch.applied_at
    if applied_at is None:
        raise ImportError("applied batch has no applied timestamp")
    transactions = TransactionRepository(session, ctx).for_import_batch(batch.id, lock=lock)
    lines = BudgetLineRepository(session, ctx).for_import_batch(batch.id, lock=lock)
    categories = CategoryRepository(session, ctx).for_import_batch(batch.id, lock=lock)
    live = tuple(row for row in transactions if row.deleted_at is None)
    to_remove = tuple(line for line in lines if line.updated_at <= applied_at)

    # F2.1's rule: a category still referenced after the rollback is archived, an unused one is
    # deleted. References count soft-deleted transactions too (they keep pointing at their
    # category), so a created category whose rows are only soft-deleted here is archived;
    # only the removed budget lines stop referencing it.
    removed_lines: dict[UUID, int] = {}
    for line in to_remove:
        removed_lines[line.category_id] = removed_lines.get(line.category_id, 0) + 1
    usage = usage_by_category(session, ctx)
    to_delete: list[Category] = []
    to_archive: list[Category] = []
    for category in categories:
        used = usage.get(category.id)
        remaining = (used.references if used else 0) - removed_lines.get(category.id, 0)
        if remaining <= 0:
            to_delete.append(category)
        elif category.archived_at is None:
            to_archive.append(category)

    affected = {YearMonth.from_date(row.date) for row in live}
    affected.update(YearMonth.from_date(line.month) for line in to_remove)
    closed = MonthStateRepository(session, ctx).closed_months()
    return _RollbackPlan(
        transactions=live,
        edited=sum(row.updated_at > applied_at for row in live),
        lines_to_remove=to_remove,
        lines_kept=len(lines) - len(to_remove),
        categories_created=tuple(categories),
        categories_to_delete=tuple(to_delete),
        categories_to_archive=tuple(to_archive),
        closed_months=tuple(str(month) for month in sorted(affected & closed)),
    )


@dataclass(frozen=True, slots=True)
class _RestoreRollbackPlan:
    transactions: tuple[Transaction, ...]
    lines: tuple[BudgetLine, ...]
    categories: tuple[Category, ...]
    months: tuple[YearMonth, ...]
    reasons: tuple[str, ...]
    settings: dict[str, object]

    def counts(self) -> dict[str, int]:
        return {
            "deleted": len(self.transactions),
            "budget_lines": len(self.lines),
            "categories": len(self.categories),
            "months": len(self.months),
        }


_M3_REPOSITORIES = (
    ScheduledItemRepository,
    ScheduledOccurrenceRepository,
    IncomeSourceRepository,
    IncomeSourceTaxRuleRepository,
    WorkLogRepository,
    WorkDayRepository,
    TaxRuleRepository,
    TaxRuleVersionRepository,
)
_M3_REFERENCE_ATTRIBUTES = (
    "category_id",
    "payment_category_id",
    "transaction_id",
    "previous_transaction_id",
)


def _restore_rollback_plan(
    session: Session, ctx: Context, batch: ImportBatch, *, lock: bool
) -> _RestoreRollbackPlan:
    applied_at = batch.applied_at
    if applied_at is None:
        raise ImportError("applied batch has no applied timestamp")
    transactions = TransactionRepository(session, ctx).for_import_batch(batch.id, lock=lock)
    lines = BudgetLineRepository(session, ctx).for_import_batch(batch.id, lock=lock)
    categories = CategoryRepository(session, ctx).for_import_batch(batch.id, lock=lock)
    all_transactions = TransactionRepository(session, ctx).list()
    all_lines = BudgetLineRepository(session, ctx).list()
    all_categories = CategoryRepository(session, ctx).list()
    all_months = MonthStateRepository(session, ctx).list()
    reasons: set[str] = set()
    if (
        len(all_transactions) != len(transactions)
        or any(row.import_batch_id != batch.id for row in all_transactions)
        or len(all_lines) != len(lines)
        or any(row.import_batch_id != batch.id for row in all_lines)
        or len(all_categories) != len(categories)
        or any(row.import_batch_id != batch.id for row in all_categories)
    ):
        reasons.add("restore_rows_added")
    if (
        any(row.updated_at > applied_at for row in transactions)
        or any(row.updated_at > applied_at for row in lines)
        or any(row.updated_at > applied_at for row in categories)
    ):
        reasons.add("restore_rows_changed")
    raw_months = (batch.report or {}).get("restore_months")
    expected_months: set[YearMonth] = set()
    if isinstance(raw_months, list):
        for raw in cast("list[object]", raw_months):
            try:
                expected_months.add(YearMonth.parse(str(raw)))
            except ValueError:
                reasons.add("restore_months_changed")
    else:
        reasons.add("restore_months_changed")
    stored_months = {YearMonth.from_date(row.month) for row in all_months}
    if stored_months != expected_months or any(row.updated_at > applied_at for row in all_months):
        reasons.add("restore_months_changed")
    workspace = CurrentWorkspaceRepository(session, ctx).get()
    if workspace.updated_at > applied_at:
        reasons.add("restore_settings_changed")
    previous = batch.previous_settings
    if not isinstance(previous, dict):
        reasons.add("restore_previous_settings_missing")
        previous = {}
    category_ids = {row.id for row in categories}
    transaction_ids = {row.id for row in transactions}
    for repository_class in _M3_REPOSITORIES:
        for row in repository_class(session, ctx).list():
            if row.created_at > applied_at:
                reasons.add("restore_m3_rows_added")
            for attribute in _M3_REFERENCE_ATTRIBUTES:
                value = getattr(row, attribute, None)
                if isinstance(value, UUID) and (value in category_ids or value in transaction_ids):
                    reasons.add("restore_m3_references")
    month_values: list[YearMonth] = []
    for raw in cast("list[object]", raw_months or []):
        try:
            month_values.append(YearMonth.parse(str(raw)))
        except ValueError:
            continue
    return _RestoreRollbackPlan(
        transactions=tuple(transactions),
        lines=tuple(lines),
        categories=tuple(categories),
        months=tuple(month_values),
        reasons=tuple(sorted(reasons)),
        settings=cast("dict[str, object]", previous),
    )


def _rollback_restore(
    session: Session, ctx: Context, batch: ImportBatch, plan: _RestoreRollbackPlan
) -> dict[str, int]:
    if plan.reasons:
        raise ImportRestoreRollbackBlocked(plan.reasons)
    TransactionRepository(session, ctx).delete_for_import_batch(batch.id)
    lines = BudgetLineRepository(session, ctx)
    for line in plan.lines:
        lines.delete(line)
    session.flush()
    categories = CategoryRepository(session, ctx)
    for category in plan.categories:
        categories.delete(category)
    MonthStateRepository(session, ctx).delete_for_months(plan.months)
    session.flush()
    CurrentWorkspaceRepository(session, ctx).update(**plan.settings)
    now = ctx.clock.now()
    batch.status = "rolled_back"
    batch.rolled_back_at = now
    AuditEventRepository(session, ctx).record(
        actor_user_id=ctx.user.id,
        action="import.rolled_back",
        entity_type="import_batch",
        entity_id=str(batch.id),
        ts=now,
        data={
            "source": batch.source,
            "mode": "restore",
            "transactions_deleted": len(plan.transactions),
            "budget_lines": len(plan.lines),
            "categories_deleted": len(plan.categories),
            "months_deleted": len(plan.months),
            "settings_restored": ",".join(sorted(plan.settings)),
        },
    )
    return plan.counts()


def rollback_import(
    factory: sessionmaker[Session],
    ctx: Context,
    batch_id: UUID,
) -> dict[str, int]:
    """Undo an applied batch.

    A merge batch is soft-deleted and keeps later edits; a restore batch is hard-deleted and
    puts the previous workspace settings back, but only while nothing in the workspace changed
    since ``applied_at``.
    """
    require_role(ctx, Role.EDITOR)

    def rollback(session: Session) -> dict[str, int]:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        lock_month_writes(session, ctx)
        batch = ImportBatchRepository(session, ctx).get_for_update(batch_id)
        if batch is None or batch.status != "applied":
            raise ImportError("batch cannot be rolled back")
        if batch.mode == "restore":
            require_role_now(session, ctx, Role.OWNER)
            restore_plan = _restore_rollback_plan(session, ctx, batch, lock=True)
            return _rollback_restore(session, ctx, batch, restore_plan)
        if batch.mode != "merge":
            raise ImportError("batch cannot be rolled back")
        plan = _rollback_plan(session, ctx, batch, lock=True)
        if plan.closed_months:
            raise ImportClosedMonths(plan.closed_months)
        now = ctx.clock.now()
        transactions = TransactionRepository(session, ctx)
        for transaction in plan.transactions:
            transactions.update(transaction.id, deleted_at=now)
        lines = BudgetLineRepository(session, ctx)
        for line in plan.lines_to_remove:
            lines.delete(line)
        session.flush()
        raw_started_months = (batch.report or {}).get("plan_started_months", [])
        if isinstance(raw_started_months, list):
            month_states = MonthStateRepository(session, ctx)
            for raw_month in cast("list[object]", raw_started_months):
                try:
                    month = YearMonth.parse(str(raw_month))
                except ValueError:
                    continue
                if lines.for_month(month):
                    continue
                state = month_states.for_month(month)
                if state is not None and state.plan_started_at is not None:
                    month_states.update(state.id, plan_started_at=None)
        ImportCategoryMappingRepository(session, ctx).delete_for_categories(
            [category.id for category in plan.categories_created]
        )
        categories = CategoryRepository(session, ctx)
        for category in plan.categories_to_delete:
            categories.delete(category)
        for category in plan.categories_to_archive:
            categories.update(category.id, archived_at=now)
        batch.status = "rolled_back"
        batch.rolled_back_at = now
        counts = plan.counts()
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="import.rolled_back",
            entity_type="import_batch",
            entity_id=str(batch.id),
            ts=now,
            data={
                "source": batch.source,
                "mode": batch.mode,
                "transactions_deleted": counts["deleted"],
                "transactions_edited": counts["edited"],
                "budget_lines": counts["budget_lines"],
                "budget_lines_kept": counts["budget_lines_kept"],
                "categories_deleted": counts["categories_to_delete"],
                "categories_archived": counts["categories_to_archive"],
            },
        )
        return counts

    return run_immediate(factory, rollback)


def preview_rollback_import(
    factory: sessionmaker[Session],
    ctx: Context,
    batch_id: UUID,
) -> RollbackPreview:
    """The rollback confirm page: counts, blockers and (for a restore) restored settings."""
    require_role(ctx, Role.EDITOR)
    with factory() as session:
        batch = ImportBatchRepository(session, ctx).get(batch_id)
        if batch is None or batch.status != "applied":
            raise ImportError("batch cannot be rolled back")
        if batch.mode == "restore":
            require_role(ctx, Role.OWNER)
            plan = _restore_rollback_plan(session, ctx, batch, lock=False)
            return RollbackPreview(
                batch=_batch_view(batch),
                mode="restore",
                counts=plan.counts(),
                blocked_reasons=plan.reasons,
                settings_restored=tuple(sorted(plan.settings)),
            )
        if batch.mode != "merge":
            raise ImportError("batch cannot be rolled back")
        plan = _rollback_plan(session, ctx, batch, lock=False)
        return RollbackPreview(
            batch=_batch_view(batch),
            mode="merge",
            counts=plan.counts(),
            closed_months=plan.closed_months,
        )


def list_import_batches(
    factory: sessionmaker[Session], ctx: Context, *, limit: int = 20
) -> list[ImportBatchView]:
    require_role(ctx, Role.EDITOR)
    with factory() as session:
        return [_batch_view(batch) for batch in ImportBatchRepository(session, ctx).recent(limit)]


def get_import_batch(
    factory: sessionmaker[Session], ctx: Context, batch_id: UUID
) -> ImportBatchView | None:
    require_role(ctx, Role.EDITOR)
    with factory() as session:
        batch = ImportBatchRepository(session, ctx).get(batch_id)
        return _batch_view(batch) if batch is not None else None


def report_errors_csv(report: dict[str, Any] | None, locale: str = "en") -> bytes:
    """Errors and skipped rows as ``file,line,column,code,message`` (formula-guarded)."""
    output = io.StringIO(newline="")
    fieldnames = ("file", "line", "column", "code", "message")
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\r\n")
    writer.writeheader()
    report_rows = report or {}
    rows = list(report_rows.get("errors") or []) + list(report_rows.get("skipped") or [])
    for entry in rows:
        row = {**entry, "message": report_message(entry, locale)}
        writer.writerow({key: guard_formula(str(row.get(key, ""))) for key in fieldnames})
    return output.getvalue().encode("utf-8")


def cleanup_expired_imports(
    factory: sessionmaker[Session], scope: WorkspaceScope, data_dir: Path, clock: Clock
) -> int:
    """One workspace's hourly cleanup: expire batches whose file is older than 24 hours
    (``failed``, file deleted), then remove files under ``imports/<workspace>/`` that no
    uploaded or planned batch owns (left by a crash between commit and delete) once they are
    older than ``ORPHAN_FILE_GRACE``. Returns the number of expired batches."""
    now = clock.now()
    cutoff = now - IMPORT_FILE_RETENTION

    def expire(session: Session) -> tuple[list[UUID], set[str]]:
        repository = ImportBatchRepository(session, scope)
        expired = repository.expired_before(cutoff)
        for batch in expired:
            batch.status = "failed"
            batch.report = _report(
                warnings=[_entry("upload", "expired", "Stored upload expired after 24 hours.")]
            )
        active = {str(batch.id) for batch in repository.awaiting_apply()}
        return [batch.id for batch in expired], active

    expired_ids, active = run_immediate(factory, expire)
    for batch_id in expired_ids:
        _delete_upload(_upload_path(data_dir, scope.workspace.id, batch_id))
    workspace_dir = data_dir / "imports" / str(scope.workspace.id)
    if workspace_dir.is_dir():
        oldest_allowed = (now - ORPHAN_FILE_GRACE).timestamp()
        for path in workspace_dir.iterdir():
            if path.name in active or not path.is_file():
                continue
            if path.stat().st_mtime < oldest_allowed:
                _delete_upload(path)
    return len(expired_ids)
