"""Outgoing email (pre-release B, ADR-0017).

Requests never send mail and never look an address up. Registration and "forgot password"
hand a ``MailRequest`` (purpose, normalized address, language) to the ``MailOutbox`` in a
background task that runs after the response was sent. Its worker thread waits a random delay,
then does the account-dependent work (lookup, link, audit) in its own transaction, renders the
mail and sends it. A failed job is logged with its kind and the exception class only: never
the address, a link, the SMTP password or the server's reply.

``SmtpMailer`` uses the standard library; tests use a fake ``Mailer`` and never the network.
"""

from __future__ import annotations

import logging
import queue
import secrets
import smtplib
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from assetboard.branding import APP_NAME
from assetboard.clock import Clock
from assetboard.config import Settings
from assetboard.i18n import gettext, translations_for
from assetboard.i18n.locale import normalize_locale

MailTemplate = Literal[
    "register_verify",
    "register_existing",
    "password_reset",
    "password_reset_no_password",
    "test",
]
_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "web" / "templates" / "email"
OUTBOX_SIZE = 100
STOP_TIMEOUT_SECONDS = 5.0


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


SUBJECTS: dict[MailTemplate, str] = {
    "register_verify": _("Confirm your email address for %(app)s"),
    "register_existing": _("You already have an account at %(app)s"),
    "password_reset": _("Reset your %(app)s password"),
    "password_reset_no_password": _("About your %(app)s password"),
    "test": _("Test email from %(app)s"),
}


@dataclass(frozen=True, slots=True)
class MailJob:
    """One email to send. ``params`` may hold a secret link, so it is never in the ``repr``."""

    template: MailTemplate
    to: str
    locale: str
    params: Mapping[str, str] = field(default_factory=dict[str, str], repr=False)


@dataclass(frozen=True, slots=True)
class MailRequest:
    """A registration or reset request, as the request left it: only what the visitor typed.

    The worker decides what (if anything) to send; see ``MailOutbox``.
    """

    purpose: Literal["register", "reset"]
    email: str
    locale: str


Resolver = Callable[[MailRequest], "MailJob | None"]


def random_delay() -> float:
    """0.5 to 3 seconds before a request's work starts, so its timing tells nothing."""
    return secrets.SystemRandom().uniform(0.5, 3.0)


class Mailer(Protocol):
    def send(self, message: EmailMessage, recipient: str) -> None: ...


@lru_cache(maxsize=4)
def _environment(locale: str) -> Environment:
    # Plain-text mail: nothing is HTML, so there is nothing to escape.
    environment = Environment(  # noqa: S701
        loader=FileSystemLoader(_TEMPLATE_DIR),
        autoescape=False,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        extensions=["jinja2.ext.i18n"],
    )
    cast(Any, environment).install_gettext_translations(translations_for(locale), newstyle=True)
    return environment


def render(job: MailJob, settings: Settings, clock: Clock) -> EmailMessage:
    """The message for ``job``, in its locale; every link was built from ``BASE_URL``."""
    if settings.smtp_from is None:
        raise RuntimeError("SMTP_FROM is not configured")
    locale = normalize_locale(job.locale)
    body = (
        _environment(locale)
        .get_template(f"{job.template}.txt")
        .render(app_name=APP_NAME, base_url=settings.base_url, **job.params)
    )
    message = EmailMessage(policy=policy.SMTP)
    # policy.SMTP refuses line breaks in header values (no header injection).
    message["Subject"] = gettext(SUBJECTS[job.template], locale) % {"app": APP_NAME}
    message["From"] = formataddr((APP_NAME, settings.smtp_from))
    message["To"] = Address(addr_spec=job.to)
    message["Date"] = format_datetime(clock.now())
    message["Message-ID"] = make_msgid(domain=settings.smtp_from.rpartition("@")[2])
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(body, charset="utf-8")
    return message


class SmtpMailer:
    """Sends through the configured SMTP server; certificates are always verified."""

    def __init__(self, settings: Settings) -> None:
        if settings.smtp_host is None:
            raise RuntimeError("SMTP_HOST is not configured")
        self._settings = settings
        self._host = settings.smtp_host

    def send(self, message: EmailMessage, recipient: str) -> None:
        settings = self._settings
        context = ssl.create_default_context()
        port = settings.smtp_effective_port
        timeout = settings.smtp_timeout_seconds
        client = (
            smtplib.SMTP_SSL(self._host, port, timeout=timeout, context=context)
            if settings.smtp_security == "tls"
            else smtplib.SMTP(self._host, port, timeout=timeout)
        )
        with client:
            if settings.smtp_security == "starttls":
                client.starttls(context=context)
            if settings.smtp_username is not None and settings.smtp_password is not None:
                client.login(settings.smtp_username, settings.smtp_password.get_secret_value())
            # The envelope names exactly one recipient, never what the headers might say.
            client.send_message(message, to_addrs=[recipient])


