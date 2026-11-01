"""Import upload and batch lifecycle (F4.2)."""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from assetboard.clock import Clock
from assetboard.db.engine import run_immediate
from assetboard.db.models.identity import Workspace
from assetboard.db.models.import_batch import ImportBatch
from assetboard.db.repositories import (
    AuditEventRepository,
    CurrentWorkspaceRepository,
    ImportBatchRepository,
)
from assetboard.domain.archive import ArchiveError
from assetboard.domain.import_upload import parse_upload
from assetboard.domain.ledger_csv import TRANSACTION_EXPORT_COLUMNS, guard_formula
from assetboard.i18n import gettext
from assetboard.services.context import Context, Role, require_role, require_role_now
from assetboard.services.months import lock_month_writes

MAX_IMPORT_SECONDS = 60
IMPORT_FILE_RETENTION = timedelta(hours=24)
_WORKER = ThreadPoolExecutor(max_workers=2, thread_name_prefix="assetboard-import")
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._ -]")


def _(message: str) -> str:
    return message


_ERROR_TRANSLATIONS = {
    "invalid UTF-8": _("Invalid UTF-8 encoding."),
    "invalid CSV": _("The CSV data is invalid."),
    "file is empty": _("The file is empty."),
    "invalid column count": _("The CSV column count is invalid."),
    "row has a different number of columns": _(
        "The row has a different number of columns than the header."
    ),
    "too many data rows": _("The upload contains too many data rows."),
    "unknown file ignored": _("An unknown file in the archive was ignored."),
    "unknown column ignored": _("An unknown CSV column was ignored."),
    "duplicate member names": _("The archive contains duplicate file names."),
    "missing required members": _("The archive is missing required files."),
    "too many members": _("The archive contains too many files."),
    "unsafe member": _("The archive contains an unsafe file."),
    "uncompressed size or ratio limit": _("The archive exceeds an uncompressed size limit."),
    "unsupported archive format": _("The archive format is not supported."),
    "unsupported archive version": _("The archive version is not supported."),
    "invalid JSON": _("The archive contains invalid JSON."),
    "invalid UTF-8 or CSV": _("The archive contains invalid UTF-8 or CSV data."),
    "checksum mismatch": _("The file checksum does not match the manifest."),
    "byte count mismatch": _("The file size does not match the manifest."),
    "row count mismatch": _("The file row count does not match the manifest."),
    "stored upload checksum mismatch": _("The stored upload no longer matches its checksum."),
    "Stored upload expired after 24 hours.": _(
        "The upload expired after 24 hours and was removed."
    ),
    "Import planning exceeded 60 seconds.": _("Import planning exceeded 60 seconds."),
    "Import planning failed.": _("Import planning failed."),
    "invalid_upload": _("The file could not be validated."),
    "invalid delimiter option": _("The CSV delimiter option is invalid."),
}


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


