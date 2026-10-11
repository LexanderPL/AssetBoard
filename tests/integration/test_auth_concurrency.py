"""Concurrency of the auth services on the real engine configuration (file SQLite, WAL,
explicit BEGIN / opt-in BEGIN IMMEDIATE per ADR-0007), driven by threads."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.db.engine import create_database_engine
from assetboard.db.migrate import upgrade_database
from assetboard.db.models.identity import AuthThrottle, User, Workspace
from assetboard.services import auth
from assetboard.services.auth import (
    LoginOutcome,
    SetupAlreadyCompleted,
    authenticate_user_async,
    authenticate_with_factory,
    change_password_with_factory,
    run_initial_setup,
    setup_is_open,
)
from assetboard.services.hash_gate import HashGate, HashGateBusy

PASSWORD = "Correct Horse Battery!"
CLOCK = FixedClock("2026-01-01T00:00:00+00:00")
SETTINGS = Settings(_env_file=None)


_ENGINES: list[Engine] = []


@pytest.fixture
def database(tmp_path: Path) -> Iterator[tuple[str, Settings]]:
    url = f"sqlite:///{tmp_path / 'auth.db'}"
    upgrade_database(Settings(_env_file=None, data_dir=tmp_path, database_url=url))
    try:
        yield url, SETTINGS
    finally:
        while _ENGINES:
            _ENGINES.pop().dispose()


def _factory(url: str, *, busy_timeout_ms: int = 5000) -> sessionmaker[Session]:
    engine = create_database_engine(url, busy_timeout_ms=busy_timeout_ms)
    _ENGINES.append(engine)
    return sessionmaker(engine, info={"clock": CLOCK}, expire_on_commit=False)


def _setup_admin(factory: sessionmaker[Session], email: str = "admin@example.com") -> User:
    user, _workspace = run_initial_setup(
        factory,
        email=email,
        display_name="Admin",
        password=PASSWORD,
        workspace_name="Home",
        settings=SETTINGS,
        clock=CLOCK,
    )
    return user


def _throttle(factory: sessionmaker[Session], scope: str, key: str) -> AuthThrottle | None:
    with factory() as session:
        return session.scalar(
            select(AuthThrottle).where(
                AuthThrottle.key_type == scope,
                AuthThrottle.key_hash == hashlib.sha256(key.encode()).hexdigest(),
            )
        )


class Overlap:
    """Holds every verification open until all requests have either verified or been refused.

    This forces the worst case: all requests are in flight at the same time, so any request
    that got past the throttle is counted while the others are still deciding.
    """

    def __init__(self, total: int) -> None:
        self.total = total
        self.arrived = 0
        self.verifications = 0
        self.condition = threading.Condition()

    def arrive(self) -> None:
        with self.condition:
            self.arrived += 1
            self.condition.notify_all()

    def verify(self, _password_hash: str, _password: str) -> bool:
        with self.condition:
            self.verifications += 1
            self.arrived += 1
            self.condition.notify_all()
            if not self.condition.wait_for(lambda: self.arrived >= self.total, timeout=20):
                raise TimeoutError("not all requests arrived")
        return False


def _run_threads(count: int, target: Callable[[int], None]) -> None:
    errors: list[BaseException] = []

    def wrapped(index: int) -> None:
        try:
            target(index)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=wrapped, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()
    assert errors == []


def test_parallel_login_burst_verifies_at_most_free_plus_one_per_account(
    database: tuple[str, Settings],
) -> None:
    factory = _factory(database[0])
    _setup_admin(factory)
    overlap = Overlap(total=20)
    outcomes: list[LoginOutcome] = []

    def attempt(index: int) -> None:
        outcomes.append(
            authenticate_with_factory(
                factory,
                email="admin@example.com",
                password="wrong password",
                settings=SETTINGS,
                clock=CLOCK,
                host=f"192.0.2.{index + 1}",  # distinct addresses: only the account bucket applies
                verify=overlap.verify,
            )
        )

    _run_threads(20, attempt)
    # Reservations are serialised, so exactly free + 1 requests get to verify a password.
    assert overlap.verifications == SETTINGS.auth_account_free_failures + 1
    assert outcomes == [LoginOutcome()] * 20
    account = _throttle(factory, "account", "admin@example.com")
    assert account is not None
    assert account.failure_count == overlap.verifications
    assert account.blocked_until is not None


@pytest.fixture(autouse=True)
def count_refusals(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Overlap]]:
    """Requests refused by the throttle never reach the verifier; count their arrival here."""
    registered: list[Overlap] = []
    original = auth.reserve_attempts

    def reserve(*args: object, **kwargs: object) -> object:
        result = original(*args, **kwargs)  # type: ignore[arg-type]
        if result is None:
            for overlap in registered:
                overlap.arrive()
        return result

    monkeypatch.setattr(auth, "reserve_attempts", reserve)
    original_init = Overlap.__init__

    def init(self: Overlap, total: int) -> None:
        original_init(self, total)
        registered.append(self)

    monkeypatch.setattr(Overlap, "__init__", init)
    yield registered


def test_parallel_login_burst_verifies_at_most_free_plus_one_per_ip(
    database: tuple[str, Settings],
) -> None:
    factory = _factory(database[0])
    overlap = Overlap(total=30)
    outcomes: list[LoginOutcome] = []

    def attempt(index: int) -> None:
        outcomes.append(
            authenticate_with_factory(
                factory,
                email=f"spray-{index}@example.com",
                password="wrong password",
                settings=SETTINGS,
                clock=CLOCK,
                host="198.51.100.7",
                verify=overlap.verify,
            )
        )

    _run_threads(30, attempt)
    assert overlap.verifications == SETTINGS.auth_ip_free_failures + 1
    assert outcomes == [LoginOutcome()] * 30
    ip_row = _throttle(factory, "ip", "198.51.100.7/32")
    assert ip_row is not None and ip_row.failure_count == overlap.verifications


def test_parallel_wrong_current_passwords_verify_at_most_free_plus_one(
    database: tuple[str, Settings],
) -> None:
    factory = _factory(database[0])
    user = _setup_admin(factory)
    overlap = Overlap(total=12)
    statuses: list[str] = []

    def attempt(_index: int) -> None:
        outcome = change_password_with_factory(
            factory,
            user_id=user.id,
            current_password="wrong current",
            new_password="Another Strong Password",
            settings=SETTINGS,
            clock=CLOCK,
            verify=overlap.verify,
        )
        statuses.append(outcome.status)

    _run_threads(12, attempt)
    verified = overlap.verifications
    assert verified == SETTINGS.auth_account_free_failures + 1
    assert sorted(statuses) == sorted(
        ["invalid_current"] * verified + ["throttled"] * (12 - verified)
    )


def test_login_releases_its_own_reservation_when_verification_fails(
    database: tuple[str, Settings],
) -> None:
    factory = _factory(database[0])
    _setup_admin(factory)
    for _ in range(SETTINGS.auth_account_free_failures):
        authenticate_with_factory(
            factory,
            email="admin@example.com",
            password="wrong",
            settings=SETTINGS,
            clock=CLOCK,
            host="192.0.2.1",
        )

    def broken(_password_hash: str, _password: str) -> bool:
        raise RuntimeError("worker died")

    with pytest.raises(RuntimeError):
        authenticate_with_factory(
            factory,
            email="admin@example.com",
            password="wrong",
            settings=SETTINGS,
            clock=CLOCK,
            host="192.0.2.1",
            verify=broken,
        )
    account = _throttle(factory, "account", "admin@example.com")
    ip_row = _throttle(factory, "ip", "192.0.2.1/32")
    assert account is not None and ip_row is not None
    # The 6th reservation set a block; releasing it restored the previous state.
    assert (account.failure_count, account.blocked_until) == (5, None)
    assert ip_row.failure_count == 5


def test_change_password_releases_its_reservation_when_hashing_fails(
    database: tuple[str, Settings],
) -> None:
    factory = _factory(database[0])
    user = _setup_admin(factory)

    def broken_hasher(_password: str) -> str:
        raise RuntimeError("worker died")

    with pytest.raises(RuntimeError):
        change_password_with_factory(
            factory,
            user_id=user.id,
            current_password=PASSWORD,
            new_password="Another Strong Password",
            settings=SETTINGS,
            clock=CLOCK,
            hasher=broken_hasher,
        )
    account = _throttle(factory, "account", "admin@example.com")
    assert account is not None and account.failure_count == 0
    with factory() as session:
        stored = session.get(User, user.id)
        assert stored is not None and auth.verify_password(stored.password_hash or "", PASSWORD)


def test_busy_hash_gate_releases_the_reservation(database: tuple[str, Settings]) -> None:
    factory = _factory(database[0])
    _setup_admin(factory)

    async def scenario() -> None:
        gate = HashGate(1, max_waiting=0)
        hold = threading.Event()
        occupied = asyncio.create_task(gate.run(lambda: hold.wait(10)))
        await asyncio.sleep(0)
        try:
            with pytest.raises(HashGateBusy):
                await authenticate_user_async(
                    factory,
                    gate,
                    email="admin@example.com",
                    password=PASSWORD,
                    settings=SETTINGS,
                    clock=CLOCK,
                    host="192.0.2.9",
                )
        finally:
            hold.set()
            await occupied

    asyncio.run(scenario())
    account = _throttle(factory, "account", "admin@example.com")
    ip_row = _throttle(factory, "ip", "192.0.2.9/32")
    assert account is not None and account.failure_count == 0
    assert ip_row is not None and ip_row.failure_count == 0


def test_async_login_through_the_gate_succeeds(database: tuple[str, Settings]) -> None:
    factory = _factory(database[0])
    _setup_admin(factory)

    async def scenario() -> LoginOutcome:
        return await authenticate_user_async(
            factory,
            HashGate(2),
            email="admin@example.com",
            password=PASSWORD,
            settings=SETTINGS,
            clock=CLOCK,
            host="192.0.2.9",
        )

    outcome = asyncio.run(scenario())
    assert outcome.succeeded


@pytest.mark.parametrize("variant", ["waits_for_lock", "lock_timeout"])
def test_concurrent_setup_has_one_winner_and_a_setup_already_completed_loser(
    database: tuple[str, Settings], monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    url = database[0]
    winner_factory = _factory(url)
    loser_factory = _factory(url, busy_timeout_ms=5000 if variant == "waits_for_lock" else 50)
    claimed, release_winner, loser_done = threading.Event(), threading.Event(), threading.Event()
    original_claim = auth._claim_setup  # pyright: ignore[reportPrivateUsage]

    def claim(session: Session, clock: FixedClock) -> bool:
        won = original_claim(session, clock)
        if threading.current_thread().name == "winner":
            claimed.set()
            assert release_winner.wait(timeout=20)
        return won

    monkeypatch.setattr(auth, "_claim_setup", claim)
    results: dict[str, object] = {}

    def run(name: str, factory: sessionmaker[Session]) -> None:
        try:
            results[name] = run_initial_setup(
                factory,
                email=f"{name}@example.com",
                display_name=name.title(),
                password=PASSWORD,
                workspace_name=name,
                settings=SETTINGS,
                clock=CLOCK,
            )
        except BaseException as exc:  # noqa: BLE001 - asserted below
            results[name] = exc
        finally:
            if name == "loser":
                loser_done.set()

    winner = threading.Thread(target=run, args=("winner", winner_factory), name="winner")
    loser = threading.Thread(target=run, args=("loser", loser_factory), name="loser")
    winner.start()
    assert claimed.wait(timeout=20)
    loser.start()
    if variant == "lock_timeout":
        # The winner keeps the write lock until the loser has given up waiting for it.
        assert loser_done.wait(timeout=20)
    release_winner.set()
    winner.join(timeout=20)
    loser.join(timeout=20)

    assert isinstance(results["winner"], tuple)
    loser_result = results["loser"]
    assert isinstance(loser_result, SetupAlreadyCompleted)
    assert loser_result.code == "setup_completed"
    if variant == "lock_timeout":
        assert isinstance(loser_result.__cause__, OperationalError)
    with winner_factory() as session:
        assert session.scalars(select(User.email)).all() == ["winner@example.com"]
        assert len(session.scalars(select(Workspace)).all()) == 1
        assert not setup_is_open(session)
