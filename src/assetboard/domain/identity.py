from __future__ import annotations

import re
import unicodedata

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
MAX_EMAIL_LENGTH = 320


class InvalidEmail(ValueError):
    code = "invalid_email"

    def __init__(self) -> None:
        super().__init__("Enter a valid email address")


# Characters with a meaning in address headers (lists, display names, quoting, routes). A new
# address with one of them is refused, so a stored address is always one plain addr-spec.
_UNSAFE_EMAIL_CHARACTERS = frozenset('<>()[],;:"\\')


def normalize_email(email: str) -> str:
    """The lookup form of an address (stripped, lowercased). Lenient, so addresses stored
    before the stricter rule for new addresses keep working."""
    normalized = email.strip().lower()
    if len(normalized) > MAX_EMAIL_LENGTH or not _EMAIL_RE.fullmatch(normalized):
        raise InvalidEmail
    return normalized


def normalize_new_email(email: str) -> str:
    """``normalize_email`` for an address entering the system (a new account, registration,
    a password reset request): also refuses header-special characters (ADR-0017)."""
    normalized = normalize_email(email)
    if any(character in _UNSAFE_EMAIL_CHARACTERS for character in normalized):
        raise InvalidEmail
    return normalized


MAX_DISPLAY_NAME_LENGTH = 120
# Control and format characters (U+202E and friends), line and paragraph separators,
# private-use and unassigned code points never belong in a name someone else will see.
_REJECTED_NAME_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Co", "Cn"})


class InvalidDisplayName(ValueError):
    code = "invalid_display_name"

    def __init__(self) -> None:
        super().__init__(
            "Display name must contain 1 to 120 characters, without control or invisible characters"
        )


def normalize_display_name(value: str) -> str:
    """One display-name rule for setup, registration, invitation sign-up and the profile.

    Surrounding whitespace is dropped and inner runs of whitespace become one space; control
    and format characters (including bidirectional overrides such as U+202E) are refused, as is
    an empty or over-long result.
    """
    stripped = value.strip()
    if any(unicodedata.category(character) in _REJECTED_NAME_CATEGORIES for character in stripped):
        raise InvalidDisplayName
    cleaned = " ".join(stripped.split())
    if not cleaned or len(cleaned) > MAX_DISPLAY_NAME_LENGTH:
        raise InvalidDisplayName
    return cleaned
