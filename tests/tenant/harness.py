"""Reusable checks for the tenant-isolation test.

They take the declarative base, models and repositories as parameters, so the self-tests run
the very same functions against a scratch base with deliberately broken models.
"""

from __future__ import annotations

import inspect as pyinspect
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.orm import DeclarativeBase, Session

from assetboard.clock import Clock
from assetboard.db.repositories.base import (
    NotFound,
    ScopedRepository,
    UnsupportedOperation,
    models_marked_workspace_owned,
)
from assetboard.services.context import Context, Role, UserRef, WorkspaceRef


def context_for(workspace_id: UUID, clock: Clock, role: Role = Role.OWNER) -> Context:
    return Context(
        user=UserRef(
            id=workspace_id,  # any id: repositories scope by workspace only
            email="synthetic@example.com",
            display_name="Synthetic",
            locale="en",
            is_instance_admin=False,
        ),
        workspace=WorkspaceRef(
            id=workspace_id,
            name="Synthetic",
            base_currency="EUR",
            locale="en",
            timezone="UTC",
            country_code=None,
        ),
        role=role,
        clock=clock,
    )


def raw_row(session: Session, model: type[Any], row_id: object) -> dict[str, object] | None:
    """Every column of a row, read straight from the table (no ORM identity map)."""
    table = inspect(model).local_table
    row = session.connection().execute(select(table).where(table.c.id == row_id)).one_or_none()
    return dict(row._mapping) if row is not None else None


def check_isolation(
    session: Session,
    model: type[Any],
    repository: type[ScopedRepository[Any]],
    factory: Callable[[Session, UUID], Any],
    update_values: Mapping[str, object],
    ctx_a: Context,
    ctx_b: Context,
) -> None:
    """User B can never read, update or delete user A's rows, and vice versa.

    Positive controls run first (each side can read, list, update and delete its own rows), so a
    repository that returns nothing at all cannot pass. After the negative checks each foreign
    row is compared column by column with its state before.
    """
    for owner, other in ((ctx_a, ctx_b), (ctx_b, ctx_a)):
        mine = repository(session, owner)
        theirs = repository(session, other)
        own_row = mine.add(factory(session, owner.workspace.id))
        their_row = theirs.add(factory(session, other.workspace.id))

        # Positive control: the owner sees and changes its own rows.
        assert mine.get(own_row.id) is own_row
        assert mine.get_or_raise(own_row.id) is own_row
        listed = {row.id for row in mine.list()}
        assert own_row.id in listed and their_row.id not in listed
        spare = mine.add(factory(session, owner.workspace.id))
        if "update" in repository.unsupported:
            with pytest.raises(UnsupportedOperation):
                mine.update(spare.id, **update_values)
        else:
            assert update_values, f"{model.__name__} needs update_values in TENANT_CASES"
            mine.update(spare.id, **update_values)
            for name, value in update_values.items():
                assert getattr(spare, name) == value
        if "delete" in repository.unsupported:
            with pytest.raises(UnsupportedOperation):
                mine.delete(spare.id)
        else:
            mine.delete(spare.id)
            assert raw_row(session, model, spare.id) is None

        # Negative checks: the other workspace cannot reach the owner's row.
        before = raw_row(session, model, own_row.id)
        assert before is not None and before["workspace_id"] == owner.workspace.id
        assert theirs.get(own_row.id) is None
        with pytest.raises(NotFound):
            theirs.get_or_raise(own_row.id)
        assert own_row.id not in {row.id for row in theirs.list()}
        expected_update = UnsupportedOperation if "update" in repository.unsupported else NotFound
        with pytest.raises(expected_update):
            theirs.update(own_row.id, **update_values)
        expected_delete = UnsupportedOperation if "delete" in repository.unsupported else NotFound
        with pytest.raises(expected_delete):
            theirs.delete(own_row.id)
        with pytest.raises(expected_delete):
            theirs.delete(own_row)
        session.flush()
        session.expire_all()
        assert raw_row(session, model, own_row.id) == before


def public_methods(cls: type[Any], stop: type[Any] | None = None) -> set[str]:
    """Public callables defined on ``cls`` and its bases, up to (excluding) ``stop``'s bases."""
    names: set[str] = set()
    for klass in cls.__mro__:
        if klass is object or (stop is not None and klass in stop.__mro__[1:]):
            continue
        for name, value in vars(klass).items():
            if not name.startswith("_") and pyinspect.isfunction(value):
                names.add(name)
    return names


def uncovered_methods(
    repositories: Iterable[type[ScopedRepository[Any]]],
    base_coverage: Mapping[str, str],
    extra_coverage: Mapping[tuple[str, str], str],
) -> list[str]:
    missing: list[str] = []
    base_methods = public_methods(ScopedRepository)
    for repository in repositories:
        for name in sorted(public_methods(repository, stop=ScopedRepository)):
            if name in base_methods and name in base_coverage:
                continue
            if (repository.__name__, name) not in extra_coverage:
                missing.append(f"{repository.__name__}.{name}")
    return missing


def non_composite_workspace_fks(base: type[DeclarativeBase]) -> list[str]:
    """Foreign keys between workspace-owned tables that do not include ``workspace_id``.

    A single-column FK from one workspace-owned table to another would let a row point at a
    parent in another workspace; the FK must be ``(workspace_id, x_id) -> (workspace_id, id)``.
    """
    owned_tables = {inspect(model).local_table for model in models_marked_workspace_owned(base)}
    problems: list[str] = []
    for table in sorted(owned_tables, key=lambda t: t.name):
        for constraint in table.foreign_key_constraints:
            if constraint.referred_table not in owned_tables:
                continue
            local = {column.name for column in constraint.columns}
            remote = {element.column.name for element in constraint.elements}
            if "workspace_id" not in local or "workspace_id" not in remote:
                problems.append(
                    f"{table.name}({', '.join(sorted(local))}) -> {constraint.referred_table.name}"
                )
    return problems
