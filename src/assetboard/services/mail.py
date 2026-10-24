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

import heapq
import logging
import secrets
import smtplib
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
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
SENDERS = 4
DROP_WARNING_EVERY = 60.0
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
    # The requester's network as audit rows store it (IPv4 /24, IPv6 /48), the same for
    # every address.
    ip_prefix: str | None = None


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


def recipient_address(address: str) -> Address:
    """The ``To`` address, only when it is exactly ``address``: the email package drops
    comments and similar syntax (``a(b)@x`` becomes ``a@x``), which would mail someone else."""
    parsed = Address(addr_spec=address)
    if parsed.addr_spec != address:
        raise ValueError("The address does not round-trip as one plain addr-spec")
    return parsed


def is_deliverable(address: str) -> bool:
    try:
        recipient_address(address)
    except Exception:  # noqa: BLE001 - any parse problem means "do not send"
        return False
    return True


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
    message["To"] = recipient_address(job.to)
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
    """A bounded in-memory outbox: a scheduler thread and a small pool of senders.

    It takes two kinds of jobs. A ``MailJob`` is rendered and sent as it is. A ``MailRequest``
    (registration or password reset) is everything the request knew: it becomes due after a
    random 0.5 to 3 seconds, and only then ``resolver`` does the account-dependent work in its
    own transaction (lookup, link, audit) and returns the mail to send, if any. So the request
    itself does the same work for every address (ADR-0017).

    The delay never blocks anything: jobs wait in a heap ordered by due time, and the scheduler
    sleeps on a condition until the earliest is due (or the outbox stops), then hands it to a
    pool of ``SENDERS`` threads, so one slow SMTP server holds up only its own job. At most
    ``maxsize`` jobs are pending (waiting or in flight); beyond that a job is dropped and a
    warning, aggregated to one line per minute with a count, tells the administrator.

    Jobs still pending when the process stops are lost; people ask for a new link.
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
        senders: int = SENDERS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._mailer = mailer
        self._settings = settings
        self._clock = clock
        self._logger = logger
        self._resolver = resolver
        self._delay = delay or random_delay
        self._maxsize = maxsize
        self._senders = senders
        self._monotonic = monotonic
        self._condition = threading.Condition()
        self._heap: list[tuple[float, int, MailJob | MailRequest]] = []
        self._sequence = 0
        self._pending = 0  # waiting in the heap or in flight
        self._closed = False
        self._scheduler: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._in_flight: set[Future[None]] = set()
        self._dropped = 0
        self._last_drop_warning: float | None = None

    def start(self) -> None:
        """Accept jobs again (application startup); threads start on the first job."""
        with self._condition:
            self._closed = False

    def enqueue(self, job: MailJob | MailRequest) -> bool:
        """Schedule ``job``; False when the outbox is stopped or full."""
        delay = self._delay() if isinstance(job, MailRequest) else 0.0
        with self._condition:
            if self._closed:
                self._logger.warning("mail_dropped kind=%s reason=stopped", _kind(job))
                return False
            if self._pending >= self._maxsize:
                self._note_drop()
                return False
            self._ensure_threads()
            self._sequence += 1
            heapq.heappush(self._heap, (self._monotonic() + delay, self._sequence, job))
            self._pending += 1
            self._condition.notify_all()
        return True

    def join(self, timeout: float | None = None) -> bool:
        """Wait until nothing is pending (tests); False if ``timeout`` ran out first."""
        with self._condition:
            return self._condition.wait_for(lambda: self._pending == 0, timeout)

    def stop(self, timeout: float = STOP_TIMEOUT_SECONDS) -> None:
        """Refuse new jobs, drop those not yet due, wait up to ``timeout`` for those in flight."""
        with self._condition:
            self._closed = True
            not_due = len(self._heap)
            self._heap.clear()
            self._pending -= not_due
            scheduler, self._scheduler = self._scheduler, None
            pool, self._pool = self._pool, None
            self._condition.notify_all()
        if not_due:
            self._logger.warning("mail_dropped count=%d reason=stopped", not_due)
        if scheduler is not None:
            scheduler.join(timeout)
        if pool is not None:
            with self._condition:
                for future in list(self._in_flight):
                    if future.cancel():  # submitted but not started: it never runs
                        self._in_flight.discard(future)
                        self._pending -= 1
            pool.shutdown(wait=False)
            with self._condition:
                if not self._condition.wait_for(lambda: self._pending == 0, timeout):
                    self._logger.warning("mail_outbox_stop_timeout")

    def _ensure_threads(self) -> None:
        # Called with the condition held, never after stop (no worker on a disposed engine).
        if self._pool is None:
            self._pool = ThreadPoolExecutor(self._senders, thread_name_prefix="mail-sender")
        if self._scheduler is None or not self._scheduler.is_alive():
            self._scheduler = threading.Thread(
                target=self._schedule, name="mail-scheduler", daemon=True
            )
            self._scheduler.start()

    def _note_drop(self) -> None:
        self._dropped += 1
        now = self._monotonic()
        if self._last_drop_warning is None or now - self._last_drop_warning >= DROP_WARNING_EVERY:
            self._logger.warning("mail_dropped count=%d reason=queue_full", self._dropped)
            self._dropped = 0
            self._last_drop_warning = now

    def _schedule(self) -> None:
        with self._condition:
            while not self._closed:
                if not self._heap:
                    self._condition.wait()
                    continue
                due = self._heap[0][0] - self._monotonic()
                if due > 0:
                    self._condition.wait(due)  # woken early by new jobs and by stop
                    continue
                _due, _sequence, job = heapq.heappop(self._heap)
                pool = self._pool
                if pool is None:
                    self._pending -= 1
                    continue
                try:
                    future = pool.submit(self._run_one, job)
                except RuntimeError:  # the pool was shut down meanwhile
                    self._pending -= 1
                    continue
                self._in_flight.add(future)
                future.add_done_callback(self._forget)
            self._condition.notify_all()

    def _forget(self, future: Future[None]) -> None:
        with self._condition:  # an RLock: safe even when called from submit's thread
            self._in_flight.discard(future)

    def _run_one(self, job: MailJob | MailRequest) -> None:
        try:
            self._handle(job)
        except Exception as exc:  # noqa: BLE001 - one bad mail never stops the others
            self._logger.warning(
                "mail_send_failed kind=%s error=%s", _kind(job), type(exc).__name__
            )
        finally:
            with self._condition:
                self._pending -= 1
                self._condition.notify_all()

    def _handle(self, job: MailJob | MailRequest) -> None:
        if isinstance(job, MailRequest):
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
