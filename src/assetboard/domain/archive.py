"""Pure workspace archive format (F4.1): deterministic CSV/ZIP encoding and validation."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import stat
import zipfile
import zlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import cast
from uuid import UUID

from assetboard.domain.bonds import (
    MAX_ANNUAL_RATE_BP,
    MAX_SERIES_CODE_LENGTH,
    BondRatePeriod,
    InterestMode,
    validate_rate_schedule,
)
from assetboard.domain.categories import Kind, Treatment, name_key
from assetboard.domain.fire import (
    MAX_HORIZON_AGE,
    MAX_STOP_WORKING_AGE,
    MAX_YEAR,
    MIN_YEAR,
    RATE_HARD_MAX_BP,
    RATE_HARD_MIN_BP,
    SWR_HARD_MAX_BP,
    SWR_HARD_MIN_BP,
    EventKind,
    ExpensesMode,
    IncomeMode,
    PortfolioMode,
    Rule,
    clean_event_label,
    clean_scenario_name,
    contribution_years,
    event_years,
)
from assetboard.domain.holdings import MAX_NOTE_LENGTH, InvestmentTransactionType
from assetboard.domain.instruments import (
    MAX_ASSET_CLASS_NAME_LENGTH,
    MAX_INSTRUMENT_NAME_LENGTH,
    MAX_SYMBOL_LENGTH,
    MAX_UNIT_LABEL_LENGTH,
    InstrumentType,
    RiskTier,
    ValuationSource,
)
from assetboard.domain.loans import (
    MAX_LOAN_NAME_LENGTH,
    MAX_TERM_MONTHS,
    DayCount,
    LoanKind,
    LoanPaymentKind,
    LoanPaymentSource,
    OverpaymentMode,
    OverpaymentStrategy,
    RateMode,
)
from assetboard.domain.money import minor_unit_for
from assetboard.domain.networth import MAX_ITEM_NAME_LENGTH, MAX_NOTES_LENGTH, Side
from assetboard.domain.transactions import MAX_AMOUNT_MINOR

FORMAT = "workspace-archive"
FORMAT_VERSION = "1.8"
# Bounds of the optional ``upcoming_days`` key (1.1); mirror ``config.MIN/MAX_UPCOMING_DAYS``.
MIN_UPCOMING_DAYS = 1
MAX_UPCOMING_DAYS = 365
CSV_COLUMNS = {
    "categories.csv": ("id", "kind", "name", "treatment", "color", "sort_order", "archived_at"),
    "budget_lines.csv": ("month", "kind", "category", "planned", "currency", "category_id"),
    "transactions.csv": (
        "date",
        "direction",
        "amount",
        "currency",
        "category",
        "description",
        "category_id",
        "id",
        "deleted_at",
    ),
    "months.csv": ("month", "closed_at", "plan_started_at"),
    "networth_categories.csv": ("id", "side", "name", "color", "sort_order", "archived_at"),
    "networth_items.csv": (
        "id",
        "side",
        "category",
        "category_id",
        "name",
        "currency",
        "notes",
        "acquired_on",
        "valuation_mode",
        "link_type",
        "link_id",
        "include_in_net_worth",
        "investable",
        "estimate_between",
        "archived_at",
    ),
    "item_valuations.csv": (
        "id",
        "item_id",
        "item",
        "as_of",
        "value",
        "currency",
        "note",
        "created_at",
    ),
    "networth_snapshots.csv": (
        "id",
        "as_of",
        "assets",
        "liabilities",
        "currency",
        "breakdown",
        "source",
    ),
    "asset_classes.csv": (
        "id",
        "name",
        "investable",
        "color",
        "sort_order",
        "risk_tier",
        "archived_at",
    ),
    "instruments.csv": (
        "id",
        "name",
        "symbol",
        "type",
        "quote_currency",
        "unit_label",
        "valuation_source",
        "provider_key",
        "provider_symbol",
        "provider_backfilled_at",
        "risk_tier_override",
        "archived_at",
    ),
    "instrument_allocations.csv": (
        "id",
        "instrument_id",
        "instrument",
        "asset_class_id",
        "asset_class",
        "weight_bp",
    ),
    "investment_transactions.csv": (
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
        "income_transaction_id",
    ),
    "bond_terms.csv": (
        "id",
        "instrument_id",
        "instrument",
        "series_code",
        "face_value",
        "maturity_date",
        "interest_mode",
        "rate_schedule",
        "tax_rate_bp",
        "early_redemption_fee_mode",
        "early_redemption_fee_rate_bp",
        "early_redemption_fee",
    ),
    "price_points.csv": (
        "id",
        "instrument_id",
        "instrument",
        "date",
        "price",
        "source",
        "provider_key",
    ),
    "fx_rates.csv": (
        "id",
        "base_currency",
        "quote_currency",
        "date",
        "rate",
        "source",
    ),
    "price_source_configs.csv": (
        "id",
        "provider_key",
        "enabled",
        "daily_call_budget",
        "fx_backfilled",
    ),
    "reference_rates.csv": ("id", "name", "notes"),
    "reference_rate_values.csv": (
        "id",
        "reference_rate_id",
        "reference_rate",
        "effective_from",
        "value_bp",
    ),
    "loans.csv": (
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
        "drawdown_mode",
        "interest_only_months",
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
    ),
    "loan_rate_changes.csv": (
        "id",
        "loan_id",
        "loan",
        "effective_from",
        "rate_bp",
        "mode",
        "reference_rate_id",
        "reference_rate",
        "margin_bp",
    ),
    "loan_transfers.csv": ("id", "loan_id", "loan", "date", "amount", "note"),
    "loan_cost_components.csv": (
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
    ),
    "loan_overpayments.csv": ("id", "loan_id", "loan", "date", "amount", "note"),
    "loan_payments.csv": (
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
    ),
    "loan_balance_checkpoints.csv": (
        "id",
        "loan_id",
        "loan",
        "as_of",
        "remaining_principal",
        "note",
    ),
    "loan_scenarios.csv": (
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
    ),
    "installment_plans.csv": (
        "id",
        "loan_id",
        "loan",
        "category_id",
        "category",
        "installment",
        "remaining_count",
        "end_date",
        "closed_at",
    ),
    "fire_scenarios.csv": (
        "id",
        "name",
        "currency",
        "birth_year",
        "start_year",
        "inflation_bp",
        "real_return_bp",
        "income_growth_bp",
        "stop_working_age",
        "horizon_age",
        "rule",
        "swr_bp",
        "income_mode",
        "income_manual",
        "income_fallback",
        "expenses_mode",
        "expenses_manual",
        "expenses_fallback",
        "portfolio_mode",
        "portfolio_manual",
        "portfolio_fallback",
        "is_default",
        "archived_at",
    ),
    "fire_contributions.csv": (
        "id",
        "scenario_id",
        "scenario",
        "year",
        "net_amount",
    ),
    "fire_events.csv": (
        "id",
        "scenario_id",
        "scenario",
        "year",
        "kind",
        "amount",
        "label",
        "source_loan_id",
    ),
    "fire_expense_exclusions.csv": (
        "id",
        "scenario_id",
        "scenario",
        "category_id",
        "category",
    ),
}
BASE_CSV_FILES = ("categories.csv", "budget_lines.csv", "transactions.csv", "months.csv")
NETWORTH_CSV_FILES = (
    "networth_categories.csv",
    "networth_items.csv",
    "item_valuations.csv",
    "networth_snapshots.csv",
)
INVESTMENT_CSV_FILES = (
    "asset_classes.csv",
    "instruments.csv",
    "instrument_allocations.csv",
    "investment_transactions.csv",
    "bond_terms.csv",
    "price_points.csv",
    "fx_rates.csv",
    "price_source_configs.csv",
)
LOAN_CSV_FILES = (
    "reference_rates.csv",
    "reference_rate_values.csv",
    "loans.csv",
    "loan_rate_changes.csv",
    "loan_cost_components.csv",
    "loan_overpayments.csv",
    "loan_payments.csv",
    "loan_balance_checkpoints.csv",
    "loan_scenarios.csv",
    "installment_plans.csv",
)
FIRE_CSV_FILES = (
    "fire_scenarios.csv",
    "fire_contributions.csv",
    "fire_events.csv",
    "fire_expense_exclusions.csv",
)
LOAN_TRANSFER_CSV_FILES = ("loan_transfers.csv",)
FILE_ORDER = ("workspace.json", *CSV_COLUMNS)
REQUIRED_FILES = ("manifest.json", *FILE_ORDER)
LINK_TYPES = frozenset({"portfolio_asset_class", "portfolio_holding", "loan_balance"})
SNAPSHOT_SOURCES = frozenset({"auto", "manual", "backfill"})

# Reader limits for untrusted archives (F4.2 "ZIP safety"); the reader stops at each of them.
MAX_MEMBERS = 64
MAX_MEMBER_BYTES = 100 * 1024 * 1024
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
RATIO_EXEMPT_BYTES = 1024 * 1024
MAX_DATA_ROWS = 1_000_000
MAX_COLUMNS = 64
MAX_ERRORS = 1000
MAX_DECIMAL_MAJOR = Decimal(MAX_AMOUNT_MINOR)


def required_files(version: str) -> tuple[str, ...]:
    """Archive members required by a compatible ``MAJOR.MINOR`` version."""
    major, minor = (int(part) for part in version.split(".", 1))
    if major != 1:
        return REQUIRED_FILES
    csv_files = BASE_CSV_FILES + (NETWORTH_CSV_FILES if minor >= 2 else ())
    if minor >= 4:
        csv_files += INVESTMENT_CSV_FILES
    if minor >= 5:
        csv_files += LOAN_CSV_FILES
    if minor >= 7:
        csv_files += LOAN_TRANSFER_CSV_FILES
    if minor >= 8:
        csv_files += FIRE_CSV_FILES
    return ("manifest.json", "workspace.json", *csv_files)


@dataclass(frozen=True, slots=True)
class CategoryRow:
    id: str
    kind: str
    name: str
    treatment: str
    color: str
    sort_order: int
    archived_at: str | None


@dataclass(frozen=True, slots=True)
class BudgetLineRow:
    month: str
    kind: str
    category: str
    planned: str
    currency: str
    category_id: str


@dataclass(frozen=True, slots=True)
class TransactionRow:
    date: str
    direction: str
    amount: str
    currency: str
    category: str
    description: str
    category_id: str
    id: str
    deleted_at: str | None


@dataclass(frozen=True, slots=True)
class MonthRow:
    month: str
    closed_at: str | None
    plan_started_at: str | None


@dataclass(frozen=True, slots=True)
class NetWorthCategoryRow:
    id: str
    side: str
    name: str
    color: str
    sort_order: int
    archived_at: str | None


@dataclass(frozen=True, slots=True)
class NetWorthItemRow:
    id: str
    side: str
    category: str
    category_id: str
    name: str
    currency: str
    notes: str
    acquired_on: str | None
    valuation_mode: str
    link_type: str | None
    link_id: str | None
    include_in_net_worth: str
    investable: str
    archived_at: str | None
    estimate_between: str = ""


@dataclass(frozen=True, slots=True)
class ItemValuationRow:
    id: str
    item_id: str
    item: str
    as_of: str
    value: str
    currency: str
    note: str
    created_at: str | None


@dataclass(frozen=True, slots=True)
class NetWorthSnapshotRow:
    id: str
    as_of: str
    assets: str
    liabilities: str
    currency: str
    breakdown: str
    source: str


@dataclass(frozen=True, slots=True)
class AssetClassRow:
    id: str
    name: str
    investable: str
    color: str
    sort_order: int
    risk_tier: str
    archived_at: str | None


@dataclass(frozen=True, slots=True)
class InstrumentRow:
    id: str
    name: str
    symbol: str
    type: str
    quote_currency: str
    unit_label: str
    valuation_source: str
    provider_key: str
    provider_symbol: str
    provider_backfilled_at: str | None
    risk_tier_override: str
    archived_at: str | None


@dataclass(frozen=True, slots=True)
class InstrumentAllocationRow:
    id: str
    instrument_id: str
    instrument: str
    asset_class_id: str
    asset_class: str
    weight_bp: int


@dataclass(frozen=True, slots=True)
class InvestmentTransactionRow:
    id: str
    instrument_id: str
    instrument: str
    date: str | None
    type: str
    quantity: str
    unit_price: str
    quote_currency: str
    fee: str
    fx_to_base: str
    note: str
    settlement_currency: str
    settlement_unit_price: str
    settlement_fee: str
    deleted_at: str | None
    income_transaction_id: str | None = None


@dataclass(frozen=True, slots=True)
class BondTermsRow:
    id: str
    instrument_id: str
    instrument: str
    series_code: str
    face_value: str
    maturity_date: str
    interest_mode: str
    rate_schedule: str
    tax_rate_bp: int
    early_redemption_fee_mode: str
    early_redemption_fee_rate_bp: int | None
    early_redemption_fee: str


@dataclass(frozen=True, slots=True)
class PricePointRow:
    id: str
    instrument_id: str
    instrument: str
    date: str
    price: str
    source: str
    provider_key: str


@dataclass(frozen=True, slots=True)
class FxRateRow:
    id: str
    base_currency: str
    quote_currency: str
    date: str
    rate: str
    source: str


@dataclass(frozen=True, slots=True)
class PriceSourceConfigRow:
    id: str
    provider_key: str
    enabled: str
    daily_call_budget: int
    fx_backfilled: str


@dataclass(frozen=True, slots=True)
class RiskTargetPolicyRow:
    mode: str
    safe_bp: int
    growth_bp: int
    speculative_bp: int
    tolerance_bp: int


@dataclass(frozen=True, slots=True)
class ReferenceRateRow:
    id: str
    name: str
    notes: str


@dataclass(frozen=True, slots=True)
class ReferenceRateValueRow:
    id: str
    reference_rate_id: str
    reference_rate: str
    effective_from: str
    value_bp: int


@dataclass(frozen=True, slots=True)
class LoanRow:
    id: str
    name: str
    kind: str
    currency: str
    principal_original: str
    origination_date: str | None
    first_payment_date: str
    term_months: int
    payment_day: int
    rate_mode: str
    day_count: str
    drawdown_mode: str
    interest_only_months: int
    margin_bp: int | None
    reference_rate_id: str | None
    reference_rate: str
    property_item_id: str | None
    property_item: str
    auto_record_payments: str
    installment_override: str
    overpayment_mode: str
    overpayment_amount: str
    overpayment_strategy: str
    archived_at: str | None


@dataclass(frozen=True, slots=True)
class LoanRateChangeRow:
    id: str
    loan_id: str
    loan: str
    effective_from: str
    rate_bp: int | None
    mode: str = "fixed"
    reference_rate_id: str | None = None
    reference_rate: str | None = None
    margin_bp: int | None = None


@dataclass(frozen=True, slots=True)
class LoanTransferRow:
    id: str
    loan_id: str
    loan: str
    date: str
    amount: str
    note: str


@dataclass(frozen=True, slots=True)
class LoanCostComponentRow:
    id: str
    loan_id: str
    loan: str
    name: str
    mode: str
    rate_bp: int | None
    fixed_amount: str
    basis: str
    effective_from: str
    effective_to: str | None


@dataclass(frozen=True, slots=True)
class LoanOverpaymentRow:
    id: str
    loan_id: str
    loan: str
    date: str
    amount: str
    note: str


@dataclass(frozen=True, slots=True)
class LoanPaymentRow:
    id: str
    loan_id: str
    loan: str
    date: str
    kind: str
    total: str
    principal: str
    interest: str
    costs: str
    overpayment: str
    note: str
    source: str
    transaction_id: str | None


@dataclass(frozen=True, slots=True)
class LoanBalanceCheckpointRow:
    id: str
    loan_id: str
    loan: str
    as_of: str
    remaining_principal: str
    note: str


@dataclass(frozen=True, slots=True)
class LoanScenarioRow:
    id: str
    loan_id: str
    loan: str
    name: str
    overpayment_mode: str
    overpayment_amount: str
    overpayment_strategy: str
    rate_shift_bp: int | None
    rate_shift_from: str | None
    one_off_date: str | None
    one_off_amount: str


@dataclass(frozen=True, slots=True)
class InstallmentPlanRow:
    id: str
    loan_id: str
    loan: str
    category_id: str
    category: str
    installment: str
    remaining_count: int
    end_date: str
    closed_at: str | None


@dataclass(frozen=True, slots=True)
class FireScenarioRow:
    id: str
    name: str
    currency: str
    birth_year: int
    start_year: int
    inflation_bp: int
    real_return_bp: int
    income_growth_bp: int
    stop_working_age: int
    horizon_age: int
    rule: str
    swr_bp: int | None
    income_mode: str
    income_manual: str
    income_fallback: str
    expenses_mode: str
    expenses_manual: str
    expenses_fallback: str
    portfolio_mode: str
    portfolio_manual: str
    portfolio_fallback: str
    is_default: str
    archived_at: str | None


@dataclass(frozen=True, slots=True)
class FireContributionRow:
    id: str
    scenario_id: str
    scenario: str
    year: int
    net_amount: str


@dataclass(frozen=True, slots=True)
class FireEventRow:
    id: str
    scenario_id: str
    scenario: str
    year: int
    kind: str
    amount: str
    label: str
    source_loan_id: str | None


@dataclass(frozen=True, slots=True)
class FireExpenseExclusionRow:
    id: str
    scenario_id: str
    scenario: str
    category_id: str
    category: str


@dataclass(frozen=True, slots=True)
class ArchiveRows:
    workspace: dict[str, object]
    categories: tuple[CategoryRow, ...]
    budget_lines: tuple[BudgetLineRow, ...]
    transactions: tuple[TransactionRow, ...]
    months: tuple[MonthRow, ...]
    networth_categories: tuple[NetWorthCategoryRow, ...] = ()
    networth_items: tuple[NetWorthItemRow, ...] = ()
    item_valuations: tuple[ItemValuationRow, ...] = ()
    networth_snapshots: tuple[NetWorthSnapshotRow, ...] = ()
    asset_classes: tuple[AssetClassRow, ...] = ()
    instruments: tuple[InstrumentRow, ...] = ()
    instrument_allocations: tuple[InstrumentAllocationRow, ...] = ()
    investment_transactions: tuple[InvestmentTransactionRow, ...] = ()
    bond_terms: tuple[BondTermsRow, ...] = ()
    price_points: tuple[PricePointRow, ...] = ()
    fx_rates: tuple[FxRateRow, ...] = ()
    price_source_configs: tuple[PriceSourceConfigRow, ...] = ()
    risk_target_policy: RiskTargetPolicyRow | None = None
    reference_rates: tuple[ReferenceRateRow, ...] = ()
    reference_rate_values: tuple[ReferenceRateValueRow, ...] = ()
    loans: tuple[LoanRow, ...] = ()
    loan_rate_changes: tuple[LoanRateChangeRow, ...] = ()
    loan_transfers: tuple[LoanTransferRow, ...] = ()
    loan_cost_components: tuple[LoanCostComponentRow, ...] = ()
    loan_overpayments: tuple[LoanOverpaymentRow, ...] = ()
    loan_payments: tuple[LoanPaymentRow, ...] = ()
    loan_balance_checkpoints: tuple[LoanBalanceCheckpointRow, ...] = ()
    loan_scenarios: tuple[LoanScenarioRow, ...] = ()
    installment_plans: tuple[InstallmentPlanRow, ...] = ()
    fire_scenarios: tuple[FireScenarioRow, ...] = ()
    fire_contributions: tuple[FireContributionRow, ...] = ()
    fire_events: tuple[FireEventRow, ...] = ()
    fire_expense_exclusions: tuple[FireExpenseExclusionRow, ...] = ()


@dataclass(frozen=True, slots=True)
class ArchiveError:
    file: str
    message: str
    line: int | None = None
    column: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedArchive:
    manifest: dict[str, object]
    files: dict[str, bytes]
    rows: ArchiveRows
    warnings: tuple[ArchiveError, ...] = ()


def csv_bytes(filename: str, rows: tuple[object, ...]) -> bytes:
    """Serialize typed rows using the canonical header and RFC 4180 CRLF endings."""
    columns = CSV_COLUMNS[filename]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output, fieldnames=columns, lineterminator="\r\n", extrasaction="ignore"
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {key: "" if getattr(row, key) is None else getattr(row, key) for key in columns}
        )
    return output.getvalue().encode("utf-8")


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _csv_data_rows(data: bytes) -> int:
    return max(
        0,
        sum(1 for _ in csv.reader(io.StringIO(data.decode("utf-8-sig"), newline=""), strict=True))
        - 1,
    )


def _string_keyed_object(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, object] = {}
    for key, item in cast(dict[object, object], value).items():
        if not isinstance(key, str):
            return None
        result[key] = item
    return result


def build_files(rows: ArchiveRows) -> dict[str, bytes]:
    """Return every data file of the current format, in canonical order."""
    return {
        "workspace.json": _json_bytes(rows.workspace),
        "categories.csv": csv_bytes("categories.csv", rows.categories),
        "budget_lines.csv": csv_bytes("budget_lines.csv", rows.budget_lines),
        "transactions.csv": csv_bytes("transactions.csv", rows.transactions),
        "months.csv": csv_bytes("months.csv", rows.months),
        "networth_categories.csv": csv_bytes("networth_categories.csv", rows.networth_categories),
        "networth_items.csv": csv_bytes("networth_items.csv", rows.networth_items),
        "item_valuations.csv": csv_bytes("item_valuations.csv", rows.item_valuations),
        "networth_snapshots.csv": csv_bytes("networth_snapshots.csv", rows.networth_snapshots),
        "asset_classes.csv": csv_bytes("asset_classes.csv", rows.asset_classes),
        "instruments.csv": csv_bytes("instruments.csv", rows.instruments),
        "instrument_allocations.csv": csv_bytes(
            "instrument_allocations.csv", rows.instrument_allocations
        ),
        "investment_transactions.csv": csv_bytes(
            "investment_transactions.csv", rows.investment_transactions
        ),
        "bond_terms.csv": csv_bytes("bond_terms.csv", rows.bond_terms),
        "price_points.csv": csv_bytes("price_points.csv", rows.price_points),
        "fx_rates.csv": csv_bytes("fx_rates.csv", rows.fx_rates),
        "price_source_configs.csv": csv_bytes(
            "price_source_configs.csv", rows.price_source_configs
        ),
        "reference_rates.csv": csv_bytes("reference_rates.csv", rows.reference_rates),
        "reference_rate_values.csv": csv_bytes(
            "reference_rate_values.csv", rows.reference_rate_values
        ),
        "loans.csv": csv_bytes("loans.csv", rows.loans),
        "loan_rate_changes.csv": csv_bytes("loan_rate_changes.csv", rows.loan_rate_changes),
        "loan_transfers.csv": csv_bytes("loan_transfers.csv", rows.loan_transfers),
        "loan_cost_components.csv": csv_bytes(
            "loan_cost_components.csv", rows.loan_cost_components
        ),
        "loan_overpayments.csv": csv_bytes("loan_overpayments.csv", rows.loan_overpayments),
        "loan_payments.csv": csv_bytes("loan_payments.csv", rows.loan_payments),
        "loan_balance_checkpoints.csv": csv_bytes(
            "loan_balance_checkpoints.csv", rows.loan_balance_checkpoints
        ),
        "loan_scenarios.csv": csv_bytes("loan_scenarios.csv", rows.loan_scenarios),
        "installment_plans.csv": csv_bytes("installment_plans.csv", rows.installment_plans),
        "fire_scenarios.csv": csv_bytes("fire_scenarios.csv", rows.fire_scenarios),
        "fire_contributions.csv": csv_bytes("fire_contributions.csv", rows.fire_contributions),
        "fire_events.csv": csv_bytes("fire_events.csv", rows.fire_events),
        "fire_expense_exclusions.csv": csv_bytes(
            "fire_expense_exclusions.csv", rows.fire_expense_exclusions
        ),
    }


def manifest_bytes(
    files: dict[str, bytes],
    *,
    app_name: str,
    app_version: str,
    exported_at: datetime,
    base_currency: str,
) -> bytes:
    """Build a manifest with a checksum and data-row count for every member."""
    listed: list[dict[str, object]] = []
    for path in FILE_ORDER:
        data = files[path]
        row_count = None if path == "workspace.json" else _csv_data_rows(data)
        listed.append(
            {
                "path": path,
                "sha256": hashlib.sha256(data).hexdigest(),
                "rows": row_count,
                "bytes": len(data),
            }
        )
    return _json_bytes(
        {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "app": {"name": app_name, "version": app_version},
            "exported_at": timestamp_text(exported_at),
            "base_currency": base_currency,
            "files": listed,
        }
    )


def timestamp_text(value: datetime | None) -> str | None:
    """Format a timestamp as the archive's UTC text (naive values are UTC)."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (
        value.astimezone(UTC)
        .isoformat(timespec="microseconds" if value.microsecond else "seconds")
        .replace("+00:00", "Z")
    )


