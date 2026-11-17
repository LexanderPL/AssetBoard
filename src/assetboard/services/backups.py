"""Database backups, retention and restore (F9.4).

The scheduled job copies the SQLite database with the SQLite backup API, which gives a
consistent snapshot while the application is running (including its write-ahead log). Files
live in ``DATA_DIR/backups``; ``BackupRun`` rows are instance-level history. Retention is
grandfather-father-son and applies only to scheduled copies; manual and pre-restore copies are
kept until an administrator removes them by hand.

Failures are recorded and sent to the enabled notification channels of active instance
administrators through the ``Notifier`` interface, so tests never touch the network.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import unquote
from uuid import UUID, uuid4

from alembic.script import ScriptDirectory
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from assetboard.clock import Clock
from assetboard.config import Settings
from assetboard.db.backup_sqlite import (
    SQLiteFileError,
    verify_integrity,
)
from assetboard.db.backup_sqlite import (
    copy_database as copy_sqlite_database,
)
from assetboard.db.backup_sqlite import (
    read_revisions as read_sqlite_revisions,
)
from assetboard.db.engine import create_database_engine
from assetboard.db.migrate import alembic_config, upgrade_database
from assetboard.db.models.backups import BackupRun
from assetboard.db.repositories import InstanceNotificationReader
from assetboard.domain.backups import (
    SCHEDULED,
    BackupRef,
    RetentionPolicy,
    backup_due,
    next_scheduled_at,
    plan_retention,
)
from assetboard.i18n.locale import resolve_locale
from assetboard.services.instance_settings import get_settings
from assetboard.services.notification_messages import backup_failure_message
from assetboard.services.notifications import decrypt_url
from assetboard.services.notify import Notifier

logger = logging.getLogger(__name__)

BACKUP_DIRECTORY = "backups"
RECENT_RUNS = 10
ERROR_DATABASE_MISSING = "database_missing"
ERROR_COPY_FAILED = "copy_failed"
ERROR_INTEGRITY_FAILED = "integrity_failed"
ERROR_NOT_SQLITE = "not_sqlite"
ERROR_STORAGE = "storage_error"
ORIGINS = ("scheduled", "manual", "pre_restore")
_PREFIXES = {
    "scheduled": "assetboard",
    "manual": "assetboard-manual",
    "pre_restore": "assetboard-pre-restore",
}


class BackupError(Exception):
    """A backup attempt failed after its run row and notification were handled."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BackupUnavailable(BackupError):
    """The configured database cannot be backed up by this feature (e.g. PostgreSQL)."""


