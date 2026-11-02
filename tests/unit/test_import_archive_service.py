"""F4.4 archive restore and merge through the real services and repositories."""

from __future__ import annotations

import json
import zipfile
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update

from assetboard.clock import Clock
from assetboard.db.models.identity import AuditEvent, Workspace
from assetboard.db.models.import_batch import ImportBatch
from assetboard.db.models.ledger import Category
from assetboard.db.repositories import (
    BudgetLineRepository,
    CategoryRepository,
    ItemValuationRepository,
    MonthStateRepository,
    NetWorthCategoryRepository,
    NetWorthItemRepository,
    NetWorthSnapshotRepository,
    ScheduledItemRepository,
    TransactionRepository,
)
from assetboard.domain.archive import (
    ArchiveRows,
    BudgetLineRow,
    CategoryRow,
    ItemValuationRow,
    MonthRow,
    NetWorthCategoryRow,
    NetWorthItemRow,
    NetWorthSnapshotRow,
    ParsedArchive,
    TransactionRow,
    parse_archive,
    write_archive,
)
from assetboard.domain.categories import SEED
from assetboard.domain.months import YearMonth
from assetboard.domain.networth import SEED as NETWORTH_SEED
from assetboard.services.archive import export_workspace, workspace_is_empty
from assetboard.services.categories import seed_into
from assetboard.services.context import PermissionDenied
from assetboard.services.import_archive import _collect_sources, _source_categories
from assetboard.services.import_csv import default_import_adapters
from assetboard.services.imports import (
    ImportAdapterRegistry,
    ImportConfirmationRequired,
    ImportRefused,
    ImportRestoreRequiresArchive,
    ImportRestoreRollbackBlocked,
    ImportRestoreUnavailable,
    apply_import,
    plan_import,
    preview_rollback_import,
    rollback_import,
    update_import_preview,
    upload_import,
)
from assetboard.services.imports import (
    ImportError as ServiceImportError,
)
from assetboard.services.networth import seed_into as networth_seed_into
from tests.tenant.workspace_db import WorkspaceDb, workspace_db

EXPORTED_AT = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
SOURCE = UUID("11111111-1111-4111-8111-111111111111")
FOOD = UUID("22222222-2222-4222-8222-222222222222")
ARCHIVED = UUID("33333333-3333-4333-8333-333333333333")
SALARY = UUID("44444444-4444-4444-8444-444444444444")
NW_VEHICLES = UUID("55555555-5555-4555-8555-555555555555")
NW_CAR = UUID("66666666-6666-4666-8666-666666666666")
NW_BROKERAGE = UUID("77777777-7777-4777-8777-777777777777")
NW_FIRST_VALUE = UUID("88888888-8888-4888-8888-888888888888")
NW_SECOND_VALUE = UUID("99999999-9999-4999-8999-999999999999")
NW_SNAPSHOT = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
NW_LINK = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    for prepared in workspace_db(tmp_path):
        prepared.add_member("own", "owner")
        prepared.add_member("ed", "editor")
        yield prepared


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    return path


def _adapters() -> ImportAdapterRegistry:
    return ImportAdapterRegistry(default_import_adapters())


def _upload(
    db: WorkspaceDb,
    data_dir: Path,
    content: bytes,
    *,
    ctx: Any | None = None,
    mode: str = "restore",
) -> UUID:
    return upload_import(
        db.factory,
        ctx or db.ctx("own", "owner"),
        data_dir=data_dir,
        filename="archive.zip",
        content=content,
        options={},
        mode=cast("Literal['restore', 'merge']", mode),
    )


def _plan(
    db: WorkspaceDb, data_dir: Path, batch_id: UUID, *, ctx: Any | None = None
) -> dict[str, Any]:
    return plan_import(db.factory, ctx or db.ctx("own", "owner"), data_dir, batch_id, _adapters())


def _apply(
    db: WorkspaceDb,
    data_dir: Path,
    batch_id: UUID,
    *,
    ctx: Any | None = None,
) -> dict[str, Any]:
    return apply_import(
        db.factory,
        ctx or db.ctx("own", "owner"),
        data_dir,
        batch_id,
        _adapters(),
    )


def _archive(rows: ArchiveRows) -> bytes:
    return write_archive(
        rows,
        app_name="AssetBoard",
        app_version="test",
        exported_at=EXPORTED_AT,
        base_currency=str(rows.workspace["base_currency"]),
    )


def _with_version(content: bytes, version: str) -> bytes:
    with zipfile.ZipFile(BytesIO(content)) as source:
        members = {info.filename: source.read(info) for info in source.infolist()}
    manifest = json.loads(members["manifest.json"])
    manifest["format_version"] = version
    members["manifest.json"] = json.dumps(manifest).encode()
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for name, data in members.items():
            target.writestr(name, data)
    return output.getvalue()


