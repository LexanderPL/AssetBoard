from __future__ import annotations

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.engine import run_immediate
from assetboard.db.models.identity import AuditEvent, LoginTicket, User, UserSession, Workspace
from assetboard.db.models.ledger import Transaction
from assetboard.db.models.totp import TotpFactor, TotpRecoveryCode
from assetboard.db.repositories.totp import TotpRepository
from assetboard.domain.totp import recovery_hash
from assetboard.services import auth, totp, workspaces
from assetboard.services.auth import read_web_session
from assetboard.services.context import (
    PermissionDenied,
    Role,
    TwoFactorRequired,
    build_context,
    require_role_now,
)
from assetboard.services.oidc_accounts import PendingLink, link_identity
from assetboard.web.security import session_cookie_name

if TYPE_CHECKING:
    from tests.conftest import OwnerApp


# Shown grouped as xxxx-xxxx-…; sign-in and hashing ignore the dashes.
CODE_PATTERN = r"<li>((?:[a-f0-9]{4}-){7}[a-f0-9]{4})</li>"


def _begin(owner: OwnerApp) -> str:
    if not isinstance(owner.app.state.clock, FixedClock):
        owner.app.state.clock = FixedClock("2026-10-21T12:00:00+00:00")
    response = owner.post("/account/totp/start", {"confirm_password": owner.web.admin_password})
    assert response.status_code == 303, response.text
    page = owner.client.get("/account/totp")
    assert "<svg" in page.text
    match = re.search(r"<code[^>]*data-totp-secret>([A-Z2-7]+)</code>", page.text)
    assert match
    return match.group(1)


def _enable(owner: OwnerApp) -> tuple[str, list[str]]:
    secret = _begin(owner)
    response = owner.post(
        "/account/totp/verify",
        {"code": pyotp.TOTP(secret).at(owner.app.state.clock.now())},
        token_from="/account/totp",
    )
    assert response.status_code == 200, response.text
    codes = re.findall(CODE_PATTERN, response.text)
    assert len(codes) == 10
    return secret, codes


def _login(owner: OwnerApp, client: TestClient, code: str = "", *, path: str = "/login"):
    first = client.post(
        path,
        data={
            "csrf_token": owner.web.csrf(client.get(path).text),
            "email": owner.web.admin_email,
            "password": owner.web.admin_password,
        },
        follow_redirects=False,
    )
    challenge_path = "/login/local/verify" if path == "/login/local" else "/login/verify"
    if first.status_code != 303 or first.headers.get("location") != challenge_path:
        return first
    challenge = client.get(challenge_path)
    return client.post(
        challenge_path,
        data={
            "csrf_token": owner.web.csrf(challenge.text),
            "second_factor": code,
        },
        follow_redirects=False,
    )


def _context(owner: OwnerApp, client: TestClient | None = None):
    app = owner.app.state
    with app.session_factory() as session:
        current = read_web_session(
            session,
            (client or owner.client).cookies[session_cookie_name(app.settings)],
            settings=app.settings,
            clock=app.clock,
        )
        assert current is not None
        return build_context(session, current, clock=app.clock)


def test_enrollment_requires_password_and_code_and_shows_recovery_codes_once(
    owner: OwnerApp,
) -> None:
    disabled = owner.post("/account/totp/disable", {"confirm_password": owner.web.admin_password})
    assert disabled.status_code == 403
    with owner.db() as session:
        assert not session.scalar(
            select(AuditEvent.id).where(AuditEvent.action == "auth.totp_disabled")
        )

    assert owner.post("/account/totp/start", {"confirm_password": "wrong"}).status_code == 401
    secret = _begin(owner)
    with owner.db() as session:
        factor = session.get(TotpFactor, owner.admin_id)
        assert factor is not None and factor.enabled_at is None
        assert secret not in factor.encrypted_secret
    assert owner.post("/account/totp/verify", {"code": "invalid"}).status_code == 400
    response = owner.post(
        "/account/totp/verify", {"code": pyotp.TOTP(secret).at(owner.app.state.clock.now())}
    )
    codes = re.findall(CODE_PATTERN, response.text)
    assert len(set(codes)) == 10
    with owner.db() as session:
        stored = list(session.scalars(select(TotpRecoveryCode.code_hash)))
        assert set(stored) == {recovery_hash(code) for code in codes}
        assert all(code not in stored for code in codes)
        factor = session.get(TotpFactor, owner.admin_id)
        assert factor is not None and factor.enabled_at is not None
        assert factor.enrollment_session_hash is None
    page = owner.client.get("/account/totp")
    assert page.headers["cache-control"] == "no-store"
    assert all(code not in page.text for code in codes)
    assert secret not in page.text and "<svg" not in page.text
    assert owner.post("/account/totp/verify", {"code": "123456"}).status_code == 400


def test_recovery_codes_can_be_regenerated_only_with_password_and_factor(owner: OwnerApp) -> None:
    secret, old_codes = _enable(owner)
    wrong = owner.post(
        "/account/totp/recovery-codes",
        {
            "confirm_password": owner.web.admin_password,
            "second_factor": "",
        },
    )
    assert wrong.status_code == 401
    with owner.db() as session:
        assert set(session.scalars(select(TotpRecoveryCode.code_hash))) == {
            recovery_hash(code) for code in old_codes
        }

    owner.app.state.clock = FixedClock("2026-10-21T12:00:30+00:00")
    response = owner.post(
        "/account/totp/recovery-codes",
        {
            "confirm_password": owner.web.admin_password,
            "second_factor": pyotp.TOTP(secret).at(owner.app.state.clock.now()),
        },
    )
    assert response.status_code == 200
    new_codes = re.findall(CODE_PATTERN, response.text)
    assert len(set(new_codes)) == 10
    assert not set(new_codes) & set(old_codes)
    with owner.db() as session:
        assert set(session.scalars(select(TotpRecoveryCode.code_hash))) == {
            recovery_hash(code) for code in new_codes
        }
        event = session.scalar(
            select(AuditEvent).where(AuditEvent.action == "auth.totp_recovery_codes_regenerated")
        )
        assert event is not None and event.data == {}
    page = owner.client.get("/account/totp")
    assert all(code not in page.text for code in new_codes)
    assert all(code not in str(event.data) for code in new_codes)


