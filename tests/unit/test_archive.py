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
    FxRateRow,
    InstrumentAllocationRow,
    InstrumentRow,
    InvestmentTransactionRow,
    ItemValuationRow,
    MonthRow,
    NetWorthCategoryRow,
    NetWorthItemRow,
    NetWorthSnapshotRow,
    PricePointRow,
    PriceSourceConfigRow,
    RiskTargetPolicyRow,
    TransactionRow,
    decimal_text,
    parse_archive,
    write_archive,
)
from assetboard.services.archive import ARCHIVE_EXCLUDED_MODELS, ARCHIVE_INCLUDED_MODELS

FIXTURES = Path(__file__).parents[1] / "fixtures"
GOLDEN = FIXTURES / "workspace-archive-v1.4.zip"
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
    parsed, errors = parse_archive(GOLDEN.read_bytes())
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


def test_investment_models_are_archived_in_1_4() -> None:
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
    ):
        assert model in ARCHIVE_INCLUDED_MODELS
    assert "ProviderError" in ARCHIVE_EXCLUDED_MODELS


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

    monkeypatch.setattr(archive, "MAX_DATA_ROWS", 21)
    assert parse_archive(_archive())[1] == ()
    monkeypatch.setattr(archive, "MAX_DATA_ROWS", 20)
    assert parse_archive(_archive())[1] == (ArchiveError("archive", "too many data rows"),)
