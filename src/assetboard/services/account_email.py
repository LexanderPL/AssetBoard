"""Open registration with a verified email, and password reset by email (ADR-0017).

Both start from a form that takes only an email address and always answers the same way, so
nothing tells the requester whether an account has that address. What differs is only the
mail that goes to the address itself, after the response:

* registration: a link to finish registering (no account yet), a note that an account exists
  (active account), or nothing (deactivated account);
* password reset: a reset link (active account with a password), a note that the account has
  no password (active account without one), or nothing.

Links carry a 256-bit token stored as its SHA-256 (``email_tokens``), used once, expiring
after 24 hours (registration) or 60 minutes (reset); at most three are live per address or
account. Every request counts in the per-address bucket ``mail_email`` whether or not an
account exists; past its limit the response stays the same and no mail is sent. The per-client
buckets (``register``, ``mail_ip``) are the requester's own and may say "try later".

No account exists before the registration link is used: the password is chosen after the
link proved the address, so nobody can create an account with someone else's address.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

import anyio.to_thread
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from assetboard.clock import Clock
from assetboard.config import Settings
from assetboard.db.engine import run_immediate
from assetboard.db.models.identity import EmailToken, User
from assetboard.domain.identity import InvalidEmail, normalize_email
from assetboard.services.auth import (
    Hasher,
    PasswordPolicyError,
    RegistrationDisabled,
    RegistrationOutcome,
    SignUpLinkInvalid,
    argon2_hash,
    count_limited_attempt,
    gate_hasher,
    invalidate_reset_tokens,
    ip_throttle_key,
    prune_auth_throttles,
    record_auth_event,
    register_user_async,
    reserve_registration_in,
    revoke_all_sessions,
    validate_password,
)
from assetboard.services.hash_gate import HashGate
from assetboard.services.instance_settings import registration_open
from assetboard.services.mail import MailJob

REGISTER_LINK_TTL = timedelta(hours=24)
RESET_LINK_TTL = timedelta(minutes=60)
MAX_LIVE_TOKENS = 3
TokenPurpose = Literal["register", "reset"]
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


class PasswordResetUnavailable(RuntimeError):
    """Password reset by email is off: no SMTP, or ``OIDC_ONLY``."""


def password_reset_available(settings: Settings) -> bool:
    return settings.mail_enabled and not settings.oidc_only


RequestStatus = Literal["accepted", "invalid_email", "throttled"]


@dataclass(frozen=True, slots=True)
class MailRequestOutcome:
    """``accepted`` is the one answer for every address; ``job`` is the mail to queue, if any,
    after the transaction committed."""

    status: RequestStatus
    job: MailJob | None = field(default=None, repr=False)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _live_token(
    session: Session, purpose: TokenPurpose, token: str, now: datetime
) -> EmailToken | None:
    if not _TOKEN.fullmatch(token):
        return None
    return session.scalar(
        select(EmailToken).where(
            EmailToken.purpose == purpose,
            EmailToken.token_hash == _token_hash(token),
            EmailToken.expires_at > now,
        )
    )


def _issue_token(
    session: Session,
    *,
    purpose: TokenPurpose,
    email: str,
    user_id: UUID | None,
    locale: str | None,
    now: datetime,
) -> str:
    """Store a new link's hash; drop expired links and the oldest beyond the per-owner cap."""
    session.execute(
        delete(EmailToken)
        .where(EmailToken.expires_at <= now)
        .execution_options(synchronize_session=False)
    )
    owner = EmailToken.user_id == user_id if user_id is not None else EmailToken.email == email
    live = session.scalars(
        select(EmailToken)
        .where(EmailToken.purpose == purpose, owner)
        .order_by(EmailToken.created_at, EmailToken.id)
    ).all()
    for row in live[: max(0, len(live) - (MAX_LIVE_TOKENS - 1))]:
        session.delete(row)
    token = secrets.token_urlsafe(32)
    session.add(
        EmailToken(
            id=uuid4(),
            purpose=purpose,
            token_hash=_token_hash(token),
            email=email,
            user_id=user_id,
            locale=locale,
            created_at=now,
            expires_at=now + (REGISTER_LINK_TTL if purpose == "register" else RESET_LINK_TTL),
        )
    )
    session.flush()
    return token


def _mail_quota(session: Session, email: str, settings: Settings, now: datetime) -> bool:
    """Whether this address may get another mail now; counted the same for every address."""
    return count_limited_attempt(
        session,
        "mail_email",
        email,
        limit=settings.mail_email_limit,
        window=timedelta(minutes=settings.mail_window_minutes),
        now=now,
        settings=settings,
    )


def _link(settings: Settings, path: str) -> str:
    """Links in mail are built from BASE_URL only, never from request headers."""
    return f"{settings.base_url}{path}"