def _rows(*, currency: str = "EUR", name: str = "Home") -> ArchiveRows:
    return ArchiveRows(
        workspace={
            "name": name,
            "base_currency": currency,
            "country_code": "PL",
            "timezone": "Europe/Warsaw",
            "locale": "pl",
            "auto_copy_plan": True,
            "taxes_enabled": True,
            "upcoming_days": 30,
        },
        categories=(
            CategoryRow(str(FOOD), "expense", "Food", "regular", "#4f9d69", 0, None),
            CategoryRow(
                str(ARCHIVED),
                "expense",
                "Car",
                "regular",
                "#a35d3b",
                1,
                "2025-12-31T18:05:00Z",
            ),
            CategoryRow(str(SALARY), "income", "Salary", "regular", "#2f7f5f", 0, None),
        ),
        budget_lines=(
            BudgetLineRow("2026-02", "expense", "Food", "50.00", currency, str(FOOD)),
            BudgetLineRow("2026-02", "expense", "Car", "20.00", currency, str(ARCHIVED)),
        ),
        transactions=(
            TransactionRow(
                "2026-02-01",
                "income",
                "100.00",
                currency,
                "Salary",
                "February",
                str(SALARY),
                str(SOURCE),
                None,
            ),
            TransactionRow(
                "2026-02-02",
                "expense",
                "12.50",
                currency,
                "Food",
                "Bakery",
                str(FOOD),
                str(uuid4()),
                None,
            ),
            TransactionRow(
                "2026-02-02",
                "expense",
                "4.00",
                currency,
                "Car",
                "Old entry",
                str(ARCHIVED),
                str(uuid4()),
                "2026-02-03T07:12:44Z",
            ),
        ),
        months=(MonthRow("2026-02", "2026-02-28T20:00:00Z", "2026-02-01T08:00:00Z"),),
        networth_categories=(
            NetWorthCategoryRow(str(NW_VEHICLES), "asset", "Vehicles", "#3b6ea8", 0, None),
        ),
        networth_items=(
            NetWorthItemRow(
                str(NW_CAR),
                "asset",
                "Vehicles",
                str(NW_VEHICLES),
                "Car",
                currency,
                "Family car",
                "2025-04-01",
                "manual",
                None,
                None,
                "true",
                "false",
                None,
            ),
            NetWorthItemRow(
                str(NW_BROKERAGE),
                "asset",
                "Vehicles",
                str(NW_VEHICLES),
                "Brokerage",
                currency,
                "",
                None,
                "linked",
                "portfolio_holding",
                str(NW_LINK),
                "true",
                "true",
                None,
            ),
        ),
        item_valuations=(
            ItemValuationRow(
                str(NW_FIRST_VALUE),
                str(NW_CAR),
                "Car",
                "2026-02-01",
                "12000.00",
                currency,
                "Bought",
                "2026-02-01T08:00:00Z",
            ),
            ItemValuationRow(
                str(NW_SECOND_VALUE),
                str(NW_CAR),
                "Car",
                "2026-02-01",
                "12050.00",
                currency,
                "",
                "2026-02-01T09:00:00Z",
            ),
        ),
        networth_snapshots=(
            NetWorthSnapshotRow(
                str(NW_SNAPSHOT),
                "2026-01-31",
                "12000.00",
                "0.00",
                currency,
                (
                    '{"categories":[{"category_id":"'
                    + str(NW_VEHICLES)
                    + '","side":"asset","total_minor":1200000}],"not_counted":'
                    '{"non_base":0,"no_value":0,"unavailable":0},"v":1}'
                ),
                "auto",
            ),
        ),
    )


def _source_workspace(db: WorkspaceDb) -> None:
    ctx = db.ctx("own", "owner")
    with db.factory.begin() as session:
        categories = CategoryRepository(session, ctx)
        food = categories.create(
            name="Food",
            name_key="food",
            kind="expense",
            treatment="regular",
            color="#4f9d69",
            sort_order=0,
            import_key=f"id:{FOOD}",
        )
        salary = categories.create(
            name="Salary",
            name_key="salary",
            kind="income",
            treatment="regular",
            color="#2f7f5f",
            sort_order=0,
            import_key=f"id:{SALARY}",
        )
        archived = categories.create(
            name="Car",
            name_key="car",
            kind="expense",
            treatment="regular",
            color="#a35d3b",
            sort_order=1,
            import_key=f"id:{ARCHIVED}",
            archived_at=datetime(2025, 12, 31, 18, 5, tzinfo=UTC),
        )
        transactions = TransactionRepository(session, ctx)
        transactions.create(
            direction="income",
            day=date(2026, 2, 1),
            amount_minor=10000,
            currency="EUR",
            category_id=salary.id,
            description="February",
            description_key="february",
            source="import",
            import_key=f"id:{SOURCE}",
            created_by_user_id=db.users["own"],
        )
        first = transactions.create(
            direction="expense",
            day=date(2026, 2, 2),
            amount_minor=1250,
            currency="EUR",
            category_id=food.id,
            description="Bakery",
            description_key="bakery",
            source="import",
            import_key=f"id:{uuid4()}",
            created_by_user_id=db.users["own"],
        )
        second = transactions.create(
            direction="expense",
            day=date(2026, 2, 2),
            amount_minor=400,
            currency="EUR",
            category_id=archived.id,
            description="Old entry",
            description_key="old entry",
            source="import",
            import_key=f"id:{uuid4()}",
            created_by_user_id=db.users["own"],
        )
        session.flush()
        second.deleted_at = datetime(2026, 2, 3, 7, 12, tzinfo=UTC)
        first.created_at = datetime(2026, 2, 3, 8, 0, tzinfo=UTC)
        second.created_at = datetime(2026, 2, 3, 9, 0, tzinfo=UTC)
        BudgetLineRepository(session, ctx).create(
            month=YearMonth(2026, 2),
            category_id=food.id,
            planned_minor=5000,
            currency="EUR",
        )
        BudgetLineRepository(session, ctx).create(
            month=YearMonth(2026, 2),
            category_id=archived.id,
            planned_minor=2000,
            currency="EUR",
        )
        MonthStateRepository(session, ctx).create_restored(
            month=YearMonth(2026, 2),
            closed_at=datetime(2026, 2, 28, 20, 0, tzinfo=UTC),
            closed_by_user_id=db.users["own"],
            plan_started_at=datetime(2026, 2, 1, 8, 0, tzinfo=UTC),
            created_at=EXPORTED_AT,
            updated_at=EXPORTED_AT,
        )
        networth_categories = NetWorthCategoryRepository(session, ctx)
        vehicles = networth_categories.create(
            name="Vehicles",
            name_key="vehicles",
            side="asset",
            color="#3b6ea8",
            sort_order=0,
            import_key=f"id:{NW_VEHICLES}",
        )
        networth_items = NetWorthItemRepository(session, ctx)
        car = networth_items.create(
            side="asset",
            category_id=vehicles.id,
            name="Car",
            currency="EUR",
            notes="Family car",
            acquired_on=date(2025, 4, 1),
            import_key=f"id:{NW_CAR}",
        )
        valuations = ItemValuationRepository(session, ctx)
        valuations.create(
            item_id=car.id,
            as_of=date(2026, 2, 1),
            value_minor=1200000,
            currency="EUR",
            note="Bought",
            import_key=f"id:{NW_FIRST_VALUE}",
            created_at=datetime(2026, 2, 1, 8, 0, tzinfo=UTC),
        )
        valuations.create(
            item_id=car.id,
            as_of=date(2026, 2, 1),
            value_minor=1205000,
            currency="EUR",
            import_key=f"id:{NW_SECOND_VALUE}",
            created_at=datetime(2026, 2, 1, 9, 0, tzinfo=UTC),
        )
        NetWorthSnapshotRepository(session, ctx).create(
            as_of=date(2026, 1, 31),
            assets_minor=1200000,
            liabilities_minor=0,
            currency="EUR",
            breakdown={
                "v": 1,
                "categories": [
                    {
                        "category_id": str(vehicles.id),
                        "side": "asset",
                        "total_minor": 1200000,
                    }
                ],
                "not_counted": {"non_base": 0, "no_value": 0, "unavailable": 0},
            },
            source="auto",
            import_key=f"id:{NW_SNAPSHOT}",
        )


