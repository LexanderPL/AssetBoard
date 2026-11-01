from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import Response

from assetboard.app import create_app
from assetboard.config import Settings
from assetboard.logging import configure_logging
from assetboard.middleware import RequestBodyLimitMiddleware, RequestIdMiddleware, redact_path

if TYPE_CHECKING:
    from conftest import WebHarness


def test_request_id_is_validated_and_returned() -> None:
    app = create_app(Settings(_env_file=None, secret_key="test-secret"))

    with TestClient(app) as client:
        valid = client.get("/healthz", headers={"x-request-id": "trace-abc_123"})
        invalid = client.get("/healthz", headers={"x-request-id": "x" * 129})

    assert valid.headers["x-request-id"] == "trace-abc_123"
    assert valid.json()["request_id"] == "trace-abc_123"
    generated_id = invalid.headers["x-request-id"]
    assert len(generated_id) == 36
    assert invalid.json()["request_id"] == generated_id


def test_request_body_limit_rejects_streamed_overflow_without_buffering_the_body() -> None:
    downstream_body = bytearray()
    response_messages: list[dict[str, object]] = []
    downstream_completed = False
    chunks = iter((b"1234", b"5678"))

    async def receive() -> dict[str, object]:
        try:
            return {"type": "http.request", "body": next(chunks), "more_body": True}
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}

    async def app(scope: object, receive: object, send: object) -> None:
        nonlocal downstream_completed
        while True:
            message = await receive()  # type: ignore[operator]
            if message["type"] == "http.disconnect":
                return
            downstream_body.extend(message["body"])
            if not message["more_body"]:
                break
        downstream_completed = True
        await send({"type": "http.response.start", "status": 200, "headers": []})  # type: ignore[operator]
        await send({"type": "http.response.body", "body": b"ok"})  # type: ignore[operator]

    async def send(message: dict[str, object]) -> None:
        response_messages.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/ordinary",
        "root_path": "",
        "headers": [],
    }
    middleware = RequestBodyLimitMiddleware(app, max_request_bytes=5, import_max_upload_bytes=10)

    import asyncio

    asyncio.run(middleware(scope, receive, send))  # type: ignore[arg-type]

    assert bytes(downstream_body) == b"1234"
    assert not downstream_completed
    assert response_messages[0]["status"] == 413
    assert response_messages[-1]["body"] == b"Request body exceeds the allowed size."
    assert (b"x-content-type-options", b"nosniff") in response_messages[0]["headers"]


def test_import_route_uses_its_larger_limit() -> None:
    import asyncio

    forwarded: list[bytes] = []
    response_messages: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"12345678", "more_body": False}

    async def app(scope: object, receive: object, send: object) -> None:
        forwarded.append((await receive())["body"])  # type: ignore[operator]
        await send({"type": "http.response.start", "status": 200, "headers": []})  # type: ignore[operator]

    async def send(message: dict[str, object]) -> None:
        response_messages.append(message)

    middleware = RequestBodyLimitMiddleware(app, max_request_bytes=5, import_max_upload_bytes=10)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/workspace/data/import",
        "root_path": "",
        "headers": [],
    }

    asyncio.run(middleware(scope, receive, send))  # type: ignore[arg-type]

    assert forwarded == [b"12345678"]
    assert response_messages[0]["status"] == 200


def test_content_length_over_limit_is_rejected_without_reading() -> None:
    import asyncio

    receive_called = False
    response_messages: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        nonlocal receive_called
        receive_called = True
        return {"type": "http.request", "body": b"123456", "more_body": False}

    async def app(scope: object, receive: object, send: object) -> None:
        raise AssertionError("over-limit request reached the route")

    async def send(message: dict[str, object]) -> None:
        response_messages.append(message)

    middleware = RequestBodyLimitMiddleware(app, max_request_bytes=5, import_max_upload_bytes=10)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/ordinary",
        "root_path": "",
        "headers": [(b"content-length", b"6")],
    }

    asyncio.run(middleware(scope, receive, send))  # type: ignore[arg-type]

    assert not receive_called
    assert response_messages[0]["status"] == 413


