"""Outgoing email: rendering, SMTP modes, the outbox and ``mail-test`` (ADR-0017)."""

from __future__ import annotations

import logging
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from assetboard import cli
from assetboard.branding import ENV_PREFIX
from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.domain.identity import InvalidEmail, normalize_email, normalize_new_email
from assetboard.services.account_email import address_tag
from assetboard.services.mail import (
    MailJob,
    MailOutbox,
    MailRequest,
    SmtpMailer,
    random_delay,
    render,
)
from tests.conftest import FakeMailer, mail_body

CLOCK = FixedClock("2026-10-19T08:30:00+00:00")
LINK = "https://money.example.test/password/reset/" + "T" * 43


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "data_dir": tmp_path,
        "base_url": "https://money.example.test",
        "smtp_host": "smtp.example.test",
        "smtp_from": "budget@example.test",
        "smtp_username": "mailer",
        "smtp_password": "smtp-secret-value",
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_render_builds_a_plain_text_message_with_safe_headers(tmp_path: Path) -> None:
    message = render(
        MailJob("password_reset", "ola@example.com", "en", {"link": LINK}),
        _settings(tmp_path),
        CLOCK,
    )
    assert message["From"] == "AssetBoard <budget@example.test>"
    assert message["To"] == "ola@example.com"
    assert message["Subject"] == "Reset your AssetBoard password"
    assert message["Auto-Submitted"] == "auto-generated"
    assert message["Date"] == "Mon, 19 Oct 2026 08:30:00 +0000"
    assert message["Message-ID"].endswith("@example.test>")
    assert message.get_content_type() == "text/plain"
    assert LINK in mail_body(message) and "60 minutes" in mail_body(message)
    with pytest.raises(ValueError):
        render(
            MailJob("test", "a@example.com\nBcc: b@example.com", "en"), _settings(tmp_path), CLOCK
        )


def test_the_job_repr_never_shows_the_link() -> None:
    assert LINK not in repr(MailJob("password_reset", "a@example.com", "en", {"link": LINK}))


class _FakeSmtp:
    calls: list[tuple[str, Any]] = []

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:
        self.calls.append(("connect", (type(self).__name__, host, port, sorted(kwargs))))

    def __enter__(self) -> _FakeSmtp:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.calls.append(("quit", None))

    def starttls(self, *, context: ssl.SSLContext) -> None:
        self.calls.append(("starttls", context.verify_mode == ssl.CERT_REQUIRED))

    def login(self, user: str, password: str) -> None:
        self.calls.append(("login", (user, password)))

    def send_message(self, message: EmailMessage, *, to_addrs: list[str]) -> None:
        self.calls.append(("send", (message["To"], to_addrs)))


class _FakeSmtpSsl(_FakeSmtp):
    pass


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {},
            [
                ("connect", ("_FakeSmtp", "smtp.example.test", 587, ["timeout"])),
                ("starttls", True),
                ("login", ("mailer", "smtp-secret-value")),
                ("send", ("a@example.com", ["a@example.com"])),
                ("quit", None),
            ],
        ),
        (
            {"smtp_security": "tls", "smtp_port": 2465},
            [
                ("connect", ("_FakeSmtpSsl", "smtp.example.test", 2465, ["context", "timeout"])),
                ("login", ("mailer", "smtp-secret-value")),
                ("send", ("a@example.com", ["a@example.com"])),
                ("quit", None),
            ],
        ),
        (
            {"smtp_security": "none", "smtp_username": None, "smtp_password": None},
            [
                ("connect", ("_FakeSmtp", "smtp.example.test", 25, ["timeout"])),
                ("send", ("a@example.com", ["a@example.com"])),
                ("quit", None),
            ],
        ),
    ],
)
def test_smtp_security_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
    expected: list[tuple[str, Any]],
) -> None:
    _FakeSmtp.calls = []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSmtp)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSmtpSsl)
    settings = _settings(tmp_path, **overrides)
    message = render(MailJob("test", "a@example.com", "en"), settings, CLOCK)
    SmtpMailer(settings).send(message, "a@example.com")
    assert _FakeSmtp.calls == expected