# Registration


def start_registration(
    factory: sessionmaker[Session],
    *,
    email: str,
    settings: Settings,
    clock: Clock,
    host: str | None,
    locale: str,
) -> MailRequestOutcome:
    """Step 1: the same outcome for every address. Raises ``RegistrationDisabled`` when open
    registration is off (or first-run setup is still open)."""
    try:
        normalized = normalize_email(email)
    except InvalidEmail:
        return MailRequestOutcome("invalid_email")

    def step(session: Session) -> MailRequestOutcome:
        if not registration_open(session, settings):
            raise RegistrationDisabled
        if not reserve_registration_in(session, host=host, settings=settings, clock=clock):
            return MailRequestOutcome("throttled")
        now = clock.now()
        if not _mail_quota(session, normalized, settings, now):
            return MailRequestOutcome("accepted")
        user = session.scalar(select(User).where(User.email == normalized))
        if user is None:
            token = _issue_token(
                session,
                purpose="register",
                email=normalized,
                user_id=None,
                locale=locale,
                now=now,
            )
            link = _link(settings, f"/register/complete/{token}")
            return MailRequestOutcome(
                "accepted", MailJob("register_verify", normalized, locale, {"link": link})
            )
        if not user.is_active:
            return MailRequestOutcome("accepted")
        params = {
            "sign_in_link": _link(settings, "/login"),
            "reset_link": _link(settings, "/password/forgot"),
        }
        return MailRequestOutcome(
            "accepted", MailJob("register_existing", normalized, user.locale or locale, params)
        )

    return run_immediate(factory, step)


def registration_email(factory: sessionmaker[Session], token: str, clock: Clock) -> str | None:
    """The address a live registration link was sent to, or ``None``."""
    with factory() as session:
        row = _live_token(session, "register", token, clock.now())
        return row.email if row is not None else None


@dataclass(frozen=True, slots=True)
class EmailLinkSignUp:
    """``auth.VerifiedSignUp`` for a registration link."""

    factory: sessionmaker[Session]
    token: str
    clock: Clock

    def check(self, email: str) -> None:
        if registration_email(self.factory, self.token, self.clock) != email:
            raise SignUpLinkInvalid

    def consume(self, session: Session, email: str) -> None:
        row = _live_token(session, "register", self.token, self.clock.now())
        if row is None or row.email != email:
            raise SignUpLinkInvalid
        # Every registration link of this address is spent with the account's creation.
        session.execute(
            delete(EmailToken)
            .where(EmailToken.purpose == "register", EmailToken.email == email)
            .execution_options(synchronize_session=False)
        )


def _registration_is_open(factory: sessionmaker[Session], settings: Settings) -> bool:
    with factory() as session:
        return registration_open(session, settings)


async def complete_registration_async(
    factory: sessionmaker[Session],
    gate: HashGate,
    *,
    token: str,
    display_name: str,
    password: str,
    workspace_name: str,
    settings: Settings,
    clock: Clock,
    host: str | None,
    user_agent: str | None,
    locale: str,
) -> RegistrationOutcome:
    """Step 2: create the account, its workspace and session data from a live link.

    Raises ``RegistrationDisabled`` when open registration was turned off meanwhile.
    """
    if not await anyio.to_thread.run_sync(lambda: _registration_is_open(factory, settings)):
        raise RegistrationDisabled
    email = await anyio.to_thread.run_sync(lambda: registration_email(factory, token, clock))
    if email is None:
        return RegistrationOutcome(status="link_invalid")
    return await register_user_async(
        factory,
        gate,
        email=email,
        display_name=display_name,
        password=password,
        workspace_name=workspace_name,
        settings=settings,
        clock=clock,
        host=host,
        user_agent=user_agent,
        workspace_locale=locale,
        seed_locale=locale,
        verified=EmailLinkSignUp(factory, token, clock),
    )


# Password reset