def _target(db: WorkspaceDb) -> UUID:
    target = db.add_workspace("Restored")
    db.add_member("own", "owner", workspace_id=target)
    return target


def test_restore_round_trip_is_byte_identical(db: WorkspaceDb, data_dir: Path) -> None:
    _source_workspace(db)
    first = export_workspace(db.factory, db.ctx("own", "owner")).content
    target = _target(db)
    target_ctx = db.ctx("own", "owner", workspace_id=target)
    batch_id = _upload(db, data_dir, first, ctx=target_ctx)
    report = _plan(db, data_dir, batch_id, ctx=target_ctx)
    assert report["counts"]["invalid"] == 0, report
    _apply(db, data_dir, batch_id, ctx=target_ctx)

    second = export_workspace(db.factory, target_ctx).content
    first_parsed, errors = parse_archive(first)
    second_parsed, second_errors = parse_archive(second)
    assert errors == () and first_parsed is not None
    assert second_errors == () and second_parsed is not None
    assert first_parsed.files["workspace.json"] == second_parsed.files["workspace.json"]
    for name in (
        "categories.csv",
        "budget_lines.csv",
        "transactions.csv",
        "months.csv",
        "networth_categories.csv",
        "networth_items.csv",
        "item_valuations.csv",
        "networth_snapshots.csv",
    ):
        assert first_parsed.files[name] == second_parsed.files[name]
    assert first_parsed.manifest["files"] == second_parsed.manifest["files"]


def test_restore_refuses_non_empty_workspace_and_editor(db: WorkspaceDb, data_dir: Path) -> None:
    _source_workspace(db)
    content = export_workspace(db.factory, db.ctx("own", "owner")).content
    with pytest.raises(ImportRestoreUnavailable):
        _upload(db, data_dir, content, ctx=db.ctx("own", "owner"))
    with pytest.raises(PermissionDenied):
        _upload(db, data_dir, content, ctx=db.ctx("ed", "editor"))
    with pytest.raises(ImportRestoreRequiresArchive):
        _upload(db, data_dir, b"date,direction,amount,category\n", ctx=db.ctx("own", "owner"))


def test_untouched_suggested_categories_are_empty_enough_and_are_removed(
    db: WorkspaceDb, data_dir: Path
) -> None:
    target = _target(db)
    target_ctx = db.ctx("own", "owner", workspace_id=target)
    with db.factory.begin() as session:
        seed_into(
            session,
            target_ctx,
            locale="en",
            actor_user_id=db.users["own"],
            actor_type="user",
            now=db.clock.now(),
            automatic=True,
        )
        networth_seed_into(
            session,
            target_ctx,
            locale="en",
            actor_user_id=db.users["own"],
            actor_type="user",
            now=db.clock.now(),
            automatic=True,
        )
    with db.factory() as session:
        assert workspace_is_empty(session, target_ctx)
        assert len(CategoryRepository(session, target_ctx).list()) == len(SEED)
        assert len(NetWorthCategoryRepository(session, target_ctx).list()) == len(NETWORTH_SEED)

    batch_id = _upload(db, data_dir, _archive(_rows()), ctx=target_ctx)
    report = _plan(db, data_dir, batch_id, ctx=target_ctx)
    assert report["counts"]["invalid"] == 0, report
    assert report["counts"]["suggested_categories_removed"] == len(SEED)
    assert report["counts"]["suggested_networth_categories_removed"] == len(NETWORTH_SEED)
    _apply(db, data_dir, batch_id, ctx=target_ctx)
    with db.factory() as session:
        names = {row.name for row in CategoryRepository(session, target_ctx).list()}
        assert names == {"Food", "Car", "Salary"}
        networth_names = {
            row.name for row in NetWorthCategoryRepository(session, target_ctx).list()
        }
        assert networth_names == {"Vehicles"}

    # A restore rollback leaves the workspace empty; it does not re-seed.
    rollback_import(db.factory, target_ctx, batch_id)
    with db.factory() as session:
        assert CategoryRepository(session, target_ctx).list() == []
        assert NetWorthCategoryRepository(session, target_ctx).list() == []
        assert workspace_is_empty(session, target_ctx)


def test_polish_suggested_set_is_also_recognised(db: WorkspaceDb) -> None:
    target = _target(db)
    ctx = db.ctx("own", "owner", workspace_id=target)
    with db.factory.begin() as session:
        seed_into(
            session,
            ctx,
            locale="pl",
            actor_user_id=db.users["own"],
            actor_type="user",
            now=db.clock.now(),
            automatic=True,
        )
    with db.factory() as session:
        assert workspace_is_empty(session, ctx)