class ImportAdapter:
    """Feature-specific row writer; F4.3/F4.4 provide the production adapters."""

    def plan(
        self,
        session: Session,
        ctx: Context,
        batch: ImportBatch,
        parsed: object,
        *,
        base_currency: str,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def apply(
        self,
        session: Session,
        ctx: Context,
        batch: ImportBatch,
        parsed: object,
        *,
        base_currency: str,
        skip_invalid: bool,
    ) -> dict[str, int]:
        raise NotImplementedError

    def rollback(
        self, session: Session, ctx: Context, batch: ImportBatch
    ) -> tuple[dict[str, int], tuple[str, ...]]:
        raise NotImplementedError

    def rollback_preview(
        self, session: Session, ctx: Context, batch: ImportBatch
    ) -> dict[str, int]:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class ImportBatchView:
    id: UUID
    source: str
    mode: str
    filename: str
    status: str
    created_at: datetime
    report: dict[str, Any] | None


def _batch_view(batch: ImportBatch) -> ImportBatchView:
    return ImportBatchView(
        id=batch.id,
        source=batch.source,
        mode=batch.mode,
        filename=batch.filename,
        status=batch.status,
        created_at=batch.created_at,
        report=batch.report,
    )


def _upload_path(data_dir: Path, workspace_id: UUID, batch_id: UUID) -> Path:
    return data_dir / "imports" / str(workspace_id) / str(batch_id)


def _safe_filename(filename: str) -> str:
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _SAFE_FILENAME.sub("_", basename).strip(" .") or "upload.csv"
    return cleaned[:255]


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
    required_role = Role.OWNER if mode == "restore" else Role.EDITOR
    require_role(ctx, required_role)
    digest = hashlib.sha256(content).hexdigest()
    path: Path | None = None

    def create(session: Session) -> UUID:
        nonlocal path
        require_role_now(session, ctx, required_role)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        repository = ImportBatchRepository(session, ctx)
        batch = repository.create(
            source="archive" if content.startswith(b"PK\x03\x04") else "transactions_csv",
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
    return {
        "file": error.file,
        "line": error.line or "",
        "column": error.column or "",
        "code": "invalid_upload",
        "message": error.message,
    }


def _plan_file(path: Path, *, delimiter: str = ",") -> tuple[str, dict[str, Any]]:
    parsed, errors = parse_upload(path.read_bytes(), delimiter=delimiter)
    if parsed is None:
        return "failed", {
            "counts": {
                "to_create": 0,
                "duplicates_skipped": 0,
                "conflicts_skipped": 0,
                "closed_months_skipped": 0,
                "ignored_categories_skipped": 0,
                "invalid": len(errors),
            },
            "months": [],
            "categories": [],
            "closed_months": [],
            "warnings": [],
            "skipped": [],
            "errors": [_error_dict(error) for error in errors],
        }

    row_count = len(parsed.archive.rows.transactions) if parsed.archive else len(parsed.csv_rows)
    warnings = [_error_dict(error) for error in parsed.warnings]
    if parsed.archive is None:
        known_columns = set(TRANSACTION_EXPORT_COLUMNS)
        warnings.extend(
            _error_dict(ArchiveError("upload.csv", "unknown column ignored", column=column))
            for column in parsed.csv_header
            if column not in known_columns
        )
    else:
        row_count = sum(
            len(rows)
            for rows in (
                parsed.archive.rows.categories,
                parsed.archive.rows.budget_lines,
                parsed.archive.rows.transactions,
                parsed.archive.rows.months,
            )
        )
    return "planned", {
        "source": parsed.source,
        "counts": {
            "to_create": 0,
            "duplicates_skipped": 0,
            "conflicts_skipped": 0,
            "closed_months_skipped": 0,
            "ignored_categories_skipped": 0,
            "invalid": parsed.invalid_row_count,
            "rows_received": row_count + parsed.invalid_row_count,
        },
        "months": [],
        "categories": [],
        "closed_months": [],
        "warnings": warnings,
        "skipped": [],
        "errors": [_error_dict(error) for error in parsed.row_errors],
    }


def _plan_worker(
    path: Path,
    factory: sessionmaker[Session],
    ctx: Context,
    batch_id: UUID,
    adapter: ImportAdapter | None,
) -> tuple[str, dict[str, Any]]:
    with factory() as session:
        batch = ImportBatchRepository(session, ctx).get(batch_id)
        if batch is None:
            raise ImportError("batch is not in this workspace")
        delimiter = ";" if batch.options.get("delimiter") == "semicolon" else ","
        status, report = _plan_file(path, delimiter=delimiter)
        if status != "planned" or adapter is None:
            return status, report
        parsed, errors = parse_upload(path.read_bytes(), delimiter=delimiter)
        if parsed is None:
            return "failed", {**report, "errors": [_error_dict(error) for error in errors]}
        base_currency = CurrentWorkspaceRepository(session, ctx).get().base_currency
        report = adapter.plan(session, ctx, batch, parsed, base_currency=base_currency)
    report.setdefault("warnings", [])
    report.setdefault("skipped", [])
    report.setdefault("errors", [])
    return status, report


def plan_import(
    factory: sessionmaker[Session],
    ctx: Context,
    data_dir: Path,
    batch_id: UUID,
    adapter: ImportAdapter | None = None,
) -> dict[str, Any]:
    require_role(ctx, Role.EDITOR)

    def locate(session: Session) -> Path:
        require_role_now(session, ctx, Role.EDITOR)
        batch = ImportBatchRepository(session, ctx).get_for_update(batch_id)
        if batch is None or batch.status != "uploaded":
            raise ImportError("batch is not awaiting a plan")
        return _upload_path(data_dir, ctx.workspace.id, batch.id)

    path = run_immediate(factory, locate)
    future = _WORKER.submit(_plan_worker, path, factory, ctx, batch_id, adapter)
    try:
        status, report = future.result(timeout=MAX_IMPORT_SECONDS)
    except TimeoutError:
        future.cancel()
        status = "failed"
        report = {
            "counts": {
                "to_create": 0,
                "duplicates_skipped": 0,
                "conflicts_skipped": 0,
                "closed_months_skipped": 0,
                "ignored_categories_skipped": 0,
                "invalid": 1,
            },
            "months": [],
            "categories": [],
            "closed_months": [],
            "warnings": [],
            "errors": [
                {
                    "file": "upload",
                    "line": "",
                    "column": "",
                    "code": "timeout",
                    "message": "Import planning exceeded 60 seconds.",
                }
            ],
        }
    except Exception:
        status = "failed"
        report = {
            "counts": {
                "to_create": 0,
                "duplicates_skipped": 0,
                "conflicts_skipped": 0,
                "closed_months_skipped": 0,
                "ignored_categories_skipped": 0,
                "invalid": 1,
            },
            "months": [],
            "categories": [],
            "closed_months": [],
            "warnings": [],
            "errors": [
                {
                    "file": "upload",
                    "line": "",
                    "column": "",
                    "code": "planning_failed",
                    "message": "Import planning failed.",
                }
            ],
        }

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


def apply_import(
    factory: sessionmaker[Session],
    ctx: Context,
    data_dir: Path,
    batch_id: UUID,
    adapter: ImportAdapter | None,
    *,
    skip_invalid: bool = False,
) -> dict[str, Any]:
    require_role(ctx, Role.EDITOR)
    if adapter is None:
        raise ImportUnavailable("No importer is registered for this upload type.")
    stored_path = _upload_path(data_dir, ctx.workspace.id, batch_id)

    def apply(session: Session) -> dict[str, Any]:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        lock_month_writes(session, ctx)
        batch = ImportBatchRepository(session, ctx).get_for_update(batch_id)
        if batch is None or batch.status != "planned":
            raise ImportError("batch is not ready to apply")
        if batch.mode == "restore":
            require_role_now(session, ctx, Role.OWNER)
        content = stored_path.read_bytes()
        if hashlib.sha256(content).hexdigest() != batch.file_sha256:
            batch.status = "failed"
            batch.report = {
                "counts": {
                    "to_create": 0,
                    "duplicates_skipped": 0,
                    "conflicts_skipped": 0,
                    "closed_months_skipped": 0,
                    "ignored_categories_skipped": 0,
                    "invalid": 1,
                },
                "months": [],
                "categories": [],
                "closed_months": [],
                "warnings": [],
                "errors": [
                    {
                        "file": "upload",
                        "line": "",
                        "column": "",
                        "code": "checksum_mismatch",
                        "message": "stored upload checksum mismatch",
                    }
                ],
            }
            return {"_failed": True, "report": batch.report}
        delimiter = ";" if batch.options.get("delimiter") == "semicolon" else ","
        parsed, _errors = parse_upload(content, delimiter=delimiter)
        if parsed is None:
            batch.status = "failed"
            report = _plan_file(stored_path, delimiter=delimiter)[1]
            batch.report = report
            return {"_failed": True, "report": report}
        base_currency = CurrentWorkspaceRepository(session, ctx).get().base_currency
        report = adapter.plan(session, ctx, batch, parsed, base_currency=base_currency)
        if report["counts"] != (batch.report or {}).get("counts", {}):
            batch.report = report
            return {"_confirmation_required": True, "report": report}
        if batch.mode == "restore" and report["counts"].get("invalid", 0):
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
    if result.get("_confirmation_required"):
        raise ImportConfirmationRequired(result["report"])
    _delete_upload(stored_path)
    return result


def rollback_import(
    factory: sessionmaker[Session],
    ctx: Context,
    batch_id: UUID,
    adapter: ImportAdapter | None,
) -> dict[str, int]:
    require_role(ctx, Role.EDITOR)
    if adapter is None:
        raise ImportUnavailable("No rollback handler is registered for this upload type.")

    def rollback(session: Session) -> dict[str, int]:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        batch = ImportBatchRepository(session, ctx).get_for_update(batch_id)
        if batch is None or batch.status != "applied" or batch.mode != "merge":
            raise ImportError("batch cannot be rolled back")
        counts, closed_months = adapter.rollback(session, ctx, batch)
        if closed_months:
            raise ImportClosedMonths(closed_months)
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
                "mode": batch.mode,
                "transactions_deleted": counts.get("deleted", 0),
                "transactions_edited": counts.get("edited", 0),
                "budget_lines": counts.get("budget_lines", 0),
                "categories_to_archive": counts.get("categories_to_archive", 0),
                "transactions_kept": counts.get("kept", 0),
            },
        )
        return counts

    return run_immediate(factory, rollback)