class RestoreError(Exception):
    """A restore was refused or failed. ``code`` is stable; ``message`` is for the CLI."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class BackupResult:
    id: UUID | None
    path: Path
    size_bytes: int
    started_at: datetime
    finished_at: datetime


@dataclass(frozen=True, slots=True)
class BackupRunView:
    id: UUID
    started_at: datetime
    finished_at: datetime | None
    status: str
    origin: str
    size_bytes: int | None
    size_label: str | None
    error: str | None
    downloadable: bool
    filename: str | None


@dataclass(frozen=True, slots=True)
class BackupPanel:
    available: bool
    reason: str | None
    directory: str | None
    runs: tuple[BackupRunView, ...]
    last_success: BackupRunView | None
    last_failure: BackupRunView | None
    next_run_at: datetime | None
    retention: RetentionPolicy
    scheduler_enabled: bool


@dataclass(frozen=True, slots=True)
class RestoreInspection:
    revisions: tuple[str, ...]
    state: Literal["current", "older", "newer", "unknown"]


@dataclass(frozen=True, slots=True)
class RestoreResult:
    target: Path
    pre_restore: Path | None
    migrated: bool
    warnings: tuple[str, ...]


# Database location and files


def database_backend(settings: Settings) -> str | None:
    if settings.database_url is None:
        return None
    return make_url(settings.database_url).get_backend_name()


def sqlite_database_path(settings: Settings) -> Path | None:
    """The SQLite file this instance writes, or ``None`` for anything else (PostgreSQL)."""
    if settings.database_url is None:
        return None
    url = make_url(settings.database_url)
    if url.get_backend_name() != "sqlite":
        return None
    database = url.database
    if not database or database == ":memory:":
        return None
    if database.startswith("file:"):
        path = unquote(database.removeprefix("file:").split("?", maxsplit=1)[0])
        return None if path in {"", ":memory:"} else Path(path).expanduser()
    return Path(database).expanduser()


def backup_directory(settings: Settings) -> Path:
    return settings.data_dir / BACKUP_DIRECTORY


def _ensure_backup_directory(settings: Settings) -> Path:
    directory = backup_directory(settings)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def _new_backup_filename(directory: Path, moment: datetime, origin: str) -> Path:
    prefix = _PREFIXES[origin]
    base = f"{prefix}-{moment.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}.db"
    candidate = directory / base
    counter = 1
    while candidate.exists() or candidate.with_name(candidate.name + ".secret_key").exists():
        candidate = directory / f"{base[:-3]}-{counter}.db"
        counter += 1
    return candidate


def _sidecar(path: Path) -> Path:
    return path.with_name(path.name + ".secret_key")


def _sqlite_sidecars(path: Path) -> tuple[Path, Path, Path]:
    return (
        path.with_name(path.name + "-wal"),
        path.with_name(path.name + "-shm"),
        path.with_name(path.name + "-journal"),
    )


def _remove_sqlite_sidecars(path: Path) -> None:
    for sidecar in _sqlite_sidecars(path):
        try:
            sidecar.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("backup_sidecar_cleanup_failed exception_type=%s", type(exc).__name__)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def verify_database(path: Path) -> None:
    """``PRAGMA integrity_check`` and ``foreign_key_check`` on a finished database file."""
    if not path.is_file():
        raise BackupError(ERROR_DATABASE_MISSING)
    wal, _shm, _journal = _sqlite_sidecars(path)
    if wal.is_file() and wal.stat().st_size > 0:
        raise BackupError("has_wal")
    try:
        verify_integrity(path)
    except SQLiteFileError as exc:
        raise BackupError(exc.code) from exc


def _copy_database(source: Path, destination: Path) -> None:
    """Copy ``source`` through the SQLite backup API into ``destination``."""
    try:
        copy_sqlite_database(source, destination)
    except SQLiteFileError as exc:
        raise BackupError(exc.code) from exc


def _copy_secret_key(settings: Settings, backup_path: Path) -> None:
    """Keep the key stored in DATA_DIR next to the copy, so the backup can encrypt/decrypt.

    A key supplied through ``ASSETBOARD_SECRET_KEY`` or a file setting is never copied: the
    operator already keeps it outside the data directory.
    """
    if "secret_key" in settings.model_fields_set:
        return
    key_file = settings.data_dir / "secret_key"
    if not key_file.is_file():
        return
    destination = _sidecar(backup_path)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        shutil.copyfile(key_file, temporary)
        os.chmod(temporary, 0o600)
        _fsync_file(temporary)
        os.replace(temporary, destination)
    except OSError as exc:
        logger.warning("backup_secret_key_copy_failed exception_type=%s", type(exc).__name__)
        temporary.unlink(missing_ok=True)


# Run history


def _start_run(
    factory: sessionmaker[Session], clock: Clock, origin: str
) -> tuple[UUID | None, datetime]:
    started = clock.now()
    try:
        run = BackupRun(
            id=uuid4(),
            started_at=started,
            status="running",
            origin=origin,
        )
        with factory.begin() as session:
            session.add(run)
        return run.id, started
    except Exception as exc:  # noqa: BLE001 - a backup must also work when history cannot be written
        logger.error("backup_run_start_failed exception_type=%s", type(exc).__name__)
        return None, started


def _finish_run(
    factory: sessionmaker[Session],
    run_id: UUID | None,
    clock: Clock,
    *,
    status: str,
    path: str | None = None,
    size_bytes: int | None = None,
    error: str | None = None,
) -> None:
    if run_id is None:
        return
    try:
        with factory.begin() as session:
            run = session.get(BackupRun, run_id)
            if run is None:
                return
            run.finished_at = clock.now()
            run.status = status
            run.path = path
            run.size_bytes = size_bytes
            run.error = error
    except Exception as exc:  # noqa: BLE001 - the copy itself may still have succeeded
        logger.error("backup_run_finish_failed exception_type=%s", type(exc).__name__)


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, BackupError):
        return exc.code
    return ERROR_STORAGE


def _notify_failure(
    factory: sessionmaker[Session],
    settings: Settings,
    notifier: Notifier | None,
    code: str,
) -> None:
    if notifier is None or not settings.notifications_enabled:
        return
    try:
        with factory() as session:
            channels = InstanceNotificationReader(session).admin_channels()
    except Exception as exc:  # noqa: BLE001 - a notification must never hide the backup error
        logger.warning("backup_notification_lookup_failed exception_type=%s", type(exc).__name__)
        return
    for channel in channels:
        url = decrypt_url(settings, channel.id, channel.url_encrypted)
        if url is None:
            continue
        locale = resolve_locale(
            user_locale=channel.user_locale,
            workspace_locale=channel.workspace_locale,
            default_locale=settings.default_locale,
        )
        title, body = backup_failure_message(locale=locale, code=code)
        try:
            result = notifier.send(url, title, body)
        except Exception as exc:  # noqa: BLE001 - the Notifier contract says it does not raise
            logger.warning(
                "backup_notification_failed channel=%s exception_type=%s",
                channel.id,
                type(exc).__name__,
            )
            continue
        if not result.ok:
            logger.warning(
                "backup_notification_failed channel=%s error=%s", channel.id, result.error
            )


def create_backup(
    factory: sessionmaker[Session],
    settings: Settings,
    clock: Clock,
    *,
    origin: str = "scheduled",
    notifier: Notifier | None = None,
) -> BackupResult:
    """Make one consistent copy and record it. Raises ``BackupError`` on failure."""
    if origin not in ORIGINS:
        raise ValueError(f"unknown backup origin: {origin}")
    source = sqlite_database_path(settings)
    if source is None:
        raise BackupUnavailable(ERROR_NOT_SQLITE)
    run_id, started = _start_run(factory, clock, origin)
    temporary: Path | None = None
    destination: Path | None = None
    try:
        directory = _ensure_backup_directory(settings)
        destination = _new_backup_filename(directory, started, origin)
        # ``mkstemp`` creates the file and returns an open descriptor; close it before SQLite
        # opens the same path itself.
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".backup-", suffix=".db.tmp", dir=directory
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        os.chmod(temporary, 0o600)
        _copy_database(source, temporary)
        verify_database(temporary)
        _fsync_file(temporary)
        os.replace(temporary, destination)
        _remove_sqlite_sidecars(temporary)
        _fsync_directory(directory)
        size_bytes = destination.stat().st_size
        _copy_secret_key(settings, destination)
        _finish_run(
            factory,
            run_id,
            clock,
            status="ok",
            path=destination.name,
            size_bytes=size_bytes,
        )
        return BackupResult(
            id=run_id,
            path=destination,
            size_bytes=size_bytes,
            started_at=started,
            finished_at=clock.now(),
        )
    except Exception as exc:
        code = _error_code(exc)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
            _remove_sqlite_sidecars(temporary)
        if destination is not None:
            destination.unlink(missing_ok=True)
            _sidecar(destination).unlink(missing_ok=True)
        _finish_run(factory, run_id, clock, status="failed", error=code)
        logger.error(
            "backup_failed origin=%s code=%s exception_type=%s",
            origin,
            code,
            type(exc).__name__,
            exc_info=True,
        )
        _notify_failure(factory, settings, notifier, code)
        if isinstance(exc, BackupError):
            raise
        raise BackupError(ERROR_STORAGE) from exc


def prune_scheduled_backups(
    factory: sessionmaker[Session],
    settings: Settings,
    now: datetime,
    policy: RetentionPolicy,
) -> tuple[str, ...]:
    """Delete scheduled copies outside the retention policy, with their run rows.

    A ``running`` row older than two hours was interrupted (a crash or a restart): it is closed
    as failed so the settings page never shows it as in progress forever.
    """
    stale_before = now - timedelta(hours=2)
    with factory.begin() as session:
        for run in session.scalars(
            select(BackupRun).where(
                BackupRun.status == "running", BackupRun.started_at < stale_before
            )
        ):
            run.status = "failed"
            run.error = "interrupted"
            run.finished_at = now
    with factory() as session:
        rows = list(
            session.scalars(
                select(BackupRun)
                .where(
                    BackupRun.origin == SCHEDULED,
                    BackupRun.status == "ok",
                    BackupRun.path.is_not(None),
                )
                .order_by(BackupRun.started_at.desc(), BackupRun.id)
            )
        )
    plan = plan_retention(
        (
            BackupRef(
                key=str(row.id),
                started_at=row.started_at,
                trigger=row.origin,
                path=row.path,
            )
            for row in rows
        ),
        policy,
    )
    if not plan.delete:
        return ()
    directory = backup_directory(settings)
    removed: list[str] = []
    for row in rows:
        if str(row.id) not in plan.delete:
            continue
        filename = row.path or ""
        if Path(filename).name != filename or not filename:
            logger.warning("backup_prune_refused id=%s reason=unsafe_path", row.id)
            continue
        path = directory / filename
        try:
            path.unlink(missing_ok=True)
            _sidecar(path).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "backup_prune_failed id=%s exception_type=%s", row.id, type(exc).__name__
            )
            continue
        try:
            with factory.begin() as session:
                stored = session.get(BackupRun, row.id)
                if stored is not None:
                    session.delete(stored)
        except Exception as exc:  # noqa: BLE001 - an orphan row is safer than a missing backup
            logger.error(
                "backup_prune_row_failed id=%s exception_type=%s", row.id, type(exc).__name__
            )
            continue
        removed.append(filename)
    if removed:
        logger.info("backup_pruned count=%d", len(removed))
    return tuple(removed)


def scheduled_backup_due(factory: sessionmaker[Session], clock: Clock) -> bool:
    with factory() as session:
        last = session.scalars(
            select(BackupRun.started_at)
            .where(BackupRun.origin == SCHEDULED, BackupRun.status == "ok")
            .order_by(BackupRun.started_at.desc())
            .limit(1)
        ).first()
    return backup_due(last, clock.now())


def run_scheduled_backup(
    factory: sessionmaker[Session],
    settings: Settings,
    clock: Clock,
    notifier: Notifier | None,
    *,
    force: bool = False,
) -> BackupResult | None:
    """The daily job and the startup catch-up: one backup, then retention.

    ``force`` is the daily cron tick; without it (startup) a backup is made only when the last
    successful scheduled one is a day old or older.
    """
    if not force and not scheduled_backup_due(factory, clock):
        return None
    try:
        result = create_backup(factory, settings, clock, origin="scheduled", notifier=notifier)
    except BackupError:
        return None
    stored = get_settings(factory)
    policy = RetentionPolicy(
        stored.backup_daily_retention,
        stored.backup_weekly_retention,
        stored.backup_monthly_retention,
    )
    prune_scheduled_backups(factory, settings, clock.now(), policy)
    return result


# Presentation


def _size_label(size_bytes: int | None) -> str | None:
    if size_bytes is None:
        return None
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes // (1024 * 1024)} MB"
    if size_bytes >= 1024:
        return f"{size_bytes // 1024} KB"
    return f"{size_bytes} B"


def _run_view(run: BackupRun, directory: Path) -> BackupRunView:
    path = directory / run.path if run.path else None
    return BackupRunView(
        id=run.id,
        started_at=run.started_at,
        finished_at=run.finished_at,
        status=run.status,
        origin=run.origin,
        size_bytes=run.size_bytes,
        size_label=_size_label(run.size_bytes),
        error=run.error,
        downloadable=bool(
            run.status == "ok"
            and path is not None
            and Path(run.path or "").name == run.path
            and path.is_file()
        ),
        filename=run.path,
    )


def build_panel(factory: sessionmaker[Session], settings: Settings, clock: Clock) -> BackupPanel:
    retention = get_settings(factory)
    policy = RetentionPolicy(
        retention.backup_daily_retention,
        retention.backup_weekly_retention,
        retention.backup_monthly_retention,
    )
    source = sqlite_database_path(settings)
    if source is None:
        backend = database_backend(settings)
        reason = "in_memory" if backend == "sqlite" else "postgresql"
        return BackupPanel(
            available=False,
            reason=reason,
            directory=None,
            runs=(),
            last_success=None,
            last_failure=None,
            next_run_at=None,
            retention=policy,
            scheduler_enabled=settings.scheduler_enabled,
        )
    directory = backup_directory(settings)
    with factory() as session:
        rows = list(
            session.scalars(
                select(BackupRun)
                .order_by(BackupRun.started_at.desc(), BackupRun.id.desc())
                .limit(RECENT_RUNS)
            )
        )
    views = tuple(_run_view(run, directory) for run in rows)
    last_success = next((view for view in views if view.status == "ok"), None)
    last_failure = next((view for view in views if view.status == "failed"), None)
    return BackupPanel(
        available=True,
        reason=None,
        directory=str(directory),
        runs=views,
        last_success=last_success,
        last_failure=last_failure,
        next_run_at=next_scheduled_at(clock.now()) if settings.scheduler_enabled else None,
        retention=policy,
        scheduler_enabled=settings.scheduler_enabled,
    )


def download_path(
    factory: sessionmaker[Session], settings: Settings, run_id: UUID
) -> tuple[Path, str] | None:
    """The verified file and download name for one backup, or ``None`` for a missing row."""
    with factory() as session:
        run = session.get(BackupRun, run_id)
        if run is None or run.status != "ok" or not run.path:
            return None
        filename = run.path
        if Path(filename).name != filename:
            return None
    directory = backup_directory(settings)
    path = directory / filename
    if not path.is_file():
        return None
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError:
        return None
    return path, filename


# Restore


def inspect_backup(path: Path, settings: Settings) -> RestoreInspection:
    """Check integrity and compare the backup's schema with the migrations in this code."""
    verify_database(path)
    revisions = read_sqlite_revisions(path)
    if not revisions:
        return RestoreInspection(revisions, "unknown")
    script = ScriptDirectory.from_config(alembic_config(settings))
    heads = set(script.get_heads())
    known = {revision.revision for revision in script.walk_revisions()}
    current = set(revisions)
    if current - known:
        return RestoreInspection(revisions, "newer")
    if current == heads:
        return RestoreInspection(revisions, "current")
    return RestoreInspection(revisions, "older")