def test_m3_rows_make_a_workspace_non_empty(db: WorkspaceDb) -> None:
    target = _target(db)
    target_ctx = db.ctx("own", "owner", workspace_id=target)
    with db.factory.begin() as session:
        seed_into(
            session,
            target_ctx,
            locale="en",
            actor_user_id=db.users["own"],
            actor_type="user",
            now=db.clock.now(),
            automatic=True,
        )
        category = CategoryRepository(session, target_ctx).ordered()[0]
        ScheduledItemRepository(session, target_ctx).create(
            name="Rent",
            direction="expense",
            category_id=category.id,
            amount_mode="fixed",
            amount_minor=1000,
            currency="EUR",
            start_date=date(2026, 3, 1),
            freq="monthly",
            interval=1,
            weekdays=[],
            month_day="first",
            month=1,
            weekend_policy="none",
        )
    with db.factory() as session:
        assert not workspace_is_empty(session, target_ctx)


def test_seed_set_that_was_edited_is_not_empty(db: WorkspaceDb) -> None:
    target = _target(db)
    target_ctx = db.ctx("own", "owner", workspace_id=target)
    with db.factory.begin() as session:
        seed_into(
            session,
            target_ctx,
            locale="en",
            actor_user_id=db.users["own"],
            actor_type="user",
            now=db.clock.now(),
            automatic=True,
        )
        category = CategoryRepository(session, target_ctx).ordered()[0]
        CategoryRepository(session, target_ctx).update(category.id, name="Changed")
    with db.factory() as session:
        assert not workspace_is_empty(session, target_ctx)


def test_restore_replaces_settings_and_seeds_and_rolls_back(
    db: WorkspaceDb, data_dir: Path
) -> None:
    target = _target(db)
    target_ctx = db.ctx("own", "owner", workspace_id=target)
    with db.factory.begin() as session:
        categories = CategoryRepository(session, target_ctx)
        for index, (name, kind) in enumerate((("Food", "expense"), ("Salary", "income"))):
            categories.create(
                name=name,
                name_key=name.lower(),
                kind=kind,
                treatment="regular",
                color="#16a34a",
                sort_order=index,
            )
    # Seeded categories written by hand are only "untouched" with the automatic seed event, so
    # this workspace is deliberately not restorable.
    content = _archive(_rows())
    with pytest.raises(ImportRestoreUnavailable):
        _upload(db, data_dir, content, ctx=target_ctx)


def test_restore_into_truly_empty_workspace_applies_settings_and_rolls_back(
    db: WorkspaceDb, data_dir: Path
) -> None:
    target = _target(db)
    target_ctx = db.ctx("own", "owner", workspace_id=target)
    with db.factory.begin() as session:
        workspace = session.get(Workspace, target)
        assert workspace is not None
        workspace.require_two_factor = True
    secure_ctx = replace(target_ctx, second_factor_verified=True)
    batch_id = _upload(db, data_dir, _archive(_rows()), ctx=secure_ctx)
    report = _plan(db, data_dir, batch_id, ctx=secure_ctx)
    assert report["counts"]["invalid"] == 0, report
    _apply(db, data_dir, batch_id, ctx=secure_ctx)

    with db.factory() as session:
        workspace = session.get(Workspace, target)
        assert workspace is not None
        assert workspace.name == "Home"
        assert workspace.require_two_factor is True
        assert workspace.timezone == "Europe/Warsaw"
        assert workspace.locale == "pl"
        assert workspace.auto_copy_plan is True
        assert workspace.taxes_enabled is True
        assert workspace.upcoming_days == 30
        rows = CategoryRepository(session, target_ctx).ordered()
        assert {(row.name, row.archived_at is not None) for row in rows} == {
            ("Food", False),
            ("Car", True),
            ("Salary", False),
        }
        assert len(TransactionRepository(session, target_ctx).list()) == 3
        assert len(BudgetLineRepository(session, target_ctx).list()) == 2
        months = MonthStateRepository(session, target_ctx).list()
        assert len(months) == 1 and months[0].closed_at is not None
        networth_categories = NetWorthCategoryRepository(session, target_ctx).ordered()
        assert [(row.name, row.side) for row in networth_categories] == [("Vehicles", "asset")]
        networth_items = NetWorthItemRepository(session, target_ctx).ordered()
        assert {row.name: row for row in networth_items}.keys() == {"Car", "Brokerage"}
        brokerage = next(row for row in networth_items if row.name == "Brokerage")
        assert brokerage.valuation_mode == "manual"
        assert brokerage.link_type is None and brokerage.link_id is None
        assert ItemValuationRepository(session, target_ctx).for_item(brokerage.id) == []
        car = next(row for row in networth_items if row.name == "Car")
        values = ItemValuationRepository(session, target_ctx).for_item(car.id)
        assert [row.value_minor for row in values] == [1200000, 1205000]
        assert [row.created_at for row in values] == [
            datetime(2026, 2, 1, 8, 0, tzinfo=UTC),
            datetime(2026, 2, 1, 9, 0, tzinfo=UTC),
        ]
        snapshots = NetWorthSnapshotRepository(session, target_ctx).for_range(None)
        assert len(snapshots) == 1
        assert snapshots[0].source == "auto" and snapshots[0].net_minor == 1200000
        assert snapshots[0].breakdown["categories"][0]["category_id"] == str(
            networth_categories[0].id
        )
        actions = [
            event.action for event in session.query(AuditEvent).order_by(AuditEvent.ts).all()
        ]
        assert "workspace.restored" in actions
        assert "import.applied" not in actions
        batch = session.get(ImportBatch, batch_id)
        assert batch is not None
        assert batch.previous_settings is not None
        assert batch.previous_settings["name"] == "Restored"
        assert batch.previous_settings["timezone"] == "UTC"

    preview = preview_rollback_import(db.factory, secure_ctx, batch_id)
    assert preview.mode == "restore"
    assert preview.blocked_reasons == ()
    rollback_import(db.factory, secure_ctx, batch_id)
    with db.factory() as session:
        assert CategoryRepository(session, target_ctx).list() == []
        assert TransactionRepository(session, target_ctx).list() == []
        assert BudgetLineRepository(session, target_ctx).list() == []
        assert MonthStateRepository(session, target_ctx).list() == []
        assert NetWorthCategoryRepository(session, target_ctx).list() == []
        assert NetWorthItemRepository(session, target_ctx).list() == []
        assert ItemValuationRepository(session, target_ctx).list() == []
        assert NetWorthSnapshotRepository(session, target_ctx).list() == []
        workspace = session.get(Workspace, target)
        assert workspace is not None and workspace.name == "Restored"
        event = session.query(AuditEvent).filter(AuditEvent.action == "import.rolled_back").one()
        assert event.data["mode"] == "restore"
        assert event.data["months_deleted"] == 1


