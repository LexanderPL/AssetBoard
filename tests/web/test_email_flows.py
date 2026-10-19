"""Open registration with a verified email and password reset by email (ADR-0017)."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.db.models.identity import (
    AuditEvent,
    AuthThrottle,
    EmailToken,
    InstanceSettings,
    Membership,
    User,
    UserSession,
)
from assetboard.services import account_email, admin_users, auth
from assetboard.services.instance_settings import update_settings
from tests.conftest import ADMIN_PASSWORD, mail_body, mail_link

if TYPE_CHECKING:
    from conftest import WebHarness

NEW_PASSWORD = "Fresh Strong Password"
CHECK_INBOX = "Check your inbox"
LINK_INVALID = "This link is not valid"


@pytest.fixture
def mail(web: WebHarness) -> Iterator[tuple[FastAPI, TestClient]]:
    """Email on, open registration on, setup done; the client is signed out."""
    app = web.mail_app(registration_ip_limit=50, mail_email_limit=10, mail_ip_limit=50)
    with TestClient(app, client=("192.0.2.150", 1000)) as client:
        web.complete_setup(app)
        web.enable_registration(app)
        yield app, client


def _post(client: TestClient, web: WebHarness, path: str, form_page: str, **data: str) -> Response:
    token = web.csrf(client.get(form_page).text)
    return client.post(path, data={"csrf_token": token, **data}, follow_redirects=False)


def _start(client: TestClient, web: WebHarness, email: str) -> Response:
    return _post(client, web, "/register", "/register", email=email)


def _forgot(client: TestClient, web: WebHarness, email: str) -> Response:
    return _post(client, web, "/password/forgot", "/password/forgot", email=email)


def _add_user(
    app: FastAPI,
    email: str,
    *,
    password: bool = True,
    active: bool = True,
    locale: str | None = None,
) -> None:
    with app.state.session_factory.begin() as session:
        user = User(
            email=email,
            display_name="Someone",
            password_hash=auth.argon2_hash(ADMIN_PASSWORD) if password else None,
            is_active=active,
            locale=locale,
        )
        session.add(user)


def _tokens(app: FastAPI, purpose: str) -> list[EmailToken]:
    with app.state.session_factory() as session:
        return list(session.scalars(select(EmailToken).where(EmailToken.purpose == purpose)))


def _same_response(responses: list[Response]) -> None:
    first = responses[0]
    for response in responses[1:]:
        assert response.status_code == first.status_code
        assert response.text == first.text
        headers = {k: v for k, v in response.headers.items() if k != "x-request-id"}
        assert headers == {k: v for k, v in first.headers.items() if k != "x-request-id"}


def _events(app: FastAPI, action: str) -> list[AuditEvent]:
    with app.state.session_factory() as session:
        return list(session.scalars(select(AuditEvent).where(AuditEvent.action == action)))


# Registration


def test_first_step_creates_no_account_and_sends_a_hashed_single_use_link(
    mail: tuple[FastAPI, TestClient], web: WebHarness
) -> None:
    app, client = mail
    response = _start(client, web, "New@Example.com")
    assert response.status_code == 200 and CHECK_INBOX in response.text
    with app.state.session_factory() as session:
        assert session.scalar(select(User).where(User.email == "new@example.com")) is None
    [message] = web.sent_mail(app)
    assert message["To"] == "new@example.com"
    assert message["Subject"] == "Confirm your email address for AssetBoard"
    link = mail_link(message, "/register/complete/")
    token = link.rsplit("/", 1)[1]
    assert len(token) == 43
    [row] = _tokens(app, "register")
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert row.expires_at - row.created_at == timedelta(hours=24)


def test_second_step_creates_the_verified_account_once(
    mail: tuple[FastAPI, TestClient], web: WebHarness
) -> None:
    app, client = mail
    _start(client, web, "new@example.com")
    link = mail_link(web.sent_mail(app)[0], "/register/complete/")
    page = client.get(link)
    assert page.status_code == 200 and "new@example.com" in page.text
    response = client.post(
        link,
        data={
            "csrf_token": web.csrf(page.text),
            "display_name": "Newcomer",
            "password": NEW_PASSWORD,
            "workspace_name": "Budget",
        },
        follow_redirects=False,
    )
    assert (response.status_code, response.headers["location"]) == (303, "/")
    assert "Newcomer" in client.get("/").text
    with app.state.session_factory() as session:
        user = session.scalars(select(User).where(User.email == "new@example.com")).one()
        role = session.scalars(select(Membership.role).where(Membership.user_id == user.id)).one()
    assert role == "owner"
    assert [e.data for e in _events(app, "auth.register")] == [{"via": "email"}]
    assert _tokens(app, "register") == []
    client.cookies.clear()
    reused = client.get(link)
    assert reused.status_code == 404 and LINK_INVALID in reused.text


def test_first_step_says_the_same_for_new_existing_and_deactivated_addresses(
    mail: tuple[FastAPI, TestClient], web: WebHarness
) -> None:
    app, client = mail
    _add_user(app, "gone@example.com", active=False)
    emails = ("new@example.com", web.admin_email, "gone@example.com")
    responses = [_start(client, web, email) for email in emails]
    _same_response(responses)
    sent = web.sent_mail(app)
    assert [m["To"] for m in sent] == ["new@example.com", web.admin_email]
    existing = mail_body(sent[1])
    assert "/register/complete/" not in existing
    assert "http://testserver/login" in existing and "http://testserver/password/forgot" in existing
    # Only the new address got a link; nothing was created for the others.
    assert [row.email for row in _tokens(app, "register")] == ["new@example.com"]


def test_responses_never_wait_for_the_mail_server(
    mail: tuple[FastAPI, TestClient], web: WebHarness
) -> None:
    app, client = mail
    mailer = app.state.fake_mailer
    mailer.release = threading.Event()
    try:
        assert _start(client, web, "slow@example.com").status_code == 200
        assert _forgot(client, web, web.admin_email).status_code == 200
        assert mailer.sent == []  # both answered while the server still "hangs"
    finally:
        mailer.release.set()
    assert len(web.sent_mail(app)) == 2


def test_an_account_created_meanwhile_spends_the_link(
    mail: tuple[FastAPI, TestClient], web: WebHarness
) -> None:
    app, client = mail
    _start(client, web, "race@example.com")
    link = mail_link(web.sent_mail(app)[0], "/register/complete/")
    page = client.get(link)
    _add_user(app, "race@example.com")
    response = client.post(
        link,
        data={"csrf_token": web.csrf(page.text), "display_name": "R", "password": NEW_PASSWORD},
    )
    assert response.status_code == 404 and LINK_INVALID in response.text
    with app.state.session_factory() as session:
        assert len(session.scalars(select(User).where(User.email == "race@example.com")).all()) == 1


def test_links_are_built_from_base_url_not_the_host_header(
    mail: tuple[FastAPI, TestClient], web: WebHarness
) -> None:
    app, client = mail
    token = web.csrf(client.get("/register").text)
    client.post("/register", data={"csrf_token": token, "email": "h@example.com"},
                headers={"host": "evil.example", "x-forwarded-host": "evil.example"})  # fmt: skip
    body = mail_body(web.sent_mail(app)[0])
    assert "evil.example" not in body and "http://testserver/register/complete/" in body


def test_mail_follows_the_request_or_the_account_language(
    mail: tuple[FastAPI, TestClient], web: WebHarness
) -> None:
    app, client = mail
    _add_user(app, "ola@example.com", locale="pl")
    client.headers["accept-language"] = "pl"
    _start(client, web, "nowy@example.com")
    client.headers["accept-language"] = "en"
    _forgot(client, web, "ola@example.com")
    register, reset = web.sent_mail(app)
    assert register["Subject"] == "Potwierdź adres e-mail w AssetBoard"
    assert "Link działa raz, przez 24 godziny." in mail_body(register)
    assert reset["Subject"] == "Zresetuj hasło do AssetBoard"


def test_the_per_address_mail_limit_is_silent_and_counts_unknown_addresses(
    web: WebHarness,
) -> None:
    app = web.mail_app(mail_email_limit=1, mail_ip_limit=50)
    with TestClient(app) as client:
        web.complete_setup(app)
        emails = (web.admin_email, web.admin_email, "nobody@example.com", "nobody@example.com")
        responses = [_forgot(client, web, email) for email in emails]
    _same_response(responses)
    assert [m["To"] for m in web.sent_mail(app)] == [web.admin_email]
    with app.state.session_factory() as session:
        rows = {
            row.key_hash: row.failure_count
            for row in session.scalars(
                select(AuthThrottle).where(AuthThrottle.key_type == "mail_email")
            )
        }
    hashes = [
        hashlib.sha256(e.encode()).hexdigest() for e in (web.admin_email, "nobody@example.com")
    ]
    assert [rows[h] for h in hashes] == [1, 1]


def test_the_per_client_limit_says_try_later(web: WebHarness) -> None:
    app = web.mail_app(mail_ip_limit=2)
    with TestClient(app) as client:
        web.complete_setup(app)
        statuses = [_forgot(client, web, f"u{i}@example.com").status_code for i in range(3)]
        throttled = _forgot(client, web, "u9@example.com")
    assert statuses == [200, 200, 429]
    assert "Too many requests from your network." in throttled.text


# Password reset


def test_reset_sets_the_password_ends_every_session_and_is_audited(
    mail: tuple[FastAPI, TestClient], web: WebHarness
) -> None:
    app, client = mail
    with TestClient(app, client=("192.0.2.151", 1000)) as signed_in:
        assert web.sign_in(signed_in).status_code == 303
        assert _forgot(client, web, web.admin_email).status_code == 200
        link = mail_link(web.sent_mail(app)[0], "/password/reset/")
        [row] = _tokens(app, "reset")
        assert row.expires_at - row.created_at == timedelta(minutes=60)
        page = client.get(link)
        assert page.status_code == 200

        def submit(new: str, confirm: str) -> Response:
            return client.post(
                link,
                data={"csrf_token": web.csrf(page.text), "new_password": new,
                      "confirm_password": confirm},
            )  # fmt: skip

        assert "do not match" in submit(NEW_PASSWORD, "other").text
        assert "too common" in submit("qwertyuiop", "qwertyuiop").text
        done = submit(NEW_PASSWORD, NEW_PASSWORD)
        assert done.status_code == 200 and "Password changed" in done.text
        assert "assetboard_session" not in done.headers.get("set-cookie", "")  # not signed in
        assert signed_in.get("/", follow_redirects=False).status_code == 303  # signed out
    with app.state.session_factory() as session:
        assert session.scalars(select(UserSession)).all() == []
    assert web.sign_in(client, password=NEW_PASSWORD).status_code == 303
    client.cookies.clear()
    assert client.get(link).status_code == 404
    assert _events(app, "auth.password_reset_requested")
    [event] = _events(app, "auth.password_reset")
    assert event.data == {"sessions_revoked": 1}


def test_forgot_says_the_same_for_every_kind_of_address(
    mail: tuple[FastAPI, TestClient], web: WebHarness
) -> None:
    app, client = mail
    _add_user(app, "sso@example.com", password=False)
    _add_user(app, "gone@example.com", active=False)
    emails = ("nobody@example.com", web.admin_email, "sso@example.com", "gone@example.com")
    responses = [_forgot(client, web, email) for email in emails]
    _same_response(responses)
    sent = web.sent_mail(app)
    assert [m["To"] for m in sent] == [web.admin_email, "sso@example.com"]
    notice = mail_body(sent[1])
    assert "has no password" in notice and "/password/reset/" not in notice
    # Only the account with a password got a link.
    assert len(_tokens(app, "reset")) == 1


def _issue_reset(app: FastAPI, web: WebHarness, client: TestClient) -> str:
    _forgot(client, web, web.admin_email)
    return mail_link(web.sent_mail(app)[-1], "/password/reset/")


@pytest.mark.parametrize("change", ["profile", "cli", "deactivate"])
def test_every_password_change_or_deactivation_spends_reset_links(
    mail: tuple[FastAPI, TestClient], web: WebHarness, change: str
) -> None:
    app, client = mail
    link = _issue_reset(app, web, client)
    factory = app.state.session_factory
    clock = app.state.clock
    if change == "profile":
        assert web.sign_in(client).status_code == 303
        client.post(
            "/account/password",
            data={"csrf_token": web.page_token(client, "/account/password"),
                  "current_password": ADMIN_PASSWORD, "new_password": NEW_PASSWORD,
                  "confirm_password": NEW_PASSWORD},
        )  # fmt: skip
        client.cookies.clear()
    elif change == "cli":
        with factory.begin() as session:
            auth.reset_user_password(
                session,
                email=web.admin_email,
                password=NEW_PASSWORD,
                settings=app.state.settings,
                clock=clock,
            )
    else:
        with factory.begin() as session:
            actor = User(email="other-admin@example.com", display_name="O", is_instance_admin=True)
            session.add(actor)
            session.flush()
            target = session.scalars(select(User.id).where(User.email == web.admin_email)).one()
            status = admin_users.apply_admin_action(
                session, actor, target, "deactivate", clock=clock, host=None, user_agent=None
            )
            assert status == "done"
    assert _tokens(app, "reset") == []
    assert client.get(link).status_code == 404


def test_unknown_expired_and_malformed_links_look_alike(
    mail: tuple[FastAPI, TestClient], web: WebHarness
) -> None:
    app, client = mail
    link = _issue_reset(app, web, client)
    later = FixedClock((app.state.clock.now() + timedelta(minutes=61)).isoformat())
    token = link.rsplit("/", 1)[1]
    assert account_email.reset_target(app.state.session_factory, token, later) is None
    paths = ("/password/reset/" + "A" * 43, "/password/reset/x", "/register/complete/" + "A" * 43)
    pages = [client.get(path) for path in paths]
    assert {p.status_code for p in pages} == {404}
    assert len({p.text.split("<title>")[1] for p in pages}) == 1


def test_live_links_are_capped_at_three_per_address(
    mail: tuple[FastAPI, TestClient], web: WebHarness
) -> None:
    app, client = mail
    for _ in range(4):
        _start(client, web, "many@example.com")
    links = [mail_link(m, "/register/complete/") for m in web.sent_mail(app)]
    assert len(_tokens(app, "register")) == 3
    assert client.get(links[0]).status_code == 404  # the oldest one was dropped
    assert client.get(links[3]).status_code == 200
    expired = FixedClock((app.state.clock.now() + timedelta(hours=25)).isoformat())
    token = links[3].rsplit("/", 1)[1]
    assert account_email.registration_email(app.state.session_factory, token, expired) is None


# Availability and the instance setting


def test_without_email_every_route_is_404_and_registration_cannot_be_turned_on(
    web: WebHarness,
) -> None:
    app = web.app()
    with TestClient(app) as client:
        web.complete_setup(app)
        assert "Forgot your password?" not in client.get("/login").text
        for path in ("/register", "/password/forgot", "/register/complete/" + "A" * 43,
                     "/password/reset/" + "A" * 43):  # fmt: skip
            assert client.get(path).status_code == 404, path
        assert web.sign_in(client).status_code == 303
        page = client.get("/admin/settings")
        assert 'name="allow_registration" aria-describedby' in page.text
        assert "data-registration-blocked" in page.text and " disabled>" in page.text
        response = client.post(
            "/admin/settings",
            data={"csrf_token": web.csrf(page.text), "allow_workspace_creation": "on",
                  "allow_registration": "on", "confirm_password": ADMIN_PASSWORD},
        )  # fmt: skip
    assert response.status_code == 400 and "needs an email server" in response.text
    with app.state.session_factory() as session:
        row = session.get(InstanceSettings, 1)
        assert row is not None and row.allow_registration is False


def test_turning_registration_on_is_audited_and_opens_the_pages(web: WebHarness) -> None:
    app = web.mail_app()
    with TestClient(app) as client:
        web.complete_setup(app)
        assert client.get("/register").status_code == 404
        login = client.get("/login").text
        assert "Forgot your password?" in login and "Create an account" not in login
        assert web.sign_in(client).status_code == 303
        page = client.get("/admin/settings")
        assert "data-registration-blocked" not in page.text
        saved = client.post(
            "/admin/settings",
            data={"csrf_token": web.csrf(page.text), "allow_workspace_creation": "on",
                  "allow_registration": "on", "confirm_password": ADMIN_PASSWORD},
        )  # fmt: skip
        assert saved.status_code == 200 and "Instance settings saved." in saved.text
        client.post("/logout", data={"csrf_token": web.page_token(client)})
        assert "Create an account" in client.get("/login").text
        assert client.get("/register").status_code == 200
    [event] = _events(app, "instance.settings_updated")
    assert event.data == {"allow_registration": True}


def test_registration_cannot_be_turned_on_under_oidc_only(
    db_session: Session, fixed_clock: FixedClock
) -> None:
    settings = Settings(
        _env_file=None,
        env="prod",
        base_url="https://money.example.test",
        oidc_issuer="https://idp.example.test/",
        oidc_client_id="assetboard",
        oidc_only=True,
        smtp_host="smtp.example.test",
        smtp_from="budget@example.test",
    )
    actor = User(email="root@example.com", display_name="Root", is_instance_admin=True)
    db_session.add(actor)
    db_session.flush()
    status = update_settings(
        db_session,
        actor,
        allow_workspace_creation=True,
        allow_registration=True,
        settings=settings,
        clock=fixed_clock,
        host=None,
        user_agent=None,
    )
    assert status == "registration_oidc_only"
