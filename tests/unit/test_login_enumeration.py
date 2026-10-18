"""The sign-in throttle looks the same for a known and an unknown email (ADR-0012).

Whatever an attacker does against an email, and whatever the account's owner does in between
(signing in, changing or confirming a password), the attacker's sign-in attempts get the same
outcomes and leave the same throttle state as for an email that has no account.
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
    return Settings(_env_file=None, secret_key="test-master-key")


# One step: who acts, and how many seconds after the previous step.
STEPS = st.lists(
    st.tuples(
        st.sampled_from(["attacker", "owner_sign_in", "owner_change", "owner_confirm"]),
        st.integers(min_value=0, max_value=1500),
    ),
    min_size=1,
    max_size=24,
)


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(steps=STEPS)
def test_any_sequence_looks_the_same_for_known_and_unknown_emails(
    db_session: Session, config: Settings, steps: list[tuple[str, int]]
) -> None:
    n = next(_examples)
    known, unknown = f"known-{n}@example.com", f"unknown-{n}@example.com"
    # The two attackers come from different addresses that see the same sequence, so their
    # address buckets stay alike too; the owner signs in from elsewhere.
    net = f"10.{n // 250}.{n % 250}"
    attacker_known, attacker_unknown, owner_host = f"{net}.1", f"{net}.2", f"{net}.3"
    owner, _workspace = create_admin_cli(
        db_session,
        email=known,
        display_name="Owner",
        password=PASSWORD,
        workspace_name="Home",
        settings=config,
        clock=at(0),
    )
    t = 0.0
    for actor, gap in steps:
        t += gap
        if actor == "attacker":
            seen_known = _login(db_session, config, t, known, "guess", attacker_known)
            seen_unknown = _login(db_session, config, t, unknown, "guess", attacker_unknown)
            assert seen_known == seen_unknown
            assert not seen_known.succeeded
        elif actor == "owner_sign_in":
            _login(db_session, config, t, known, PASSWORD, owner_host)
        elif actor == "owner_change":
            # A wrong current password, then (sometimes) the right one: neither reaches the
            # sign-in bucket.
            change_password(
                db_session, owner, "typo", "Another Strong Pass", settings=config, clock=at(t)
            )
        else:
            _confirm(db_session, config, owner.id, "typo", t)
        now = at(t).now()
        assert _seen_by_attacker(db_session, config, known, now) == _seen_by_attacker(
            db_session, config, unknown, now
        )


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


def test_the_owner_signing_in_between_blocks_changes_nothing_for_the_attacker(
    db_session: Session, config: Settings
) -> None:
    """The reviewer's scenario: build the counter, wait out the block, the owner signs in,
    try again. Before the fix the owner's sign-in reset the bucket and the last attempt got the
    ordinary message for a known email but another block for an unknown one."""
    create_admin_cli(
        db_session,
        email="victim@example.com",
        display_name="Victim",
        password=PASSWORD,
        workspace_name="Home",
        settings=config,
        clock=at(0),
    )
    free = config.auth_account_free_failures
    base = config.auth_throttle_base_seconds

    def attack(email: str, host: str) -> list[LoginOutcome]:
        outcomes = [_login(db_session, config, 0, email, "guess", host) for _ in range(free + 1)]
        if email == "victim@example.com":
            owner = _login(db_session, config, base + 1, email, PASSWORD, "203.0.113.9")
            assert owner.succeeded
        outcomes.append(_login(db_session, config, base + 2, email, "guess", host))
        return outcomes

    known = attack("victim@example.com", "192.0.2.1")
    unknown = attack("nobody@example.com", "192.0.2.2")
    assert known == unknown
    # The last attempt is verified (the block had ended) and fails like any other.
    assert known[-1] == LoginOutcome()
    # It is the seventh failure in the window, so it blocks again, for twice as long.
    then = at(base + 3).now()
    blocked = BASE_TIME + timedelta(seconds=base + 2 + 2 * base)
    assert _seen_by_attacker(db_session, config, "victim@example.com", then)[2] == blocked
    assert _seen_by_attacker(db_session, config, "nobody@example.com", then)[2] == blocked


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