def test_restore_rollback_is_refused_after_a_change(db: WorkspaceDb, data_dir: Path) -> None:
    target = _target(db)
    target_ctx = db.ctx("own", "owner", workspace_id=target)
    batch_id = _upload(db, data_dir, _archive(_rows()), ctx=target_ctx)
    _plan(db, data_dir, batch_id, ctx=target_ctx)
    _apply(db, data_dir, batch_id, ctx=target_ctx)

    later = db.clock.now() + timedelta(seconds=1)
    with db.factory.begin() as session:
        session.execute(
            update(Category)
            .where(Category.workspace_id == target)
            .values(name="Changed", updated_at=later)
        )
    preview = preview_rollback_import(db.factory, target_ctx, batch_id)
    assert "restore_rows_changed" in preview.blocked_reasons
    with pytest.raises(ImportRestoreRollbackBlocked):
        rollback_import(db.factory, target_ctx, batch_id)


@pytest.mark.parametrize(
    ("fixture", "taxes", "upcoming"),
    [
        ("workspace-archive-v1.0.zip", False, None),
        ("workspace-archive-v1.1.zip", True, 14),
    ],
)
def test_restore_reads_released_golden_archives(
    db: WorkspaceDb, data_dir: Path, fixture: str, taxes: bool, upcoming: int | None
) -> None:
    content = (Path(__file__).parents[1] / "fixtures" / fixture).read_bytes()
    target = _target(db)
    ctx = db.ctx("own", "owner", workspace_id=target)
    batch_id = _upload(db, data_dir, content, ctx=ctx)
    report = _plan(db, data_dir, batch_id, ctx=ctx)
    assert report["counts"]["invalid"] == 0, report
    _apply(db, data_dir, batch_id, ctx=ctx)
    with db.factory() as session:
        workspace = session.get(Workspace, target)
        assert workspace is not None
        assert workspace.taxes_enabled is taxes
        assert workspace.upcoming_days == upcoming
        assert CategoryRepository(session, ctx).list()
        assert TransactionRepository(session, ctx).list()


def test_restore_reads_the_v1_2_golden_archive(db: WorkspaceDb, data_dir: Path) -> None:
    content = (Path(__file__).parents[1] / "fixtures" / "workspace-archive-v1.2.zip").read_bytes()
    target = _target(db)
    ctx = db.ctx("own", "owner", workspace_id=target)
    batch_id = _upload(db, data_dir, content, ctx=ctx)
    report = _plan(db, data_dir, batch_id, ctx=ctx)
    assert report["counts"]["invalid"] == 0, report
    assert report["counts"]["networth_categories_created"] == 1
    assert report["counts"]["networth_items_created"] == 2
    assert report["counts"]["item_valuations_created"] == 2
    assert report["counts"]["networth_snapshots_created"] == 1
    assert report["counts"]["linked_items_unlinked"] == 1
    _apply(db, data_dir, batch_id, ctx=ctx)
    with db.factory() as session:
        categories = NetWorthCategoryRepository(session, ctx).ordered()
        items = NetWorthItemRepository(session, ctx).ordered()
        assert [row.name for row in categories] == ["Vehicles"]
        assert [row.name for row in items] == ["Car", "Brokerage"]
        assert {row.valuation_mode for row in items} == {"manual"}
        car = next(row for row in items if row.name == "Car")
        values = ItemValuationRepository(session, ctx).for_item(car.id)
        assert [row.value_minor for row in values] == [1200000, 1205000]
        snapshots = NetWorthSnapshotRepository(session, ctx).for_range(None)
        assert snapshots[0].breakdown["categories"][0]["category_id"] == str(categories[0].id)


def test_restore_keeps_unknown_breakdown_category_ids(db: WorkspaceDb, data_dir: Path) -> None:
    rows = _rows()
    unknown = str(uuid4())
    snapshot = replace(
        rows.networth_snapshots[0],
        breakdown=(
            '{"categories":[{"category_id":"'
            + unknown
            + '","side":"asset","total_minor":1200000}],"not_counted":'
            '{"non_base":0,"no_value":0,"unavailable":0},"v":1}'
        ),
    )
    rows = replace(rows, networth_snapshots=(snapshot,))
    target = _target(db)
    ctx = db.ctx("own", "owner", workspace_id=target)
    batch_id = _upload(db, data_dir, _archive(rows), ctx=ctx)
    assert _plan(db, data_dir, batch_id, ctx=ctx)["counts"]["invalid"] == 0
    _apply(db, data_dir, batch_id, ctx=ctx)
    with db.factory() as session:
        stored = NetWorthSnapshotRepository(session, ctx).for_range(None)[0]
        assert stored.breakdown["categories"][0]["category_id"] == unknown


def test_newer_major_archive_is_refused_and_newer_minor_warns(
    db: WorkspaceDb, data_dir: Path
) -> None:
    target = _target(db)
    ctx = db.ctx("own", "owner", workspace_id=target)
    major_batch = _upload(db, data_dir, _with_version(_archive(_rows()), "2.0"), ctx=ctx)
    major = _plan(db, data_dir, major_batch, ctx=ctx)
    assert major["errors"][0]["code"] == "newer_major_version"
    assert major["counts"]["invalid"] == 1

    minor_batch = _upload(db, data_dir, _with_version(_archive(_rows()), "1.9"), ctx=ctx)
    minor = _plan(db, data_dir, minor_batch, ctx=ctx)
    assert minor["counts"]["invalid"] == 0
    assert any(warning["code"] == "newer_minor_version" for warning in minor["warnings"])


