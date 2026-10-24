from datetime import UTC, datetime

import pyotp
import pytest
from hypothesis import given
from hypothesis import strategies as st

from assetboard.domain.totp import (
    TOTP_INTERVAL_SECONDS,
    TOTP_WINDOW_STEPS,
    format_recovery_code,
    matching_step,
    recovery_hash,
)

RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


@pytest.mark.parametrize("timestamp", [29, 59, 60, 89])
def test_rfc6238_sha1_vector_and_adjacent_window(timestamp: int) -> None:
    assert matching_step(RFC_SECRET, "287082", datetime.fromtimestamp(timestamp, UTC), -1) == 1


def test_expiry_replay_and_invalid_codes() -> None:
    assert matching_step(RFC_SECRET, "287082", datetime.fromtimestamp(90, UTC), -1) is None
    assert matching_step(RFC_SECRET, "287082", datetime.fromtimestamp(59, UTC), 1) is None
    for code in ("", "28708", "2870820", "２８７０８２"):
        assert matching_step(RFC_SECRET, code, datetime.fromtimestamp(59, UTC), -1) is None


def test_recovery_hash_normalizes_format_and_is_sha256() -> None:
    assert recovery_hash("AB-CD ") == recovery_hash("abcd")
    assert (
        recovery_hash("abcd") == "88d4266fd4e6338d13b845fcf289579d209c897823b9217da3e161936f031589"
    )


def test_recovery_codes_are_grouped_for_display_and_dashes_are_ignored() -> None:
    code = "0123456789abcdef0123456789abcdef"
    shown = format_recovery_code(code)
    assert shown == "0123-4567-89ab-cdef-0123-4567-89ab-cdef"
    assert recovery_hash(shown) == recovery_hash(code)


@given(
    now=st.integers(min_value=10**6, max_value=4 * 10**9),
    offset=st.integers(min_value=-3, max_value=3),
    last_offset=st.integers(min_value=-4, max_value=3),
)
def test_accepted_step_is_fresh_within_the_window_and_never_replayable(
    now: int, offset: int, last_offset: int
) -> None:
    moment = datetime.fromtimestamp(now, UTC)
    current = now // TOTP_INTERVAL_SECONDS
    last_step = current + last_offset
    code = pyotp.TOTP(RFC_SECRET).at((current + offset) * TOTP_INTERVAL_SECONDS)
    step = matching_step(RFC_SECRET, code, moment, last_step)
    if step is None:
        return
    assert step > last_step
    assert abs(step - current) <= TOTP_WINDOW_STEPS
    # Once accepted (last_step advanced to it), the same code never succeeds again.
    assert matching_step(RFC_SECRET, code, moment, step) is None