def preview_rollback_import(
    factory: sessionmaker[Session],
    ctx: Context,
    batch_id: UUID,
    adapter: ImportAdapter | None,
) -> tuple[ImportBatchView, dict[str, int]]:
    require_role(ctx, Role.EDITOR)
    if adapter is None:
        raise ImportUnavailable("No rollback handler is registered for this upload type.")
    with factory() as session:
        batch = ImportBatchRepository(session, ctx).get(batch_id)
        if batch is None or batch.status != "applied" or batch.mode != "merge":
            raise ImportError("batch cannot be rolled back")
        counts = adapter.rollback_preview(session, ctx, batch)
        return _batch_view(batch), counts


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
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=("file", "line", "column", "code", "message"),
        lineterminator="\r\n",
    )
    writer.writeheader()
    report_rows = report or {}
    rows = list(report_rows.get("errors") or []) + list(report_rows.get("skipped") or [])
    for error in rows:
        row = dict(error)
        message = str(row.get("message", ""))
        row["message"] = gettext(
            _ERROR_TRANSLATIONS.get(message, _("The file could not be validated.")), locale
        )
        writer.writerow(
            {key: guard_formula(str(row.get(key, ""))) for key in writer.fieldnames or ()}
        )
    return output.getvalue().encode("utf-8")