def test_a_failed_send_logs_only_the_template_and_the_error_class(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    mailer = FakeMailer()
    mailer.fail = True
    logger = logging.getLogger("test-outbox")
    outbox = MailOutbox(mailer, _settings(tmp_path), CLOCK, logger)
    caplog.set_level(logging.DEBUG, logger="test-outbox")
    outbox.enqueue(MailJob("password_reset", "ola@example.com", "en", {"link": LINK}))
    outbox.join()
    outbox.stop()
    assert "mail_send_failed kind=password_reset error=SMTPAuthenticationError" in caplog.text
    for secret in ("ola@example.com", LINK, "secret-reply", "smtp-secret-value"):
        assert secret not in caplog.text


def test_drops_from_a_full_outbox_are_warned_once_a_minute_with_a_count(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    mailer = FakeMailer()
    mailer.release = threading.Event()
    now = [1000.0]
    outbox = MailOutbox(
        mailer,
        _settings(tmp_path),
        CLOCK,
        logging.getLogger("t"),
        maxsize=1,
        monotonic=lambda: now[0],
    )
    job = MailJob("test", "a@example.com", "en")
    assert outbox.enqueue(job)  # the one pending job (its send blocks)
    assert [outbox.enqueue(job) for _ in range(3)] == [False, False, False]
    now[0] += 61
    assert outbox.enqueue(job) is False
    drops = [r.getMessage() for r in caplog.records if "mail_dropped" in r.getMessage()]
    assert drops == [
        "mail_dropped count=1 reason=queue_full",
        "mail_dropped count=3 reason=queue_full",
    ]
    mailer.release.set()
    assert outbox.join(timeout=5)
    outbox.stop()


def test_mail_test_command_reports_success_or_only_the_error_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {
        f"{ENV_PREFIX}_DATA_DIR": str(tmp_path),
        f"{ENV_PREFIX}_BASE_URL": "http://localhost:8000",
        f"{ENV_PREFIX}_SMTP_HOST": "smtp.example.test",
        f"{ENV_PREFIX}_SMTP_FROM": "budget@example.test",
        f"{ENV_PREFIX}_SMTP_USERNAME": "mailer",
        f"{ENV_PREFIX}_SMTP_PASSWORD": "smtp-secret-value",
    }
    mailer = FakeMailer()
    monkeypatch.setattr(cli, "mail_test_mailer", lambda _settings: mailer)
    sent = CliRunner().invoke(cli.app, ["mail-test", "Me@Example.com"], env=env)
    assert sent.exit_code == 0 and "Test email sent to me@example.com." in sent.output
    assert [m["To"] for m in mailer.sent] == ["me@example.com"]
    mailer.fail = True
    failed = CliRunner().invoke(cli.app, ["mail-test", "me@example.com"], env=env)
    assert failed.exit_code == 1
    assert "Sending failed: SMTPAuthenticationError" in failed.output
    assert "secret" not in failed.output
    off = CliRunner().invoke(cli.app, ["mail-test", "me@example.com"], env={
        f"{ENV_PREFIX}_DATA_DIR": str(tmp_path)})  # fmt: skip
    assert off.exit_code == 1 and "SMTP_HOST is not set" in off.output


def test_the_envelope_names_only_the_verified_recipient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeSmtp.calls = []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSmtp)
    settings = _settings(tmp_path)
    message = render(MailJob("test", "ola@example.com", "en"), settings, CLOCK)
    assert str(message["To"]) == "ola@example.com"
    SmtpMailer(settings).send(message, "ola@example.com")
    assert ("send", ("ola@example.com", ["ola@example.com"])) in _FakeSmtp.calls


@pytest.mark.parametrize(
    "address",
    [
        "victim@example.com,attacker@evil.test",
        '"attacker@evil.test"@example.com',
        "attacker@evil.test<victim@example.com>",
    ],
)
def test_header_special_addresses_are_refused_for_new_input(address: str) -> None:
    with pytest.raises(InvalidEmail):
        normalize_new_email(address)
    assert normalize_email("Ola@Example.com") == "ola@example.com"


def test_requests_wait_a_random_delay_before_any_work(tmp_path: Path) -> None:
    assert all(0.5 <= random_delay() <= 3.0 for _ in range(50))
    resolved: list[MailRequest] = []

    def resolver(request: MailRequest) -> MailJob | None:
        resolved.append(request)
        return MailJob("test", request.email, request.locale)

    mailer = FakeMailer()
    outbox = MailOutbox(
        mailer,
        _settings(tmp_path),
        CLOCK,
        logging.getLogger("t"),
        resolver=resolver,
        delay=lambda: 0.3,
    )
    started = time.monotonic()
    outbox.enqueue(MailRequest("reset", "ola@example.com", "en"))
    time.sleep(0.1)
    assert resolved == []  # nothing was looked up before the delay
    assert outbox.join(timeout=5)
    assert time.monotonic() - started >= 0.3
    assert mailer.recipients == ["ola@example.com"]
    outbox.stop()


def test_delays_do_not_block_each_other(tmp_path: Path) -> None:
    mailer = FakeMailer()
    outbox = MailOutbox(
        mailer,
        _settings(tmp_path),
        CLOCK,
        logging.getLogger("t"),
        resolver=lambda request: MailJob("test", request.email, request.locale),
        delay=lambda: 0.2,
    )
    started = time.monotonic()
    for index in range(40):
        assert outbox.enqueue(MailRequest("reset", f"u{index}@example.com", "en"))
    assert outbox.join(timeout=5)
    # One blocking delay after another would take 8 seconds.
    assert time.monotonic() - started < 3
    assert len(mailer.recipients) == 40
    outbox.stop()


class _SlowForOne(FakeMailer):
    def send(self, message: EmailMessage, recipient: str) -> None:
        if recipient == "slow@example.com":
            assert self.release is not None
            self.release.wait(10)
        self.sent.append(message)
        self.recipients.append(recipient)


def test_a_slow_send_does_not_hold_up_the_others(tmp_path: Path) -> None:
    mailer = _SlowForOne()
    mailer.release = threading.Event()
    outbox = MailOutbox(mailer, _settings(tmp_path), CLOCK, logging.getLogger("t"))
    for address in ("slow@example.com", "a@example.com", "b@example.com", "c@example.com"):
        outbox.enqueue(MailJob("test", address, "en"))
    deadline = time.monotonic() + 5
    while len(mailer.recipients) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sorted(mailer.recipients) == ["a@example.com", "b@example.com", "c@example.com"]
    mailer.release.set()
    assert outbox.join(timeout=5)
    outbox.stop()


def test_stop_refuses_new_jobs_and_does_not_wait_out_a_delay(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    mailer = FakeMailer()
    outbox = MailOutbox(
        mailer,
        _settings(tmp_path),
        CLOCK,
        logging.getLogger("t"),
        resolver=lambda request: MailJob("test", request.email, request.locale),
        delay=lambda: 30.0,
    )
    outbox.enqueue(MailRequest("reset", "late@example.com", "en"))
    started = time.monotonic()
    outbox.stop(timeout=2)
    assert time.monotonic() - started < 1  # the wait was interrupted
    assert "mail_dropped count=1 reason=stopped" in caplog.text
    assert outbox.enqueue(MailJob("test", "after@example.com", "en")) is False
    outbox.start()  # the next application start accepts jobs again
    assert outbox.enqueue(MailJob("test", "after@example.com", "en"))
    assert outbox.join(timeout=5)
    outbox.stop()
    assert mailer.recipients == ["after@example.com"]


def test_the_log_tag_depends_on_the_key() -> None:
    assert address_tag(b"key-one", "ola@example.com") != address_tag(b"key-two", "ola@example.com")
    assert address_tag(b"key-one", "ola@example.com") == address_tag(b"key-one", "ola@example.com")