def test_merge_settings_diff_uses_1_0_defaults(db: WorkspaceDb, data_dir: Path) -> None:
    target = _target(db)
    ctx = db.ctx("own", "owner", workspace_id=target)
    content = (Path(__file__).parents[1] / "fixtures" / "workspace-archive-v1.0.zip").read_bytes()
    batch_id = _upload(db, data_dir, content, ctx=ctx, mode="merge")
    report = _plan(db, data_dir, batch_id, ctx=ctx)
    assert "taxes_enabled" not in report["settings_diff"]
    assert "upcoming_days" not in report["settings_diff"]


def test_restore_apply_detects_a_workspace_that_became_non_empty(
    db: WorkspaceDb, data_dir: Path
) -> None:
    target = _target(db)
    ctx = db.ctx("own", "owner", workspace_id=target)
    batch_id = _upload(db, data_dir, _archive(_rows()), ctx=ctx)
    _plan(db, data_dir, batch_id, ctx=ctx)
    with db.factory.begin() as session:
        CategoryRepository(session, ctx).create(
            name="Added elsewhere",
            name_key="added elsewhere",
            kind="expense",
            treatment="regular",
            color="#111111",
            sort_order=0,
        )
    with pytest.raises(ImportConfirmationRequired):
        _apply(db, data_dir, batch_id, ctx=ctx)
    with pytest.raises(ImportRestoreUnavailable):
        _apply(db, data_dir, batch_id, ctx=ctx)
    with db.factory() as session:
        batch = session.get(ImportBatch, batch_id)
        assert batch is not None and batch.status == "planned"
        assert batch.report is not None
        assert batch.report["errors"][0]["code"] == "workspace_not_empty"


def test_merge_records_malformed_rows_instead_of_dropping_them() -> None:
    rows = ArchiveRows(
        workspace={
            "name": "Home",
            "base_currency": "EUR",
            "country_code": None,
            "timezone": "UTC",
            "locale": "en",
            "auto_copy_plan": False,
            "taxes_enabled": False,
            "upcoming_days": None,
        },
        categories=(CategoryRow(str(FOOD), "expense", "Food", "regular", "#111111", 0, None),),
        budget_lines=(BudgetLineRow("2026-02", "expense", "Food", "oops", "EUR", str(FOOD)),),
        transactions=(
            TransactionRow(
                "2026-02-02", "expense", "oops", "EUR", "Food", "Broken", str(FOOD),
                str(uuid4()), None,
            ),
        ),
        months=(),
    )  # fmt: skip
    archive = ParsedArchive(manifest={}, files={}, rows=rows)
    sources = _source_categories(rows.categories)
    collected = _collect_sources(
        archive,
        sources=sources,
        currency="EUR",
        today=date(2026, 3, 1),
        closed=frozenset(),
        out_of_time=lambda: False,
    )
    assert collected.invalid == 2
    assert {(entry["file"], entry["code"]) for entry in collected.errors} == {
        ("transactions.csv", "invalid_amount"),
        ("budget_lines.csv", "invalid_amount"),
    }


def test_restore_rejects_duplicate_active_category_names(db: WorkspaceDb, data_dir: Path) -> None:
    rows = _rows()
    duplicate = ArchiveRows(
        workspace=rows.workspace,
        categories=(
            *rows.categories,
            CategoryRow(
                str(uuid4()),
                "expense",
                "food",
                "regular",
                "#111111",
                2,
                None,
            ),
        ),
        budget_lines=(),
        transactions=(),
        months=(),
    )
    target = _target(db)
    ctx = db.ctx("own", "owner", workspace_id=target)
    batch_id = _upload(db, data_dir, _archive(duplicate), ctx=ctx)
    report = _plan(db, data_dir, batch_id, ctx=ctx)
    assert report["errors"][0]["code"] == "duplicate_category_name"
    with pytest.raises(ServiceImportError):
        _apply(db, data_dir, batch_id, ctx=ctx)


def test_restore_rollback_refuses_a_row_added_after_it(db: WorkspaceDb, data_dir: Path) -> None:
    target = _target(db)
    ctx = db.ctx("own", "owner", workspace_id=target)
    batch_id = _upload(db, data_dir, _archive(_rows()), ctx=ctx)
    _plan(db, data_dir, batch_id, ctx=ctx)
    _apply(db, data_dir, batch_id, ctx=ctx)
    with db.factory.begin() as session:
        category = CategoryRepository(session, ctx).ordered()[0]
        TransactionRepository(session, ctx).create(
            direction="expense",
            day=date(2026, 3, 2),
            amount_minor=100,
            currency="EUR",
            category_id=category.id,
            description="Added later",
            description_key="added later",
            source="manual",
            created_by_user_id=db.users["own"],
        )
    preview = preview_rollback_import(db.factory, ctx, batch_id)
    assert "restore_rows_added" in preview.blocked_reasons
    with pytest.raises(ImportRestoreRollbackBlocked):
        rollback_import(db.factory, ctx, batch_id)