def request_password_reset(
    factory: sessionmaker[Session],
    *,
    email: str,
    settings: Settings,
    clock: Clock,
    host: str | None,
    user_agent: str | None,
    locale: str,
) -> MailRequestOutcome:
    """The same outcome for every address; raises ``PasswordResetUnavailable`` when off."""
    if not password_reset_available(settings):
        raise PasswordResetUnavailable
    try:
        normalized = normalize_email(email)
    except InvalidEmail:
        return MailRequestOutcome("invalid_email")

    def step(session: Session) -> MailRequestOutcome:
        now = clock.now()
        if not count_limited_attempt(
            session,
            "mail_ip",
            ip_throttle_key(host),
            limit=settings.mail_ip_limit,
            window=timedelta(minutes=settings.mail_window_minutes),
            now=now,
            settings=settings,
        ):
            return MailRequestOutcome("throttled")
        prune_auth_throttles(session, clock)
        if not _mail_quota(session, normalized, settings, now):
            return MailRequestOutcome("accepted")
        user = session.scalar(select(User).where(User.email == normalized))
        if user is None or not user.is_active:
            return MailRequestOutcome("accepted")
        mail_locale = user.locale or locale
        if user.password_hash is None:
            # Signs in through an identity provider or a proxy: no password is ever added here.
            params = {"sign_in_link": _link(settings, "/login")}
            return MailRequestOutcome(
                "accepted",
                MailJob("password_reset_no_password", normalized, mail_locale, params),
            )
        token = _issue_token(
            session,
            purpose="reset",
            email=normalized,
            user_id=user.id,
            locale=mail_locale,
            now=now,
        )
        record_auth_event(
            session,
            clock,
            action="auth.password_reset_requested",
            entity_id=str(user.id),
            host=host,
            user_agent=user_agent,
        )
        link = _link(settings, f"/password/reset/{token}")
        return MailRequestOutcome(
            "accepted", MailJob("password_reset", normalized, mail_locale, {"link": link})
        )

    return run_immediate(factory, step)


@dataclass(frozen=True, slots=True)
class ResetTarget:
    user_id: UUID
    email: str
    display_name: str


def reset_target(factory: sessionmaker[Session], token: str, clock: Clock) -> ResetTarget | None:
    """The active account with a password that a live reset link is for, or ``None``."""
    with factory() as session:
        row = _live_token(session, "reset", token, clock.now())
        user = session.get(User, row.user_id) if row is not None and row.user_id else None
        if user is None or not user.is_active or user.password_hash is None:
            return None
        return ResetTarget(user.id, user.email, user.display_name)


ResetStatus = Literal["done", "link_invalid", "mismatch", "invalid", "throttled"]


@dataclass(frozen=True, slots=True)
class ResetOutcome:
    status: ResetStatus
    error: PasswordPolicyError | None = None
    sessions_revoked: int = 0


def reset_password(
    factory: sessionmaker[Session],
    *,
    token: str,
    password: str,
    confirm_password: str,
    settings: Settings,
    clock: Clock,
    host: str | None,
    user_agent: str | None,
    hasher: Hasher | None = None,
) -> ResetOutcome:
    """Set a new password from a live link: every session of the account ends, every reset
    link of it is spent, and the account is not signed in."""
    if not password_reset_available(settings):
        raise PasswordResetUnavailable

    def count(session: Session) -> bool:
        return count_limited_attempt(
            session,
            "mail_ip",
            ip_throttle_key(host),
            limit=settings.mail_ip_limit,
            window=timedelta(minutes=settings.mail_window_minutes),
            now=clock.now(),
            settings=settings,
        )

    if not run_immediate(factory, count):
        return ResetOutcome("throttled")
    target = reset_target(factory, token, clock)
    if target is None:
        return ResetOutcome("link_invalid")
    if password != confirm_password:
        return ResetOutcome("mismatch")
    try:
        validate_password(password, target.email, target.display_name)
    except PasswordPolicyError as exc:
        return ResetOutcome("invalid", error=exc)
    new_hash = (hasher or argon2_hash)(password)

    def apply(session: Session) -> ResetOutcome:
        row = _live_token(session, "reset", token, clock.now())
        user = session.get(User, target.user_id)
        if (
            row is None
            or row.user_id != target.user_id
            or user is None
            or not user.is_active
            or user.password_hash is None
        ):
            return ResetOutcome("link_invalid")
        user.password_hash = new_hash
        user.must_change_password = False
        invalidate_reset_tokens(session, user.id)
        revoked = revoke_all_sessions(session, user.id)
        record_auth_event(
            session,
            clock,
            action="auth.password_reset",
            entity_id=str(user.id),
            actor_user_id=user.id,
            host=host,
            user_agent=user_agent,
            data={"sessions_revoked": revoked},
        )
        session.flush()
        return ResetOutcome("done", sessions_revoked=revoked)

    return run_immediate(factory, apply)


async def reset_password_async(
    factory: sessionmaker[Session],
    gate: HashGate,
    *,
    token: str,
    password: str,
    confirm_password: str,
    settings: Settings,
    clock: Clock,
    host: str | None,
    user_agent: str | None,
) -> ResetOutcome:
    """Web entry point: hashing goes through the hash gate (``HashGateBusy`` when full)."""
    hasher = gate_hasher(gate)
    return await anyio.to_thread.run_sync(
        lambda: reset_password(
            factory,
            token=token,
            password=password,
            confirm_password=confirm_password,
            settings=settings,
            clock=clock,
            host=host,
            user_agent=user_agent,
            hasher=hasher,
        )
    )
