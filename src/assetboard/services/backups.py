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
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import unquote
from uuid import UUID, uuid4

try:  # pragma: no cover - the deployment targets Linux, the fallback keeps tests portable
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

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
ERROR_NOT_ASSETBOARD = "not_assetboard"
ERROR_SECRET_KEY = "secret_key_copy_failed"
ERROR_STORAGE = "storage_error"
ORIGINS = ("scheduled", "manual", "pre_restore")
# A second scheduled attempt within this window is a duplicate (startup catch-up plus the
# daily cron, or two workers starting together), not a retry after a failure.
SCHEDULED_DEDUPLICATION = timedelta(hours=1)
# A crashed process leaves a full database copy behind as ``.backup-*.db.tmp``.
STALE_TEMPORARY_AGE = timedelta(days=1)
_scheduled_lock = threading.Lock()
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


@dataclass(frozen=True, slots=True)
class _PreRestore:
    path: Path
    started: datetime
    finished: datetime
    unverified: bool


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


@contextmanager
def _backup_lock(directory: Path) -> Generator[None]:
    """Serialize backup writers across threads and processes.

    Two ``Back up now`` clicks in the same second used to pick the same file name; the loser
    then deleted the other run's copy. The lock lives beside the backups and covers naming,
    copying, the secret-key sidecar and cleanup. Deployment targets are Unix; without
    ``fcntl`` the lock degrades to no locking rather than refusing to back up.
    """
    descriptor = os.open(directory / ".backup.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _new_backup_filename(directory: Path, moment: datetime, origin: str) -> Path:
    prefix = _PREFIXES[origin]
    base = f"{prefix}-{moment.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}.db"
    candidate = directory / base
    counter = 1
    while candidate.exists() or candidate.with_name(candidate.name + ".secret_key").exists():
        candidate = directory / f"{base[:-3]}-{counter}.db"
        counter += 1
    return candidate


def _safe_unlink(path: Path) -> None:
    """Remove a file without letting a cleanup error hide the original failure."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("backup_cleanup_failed exception_type=%s", type(exc).__name__)


def _cleanup_stale_temporary_files(directory: Path, now: datetime) -> None:
    cutoff = now.timestamp() - STALE_TEMPORARY_AGE.total_seconds()
    for pattern in (".backup-*.db.tmp*", ".pre-restore-*.db.tmp*", ".secret_key.*.tmp"):
        for path in directory.glob(pattern):
            try:
                if path.stat().st_mtime < cutoff:
                    _safe_unlink(path)
                    _remove_sqlite_sidecars(path)
            except OSError:
                continue


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

    The decision is made from the stored file, not from ``settings.model_fields_set``: startup
    calls ``ensure_secret_key()``, which assigns the generated key to ``secret_key`` and would
    otherwise make every running instance look as if the key came from the environment.

    A key that differs from the stored file was supplied through ``ASSETBOARD_SECRET_KEY`` or
    a ``*_FILE`` setting; it is never copied because the operator already keeps it outside the
    data directory. A copy failure fails the whole backup: a database whose encrypted values
    cannot be read is not a usable disaster-recovery copy.
    """
    key_file = settings.data_dir / "secret_key"
    try:
        stored = key_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise BackupError(ERROR_SECRET_KEY) from exc
    if not stored:
        raise BackupError(ERROR_SECRET_KEY)
    if settings.secret_key is not None and settings.secret_key.strip() != stored:
        return
    destination = _sidecar(backup_path)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        shutil.copyfile(key_file, temporary)
        os.chmod(temporary, 0o600)
        _fsync_file(temporary)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except OSError as exc:
        _safe_unlink(temporary)
        raise BackupError(ERROR_SECRET_KEY) from exc


def _validate_source(source: Path) -> None:
    """Refuse a source that cannot be a complete AssetBoard database before copying it.

    ``sqlite3.connect`` creates a missing file, so without this check a lost database produced
    a small, valid-looking, table-less "backup" that retention later treated as the newest
    good copy.
    """
    try:
        if not source.is_file() or source.stat().st_size == 0:
            raise BackupError(ERROR_DATABASE_MISSING)
    except OSError as exc:
        raise BackupError(ERROR_DATABASE_MISSING) from exc
    if not read_sqlite_revisions(source):
        raise BackupError(ERROR_NOT_ASSETBOARD)


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
    try:
        _validate_source(source)
    except BackupError as exc:
        # Do not start a run row first: writing to a missing database would create an empty
        # file there, which is exactly the failure this check exists to prevent.
        logger.error(
            "backup_failed origin=%s code=%s exception_type=%s",
            origin,
            exc.code,
            type(exc).__name__,
        )
        _notify_failure(factory, settings, notifier, exc.code)
        raise
    run_id, started = _start_run(factory, clock, origin)
    temporary: Path | None = None
    destination: Path | None = None
    try:
        directory = _ensure_backup_directory(settings)
        _cleanup_stale_temporary_files(directory, started)
        with _backup_lock(directory):
            destination = _new_backup_filename(directory, started, origin)
            # ``mkstemp`` creates the file and returns an open descriptor; close it before
            # SQLite opens the same path itself.
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
            _safe_unlink(temporary)
            _remove_sqlite_sidecars(temporary)
        if destination is not None:
            _safe_unlink(destination)
            _safe_unlink(_sidecar(destination))
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
    as failed so the settings page never shows it as in progress forever. An all-zero policy is
    refused here even if it reached the database by hand: deleting the copy just made would
    leave the instance without a safety net, so the defaults apply instead.
    """
    if policy.daily == 0 and policy.weekly == 0 and policy.monthly == 0:
        logger.warning("backup_retention_all_zero: using the default policy")
        policy = RetentionPolicy()
    _cleanup_stale_temporary_files(backup_directory(settings), now)
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


def scheduled_backup_due(
    factory: sessionmaker[Session],
    clock: Clock,
    *,
    interval: timedelta = timedelta(hours=24),
    settings: Settings | None = None,
) -> bool:
    with factory() as session:
        last = session.execute(
            select(BackupRun.started_at, BackupRun.path)
            .where(BackupRun.origin == SCHEDULED, BackupRun.status == "ok")
            .order_by(BackupRun.started_at.desc())
            .limit(1)
        ).first()
    if last is None:
        return True
    started, filename = last
    if settings is not None and (
        not filename or not (backup_directory(settings) / filename).is_file()
    ):
        # A row whose file an operator deleted must not suppress the next copy.
        return True
    return backup_due(started, clock.now(), interval=interval)


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
    successful scheduled one is a day old or older. Both paths also skip a copy taken within
    the last hour: a restart just before 03:30 used to run the catch-up and the cron back to
    back, and two app workers could start together. A failed run does not suppress the retry.
    """
    if not _scheduled_lock.acquire(blocking=False):
        return None
    try:
        interval = SCHEDULED_DEDUPLICATION if force else timedelta(hours=24)
        if not scheduled_backup_due(factory, clock, interval=interval, settings=settings):
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
    finally:
        _scheduled_lock.release()


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
        # Separate queries: ten failures must not hide the last success, and ten successes must
        # not hide the last failure.
        last_ok = session.scalars(
            select(BackupRun)
            .where(BackupRun.status == "ok")
            .order_by(BackupRun.started_at.desc(), BackupRun.id.desc())
            .limit(1)
        ).first()
        last_failed = session.scalars(
            select(BackupRun)
            .where(BackupRun.status == "failed")
            .order_by(BackupRun.started_at.desc(), BackupRun.id.desc())
            .limit(1)
        ).first()
    views = tuple(_run_view(run, directory) for run in rows)
    last_success = _run_view(last_ok, directory) if last_ok is not None else None
    last_failure = _run_view(last_failed, directory) if last_failed is not None else None
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
        _fsync_directory(settings.data_dir)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_file_atomically(source: Path, destination: Path) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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


def _make_pre_restore_backup(settings: Settings, source: Path, clock: Clock) -> _PreRestore | None:
    """Copy the current database before a restore, even when it is already damaged.

    A corrupt live database is the most common reason to restore. The online copy and its
    integrity check are used when they work; otherwise a raw file copy is kept so the operator
    still has the exact previous state (and can roll back to it), and the caller reports the
    copy as unverified. Without the raw fallback a damaged database blocked its own recovery.
    """
    if not source.is_file():
        return None
    directory = _ensure_backup_directory(settings)
    started = clock.now()
    _cleanup_stale_temporary_files(directory, started)
    with _backup_lock(directory):
        destination = _new_backup_filename(directory, started, "pre_restore")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".pre-restore-", suffix=".db.tmp", dir=directory
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        os.chmod(temporary, 0o600)
        unverified = False
        try:
            try:
                _copy_database(source, temporary)
                verify_database(temporary)
            except BackupError:
                logger.warning("pre_restore_copy_unverified")
                unverified = True
                _safe_unlink(temporary)
                _remove_sqlite_sidecars(temporary)
                shutil.copyfile(source, temporary)
                os.chmod(temporary, 0o600)
            _fsync_file(temporary)
            os.replace(temporary, destination)
            _remove_sqlite_sidecars(temporary)
            _fsync_directory(directory)
            _copy_secret_key(settings, destination)
        except Exception:
            _safe_unlink(temporary)
            _remove_sqlite_sidecars(temporary)
            _safe_unlink(destination)
            _safe_unlink(_sidecar(destination))
            raise
    return _PreRestore(destination, started, clock.now(), unverified)


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


def _rollback(settings: Settings, target: Path, pre_restore: Path | None) -> bool:
    """Put the previous database back; ``False`` when it could not be done."""
    try:
        if pre_restore is not None:
            _copy_file_atomically(pre_restore, target)
        else:
            target.unlink(missing_ok=True)
            _remove_sqlite_sidecars(target)
        return True
    except Exception as exc:  # noqa: BLE001 - reported with the migration failure below
        logger.error("restore_rollback_failed exception_type=%s", type(exc).__name__)
        return False


def _rollback_message(pre_restore: _PreRestore | None) -> str:
    if pre_restore is None:
        return "schema migrations failed; the copied database was removed"
    return (
        f"schema migrations failed and the original database could not be put back; "
        f"the pre-restore copy is at {pre_restore.path}"
    )


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
    pre_restore: _PreRestore | None = None
    if target.is_file() and target.stat().st_size:
        try:
            pre_restore = _make_pre_restore_backup(settings, target, clock)
        except Exception as exc:
            raise RestoreError(
                "pre_restore_failed",
                "could not create the pre-restore backup; the database was not changed",
            ) from exc
    try:
        _copy_file_atomically(source, target)
    except OSError as exc:
        raise RestoreError("copy_failed", "could not replace the database file") from exc
    migrated = False
    if inspection.state == "older":
        try:
            upgrade_database(settings)
        except Exception as exc:
            rolled_back = _rollback(settings, target, pre_restore.path if pre_restore else None)
            if rolled_back and pre_restore is not None:
                _record_pre_restore(
                    settings, pre_restore.path, pre_restore.started, pre_restore.finished
                )
            raise RestoreError(
                "migration_failed",
                (
                    "schema migrations failed; the pre-restore backup was put back"
                    if rolled_back
                    else _rollback_message(pre_restore)
                ),
            ) from exc
        migrated = True
    warnings: list[str] = []
    if pre_restore is not None and pre_restore.unverified:
        warnings.append("pre_restore_unverified")
    key_sidecar = _sidecar(source)
    if key_sidecar.is_file():
        if "secret_key" in settings.model_fields_set:
            warnings.append("secret_key_env")
        else:
            try:
                _write_secret_key(settings, key_sidecar.read_bytes())
            except OSError as exc:
                rolled_back = _rollback(settings, target, pre_restore.path if pre_restore else None)
                if rolled_back and pre_restore is not None:
                    _record_pre_restore(
                        settings, pre_restore.path, pre_restore.started, pre_restore.finished
                    )
                if rolled_back:
                    message = (
                        "the secret key could not be restored; the pre-restore backup was put back"
                    )
                else:
                    message = _rollback_message(pre_restore)
                raise RestoreError("key_failed", message) from exc
            warnings.append("secret_key_restored")
    else:
        warnings.append("secret_key_missing")
    if pre_restore is not None:
        _record_pre_restore(settings, pre_restore.path, pre_restore.started, pre_restore.finished)
    return RestoreResult(
        target=target,
        pre_restore=pre_restore.path if pre_restore else None,
        migrated=migrated,
        warnings=tuple(warnings),
    )