def test_restore_rollback_refuses_settings_month_and_m3_changes(
    db: WorkspaceDb, data_dir: Path
) -> None:
    target = _target(db)
    target_ctx = db.ctx("own", "owner", workspace_id=target)
    batch_id = _upload(db, data_dir, _archive(_rows()), ctx=target_ctx)
    _plan(db, data_dir, batch_id, ctx=target_ctx)
    _apply(db, data_dir, batch_id, ctx=target_ctx)
    later_clock = type(db.clock)((db.clock.now() + timedelta(seconds=1)).isoformat())
    with db.factory.begin() as session:
        session.info["clock"] = later_clock
        workspace = session.get(Workspace, target)
        assert workspace is not None
        workspace.name = "Changed"
        session.flush()
        month = MonthStateRepository(session, target_ctx).list()[0]
        MonthStateRepository(session, target_ctx).update(month.id, closed_at=None)
        category = CategoryRepository(session, target_ctx).ordered()[0]
        ScheduledItemRepository(session, target_ctx).create(
            name="Rent",
            direction="expense",
            category_id=category.id,
            amount_mode="fixed",
            amount_minor=1000,
            currency="EUR",
            start_date=date(2026, 3, 1),
            freq="monthly",
            interval=1,
            weekdays=[],
            month_day="first",
            month=1,
            weekend_policy="none",
        )
    preview = preview_rollback_import(db.factory, target_ctx, batch_id)
    assert {
        "restore_months_changed",
        "restore_settings_changed",
        "restore_m3_rows_added",
        "restore_m3_references",
    } <= set(preview.blocked_reasons)
    with pytest.raises(ImportRestoreRollbackBlocked):
        rollback_import(db.factory, target_ctx, batch_id)


def test_restore_rollback_owner_only_and_stepping_clock(db: WorkspaceDb, data_dir: Path) -> None:
    target = _target(db)
    target_ctx = db.ctx("own", "owner", workspace_id=target)
    batch_id = _upload(db, data_dir, _archive(_rows()), ctx=target_ctx)
    _plan(db, data_dir, batch_id, ctx=target_ctx)

    class SteppingClock(Clock):
        def __init__(self) -> None:
            self.value = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)

        def now(self) -> datetime:
            self.value += timedelta(microseconds=1)
            return self.value

        def today(self, tz: str) -> date:
            del tz
            return self.value.date()

    stepping_clock = SteppingClock()
    db.factory.configure(info={"clock": stepping_clock})
    stepping = replace(target_ctx, clock=stepping_clock)
    _apply(db, data_dir, batch_id, ctx=stepping)
    preview = preview_rollback_import(db.factory, target_ctx, batch_id)
    assert preview.blocked_reasons == ()
    rollback_import(db.factory, target_ctx, batch_id)


def test_merge_into_source_workspace_creates_nothing(db: WorkspaceDb, data_dir: Path) -> None:
    _source_workspace(db)
    content = export_workspace(db.factory, db.ctx("own", "owner")).content
    batch_id = _upload(db, data_dir, content, ctx=db.ctx("own", "owner"), mode="merge")
    report = _plan(db, data_dir, batch_id, ctx=db.ctx("own", "owner"))
    assert report["counts"]["to_create"] == 0, report
    assert [warning["code"] for warning in report["warnings"]] == ["networth_restore_only"]
    _apply(db, data_dir, batch_id, ctx=db.ctx("own", "owner"))
    with db.factory() as session:
        assert len(CategoryRepository(session, db.ctx("own", "owner")).list()) == 3
        assert len(TransactionRepository(session, db.ctx("own", "owner")).list()) == 3
        assert len(NetWorthItemRepository(session, db.ctx("own", "owner")).list()) == 1
        assert len(ItemValuationRepository(session, db.ctx("own", "owner")).list()) == 2


def test_merge_into_another_workspace_keeps_settings_and_maps_archived(
    db: WorkspaceDb, data_dir: Path
) -> None:
    target = _target(db)
    target_ctx = db.ctx("own", "owner", workspace_id=target)
    content = _archive(_rows())
    batch_id = _upload(db, data_dir, content, ctx=target_ctx, mode="merge")
    report = _plan(db, data_dir, batch_id, ctx=target_ctx)
    assert report["counts"]["invalid"] == 0, report
    assert report["archive_closed_months"] == ["2026-02"]
    assert "timezone" in report["settings_diff"]
    _apply(db, data_dir, batch_id, ctx=target_ctx)
    with db.factory() as session:
        assert CategoryRepository(session, target_ctx).list()
        transactions = TransactionRepository(session, target_ctx).list()
        assert len(transactions) == 2  # the deleted Car row is not imported in merge mode
        assert {row.description for row in transactions} == {"February", "Bakery"}
        lines = BudgetLineRepository(session, target_ctx).list()
        assert [(line.month, line.planned_minor) for line in lines] == [(date(2026, 2, 1), 5000)]
        months = MonthStateRepository(session, target_ctx).list()
        assert len(months) == 1 and months[0].plan_started_at is not None
        workspace = session.get(Workspace, target)
        assert workspace is not None and workspace.name == "Restored"


def test_merge_keeps_active_and_archived_categories_with_one_name(
    db: WorkspaceDb, data_dir: Path
) -> None:
    active = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    archived = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    rows = ArchiveRows(
        workspace={
            "name": "Home",
            "base_currency": "EUR",
            "country_code": None,
            "timezone": "UTC",
            "locale": "en",
            "auto_copy_plan": False,
            "taxes_enabled": False,
            "upcoming_days": None,
        },
        categories=(
            CategoryRow(str(active), "expense", "Car", "regular", "#111111", 0, None),
            CategoryRow(
                str(archived),
                "expense",
                "Car",
                "regular",
                "#222222",
                1,
                "2025-12-31T18:05:00Z",
            ),
        ),
        budget_lines=(
            BudgetLineRow("2026-02", "expense", "Car", "10.00", "EUR", str(active)),
            BudgetLineRow("2026-02", "expense", "Car", "20.00", "EUR", str(archived)),
        ),
        transactions=(
            TransactionRow(
                "2026-02-02",
                "expense",
                "1.00",
                "EUR",
                "Car",
                "Active",
                str(active),
                str(uuid4()),
                None,
            ),
            TransactionRow(
                "2026-02-03",
                "expense",
                "2.00",
                "EUR",
                "Car",
                "Archived",
                str(archived),
                str(uuid4()),
                None,
            ),
        ),
        months=(),
    )
    target = _target(db)
    ctx = db.ctx("own", "owner", workspace_id=target)
    batch_id = _upload(db, data_dir, _archive(rows), ctx=ctx, mode="merge")
    report = _plan(db, data_dir, batch_id, ctx=ctx)
    car_entries = [
        entry
        for entry in report["mapping"]
        if entry["kind"] == "expense" and entry["source_name_key"] == "car"
    ]
    assert {entry["source_id"] for entry in car_entries} == {str(active), str(archived)}
    assert {entry["action"] for entry in car_entries} == {"create"}
    _apply(db, data_dir, batch_id, ctx=ctx)
    with db.factory() as session:
        categories = CategoryRepository(session, ctx).list()
        cars = [row for row in categories if row.name == "Car"]
        assert len(cars) == 2
        assert {row.archived_at is not None for row in cars} == {False, True}
        transactions = TransactionRepository(session, ctx).list()
        assert len(transactions) == 2
        lines = BudgetLineRepository(session, ctx).list()
        assert [line.planned_minor for line in lines] == [1000]


