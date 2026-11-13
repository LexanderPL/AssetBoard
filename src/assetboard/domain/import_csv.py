"""Pure parsing rules for lone ledger CSV imports (F4.3).

The upload framework (``domain.import_upload``) classifies the file and reads its rows; this
module owns the format rules: header names, delimiter detection and the strict value parsers
shared by the three F4.3 adapters. No I/O, no clock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from assetboard.domain.budget import planned_minor
from assetboard.domain.categories import Kind, Treatment, clean_name, name_key
from assetboard.domain.months import InvalidMonth, YearMonth
from assetboard.domain.transactions import EntryError, amount_minor

TRANSACTIONS = "transactions_csv"
BUDGET_LINES = "budget_lines_csv"
CATEGORIES = "categories_csv"
ITEM_VALUATIONS = "item_valuations_csv"
INVESTMENT_TRANSACTIONS = "investment_transactions_csv"
MONTHS = "months_csv"
REFERENCE_RATE_VALUES = "reference_rate_values_csv"
LOANS = "loans_csv"
LOAN_RATE_CHANGES = "loan_rate_changes_csv"
REFERENCE_RATES = "reference_rates_csv"
LOAN_COST_COMPONENTS = "loan_cost_components_csv"
LOAN_OVERPAYMENTS = "loan_overpayments_csv"
LOAN_PAYMENTS = "loan_payments_csv"
LOAN_BALANCE_CHECKPOINTS = "loan_balance_checkpoints_csv"
LOAN_SCENARIOS = "loan_scenarios_csv"
INSTALLMENT_PLANS = "installment_plans_csv"

KNOWN_COLUMNS: dict[str, frozenset[str]] = {
    TRANSACTIONS: frozenset(
        {
            "date",
            "direction",
            "amount",
            "currency",
            "category",
            "description",
            "category_id",
            "id",
            "deleted_at",
        }
    ),
    BUDGET_LINES: frozenset({"month", "kind", "category", "planned", "currency", "category_id"}),
    CATEGORIES: frozenset(
        {"id", "kind", "name", "treatment", "color", "sort_order", "archived_at"}
    ),
    ITEM_VALUATIONS: frozenset(
        {"id", "item_id", "item", "as_of", "value", "currency", "note", "created_at"}
    ),
    INVESTMENT_TRANSACTIONS: frozenset(
        {
            "id",
            "instrument_id",
            "instrument",
            "date",
            "type",
            "quantity",
            "unit_price",
            "quote_currency",
            "fee",
            "fx_to_base",
            "note",
            "settlement_currency",
            "settlement_unit_price",
            "settlement_fee",
            "deleted_at",
        }
    ),
    MONTHS: frozenset({"month", "closed_at", "plan_started_at"}),
    REFERENCE_RATE_VALUES: frozenset(
        {"id", "reference_rate_id", "reference_rate", "effective_from", "value_bp"}
    ),
    REFERENCE_RATES: frozenset({"id", "name", "notes"}),
    LOANS: frozenset(
        {
            "id",
            "name",
            "kind",
            "currency",
            "principal_original",
            "origination_date",
            "first_payment_date",
            "term_months",
            "payment_day",
            "rate_mode",
            "day_count",
            "margin_bp",
            "reference_rate_id",
            "reference_rate",
            "property_item_id",
            "property_item",
            "auto_record_payments",
            "installment_override",
            "overpayment_mode",
            "overpayment_amount",
            "overpayment_strategy",
            "archived_at",
        }
    ),
    LOAN_RATE_CHANGES: frozenset(
        {
            "id",
            "loan_id",
            "loan",
            "effective_from",
            "rate_bp",
            "mode",
            "reference_rate_id",
            "reference_rate",
            "margin_bp",
        }
    ),
    LOAN_COST_COMPONENTS: frozenset(
        {
            "id",
            "loan_id",
            "loan",
            "name",
            "mode",
            "rate_bp",
            "fixed_amount",
            "basis",
            "effective_from",
            "effective_to",
        }
    ),
    LOAN_OVERPAYMENTS: frozenset({"id", "loan_id", "loan", "date", "amount", "note"}),
    LOAN_PAYMENTS: frozenset(
        {
            "id",
            "loan_id",
            "loan",
            "date",
            "kind",
            "total",
            "principal",
            "interest",
            "costs",
            "overpayment",
            "note",
            "source",
            "transaction_id",
        }
    ),
    LOAN_BALANCE_CHECKPOINTS: frozenset(
        {"id", "loan_id", "loan", "as_of", "remaining_principal", "note"}
    ),
    LOAN_SCENARIOS: frozenset(
        {
            "id",
            "loan_id",
            "loan",
            "name",
            "overpayment_mode",
            "overpayment_amount",
            "overpayment_strategy",
            "rate_shift_bp",
            "rate_shift_from",
            "one_off_date",
            "one_off_amount",
        }
    ),
    INSTALLMENT_PLANS: frozenset(
        {
            "id",
            "loan_id",
            "loan",
            "category_id",
            "category",
            "installment",
            "remaining_count",
            "end_date",
            "closed_at",
        }
    ),
}

REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    TRANSACTIONS: frozenset({"date", "direction", "amount", "category"}),
    BUDGET_LINES: frozenset({"month", "category", "planned"}),
    CATEGORIES: frozenset({"kind", "name"}),
    ITEM_VALUATIONS: frozenset({"item", "as_of", "value"}),
    INVESTMENT_TRANSACTIONS: frozenset({"instrument", "date", "type", "quantity", "unit_price"}),
    MONTHS: frozenset({"month"}),
    REFERENCE_RATE_VALUES: frozenset({"reference_rate", "effective_from", "value_bp"}),
    REFERENCE_RATES: frozenset({"id", "name"}),
    LOANS: frozenset({"id", "name", "kind", "currency", "first_payment_date"}),
    LOAN_RATE_CHANGES: frozenset({"id", "loan_id", "effective_from", "rate_bp"}),
    LOAN_COST_COMPONENTS: frozenset({"id", "loan_id", "name", "mode", "effective_from"}),
    LOAN_OVERPAYMENTS: frozenset({"id", "loan_id", "date", "amount"}),
    LOAN_PAYMENTS: frozenset(
        {"id", "loan_id", "date", "kind", "total", "principal", "interest", "costs"}
    ),
    LOAN_BALANCE_CHECKPOINTS: frozenset({"id", "loan_id", "as_of", "remaining_principal"}),
    LOAN_SCENARIOS: frozenset({"id", "loan_id", "name"}),
    INSTALLMENT_PLANS: frozenset(
        {"id", "loan_id", "category_id", "installment", "remaining_count"}
    ),
}

MAX_AMOUNT_TEXT_LENGTH = 64
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_MONTH = re.compile(r"[0-9]{4}-[0-9]{2}")
_DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)


class HeaderError(ValueError):
    """The header cannot be used; ``code`` names the report code."""

    def __init__(self, code: str, column: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.column = column


@dataclass(frozen=True, slots=True)
class HeaderClassification:
    source: str
    columns: tuple[str, ...]
    unknown_columns: tuple[str, ...]
    missing_columns: tuple[str, ...]


def normalize_header(cells: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Trimmed, case-insensitive header names (a BOM is removed from the first cell)."""
    normalized: list[str] = []
    for index, cell in enumerate(cells):
        value = cell.strip().casefold()
        if index == 0:
            value = value.removeprefix("\ufeff")
        normalized.append(value)
    return tuple(normalized)


