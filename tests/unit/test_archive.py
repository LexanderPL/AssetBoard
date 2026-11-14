from __future__ import annotations

import csv
import io
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_BZIP2, ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from assetboard.db.base import Base, WorkspaceOwned
from assetboard.domain.archive import (
    ArchiveError,
    ArchiveRows,
    AssetClassRow,
    BondTermsRow,
    BudgetLineRow,
    CategoryRow,
    FireContributionRow,
    FireEventRow,
    FireExpenseExclusionRow,
    FireScenarioRow,
    FxRateRow,
    InstallmentPlanRow,
    InstrumentAllocationRow,
    InstrumentRow,
    InvestmentTransactionRow,
    ItemValuationRow,
    LoanBalanceCheckpointRow,
    LoanCostComponentRow,
    LoanOverpaymentRow,
    LoanPaymentRow,
    LoanRateChangeRow,
    LoanRow,
    LoanScenarioRow,
    MonthRow,
    NetWorthCategoryRow,
    NetWorthItemRow,
    NetWorthSnapshotRow,
    PricePointRow,
    PriceSourceConfigRow,
    ReferenceRateRow,
    ReferenceRateValueRow,
    RiskTargetPolicyRow,
    TransactionRow,
    decimal_text,
    parse_archive,
    write_archive,
)
from assetboard.services.archive import ARCHIVE_EXCLUDED_MODELS, ARCHIVE_INCLUDED_MODELS

FIXTURES = Path(__file__).parents[1] / "fixtures"
GOLDEN = FIXTURES / "workspace-archive-v1.7.zip"
GOLDEN_V1_6 = FIXTURES / "workspace-archive-v1.6.zip"
GOLDEN_V1_5 = FIXTURES / "workspace-archive-v1.5.zip"
GOLDEN_V1_4 = FIXTURES / "workspace-archive-v1.4.zip"
GOLDEN_V1_3 = FIXTURES / "workspace-archive-v1.3.zip"
GOLDEN_V1_2 = FIXTURES / "workspace-archive-v1.2.zip"
GOLDEN_V1_1 = FIXTURES / "workspace-archive-v1.1.zip"
GOLDEN_V1_0 = FIXTURES / "workspace-archive-v1.0.zip"
EXPORTED_AT = datetime(2026, 10, 18, 14, 30, tzinfo=UTC)