def decimal_text(value: Decimal) -> str:
    """Plain decimal text, never an exponent and without trailing fractional zeros (F6.9).

    ``Decimal("12.3400")`` becomes ``"12.34"`` and ``Decimal("0.000")`` becomes ``"0"``. The
    value is not rounded; ``Decimal`` parses the text back exactly.
    """
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def lineage_id(row_id: UUID, import_key: str | None) -> UUID:
    """Preserve origin IDs across restore/export cycles; content keys use the local ID."""
    return UUID(import_key[3:]) if import_key and import_key.startswith("id:") else row_id


def zip_bytes(files: dict[str, bytes], *, exported_at: datetime) -> bytes:
    """Create a deterministic deflated ZIP with fixed entry order and timestamps."""
    stamp = exported_at.astimezone(UTC)
    year = max(1980, min(stamp.year, 2107))
    zip_time = (year, stamp.month, stamp.day, stamp.hour, stamp.minute, stamp.second // 2 * 2)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in REQUIRED_FILES:
            info = zipfile.ZipInfo(name, zip_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            archive.writestr(info, files[name])
    return output.getvalue()


def write_archive(
    rows: ArchiveRows,
    *,
    app_name: str,
    app_version: str,
    exported_at: datetime,
    base_currency: str,
) -> bytes:
    files = build_files(rows)
    files["manifest.json"] = manifest_bytes(
        files,
        app_name=app_name,
        app_version=app_version,
        exported_at=exported_at,
        base_currency=base_currency,
    )
    return zip_bytes(files, exported_at=exported_at)


def parse_archive(data: bytes) -> tuple[ParsedArchive | None, tuple[ArchiveError, ...]]:
    """Read an in-memory archive and verify its member set, checksums and row counts."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                return None, (ArchiveError("archive", "duplicate member names"),)
            # F4.2's documented limits also bound this shared reader before upload exists.
            if len(names) > MAX_MEMBERS:
                return None, (ArchiveError("archive", "too many members"),)
            files: dict[str, bytes] = {}
            total = 0
            for info in archive.infolist():
                mode = info.external_attr >> 16
                if (
                    any(part in info.filename for part in ("/", "\\", "..", ":"))
                    or (stat.S_IFMT(mode) not in (0, stat.S_IFREG))
                    or info.flag_bits & 1
                    or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                ):
                    return None, (ArchiveError("archive", "unsafe member"),)
                with archive.open(info) as member:
                    content = member.read(MAX_MEMBER_BYTES + 1)
                total += len(content)
                if (
                    len(content) > MAX_MEMBER_BYTES
                    or total > MAX_TOTAL_BYTES
                    or (
                        len(content) >= RATIO_EXEMPT_BYTES
                        and len(content) > info.compress_size * MAX_COMPRESSION_RATIO
                    )
                ):
                    return None, (ArchiveError("archive", "uncompressed size or ratio limit"),)
                files[info.filename] = content
    except (
        zipfile.BadZipFile,
        OSError,
        RuntimeError,
        NotImplementedError,
        EOFError,
        zlib.error,
    ) as exc:
        return None, (ArchiveError("archive", str(exc)),)
    if "manifest.json" not in files:
        return None, (ArchiveError("archive", "missing required members"),)
    try:
        loaded_manifest: object = json.loads(files["manifest.json"])
    except (ValueError, RecursionError):  # also over-long integers and deep nesting
        return None, (ArchiveError("manifest.json", "invalid JSON"),)
    manifest = _string_keyed_object(loaded_manifest)
    if manifest is None:
        return None, (ArchiveError("manifest.json", "invalid JSON object"),)
    if manifest.get("format") != FORMAT:
        return None, (ArchiveError("manifest.json", "unsupported archive format"),)
    version = manifest.get("format_version")
    match = (
        re.fullmatch(r"([0-9]{1,4})\.([0-9]{1,4})", version) if isinstance(version, str) else None
    )
    if match is None:
        return None, (ArchiveError("manifest.json", "unsupported archive version"),)
    major, minor = int(match.group(1)), int(match.group(2))
    current_major, current_minor = (int(part) for part in FORMAT_VERSION.split("."))
    if major > current_major:
        return None, (ArchiveError("manifest.json", "made by a newer version of the app"),)
    if major < current_major:  # no 0.x upgrade path exists; 1.x is the first format
        return None, (ArchiveError("manifest.json", "unsupported archive version"),)
    required = required_files(str(version))
    if not set(required) <= set(names):
        return None, (ArchiveError("archive", "missing required members"),)
    warnings = [
        ArchiveError(name, "unknown file ignored") for name in names if name not in REQUIRED_FILES
    ]
    if minor > current_minor:
        warnings.append(ArchiveError("manifest.json", f"newer minor version: {version}"))
    warnings.extend(
        ArchiveError("manifest.json", f"unknown key: {key}")
        for key in manifest.keys()
        - {"format", "format_version", "app", "exported_at", "base_currency", "files"}
    )
    try:
        _parse_timestamp(str(manifest.get("exported_at", "")))
        currency = manifest.get("base_currency")
        if not isinstance(currency, str) or currency != currency.upper():
            raise ValueError
        minor_unit_for(currency)
        app = _string_keyed_object(manifest.get("app"))
        if app is None or not all(isinstance(app.get(key), str) for key in ("name", "version")):
            raise ValueError
    except (TypeError, ValueError):
        return None, (ArchiveError("manifest.json", "invalid metadata"),)
    entries_value = manifest.get("files")
    if not isinstance(entries_value, list):
        return None, (ArchiveError("manifest.json", "files must be a list"),)
    entries = cast(list[object], entries_value)
    expected: dict[str, dict[str, object]] = {}
    for value in entries:
        entry = _string_keyed_object(value)
        if entry is None:
            return None, (ArchiveError("manifest.json", "invalid file entry"),)
        path = entry.get("path")
        if not isinstance(path, str):
            return None, (ArchiveError("manifest.json", "invalid file path"),)
        expected[path] = entry
    # Unlisted extra members are only warned about above (ADR-0016: readers ignore what they
    # do not know); every listed file must exist and every data file must be listed.
    if (
        len(expected) != len(entries)
        or "manifest.json" in expected
        or not set(required) - {"manifest.json"} <= set(expected)
        or not set(expected) <= set(files)
    ):
        return None, (ArchiveError("manifest.json", "invalid file list"),)
    errors: list[ArchiveError] = []
    data_rows = 0
    for name, entry in expected.items():
        raw = files[name]
        if entry.get("sha256") != hashlib.sha256(raw).hexdigest():
            errors.append(ArchiveError(name, "checksum mismatch"))
        if entry.get("bytes") != len(raw):
            errors.append(ArchiveError(name, "byte count mismatch"))
        if not name.endswith(".csv"):
            if entry.get("rows") is not None:
                errors.append(ArchiveError(name, "JSON row count must be null"))
        else:
            try:
                rows = _csv_data_rows(raw)
            except (UnicodeDecodeError, csv.Error):
                errors.append(ArchiveError(name, "invalid UTF-8 or CSV"))
            else:
                if entry.get("rows") != rows:
                    errors.append(ArchiveError(name, "row count mismatch"))
                data_rows += rows
    if errors:
        return None, tuple(errors)
    if data_rows > MAX_DATA_ROWS:
        return None, (ArchiveError("archive", "too many data rows"),)
    try:
        workspace = _string_keyed_object(json.loads(files["workspace.json"]))
    except (ValueError, RecursionError):
        workspace = None
    if workspace is None:
        return None, (ArchiveError("workspace.json", "invalid JSON object"),)
    if workspace.get("base_currency") != currency:
        return None, (ArchiveError("workspace.json", "base currency differs from manifest"),)
    if (
        not all(
            isinstance(workspace.get(key), str) and workspace[key]
            for key in ("name", "timezone", "locale")
        )
        or type(workspace.get("auto_copy_plan")) is not bool
        or (
            workspace.get("country_code") is not None
            and not re.fullmatch(r"[A-Z]{2}", str(workspace["country_code"]))
        )
        or ("taxes_enabled" in workspace and type(workspace["taxes_enabled"]) is not bool)
        or not _valid_upcoming_days(workspace.get("upcoming_days"))
        or not _valid_cost_basis(workspace.get("investment_cost_basis", "average"))
    ):
        return None, (ArchiveError("workspace.json", "invalid workspace settings"),)
    risk_policy: RiskTargetPolicyRow | None = None
    raw_policy = workspace.get("risk_target_policy")
    if raw_policy is not None:
        try:
            risk_policy = _risk_target_policy(raw_policy)
        except ValueError:
            return None, (ArchiveError("workspace.json", "invalid workspace settings"),)
        if isinstance(raw_policy, dict):
            warnings.extend(
                ArchiveError("workspace.json", f"unknown key: risk_target_policy.{key}")
                for key in cast("dict[object, object]", raw_policy).keys()
                - {"mode", "safe_bp", "growth_bp", "speculative_bp", "tolerance_bp"}
            )
    warnings.extend(
        ArchiveError("workspace.json", f"unknown key: {key}")
        for key in workspace.keys()
        - {
            "name",
            "base_currency",
            "country_code",
            "timezone",
            "locale",
            "auto_copy_plan",
            "taxes_enabled",
            "upcoming_days",
            "investment_cost_basis",
            "risk_target_policy",
        }
    )
    parsed = _read_rows(
        files, workspace, currency, str(version), errors, warnings, risk_policy=risk_policy
    )
    if errors:
        return None, tuple(errors)
    return ParsedArchive(manifest, files, parsed, tuple(warnings)), ()


def _valid_upcoming_days(value: object) -> bool:
    """``upcoming_days`` (1.1) is optional: absent or ``null`` means the instance default."""
    return value is None or (type(value) is int and MIN_UPCOMING_DAYS <= value <= MAX_UPCOMING_DAYS)


def _valid_cost_basis(value: object) -> bool:
    return isinstance(value, str) and value in {"average", "fifo"}


def _risk_target_policy(value: object) -> RiskTargetPolicyRow:
    """Validate the optional 1.4 ``risk_target_policy`` object with the editor's rules."""
    data = _string_keyed_object(value)
    if data is None:
        raise ValueError("invalid risk target policy")
    mode = data.get("mode")
    if mode not in {"fixed", "age_scaled", "age_in_bonds"}:
        raise ValueError("invalid risk target policy")
    values: dict[str, int] = {}
    for key in ("safe_bp", "growth_bp", "speculative_bp", "tolerance_bp"):
        number = data.get(key)
        if type(number) is not int or not 0 <= number <= 10_000:
            raise ValueError("invalid risk target policy")
        values[key] = number
    if (
        mode == "fixed"
        and values["safe_bp"] + values["growth_bp"] + values["speculative_bp"] != 10_000
    ):
        raise ValueError("invalid risk target policy")
    return RiskTargetPolicyRow(
        mode=cast("str", mode),
        safe_bp=values["safe_bp"],
        growth_bp=values["growth_bp"],
        speculative_bp=values["speculative_bp"],
        tolerance_bp=values["tolerance_bp"],
    )


def _parse_timestamp(value: str) -> str:
    if not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})",
        value,
    ):
        raise ValueError("invalid timestamp")
    try:
        result = timestamp_text(datetime.fromisoformat(value))
    except OverflowError:  # an offset that moves year 1 or 9999 out of range
        raise ValueError("invalid timestamp") from None
    assert result is not None
    return result