@dataclass(frozen=True, slots=True)
class _WorkspaceRef:
    id: UUID


@dataclass(frozen=True, slots=True)
class _WorkspaceScope:
    workspace: _WorkspaceRef


def cleanup_expired_imports(factory: sessionmaker[Session], data_dir: Path, clock: Clock) -> int:
    """Delete stale upload files and mark their batches failed, without crossing tenants."""
    with factory() as session:
        workspace_ids = list(session.scalars(select(Workspace.id)))
    cutoff = clock.now() - IMPORT_FILE_RETENTION
    expired_count = 0
    for workspace_id in workspace_ids:
        scope = _WorkspaceScope(_WorkspaceRef(workspace_id))

        def expire(session: Session, scope: _WorkspaceScope = scope) -> list[tuple[UUID, UUID]]:
            expired = ImportBatchRepository(session, scope).expired_before(cutoff)
            files: list[tuple[UUID, UUID]] = []
            for batch in expired:
                batch.status = "failed"
                batch.report = {
                    "counts": {
                        "to_create": 0,
                        "duplicates_skipped": 0,
                        "conflicts_skipped": 0,
                        "closed_months_skipped": 0,
                        "ignored_categories_skipped": 0,
                        "invalid": 0,
                    },
                    "months": [],
                    "categories": [],
                    "closed_months": [],
                    "warnings": [
                        {
                            "file": "upload",
                            "line": "",
                            "column": "",
                            "message": "Stored upload expired after 24 hours.",
                        }
                    ],
                    "errors": [],
                }
                files.append((batch.workspace_id, batch.id))
            return files

        files = run_immediate(factory, expire)
        for owner_id, batch_id in files:
            _delete_upload(_upload_path(data_dir, owner_id, batch_id))
        expired_count += len(files)
    return expired_count