def _rows() -> ArchiveRows:
    category_id = "2b0e8c1e-5f7a-4c6e-9a51-3f1d2c7b9e01"
    transaction_id = "5c7d8e9f-0a1b-4c2d-9e3f-4a5b6c7d8e02"
    networth_category_id = "3f9a1c2d-4b5e-4f60-8a71-2b3c4d5e6f70"
    networth_item_id = "c4d5e6f7-a8b9-4c0d-9e1f-2a3b4c5d6e7f"
    linked_item_id = "d5e6f7a8-b9c0-4d1e-8f2a-3b4c5d6e7f80"
    first_valuation_id = "e6f7a8b9-c0d1-4e2f-9a3b-4c5d6e7f8091"
    second_valuation_id = "f7a8b9c0-d1e2-4f3a-8b4c-5d6e7f8091a2"
    snapshot_id = "a8b9c0d1-e2f3-4a4b-9c5d-6e7f8091a2b3"
    asset_class_id = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    bond_class_id = "1b2c3d4e-5f60-4b7c-9d8e-0f1a2b3c4d5e"
    instrument_id = "2c3d4e5f-6071-4c8d-ae9f-1a2b3c4d5e6f"
    bond_instrument_id = "3d4e5f60-7182-4d9e-bfa0-2b3c4d5e6f70"
    allocation_id = "4e5f6071-8293-4eaf-80b1-3c4d5e6f7081"
    bond_allocation_id = "5f607182-93a4-4fb0-91c2-4d5e6f708192"
    investment_transaction_id = "60718293-a4b5-40c1-82d3-5e6f708192a3"
    bond_terms_id = "718293a4-b5c6-41d2-93e4-6f708192a3b4"
    price_point_id = "8293a4b5-c6d7-42e3-84f5-708192a3b4c5"
    fx_rate_id = "93a4b5c6-d7e8-43f4-a506-8192a3b4c5d6"
    provider_config_id = "a4b5c6d7-e8f9-44a5-b617-92a3b4c5d6e7"
    reference_rate_id = "b5c6d7e8-f9a0-45b6-8c28-a3b4c5d6e7f8"
    reference_value_id = "c6d7e8f9-a0b1-46c7-9d39-b4c5d6e7f809"
    loan_id = "d7e8f9a0-b1c2-47d8-ae4a-c5d6e7f8091a"
    fixed_loan_id = "e8f9a0b1-c2d3-48e9-bf5b-d6e7f8091a2b"
    plan_loan_id = "f9a0b1c2-d3e4-49fa-806c-e7f8091a2b3c"
    rate_change_id = "a0b1c2d3-e4f5-4a0b-917d-f8091a2b3c4d"
    cost_component_id = "b1c2d3e4-f5a6-4b1c-828e-091a2b3c4d5e"
    loan_overpayment_id = "c2d3e4f5-a6b7-4c2d-939f-1a2b3c4d5e6f"
    loan_payment_id = "d3e4f5a6-b7c8-4d3e-84a0-2b3c4d5e6f70"
    checkpoint_id = "e4f5a6b7-c8d9-4e4f-95b1-3c4d5e6f7081"
    scenario_id = "f5a6b7c8-d9e0-4f50-86c2-4d5e6f708192"
    installment_plan_id = "a6b7c8d9-e0f1-4a61-97d3-5e6f708192a3"
    fire_scenario_id = "d1e2f3a4-b5c6-4d7e-8f90-1a2b3c4d5e6f"
    fire_archived_id = "e2f3a4b5-c6d7-4e8f-90a1-2b3c4d5e6f70"
    fire_contribution_id = "f3a4b5c6-d7e8-4f90-a1b2-3c4d5e6f7081"
    fire_event_id = "a4b5c6d7-e8f9-4a01-b2c3-4d5e6f708192"
    fire_exclusion_id = "b5c6d7e8-f9a0-4b12-83c4-5d6e7f8091a2"
    breakdown = (
        '{"categories":[{"category_id":"'
        + networth_category_id
        + '","side":"asset","total_minor":1200000}],"not_counted":'
        '{"non_base":0,"no_value":0,"unavailable":0},"v":1}'
    )
    return ArchiveRows(
        workspace={
            "name": "Home",
            "base_currency": "EUR",
            "country_code": "PL",
            "timezone": "Europe/Warsaw",
            "locale": "pl",
            "auto_copy_plan": False,
            "taxes_enabled": True,
            "upcoming_days": 14,
            "investment_cost_basis": "fifo",
            "risk_target_policy": {
                "mode": "fixed",
                "safe_bp": 6000,
                "growth_bp": 3000,
                "speculative_bp": 1000,
                "tolerance_bp": 500,
            },
        },
        categories=(CategoryRow(category_id, "expense", "Food", "regular", "#4f9d69", 0, None),),
        budget_lines=(BudgetLineRow("2026-09", "expense", "Food", "550.00", "EUR", category_id),),
        transactions=(
            TransactionRow(
                "2026-09-02",
                "expense",
                "4.20",
                "EUR",
                "Food",
                "Bakery, rolls",
                category_id,
                transaction_id,
                "2026-09-03T07:12:44Z",
            ),
        ),
        months=(MonthRow("2026-09", None, "2026-09-01T06:30:12Z"),),
        networth_categories=(
            NetWorthCategoryRow(networth_category_id, "asset", "Vehicles", "#3b6ea8", 0, None),
        ),
        networth_items=(
            NetWorthItemRow(
                networth_item_id,
                "asset",
                "Vehicles",
                networth_category_id,
                "Car",
                "EUR",
                "Family car",
                "2025-04-01",
                "manual",
                None,
                None,
                "true",
                "false",
                None,
                "false",
            ),
            NetWorthItemRow(
                linked_item_id,
                "asset",
                "Vehicles",
                networth_category_id,
                "Brokerage",
                "EUR",
                "",
                None,
                "linked",
                "portfolio_holding",
                instrument_id,
                "true",
                "true",
                None,
                "true",
            ),
        ),
        item_valuations=(
            ItemValuationRow(
                first_valuation_id,
                networth_item_id,
                "Car",
                "2026-09-01",
                "12000.00",
                "EUR",
                "Bought",
                "2026-09-01T08:00:00Z",
            ),
            ItemValuationRow(
                second_valuation_id,
                networth_item_id,
                "Car",
                "2026-09-01",
                "12050.00",
                "EUR",
                "",
                "2026-09-01T09:00:00Z",
            ),
        ),
        networth_snapshots=(
            NetWorthSnapshotRow(
                snapshot_id,
                "2026-08-31",
                "12000.00",
                "0.00",
                "EUR",
                breakdown,
                "auto",
            ),
        ),
        asset_classes=(
            AssetClassRow(asset_class_id, "Stocks", "true", "#2563eb", 0, "growth", None),
            AssetClassRow(bond_class_id, "Bonds", "true", "#16a34a", 1, "safe", None),
        ),
        instruments=(
            InstrumentRow(
                instrument_id,
                "World ETF",
                "VWCE",
                "etf",
                "EUR",
                "share",
                "manual",
                "",
                "",
                None,
                "",
                None,
            ),
            InstrumentRow(
                bond_instrument_id,
                "Treasury bond",
                "",
                "bond_model",
                "EUR",
                "bond",
                "bond_model",
                "",
                "",
                None,
                "",
                None,
            ),
        ),
        instrument_allocations=(
            InstrumentAllocationRow(
                allocation_id, instrument_id, "World ETF", asset_class_id, "Stocks", 10_000
            ),
            InstrumentAllocationRow(
                bond_allocation_id,
                bond_instrument_id,
                "Treasury bond",
                bond_class_id,
                "Bonds",
                10_000,
            ),
        ),
        investment_transactions=(
            InvestmentTransactionRow(
                investment_transaction_id,
                instrument_id,
                "World ETF",
                "2026-09-01",
                "buy",
                "10",
                "100.5",
                "EUR",
                "1.00",
                "1",
                "First lot",
                "",
                "",
                "",
                None,
            ),
        ),
        bond_terms=(
            BondTermsRow(
                bond_terms_id,
                bond_instrument_id,
                "Treasury bond",
                "EDO",
                "1000.00",
                "2030-01-01",
                "capitalized_annually",
                '[{"annual_rate_bp":500,"from_date":"2026-01-01"}]',
                1900,
                "percent",
                100,
                "",
            ),
        ),
        price_points=(
            PricePointRow(
                price_point_id,
                instrument_id,
                "World ETF",
                "2026-09-01",
                "101.25",
                "manual",
                "",
            ),
        ),
        fx_rates=(FxRateRow(fx_rate_id, "EUR", "USD", "2026-09-01", "0.92", "nbp"),),
        price_source_configs=(
            PriceSourceConfigRow(provider_config_id, "nbp", "true", 20, '{"USD":"2026-09-01"}'),
        ),
        risk_target_policy=RiskTargetPolicyRow("fixed", 6000, 3000, 1000, 500),
        reference_rates=(ReferenceRateRow(reference_rate_id, "WIBOR", "3M fixing"),),
        reference_rate_values=(
            ReferenceRateValueRow(
                reference_value_id,
                reference_rate_id,
                "WIBOR",
                "2026-01-01",
                575,
            ),
        ),
        loans=(
            LoanRow(
                loan_id,
                "Home mortgage",
                "annuity",
                "EUR",
                "300000.00",
                "2026-01-01",
                "2026-02-01",
                360,
                1,
                "variable",
                "monthly",
                150,
                reference_rate_id,
                "WIBOR",
                networth_item_id,
                "Car",
                "true",
                "",
                "fixed_extra",
                "100.00",
                "reduce_term",
                None,
            ),
            LoanRow(
                fixed_loan_id,
                "Car loan",
                "annuity",
                "EUR",
                "50000.00",
                "2026-01-15",
                "2026-02-15",
                24,
                15,
                "fixed",
                "monthly",
                None,
                None,
                "",
                None,
                "",
                "false",
                "",
                "none",
                "",
                "reduce_term",
                None,
            ),
            LoanRow(
                plan_loan_id,
                "Phone",
                "installment_plan",
                "EUR",
                "3000.00",
                None,
                "2026-04-05",
                3,
                5,
                "fixed",
                "monthly",
                None,
                None,
                "",
                None,
                "",
                "false",
                "",
                "none",
                "",
                "reduce_term",
                None,
            ),
        ),
        loan_rate_changes=(
            LoanRateChangeRow(rate_change_id, fixed_loan_id, "Car loan", "2026-01-15", 650),
        ),
        loan_cost_components=(
            LoanCostComponentRow(
                cost_component_id,
                loan_id,
                "Home mortgage",
                "Account fee",
                "percent",
                20,
                "",
                "opening_balance",
                "2026-01-01",
                None,
            ),
        ),
        loan_overpayments=(
            LoanOverpaymentRow(
                loan_overpayment_id,
                loan_id,
                "Home mortgage",
                "2026-06-01",
                "5000.00",
                "Bonus",
            ),
        ),
        loan_payments=(
            LoanPaymentRow(
                loan_payment_id,
                loan_id,
                "Home mortgage",
                "2026-03-01",
                "installment",
                "2000.00",
                "1000.00",
                "900.00",
                "0.00",
                "100.00",
                "",
                "manual",
                transaction_id,
            ),
        ),
        loan_balance_checkpoints=(
            LoanBalanceCheckpointRow(
                checkpoint_id,
                loan_id,
                "Home mortgage",
                "2026-07-01",
                "280000.00",
                "Bank statement",
            ),
        ),
        loan_scenarios=(
            LoanScenarioRow(
                scenario_id,
                loan_id,
                "Home mortgage",
                "Faster",
                "fixed_extra",
                "300.00",
                "reduce_term",
                None,
                None,
                None,
                "",
            ),
        ),
        installment_plans=(
            InstallmentPlanRow(
                installment_plan_id,
                plan_loan_id,
                "Phone",
                category_id,
                "Food",
                "1000.00",
                3,
                "2026-06-05",
                None,
            ),
        ),
        fire_scenarios=(
            FireScenarioRow(
                fire_scenario_id,
                "Lean FIRE",
                "EUR",
                1985,
                2026,
                250,
                500,
                300,
                55,
                90,
                "swr",
                400,
                "manual",
                "3000.00",
                "",
                "manual",
                "2000.00",
                "",
                "manual",
                "100000.00",
                "",
                "true",
                None,
            ),
            FireScenarioRow(
                fire_archived_id,
                "Coast plan",
                "EUR",
                1990,
                2026,
                250,
                500,
                300,
                60,
                95,
                "returns_cover_expenses",
                None,
                "trailing_12m",
                "",
                "2500.00",
                "trailing_12m",
                "",
                "1800.00",
                "linked_investable",
                "",
                "80000.00",
                "false",
                "2026-09-30T20:00:00Z",
            ),
        ),
        fire_contributions=(
            FireContributionRow(
                fire_contribution_id,
                fire_scenario_id,
                "Lean FIRE",
                2027,
                "1200.00",
            ),
        ),
        fire_events=(
            FireEventRow(
                fire_event_id,
                fire_scenario_id,
                "Lean FIRE",
                2030,
                "cash",
                "-5000.00",
                "Home renovation",
                loan_id,
            ),
        ),
        fire_expense_exclusions=(
            FireExpenseExclusionRow(
                fire_exclusion_id,
                fire_archived_id,
                "Coast plan",
                category_id,
                "Food",
            ),
        ),
    )


def _archive(rows: ArchiveRows | None = None) -> bytes:
    return write_archive(
        rows or _rows(),
        app_name="AssetBoard",
        app_version="0.4.0",
        exported_at=EXPORTED_AT,
        base_currency="EUR",
    )


def test_archive_matches_golden_and_is_deterministic() -> None:
    assert _archive() == GOLDEN.read_bytes()
    assert _archive() == _archive()


