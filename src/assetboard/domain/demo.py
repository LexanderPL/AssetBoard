"""The deterministic demo dataset plan (F9.11a).

Pure: no I/O, no database and no clock. ``build_plan`` takes a reference date ``T``, the
workspace locale and base currency, and a ``translate`` callable (gettext for the locale). The
service writer (``services.demo``) applies the plan through the normal services.

Amounts are defined once in EUR-scale and converted with :data:`CURRENCY_FACTORS` (EUR 1,
PLN 4.3, USD 1.1, GBP 0.85, CHF 0.95, every other currency 1). Bills are rounded to whole
currency units, other amounts keep the currency's minor unit. All names, including synthetic
payees and descriptions, are message ids translated through ``translate``.

The plan is a stable, sorted description of the rows to create. ``random.Random`` is seeded
from ``T``'s ISO date, and every collection is sorted before it is returned so set/dict
iteration order cannot change the dataset. Bump :data:`DEMO_DATASET_VERSION` whenever the
contents change (F9.11b's reset job compares it).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from random import Random
from types import MappingProxyType
from typing import Literal

from assetboard.domain.money import Money

DEMO_DATASET_VERSION: int = 2

CURRENCY_FACTORS: MappingProxyType[str, Decimal] = MappingProxyType(
    {
        "EUR": Decimal("1"),
        "PLN": Decimal("4.3"),
        "USD": Decimal("1.1"),
        "GBP": Decimal("0.85"),
        "CHF": Decimal("0.95"),
    }
)

# Models the demo intentionally leaves empty, with the reason shown in the coverage test.
DEMO_EXCLUDED_MODELS: MappingProxyType[str, str] = MappingProxyType(
    {
        "Invitation": "invitations are an access-control feature, not sample data",
        "NotificationChannel": "no external notification channel is configured",
        "NotificationLog": "the demo sends no notifications",
        "PriceSourceConfig": "no price provider is enabled in the demo",
        "ProviderError": "no provider calls are made",
        "LoanScenario": "scenarios are user what-ifs, not sample data",
        "LoanPayment": "actual payments are user records, not sample data",
        "InstallmentPlan": "installment plans are user records, not sample data",
        "FireScenario": "FIRE scenarios are user what-ifs, not sample data",
        "FireContribution": "contribution overrides belong to a user's FIRE scenario",
        "FireEvent": "FIRE events belong to a user's FIRE scenario",
        "FireExpenseExclusion": "expense exclusions belong to a user's FIRE scenario",
    }
)


def _(message: str) -> str:
    """Marks a message for extraction; the plan translates it for the workspace locale."""
    return message


def currency_factor(currency: str) -> Decimal:
    """The EUR-scale factor for ``currency``; unknown currencies keep the EUR amount."""
    return CURRENCY_FACTORS.get(currency.upper(), Decimal(1))


def convert_amount(eur_amount: Decimal, currency: str, *, whole: bool = False) -> Money:
    """Convert a EUR-scale major amount to ``currency``, half up, once per line.

    ``whole`` rounds to whole currency units first (bills); otherwise the currency's minor unit
    applies (both through :class:`Money`).
    """
    if not eur_amount.is_finite() or eur_amount < 0:
        raise ValueError("amount must be a finite non-negative Decimal")
    value = eur_amount * currency_factor(currency)
    if whole:
        value = value.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return Money.from_decimal(value, currency.upper())


@dataclass(frozen=True, slots=True)
class DemoTransaction:
    day: date
    direction: Literal["income", "expense"]
    amount: Money
    category: str
    description: str


@dataclass(frozen=True, slots=True)
class DemoPlannedValue:
    month: date  # first day
    category: str
    amount: Money


@dataclass(frozen=True, slots=True)
class DemoRecurring:
    key: str
    name: str
    category: str
    amount: Money
    amount_mode: Literal["fixed", "estimate"]
    freq: Literal["once", "weekly", "monthly", "yearly"]
    start_date: date
    interval: int = 1
    end_date: date | None = None
    max_count: int | None = None
    weekdays: tuple[int, ...] = ()
    month_day: str = "1"
    month: int = 1
    weekend_policy: Literal["none", "previous_working_day", "next_working_day"] = "none"
    auto_post: bool = False
    # Leave the occurrence with this nominal date pending (the overdue bill in M0).
    overdue_nominal: date | None = None
    # Mark this occurrence skipped instead of posting it (a past weekly market).
    skip_nominal: date | None = None


@dataclass(frozen=True, slots=True)
class DemoWorkDay:
    day: date
    units: Decimal


@dataclass(frozen=True, slots=True)
class DemoIncomeSource:
    name: str
    category: str
    unit_label: str
    rate: Money
    pay_day: int
    pay_lag_months: int
    expected_units: Decimal
    average_months: int
    active_from: date
    work_days: tuple[DemoWorkDay, ...]


@dataclass(frozen=True, slots=True)
class DemoTaxRule:
    name: str
    percent_bp: int
    payment_category: str
    payment_day: int = 10


@dataclass(frozen=True, slots=True)
class DemoValuation:
    as_of: date
    value: Money


@dataclass(frozen=True, slots=True)
class DemoNetWorthItem:
    name: str
    side: Literal["asset", "liability"]
    category: str
    values: tuple[DemoValuation, ...]


@dataclass(frozen=True, slots=True)
class DemoAllocation:
    asset_class: str
    weight_bp: int


@dataclass(frozen=True, slots=True)
class DemoInstrument:
    name: str
    symbol: str
    type: str
    quote_currency: str
    unit_label: str
    allocations: tuple[DemoAllocation, ...]


@dataclass(frozen=True, slots=True)
class DemoInstrumentTransaction:
    instrument: str
    type: Literal["buy", "sell", "dividend", "split"]
    day: date
    quantity: Decimal
    unit_price: Decimal
    fee: Money
    fx_to_base: Decimal | None = None
    note: str = ""
    amounts: Literal["quote", "base"] = "quote"
    base_unit_price: Decimal | None = None
    base_fee: Money | None = None
    record_income: bool = False
    income_category: str | None = None


@dataclass(frozen=True, slots=True)
class DemoPricePoint:
    instrument: str
    day: date
    price: Decimal


@dataclass(frozen=True, slots=True)
class DemoFxPoint:
    quote_currency: str
    day: date
    rate: Decimal


@dataclass(frozen=True, slots=True)
class DemoBondTerms:
    instrument: str
    series_code: str
    face_value: Money
    maturity_date: date
    interest_mode: str
    rate_schedule: tuple[tuple[date, int], ...]  # (from_date, annual_rate_bp)
    tax_rate_bp: int


@dataclass(frozen=True, slots=True)
class DemoReferenceRate:
    name: str
    notes: str
    values: tuple[tuple[date, int], ...]  # (effective_from, annual_rate_bp)


@dataclass(frozen=True, slots=True)
class DemoLoanCost:
    name: str
    mode: str
    rate_bp: int | None
    fixed: Money | None
    effective_from: date
    effective_to: date | None = None


@dataclass(frozen=True, slots=True)
class DemoLoan:
    name: str
    kind: str
    entry_path: str
    principal_original: Money | None
    current_balance: Money | None
    balance_date: date | None
    origination_date: date | None
    first_payment_date: date
    term_months: int
    payment_day: int
    rate_mode: str
    fixed_rate_bp: int | None
    reference_rate_name: str | None
    margin_bp: int | None
    installment_override: Money | None
    overpayment_mode: str
    overpayment_amount: Money | None
    overpayment_strategy: str
    rate_changes: tuple[tuple[date, int], ...]
    costs: tuple[DemoLoanCost, ...]
    overpayments: tuple[tuple[date, Money, str], ...]


@dataclass(frozen=True, slots=True)
class DemoRiskPolicy:
    safe_bp: int
    growth_bp: int
    speculative_bp: int
    tolerance_bp: int


@dataclass(frozen=True, slots=True)
class DemoCsvRow:
    day: date
    direction: Literal["income", "expense"]
    amount: Money
    category: str
    description: str


@dataclass(frozen=True, slots=True)
class DemoPlan:
    version: int
    reference_date: date
    locale: str
    base_currency: str
    months: tuple[date, ...]  # M-3, M-2, M-1, M0
    closed_month: date  # M-3
    transactions: tuple[DemoTransaction, ...]
    planned_values: tuple[DemoPlannedValue, ...]
    recurring: tuple[DemoRecurring, ...]
    income_source: DemoIncomeSource
    tax_rule: DemoTaxRule
    networth_items: tuple[DemoNetWorthItem, ...]
    snapshot_dates: tuple[date, ...]
    instruments: tuple[DemoInstrument, ...]
    investment_transactions: tuple[DemoInstrumentTransaction, ...]
    price_points: tuple[DemoPricePoint, ...]
    fx_points: tuple[DemoFxPoint, ...]
    bond_terms: DemoBondTerms
    risk_policy: DemoRiskPolicy
    csv_rows: tuple[DemoCsvRow, ...]
    reference_rates: tuple[DemoReferenceRate, ...]
    loans: tuple[DemoLoan, ...]


# ---------------------------------------------------------------------------
# Calendar helpers
# ---------------------------------------------------------------------------


def _month_start(reference: date, offset: int) -> date:
    index = reference.year * 12 + (reference.month - 1) + offset
    return date(index // 12, index % 12 + 1, 1)


def _month_end(reference: date, offset: int) -> date:
    return _month_start(reference, offset + 1) - timedelta(days=1)


def _month_days(month: date) -> int:
    return (_month_start(month, 1) - timedelta(days=1)).day


def _clamp_month_day(month: date, day: int) -> date:
    return month.replace(day=min(day, _month_days(month)))


# ---------------------------------------------------------------------------
# Category and description message ids
# ---------------------------------------------------------------------------

CAT_FOOD = _("Food")
CAT_EATING_OUT = _("Eating out")
CAT_GIFTS = _("Gifts")
CAT_HEALTH = _("Health")
CAT_HOUSING = _("Housing")
CAT_TRANSPORT = _("Transport")
CAT_COSMETICS = _("Cosmetics")
CAT_HOBBIES = _("Hobbies")
CAT_UTILITIES = _("Utilities")
CAT_TRAVEL = _("Travel")
CAT_ENTERTAINMENT = _("Entertainment")
CAT_EDUCATION = _("Education")
CAT_TAXES = _("Taxes")
CAT_CLOTHES = _("Clothes")
CAT_OTHER = _("Other")
CAT_SAVINGS = _("Savings")
CAT_INVESTMENTS = _("Investments")
CAT_SALARY = _("Salary")
CAT_BONUS = _("Bonus")
CAT_INTEREST = _("Interest")
CAT_SALE = _("Sale of items")

PLAN_CATEGORIES: tuple[str, ...] = (
    CAT_FOOD,
    CAT_EATING_OUT,
    CAT_GIFTS,
    CAT_HEALTH,
    CAT_HOUSING,
    CAT_TRANSPORT,
    CAT_COSMETICS,
    CAT_HOBBIES,
    CAT_UTILITIES,
    CAT_TRAVEL,
    CAT_ENTERTAINMENT,
    CAT_EDUCATION,
    CAT_TAXES,
    CAT_CLOTHES,
    CAT_OTHER,
    CAT_SAVINGS,
    CAT_INVESTMENTS,
    CAT_SALARY,
    CAT_BONUS,
    CAT_INTEREST,
    CAT_SALE,
)

BASELINE_PLAN_EUR: dict[str, Decimal] = {
    CAT_FOOD: Decimal("480"),
    CAT_EATING_OUT: Decimal("160"),
    CAT_GIFTS: Decimal("40"),
    CAT_HEALTH: Decimal("90"),
    CAT_HOUSING: Decimal("1450"),
    CAT_TRANSPORT: Decimal("180"),
    CAT_COSMETICS: Decimal("35"),
    CAT_HOBBIES: Decimal("80"),
    CAT_UTILITIES: Decimal("260"),
    CAT_TRAVEL: Decimal("120"),
    CAT_ENTERTAINMENT: Decimal("95"),
    CAT_EDUCATION: Decimal("60"),
    CAT_TAXES: Decimal("260"),
    CAT_CLOTHES: Decimal("90"),
    CAT_OTHER: Decimal("40"),
    CAT_SAVINGS: Decimal("300"),
    CAT_INVESTMENTS: Decimal("500"),
    CAT_SALARY: Decimal("5400"),
    CAT_BONUS: Decimal("0"),
    CAT_INTEREST: Decimal("10"),
    CAT_SALE: Decimal("0"),
}


@dataclass(frozen=True, slots=True)
class _ExpenseTemplate:
    category: str
    description: str
    low_eur: Decimal
    high_eur: Decimal
    whole: bool = False


_EXPENSE_TEMPLATES: tuple[_ExpenseTemplate, ...] = (
    _ExpenseTemplate(CAT_FOOD, _("Bakery"), Decimal("3.20"), Decimal("18.00")),
    _ExpenseTemplate(CAT_FOOD, _("Supermarket"), Decimal("24.00"), Decimal("94.00")),
    _ExpenseTemplate(CAT_FOOD, _("Farmers market"), Decimal("12.00"), Decimal("48.00")),
    _ExpenseTemplate(CAT_FOOD, _("Corner shop"), Decimal("2.40"), Decimal("16.00")),
    _ExpenseTemplate(CAT_FOOD, _("Groceries"), Decimal("30.00"), Decimal("120.00")),
    _ExpenseTemplate(CAT_EATING_OUT, _("Pizza night"), Decimal("14.00"), Decimal("42.00")),
    _ExpenseTemplate(CAT_EATING_OUT, _("Coffee"), Decimal("2.80"), Decimal("9.00")),
    _ExpenseTemplate(CAT_EATING_OUT, _("Lunch"), Decimal("8.00"), Decimal("22.00")),
    _ExpenseTemplate(CAT_EATING_OUT, _("Sushi"), Decimal("18.00"), Decimal("55.00")),
    _ExpenseTemplate(CAT_EATING_OUT, _("Brunch"), Decimal("12.00"), Decimal("38.00")),
    _ExpenseTemplate(CAT_TRANSPORT, _("Fuel"), Decimal("28.00"), Decimal("72.00"), whole=True),
    _ExpenseTemplate(CAT_TRANSPORT, _("Train ticket"), Decimal("4.00"), Decimal("24.00")),
    _ExpenseTemplate(CAT_TRANSPORT, _("Bus pass"), Decimal("18.00"), Decimal("55.00"), True),
    _ExpenseTemplate(CAT_TRANSPORT, _("Parking"), Decimal("2.00"), Decimal("14.00")),
    _ExpenseTemplate(CAT_TRANSPORT, _("Bike repair"), Decimal("10.00"), Decimal("45.00")),
    _ExpenseTemplate(CAT_HOUSING, _("Hardware store"), Decimal("8.00"), Decimal("64.00")),
    _ExpenseTemplate(CAT_HOUSING, _("Furniture"), Decimal("40.00"), Decimal("220.00"), True),
    _ExpenseTemplate(CAT_UTILITIES, _("Water bill"), Decimal("22.00"), Decimal("42.00"), True),
    _ExpenseTemplate(CAT_HEALTH, _("Pharmacy"), Decimal("6.00"), Decimal("34.00")),
    _ExpenseTemplate(CAT_HEALTH, _("Dentist"), Decimal("40.00"), Decimal("160.00"), True),
    _ExpenseTemplate(CAT_COSMETICS, _("Haircut"), Decimal("18.00"), Decimal("42.00"), True),
    _ExpenseTemplate(CAT_COSMETICS, _("Drugstore"), Decimal("5.00"), Decimal("28.00")),
    _ExpenseTemplate(CAT_HOBBIES, _("Bookshop"), Decimal("8.00"), Decimal("32.00")),
    _ExpenseTemplate(CAT_HOBBIES, _("Board game"), Decimal("15.00"), Decimal("55.00")),
    _ExpenseTemplate(CAT_TRAVEL, _("Train trip"), Decimal("25.00"), Decimal("120.00"), True),
    _ExpenseTemplate(CAT_TRAVEL, _("Hotel"), Decimal("60.00"), Decimal("240.00"), True),
    _ExpenseTemplate(CAT_ENTERTAINMENT, _("Cinema"), Decimal("8.00"), Decimal("26.00")),
    _ExpenseTemplate(CAT_ENTERTAINMENT, _("Concert"), Decimal("18.00"), Decimal("75.00"), True),
    _ExpenseTemplate(CAT_EDUCATION, _("Language course"), Decimal("25.00"), Decimal("90.00"), True),
    _ExpenseTemplate(CAT_EDUCATION, _("Online course"), Decimal("12.00"), Decimal("48.00"), True),
    _ExpenseTemplate(CAT_GIFTS, _("Birthday gift"), Decimal("15.00"), Decimal("65.00"), True),
    _ExpenseTemplate(CAT_GIFTS, _("Charity"), Decimal("5.00"), Decimal("30.00"), True),
    _ExpenseTemplate(CAT_CLOTHES, _("T-shirt"), Decimal("10.00"), Decimal("38.00")),
    _ExpenseTemplate(CAT_CLOTHES, _("Shoes"), Decimal("30.00"), Decimal("120.00"), True),
    _ExpenseTemplate(CAT_OTHER, _("Stationery"), Decimal("3.00"), Decimal("18.00")),
    _ExpenseTemplate(CAT_OTHER, _("Household items"), Decimal("6.00"), Decimal("45.00")),
)


def _template_amount(rng: Random, template: _ExpenseTemplate, currency: str) -> Money:
    span = int((template.high_eur - template.low_eur) * 100)
    step = rng.randrange(span + 1) if span > 0 else 0
    eur = template.low_eur + Decimal(step) / 100
    return convert_amount(eur, currency, whole=template.whole)


def _transactions_for_month(
    rng: Random,
    month: date,
    last_day: int,
    *,
    currency: str,
    translate: Translate,
) -> list[DemoTransaction]:
    days_in_month = _month_days(month)
    count = 38 if last_day >= days_in_month else max(6, 38 * last_day // days_in_month)
    rows: list[DemoTransaction] = []
    for index in range(count):
        template = _EXPENSE_TEMPLATES[
            (index * 7 + rng.randrange(len(_EXPENSE_TEMPLATES))) % len(_EXPENSE_TEMPLATES)
        ]
        day = month.replace(day=rng.randint(1, last_day))
        rows.append(
            DemoTransaction(
                day=day,
                direction="expense",
                amount=_template_amount(rng, template, currency),
                category=translate(template.category),
                description=translate(template.description),
            )
        )
    for day, category, description in (
        (2, CAT_SAVINGS, _("Monthly savings")),
        (3, CAT_INVESTMENTS, _("Broker transfer")),
    ):
        if day <= last_day:
            rows.append(
                DemoTransaction(
                    day=month.replace(day=day),
                    direction="expense",
                    amount=convert_amount(Decimal("300"), currency, whole=True),
                    category=translate(category),
                    description=translate(description),
                )
            )
    for day, category, description, eur in (
        (6, CAT_INTEREST, _("Savings interest"), Decimal("8.50")),
        (14, CAT_SALE, _("Sold an old chair"), Decimal("45")),
    ):
        if day <= last_day:
            rows.append(
                DemoTransaction(
                    day=month.replace(day=day),
                    direction="income",
                    amount=convert_amount(eur, currency),
                    category=translate(category),
                    description=translate(description),
                )
            )
    return rows


Translate = Callable[[str], str]


# ---------------------------------------------------------------------------
# Price walk (no float: integer random steps scaled with Decimal)
# ---------------------------------------------------------------------------


def _noise(rng: Random, amplitude: Decimal) -> Decimal:
    return Decimal(rng.randrange(-1000, 1001)) / Decimal(1000) * amplitude


def _walk(
    rng: Random,
    *,
    start: Decimal,
    count: int,
    drift: Decimal,
    amplitude: Decimal,
    quantum: Decimal,
) -> list[Decimal]:
    values: list[Decimal] = []
    current = start.quantize(quantum)
    for index in range(count):
        if index:
            current = current * (Decimal(1) + drift + _noise(rng, amplitude))
            current = max(current.quantize(quantum), quantum)
        values.append(current)
    return values


def _weekly_dates(end: date, count: int) -> list[date]:
    first = end - timedelta(weeks=count - 1)
    return [first + timedelta(weeks=index) for index in range(count)]


def _price_at(points: list[tuple[date, Decimal]], day: date) -> Decimal:
    candidates = [price for point_day, price in points if point_day <= day]
    return candidates[-1] if candidates else points[0][1]


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


def build_plan(
    *,
    reference_date: date,
    locale: str,
    base_currency: str,
    translate: Translate | None = None,
) -> DemoPlan:
    """Build the whole demo plan for ``reference_date`` (``T``).

    The same ``T``, locale and base currency always produce an equal plan. A later ``T`` moves
    every date and (because the RNG is seeded from the date) produces different descriptions
    and amounts.
    """
    translate = translate or (lambda message: message)
    currency = base_currency.upper()
    rng = Random(reference_date.isoformat())  # noqa: S311 - deterministic sample data
    months = tuple(_month_start(reference_date, offset) for offset in (-3, -2, -1, 0))
    closed_month = months[0]

    transactions: list[DemoTransaction] = []
    for index, month in enumerate(months):
        last_day = reference_date.day if index == 3 else _month_days(month)
        transactions.extend(
            _transactions_for_month(rng, month, last_day, currency=currency, translate=translate)
        )
    transactions.sort(key=lambda row: (row.day, row.direction, row.description, row.category))

    recurring = _recurring_plan(reference_date, currency, translate)
    income_source, tax_rule = _income_plan(reference_date, months, currency, translate)

    planned_values = _planned_values(
        months, transactions, recurring, currency=currency, translate=translate
    )

    networth_items = _networth_items(reference_date, currency, translate)
    snapshot_dates = tuple(_month_end(reference_date, offset) for offset in range(-12, 0))

    instruments, investment_transactions, price_points, fx_points, bond_terms = _investment_plan(
        reference_date, currency, translate, rng
    )

    csv_rows = _csv_rows(reference_date, currency, translate)
    reference_rates, demo_loans = _loan_plan(reference_date, currency, translate)

    return DemoPlan(
        version=DEMO_DATASET_VERSION,
        reference_date=reference_date,
        locale=locale,
        base_currency=currency,
        months=months,
        closed_month=closed_month,
        transactions=tuple(transactions),
        planned_values=planned_values,
        recurring=tuple(recurring),
        income_source=income_source,
        tax_rule=tax_rule,
        networth_items=networth_items,
        snapshot_dates=snapshot_dates,
        instruments=instruments,
        investment_transactions=investment_transactions,
        price_points=price_points,
        fx_points=fx_points,
        bond_terms=bond_terms,
        risk_policy=DemoRiskPolicy(
            safe_bp=5000, growth_bp=4000, speculative_bp=1000, tolerance_bp=500
        ),
        csv_rows=csv_rows,
        reference_rates=reference_rates,
        loans=demo_loans,
    )


def _loan_plan(
    reference: date, currency: str, translate: Translate
) -> tuple[tuple[DemoReferenceRate, ...], tuple[DemoLoan, ...]]:
    """One fixed mortgage entered from its original terms and one variable car loan."""
    start = _month_start(reference, -36)
    mid = _month_start(reference, -12)
    reference_name = translate(_("Sample reference rate"))
    reference_rates = (
        DemoReferenceRate(
            name=reference_name,
            notes=translate(_("Synthetic reference-rate series for the demo")),
            values=((start, 400), (mid, 450)),
        ),
    )
    mortgage_principal = convert_amount(Decimal("300000"), currency, whole=True)
    mortgage = DemoLoan(
        name=translate(_("Mortgage")),
        kind="annuity",
        entry_path="original",
        principal_original=mortgage_principal,
        current_balance=None,
        balance_date=None,
        origination_date=start,
        first_payment_date=_month_start(reference, -35),
        term_months=300,
        payment_day=1,
        rate_mode="fixed",
        fixed_rate_bp=550,
        reference_rate_name=None,
        margin_bp=None,
        installment_override=None,
        overpayment_mode="fixed_extra",
        overpayment_amount=convert_amount(Decimal("100"), currency, whole=True),
        overpayment_strategy="reduce_term",
        rate_changes=((start, 550), (mid, 600)),
        costs=(
            DemoLoanCost(
                name=translate(_("Life insurance")),
                mode="percent",
                rate_bp=10,
                fixed=None,
                effective_from=start,
            ),
            DemoLoanCost(
                name=translate(_("Bank fee")),
                mode="fixed",
                rate_bp=None,
                fixed=convert_amount(Decimal("25"), currency, whole=True),
                effective_from=start,
            ),
        ),
        overpayments=(
            (
                _month_start(reference, -2),
                convert_amount(Decimal("1000"), currency, whole=True),
                translate(_("Annual bonus")),
            ),
        ),
    )
    car = DemoLoan(
        name=translate(_("Car loan")),
        kind="equal_principal",
        entry_path="current",
        principal_original=convert_amount(Decimal("20000"), currency, whole=True),
        current_balance=convert_amount(Decimal("15000"), currency, whole=True),
        balance_date=_month_start(reference, -1),
        origination_date=None,
        first_payment_date=_month_start(reference, 0),
        term_months=36,
        payment_day=1,
        rate_mode="variable",
        fixed_rate_bp=None,
        reference_rate_name=reference_name,
        margin_bp=150,
        installment_override=None,
        overpayment_mode="none",
        overpayment_amount=None,
        overpayment_strategy="reduce_term",
        rate_changes=(),
        costs=(
            DemoLoanCost(
                name=translate(_("Bank fee")),
                mode="fixed",
                rate_bp=None,
                fixed=convert_amount(Decimal("10"), currency, whole=True),
                effective_from=_month_start(reference, -1),
            ),
        ),
        overpayments=(),
    )
    return reference_rates, (mortgage, car)


def _recurring_plan(reference: date, currency: str, translate: Translate) -> list[DemoRecurring]:
    start = _month_start(reference, -24)
    return [
        DemoRecurring(
            key="salary",
            name=translate(_("Salary")),
            category=translate(CAT_SALARY),
            amount=convert_amount(Decimal("5400"), currency, whole=True),
            amount_mode="fixed",
            freq="monthly",
            start_date=start,
            month_day="1",
            auto_post=True,
        ),
        DemoRecurring(
            key="rent",
            name=translate(_("Rent")),
            category=translate(CAT_HOUSING),
            amount=convert_amount(Decimal("1250"), currency, whole=True),
            amount_mode="fixed",
            freq="monthly",
            start_date=start.replace(day=2),
            month_day="2",
            auto_post=True,
        ),
        DemoRecurring(
            key="utility",
            name=translate(_("Electricity and gas")),
            category=translate(CAT_UTILITIES),
            amount=convert_amount(Decimal("145"), currency, whole=True),
            amount_mode="estimate",
            freq="monthly",
            start_date=start.replace(day=3),
            month_day="3",
            overdue_nominal=_clamp_month_day(_month_start(reference, 0), 3),
        ),
        DemoRecurring(
            key="streaming",
            name=translate(_("Streaming subscription")),
            category=translate(CAT_ENTERTAINMENT),
            amount=convert_amount(Decimal("16"), currency),
            amount_mode="fixed",
            freq="monthly",
            start_date=start.replace(day=12),
            month_day="12",
        ),
        DemoRecurring(
            key="music",
            name=translate(_("Music subscription")),
            category=translate(CAT_ENTERTAINMENT),
            amount=convert_amount(Decimal("11"), currency),
            amount_mode="fixed",
            freq="monthly",
            start_date=start.replace(day=25),
            month_day="25",
        ),
        DemoRecurring(
            key="internet",
            name=translate(_("Internet")),
            category=translate(CAT_UTILITIES),
            amount=convert_amount(Decimal("45"), currency, whole=True),
            amount_mode="fixed",
            freq="monthly",
            start_date=start.replace(day=18),
            month_day="18",
        ),
        DemoRecurring(
            key="phone",
            name=translate(_("Phone in 12 installments")),
            category=translate(CAT_OTHER),
            amount=convert_amount(Decimal("65"), currency, whole=True),
            amount_mode="fixed",
            freq="monthly",
            start_date=_month_start(reference, -8).replace(day=15),
            month_day="15",
            max_count=12,
        ),
        DemoRecurring(
            key="market",
            name=translate(_("Saturday market")),
            category=translate(CAT_FOOD),
            amount=convert_amount(Decimal("28"), currency),
            amount_mode="estimate",
            freq="weekly",
            start_date=_month_start(reference, -3),
            weekdays=(5,),  # Saturday
            skip_nominal=_month_start(reference, -3)
            + timedelta(days=(5 - _month_start(reference, -3).weekday()) % 7),
        ),
        DemoRecurring(
            key="insurance",
            name=translate(_("Home insurance")),
            category=translate(CAT_HOUSING),
            amount=convert_amount(Decimal("420"), currency, whole=True),
            amount_mode="fixed",
            freq="yearly",
            start_date=_clamp_month_day(_month_start(reference, 3), 10),
            month_day="10",
            month=_month_start(reference, 3).month,
        ),
        DemoRecurring(
            key="one-time",
            name=translate(_("Bike service")),
            category=translate(CAT_TRANSPORT),
            amount=convert_amount(Decimal("180"), currency, whole=True),
            amount_mode="fixed",
            freq="once",
            start_date=reference + timedelta(days=14),
        ),
    ]


def _income_plan(
    reference: date, months: tuple[date, ...], currency: str, translate: Translate
) -> tuple[DemoIncomeSource, DemoTaxRule]:
    work_days: list[DemoWorkDay] = []
    for index, month in enumerate(months):
        last = reference.day if index == 3 else _month_days(month)
        for day_number in range(1, last + 1):
            day = month.replace(day=day_number)
            if day.weekday() < 5:
                work_days.append(DemoWorkDay(day=day, units=Decimal(8)))
    work_days.sort(key=lambda row: row.day)
    return (
        DemoIncomeSource(
            name=translate(_("Hourly consulting")),
            category=translate(CAT_SALARY),
            unit_label=translate(_("hour")),
            rate=convert_amount(Decimal("42"), currency),
            pay_day=10,
            pay_lag_months=1,
            expected_units=Decimal(160),
            average_months=3,
            active_from=months[0],
            work_days=tuple(work_days),
        ),
        DemoTaxRule(
            name=translate(_("Income tax")),
            percent_bp=2000,
            payment_category=translate(CAT_TAXES),
            payment_day=10,
        ),
    )


def _planned_values(
    months: tuple[date, ...],
    transactions: list[DemoTransaction],
    recurring: list[DemoRecurring],
    *,
    currency: str,
    translate: Translate,
) -> tuple[DemoPlannedValue, ...]:
    planned: list[DemoPlannedValue] = []
    for month_index, month in enumerate(months):
        totals: dict[str, int] = {}
        for row in transactions:
            if row.day.replace(day=1) == month:
                totals[row.category] = totals.get(row.category, 0) + row.amount.amount_minor
        for item in recurring:
            if item.freq in {"monthly", "weekly"} and _month_start(item.start_date, 0) <= month:
                expected = item.amount.amount_minor
                if item.freq == "weekly":
                    expected *= 4
                totals[item.category] = totals.get(item.category, 0) + expected
        for category_index, category_id in enumerate(PLAN_CATEGORIES):
            category = translate(category_id)
            actual = Money(totals.get(category, 0), currency).to_decimal()
            factor = Decimal("1.10") if (month_index + category_index) % 2 == 0 else Decimal("0.90")
            value = actual * factor
            if value == 0:
                value = convert_amount(
                    BASELINE_PLAN_EUR[category_id], currency, whole=True
                ).to_decimal()
            planned.append(
                DemoPlannedValue(
                    month=month,
                    category=category,
                    amount=Money.from_decimal(
                        value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), currency
                    ),
                )
            )
    planned.sort(key=lambda row: (row.month, row.category))
    return tuple(planned)


def _networth_items(
    reference: date, currency: str, translate: Translate
) -> tuple[DemoNetWorthItem, ...]:
    dates = tuple(_month_end(reference, offset) for offset in range(-12, 0))
    items: list[DemoNetWorthItem] = []

    def add(
        name: str,
        side: Literal["asset", "liability"],
        category: str,
        values: list[int],
        *,
        gap: int | None = None,
    ) -> None:
        points: list[DemoValuation] = []
        for index, value_minor in enumerate(values):
            if gap is not None and index == gap:
                continue
            points.append(
                DemoValuation(
                    as_of=dates[index],
                    value=Money(value_minor, currency),
                )
            )
        items.append(
            DemoNetWorthItem(
                name=translate(name),
                side=side,
                category=translate(category),
                values=tuple(points),
            )
        )

    add(
        _("Current account"),
        "asset",
        CAT_OTHER,
        [310000 + index * 12000 for index in range(12)],
        gap=5,
    )
    add(_("Savings account"), "asset", CAT_OTHER, [1200000 + index * 30000 for index in range(12)])
    add(
        _("Emergency fund"),
        "asset",
        CAT_OTHER,
        [800000 + index * 10000 for index in range(12)],
    )
    add(
        _("Car"),
        "asset",
        _("Vehicles"),
        [max(900000 - index * 16000, 400000) for index in range(12)],
    )
    add(
        _("Apartment"),
        "asset",
        _("Real estate"),
        [26000000 + index * 130000 for index in range(12)],
    )
    add(
        _("Mortgage"),
        "liability",
        _("Mortgage"),
        [max(18000000 - index * 28000, 12000000) for index in range(12)],
    )
    add(
        _("Credit card"),
        "liability",
        _("Other"),
        [60000 + (index % 4) * 9000 for index in range(12)],
    )
    return tuple(items)


def _investment_plan(
    reference: date, currency: str, translate: Translate, rng: Random
) -> tuple[
    tuple[DemoInstrument, ...],
    tuple[DemoInstrumentTransaction, ...],
    tuple[DemoPricePoint, ...],
    tuple[DemoFxPoint, ...],
    DemoBondTerms,
]:
    cash_currency = "GBP" if currency == "USD" else "USD"
    # The US stock must be foreign to the base so the F6.2a base-settled buy is valid.
    us_currency = "USD" if currency != "USD" else "EUR"
    instruments = (
        DemoInstrument(
            name=translate(_("Global ETF")),
            symbol="VWCE",
            type="etf",
            quote_currency=currency,
            unit_label=translate(_("share")),
            allocations=(DemoAllocation(translate(_("Stocks")), 10000),),
        ),
        DemoInstrument(
            name=translate(_("Balanced 80/20 fund")),
            symbol="BAL80",
            type="etf",
            quote_currency=currency,
            unit_label=translate(_("share")),
            allocations=(
                DemoAllocation(translate(_("Stocks")), 8000),
                DemoAllocation(translate(_("Bonds")), 2000),
            ),
        ),
        DemoInstrument(
            name=translate(_("US stock")),
            symbol="USST",
            type="equity",
            quote_currency=us_currency,
            unit_label=translate(_("share")),
            allocations=(DemoAllocation(translate(_("Stocks")), 10000),),
        ),
        DemoInstrument(
            name=translate(_("Gold")),
            symbol="XAU",
            type="metal",
            quote_currency=currency,
            unit_label=translate(_("oz")),
            allocations=(DemoAllocation(translate(_("Metals")), 10000),),
        ),
        DemoInstrument(
            name=translate(_("Bitcoin")),
            symbol="BTC",
            type="crypto",
            quote_currency=currency,
            unit_label=translate(_("coin")),
            allocations=(DemoAllocation(translate(_("Crypto")), 10000),),
        ),
        DemoInstrument(
            name=translate(_("Government bond")),
            symbol="GOV5",
            type="bond_model",
            quote_currency=currency,
            unit_label=translate(_("bond")),
            allocations=(DemoAllocation(translate(_("Bonds")), 10000),),
        ),
        DemoInstrument(
            name=translate(_("Currency cash")),
            symbol=cash_currency,
            type="currency",
            quote_currency=cash_currency,
            unit_label=cash_currency,
            allocations=(DemoAllocation(translate(_("Currencies")), 10000),),
        ),
    )

    end = reference
    dates = _weekly_dates(end, 104)
    price_series: dict[str, list[tuple[date, Decimal]]] = {
        translate(_("Global ETF")): list(
            zip(
                dates,
                _walk(
                    rng,
                    start=Decimal("95"),
                    count=104,
                    drift=Decimal("0.0022"),
                    amplitude=Decimal("0.018"),
                    quantum=Decimal("0.01"),
                ),
                strict=True,
            )
        ),
        translate(_("Balanced 80/20 fund")): list(
            zip(
                dates,
                _walk(
                    rng,
                    start=Decimal("120"),
                    count=104,
                    drift=Decimal("0.0015"),
                    amplitude=Decimal("0.010"),
                    quantum=Decimal("0.01"),
                ),
                strict=True,
            )
        ),
        translate(_("Gold")): list(
            zip(
                dates,
                _walk(
                    rng,
                    start=Decimal("1850"),
                    count=104,
                    drift=Decimal("0.0018"),
                    amplitude=Decimal("0.015"),
                    quantum=Decimal("0.01"),
                ),
                strict=True,
            )
        ),
        translate(_("Bitcoin")): list(
            zip(
                dates,
                _walk(
                    rng,
                    start=Decimal("42000"),
                    count=104,
                    drift=Decimal("0.0040"),
                    amplitude=Decimal("0.070"),
                    quantum=Decimal("0.01"),
                ),
                strict=True,
            )
        ),
    }
    split_day = _month_start(reference, -8).replace(day=14)
    us_prices = _walk(
        rng,
        start=Decimal("90"),
        count=104,
        drift=Decimal("0.0030"),
        amplitude=Decimal("0.030"),
        quantum=Decimal("0.01"),
    )
    us_points: list[tuple[date, Decimal]] = []
    for point_day, price in zip(dates, us_prices, strict=True):
        # A 2-for-1 split about eight months before T halves the quoted price.
        us_points.append((point_day, price * 2 if point_day < split_day else price))
    price_series[translate(_("US stock"))] = us_points

    price_points: list[DemoPricePoint] = []
    for instrument, points in price_series.items():
        for point_day, price in points:
            # The newest gold price is older, so the page shows its stale badge.
            if instrument == translate(_("Gold")) and point_day > end - timedelta(days=20):
                continue
            price_points.append(DemoPricePoint(instrument=instrument, day=point_day, price=price))
    price_points.sort(key=lambda row: (row.instrument, row.day))

    fx_currencies = {cash_currency, us_currency}
    fx_points: list[DemoFxPoint] = []
    for quote in sorted(fx_currencies):
        if quote == currency:
            continue
        base_rate = currency_factor(quote) / currency_factor(currency)
        path = _walk(
            rng,
            start=base_rate,
            count=104,
            drift=Decimal("0.0001"),
            amplitude=Decimal("0.006"),
            quantum=Decimal("0.0001"),
        )
        for point_day, rate in zip(dates, path, strict=True):
            fx_points.append(DemoFxPoint(quote_currency=quote, day=point_day, rate=rate))
    fx_points.sort(key=lambda row: (row.quote_currency, row.day))
    us_fx_by_day = {row.day: row.rate for row in fx_points if row.quote_currency == us_currency}

    def us_fx(day: date) -> Decimal | None:
        if currency == us_currency:
            return Decimal(1)
        candidates = [
            row.rate for row in fx_points if row.quote_currency == us_currency and row.day <= day
        ]
        return candidates[-1] if candidates else us_fx_by_day.get(day)

    cash_rate = (
        Decimal(1)
        if cash_currency == currency
        else next(row.rate for row in fx_points if row.quote_currency == cash_currency)
    )

    empty = Money(0, currency)
    txns: list[DemoInstrumentTransaction] = []

    def buy(
        instrument: str,
        day: date,
        quantity: Decimal,
        price: Decimal,
        *,
        fee: Money | None = None,
        fx: Decimal | None = None,
        amounts: Literal["quote", "base"] = "quote",
        base_unit_price: Decimal | None = None,
        base_fee: Money | None = None,
        note: str = "",
    ) -> None:
        txns.append(
            DemoInstrumentTransaction(
                instrument=instrument,
                type="buy",
                day=day,
                quantity=quantity,
                unit_price=price,
                fee=fee or empty,
                fx_to_base=fx,
                amounts=amounts,
                base_unit_price=base_unit_price,
                base_fee=base_fee,
                note=note,
            )
        )

    global_etf = translate(_("Global ETF"))
    balanced = translate(_("Balanced 80/20 fund"))
    us_stock = translate(_("US stock"))
    gold = translate(_("Gold"))
    bitcoin = translate(_("Bitcoin"))
    bond = translate(_("Government bond"))
    cash = translate(_("Currency cash"))

    for offset in range(-23, 1):
        day = min(_clamp_month_day(_month_start(reference, offset), 1), reference)
        buy(
            global_etf,
            day,
            Decimal("2"),
            _price_at(price_series[global_etf], day),
            fee=Money(100, currency),
        )
    for offset in (-20, -16, -12, -8, -4, 0):
        day = min(_clamp_month_day(_month_start(reference, offset), 6), reference)
        if day <= reference:
            buy(
                balanced,
                day,
                Decimal("3"),
                _price_at(price_series[balanced], day),
                fee=Money(150, currency),
            )
    for offset in (-20, -15, -9):
        day = _clamp_month_day(_month_start(reference, offset), 8)
        buy(
            us_stock,
            day,
            Decimal("6"),
            _price_at(price_series[us_stock], day),
            fee=Money(200, us_currency),
            fx=us_fx(day),
        )
    txns.append(
        DemoInstrumentTransaction(
            instrument=us_stock,
            type="split",
            day=split_day,
            quantity=Decimal(2),
            unit_price=Decimal(1),
            fee=Money(0, us_currency),
            fx_to_base=Decimal(1),
            note=translate(_("2-for-1 split")),
        )
    )
    base_buy_day = _clamp_month_day(_month_start(reference, -6), 9)
    buy(
        us_stock,
        base_buy_day,
        Decimal("4"),
        Decimal("90"),
        fee=Money(0, us_currency),
        fx=us_fx(base_buy_day),
        amounts="base",
        base_unit_price=Decimal("200"),
        base_fee=Money(100, currency),
        note=translate(_("Settled in the base currency")),
    )
    sell_day = _clamp_month_day(_month_start(reference, -3), 20)
    txns.append(
        DemoInstrumentTransaction(
            instrument=us_stock,
            type="sell",
            day=sell_day,
            quantity=Decimal("10"),
            unit_price=_price_at(price_series[us_stock], sell_day),
            fee=Money(250, us_currency),
            fx_to_base=us_fx(sell_day),
            note=translate(_("Rebalancing sale")),
        )
    )
    for offset in (-12, -9, -6, -3):
        day = _clamp_month_day(_month_start(reference, offset), 14)
        txns.append(
            DemoInstrumentTransaction(
                instrument=us_stock,
                type="dividend",
                day=day,
                quantity=Decimal(1),
                unit_price=Decimal("12.00"),
                fee=Money(180, us_currency),  # 15% withholding
                fx_to_base=us_fx(day),
                note=translate(_("Quarterly dividend")),
                record_income=True,
                income_category=translate(CAT_INTEREST),
            )
        )
    for offset in (-18, -12, -6, -2):
        day = _clamp_month_day(_month_start(reference, offset), 8)
        buy(gold, day, Decimal("1"), _price_at(price_series[gold], day))
    for offset in (-20, -16, -12, -8, -4, -1):
        day = _clamp_month_day(_month_start(reference, offset), 10)
        buy(bitcoin, day, Decimal("0.05"), _price_at(price_series[bitcoin], day))
    bond_day = _clamp_month_day(_month_start(reference, -10), 12)
    buy(bond, bond_day, Decimal("10"), Decimal("100"))
    cash_day = _clamp_month_day(_month_start(reference, -18), 15)
    buy(cash, cash_day, Decimal("500"), Decimal("1"), fx=cash_rate)
    cash_day_2 = _clamp_month_day(_month_start(reference, -6), 15)
    buy(cash, cash_day_2, Decimal("200"), Decimal("1"), fx=cash_rate)

    txns.sort(key=lambda row: (row.day, row.type, row.instrument, row.quantity))

    maturity = _clamp_month_day(date(reference.year + 5, reference.month, 1), reference.day)
    bond_terms = DemoBondTerms(
        instrument=bond,
        series_code="DEMO-5Y",
        face_value=Money(10000, currency),
        maturity_date=maturity,
        interest_mode="capitalized_annually",
        rate_schedule=(
            (_month_start(reference, 0), 450),
            (_month_start(reference, 12), 500),
        ),
        tax_rate_bp=1900,
    )
    return instruments, tuple(txns), tuple(price_points), tuple(fx_points), bond_terms


def _csv_rows(reference: date, currency: str, translate: Translate) -> tuple[DemoCsvRow, ...]:
    month = _month_start(reference, -3)
    rows = (
        DemoCsvRow(
            day=month.replace(day=2),
            direction="expense",
            amount=convert_amount(Decimal("18.40"), currency),
            category=translate(CAT_EDUCATION),
            description=translate(_("Imported bookshop")),
        ),
        DemoCsvRow(
            day=month.replace(day=3),
            direction="expense",
            amount=convert_amount(Decimal("56.20"), currency),
            category=translate(CAT_FOOD),
            description=translate(_("Imported market")),
        ),
        DemoCsvRow(
            day=month.replace(day=5),
            direction="income",
            amount=convert_amount(Decimal("75.00"), currency),
            category=translate(CAT_SALE),
            description=translate(_("Imported refund")),
        ),
    )
    return tuple(sorted(rows, key=lambda row: (row.day, row.direction, row.description)))