def _decimal_text(value: str, currency: str, *, allow_zero: bool) -> str:
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        raise ValueError("invalid unsigned decimal")
    whole, _, fractional = value.partition(".")
    digits = minor_unit_for(currency)
    if fractional[digits:].strip("0") or (not allow_zero and not (whole + fractional).strip("0")):
        raise ValueError("invalid precision or zero amount")
    return value


def _plain_decimal(value: str, *, allow_zero: bool) -> str:
    """A positive plain decimal that fits ``DecimalType``; no exponent or locale format."""
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        raise ValueError("invalid decimal")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("invalid decimal") from None
    if not number.is_finite() or number < 0 or (number == 0 and not allow_zero):
        raise ValueError("invalid decimal")
    exponent = number.as_tuple().exponent
    if not isinstance(exponent, int):
        raise ValueError("invalid decimal")
    scale = max(-exponent, 0)
    integer_digits = 0 if number.is_zero() else max(number.adjusted() + 1, 0)
    if scale > 18 or integer_digits > 20 or integer_digits + scale > 38:
        raise ValueError("invalid decimal")
    return value


def _money_minor(value: str, currency: str, *, allow_zero: bool) -> int:
    """A currency amount with at most the currency's minor digits, returned as minor units."""
    text = value or "0"
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ValueError("invalid decimal") from None
    if not number.is_finite() or number < 0:
        raise ValueError("invalid decimal")
    minor = number.scaleb(minor_unit_for(currency))
    if minor != minor.to_integral_value():
        raise ValueError("too many decimals")
    result = int(minor)
    if result == 0 and not allow_zero:
        raise ValueError("amount must be positive")
    return result


def _signed_money_minor(value: str, currency: str) -> int:
    """A signed currency amount with at most the currency's minor digits, in minor units."""
    text = value or "0"
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ValueError("invalid decimal") from None
    if not number.is_finite():
        raise ValueError("invalid decimal")
    minor = number.scaleb(minor_unit_for(currency))
    if minor != minor.to_integral_value():
        raise ValueError("too many decimals")
    result = int(minor)
    if abs(result) > MAX_AMOUNT_MINOR:
        raise ValueError("amount too large")
    return result


def _nonnegative_money_minor(value: str, currency: str) -> int:
    """A non-negative currency amount within the shared amount limit, in minor units."""
    result = _money_minor(value, currency, allow_zero=True)
    if result > MAX_AMOUNT_MINOR:
        raise ValueError("amount too large")
    return result


def _valid_currency(value: str) -> str:
    code = value.upper()
    if not code:
        raise ValueError("required value missing")
    try:
        minor_unit_for(code)
    except ValueError:
        raise ValueError("invalid currency") from None
    return code