def test_released_v1_0_archive_still_reads_without_the_1_1_keys() -> None:
    parsed, errors = parse_archive(GOLDEN_V1_0.read_bytes())
    assert errors == ()
    assert parsed is not None and parsed.warnings == ()
    assert parsed.manifest["format_version"] == "1.0"
    assert "taxes_enabled" not in parsed.rows.workspace
    assert "upcoming_days" not in parsed.rows.workspace
    assert parsed.rows.networth_categories == ()
    assert parsed.rows.networth_items == ()
    assert parsed.rows.item_valuations == ()
    assert parsed.rows.networth_snapshots == ()


def test_v1_1_workspace_carries_the_taxes_switch_and_upcoming_days() -> None:
    parsed, errors = parse_archive(GOLDEN_V1_1.read_bytes())
    assert errors == ()
    assert parsed is not None and parsed.warnings == ()
    assert parsed.manifest["format_version"] == "1.1"
    assert parsed.rows.workspace["taxes_enabled"] is True
    assert parsed.rows.workspace["upcoming_days"] == 14
    assert parsed.rows.networth_categories == ()
    assert parsed.rows.networth_items == ()
    assert parsed.rows.item_valuations == ()
    assert parsed.rows.networth_snapshots == ()


def test_v1_3_requires_every_net_worth_file() -> None:
    with ZipFile(io.BytesIO(_archive())) as source:
        files = {
            name: source.read(name) for name in source.namelist() if name != "item_valuations.csv"
        }
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    parsed, errors = parse_archive(output.getvalue())
    assert parsed is None
    assert errors == (ArchiveError("archive", "missing required members"),)


def test_v1_2_workspace_reads_without_the_estimate_column() -> None:
    parsed, errors = parse_archive(GOLDEN_V1_2.read_bytes())
    assert errors == ()
    assert parsed is not None and parsed.warnings == ()
    assert parsed.manifest["format_version"] == "1.2"
    assert [row.estimate_between for row in parsed.rows.networth_items] == ["", ""]


def test_v1_3_requires_the_estimate_column() -> None:
    files = _files()
    rows = list(csv.reader(io.StringIO(files["networth_items.csv"].decode("utf-8"), newline="")))
    column = rows[0].index("estimate_between")
    for row in rows:
        del row[column]
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\r\n").writerows(rows)
    files["networth_items.csv"] = output.getvalue().encode("utf-8")
    parsed, errors = parse_archive(_rewrite(files))
    assert parsed is None
    assert any(error.message == "invalid or missing header" for error in errors)


def test_v1_3_workspace_carries_the_net_worth_files_and_estimate_switch() -> None:
    parsed, errors = parse_archive(GOLDEN_V1_3.read_bytes())
    assert errors == ()
    assert parsed is not None and parsed.warnings == ()
    assert parsed.manifest["format_version"] == "1.3"
    assert len(parsed.rows.networth_categories) == 1
    assert [row.name for row in parsed.rows.networth_items] == ["Car", "Brokerage"]
    assert [row.estimate_between for row in parsed.rows.networth_items] == ["false", "true"]
    assert [row.value for row in parsed.rows.item_valuations] == ["12000.00", "12050.00"]
    assert parsed.rows.item_valuations[0].created_at == "2026-09-01T08:00:00Z"
    assert [row.source for row in parsed.rows.networth_snapshots] == ["auto"]
    assert parsed.rows.asset_classes == ()
    assert parsed.rows.instruments == ()
    assert parsed.rows.risk_target_policy is None


def test_v1_4_workspace_carries_investments_and_risk_targets() -> None:
    parsed, errors = parse_archive(GOLDEN_V1_4.read_bytes())
    assert errors == ()
    assert parsed is not None and parsed.warnings == ()
    assert parsed.manifest["format_version"] == "1.4"
    assert [row.name for row in parsed.rows.asset_classes] == ["Stocks", "Bonds"]
    assert [row.risk_tier for row in parsed.rows.asset_classes] == ["growth", "safe"]
    assert [row.name for row in parsed.rows.instruments] == ["World ETF", "Treasury bond"]
    assert [row.weight_bp for row in parsed.rows.instrument_allocations] == [10_000, 10_000]
    assert parsed.rows.investment_transactions[0].quantity == "10"
    assert parsed.rows.investment_transactions[0].unit_price == "100.5"
    assert parsed.rows.investment_transactions[0].fee == "1.00"
    assert parsed.rows.price_points[0].price == "101.25"
    assert parsed.rows.fx_rates[0].rate == "0.92"
    assert parsed.rows.price_source_configs[0].enabled == "true"
    assert parsed.rows.risk_target_policy == RiskTargetPolicyRow("fixed", 6000, 3000, 1000, 500)
    assert parsed.rows.workspace["investment_cost_basis"] == "fifo"
    assert parsed.rows.reference_rates == ()
    assert parsed.rows.loans == ()
    assert parsed.rows.installment_plans == ()


def test_v1_5_workspace_carries_loans_and_reference_rates() -> None:
    parsed, errors = parse_archive(GOLDEN_V1_5.read_bytes())
    assert errors == ()
    assert parsed is not None and parsed.warnings == ()
    assert parsed.manifest["format_version"] == "1.5"
    assert [row.name for row in parsed.rows.reference_rates] == ["WIBOR"]
    assert parsed.rows.reference_rate_values[0].value_bp == 575
    assert [row.name for row in parsed.rows.loans] == ["Home mortgage", "Car loan", "Phone"]
    assert parsed.rows.loans[0].property_item == "Car"
    assert parsed.rows.loans[0].auto_record_payments == "true"
    assert parsed.rows.loans[0].day_count == "monthly"
    assert parsed.rows.loan_rate_changes[0].rate_bp == 650
    assert parsed.rows.loan_cost_components[0].rate_bp == 20
    assert parsed.rows.loan_overpayments[0].amount == "5000.00"
    assert parsed.rows.loan_payments[0].transaction_id == ("5c7d8e9f-0a1b-4c2d-9e3f-4a5b6c7d8e02")
    assert parsed.rows.loan_balance_checkpoints[0].remaining_principal == "280000.00"
    assert parsed.rows.loan_scenarios[0].overpayment_amount == "300.00"
    assert parsed.rows.installment_plans[0].remaining_count == 3


def test_v1_6_workspace_adds_the_rate_segment_type() -> None:
    parsed, errors = parse_archive(GOLDEN_V1_6.read_bytes())
    assert errors == ()
    assert parsed is not None and parsed.warnings == ()
    assert parsed.manifest["format_version"] == "1.6"
    change = parsed.rows.loan_rate_changes[0]
    assert change.mode == "fixed"
    assert change.rate_bp == 650
    assert change.reference_rate_id is None
    assert change.margin_bp is None
    assert parsed.rows.fire_scenarios == ()
    assert parsed.rows.fire_contributions == ()
    assert parsed.rows.fire_events == ()
    assert parsed.rows.fire_expense_exclusions == ()


def test_v1_7_workspace_carries_fire_scenarios_and_children() -> None:
    parsed, errors = parse_archive(GOLDEN.read_bytes())
    assert errors == ()
    assert parsed is not None and parsed.warnings == ()
    assert parsed.manifest["format_version"] == "1.7"
    assert [row.name for row in parsed.rows.fire_scenarios] == ["Lean FIRE", "Coast plan"]
    assert parsed.rows.fire_scenarios[0].rule == "swr"
    assert parsed.rows.fire_scenarios[0].swr_bp == 400
    assert parsed.rows.fire_scenarios[0].income_manual == "3000.00"
    assert parsed.rows.fire_scenarios[1].income_mode == "trailing_12m"
    assert parsed.rows.fire_scenarios[1].income_fallback == "2500.00"
    assert parsed.rows.fire_scenarios[1].archived_at == "2026-09-30T20:00:00Z"
    assert parsed.rows.fire_contributions[0].net_amount == "1200.00"
    assert parsed.rows.fire_events[0].amount == "-5000.00"
    assert parsed.rows.fire_events[0].source_loan_id == ("d7e8f9a0-b1c2-47d8-ae4a-c5d6e7f8091a")
    assert parsed.rows.fire_expense_exclusions[0].category == "Food"


