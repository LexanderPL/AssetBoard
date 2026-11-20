"""The sign-in throttle looks the same for a known and an unknown email (ADR-0012).

The same request sequence is sent for a known and an unknown email: attacker guesses and the
owner's password changes and confirmations, right or wrong. Everything an attacker can observe
through sign-in must be identical. A successful sign-in is the documented exception (SEC-02):
it takes back only its own attempt from the failures-only account bucket.
"""

from __future__ import annotations

import hashlib
import itertools
from datetime import datetime, timedelta
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.db.models.identity import AuthThrottle
from assetboard.services import auth
from assetboard.services.auth import (
    LoginOutcome,
    authenticate_user,
    change_password,
    create_admin_cli,
)
from tests.unit.test_auth import BASE_TIME, PASSWORD

_examples = itertools.count()


def at(seconds: float) -> FixedClock:
    return FixedClock((BASE_TIME + timedelta(seconds=seconds)).isoformat())


def _login(session: Session, config: Settings, t: float, email: str, password: str, host: str):
    return authenticate_user(
        session, email=email, password=password, settings=config, clock=at(t), host=host
    )


def _seen_by_attacker(
    session: Session, config: Settings, email: str, now: datetime
) -> tuple[Any, ...]:
    """The sign-in bucket as the next attempt reads it: the count still in its window (and
    when that window started), and an active block. An expired window counts as zero, as an
    idle row does, whatever is stored."""
    row = session.scalar(
        select(AuthThrottle).where(
            AuthThrottle.key_type == "account",
            AuthThrottle.key_hash == hashlib.sha256(email.encode()).hexdigest(),
        )
    )
    window = timedelta(minutes=config.auth_ip_window_minutes)
    count = auth._current_count(row, now, window)  # pyright: ignore[reportPrivateUsage]
    started = row.window_started_at if row is not None and count else None
    blocked = row.blocked_until if row and row.blocked_until and row.blocked_until > now else None
    return count, started, blocked


@pytest.fixture
def config() -> Settings:
    return Settings(_env_file=None, secret_key="test-key-94fc76f664e808de8653e04")


OWNER_HOST = "203.0.113.9"
# One step: which request, and how many seconds after the previous step.
#   attacker            a guess against the email
#   owner_sign_in       the owner's correct password; sent to the unknown email too, as the
#                       same request from someone who has an account elsewhere
#   owner_change_ok / owner_change_typo / owner_confirm_ok / owner_confirm_typo
#                       password change or confirmation by the signed-in owner (no analogue
#                       for an email without an account)
ACTIONS = [
    "attacker",
    "owner_sign_in",
    "owner_change_ok",
    "owner_change_typo",
    "owner_confirm_ok",
    "owner_confirm_typo",
]
# A successful owner sign-in releases its own attempt from the known email's bucket (SEC-02),
# so the property below covers the request sequences that must stay indistinguishable.
STEPS = st.lists(
    st.tuples(
        st.sampled_from([action for action in ACTIONS if action != "owner_sign_in"]),
        st.integers(min_value=0, max_value=1500),
    ),
    min_size=1,
    max_size=24,
)


def _run(
    db_session: Session, config: Settings, steps: list[tuple[str, int]]
) -> tuple[list[Any], list[Any]]:
    """Run the same request sequence against a known and an unknown email.

    Returns, per email, everything an attacker can observe: the outcome of each of their
    attempts (status, message and wait follow from it) and the sign-in bucket as the next
    attempt reads it, after every step.
    """
    n = next(_examples)
    known, unknown = f"known-{n}@example.com", f"unknown-{n}@example.com"
    # Each side's attacker comes from its own address, which sees the same sequence.
    net = f"10.{n // 250}.{n % 250}"
    attackers = {known: f"{net}.1", unknown: f"{net}.2"}
    owner, _workspace = create_admin_cli(
        db_session,
        email=known,
        display_name="Owner",
        password=PASSWORD,
        workspace_name="Home",
        settings=config,
        clock=at(0),
    )
    current = PASSWORD
    changes = itertools.count(1)
    seen: dict[str, list[Any]] = {known: [], unknown: []}
    t = 0.0
    for action, gap in steps:
        t += gap
        if action == "attacker":
            for email in (known, unknown):
                outcome = _login(db_session, config, t, email, "guess", attackers[email])
                assert not outcome.succeeded
                seen[email].append(outcome)
        elif action == "owner_sign_in":
            for email in (known, unknown):
                _login(db_session, config, t, email, current, OWNER_HOST)
        elif action.startswith("owner_change"):
            ok = action.endswith("_ok")
            new_password = f"Another Strong Pass {next(changes)}"
            outcome = change_password(
                db_session,
                owner,
                current if ok else "typo",
                new_password,
                settings=config,
                clock=at(t),
            )
            if outcome.status == "changed":
                current = new_password
        else:
            _confirm(db_session, config, owner.id, current if action.endswith("_ok") else "typo", t)
        now = at(t).now()
        for email in (known, unknown):
            seen[email].append(_seen_by_attacker(db_session, config, email, now))
    return seen[known], seen[unknown]


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(steps=STEPS)
def test_any_sequence_looks_the_same_for_known_and_unknown_emails(
    db_session: Session, config: Settings, steps: list[tuple[str, int]]
) -> None:
    known, unknown = _run(db_session, config, steps)
    assert known == unknown


def _confirm(session: Session, config: Settings, user_id: Any, password: str, t: float) -> None:
    """A password confirmation (profile, admin, sign out others, link) in this session."""
    auth._confirm_password_flow(  # pyright: ignore[reportPrivateUsage]
        auth.SameSession(session),
        user_id=user_id,
        password=password,
        purpose="test",
        settings=config,
        clock=at(t),
        host="198.51.100.20",
        user_agent=None,
        verify=None,
        on_success=lambda _session, _user: True,
    )


def test_a_successful_sign_in_releases_only_its_own_attempt(
    db_session: Session, config: Settings
) -> None:
    """The documented SEC-02 trade-off: five wrong guesses then the owner's sign-in.

    The known email's bucket loses only the owner's own attempt, so the next attacker guess is
    counted rather than refused; the unknown email also counts the failed owner request, so
    its next guess is already blocked. The response to a guess is still uniform; only the
    successful sign-in itself changes the timing, which the audit accepted.
    """
    steps = [("attacker", 0)] * 5 + [("owner_sign_in", 61), ("attacker", 1)]
    known, unknown = _run(db_session, config, steps)
    known_attempts = [item for item in known if isinstance(item, LoginOutcome)]
    unknown_attempts = [item for item in unknown if isinstance(item, LoginOutcome)]
    assert known_attempts[-1] == LoginOutcome()
    assert unknown_attempts[-1].throttled_until is not None


def test_password_change_and_confirmation_failures_leave_the_sign_in_bucket_alone(
    db_session: Session, config: Settings
) -> None:
    owner, _workspace = create_admin_cli(
        db_session,
        email="owner@example.com",
        display_name="Owner",
        password=PASSWORD,
        workspace_name="Home",
        settings=config,
        clock=at(0),
    )
    for _ in range(config.auth_account_free_failures + 3):
        change_password(
            db_session, owner, "typo", "Another Strong Pass", settings=config, clock=at(1)
        )
        _confirm(db_session, config, owner.id, "typo", 1)
    assert _seen_by_attacker(db_session, config, "owner@example.com", at(2).now()) == (
        0,
        None,
        None,
    )
    assert _login(db_session, config, 2, "owner@example.com", PASSWORD, "192.0.2.5").succeeded