def test_password_login_requires_factor_prevents_replay_and_consumes_recovery_once(
    owner: OwnerApp,
) -> None:
    secret, codes = _enable(owner)
    client = TestClient(owner.app)
    failed = _login(owner, client)
    assert failed.status_code == 401
    assert (
        "set-cookie" not in failed.headers
        or "assetboard_session=" not in failed.headers["set-cookie"]
    )
    enrollment_code = pyotp.TOTP(secret).at(owner.app.state.clock.now())
    assert _login(owner, client, enrollment_code).status_code == 401
    owner.app.state.clock = FixedClock("2026-10-21T12:00:30+00:00")
    code = pyotp.TOTP(secret).at(owner.app.state.clock.now())
    assert _login(owner, client, code).status_code == 303
    other = TestClient(owner.app)
    assert _login(owner, other, code).status_code == 401
    assert _login(owner, other, codes[0]).status_code == 303
    assert _login(owner, TestClient(owner.app), codes[0]).status_code == 401
    with owner.db() as session:
        assert len(list(session.scalars(select(TotpRecoveryCode)))) == 9


def test_password_and_factor_are_separate_steps_with_a_hashed_single_use_ticket(
    owner: OwnerApp,
) -> None:
    secret, _codes = _enable(owner)
    client = TestClient(owner.app)
    assert client.get("/login/verify", follow_redirects=False).headers["location"] == "/login"
    with owner.db() as session:
        existing_sessions = len(list(session.scalars(select(UserSession))))
    login_page = client.get("/login")
    assert 'name="second_factor"' not in login_page.text
    first = client.post(
        "/login",
        data={
            "csrf_token": owner.web.csrf(login_page.text),
            "email": owner.web.admin_email,
            "password": owner.web.admin_password,
        },
        follow_redirects=False,
    )
    assert first.status_code == 303 and first.headers["location"] == "/login/verify"
    assert "Max-Age=300" in first.headers["set-cookie"]
    assert "httponly" in first.headers["set-cookie"].lower()
    assert "samesite=lax" in first.headers["set-cookie"].lower()
    ticket_cookie = "assetboard_login_ticket"
    ticket = client.cookies[ticket_cookie]
    assert "assetboard_session" not in first.headers.get("set-cookie", "")
    challenge = client.get("/login/verify")
    assert "Enter the code from your authenticator app" in challenge.text
    assert 'name="second_factor"' in challenge.text
    with owner.db() as session:
        rows = list(session.scalars(select(LoginTicket)))
        assert len(rows) == 1 and rows[0].token_hash != ticket
        assert rows[0].token_hash == hashlib.sha256(ticket.encode()).hexdigest()
        assert rows[0].expires_at == owner.app.state.clock.now() + timedelta(minutes=5)

    bad = client.post(
        "/login/verify",
        data={"csrf_token": owner.web.csrf(challenge.text), "second_factor": "wrong"},
        follow_redirects=False,
    )
    assert bad.status_code == 401
    assert client.cookies.get(ticket_cookie) == ticket
    with owner.db() as session:
        assert session.scalar(select(LoginTicket)) is not None
        assert len(list(session.scalars(select(UserSession)))) == existing_sessions

    owner.app.state.clock = FixedClock("2026-10-21T12:00:30+00:00")
    retry_page = client.get("/login/verify")
    success = client.post(
        "/login/verify",
        data={
            "csrf_token": owner.web.csrf(retry_page.text),
            "second_factor": pyotp.TOTP(secret).at(owner.app.state.clock.now()),
        },
        follow_redirects=False,
    )
    assert success.status_code == 303
    assert client.cookies.get(ticket_cookie) is None
    with owner.db() as session:
        assert session.scalar(select(LoginTicket)) is None


def test_code_failures_are_throttled_on_login_and_enrollment(owner: OwnerApp) -> None:
    _begin(owner)
    for _ in range(6):
        assert owner.post("/account/totp/verify", {"code": "invalid"}).status_code == 400
    assert (
        owner.post(
            "/account/totp/start", {"confirm_password": owner.web.admin_password}
        ).status_code
        == 303
    )
    response = owner.post("/account/totp/verify", {"code": "invalid"})
    assert response.status_code == 401 and response.headers["retry-after"] == "60"
    owner.app.state.clock = FixedClock("2026-10-21T13:00:00+00:00")
    # A password and a failed second factor share the existing sign-in limits.
    _, codes = _enable(owner)
    client = TestClient(owner.app)
    for _ in range(6):
        assert _login(owner, client, "invalid").status_code == 401
    response = _login(owner, client, codes[0])
    assert response.status_code == 401 and response.headers["retry-after"] == "60"


def test_pending_enrollment_is_session_bound_and_expires(owner: OwnerApp) -> None:
    other = TestClient(owner.app)
    assert owner.web.sign_in(other).status_code == 303
    secret = _begin(owner)
    assert secret not in other.get("/account/totp").text
    response = other.post(
        "/account/totp/verify",
        data={
            "code": pyotp.TOTP(secret).at(owner.app.state.clock.now()),
            "csrf_token": owner.web.csrf(other.get("/account/totp").text),
        },
    )
    assert response.status_code == 400
    owner.app.state.clock = FixedClock("2026-10-21T12:10:00+00:00")
    assert secret not in owner.client.get("/account/totp").text
    assert (
        owner.post(
            "/account/totp/verify", {"code": pyotp.TOTP(secret).at(owner.app.state.clock.now())}
        ).status_code
        == 400
    )


def test_enable_and_disable_revoke_other_password_sessions(owner: OwnerApp) -> None:
    other = TestClient(owner.app)
    assert owner.web.sign_in(other).status_code == 303
    _, codes = _enable(owner)
    assert other.get("/account", follow_redirects=False).status_code == 303
    assert _login(owner, other, codes[0]).status_code == 303
    assert (
        owner.post(
            "/account/totp/disable", {"confirm_password": "wrong", "second_factor": codes[1]}
        ).status_code
        == 401
    )
    # The password alone is not enough: a current code or a recovery code is required too.
    assert (
        owner.post(
            "/account/totp/disable", {"confirm_password": owner.web.admin_password}
        ).status_code
        == 401
    )
    assert (
        owner.post(
            "/account/totp/disable",
            {"confirm_password": owner.web.admin_password, "second_factor": codes[1]},
        ).status_code
        == 303
    )
    assert other.get("/account", follow_redirects=False).status_code == 303
    assert owner.client.get("/").status_code == 200
    with owner.db() as session:
        assert session.get(TotpFactor, owner.admin_id) is None
        assert list(session.scalars(select(TotpRecoveryCode))) == []
        assert list(
            session.scalars(
                select(AuditEvent.action).where(
                    AuditEvent.action.in_(["auth.totp_enabled", "auth.totp_disabled"])
                )
            )
        ) == ["auth.totp_enabled", "auth.totp_disabled"]


