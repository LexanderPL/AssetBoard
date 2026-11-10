"""Service tests for F9.11b demo mode: lifecycle, reset atomicity and login."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import func, select

from assetboard.config import Settings
from assetboard.db.models.identity import (
    AuditEvent,
    InstanceSettings,
    Membership,
    User,
    UserSession,
    Workspace,
)
from assetboard.db.models.import_batch import ImportBatch
from assetboard.db.models.ledger import Category, Transaction
from assetboard.domain.demo import DEMO_DATASET_VERSION
from assetboard.services import demo as demo_service
from assetboard.services import imports
from assetboard.services.admin_users import NotInstanceAdmin
from assetboard.services.demo_mode import (
    DEMO_EMAIL,
    DemoEmailTaken,
    delete_demo_instance,
    demo_login,
    disable_demo_instance,
    ensure_demo_instance,
    read_demo_status,
    reset_demo_instance,
)
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    yield from workspace_db(tmp_path)


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "data_dir": tmp_path / "data",
        "demo_enabled": True,
        "demo_timezone": "Europe/Warsaw",
        "demo_reset_time": "03:00",
        "demo_ip_limit": 20,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def _demo_ids(db: WorkspaceDb) -> tuple[UUID, UUID]:
    with db.factory() as session:
        user = session.scalars(select(User).where(User.is_demo.is_(True))).one()
        workspace = session.scalars(select(Workspace).where(Workspace.is_demo.is_(True))).one()
        return user.id, workspace.id


def _category_count(db: WorkspaceDb, workspace_id: UUID) -> int:
    with db.factory() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(Category)
                .where(Category.workspace_id == workspace_id)
            )
            or 0
        )


def _add_rogue_category(db: WorkspaceDb, workspace_id: UUID, name: str) -> None:
    with db.factory.begin() as session:
        session.add(
            Category(
                workspace_id=workspace_id,
                name=name,
                name_key=name.lower(),
                kind="expense",
                treatment="regular",
                color="#123456",
                sort_order=999,
            )
        )


def test_ensure_creates_one_demo_user_and_workspace_idempotently(
    db: WorkspaceDb, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    first = ensure_demo_instance(db.factory, settings, db.clock, settings.data_dir)

    assert first.created is True
    assert first.reset_due is False
    user_id, workspace_id = _demo_ids(db)
    assert (first.user_id, first.workspace_id) == (user_id, workspace_id)

    with db.factory() as session:
        user = session.get(User, user_id)
        workspace = session.get(Workspace, workspace_id)
        assert user is not None and workspace is not None
        assert user.email == DEMO_EMAIL
        assert user.display_name == "Demo"
        assert user.password_hash is None
        assert user.is_demo is True
        assert user.is_active is True
        assert user.is_instance_admin is False
        assert user.birth_year == 2026 - 38
        assert user.life_expectancy == 88
        assert workspace.name == "Demo"
        assert workspace.base_currency == "EUR"
        assert workspace.locale == "en"
        assert workspace.timezone == "Europe/Warsaw"
        assert workspace.is_demo is True
        members = list(
            session.scalars(select(Membership).where(Membership.workspace_id == workspace_id))
        )
        assert len(members) == 1
        assert members[0].user_id == user_id
        assert members[0].role == "owner"
        assert (
            session.scalar(
                select(func.count())
                .select_from(Transaction)
                .where(Transaction.workspace_id == workspace_id)
            )
            or 0
        ) > 0
        row = session.get(InstanceSettings, 1)
        assert row is not None
        assert row.demo_last_reset_at is not None
        assert row.demo_last_reset_error is None
        assert row.demo_dataset_version == DEMO_DATASET_VERSION

    second = ensure_demo_instance(db.factory, settings, db.clock, settings.data_dir)
    assert second.created is False
    assert (second.user_id, second.workspace_id) == (user_id, workspace_id)
    assert second.reset_due is False


def test_ensure_aligns_the_workspace_with_changed_instance_defaults(
    db: WorkspaceDb, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    ensure_demo_instance(db.factory, settings, db.clock, settings.data_dir)
    _, workspace_id = _demo_ids(db)

    changed = _settings(
        tmp_path,
        default_currency="USD",
        default_locale="pl",
        demo_timezone="UTC",
    )
    status = ensure_demo_instance(db.factory, changed, db.clock, changed.data_dir)

    with db.factory() as session:
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None
        assert workspace.name == "Demo"
        assert workspace.base_currency == "USD"
        assert workspace.locale == "pl"
        assert workspace.timezone == "UTC"
    assert status.reset_due is True  # the next startup rebuilds in the new defaults


def test_a_real_account_with_the_demo_email_is_never_taken_over(
    db: WorkspaceDb, tmp_path: Path
) -> None:
    with db.factory.begin() as session:
        session.add(
            User(
                email=DEMO_EMAIL,
                display_name="Real person",
                password_hash="hash",
                is_active=True,
                is_demo=False,
            )
        )
    settings = _settings(tmp_path)
    with pytest.raises(DemoEmailTaken):
        ensure_demo_instance(db.factory, settings, db.clock, settings.data_dir)
    with db.factory() as session:
        assert session.scalars(select(Workspace).where(Workspace.is_demo.is_(True))).first() is None


def test_reset_replaces_rows_but_keeps_ids_membership_and_sessions(
    db: WorkspaceDb, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    ensure_demo_instance(db.factory, settings, db.clock, settings.data_dir)
    user_id, workspace_id = _demo_ids(db)
    _add_rogue_category(db, workspace_id, "Rogue")
    with db.factory() as session:
        membership_id = session.scalars(
            select(Membership.id).where(Membership.workspace_id == workspace_id)
        ).one()
        batch_id = session.scalars(
            select(ImportBatch.id).where(ImportBatch.workspace_id == workspace_id)
        ).one()
        old_file = imports._upload_path(settings.data_dir, workspace_id, batch_id)
        old_file.parent.mkdir(parents=True, exist_ok=True)
        old_file.write_bytes(b"old upload")
        before = int(
            session.scalar(
                select(func.count())
                .select_from(Transaction)
                .where(Transaction.workspace_id == workspace_id)
            )
            or 0
        )
    login = demo_login(db.factory, settings, db.clock, host="192.0.2.10", user_agent="test")
    assert login is not None and login.session_id is not None
    with db.factory() as session:
        session_hash = []
        for row in session.scalars(select(UserSession).where(UserSession.user_id == user_id)):
            session_hash.append(row.id_hash)
    assert session_hash

    result = reset_demo_instance(db.factory, settings, db.clock, settings.data_dir, reason="manual")

    assert result.ok is True
    assert result.version == DEMO_DATASET_VERSION
    assert _demo_ids(db) == (user_id, workspace_id)
    with db.factory() as session:
        assert session.get(Membership, membership_id) is not None
        assert session.get(Workspace, workspace_id) is not None
        assert session.get(User, user_id) is not None
        assert (
            session.scalar(
                select(func.count())
                .select_from(Category)
                .where(Category.workspace_id == workspace_id, Category.name == "Rogue")
            )
            or 0
        ) == 0
        after = int(
            session.scalar(
                select(func.count())
                .select_from(Transaction)
                .where(Transaction.workspace_id == workspace_id)
            )
            or 0
        )
        assert after > 0 and after == before  # the rogue row is gone, dataset rebuilt
        assert set(
            session.scalars(select(UserSession.id_hash).where(UserSession.user_id == user_id))
        ) == set(session_hash)
        row = session.get(InstanceSettings, 1)
        assert row is not None and row.demo_last_reset_at is not None
        assert row.demo_last_reset_error is None
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "demo.reset", AuditEvent.workspace_id.is_(None)
                )
            )
        )
        assert events and events[-1].data["reason"] == "manual"
        assert events[-1].data["status"] == "ok"
    assert not old_file.exists()


def test_a_failed_reset_keeps_the_old_data_and_old_files(
    db: WorkspaceDb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    ensure_demo_instance(db.factory, settings, db.clock, settings.data_dir)
    _, workspace_id = _demo_ids(db)
    _add_rogue_category(db, workspace_id, "Rogue")
    with db.factory() as session:
        batch_id = session.scalars(
            select(ImportBatch.id).where(ImportBatch.workspace_id == workspace_id)
        ).one()
        old_file = imports._upload_path(settings.data_dir, workspace_id, batch_id)
        old_file.parent.mkdir(parents=True, exist_ok=True)
        old_file.write_bytes(b"old upload")

    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("sensitive detail")

    monkeypatch.setattr(demo_service, "build_demo_workspace_in_session", broken)
    result = reset_demo_instance(
        db.factory, settings, db.clock, settings.data_dir, reason="scheduled"
    )

    assert result.ok is False
    assert result.error == "RuntimeError"
    assert _category_count(db, workspace_id) > 0
    with db.factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(Category)
                .where(Category.workspace_id == workspace_id, Category.name == "Rogue")
            )
            or 0
        ) == 1
        row = session.get(InstanceSettings, 1)
        assert row is not None and row.demo_last_reset_error == "RuntimeError"
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "demo.reset", AuditEvent.workspace_id.is_(None)
                )
            )
        )
        assert events[-1].data["status"] == "failed"
    assert old_file.exists()


def test_reset_does_not_touch_another_workspace(db: WorkspaceDb, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ensure_demo_instance(db.factory, settings, db.clock, settings.data_dir)
    _, demo_workspace = _demo_ids(db)
    other = db.add_workspace("Real")
    db.add_member("real", "owner", workspace_id=other)
    _add_rogue_category(db, other, "Keep me")
    _add_rogue_category(db, demo_workspace, "Remove me")

    assert reset_demo_instance(
        db.factory, settings, db.clock, settings.data_dir, reason="manual"
    ).ok
    assert _category_count(db, other) >= 1
    with db.factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(Category)
                .where(Category.workspace_id == other, Category.name == "Keep me")
            )
            or 0
        ) == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(Category)
                .where(Category.workspace_id == demo_workspace, Category.name == "Remove me")
            )
            or 0
        ) == 0


def test_demo_login_counts_per_address_and_marks_the_session(
    db: WorkspaceDb, tmp_path: Path
) -> None:
    settings = _settings(tmp_path, demo_ip_limit=1)
    ensure_demo_instance(db.factory, settings, db.clock, settings.data_dir)
    user_id, workspace_id = _demo_ids(db)

    first = demo_login(db.factory, settings, db.clock, host="192.0.2.20", user_agent="test")
    assert first is not None and first.session_id is not None
    assert first.expires_at is not None
    second = demo_login(db.factory, settings, db.clock, host="192.0.2.20", user_agent="test")
    assert second is not None and second.throttled is True
    other_address = demo_login(db.factory, settings, db.clock, host="192.0.2.21", user_agent="test")
    assert other_address is not None and other_address.session_id is not None

    with db.factory() as session:
        row = session.scalars(select(UserSession).where(UserSession.user_id == user_id)).first()
        assert row is not None
        assert row.auth_method == "demo"
        assert row.current_workspace_id == workspace_id
        assert row.second_factor_verified is False
        login_event = session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "auth.login_success", AuditEvent.entity_id == str(user_id)
            )
        ).first()
        assert login_event is not None and login_event.data == {"method": "demo"}


def test_turning_demo_off_revokes_sessions_and_deactivates_the_user(
    db: WorkspaceDb, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    ensure_demo_instance(db.factory, settings, db.clock, settings.data_dir)
    user_id, workspace_id = _demo_ids(db)
    assert demo_login(db.factory, settings, db.clock, host="192.0.2.30", user_agent=None)

    assert disable_demo_instance(db.factory) is True
    with db.factory() as session:
        user = session.get(User, user_id)
        assert user is not None and user.is_active is False
        assert session.get(Workspace, workspace_id) is not None
        assert (
            session.scalar(
                select(func.count()).select_from(UserSession).where(UserSession.user_id == user_id)
            )
            or 0
        ) == 0


def test_delete_demo_data_requires_an_instance_admin_and_removes_only_the_demo(
    db: WorkspaceDb, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    ensure_demo_instance(db.factory, settings, db.clock, settings.data_dir)
    _, workspace_id = _demo_ids(db)
    other = db.add_workspace("Real")
    with db.factory.begin() as session:
        admin = User(
            email="admin@example.com",
            display_name="Admin",
            password_hash=None,
            is_active=True,
            is_instance_admin=True,
        )
        session.add(admin)
        session.flush()
        admin_id = admin.id

    with pytest.raises(NotInstanceAdmin):
        delete_demo_instance(
            db.factory,
            db.clock,
            actor_id=UUID(int=0),
        )
    assert delete_demo_instance(
        db.factory, db.clock, actor_id=admin_id, host="192.0.2.40", user_agent="test"
    )
    with db.factory() as session:
        assert session.get(Workspace, workspace_id) is None
        assert session.get(Workspace, other) is not None
        assert session.scalars(select(User).where(User.is_demo.is_(True))).first() is None
        event = session.scalars(select(AuditEvent).where(AuditEvent.action == "demo.deleted")).one()
        assert event.workspace_id is None
        row = session.get(InstanceSettings, 1)
        assert row is not None and row.demo_last_reset_at is None


def test_read_status_returns_the_last_reset_and_next_reset_is_in_the_demo_zone(
    db: WorkspaceDb, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    ensure_demo_instance(db.factory, settings, db.clock, settings.data_dir)
    status = read_demo_status(db.factory, clock=db.clock)
    assert status is not None and status.workspace_id is not None
    assert status.last_reset_at is not None
    assert status.reset_due is False
    now = datetime(2026, 3, 1, 1, 30, tzinfo=UTC)  # 02:30 in Warsaw
    from assetboard.services.demo_mode import next_reset_at

    assert next_reset_at(settings, now) == datetime(2026, 3, 1, 2, 0, tzinfo=UTC)
    assert next_reset_at(settings, now + timedelta(hours=1)) == datetime(
        2026, 3, 2, 2, 0, tzinfo=UTC
    )
