"""FIRE scenarios and the pure projection engine (F8.1-F8.2).

The FIRE planner stores rates in integer basis points, ages in whole years and every amount in
integer minor units of the scenario's currency. Nothing here reads the database, the clock or
the environment.

Signed amounts (an explicit exception to overview §3.2, which stores income and expense amounts
positive): a contribution override is the net saving (positive) or dissaving (negative) of that
year, and an event is a cash withdrawal/addition or an increase/reduction of the annual expense
or income series. The sign is the meaning, so the model keeps it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from assetboard.domain.money import Money, minor_unit_for
from assetboard.domain.text import collapse_whitespace, has_rejected_characters, text_key
from assetboard.domain.transactions import MAX_AMOUNT_MINOR, EntryError, amount_minor

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


def extreme_rate_change(
    *,
    inflation_bp: int,
    real_return_bp: int,
    income_growth_bp: int,
    swr_bp: int | None,
    previous: tuple[int, int, int, int | None] | None,
) -> bool:
    """Whether a **changed** rate falls outside its ordinary range.

    ``previous`` is the saved ``(inflation, real return, income growth, withdrawal)`` tuple, or
    ``None`` for a new scenario. Only a field whose value changed asks for confirmation again,
    so editing another field of a plan with an unusual rate does not nag on every save.
    """
    current = (inflation_bp, real_return_bp, income_growth_bp, swr_bp)
    bounds = (INFLATION_SANE_BP, REAL_RETURN_SANE_BP, INCOME_GROWTH_SANE_BP, SWR_SANE_BP)
    for value, sane, saved in zip(
        current, bounds, previous or (None, None, None, None), strict=True
    ):
        if value is not None and not in_range(value, sane) and (saved is None or value != saved):
            return True
    return False


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


class FireProjectionError(ValueError):
    """Invalid projection input; ``field`` and ``code`` name the problem for the caller."""

    def __init__(self, field: str, code: str) -> None:
        super().__init__(code)
        self.field = field
        self.code = code


@dataclass(frozen=True, slots=True)
class ContributionOverride:
    """One plan year whose net line replaces ``income - expenses`` (signed nominal units)."""

    year: int
    amount: Money


@dataclass(frozen=True, slots=True)
class ProjectionEvent:
    """A one-off cash flow or a permanent change to one annual series.

    ``CASH`` amounts are nominal minor units in their event year. ``INCOME_CHANGE`` and
    ``EXPENSE_CHANGE`` amounts are annual amounts in the scenario start year's (today's) money
    and stay active from the event year onward.
    """

    year: int
    kind: EventKind
    amount: Money


@dataclass(frozen=True, slots=True)
class ProjectionInputs:
    """Everything the pure projection needs; every amount is in ``currency``."""

    currency: str
    birth_year: int
    start_year: int
    horizon_age: int
    stop_working_age: int
    inflation_bp: int
    real_return_bp: int
    income_growth_bp: int
    rule: Rule
    swr_bp: int | None
    start_income: Money
    start_expenses: Money
    start_portfolio: Money
    contributions: tuple[ContributionOverride, ...] = ()
    events: tuple[ProjectionEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class ProjectionRow:
    """One plan year, nominal and in the start year's money.

    ``net`` at ``t = 0`` is the starting portfolio (the spreadsheet reuses that column), so the
    first row's net worth is ``net + gain + cash``; later rows add ``net`` and ``cash`` to the
    opening net worth. ``income_stopped`` marks the first year whose base salary is zero, not
    event income.
    """

    year: int
    age: int
    income: Money
    expenses: Money
    net: Money
    gain: Money
    cash: Money
    net_worth: Money
    real_income: Money
    real_expenses: Money
    real_net: Money
    real_gain: Money
    real_cash: Money
    real_net_worth: Money
    is_fire: bool
    income_stopped: bool


@dataclass(frozen=True, slots=True)
class Projection:
    """A complete projection plus the metrics the scenario page and comparison show."""

    inputs: ProjectionInputs
    rows: tuple[ProjectionRow, ...]
    fire_index: int | None
    fire_year: int | None
    fire_age: int | None
    years_to_fire: int | None
    networth_at_fire: Money | None
    real_networth_at_fire: Money | None
    fire_number_today: Money | None
    progress_sheet_bp: int | None
    progress_today_bp: int | None
    depletion_age: int | None
    min_networth_after_stop: Money | None


@dataclass(frozen=True, slots=True)
class _ProjectionPlan:
    currency: str
    birth_year: int
    start_year: int
    horizon_age: int
    stop_working_age: int
    inflation_bp: int
    real_return_bp: int
    income_growth_bp: int
    rule: Rule
    swr_bp: int | None
    start_income_minor: int
    start_expenses_minor: int
    start_portfolio_minor: int
    overrides: dict[int, int]
    cash_by_year: dict[int, int]
    income_changes: dict[int, int]
    expense_changes: dict[int, int]


def _integer(value: object, field: str, code: str) -> int:
    if type(value) is not int:
        raise FireProjectionError(field, code)
    return value


def _currency(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise FireProjectionError(field, "invalid_currency")
    try:
        minor_unit_for(value)
    except ValueError:
        raise FireProjectionError(field, "invalid_currency") from None
    return value.upper()


def _rate(value: object, field: str) -> int:
    rate = _integer(value, field, "invalid_rate")
    if not RATE_HARD_MIN_BP <= rate <= RATE_HARD_MAX_BP:
        raise FireProjectionError(field, "invalid_rate")
    return rate


def _money_minor(money: Money, field: str, currency: str, *, signed: bool) -> int:
    if money.currency != currency:
        raise FireProjectionError(field, "invalid_currency")
    amount = money.amount_minor
    if abs(amount) > MAX_AMOUNT_MINOR or (not signed and amount < 0):
        raise FireProjectionError(field, "invalid_amount")
    return amount


def _validate(inputs: ProjectionInputs) -> _ProjectionPlan:
    currency = _currency(inputs.currency, "currency")

    birth_year = _integer(inputs.birth_year, "birth_year", "invalid_birth_year")
    start_year = _integer(inputs.start_year, "start_year", "invalid_start_year")
    if not MIN_YEAR <= birth_year <= MAX_YEAR:
        raise FireProjectionError("birth_year", "invalid_birth_year")
    if not MIN_YEAR <= start_year <= MAX_YEAR:
        raise FireProjectionError("start_year", "invalid_start_year")
    if birth_year > start_year:
        raise FireProjectionError("birth_year", "invalid_birth_year")
    age = age_at_start(start_year, birth_year)

    horizon_age = _integer(inputs.horizon_age, "horizon_age", "invalid_horizon")
    if not MIN_HORIZON_AGE <= horizon_age <= MAX_HORIZON_AGE:
        raise FireProjectionError("horizon_age", "invalid_horizon")
    if horizon_age <= age:
        raise FireProjectionError("horizon_age", "horizon_not_after_age")
    last_year = birth_year + horizon_age
    if last_year > MAX_YEAR:
        raise FireProjectionError("horizon_age", "invalid_horizon")

    stop_working_age = _integer(inputs.stop_working_age, "stop_working_age", "invalid_stop_age")
    if not 0 <= stop_working_age <= MAX_STOP_WORKING_AGE:
        raise FireProjectionError("stop_working_age", "invalid_stop_age")
    if stop_working_age < age:
        raise FireProjectionError("stop_working_age", "stop_before_age")

    inflation_bp = _rate(inputs.inflation_bp, "inflation_bp")
    real_return_bp = _rate(inputs.real_return_bp, "real_return_bp")
    income_growth_bp = _rate(inputs.income_growth_bp, "income_growth_bp")

    try:
        rule = Rule(inputs.rule)
    except (TypeError, ValueError):
        raise FireProjectionError("rule", "invalid_rule") from None
    swr_bp: int | None
    if rule is Rule.SWR:
        if inputs.swr_bp is None:
            raise FireProjectionError("swr_bp", "invalid_swr")
        swr_bp = _integer(inputs.swr_bp, "swr_bp", "invalid_swr")
        if not SWR_HARD_MIN_BP <= swr_bp <= SWR_HARD_MAX_BP:
            raise FireProjectionError("swr_bp", "invalid_swr")
    else:
        if inputs.swr_bp is not None:
            raise FireProjectionError("swr_bp", "invalid_swr")
        swr_bp = None

    start_income_minor = _money_minor(inputs.start_income, "start_income", currency, signed=False)
    start_expenses_minor = _money_minor(
        inputs.start_expenses, "start_expenses", currency, signed=False
    )
    start_portfolio_minor = _money_minor(
        inputs.start_portfolio, "start_portfolio", currency, signed=False
    )

    overrides: dict[int, int] = {}
    first_override_year = start_year + 1
    for contribution in inputs.contributions:
        year = _integer(contribution.year, "contributions", "rows_outside_window")
        if not first_override_year <= year <= last_year:
            raise FireProjectionError("contributions", "rows_outside_window")
        if year in overrides:
            raise FireProjectionError("contributions", "duplicate_contribution_year")
        overrides[year] = _money_minor(contribution.amount, "contributions", currency, signed=True)

    cash_by_year: dict[int, int] = {}
    income_changes: dict[int, int] = {}
    expense_changes: dict[int, int] = {}
    for event in inputs.events:
        year = _integer(event.year, "events", "rows_outside_window")
        if not start_year <= year <= last_year:
            raise FireProjectionError("events", "rows_outside_window")
        try:
            kind = EventKind(event.kind)
        except (TypeError, ValueError):
            raise FireProjectionError("events", "invalid_event_kind") from None
        amount = _money_minor(event.amount, "events", currency, signed=True)
        if kind is EventKind.CASH:
            cash_by_year[year] = cash_by_year.get(year, 0) + amount
        elif kind is EventKind.INCOME_CHANGE:
            income_changes[year] = income_changes.get(year, 0) + amount
        else:
            expense_changes[year] = expense_changes.get(year, 0) + amount

    return _ProjectionPlan(
        currency=currency,
        birth_year=birth_year,
        start_year=start_year,
        horizon_age=horizon_age,
        stop_working_age=stop_working_age,
        inflation_bp=inflation_bp,
        real_return_bp=real_return_bp,
        income_growth_bp=income_growth_bp,
        rule=rule,
        swr_bp=swr_bp,
        start_income_minor=start_income_minor,
        start_expenses_minor=start_expenses_minor,
        start_portfolio_minor=start_portfolio_minor,
        overrides=overrides,
        cash_by_year=cash_by_year,
        income_changes=income_changes,
        expense_changes=expense_changes,
    )


def _round_ratio(numerator: int, denominator: int) -> int:
    """``numerator / denominator`` rounded half away from zero, exactly and once."""
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    if numerator >= 0:
        return (2 * numerator + denominator) // (2 * denominator)
    return -((-2 * numerator + denominator) // (2 * denominator))


def _inflate(value: int, rate_bp: int, years: int) -> int:
    """``value * (1 + rate) ** years`` with one half-up rounding at the end."""
    return _round_ratio(value * (10_000 + rate_bp) ** years, 10_000**years)


def _deflate(value: int, inflation_bp: int, years: int) -> int:
    """``value / (1 + inflation) ** years`` with one half-up rounding at the end."""
    return _round_ratio(value * 10_000**years, (10_000 + inflation_bp) ** years)


def _active_changes(changes: dict[int, int], rate_bp: int, years: int, year: int) -> int:
    """Inflate every active event group from the start year; one rounding per group."""
    total = 0
    for change_year, amount in changes.items():
        if change_year <= year:
            total += _inflate(amount, rate_bp, years)
    return total


def project(inputs: ProjectionInputs) -> Projection:
    """Project one FIRE plan year by year from ``start_year`` through ``horizon_age``.

    Parity follows the source spreadsheet. ``t = 0`` reuses the net column as the starting
    portfolio and earns one gain before the row's cash flow; later rows add ``net`` and ``cash``
    to the opening net worth. The nominal gain rate is the additive ``inflation + real return``,
    exactly as the sheet formulas state. Base income grows with income growth while the row's
    age is below the stop age; base expenses grow with inflation; event changes are separate
    components inflated from the start year and active from their event year. Contribution
    overrides replace the net line for their year but leave the shown income, expenses and cash
    untouched. All series continue to the horizon after FIRE and after income stops.

    Under ``returns_cover_expenses`` the first row with ``expenses < gain`` and a non-negative
    opening net worth is the FIRE row. The strict comparison and the previous-year gain mean the
    crossing is marked one year after the moment the return first covers the year's expenses;
    this is the spreadsheet's definition. Under ``swr`` the first row with
    ``opening net worth * swr >= expenses`` wins, and cash does not enter either predicate in
    its own event year. Real values deflate each nominal line by ``(1 + inflation) ** t``.

    ``depletion_age`` and ``min_networth_after_stop`` look at rows from
    ``max(1, stop_working_age - age_at_start)`` onward, inclusive. Invalid inputs raise
    :class:`FireProjectionError` with a form-friendly ``field`` and ``code``.
    """
    plan = _validate(inputs)
    currency = plan.currency
    age = age_at_start(plan.start_year, plan.birth_year)
    last_year = plan.birth_year + plan.horizon_age
    gain_bp = plan.inflation_bp + plan.real_return_bp

    rows: list[ProjectionRow] = []
    fire_index: int | None = None
    previous_net_worth = 0
    for offset in range(last_year - plan.start_year + 1):
        year = plan.start_year + offset
        row_age = year - plan.birth_year
        if offset == 0:
            base_income = plan.start_income_minor
            base_expenses = plan.start_expenses_minor
        else:
            base_income = (
                _inflate(plan.start_income_minor, plan.income_growth_bp, offset)
                if row_age < plan.stop_working_age
                else 0
            )
            base_expenses = _inflate(plan.start_expenses_minor, plan.inflation_bp, offset)
        income = base_income + _active_changes(
            plan.income_changes, plan.income_growth_bp, offset, year
        )
        expenses = base_expenses + _active_changes(
            plan.expense_changes, plan.inflation_bp, offset, year
        )
        net = (
            plan.start_portfolio_minor
            if offset == 0
            else plan.overrides.get(year, income - expenses)
        )
        cash = plan.cash_by_year.get(year, 0)
        opening = plan.start_portfolio_minor if offset == 0 else previous_net_worth
        gain = _round_ratio(opening * gain_bp, 10_000)
        net_worth = net + gain + cash if offset == 0 else opening + net + gain + cash

        is_fire = False
        if fire_index is None and opening >= 0:
            if plan.rule is Rule.SWR:
                assert plan.swr_bp is not None
                is_fire = opening * plan.swr_bp >= expenses * 10_000
            else:
                is_fire = expenses < gain
            if is_fire:
                fire_index = offset

        rows.append(
            ProjectionRow(
                year=year,
                age=row_age,
                income=Money(income, currency),
                expenses=Money(expenses, currency),
                net=Money(net, currency),
                gain=Money(gain, currency),
                cash=Money(cash, currency),
                net_worth=Money(net_worth, currency),
                real_income=Money(_deflate(income, plan.inflation_bp, offset), currency),
                real_expenses=Money(_deflate(expenses, plan.inflation_bp, offset), currency),
                real_net=Money(_deflate(net, plan.inflation_bp, offset), currency),
                real_gain=Money(_deflate(gain, plan.inflation_bp, offset), currency),
                real_cash=Money(_deflate(cash, plan.inflation_bp, offset), currency),
                real_net_worth=Money(_deflate(net_worth, plan.inflation_bp, offset), currency),
                is_fire=is_fire,
                income_stopped=offset > 0 and row_age >= plan.stop_working_age,
            )
        )
        previous_net_worth = net_worth

    fire_row = rows[fire_index] if fire_index is not None else None
    fire_year = plan.start_year + fire_index if fire_index is not None else None
    fire_age = age + fire_index if fire_index is not None else None

    first_expenses = rows[0].expenses.amount_minor
    fire_number_today: Money | None
    if first_expenses == 0:
        fire_number_today = Money(0, currency)
    elif plan.rule is Rule.SWR:
        assert plan.swr_bp is not None
        fire_number_today = Money(_round_ratio(first_expenses * 10_000, plan.swr_bp), currency)
    elif gain_bp > 0:
        fire_number_today = Money(_round_ratio(first_expenses * 10_000, gain_bp), currency)
    else:
        fire_number_today = None

    progress_sheet_bp: int | None = None
    if fire_row is not None and fire_row.net_worth.amount_minor > 0:
        progress_sheet_bp = _round_ratio(
            rows[0].net_worth.amount_minor * 10_000, fire_row.net_worth.amount_minor
        )
    progress_today_bp: int | None = None
    if fire_number_today is not None and fire_number_today.amount_minor > 0:
        progress_today_bp = _round_ratio(
            plan.start_portfolio_minor * 10_000, fire_number_today.amount_minor
        )

    retirement_start = max(1, plan.stop_working_age - age)
    retirement_rows = rows[retirement_start:]
    depletion_age = next(
        (row.age for row in retirement_rows if row.net_worth.amount_minor < 0), None
    )
    min_after_stop = min((row.net_worth.amount_minor for row in retirement_rows), default=None)

    return Projection(
        inputs=inputs,
        rows=tuple(rows),
        fire_index=fire_index,
        fire_year=fire_year,
        fire_age=fire_age,
        years_to_fire=fire_index,
        networth_at_fire=fire_row.net_worth if fire_row is not None else None,
        real_networth_at_fire=(fire_row.real_net_worth if fire_row is not None else None),
        fire_number_today=fire_number_today,
        progress_sheet_bp=progress_sheet_bp,
        progress_today_bp=progress_today_bp,
        depletion_age=depletion_age,
        min_networth_after_stop=(
            Money(min_after_stop, currency) if min_after_stop is not None else None
        ),
    )
