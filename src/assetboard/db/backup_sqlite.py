"""Low-level SQLite file checks for backups (F9.4).

Raw SQL stays inside ``assetboard.db`` (the tenant-isolation static scan enforces it). The
service layer decides what a problem means and how to report it; this module only speaks
SQLite and returns stable short codes.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

ERROR_DATABASE_MISSING = "database_missing"
ERROR_COPY_FAILED = "copy_failed"
ERROR_INTEGRITY_FAILED = "integrity_failed"
_REQUIRED_TABLES = frozenset({"alembic_version", "users", "workspaces"})


class SQLiteFileError(Exception):
    """A SQLite operation failed. ``code`` is short and safe for the UI and notifications."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _connect_read_only(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def copy_database(source: Path, destination: Path) -> None:
    """Copy ``source`` through SQLite's backup API into ``destination``.

    The destination is left in rollback-journal mode so the file stands alone (no ``-wal``
    sidecar has to travel with it).
    """
    if not source.is_file():
        raise SQLiteFileError(ERROR_DATABASE_MISSING)
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(str(source), timeout=30)
        source_connection.execute("PRAGMA busy_timeout=30000")
        destination_connection = sqlite3.connect(str(destination), timeout=30)
        source_connection.backup(destination_connection)
        destination_connection.execute("PRAGMA journal_mode=DELETE")
        destination_connection.commit()
    except sqlite3.DatabaseError as exc:
        raise SQLiteFileError(ERROR_COPY_FAILED) from exc
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()


def verify_integrity(path: Path) -> None:
    """Refuse a file that SQLite cannot read or that has broken rows or references."""
    if not path.is_file():
        raise SQLiteFileError(ERROR_DATABASE_MISSING)
    try:
        connection = _connect_read_only(path)
    except sqlite3.DatabaseError as exc:
        raise SQLiteFileError(ERROR_INTEGRITY_FAILED) from exc
    try:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise SQLiteFileError(ERROR_INTEGRITY_FAILED)
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise SQLiteFileError(ERROR_INTEGRITY_FAILED)
    except sqlite3.DatabaseError as exc:
        raise SQLiteFileError(ERROR_INTEGRITY_FAILED) from exc
    finally:
        connection.close()


def read_revisions(path: Path) -> tuple[str, ...]:
    """The Alembic revisions in a complete database, or ``()`` when it is not one."""
    try:
        connection = _connect_read_only(path)
    except sqlite3.DatabaseError:
        return ()
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if not tables >= _REQUIRED_TABLES:
            return ()
        return tuple(
            sorted(
                str(row[0])
                for row in connection.execute("SELECT version_num FROM alembic_version").fetchall()
            )
        )
    except sqlite3.DatabaseError:
        return ()
    finally:
        connection.close()
