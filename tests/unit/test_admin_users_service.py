"""Instance administration services (F1.5 part B)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select, update

from assetboard.db.engine import run_immediate
from assetboard.db.models.identity import (
    AuditEvent,
    EmailToken,
    ExternalIdentity,
    Membership,
    SetupState,
    User,
    UserSession,
)
from assetboard.db.models.totp import TotpFactor, TotpRecoveryCode
from assetboard.domain.totp import recovery_hash
from assetboard.services import admin_users
from assetboard.services.admin_users import (
    AUDIT_DATA_KEYS,
    NotInstanceAdmin,
    OwnershipBlocked,
    apply_admin_action,
    generate_temporary_password,
    get_user,
    list_users,
)
from assetboard.services.auth import PasswordPolicyError, validate_password
from assetboard.services.external_identity import PROXY_PROVIDER
from tests.tenant.serialize import serialize_at_checkpoint
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    for database in workspace_db(tmp_path):
        with database.factory.begin() as session:
            session.execute(update(SetupState).values(completed_at=database.clock.now()))
        yield database


def add_user(db: WorkspaceDb, email: str, *, admin: bool = False, active: bool = True) -> UUID:
    with db.factory.begin() as session:
        user = User(
            email=email, display_name=email.split("@")[0], is_instance_admin=admin, is_active=active
        )
        session.add(user)
        session.flush()
        return user.id


def run(db: WorkspaceDb, actor_id: UUID, target_id: UUID, action: Any) -> Any:
    def apply(session: Any) -> Any:
        actor = session.get(User, actor_id)
        return apply_admin_action(
            session, actor, target_id, action, clock=db.clock, host=None, user_agent=None
        )

    return run_immediate(db.factory, apply)


def admins(db: WorkspaceDb) -> list[str]:
    with db.factory() as session:
        return sorted(
            session.scalars(
                select(User.email).where(User.is_instance_admin.is_(True), User.is_active.is_(True))
            ).all()
        )


def test_two_administrators_demoting_each_other_at_once_keep_one(
    db: WorkspaceDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = add_user(db, "a@example.com", admin=True)
    b = add_user(db, "b@example.com", admin=True)
    results: dict[str, str] = {}

    def demote(name: str, actor: UUID, target: UUID) -> None:
        results[name] = run(db, actor, target, "demote")

    outcomes = serialize_at_checkpoint(
        db.engine,
        monkeypatch,
        admin_users,
        lambda: demote("first", a, b),
        lambda: demote("second", b, a),
    )
    # The first demotes b; b is no longer an administrator when its own action runs.
    assert outcomes["first"] is None and results["first"] == "done"
    assert isinstance(outcomes["second"], NotInstanceAdmin)
    assert admins(db) == ["a@example.com"]


def test_two_administrators_deactivating_each_other_at_once_keep_one(
    db: WorkspaceDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = add_user(db, "a@example.com", admin=True)
    b = add_user(db, "b@example.com", admin=True)
    outcomes = serialize_at_checkpoint(
        db.engine,
        monkeypatch,
        admin_users,
        lambda: run(db, a, b, "deactivate"),
        lambda: run(db, b, a, "deactivate"),
    )
    assert outcomes["first"] is None and isinstance(outcomes["second"], NotInstanceAdmin)
    assert admins(db) == ["a@example.com"]


def test_the_last_active_administrator_is_kept(db: WorkspaceDb) -> None:
    actor = add_user(db, "a@example.com", admin=True)
    # Another administrator that is deactivated does not count as a remaining one.
    other = add_user(db, "b@example.com", admin=True, active=False)
    assert run(db, actor, actor, "demote") == "self"
    assert run(db, actor, actor, "deactivate") == "self"
    assert run(db, actor, other, "demote") == "done"  # demoting an inactive admin is fine
    assert admins(db) == ["a@example.com"]


def test_a_non_administrator_cannot_act(db: WorkspaceDb) -> None:
    actor = add_user(db, "user@example.com")
    target = add_user(db, "other@example.com")
    for action in ("activate", "deactivate", "promote", "demote", "reset_totp", "delete"):
        with pytest.raises(NotInstanceAdmin):
            run(db, actor, target, action)


def test_audit_data_keys_are_named_for_every_action() -> None:
    assert AUDIT_DATA_KEYS == {
        "admin.user_created": ("temporary_password",),
        "admin.user_activated": (),
        "admin.user_deactivated": ("sessions_revoked",),
        "admin.admin_granted": (),
        "admin.admin_revoked": (),
        "admin.totp_reset": ("sessions_revoked",),
        "admin.user_deleted": ("memberships_removed",),
    }


def test_admin_totp_reset_clears_factor_codes_and_rejects_self_reset(db: WorkspaceDb) -> None:
    actor_id = add_user(db, "admin@example.com", admin=True)
    target_id = add_user(db, "target@example.com")
    with db.factory.begin() as session:
        session.add(
            TotpFactor(
                user_id=target_id,
                encrypted_secret="encrypted",
                enabled_at=db.clock.now(),
                last_step=42,
            )
        )
        session.add(TotpRecoveryCode(user_id=target_id, code_hash=recovery_hash("recovery")))

    assert run(db, actor_id, actor_id, "reset_totp") == "self_totp_reset"
    assert run(db, actor_id, target_id, "reset_totp") == "done"
    assert run(db, actor_id, target_id, "reset_totp") == "unchanged"
    with db.factory() as session:
        assert session.get(TotpFactor, target_id) is None
        assert (
            list(
                session.scalars(
                    select(TotpRecoveryCode).where(TotpRecoveryCode.user_id == target_id)
                )
            )
            == []
        )
        audit = session.scalar(select(AuditEvent).where(AuditEvent.action == "admin.totp_reset"))
        assert audit is not None and audit.data == {"sessions_revoked": 0}


def test_temporary_passwords_pass_the_policy_and_differ() -> None:
    passwords = {generate_temporary_password("new@example.com", "New") for _ in range(50)}
    assert len(passwords) == 50
    for password in passwords:
        assert len(password) >= 16
        validate_password(password, "new@example.com", "New")


def test_temporary_password_generation_gives_up_after_twenty_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []

    def always_refuse(password: str, _email: str, _name: str) -> None:
        attempts.append(password)
        raise PasswordPolicyError("password_too_common", "refused")

    monkeypatch.setattr(admin_users, "validate_password", always_refuse)
    with pytest.raises(RuntimeError, match="temporary password"):
        generate_temporary_password("new@example.com", "New")
    assert len(attempts) == 20


@pytest.mark.parametrize("action", ["deactivate", "demote"])
def test_the_last_administrator_guard_on_its_own(db: WorkspaceDb, action: Any) -> None:
    # The guard behind apply_admin_action's actor check: an actor that is no longer an active
    # administrator (for example demoted meanwhile) must still not remove the last one.
    former = add_user(db, "former@example.com", admin=False)
    last = add_user(db, "last@example.com", admin=True)
    with db.factory() as session:
        actor, target = session.get(User, former), session.get(User, last)
        assert actor is not None and target is not None
        assert admin_users._protection(session, actor, target, action) == "last_admin"
        assert admin_users._protection(session, target, target, action) == "self"
        assert admin_users._protection(session, actor, target, "promote") is None


def deactivate(db: WorkspaceDb, user_id: UUID) -> None:
    with db.factory.begin() as session:
        user = session.get(User, user_id)
        assert user is not None
        user.is_active = False


def join(db: WorkspaceDb, user_id: UUID, workspace_id: UUID, role: str) -> UUID:
    with db.factory.begin() as session:
        membership = Membership(workspace_id=workspace_id, user_id=user_id, role=role)
        session.add(membership)
        session.flush()
        return membership.id


def memberships_for(db: WorkspaceDb, user_id: UUID) -> list[Membership]:
    with db.factory() as session:
        return list(session.scalars(select(Membership).where(Membership.user_id == user_id)))


def test_delete_removes_all_memberships_the_account_and_keeps_the_audit_row(
    db: WorkspaceDb,
) -> None:
    actor = add_user(db, "admin@example.com", admin=True)
    target = add_user(db, "target@example.com")
    other = db.add_workspace("Other")
    join(db, target, db.workspace_id, "viewer")
    join(db, target, other, "editor")
    now = db.clock.now()
    with db.factory.begin() as session:
        session.add(ExternalIdentity(user_id=target, provider="https://idp.test", subject="s"))
        session.add(
            UserSession(
                id_hash="a" * 64,
                user_id=target,
                created_at=now,
                last_seen_at=now,
                expires_at=now + timedelta(days=1),
            )
        )
        session.add(
            EmailToken(
                id=uuid4(),
                purpose="reset",
                token_hash="b" * 64,
                email="target@example.com",
                user_id=target,
                created_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
    deactivate(db, target)

    assert run(db, actor, target, "delete") == "done"

    with db.factory() as session:
        assert session.get(User, target) is None
        assert list(session.scalars(select(ExternalIdentity))) == []
        assert list(session.scalars(select(UserSession))) == []
        assert list(session.scalars(select(EmailToken))) == []
        audit = session.scalar(select(AuditEvent).where(AuditEvent.action == "admin.user_deleted"))
        assert audit is not None
        assert audit.entity_id == str(target)
        assert audit.actor_user_id == actor
        assert audit.data == {"memberships_removed": 2}
    assert memberships_for(db, target) == []


def test_delete_refuses_the_only_owner_of_a_workspace_and_lists_it(db: WorkspaceDb) -> None:
    actor = add_user(db, "admin@example.com", admin=True)
    target = add_user(db, "target@example.com")
    family = db.add_workspace("Family")
    join(db, target, db.workspace_id, "viewer")
    join(db, target, family, "owner")
    deactivate(db, target)

    result = run(db, actor, target, "delete")

    assert isinstance(result, OwnershipBlocked)
    assert [(workspace.id, workspace.name) for workspace in result.workspaces] == [
        (family, "Family")
    ]
    with db.factory() as session:
        assert session.get(User, target) is not None
        assert (
            session.scalar(select(AuditEvent).where(AuditEvent.action == "admin.user_deleted"))
            is None
        )
    assert len(memberships_for(db, target)) == 2


def test_delete_allows_a_shared_owner_and_keeps_the_workspace(db: WorkspaceDb) -> None:
    actor = add_user(db, "admin@example.com", admin=True)
    target = add_user(db, "target@example.com")
    other_owner = add_user(db, "other@example.com")
    family = db.add_workspace("Family")
    join(db, target, family, "owner")
    join(db, other_owner, family, "owner")
    join(db, target, db.workspace_id, "viewer")
    deactivate(db, target)

    assert run(db, actor, target, "delete") == "done"

    with db.factory() as session:
        assert session.get(User, target) is None
        assert session.get(User, other_owner) is not None
        rows = list(session.scalars(select(Membership).where(Membership.workspace_id == family)))
        assert [(row.user_id, row.role) for row in rows] == [(other_owner, "owner")]


def test_delete_refuses_active_self_and_demo_accounts(db: WorkspaceDb) -> None:
    actor = add_user(db, "admin@example.com", admin=True)
    active = add_user(db, "active@example.com")
    assert run(db, actor, active, "delete") == "active_user"
    assert run(db, actor, actor, "delete") == "self"
    demo = add_user(db, "demo@example.invalid", active=False)
    with db.factory.begin() as session:
        user = session.get(User, demo)
        assert user is not None
        user.is_demo = True
    assert run(db, actor, demo, "delete") == "not_found"
    with db.factory() as session:
        assert session.get(User, active) is not None
        assert session.get(User, demo) is not None


def link(db: WorkspaceDb, user_id: UUID, provider: str, subject: str) -> None:
    with db.factory.begin() as session:
        session.add(ExternalIdentity(user_id=user_id, provider=provider, subject=subject))


def add_users(db: WorkspaceDb, count: int) -> None:
    """``count`` users cycling through password, OIDC, proxy and mixed sign-in methods."""
    for index in range(count):
        user_id = add_user(db, f"user{index:02d}@example.com")
        if index % 4 in (0, 3):
            with db.factory.begin() as session:
                session.execute(update(User).where(User.id == user_id).values(password_hash="x"))
        if index % 4 in (1, 3):
            link(db, user_id, "https://idp.example.test", f"sub-{index}")
        if index % 4 in (2, 3):
            link(db, user_id, PROXY_PROVIDER, f"user{index}")


def statements_while(db: WorkspaceDb, call: Any) -> int:
    count = 0

    def count_statement(*_args: Any) -> None:
        nonlocal count
        count += 1

    event.listen(db.engine, "before_cursor_execute", count_statement)
    try:
        call()
    finally:
        event.remove(db.engine, "before_cursor_execute", count_statement)
    return count


def test_listing_users_shows_each_sign_in_method(db: WorkspaceDb) -> None:
    actor = add_user(db, "admin@example.com", admin=True)
    add_users(db, 4)
    rows = {row.email: row.methods for row in list_users(db.factory, actor_id=actor)}
    assert rows == {
        "admin@example.com": (),
        "user00@example.com": ("password",),
        "user01@example.com": ("oidc",),
        "user02@example.com": ("proxy",),
        "user03@example.com": ("password", "oidc", "proxy"),
    }
    for row in list_users(db.factory, actor_id=actor):
        assert get_user(db.factory, actor_id=actor, user_id=row.id) == row


def test_listing_users_runs_the_same_queries_for_3_and_10_users(db: WorkspaceDb) -> None:
    actor = add_user(db, "admin@example.com", admin=True)
    add_users(db, 2)  # 3 users with the administrator
    with_3 = statements_while(db, lambda: list_users(db.factory, actor_id=actor))
    for index in range(7):  # 10 users, each with linked identities
        user_id = add_user(db, f"more{index:02d}@example.com")
        link(db, user_id, "https://idp.example.test", f"more-{index}")
        link(db, user_id, PROXY_PROVIDER, f"more{index}")
    assert len(list_users(db.factory, actor_id=actor)) == 10
    with_10 = statements_while(db, lambda: list_users(db.factory, actor_id=actor))
    assert with_10 == with_3