def detect_delimiter(header_line: str) -> str:
    """Pick ``,`` or ``;`` by which split yields the most known column names.

    A candidate whose cells are all known wins; otherwise the candidate with more known names
    wins, and a tie stays with the canonical comma.
    """
    line = header_line.removeprefix("\ufeff").rstrip("\r\n")
    known: frozenset[str] = frozenset[str]().union(*KNOWN_COLUMNS.values())
    best = (0, 0, ",")
    for delimiter in (",", ";"):
        columns = normalize_header(line.split(delimiter))
        known_count = sum(column in known for column in columns)
        all_known = int(known_count == len(columns) and bool(columns))
        score = (all_known, known_count)
        if score > best[:2]:
            best = (*score, delimiter)
    return best[2]


def classify_header(cells: tuple[str, ...] | list[str]) -> HeaderClassification:
    """Recognise the file kind from its header and list unknown/missing columns."""
    columns = normalize_header(cells)
    if len(columns) != len(set(columns)):
        raise HeaderError("duplicate_column")
    if not columns:
        raise HeaderError("unknown_file")
    scores = {source: len(set(columns) & known) for source, known in KNOWN_COLUMNS.items()}
    # Ties prefer transactions, then planned values, then categories, then item valuations,
    # then investment transactions, then months.
    order = (
        TRANSACTIONS,
        BUDGET_LINES,
        CATEGORIES,
        ITEM_VALUATIONS,
        INVESTMENT_TRANSACTIONS,
        REFERENCE_RATE_VALUES,
        REFERENCE_RATES,
        LOANS,
        LOAN_RATE_CHANGES,
        LOAN_COST_COMPONENTS,
        LOAN_OVERPAYMENTS,
        LOAN_PAYMENTS,
        LOAN_BALANCE_CHECKPOINTS,
        LOAN_SCENARIOS,
        INSTALLMENT_PLANS,
        MONTHS,
    )
    source = max(order, key=lambda item: (scores[item], -order.index(item)))
    if scores[source] == 0:
        raise HeaderError("unknown_file")
    known = KNOWN_COLUMNS[source]
    return HeaderClassification(
        source=source,
        columns=columns,
        unknown_columns=tuple(column for column in columns if column not in known),
        missing_columns=tuple(sorted(REQUIRED_COLUMNS[source] - set(columns))),
    )


