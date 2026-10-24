"""Deleting a workspace (ADR-0020): owners only, typed name, everything goes, one instance event."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session
from sqlalchemy.sql import ClauseElement

from assetboard.db.base import Base
from assetboard.db.models.identity import AuditEvent, UserSession, Workspace
from assetboard.db.repositories.base import repositories_for
from assetboard.services.auth import WebSession
from assetboard.services.context import NoWorkspace, PermissionDenied, Role, build_context
from assetboard.services.workspaces import (
    WorkspaceNameMismatch,
    delete_workspace,
    deletion_preview,
    update_settings,
)
from tests.tenant.factories import TENANT_CASES
from tests.tenant.harness import context_for, owned_models
from tests.tenant.workspace_db import WorkspaceDb, workspace_db

OWNED_MODELS = owned_models()


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    yield from workspace_db(tmp_path)


@pytest.fixture
def team(db: WorkspaceDb) -> WorkspaceDb:
    db.add_member("olga", "owner")
    db.add_member("ed", "editor")
    db.add_member("vic", "viewer")
    return db


def _fill(db: WorkspaceDb, workspace_id: UUID) -> None:
    """One row of every workspace-owned model (discovered, so new models are included)."""
    with db.factory.begin() as session:
        ctx = context_for(workspace_id, db.clock)
        for model in OWNED_MODELS:
            row = TENANT_CASES[model].factory(session, workspace_id)
            repositories_for(Base)[model](session, ctx).add(row)


def _counts(db: WorkspaceDb, workspace_id: UUID) -> dict[str, int]:
    with db.factory() as session:
        return {
            model.__name__: session.scalar(
                select(func.count()).select_from(model).where(model.workspace_id == workspace_id)
            )
            or 0
            for model in OWNED_MODELS
        }


def _session(db: WorkspaceDb, key: str, workspace_id: UUID | None) -> str:
    now = db.clock.now()
    id_hash = uuid4().hex * 2
    with db.factory.begin() as session:
        session.add(
            UserSession(
                id_hash=id_hash,
                user_id=db.users[key],
                created_at=now,
                last_seen_at=now,
                expires_at=now + timedelta(days=1),
                current_workspace_id=workspace_id,
            )
        )
    return id_hash


def _web_session(db: WorkspaceDb, key: str, id_hash: str) -> WebSession:
    with db.factory() as session:
        row = session.get(UserSession, id_hash)
        assert row is not None
        return WebSession(
            session_hash=id_hash,
            user_id=db.users[key],
            email=f"{key}@example.com",
            display_name=key.title(),
            locale="en",
            is_instance_admin=False,
            expires_at=row.expires_at,
            needs_touch=False,
            current_workspace_id=row.current_workspace_id,
        )


def _instance_events(db: WorkspaceDb) -> list[AuditEvent]:
    with db.factory() as session:
        return list(session.scalars(select(AuditEvent).where(AuditEvent.workspace_id.is_(None))))


def _workspace_exists(db: WorkspaceDb, workspace_id: UUID) -> bool:
    with db.factory() as session:
        return session.get(Workspace, workspace_id) is not None


def test_owner_deletes_the_workspace_and_all_its_data(team: WorkspaceDb) -> None:
    other = team.add_workspace("Other")
    team.add_member("stranger", "owner", workspace_id=other)
    _fill(team, team.workspace_id)
    _fill(team, other)
    other_before = _counts(team, other)

    delete_workspace(team.factory, team.ctx("olga", "owner"), confirm_name="Home")

    assert not _workspace_exists(team, team.workspace_id)
    assert set(_counts(team, team.workspace_id).values()) == {0}
    assert _counts(team, other) == other_before
    assert _workspace_exists(team, other)


def test_deletion_records_one_instance_event_without_amounts(team: WorkspaceDb) -> None:
    _fill(team, team.workspace_id)
    delete_workspace(team.factory, team.ctx("olga", "owner"), confirm_name="Home")
    [recorded] = [e for e in _instance_events(team) if e.action == "workspace.delete"]
    assert recorded.workspace_id is None
    assert (recorded.entity_type, recorded.entity_id) == ("workspace", str(team.workspace_id))
    assert recorded.actor_user_id == team.users["olga"]
    assert recorded.actor_type == "user"
    assert recorded.data == {"name": "Home"}
    assert recorded.ts == team.clock.now()


@pytest.mark.parametrize("member", ["ed", "vic"])
def test_editors_and_viewers_cannot_delete(team: WorkspaceDb, member: str) -> None:
    role = "editor" if member == "ed" else "viewer"
    with pytest.raises(PermissionDenied):
        delete_workspace(team.factory, team.ctx(member, role), confirm_name="Home")
    with pytest.raises(PermissionDenied):
        deletion_preview(team.factory, team.ctx(member, role))
    assert _workspace_exists(team, team.workspace_id)


@pytest.mark.parametrize("typed", ["", "home", "HOME", "Hom", "Home!", "Other"])
def test_a_wrong_name_is_refused_and_nothing_changes(team: WorkspaceDb, typed: str) -> None:
    _fill(team, team.workspace_id)
    before = _counts(team, team.workspace_id)
    with pytest.raises(WorkspaceNameMismatch):
        delete_workspace(team.factory, team.ctx("olga", "owner"), confirm_name=typed)
    assert _workspace_exists(team, team.workspace_id)
    assert _counts(team, team.workspace_id) == before
    assert _instance_events(team) == []


def test_surrounding_spaces_in_the_typed_name_are_ignored(team: WorkspaceDb) -> None:
    delete_workspace(team.factory, team.ctx("olga", "owner"), confirm_name="  Home ")
    assert not _workspace_exists(team, team.workspace_id)


def test_the_name_is_compared_with_the_stored_name(team: WorkspaceDb) -> None:
    owner = team.ctx("olga", "owner")  # its WorkspaceRef says "Home"
    update_settings(
        team.factory,
        owner,
        name="Family",
        base_currency="EUR",
        country_code=None,
        timezone="UTC",
        locale="en",
        auto_copy_plan=False,
    )
    with pytest.raises(WorkspaceNameMismatch):
        delete_workspace(team.factory, owner, confirm_name="Home")
    delete_workspace(team.factory, owner, confirm_name="Family")
    assert not _workspace_exists(team, team.workspace_id)


def test_members_move_to_another_workspace_or_to_no_workspace(team: WorkspaceDb) -> None:
    other = team.add_workspace("Other")
    team.add_member("ed", "viewer", workspace_id=other)
    olga_session = _session(team, "olga", team.workspace_id)
    ed_session = _session(team, "ed", team.workspace_id)
    ed_elsewhere = _session(team, "ed", other)

    delete_workspace(team.factory, team.ctx("olga", "owner"), confirm_name="Home")

    with team.factory() as session:
        selected = {
            key: session.scalar(
                select(UserSession.current_workspace_id).where(UserSession.id_hash == key)
            )
            for key in (olga_session, ed_session, ed_elsewhere)
        }
        assert selected == {olga_session: None, ed_session: None, ed_elsewhere: other}
        ed_ctx = build_context(session, _web_session(team, "ed", ed_session), clock=team.clock)
        assert (ed_ctx.workspace.id, ed_ctx.role) == (other, Role.VIEWER)
        with pytest.raises(NoWorkspace):  # Olga had only this workspace
            build_context(session, _web_session(team, "olga", olga_session), clock=team.clock)
        # A session still carrying the deleted id (read before the delete) does not crash.
        stale = replace(
            _web_session(team, "ed", ed_session), current_workspace_id=team.workspace_id
        )
        assert build_context(session, stale, clock=team.clock).workspace.id == other


def test_the_last_workspace_of_the_instance_can_be_deleted(db: WorkspaceDb) -> None:
    db.add_member("olga", "owner")
    delete_workspace(db.factory, db.ctx("olga", "owner"), confirm_name="Home")
    with db.factory() as session:
        assert session.scalar(select(func.count()).select_from(Workspace)) == 0


def test_the_preview_names_the_workspace_and_counts_other_members(team: WorkspaceDb) -> None:
    preview = deletion_preview(team.factory, team.ctx("olga", "owner"))
    assert (preview.name, preview.other_members) == ("Home", 2)


def test_deletion_is_portable_sql_scoped_to_the_workspace(team: WorkspaceDb) -> None:
    """Every statement compiles for PostgreSQL; the lock comes first; every write is scoped."""
    _fill(team, team.workspace_id)
    seen: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        seen.append(statement)

    compiled: list[str] = []

    def record_orm(state: Any) -> None:
        statement = state.statement
        assert isinstance(statement, ClauseElement)
        compiled.append(str(statement.compile(dialect=postgresql.dialect())))

    event.listen(team.engine, "before_cursor_execute", record)
    event.listen(Session, "do_orm_execute", record_orm)
    try:
        delete_workspace(team.factory, team.ctx("olga", "owner"), confirm_name="Home")
    finally:
        event.remove(team.engine, "before_cursor_execute", record)
        event.remove(Session, "do_orm_execute", record_orm)
    workspace_locks = [s for s in compiled if "FROM workspaces" in s and "FOR UPDATE" in s]
    first_delete = next(i for i, s in enumerate(compiled) if s.startswith("DELETE"))
    assert workspace_locks and compiled.index(workspace_locks[0]) < first_delete
    writes = [s for s in seen if s.startswith(("DELETE", "UPDATE"))]
    assert writes
    for statement in writes:
        assert "WHERE" in statement, statement
        assert "workspace_id = ?" in statement or "workspaces.id = ?" in statement, statement
    deleted_tables = [s.split()[2] for s in writes if s.startswith("DELETE")]
    assert deleted_tables[-1] == "workspaces"