def test_fire_files_are_required_in_1_7_and_absent_before_it() -> None:
    from assetboard.domain.archive import FIRE_CSV_FILES, required_files

    assert not any(name in required_files("1.6") for name in FIRE_CSV_FILES)
    assert all(name in required_files("1.7") for name in FIRE_CSV_FILES)
    for missing in FIRE_CSV_FILES:
        with ZipFile(io.BytesIO(_archive())) as source:
            files = {name: source.read(name) for name in source.namelist() if name != missing}
        output = io.BytesIO()
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        parsed, errors = parse_archive(output.getvalue())
        assert parsed is None
        assert errors == (ArchiveError("archive", "missing required members"),)


def _edit_csv(files: dict[str, bytes], name: str, row: int, column: str, value: str) -> None:
    rows = list(csv.reader(io.StringIO(files[name].decode(), newline="")))
    rows[row + 1][rows[0].index(column)] = value
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\r\n").writerows(rows)
    files[name] = output.getvalue().encode()


@pytest.mark.parametrize(
    ("name", "row", "column", "value"),
    [
        ("fire_scenarios.csv", 0, "income_mode", "linked"),
        ("fire_scenarios.csv", 0, "expenses_mode", "trailing_12m"),
        ("fire_scenarios.csv", 0, "portfolio_mode", "linked_investable"),
        ("fire_scenarios.csv", 0, "rule", "invalid"),
        ("fire_scenarios.csv", 0, "inflation_bp", "5001"),
        ("fire_scenarios.csv", 0, "inflation_bp", "-2001"),
        ("fire_scenarios.csv", 0, "birth_year", "2200"),
        ("fire_scenarios.csv", 0, "horizon_age", "0"),
        ("fire_scenarios.csv", 0, "stop_working_age", "40"),
        ("fire_scenarios.csv", 0, "currency", "EURO"),
        ("fire_scenarios.csv", 0, "id", "not-a-uuid"),
        ("fire_scenarios.csv", 0, "archived_at", "2026-09-30T20:00:00"),
        ("fire_scenarios.csv", 0, "income_manual", "3000.001"),
        ("fire_scenarios.csv", 0, "income_fallback", "1.00"),
        ("fire_scenarios.csv", 0, "swr_bp", ""),
        ("fire_scenarios.csv", 1, "swr_bp", "400"),
        ("fire_scenarios.csv", 1, "income_manual", "1.00"),
        ("fire_scenarios.csv", 1, "income_fallback", "-1.00"),
        ("fire_scenarios.csv", 1, "is_default", "true"),
        ("fire_contributions.csv", 0, "year", "2026"),
        ("fire_contributions.csv", 0, "net_amount", "12.345"),
        ("fire_contributions.csv", 0, "scenario", "Different"),
        ("fire_events.csv", 0, "kind", "transfer"),
        ("fire_events.csv", 0, "year", "2025"),
        ("fire_events.csv", 0, "amount", "12.345"),
        ("fire_events.csv", 0, "label", ""),
        ("fire_events.csv", 0, "source_loan_id", "not-a-uuid"),
        ("fire_expense_exclusions.csv", 0, "category", "Different"),
        ("fire_expense_exclusions.csv", 0, "category_id", "not-a-uuid"),
    ],
)
def test_reader_validates_fire_rows(name: str, row: int, column: str, value: str) -> None:
    files = _files()
    _edit_csv(files, name, row, column, value)
    parsed, errors = parse_archive(_rewrite(files))
    assert parsed is None and errors, (name, column, value)
    assert any(error.file == name for error in errors), (name, column, value)


@pytest.mark.parametrize(
    ("name", "row", "column", "value", "expected_column", "message"),
    [
        ("fire_scenarios.csv", 0, "income_manual", "", None, "missing manual FIRE amount"),
        (
            "fire_scenarios.csv",
            0,
            "income_fallback",
            "1.00",
            None,
            "fallback for a manual FIRE input",
        ),
        (
            "fire_scenarios.csv",
            1,
            "income_manual",
            "1.00",
            None,
            "manual value for a linked FIRE input",
        ),
        ("fire_scenarios.csv", 0, "swr_bp", "", None, "invalid FIRE withdrawal rate"),
        ("fire_scenarios.csv", 1, "swr_bp", "400", None, "invalid FIRE withdrawal rate"),
        ("fire_scenarios.csv", 1, "is_default", "true", None, "archived default scenario"),
        (
            "fire_scenarios.csv",
            0,
            "stop_working_age",
            "40",
            None,
            "invalid FIRE stop-working age",
        ),
        ("fire_scenarios.csv", 0, "horizon_age", "0", "horizon_age", "invalid age"),
        ("fire_scenarios.csv", 0, "currency", "EURO", "currency", "invalid currency"),
        (
            "fire_contributions.csv",
            0,
            "year",
            "2026",
            None,
            "FIRE contribution outside the plan window",
        ),
        ("fire_contributions.csv", 0, "net_amount", "12.345", "net_amount", "too many decimals"),
        (
            "fire_contributions.csv",
            0,
            "scenario",
            "Different",
            None,
            "FIRE scenario reference mismatch",
        ),
        (
            "fire_events.csv",
            0,
            "year",
            "2025",
            None,
            "FIRE event outside the plan window",
        ),
        ("fire_events.csv", 0, "amount", "12.345", "amount", "too many decimals"),
        ("fire_events.csv", 0, "label", "", "label", "required value missing"),
        (
            "fire_expense_exclusions.csv",
            0,
            "category",
            "Different",
            None,
            "FIRE exclusion category mismatch",
        ),
    ],
)
def test_reader_reports_the_fire_file_column_and_message(
    name: str, row: int, column: str, value: str, expected_column: str | None, message: str
) -> None:
    files = _files()
    _edit_csv(files, name, row, column, value)
    parsed, errors = parse_archive(_rewrite(files))
    assert parsed is None
    error = next(error for error in errors if error.message == message)
    assert error.file == name
    assert error.column == expected_column


def test_reader_accepts_a_fire_scenario_currency_different_from_the_base() -> None:
    files = _files()
    _edit_csv(files, "fire_scenarios.csv", 0, "currency", "USD")
    parsed, errors = parse_archive(_rewrite(files))
    assert errors == () and parsed is not None
    assert parsed.rows.fire_scenarios[0].currency == "USD"


def test_reader_accepts_an_unusable_exclusion_category_for_restore_handling() -> None:
    # A wrong-kind category is not an archive error: restore skips the exclusion with a warning
    # and falls linked expenses back, as the reader cannot repair the user's data.
    files = _files()
    _edit_csv(files, "categories.csv", 0, "kind", "income")
    _edit_csv(files, "transactions.csv", 0, "direction", "income")
    _edit_csv(files, "budget_lines.csv", 0, "kind", "income")
    parsed, errors = parse_archive(_rewrite(files))
    assert errors == () and parsed is not None
    assert parsed.rows.categories[0].kind == "income"

    # A non-regular treatment is likewise accepted; only the companion name must match.
    files = _files()
    _edit_csv(files, "categories.csv", 0, "treatment", "savings")
    parsed, errors = parse_archive(_rewrite(files))
    assert errors == () and parsed is not None

    files = _files()
    _edit_csv(files, "fire_expense_exclusions.csv", 0, "category", "Different")
    parsed, errors = parse_archive(_rewrite(files))
    assert parsed is None
    assert any(
        error.file == "fire_expense_exclusions.csv"
        and error.message == "FIRE exclusion category mismatch"
        for error in errors
    )


