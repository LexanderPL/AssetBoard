"""Service-layer demo restrictions (F9.11b).

One row per restricted service function: it runs as the demo user/workspace and must raise
``DemoRestricted`` (or the documented equivalent refusal) without writing anything. The mail,
admin and login paths have their own tests because their refusal is not an exception.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from assetboard.config import Settings
from assetboard.db.models.identity import (
    AuditEvent,
    AuthThrottle,
    EmailToken,
    ExternalIdentity,
    Invitation,
    Membership,
    User,
    UserSession,
    Workspace,
)
from assetboard.db.models.notifications import NotificationChannel
from assetboard.db.models.prices import PriceSourceConfig
from assetboard.db.models.totp import TotpFactor
from assetboard.services import (
    account_email,
    auth,
    invitations,
    notifications,
    price_refresh,
    price_sources,
    profile,
    taxes,
    totp,
    workspaces,
)
from assetboard.services.admin_users import apply_admin_action
from assetboard.services.auth import HashGate, argon2_hash
from assetboard.services.context import Context, DemoRestricted, Role, UserRef, WorkspaceRef
from assetboard.services.external_identity import PROXY_PROVIDER, SignInIdentity
from assetboard.services.mail import MailRequest
from assetboard.services.notifications import ChannelInput
from assetboard.services.oidc_accounts import link_identity, sign_in_external
from assetboard.services.oidc_flow import PendingLink
from assetboard.services.providers.registry import build_provider_runtime
from assetboard.services.providers.transport import ProviderRuntime
from tests.fake_notifier import FakeNotifier
from tests.fake_provider_http import FakeProviderHttpClient
from tests.tenant.workspace_db import WorkspaceDb, workspace_db

ISSUER = "https://idp.example.test/realms/demo"
DEMO_EMAIL = "demo@example.invalid"
REAL_EMAIL = "real@example.com"
SESSION_HASH = "a" * 64


@dataclass(frozen=True)
class DemoEnv:
    db: WorkspaceDb
    user_id: UUID
    workspace_id: UUID
    membership_id: UUID
    email: str
    settings: Settings
    runtime: ProviderRuntime
    notifier: FakeNotifier
    ctx: Context


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    yield from workspace_db(tmp_path)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        secret_key="demo-restrictions-secret",
        base_url="https://money.example.test",
        smtp_host="smtp.example.test",
        smtp_from="finance@example.test",
        oidc_issuer=ISSUER,
        oidc_client_id="assetboard",
        oidc_client_secret="secret",
    )


def _demo_env(db: WorkspaceDb, tmp_path: Path) -> DemoEnv:
    with db.factory.begin() as session:
        user = User(
            email=DEMO_EMAIL,
            display_name="Demo",
            password_hash=None,
            is_demo=True,
        )
        session.add(user)
        session.flush()
        workspace = Workspace(
            name="Demo",
            base_currency="EUR",
            locale="en",
            timezone="UTC",
            is_demo=True,
        )
        session.add(workspace)
        session.flush()
        membership = Membership(workspace_id=workspace.id, user_id=user.id, role="owner")
        session.add(membership)
        session.flush()
        user_id, workspace_id, membership_id = user.id, workspace.id, membership.id

    settings = _settings(tmp_path)
    clock = db.clock
    ctx = Context(
        user=UserRef(
            id=user_id,
            email=DEMO_EMAIL,
            display_name="Demo",
            locale="en",
            is_instance_admin=False,
            is_demo=True,
        ),
        workspace=WorkspaceRef(
            id=workspace_id,
            name="Demo",
            base_currency="EUR",
            locale="en",
            timezone="UTC",
            country_code=None,
            is_demo=True,
        ),
        role=Role.OWNER,
        clock=clock,
    )
    runtime = build_provider_runtime(settings, clock, http=FakeProviderHttpClient({}))
    return DemoEnv(
        db=db,
        user_id=user_id,
        workspace_id=workspace_id,
        membership_id=membership_id,
        email=DEMO_EMAIL,
        settings=settings,
        runtime=runtime,
        notifier=FakeNotifier(),
        ctx=ctx,
    )


@pytest.fixture
def env(db: WorkspaceDb, tmp_path: Path) -> DemoEnv:
    return _demo_env(db, tmp_path)


WATCHED = (
    User,
    Workspace,
    Membership,
    UserSession,
    AuditEvent,
    EmailToken,
    AuthThrottle,
    TotpFactor,
    NotificationChannel,
    PriceSourceConfig,
    Invitation,
)


def _row_counts(db: WorkspaceDb) -> tuple[int, ...]:
    with db.factory() as session:
        return tuple(
            int(session.scalar(select(func.count()).select_from(model)) or 0) for model in WATCHED
        )


def _profile_update(env: DemoEnv) -> None:
    profile.update_profile(
        env.db.factory,
        user_id=env.user_id,
        display_name="Changed",
        locale="pl",
        theme="dark",
        today=env.db.clock.today("UTC"),
    )


def _profile_link(env: DemoEnv) -> None:
    with env.db.factory() as session:
        user = session.get(User, env.user_id)
        assert user is not None
        profile.link_identity_to_user(
            session,
            user,
            provider=PROXY_PROVIDER,
            subject="demo-subject",
            email=None,
            confirmed="password",
            clock=env.db.clock,
            host=None,
            user_agent=None,
        )


def _profile_unlink(env: DemoEnv) -> None:
    with env.db.factory() as session:
        user = session.get(User, env.user_id)
        assert user is not None
        profile.unlink_identity(
            session,
            user,
            uuid4(),
            current_session_hash=SESSION_HASH,
            confirmed="password",
            settings=env.settings,
            clock=env.db.clock,
            host=None,
            user_agent=None,
        )


def _password_change(env: DemoEnv) -> None:
    with env.db.factory() as session:
        user = session.get(User, env.user_id)
        assert user is not None
        auth.change_password(
            session,
            user,
            "Old Password",
            "New Password 123",
            settings=env.settings,
            clock=env.db.clock,
        )


def _password_change_factory(env: DemoEnv) -> None:
    auth.change_password_with_factory(
        env.db.factory,
        user_id=env.user_id,
        current_password="Old Password",
        new_password="New Password 123",
        settings=env.settings,
        clock=env.db.clock,
    )


def _password_change_async(env: DemoEnv) -> None:
    asyncio.run(
        auth.change_password_async(
            env.db.factory,
            HashGate(1),
            user_id=env.user_id,
            current_password="Old Password",
            new_password="New Password 123",
            settings=env.settings,
            clock=env.db.clock,
        )
    )


def _workspace_create(env: DemoEnv) -> None:
    workspaces.create_own_workspace(
        env.db.factory,
        user_id=env.user_id,
        session_hash=SESSION_HASH,
        name="Another",
        base_currency="EUR",
        country_code=None,
        timezone="UTC",
        locale="en",
        seed_locale="en",
        clock=env.db.clock,
    )


def _workspace_update(env: DemoEnv) -> None:
    workspaces.update_settings(
        env.db.factory,
        env.ctx,
        name="Renamed",
        base_currency="EUR",
        country_code=None,
        timezone="UTC",
        locale="en",
        auto_copy_plan=False,
    )


def _workspace_delete(env: DemoEnv) -> None:
    workspaces.delete_workspace(
        env.db.factory,
        env.ctx,
        confirm_name="Demo",
        expected_workspace_id=env.workspace_id,
    )


def _workspace_preview(env: DemoEnv) -> None:
    workspaces.deletion_preview(env.db.factory, env.ctx)


def _workspace_change_role(env: DemoEnv) -> None:
    workspaces.change_role(env.db.factory, env.ctx, membership_id=env.membership_id, role="viewer")


def _workspace_remove_member(env: DemoEnv) -> None:
    workspaces.remove_member(env.db.factory, env.ctx, membership_id=env.membership_id)


def _workspace_invite(env: DemoEnv) -> None:
    workspaces.create_invitation(
        env.db.factory, env.ctx, role="viewer", email="new@example.com", expires_in_days=7
    )


def _workspace_revoke(env: DemoEnv) -> None:
    workspaces.revoke_invitation(env.db.factory, env.ctx, invitation_id=uuid4())


def _workspace_menu(env: DemoEnv) -> None:
    taxes.set_taxes_enabled(env.db.factory, env.ctx, True)


def _invitation_accept(env: DemoEnv) -> None:
    invitations.accept_invitation(
        env.db.factory,
        "b" * 43,
        user_id=env.user_id,
        user_email=env.email,
        clock=env.db.clock,
    )


def _notification_create(env: DemoEnv) -> None:
    notifications.create_channel(
        env.db.factory,
        env.ctx,
        env.settings,
        env.notifier,
        ChannelInput(name="Phone", url="ntfys://ntfy.example.com/reminders"),
    )


def _notification_update(env: DemoEnv) -> None:
    notifications.update_channel(
        env.db.factory,
        env.ctx,
        env.settings,
        env.notifier,
        uuid4(),
        ChannelInput(name="Phone", url="ntfys://ntfy.example.com/reminders"),
    )


def _notification_delete(env: DemoEnv) -> None:
    notifications.delete_channel(env.db.factory, env.ctx, uuid4())


def _notification_test(env: DemoEnv) -> None:
    notifications.send_test(env.db.factory, env.ctx, env.settings, env.notifier, uuid4())


def _provider_save(env: DemoEnv) -> None:
    price_sources.save_provider_config(env.db.factory, env.ctx, env.settings, "nbp", enabled=True)


def _provider_refresh_now(env: DemoEnv) -> None:
    price_refresh.refresh_now(env.db.factory, env.ctx, "nbp", env.runtime)


def _provider_lookup(env: DemoEnv) -> None:
    price_refresh.lookup_provider(
        env.db.factory,
        env.ctx,
        "nbp",
        env.runtime,
        type_value="metal",
        quote_currency="EUR",
        unit_label="oz",
        provider_symbol="XAU",
    )


def _provider_refresh(env: DemoEnv) -> None:
    price_refresh.refresh_provider(env.db.factory, env.ctx, "nbp", env.runtime)


def _totp_begin(env: DemoEnv) -> None:
    with env.db.factory() as session:
        user = session.get(User, env.user_id)
        assert user is not None
        totp.begin_enrollment(
            session,
            user,
            session_hash=SESSION_HASH,
            settings=env.settings,
            clock=env.db.clock,
        )


def _totp_complete(env: DemoEnv) -> None:
    totp.complete_enrollment(
        env.db.factory,
        user_id=env.user_id,
        session_hash=SESSION_HASH,
        code="123456",
        settings=env.settings,
        clock=env.db.clock,
    )


def _totp_regenerate(env: DemoEnv) -> None:
    totp.regenerate_recovery_codes(
        env.db.factory,
        user_id=env.user_id,
        session_hash=SESSION_HASH,
        password="Old Password",
        second_factor="123456",
        settings=env.settings,
        clock=env.db.clock,
    )


def _totp_disable(env: DemoEnv) -> None:
    totp.disable(
        env.db.factory,
        user_id=env.user_id,
        session_hash=SESSION_HASH,
        password="Old Password",
        second_factor="123456",
        settings=env.settings,
        clock=env.db.clock,
    )


CASES: list[tuple[str, Callable[[DemoEnv], Any]]] = [
    ("profile.update_profile", _profile_update),
    ("profile.link_identity_to_user", _profile_link),
    ("profile.unlink_identity", _profile_unlink),
    ("auth.change_password", _password_change),
    ("auth.change_password_with_factory", _password_change_factory),
    ("auth.change_password_async", _password_change_async),
    ("workspaces.create_own_workspace", _workspace_create),
    ("workspaces.update_settings", _workspace_update),
    ("workspaces.delete_workspace", _workspace_delete),
    ("workspaces.deletion_preview", _workspace_preview),
    ("workspaces.change_role", _workspace_change_role),
    ("workspaces.remove_member", _workspace_remove_member),
    ("workspaces.create_invitation", _workspace_invite),
    ("workspaces.revoke_invitation", _workspace_revoke),
    ("taxes.set_taxes_enabled", _workspace_menu),
    ("invitations.accept_invitation", _invitation_accept),
    ("notifications.create_channel", _notification_create),
    ("notifications.update_channel", _notification_update),
    ("notifications.delete_channel", _notification_delete),
    ("notifications.send_test", _notification_test),
    ("price_sources.save_provider_config", _provider_save),
    ("price_refresh.refresh_now", _provider_refresh_now),
    ("price_refresh.lookup_provider", _provider_lookup),
    ("price_refresh.refresh_provider", _provider_refresh),
    ("totp.begin_enrollment", _totp_begin),
    ("totp.complete_enrollment", _totp_complete),
    ("totp.regenerate_recovery_codes", _totp_regenerate),
    ("totp.disable", _totp_disable),
]


@pytest.mark.parametrize(("name", "call"), CASES, ids=[name for name, _call in CASES])
def test_every_restricted_service_refuses_the_demo_without_writing(
    env: DemoEnv, name: str, call: Callable[[DemoEnv], Any]
) -> None:
    before = _row_counts(env.db)
    with pytest.raises(DemoRestricted):
        call(env)
    assert _row_counts(env.db) == before, f"{name} wrote something before refusing"

    if name.startswith("notifications."):
        assert env.notifier.sent == []
    if name.startswith("price_") or name.startswith("price_refresh."):
        fake_http = env.runtime.http.client
        assert fake_http.calls == []  # type: ignore[attr-defined]


def test_the_mail_resolver_never_sends_or_issues_a_link_for_the_demo_user(
    env: DemoEnv,
) -> None:
    outcome = account_email.request_password_reset(
        env.db.factory,
        email=env.email,
        settings=env.settings,
        clock=env.db.clock,
        host="192.0.2.1",
        locale="en",
        log_key=b"test-key",
    )
    assert outcome.status == "accepted"
    assert outcome.request is not None
    assert (
        account_email.resolve_mail_request(
            env.db.factory,
            env.settings,
            env.db.clock,
            outcome.request,
            log_key=b"test-key",
        )
        is None
    )

    registration = MailRequest("register", env.email, "en")
    assert (
        account_email.resolve_mail_request(
            env.db.factory, env.settings, env.db.clock, registration, log_key=b"test-key"
        )
        is None
    )
    with env.db.factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(EmailToken).where(EmailToken.email == env.email)
            )
            or 0
        ) == 0


def test_a_demo_reset_link_is_refused_by_the_target_and_the_apply_step(
    env: DemoEnv,
) -> None:
    token = "c" * 43
    with env.db.factory.begin() as session:
        session.add(
            EmailToken(
                id=uuid4(),
                purpose="reset",
                token_hash=hashlib.sha256(token.encode("ascii")).hexdigest(),
                email=env.email,
                user_id=env.user_id,
                locale="en",
                created_at=env.db.clock.now(),
                expires_at=env.db.clock.now().replace(year=env.db.clock.now().year + 1),
            )
        )

    assert account_email.reset_target(env.db.factory, token, env.db.clock) is None
    outcome = account_email.reset_password(
        env.db.factory,
        token=token,
        password="New Password 123",
        confirm_password="New Password 123",
        settings=env.settings,
        clock=env.db.clock,
        host="192.0.2.1",
        user_agent="test",
    )
    assert outcome.status == "link_invalid"
    with env.db.factory() as session:
        user = session.get(User, env.user_id)
        assert user is not None and user.password_hash is None


def test_a_demo_password_cannot_sign_in_even_if_a_hash_is_set(env: DemoEnv) -> None:
    with env.db.factory.begin() as session:
        user = session.get(User, env.user_id)
        assert user is not None
        user.password_hash = argon2_hash("Demo Password 123")

    with env.db.factory.begin() as session:
        outcome = auth.authenticate_user(
            session,
            email=env.email,
            password="Demo Password 123",
            settings=env.settings,
            clock=env.db.clock,
            host="192.0.2.1",
        )
    assert not outcome.succeeded
    with env.db.factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(UserSession)
                .where(UserSession.user_id == env.user_id)
            )
            or 0
        ) == 0


def test_oidc_linking_and_sign_in_refuse_the_demo_user(env: DemoEnv) -> None:
    pending = PendingLink(
        user_id=env.user_id,
        issuer=ISSUER,
        subject="demo-subject",
        email=env.email,
        admin=None,
        next_path="/",
    )
    with pytest.raises(DemoRestricted):
        link_identity(
            env.db.factory,
            pending,
            "Demo Password 123",
            settings=env.settings,
            clock=env.db.clock,
            host=None,
        )

    identity = SignInIdentity(
        issuer=ISSUER,
        subject="demo-subject",
        email=env.email,
        email_verified=True,
        display_name="Demo",
        groups=None,
    )
    with pytest.raises(DemoRestricted):
        sign_in_external(
            env.db.factory,
            identity,
            next_path="/",
            settings=env.settings,
            clock=env.db.clock,
            host=None,
        )
    with env.db.factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ExternalIdentity)
                .where(ExternalIdentity.user_id == env.user_id)
            )
            or 0
        ) == 0


def test_an_invitation_to_the_demo_workspace_never_adds_a_real_member(
    db: WorkspaceDb, env: DemoEnv
) -> None:
    db.add_member("real", "owner")
    real_id = db.users["real"]
    token, token_hash = invitations.new_token()
    with db.factory.begin() as session:
        session.add(
            Invitation(
                workspace_id=env.workspace_id,
                token_hash=token_hash,
                role="viewer",
                email=REAL_EMAIL,
                expires_at=db.clock.now().replace(year=db.clock.now().year + 1),
                created_by_user_id=env.user_id,
            )
        )
        before = session.scalar(
            select(func.count())
            .select_from(Membership)
            .where(Membership.workspace_id == env.workspace_id)
        )

    with pytest.raises(DemoRestricted):
        invitations.accept_invitation(
            db.factory,
            token,
            user_id=real_id,
            user_email=REAL_EMAIL,
            clock=db.clock,
        )
    with pytest.raises(DemoRestricted), db.factory.begin() as session:
        invitations.consume_for_new_user(
            session, token, user_id=real_id, email=REAL_EMAIL, now=db.clock.now()
        )

    with db.factory() as session:
        after = session.scalar(
            select(func.count())
            .select_from(Membership)
            .where(Membership.workspace_id == env.workspace_id)
        )
        row = session.scalar(select(Invitation).where(Invitation.token_hash == token_hash))
    assert after == before
    assert row is not None and row.used_at is None


def test_the_scheduler_helpers_skip_the_demo_workspace(env: DemoEnv) -> None:
    """The scheduler-level skip for providers and reminders, independent of the UI."""
    with env.db.factory.begin() as session:
        session.add(
            PriceSourceConfig(
                workspace_id=env.workspace_id,
                provider_key="nbp",
                enabled=True,
            )
        )

    price_refresh.refresh_all(env.db.factory, "nbp", env.runtime, logging.getLogger("test"))
    fake_http = env.runtime.http.client
    assert fake_http.calls == []  # type: ignore[attr-defined]

    stats = notifications.run_workspace(
        env.db.factory, env.ctx, env.db.clock, env.settings, env.notifier
    )
    assert stats.channels == 0
    assert env.notifier.sent == []


def test_an_instance_admin_cannot_act_on_the_demo_user(db: WorkspaceDb, env: DemoEnv) -> None:
    with db.factory.begin() as session:
        admin = User(
            email="admin@example.com",
            display_name="Admin",
            password_hash=None,
            is_instance_admin=True,
        )
        session.add(admin)
        session.flush()
        admin_id = admin.id

    for action in ("activate", "deactivate", "promote", "demote", "reset_totp"):
        with db.factory.begin() as session:
            actor = session.get(User, admin_id)
            assert actor is not None
            assert (
                apply_admin_action(
                    session,
                    actor,
                    env.user_id,
                    action,  # type: ignore[arg-type]
                    clock=db.clock,
                    host=None,
                    user_agent=None,
                )
                == "not_found"
            )

    with db.factory() as session:
        demo = session.get(User, env.user_id)
        assert demo is not None and demo.is_active and not demo.is_instance_admin
