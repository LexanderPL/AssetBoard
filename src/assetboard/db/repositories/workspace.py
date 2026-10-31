"""The current workspace's own row, and creating a workspace with its first owner.

``Workspace`` is the tenant itself and has no ``workspace_id``, so it is not a
``ScopedRepository``. This repository can only ever read or change ``ctx.workspace.id``, and
only the fields in ``UPDATABLE_FIELDS``.

``delete_with_all_data`` removes the current workspace and every row that belongs to it (hard
deletion, ADR-0020). It finds the rows from the metadata, not from a hand-kept list, so a new
workspace-owned model is covered without changes here.

``create_owned_workspace`` is the one place that creates a workspace (first-run setup,
registration, ``create-admin``, OIDC and proxy auto-create). It runs before any Context exists
and returns a ``NewWorkspaceScope`` for writing into the new workspace through the scoped
repositories (its owner membership, the seeded categories, audit events).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Column, CursorResult, MetaData, Table, delete, select, update
from sqlalchemy.orm import Session

import assetboard.db.models  # noqa: F401 - registers every table in Base.metadata
from assetboard.db.base import Base
from assetboard.db.models.identity import Workspace
from assetboard.db.repositories.base import NotFound, WorkspaceScope
from assetboard.db.repositories.membership import MembershipRepository

UPDATABLE_FIELDS = frozenset(
    {
        "name",
        "base_currency",
        "country_code",
        "timezone",
        "locale",
        "auto_copy_plan",
        "require_two_factor",
        "taxes_enabled",
        "upcoming_days",
    }
)


class CurrentWorkspaceRepository:
    def __init__(self, session: Session, ctx: WorkspaceScope) -> None:
        self._session = session
        self._workspace_id: UUID = ctx.workspace.id

    def get(self) -> Workspace:
        statement = select(Workspace).where(Workspace.id == self._workspace_id)
        workspace = self._session.scalars(statement).one_or_none()
        if workspace is None:
            raise NotFound
        return workspace

    def lock(self, *, exclusive: bool) -> None:
        """Lock the current workspace's row until the transaction ends.

        ``FOR SHARE`` (``exclusive=False``) or ``FOR UPDATE`` on PostgreSQL. SQLite has no row
        locks; there every write already runs under ``BEGIN IMMEDIATE``, which serialises
        writers, and SQLAlchemy leaves the clause out. Used by ``services.months``: writes to a
        month take the shared lock before checking that it is open, closing and reopening take
        the exclusive one, so a close can never interleave with a write (docs/architecture.md).
        """
        statement = select(Workspace.id).where(Workspace.id == self._workspace_id)
        statement = statement.with_for_update(read=not exclusive)
        if self._session.execute(statement).one_or_none() is None:
            raise NotFound

    def update(self, **values: object) -> Workspace:
        """Change allowed fields of the current workspace; returns it."""
        invalid = set(values) - UPDATABLE_FIELDS
        if invalid:
            raise ValueError(f"cannot update {', '.join(sorted(invalid))}")
        workspace = self.get()
        for name, value in values.items():
            setattr(workspace, name, value)
        self._session.flush()
        return workspace

    def delete_with_all_data(self, *, now: datetime) -> None:
        """Remove the current workspace and every row that belongs to it, in the caller's
        transaction (ADR-0020).

        1. Every table with a ``workspace_id`` column loses its rows of this workspace, children
           before parents (the reverse of the metadata's dependency order), so the composite
           foreign keys between workspace-owned tables are never violated on any database.
           Audit events of the workspace go too: they hold its financial history.
        2. Other references to the workspace (nullable columns such as
           ``user_sessions.current_workspace_id``) are set to NULL explicitly, without relying
           on ``ON DELETE SET NULL``; ``updated_at`` is set too where a table has one.
        3. The workspace row itself is deleted.

        Plain SQL ``DELETE``/``UPDATE`` statements, always filtered by this workspace's id: the
        append-only guard applies to ORM deletes, and no ORM object of these rows is changed.
        Callers lock the workspace (``lock(exclusive=True)``) first.
        """
        owned, references = workspace_references()
        for table in owned:
            self._session.execute(delete(table).where(table.c.workspace_id == self._workspace_id))
        for column in references:
            table = column.table
            values: dict[str, object] = {column.name: None}
            if "updated_at" in table.c:
                values["updated_at"] = now
            self._session.execute(update(table).where(column == self._workspace_id).values(values))
        workspaces = Base.metadata.tables["workspaces"]
        result = cast(
            "CursorResult[Any]",
            self._session.execute(delete(workspaces).where(workspaces.c.id == self._workspace_id)),
        )
        if result.rowcount != 1:
            raise NotFound


def workspace_references(
    metadata: MetaData = Base.metadata, tenant_table: str = "workspaces"
) -> tuple[list[Table], list[Column[Any]]]:
    """What ``delete_with_all_data`` clears, from the metadata.

    Returns the workspace-owned tables (those with a ``workspace_id`` column), children first,
    and the other columns with a foreign key to ``workspaces.id``. A non-nullable reference
    outside a ``workspace_id`` column could be neither deleted nor cleared, so it is refused.
    """
    workspaces = metadata.tables[tenant_table]
    owned: list[Table] = []
    references: list[Column[Any]] = []
    for table in reversed(metadata.sorted_tables):
        if table is workspaces:
            continue
        if "workspace_id" in table.c:
            owned.append(table)
        for foreign_key in table.foreign_keys:
            column = foreign_key.parent
            if foreign_key.column is not workspaces.c.id or column.name == "workspace_id":
                continue
            if not column.nullable:
                raise RuntimeError(
                    f"{table.name}.{column.name} references workspaces but is neither "
                    "workspace_id nor nullable; workspace deletion cannot clear it"
                )
            references.append(column)
    return owned, references


@dataclass(frozen=True, slots=True)
class _WorkspaceId:
    id: UUID


@dataclass(frozen=True, slots=True)
class NewWorkspaceScope:
    """A ``WorkspaceScope`` for a workspace created in the current transaction."""

    workspace: _WorkspaceId


def create_owned_workspace(
    session: Session,
    *,
    name: str,
    base_currency: str,
    country_code: str | None,
    timezone: str,
    locale: str,
    owner_user_id: UUID,
) -> tuple[Workspace, NewWorkspaceScope]:
    """Create a workspace and make ``owner_user_id`` its owner, in the caller's transaction.

    The values are validated by the caller (``services.workspace_options``).
    """
    workspace = Workspace(
        name=name,
        base_currency=base_currency,
        country_code=country_code,
        timezone=timezone,
        locale=locale,
    )
    session.add(workspace)
    session.flush()
    scope = NewWorkspaceScope(_WorkspaceId(workspace.id))
    MembershipRepository(session, scope).add_member(user_id=owner_user_id, role="owner")
    session.flush()
    return workspace, scope