def test_reader_rejects_duplicate_fire_ids_names_defaults_and_children() -> None:
    files = _files()
    _edit_csv(files, "fire_scenarios.csv", 1, "id", "d1e2f3a4-b5c6-4d7e-8f90-1a2b3c4d5e6f")
    parsed, errors = parse_archive(_rewrite(files))
    assert parsed is None and any(error.message == "duplicate FIRE scenario id" for error in errors)

    files = _files()
    _edit_csv(files, "fire_scenarios.csv", 1, "archived_at", "")
    _edit_csv(files, "fire_scenarios.csv", 1, "name", "Lean FIRE")
    parsed, errors = parse_archive(_rewrite(files))
    assert parsed is None and any(
        error.message == "duplicate active FIRE scenario name" for error in errors
    )

    files = _files()
    _edit_csv(files, "fire_scenarios.csv", 1, "is_default", "true")
    _edit_csv(files, "fire_scenarios.csv", 1, "archived_at", "")
    parsed, errors = parse_archive(_rewrite(files))
    assert parsed is None and any(error.message == "duplicate default scenario" for error in errors)

    files = _files()
    _edit_csv(files, "fire_scenarios.csv", 1, "is_default", "true")
    parsed, errors = parse_archive(_rewrite(files))
    assert parsed is None and any(error.message == "archived default scenario" for error in errors)

    files = _files()
    header, row = files["fire_contributions.csv"].splitlines()[:2]
    files["fire_contributions.csv"] = header + b"\r\n" + row + b"\r\n" + row + b"\r\n"
    parsed, errors = parse_archive(_rewrite(files))
    assert parsed is None and any(
        error.message == "duplicate FIRE contribution id" for error in errors
    )

    files = _files()
    _edit_csv(files, "fire_expense_exclusions.csv", 0, "category_id", str(uuid4()))
    parsed, errors = parse_archive(_rewrite(files))
    assert errors == () and parsed is not None

    files = _files()
    header, row = files["fire_expense_exclusions.csv"].splitlines()[:2]
    files["fire_expense_exclusions.csv"] = header + b"\r\n" + row + b"\r\n" + row + b"\r\n"
    parsed, errors = parse_archive(_rewrite(files))
    assert parsed is None and any(
        error.message == "duplicate FIRE exclusion id" for error in errors
    )


def test_loan_files_are_optional_before_1_5_and_required_in_1_5() -> None:
    from assetboard.domain.archive import required_files

    assert "loans.csv" not in required_files("1.4")
    assert "installment_plans.csv" not in required_files("1.4")
    for name in (
        "loans.csv",
        "loan_rate_changes.csv",
        "reference_rates.csv",
        "reference_rate_values.csv",
        "loan_cost_components.csv",
        "loan_overpayments.csv",
        "loan_payments.csv",
        "loan_balance_checkpoints.csv",
        "loan_scenarios.csv",
        "installment_plans.csv",
    ):
        assert name in required_files("1.5")


def test_v1_5_requires_every_loan_file() -> None:
    with ZipFile(io.BytesIO(_archive())) as source:
        files = {name: source.read(name) for name in source.namelist() if name != "loans.csv"}
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    parsed, errors = parse_archive(output.getvalue())
    assert parsed is None
    assert errors == (ArchiveError("archive", "missing required members"),)


@pytest.mark.parametrize(
    ("key", "value", "valid"),
    [
        ("taxes_enabled", False, True),
        ("taxes_enabled", "yes", False),
        ("taxes_enabled", 1, False),
        ("upcoming_days", None, True),
        ("upcoming_days", 1, True),
        ("upcoming_days", 365, True),
        ("upcoming_days", 0, False),
        ("upcoming_days", 366, False),
        ("upcoming_days", True, False),
        ("upcoming_days", "14", False),
    ],
)
def test_reader_validates_the_1_1_workspace_keys(key: str, value: object, valid: bool) -> None:
    import json

    files = _files()
    workspace = json.loads(files["workspace.json"])
    workspace[key] = value
    files["workspace.json"] = json.dumps(workspace).encode()
    parsed, errors = parse_archive(_rewrite(files))
    assert (parsed is not None) is valid
    if not valid:
        assert errors == (ArchiveError("workspace.json", "invalid workspace settings"),)