def _valid_fx_backfilled(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = json.loads(value)
    except (ValueError, RecursionError):
        raise ValueError("invalid fx history") from None
    if not isinstance(parsed, dict):
        raise ValueError("invalid fx history")
    normalized: dict[str, str] = {}
    for currency, day in cast("dict[object, object]", parsed).items():
        if not isinstance(currency, str) or not isinstance(day, str):
            raise ValueError("invalid fx history")
        try:
            code = _valid_currency(currency)
            date.fromisoformat(day)
        except ValueError:
            raise ValueError("invalid fx history") from None
        normalized[code] = day
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _valid_rate_schedule(value: str) -> str:
    try:
        parsed = json.loads(value)
    except (ValueError, RecursionError):
        raise ValueError("invalid rate schedule") from None
    if not isinstance(parsed, list) or not parsed:
        raise ValueError("invalid rate schedule")
    entries: list[object] = cast("list[object]", parsed)
    periods: list[BondRatePeriod] = []
    for entry in entries:
        row = _string_keyed_object(entry)
        if row is None or set(row) != {"from_date", "annual_rate_bp"}:
            raise ValueError("invalid rate schedule")
        day, rate = row["from_date"], row["annual_rate_bp"]
        if not isinstance(day, str) or type(rate) is not int or not 0 <= rate <= MAX_ANNUAL_RATE_BP:
            raise ValueError("invalid rate schedule")
        try:
            periods.append(BondRatePeriod(date.fromisoformat(day), rate))
        except ValueError:
            raise ValueError("invalid rate schedule") from None
    if any(
        periods[index].from_date <= periods[index - 1].from_date for index in range(1, len(periods))
    ):
        raise ValueError("invalid rate schedule")
    return json.dumps(
        [
            {"from_date": period.from_date.isoformat(), "annual_rate_bp": period.annual_rate_bp}
            for period in periods
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _validate_cell(file: str, column: str, value: str, currency: str) -> str:
    if column in (
        "id",
        "category_id",
        "item_id",
        "link_id",
        "instrument_id",
        "asset_class_id",
        "income_transaction_id",
        "reference_rate_id",
        "loan_id",
        "property_item_id",
        "transaction_id",
        "source_loan_id",
    ):
        return str(UUID(value)) if value else ""
    if column == "kind":
        if file == "loans.csv":
            return LoanKind(value).value if value else ""
        if file == "loan_payments.csv":
            return LoanPaymentKind(value).value if value else ""
        if file == "fire_events.csv":
            return EventKind(value).value
        return Kind(value).value if value else ""
    if column == "direction":
        return Kind(value).value if value else ""
    if column == "side":
        return Side(value).value if value else ""
    if column == "treatment":
        return Treatment(value or "regular").value
    if column == "rate_mode":
        return RateMode(value).value
    if column == "rule":
        return Rule(value).value
    if column == "income_mode":
        return IncomeMode(value).value
    if column == "expenses_mode":
        return ExpensesMode(value).value
    if column == "portfolio_mode":
        return PortfolioMode(value).value
    if column in ("birth_year", "start_year"):
        if not re.fullmatch(r"[0-9]{4}", value):
            raise ValueError("invalid year")
        number = int(value)
        if not MIN_YEAR <= number <= MAX_YEAR:
            raise ValueError("invalid year")
        return str(number)
    if column in ("stop_working_age", "horizon_age"):
        if not re.fullmatch(r"[0-9]{1,3}", value):
            raise ValueError("invalid age")
        number = int(value)
        if not 0 <= number <= MAX_STOP_WORKING_AGE:
            raise ValueError("invalid age")
        if column == "horizon_age" and not 1 <= number <= MAX_HORIZON_AGE:
            raise ValueError("invalid age")
        return str(number)
    if column in ("inflation_bp", "real_return_bp", "income_growth_bp"):
        if not re.fullmatch(r"-?[0-9]{1,5}", value):
            raise ValueError("invalid rate")
        number = int(value)
        if not RATE_HARD_MIN_BP <= number <= RATE_HARD_MAX_BP:
            raise ValueError("invalid rate")
        return str(number)
    if column == "swr_bp":
        if not value:
            return ""
        if not re.fullmatch(r"[0-9]{1,4}", value):
            raise ValueError("invalid swr")
        number = int(value)
        if not SWR_HARD_MIN_BP <= number <= SWR_HARD_MAX_BP:
            raise ValueError("invalid swr")
        return str(number)
    if column == "day_count":
        return DayCount(value).value
    if column == "overpayment_mode":
        return OverpaymentMode(value).value if value else ""
    if column == "overpayment_strategy":
        return OverpaymentStrategy(value).value if value else ""
    if column == "valuation_mode":
        if value not in {"", "manual", "linked"}:
            raise ValueError("invalid valuation mode")
        return value or "manual"
    if column == "link_type":
        if value and value not in LINK_TYPES:
            raise ValueError("invalid link type")
        return value
    if column in (
        "include_in_net_worth",
        "investable",
        "enabled",
        "auto_record_payments",
    ):
        if column == "investable":
            default = "true" if file == "asset_classes.csv" else "false"
        elif column == "include_in_net_worth":
            default = "true"
        else:
            default = "false"
        chosen = value or default
        if chosen not in {"true", "false"}:
            raise ValueError("invalid boolean")
        return chosen
    if column == "is_default":
        chosen = value or "false"
        if chosen not in {"true", "false"}:
            raise ValueError("invalid boolean")
        return chosen
    if column == "estimate_between":
        if value not in {"", "true", "false"}:
            raise ValueError("invalid boolean")
        return value
    if column in (
        "archived_at",
        "deleted_at",
        "closed_at",
        "plan_started_at",
        "created_at",
        "provider_backfilled_at",
    ):
        return _parse_timestamp(value) if value else ""
    if column in (
        "date",
        "month",
        "as_of",
        "acquired_on",
        "maturity_date",
        "origination_date",
        "first_payment_date",
        "effective_from",
        "effective_to",
        "rate_shift_from",
        "one_off_date",
        "end_date",
    ):
        optional_dates = {
            "acquired_on",
            "origination_date",
            "effective_to",
            "rate_shift_from",
            "one_off_date",
        }
        if not value and (column in optional_dates or file == "investment_transactions.csv"):
            return ""
        pattern = r"[0-9]{4}-[0-9]{2}" + ("" if column == "month" else r"-[0-9]{2}")
        if not re.fullmatch(pattern, value):
            raise ValueError("invalid date or month")
        try:
            date.fromisoformat(value + "-01" if column == "month" else value)
        except ValueError:
            raise ValueError("invalid date or month") from None
    if column == "year":
        if not re.fullmatch(r"[0-9]{4}", value):
            raise ValueError("invalid year")
        number = int(value)
        if not MIN_YEAR <= number <= MAX_YEAR:
            raise ValueError("invalid year")
        return str(number)
    if column in (
        "income_manual",
        "income_fallback",
        "expenses_manual",
        "expenses_fallback",
        "portfolio_manual",
        "portfolio_fallback",
    ):
        if not value:
            return ""
        return _plain_decimal(value, allow_zero=True)
    if file in {"fire_contributions.csv", "fire_events.csv"} and column in {
        "net_amount",
        "amount",
    }:
        if not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value):
            raise ValueError("invalid signed decimal")
        try:
            number = Decimal(value)
        except InvalidOperation:
            raise ValueError("invalid signed decimal") from None
        if not number.is_finite() or abs(number) > MAX_DECIMAL_MAJOR:
            raise ValueError("invalid signed decimal")
        exponent = number.as_tuple().exponent
        if not isinstance(exponent, int) or max(-exponent, 0) > 18:
            raise ValueError("invalid signed decimal")
        return value
    if column in ("amount", "planned", "value", "assets", "liabilities"):
        if column == "planned" and not value:
            return "0"
        return _decimal_text(
            value,
            currency,
            allow_zero=column in {"planned", "value", "assets", "liabilities"},
        )
    if column in (
        "principal_original",
        "installment_override",
        "overpayment_amount",
        "fixed_amount",
        "one_off_amount",
    ):
        return _decimal_text(value, currency, allow_zero=False) if value else ""
    if column in ("total", "installment"):
        return _decimal_text(value, currency, allow_zero=False)
    if column in ("principal", "interest", "costs", "overpayment", "remaining_principal"):
        return _decimal_text(value, currency, allow_zero=True)
    if column in ("value_bp", "rate_bp", "margin_bp"):
        if not value and column in {"rate_bp", "margin_bp"}:
            return ""
        if not re.fullmatch(r"[0-9]{1,5}", value):
            raise ValueError("invalid rate")
        number = int(value)
        if not 0 <= number <= 10_000:
            raise ValueError("invalid rate")
        return str(number)
    if column == "rate_shift_bp":
        if not value:
            return ""
        if not re.fullmatch(r"-?[0-9]{1,5}", value):
            raise ValueError("invalid rate shift")
        number = int(value)
        if number == 0 or not -10_000 <= number <= 10_000:
            raise ValueError("invalid rate shift")
        return str(number)
    if column == "payment_day":
        if not re.fullmatch(r"[0-9]{1,2}", value) or not 1 <= int(value) <= 31:
            raise ValueError("invalid payment day")
        return str(int(value))
    if column == "term_months":
        if not re.fullmatch(r"[0-9]{1,3}", value) or not 0 <= int(value) <= MAX_TERM_MONTHS:
            raise ValueError("invalid term")
        return str(int(value))
    if column == "remaining_count":
        if not re.fullmatch(r"[0-9]{1,3}", value) or not 0 <= int(value) <= MAX_TERM_MONTHS:
            raise ValueError("invalid remaining count")
        return str(int(value))
    if column == "basis":
        if value != "opening_balance":
            raise ValueError("invalid cost basis")
        return value
    if column == "drawdown_mode":
        chosen = value or "full_at_origination"
        if chosen not in {"full_at_origination", "tranches"}:
            raise ValueError("invalid drawdown mode")
        return chosen
    if column == "interest_only_months":
        chosen = value or "0"
        if not re.fullmatch(r"[0-9]{1,3}", chosen) or not 0 <= int(chosen) <= MAX_TERM_MONTHS:
            raise ValueError("invalid interest-only period")
        return str(int(chosen))
    if column == "mode":
        if file == "loan_rate_changes.csv":
            return RateMode(value or "fixed").value
        if value not in {"percent", "fixed"}:
            raise ValueError("invalid cost mode")
        return value
    if column == "currency":
        if file in {
            "networth_items.csv",
            "item_valuations.csv",
            "networth_snapshots.csv",
            "fire_scenarios.csv",
        }:
            return _valid_currency(value)
        if value and value != currency:
            raise ValueError("currency differs from workspace")
        return currency
    if column in ("quote_currency", "settlement_currency", "base_currency"):
        if column == "settlement_currency" and not value:
            return ""
        return _valid_currency(value)
    if column in ("quantity", "unit_price", "price", "settlement_unit_price"):
        if column == "settlement_unit_price" and not value:
            return ""
        return _plain_decimal(value, allow_zero=True)
    if column in ("rate", "fx_to_base"):
        if column == "fx_to_base" and not value:
            return ""
        return _plain_decimal(value, allow_zero=False)
    if column == "fee":
        return _plain_decimal(value or "0", allow_zero=True)
    if column == "settlement_fee":
        return _plain_decimal(value, allow_zero=True) if value else ""
    if column == "early_redemption_fee":
        return _plain_decimal(value, allow_zero=True) if value else ""
    if column == "face_value":
        return _plain_decimal(value, allow_zero=False)
    if column in ("risk_tier", "risk_tier_override"):
        if value and value not in {tier.value for tier in RiskTier}:
            raise ValueError("invalid risk tier")
        return value
    if column == "type":
        if file == "instruments.csv":
            if value not in {kind.value for kind in InstrumentType}:
                raise ValueError("invalid instrument type")
            return value
        if value not in {kind.value for kind in InvestmentTransactionType}:
            raise ValueError("invalid transaction type")
        return value
    if column == "valuation_source":
        if value not in {source.value for source in ValuationSource}:
            raise ValueError("invalid valuation source")
        return value
    if column == "interest_mode":
        if value not in {mode.value for mode in InterestMode}:
            raise ValueError("invalid interest mode")
        return value
    if column == "early_redemption_fee_mode":
        if value not in {"", "percent", "fixed"}:
            raise ValueError("invalid early redemption fee")
        return value
    if column in ("weight_bp", "tax_rate_bp", "daily_call_budget"):
        if not re.fullmatch(r"[0-9]{1,6}", value):
            raise ValueError("invalid whole number")
        number = int(value)
        if column == "weight_bp" and not 1 <= number <= 10_000:
            raise ValueError("invalid allocation weight")
        if column == "tax_rate_bp" and not 0 <= number <= 10_000:
            raise ValueError("invalid tax rate")
        if column == "daily_call_budget" and not 1 <= number <= 100_000:
            raise ValueError("invalid daily call budget")
        return str(number)
    if column == "early_redemption_fee_rate_bp":
        if value and not re.fullmatch(r"[0-9]{1,5}", value):
            raise ValueError("invalid early redemption fee")
        if value and not 0 <= int(value) <= 10_000:
            raise ValueError("invalid early redemption fee")
        return value
    if column == "fx_backfilled":
        return _valid_fx_backfilled(value)
    if column == "rate_schedule":
        return _valid_rate_schedule(value)
    if column == "category":
        if not 1 <= len(value) <= 80:
            raise ValueError("invalid category name")
    elif column == "scenario":
        try:
            clean_scenario_name(value)
        except ValueError:
            raise ValueError("invalid scenario name") from None
    elif column == "label":
        try:
            return clean_event_label(value)
        except ValueError:
            raise ValueError("invalid event label") from None
    elif column in ("reference_rate", "property_item"):
        if not value:
            return ""
        maximum = MAX_LOAN_NAME_LENGTH if column == "reference_rate" else MAX_ITEM_NAME_LENGTH
        if len(value) > maximum:
            raise ValueError("invalid name")
    elif column in ("name", "instrument", "asset_class", "loan"):
        if column == "instrument":
            maximum = MAX_INSTRUMENT_NAME_LENGTH
        elif column == "asset_class":
            maximum = MAX_ASSET_CLASS_NAME_LENGTH
        elif column == "loan":
            maximum = MAX_LOAN_NAME_LENGTH
        elif file == "fire_scenarios.csv":
            try:
                return clean_scenario_name(value)
            except ValueError:
                raise ValueError("invalid scenario name") from None
        elif file == "networth_items.csv":
            maximum = MAX_ITEM_NAME_LENGTH
        elif file == "instruments.csv":
            maximum = MAX_INSTRUMENT_NAME_LENGTH
        else:
            maximum = MAX_ASSET_CLASS_NAME_LENGTH
        if not 1 <= len(value) <= maximum:
            raise ValueError("invalid name")
    elif column == "item":
        if not 1 <= len(value) <= MAX_ITEM_NAME_LENGTH:
            raise ValueError("invalid item name")
    elif column in (
        "description",
        "notes",
        "note",
        "provider_symbol",
        "symbol",
        "unit_label",
        "provider_key",
        "series_code",
    ):
        limits = {
            "description": 200,
            "notes": MAX_NOTES_LENGTH,
            "note": MAX_NOTE_LENGTH,
            "provider_symbol": 64,
            "symbol": MAX_SYMBOL_LENGTH,
            "unit_label": MAX_UNIT_LABEL_LENGTH,
            "provider_key": 32,
            "series_code": MAX_SERIES_CODE_LENGTH,
        }
        limit = limits[column]
        if column == "unit_label" and not value:
            raise ValueError("required value missing")
        if len(value) > limit:
            raise ValueError("text too long")
    elif column == "color":
        if value and not re.fullmatch(r"#[0-9a-f]{6}", value):
            raise ValueError("invalid color")
        return value or "#16a34a"
    elif column == "sort_order":
        if not re.fullmatch(r"[0-9]{1,9}", value):
            raise ValueError("invalid sort order")
    elif column == "source":
        if file == "price_points.csv":
            if value not in {"manual", "provider"}:
                raise ValueError("invalid price source")
        elif file == "fx_rates.csv":
            if not value or len(value) > 32:
                raise ValueError("invalid FX source")
        elif file == "loan_payments.csv":
            if value not in {source.value for source in LoanPaymentSource}:
                raise ValueError("invalid payment source")
        elif value not in SNAPSHOT_SOURCES:
            raise ValueError("invalid snapshot source")
    elif column == "breakdown":
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            raise ValueError("invalid breakdown JSON") from None
        if not isinstance(parsed, dict):
            raise ValueError("invalid breakdown JSON")
    return value


_REQUIRED_COLUMNS = {
    "categories.csv": {"id", "kind", "name"},
    "transactions.csv": {"date", "direction", "amount", "category", "category_id"},
    "budget_lines.csv": {"month", "category", "planned", "category_id"},
    "months.csv": {"month"},
    "networth_categories.csv": {"id", "side", "name"},
    "networth_items.csv": {"id", "side", "category", "category_id", "name", "currency"},
    "item_valuations.csv": {"id", "item_id", "item", "as_of", "value", "currency"},
    "networth_snapshots.csv": {
        "id",
        "as_of",
        "assets",
        "liabilities",
        "currency",
        "breakdown",
        "source",
    },
    "asset_classes.csv": {"id", "name"},
    "instruments.csv": {
        "id",
        "name",
        "type",
        "quote_currency",
        "unit_label",
        "valuation_source",
    },
    "instrument_allocations.csv": {
        "id",
        "instrument_id",
        "instrument",
        "asset_class_id",
        "asset_class",
        "weight_bp",
    },
    "investment_transactions.csv": {
        "id",
        "instrument_id",
        "instrument",
        "date",
        "type",
        "quantity",
        "unit_price",
        "quote_currency",
    },
    "bond_terms.csv": {
        "id",
        "instrument_id",
        "instrument",
        "face_value",
        "maturity_date",
        "interest_mode",
        "rate_schedule",
        "tax_rate_bp",
    },
    "price_points.csv": {"id", "instrument_id", "instrument", "date", "price", "source"},
    "fx_rates.csv": {
        "id",
        "base_currency",
        "quote_currency",
        "date",
        "rate",
        "source",
    },
    "price_source_configs.csv": {
        "id",
        "provider_key",
        "enabled",
        "daily_call_budget",
    },
    "reference_rates.csv": {"id", "name"},
    "reference_rate_values.csv": {
        "id",
        "reference_rate_id",
        "reference_rate",
        "effective_from",
        "value_bp",
    },
    "loans.csv": {
        "id",
        "name",
        "kind",
        "currency",
        "first_payment_date",
        "term_months",
        "payment_day",
        "rate_mode",
        "day_count",
        "overpayment_mode",
        "overpayment_strategy",
    },
    "loan_rate_changes.csv": {"id", "loan_id", "loan", "effective_from"},
    "loan_transfers.csv": {"id", "loan_id", "loan", "date", "amount"},
    "loan_cost_components.csv": {
        "id",
        "loan_id",
        "loan",
        "name",
        "mode",
        "basis",
        "effective_from",
    },
    "loan_overpayments.csv": {"id", "loan_id", "loan", "date", "amount"},
    "loan_payments.csv": {
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
        "source",
    },
    "loan_balance_checkpoints.csv": {
        "id",
        "loan_id",
        "loan",
        "as_of",
        "remaining_principal",
    },
    "loan_scenarios.csv": {"id", "loan_id", "loan", "name"},
    "installment_plans.csv": {
        "id",
        "loan_id",
        "loan",
        "category_id",
        "category",
        "installment",
        "remaining_count",
        "end_date",
    },
    "fire_scenarios.csv": {
        "id",
        "name",
        "currency",
        "birth_year",
        "start_year",
        "inflation_bp",
        "real_return_bp",
        "income_growth_bp",
        "stop_working_age",
        "horizon_age",
        "rule",
        "income_mode",
        "expenses_mode",
        "portfolio_mode",
    },
    "fire_contributions.csv": {"id", "scenario_id", "scenario", "year", "net_amount"},
    "fire_events.csv": {"id", "scenario_id", "scenario", "year", "kind", "amount", "label"},
    "fire_expense_exclusions.csv": {
        "id",
        "scenario_id",
        "scenario",
        "category_id",
        "category",
    },
}


def required_columns(version: str) -> dict[str, set[str]]:
    """CSV columns required by a compatible ``MAJOR.MINOR`` version."""
    columns = {name: set(names) for name, names in _REQUIRED_COLUMNS.items()}
    major, minor = (int(part) for part in version.split(".", 1))
    if major == 1 and minor >= 3:
        columns["networth_items.csv"].add("estimate_between")
    if major == 1 and minor >= 6:
        columns["loan_rate_changes.csv"].add("mode")
    if major == 1 and minor >= 7:
        columns["loans.csv"].update({"drawdown_mode", "interest_only_months"})
    return columns


def _read_rows(
    files: dict[str, bytes],
    workspace: dict[str, object],
    currency: str,
    version: str,
    errors: list[ArchiveError],
    warnings: list[ArchiveError],
    *,
    risk_policy: RiskTargetPolicyRow | None = None,
) -> ArchiveRows:
    required = required_columns(version)
    tables: dict[str, list[dict[str, str]]] = {}
    for name, columns in CSV_COLUMNS.items():
        rows: list[dict[str, str]] = []
        tables[name] = rows
        if name not in files:
            continue
        try:
            reader = csv.reader(
                io.StringIO(files[name].decode("utf-8-sig"), newline=""), strict=True
            )
            header = next(reader, list[str]())
            if (
                len(header) > MAX_COLUMNS
                or len(header) != len(set(header))
                or not required[name] <= set(header)
            ):
                errors.append(ArchiveError(name, "invalid or missing header", 1))
                continue
            warnings.extend(
                ArchiveError(name, f"unknown column: {key}", 1, key)
                for key in header
                if key not in columns
            )
            for index, cells in enumerate(reader):
                if len(errors) >= MAX_ERRORS:
                    break
                line = reader.line_num
                if len(cells) != len(header):
                    errors.append(ArchiveError(name, "wrong number of cells", line))
                    continue
                raw = dict(zip(header, cells, strict=True))
                row: dict[str, str] = {}
                for column in columns:
                    value = raw.get(column, "")
                    if column == "sort_order" and not value:
                        value = str(index)
                    try:
                        if (
                            column in required[name]
                            and not value
                            and not (name == "investment_transactions.csv" and column == "date")
                        ):
                            raise ValueError("required value missing")
                        row[column] = _validate_cell(name, column, value, currency)
                    except ValueError as exc:
                        errors.append(ArchiveError(name, str(exc), line, column))
                if len(row) == len(columns):
                    if name == "loan_rate_changes.csv":
                        mode = row["mode"] or "fixed"
                        if mode == "fixed" and not row["rate_bp"]:
                            errors.append(
                                ArchiveError(name, "a fixed rate change needs a rate", line)
                            )
                            continue
                        if mode == "variable" and (
                            not row["reference_rate_id"] or not row["margin_bp"]
                        ):
                            errors.append(
                                ArchiveError(
                                    name,
                                    "a variable rate change needs a reference rate and margin",
                                    line,
                                )
                            )
                            continue
                    rows.append(row)
        except (UnicodeDecodeError, csv.Error) as exc:
            errors.append(ArchiveError(name, str(exc)))
    if len(errors) >= MAX_ERRORS:
        # The rows are incomplete, so cross-file checks would only add noise.
        errors.append(ArchiveError("archive", "too many errors; later rows were not checked"))
        return ArchiveRows(workspace, (), (), (), ())

    months = tables["months.csv"]
    if len({row["month"] for row in months}) != len(months):
        errors.append(ArchiveError("months.csv", "duplicate month"))

    categories = tables["categories.csv"]
    by_id = {row["id"]: row for row in categories}
    if len(by_id) != len(categories):
        errors.append(ArchiveError("categories.csv", "duplicate category id"))
    for name in ("transactions.csv", "budget_lines.csv"):
        seen: set[object] = set()
        for row in tables[name]:
            category = by_id.get(row["category_id"])
            kind = row.get("direction", row.get("kind", ""))
            if (
                category is None
                or (kind and kind != category["kind"])
                or row["category"] != category["name"]
            ):
                errors.append(ArchiveError(name, "category reference mismatch"))
            key = (row["month"], row["category_id"]) if name == "budget_lines.csv" else row["id"]
            if key and key in seen:
                errors.append(ArchiveError(name, "duplicate row identity"))
            seen.add(key)

    networth_categories = tables["networth_categories.csv"]
    networth_by_id = {row["id"]: row for row in networth_categories}
    if len(networth_by_id) != len(networth_categories):
        errors.append(ArchiveError("networth_categories.csv", "duplicate net-worth category id"))
    active_networth_names: set[tuple[str, str]] = set()
    for row in networth_categories:
        if not row["archived_at"]:
            key = (row["side"], name_key(row["name"]))
            if key in active_networth_names:
                errors.append(
                    ArchiveError(
                        "networth_categories.csv", "duplicate active net-worth category name"
                    )
                )
            active_networth_names.add(key)

    networth_items = tables["networth_items.csv"]
    networth_items_by_id = {row["id"]: row for row in networth_items}
    if len(networth_items_by_id) != len(networth_items):
        errors.append(ArchiveError("networth_items.csv", "duplicate net-worth item id"))
    active_links: set[tuple[str, str]] = set()
    for row in networth_items:
        category = networth_by_id.get(row["category_id"])
        if (
            category is None
            or row["side"] != category["side"]
            or row["category"] != category["name"]
        ):
            errors.append(
                ArchiveError("networth_items.csv", "net-worth category reference mismatch")
            )
        linked = row["valuation_mode"] == "linked"
        if linked != bool(row["link_type"] and row["link_id"]):
            errors.append(ArchiveError("networth_items.csv", "invalid link"))
        elif linked and not row["archived_at"]:
            key = (row["link_type"], row["link_id"])
            if key in active_links:
                errors.append(ArchiveError("networth_items.csv", "duplicate active link"))
            active_links.add(key)

    item_valuations = tables["item_valuations.csv"]
    seen_valuations: set[str] = set()
    for row in item_valuations:
        item = networth_items_by_id.get(row["item_id"])
        if item is None or row["item"] != item["name"]:
            errors.append(ArchiveError("item_valuations.csv", "item valuation reference mismatch"))
        elif row["currency"] != item["currency"]:
            errors.append(ArchiveError("item_valuations.csv", "item valuation currency differs"))
        elif item["valuation_mode"] == "linked":
            errors.append(ArchiveError("item_valuations.csv", "linked item has valuations"))
        if row["id"] in seen_valuations:
            errors.append(ArchiveError("item_valuations.csv", "duplicate item valuation id"))
        seen_valuations.add(row["id"])

    networth_snapshots = tables["networth_snapshots.csv"]
    seen_snapshots: set[tuple[str, str]] = set()
    seen_snapshot_ids: set[str] = set()
    for row in networth_snapshots:
        key = (row["as_of"], row["source"])
        if key in seen_snapshots:
            errors.append(
                ArchiveError("networth_snapshots.csv", "duplicate snapshot date and source")
            )
        seen_snapshots.add(key)
        if row["id"] in seen_snapshot_ids:
            errors.append(ArchiveError("networth_snapshots.csv", "duplicate snapshot id"))
        seen_snapshot_ids.add(row["id"])

    asset_classes = tables["asset_classes.csv"]
    asset_classes_by_id = {row["id"]: row for row in asset_classes}
    if len(asset_classes_by_id) != len(asset_classes):
        errors.append(ArchiveError("asset_classes.csv", "duplicate asset class id"))
    active_asset_names: set[str] = set()
    for row in asset_classes:
        if not row["archived_at"]:
            key = name_key(row["name"])
            if key in active_asset_names:
                errors.append(
                    ArchiveError("asset_classes.csv", "duplicate active asset class name")
                )
            active_asset_names.add(key)

    instruments = tables["instruments.csv"]
    instruments_by_id = {row["id"]: row for row in instruments}
    if len(instruments_by_id) != len(instruments):
        errors.append(ArchiveError("instruments.csv", "duplicate instrument id"))
    active_instrument_names: set[str] = set()
    for row in instruments:
        if row["type"] == "currency" and row["unit_label"] != row["quote_currency"]:
            errors.append(ArchiveError("instruments.csv", "currency unit label mismatch"))
        if not row["archived_at"]:
            key = name_key(row["name"])
            if key in active_instrument_names:
                errors.append(ArchiveError("instruments.csv", "duplicate active instrument name"))
            active_instrument_names.add(key)

    allocations = tables["instrument_allocations.csv"]
    allocation_ids: set[str] = set()
    allocation_pairs: set[tuple[str, str]] = set()
    weights: dict[str, int] = {}
    for row in allocations:
        if row["id"] in allocation_ids:
            errors.append(ArchiveError("instrument_allocations.csv", "duplicate allocation id"))
        allocation_ids.add(row["id"])
        instrument = instruments_by_id.get(row["instrument_id"])
        asset_class = asset_classes_by_id.get(row["asset_class_id"])
        if (
            instrument is None
            or asset_class is None
            or row["instrument"] != instrument["name"]
            or row["asset_class"] != asset_class["name"]
        ):
            errors.append(
                ArchiveError("instrument_allocations.csv", "allocation reference mismatch")
            )
        pair = (row["instrument_id"], row["asset_class_id"])
        if pair in allocation_pairs:
            errors.append(ArchiveError("instrument_allocations.csv", "duplicate allocation row"))
        allocation_pairs.add(pair)
        weights[row["instrument_id"]] = weights.get(row["instrument_id"], 0) + int(row["weight_bp"])
    for instrument in instruments:
        if weights.get(instrument["id"]) != 10_000:
            errors.append(
                ArchiveError("instrument_allocations.csv", "allocation weights must total 100%")
            )

    transactions = tables["investment_transactions.csv"]
    transaction_ids: set[str] = set()
    for row in transactions:
        if row["id"] in transaction_ids:
            errors.append(
                ArchiveError("investment_transactions.csv", "duplicate investment transaction id")
            )
        transaction_ids.add(row["id"])
        instrument = instruments_by_id.get(row["instrument_id"])
        if (
            instrument is None
            or row["instrument"] != instrument["name"]
            or row["quote_currency"] != instrument["quote_currency"]
        ):
            errors.append(
                ArchiveError("investment_transactions.csv", "instrument reference mismatch")
            )
        try:
            _money_minor(row["fee"], row["quote_currency"], allow_zero=True)
        except ValueError as exc:
            errors.append(ArchiveError("investment_transactions.csv", str(exc), column="fee"))
        settlement = (
            row["settlement_currency"],
            row["settlement_unit_price"],
            row["settlement_fee"],
        )
        if any(settlement) and not all(settlement):
            errors.append(
                ArchiveError("investment_transactions.csv", "settlement fields must be all or none")
            )
        elif row["settlement_currency"]:
            if row["settlement_currency"] != currency:
                errors.append(
                    ArchiveError(
                        "investment_transactions.csv",
                        "settlement currency differs from workspace",
                    )
                )
            if instrument is not None and (
                instrument["quote_currency"] == currency or instrument["type"] == "currency"
            ):
                errors.append(
                    ArchiveError("investment_transactions.csv", "settlement is not allowed here")
                )
            try:
                _money_minor(row["settlement_fee"], row["settlement_currency"], allow_zero=True)
            except ValueError as exc:
                errors.append(
                    ArchiveError("investment_transactions.csv", str(exc), column="settlement_fee")
                )
        if row["type"] == "dividend":
            if Decimal(row["quantity"]) != 1:
                errors.append(
                    ArchiveError(
                        "investment_transactions.csv",
                        "invalid dividend encoding",
                        column="quantity",
                    )
                )
            try:
                gross_minor = _money_minor(
                    row["unit_price"], row["quote_currency"], allow_zero=False
                )
                fee_minor = _money_minor(row["fee"], row["quote_currency"], allow_zero=True)
                if fee_minor > gross_minor:
                    raise ValueError("withholding exceeds the gross amount")
            except ValueError as exc:
                errors.append(
                    ArchiveError("investment_transactions.csv", str(exc), column="unit_price")
                )
            if (
                not row["fx_to_base"]
                or Decimal(row["fx_to_base"]) <= 0
                or row["quote_currency"] == currency
                and row["fx_to_base"] != "1"
            ):
                errors.append(
                    ArchiveError(
                        "investment_transactions.csv", "invalid dividend FX", column="fx_to_base"
                    )
                )
            if any(settlement):
                errors.append(
                    ArchiveError("investment_transactions.csv", "dividend has settlement fields")
                )
        elif row["type"] == "split":
            if instrument is not None and instrument["type"] in {"currency", "bond_model"}:
                errors.append(
                    ArchiveError(
                        "investment_transactions.csv", "split is not allowed for this instrument"
                    )
                )
            if (
                row["quantity"] == row["unit_price"]
                or Decimal(row["quantity"]) <= 0
                or Decimal(row["unit_price"]) <= 0
            ):
                errors.append(ArchiveError("investment_transactions.csv", "invalid split encoding"))
            if row["fee"] not in {"", "0", "0.00", "0.0"}:
                errors.append(ArchiveError("investment_transactions.csv", "split fee must be zero"))
            if row["fx_to_base"]:
                errors.append(ArchiveError("investment_transactions.csv", "split must not have FX"))
            if any(settlement):
                errors.append(
                    ArchiveError("investment_transactions.csv", "split has settlement fields")
                )
        elif row["income_transaction_id"]:
            errors.append(
                ArchiveError("investment_transactions.csv", "income link is only for dividends")
            )

    bonds = tables["bond_terms.csv"]
    bond_ids: set[str] = set()
    bond_instruments: set[str] = set()
    for row in bonds:
        if row["id"] in bond_ids:
            errors.append(ArchiveError("bond_terms.csv", "duplicate bond terms id"))
        bond_ids.add(row["id"])
        instrument = instruments_by_id.get(row["instrument_id"])
        if (
            instrument is None
            or row["instrument"] != instrument["name"]
            or instrument["type"] != "bond_model"
        ):
            errors.append(ArchiveError("bond_terms.csv", "bond terms reference mismatch"))
        if row["instrument_id"] in bond_instruments:
            errors.append(ArchiveError("bond_terms.csv", "duplicate bond terms instrument"))
        bond_instruments.add(row["instrument_id"])
        try:
            _money_minor(
                row["face_value"],
                instrument["quote_currency"] if instrument else currency,
                allow_zero=False,
            )
        except ValueError as exc:
            errors.append(ArchiveError("bond_terms.csv", str(exc), column="face_value"))
        mode = row["early_redemption_fee_mode"]
        rate_set = bool(row["early_redemption_fee_rate_bp"])
        fee_set = bool(row["early_redemption_fee"])
        if (
            (mode == "" and (rate_set or fee_set))
            or (mode == "percent" and (not rate_set or fee_set))
            or (mode == "fixed" and (rate_set or not fee_set))
        ):
            errors.append(ArchiveError("bond_terms.csv", "invalid early redemption fee"))
        if mode == "fixed" and instrument is not None:
            try:
                _money_minor(
                    row["early_redemption_fee"], instrument["quote_currency"], allow_zero=True
                )
            except ValueError as exc:
                errors.append(
                    ArchiveError("bond_terms.csv", str(exc), column="early_redemption_fee")
                )
        try:
            periods = [
                BondRatePeriod(
                    date.fromisoformat(str(item["from_date"])),
                    int(str(item["annual_rate_bp"])),
                )
                for item in cast("list[dict[str, object]]", json.loads(row["rate_schedule"]))
            ]
            validate_rate_schedule(periods, date.fromisoformat(row["maturity_date"]))
        except (ValueError, KeyError, TypeError):
            errors.append(ArchiveError("bond_terms.csv", "invalid rate schedule"))

    prices = tables["price_points.csv"]
    price_ids: set[str] = set()
    price_keys: set[tuple[str, str, str]] = set()
    for row in prices:
        if row["id"] in price_ids:
            errors.append(ArchiveError("price_points.csv", "duplicate price point id"))
        price_ids.add(row["id"])
        instrument = instruments_by_id.get(row["instrument_id"])
        if instrument is None or row["instrument"] != instrument["name"]:
            errors.append(ArchiveError("price_points.csv", "price point reference mismatch"))
        key = (row["instrument_id"], row["date"], row["source"])
        if key in price_keys:
            errors.append(ArchiveError("price_points.csv", "duplicate instrument, date and source"))
        price_keys.add(key)

    fx_rates = tables["fx_rates.csv"]
    fx_ids: set[str] = set()
    fx_keys: set[tuple[str, str, str, str]] = set()
    for row in fx_rates:
        if row["id"] in fx_ids:
            errors.append(ArchiveError("fx_rates.csv", "duplicate FX rate id"))
        fx_ids.add(row["id"])
        if row["base_currency"] != currency:
            errors.append(ArchiveError("fx_rates.csv", "FX base currency differs from workspace"))
        key = (row["base_currency"], row["quote_currency"], row["date"], row["source"])
        if key in fx_keys:
            errors.append(ArchiveError("fx_rates.csv", "duplicate FX pair, date and source"))
        fx_keys.add(key)

    configs = tables["price_source_configs.csv"]
    config_ids: set[str] = set()
    config_providers: set[str] = set()
    for row in configs:
        if row["id"] in config_ids:
            errors.append(ArchiveError("price_source_configs.csv", "duplicate provider config id"))
        config_ids.add(row["id"])
        if row["provider_key"] in config_providers:
            errors.append(ArchiveError("price_source_configs.csv", "duplicate provider config"))
        config_providers.add(row["provider_key"])

    reference_rates = tables["reference_rates.csv"]
    reference_rates_by_id: dict[str, dict[str, str]] = {}
    reference_rate_keys: set[str] = set()
    for row in reference_rates:
        if row["id"] in reference_rates_by_id:
            errors.append(ArchiveError("reference_rates.csv", "duplicate reference rate id"))
        reference_rates_by_id[row["id"]] = row
        key = name_key(row["name"])
        if key in reference_rate_keys:
            errors.append(ArchiveError("reference_rates.csv", "duplicate reference rate name"))
        reference_rate_keys.add(key)

    reference_values = tables["reference_rate_values.csv"]
    reference_value_ids: set[str] = set()
    reference_value_keys: set[tuple[str, str]] = set()
    for row in reference_values:
        if row["id"] in reference_value_ids:
            errors.append(
                ArchiveError("reference_rate_values.csv", "duplicate reference rate value id")
            )
        reference_value_ids.add(row["id"])
        rate = reference_rates_by_id.get(row["reference_rate_id"])
        if rate is None or row["reference_rate"] != rate["name"]:
            errors.append(
                ArchiveError("reference_rate_values.csv", "reference rate value reference mismatch")
            )
        key = (row["reference_rate_id"], row["effective_from"])
        if key in reference_value_keys:
            errors.append(
                ArchiveError("reference_rate_values.csv", "duplicate reference rate value")
            )
        reference_value_keys.add(key)

    loans = tables["loans.csv"]
    loans_by_id: dict[str, dict[str, str]] = {}
    ledger_transaction_ids = {row["id"] for row in tables["transactions.csv"]}
    for row in loans:
        if row["id"] in loans_by_id:
            errors.append(ArchiveError("loans.csv", "duplicate loan id"))
        loans_by_id[row["id"]] = row
        term = int(row["term_months"])
        if (row["kind"] == "installment_plan" and term < 0) or (
            row["kind"] != "installment_plan" and term < 1
        ):
            errors.append(ArchiveError("loans.csv", "invalid loan term", column="term_months"))
        if row["origination_date"] and row["first_payment_date"] <= row["origination_date"]:
            errors.append(
                ArchiveError(
                    "loans.csv",
                    "first payment is not after origination",
                    column="first_payment_date",
                )
            )
        drawdown_mode = row.get("drawdown_mode", "full_at_origination")
        interest_only = int(row.get("interest_only_months", "0") or 0)
        if drawdown_mode not in {"full_at_origination", "tranches"}:
            errors.append(ArchiveError("loans.csv", "invalid drawdown mode"))
        if drawdown_mode == "tranches" and not row["principal_original"]:
            errors.append(
                ArchiveError(
                    "loans.csv",
                    "tranched loan needs a principal",
                    column="principal_original",
                )
            )
        if term > 0 and interest_only >= term:
            errors.append(
                ArchiveError(
                    "loans.csv",
                    "invalid interest-only period",
                    column="interest_only_months",
                )
            )
        if row["kind"] == "installment_plan" and interest_only != 0:
            errors.append(
                ArchiveError(
                    "loans.csv",
                    "invalid interest-only period",
                    column="interest_only_months",
                )
            )
        if row["rate_mode"] == "fixed":
            if row["reference_rate_id"] or row["margin_bp"]:
                errors.append(ArchiveError("loans.csv", "invalid loan rate configuration"))
        else:
            rate = reference_rates_by_id.get(row["reference_rate_id"])
            if rate is None or row["reference_rate"] != rate["name"] or not row["margin_bp"]:
                errors.append(ArchiveError("loans.csv", "invalid loan rate configuration"))
        if row["reference_rate_id"] and not row["reference_rate"]:
            errors.append(
                ArchiveError(
                    "loans.csv", "invalid loan rate configuration", column="reference_rate_id"
                )
            )
        if row["property_item_id"]:
            item = networth_items_by_id.get(row["property_item_id"])
            if item is None or row["property_item"] != item["name"]:
                errors.append(
                    ArchiveError(
                        "loans.csv", "loan property item mismatch", column="property_item_id"
                    )
                )
        elif row["property_item"]:
            errors.append(
                ArchiveError("loans.csv", "loan property item mismatch", column="property_item_id")
            )
        if row["kind"] != "annuity" and row["installment_override"]:
            errors.append(ArchiveError("loans.csv", "installment override is annuity only"))
        if (row["overpayment_mode"] == "none" and row["overpayment_amount"]) or (
            row["overpayment_mode"] != "none" and not row["overpayment_amount"]
        ):
            errors.append(ArchiveError("loans.csv", "invalid loan overpayment"))
        if (
            row["overpayment_mode"] == "target_total"
            and row["installment_override"]
            and row["overpayment_amount"]
        ):
            override = _money_minor(row["installment_override"], row["currency"], allow_zero=False)
            target = _money_minor(row["overpayment_amount"], row["currency"], allow_zero=False)
            if target < override:
                errors.append(ArchiveError("loans.csv", "invalid loan overpayment"))

    def _loan_reference(file: str, row: dict[str, str]) -> None:
        loan = loans_by_id.get(row["loan_id"])
        if loan is None or row["loan"] != loan["name"]:
            errors.append(ArchiveError(file, "loan reference mismatch"))

    rate_changes = tables["loan_rate_changes.csv"]
    rate_change_ids: set[str] = set()
    rate_change_keys: set[tuple[str, str]] = set()
    for row in rate_changes:
        _loan_reference("loan_rate_changes.csv", row)
        if row["id"] in rate_change_ids:
            errors.append(ArchiveError("loan_rate_changes.csv", "duplicate loan rate change id"))
        rate_change_ids.add(row["id"])
        key = (row["loan_id"], row["effective_from"])
        if key in rate_change_keys:
            errors.append(ArchiveError("loan_rate_changes.csv", "duplicate loan rate change"))
        rate_change_keys.add(key)
        if row["mode"] == "variable":
            rate = reference_rates_by_id.get(row["reference_rate_id"])
            if rate is None or row["reference_rate"] != rate["name"]:
                errors.append(
                    ArchiveError("loan_rate_changes.csv", "rate change reference mismatch")
                )
        elif row["reference_rate_id"] or row["margin_bp"] or not row["rate_bp"]:
            errors.append(
                ArchiveError("loan_rate_changes.csv", "invalid rate change configuration")
            )

    transfers = tables["loan_transfers.csv"]
    transfer_ids: set[str] = set()
    transfer_totals: dict[str, int] = {}
    for row in transfers:
        _loan_reference("loan_transfers.csv", row)
        if row["id"] in transfer_ids:
            errors.append(ArchiveError("loan_transfers.csv", "duplicate loan transfer id"))
        transfer_ids.add(row["id"])
        if len(row["note"]) > 200:
            errors.append(
                ArchiveError("loan_transfers.csv", "invalid transfer note", column="note")
            )
        loan = loans_by_id.get(row["loan_id"])
        if loan is None:
            continue
        if loan.get("drawdown_mode", "full_at_origination") != "tranches":
            errors.append(ArchiveError("loan_transfers.csv", "transfer on a non-tranched loan"))
            continue
        try:
            amount = _money_minor(row["amount"], loan["currency"], allow_zero=False)
        except ValueError:
            continue
        transfer_totals[row["loan_id"]] = transfer_totals.get(row["loan_id"], 0) + amount
        if row["date"] and loan["origination_date"] and row["date"] < loan["origination_date"]:
            errors.append(
                ArchiveError("loan_transfers.csv", "transfer before loan start", column="date")
            )
    for loan_id, total in transfer_totals.items():
        loan = loans_by_id[loan_id]
        if loan["principal_original"]:
            principal = _money_minor(loan["principal_original"], loan["currency"], allow_zero=False)
            if total > principal:
                errors.append(
                    ArchiveError("loan_transfers.csv", "transfers exceed the loan principal")
                )

    cost_components = tables["loan_cost_components.csv"]
    cost_ids: set[str] = set()
    for row in cost_components:
        _loan_reference("loan_cost_components.csv", row)
        if row["id"] in cost_ids:
            errors.append(
                ArchiveError("loan_cost_components.csv", "duplicate loan cost component id")
            )
        cost_ids.add(row["id"])
        if row["mode"] == "percent":
            valid = bool(row["rate_bp"]) and not row["fixed_amount"]
        else:
            valid = not row["rate_bp"] and bool(row["fixed_amount"])
        if not valid:
            errors.append(ArchiveError("loan_cost_components.csv", "invalid cost component"))
        if row["effective_to"] and row["effective_to"] < row["effective_from"]:
            errors.append(
                ArchiveError(
                    "loan_cost_components.csv",
                    "invalid cost component range",
                    column="effective_to",
                )
            )

    overpayments = tables["loan_overpayments.csv"]
    overpayment_ids: set[str] = set()
    for row in overpayments:
        _loan_reference("loan_overpayments.csv", row)
        if row["id"] in overpayment_ids:
            errors.append(ArchiveError("loan_overpayments.csv", "duplicate loan overpayment id"))
        overpayment_ids.add(row["id"])

    payments = tables["loan_payments.csv"]
    payment_ids: set[str] = set()
    for row in payments:
        _loan_reference("loan_payments.csv", row)
        if row["id"] in payment_ids:
            errors.append(ArchiveError("loan_payments.csv", "duplicate loan payment id"))
        payment_ids.add(row["id"])
        total = _money_minor(row["total"], currency, allow_zero=False)
        components = sum(
            _money_minor(row[column], currency, allow_zero=True)
            for column in ("principal", "interest", "costs", "overpayment")
        )
        if total != components:
            errors.append(ArchiveError("loan_payments.csv", "loan payment components differ"))
        if row["transaction_id"] and row["transaction_id"] not in ledger_transaction_ids:
            errors.append(ArchiveError("loan_payments.csv", "loan payment transaction missing"))

    checkpoints = tables["loan_balance_checkpoints.csv"]
    checkpoint_ids: set[str] = set()
    checkpoint_keys: set[tuple[str, str]] = set()
    for row in checkpoints:
        _loan_reference("loan_balance_checkpoints.csv", row)
        if row["id"] in checkpoint_ids:
            errors.append(
                ArchiveError("loan_balance_checkpoints.csv", "duplicate loan balance checkpoint id")
            )
        checkpoint_ids.add(row["id"])
        key = (row["loan_id"], row["as_of"])
        if key in checkpoint_keys:
            errors.append(
                ArchiveError("loan_balance_checkpoints.csv", "duplicate loan balance checkpoint")
            )
        checkpoint_keys.add(key)

    scenarios = tables["loan_scenarios.csv"]
    scenario_ids: set[str] = set()
    scenario_keys: set[tuple[str, str]] = set()
    for row in scenarios:
        _loan_reference("loan_scenarios.csv", row)
        if row["id"] in scenario_ids:
            errors.append(ArchiveError("loan_scenarios.csv", "duplicate loan scenario id"))
        scenario_ids.add(row["id"])
        key = (row["loan_id"], name_key(row["name"]))
        if key in scenario_keys:
            errors.append(ArchiveError("loan_scenarios.csv", "duplicate loan scenario name"))
        scenario_keys.add(key)
        mode = row["overpayment_mode"]
        if (
            (mode == "" and (row["overpayment_amount"] or row["overpayment_strategy"]))
            or (mode == "none" and (row["overpayment_amount"] or row["overpayment_strategy"]))
            or (mode not in {"", "none"} and not row["overpayment_amount"])
        ):
            errors.append(ArchiveError("loan_scenarios.csv", "invalid loan scenario"))
        if bool(row["rate_shift_bp"]) != bool(row["rate_shift_from"]):
            errors.append(ArchiveError("loan_scenarios.csv", "invalid loan scenario"))
        if bool(row["one_off_date"]) != bool(row["one_off_amount"]):
            errors.append(ArchiveError("loan_scenarios.csv", "invalid loan scenario"))

    plans = tables["installment_plans.csv"]
    plan_ids: set[str] = set()
    plan_loans: set[str] = set()
    for row in plans:
        _loan_reference("installment_plans.csv", row)
        if row["id"] in plan_ids:
            errors.append(ArchiveError("installment_plans.csv", "duplicate installment plan id"))
        plan_ids.add(row["id"])
        if row["loan_id"] in plan_loans:
            errors.append(ArchiveError("installment_plans.csv", "duplicate installment plan loan"))
        plan_loans.add(row["loan_id"])
        loan = loans_by_id.get(row["loan_id"])
        if loan is not None and loan["kind"] != "installment_plan":
            errors.append(
                ArchiveError(
                    "installment_plans.csv",
                    "installment plan must reference an installment-plan loan",
                )
            )
        category = by_id.get(row["category_id"])
        if category is None or row["category"] != category["name"]:
            errors.append(
                ArchiveError("installment_plans.csv", "installment plan category mismatch")
            )

    fire_scenarios = tables["fire_scenarios.csv"]
    fire_scenarios_by_id: dict[str, dict[str, str]] = {}
    active_fire_names: set[str] = set()
    active_fire_defaults = 0
    for row in fire_scenarios:
        scenario_id = row["id"]
        if scenario_id in fire_scenarios_by_id:
            errors.append(ArchiveError("fire_scenarios.csv", "duplicate FIRE scenario id"))
        fire_scenarios_by_id[scenario_id] = row
        key = name_key(row["name"])
        if not row["archived_at"]:
            if key in active_fire_names:
                errors.append(
                    ArchiveError("fire_scenarios.csv", "duplicate active FIRE scenario name")
                )
            active_fire_names.add(key)
        if row["is_default"] == "true":
            if row["archived_at"]:
                errors.append(ArchiveError("fire_scenarios.csv", "archived default scenario"))
            else:
                active_fire_defaults += 1
                if active_fire_defaults > 1:
                    errors.append(ArchiveError("fire_scenarios.csv", "duplicate default scenario"))
        birth_year = int(row["birth_year"])
        start_year = int(row["start_year"])
        horizon_age = int(row["horizon_age"])
        stop_working_age = int(row["stop_working_age"])
        age = start_year - birth_year
        if birth_year > start_year:
            errors.append(ArchiveError("fire_scenarios.csv", "invalid FIRE scenario years"))
        if horizon_age <= age or birth_year + horizon_age > MAX_YEAR:
            errors.append(ArchiveError("fire_scenarios.csv", "invalid FIRE scenario horizon"))
        if stop_working_age < age:
            errors.append(ArchiveError("fire_scenarios.csv", "invalid FIRE stop-working age"))
        if row["rule"] == "swr":
            if not row["swr_bp"]:
                errors.append(ArchiveError("fire_scenarios.csv", "invalid FIRE withdrawal rate"))
        elif row["swr_bp"]:
            errors.append(ArchiveError("fire_scenarios.csv", "invalid FIRE withdrawal rate"))
        currency = row["currency"]
        for mode_column, manual_column, fallback_column in (
            ("income_mode", "income_manual", "income_fallback"),
            ("expenses_mode", "expenses_manual", "expenses_fallback"),
            ("portfolio_mode", "portfolio_manual", "portfolio_fallback"),
        ):
            mode = row[mode_column]
            manual = row[manual_column]
            fallback = row[fallback_column]
            if mode == "manual":
                if not manual:
                    errors.append(ArchiveError("fire_scenarios.csv", "missing manual FIRE amount"))
                else:
                    try:
                        _nonnegative_money_minor(manual, currency)
                    except ValueError as exc:
                        errors.append(
                            ArchiveError("fire_scenarios.csv", str(exc), column=manual_column)
                        )
                if fallback:
                    errors.append(
                        ArchiveError("fire_scenarios.csv", "fallback for a manual FIRE input")
                    )
            else:
                if manual:
                    errors.append(
                        ArchiveError("fire_scenarios.csv", "manual value for a linked FIRE input")
                    )
                if fallback:
                    try:
                        _nonnegative_money_minor(fallback, currency)
                    except ValueError as exc:
                        errors.append(
                            ArchiveError("fire_scenarios.csv", str(exc), column=fallback_column)
                        )

    contributions = tables["fire_contributions.csv"]
    contribution_ids: set[str] = set()
    contribution_keys: set[tuple[str, str]] = set()
    for row in contributions:
        scenario = fire_scenarios_by_id.get(row["scenario_id"])
        if scenario is None or row["scenario"] != scenario["name"]:
            errors.append(
                ArchiveError("fire_contributions.csv", "FIRE scenario reference mismatch")
            )
            continue
        if row["id"] in contribution_ids:
            errors.append(ArchiveError("fire_contributions.csv", "duplicate FIRE contribution id"))
        contribution_ids.add(row["id"])
        key = (row["scenario_id"], row["year"])
        if key in contribution_keys:
            errors.append(
                ArchiveError("fire_contributions.csv", "duplicate FIRE contribution year")
            )
        contribution_keys.add(key)
        year = int(row["year"])
        window = contribution_years(
            int(scenario["start_year"]),
            int(scenario["birth_year"]),
            int(scenario["horizon_age"]),
        )
        if year not in window:
            errors.append(
                ArchiveError("fire_contributions.csv", "FIRE contribution outside the plan window")
            )
        try:
            _signed_money_minor(row["net_amount"], scenario["currency"])
        except ValueError as exc:
            errors.append(ArchiveError("fire_contributions.csv", str(exc), column="net_amount"))

    events = tables["fire_events.csv"]
    event_ids: set[str] = set()
    for row in events:
        scenario = fire_scenarios_by_id.get(row["scenario_id"])
        if scenario is None or row["scenario"] != scenario["name"]:
            errors.append(ArchiveError("fire_events.csv", "FIRE scenario reference mismatch"))
            continue
        if row["id"] in event_ids:
            errors.append(ArchiveError("fire_events.csv", "duplicate FIRE event id"))
        event_ids.add(row["id"])
        year = int(row["year"])
        window = event_years(
            int(scenario["start_year"]),
            int(scenario["birth_year"]),
            int(scenario["horizon_age"]),
        )
        if year not in window:
            errors.append(ArchiveError("fire_events.csv", "FIRE event outside the plan window"))
        try:
            _signed_money_minor(row["amount"], scenario["currency"])
        except ValueError as exc:
            errors.append(ArchiveError("fire_events.csv", str(exc), column="amount"))

    exclusions = tables["fire_expense_exclusions.csv"]
    exclusion_ids: set[str] = set()
    exclusion_keys: set[tuple[str, str]] = set()
    for row in exclusions:
        scenario = fire_scenarios_by_id.get(row["scenario_id"])
        if scenario is None or row["scenario"] != scenario["name"]:
            errors.append(
                ArchiveError("fire_expense_exclusions.csv", "FIRE scenario reference mismatch")
            )
            continue
        if row["id"] in exclusion_ids:
            errors.append(
                ArchiveError("fire_expense_exclusions.csv", "duplicate FIRE exclusion id")
            )
        exclusion_ids.add(row["id"])
        key = (row["scenario_id"], row["category_id"])
        if key in exclusion_keys:
            errors.append(ArchiveError("fire_expense_exclusions.csv", "duplicate FIRE exclusion"))
        exclusion_keys.add(key)
        category = by_id.get(row["category_id"])
        # A wrong-kind or non-regular category is not an archive error: the restore skips the
        # exclusion with a warning and falls linked expenses back to their archived amount.
        # Only a present category with a different companion name is a cross-file mismatch.
        if category is not None and row["category"] != category["name"]:
            errors.append(
                ArchiveError("fire_expense_exclusions.csv", "FIRE exclusion category mismatch")
            )

    return ArchiveRows(
        workspace=workspace,
        categories=tuple(
            CategoryRow(
                row["id"],
                row["kind"],
                row["name"],
                row["treatment"],
                row["color"],
                int(row["sort_order"]),
                row["archived_at"] or None,
            )
            for row in categories
        ),
        budget_lines=tuple(BudgetLineRow(**row) for row in tables["budget_lines.csv"]),
        transactions=tuple(
            TransactionRow(
                row["date"],
                row["direction"],
                row["amount"],
                row["currency"],
                row["category"],
                row["description"],
                row["category_id"],
                row["id"],
                row["deleted_at"] or None,
            )
            for row in tables["transactions.csv"]
        ),
        months=tuple(
            MonthRow(row["month"], row["closed_at"] or None, row["plan_started_at"] or None)
            for row in tables["months.csv"]
        ),
        networth_categories=tuple(
            NetWorthCategoryRow(
                row["id"],
                row["side"],
                row["name"],
                row["color"],
                int(row["sort_order"]),
                row["archived_at"] or None,
            )
            for row in networth_categories
        ),
        networth_items=tuple(
            NetWorthItemRow(
                row["id"],
                row["side"],
                row["category"],
                row["category_id"],
                row["name"],
                row["currency"],
                row["notes"],
                row["acquired_on"] or None,
                row["valuation_mode"],
                row["link_type"] or None,
                row["link_id"] or None,
                row["include_in_net_worth"],
                row["investable"],
                row["archived_at"] or None,
                row["estimate_between"],
            )
            for row in networth_items
        ),
        item_valuations=tuple(
            ItemValuationRow(
                row["id"],
                row["item_id"],
                row["item"],
                row["as_of"],
                row["value"],
                row["currency"],
                row["note"],
                row["created_at"] or None,
            )
            for row in item_valuations
        ),
        networth_snapshots=tuple(
            NetWorthSnapshotRow(
                row["id"],
                row["as_of"],
                row["assets"],
                row["liabilities"],
                row["currency"],
                row["breakdown"],
                row["source"],
            )
            for row in networth_snapshots
        ),
        asset_classes=tuple(
            AssetClassRow(
                row["id"],
                row["name"],
                row["investable"],
                row["color"],
                int(row["sort_order"]),
                row["risk_tier"],
                row["archived_at"] or None,
            )
            for row in asset_classes
        ),
        instruments=tuple(
            InstrumentRow(
                row["id"],
                row["name"],
                row["symbol"],
                row["type"],
                row["quote_currency"],
                row["unit_label"],
                row["valuation_source"],
                row["provider_key"],
                row["provider_symbol"],
                row["provider_backfilled_at"] or None,
                row["risk_tier_override"],
                row["archived_at"] or None,
            )
            for row in instruments
        ),
        instrument_allocations=tuple(
            InstrumentAllocationRow(
                row["id"],
                row["instrument_id"],
                row["instrument"],
                row["asset_class_id"],
                row["asset_class"],
                int(row["weight_bp"]),
            )
            for row in allocations
        ),
        investment_transactions=tuple(
            InvestmentTransactionRow(
                row["id"],
                row["instrument_id"],
                row["instrument"],
                row["date"] or None,
                row["type"],
                row["quantity"],
                row["unit_price"],
                row["quote_currency"],
                row["fee"],
                row["fx_to_base"],
                row["note"],
                row["settlement_currency"],
                row["settlement_unit_price"],
                row["settlement_fee"],
                row["deleted_at"] or None,
                row["income_transaction_id"] or None,
            )
            for row in transactions
        ),
        bond_terms=tuple(
            BondTermsRow(
                row["id"],
                row["instrument_id"],
                row["instrument"],
                row["series_code"],
                row["face_value"],
                row["maturity_date"],
                row["interest_mode"],
                row["rate_schedule"],
                int(row["tax_rate_bp"]),
                row["early_redemption_fee_mode"],
                int(row["early_redemption_fee_rate_bp"])
                if row["early_redemption_fee_rate_bp"]
                else None,
                row["early_redemption_fee"],
            )
            for row in bonds
        ),
        price_points=tuple(
            PricePointRow(
                row["id"],
                row["instrument_id"],
                row["instrument"],
                row["date"],
                row["price"],
                row["source"],
                row["provider_key"],
            )
            for row in prices
        ),
        fx_rates=tuple(
            FxRateRow(
                row["id"],
                row["base_currency"],
                row["quote_currency"],
                row["date"],
                row["rate"],
                row["source"],
            )
            for row in fx_rates
        ),
        price_source_configs=tuple(
            PriceSourceConfigRow(
                row["id"],
                row["provider_key"],
                row["enabled"],
                int(row["daily_call_budget"]),
                row["fx_backfilled"],
            )
            for row in configs
        ),
        risk_target_policy=risk_policy,
        reference_rates=tuple(
            ReferenceRateRow(row["id"], row["name"], row["notes"]) for row in reference_rates
        ),
        reference_rate_values=tuple(
            ReferenceRateValueRow(
                row["id"],
                row["reference_rate_id"],
                row["reference_rate"],
                row["effective_from"],
                int(row["value_bp"]),
            )
            for row in reference_values
        ),
        loans=tuple(
            LoanRow(
                row["id"],
                row["name"],
                row["kind"],
                row["currency"],
                row["principal_original"],
                row["origination_date"] or None,
                row["first_payment_date"],
                int(row["term_months"]),
                int(row["payment_day"]),
                row["rate_mode"],
                row["day_count"],
                row.get("drawdown_mode", "full_at_origination"),
                int(row.get("interest_only_months", "0") or 0),
                int(row["margin_bp"]) if row["margin_bp"] else None,
                row["reference_rate_id"] or None,
                row["reference_rate"],
                row["property_item_id"] or None,
                row["property_item"],
                row["auto_record_payments"],
                row["installment_override"],
                row["overpayment_mode"],
                row["overpayment_amount"],
                row["overpayment_strategy"],
                row["archived_at"] or None,
            )
            for row in loans
        ),
        loan_rate_changes=tuple(
            LoanRateChangeRow(
                id=row["id"],
                loan_id=row["loan_id"],
                loan=row["loan"],
                effective_from=row["effective_from"],
                rate_bp=int(row["rate_bp"]) if row["rate_bp"] else None,
                mode=row["mode"] or "fixed",
                reference_rate_id=row["reference_rate_id"] or None,
                reference_rate=row["reference_rate"] or None,
                margin_bp=int(row["margin_bp"]) if row["margin_bp"] else None,
            )
            for row in rate_changes
        ),
        loan_transfers=tuple(
            LoanTransferRow(
                id=row["id"],
                loan_id=row["loan_id"],
                loan=row["loan"],
                date=row["date"],
                amount=row["amount"],
                note=row["note"],
            )
            for row in tables["loan_transfers.csv"]
        ),
        loan_cost_components=tuple(
            LoanCostComponentRow(
                row["id"],
                row["loan_id"],
                row["loan"],
                row["name"],
                row["mode"],
                int(row["rate_bp"]) if row["rate_bp"] else None,
                row["fixed_amount"],
                row["basis"],
                row["effective_from"],
                row["effective_to"] or None,
            )
            for row in cost_components
        ),
        loan_overpayments=tuple(
            LoanOverpaymentRow(
                row["id"],
                row["loan_id"],
                row["loan"],
                row["date"],
                row["amount"],
                row["note"],
            )
            for row in overpayments
        ),
        loan_payments=tuple(
            LoanPaymentRow(
                row["id"],
                row["loan_id"],
                row["loan"],
                row["date"],
                row["kind"],
                row["total"],
                row["principal"],
                row["interest"],
                row["costs"],
                row["overpayment"],
                row["note"],
                row["source"],
                row["transaction_id"] or None,
            )
            for row in payments
        ),
        loan_balance_checkpoints=tuple(
            LoanBalanceCheckpointRow(
                row["id"],
                row["loan_id"],
                row["loan"],
                row["as_of"],
                row["remaining_principal"],
                row["note"],
            )
            for row in checkpoints
        ),
        loan_scenarios=tuple(
            LoanScenarioRow(
                row["id"],
                row["loan_id"],
                row["loan"],
                row["name"],
                row["overpayment_mode"],
                row["overpayment_amount"],
                row["overpayment_strategy"],
                int(row["rate_shift_bp"]) if row["rate_shift_bp"] else None,
                row["rate_shift_from"] or None,
                row["one_off_date"] or None,
                row["one_off_amount"],
            )
            for row in scenarios
        ),
        installment_plans=tuple(
            InstallmentPlanRow(
                row["id"],
                row["loan_id"],
                row["loan"],
                row["category_id"],
                row["category"],
                row["installment"],
                int(row["remaining_count"]),
                row["end_date"],
                row["closed_at"] or None,
            )
            for row in plans
        ),
        fire_scenarios=tuple(
            FireScenarioRow(
                row["id"],
                row["name"],
                row["currency"],
                int(row["birth_year"]),
                int(row["start_year"]),
                int(row["inflation_bp"]),
                int(row["real_return_bp"]),
                int(row["income_growth_bp"]),
                int(row["stop_working_age"]),
                int(row["horizon_age"]),
                row["rule"],
                int(row["swr_bp"]) if row["swr_bp"] else None,
                row["income_mode"],
                row["income_manual"],
                row["income_fallback"],
                row["expenses_mode"],
                row["expenses_manual"],
                row["expenses_fallback"],
                row["portfolio_mode"],
                row["portfolio_manual"],
                row["portfolio_fallback"],
                row["is_default"],
                row["archived_at"] or None,
            )
            for row in fire_scenarios
        ),
        fire_contributions=tuple(
            FireContributionRow(
                row["id"],
                row["scenario_id"],
                row["scenario"],
                int(row["year"]),
                row["net_amount"],
            )
            for row in contributions
        ),
        fire_events=tuple(
            FireEventRow(
                row["id"],
                row["scenario_id"],
                row["scenario"],
                int(row["year"]),
                row["kind"],
                row["amount"],
                row["label"],
                row["source_loan_id"] or None,
            )
            for row in events
        ),
        fire_expense_exclusions=tuple(
            FireExpenseExclusionRow(
                row["id"],
                row["scenario_id"],
                row["scenario"],
                row["category_id"],
                row["category"],
            )
            for row in exclusions
        ),
    )