def send_now(job: MailJob, *, mailer: Mailer, settings: Settings, clock: Clock) -> str | None:
    """Render and send synchronously (the ``mail-test`` command): ``None`` when sent, else the
    exception's class name, which is all that is ever reported."""
    try:
        mailer.send(render(job, settings, clock), job.to)
    except Exception as exc:  # noqa: BLE001 - only the class name is reported
        return type(exc).__name__
    return None


class MailOutbox:
    """A bounded in-memory queue drained by one worker thread, started on first use.

    It takes two kinds of jobs. A ``MailJob`` is rendered and sent as it is (``mail-test``
    style). A ``MailRequest`` (registration or password reset) is everything the request knew:
    the worker first waits a random 0.5 to 3 seconds, then ``resolver`` does the
    account-dependent work in its own transaction (lookup, link, audit) and returns the mail to
    send, if any. So the request itself does the same work for every address (ADR-0017).

    Jobs still queued when the process stops are lost; people ask for a new link.
    """

    def __init__(
        self,
        mailer: Mailer,
        settings: Settings,
        clock: Clock,
        logger: logging.Logger,
        *,
        resolver: Resolver | None = None,
        delay: Callable[[], float] | None = None,
        maxsize: int = OUTBOX_SIZE,
    ) -> None:
        self._mailer = mailer
        self._settings = settings
        self._clock = clock
        self._logger = logger
        self._resolver = resolver
        self._delay = delay or random_delay
        self._maxsize = maxsize
        self._lock = threading.Lock()
        # Each worker owns its queue, so a stop and a new enqueue never share one.
        self._queue: queue.Queue[MailJob | MailRequest | None] = queue.Queue(maxsize=maxsize)
        self._worker: threading.Thread | None = None

    def enqueue(self, job: MailJob | MailRequest) -> bool:
        """Queue ``job``; False (and a warning) when the queue is full."""
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._queue = queue.Queue(maxsize=self._maxsize)
                self._worker = threading.Thread(
                    target=self._run, args=(self._queue,), name="mail-outbox", daemon=True
                )
                self._worker.start()
            try:
                self._queue.put_nowait(job)
            except queue.Full:
                self._logger.warning("mail_dropped kind=%s reason=queue_full", _kind(job))
                return False
        return True

    def join(self) -> None:
        """Wait until every queued job was handled (tests)."""
        with self._lock:
            current = self._queue
        current.join()

    def stop(self, timeout: float = STOP_TIMEOUT_SECONDS) -> None:
        with self._lock:
            worker, stopping = self._worker, self._queue
            self._worker = None
            self._queue = queue.Queue(maxsize=self._maxsize)
        if worker is None:
            return
        try:
            stopping.put(None, timeout=timeout)
        except queue.Full:
            self._logger.warning("mail_outbox_stop_timeout")
            return
        worker.join(timeout)

    def _run(self, jobs: queue.Queue[MailJob | MailRequest | None]) -> None:
        while True:
            job = jobs.get()
            try:
                if job is None:
                    return
                self._handle(job)
            except Exception as exc:  # noqa: BLE001 - one bad mail never stops the worker
                self._logger.warning(
                    "mail_send_failed kind=%s error=%s", _kind(job), type(exc).__name__
                )
            finally:
                jobs.task_done()

    def _handle(self, job: MailJob | MailRequest) -> None:
        if isinstance(job, MailRequest):
            time.sleep(self._delay())
            if self._resolver is None:
                raise RuntimeError("No resolver for mail requests")
            resolved = self._resolver(job)
            if resolved is None:
                return
            job = resolved
        self._mailer.send(render(job, self._settings, self._clock), job.to)
        self._logger.debug("mail_sent kind=%s", job.template)


def _kind(job: MailJob | MailRequest | None) -> str:
    if job is None:
        return ""
    return job.template if isinstance(job, MailJob) else job.purpose