def test_reader_checks_checksums() -> None:
    parsed, errors = parse_archive(GOLDEN.read_bytes())
    assert errors == ()
    assert parsed is not None
    assert parsed.manifest["base_currency"] == "EUR"

    with ZipFile(GOLDEN) as source:
        files = {name: source.read(name) for name in source.namelist()}
    files["categories.csv"] = files["categories.csv"].replace(b"Food", b"Meal")
    corrupted = io.BytesIO()
    with ZipFile(corrupted, "w", ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    parsed, errors = parse_archive(corrupted.getvalue())
    assert parsed is None
    assert any(error.message == "checksum mismatch" for error in errors)


def test_investment_and_loan_models_are_archived_in_1_5() -> None:
    for model in (
        "AssetClass",
        "RiskTargetPolicy",
        "Instrument",
        "InstrumentAllocation",
        "InvestmentTransaction",
        "BondTerms",
        "PricePoint",
        "FxRate",
        "PriceSourceConfig",
        "ReferenceRate",
        "ReferenceRateValue",
        "Loan",
        "LoanRateChange",
        "LoanCostComponent",
        "LoanOverpayment",
        "LoanPayment",
        "LoanBalanceCheckpoint",
        "LoanScenario",
        "InstallmentPlan",
    ):
        assert model in ARCHIVE_INCLUDED_MODELS
    assert "ProviderError" in ARCHIVE_EXCLUDED_MODELS


def test_fire_models_are_archived_in_1_7() -> None:
    for model in (
        "FireScenario",
        "FireContribution",
        "FireEvent",
        "FireExpenseExclusion",
    ):
        assert model in ARCHIVE_INCLUDED_MODELS
        assert model not in ARCHIVE_EXCLUDED_MODELS


def test_every_workspace_owned_model_is_archived_or_excluded() -> None:
    owned = {
        mapper.class_
        for mapper in Base.registry.mappers
        if issubclass(mapper.class_, WorkspaceOwned)
    }
    owned_names = {model.__name__ for model in owned}
    assert ARCHIVE_INCLUDED_MODELS | set(ARCHIVE_EXCLUDED_MODELS) == owned_names
    assert not (ARCHIVE_INCLUDED_MODELS & set(ARCHIVE_EXCLUDED_MODELS))


def _rewrite(members: dict[str, bytes], **manifest_changes: object) -> bytes:
    """Re-sign intentional edits so tests exercise validation beyond the checksum."""
    import json

    from assetboard.domain.archive import manifest_bytes

    manifest = json.loads(
        manifest_bytes(
            members,
            app_name="AssetBoard",
            app_version="0.4.0",
            exported_at=EXPORTED_AT,
            base_currency="EUR",
        )
    )
    manifest.update(manifest_changes)
    members = {"manifest.json": json.dumps(manifest).encode(), **members}
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return output.getvalue()


def _files() -> dict[str, bytes]:
    from assetboard.domain.archive import build_files

    return build_files(_rows())


def test_reader_checks_counts_currency_and_versions() -> None:
    import json

    from assetboard.domain.archive import manifest_bytes

    files = _files()
    manifest = json.loads(
        manifest_bytes(
            files,
            app_name="AssetBoard",
            app_version="0.4.0",
            exported_at=EXPORTED_AT,
            base_currency="EUR",
        )
    )
    manifest["files"][1]["rows"] += 1
    parsed, errors = parse_archive(_rewrite(files, files=manifest["files"]))
    assert parsed is None and any(e.message == "row count mismatch" for e in errors)
    for changes, message in [
        ({"base_currency": "PLN"}, "base currency differs from manifest"),
        ({"format_version": "2.0"}, "made by a newer version of the app"),
        ({"format_version": "0.9"}, "unsupported archive version"),
    ]:
        parsed, errors = parse_archive(_rewrite(files, **changes))
        assert parsed is None and any(e.message == message for e in errors)


def test_reader_accepts_reordered_columns_bom_defaults_and_future_minor() -> None:
    files = _files()
    files["categories.csv"] = (
        "\ufeffname,id,kind,future\nFood,2b0e8c1e-5f7a-4c6e-9a51-3f1d2c7b9e01,expense,x\n"
    ).encode()
    parsed, errors = parse_archive(_rewrite(files, format_version="1.9", future=True))
    assert not errors and parsed is not None
    assert parsed.rows.categories[0].treatment == "regular"
    assert parsed.rows.categories[0].sort_order == 0
    assert sorted(w.message for w in parsed.warnings) == [
        "newer minor version: 1.9",
        "unknown column: future",
        "unknown key: future",
    ]


def test_reader_validates_fields_and_references() -> None:
    cases = [
        ("transactions.csv", b"4.20", b"4.201"),
        ("transactions.csv", b"4.20", b"-4.20"),
        ("transactions.csv", b"4.20", b"0.00"),
        ("transactions.csv", b"2026-09-02", b"2026-02-30"),
        ("transactions.csv", b"Food", b"Missing"),
        ("transactions.csv", b"expense", b"income"),
        ("transactions.csv", b"EUR", b"PLN"),
        ("transactions.csv", b"2026-09-03T07:12:44Z", b"2026-09-03T07:12:44"),
        ("categories.csv", b"regular", b"invalid"),
        ("categories.csv", b"#4f9d69", b"red"),
        ("categories.csv", b",0,", b",-1,"),
        ("categories.csv", b"2b0e8c1e-5f7a-4c6e-9a51-3f1d2c7b9e01", b"invalid"),
    ]
    for file, old, new in cases:
        files = _files()
        files[file] = files[file].replace(old, new)
        parsed, errors = parse_archive(_rewrite(files))
        assert parsed is None and errors, (file, new)


def test_reader_normalizes_timestamps_and_accepts_trailing_decimal_zeros() -> None:
    files = _files()
    files["transactions.csv"] = (
        files["transactions.csv"]
        .replace(b"4.20", b"4.2000")
        .replace(b"2026-09-03T07:12:44Z", b"2026-09-03T09:12:44+02:00")
    )
    parsed, errors = parse_archive(_rewrite(files))
    assert not errors and parsed is not None
    assert parsed.rows.transactions[0].deleted_at == "2026-09-03T07:12:44Z"


def _replace_first_line(files: dict[str, bytes], name: str, old: bytes, new: bytes) -> None:
    header, *rows = files[name].splitlines(keepends=True)
    for index, row in enumerate(rows):
        if old in row:
            rows[index] = row.replace(old, new, 1)
            break
    else:  # pragma: no cover - the fixture always contains the marker
        raise AssertionError(f"{old!r} not found in {name}")
    files[name] = header + b"".join(rows)


def test_reader_checks_net_worth_references_and_snapshot_rules() -> None:
    files = _files()
    _replace_first_line(files, "networth_items.csv", b",asset,Vehicles,", b",liability,Vehicles,")
    parsed, errors = parse_archive(_rewrite(files))
    assert parsed is None
    assert any(error.message == "net-worth category reference mismatch" for error in errors)

    files = _files()
    _replace_first_line(files, "item_valuations.csv", b",EUR,Bought,", b",USD,Bought,")
    parsed, errors = parse_archive(_rewrite(files))
    assert parsed is None
    assert any(error.message == "item valuation currency differs" for error in errors)

    files = _files()
    _replace_first_line(files, "item_valuations.csv", b",EUR,Bought,", b",ZZZ,Bought,")
    parsed, errors = parse_archive(_rewrite(files))
    assert parsed is None
    assert any(error.message == "invalid currency" for error in errors)

    files = _files()
    _replace_first_line(files, "networth_snapshots.csv", b",auto", b",other")
    parsed, errors = parse_archive(_rewrite(files))
    assert parsed is None
    assert any(error.message == "invalid snapshot source" for error in errors)

    files = _files()
    files["networth_snapshots.csv"] += files["networth_snapshots.csv"].splitlines(keepends=True)[1]
    parsed, errors = parse_archive(_rewrite(files))
    assert parsed is None
    assert any(error.message == "duplicate snapshot date and source" for error in errors)


def test_reader_refuses_unsafe_and_invalid_archives() -> None:
    for data in (b"", b"not a zip"):
        assert parse_archive(data)[1]
    files = _files()
    files["../secret"] = b"x"
    assert parse_archive(_rewrite(files))[1] == (ArchiveError("archive", "unsafe member"),)
    files = _files()
    files["months.csv"] += files["months.csv"].splitlines(keepends=True)[1]
    assert parse_archive(_rewrite(files))[1] == (ArchiveError("months.csv", "duplicate month"),)


def test_filtered_csv_columns_match_archive() -> None:
    from assetboard.domain.archive import CSV_COLUMNS
    from assetboard.domain.ledger_csv import TRANSACTION_EXPORT_COLUMNS

    assert CSV_COLUMNS["transactions.csv"] == (*TRANSACTION_EXPORT_COLUMNS, "deleted_at")


def test_zip_metadata_and_lossless_text() -> None:
    from dataclasses import replace

    rows = _rows()
    text = '=SUM(1,2)\r\nZażółć "gęślą"'
    rows = replace(rows, transactions=(replace(rows.transactions[0], description=text),))
    data = write_archive(
        rows,
        app_name="AssetBoard",
        app_version="0.4.0",
        exported_at=EXPORTED_AT,
        base_currency="EUR",
    )
    parsed, errors = parse_archive(data)
    assert not errors and parsed is not None
    assert parsed.rows.transactions[0].description == text
    with ZipFile(io.BytesIO(data)) as archive:
        for member in archive.infolist():
            assert member.date_time == (2026, 10, 18, 14, 30, 0)
            assert member.compress_type == ZIP_DEFLATED
            assert member.extra == b""


@given(
    st.decimals(
        min_value=Decimal("0"),
        max_value=Decimal("99999999999999999999.999999999999999999"),
        places=18,
    )
)
def test_decimal_text_is_plain_lossless_and_without_trailing_zeros(value: Decimal) -> None:
    text = decimal_text(value)
    assert "e" not in text.lower()
    assert Decimal(text) == value
    if "." in text:
        assert not text.endswith("0")


@pytest.mark.parametrize(
    ("change", "message", "file"),
    [
        (
            lambda rows: replace(
                rows,
                instrument_allocations=(
                    replace(rows.instrument_allocations[0], weight_bp=9_000),
                    *rows.instrument_allocations[1:],
                ),
            ),
            "allocation weights must total 100%",
            "instrument_allocations.csv",
        ),
        (
            lambda rows: replace(
                rows,
                investment_transactions=(
                    replace(rows.investment_transactions[0], quote_currency="USD"),
                ),
            ),
            "instrument reference mismatch",
            "investment_transactions.csv",
        ),
        (
            lambda rows: replace(
                rows,
                investment_transactions=(
                    replace(
                        rows.investment_transactions[0],
                        settlement_currency="EUR",
                        settlement_unit_price="1",
                        settlement_fee="1.00",
                    ),
                ),
            ),
            "settlement is not allowed here",
            "investment_transactions.csv",
        ),
        (
            lambda rows: replace(
                rows,
                investment_transactions=(
                    replace(
                        rows.investment_transactions[0],
                        type="dividend",
                        quantity="2",
                        unit_price="10",
                        fx_to_base="1",
                    ),
                ),
            ),
            "invalid dividend encoding",
            "investment_transactions.csv",
        ),
        (
            lambda rows: replace(
                rows,
                investment_transactions=(
                    replace(
                        rows.investment_transactions[0],
                        type="dividend",
                        quantity="1",
                        unit_price="10",
                        fee="10.01",
                        fx_to_base="1",
                    ),
                ),
            ),
            "withholding exceeds the gross amount",
            "investment_transactions.csv",
        ),
        (
            lambda rows: replace(
                rows,
                investment_transactions=(
                    replace(
                        rows.investment_transactions[0],
                        type="dividend",
                        quantity="1",
                        unit_price="10",
                        fx_to_base="2",
                    ),
                ),
            ),
            "invalid dividend FX",
            "investment_transactions.csv",
        ),
        (
            lambda rows: replace(
                rows,
                investment_transactions=(
                    replace(
                        rows.investment_transactions[0],
                        type="dividend",
                        quantity="1",
                        unit_price="10",
                        fx_to_base="1",
                        settlement_currency="EUR",
                        settlement_unit_price="1",
                        settlement_fee="0.00",
                    ),
                ),
            ),
            "dividend has settlement fields",
            "investment_transactions.csv",
        ),
        (
            lambda rows: replace(
                rows,
                investment_transactions=(
                    replace(
                        rows.investment_transactions[0],
                        type="split",
                        quantity="2",
                        unit_price="2",
                        fee="0",
                        fx_to_base="",
                    ),
                ),
            ),
            "invalid split encoding",
            "investment_transactions.csv",
        ),
        (
            lambda rows: replace(
                rows,
                investment_transactions=(
                    replace(
                        rows.investment_transactions[0],
                        type="split",
                        quantity="2",
                        unit_price="1",
                        fee="1.00",
                        fx_to_base="",
                    ),
                ),
            ),
            "split fee must be zero",
            "investment_transactions.csv",
        ),
        (
            lambda rows: replace(
                rows,
                investment_transactions=(
                    replace(
                        rows.investment_transactions[0],
                        type="split",
                        quantity="2",
                        unit_price="1",
                        fee="0",
                        fx_to_base="1",
                    ),
                ),
            ),
            "split must not have FX",
            "investment_transactions.csv",
        ),
        (
            lambda rows: replace(
                rows,
                investment_transactions=(
                    replace(
                        rows.investment_transactions[0],
                        instrument_id=rows.instruments[1].id,
                        instrument=rows.instruments[1].name,
                        type="split",
                        quantity="2",
                        unit_price="1",
                        fee="0",
                        fx_to_base="",
                    ),
                ),
            ),
            "split is not allowed for this instrument",
            "investment_transactions.csv",
        ),
        (
            lambda rows: replace(
                rows,
                investment_transactions=(
                    replace(
                        rows.investment_transactions[0],
                        income_transaction_id=str(uuid4()),
                    ),
                ),
            ),
            "income link is only for dividends",
            "investment_transactions.csv",
        ),
        (
            lambda rows: replace(
                rows,
                asset_classes=(
                    replace(rows.asset_classes[0], risk_tier="risky"),
                    *rows.asset_classes[1:],
                ),
            ),
            "invalid risk tier",
            "asset_classes.csv",
        ),
        (
            lambda rows: replace(
                rows,
                price_source_configs=(
                    *rows.price_source_configs,
                    replace(rows.price_source_configs[0], id=str(uuid4())),
                ),
            ),
            "duplicate provider config",
            "price_source_configs.csv",
        ),
        (
            lambda rows: replace(
                rows,
                bond_terms=(
                    replace(
                        rows.bond_terms[0],
                        rate_schedule=(
                            '[{"annual_rate_bp":500,"from_date":"2026-01-01"},'
                            '{"annual_rate_bp":600,"from_date":"2026-01-01"}]'
                        ),
                    ),
                ),
            ),
            "invalid rate schedule",
            "bond_terms.csv",
        ),
    ],
)
def test_reader_rejects_invalid_investment_rows(
    change: Callable[[ArchiveRows], ArchiveRows], message: str, file: str
) -> None:
    rows = change(_rows())
    parsed, errors = parse_archive(_archive(rows))
    assert parsed is None
    assert any(error.message == message and error.file == file for error in errors), errors


@pytest.mark.parametrize(
    ("change", "message", "file"),
    [
        (
            lambda rows: replace(rows, loans=(rows.loans[0], rows.loans[0], *rows.loans[1:])),
            "duplicate loan id",
            "loans.csv",
        ),
        (
            lambda rows: replace(
                rows,
                loans=(
                    replace(
                        rows.loans[0],
                        reference_rate_id=None,
                        reference_rate="",
                    ),
                    *rows.loans[1:],
                ),
            ),
            "invalid loan rate configuration",
            "loans.csv",
        ),
        (
            lambda rows: replace(
                rows,
                loans=(
                    replace(rows.loans[0], property_item="Other"),
                    *rows.loans[1:],
                ),
            ),
            "loan property item mismatch",
            "loans.csv",
        ),
        (
            lambda rows: replace(
                rows,
                loans=(
                    rows.loans[0],
                    replace(rows.loans[1], term_months=0),
                    rows.loans[2],
                ),
            ),
            "invalid loan term",
            "loans.csv",
        ),
        (
            lambda rows: replace(
                rows,
                reference_rates=(
                    *rows.reference_rates,
                    replace(rows.reference_rates[0], id=str(uuid4())),
                ),
            ),
            "duplicate reference rate name",
            "reference_rates.csv",
        ),
        (
            lambda rows: replace(
                rows,
                reference_rate_values=(
                    *rows.reference_rate_values,
                    replace(rows.reference_rate_values[0], id=str(uuid4())),
                ),
            ),
            "duplicate reference rate value",
            "reference_rate_values.csv",
        ),
        (
            lambda rows: replace(
                rows,
                reference_rate_values=(
                    replace(rows.reference_rate_values[0], reference_rate="EURIBOR"),
                ),
            ),
            "reference rate value reference mismatch",
            "reference_rate_values.csv",
        ),
        (
            lambda rows: replace(
                rows,
                loan_rate_changes=(
                    *rows.loan_rate_changes,
                    replace(rows.loan_rate_changes[0], id=str(uuid4())),
                ),
            ),
            "duplicate loan rate change",
            "loan_rate_changes.csv",
        ),
        (
            lambda rows: replace(
                rows,
                loan_cost_components=(
                    replace(
                        rows.loan_cost_components[0],
                        rate_bp=None,
                        fixed_amount="",
                    ),
                ),
            ),
            "invalid cost component",
            "loan_cost_components.csv",
        ),
        (
            lambda rows: replace(
                rows,
                loan_payments=(replace(rows.loan_payments[0], total="2000.01"),),
            ),
            "loan payment components differ",
            "loan_payments.csv",
        ),
        (
            lambda rows: replace(
                rows,
                loan_payments=(replace(rows.loan_payments[0], transaction_id=str(uuid4())),),
            ),
            "loan payment transaction missing",
            "loan_payments.csv",
        ),
        (
            lambda rows: replace(
                rows,
                installment_plans=(
                    *rows.installment_plans,
                    replace(rows.installment_plans[0], id=str(uuid4())),
                ),
            ),
            "duplicate installment plan loan",
            "installment_plans.csv",
        ),
        (
            lambda rows: replace(
                rows,
                installment_plans=(
                    replace(
                        rows.installment_plans[0],
                        loan_id=rows.loans[1].id,
                        loan=rows.loans[1].name,
                    ),
                ),
            ),
            "installment plan must reference an installment-plan loan",
            "installment_plans.csv",
        ),
    ],
)
def test_reader_rejects_invalid_loan_rows(
    change: Callable[[ArchiveRows], ArchiveRows], message: str, file: str
) -> None:
    parsed, errors = parse_archive(_archive(change(_rows())))
    assert parsed is None
    assert any(error.message == message and error.file == file for error in errors), errors


def test_reader_accepts_dividends_splits_and_an_income_link() -> None:
    rows = _rows()
    template = rows.investment_transactions[0]
    income_id = "5c7d8e9f-0a1b-4c2d-9e3f-4a5b6c7d8e02"
    dividend = replace(
        template,
        type="dividend",
        quantity="1",
        unit_price="10",
        fee="1.00",
        fx_to_base="1",
        income_transaction_id=income_id,
    )
    split = replace(
        template,
        id=str(uuid4()),
        type="split",
        quantity="2",
        unit_price="1",
        fee="0.00",
        fx_to_base="",
        income_transaction_id=None,
    )
    parsed, errors = parse_archive(
        _archive(replace(rows, investment_transactions=(dividend, split)))
    )
    assert errors == ()
    assert parsed is not None
    assert parsed.rows.investment_transactions[0].income_transaction_id == income_id
    assert parsed.rows.investment_transactions[1].type == "split"


def test_reader_rejects_an_invalid_risk_policy() -> None:
    rows = _rows()
    workspace = dict(rows.workspace)
    workspace["risk_target_policy"] = {
        "mode": "fixed",
        "safe_bp": 5000,
        "growth_bp": 4000,
        "speculative_bp": 500,
        "tolerance_bp": 500,
    }
    parsed, errors = parse_archive(_archive(replace(rows, workspace=workspace)))
    assert parsed is None
    assert errors == (ArchiveError("workspace.json", "invalid workspace settings"),)


def test_decimal_text_examples() -> None:
    assert decimal_text(Decimal("0.000")) == "0"
    assert decimal_text(Decimal("12.3400")) == "12.34"
    assert decimal_text(Decimal("100.5")) == "100.5"
    assert decimal_text(Decimal("0.00924")) == "0.00924"


def test_reader_warns_about_unknown_files() -> None:
    import hashlib
    import json

    from assetboard.domain.archive import manifest_bytes

    members = _files()
    manifest = json.loads(
        manifest_bytes(
            members,
            app_name="AssetBoard",
            app_version="0.4.0",
            exported_at=EXPORTED_AT,
            base_currency="EUR",
        )
    )
    extra = b'{"new":true}'
    members["future.json"] = extra
    manifest["files"].append(
        {
            "path": "future.json",
            "bytes": len(extra),
            "rows": None,
            "sha256": hashlib.sha256(extra).hexdigest(),
        }
    )
    # Build directly: the v1 writer intentionally only writes the current files.
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for name, data in members.items():
            archive.writestr(name, data)
    parsed, errors = parse_archive(output.getvalue())
    assert not errors and parsed is not None
    assert parsed.warnings[0].file == "future.json"


@given(st.text(alphabet='abcąćęłńóśźż0123=+-@,;"\r\n ', max_size=200))
def test_csv_text_round_trip(description: str) -> None:
    from dataclasses import replace

    rows = _rows()
    rows = replace(rows, transactions=(replace(rows.transactions[0], description=description),))
    encoded = write_archive(
        rows,
        app_name="AssetBoard",
        app_version="0.4.0",
        exported_at=EXPORTED_AT,
        base_currency="EUR",
    )
    parsed, errors = parse_archive(encoded)
    assert not errors and parsed is not None
    assert parsed.rows.transactions[0].description == description


def _zip(members: list[tuple[ZipInfo | str, bytes]], *, compression: int = ZIP_DEFLATED) -> bytes:
    output = io.BytesIO()
    with ZipFile(output, "w", compression) as archive:
        for name, data in members:
            archive.writestr(name, data)
    return output.getvalue()


def _signed_members() -> list[tuple[ZipInfo | str, bytes]]:
    with ZipFile(io.BytesIO(_rewrite(_files()))) as archive:
        return [(name, archive.read(name)) for name in archive.namelist()]


def _special(name: str, *, mode: int = 0) -> ZipInfo:
    info = ZipInfo(name)
    info.external_attr = mode << 16
    return info


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ((_special("link", mode=0o120777), b"/etc/passwd"), "unsafe member"),
        ((_special("dir", mode=0o040755), b""), "unsafe member"),
        (("C:evil", b"x"), "unsafe member"),
        (("sub\\evil", b"x"), "unsafe member"),
        (("zeros", b"\0" * (2 * 1024 * 1024)), "uncompressed size or ratio limit"),
    ],
    ids=["symlink", "directory", "drive", "backslash", "ratio"],
)
def test_reader_refuses_each_unsafe_member(
    extra: tuple[ZipInfo | str, bytes], message: str
) -> None:
    assert parse_archive(_zip([*_signed_members(), extra]))[1] == (
        ArchiveError("archive", message),
    )


