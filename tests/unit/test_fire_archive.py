"""F8.6 FIRE archive extension: export lineage, restore fallbacks, round trips and rollback."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import pytest

from assetboard.db.repositories import (
    FireContributionRepository,
    FireEventRepository,
    FireExpenseExclusionRepository,
    FireScenarioRepository,
)
from assetboard.domain.archive import (
    FILE_ORDER,
    FIRE_CSV_FILES,
    ArchiveRows,
    CategoryRow,
    FireContributionRow,
    FireEventRow,
    FireExpenseExclusionRow,
    FireScenarioRow,
    ItemValuationRow,
    LoanRow,
    NetWorthCategoryRow,
    NetWorthItemRow,
    TransactionRow,
    parse_archive,
    write_archive,
)
from assetboard.domain.categories import name_key
from assetboard.domain.fire import (
    ContributionOverride,
    EventKind,
    ProjectionEvent,
    ProjectionInputs,
    Rule,
    project,
)
from assetboard.domain.import_upload import parse_upload
from assetboard.domain.money import Money
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
FOOD = UUID("11111111-1111-4111-8111-111111111111")
HOUSING = UUID("22222222-2222-4222-8222-222222222222")
SALARY = UUID("33333333-3333-4333-8333-333333333333")
NW_ASSETS = UUID("44444444-4444-4444-8444-444444444444")
NW_BROKERAGE = UUID("55555555-5555-4555-8555-555555555555")
NW_VALUE = UUID("66666666-6666-4666-8666-666666666666")
INCOME_TX = UUID("77777777-7777-4777-8777-777777777777")
FOOD_TX = UUID("88888888-8888-4888-8888-888888888888")
HOUSING_TX = UUID("99999999-9999-4999-8999-999999999999")
MANUAL_SCENARIO = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
LINKED_SCENARIO = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
CONTRIBUTION = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
EVENT = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
EXCLUSION = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
LOAN = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")


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


def _manual_scenario() -> FireScenarioRow:
    return FireScenarioRow(
        str(MANUAL_SCENARIO),
        "Manual plan",
        "EUR",
        1990,
        2026,
        250,
        500,
        300,
        55,
        95,
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
    )


def _linked_scenario(
    *,
    currency: str = "EUR",
    income_fallback: str = "36000.00",
    expenses_fallback: str = "8400.00",
    portfolio_fallback: str = "50000.00",
    is_default: str = "false",
) -> FireScenarioRow:
    return FireScenarioRow(
        str(LINKED_SCENARIO),
        "Linked plan",
        currency,
        1985,
        2026,
        250,
        500,
        300,
        55,
        90,
        "returns_cover_expenses",
        None,
        "trailing_12m",
        "",
        income_fallback,
        "trailing_12m",
        "",
        expenses_fallback,
        "linked_investable",
        "",
        portfolio_fallback,
        is_default,
        None,
    )


def _rows(
    *,
    transactions: tuple[TransactionRow, ...] | None = None,
    networth_items: tuple[NetWorthItemRow, ...] | None = None,
    item_valuations: tuple[ItemValuationRow, ...] | None = None,
    scenarios: tuple[FireScenarioRow, ...] | None = None,
    contributions: tuple[FireContributionRow, ...] | None = None,
    events: tuple[FireEventRow, ...] | None = None,
    exclusions: tuple[FireExpenseExclusionRow, ...] | None = None,
    loans: tuple[LoanRow, ...] = (),
) -> ArchiveRows:
    if transactions is None:
        transactions = (
            TransactionRow(
                "2026-01-10",
                "income",
                "3000.00",
                "EUR",
                "Salary",
                "January",
                str(SALARY),
                str(INCOME_TX),
                None,
            ),
            TransactionRow(
                "2026-01-15",
                "expense",
                "500.00",
                "EUR",
                "Food",
                "Groceries",
                str(FOOD),
                str(FOOD_TX),
                None,
            ),
            TransactionRow(
                "2026-01-20",
                "expense",
                "700.00",
                "EUR",
                "Housing",
                "Rent",
                str(HOUSING),
                str(HOUSING_TX),
                None,
            ),
        )
    if networth_items is None:
        networth_items = (
            NetWorthItemRow(
                str(NW_BROKERAGE),
                "asset",
                "Assets",
                str(NW_ASSETS),
                "Brokerage",
                "EUR",
                "",
                None,
                "manual",
                None,
                None,
                "true",
                "true",
                None,
                "false",
            ),
        )
    if item_valuations is None:
        item_valuations = (
            ItemValuationRow(
                str(NW_VALUE),
                str(NW_BROKERAGE),
                "Brokerage",
                "2026-02-01",
                "50000.00",
                "EUR",
                "",
                "2026-02-01T08:00:00Z",
            ),
        )
    if scenarios is None:
        scenarios = (_linked_scenario(), _manual_scenario())
    if contributions is None:
        contributions = (
            FireContributionRow(
                str(CONTRIBUTION),
                str(MANUAL_SCENARIO),
                "Manual plan",
                2027,
                "1200.00",
            ),
        )
    if events is None:
        events = (
            FireEventRow(
                str(EVENT),
                str(MANUAL_SCENARIO),
                "Manual plan",
                2030,
                "cash",
                "-5000.00",
                "Renovation",
                None,
            ),
        )
    if exclusions is None:
        exclusions = (
            FireExpenseExclusionRow(
                str(EXCLUSION),
                str(LINKED_SCENARIO),
                "Linked plan",
                str(FOOD),
                "Food",
            ),
        )
    return ArchiveRows(
        workspace=_workspace(),
        categories=(
            CategoryRow(str(FOOD), "expense", "Food", "regular", "#4f9d69", 0, None),
            CategoryRow(str(HOUSING), "expense", "Housing", "regular", "#3b6ea8", 1, None),
            CategoryRow(str(SALARY), "income", "Salary", "regular", "#2f7f5f", 0, None),
        ),
        budget_lines=(),
        transactions=transactions,
        months=(),
        networth_categories=(
            NetWorthCategoryRow(str(NW_ASSETS), "asset", "Assets", "#3b6ea8", 0, None),
        ),
        networth_items=networth_items,
        item_valuations=item_valuations,
        fire_scenarios=scenarios,
        fire_contributions=contributions,
        fire_events=events,
        fire_expense_exclusions=exclusions,
        loans=loans,
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


def _scenario(ctx: Any, factory: Any, name: str) -> Any:
    with factory() as session:
        row = next(
            item for item in FireScenarioRepository(session, ctx).list() if item.name == name
        )
        return row


def _data_files(content: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return {name: archive.read(name) for name in FILE_ORDER if name in archive.namelist()}


def test_linked_and_manual_round_trip_keep_projection_and_bytes(
    db: WorkspaceDb, data_dir: Path
) -> None:
    rows = _rows()
    original = _archive(rows)
    workspace_id, ctx, _batch_id, report, counts = _restore(db, data_dir, rows)

    assert report["counts"]["fire_scenarios_created"] == 2
    assert report["counts"]["fire_contributions_created"] == 1
    assert report["counts"]["fire_events_created"] == 1
    assert report["counts"]["fire_expense_exclusions_created"] == 1
    assert report["counts"]["fire_linked_fallbacks"] == 0
    assert report["counts"]["fire_missing_fallbacks"] == 0
    assert [warning["code"] for warning in report["warnings"]] == []

    with db.factory() as session:
        scenarios = {row.name: row for row in FireScenarioRepository(session, ctx).list()}
        linked = scenarios["Linked plan"]
        manual = scenarios["Manual plan"]
        assert linked.income_mode == "trailing_12m"
        assert linked.expenses_mode == "trailing_12m"
        assert linked.portfolio_mode == "linked_investable"
        assert linked.income_manual_minor is None
        assert manual.is_default is True
        assert linked.import_key == f"id:{LINKED_SCENARIO}"
        assert manual.import_key == f"id:{MANUAL_SCENARIO}"
        assert len(FireContributionRepository(session, ctx).list()) == 1
        assert len(FireEventRepository(session, ctx).list()) == 1
        assert len(FireExpenseExclusionRepository(session, ctx).list()) == 1

    linked_results = scenario_results(db.factory, ctx, _scenario(ctx, db.factory, "Linked plan").id)
    assert linked_results.projection is not None
    assert linked_results.blocks == ()
    assert linked_results.projection.inputs.start_income == Money(3_600_000, "EUR")
    assert linked_results.projection.inputs.start_expenses == Money(840_000, "EUR")
    assert linked_results.projection.inputs.start_portfolio == Money(5_000_000, "EUR")

    manual_results = scenario_results(db.factory, ctx, _scenario(ctx, db.factory, "Manual plan").id)
    expected = project(
        ProjectionInputs(
            currency="EUR",
            birth_year=1990,
            start_year=2026,
            horizon_age=95,
            stop_working_age=55,
            inflation_bp=250,
            real_return_bp=500,
            income_growth_bp=300,
            rule=Rule.SWR,
            swr_bp=400,
            start_income=Money(300_000, "EUR"),
            start_expenses=Money(200_000, "EUR"),
            start_portfolio=Money(10_000_000, "EUR"),
            contributions=(ContributionOverride(2027, Money(120_000, "EUR")),),
            events=(ProjectionEvent(2030, EventKind.CASH, Money(-500_000, "EUR")),),
        )
    )
    assert manual_results.projection == expected

    content = export_workspace(db.factory, ctx).content
    parsed, errors = parse_archive(content)
    assert errors == () and parsed is not None
    assert parsed.manifest["format_version"] == "1.7"
    assert _data_files(original) == _data_files(content)


def test_missing_sources_fall_back_to_archived_values(db: WorkspaceDb, data_dir: Path) -> None:
    rows = _rows(
        transactions=(),
        networth_items=(),
        item_valuations=(),
        scenarios=(_linked_scenario(),),
        contributions=(),
        events=(),
        exclusions=(),
    )
    _workspace_id, ctx, _batch_id, report, _counts = _restore(db, data_dir, rows)
    assert report["counts"]["fire_linked_fallbacks"] == 3
    assert report["counts"]["fire_missing_fallbacks"] == 0
    codes = {warning["code"] for warning in report["warnings"]}
    assert "fire_linked_fallback" in codes
    assert "fire_missing_fallback" not in codes
    with db.factory() as session:
        scenario = next(iter(FireScenarioRepository(session, ctx).list()))
        assert scenario.income_mode == "manual"
        assert scenario.income_manual_minor == 3_600_000
        assert scenario.expenses_mode == "manual"
        assert scenario.expenses_manual_minor == 840_000
        assert scenario.portfolio_mode == "manual"
        assert scenario.portfolio_manual_minor == 5_000_000


def test_missing_sources_without_fallbacks_restore_zero_with_warning(
    db: WorkspaceDb, data_dir: Path
) -> None:
    rows = _rows(
        transactions=(),
        networth_items=(),
        item_valuations=(),
        scenarios=(
            _linked_scenario(income_fallback="", expenses_fallback="", portfolio_fallback=""),
        ),
        contributions=(),
        events=(),
        exclusions=(),
    )
    _workspace_id, ctx, _batch_id, report, _counts = _restore(db, data_dir, rows)
    assert report["counts"]["fire_linked_fallbacks"] == 3
    assert report["counts"]["fire_missing_fallbacks"] == 3
    assert {warning["code"] for warning in report["warnings"]} >= {
        "fire_linked_fallback",
        "fire_missing_fallback",
    }
    with db.factory() as session:
        scenario = next(iter(FireScenarioRepository(session, ctx).list()))
        assert scenario.income_mode == "manual"
        assert scenario.income_manual_minor == 0
        assert scenario.expenses_mode == "manual"
        assert scenario.expenses_manual_minor == 0
        assert scenario.portfolio_mode == "manual"
        assert scenario.portfolio_manual_minor == 0


def test_missing_exclusion_is_skipped_and_forces_expense_fallback(
    db: WorkspaceDb, data_dir: Path
) -> None:
    rows = _rows(
        exclusions=(
            FireExpenseExclusionRow(
                str(EXCLUSION),
                str(LINKED_SCENARIO),
                "Linked plan",
                str(UUID(int=0)),
                "Missing",
            ),
        )
    )
    _workspace_id, ctx, _batch_id, report, _counts = _restore(db, data_dir, rows)
    assert report["counts"]["fire_skipped_exclusions"] == 1
    assert report["counts"]["fire_expense_exclusions_created"] == 0
    assert report["counts"]["fire_linked_fallbacks"] == 1
    codes = {warning["code"] for warning in report["warnings"]}
    assert "fire_exclusion_missing" in codes
    with db.factory() as session:
        scenario = next(
            row for row in FireScenarioRepository(session, ctx).list() if row.name == "Linked plan"
        )
        assert scenario.income_mode == "trailing_12m"
        assert scenario.expenses_mode == "manual"
        assert scenario.expenses_manual_minor == 840_000
        assert scenario.portfolio_mode == "linked_investable"
        assert FireExpenseExclusionRepository(session, ctx).list() == []


def test_missing_source_loan_clears_the_event_link(db: WorkspaceDb, data_dir: Path) -> None:
    rows = _rows(
        events=(
            FireEventRow(
                str(EVENT),
                str(MANUAL_SCENARIO),
                "Manual plan",
                2030,
                "cash",
                "-5000.00",
                "Renovation",
                str(UUID(int=1)),
            ),
        )
    )
    _workspace_id, ctx, _batch_id, report, _counts = _restore(db, data_dir, rows)
    assert report["counts"]["fire_cleared_source_loans"] == 1
    assert "fire_source_loan_missing" in {warning["code"] for warning in report["warnings"]}
    with db.factory() as session:
        event = next(iter(FireEventRepository(session, ctx).list()))
        assert event.source_loan_id is None


def test_currency_mismatch_falls_back_and_promotes_the_first_default(
    db: WorkspaceDb, data_dir: Path
) -> None:
    rows = _rows(
        scenarios=(_linked_scenario(currency="USD", is_default="false"),),
        contributions=(),
        events=(),
        exclusions=(),
    )
    _workspace_id, ctx, _batch_id, report, _counts = _restore(db, data_dir, rows)
    assert report["counts"]["fire_linked_fallbacks"] == 3
    assert report["counts"]["fire_default_promoted"] == 1
    codes = {warning["code"] for warning in report["warnings"]}
    assert "fire_linked_fallback" in codes
    assert "fire_default_promoted" in codes
    with db.factory() as session:
        scenario = next(iter(FireScenarioRepository(session, ctx).list()))
        assert scenario.income_mode == "manual"
        assert scenario.expenses_mode == "manual"
        assert scenario.portfolio_mode == "manual"
        assert scenario.is_default is True


def test_restore_rollback_removes_fire_before_loans(db: WorkspaceDb, data_dir: Path) -> None:
    loan = LoanRow(
        str(LOAN),
        "Mortgage",
        "annuity",
        "EUR",
        "100000.00",
        "2025-01-01",
        "2025-02-01",
        120,
        1,
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
    )
    rows = _rows(
        loans=(loan,),
        events=(
            FireEventRow(
                str(EVENT),
                str(MANUAL_SCENARIO),
                "Manual plan",
                2030,
                "expense_change",
                "-1200.00",
                "Loan repaid: Mortgage",
                str(LOAN),
            ),
        ),
    )
    _workspace_id, ctx, batch_id, _report, _counts = _restore(db, data_dir, rows)
    with db.factory() as session:
        event = next(iter(FireEventRepository(session, ctx).list()))
        assert event.source_loan_id is not None
    counts = rollback_import(db.factory, ctx, batch_id)
    assert (
        counts["fire_scenarios"]
        + counts["fire_contributions"]
        + counts["fire_events"]
        + counts["fire_expense_exclusions"]
        == 5
    )
    with db.factory() as session:
        assert FireScenarioRepository(session, ctx).list() == []
        assert FireContributionRepository(session, ctx).list() == []
        assert FireEventRepository(session, ctx).list() == []
        assert FireExpenseExclusionRepository(session, ctx).list() == []


def test_merge_ignores_fire_files_with_one_warning(db: WorkspaceDb, data_dir: Path) -> None:
    workspace_id, ctx, batch_id, report, _counts = _restore(db, data_dir, _rows(), mode="merge")
    assert [item["code"] for item in report["warnings"]].count("fire_restore_only") == 1
    with db.factory() as session:
        assert FireScenarioRepository(session, ctx).list() == []
        assert FireContributionRepository(session, ctx).list() == []
        assert FireEventRepository(session, ctx).list() == []
        assert FireExpenseExclusionRepository(session, ctx).list() == []


@pytest.mark.parametrize(
    "name",
    FIRE_CSV_FILES,
)
def test_lone_fire_csv_is_refused(name: str) -> None:
    files = _data_files(_archive(_rows()))
    parsed, errors = parse_upload(files[name])
    assert parsed is None
    assert errors and errors[0].message in {"unknown file", "only part of a full restore"}


def test_fire_rows_make_a_workspace_non_empty_and_export_only_current_workspace(
    db: WorkspaceDb,
) -> None:
    db.add_member("own", "owner")
    ctx = db.ctx("own", "owner")
    other = db.add_workspace("Private")
    with db.factory.begin() as session:
        for workspace_id in (db.workspace_id, other):
            repository = FireScenarioRepository(
                session, db.ctx("own", "owner", workspace_id=workspace_id)
            )
            repository.create(
                name="Plan",
                name_key=name_key("Plan"),
                currency="EUR",
                birth_year=1990,
                start_year=2026,
                inflation_bp=250,
                real_return_bp=500,
                income_growth_bp=300,
                stop_working_age=55,
                horizon_age=90,
                rule="swr",
                swr_bp=400,
                income_mode="manual",
                income_manual_minor=300_000,
                expenses_mode="manual",
                expenses_manual_minor=200_000,
                portfolio_mode="manual",
                portfolio_manual_minor=10_000_000,
                is_default=True,
                import_key=(f"id:{MANUAL_SCENARIO}" if workspace_id == db.workspace_id else None),
            )
    with db.factory() as session:
        assert workspace_is_empty(session, ctx) is False
    parsed, errors = parse_archive(export_workspace(db.factory, ctx).content)
    assert errors == () and parsed is not None
    assert len(parsed.rows.fire_scenarios) == 1
    assert parsed.rows.fire_scenarios[0].id == str(MANUAL_SCENARIO)


def test_restore_uses_fresh_fire_ids_when_the_lineage_id_is_taken(
    db: WorkspaceDb, data_dir: Path
) -> None:
    db.add_member("own", "owner")
    with db.factory.begin() as session:
        FireScenarioRepository(session, db.ctx("own", "owner")).create(
            id=LINKED_SCENARIO,
            name="Other plan",
            name_key="other plan",
            currency="EUR",
            birth_year=1990,
            start_year=2026,
            inflation_bp=250,
            real_return_bp=500,
            income_growth_bp=300,
            stop_working_age=55,
            horizon_age=90,
            rule="swr",
            swr_bp=400,
            income_mode="manual",
            income_manual_minor=300_000,
            expenses_mode="manual",
            expenses_manual_minor=200_000,
            portfolio_mode="manual",
            portfolio_manual_minor=10_000_000,
            is_default=True,
        )
    workspace_id, ctx, _batch_id, _report, _counts = _restore(db, data_dir, _rows())
    with db.factory() as session:
        restored = next(
            row for row in FireScenarioRepository(session, ctx).list() if row.name == "Linked plan"
        )
        assert restored.id != LINKED_SCENARIO
        assert restored.import_key == f"id:{LINKED_SCENARIO}"
        other = FireScenarioRepository(session, db.ctx("own", "owner")).get(LINKED_SCENARIO)
        assert other is not None and other.name == "Other plan"


def test_v1_6_golden_restores_without_fire_rows_or_warnings(
    db: WorkspaceDb, data_dir: Path
) -> None:
    content = (Path(__file__).parents[1] / "fixtures" / "workspace-archive-v1.6.zip").read_bytes()
    workspace_id, ctx = _target(db)
    batch_id = upload_import(
        db.factory,
        ctx,
        data_dir=data_dir,
        filename="archive.zip",
        content=content,
        options={},
        mode="restore",
    )
    report = plan_import(db.factory, ctx, data_dir, batch_id, _adapters())
    assert not any(warning["code"].startswith("fire_") for warning in report["warnings"])
    apply_import(db.factory, ctx, data_dir, batch_id, _adapters())
    with db.factory() as session:
        assert FireScenarioRepository(session, ctx).list() == []
        assert FireContributionRepository(session, ctx).list() == []
        assert FireEventRepository(session, ctx).list() == []
        assert FireExpenseExclusionRepository(session, ctx).list() == []
