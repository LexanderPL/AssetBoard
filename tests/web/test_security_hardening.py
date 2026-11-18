"""Security regression tests: cookies, sessions, headers and production error pages.

The feature-specific tests live beside their feature (CSRF in ``test_csrf_origin.py``,
sign-in throttling in ``test_sessions_page.py``, password-reset links in
``test_email_flows.py``). This module gathers the cross-cutting checks from F9.6 that must
hold for every deployment.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from assetboard.cli import server_options
from assetboard.middleware import HSTS_HEADER, HSTS_VALUE, SECURITY_HEADERS
from assetboard.web.security import session_cookie_name

if TYPE_CHECKING:
    from conftest import WebHarness

HTML_PATHS = ("/", "/login", "/missing-page", "/account/password")


def _logged_in(web: WebHarness, base_url: str) -> TestClient:
    app = web.app(base_url=base_url)
    client = TestClient(
        app, base_url="https://testserver" if base_url.startswith("https") else "http://testserver"
    )
    client.__enter__()
    web.complete_setup(app)
    assert web.sign_in(client).status_code == 303
    return client


@pytest.mark.parametrize("https", [False, True])
def test_security_headers_on_pages_errors_and_static_files(web: WebHarness, https: bool) -> None:
    base_url = "https://assets.example" if https else "http://localhost:8000"
    client = _logged_in(web, base_url)
    try:
        for path in HTML_PATHS:
            response = client.get(path)
            assert response.status_code in {200, 404}, (path, response.status_code)
            headers = response.headers
            assert headers["content-security-policy"] == SECURITY_HEADERS["content-security-policy"]
            assert "unsafe-inline" not in headers["content-security-policy"]
            assert "unsafe-eval" not in headers["content-security-policy"]
            assert headers["x-content-type-options"] == "nosniff"
            assert headers["x-frame-options"] == "DENY"
            assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
            assert headers["permissions-policy"] == SECURITY_HEADERS["permissions-policy"]
            assert headers["cache-control"] == "no-store"
            assert headers["x-request-id"]
            assert (headers.get(HSTS_HEADER) == HSTS_VALUE) is https
        static = client.get("/static/vendor/htmx.min.js")
        assert static.status_code == 200
        assert static.headers["x-content-type-options"] == "nosniff"
        assert (
            static.headers["content-security-policy"] == SECURITY_HEADERS["content-security-policy"]
        )
        assert (static.headers.get(HSTS_HEADER) == HSTS_VALUE) is https
        assert "no-store" not in static.headers.get("cache-control", "")
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("https", [False, True])
def test_every_cookie_has_secure_flags_for_the_base_url_scheme(
    web: WebHarness, https: bool
) -> None:
    base_url = "https://assets.example" if https else "http://localhost:8000"
    app = web.app(base_url=base_url)
    with TestClient(app, base_url="https://testserver" if https else "http://testserver") as client:
        web.complete_setup(app)
        login_page = client.get("/login")
        response = web.sign_in(client)
    assert response.status_code == 303
    cookies = login_page.headers.get_list("set-cookie") + response.headers.get_list("set-cookie")
    assert cookies
    for cookie in cookies:
        if not cookie.startswith(("assetboard_", "__Host-assetboard_")):
            continue
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie
        assert "Path=/" in cookie
        assert "Domain" not in cookie
        assert ("Secure" in cookie) is https
        if https:
            assert cookie.startswith("__Host-assetboard_")
        else:
            assert not cookie.startswith("__Host-")
    names = {cookie.split("=", 1)[0] for cookie in cookies}
    assert ("__Host-assetboard_session" if https else "assetboard_session") in names
    assert ("__Host-assetboard_csrf" if https else "assetboard_csrf") in names


def test_login_replaces_a_planted_session_cookie(web: WebHarness) -> None:
    """Session fixation: a value an attacker chose before sign-in never becomes a session."""
    app = web.app()
    planted = "attacker-chosen-session-value-0123456789abcdef"
    with TestClient(app) as client:
        web.complete_setup(app)
        client.cookies.set("assetboard_session", planted)
        response = web.sign_in(client)
        assert response.status_code == 303
        issued = response.cookies["assetboard_session"]
        assert issued != planted
        assert response.cookies.get("assetboard_session") == issued
    with TestClient(app) as replay:
        replay.cookies.set("assetboard_session", planted)
        assert replay.get("/", follow_redirects=False).status_code == 303


def test_a_logout_revokes_the_server_side_session_for_a_replayed_cookie(
    signed_in: TestClient, web: WebHarness
) -> None:
    session_id = signed_in.cookies["assetboard_session"]
    page = signed_in.get("/account/password")
    response = signed_in.post(
        "/logout",
        data={"csrf_token": web.csrf(page.text)},
        follow_redirects=False,
    )
    assert (response.status_code, response.headers["location"]) == (303, "/login")
    with TestClient(signed_in.app) as replay:
        replay.cookies.set("assetboard_session", session_id)
        assert replay.get("/", follow_redirects=False).status_code == 303
        assert replay.get("/account", follow_redirects=False).status_code == 303


def test_production_500_pages_never_leak_the_exception_or_a_stack_trace(web: WebHarness) -> None:
    marker = "synthetic-production-secret-marker"
    app = web.app(env="prod")

    @app.get("/_test/leak")
    async def leak() -> None:
        raise RuntimeError(marker)

    @app.get("/_test/failing-render")
    async def failing_render() -> None:
        raise ValueError(marker)

    with TestClient(app, raise_server_exceptions=False) as client:
        web.complete_setup(app)
        assert web.sign_in(client).status_code == 303
        responses = [
            client.get("/_test/leak", headers={"x-request-id": "leak-check"}),
            client.get("/_test/failing-render"),
            client.get("/does-not-exist"),
        ]
    for response in responses:
        assert response.status_code in {404, 500}
        body = response.text
        for forbidden in (
            marker,
            "RuntimeError",
            "ValueError",
            "Traceback",
            "test_security_hardening.py",
            "/home/",
            "site-packages",
        ):
            assert forbidden not in body, (response.request.url, forbidden)
        assert (
            response.headers["content-security-policy"]
            == SECURITY_HEADERS["content-security-policy"]
        )
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-request-id"]
    assert responses[0].headers["x-request-id"] == "leak-check"


def test_responses_do_not_advertise_the_server_software(
    signed_in: TestClient, web: WebHarness
) -> None:
    response = signed_in.get("/")
    assert "server" not in {name.lower() for name in response.headers}
    assert re.search(r"\buvicorn\b", response.text, re.IGNORECASE) is None
    assert server_options(web.settings())["server_header"] is False


def test_api_documentation_and_schema_are_not_served(
    signed_in: TestClient, web: WebHarness
) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        response = signed_in.get(path)
        assert response.status_code == 404, (path, response.status_code)


@pytest.mark.parametrize("https", [False, True])
def test_a_413_response_carries_the_security_headers_and_request_id(
    web: WebHarness, https: bool
) -> None:
    base_url = "https://assets.example" if https else "http://localhost:8000"
    normal = web.app(base_url=base_url)
    with TestClient(
        normal, base_url="https://testserver" if https else "http://testserver"
    ) as client:
        web.complete_setup(normal)
        assert web.sign_in(client).status_code == 303
        session = client.cookies[session_cookie_name(normal.state.settings)]
        token = web.csrf(client.get("/").text)

    limited = web.app(base_url=base_url, max_request_bytes=64)
    with TestClient(
        limited, base_url="https://testserver" if https else "http://testserver"
    ) as client:
        client.cookies.set(session_cookie_name(limited.state.settings), session)
        response = client.post(
            "/dashboard/customise",
            data={"csrf_token": token, "order": "x" * 200},
        )
    assert response.status_code == 413
    assert response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert response.headers["permissions-policy"]
    assert response.headers["x-request-id"]
    assert response.headers["cache-control"] == "no-store"
    assert (response.headers.get(HSTS_HEADER) == HSTS_VALUE) is https


def test_unsupported_methods_are_rejected(signed_in: TestClient) -> None:
    assert signed_in.put("/").status_code == 405
