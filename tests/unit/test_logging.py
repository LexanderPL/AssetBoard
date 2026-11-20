from __future__ import annotations

import io
import json
import logging

from fastapi.testclient import TestClient

from assetboard.app import create_app
from assetboard.config import Settings
from assetboard.logging import JsonFormatter, configure_logging
from assetboard.middleware import redact_path


def test_json_logging_emits_parseable_request_and_traceback_records() -> None:
    app = create_app(
        Settings(_env_file=None, secret_key="test-key-9caf06bb4436cdbfa20af91", log_format="json")
    )
    logger: logging.Logger = app.state.logger
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)

    try:
        with TestClient(app) as client:
            response = client.get(
                "/readyz?token=do-not-log",
                headers={"x-request-id": "json-trace"},
            )
        try:
            raise RuntimeError("synthetic failure")
        except RuntimeError:
            logger.exception("synthetic_exception", extra={"request_id": "exception-trace"})
    finally:
        logger.removeHandler(handler)
        handler.close()

    assert response.status_code == 200
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    request_record = next(
        record for record in records if record["message"].startswith("request_complete")
    )
    exception_record = next(
        record for record in records if record["message"] == "synthetic_exception"
    )

    assert request_record["request_id"] == "json-trace"
    assert request_record["method"] == "GET"
    assert request_record["path"] == "/readyz"
    assert request_record["status_code"] == 200
    assert isinstance(request_record["duration_ms"], (int, float))
    assert "do-not-log" not in output.getvalue()
    assert exception_record["request_id"] == "exception-trace"
    assert "RuntimeError: synthetic failure" in exception_record["exception"]


def test_control_characters_in_the_request_path_stay_on_one_log_line() -> None:
    app = create_app(Settings(_env_file=None, secret_key="test-secret-key-0123456789abcdef"))
    logger: logging.Logger = app.state.logger
    records: list[logging.LogRecord] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Collect()
    logger.addHandler(handler)
    try:
        with TestClient(app) as client:
            assert client.get("/foo%0aINJECTED%20line", follow_redirects=False).status_code == 303
            assert client.get("/foo%1b[31mred", follow_redirects=False).status_code == 303
            assert client.get("/readyz").status_code == 200
    finally:
        logger.removeHandler(handler)

    requests = [record for record in records if record.getMessage().startswith("request_complete")]
    assert len(requests) == 3
    paths = {str(record.__dict__["path"]) for record in requests}
    assert paths == {"/foo\\x0aINJECTED line", "/foo\\x1b[31mred", "/readyz"}
    formatter = logging.Formatter("%(message)s")
    for record in requests:
        rendered = formatter.format(record)
        assert "\n" not in rendered
        assert "\x1b" not in rendered
        assert "\x0a" not in record.getMessage()
        assert "\x1b" not in record.getMessage()


def test_redact_path_escapes_c0_c1_and_line_separators() -> None:
    assert redact_path("/invite/secret-token") == "/invite/[redacted]"
    assert redact_path("/a\x00b\x1b[31m") == "/a\\x00b\\x1b[31m"
    assert redact_path("/a\x85b\u2028c") == "/a\\x85b\\u2028c"
    assert redact_path("/ordinary/path") == "/ordinary/path"


def test_text_logging_includes_request_id() -> None:
    logger = configure_logging("INFO", "text")
    handler = logger.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    original_stream = handler.stream
    output = io.StringIO()
    handler.setStream(output)
    try:
        logger.info("text_record", extra={"request_id": "text-trace"})
    finally:
        handler.setStream(original_stream)

    assert "request_id=text-trace" in output.getvalue()
