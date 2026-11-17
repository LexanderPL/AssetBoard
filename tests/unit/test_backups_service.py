"""Database backup, retention, notification and restore services (F9.4)."""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as day_time
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.db.base import WorkspaceScopedMixin
from assetboard.db.engine import create_database_engine
from assetboard.db.migrate import alembic_config, schema_state, upgrade_database
from assetboard.db.models.backups import BackupRun
from assetboard.db.models.identity import ExternalIdentity, User, Workspace
from assetboard.db.models.notifications import NotificationChannel
from assetboard.domain.backups import RetentionPolicy
from assetboard.services.backups import (
    ERROR_COPY_FAILED,
    BackupError,
    BackupUnavailable,
    RestoreError,
    build_panel,
    create_backup,
    download_path,
    inspect_backup,
    prune_scheduled_backups,
    restore_backup,
    scheduled_backup_due,
    sqlite_database_path,
    verify_database,
)
from assetboard.services.notifications import encrypt_url
from tests.fake_notifier import FakeNotifier


@dataclass
class Env:
    settings: Settings
    engine: Engine
    factory: sessionmaker[Session]
    clock: FixedClock
    database: Path


@pytest.fixture
def env(tmp_path: Path, migrated_database_template: Path) -> Iterator[Env]:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database = data_dir / "assetboard.db"
    shutil.copyfile(migrated_database_template, database)
    settings = Settings(
        _env_file=None,
        data_dir=data_dir,
        database_url=f"sqlite:///{database}",
    )
    engine = create_database_engine(settings.database_url or "")
    clock = FixedClock("2026-03-01T12:00:00+00:00")
    factory = sessionmaker(engine, class_=Session, info={"clock": clock}, expire_on_commit=False)
    try:
        yield Env(settings, engine, factory, clock, database)
    finally:
        engine.dispose()


def _user(session: Session, email: str) -> User:
    user = User(email=email, display_name="Test", is_active=True)
    session.add(user)
    session.flush()
    return user


def _workspace(session: Session) -> Workspace:
    workspace = Workspace(name="Home", base_currency="EUR", locale="en", timezone="UTC")
    session.add(workspace)
    session.flush()
    return workspace


def _channel(session: Session, settings: Settings, user: User, workspace: Workspace, now: datetime):
    channel_id = uuid4()
    channel = NotificationChannel(
        id=channel_id,
        workspace_id=workspace.id,
        user_id=user.id,
        name="Admin phone",
        url_encrypted=encrypt_url(settings, channel_id, "ntfy://example/topic", now),
        scheme="ntfy",
        service_name="ntfy",
        enabled=True,
        mode="digest",
        digest_time=day_time(9, 0),
        default_lead_days=3,
    )
    session.add(channel)
    session.flush()
    return channel


def _sqlite_header(path: Path) -> bytes:
    with path.open("rb") as stream:
        return stream.read(16)


def test_backup_runs_are_instance_level_not_workspace_data() -> None:
    assert not hasattr(BackupRun, "workspace_id")
    assert not issubclass(BackupRun, WorkspaceScopedMixin)


def test_sqlite_path_and_postgres_refusal(env: Env, tmp_path: Path) -> None:
    assert sqlite_database_path(env.settings) == env.database
    postgres = Settings.model_construct(data_dir=tmp_path, database_url="postgresql://user@host/db")
    assert sqlite_database_path(postgres) is None
    with pytest.raises(BackupUnavailable):
        create_backup(env.factory, postgres, env.clock)
    with env.factory() as session:
        assert session.scalars(select(BackupRun)).all() == []


def test_backup_is_a_verified_standalone_sqlite_file_and_records_the_run(env: Env) -> None:
    with env.factory.begin() as session:
        _user(session, "backup@example.com")
    result = create_backup(env.factory, env.settings, env.clock, origin="manual")
    assert result.path.is_file()
    assert _sqlite_header(result.path) == b"SQLite format 3\x00"
    assert os.stat(result.path).st_mode & 0o777 == 0o600
    assert not result.path.with_name(result.path.name + "-wal").exists()
    verify_database(result.path)
    with env.factory() as session:
        run = session.get(BackupRun, result.id)
        assert run is not None
        assert (run.status, run.origin, run.path, run.size_bytes) == (
            "ok",
            "manual",
            result.path.name,
            result.path.stat().st_size,
        )
        assert run.finished_at is not None
        assert run.error is None
    panel = build_panel(env.factory, env.settings, env.clock)
    assert panel.available is True
    assert panel.last_success is not None and panel.last_success.downloadable is True


