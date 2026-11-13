"""FIRE scenarios: pure rules and constants shared by the model and the service (F8.1).

The FIRE planner stores rates in integer basis points, ages in whole years and every amount in
integer minor units of the scenario's currency. Nothing here reads the database, the clock or
the environment; the projection itself arrives with F8.2.

Signed amounts (an explicit exception to overview §3.2, which stores income and expense amounts
positive): a contribution override is the net saving (positive) or dissaving (negative) of that
year, and an event is a cash withdrawal/addition or an increase/reduction of the annual expense
or income series. The sign is the meaning, so the model keeps it.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from assetboard.domain.text import collapse_whitespace, has_rejected_characters, text_key
from assetboard.domain.transactions import EntryError, amount_minor

MIN_YEAR = 1900
MAX_YEAR = 2199
MAX_NAME_LENGTH = 200
MAX_NAME_KEY_LENGTH = 255
MAX_LABEL_LENGTH = 200
MAX_CONTRIBUTION_ROWS = 120
MAX_EVENT_ROWS = 200
MIN_HORIZON_AGE = 1
MAX_HORIZON_AGE = 120
MAX_STOP_WORKING_AGE = 120
DEFAULT_LIFE_EXPECTANCY = 90

# Hard bounds accepted by the service and the database.
RATE_HARD_MIN_BP = -2000
RATE_HARD_MAX_BP = 5000
SWR_HARD_MIN_BP = 50
SWR_HARD_MAX_BP = 2000

# Ranges treated as ordinary; a rate outside them needs one explicit confirmation.
INFLATION_SANE_BP = (0, 1000)
REAL_RETURN_SANE_BP = (-200, 800)
INCOME_GROWTH_SANE_BP = (0, 1000)
SWR_SANE_BP = (200, 600)

# Illustrative starting values (owner decision 2026-11-13): clearly editable, not advice.
ILLUSTRATIVE_INFLATION_BP = 250
ILLUSTRATIVE_REAL_RETURN_BP = 500
ILLUSTRATIVE_INCOME_GROWTH_BP = 300
ILLUSTRATIVE_SWR_BP = 400


class Rule(StrEnum):
    """How the projection decides that the portfolio covers the plan."""

    RETURNS_COVER_EXPENSES = "returns_cover_expenses"
    SWR = "swr"


class IncomeMode(StrEnum):
    MANUAL = "manual"
    TRAILING_12M = "trailing_12m"


class ExpensesMode(StrEnum):
    MANUAL = "manual"
    TRAILING_12M = "trailing_12m"


class PortfolioMode(StrEnum):
    MANUAL = "manual"
    LINKED_INVESTABLE = "linked_investable"


class EventKind(StrEnum):
    """A one-off cash flow or a permanent change to one annual series."""

    CASH = "cash"
    EXPENSE_CHANGE = "expense_change"
    INCOME_CHANGE = "income_change"


def age_at_start(start_year: int, birth_year: int) -> int:
    """The age used by validation: the scenario's start year minus the birth year."""
    return start_year - birth_year


def contribution_years(start_year: int, birth_year: int, horizon_age: int) -> range:
    """Allowed override years: the first full plan year through the horizon year."""
    return range(start_year + 1, birth_year + horizon_age + 1)


def event_years(start_year: int, birth_year: int, horizon_age: int) -> range:
    """Allowed event years: the start year itself through the horizon year."""
    return range(start_year, birth_year + horizon_age + 1)


def year_in_window(year: int, window: range) -> bool:
    return year in window


def default_horizon_age(life_expectancy: int | None) -> int:
    """The profile's life expectancy, or 90 when the profile has none."""
    if life_expectancy is None:
        return DEFAULT_LIFE_EXPECTANCY
    return life_expectancy


def in_range(value: int, bounds: tuple[int, int]) -> bool:
    low, high = bounds
    return low <= value <= high


def extreme_rates(
    *,
    inflation_bp: int,
    real_return_bp: int,
    income_growth_bp: int,
    swr_bp: int | None,
) -> bool:
    """Whether any rate falls outside its ordinary range and needs a confirmation."""
    if not in_range(inflation_bp, INFLATION_SANE_BP):
        return True
    if not in_range(real_return_bp, REAL_RETURN_SANE_BP):
        return True
    if not in_range(income_growth_bp, INCOME_GROWTH_SANE_BP):
        return True
    return swr_bp is not None and not in_range(swr_bp, SWR_SANE_BP)


def clean_scenario_name(value: str) -> str:
    """Trimmed, whitespace-collapsed, no control/invisible characters, within its column."""
    if has_rejected_characters(value.strip()):
        raise ValueError("invalid_name")
    cleaned = collapse_whitespace(value)
    if not cleaned or len(cleaned) > MAX_NAME_LENGTH:
        raise ValueError("invalid_name")
    if len(text_key(cleaned)) > MAX_NAME_KEY_LENGTH:
        raise ValueError("invalid_name")
    return cleaned


def clean_event_label(value: str) -> str:
    """Trimmed, whitespace-collapsed, non-empty and free of control/invisible characters."""
    if has_rejected_characters(value.strip()):
        raise ValueError("invalid_label")
    cleaned = collapse_whitespace(value)
    if not cleaned or len(cleaned) > MAX_LABEL_LENGTH:
        raise ValueError("invalid_label")
    return cleaned


def signed_amount_minor(amount: Decimal, currency: str) -> int:
    """Signed minor units of ``amount`` without rounding; zero is allowed.

    ``amount_minor`` refuses zero and negatives (ledger rows are positive); a FIRE contribution
    or event keeps its sign, so the magnitude goes through the shared check and the sign is
    applied afterwards. Error codes are the ledger's: ``too_many_decimals``,
    ``amount_too_large``, ``invalid_amount``.
    """
    if type(amount) is not Decimal or not amount.is_finite():
        raise EntryError("invalid_amount")
    if amount == 0:
        return 0
    magnitude = amount_minor(abs(amount), currency)
    return magnitude if amount > 0 else -magnitude