def test_reader_refuses_an_encrypted_member() -> None:
    data = bytearray(_zip([*_signed_members(), ("secret", b"x")]))
    # zipfile cannot write encrypted members: set bit 0 of the general-purpose flags in the
    # central directory entry of the last member (signature PK\x01\x02, flags at offset 8).
    entry = data.rindex(b"PK\x01\x02")
    data[entry + 8] |= 1
    assert parse_archive(bytes(data))[1] == (ArchiveError("archive", "unsafe member"),)


def test_reader_refuses_other_methods_duplicates_and_too_many_members() -> None:
    bz = _zip([*_signed_members(), ("notes.txt", b"x")], compression=ZIP_BZIP2)
    assert parse_archive(bz)[1] == (ArchiveError("archive", "unsafe member"),)
    members = _signed_members()
    with pytest.warns(UserWarning, match="Duplicate name"):
        duplicated = _zip([*members, members[-1]])
    assert parse_archive(duplicated)[1] == (ArchiveError("archive", "duplicate member names"),)
    many = _zip([*members, *((f"extra{n}.txt", b"x") for n in range(60))])
    assert parse_archive(many)[1] == (ArchiveError("archive", "too many members"),)


@pytest.mark.parametrize("limit", ["MAX_MEMBER_BYTES", "MAX_TOTAL_BYTES"])
def test_reader_stops_at_the_size_limits(monkeypatch: pytest.MonkeyPatch, limit: str) -> None:
    import assetboard.domain.archive as archive

    monkeypatch.setattr(archive, limit, 100)
    data = _zip(_signed_members(), compression=ZIP_STORED)
    assert parse_archive(data)[1] == (ArchiveError("archive", "uncompressed size or ratio limit"),)


