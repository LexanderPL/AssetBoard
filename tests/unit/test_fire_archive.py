"""F8.8 archive 2.0: one plan, events, 1.8 upgrade and rollback."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import pytest
from sqlalchemy import select

from assetboard.db.models.identity import AuditEvent
from assetboard.db.repositories import FireEventRepository, FireScenarioRepository
from assetboard.domain.archive import (
    FILE_ORDER,
    ArchiveError,
    ArchiveRows,
    FireEventRow,
    FireLoanSettingRow,
    FirePlanRow,
    LoanRateChangeRow,
    LoanRow,
    parse_archive,
    write_archive,
)
from assetboard.domain.fire import EventKind, ProjectionEvent, ProjectionInputs, Rule, project
from assetboard.domain.money import Money
from assetboard.services import fire_scenarios
from assetboard.services.archive import export_workspace, workspace_is_empty
from assetboard.services.fire_projection import scenario_results
from assetboard.services.import_csv import default_import_adapters
from assetboard.services.imports import (
    ImportAdapterRegistry,
    apply_import,
    plan_import,
    rollback_import,
    upload_import,
)
from tests.tenant.workspace_db import WorkspaceDb, workspace_db

EXPORTED_AT = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parents[1] / "fixtures"
GOLDEN_V1_8 = FIXTURES / "workspace-archive-v1.8.zip"
GOLDEN_V1_7 = FIXTURES / "workspace-archive-v1.7.zip"
GOLDEN_V2_0 = FIXTURES / "workspace-archive-v2.0.zip"
PLAN = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
EVENT = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
OVERRIDE = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
LOAN = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
LOAN_RATE = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
LOAN_SETTING = UUID("11111111-1111-4111-8111-111111111111")


def _workspace() -> dict[str, object]:
    return {
        "name": "Home",
        "base_currency": "EUR",
        "country_code": "PL",
        "timezone": "Europe/Warsaw",
        "locale": "en",
        "auto_copy_plan": False,
        "taxes_enabled": True,
        "upcoming_days": 30,
        "investment_cost_basis": "fifo",
    }


def _plan_row(*, needs_review: str = "true") -> FirePlanRow:
    return FirePlanRow(
        str(PLAN),
        "EUR",
        1985,
        250,
        500,
        300,
        55,
        90,
        "swr",
        400,
        "3000.00",
        "2000.00",
        "100000.00",
        needs_review,
    )


def _rows(
    *,
    plan: FirePlanRow | None = None,
    events: tuple[FireEventRow, ...] | None = None,
) -> ArchiveRows:
    return ArchiveRows(
        workspace=_workspace(),
        categories=(),
        budget_lines=(),
        transactions=(),
        months=(),
        fire_plan=_plan_row() if plan is None else plan,
        fire_events=(
            (
                FireEventRow(str(UUID(int=9)), 2020, "cash", "100.00", "Old bonus"),
                FireEventRow(str(OVERRIDE), 2027, "saving_override", "-1200.00", "Sabbatical"),
                FireEventRow(str(EVENT), 2030, "cash", "-5000.00", "Renovation"),
            )
            if events is None
            else events
        ),
    )


def _archive(rows: ArchiveRows) -> bytes:
    return write_archive(
        rows,
        app_name="AssetBoard",
        app_version="test",
        exported_at=EXPORTED_AT,
        base_currency=str(rows.workspace["base_currency"]),
    )


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    yield from workspace_db(tmp_path)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    return path


def _adapters() -> ImportAdapterRegistry:
    return ImportAdapterRegistry(default_import_adapters())


def _target(db: WorkspaceDb) -> tuple[UUID, Any]:
    workspace_id = db.add_workspace("Restored")
    db.add_member("own", "owner", workspace_id=workspace_id)
    return workspace_id, db.ctx("own", "owner", workspace_id=workspace_id)


def _restore(
    db: WorkspaceDb,
    data_dir: Path,
    rows: ArchiveRows,
    *,
    mode: Literal["restore", "merge"] = "restore",
) -> tuple[UUID, Any, UUID, dict[str, Any], dict[str, Any]]:
    workspace_id, ctx = _target(db)
    content = _archive(rows)
    batch_id = upload_import(
        db.factory,
        ctx,
        data_dir=data_dir,
        filename="archive.zip",
        content=content,
        options={},
        mode=mode,
    )
    report = plan_import(db.factory, ctx, data_dir, batch_id, _adapters())
    counts = apply_import(db.factory, ctx, data_dir, batch_id, _adapters())
    return workspace_id, ctx, batch_id, report, counts


def _restore_bytes(
    db: WorkspaceDb,
    data_dir: Path,
    content: bytes,
    *,
    mode: Literal["restore", "merge"] = "restore",
) -> tuple[UUID, Any, UUID, dict[str, Any], dict[str, Any]]:
    workspace_id, ctx = _target(db)
    batch_id = upload_import(
        db.factory,
        ctx,
        data_dir=data_dir,
        filename="archive.zip",
        content=content,
        options={},
        mode=mode,
    )
    report = plan_import(db.factory, ctx, data_dir, batch_id, _adapters())
    counts = apply_import(db.factory, ctx, data_dir, batch_id, _adapters())
    return workspace_id, ctx, batch_id, report, counts


def _data_files(content: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return {name: archive.read(name) for name in FILE_ORDER if name in archive.namelist()}


def test_2_0_round_trip_keeps_plan_events_and_projection(db: WorkspaceDb, data_dir: Path) -> None:
    rows = _rows()
    original = _archive(rows)
    workspace_id, ctx, _batch_id, report, counts = _restore(db, data_dir, rows)
    assert report["counts"]["fire_plan_created"] == 1
    assert report["counts"]["fire_events_created"] == 3
    assert counts

    with db.factory() as session:
        plan = FireScenarioRepository(session, ctx).live()
        assert plan is not None
        assert plan.import_key == f"id:{PLAN}"
        assert plan.needs_review is True
        events = FireEventRepository(session, ctx).for_plan(plan.id)
        assert [(row.year, row.kind) for row in events] == [
            (2020, "cash"),
            (2027, "saving_override"),
            (2030, "cash"),
        ]
        assert events[1].import_key == f"id:{OVERRIDE}"

    results = scenario_results(db.factory, ctx)
    assert results.projection is not None
    assert results.detail.plan.needs_review is True
    assert results.detail.archived_events[0].year == 2020
    expected = project(
        ProjectionInputs(
            currency="EUR",
            birth_year=1985,
            start_year=2026,
            horizon_age=90,
            stop_working_age=55,
            inflation_bp=250,
            real_return_bp=500,
            income_growth_bp=300,
            rule=Rule.SWR,
            swr_bp=400,
            start_income=Money(300_000, "EUR"),
            start_expenses=Money(200_000, "EUR"),
            start_portfolio=Money(10_000_000, "EUR"),
            events=(
                ProjectionEvent(2027, EventKind.SAVING_OVERRIDE, Money(-120_000, "EUR")),
                ProjectionEvent(2030, EventKind.CASH, Money(-500_000, "EUR")),
            ),
        )
    )
    assert results.projection == expected

    content = export_workspace(db.factory, ctx).content
    parsed, errors = parse_archive(content)
    assert errors == () and parsed is not None
    assert parsed.manifest["format_version"] == "2.3"
    assert _data_files(original)["fire_plan.csv"] == _data_files(content)["fire_plan.csv"]
    assert _data_files(original)["fire_events.csv"] == _data_files(content)["fire_events.csv"]


def test_a_horizon_rule_plan_round_trips_without_a_withdrawal_rate(
    db: WorkspaceDb, data_dir: Path
) -> None:
    rows = replace(
        _rows(),
        fire_plan=replace(_plan_row(), rule="lasts_to_horizon", swr_bp=""),
    )
    _workspace_id, ctx, _batch_id, report, _counts = _restore(db, data_dir, rows)
    assert report["counts"]["fire_plan_created"] == 1
    with db.factory() as session:
        plan = FireScenarioRepository(session, ctx).live()
        assert plan is not None
        assert plan.rule == "lasts_to_horizon"
        assert plan.swr_bp is None
    content = export_workspace(db.factory, ctx).content
    parsed, errors = parse_archive(content)
    assert errors == () and parsed is not None
    assert parsed.rows.fire_plan is not None
    assert parsed.rows.fire_plan.rule == "lasts_to_horizon"
    assert parsed.rows.fire_plan.swr_bp is None


def test_a_restored_plan_keeps_its_currency_snapshot_and_blocks_on_a_mismatch(
    db: WorkspaceDb, data_dir: Path
) -> None:
    rows = replace(_rows(), fire_plan=replace(_plan_row(needs_review="false"), currency="USD"))
    _workspace_id, ctx, _batch_id, report, _counts = _restore(db, data_dir, rows)
    assert report["counts"]["fire_plan_created"] == 1
    with db.factory() as session:
        plan = FireScenarioRepository(session, ctx).live()
        assert plan is not None
        assert plan.currency == "USD"
        assert plan.needs_review is False
    results = scenario_results(db.factory, ctx)
    assert results.projection is None
    assert results.blocks[0].code == "plan_currency_mismatch"


def test_no_plan_writes_header_only_fire_files(db: WorkspaceDb, data_dir: Path) -> None:
    rows = replace(_rows(), fire_plan=None, fire_events=())
    content = _archive(rows)
    parsed, errors = parse_archive(content)
    assert errors == () and parsed is not None
    assert parsed.rows.fire_plan is None
    assert parsed.rows.fire_events == ()
    _workspace_id, _ctx, _batch_id, report, _counts = _restore(db, data_dir, rows)
    assert report["counts"]["fire_plan_created"] == 0


def test_legacy_golden_upgrades_to_one_plan(db: WorkspaceDb, data_dir: Path) -> None:
    _workspace_id, ctx, _batch_id, report, _counts = _restore_bytes(
        db, data_dir, GOLDEN_V1_8.read_bytes()
    )
    assert report["counts"]["fire_plan_created"] == 1
    codes = [warning["code"] for warning in report["warnings"]]
    assert "fire_legacy_scenarios_skipped" in codes
    assert "fire_loan_payoff_removed" in codes
    assert "fire_contributions_migrated" in codes
    with db.factory() as session:
        plan = FireScenarioRepository(session, ctx).live()
        assert plan is not None
        assert plan.income_minor == 300_000
        assert plan.expenses_minor == 200_000
        assert plan.portfolio_minor == 10_000_000
        assert plan.needs_review is True
        events = FireEventRepository(session, ctx).for_plan(plan.id)
        assert [(row.year, row.kind, row.amount_minor) for row in events] == [
            (2027, "saving_override", 120_000)
        ]


def test_1_7_golden_has_no_fire_rows_or_warning(db: WorkspaceDb, data_dir: Path) -> None:
    _workspace_id, ctx, _batch_id, report, _counts = _restore_bytes(
        db, data_dir, GOLDEN_V1_7.read_bytes()
    )
    assert report["counts"]["fire_plan_created"] == 0
    assert not any(warning.get("file", "").startswith("fire_") for warning in report["warnings"])
    with db.factory() as session:
        assert FireScenarioRepository(session, ctx).list() == []


def _legacy_archive(
    *,
    scenarios: list[list[str]],
    contributions: list[list[str]] | None = None,
    events: list[list[str]] | None = None,
) -> bytes:
    with zipfile.ZipFile(GOLDEN_V1_8) as source:
        members = {name: source.read(name) for name in source.namelist()}

    def csv_bytes(name: str, header: list[str], rows: list[list[str]]) -> bytes:
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\r\n")
        writer.writerow(header)
        writer.writerows(rows)
        return output.getvalue().encode()

    members["fire_scenarios.csv"] = csv_bytes(
        "fire_scenarios.csv",
        [
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
        ],
        scenarios,
    )
    members["fire_contributions.csv"] = csv_bytes(
        "fire_contributions.csv",
        ["id", "scenario_id", "scenario", "year", "net_amount"],
        contributions or [],
    )
    members["fire_events.csv"] = csv_bytes(
        "fire_events.csv",
        ["id", "scenario_id", "scenario", "year", "kind", "amount", "label", "source_loan_id"],
        events or [],
    )
    members["fire_expense_exclusions.csv"] = csv_bytes(
        "fire_expense_exclusions.csv",
        ["id", "scenario_id", "scenario", "category_id", "category"],
        [],
    )
    entries = []
    for name, data in members.items():
        if name == "manifest.json":
            continue
        entries.append(
            {
                "path": name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "rows": None
                if not name.endswith(".csv")
                else len(list(csv.reader(io.StringIO(data.decode(), newline="")))) - 1,
                "bytes": len(data),
            }
        )
    members["manifest.json"] = json.dumps(
        {
            "format": "workspace-archive",
            "format_version": "1.8",
            "app": {"name": "AssetBoard", "version": "0.7.5"},
            "exported_at": "2026-10-18T14:30:00Z",
            "base_currency": "EUR",
            "files": entries,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return output.getvalue()


def test_1_8_linked_fallbacks_and_missing_fallbacks(db: WorkspaceDb, data_dir: Path) -> None:
    content = _legacy_archive(
        scenarios=[
            [
                str(PLAN),
                "Linked plan",
                "EUR",
                "1985",
                "2026",
                "250",
                "500",
                "300",
                "55",
                "90",
                "returns_cover_expenses",
                "",
                "trailing_12m",
                "",
                "36000.00",
                "trailing_12m",
                "",
                "8400.00",
                "linked_investable",
                "",
                "50000.00",
                "true",
                "",
            ]
        ],
        contributions=[[str(OVERRIDE), str(PLAN), "Linked plan", "2027", "1200.00"]],
    )
    _workspace_id, ctx, _batch_id, report, _counts = _restore_bytes(db, data_dir, content)
    codes = [warning["code"] for warning in report["warnings"]]
    assert "fire_linked_fallback_used" in codes
    with db.factory() as session:
        plan = FireScenarioRepository(session, ctx).live()
        assert plan is not None
        assert plan.income_minor == 3_600_000
        assert plan.expenses_minor == 840_000
        assert plan.portfolio_minor == 5_000_000
        events = FireEventRepository(session, ctx).for_plan(plan.id)
        assert [(row.year, row.kind) for row in events] == [(2027, "saving_override")]


def test_1_8_missing_fallback_becomes_zero_with_a_warning(db: WorkspaceDb, data_dir: Path) -> None:
    content = _legacy_archive(
        scenarios=[
            [
                str(PLAN),
                "Empty linked plan",
                "EUR",
                "1985",
                "2026",
                "250",
                "500",
                "300",
                "55",
                "90",
                "returns_cover_expenses",
                "",
                "trailing_12m",
                "",
                "",
                "manual",
                "2000.00",
                "",
                "manual",
                "100000.00",
                "",
                "true",
                "",
            ]
        ]
    )
    _workspace_id, ctx, _batch_id, report, _counts = _restore_bytes(db, data_dir, content)
    assert "fire_missing_fallback_zero" in {warning["code"] for warning in report["warnings"]}
    with db.factory() as session:
        plan = FireScenarioRepository(session, ctx).live()
        assert plan is not None
        assert plan.income_minor == 0


def test_restore_rollback_removes_plan_and_events(db: WorkspaceDb, data_dir: Path) -> None:
    _workspace_id, ctx, batch_id, _report, _counts = _restore(db, data_dir, _rows())
    rollback_import(db.factory, ctx, batch_id)
    with db.factory() as session:
        assert FireScenarioRepository(session, ctx).list() == []
        assert FireEventRepository(session, ctx).list() == []
        audit = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "import.rolled_back")
        ).one()
        assert audit.data["fire_scenarios_deleted"] == 1
        assert audit.data["fire_events_deleted"] == 3
        assert audit.data["fire_rows_deleted"] == 4
        # The removed 1.8 tables must not survive as always-zero audit keys.
        assert "fire_contributions_deleted" not in audit.data
        assert "fire_expense_exclusions_deleted" not in audit.data


def test_merge_ignores_fire_files_with_one_warning(db: WorkspaceDb, data_dir: Path) -> None:
    workspace_id, ctx = _target(db)
    batch_id = upload_import(
        db.factory,
        ctx,
        data_dir=data_dir,
        filename="archive.zip",
        content=_archive(_rows()),
        options={},
        mode="merge",
    )
    report = plan_import(db.factory, ctx, data_dir, batch_id, _adapters())
    assert [item["code"] for item in report["warnings"]].count("fire_restore_only") == 1
    apply_import(db.factory, ctx, data_dir, batch_id, _adapters())
    with db.factory() as session:
        assert FireScenarioRepository(session, ctx).list() == []
    assert workspace_id


def test_fire_rows_make_a_workspace_non_empty_and_export_uses_current_workspace(
    db: WorkspaceDb, data_dir: Path
) -> None:
    _workspace_id, ctx, _batch_id, _report, _counts = _restore(db, data_dir, _rows())
    with db.factory() as session:
        assert workspace_is_empty(session, ctx) is False
    content = export_workspace(db.factory, ctx).content
    parsed, errors = parse_archive(content)
    assert errors == () and parsed is not None
    assert parsed.rows.fire_plan is not None
    assert parsed.rows.fire_plan.id == str(PLAN)


def test_2_0_has_no_fire_scenarios_or_contributions_files() -> None:
    content = _archive(_rows())
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = set(archive.namelist())
    assert {"fire_plan.csv", "fire_events.csv"} <= names
    assert (
        not {
            "fire_scenarios.csv",
            "fire_contributions.csv",
            "fire_expense_exclusions.csv",
        }
        & names
    )


def test_reader_reports_events_without_a_plan() -> None:
    rows = replace(
        _rows(),
        fire_plan=None,
        fire_events=(FireEventRow(str(EVENT), 2030, "cash", "1.00", "Cash"),),
    )
    # A header-only plan plus an event row is a cross-file error. write_archive never writes
    # this combination, so edit a valid archive and re-sign the manifest.
    from assetboard.domain.archive import manifest_bytes

    with zipfile.ZipFile(io.BytesIO(_archive(rows))) as archive:
        files = {name: archive.read(name) for name in archive.namelist() if name != "manifest.json"}
    files["fire_plan.csv"] = files["fire_plan.csv"].splitlines(keepends=True)[0]
    manifest = json.loads(
        manifest_bytes(
            files,
            app_name="AssetBoard",
            app_version="test",
            exported_at=EXPORTED_AT,
            base_currency="EUR",
        )
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest).encode())
        for name, data in files.items():
            archive.writestr(name, data)
    parsed, errors = parse_archive(output.getvalue())
    assert parsed is None
    assert any(error.message == "FIRE events without a plan" for error in errors)
    assert EVENT


def test_archive_reader_accepts_needs_review_false() -> None:
    parsed, errors = parse_archive(_archive(_rows(plan=_plan_row(needs_review="false"))))
    assert errors == () and parsed is not None
    assert parsed.rows.fire_plan is not None
    assert parsed.rows.fire_plan.needs_review == "false"
    assert ArchiveError is not None


def _loan_rows() -> tuple[tuple[LoanRow, ...], tuple[LoanRateChangeRow, ...]]:
    return (
        (
            LoanRow(
                str(LOAN),
                "Mortgage",
                "annuity",
                "EUR",
                "100000.00",
                "2026-01-01",
                "2026-02-01",
                24,
                1,
                "fixed",
                "monthly",
                "full_at_origination",
                0,
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
        (LoanRateChangeRow(str(LOAN_RATE), str(LOAN), "Mortgage", "2026-01-01", 600),),
    )


def test_fire_loan_settings_round_trip_and_ignored_projection(
    db: WorkspaceDb, data_dir: Path
) -> None:
    loan_rows, rate_rows = _loan_rows()
    rows = replace(
        _rows(),
        loans=loan_rows,
        loan_rate_changes=rate_rows,
        fire_loan_settings=(FireLoanSettingRow(str(LOAN_SETTING), str(LOAN), "Mortgage", "false"),),
    )
    _workspace_id, ctx, _batch_id, _report, counts = _restore(db, data_dir, rows)
    assert counts["counts"]["fire_loan_settings_created"] == 1, counts
    results = scenario_results(db.factory, ctx)
    assert results.loans is not None
    row = next(item for item in results.loans.rows if item.loan_id == LOAN)
    assert row.state.value == "ignored"
    assert results.loans.included_annual_total.amount_minor == 0
    assert results.loans.ignored_annual_total.amount_minor > 0
    content = export_workspace(db.factory, ctx).content
    parsed, errors = parse_archive(content)
    assert errors == () and parsed is not None
    assert parsed.rows.fire_loan_settings[0].loan_id == str(LOAN)


def test_a_missing_loan_setting_is_a_warning_and_is_skipped(
    db: WorkspaceDb, data_dir: Path
) -> None:
    rows = replace(
        _rows(),
        fire_loan_settings=(FireLoanSettingRow(str(LOAN_SETTING), str(LOAN), "Missing", "false"),),
    )
    parsed, errors = parse_archive(_archive(rows))
    assert errors == () and parsed is not None
    assert parsed.rows.fire_loan_settings == ()
    assert any("missing loan" in warning.message for warning in parsed.warnings)
    _workspace_id, ctx, _batch_id, _report, counts = _restore(db, data_dir, rows)
    assert counts["counts"].get("fire_loan_settings_created", 0) == 0


def test_released_2_0_archive_reads_with_every_loan_included() -> None:
    parsed, errors = parse_archive(GOLDEN_V2_0.read_bytes())
    assert errors == () and parsed is not None
    assert parsed.manifest["format_version"] == "2.0"
    assert parsed.rows.fire_loan_settings == ()


def test_duplicate_and_invalid_fire_loan_settings_are_errors() -> None:
    loan_rows, rate_rows = _loan_rows()
    duplicated = FireLoanSettingRow(str(LOAN_SETTING), str(LOAN), "Mortgage", "false")
    invalid = FireLoanSettingRow(str(UUID(int=8)), str(LOAN), "Mortgage", "maybe")
    parsed, errors = parse_archive(
        _archive(
            replace(
                _rows(),
                loans=loan_rows,
                loan_rate_changes=rate_rows,
                fire_loan_settings=(duplicated, duplicated, invalid),
            )
        )
    )
    assert parsed is None
    messages = {error.message for error in errors}
    assert "duplicate FIRE loan setting id" in messages
    assert "duplicate FIRE loan setting" in messages
    assert "invalid FIRE loan setting" in messages


def test_an_included_true_setting_is_accepted_as_a_no_op(db: WorkspaceDb, data_dir: Path) -> None:
    loan_rows, rate_rows = _loan_rows()
    rows = replace(
        _rows(),
        loans=loan_rows,
        loan_rate_changes=rate_rows,
        fire_loan_settings=(FireLoanSettingRow(str(LOAN_SETTING), str(LOAN), "Mortgage", "true"),),
    )
    _workspace_id, ctx, _batch_id, _report, counts = _restore(db, data_dir, rows)
    assert counts["counts"]["fire_loan_settings_created"] == 0
    detail = fire_scenarios.get_fire_plan(db.factory, ctx)
    assert detail.ignored_loan_ids == frozenset()
