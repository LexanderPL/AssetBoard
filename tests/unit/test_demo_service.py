"""Writer tests for the demo dataset (F9.11a): en/pl, two currencies, coverage and round trip."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import func, select

from assetboard.clock import FixedClock
from assetboard.db.models.identity import User, Workspace
from assetboard.db.models.import_batch import ImportBatch
from assetboard.db.models.ledger import BudgetLine, MonthState, Transaction
from assetboard.db.models.prices import PricePoint
from assetboard.db.models.scheduled import ScheduledItem, ScheduledOccurrence
from assetboard.db.repositories import InvestmentTransactionRepository
from assetboard.domain.archive import parse_archive
from assetboard.domain.demo import DEMO_EXCLUDED_MODELS, DemoPlan
from assetboard.domain.months import YearMonth
from assetboard.services.archive import export_workspace
from assetboard.services.context import Context
from assetboard.services.demo import (
    DemoBuildResult,
    DemoWorkspaceNotEmpty,
    build_demo_workspace,
    ensure_demo_profile,
    plan_for_workspace,
)
from assetboard.services.import_csv import default_import_adapters
from assetboard.services.imports import (
    ImportAdapterRegistry,
    apply_import,
    plan_import,
    upload_import,
)
from assetboard.services.portfolio import current_portfolio_data_in_session
from tests.tenant.harness import owned_models
from tests.tenant.workspace_db import WorkspaceDb, workspace_db

REFERENCE = date(2026, 11, 20)


@dataclass
class BuiltWorkspace:
    db: WorkspaceDb
    ctx: Context
    plan: DemoPlan
    result: DemoBuildResult
    duration: float
    data_dir: Path


@pytest.fixture(
    scope="module",
    params=[("en", "EUR"), ("pl", "PLN")],
    ids=["en-eur", "pl-pln"],
)
def built(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> Iterator[BuiltWorkspace]:
    locale, currency = request.param
    path = tmp_path_factory.mktemp(f"demo-{locale}-{currency}")
    for db in workspace_db(path):
        with db.factory.begin() as session:
            workspace = session.get(Workspace, db.workspace_id)
            assert workspace is not None
            workspace.base_currency = currency
            workspace.locale = locale
        db.add_member("owner", "owner")
        base = db.ctx("owner", "owner")
        ctx = replace(
            base,
            workspace=replace(
                base.workspace,
                base_currency=currency,
                locale=locale,
            ),
            clock=FixedClock("2026-11-20T12:00:00+00:00"),
        )
        data_dir = path / "data"
        plan = plan_for_workspace(ctx, REFERENCE)
        started = perf_counter()
        result = build_demo_workspace(db.factory, ctx, plan, data_dir=data_dir)
        duration = perf_counter() - started
        yield BuiltWorkspace(
            db=db,
            ctx=ctx,
            plan=plan,
            result=result,
            duration=duration,
            data_dir=data_dir,
        )


def _count(db: WorkspaceDb, model: Any, *where: Any) -> int:
    with db.factory() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(model)
                .where(model.workspace_id == db.workspace_id, *where)
            )
            or 0
        )


def test_build_is_under_ten_seconds(built: BuiltWorkspace) -> None:
    assert built.duration < 10


def test_every_month_has_transactions_and_planned_values(built: BuiltWorkspace) -> None:
    for index, month in enumerate(built.plan.months):
        end = YearMonth.from_date(month).last_day
        transactions = _count(
            built.db,
            Transaction,
            Transaction.date >= month,
            Transaction.date <= end,
        )
        assert transactions > 0, month
        if index < 3:  # a full month
            assert 40 <= transactions <= 60, (month, transactions)
        assert _count(built.db, BudgetLine, BudgetLine.month == month) > 0, month


def test_planned_values_have_lines_over_and_under_the_actual(built: BuiltWorkspace) -> None:
    with built.db.factory() as session:
        for month in built.plan.months:
            end = YearMonth.from_date(month).last_day
            lines = {
                line.category_id: line.planned_minor
                for line in session.scalars(
                    select(BudgetLine).where(
                        BudgetLine.workspace_id == built.db.workspace_id,
                        BudgetLine.month == month,
                    )
                )
            }
            actual: dict[UUID, int] = {}
            for row in session.scalars(
                select(Transaction).where(
                    Transaction.workspace_id == built.db.workspace_id,
                    Transaction.date >= month,
                    Transaction.date <= end,
                )
            ):
                actual[row.category_id] = actual.get(row.category_id, 0) + row.amount_minor
            over = sum(1 for key, planned in lines.items() if actual.get(key, 0) > planned)
            under = sum(1 for key, planned in lines.items() if actual.get(key, 0) < planned)
            assert over > 0 and under > 0, month


def test_m0_has_paid_pending_and_overdue_occurrences(built: BuiltWorkspace) -> None:
    with built.db.factory() as session:
        rows = list(
            session.scalars(
                select(ScheduledOccurrence).where(
                    ScheduledOccurrence.workspace_id == built.db.workspace_id
                )
            )
        )
        items = list(
            session.scalars(
                select(ScheduledItem).where(ScheduledItem.workspace_id == built.db.workspace_id)
            )
        )
    month = YearMonth.from_date(REFERENCE)
    in_m0 = [row for row in rows if month.contains(row.due_date)]
    assert any(row.status == "posted" for row in in_m0)
    assert any(row.status == "pending" and row.due_date > REFERENCE for row in in_m0)
    assert any(row.status == "pending" and row.due_date < REFERENCE for row in in_m0)
    assert {row.status for row in rows} == {"pending", "posted", "skipped"}
    assert {item.freq for item in items} == {"once", "weekly", "monthly", "yearly"}


def test_holdings_compute_without_oversell_split_and_dividends_appear(
    built: BuiltWorkspace,
) -> None:
    with built.db.factory() as session:
        data = current_portfolio_data_in_session(session, built.ctx)
        assert data.valuation.rows
        assert all(row.value is not None for row in data.valuation.rows)
        trades = list(InvestmentTransactionRepository(session, built.ctx).live())
        dividends = [row for row in trades if row.type == "dividend"]
        assert len(dividends) >= 4
        assert all(row.fee_minor > 0 for row in dividends)  # 15% withholding
        assert any(row.income_transaction_id is not None for row in dividends)
        assert any(row.type == "split" for row in trades)
        assert any(row.type == "sell" for row in trades)
        assert any(row.settlement_currency is not None for row in trades)
        assert any(row.fx_to_base is not None and row.quote_currency == "USD" for row in trades)


def test_last_price_is_close_to_t_and_one_point_is_stale(built: BuiltWorkspace) -> None:
    with built.db.factory() as session:
        points = list(
            session.scalars(
                select(PricePoint).where(PricePoint.workspace_id == built.db.workspace_id)
            )
        )
    by_instrument: dict[UUID, list[date]] = {}
    for point in points:
        by_instrument.setdefault(point.instrument_id, []).append(point.date)
    assert by_instrument
    latest = sorted(max(days) for days in by_instrument.values())
    assert latest[-1] >= REFERENCE - timedelta(days=3)
    assert latest[0] <= REFERENCE - timedelta(days=10)  # the stale badge


def test_variable_income_has_a_filled_calendar_and_tax_children(built: BuiltWorkspace) -> None:
    with built.db.factory() as session:
        occurrences = list(
            session.scalars(
                select(ScheduledOccurrence).where(
                    ScheduledOccurrence.workspace_id == built.db.workspace_id,
                    ScheduledOccurrence.generated_by == "income",
                )
            )
        )
        assert len(occurrences) >= 4  # M-3…M0
        posted = [row for row in occurrences if row.status == "posted"]
        assert posted
        tax_children = [
            child
            for row in posted
            for child in session.scalars(
                select(ScheduledOccurrence).where(
                    ScheduledOccurrence.workspace_id == built.db.workspace_id,
                    ScheduledOccurrence.parent_occurrence_id == row.id,
                )
            )
        ]
        assert tax_children
        assert all(child.generated_by == "tax" for child in tax_children)
        assert any(child.status == "posted" and child.expected_minor > 0 for child in tax_children)


def test_linked_dividend_income_stays_in_the_closed_m3_month(built: BuiltWorkspace) -> None:
    with built.db.factory() as session:
        state = session.scalars(
            select(MonthState).where(
                MonthState.workspace_id == built.db.workspace_id,
                MonthState.month == built.plan.closed_month,
            )
        ).one()
        assert state.closed_at is not None
        closed_month = YearMonth.from_date(built.plan.closed_month)
        dividends = [
            row
            for row in InvestmentTransactionRepository(session, built.ctx).live()
            if row.type == "dividend"
            and row.date is not None
            and YearMonth.from_date(row.date) == closed_month
            and row.income_transaction_id is not None
        ]
        assert dividends
        for dividend in dividends:
            income = session.get(Transaction, dividend.income_transaction_id)
            assert income is not None
            assert income.deleted_at is None
            assert income.direction == "income"
            assert closed_month.contains(income.date)


def test_every_workspace_owned_model_is_covered_or_excluded(built: BuiltWorkspace) -> None:
    owned = owned_models()
    assert {model.__name__ for model in owned} >= set(DEMO_EXCLUDED_MODELS)
    for model in owned:
        if model.__name__ in DEMO_EXCLUDED_MODELS:
            continue
        assert _count(built.db, model) > 0, model.__name__
    assert _count(built.db, ImportBatch) == 1


def test_the_data_page_has_one_completed_real_csv_import(built: BuiltWorkspace) -> None:
    with built.db.factory() as session:
        batch = session.scalars(
            select(ImportBatch).where(ImportBatch.workspace_id == built.db.workspace_id)
        ).one()
        assert batch.status == "applied"
        assert batch.applied_at is not None
        assert batch.source == "transactions_csv"
        assert batch.options["remember_mappings"] is True
        imported = session.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(
                Transaction.workspace_id == built.db.workspace_id,
                Transaction.source == "import",
            )
        )
        assert imported == len(built.plan.csv_rows)


def test_the_builder_refuses_a_workspace_that_is_no_longer_empty(built: BuiltWorkspace) -> None:
    with pytest.raises(DemoWorkspaceNotEmpty):
        build_demo_workspace(
            built.db.factory,
            built.ctx,
            built.plan,
            data_dir=built.data_dir,
        )


def test_the_owner_profile_is_only_filled_when_empty(tmp_path: Path) -> None:
    for db in workspace_db(tmp_path):
        db.add_member("owner", "owner")
        other = db.add_workspace("Real profile")
        db.add_member("real", "owner", workspace_id=other)
        with db.factory.begin() as session:
            real = session.scalars(select(User).where(User.email == "real@example.com")).one()
            real.birth_year = 1970
            real.life_expectancy = 95
        with db.factory.begin() as session:
            assert ensure_demo_profile(session, db.workspace_id, reference_date=REFERENCE) == (
                1988,
                88,
            )
        with db.factory.begin() as session:
            assert ensure_demo_profile(session, other, reference_date=REFERENCE) == (1970, 95)
        break


_UUID = re.compile(rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _canonical_files(
    content: bytes,
) -> tuple[dict[str, bytes], dict[str, object], dict[str, object]]:
    parsed, errors = parse_archive(content)
    assert not errors and parsed is not None
    mapping: dict[bytes, bytes] = {}

    def replace_id(match: re.Match[bytes]) -> bytes:
        raw = match.group(0)
        if raw not in mapping:
            mapping[raw] = f"ID{len(mapping) + 1:04d}".encode()
        return mapping[raw]

    files: dict[str, bytes] = {}
    for name in sorted(parsed.files):
        if name in {"manifest.json", "workspace.json"}:
            continue
        files[name] = _UUID.sub(replace_id, parsed.files[name])
    workspace = json.loads(parsed.files["workspace.json"])
    manifest_rows = cast("list[dict[str, object]]", parsed.manifest["files"])
    rows: dict[str, object] = {}
    for entry in manifest_rows:
        path = entry["path"]
        if path != "manifest.json":
            rows[str(path)] = entry["rows"]
    return files, workspace, rows


def _canonical_without_ids(
    content: bytes,
) -> tuple[dict[str, bytes], dict[str, object], dict[str, object]]:
    """Compare two independent builds: ids are random, so replace them and sort the rows."""
    parsed, errors = parse_archive(content)
    assert not errors and parsed is not None
    files: dict[str, bytes] = {}
    for name in sorted(parsed.files):
        if name in {"manifest.json", "workspace.json"}:
            continue
        lines = _UUID.sub(b"ID", parsed.files[name]).splitlines()
        files[name] = b"\r\n".join([lines[0], *sorted(lines[1:])]) + b"\r\n"
    workspace = json.loads(parsed.files["workspace.json"])
    manifest_rows = cast("list[dict[str, object]]", parsed.manifest["files"])
    rows: dict[str, object] = {}
    for entry in manifest_rows:
        path = entry["path"]
        if path != "manifest.json":
            rows[str(path)] = entry["rows"]
    return files, workspace, rows


def test_two_builds_of_the_same_reference_are_archive_identical(
    built: BuiltWorkspace,
) -> None:
    target = built.db.add_workspace("Home")
    built.db.add_member("owner", "owner", workspace_id=target)
    with built.db.factory.begin() as session:
        workspace = session.get(Workspace, target)
        assert workspace is not None
        workspace.base_currency = built.ctx.workspace.base_currency
        workspace.locale = built.ctx.workspace.locale
    target_ctx = replace(built.ctx, workspace=replace(built.ctx.workspace, id=target))
    plan = plan_for_workspace(target_ctx, REFERENCE)
    build_demo_workspace(
        built.db.factory,
        target_ctx,
        plan,
        data_dir=built.data_dir / "second",
    )

    first = export_workspace(built.db.factory, built.ctx).content
    second = export_workspace(built.db.factory, target_ctx).content
    first_files, first_workspace, first_rows = _canonical_without_ids(first)
    second_files, second_workspace, second_rows = _canonical_without_ids(second)

    assert first_workspace == second_workspace
    assert first_rows == second_rows
    assert set(first_files) == set(second_files)
    for name in first_files:
        assert first_files[name] == second_files[name], name


def test_archive_export_restores_into_an_empty_workspace(built: BuiltWorkspace) -> None:
    first = export_workspace(built.db.factory, built.ctx).content
    target = built.db.add_workspace("Restored")
    built.db.add_member("owner", "owner", workspace_id=target)
    target_ctx = replace(
        built.ctx,
        workspace=replace(
            built.ctx.workspace,
            id=target,
            name="Restored",
        ),
    )
    batch_id = upload_import(
        built.db.factory,
        target_ctx,
        data_dir=built.data_dir,
        filename="demo.zip",
        content=first,
        options={},
        mode="restore",
    )
    registry = ImportAdapterRegistry(default_import_adapters())
    report = plan_import(built.db.factory, target_ctx, built.data_dir, batch_id, adapters=registry)
    assert report["counts"]["invalid"] == 0, report
    apply_import(built.db.factory, target_ctx, built.data_dir, batch_id, registry)

    second = export_workspace(built.db.factory, target_ctx).content
    first_files, first_workspace, first_rows = _canonical_files(first)
    second_files, second_workspace, second_rows = _canonical_files(second)
    assert first_workspace == second_workspace
    assert first_rows == second_rows
    assert set(first_files) == set(second_files)
    for name in first_files:
        assert first_files[name] == second_files[name], name