def test_reader_warns_about_unlisted_files() -> None:
    parsed, errors = parse_archive(_zip([*_signed_members(), ("README.txt", b"notes")]))
    assert not errors and parsed is not None
    assert parsed.warnings == (ArchiveError("README.txt", "unknown file ignored"),)


def test_reader_refuses_listed_files_that_are_missing() -> None:
    import json

    members = dict(_signed_members())
    manifest = json.loads(members["manifest.json"])
    manifest["files"].append({"path": "gone.csv", "sha256": "0", "rows": 0, "bytes": 0})
    members["manifest.json"] = json.dumps(manifest).encode()
    errors = parse_archive(_zip(list(members.items())))[1]
    assert errors == (ArchiveError("manifest.json", "invalid file list"),)


@pytest.mark.parametrize(
    ("file", "old", "new"),
    [
        ("categories.csv", b",0,", b"," + b"9" * 5000 + b","),
        ("categories.csv", b",0,", b",1234567890,"),
        ("transactions.csv", b"2026-09-03T07:12:44Z", b"0001-01-01T00:00:00+01:00"),
        ("transactions.csv", b"2026-09-03T07:12:44Z", b"9999-12-31T23:59:59-01:00"),
        ("workspace.json", b'"auto_copy_plan"', b'"x":' + b"9" * 5000 + b',"auto_copy_plan"'),
        ("workspace.json", b"{", b'{"x":' + b"[" * 100_000 + b"]" * 100_000 + b","),
    ],
    ids=["huge-sort", "long-sort", "early-ts", "late-ts", "huge-int", "deep-json"],
)
def test_reader_reports_hostile_values_as_errors(file: str, old: bytes, new: bytes) -> None:
    files = _files()
    assert old in files[file]
    files[file] = files[file].replace(old, new, 1)
    parsed, errors = parse_archive(_rewrite(files))
    assert parsed is None and errors


@pytest.mark.parametrize(
    "manifest",
    [
        b"[" * 100_000 + b"]" * 100_000,
        b'{"format":' + b"1" * 5000 + b"}",
        b'{"format":"workspace-archive","format_version":"1.0",'
        b'"exported_at":"9999-12-31T23:59:59-01:00"}',
    ],
    ids=["deep", "huge-int", "late-ts"],
)
def test_reader_reports_a_hostile_manifest_as_an_error(manifest: bytes) -> None:
    members = dict(_signed_members())
    members["manifest.json"] = manifest
    parsed, errors = parse_archive(_zip(list(members.items())))
    assert parsed is None and errors and errors[0].file == "manifest.json"


def test_reader_caps_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import assetboard.domain.archive as archive

    monkeypatch.setattr(archive, "MAX_ERRORS", 5)
    files = _files()
    files["months.csv"] = b"month\r\n" + b"bad\r\n" * 50
    parsed, errors = parse_archive(_rewrite(files))
    assert parsed is None
    assert len(errors) == 6
    assert errors[-1] == ArchiveError("archive", "too many errors; later rows were not checked")


def test_reader_caps_data_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    import assetboard.domain.archive as archive

    monkeypatch.setattr(archive, "MAX_DATA_ROWS", 38)
    assert parse_archive(_archive())[1] == ()
    monkeypatch.setattr(archive, "MAX_DATA_ROWS", 37)
    assert parse_archive(_archive())[1] == (ArchiveError("archive", "too many data rows"),)
