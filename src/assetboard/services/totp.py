from __future__ import annotations

import base64
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from uuid import UUID

import pyotp
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session, sessionmaker

from assetboard.branding import APP_NAME
from assetboard.clock import Clock
from assetboard.config import Settings
from assetboard.db.engine import run_immediate
from assetboard.db.models.identity import User
from assetboard.db.repositories.totp import TotpRepository
from assetboard.db.repositories.user_memberships import UserMembershipReader
from assetboard.domain.totp import RECOVERY_CODE_COUNT, matching_step, recovery_hash
from assetboard.services.auth import (
    Throttled,
    ThrottleSlot,
    Verifier,
    blocked_until,
    delete_login_tickets,
    derive_subkey,
    record_auth_event,
    reserve_attempts,
)
from assetboard.services.context import Role, WorkspaceChoice
from assetboard.services.profile import ConfirmMethod, ConfirmOutcome, confirm_sensitive

ENROLLMENT_LIFETIME = timedelta(minutes=10)
logger = logging.getLogger(f"{APP_NAME.lower()}.totp")


@dataclass(frozen=True, slots=True)
class TotpState:
    enabled: bool
    secret: str | None = None
    workspaces: tuple[WorkspaceChoice, ...] = ()


@lru_cache(maxsize=4)
def _cipher_for(secret_key: str) -> Fernet:
    # Derived once per instance secret, like the CSRF key.
    return Fernet(base64.urlsafe_b64encode(derive_subkey(secret_key, "totp")))


def _cipher(settings: Settings) -> Fernet:
    return _cipher_for(settings.ensure_secret_key())


def encrypt_secret(settings: Settings, user_id: UUID, secret: str, now: datetime) -> str:
    """Encrypt ``secret`` bound to its account: the plaintext is ``"<user_id>|<secret>"``."""
    plaintext = f"{user_id}|{secret}".encode()
    return _cipher(settings).encrypt_at_time(plaintext, int(now.timestamp())).decode()


def decrypt_secret(settings: Settings, user_id: UUID, token: str) -> str | None:
    """The secret, or ``None`` if it cannot be read or belongs to another account."""
    try:
        plaintext = _cipher(settings).decrypt(token).decode()
    except InvalidToken:
        logger.warning(
            "An authenticator secret could not be decrypted: the instance secret key has "
            "probably changed since enrollment. Authenticator codes fail for this account; "
            "recovery codes still work. See 'Secret key and two-factor authentication' in the "
            "deployment guide."
        )
        return None
    prefix = f"{user_id}|"
    if not plaintext.startswith(prefix):
        logger.warning("An authenticator secret is not bound to its account; it was refused.")
        return None
    return plaintext.removeprefix(prefix)


def state(
    factory: sessionmaker[Session],
    *,
    user_id: UUID,
    session_hash: str,
    settings: Settings,
    clock: Clock,
) -> TotpState:
    with factory() as session:
        repository = TotpRepository(session, user_id)
        workspaces = tuple(
            WorkspaceChoice(id=m.workspace_id, name=m.workspace_name, role=Role(m.role))
            for m in UserMembershipReader(session, user_id).memberships()
        )
        factor = repository.factor()
        if factor is None:
            return TotpState(False, workspaces=workspaces)
        if factor.enabled_at is not None:
            return TotpState(True, workspaces=workspaces)
        if (
            factor.enrollment_session_hash == session_hash
            and factor.enrollment_expires_at is not None
            and factor.enrollment_expires_at > clock.now()
        ):
            secret = decrypt_secret(settings, user_id, factor.encrypted_secret)
            return TotpState(False, secret, workspaces)
        return TotpState(False, workspaces=workspaces)


def begin_enrollment(
    session: Session,
    user: User,
    *,
    session_hash: str,
    settings: Settings,
    clock: Clock,
) -> bool:
    repository = TotpRepository(session, user.id)
    factor = repository.factor(lock=True)
    if user.password_hash is None or not repository.owns_session(session_hash):
        return False
    if factor is not None and factor.enabled_at is not None:
        return False
    secret = encrypt_secret(settings, user.id, pyotp.random_base32(), clock.now())
    if factor is None:
        factor = repository.add_factor(encrypted_secret=secret)
    else:
        factor.encrypted_secret = secret
    factor.enrollment_session_hash = session_hash
    factor.enrollment_expires_at = clock.now() + ENROLLMENT_LIFETIME
    factor.last_step = -1
    return True