def _write_secret_key(settings: Settings, key: bytes) -> None:
    settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = settings.data_dir / "secret_key"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".secret_key.", suffix=".tmp", dir=settings.data_dir
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_file_atomically(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        os.chmod(temporary, 0o600)
        _fsync_file(temporary)
        os.replace(temporary, destination)
        _remove_sqlite_sidecars(destination)
        _fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _make_pre_restore_backup(
    settings: Settings, source: Path, clock: Clock
) -> tuple[Path, datetime, datetime] | None:
    if not source.is_file():
        return None
    directory = _ensure_backup_directory(settings)
    started = clock.now()
    destination = _new_backup_filename(directory, started, "pre_restore")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".pre-restore-", suffix=".db.tmp", dir=directory
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    os.chmod(temporary, 0o600)
    try:
        _copy_database(source, temporary)
        verify_database(temporary)
        _fsync_file(temporary)
        os.replace(temporary, destination)
        _remove_sqlite_sidecars(temporary)
        _fsync_directory(directory)
        _copy_secret_key(settings, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        _remove_sqlite_sidecars(temporary)
        destination.unlink(missing_ok=True)
        _sidecar(destination).unlink(missing_ok=True)
        raise
    return destination, started, clock.now()


def _record_pre_restore(
    settings: Settings,
    path: Path,
    started: datetime,
    finished: datetime,
) -> None:
    if settings.database_url is None:
        return
    engine = create_database_engine(settings.database_url)
    factory = sessionmaker(engine, class_=Session, expire_on_commit=False)
    try:
        size = path.stat().st_size
        with factory.begin() as session:
            session.add(
                BackupRun(
                    id=uuid4(),
                    started_at=started,
                    finished_at=finished,
                    status="ok",
                    origin="pre_restore",
                    path=path.name,
                    size_bytes=size,
                )
            )
    except Exception as exc:  # noqa: BLE001 - the restore itself already succeeded
        logger.warning("restore_history_failed exception_type=%s", type(exc).__name__)
    finally:
        engine.dispose()


def _rollback(settings: Settings, target: Path, pre_restore: Path | None) -> None:
    try:
        if pre_restore is not None:
            _copy_file_atomically(pre_restore, target)
        else:
            target.unlink(missing_ok=True)
            _remove_sqlite_sidecars(target)
    except Exception as exc:  # noqa: BLE001 - reported with the migration failure below
        logger.error("restore_rollback_failed exception_type=%s", type(exc).__name__)


def restore_backup(
    settings: Settings,
    backup_file: str | Path,
    clock: Clock,
) -> RestoreResult:
    """Replace the configured SQLite database with a checked backup.

    Refuses PostgreSQL, in-memory databases, files with a non-empty ``-wal`` sidecar and
    backups whose schema is unknown or newer than this code. A pre-restore copy is always
    made when a database already exists; a failed migration puts it back.
    """
    target = sqlite_database_path(settings)
    if target is None:
        raise RestoreError(
            "not_sqlite",
            "restore is available for SQLite databases only: use pg_restore for PostgreSQL",
        )
    source = Path(backup_file).expanduser()
    if not source.is_file():
        raise RestoreError("missing", f"backup file not found: {source}")
    if source.resolve() == target.resolve():
        raise RestoreError("same_database", "the backup file is the live database")
    live_wal = target.with_name(target.name + "-wal")
    if live_wal.exists():
        raise RestoreError(
            "database_in_use",
            "the live database has a write-ahead log: stop the application before restoring",
        )
    try:
        inspection = inspect_backup(source, settings)
    except BackupError as exc:
        messages = {
            "has_wal": (
                "the backup has a write-ahead log beside it: stop the application and copy the "
                "database with the SQLite backup API instead"
            ),
            ERROR_INTEGRITY_FAILED: "the backup failed SQLite's integrity check",
            ERROR_DATABASE_MISSING: "the backup file could not be read",
            ERROR_COPY_FAILED: "the backup file could not be read",
        }
        raise RestoreError(
            exc.code, messages.get(exc.code, "the backup file could not be read")
        ) from exc
    if inspection.state == "newer":
        raise RestoreError(
            "schema_newer",
            "the backup was written by a newer version of this application; upgrade first",
        )
    if inspection.state == "unknown":
        raise RestoreError(
            "schema_unknown",
            "the file is not a complete database from this application",
        )
    if target.is_file() and target.stat().st_size:
        try:
            pre_restore = _make_pre_restore_backup(settings, target, clock)
        except Exception as exc:
            raise RestoreError(
                "pre_restore_failed",
                "could not create the pre-restore backup; the database was not changed",
            ) from exc
    else:
        pre_restore = None
    try:
        _copy_file_atomically(source, target)
    except OSError as exc:
        raise RestoreError("copy_failed", "could not replace the database file") from exc
    migrated = False
    if inspection.state == "older":
        try:
            upgrade_database(settings)
        except Exception as exc:
            _rollback(settings, target, pre_restore[0] if pre_restore else None)
            raise RestoreError(
                "migration_failed",
                "schema migrations failed; the pre-restore backup was put back",
            ) from exc
        migrated = True
    warnings: list[str] = []
    key_sidecar = _sidecar(source)
    if key_sidecar.is_file():
        if "secret_key" in settings.model_fields_set:
            warnings.append("secret_key_env")
        else:
            try:
                _write_secret_key(settings, key_sidecar.read_bytes())
            except OSError as exc:
                _rollback(settings, target, pre_restore[0] if pre_restore else None)
                raise RestoreError(
                    "key_failed",
                    "the secret key could not be restored; the pre-restore backup was put back",
                ) from exc
            warnings.append("secret_key_restored")
    else:
        warnings.append("secret_key_missing")
    if pre_restore is not None:
        _record_pre_restore(settings, pre_restore[0], pre_restore[1], pre_restore[2])
    return RestoreResult(
        target=target,
        pre_restore=pre_restore[0] if pre_restore else None,
        migrated=migrated,
        warnings=tuple(warnings),
    )
