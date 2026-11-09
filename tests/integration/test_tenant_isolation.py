"""Tenant isolation (F1.2): every workspace-owned model, through its repository, both directions.

Adding a workspace-owned model without a repository and a factory in ``tests/tenant/factories.py``
fails this module by design. See docs/architecture.md, "How to add a new workspace-owned model".
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import (
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    MetaData,
    String,
    Table,
    Uuid,
    inspect,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from assetboard.clock import FixedClock
from assetboard.db.base import (
    CONSTRAINT_NAMING_CONVENTION,
    AppendOnlyViolationError,
    Base,
    IdMixin,
    TimestampMixin,
    WorkspaceOwned,
    WorkspaceReassignmentError,
    WorkspaceScopedMixin,
)
from assetboard.db.engine import create_database_engine
from assetboard.db.models.identity import (
    AuditEvent,
    Invitation,
    Membership,
    User,
    UserSession,
    Workspace,
)
from assetboard.db.repositories import (
    UPDATABLE_FIELDS,
    AssetClassRepository,
    AuditEventRepository,
    AuditFilter,
    BondTermsRepository,
    BudgetLineRepository,
    CategoryRepository,
    CurrentWorkspaceRepository,
    ForeignWorkspace,
    FxRateRepository,
    ImportBatchRepository,
    ImportCategoryMappingRepository,
    InstanceAuditReader,
    InstrumentAllocationRepository,
    InstrumentRepository,
    InvestmentTransactionRepository,
    InvitationRepository,
    InvitationTokenReader,
    ItemValuationRepository,
    LineUsage,
    MembershipRepository,
    MonthStateRepository,
    NetWorthCategoryRepository,
    NetWorthItemRepository,
    NetWorthSnapshotRepository,
    NotFound,
    PricePointRepository,
    PriceSourceConfigRepository,
    ProviderErrorRepository,
    RiskTargetPolicyRepository,
    ScopedRepository,
    TransactionRepository,
    UserMembershipReader,
)
from assetboard.db.repositories.base import models_marked_workspace_owned, repositories_for
from assetboard.db.repositories.workspace import workspace_references
from assetboard.domain.months import YearMonth
from assetboard.domain.transaction_search import SortOrder, TransactionFilter
from tests.tenant.coverage import (
    BASE_METHOD_COVERAGE,
    CURRENT_WORKSPACE_METHOD_COVERAGE,
    EXTRA_METHOD_COVERAGE,
    INSTANCE_AUDIT_READER_METHOD_COVERAGE,
    READER_METHOD_COVERAGE,
    TOKEN_READER_METHOD_COVERAGE,
)
from tests.tenant.factories import TENANT_CASES
from tests.tenant.harness import (
    ARCHITECTURE_HINT,
    check_isolation,
    context_for,
    discovery_problems,
    non_composite_workspace_fks,
    owned_models,
    public_methods,
    raw_row,
    relationships_into_owned_models,
    uncovered_methods,
)


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


OWNED_MODELS = owned_models()


# Discovery and registries


def test_discovery_finds_every_workspace_owned_model_with_repository_and_factory() -> None:
    assert {Membership, AuditEvent} <= set(OWNED_MODELS)
    assert discovery_problems(Base, repositories_for(Base), TENANT_CASES) == []


@pytest.mark.parametrize("model", OWNED_MODELS, ids=lambda model: model.__name__)
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


@pytest.mark.parametrize("model", OWNED_MODELS, ids=lambda model: model.__name__)
def test_any_exists_stays_in_its_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any], model: type[Any]
) -> None:
    ctx_a, ctx_b = two_workspaces
    repository = repositories_for(Base)[model]
    in_a = repository(db_session, ctx_a)
    in_b = repository(db_session, ctx_b)
    assert not in_a.any_exists()
    in_a.add(TENANT_CASES[model].factory(db_session, ctx_a.workspace.id))
    assert in_a.any_exists()
    assert not in_b.any_exists()
    in_b.add(TENANT_CASES[model].factory(db_session, ctx_b.workspace.id))
    assert in_b.any_exists()


@pytest.mark.parametrize("model", OWNED_MODELS, ids=lambda model: model.__name__)
def test_user_b_cannot_read_update_or_delete_user_a_rows(
    db_session: Session, two_workspaces: tuple[Any, Any], model: type[Any]
) -> None:
    case = TENANT_CASES[model]
    repository = repositories_for(Base)[model]
    assert repository.unsupported == case.unsupported, "unsupported operations changed"
    ctx_a, ctx_b = two_workspaces
    if model.__name__ == "RiskTargetPolicy":
        pytest.skip("singleton: covered by test_risk_policy_singleton_isolation")
    check_isolation(
        db_session,
        model,
        repositories_for(Base)[model],
        case.factory,
        case.update_values,
        ctx_a,
        ctx_b,
    )


def test_risk_policy_singleton_isolation(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    ctx_a, ctx_b = two_workspaces
    policies_a = RiskTargetPolicyRepository(db_session, ctx_a)
    policies_b = RiskTargetPolicyRepository(db_session, ctx_b)
    a_policy = policies_a.create(
        mode="fixed", safe_bp=5000, growth_bp=4000, speculative_bp=1000, tolerance_bp=500
    )
    b_policy = policies_b.create(
        mode="fixed", safe_bp=5000, growth_bp=4000, speculative_bp=1000, tolerance_bp=500
    )
    assert policies_a.for_workspace() is a_policy
    assert policies_b.for_workspace() is b_policy
    assert policies_a.get(b_policy.id) is None
    assert policies_b.get(a_policy.id) is None
    with pytest.raises(NotFound):
        policies_a.update(b_policy.id, tolerance_bp=700)
    with pytest.raises(NotFound):
        policies_a.delete(b_policy.id)
    policies_a.update(a_policy.id, tolerance_bp=700)
    assert a_policy.tolerance_bp == 700
    policies_a.delete(a_policy.id)
    assert policies_a.for_workspace() is None
    assert policies_b.for_workspace() is b_policy


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


@pytest.mark.parametrize("model", OWNED_MODELS, ids=lambda model: model.__name__)
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


def test_audit_events_cannot_be_changed_at_flush(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    ctx = two_workspaces[0]
    repository = AuditEventRepository(db_session, ctx)
    event = repository.add(TENANT_CASES[AuditEvent].factory(db_session, ctx.workspace.id))
    db_session.expire_all()
    loaded = repository.get_or_raise(event.id)
    loaded.action = "test.tampered"
    with pytest.raises(AppendOnlyViolationError, match="cannot be changed"):
        db_session.flush()


def test_audit_events_cannot_be_deleted_at_flush(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    ctx = two_workspaces[0]
    repository = AuditEventRepository(db_session, ctx)
    event = repository.add(TENANT_CASES[AuditEvent].factory(db_session, ctx.workspace.id))
    db_session.expire_all()
    db_session.delete(repository.get_or_raise(event.id))
    with pytest.raises(AppendOnlyViolationError, match="cannot be deleted"):
        db_session.flush()


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
    CurrentWorkspaceRepository(db_session, ctx_a).update(require_two_factor=True)
    assert alice_reader.authorization_in(ctx_a.workspace.id) == ("owner", True)
    assert alice_reader.authorization_in(ctx_b.workspace.id) is None
    assert bob_reader.authorization_in(ctx_a.workspace.id) is None
    assert bob_reader.authorization_in(ctx_b.workspace.id) == ("owner", False)


def _member(session: Session, workspace_id: UUID, email: str, role: str) -> Membership:
    user = User(email=email, display_name=email.split("@")[0])
    session.add(user)
    session.flush()
    membership = Membership(workspace_id=workspace_id, user_id=user.id, role=role)
    session.add(membership)
    session.flush()
    return membership


def test_membership_queries_stay_in_their_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    ctx_a, ctx_b = two_workspaces
    a_owner = _member(db_session, ctx_a.workspace.id, "a-owner@example.com", "owner")
    a_viewer = _member(db_session, ctx_a.workspace.id, "a-viewer@example.com", "viewer")
    b_owner = _member(db_session, ctx_b.workspace.id, "b-owner@example.com", "owner")
    b_owner_two = _member(db_session, ctx_b.workspace.id, "b-owner2@example.com", "owner")
    in_a, in_b = MembershipRepository(db_session, ctx_a), MembershipRepository(db_session, ctx_b)

    assert {m.membership_id for m in in_a.members()} == {a_owner.id, a_viewer.id}
    assert {m.membership_id for m in in_b.members()} == {b_owner.id, b_owner_two.id}
    assert in_a.for_user(a_owner.user_id) is a_owner
    assert in_a.for_user(b_owner.user_id) is None
    assert in_b.for_user(a_viewer.user_id) is None
    assert (in_a.count_owners(), in_b.count_owners()) == (1, 2)

    newcomer = User(email="newcomer@example.com", display_name="Newcomer")
    db_session.add(newcomer)
    db_session.flush()
    added = in_a.add_member(user_id=newcomer.id, role="editor")
    assert added.workspace_id == ctx_a.workspace.id
    assert in_b.for_user(newcomer.id) is None


def test_audit_record_writes_into_its_own_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any], fixed_clock: FixedClock
) -> None:
    ctx_a, ctx_b = two_workspaces
    actor = _member(db_session, ctx_a.workspace.id, "actor@example.com", "owner")
    event = AuditEventRepository(db_session, ctx_a).record(
        actor_user_id=actor.user_id,
        action="test.recorded",
        entity_type="test",
        entity_id=None,
        ts=fixed_clock.now(),
        data={"n": 1},
    )
    stored = raw_row(db_session, AuditEvent, event.id)
    assert stored is not None and stored["workspace_id"] == ctx_a.workspace.id
    assert AuditEventRepository(db_session, ctx_b).get(event.id) is None


def test_audit_pages_stay_in_their_scope(
    db_session: Session, two_workspaces: tuple[Any, Any], fixed_clock: FixedClock
) -> None:
    ctx_a, ctx_b = two_workspaces
    actor = _member(db_session, ctx_a.workspace.id, "actor@example.com", "owner")
    for ctx, action in ((ctx_a, "test.in_a"), (ctx_b, "test.in_b")):
        AuditEventRepository(db_session, ctx).record(
            actor_user_id=actor.user_id,
            action=action,
            entity_type="test",
            entity_id=None,
            ts=fixed_clock.now(),
        )
    db_session.add(
        AuditEvent(
            id=uuid4(),
            workspace_id=None,
            actor_type="system",
            action="test.instance",
            entity_type="test",
            data={},
            ts=fixed_clock.now(),
        )
    )
    db_session.flush()
    everything = AuditFilter()
    assert [
        e.action for e in AuditEventRepository(db_session, ctx_a).page(everything, limit=10)
    ] == ["test.in_a"]
    assert [
        e.action for e in AuditEventRepository(db_session, ctx_b).page(everything, limit=10)
    ] == ["test.in_b"]
    # The instance reader never returns a workspace's events, whatever the filter.
    assert [e.action for e in InstanceAuditReader(db_session).page(everything, limit=10)] == [
        "test.instance"
    ]
    asked_for_a = AuditFilter(actions=("test.in_a", "test.in_b"))
    assert InstanceAuditReader(db_session).page(asked_for_a, limit=10) == []


def test_current_workspace_repository_touches_only_its_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    ctx_a, ctx_b = two_workspaces
    a_repo = CurrentWorkspaceRepository(db_session, ctx_a)
    b_repo = CurrentWorkspaceRepository(db_session, ctx_b)
    assert a_repo.get().id == ctx_a.workspace.id
    assert b_repo.get().id == ctx_b.workspace.id
    before_b = raw_row(db_session, Workspace, ctx_b.workspace.id)

    a_repo.update(name="Renamed A", base_currency="PLN", require_two_factor=True)
    db_session.expire_all()
    after_a = raw_row(db_session, Workspace, ctx_a.workspace.id)
    assert after_a is not None and (after_a["name"], after_a["base_currency"]) == (
        "Renamed A",
        "PLN",
    )
    assert raw_row(db_session, Workspace, ctx_b.workspace.id) == before_b

    assert {
        "name", "base_currency", "country_code", "timezone", "locale", "auto_copy_plan",
        "require_two_factor", "taxes_enabled", "upcoming_days", "investment_cost_basis"
    } == UPDATABLE_FIELDS  # fmt: skip
    for forbidden in ("id", "created_at", "updated_at", "unknown"):
        with pytest.raises(ValueError):
            a_repo.update(**{forbidden: ctx_b.workspace.id})
    db_session.expire_all()
    assert raw_row(db_session, Workspace, ctx_b.workspace.id) == before_b

    # lock (F2.5) reads only its own workspace's row, and refuses a workspace that is gone.
    a_repo.lock(exclusive=False)
    b_repo.lock(exclusive=True)
    gone = CurrentWorkspaceRepository(
        db_session, context_for(uuid4(), FixedClock("2026-01-01T00:00:00+00:00"))
    )
    with pytest.raises(NotFound):
        gone.lock(exclusive=False)
    assert raw_row(db_session, Workspace, ctx_b.workspace.id) == before_b


def test_invitation_transitions_stay_in_their_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any], fixed_clock: FixedClock
) -> None:
    ctx_a, ctx_b = two_workspaces
    now = fixed_clock.now()
    creator = _member(db_session, ctx_a.workspace.id, "creator@example.com", "owner")
    in_a, in_b = InvitationRepository(db_session, ctx_a), InvitationRepository(db_session, ctx_b)

    def make(repository: InvitationRepository) -> Invitation:
        return repository.create(
            token_hash=uuid4().hex * 2,
            role="viewer",
            email=None,
            expires_at=now + timedelta(days=1),
            created_by_user_id=creator.user_id,
        )

    a_invite, b_invite = make(in_a), make(in_b)
    assert a_invite.workspace_id == ctx_a.workspace.id
    assert [i.id for i in in_a.list_pending(now)] == [a_invite.id]
    assert [i.id for i in in_b.list_pending(now)] == [b_invite.id]
    assert (in_a.count_pending(now), in_b.count_pending(now)) == (1, 1)
    before = raw_row(db_session, Invitation, a_invite.id)
    for transition in (
        lambda: in_b.revoke(a_invite.id, now),
        lambda: in_b.mark_used(a_invite.id, user_id=creator.user_id, now=now),
        lambda: in_b.record_failed_email(a_invite.id, now=now, limit=5),
    ):
        with pytest.raises(NotFound):
            transition()
    db_session.expire_all()
    assert raw_row(db_session, Invitation, a_invite.id) == before
    # Positive control: the owning workspace can make each transition.
    assert in_a.record_failed_email(a_invite.id, now=now, limit=2) is False
    assert in_a.record_failed_email(a_invite.id, now=now, limit=2) is True
    assert in_a.count_pending(now) == 0
    in_b.mark_used(b_invite.id, user_id=creator.user_id, now=now)
    with pytest.raises(NotFound):
        in_b.revoke(b_invite.id, now)  # only pending invitations can be revoked


def test_invitation_token_reader_finds_only_the_matching_hash(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    ctx_a, ctx_b = two_workspaces
    a_invite = InvitationRepository(db_session, ctx_a).add(
        TENANT_CASES[Invitation].factory(db_session, ctx_a.workspace.id)
    )
    b_invite = InvitationRepository(db_session, ctx_b).add(
        TENANT_CASES[Invitation].factory(db_session, ctx_b.workspace.id)
    )
    reader = InvitationTokenReader(db_session)
    found = reader.locate(a_invite.token_hash)
    assert found is not None
    assert (found.invitation_id, found.workspace_id) == (a_invite.id, ctx_a.workspace.id)
    found_b = reader.locate(b_invite.token_hash)
    assert found_b is not None and found_b.workspace_id == ctx_b.workspace.id
    assert reader.locate("0" * 64) is None
    assert reader.locate(a_invite.token_hash.upper()) is None


def test_category_queries_stay_in_their_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    ctx_a, ctx_b = two_workspaces
    in_a, in_b = CategoryRepository(db_session, ctx_a), CategoryRepository(db_session, ctx_b)

    def make(repository: CategoryRepository, name: str, kind: str = "expense") -> Any:
        return repository.create(
            name=name,
            name_key=name.casefold(),
            kind=kind,
            treatment="regular",
            color="#16a34a",
            sort_order=0,
        )

    a_food, a_salary = make(in_a, "Food"), make(in_a, "Salary", "income")
    b_food = make(in_b, "Food")  # the same name in another workspace is no clash
    assert (a_food.workspace_id, b_food.workspace_id) == (ctx_a.workspace.id, ctx_b.workspace.id)
    assert [c.id for c in in_a.ordered()] == [a_food.id, a_salary.id]
    assert [c.id for c in in_a.ordered("income")] == [a_salary.id]
    assert [c.id for c in in_b.ordered()] == [b_food.id]
    assert in_b.ordered("income") == []
    assert in_a.active_by_name_key(kind="expense", name_key="food") is a_food
    assert in_b.active_by_name_key(kind="expense", name_key="food") is b_food
    assert in_b.active_by_name_key(kind="income", name_key="salary") is None
    assert (in_a.count(), in_b.count()) == (2, 1)
    assert (in_a.next_sort_order("expense"), in_b.next_sort_order("expense")) == (1, 1)
    assert (in_a.next_sort_order("income"), in_b.next_sort_order("income")) == (1, 0)
    source_id = UUID("f0e1d2c3-b4a5-4968-8776-655443322110")
    in_a.update(a_food.id, import_key=f"id:{source_id}")
    assert in_a.by_lineage_ids([source_id]) == [a_food]
    assert in_b.by_lineage_ids([source_id]) == []
    assert in_b.by_lineage_ids([a_food.id]) == []


def test_investment_queries_stay_in_their_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    ctx_a, ctx_b = two_workspaces
    classes_a = AssetClassRepository(db_session, ctx_a)
    classes_b = AssetClassRepository(db_session, ctx_b)
    a_stocks = classes_a.create(
        name="Stocks",
        name_key="stocks",
        investable=True,
        color="#2563eb",
        sort_order=0,
    )
    a_bonds = classes_a.create(
        name="Bonds",
        name_key="bonds",
        investable=True,
        color="#16a34a",
        sort_order=1,
    )
    b_stocks = classes_b.create(
        name="Stocks",
        name_key="stocks",
        investable=True,
        color="#2563eb",
        sort_order=0,
    )
    assert [row.id for row in classes_a.ordered()] == [a_stocks.id, a_bonds.id]
    assert [row.id for row in classes_b.ordered()] == [b_stocks.id]
    assert classes_a.active_by_name_key("stocks") is a_stocks
    assert classes_b.active_by_name_key("stocks") is b_stocks
    assert classes_b.active_by_name_key("bonds") is None
    assert (classes_a.active_count(), classes_b.active_count()) == (2, 1)
    assert (classes_a.next_sort_order(), classes_b.next_sort_order()) == (2, 1)

    instruments_a = InstrumentRepository(db_session, ctx_a)
    instruments_b = InstrumentRepository(db_session, ctx_b)
    a_fund = instruments_a.create(
        name="World ETF",
        name_key="world etf",
        symbol="VWCE",
        type="etf",
        quote_currency="EUR",
        unit_label="share",
        valuation_source="manual",
    )
    b_fund = instruments_b.create(
        name="World ETF",
        name_key="world etf",
        symbol="VWCE",
        type="etf",
        quote_currency="EUR",
        unit_label="share",
        valuation_source="manual",
    )
    assert [row.id for row in instruments_a.ordered()] == [a_fund.id]
    assert [row.id for row in instruments_b.ordered()] == [b_fund.id]
    assert instruments_a.active_by_name_key("world etf") is a_fund
    assert instruments_b.active_by_name_key("world etf") is b_fund
    assert instruments_a.get_for_update(a_fund.id) is a_fund
    assert instruments_b.get_for_update(a_fund.id) is None

    allocations_a = InstrumentAllocationRepository(db_session, ctx_a)
    allocations_b = InstrumentAllocationRepository(db_session, ctx_b)
    a_allocation = allocations_a.create(
        instrument_id=a_fund.id,
        asset_class_id=a_stocks.id,
        weight_bp=8_000,
    )
    a_bonds_allocation = allocations_a.create(
        instrument_id=a_fund.id,
        asset_class_id=a_bonds.id,
        weight_bp=2_000,
    )
    b_allocation = allocations_b.create(
        instrument_id=b_fund.id,
        asset_class_id=b_stocks.id,
        weight_bp=10_000,
    )
    assert [row.id for row in allocations_a.for_instrument(a_fund.id)] == [
        a_allocation.id,
        a_bonds_allocation.id,
    ]
    assert [row.id for row in allocations_b.for_instrument(b_fund.id)] == [b_allocation.id]
    assert allocations_b.for_instrument(a_fund.id) == []

    transactions_a = InvestmentTransactionRepository(db_session, ctx_a)
    transactions_b = InvestmentTransactionRepository(db_session, ctx_b)
    a_transaction = transactions_a.create(
        instrument_id=a_fund.id,
        date=date(2026, 3, 1),
        type="buy",
        quantity=Decimal("1"),
        unit_price=Decimal("100"),
        quote_currency="EUR",
        fee_minor=0,
        fx_to_base=Decimal("1"),
    )
    b_transaction = transactions_b.create(
        instrument_id=b_fund.id,
        date=date(2026, 3, 1),
        type="buy",
        quantity=Decimal("2"),
        unit_price=Decimal("50"),
        quote_currency="EUR",
        fee_minor=0,
        fx_to_base=Decimal("1"),
    )
    assert [row.id for row in transactions_a.for_instrument(a_fund.id)] == [a_transaction.id]
    assert transactions_b.for_instrument(a_fund.id) == []
    assert [row.id for row in transactions_a.live()] == [a_transaction.id]
    assert [row.id for row in transactions_b.live()] == [b_transaction.id]
    assert [row.id for row in transactions_a.live_for_instruments((a_fund.id,))] == [
        a_transaction.id
    ]
    assert transactions_b.live_for_instruments((a_fund.id,)) == []
    assert [row.id for row in transactions_a.live_dated(date(2026, 3, 1), (a_fund.id,))] == [
        a_transaction.id
    ]
    assert transactions_b.live_dated(date(2026, 3, 1), (a_fund.id,)) == []
    assert transactions_a.any_for_instrument(a_fund.id)
    assert not transactions_b.any_for_instrument(a_fund.id)
    assert transactions_a.instrument_ids() == {a_fund.id}
    assert transactions_b.instrument_ids() == {b_fund.id}
    assert transactions_a.get_for_update(a_transaction.id) is a_transaction
    assert transactions_b.get_for_update(a_transaction.id) is None
    assert [
        row.id
        for row in transactions_a.filtered(
            instrument_id=a_fund.id,
            types=("buy",),
            date_from=None,
            date_to=None,
            limit=10,
            offset=0,
        )
    ] == [a_transaction.id]
    assert (
        transactions_b.filtered(
            instrument_id=a_fund.id,
            types=("buy",),
            date_from=None,
            date_to=None,
            limit=10,
            offset=0,
        )
        == []
    )
    assert (
        transactions_a.count_filtered(
            instrument_id=a_fund.id, types=("buy",), date_from=None, date_to=None
        )
        == 1
    )
    assert (
        transactions_b.count_filtered(
            instrument_id=a_fund.id, types=("buy",), date_from=None, date_to=None
        )
        == 0
    )
    # F6.2a: the "FX needs checking" filter reads rows quoted outside a currency.
    assert [
        row.id
        for row in transactions_a.filtered_not_in_currency(
            "PLN", instrument_id=None, types=(), date_from=None, date_to=None
        )
    ] == [a_transaction.id]
    assert [
        row.id
        for row in transactions_b.filtered_not_in_currency(
            "PLN", instrument_id=a_fund.id, types=(), date_from=None, date_to=None
        )
    ] == []

    a_income_category = CategoryRepository(db_session, ctx_a).create(
        name="Interest",
        name_key="interest",
        kind="income",
        treatment="regular",
        color="#16a34a",
        sort_order=0,
    )
    b_income_category = CategoryRepository(db_session, ctx_b).create(
        name="Interest",
        name_key="interest",
        kind="income",
        treatment="regular",
        color="#16a34a",
        sort_order=0,
    )
    a_income = TransactionRepository(db_session, ctx_a).create(
        direction="income",
        day=date(2026, 3, 1),
        amount_minor=100,
        currency="EUR",
        category_id=a_income_category.id,
        description="Dividend",
        description_key="dividend",
        source="investment",
        created_by_user_id=None,
    )
    b_income = TransactionRepository(db_session, ctx_b).create(
        direction="income",
        day=date(2026, 3, 1),
        amount_minor=200,
        currency="EUR",
        category_id=b_income_category.id,
        description="Dividend",
        description_key="dividend",
        source="investment",
        created_by_user_id=None,
    )
    transactions_a.update(a_transaction.id, income_transaction_id=a_income.id)
    transactions_b.update(b_transaction.id, income_transaction_id=b_income.id)
    assert transactions_a.for_income_transaction(a_income.id) is a_transaction
    assert transactions_a.for_income_transaction(b_income.id) is None
    assert transactions_b.for_income_transaction(b_income.id) is b_transaction
    assert transactions_b.for_income_transaction(a_income.id) is None
    assert transactions_a.income_links([a_income.id, b_income.id]) == {
        a_income.id: a_transaction.id
    }
    assert transactions_b.income_links([a_income.id, b_income.id]) == {
        b_income.id: b_transaction.id
    }


def test_risk_policy_queries(db_session: Session, two_workspaces: tuple[Any, Any]) -> None:
    ctx_a, ctx_b = two_workspaces
    policies_a = RiskTargetPolicyRepository(db_session, ctx_a)
    policies_b = RiskTargetPolicyRepository(db_session, ctx_b)
    a_policy = policies_a.create(
        mode="fixed", safe_bp=5000, growth_bp=4000, speculative_bp=1000, tolerance_bp=500
    )
    b_policy = policies_b.create(
        mode="age_scaled", safe_bp=5000, growth_bp=4000, speculative_bp=1000, tolerance_bp=500
    )
    assert policies_a.for_workspace() is a_policy
    assert policies_b.for_workspace() is b_policy
    assert policies_a.for_workspace(for_update=True) is a_policy
    assert policies_b.for_workspace(for_update=True) is b_policy
    assert policies_a.get(b_policy.id) is None
    assert policies_b.get(a_policy.id) is None


def test_price_queries_stay_in_their_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    ctx_a, ctx_b = two_workspaces
    instruments_a = InstrumentRepository(db_session, ctx_a)
    instruments_b = InstrumentRepository(db_session, ctx_b)
    a_fund = instruments_a.create(
        name="World ETF",
        name_key="world etf",
        symbol="VWCE",
        type="etf",
        quote_currency="EUR",
        unit_label="share",
        valuation_source="manual",
    )
    b_fund = instruments_b.create(
        name="World ETF",
        name_key="world etf",
        symbol="VWCE",
        type="etf",
        quote_currency="EUR",
        unit_label="share",
        valuation_source="manual",
    )

    prices_a = PricePointRepository(db_session, ctx_a)
    prices_b = PricePointRepository(db_session, ctx_b)
    a_price = prices_a.create(
        instrument_id=a_fund.id,
        date=date(2026, 3, 1),
        price=Decimal("100"),
        source="manual",
    )
    b_price = prices_b.create(
        instrument_id=b_fund.id,
        date=date(2026, 3, 1),
        price=Decimal("50"),
        source="manual",
    )
    assert prices_a.manual_on(a_fund.id, date(2026, 3, 1)) is a_price
    assert prices_b.manual_on(a_fund.id, date(2026, 3, 1)) is None
    assert prices_a.manual_on(b_fund.id, date(2026, 3, 1)) is None
    assert [
        row.id for row in prices_a.for_instruments_on_or_before((a_fund.id,), date(2026, 3, 1))
    ] == [a_price.id]
    assert prices_b.for_instruments_on_or_before((a_fund.id,), date(2026, 3, 1)) == []
    assert [
        row.id for row in prices_b.for_instruments_on_or_before((b_fund.id,), date(2026, 3, 1))
    ] == [b_price.id]

    rates_a = FxRateRepository(db_session, ctx_a)
    rates_b = FxRateRepository(db_session, ctx_b)
    a_rate = rates_a.create(
        base_currency="EUR",
        quote_currency="USD",
        date=date(2026, 3, 1),
        rate=Decimal("0.92"),
        source="nbp",
    )
    b_rate = rates_b.create(
        base_currency="EUR",
        quote_currency="USD",
        date=date(2026, 3, 1),
        rate=Decimal("0.93"),
        source="nbp",
    )
    assert [row.id for row in rates_a.for_base_on_or_before("EUR", date(2026, 3, 1))] == [a_rate.id]
    assert [row.id for row in rates_b.for_base_on_or_before("EUR", date(2026, 3, 1))] == [b_rate.id]


def test_bond_terms_queries_stay_in_their_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    ctx_a, ctx_b = two_workspaces
    instruments_a = InstrumentRepository(db_session, ctx_a)
    instruments_b = InstrumentRepository(db_session, ctx_b)
    a_bond = instruments_a.create(
        name="Treasury bonds",
        name_key="treasury bonds",
        symbol=None,
        type="bond_model",
        quote_currency="PLN",
        unit_label="bond",
        valuation_source="bond_model",
    )
    b_bond = instruments_b.create(
        name="Treasury bonds",
        name_key="treasury bonds",
        symbol=None,
        type="bond_model",
        quote_currency="PLN",
        unit_label="bond",
        valuation_source="bond_model",
    )
    terms_a = BondTermsRepository(db_session, ctx_a)
    terms_b = BondTermsRepository(db_session, ctx_b)
    values = {
        "series_code": "OTS",
        "face_value_minor": 10_000,
        "maturity_date": date(2027, 1, 1),
        "interest_mode": "capitalized_annually",
        "rate_schedule": [{"from_date": "2026-01-01", "annual_rate_bp": 500}],
        "tax_rate_bp": 1_900,
    }
    a_terms = terms_a.create(instrument_id=a_bond.id, **values)
    b_terms = terms_b.create(instrument_id=b_bond.id, **values)
    assert terms_a.for_instrument(a_bond.id) is a_terms
    assert terms_a.for_instrument(b_bond.id) is None
    assert terms_b.for_instrument(a_bond.id) is None
    assert [row.id for row in terms_a.for_instruments((a_bond.id, b_bond.id))] == [a_terms.id]
    assert [row.id for row in terms_b.for_instruments((a_bond.id, b_bond.id))] == [b_terms.id]
    assert terms_a.instrument_ids() == {a_bond.id}
    assert terms_b.instrument_ids() == {b_bond.id}


def test_price_source_queries_stay_in_their_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    ctx_a, ctx_b = two_workspaces
    instruments_a = InstrumentRepository(db_session, ctx_a)
    instruments_b = InstrumentRepository(db_session, ctx_b)
    a_fund = instruments_a.create(
        name="Gold",
        name_key="gold",
        symbol="XAU",
        type="metal",
        quote_currency="PLN",
        unit_label="oz",
        valuation_source="provider",
        provider_key="nbp",
        provider_symbol="XAU",
    )
    b_fund = instruments_b.create(
        name="Gold",
        name_key="gold",
        symbol="XAU",
        type="metal",
        quote_currency="PLN",
        unit_label="oz",
        valuation_source="provider",
        provider_key="nbp",
        provider_symbol="XAU",
    )
    assert [row.id for row in instruments_a.provider_instruments("nbp")] == [a_fund.id]
    assert [row.id for row in instruments_b.provider_instruments("nbp")] == [b_fund.id]

    configs_a = PriceSourceConfigRepository(db_session, ctx_a)
    configs_b = PriceSourceConfigRepository(db_session, ctx_b)
    a_config = configs_a.create(provider_key="nbp", enabled=True, settings_encrypted=None)
    b_config = configs_b.create(provider_key="nbp", enabled=False, settings_encrypted=None)
    assert configs_a.for_provider("nbp") is a_config
    assert configs_b.for_provider("nbp") is b_config
    assert [row.id for row in configs_a.enabled()] == [a_config.id]
    assert configs_b.enabled() == []

    now = datetime(2026, 3, 1, 12, tzinfo=UTC)
    errors_a = ProviderErrorRepository(db_session, ctx_a)
    errors_b = ProviderErrorRepository(db_session, ctx_b)
    old = errors_a.create(
        provider_key="nbp",
        instrument_id=a_fund.id,
        scope="latest",
        code="http_500",
        occurred_at=now - timedelta(days=100),
        attempts=1,
    )
    new = errors_a.create(
        provider_key="nbp",
        instrument_id=a_fund.id,
        scope="history",
        code="timeout",
        occurred_at=now,
        attempts=2,
    )
    theirs = errors_b.create(
        provider_key="nbp",
        instrument_id=b_fund.id,
        scope="latest",
        code="http_500",
        occurred_at=now,
        attempts=1,
    )
    assert errors_a.latest_for_provider("nbp") is new
    assert errors_a.latest_for_instrument("nbp", a_fund.id) is new
    assert errors_b.latest_for_provider("nbp") is theirs
    assert errors_b.latest_for_instrument("nbp", a_fund.id) is None
    assert errors_a.prune(now - timedelta(days=90)) == 1
    assert errors_a.get(old.id) is None

    prices_a = PricePointRepository(db_session, ctx_a)
    prices_b = PricePointRepository(db_session, ctx_b)
    a_price = prices_a.create(
        instrument_id=a_fund.id,
        date=date(2026, 3, 1),
        price=Decimal("100"),
        source="provider",
        provider_key="nbp",
    )
    prices_b.create(
        instrument_id=b_fund.id,
        date=date(2026, 3, 1),
        price=Decimal("50"),
        source="provider",
        provider_key="nbp",
    )
    assert prices_a.provider_on(a_fund.id, date(2026, 3, 1)) is a_price
    assert prices_a.provider_on(b_fund.id, date(2026, 3, 1)) is None
    assert prices_a.latest_provider_for_instrument(a_fund.id, "nbp") is a_price
    assert prices_b.latest_provider_for_instrument(a_fund.id, "nbp") is None
    assert prices_a.any_provider_for_instrument(a_fund.id, "nbp")
    assert not prices_b.any_provider_for_instrument(a_fund.id, "nbp")

    rates_a = FxRateRepository(db_session, ctx_a)
    rates_b = FxRateRepository(db_session, ctx_b)
    a_rate = rates_a.create(
        base_currency="PLN",
        quote_currency="EUR",
        date=date(2026, 3, 1),
        rate=Decimal("4.30"),
        source="nbp",
    )
    rates_b.create(
        base_currency="PLN",
        quote_currency="EUR",
        date=date(2026, 3, 1),
        rate=Decimal("4.31"),
        source="nbp",
    )
    assert rates_a.rate_on("PLN", "EUR", date(2026, 3, 1), "nbp") is a_rate
    assert rates_b.rate_on("PLN", "EUR", date(2026, 3, 1), "nbp") is not a_rate
    assert [
        row.id for row in rates_a.for_base_source_on_or_before("PLN", "nbp", date(2026, 3, 1))
    ] == [a_rate.id]
    assert len(rates_b.for_base_source_on_or_before("PLN", "nbp", date(2026, 3, 1))) == 1


def test_networth_queries_stay_in_their_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    ctx_a, ctx_b = two_workspaces
    categories_a = NetWorthCategoryRepository(db_session, ctx_a)
    categories_b = NetWorthCategoryRepository(db_session, ctx_b)

    def category(repository: NetWorthCategoryRepository, name: str, side: str = "asset") -> Any:
        return repository.create(
            name=name,
            name_key=name.casefold(),
            side=side,
            color="#16a34a",
            sort_order=0,
        )

    a_vehicle = category(categories_a, "Vehicles")
    a_mortgage = category(categories_a, "Mortgage", "liability")
    b_vehicle = category(categories_b, "Vehicles")
    assert [row.id for row in categories_a.ordered()] == [a_vehicle.id, a_mortgage.id]
    assert [row.id for row in categories_a.ordered("liability")] == [a_mortgage.id]
    assert [row.id for row in categories_b.ordered()] == [b_vehicle.id]
    assert categories_a.active_by_name_key(side="asset", name_key="vehicles") is a_vehicle
    assert categories_b.active_by_name_key(side="asset", name_key="vehicles") is b_vehicle
    assert categories_b.active_by_name_key(side="liability", name_key="mortgage") is None
    assert (categories_a.count(), categories_b.count()) == (2, 1)
    assert (categories_a.next_sort_order("asset"), categories_b.next_sort_order("asset")) == (
        1,
        1,
    )

    items_a = NetWorthItemRepository(db_session, ctx_a)
    items_b = NetWorthItemRepository(db_session, ctx_b)
    a_item = items_a.create(
        side="asset",
        category_id=a_vehicle.id,
        name="Car",
        currency="EUR",
        notes="",
        acquired_on=None,
        valuation_mode="manual",
        link_type=None,
        link_id=None,
        include_in_net_worth=True,
        investable=False,
        estimate_between=True,
    )
    b_item = items_b.create(
        side="asset",
        category_id=b_vehicle.id,
        name="Car",
        currency="EUR",
        notes="",
        acquired_on=None,
        valuation_mode="manual",
        link_type=None,
        link_id=None,
        include_in_net_worth=True,
        investable=False,
        estimate_between=True,
    )
    assert [row.id for row in items_a.ordered()] == [a_item.id]
    assert [row.id for row in items_b.ordered()] == [b_item.id]
    assert [row.id for row in items_a.by_ids([a_item.id, b_item.id])] == [a_item.id]
    assert [row.id for row in items_b.by_ids([a_item.id, b_item.id])] == [b_item.id]
    assert items_a.by_ids([]) == []
    assert items_a.counts_by_category() == {a_vehicle.id: 1}
    assert items_b.counts_by_category() == {b_vehicle.id: 1}
    link_id = uuid4()
    linked_a = items_a.create(
        side="asset",
        category_id=a_vehicle.id,
        name="Linked",
        currency="EUR",
        valuation_mode="linked",
        link_type="portfolio_asset_class",
        link_id=link_id,
        estimate_between=True,
    )
    linked_b = items_b.create(
        side="asset",
        category_id=b_vehicle.id,
        name="Linked",
        currency="USD",
        valuation_mode="linked",
        link_type="portfolio_asset_class",
        link_id=link_id,
        estimate_between=True,
    )
    assert items_a.active_linked("portfolio_asset_class", link_id) is linked_a
    assert items_b.active_linked("portfolio_asset_class", link_id) is linked_b
    assert items_b.active_linked("portfolio_asset_class", uuid4()) is None

    valuations_a = ItemValuationRepository(db_session, ctx_a)
    valuations_b = ItemValuationRepository(db_session, ctx_b)
    a_value = valuations_a.create(
        item_id=a_item.id,
        as_of=date(2026, 10, 7),
        value_minor=125000,
        currency="EUR",
        note="",
    )
    b_value = valuations_b.create(
        item_id=b_item.id,
        as_of=date(2026, 10, 7),
        value_minor=99000,
        currency="EUR",
        note="",
    )
    assert [row.id for row in valuations_a.for_item(a_item.id)] == [a_value.id]
    assert valuations_b.for_item(a_item.id) == []
    assert [row.id for row in valuations_b.for_item(b_item.id)] == [b_value.id]
    assert [row.id for row in valuations_a.for_items([a_item.id, b_item.id])] == [a_value.id]
    assert [row.id for row in valuations_b.for_items([a_item.id, b_item.id])] == [b_value.id]
    assert valuations_a.earliest_as_of() == date(2026, 10, 7)
    assert valuations_b.earliest_as_of() == date(2026, 10, 7)

    snapshots_a = NetWorthSnapshotRepository(db_session, ctx_a)
    snapshots_b = NetWorthSnapshotRepository(db_session, ctx_b)

    def snapshot(repository: NetWorthSnapshotRepository, assets: int, source: str) -> Any:
        return repository.create(
            as_of=date(2026, 10, 31),
            assets_minor=assets,
            liabilities_minor=1,
            currency="EUR",
            breakdown={"v": 1, "categories": []},
            source=source,
        )

    a_snapshot = snapshot(snapshots_a, 125000, "manual")
    b_snapshot = snapshot(snapshots_b, 99000, "manual")
    assert [row.id for row in snapshots_a.for_date(date(2026, 10, 31))] == [a_snapshot.id]
    assert [row.id for row in snapshots_b.for_date(date(2026, 10, 31))] == [b_snapshot.id]
    assert [row.id for row in snapshots_a.for_range(None)] == [a_snapshot.id]
    assert [row.id for row in snapshots_b.for_range(None)] == [b_snapshot.id]
    assert snapshots_a.for_range(date(2026, 11, 1)) == []
    assert snapshots_b.for_range(date(2026, 11, 1)) == []

    def batch(repository: ImportBatchRepository, name: str) -> Any:
        return repository.create(
            source="item_valuations_csv",
            mode="merge",
            filename=name,
            file_sha256="a" * 64,
            size_bytes=1,
            options={},
            status="uploaded",
            started_by_user_id=None,
        )

    batch_a = batch(ImportBatchRepository(db_session, ctx_a), "a.csv")
    batch_b = batch(ImportBatchRepository(db_session, ctx_b), "b.csv")
    imported_category_a = category(categories_a, "Imported")
    imported_category_a.import_batch_id = batch_a.id
    db_session.flush()
    assert categories_a.for_import_batch(batch_a.id) == [imported_category_a]
    assert categories_b.for_import_batch(batch_a.id) == []
    lineage = uuid4()
    imported_item_a = items_a.create(
        side="asset",
        category_id=a_vehicle.id,
        name="Imported",
        currency="EUR",
        import_batch_id=batch_a.id,
        import_key=f"id:{lineage}",
        estimate_between=True,
    )
    assert items_a.for_import_batch(batch_a.id) == [imported_item_a]
    assert items_b.for_import_batch(batch_a.id) == []
    assert items_a.by_lineage_ids([lineage]) == [imported_item_a]
    assert items_a.by_lineage_ids([imported_item_a.id]) == [imported_item_a]
    assert items_b.by_lineage_ids([lineage, imported_item_a.id]) == []

    imported_value_a = valuations_a.create(
        item_id=a_item.id,
        as_of=date(2026, 11, 1),
        value_minor=100,
        currency="EUR",
        import_batch_id=batch_a.id,
    )
    valuations_b.create(
        item_id=b_item.id,
        as_of=date(2026, 11, 1),
        value_minor=100,
        currency="EUR",
        import_batch_id=batch_b.id,
    )
    assert valuations_a.for_import_batch(batch_a.id) == [imported_value_a]
    assert valuations_b.for_import_batch(batch_a.id) == []
    assert valuations_a.delete_for_import_batch(batch_b.id) == 0
    assert valuations_b.delete_for_import_batch(batch_b.id) == 1
    assert valuations_a.delete_for_import_batch(batch_a.id) == 1
    assert valuations_a.get(imported_value_a.id) is None

    imported_snapshot_a = snapshots_a.create(
        as_of=date(2026, 11, 30),
        assets_minor=100,
        liabilities_minor=0,
        currency="EUR",
        breakdown={"v": 1, "categories": []},
        source="auto",
        import_batch_id=batch_a.id,
    )
    assert snapshots_a.for_import_batch(batch_a.id) == [imported_snapshot_a]
    assert snapshots_b.for_import_batch(batch_a.id) == []


def test_month_state_queries_stay_in_their_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    ctx_a, ctx_b = two_workspaces
    in_a, in_b = MonthStateRepository(db_session, ctx_a), MonthStateRepository(db_session, ctx_b)
    october, november = YearMonth(2026, 10), YearMonth(2026, 11)

    a_october = in_a.create(october)
    assert (a_october.workspace_id, a_october.month) == (ctx_a.workspace.id, date(2026, 10, 1))
    assert in_a.for_month(october) is a_october
    assert in_b.for_month(october) is None  # another workspace's month is not seen
    assert in_a.for_month(november) is None
    b_october = in_b.create(october)  # the same month in another workspace is no clash
    assert b_october.workspace_id == ctx_b.workspace.id
    assert in_b.for_month(october) is b_october
    assert in_a.for_month(october) is a_october
    restored_b = in_b.create_restored(
        month=november,
        closed_at=None,
        closed_by_user_id=None,
        plan_started_at=None,
        created_at=datetime(2026, 11, 1, 12, tzinfo=UTC),
        updated_at=datetime(2026, 11, 1, 12, tzinfo=UTC),
    )
    assert restored_b.workspace_id == ctx_b.workspace.id
    assert in_a.for_month(november) is None
    in_a.update(a_october.id, closed_at=datetime(2026, 10, 31, 12, tzinfo=UTC))
    assert in_a.closed_months() == {october}
    assert in_b.closed_months() == frozenset()  # B's October is open; A's close is not seen
    assert in_b.delete_for_months([october]) == 1  # B's own October, not A's
    assert in_a.for_month(october) is a_october
    assert in_a.delete_for_months([november]) == 0
    assert in_b.delete_for_months([november]) == 1


def test_transaction_queries_stay_in_their_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    ctx_a, ctx_b = two_workspaces
    in_a, in_b = TransactionRepository(db_session, ctx_a), TransactionRepository(db_session, ctx_b)
    october = YearMonth(2026, 10)
    assert (in_a.any_exists(), in_b.any_exists()) == (False, False)

    def category(ctx: Any, name: str) -> Any:
        return CategoryRepository(db_session, ctx).create(
            name=name, name_key=name.casefold(), kind="expense", treatment="regular",
            color="#16a34a", sort_order=0,
        )  # fmt: skip

    def make(repository: TransactionRepository, category_id: UUID, description: str) -> Any:
        return repository.create(
            direction="expense", day=date(2026, 10, 7), amount_minor=1250, currency="EUR",
            category_id=category_id, description=description,
            description_key=description.casefold(), source="manual", created_by_user_id=None,
        )  # fmt: skip

    food_a, food_b = category(ctx_a, "Food"), category(ctx_b, "Food")
    a_row = make(in_a, food_a.id, "Lidl")
    assert a_row.workspace_id == ctx_a.workspace.id
    assert (in_a.any_exists(), in_b.any_exists()) == (True, False)
    assert [r.id for r in in_a.for_month(october)] == [a_row.id]
    assert in_b.for_month(october) == []
    assert in_a.get_for_update(a_row.id) is a_row
    assert in_b.get_for_update(a_row.id) is None  # another workspace's row is not found
    assert set(in_a.counts_by_category()) == {food_a.id}
    assert in_b.counts_by_category() == {}
    assert in_a.active_months_by_category() == {food_a.id: frozenset({october})}
    assert in_b.active_months_by_category() == {}
    assert in_a.description_keys(direction="expense", key_prefix="li", limit=10) == ["lidl"]
    assert in_b.description_keys(direction="expense", key_prefix="li", limit=10) == []
    assert [m.description for m in in_a.description_details(
        direction="expense", keys=["lidl"])] == ["Lidl"]  # fmt: skip
    assert in_b.description_details(direction="expense", keys=["lidl"]) == []
    b_row = make(in_b, food_b.id, "Lidl")
    assert b_row.workspace_id == ctx_b.workspace.id
    assert [r.id for r in in_b.for_month(october)] == [b_row.id]
    assert [r.id for r in in_a.for_month(october)] == [a_row.id]


def test_import_batch_queries_stay_in_their_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    ctx_a, ctx_b = two_workspaces
    in_a, in_b = ImportBatchRepository(db_session, ctx_a), ImportBatchRepository(db_session, ctx_b)

    def create(repository: ImportBatchRepository) -> Any:
        return repository.create(
            source="archive",
            mode="merge",
            filename="backup.zip",
            file_sha256="a" * 64,
            size_bytes=1,
            options={},
            status="uploaded",
            started_by_user_id=None,
        )

    batch_a = create(in_a)
    assert batch_a.workspace_id == ctx_a.workspace.id
    assert in_a.recent() == [batch_a]
    assert in_b.recent() == []
    assert in_a.get_for_update(batch_a.id) is batch_a
    assert in_b.get_for_update(batch_a.id) is None
    assert in_a.expired_before(datetime.max.replace(tzinfo=UTC)) == [batch_a]
    assert in_b.expired_before(datetime.max.replace(tzinfo=UTC)) == []
    assert in_a.awaiting_apply() == [batch_a]
    assert in_b.awaiting_apply() == []
    batch_b = create(in_b)
    assert batch_b.workspace_id == ctx_b.workspace.id
    assert in_a.recent() == [batch_a]
    assert in_b.recent() == [batch_b]


def test_import_category_mapping_queries_stay_in_their_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    ctx_a, ctx_b = two_workspaces
    in_a = ImportCategoryMappingRepository(db_session, ctx_a)
    in_b = ImportCategoryMappingRepository(db_session, ctx_b)
    category_a = CategoryRepository(db_session, ctx_a).create(
        name="Food", name_key="food", kind="expense", treatment="regular",
        color="#16a34a", sort_order=0,
    )  # fmt: skip
    category_b = CategoryRepository(db_session, ctx_b).create(
        name="Food", name_key="food", kind="expense", treatment="regular",
        color="#16a34a", sort_order=0,
    )  # fmt: skip
    mapping_a = in_a.create(
        kind="expense", source_name_key="food", action="map", category_id=category_a.id
    )
    mapping_b = in_b.create(
        kind="expense", source_name_key="food", action="map", category_id=category_b.id
    )

    assert mapping_a.workspace_id == ctx_a.workspace.id
    assert mapping_b.workspace_id == ctx_b.workspace.id
    assert in_a.for_kind("expense") == [mapping_a]
    assert in_b.for_kind("expense") == [mapping_b]
    assert in_a.get_for_source(kind="expense", source_name_key="food") is mapping_a
    assert in_b.get_for_source(kind="expense", source_name_key="food") is mapping_b
    assert in_b.get_for_source(kind="expense", source_name_key="other") is None

    # Deleting mappings for a created category never crosses the workspace boundary.
    assert in_a.delete_for_categories([category_b.id]) == 0
    assert in_a.get_for_source(kind="expense", source_name_key="food") is mapping_a
    assert in_a.delete_for_categories([category_a.id]) == 1
    assert in_a.get_for_source(kind="expense", source_name_key="food") is None
    assert in_b.get_for_source(kind="expense", source_name_key="food") is mapping_b
    assert mapping_b.action == "map"

    # Upsert changes only the owning workspace's row.
    updated = in_a.upsert(kind="expense", source_name_key="food", action="ignore", category_id=None)
    assert updated.action == "ignore"
    assert in_b.get_for_source(kind="expense", source_name_key="food") is mapping_b
    assert mapping_b.action == "map"


def test_imported_ledger_rows_and_dedupe_lookups_stay_in_their_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    from assetboard.db.repositories import BudgetLineRepository

    ctx_a, ctx_b = two_workspaces
    batches = [ImportBatchRepository(db_session, ctx) for ctx in (ctx_a, ctx_b)]
    batch_a = batches[0].create(
        source="transactions_csv",
        mode="merge",
        filename="a.csv",
        file_sha256="a" * 64,
        size_bytes=1,
        options={},
        status="uploaded",
        started_by_user_id=None,
    )
    batch_b = batches[1].create(
        source="transactions_csv",
        mode="merge",
        filename="b.csv",
        file_sha256="b" * 64,
        size_bytes=1,
        options={},
        status="uploaded",
        started_by_user_id=None,
    )
    category_a = CategoryRepository(db_session, ctx_a).create(
        name="Imported", name_key="imported", kind="expense", treatment="regular",
        color="#16a34a", sort_order=0, import_batch_id=batch_a.id,
    )  # fmt: skip
    category_b = CategoryRepository(db_session, ctx_b).create(
        name="Imported", name_key="imported", kind="expense", treatment="regular",
        color="#16a34a", sort_order=0, import_batch_id=batch_b.id,
    )  # fmt: skip
    source_id = uuid4()
    transaction_a = TransactionRepository(db_session, ctx_a).create_imported(
        direction="expense",
        day=date(2026, 10, 7),
        amount_minor=1250,
        currency="EUR",
        category_id=category_a.id,
        description="Coffee",
        description_key="coffee",
        import_batch_id=batch_a.id,
        import_key=f"id:{source_id}",
        created_by_user_id=None,
    )
    lines_a = BudgetLineRepository(db_session, ctx_a)
    line_a = lines_a.create(
        month=YearMonth(2026, 10),
        category_id=category_a.id,
        planned_minor=5000,
        currency="EUR",
        import_batch_id=batch_a.id,
    )
    transactions_a = TransactionRepository(db_session, ctx_a)
    transactions_b = TransactionRepository(db_session, ctx_b)

    restored_a = transactions_a.create_restored(
        direction="expense",
        day=date(2026, 10, 8),
        amount_minor=500,
        currency="EUR",
        category_id=category_a.id,
        description="Restored",
        description_key="restored",
        import_batch_id=batch_a.id,
        import_key=f"id:{uuid4()}",
        created_by_user_id=None,
        created_at=datetime(2026, 10, 8, 12, tzinfo=UTC),
        deleted_at=None,
    )
    assert restored_a.workspace_id == ctx_a.workspace.id
    assert transactions_b.get(restored_a.id) is None

    assert transaction_a.source == "import"
    assert set(transactions_a.for_import_batch(batch_a.id)) == {transaction_a, restored_a}
    assert transactions_b.for_import_batch(batch_a.id) == []
    assert transactions_a.by_import_keys([f"id:{source_id}"]) == [transaction_a]
    assert transactions_b.by_import_keys([f"id:{source_id}"]) == []
    assert transactions_a.by_lineage_ids([source_id]) == [transaction_a]
    assert transactions_b.by_lineage_ids([source_id]) == []
    assert transactions_b.by_lineage_ids([transaction_a.id]) == []
    assert lines_a.for_import_batch(batch_a.id) == [line_a]
    assert BudgetLineRepository(db_session, ctx_b).for_import_batch(batch_a.id) == []
    assert CategoryRepository(db_session, ctx_a).for_import_batch(batch_a.id) == [category_a]
    assert CategoryRepository(db_session, ctx_b).for_import_batch(batch_a.id) == []
    assert CategoryRepository(db_session, ctx_b).for_import_batch(batch_b.id) == [category_b]
    assert transactions_b.delete_for_import_batch(batch_a.id) == 0
    assert transactions_a.delete_for_import_batch(batch_a.id) == 2
    assert transactions_a.for_import_batch(batch_a.id) == []


def test_transaction_search_stays_in_its_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    ctx_a, ctx_b = two_workspaces
    in_a, in_b = TransactionRepository(db_session, ctx_a), TransactionRepository(db_session, ctx_b)

    def category(ctx: Any) -> Any:
        return CategoryRepository(db_session, ctx).create(
            name="Food", name_key="food", kind="expense", treatment="regular",
            color="#16a34a", sort_order=0,
        )  # fmt: skip

    def make(repository: TransactionRepository, category_id: UUID) -> Any:
        return repository.create(
            direction="expense", day=date(2026, 10, 7), amount_minor=1250, currency="EUR",
            category_id=category_id, description="Lidl", description_key="lidl",
            source="manual", created_by_user_id=None,
        )  # fmt: skip

    food_a, food_b = category(ctx_a), category(ctx_b)
    a_row, b_row = make(in_a, food_a.id), make(in_b, food_b.id)
    # The same filter (even naming the other workspace's category) sees only its own rows.
    everything = TransactionFilter(text_key="lidl")
    both = TransactionFilter(category_ids=frozenset({food_a.id, food_b.id}))
    for repository, own, food in ((in_a, a_row, food_a), (in_b, b_row, food_b)):
        for where in (everything, both, TransactionFilter()):
            page = repository.search(where, sort=SortOrder.DATE_DESC, limit=50, offset=0)
            assert [(r.id, c.id) for r, c in page] == [(own.id, food.id)]
            assert repository.count_matching(where) == 1
            assert [(r.id, c.id) for r, c in repository.iter_matching(where)] == [(own.id, food.id)]
            for lock in (True, False):
                found = repository.matching_for_update(where=where, lock=lock, limit=10)
                assert [(r.id, c.id) for r, c in found] == [(own.id, food.id)]
        # Ids of the other workspace's rows are not found.
        ids = {a_row.id, b_row.id}
        assert [r.id for r, _c in repository.matching_for_update(ids=ids)] == [own.id]
    assert in_b.count_matching(TransactionFilter(category_ids=frozenset({food_a.id}))) == 0


def test_budget_line_queries_stay_in_their_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    ctx_a, ctx_b = two_workspaces
    in_a, in_b = BudgetLineRepository(db_session, ctx_a), BudgetLineRepository(db_session, ctx_b)
    october = YearMonth(2026, 10)
    assert (in_a.any_exists(), in_b.any_exists()) == (False, False)

    def category(ctx: Any) -> Any:
        return CategoryRepository(db_session, ctx).create(
            name="Food", name_key="food", kind="expense", treatment="regular",
            color="#16a34a", sort_order=0,
        )  # fmt: skip

    food_a, food_b = category(ctx_a), category(ctx_b)
    a_line = in_a.create(month=october, category_id=food_a.id, planned_minor=55000, currency="EUR")
    assert a_line.workspace_id == ctx_a.workspace.id
    assert (in_a.any_exists(), in_b.any_exists()) == (True, False)
    assert [line.id for line in in_a.for_month(october)] == [a_line.id]
    assert in_b.for_month(october) == []
    assert in_a.line(october, food_a.id) is a_line
    assert in_b.line(october, food_a.id) is None  # another workspace's line is not found
    assert in_a.usage_by_category() == {food_a.id: LineUsage(1, frozenset({october}))}
    assert in_b.usage_by_category() == {}
    # The composite foreign key refuses a line on another workspace's category.
    with pytest.raises(IntegrityError), db_session.begin_nested():
        in_b.create(month=october, category_id=food_a.id, planned_minor=1, currency="EUR")
    b_line = in_b.create(month=october, category_id=food_b.id, planned_minor=100, currency="EUR")
    assert [line.id for line in in_b.for_month(october)] == [b_line.id]
    assert [line.id for line in in_a.for_month(october)] == [a_line.id]


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
    assert set(CURRENT_WORKSPACE_METHOD_COVERAGE) == public_methods(CurrentWorkspaceRepository)
    assert set(TOKEN_READER_METHOD_COVERAGE) == public_methods(InvitationTokenReader)
    assert set(INSTANCE_AUDIT_READER_METHOD_COVERAGE) == public_methods(InstanceAuditReader)
    covering = {
        *INSTANCE_AUDIT_READER_METHOD_COVERAGE.values(),
        *TOKEN_READER_METHOD_COVERAGE.values(),
        *BASE_METHOD_COVERAGE.values(),
        *EXTRA_METHOD_COVERAGE.values(),
        *READER_METHOD_COVERAGE.values(),
        *CURRENT_WORKSPACE_METHOD_COVERAGE.values(),
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


class ScratchLetter(IdMixin, WorkspaceScopedMixin, ScratchBase):
    __tablename__ = "scratch_letters"

    text: Mapped[str] = mapped_column(String(80), nullable=False)


class ScratchMemo(IdMixin, WorkspaceScopedMixin, ScratchBase):
    __tablename__ = "scratch_memos"

    text: Mapped[str] = mapped_column(String(80), nullable=False)


class UnscopedGetRepository(ScopedRepository[ScratchLetter]):
    model = ScratchLetter

    def get(self, row_id: UUID) -> ScratchLetter | None:  # bypasses the workspace filter
        return self._session.get(ScratchLetter, row_id)


class UnscopedDeleteRepository(ScopedRepository[ScratchMemo]):
    model = ScratchMemo

    def delete(self, target: ScratchMemo | UUID) -> None:  # deletes whatever it is given
        row_id = target if isinstance(target, UUID) else target.id
        self._session.delete(self._session.get(ScratchMemo, row_id))
        self._session.flush()


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


def _tag(session: Session, workspace_id: UUID) -> ScratchTag:
    return ScratchTag(workspace_id=workspace_id, label="tag")


def test_isolation_harness_detects_discovery_problems() -> None:
    cases = {
        model: object()
        for model in (ScratchNote, ScratchTag, ScratchLetter, ScratchMemo, ScratchWorkspace)
    }
    assert discovery_problems(ScratchBase, repositories_for(ScratchBase), cases) == [
        "ScratchUnmarked has workspace_id but not the WorkspaceOwned marker",
        f"ScratchChild has no repository; {ARCHITECTURE_HINT}",
        f"ScratchChild has no tenant factory; {ARCHITECTURE_HINT}",
        f"ScratchUnmarked has no repository; {ARCHITECTURE_HINT}",
        f"ScratchUnmarked has no tenant factory; {ARCHITECTURE_HINT}",
        "tenant factory for ScratchWorkspace, which is not workspace-owned",
    ]


def _scratch_contexts(session: Session, clock: FixedClock) -> tuple[Any, Any]:
    first, second = ScratchWorkspace(name="A"), ScratchWorkspace(name="B")
    session.add_all([first, second])
    session.flush()
    return context_for(first.id, clock), context_for(second.id, clock)


def test_isolation_harness_passes_a_correctly_scoped_repository(
    scratch_session: Session, fixed_clock: FixedClock
) -> None:
    ctx_a, ctx_b = _scratch_contexts(scratch_session, fixed_clock)
    check_isolation(
        scratch_session, ScratchTag, ChattyTagRepository, _tag, {"label": "x"}, ctx_a, ctx_b
    )


def test_isolation_harness_detects_unscoped_repository(
    scratch_session: Session, fixed_clock: FixedClock
) -> None:
    ctx_a, ctx_b = _scratch_contexts(scratch_session, fixed_clock)
    with pytest.raises(
        AssertionError, match="ScratchNote: could read a row of another workspace through list"
    ):
        check_isolation(
            scratch_session, ScratchNote, LeakyNoteRepository, _note, {"text": "x"}, ctx_a, ctx_b
        )


@pytest.mark.parametrize(
    ("model", "repository", "message"),
    [
        (
            ScratchLetter,
            UnscopedGetRepository,
            "ScratchLetter: could read a row of another workspace through get",
        ),
        (
            ScratchMemo,
            UnscopedDeleteRepository,
            "ScratchMemo: could change a row of another workspace through delete by id",
        ),
    ],
    ids=["get_without_scope", "delete_without_scope"],
)
def test_isolation_harness_detects_unscoped_methods(
    scratch_session: Session,
    fixed_clock: FixedClock,
    model: type[Any],
    repository: type[ScopedRepository[Any]],
    message: str,
) -> None:
    ctx_a, ctx_b = _scratch_contexts(scratch_session, fixed_clock)

    def factory(_session: Session, workspace_id: UUID) -> Any:
        return model(workspace_id=workspace_id, text="x")

    with pytest.raises(AssertionError, match=message):
        check_isolation(scratch_session, model, repository, factory, {"text": "y"}, ctx_a, ctx_b)


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


# Relationships into workspace-owned models


def test_no_relationship_from_unowned_models_into_workspace_owned_models() -> None:
    assert relationships_into_owned_models(Base) == []


def test_relationship_check_detects_a_seeded_relationship() -> None:
    class RelationshipBase(DeclarativeBase):
        pass

    class Home(IdMixin, RelationshipBase):
        __tablename__ = "homes"

        notes: Mapped[list[Note]] = relationship()

    class Note(IdMixin, WorkspaceOwned, RelationshipBase):
        __tablename__ = "notes"

        workspace_id: Mapped[UUID] = mapped_column(ForeignKey("homes.id"), nullable=False)
        home: Mapped[Home] = relationship(viewonly=True)  # owned -> unowned is fine

    assert relationships_into_owned_models(RelationshipBase) == ["Home.notes -> Note"]


def test_scheduled_queries_stay_in_their_workspace(db_session, two_workspaces) -> None:
    from assetboard.db.models.ledger import Category
    from assetboard.db.repositories.scheduled import (
        ScheduledItemRepository,
        ScheduledOccurrenceRepository,
    )

    ctx_a, ctx_b = two_workspaces
    items = ScheduledItemRepository(db_session, ctx_a)
    rows = ScheduledOccurrenceRepository(db_session, ctx_a)
    category = CategoryRepository(db_session, ctx_a).add(
        TENANT_CASES[Category].factory(db_session, ctx_a.workspace.id)
    )
    item = items.create(
        name="Insurance",
        direction="expense",
        category_id=category.id,
        amount_mode="fixed",
        amount_minor=180000,
        currency="EUR",
        start_date=date(2026, 1, 1),
        freq="monthly",
        interval=1,
        weekdays=[],
        month_day="14",
        month=1,
        weekend_policy="none",
        notes="",
    )
    row = rows.create(
        scheduled_item_id=item.id,
        category_id=category.id,
        direction="expense",
        nominal_date=date(2026, 3, 14),
        due_date=date(2026, 3, 14),
        expected_minor=180000,
        currency="EUR",
        amount_state="confirmed",
        status="pending",
        label="Insurance",
    )
    assert item.workspace_id == row.workspace_id == ctx_a.workspace.id
    other_items = ScheduledItemRepository(db_session, ctx_b)
    other_rows = ScheduledOccurrenceRepository(db_session, ctx_b)
    with pytest.raises(ForeignWorkspace):
        other_items.create(workspace_id=ctx_a.workspace.id)
    with pytest.raises(ForeignWorkspace):
        other_rows.create(workspace_id=ctx_a.workspace.id)
    assert other_items.ordered() == [] and other_items.ordered(active_only=True) == []
    assert other_items.category_counts() == {} and other_rows.category_counts() == {}
    assert other_rows.for_item(item.id) == []
    assert other_rows.for_income_source(uuid4()) == []
    assert other_rows.for_income_work_month(uuid4(), date(2026, 3, 1)) is None
    assert other_rows.children(row.id) == []
    assert other_rows.for_tax_attachment(uuid4()) == []
    assert other_rows.upcoming(date(2026, 4, 1)) == []
    assert rows.get_for_update(row.id) == row
    assert other_rows.get_for_update(row.id) is None
    transaction = TransactionRepository(db_session, ctx_a).create(
        direction="expense",
        day=date(2026, 3, 14),
        amount_minor=180000,
        currency="EUR",
        category_id=category.id,
        description="Insurance",
        description_key="insurance",
        source="recurring",
        created_by_user_id=None,
    )
    rows.update(row.id, transaction_id=transaction.id, previous_transaction_id=transaction.id)
    for previous in (False, True):
        assert rows.linked_to_transaction_for_update(transaction.id, previous=previous) == [row]
        assert other_rows.linked_to_transaction_for_update(transaction.id, previous=previous) == []
    rows.update(row.id, transaction_id=None, previous_transaction_id=None)
    assert not other_items.any_exists() and items.any_exists()
    with pytest.raises(NotFound):
        other_rows.delete(row)
    assert rows.for_item(item.id)[0].id == row.id
    assert rows.upcoming(date(2026, 4, 1))[0][0].id == row.id
    assert item.id in {value.id for value in items.ordered()}
    assert item.id in {value.id for value in items.ordered(active_only=True)}
    assert items.category_counts()[item.category_id] >= 1
    assert rows.category_counts() == {item.category_id: 1}
    items.update(item.id, is_tax=True)
    assert [value.id for value in items.tax_items()] == [item.id]
    assert other_items.tax_items() == []
    rows.delete(row)
    assert rows.for_item(item.id) == []


def test_income_queries_stay_in_their_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    from assetboard.db.repositories import (
        IncomeSourceRepository,
        IncomeSourceTaxRuleRepository,
        ScheduledOccurrenceRepository,
        TaxRuleRepository,
        WorkDayRepository,
        WorkLogRepository,
    )

    ctx_a, ctx_b = two_workspaces

    def build(ctx: Any, suffix: str) -> tuple[Any, Any, Any]:
        category = CategoryRepository(db_session, ctx).create(
            name=f"Income {suffix}",
            name_key=f"income-{suffix.casefold()}",
            kind="income",
            treatment="regular",
            color="#16a34a",
            sort_order=0,
        )
        source = IncomeSourceRepository(db_session, ctx).create(
            name=f"Source {suffix}",
            kind="variable",
            category_id=category.id,
            currency="EUR",
            fixed_minor=None,
            rate_minor=100,
            unit_label="day",
            pay_day=10,
            pay_lag_months=1,
            expected_mode="manual",
            expected_units=None,
            average_months=3,
            active_from=date(2026, 1, 1),
            active_to=None,
        )
        tax_rule = TaxRuleRepository(db_session, ctx).create(
            name=f"Tax {suffix}", kind="payer_withheld", base="net", notes=""
        )
        expense = CategoryRepository(db_session, ctx).create(
            name=f"Expense {suffix}",
            name_key=f"expense-{suffix.casefold()}",
            kind="expense",
            treatment="regular",
            color="#16a34a",
            sort_order=1,
        )
        attachment = IncomeSourceTaxRuleRepository(db_session, ctx).create(
            income_source_id=source.id,
            tax_rule_id=tax_rule.id,
            sort_order=0,
            generate_payment=True,
            payment_category_id=expense.id,
            due_day=10,
            due_month_offset=1,
        )
        log = WorkLogRepository(db_session, ctx).create(
            income_source_id=source.id,
            work_month=date(2026, 1, 1),
            units=Decimal("19.5"),
        )
        return source, attachment, log

    source_a, attachment_a, log_a = build(ctx_a, "A")
    source_b, attachment_b, log_b = build(ctx_b, "B")
    sources_a, sources_b = (
        IncomeSourceRepository(db_session, ctx_a),
        IncomeSourceRepository(db_session, ctx_b),
    )
    attachments_a = IncomeSourceTaxRuleRepository(db_session, ctx_a)
    attachments_b = IncomeSourceTaxRuleRepository(db_session, ctx_b)
    logs_a, logs_b = WorkLogRepository(db_session, ctx_a), WorkLogRepository(db_session, ctx_b)

    assert [row.id for row in sources_a.ordered()] == [source_a.id]
    assert [row.id for row in sources_b.ordered(active_only=True)] == [source_b.id]
    assert sources_a.any_exists() and sources_b.any_exists()
    assert sources_a.category_counts() == {source_a.category_id: 1}
    assert sources_b.category_counts() == {source_b.category_id: 1}
    assert [row.id for row in logs_a.for_source(source_a.id)] == [log_a.id]
    assert logs_b.for_source(source_a.id) == []
    assert [row.id for row in attachments_a.for_source(source_a.id)] == [attachment_a.id]
    assert attachments_b.for_source(source_a.id) == []
    assert attachments_a.for_source_and_rule(source_a.id, attachment_a.tax_rule_id) is attachment_a
    assert attachments_b.for_source_and_rule(source_b.id, attachment_a.tax_rule_id) is None
    assert attachments_a.payment_category_counts() == {attachment_a.payment_category_id: 1}
    assert [row.id for row in attachments_a.for_rule(attachment_a.tax_rule_id)] == [attachment_a.id]
    assert attachments_b.for_rule(attachment_a.tax_rule_id) == []
    assert attachments_b.payment_category_counts() == {attachment_b.payment_category_id: 1}
    assert [row.id for row in logs_b.for_source(source_b.id)] == [log_b.id]
    assert [row.id for row in attachments_b.for_source(source_b.id)] == [attachment_b.id]
    assert logs_a.work_months_for_sources([source_a.id, source_b.id]) == {
        source_a.id: {date(2026, 1, 1)}
    }
    assert logs_b.work_months_for_sources([source_a.id]) == {}

    # F3.7 day entries: a month query sees only its own workspace's rows.
    day_a = WorkDayRepository(db_session, ctx_a).create(
        income_source_id=source_a.id, work_date=date(2026, 1, 5), units=Decimal("1")
    )
    january = (date(2026, 1, 1), date(2026, 1, 31))
    assert [
        row.id for row in WorkDayRepository(db_session, ctx_a).for_month(source_a.id, *january)
    ] == [day_a.id]
    assert WorkDayRepository(db_session, ctx_b).for_month(source_a.id, *january) == []

    # Pending income occurrences: the month list and the overdue count see only their own.
    occurrences = {}
    for ctx, source in ((ctx_a, source_a), (ctx_b, source_b)):
        occurrences[ctx] = ScheduledOccurrenceRepository(db_session, ctx).create(
            scheduled_item_id=None,
            income_source_id=source.id,
            work_month=date(2026, 1, 1),
            direction="income",
            category_id=source.category_id,
            nominal_date=date(2026, 2, 10),
            due_date=date(2026, 2, 10),
            expected_minor=100,
            currency="EUR",
            amount_state="estimated",
            status="pending",
            generated_by="income",
            label=source.name,
        )
    for ctx in (ctx_a, ctx_b):
        repository = ScheduledOccurrenceRepository(db_session, ctx)
        listed = repository.pending_for_month(date(2026, 2, 1), date(2026, 2, 28))
        assert [row.id for row, _item, _name in listed] == [occurrences[ctx].id]
        assert repository.overdue_count(date(2026, 3, 1)) == 1
    occurrences_a = ScheduledOccurrenceRepository(db_session, ctx_a)
    occurrences_b = ScheduledOccurrenceRepository(db_session, ctx_b)
    assert [row.id for row in occurrences_a.for_income_source(source_a.id)] == [
        occurrences[ctx_a].id
    ]
    assert occurrences_b.for_income_source(source_a.id) == []
    assert occurrences_b.for_income_work_month(source_a.id, date(2026, 1, 1)) is None


def test_notification_queries_stay_in_their_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any], fixed_clock: FixedClock
) -> None:
    from datetime import time

    from assetboard.db.repositories import (
        NotificationChannelRepository,
        NotificationLogRepository,
    )

    ctx_a, ctx_b = two_workspaces
    user = User(email=f"notify-{uuid4().hex[:8]}@example.com", display_name="Notify")
    db_session.add(user)
    db_session.flush()
    now = fixed_clock.now()

    def build(ctx: Any) -> tuple[Any, Any]:
        channel = NotificationChannelRepository(db_session, ctx).create(
            user_id=user.id,
            name="Phone",
            url_encrypted="synthetic",
            scheme="ntfy",
            service_name="ntfy",
            enabled=True,
            mode="digest",
            digest_time=time(8, 0),
            default_lead_days=3,
            notify_due_soon=True,
            notify_overdue=True,
            notify_work_logs=True,
            include_amounts=False,
        )
        log = NotificationLogRepository(db_session, ctx).create(
            channel_id=channel.id,
            kind="overdue",
            subject_key="occurrence",
            occurrence_id=None,
            sent_on=date(2026, 1, 5),
            status="sending",
            attempted_at=now - timedelta(hours=1),
        )
        return channel, log

    channel_a, log_a = build(ctx_a)
    channel_b, log_b = build(ctx_b)
    channels_a = NotificationChannelRepository(db_session, ctx_a)
    channels_b = NotificationChannelRepository(db_session, ctx_b)
    logs_a = NotificationLogRepository(db_session, ctx_a)
    logs_b = NotificationLogRepository(db_session, ctx_b)

    assert [row.id for row in channels_a.for_user(user.id)] == [channel_a.id]
    assert channels_b.owned(channel_a.id, user.id) is None
    assert channels_a.owned(channel_a.id, user.id) is channel_a
    assert channels_a.count_for_user(user.id) == 1
    assert [row.id for row in channels_b.enabled()] == [channel_b.id]

    assert logs_b.since(channel_a.id, date(2026, 1, 1)) == []
    assert [row.id for row in logs_a.since(channel_a.id, date(2026, 1, 1))] == [log_a.id]
    assert logs_b.slot(channel_a.id, "overdue", "occurrence", date(2026, 1, 5)) is None
    assert logs_a.slot(channel_a.id, "overdue", "occurrence", date(2026, 1, 5)) is log_a
    assert [row.id for row in logs_b.stale_sending(now)] == [log_b.id]
    log_a.status = "sent"
    db_session.flush()
    assert logs_a.overdue_sent_counts(channel_a.id) == {"occurrence": 1}
    assert logs_b.overdue_sent_counts(channel_a.id) == {}
    assert [(row.id, name) for row, name in logs_b.recent_for_user(user.id, 10)] == [
        (log_b.id, "Phone")
    ]

    assert logs_b.prune(date(2027, 1, 1)) == 1
    assert logs_a.get(log_a.id) is not None
    assert channels_b.delete_for_user(user.id) == 1
    assert channels_a.get(channel_a.id) is not None
    assert channels_b.get(channel_b.id) is None


# Deleting a workspace (ADR-0020): every workspace-owned model, found by discovery


def _owned_rows(session: Session, model: type[Any], workspace_id: UUID) -> list[dict[str, object]]:
    table = inspect(model).local_table
    rows = session.connection().execute(select(table).where(table.c.workspace_id == workspace_id))
    return sorted((dict(row._mapping) for row in rows), key=lambda row: str(row["id"]))


def _session_row(session: Session, user_id: UUID, workspace_id: UUID | None) -> str:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    id_hash = uuid4().hex * 2
    session.add(
        UserSession(
            id_hash=id_hash,
            user_id=user_id,
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(days=1),
            current_workspace_id=workspace_id,
        )
    )
    session.flush()
    return id_hash


def test_deleting_a_workspace_removes_every_owned_row_and_nothing_else(
    db_session: Session, two_workspaces: tuple[Any, Any], fixed_clock: FixedClock
) -> None:
    ctx_a, ctx_b = two_workspaces
    a_id, b_id = ctx_a.workspace.id, ctx_b.workspace.id
    for model in OWNED_MODELS:  # discovery: a new workspace-owned model is covered by itself
        for ctx in two_workspaces:
            row = TENANT_CASES[model].factory(db_session, ctx.workspace.id)
            repositories_for(Base)[model](db_session, ctx).add(row)
    for model in OWNED_MODELS:
        assert _owned_rows(db_session, model, a_id), f"no {model.__name__} rows to delete"
    user = User(email="sessions@example.com", display_name="Sessions")
    db_session.add(user)
    db_session.add(
        AuditEvent(
            id=uuid4(),
            workspace_id=None,
            actor_type="system",
            action="auth.logout",
            entity_type="user",
            entity_id=None,
            data={},
            ts=fixed_clock.now(),
        )
    )
    db_session.flush()
    in_a = _session_row(db_session, user.id, a_id)
    in_b = _session_row(db_session, user.id, b_id)
    before_b = {model: _owned_rows(db_session, model, b_id) for model in OWNED_MODELS}
    workspace_b = raw_row(db_session, Workspace, b_id)
    instance_events = InstanceAuditReader(db_session).page(AuditFilter(), limit=10)

    CurrentWorkspaceRepository(db_session, ctx_a).delete_with_all_data(now=fixed_clock.now())

    for model in OWNED_MODELS:
        assert _owned_rows(db_session, model, a_id) == [], model.__name__
        assert _owned_rows(db_session, model, b_id) == before_b[model], model.__name__
    assert raw_row(db_session, Workspace, a_id) is None
    assert raw_row(db_session, Workspace, b_id) == workspace_b
    sessions = inspect(UserSession).local_table
    selected = dict(
        db_session.connection()
        .execute(
            select(sessions.c.id_hash, sessions.c.current_workspace_id).where(
                sessions.c.id_hash.in_([in_a, in_b])
            )
        )
        .all()
    )
    assert selected == {in_a: None, in_b: b_id}
    assert InstanceAuditReader(db_session).page(AuditFilter(), limit=10) == instance_events
    with pytest.raises(NotFound):  # already gone
        CurrentWorkspaceRepository(db_session, ctx_a).delete_with_all_data(now=fixed_clock.now())


def test_workspace_references_cover_every_reference_to_workspaces() -> None:
    owned, references = workspace_references()
    assert {table.name for table in owned} == {
        inspect(model).local_table.name for model in OWNED_MODELS
    }
    assert [f"{column.table.name}.{column.name}" for column in references] == [
        "user_sessions.current_workspace_id"
    ]
    # Children before parents: the composite foreign keys hold at every step.
    order = [table.name for table in owned]
    for child, parent in (
        ("transactions", "categories"),
        ("budget_lines", "categories"),
        ("scheduled_occurrences", "scheduled_items"),
        ("scheduled_items", "categories"),
    ):
        assert order.index(child) < order.index(parent)


def test_workspace_references_refuse_a_reference_that_cannot_be_cleared() -> None:
    metadata = MetaData()
    Table("workspaces", metadata, Column("id", Uuid(), primary_key=True))
    Table(
        "pins",
        metadata,
        Column("id", Uuid(), primary_key=True),
        Column("pinned_workspace_id", Uuid(), ForeignKey("workspaces.id"), nullable=False),
    )
    with pytest.raises(RuntimeError, match="pins.pinned_workspace_id"):
        workspace_references(metadata)


def test_tax_rule_queries_stay_in_their_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    from assetboard.db.repositories import TaxRuleRepository, TaxRuleVersionRepository

    ctx_a, ctx_b = two_workspaces
    rules_a, rules_b = TaxRuleRepository(db_session, ctx_a), TaxRuleRepository(db_session, ctx_b)
    versions_a = TaxRuleVersionRepository(db_session, ctx_a)
    versions_b = TaxRuleVersionRepository(db_session, ctx_b)
    rule_a = rules_a.create(name="Income tax A", kind="payer_withheld", base="net", notes="")
    rule_b = rules_b.create(name="Income tax B", kind="payer_withheld", base="net", notes="")
    version_a = versions_a.create(
        tax_rule_id=rule_a.id,
        effective_from=date(2026, 1, 1),
        mode="percent",
        rate_bp=1200,
        fixed_minor=None,
    )
    versions_b.create(
        tax_rule_id=rule_b.id,
        effective_from=date(2026, 1, 1),
        mode="percent",
        rate_bp=1500,
        fixed_minor=None,
    )
    fixed_version_a = versions_a.create(
        tax_rule_id=rule_a.id,
        effective_from=date(2027, 1, 1),
        mode="fixed",
        rate_bp=None,
        fixed_minor=500,
    )
    assert [row.id for row in rules_a.ordered()] == [rule_a.id]
    assert rules_a.payment_category_counts() == {} and rules_b.payment_category_counts() == {}
    assert not rules_a.has_posted_income(rule_a.id)
    assert not rules_b.has_posted_income(rule_a.id)
    assert rules_a.effective_for_all(date(2026, 1, 1)) == [(rule_a, version_a)]
    assert rules_b.effective_for_all(date(2026, 1, 1))[0][0].id == rule_b.id
    assert versions_a.for_rule(rule_a.id) == [version_a, fixed_version_a]
    assert versions_b.for_rule(rule_a.id) == []
    assert versions_a.effective(rule_a.id, date(2026, 1, 1)) == version_a
    assert versions_b.effective(rule_a.id, date(2026, 1, 1)) is None
    assert versions_a.any_fixed_amount() and not versions_b.any_fixed_amount()
    assert [row.id for row in versions_a.all_for_rules()] == [
        version_a.id,
        fixed_version_a.id,
    ]
    updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    assert (
        versions_b.update_future(
            fixed_version_a.id,
            today=date(2026, 1, 1),
            effective_from=date(2027, 2, 1),
            mode="percent",
            rate_bp=1900,
            fixed_minor=None,
            updated_at=updated_at,
        )
        == 0
    )
    assert (
        versions_a.update_future(
            fixed_version_a.id,
            today=date(2026, 1, 1),
            effective_from=date(2027, 2, 1),
            mode="percent",
            rate_bp=1900,
            fixed_minor=None,
            updated_at=updated_at,
        )
        == 1
    )
    updated = versions_a.get(fixed_version_a.id)
    assert updated is not None and (updated.rate_bp, updated.fixed_minor) == (1900, None)
    with pytest.raises(ForeignWorkspace):
        rules_b.create(
            workspace_id=ctx_a.workspace.id,
            name="Cross workspace",
            kind="payer_withheld",
            base="net",
            notes="",
        )
    with pytest.raises(ForeignWorkspace):
        versions_b.create(
            workspace_id=ctx_a.workspace.id,
            tax_rule_id=rule_a.id,
            effective_from=date(2027, 1, 1),
            mode="percent",
            rate_bp=100,
            fixed_minor=None,
        )
