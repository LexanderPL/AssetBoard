"""Tenant isolation (F1.2): every workspace-owned model, through its repository, both directions.

Adding a workspace-owned model without a repository and a factory in ``tests/tenant/factories.py``
fails this module by design. See docs/architecture.md, "How to add a new workspace-owned model".
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import ForeignKeyConstraint, MetaData, String, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

import assetboard.db.repositories  # noqa: F401 - registers every repository
from assetboard.clock import FixedClock
from assetboard.db.base import (
    CONSTRAINT_NAMING_CONVENTION,
    Base,
    IdMixin,
    TimestampMixin,
    WorkspaceOwned,
    WorkspaceReassignmentError,
    WorkspaceScopedMixin,
)
from assetboard.db.engine import create_database_engine
from assetboard.db.models.identity import AuditEvent, Membership, User, Workspace
from assetboard.db.repositories import (
    AuditEventRepository,
    ForeignWorkspace,
    MembershipRepository,
    ScopedRepository,
    UserMembershipReader,
)
from assetboard.db.repositories.base import (
    models_marked_workspace_owned,
    models_with_workspace_column,
    repositories_for,
)
from tests.tenant.coverage import (
    BASE_METHOD_COVERAGE,
    EXTRA_METHOD_COVERAGE,
    READER_METHOD_COVERAGE,
)
from tests.tenant.factories import TENANT_CASES
from tests.tenant.harness import (
    check_isolation,
    context_for,
    non_composite_workspace_fks,
    public_methods,
    raw_row,
    uncovered_methods,
)

ARCHITECTURE_HINT = "see docs/architecture.md, 'How to add a new workspace-owned model'"


def _workspace(session: Session, name: str) -> Workspace:
    workspace = Workspace(name=name, base_currency="EUR", locale="en", timezone="UTC")
    session.add(workspace)
    session.flush()
    return workspace


@pytest.fixture
def two_workspaces(db_session: Session, fixed_clock: FixedClock) -> tuple[Any, Any]:
    first = _workspace(db_session, "Workspace A")
    second = _workspace(db_session, "Workspace B")
    return context_for(first.id, fixed_clock), context_for(second.id, fixed_clock)


def _owned_models() -> list[type[Any]]:
    return sorted(models_with_workspace_column(Base), key=lambda model: model.__name__)


# Discovery and registries


def test_discovery_finds_workspace_owned_models() -> None:
    by_column = models_with_workspace_column(Base)
    by_marker = models_marked_workspace_owned(Base)
    assert by_column == by_marker, "a model has workspace_id without WorkspaceOwned, or vice versa"
    assert {Membership, AuditEvent} <= by_column


@pytest.mark.parametrize("model", _owned_models(), ids=lambda model: model.__name__)
def test_every_workspace_owned_model_has_a_repository_and_a_factory(model: type[Any]) -> None:
    assert model in repositories_for(Base), (
        f"{model.__name__} has no repository; {ARCHITECTURE_HINT}"
    )
    assert model in TENANT_CASES, f"{model.__name__} has no tenant factory; {ARCHITECTURE_HINT}"


def test_registry_keys_equal_the_discovered_set() -> None:
    discovered = models_with_workspace_column(Base)
    assert set(repositories_for(Base)) == discovered
    assert set(TENANT_CASES) == discovered


@pytest.mark.parametrize("model", _owned_models(), ids=lambda model: model.__name__)
def test_factories_build_rows_for_the_requested_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any], model: type[Any]
) -> None:
    for ctx in two_workspaces:
        row = TENANT_CASES[model].factory(db_session, ctx.workspace.id)
        assert type(row) is model
        assert row.workspace_id == ctx.workspace.id
        repositories_for(Base)[model](db_session, ctx).add(row)
        stored = raw_row(db_session, model, row.id)
        assert stored is not None and stored["workspace_id"] == ctx.workspace.id


# The cross-tenant check


@pytest.mark.parametrize("model", _owned_models(), ids=lambda model: model.__name__)
def test_user_b_cannot_read_update_or_delete_user_a_rows(
    db_session: Session, two_workspaces: tuple[Any, Any], model: type[Any]
) -> None:
    case = TENANT_CASES[model]
    repository = repositories_for(Base)[model]
    assert repository.unsupported == case.unsupported, "unsupported operations changed"
    ctx_a, ctx_b = two_workspaces
    check_isolation(
        db_session,
        model,
        repositories_for(Base)[model],
        case.factory,
        case.update_values,
        ctx_a,
        ctx_b,
    )


def test_add_refuses_rows_of_another_workspace_or_type(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    ctx_a, ctx_b = two_workspaces
    foreign = TENANT_CASES[Membership].factory(db_session, ctx_a.workspace.id)
    with pytest.raises(ForeignWorkspace):
        MembershipRepository(db_session, ctx_b).add(foreign)
    assert foreign not in db_session
    with pytest.raises(TypeError):
        MembershipRepository(db_session, ctx_b).add(  # type: ignore[arg-type]
            TENANT_CASES[AuditEvent].factory(db_session, ctx_b.workspace.id)
        )
    unscoped = TENANT_CASES[Membership].factory(db_session, ctx_b.workspace.id)
    unscoped.workspace_id = None
    assert MembershipRepository(db_session, ctx_b).add(unscoped).workspace_id == ctx_b.workspace.id


@pytest.mark.parametrize("model", _owned_models(), ids=lambda model: model.__name__)
def test_moving_a_row_between_workspaces_fails_at_flush(
    db_session: Session, two_workspaces: tuple[Any, Any], model: type[Any]
) -> None:
    ctx_a, ctx_b = two_workspaces
    row = repositories_for(Base)[model](db_session, ctx_a).add(
        TENANT_CASES[model].factory(db_session, ctx_a.workspace.id)
    )
    row.workspace_id = ctx_b.workspace.id
    with pytest.raises(WorkspaceReassignmentError):
        db_session.flush()


def test_instance_level_audit_event_cannot_be_moved_into_a_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any], fixed_clock: FixedClock
) -> None:
    event = AuditEvent(
        id=uuid4(),
        actor_type="system",
        action="auth.test",
        entity_type="user",
        ts=fixed_clock.now(),
    )
    db_session.add(event)
    db_session.flush()
    db_session.expire(event, ["workspace_id"])
    event.workspace_id = two_workspaces[0].workspace.id  # NULL -> a workspace
    with pytest.raises(WorkspaceReassignmentError):
        db_session.flush()


def test_instance_audit_events_are_invisible_to_workspace_repositories(
    db_session: Session, two_workspaces: tuple[Any, Any], fixed_clock: FixedClock
) -> None:
    instance_event = AuditEvent(
        id=uuid4(),
        actor_type="system",
        action="auth.test",
        entity_type="user",
        ts=fixed_clock.now(),
    )
    db_session.add(instance_event)
    db_session.flush()
    for ctx in two_workspaces:
        repository = AuditEventRepository(db_session, ctx)
        assert repository.get(instance_event.id) is None
        assert instance_event.id not in {event.id for event in repository.list()}


def test_user_membership_reader_returns_only_its_users_rows(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    ctx_a, ctx_b = two_workspaces
    alice = User(email="alice@example.com", display_name="Alice")
    bob = User(email="bob@example.com", display_name="Bob")
    db_session.add_all([alice, bob])
    db_session.flush()
    db_session.add_all(
        [
            Membership(workspace_id=ctx_a.workspace.id, user_id=alice.id, role="owner"),
            Membership(workspace_id=ctx_b.workspace.id, user_id=bob.id, role="owner"),
        ]
    )
    db_session.flush()
    alice_reader = UserMembershipReader(db_session, alice.id)
    bob_reader = UserMembershipReader(db_session, bob.id)
    assert [m.workspace_id for m in alice_reader.memberships()] == [ctx_a.workspace.id]
    assert [m.workspace_id for m in bob_reader.memberships()] == [ctx_b.workspace.id]
    assert alice_reader.membership_in(ctx_a.workspace.id) is not None
    assert alice_reader.membership_in(ctx_b.workspace.id) is None
    assert bob_reader.membership_in(ctx_a.workspace.id) is None


# Method coverage (#11)


def test_every_public_repository_method_is_covered() -> None:
    module = sys.modules[__name__]
    repositories = list(repositories_for(Base).values())
    assert uncovered_methods(repositories, BASE_METHOD_COVERAGE, EXTRA_METHOD_COVERAGE) == []
    assert set(BASE_METHOD_COVERAGE) == public_methods(ScopedRepository), "stale base entry"
    names = {repository.__name__: repository for repository in repositories}
    for repository_name, method in EXTRA_METHOD_COVERAGE:
        assert method in public_methods(names[repository_name]), "stale extra entry"
    assert set(READER_METHOD_COVERAGE) == public_methods(UserMembershipReader)
    covering = {
        *BASE_METHOD_COVERAGE.values(),
        *EXTRA_METHOD_COVERAGE.values(),
        *READER_METHOD_COVERAGE.values(),
    }
    for test_name in covering:
        assert callable(getattr(module, test_name, None)), f"no test named {test_name}"


# Composite foreign keys between workspace-owned tables


def test_workspace_owned_foreign_keys_include_workspace_id() -> None:
    assert non_composite_workspace_fks(Base) == []


# Self-tests: the same checks on a scratch base with deliberately broken models (#17)


class ScratchBase(DeclarativeBase):
    metadata = MetaData(naming_convention=CONSTRAINT_NAMING_CONVENTION)


class ScratchWorkspace(IdMixin, TimestampMixin, ScratchBase):
    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(String(80), nullable=False)


class ScratchNote(IdMixin, WorkspaceScopedMixin, ScratchBase):
    __tablename__ = "scratch_notes"

    text: Mapped[str] = mapped_column(String(80), nullable=False)


class ScratchUnmarked(IdMixin, ScratchBase):
    """Has workspace_id but not the WorkspaceOwned marker."""

    __tablename__ = "scratch_unmarked"

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class ScratchChild(IdMixin, WorkspaceScopedMixin, ScratchBase):
    """A plain FK to another workspace-owned table: must be reported."""

    __tablename__ = "scratch_children"

    note_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    __table_args__ = (ForeignKeyConstraint(["note_id"], ["scratch_notes.id"]),)


class ScratchTag(IdMixin, WorkspaceScopedMixin, ScratchBase):
    __tablename__ = "scratch_tags"

    label: Mapped[str] = mapped_column(String(80), nullable=False)


class ChattyTagRepository(ScopedRepository[ScratchTag]):
    """Adds a public method without an entry in the coverage registry."""

    model = ScratchTag

    def search(self, label: str) -> list[ScratchTag]:
        return list(self._session.scalars(self._scoped_select().where(ScratchTag.label == label)))


class LeakyNoteRepository(ScopedRepository[ScratchNote]):
    model = ScratchNote

    def _scoped_select(self) -> Any:  # forgets the workspace filter
        from sqlalchemy import select

        return select(ScratchNote)


@pytest.fixture
def scratch_session(tmp_path: Path, fixed_clock: FixedClock) -> Iterator[Session]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'scratch.db'}")
    ScratchBase.metadata.create_all(engine)
    try:
        with Session(engine, info={"clock": fixed_clock}) as session:
            yield session
    finally:
        engine.dispose()


def _note(session: Session, workspace_id: UUID) -> ScratchNote:
    return ScratchNote(workspace_id=workspace_id, text="note")


def test_isolation_harness_detects_missing_repository() -> None:
    by_column = models_with_workspace_column(ScratchBase)
    assert ScratchUnmarked in by_column
    assert by_column != models_marked_workspace_owned(ScratchBase)  # the marker check fires
    # ScratchChild has no repository: the registry check fires for it.
    assert ScratchChild in by_column and ScratchChild not in repositories_for(ScratchBase)
    assert repositories_for(ScratchBase) == {
        ScratchNote: LeakyNoteRepository,
        ScratchTag: ChattyTagRepository,
    }


def test_isolation_harness_detects_unscoped_repository(
    scratch_session: Session, fixed_clock: FixedClock
) -> None:
    first, second = ScratchWorkspace(name="A"), ScratchWorkspace(name="B")
    scratch_session.add_all([first, second])
    scratch_session.flush()
    with pytest.raises(AssertionError):
        check_isolation(
            scratch_session,
            ScratchNote,
            LeakyNoteRepository,
            _note,
            {"text": "changed"},
            context_for(first.id, fixed_clock),
            context_for(second.id, fixed_clock),
        )


def test_isolation_harness_detects_uncovered_methods() -> None:
    assert uncovered_methods([ChattyTagRepository], BASE_METHOD_COVERAGE, {}) == [
        "ChattyTagRepository.search"
    ]
    covered = {("ChattyTagRepository", "search"): "test_search"}
    assert uncovered_methods([ChattyTagRepository], BASE_METHOD_COVERAGE, covered) == []


def test_isolation_harness_detects_non_composite_foreign_keys() -> None:
    assert non_composite_workspace_fks(ScratchBase) == [
        "scratch_children(note_id) -> scratch_notes"
    ]


def test_composite_foreign_keys_pass_the_check() -> None:
    class CompositeBase(DeclarativeBase):
        pass

    class Parent(IdMixin, WorkspaceOwned, CompositeBase):
        __tablename__ = "parents"

        workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)

    class Child(IdMixin, WorkspaceOwned, CompositeBase):
        __tablename__ = "children"

        workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
        parent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
        __table_args__ = (
            ForeignKeyConstraint(
                ["workspace_id", "parent_id"], ["parents.workspace_id", "parents.id"]
            ),
        )

    assert {Parent, Child} == models_marked_workspace_owned(CompositeBase)
    assert non_composite_workspace_fks(CompositeBase) == []