def test_request_body_limit_is_the_outermost_app_middleware() -> None:
    app = create_app(Settings(_env_file=None, secret_key="test-secret"))

    assert app.user_middleware[0].cls is RequestBodyLimitMiddleware


def test_request_log_has_safe_path_status_duration_and_request_id() -> None:
    app = create_app(Settings(_env_file=None, secret_key="test-secret"))
    logger: logging.Logger = app.state.logger
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(logging.Formatter("%(request_id)s %(message)s"))
    logger.addHandler(handler)

    try:
        with TestClient(app) as client:
            response = client.get(
                "/readyz?token=do-not-log",
                headers={"x-request-id": "safe-trace"},
            )
            client.get("/healthz")
    finally:
        logger.removeHandler(handler)
        handler.close()

    assert response.status_code == 200
    # Startup also logs (the first-run setup token); only request lines matter here.
    lines = [line for line in output.getvalue().splitlines() if "request_complete" in line]
    assert len(lines) == 1
    assert lines[0].startswith("safe-trace request_complete method=GET path=/readyz ")
    assert "status_code=200" in lines[0]
    assert "duration_ms=" in lines[0]
    assert "do-not-log" not in lines[0]


def test_healthz_suppresses_success_and_warns_on_failure() -> None:
    app = FastAPI()

    @app.get("/healthz")
    async def healthz(unavailable: bool = False) -> Response:
        return Response(status_code=503 if unavailable else 200)

    logger = configure_logging("INFO", "text")
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(logging.Formatter("%(levelname)s %(request_id)s %(message)s"))
    logger.addHandler(handler)

    try:
        with TestClient(RequestIdMiddleware(app, logger)) as client:
            healthy = client.get("/healthz", headers={"x-request-id": "healthy-id"})
            unhealthy = client.get(
                "/healthz?unavailable=true",
                headers={"x-request-id": "unhealthy-id"},
            )
    finally:
        logger.removeHandler(handler)
        handler.close()

    assert healthy.status_code == 200
    assert unhealthy.status_code == 503
    lines = output.getvalue().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("WARNING unhealthy-id request_complete method=GET path=/healthz ")
    assert "status_code=503" in lines[0]


def test_unhandled_exception_logs_traceback_and_returns_request_id(web: WebHarness) -> None:
    app = web.app()

    @app.get("/route-that-raises")
    async def route_that_raises() -> None:
        raise RuntimeError("synthetic route failure")

    logger: logging.Logger = app.state.logger
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(logging.Formatter("%(request_id)s %(message)s"))
    logger.addHandler(handler)

    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            web.complete_setup(app)
            web.sign_in(client)
            response = client.get(
                "/route-that-raises",
                headers={"x-request-id": "failure-trace"},
            )
    finally:
        logger.removeHandler(handler)
        handler.close()

    assert response.status_code == 500
    assert response.headers["x-request-id"] == "failure-trace"
    assert "failure-trace request_complete method=GET path=/route-that-raises " in output.getvalue()
    assert "status_code=500" in output.getvalue()
    assert "Traceback (most recent call last)" in output.getvalue()
    assert "synthetic route failure" in output.getvalue()


@pytest.mark.parametrize(
    ("path", "logged"),
    [
        ("/invite/" + "a" * 43, "/invite/[redacted]"),
        ("/invite/" + "a" * 43 + "/register", "/invite/[redacted]/register"),
        ("/invite/short", "/invite/[redacted]"),
        ("/prefix/invite/tok/accept", "/prefix/invite/[redacted]/accept"),
        ("/workspace/invitations", "/workspace/invitations"),
        ("/login", "/login"),
        ("/register/complete/" + "a" * 43, "/register/complete/[redacted]"),
        ("/password/reset/" + "a" * 43, "/password/reset/[redacted]"),
        ("/password/forgot", "/password/forgot"),
        ("/register", "/register"),
    ],
)
def test_request_log_redacts_invitation_tokens(path: str, logged: str) -> None:
    assert redact_path(path) == logged