def _policy_form() -> dict[str, str]:
    return {
        "name": "Home",
        "base_currency": "EUR",
        "timezone": "UTC",
        "locale": "en",
        "require_two_factor": "1",
    }


def _set_policy(owner: OwnerApp, workspace_id, required: bool) -> None:  # noqa: ANN001
    with owner.app.state.session_factory.begin() as session:
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None
        workspace.require_two_factor = required


def test_workspace_policy_is_scoped_owner_only_and_blocks_existing_local_sessions(
    owner: OwnerApp,
) -> None:
    ctx = _context(owner)
    other_workspace = owner.add_workspace("Other")
    member_id = owner.add_user("member")
    owner.join(member_id, owner.home_id, "editor")
    owner.join(member_id, other_workspace, "owner")
    member = TestClient(owner.app)
    assert (
        owner.web.sign_in(
            member, email="member@example.com", password=owner.web.admin_password
        ).status_code
        == 303
    )
    member_ctx = _context(owner, member)
    with pytest.raises(PermissionDenied):
        workspaces.update_settings(
            owner.app.state.session_factory,
            replace(ctx, role=Role.EDITOR),
            name="Home",
            base_currency="EUR",
            country_code=None,
            timezone="UTC",
            locale="en",
            auto_copy_plan=False,
            require_two_factor=True,
        )
    # The owner enables their own factor first (see the self-lockout test), then the policy.
    secret, _codes = _enable(owner)
    assert owner.post("/workspace/settings", _policy_form()).status_code == 303
    assert owner.client.get("/").status_code == 200
    # An existing password session of a member without a factor is blocked at once.
    assert member.get("/", follow_redirects=False).headers["location"] == "/account/totp"
    assert "Other" in member.get("/account/totp").text
    blocked = member.get("/", headers={"HX-Request": "true"}, follow_redirects=False)
    assert blocked.status_code == 204 and blocked.headers["hx-redirect"] == "/account/totp"
    with owner.db() as session:
        other = session.get(Workspace, other_workspace)
        assert other is not None and other.require_two_factor is False
        with pytest.raises(TwoFactorRequired):
            require_role_now(session, member_ctx, Role.EDITOR)
    # Switching away leaves the unrelated workspace accessible.
    member_token = owner.web.csrf(member.get("/account/totp").text)
    assert (
        member.post(
            "/workspace/switch",
            data={"workspace_id": str(other_workspace), "csrf_token": member_token},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert member.get("/").status_code == 200
    # Disabling the owner's own factor puts the owner behind the policy too.
    owner.app.state.clock = FixedClock("2026-10-21T12:00:30+00:00")
    owner.post(
        "/account/totp/disable",
        {
            "confirm_password": owner.web.admin_password,
            "second_factor": pyotp.TOTP(secret).at(owner.app.state.clock.now()),
        },
    )
    assert owner.client.get("/", follow_redirects=False).headers["location"] == "/account/totp"


def test_owner_cannot_require_two_factor_without_their_own_verified_factor(
    owner: OwnerApp,
) -> None:
    response = owner.post("/workspace/settings", _policy_form())
    assert response.status_code == 400
    assert "Enable two-factor authentication for your own account" in response.text
    assert 'id="require_two_factor-error"' in response.text
    with owner.db() as session:
        workspace = session.get(Workspace, owner.home_id)
        assert workspace is not None and workspace.require_two_factor is False
    assert owner.client.get("/").status_code == 200  # not locked out

    ctx = _context(owner)
    arguments = {
        "name": "Home",
        "base_currency": "EUR",
        "country_code": None,
        "timezone": "UTC",
        "locale": "en",
        "auto_copy_plan": False,
        "require_two_factor": True,
    }
    factory = owner.app.state.session_factory
    with pytest.raises(workspaces.InvalidWorkspaceSettings) as refused:
        workspaces.update_settings(factory, ctx, **arguments)
    assert (refused.value.field, refused.value.code) == (
        "require_two_factor",
        "own_two_factor_required",
    )
    # External sessions are exempt from the policy, so their owner may turn it on.
    for method in ("oidc", "proxy"):
        _set_policy(owner, owner.home_id, False)
        external = replace(ctx, auth_method=method, second_factor_verified=False)
        assert workspaces.update_settings(factory, external, **arguments).require_two_factor
    # A password session with a verified factor may, too.
    _set_policy(owner, owner.home_id, False)
    verified = replace(ctx, second_factor_verified=True)
    assert workspaces.update_settings(factory, verified, **arguments).require_two_factor


def test_policy_turned_on_between_requests_gates_writes_htmx_and_export(
    owner: OwnerApp,
) -> None:
    month = "/m/2026-10"
    token = owner.token(month)
    ctx = _context(owner)  # built while the policy is still off
    _set_policy(owner, owner.home_id, True)
    form = {"csrf_token": token, "direction": "expense", "day": "2026-10-05", "amount": "12.00"}

    write = owner.client.post(f"{month}/transactions", data=form, follow_redirects=False)
    assert write.status_code == 303 and write.headers["location"] == "/account/totp"
    htmx = owner.client.post(
        f"{month}/transactions",
        data=form,
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert htmx.status_code == 204 and htmx.headers["hx-redirect"] == "/account/totp"
    export = owner.client.get("/transactions/export.csv", follow_redirects=False)
    assert export.status_code == 303 and export.headers["location"] == "/account/totp"
    assert "text/csv" not in export.headers.get("content-type", "")
    # A Context built before the change still meets the write guard's fresh read.
    with pytest.raises(TwoFactorRequired):
        workspaces.update_settings(
            owner.app.state.session_factory,
            ctx,
            name="Renamed",
            base_currency="EUR",
            country_code=None,
            timezone="UTC",
            locale="en",
            auto_copy_plan=False,
        )
    with owner.db() as session:
        assert session.scalar(select(Transaction.id)) is None
        workspace = session.get(Workspace, owner.home_id)
        assert workspace is not None and workspace.name != "Renamed"
        assert workspace.require_two_factor is True


@pytest.mark.parametrize("method", ["oidc", "proxy"])
def test_external_sessions_skip_local_factor_and_workspace_requirement(
    owner: OwnerApp, method: str
) -> None:
    _enable(owner)
    with owner.app.state.session_factory.begin() as session:
        workspace = session.get(Workspace, owner.home_id)
        workspace.require_two_factor = True
        current = read_web_session(
            session,
            owner.client.cookies[session_cookie_name(owner.app.state.settings)],
            settings=owner.app.state.settings,
            clock=owner.app.state.clock,
        )
        assert current is not None
        external = replace(current, auth_method=method, second_factor_verified=False)
        assert (
            build_context(session, external, clock=owner.app.state.clock).workspace.id
            == owner.home_id
        )


def test_break_glass_local_login_cannot_skip_totp(owner: OwnerApp) -> None:
    _, codes = _enable(owner)
    settings = owner.app.state.settings
    settings.oidc_only = True
    settings.allow_local_admin_login = True
    client = TestClient(owner.app)
    assert _login(owner, client, path="/login/local").status_code == 401
    assert _login(owner, client, codes[0], path="/login/local").status_code == 303


def test_recovery_code_is_consumed_atomically_and_scoped_to_its_user(owner: OwnerApp) -> None:
    _, codes = _enable(owner)
    other_id = owner.add_user("other")
    with owner.db() as session:
        assert not TotpRepository(session, other_id).consume_recovery_code(recovery_hash(codes[0]))

    def attempt() -> bool:
        return run_immediate(
            owner.app.state.session_factory,
            lambda session: totp.verify_sign_in(
                session,
                owner.admin_id,
                code=codes[0],
                settings=owner.app.state.settings,
                now=owner.app.state.clock.now(),
            )[0],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: attempt(), range(2))) == [False, True]


def test_every_totp_post_requires_csrf(owner: OwnerApp) -> None:
    for path in ("start", "verify", "disable", "recovery-codes"):
        assert owner.client.post(f"/account/totp/{path}", data={}).status_code == 403


def test_password_confirmed_identity_linking_cannot_bypass_totp(owner: OwnerApp) -> None:
    _, codes = _enable(owner)
    settings = owner.app.state.settings
    settings.proxy_auth_enabled = True
    settings.proxy_auth_link_by_email = True
    pending = PendingLink(
        user_id=owner.admin_id,
        issuer="proxy",
        subject="new-identity",
        email=owner.web.admin_email,
        admin=None,
        next_path="/",
    )
    kwargs = {"settings": settings, "clock": owner.app.state.clock, "host": "192.0.2.10"}
    assert (
        link_identity(
            owner.app.state.session_factory, pending, owner.web.admin_password, **kwargs
        ).status
        == "failed"
    )
    assert (
        link_identity(
            owner.app.state.session_factory,
            pending,
            owner.web.admin_password,
            second_factor=codes[0],
            **kwargs,
        ).status
        == "linked"
    )


def test_recovery_still_works_if_the_instance_key_changes(owner: OwnerApp) -> None:
    secret, codes = _enable(owner)
    owner.app.state.settings.secret_key = "replacement-instance-secret"
    client = TestClient(owner.app)
    owner.app.state.clock = FixedClock("2026-10-21T12:00:30+00:00")
    assert (
        _login(owner, client, pyotp.TOTP(secret).at(owner.app.state.clock.now())).status_code == 401
    )
    assert _login(owner, client, codes[0]).status_code == 303


def test_totp_messages_are_translated_and_secrets_are_not_logged(
    owner: OwnerApp, caplog: pytest.LogCaptureFixture
) -> None:
    owner.client.headers["accept-language"] = "pl"
    page = owner.client.get("/account/totp")
    assert "Uwierzytelnianie dwuskładnikowe" in page.text
    assert "Skonfiguruj uwierzytelnianie dwuskładnikowe" in page.text
    with caplog.at_level("INFO"):
        secret, codes = _enable(owner)
    assert secret not in caplog.text
    assert all(code not in caplog.text for code in codes)


def test_instance_admin_can_reset_another_users_factor(owner: OwnerApp) -> None:
    admin_secret, _admin_recovery_codes = _enable(owner)
    target_id = owner.add_user("mfa-user")
    target_email = "mfa-user@example.com"
    target_client = TestClient(owner.app)
    assert (
        owner.web.sign_in(
            target_client, email=target_email, password=owner.web.admin_password
        ).status_code
        == 303
    )
    secret = pyotp.random_base32()
    with owner.app.state.session_factory.begin() as session:
        session.add(
            TotpFactor(
                user_id=target_id,
                encrypted_secret=totp.encrypt_secret(
                    owner.app.state.settings, target_id, secret, owner.app.state.clock.now()
                ),
                enabled_at=owner.app.state.clock.now(),
                last_step=-1,
            )
        )
        session.add(TotpRecoveryCode(user_id=target_id, code_hash=recovery_hash("recovery")))

    users_page = owner.client.get("/admin/users")
    assert "Two-factor authentication enabled" in users_page.text
    assert f"/admin/users/{target_id}/reset_totp" in users_page.text
    confirm_page = owner.client.get(f"/admin/users/{target_id}/reset_totp")
    missing_factor = owner.client.post(
        f"/admin/users/{target_id}/reset_totp",
        data={
            "csrf_token": owner.web.csrf(confirm_page.text),
            "confirm_password": owner.web.admin_password,
        },
        follow_redirects=False,
    )
    assert missing_factor.status_code == 401
    with owner.db() as session:
        assert session.get(TotpFactor, target_id) is not None

    owner.app.state.clock = FixedClock("2026-10-21T12:00:30+00:00")
    confirm_page = owner.client.get(f"/admin/users/{target_id}/reset_totp")
    response = owner.client.post(
        f"/admin/users/{target_id}/reset_totp",
        data={
            "csrf_token": owner.web.csrf(confirm_page.text),
            "confirm_password": owner.web.admin_password,
            "second_factor": pyotp.TOTP(admin_secret).at(owner.app.state.clock.now()),
        },
        follow_redirects=False,
    )
    assert response.status_code == 200 and "data-admin-notice" in response.text
    assert target_client.get("/account", follow_redirects=False).status_code == 303
    with owner.db() as session:
        assert session.get(TotpFactor, target_id) is None
        assert (
            list(
                session.scalars(
                    select(TotpRecoveryCode).where(TotpRecoveryCode.user_id == target_id)
                )
            )
            == []
        )
        event = session.scalar(select(AuditEvent).where(AuditEvent.action == "admin.totp_reset"))
        assert event is not None
        assert event.entity_id == str(target_id)
        assert event.actor_user_id == owner.admin_id
        assert event.data == {"sessions_revoked": 1}


def _current(owner: OwnerApp):
    app = owner.app.state
    with app.session_factory() as session:
        current = read_web_session(
            session,
            owner.client.cookies[session_cookie_name(app.settings)],
            settings=app.settings,
            clock=app.clock,
        )
        assert current is not None
        return current


def _service_kwargs(owner: OwnerApp, second_factor: str, password: str | None = None):
    app = owner.app.state
    current = _current(owner)
    return {
        "user_id": current.user_id,
        "session_hash": current.session_hash,
        "password": owner.web.admin_password if password is None else password,
        "second_factor": second_factor,
        "settings": app.settings,
        "clock": app.clock,
    }


def _confirm_failures(owner: OwnerApp, purpose: str) -> int:
    with owner.db() as session:
        return sum(
            1
            for event in session.scalars(
                select(AuditEvent).where(AuditEvent.action == "auth.password_confirm_failure")
            )
            if event.data == {"purpose": purpose}
        )


def test_disable_service_requires_a_second_factor_with_the_password(owner: OwnerApp) -> None:
    secret, codes = _enable(owner)
    factory = owner.app.state.session_factory
    for wrong in ("", "000000", "not-a-code"):
        outcome = totp.disable(factory, **_service_kwargs(owner, wrong))
        assert outcome.status == "failed"
    # A wrong second factor counts as a failed confirmation, like a wrong password.
    assert _confirm_failures(owner, "totp_disable") == 3
    assert totp.disable(factory, **_service_kwargs(owner, codes[0], "wrong")).status == "failed"
    with owner.db() as session:
        assert session.get(TotpFactor, owner.admin_id) is not None
        assert len(list(session.scalars(select(TotpRecoveryCode)))) == 10

    owner.app.state.clock = FixedClock("2026-10-21T12:00:30+00:00")
    code = pyotp.TOTP(secret).at(owner.app.state.clock.now())
    outcome = totp.disable(factory, **_service_kwargs(owner, code))
    assert outcome.status == "confirmed" and outcome.value is True
    with owner.db() as session:
        assert session.get(TotpFactor, owner.admin_id) is None
        assert list(session.scalars(select(TotpRecoveryCode))) == []


def test_disable_service_accepts_an_unused_recovery_code_once(owner: OwnerApp) -> None:
    _, codes = _enable(owner)
    factory = owner.app.state.session_factory
    outcome = totp.disable(factory, **_service_kwargs(owner, codes[3].upper()))
    assert outcome.status == "confirmed" and outcome.value is True
    # Nothing left to disable: the confirmation passes (no factor) but the action refuses.
    assert totp.disable(factory, **_service_kwargs(owner, codes[3])).value is False


def test_regenerate_service_requires_a_second_factor_with_the_password(owner: OwnerApp) -> None:
    _, codes = _enable(owner)
    factory = owner.app.state.session_factory
    before = {recovery_hash(code) for code in codes}
    outcome = totp.regenerate_recovery_codes(factory, **_service_kwargs(owner, ""))
    assert outcome.status == "failed" and outcome.value is None
    assert _confirm_failures(owner, "totp_recovery_codes") == 1
    with owner.db() as session:
        assert set(session.scalars(select(TotpRecoveryCode.code_hash))) == before

    outcome = totp.regenerate_recovery_codes(factory, **_service_kwargs(owner, codes[0]))
    assert outcome.status == "confirmed" and outcome.value is not None
    assert len(set(outcome.value)) == 10
    with owner.db() as session:
        stored = set(session.scalars(select(TotpRecoveryCode.code_hash)))
    assert stored == {recovery_hash(code) for code in outcome.value}
    assert not stored & before
    # The code that confirmed is gone with the rest of the old set.
    assert (
        totp.regenerate_recovery_codes(factory, **_service_kwargs(owner, codes[1])).status
        == "failed"
    )


def test_password_rehash_is_applied_only_after_a_correct_code(
    owner: OwnerApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret, _codes = _enable(owner)
    app = owner.app.state
    with owner.db() as session:
        stored = session.get(User, owner.admin_id)
        assert stored is not None
        before = stored.password_hash
    hashed: list[str] = []

    def spy(password: str) -> str:
        hashed.append(password)
        return auth.argon2_hash(password)

    real_hasher = auth.password_hasher()

    class NeedsRehash:
        def verify(self, encoded: str, password: str) -> bool:
            return real_hasher.verify(encoded, password)

        def check_needs_rehash(self, _encoded: str) -> bool:
            return True

        def hash(self, password: str) -> str:
            return real_hasher.hash(password)

    monkeypatch.setattr(auth, "password_hasher", lambda: NeedsRehash())

    first = auth.authenticate_with_factory(
        app.session_factory,
        email=owner.web.admin_email,
        password=owner.web.admin_password,
        settings=app.settings,
        clock=app.clock,
        host="192.0.2.20",
        hasher=spy,
    )
    assert first.login_ticket is not None and not first.succeeded
    # The upgrade is prepared on the ticket, not applied.
    assert hashed == [owner.web.admin_password]
    with owner.db() as session:
        stored = session.get(User, owner.admin_id)
        pending = session.scalar(select(LoginTicket))
        assert stored is not None and stored.password_hash == before
        assert pending is not None and pending.replacement_password_hash is not None
        assert before not in (pending.password_fingerprint, pending.replacement_password_hash)

    def complete(code: str) -> auth.LoginOutcome:
        return auth.complete_login_ticket(
            app.session_factory,
            ticket=first.login_ticket or "",
            code=code,
            settings=app.settings,
            clock=app.clock,
            host="192.0.2.20",
            user_agent=None,
            existing_session_id=None,
            require_instance_admin=False,
        )

    for wrong in ("", "000000"):
        assert not complete(wrong).succeeded
    with owner.db() as session:
        stored = session.get(User, owner.admin_id)
        assert stored is not None and stored.password_hash == before

    app.clock = FixedClock("2026-10-21T12:00:30+00:00")
    assert complete(pyotp.TOTP(secret).at(app.clock.now())).succeeded
    with owner.db() as session:
        stored = session.get(User, owner.admin_id)
        assert stored is not None and stored.password_hash != before
        assert stored.password_hash == pending.replacement_password_hash


def test_wrong_code_fails_like_a_wrong_password_and_is_audited_internally(
    owner: OwnerApp,
) -> None:
    _enable(owner)
    client = TestClient(owner.app)
    token = owner.web.csrf(client.get("/login").text)

    def attempt(password: str, code: str):
        return client.post(
            "/login",
            data={
                "csrf_token": token,
                "email": owner.web.admin_email,
                "password": password,
                "second_factor": code,
            },
            follow_redirects=False,
        )

    wrong_password = attempt("Not The Password 123", "000000")
    second_step = attempt(owner.web.admin_password, "000000")
    assert second_step.status_code == 303
    assert second_step.headers["location"] == "/login/verify"
    challenge = client.get("/login/verify")
    wrong_code = client.post(
        "/login/verify",
        data={"csrf_token": owner.web.csrf(challenge.text), "second_factor": "000000"},
        follow_redirects=False,
    )
    assert wrong_code.status_code == wrong_password.status_code == 401
    # The code page is reachable only after a correct password, so it may say which failed.
    assert "The code is not correct." in wrong_code.text
    assert "The email or password is not correct." not in wrong_code.text
    assert "The email or password is not correct." in wrong_password.text

    assert wrong_code.headers["content-type"] == wrong_password.headers["content-type"]
    with owner.db() as session:
        failures = list(
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.action == "auth.login_failure")
                .order_by(AuditEvent.ts)
            )
        )
    assert [event.data for event in failures] == [{}, {"reason": "second_factor"}]


def test_secret_is_bound_to_its_account_and_key_change_logs_a_warning(
    owner: OwnerApp, caplog: pytest.LogCaptureFixture
) -> None:
    settings = owner.app.state.settings
    now = owner.app.state.clock.now()
    other_id = owner.add_user("other")
    secret = pyotp.random_base32()
    token = totp.encrypt_secret(settings, owner.admin_id, secret, now)
    assert secret not in token
    assert totp.decrypt_secret(settings, owner.admin_id, token) == secret
    with caplog.at_level("WARNING"):
        assert totp.decrypt_secret(settings, other_id, token) is None
    assert "not bound to its account" in caplog.text
    caplog.clear()
    settings.secret_key = "replacement-instance-secret"
    with caplog.at_level("WARNING"):
        assert totp.decrypt_secret(settings, owner.admin_id, token) is None
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "Secret key and two-factor authentication" in warnings[0].getMessage()
    assert secret not in caplog.text and token not in caplog.text


def test_secret_and_recovery_code_pages_are_never_cached(owner: OwnerApp) -> None:
    owner.app.state.clock = FixedClock("2026-10-21T12:00:00+00:00")
    assert (
        owner.post(
            "/account/totp/start", {"confirm_password": owner.web.admin_password}
        ).status_code
        == 303
    )
    page = owner.client.get("/account/totp")
    assert "data-totp-secret" in page.text
    assert page.headers["cache-control"] == "no-store"
    secret = re.search(r"data-totp-secret>([A-Z2-7]+)</code>", page.text)
    assert secret
    shown = owner.post(
        "/account/totp/verify",
        {"code": pyotp.TOTP(secret.group(1)).at(owner.app.state.clock.now())},
        token_from="/account/totp",
    )
    assert re.findall(CODE_PATTERN, shown.text)
    assert shown.headers["cache-control"] == "no-store"


def test_cli_password_reset_keeps_the_second_factor(owner: OwnerApp) -> None:
    secret, _codes = _enable(owner)
    with owner.app.state.session_factory.begin() as session:
        auth.reset_user_password(
            session,
            email=owner.web.admin_email,
            password="Operator Reset Password 1",
            settings=owner.app.state.settings,
            clock=owner.app.state.clock,
        )
    with owner.db() as session:
        factor = session.get(TotpFactor, owner.admin_id)
        assert factor is not None and factor.enabled_at is not None
    client = TestClient(owner.app)

    def sign_in(code: str):
        first = client.post(
            "/login",
            data={
                "csrf_token": owner.web.csrf(client.get("/login").text),
                "email": owner.web.admin_email,
                "password": "Operator Reset Password 1",
            },
            follow_redirects=False,
        )
        if first.headers.get("location") != "/login/verify":
            return first
        challenge = client.get("/login/verify")
        return client.post(
            "/login/verify",
            data={"csrf_token": owner.web.csrf(challenge.text), "second_factor": code},
            follow_redirects=False,
        )

    assert sign_in("").status_code == 401
    owner.app.state.clock = FixedClock("2026-10-21T12:00:30+00:00")
    assert sign_in(pyotp.TOTP(secret).at(owner.app.state.clock.now())).status_code == 303


def test_cli_mfa_reset_removes_factor_and_password_sessions_only(owner: OwnerApp) -> None:
    _enable(owner)
    with owner.app.state.session_factory.begin() as session:
        session.add(
            UserSession(
                id_hash="e" * 64,
                user_id=owner.admin_id,
                created_at=owner.app.state.clock.now(),
                last_seen_at=owner.app.state.clock.now(),
                expires_at=owner.app.state.clock.now() + timedelta(days=1),
                auth_method="oidc",
            )
        )
    with owner.app.state.session_factory.begin() as session:
        user, revoked = auth.reset_user_two_factor(
            session, email=owner.web.admin_email.upper(), clock=owner.app.state.clock
        )
        assert user.id == owner.admin_id
    assert revoked == 1  # the owner's own password session
    with owner.db() as session:
        assert session.get(TotpFactor, owner.admin_id) is None
        assert list(session.scalars(select(TotpRecoveryCode))) == []
        assert [row.auth_method for row in session.scalars(select(UserSession))] == ["oidc"]
        event = session.scalar(select(AuditEvent).where(AuditEvent.action == "cli.reset_mfa"))
        assert event is not None
        assert (event.actor_type, event.actor_user_id) == ("cli", None)
        assert event.entity_id == str(owner.admin_id)
        assert event.data == {"sessions_revoked": 1}
    with owner.app.state.session_factory.begin() as session, pytest.raises(auth.NoTwoFactor):
        auth.reset_user_two_factor(
            session, email=owner.web.admin_email, clock=owner.app.state.clock
        )
    with owner.app.state.session_factory.begin() as session, pytest.raises(auth.UnknownUser):
        auth.reset_user_two_factor(session, email="nobody@example.com", clock=owner.app.state.clock)


def _password_step(owner: OwnerApp, client: TestClient, *, path: str = "/login"):
    return client.post(
        path,
        data={
            "csrf_token": owner.web.csrf(client.get(path).text),
            "email": owner.web.admin_email,
            "password": owner.web.admin_password,
        },
        follow_redirects=False,
    )


def _code_step(client: TestClient, owner: OwnerApp, code: str, *, path: str = "/login/verify"):
    page = client.get(path, follow_redirects=False)
    token = (
        owner.web.csrf(page.text)
        if page.status_code == 200
        else owner.web.csrf(client.get(path.removesuffix("/verify")).text)
    )
    return client.post(
        path, data={"csrf_token": token, "second_factor": code}, follow_redirects=False
    )


def test_code_page_has_one_time_code_field_and_a_start_over_link(owner: OwnerApp) -> None:
    _enable(owner)
    client = TestClient(owner.app)
    assert _password_step(owner, client).headers["location"] == "/login/verify"
    page = client.get("/login/verify").text
    assert 'autocomplete="one-time-code"' in page
    assert 'name="csrf_token"' in page
    assert re.search(r'<a [^>]*href="/login"[^>]*data-start-over', page)
    assert 'name="password"' not in page and 'name="email"' not in page


def test_account_without_two_factor_signs_in_in_one_step(owner: OwnerApp) -> None:
    client = TestClient(owner.app)
    response = _password_step(owner, client)
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert client.cookies.get("assetboard_session")
    assert client.cookies.get("assetboard_login_ticket") is None
    with owner.db() as session:
        assert session.scalar(select(LoginTicket)) is None


def test_expired_ticket_returns_to_login_with_a_neutral_message(owner: OwnerApp) -> None:
    secret, _codes = _enable(owner)
    client = TestClient(owner.app)
    assert _password_step(owner, client).status_code == 303
    challenge = client.get("/login/verify")
    owner.app.state.clock = FixedClock("2026-10-21T12:05:00+00:00")  # exactly 5 minutes on
    expired = client.post(
        "/login/verify",
        data={
            "csrf_token": owner.web.csrf(challenge.text),
            "second_factor": pyotp.TOTP(secret).at(owner.app.state.clock.now()),
        },
        follow_redirects=False,
    )
    assert expired.status_code == 303 and expired.headers["location"] == "/login?expired=1"
    assert client.cookies.get("assetboard_login_ticket") is None
    assert "assetboard_session" not in client.cookies
    page = client.get("/login?expired=1").text
    assert "Your sign-in has expired. Please sign in again." in page
    assert 'name="password"' in page
    with owner.db() as session:
        assert session.scalar(select(LoginTicket)) is None  # removed on use
    # A GET with a stale cookie also starts over; no cookie means no message.
    assert client.get("/login/verify", follow_redirects=False).headers["location"] == "/login"


def test_expired_tickets_are_deleted_when_a_new_one_is_issued(owner: OwnerApp) -> None:
    _enable(owner)
    assert _password_step(owner, TestClient(owner.app)).status_code == 303
    owner.app.state.clock = FixedClock("2026-10-21T12:06:00+00:00")
    client = TestClient(owner.app)
    assert _password_step(owner, client).status_code == 303
    with owner.db() as session:
        rows = list(session.scalars(select(LoginTicket)))
    assert [row.token_hash for row in rows] == [
        hashlib.sha256(client.cookies["assetboard_login_ticket"].encode()).hexdigest()
    ]


def test_consumed_ticket_cannot_be_replayed(owner: OwnerApp) -> None:
    secret, _codes = _enable(owner)
    client = TestClient(owner.app)
    assert _password_step(owner, client).status_code == 303
    ticket = client.cookies["assetboard_login_ticket"]
    owner.app.state.clock = FixedClock("2026-10-21T12:00:30+00:00")  # past the enrollment code
    code = pyotp.TOTP(secret).at(owner.app.state.clock.now())
    assert _code_step(client, owner, code).status_code == 303
    replay = TestClient(owner.app)
    replay.cookies.set("assetboard_login_ticket", ticket)
    response = _code_step(replay, owner, code)
    assert response.status_code == 303 and response.headers["location"] == "/login?expired=1"
    assert "assetboard_session" not in replay.cookies


def test_each_wrong_code_counts_and_a_ticket_allows_at_most_five(owner: OwnerApp) -> None:
    secret, _codes = _enable(owner)
    client = TestClient(owner.app)
    assert _password_step(owner, client).status_code == 303
    ticket = client.cookies["assetboard_login_ticket"]
    for attempt in range(1, auth.MAX_LOGIN_TICKET_ATTEMPTS):
        response = _code_step(client, owner, "000000")
        assert response.status_code == 401, attempt
        assert client.cookies.get("assetboard_login_ticket") == ticket  # kept
    with owner.db() as session:
        pending = session.scalar(select(LoginTicket))
        assert pending is not None and pending.code_attempts == 4
    last = _code_step(client, owner, "000000")
    assert last.status_code == 303 and last.headers["location"] == "/login?expired=1"
    with owner.db() as session:
        assert session.scalar(select(LoginTicket)) is None
    assert "assetboard_session" not in client.cookies
    # The used-up ticket is gone even with a correct code.
    client.cookies.set("assetboard_login_ticket", ticket)
    code = pyotp.TOTP(secret).at(owner.app.state.clock.now())
    assert _code_step(client, owner, code).headers["location"] == "/login?expired=1"


def test_wrong_codes_hit_the_sign_in_throttle_and_keep_the_ticket(owner: OwnerApp) -> None:
    secret, _codes = _enable(owner)
    owner.app.state.settings.auth_account_free_failures = 2
    client = TestClient(owner.app)
    assert _password_step(owner, client).status_code == 303
    ticket = client.cookies["assetboard_login_ticket"]
    statuses = []
    for _ in range(3):
        response = _code_step(client, owner, "000000")
        statuses.append((response.status_code, response.headers.get("retry-after")))
    assert statuses[-1][0] == 401 and statuses[-1][1] is not None, statuses
    assert client.cookies.get("assetboard_login_ticket") == ticket
    # Throttled: even the right code is refused without consuming the ticket.
    code = pyotp.TOTP(secret).at(owner.app.state.clock.now())
    throttled = _code_step(client, owner, code)
    assert throttled.status_code == 401 and throttled.headers.get("retry-after")
    assert "assetboard_session" not in client.cookies
    with owner.db() as session:
        assert session.scalar(select(LoginTicket)) is not None


def test_no_session_and_no_access_before_the_second_factor(owner: OwnerApp) -> None:
    _enable(owner)
    client = TestClient(owner.app)
    with owner.db() as session:
        before = len(session.scalars(select(UserSession)).all())
    assert _password_step(owner, client).status_code == 303
    with owner.db() as session:
        assert len(session.scalars(select(UserSession)).all()) == before
    for path in ("/", "/account", "/account/totp"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login"), path


def test_break_glass_ticket_is_bound_to_its_flow_and_to_an_admin(owner: OwnerApp) -> None:
    secret, _codes = _enable(owner)
    settings = owner.app.state.settings
    settings.oidc_only = True
    settings.allow_local_admin_login = True
    client = TestClient(owner.app)
    first = _password_step(owner, client, path="/login/local")
    assert first.headers["location"] == "/login/local/verify"
    # The break-glass ticket does not open the normal second step, and vice versa.
    assert client.get("/login/verify", follow_redirects=False).status_code in (303, 404)
    # Demoted between the two steps: the ticket ends.
    with owner.db() as session, session.begin():
        user = session.get(User, owner.admin_id)
        assert user is not None
        user.is_instance_admin = False
    code = pyotp.TOTP(secret).at(owner.app.state.clock.now())
    response = _code_step(client, owner, code, path="/login/local/verify")
    assert response.headers["location"] == "/login/local?expired=1"
    assert "assetboard_session" not in client.cookies


def test_break_glass_ticket_succeeds_with_a_code(owner: OwnerApp) -> None:
    secret, _codes = _enable(owner)
    settings = owner.app.state.settings
    settings.oidc_only = True
    settings.allow_local_admin_login = True
    client = TestClient(owner.app)
    assert _password_step(owner, client, path="/login/local").status_code == 303
    owner.app.state.clock = FixedClock("2026-10-21T12:00:30+00:00")  # past the enrollment code
    code = pyotp.TOTP(secret).at(owner.app.state.clock.now())
    response = _code_step(client, owner, code, path="/login/local/verify")
    assert response.status_code == 303 and client.cookies.get("assetboard_session")
    assert client.cookies.get("assetboard_login_ticket") is None


def _ticket_count(owner: OwnerApp) -> int:
    with owner.db() as session:
        return len(session.scalars(select(LoginTicket)).all())


def test_ticket_cookie_is_scoped_to_the_login_routes(owner: OwnerApp) -> None:
    secret, _codes = _enable(owner)
    client = TestClient(owner.app)
    first = _password_step(owner, client)
    assert "Path=/login" in first.headers["set-cookie"]
    owner.app.state.clock = FixedClock("2026-10-21T12:00:30+00:00")
    code = pyotp.TOTP(secret).at(owner.app.state.clock.now())
    success = _code_step(client, owner, code)
    cleared = [h for h in success.headers.get_list("set-cookie") if "login_ticket" in h]
    assert cleared and all("Path=/login" in h and "Max-Age=0" in h for h in cleared)


def test_ticket_stores_no_copy_of_the_password_hash(owner: OwnerApp) -> None:
    _enable(owner)
    assert _password_step(owner, TestClient(owner.app)).status_code == 303
    with owner.db() as session:
        user = session.get(User, owner.admin_id)
        pending = session.scalar(select(LoginTicket))
        assert user is not None and pending is not None and user.password_hash
        assert pending.password_fingerprint != user.password_hash
        assert len(pending.password_fingerprint) == 64
        assert pending.password_fingerprint == auth.password_fingerprint(
            owner.app.state.settings, user.password_hash
        )


def test_pending_ticket_ends_when_the_password_changes(owner: OwnerApp) -> None:
    secret, _codes = _enable(owner)
    client = TestClient(owner.app)
    assert _password_step(owner, client).status_code == 303
    with owner.db() as session, session.begin():
        auth.reset_user_password(
            session,
            email=owner.web.admin_email,
            password="A Brand New Password 1",
            settings=owner.app.state.settings,
            clock=owner.app.state.clock,
        )
    assert _ticket_count(owner) == 0
    owner.app.state.clock = FixedClock("2026-10-21T12:00:30+00:00")
    code = pyotp.TOTP(secret).at(owner.app.state.clock.now())
    assert _code_step(client, owner, code).headers["location"] == "/login?expired=1"


def test_pending_ticket_ends_when_the_password_hash_changes_behind_it(owner: OwnerApp) -> None:
    secret, _codes = _enable(owner)
    client = TestClient(owner.app)
    assert _password_step(owner, client).status_code == 303
    with owner.db() as session, session.begin():  # no revocation hook: the fingerprint alone
        user = session.get(User, owner.admin_id)
        assert user is not None
        user.password_hash = auth.argon2_hash("A Brand New Password 1")
    owner.app.state.clock = FixedClock("2026-10-21T12:00:30+00:00")
    code = pyotp.TOTP(secret).at(owner.app.state.clock.now())
    assert _code_step(client, owner, code).headers["location"] == "/login?expired=1"


def test_pending_ticket_ends_when_totp_is_disabled(owner: OwnerApp) -> None:
    secret, codes = _enable(owner)
    client = TestClient(owner.app)
    assert _password_step(owner, client).status_code == 303
    assert _ticket_count(owner) == 1
    response = owner.post(
        "/account/totp/disable",
        {"confirm_password": owner.web.admin_password, "second_factor": codes[0]},
    )
    assert response.status_code == 303, response.text
    with owner.db() as session:
        assert session.get(TotpFactor, owner.admin_id) is None
    assert _ticket_count(owner) == 0
    code = pyotp.TOTP(secret).at(owner.app.state.clock.now())
    assert _code_step(client, owner, code).headers["location"] == "/login?expired=1"


def test_pending_ticket_ends_on_reset_and_re_enrolment(owner: OwnerApp) -> None:
    _secret, _codes = _enable(owner)
    client = TestClient(owner.app)
    assert _password_step(owner, client).status_code == 303
    ticket = client.cookies["assetboard_login_ticket"]
    with owner.db() as session, session.begin():
        auth.reset_user_two_factor(
            session, email=owner.web.admin_email, clock=owner.app.state.clock
        )
    assert _ticket_count(owner) == 0
    # Enrolled again (the attacker's authenticator, say): the old ticket stays dead.
    assert owner.web.sign_in(owner.client).status_code == 303
    owner.app.state.clock = FixedClock("2026-10-21T12:01:00+00:00")
    new_secret, _new_codes = _enable(owner)
    owner.app.state.clock = FixedClock("2026-10-21T12:01:30+00:00")
    client.cookies.set("assetboard_login_ticket", ticket, path="/login")
    code = pyotp.TOTP(new_secret).at(owner.app.state.clock.now())
    assert _code_step(client, owner, code).headers["location"] == "/login?expired=1"
    assert "assetboard_session" not in client.cookies


def test_ticket_issued_before_the_factor_was_enabled_is_refused(owner: OwnerApp) -> None:
    secret, _codes = _enable(owner)
    client = TestClient(owner.app)
    assert _password_step(owner, client).status_code == 303
    with owner.db() as session, session.begin():  # a factor enabled after the ticket
        factor = session.get(TotpFactor, owner.admin_id)
        assert factor is not None
        factor.enabled_at = owner.app.state.clock.now() + timedelta(seconds=1)
    assert client.get("/login/verify", follow_redirects=False).headers["location"] == (
        "/login?expired=1"
    )


def test_signing_out_other_devices_ends_pending_tickets(owner: OwnerApp) -> None:
    _enable(owner)
    assert _password_step(owner, TestClient(owner.app)).status_code == 303
    with owner.db() as session, session.begin():
        auth.revoke_other_sessions(
            session, owner.admin_id, "not-a-current-session", clock=owner.app.state.clock
        )
    assert _ticket_count(owner) == 0