def test_backup_copies_the_stored_secret_key_beside_it(env: Env) -> None:
    key_file = env.settings.data_dir / "secret_key"
    key_file.write_text("stored-key", encoding="utf-8")
    result = create_backup(env.factory, env.settings, env.clock, origin="manual")
    sidecar = result.path.with_name(result.path.name + ".secret_key")
    assert sidecar.read_text(encoding="utf-8") == "stored-key"
    assert os.stat(sidecar).st_mode & 0o777 == 0o600


def test_a_configured_secret_key_is_not_written_to_the_backup_directory(
    tmp_path: Path, migrated_database_template: Path
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database = data_dir / "assetboard.db"
    shutil.copyfile(migrated_database_template, database)
    settings = Settings(
        _env_file=None,
        data_dir=data_dir,
        database_url=f"sqlite:///{database}",
        secret_key="configured-key",
    )
    engine = create_database_engine(settings.database_url or "")
    clock = FixedClock("2026-03-01T12:00:00+00:00")
    factory = sessionmaker(engine, class_=Session, info={"clock": clock}, expire_on_commit=False)
    try:
        result = create_backup(factory, settings, clock, origin="manual")
        assert not result.path.with_name(result.path.name + ".secret_key").exists()
    finally:
        engine.dispose()


def test_backup_is_consistent_under_concurrent_writes(env: Env) -> None:
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        engine = create_database_engine(env.settings.database_url or "")
        clock = env.clock
        factory = sessionmaker(
            engine, class_=Session, info={"clock": clock}, expire_on_commit=False
        )
        try:
            for index in range(60):
                if stop.is_set():
                    break
                with factory.begin() as session:
                    user = _user(session, f"concurrent-{index}@example.com")
                    session.add(
                        ExternalIdentity(
                            user_id=user.id,
                            provider="https://idp.example",
                            subject=f"subject-{index}",
                        )
                    )
                time.sleep(0.002)
        except BaseException as exc:  # noqa: BLE001 - the assertion below reports it
            errors.append(exc)
        finally:
            engine.dispose()

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        for _ in range(6):
            result = create_backup(env.factory, env.settings, env.clock, origin="manual")
            verify_database(result.path)
            with sqlite3.connect(result.path) as connection:
                users, identities = connection.execute(
                    "SELECT (SELECT count(*) FROM users), "
                    "(SELECT count(*) FROM external_identities)"
                ).fetchone()
            assert users == identities
            assert users >= 0
            time.sleep(0.01)
    finally:
        stop.set()
        thread.join(timeout=30)
    assert not thread.is_alive()
    assert errors == []


def test_scheduled_due_uses_the_fixed_clock(env: Env) -> None:
    assert scheduled_backup_due(env.factory, env.clock) is True
    now = env.clock.now()
    with env.factory.begin() as session:
        session.add(
            BackupRun(
                id=uuid4(),
                started_at=now - timedelta(hours=23),
                finished_at=now - timedelta(hours=23),
                status="ok",
                origin="scheduled",
                path="recent.db",
                size_bytes=1,
            )
        )
    assert scheduled_backup_due(env.factory, env.clock) is False
    with env.factory.begin() as session:
        session.scalars(
            select(BackupRun).where(BackupRun.origin == "scheduled")
        ).one().started_at = now - timedelta(hours=25)
    assert scheduled_backup_due(env.factory, env.clock) is True


def test_retention_prunes_only_scheduled_files_and_records(env: Env) -> None:
    directory = env.settings.data_dir / "backups"
    directory.mkdir()
    now = env.clock.now()
    with env.factory.begin() as session:
        for index in range(6):
            name = f"daily-{index}.db"
            (directory / name).write_bytes(b"database")
            origin = "scheduled" if index < 4 else "manual"
            session.add(
                BackupRun(
                    id=uuid4(),
                    started_at=now - timedelta(days=index),
                    finished_at=now - timedelta(days=index),
                    status="ok",
                    origin=origin,
                    path=name,
                    size_bytes=8,
                )
            )
        pre = "pre-restore.db"
        (directory / pre).write_bytes(b"database")
        session.add(
            BackupRun(
                id=uuid4(),
                started_at=now - timedelta(days=100),
                finished_at=now - timedelta(days=100),
                status="ok",
                origin="pre_restore",
                path=pre,
                size_bytes=8,
            )
        )
    removed = prune_scheduled_backups(
        env.factory, env.settings, now, RetentionPolicy(daily=2, weekly=1, monthly=1)
    )
    assert set(removed) == {"daily-2.db", "daily-3.db"}
    assert (directory / "daily-0.db").is_file()
    assert (directory / "daily-1.db").is_file()
    assert (directory / pre).is_file()
    assert (directory / "daily-4.db").is_file()
    assert (directory / "daily-5.db").is_file()
    with env.factory() as session:
        paths = {run.path for run in session.scalars(select(BackupRun))}
    assert paths == {"daily-0.db", "daily-1.db", "daily-4.db", "daily-5.db", pre}


def test_retention_closes_interrupted_running_rows(env: Env) -> None:
    now = env.clock.now()
    with env.factory.begin() as session:
        session.add(
            BackupRun(
                id=uuid4(),
                started_at=now - timedelta(hours=5),
                status="running",
                origin="scheduled",
            )
        )
    prune_scheduled_backups(env.factory, env.settings, now, RetentionPolicy())
    with env.factory() as session:
        run = session.scalars(select(BackupRun)).one()
        assert (run.status, run.error) == ("failed", "interrupted")
        assert run.finished_at == now


def test_failure_notifies_administrators_channels_and_records_the_run(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    with env.factory.begin() as session:
        admin = _user(session, "admin@example.com")
        admin.is_instance_admin = True
        workspace = _workspace(session)
        _channel(session, env.settings, admin, workspace, env.clock.now())
    notifier = FakeNotifier()

    def fail(_source: Path, _destination: Path) -> None:
        raise BackupError(ERROR_COPY_FAILED)

    monkeypatch.setattr("assetboard.services.backups._copy_database", fail)
    with pytest.raises(BackupError):
        create_backup(env.factory, env.settings, env.clock, notifier=notifier)
    assert len(notifier.sent) == 1
    message = notifier.sent[0]
    assert "database backup failed" in message.title
    assert ERROR_COPY_FAILED in message.body
    with env.factory() as session:
        run = session.scalars(select(BackupRun)).one()
        assert (run.status, run.error, run.path) == ("failed", ERROR_COPY_FAILED, None)


def test_failure_without_channels_only_logs(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    notifier = FakeNotifier()

    def fail(_source: Path, _destination: Path) -> None:
        raise BackupError(ERROR_COPY_FAILED)

    monkeypatch.setattr("assetboard.services.backups._copy_database", fail)
    with pytest.raises(BackupError):
        create_backup(env.factory, env.settings, env.clock, notifier=notifier)
    assert notifier.sent == []


def test_download_path_refuses_rows_outside_the_backup_directory(env: Env) -> None:
    with env.factory.begin() as session:
        run = BackupRun(
            id=uuid4(),
            started_at=env.clock.now(),
            finished_at=env.clock.now(),
            status="ok",
            origin="manual",
            path="../outside.db",
            size_bytes=1,
        )
        session.add(run)
    assert download_path(env.factory, env.settings, run.id) is None


def test_restore_round_trip_saves_a_pre_restore_copy(env: Env) -> None:
    with env.factory.begin() as session:
        user = _user(session, "restored@example.com")
        original_id = user.id
    backup = create_backup(env.factory, env.settings, env.clock, origin="manual")
    with env.factory.begin() as session:
        session.delete(session.get(User, original_id))
        _user(session, "changed@example.com")
    env.engine.dispose()
    result = restore_backup(env.settings, backup.path, env.clock)
    assert result.migrated is False
    assert result.pre_restore is not None and result.pre_restore.is_file()
    with env.factory() as session:
        emails = set(session.scalars(select(User.email)))
        assert emails == {"restored@example.com"}
        pre_run = session.scalars(select(BackupRun).where(BackupRun.origin == "pre_restore")).one()
        assert pre_run.path == result.pre_restore.name
    with sqlite3.connect(result.pre_restore) as connection:
        emails = {row[0] for row in connection.execute("SELECT email FROM users")}
    assert emails == {"changed@example.com"}


def test_restore_restores_the_secret_key_sidecar(env: Env) -> None:
    key_file = env.settings.data_dir / "secret_key"
    key_file.write_text("backup-key", encoding="utf-8")
    backup = create_backup(env.factory, env.settings, env.clock, origin="manual")
    env.engine.dispose()
    key_file.write_text("replacement-key", encoding="utf-8")
    result = restore_backup(env.settings, backup.path, env.clock)
    assert "secret_key_restored" in result.warnings
    assert key_file.read_text(encoding="utf-8") == "backup-key"


def test_restore_refuses_postgres_corrupt_and_newer_backups(env: Env, tmp_path: Path) -> None:
    postgres = Settings.model_construct(data_dir=tmp_path, database_url="postgresql://user@host/db")
    with pytest.raises(RestoreError) as postgres_error:
        restore_backup(postgres, env.database, env.clock)
    assert postgres_error.value.code == "not_sqlite"

    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a database")
    with pytest.raises(RestoreError) as corrupt_error:
        restore_backup(env.settings, corrupt, env.clock)
    assert corrupt_error.value.code == "integrity_failed"

    future = tmp_path / "future.db"
    with sqlite3.connect(future) as connection:
        connection.executescript(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL);"
            "CREATE TABLE users (id TEXT);"
            "CREATE TABLE workspaces (id TEXT);"
            "INSERT INTO alembic_version VALUES ('9999_future_revision');"
        )
    with pytest.raises(RestoreError) as future_error:
        restore_backup(env.settings, future, env.clock)
    assert future_error.value.code == "schema_newer"


def test_restore_refuses_while_the_live_database_has_a_write_ahead_log(env: Env) -> None:
    backup = create_backup(env.factory, env.settings, env.clock, origin="manual")
    live_wal = env.database.with_name(env.database.name + "-wal")
    live_wal.write_bytes(b"pending")
    try:
        with pytest.raises(RestoreError) as error:
            restore_backup(env.settings, backup.path, env.clock)
        assert error.value.code == "database_in_use"
    finally:
        live_wal.unlink(missing_ok=True)


def test_restore_migrates_an_older_schema(env: Env) -> None:
    env.engine.dispose()
    previous = "0068_f38_work_logs_import"
    command.downgrade(alembic_config(env.settings), previous)
    with env.factory.begin() as session:
        _user(session, "older@example.com")
    backup = create_backup(env.factory, env.settings, env.clock, origin="manual")
    assert inspect_backup(backup.path, env.settings).state == "older"
    env.engine.dispose()
    upgrade_database(env.settings)
    assert schema_state(env.settings) == "current"
    result = restore_backup(env.settings, backup.path, env.clock)
    assert result.migrated is True
    assert schema_state(env.settings) == "current"
    with env.factory() as session:
        assert session.scalars(select(User.email)).one() == "older@example.com"


def test_restore_refuses_a_backup_with_a_live_write_ahead_log(env: Env) -> None:
    backup = create_backup(env.factory, env.settings, env.clock, origin="manual")
    env.engine.dispose()
    sidecar = backup.path.with_name(backup.path.name + "-wal")
    sidecar.write_bytes(b"pending")
    try:
        with pytest.raises(RestoreError) as error:
            restore_backup(env.settings, backup.path, env.clock)
        assert error.value.code == "has_wal"
    finally:
        sidecar.unlink(missing_ok=True)


def test_inspection_rejects_a_file_without_alembic_metadata(env: Env, tmp_path: Path) -> None:
    empty = tmp_path / "empty.db"
    with sqlite3.connect(empty) as connection:
        connection.execute("CREATE TABLE users (id TEXT)")
    assert inspect_backup(empty, env.settings).state == "unknown"