def parse_date(value: str) -> date:
    """A strict ``YYYY-MM-DD`` date in 1900-01-01..2199-12-31."""
    text = value.strip()
    if not _DATE.fullmatch(text):
        raise EntryError("invalid_date")
    try:
        result = date.fromisoformat(text)
    except ValueError:
        raise EntryError("invalid_date") from None
    if not (date(1900, 1, 1) <= result <= date(2199, 12, 31)):
        raise EntryError("date_out_of_range")
    return result


def parse_month(value: str) -> YearMonth:
    text = value.strip()
    if not _MONTH.fullmatch(text):
        raise EntryError("invalid_month")
    try:
        return YearMonth.parse(text)
    except InvalidMonth:
        raise EntryError("invalid_month") from None


def parse_timestamp(value: str) -> datetime:
    """An archive timestamp, converted to UTC; an empty value is invalid here."""
    text = value.strip()
    if not _TIMESTAMP.fullmatch(text):
        raise EntryError("invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(text)
    except (ValueError, OverflowError):
        raise EntryError("invalid_timestamp") from None
    if parsed.tzinfo is None:
        raise EntryError("invalid_timestamp")
    return parsed.astimezone(UTC)


def parse_uuid(value: str) -> UUID:
    try:
        return UUID(value.strip())
    except (ValueError, AttributeError):
        raise EntryError("invalid_id") from None


def parse_decimal(value: str, *, decimal: str) -> Decimal:
    """A plain unsigned decimal in the chosen separator; never a locale-formatted number."""
    text = value.strip()
    if not text or len(text) > MAX_AMOUNT_TEXT_LENGTH:
        raise EntryError("invalid_amount")
    if decimal == "comma":
        if "." in text:
            raise EntryError("invalid_amount")
        text = text.replace(",", ".")
    elif "," in text:
        raise EntryError("invalid_amount")
    if not _DECIMAL.fullmatch(text):
        raise EntryError("invalid_amount")
    return Decimal(text)


def parse_transaction_amount(value: str, currency: str, *, decimal: str) -> int:
    return amount_minor(parse_decimal(value, decimal=decimal), currency)


def parse_planned_amount(value: str, currency: str, *, decimal: str) -> int:
    text = value.strip()
    if not text:
        return 0
    return planned_minor(parse_decimal(text, decimal=decimal), currency)


def parse_direction(value: str) -> Kind:
    try:
        return Kind(value.strip())
    except ValueError:
        raise EntryError("invalid_direction") from None


def parse_kind(value: str) -> Kind:
    try:
        return Kind(value.strip())
    except ValueError:
        raise EntryError("invalid_kind") from None


def parse_treatment(value: str) -> Treatment:
    text = value.strip()
    if not text:
        return Treatment.REGULAR
    try:
        return Treatment(text)
    except ValueError:
        raise EntryError("invalid_treatment") from None


def parse_sort_order(value: str) -> int:
    text = value.strip()
    if not text or not re.fullmatch(r"[0-9]{1,9}", text):
        raise EntryError("invalid_sort_order")
    return int(text)


def parse_category_name(value: str) -> str:
    try:
        return clean_name(value)
    except ValueError:
        raise EntryError("invalid_category_name") from None


def parse_currency(value: str, base_currency: str) -> str:
    text = value.strip()
    if not text:
        return base_currency
    if text != base_currency:
        raise EntryError("invalid_currency")
    return text


def parse_category_key(value: str) -> str:
    return name_key(parse_category_name(value))


TEMPLATE_FILES: dict[str, bytes] = {
    "transactions.csv": (
        b"date,direction,amount,category,description\r\n"
        b"2025-01-03,expense,12.5,Food,Bakery\r\n"
        b"2025-01-03,expense,12.5,Food,Bakery\r\n"
        b"2025-01-31,income,3000,Salary,\r\n"
    ),
    "budget_lines.csv": (
        b"month,kind,category,planned\r\n"
        b"2026-09,expense,Food,550.00\r\n"
        b"2026-09,income,Salary,3000.00\r\n"
    ),
    "item_valuations.csv": (
        b"item,as_of,value\r\nCar,2026-01-31,12000.00\r\nCar,2026-02-28,11500.00\r\n"
    ),
}