def complete_enrollment(
    factory: sessionmaker[Session],
    *,
    user_id: UUID,
    session_hash: str,
    code: str,
    settings: Settings,
    clock: Clock,
) -> list[str] | Throttled | None:
    def complete(session: Session) -> list[str] | Throttled | None:
        repository = TotpRepository(session, user_id)
        factor = repository.factor(lock=True)
        if (
            factor is None
            or factor.enabled_at is not None
            or factor.enrollment_session_hash != session_hash
            or factor.enrollment_expires_at is None
            or factor.enrollment_expires_at <= clock.now()
            or not repository.owns_session(session_hash)
        ):
            return None
        slot = ThrottleSlot(
            "account",
            f"totp:{user_id}",
            timedelta(minutes=settings.auth_ip_window_minutes),
            settings.auth_account_free_failures,
        )
        if reserve_attempts(session, (slot,), clock.now(), settings) is None:
            return Throttled(
                blocked_until(session, [(slot.scope, slot.key)], clock.now()) or clock.now()
            )
        secret = decrypt_secret(settings, user_id, factor.encrypted_secret)
        if secret is None:
            return None
        step = matching_step(secret, code, clock.now(), factor.last_step)
        if step is None:
            return None
        factor.enabled_at = clock.now()
        delete_login_tickets(session, user_id)
        factor.enrollment_session_hash = None
        factor.enrollment_expires_at = None
        factor.last_step = step
        codes = [secrets.token_hex(16) for _ in range(RECOVERY_CODE_COUNT)]
        repository.add_recovery_codes([recovery_hash(value) for value in codes])
        repository.update_password_sessions(session_hash, verified=True)
        record_auth_event(
            session,
            clock,
            action="auth.totp_enabled",
            entity_id=str(user_id),
            actor_user_id=user_id,
        )
        return codes

    return run_immediate(factory, complete)


def regenerate_recovery_codes(
    factory: sessionmaker[Session],
    *,
    user_id: UUID,
    session_hash: str,
    password: str,
    second_factor: str,
    settings: Settings,
    clock: Clock,
    host: str | None = None,
    user_agent: str | None = None,
    verify: Verifier | None = None,
) -> ConfirmOutcome[list[str] | None]:
    """Replace every recovery code after the password and a second factor are confirmed.

    The second factor (an authenticator code or an unused recovery code) is checked with the
    password, so a wrong one counts as a failed confirmation.
    """

    def action(session: Session, user: User, method: ConfirmMethod) -> list[str] | None:
        if method != "password":
            return None
        return _regenerate_recovery_codes(session, user, session_hash=session_hash, clock=clock)

    return confirm_sensitive(
        factory,
        user_id=user_id,
        session_hash=session_hash,
        password=password,
        purpose="totp_recovery_codes",
        settings=settings,
        clock=clock,
        on_success=action,
        host=host,
        user_agent=user_agent,
        verify=verify,
        second_factor=second_factor,
    )


def _regenerate_recovery_codes(
    session: Session,
    user: User,
    *,
    session_hash: str,
    clock: Clock,
) -> list[str] | None:
    repository = TotpRepository(session, user.id)
    factor = repository.factor(lock=True)
    if (
        user.password_hash is None
        or factor is None
        or factor.enabled_at is None
        or not repository.owns_session(session_hash)
    ):
        return None
    codes = [secrets.token_hex(16) for _ in range(RECOVERY_CODE_COUNT)]
    repository.replace_recovery_codes([recovery_hash(code) for code in codes])
    record_auth_event(
        session,
        clock,
        action="auth.totp_recovery_codes_regenerated",
        entity_id=str(user.id),
        actor_user_id=user.id,
    )
    return codes


def verify_sign_in(
    session: Session,
    user_id: UUID,
    *,
    code: str,
    settings: Settings,
    now: datetime,
) -> tuple[bool, bool]:
    repository = TotpRepository(session, user_id)
    factor = repository.factor(lock=True)
    if factor is None or factor.enabled_at is None:
        return True, False
    if len(code) > 64:
        return False, False
    secret = decrypt_secret(settings, user_id, factor.encrypted_secret)
    step = None if secret is None else matching_step(secret, code.strip(), now, factor.last_step)
    if step is not None:
        factor.last_step = step
        return True, True
    recovered = repository.consume_recovery_code(recovery_hash(code))
    return recovered, recovered


def disable(
    factory: sessionmaker[Session],
    *,
    user_id: UUID,
    session_hash: str,
    password: str,
    second_factor: str,
    settings: Settings,
    clock: Clock,
    host: str | None = None,
    user_agent: str | None = None,
    verify: Verifier | None = None,
) -> ConfirmOutcome[bool]:
    """Remove the factor after the password and a second factor are confirmed.

    As for regenerating recovery codes, a wrong second factor is a failed confirmation.
    """

    def action(session: Session, user: User, method: ConfirmMethod) -> bool:
        return method == "password" and _disable(
            session, user, session_hash=session_hash, clock=clock
        )

    return confirm_sensitive(
        factory,
        user_id=user_id,
        session_hash=session_hash,
        password=password,
        purpose="totp_disable",
        settings=settings,
        clock=clock,
        on_success=action,
        host=host,
        user_agent=user_agent,
        verify=verify,
        second_factor=second_factor,
    )


def _disable(
    session: Session,
    user: User,
    *,
    session_hash: str,
    clock: Clock,
) -> bool:
    repository = TotpRepository(session, user.id)
    if user.password_hash is None or not repository.owns_session(session_hash):
        return False
    factor = repository.factor(lock=True)
    if factor is None or factor.enabled_at is None:
        return False
    repository.clear()
    repository.update_password_sessions(session_hash, verified=False)
    record_auth_event(
        session, clock, action="auth.totp_disabled", entity_id=str(user.id), actor_user_id=user.id
    )
    return True