def test_merge_skips_closed_and_future_rows_but_restore_keeps_them(
    db: WorkspaceDb, data_dir: Path
) -> None:
    rows = ArchiveRows(
        workspace={
            "name": "Home",
            "base_currency": "EUR",
            "country_code": None,
            "timezone": "UTC",
            "locale": "en",
            "auto_copy_plan": False,
            "taxes_enabled": False,
            "upcoming_days": None,
        },
        categories=(CategoryRow(str(FOOD), "expense", "Food", "regular", "#111111", 0, None),),
        budget_lines=(BudgetLineRow("2026-02", "expense", "Food", "10.00", "EUR", str(FOOD)),),
        transactions=(
            TransactionRow(
                "2026-02-02", "expense", "1.00", "EUR", "Food", "Closed", str(FOOD),
                str(uuid4()), None,
            ),
            TransactionRow(
                "2026-01-02", "expense", "2.00", "EUR", "Food", "Open", str(FOOD),
                str(uuid4()), None,
            ),
            TransactionRow(
                "2026-04-02", "expense", "3.00", "EUR", "Food", "Future", str(FOOD),
                str(uuid4()), None,
            ),
        ),
        months=(),
    )  # fmt: skip
    target = _target(db)
    ctx = db.ctx("own", "owner", workspace_id=target)
    with db.factory.begin() as session:
        MonthStateRepository(session, ctx).create_restored(
            month=YearMonth(2026, 2),
            closed_at=db.clock.now(),
            closed_by_user_id=db.users["own"],
            plan_started_at=None,
            created_at=db.clock.now(),
            updated_at=db.clock.now(),
        )
    batch_id = _upload(db, data_dir, _archive(rows), ctx=ctx, mode="merge")
    report = _plan(db, data_dir, batch_id, ctx=ctx)
    assert report["counts"]["closed_months_skipped"] == 2
    assert report["counts"]["future_dates_skipped"] == 1
    assert report["counts"]["to_create"] == 2  # the category and the open transaction
    _apply(db, data_dir, batch_id, ctx=ctx)
    with db.factory() as session:
        assert [row.description for row in TransactionRepository(session, ctx).list()] == ["Open"]

    with pytest.raises(ImportRestoreUnavailable):
        _upload(db, data_dir, _archive(rows), ctx=ctx, mode="restore")


def test_merge_maps_one_archive_category_to_an_existing_one(
    db: WorkspaceDb, data_dir: Path
) -> None:
    source = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    rows = ArchiveRows(
        workspace={
            "name": "Home",
            "base_currency": "EUR",
            "country_code": None,
            "timezone": "UTC",
            "locale": "en",
            "auto_copy_plan": False,
            "taxes_enabled": False,
            "upcoming_days": None,
        },
        categories=(CategoryRow(str(source), "expense", "Car", "regular", "#111111", 0, None),),
        budget_lines=(),
        transactions=(
            TransactionRow(
                "2026-02-02",
                "expense",
                "1.00",
                "EUR",
                "Car",
                "Fuel",
                str(source),
                str(uuid4()),
                None,
            ),
        ),
        months=(),
    )
    target = _target(db)
    ctx = db.ctx("own", "owner", workspace_id=target)
    with db.factory.begin() as session:
        transport = CategoryRepository(session, ctx).create(
            name="Transport",
            name_key="transport",
            kind="expense",
            treatment="regular",
            color="#333333",
            sort_order=0,
        )
    batch_id = _upload(db, data_dir, _archive(rows), ctx=ctx, mode="merge")
    _plan(db, data_dir, batch_id, ctx=ctx)
    options = {
        "mappings": [
            {
                "source_id": str(source),
                "kind": "expense",
                "source_name_key": "car",
                "action": "map",
                "category_id": str(transport.id),
                "create_name": "Car",
                "create_treatment": "regular",
                "create_color": "#111111",
            }
        ]
    }
    update_import_preview(db.factory, ctx, data_dir, batch_id, _adapters(), options=options)
    apply_import(
        db.factory,
        ctx,
        data_dir,
        batch_id,
        _adapters(),
        options=options,
    )
    with db.factory() as session:
        categories = CategoryRepository(session, ctx).list()
        assert [row.name for row in categories] == ["Transport"]
        [transaction] = TransactionRepository(session, ctx).list()
        assert transaction.category_id == transport.id


def test_merge_refuses_currency_mismatch_and_restore_accepts_it(
    db: WorkspaceDb, data_dir: Path
) -> None:
    target = _target(db)
    target_ctx = db.ctx("own", "owner", workspace_id=target)
    content = _archive(_rows(currency="PLN"))
    batch_id = _upload(db, data_dir, content, ctx=target_ctx, mode="merge")
    report = _plan(db, data_dir, batch_id, ctx=target_ctx)
    assert report["refused"] is True
    assert report["errors"][0]["code"] == "currency_mismatch"
    with pytest.raises(ImportRefused):
        _apply(db, data_dir, batch_id, ctx=target_ctx)

    restore_batch = _upload(db, data_dir, content, ctx=target_ctx, mode="restore")
    restore_report = _plan(db, data_dir, restore_batch, ctx=target_ctx)
    assert restore_report["counts"]["invalid"] == 0, restore_report
    _apply(db, data_dir, restore_batch, ctx=target_ctx)
