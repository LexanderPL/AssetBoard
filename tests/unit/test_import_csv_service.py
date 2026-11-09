"""F4.3 service behaviour: lone CSV adapters, mapping, saved mappings and rollback."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select

from assetboard.db.models.identity import AuditEvent
from assetboard.db.models.import_batch import ImportBatch, ImportCategoryMapping
from assetboard.db.models.ledger import BudgetLine, Category, MonthState, Transaction
from assetboard.db.repositories import (
    CategoryRepository,
    ImportCategoryMappingRepository,
)
from assetboard.domain.categories import PALETTE, name_key
from assetboard.domain.months import YearMonth
from assetboard.services.context import PermissionDenied
from assetboard.services.import_csv import default_import_adapters, mapping_category_options
from assetboard.services.imports import (
    ImportAdapterRegistry,
    ImportConfirmationRequired,
    ImportMappingInvalid,
    apply_import,
    close_imported_months,
    plan_import,
    rollback_import,
    update_import_preview,
    upload_import,
)
from assetboard.services.imports import (
    ImportError as ImportServiceError,
)
from assetboard.services.months import close_month
from tests.tenant.workspace_db import WorkspaceDb, workspace_db

UPLOAD = "upload.csv"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    for prepared in workspace_db(tmp_path):
        prepared.add_member("ed", "editor")
        prepared.add_member("vic", "viewer")
        yield prepared


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    return path


def _ctx(db: WorkspaceDb, key: str = "ed", role: str = "editor"):
    return db.ctx(key, role)


def _adapters() -> ImportAdapterRegistry:
    return ImportAdapterRegistry(default_import_adapters())


def _upload(
    db: WorkspaceDb,
    data_dir: Path,
    content: bytes,
    *,
    options: dict[str, object] | None = None,
) -> UUID:
    return upload_import(
        db.factory,
        _ctx(db),
        data_dir=data_dir,
        filename=UPLOAD,
        content=content,
        options=options or {"delimiter": "detect", "decimal": "dot"},
    )


def _plan(db: WorkspaceDb, data_dir: Path, batch_id: UUID) -> dict[str, Any]:
    return plan_import(db.factory, _ctx(db), data_dir, batch_id, _adapters())


def _apply(
    db: WorkspaceDb,
    data_dir: Path,
    batch_id: UUID,
    *,
    options: dict[str, object] | None = None,
    skip_invalid: bool = False,
) -> dict[str, Any]:
    return apply_import(
        db.factory,
        _ctx(db),
        data_dir,
        batch_id,
        _adapters(),
        skip_invalid=skip_invalid,
        options=options,
    )


def _batch(db: WorkspaceDb, batch_id: UUID) -> ImportBatch:
    with db.factory() as session:
        batch = session.get(ImportBatch, batch_id)
        assert batch is not None
        return batch


def _category(
    db: WorkspaceDb,
    name: str,
    *,
    kind: str = "expense",
    treatment: str = "regular",
    color: str = PALETTE[0],
    archived: bool = False,
) -> UUID:
    with db.factory.begin() as session:
        repository = CategoryRepository(session, _ctx(db))
        category = repository.create(
            name=name,
            name_key=name_key(name),
            kind=kind,
            treatment=treatment,
            color=color,
            sort_order=repository.next_sort_order(kind),
            archived_at=db.clock.now() if archived else None,
        )
        return category.id


def _categories(db: WorkspaceDb, kind: str | None = None) -> list[Category]:
    with db.factory() as session:
        return CategoryRepository(session, _ctx(db)).ordered(kind)


def _transactions(db: WorkspaceDb) -> list[Transaction]:
    with db.factory() as session:
        return list(
            session.scalars(
                select(Transaction)
                .where(Transaction.source == "import")
                .order_by(Transaction.date, Transaction.created_at, Transaction.id)
            )
        )


def _budget_lines(db: WorkspaceDb) -> list[BudgetLine]:
    with db.factory() as session:
        return list(
            session.scalars(select(BudgetLine).order_by(BudgetLine.month, BudgetLine.category_id))
        )


def _mappings(db: WorkspaceDb) -> list[ImportCategoryMapping]:
    with db.factory() as session:
        return list(
            session.scalars(
                select(ImportCategoryMapping).order_by(
                    ImportCategoryMapping.source_name_key, ImportCategoryMapping.id
                )
            )
        )


def _mapping_options(
    report: dict[str, Any],
    *,
    action: str | None = None,
    category_id: str | None = None,
    create_name: str | None = None,
    create_treatment: str | None = None,
    create_color: str | None = None,
    remember: bool = False,
) -> dict[str, Any]:
    entries: list[dict[str, str]] = []
    for entry in report["mapping"]:
        entries.append(
            {
                "kind": entry["kind"],
                "source_name_key": entry["source_name_key"],
                "action": action or entry["action"],
                "category_id": entry["category_id"] if category_id is None else category_id,
                "create_name": entry["create_name"] if create_name is None else create_name,
                "create_treatment": (
                    entry["create_treatment"] if create_treatment is None else create_treatment
                ),
                "create_color": entry["create_color"] if create_color is None else create_color,
            }
        )
    return {"mappings": entries, "remember_mappings": remember}


def _month_state(db: WorkspaceDb, month: str) -> MonthState | None:
    with db.factory() as session:
        return session.scalars(
            select(MonthState).where(MonthState.month == date.fromisoformat(f"{month}-01"))
        ).one_or_none()


# --- transactions ------------------------------------------------------------


def test_transactions_map_by_name_and_create_missing_categories(
    db: WorkspaceDb, data_dir: Path
) -> None:
    food = _category(db, "Food")
    salary = _category(db, "Salary", kind="income")
    content = (
        "date,direction,amount,category,description\n"
        "2026-02-01,expense,12.50,Food,Lunch\n"
        "2026-02-02,income,3000,Salary,Pay\n"
        "2026-02-03,expense,20,Fun stuff,Game\n"
        "2026-02-04,expense,100,Oszczędności,Save\n"
    ).encode()

    batch_id = _upload(db, data_dir, content)
    report = _plan(db, data_dir, batch_id)

    assert report["counts"]["to_create"] == 4
    by_name = {entry["source_name"]: entry for entry in report["mapping"]}
    assert (by_name["Food"]["action"], by_name["Food"]["category_id"]) == ("map", str(food))
    assert (by_name["Salary"]["action"], by_name["Salary"]["category_id"]) == (
        "map",
        str(salary),
    )
    assert (by_name["Fun stuff"]["action"], by_name["Fun stuff"]["create_treatment"]) == (
        "create",
        "regular",
    )
    assert (by_name["Oszczędności"]["action"], by_name["Oszczędności"]["create_treatment"]) == (
        "create",
        "savings",
    )
    assert report["months"][0]["month"] == "2026-02"
    assert report["months"][0]["expense_total"] == "132.50"
    assert report["months"][0]["income_total"] == "3000.00"

    _apply(db, data_dir, batch_id)

    imported = _transactions(db)
    assert len(imported) == 4
    assert {row.category_id for row in imported if row.direction == "expense"} == {
        food,
        *[
            category.id
            for category in _categories(db, "expense")
            if category.name in {"Fun stuff", "Oszczędności"}
        ],
    }
    created = {category.name: category for category in _categories(db)}
    assert created["Fun stuff"].treatment == "regular"
    assert created["Oszczędności"].treatment == "savings"
    assert created["Fun stuff"].sort_order == 1
    assert created["Oszczędności"].sort_order == 2
    assert created["Fun stuff"].color == PALETTE[1]
    assert created["Oszczędności"].color == PALETTE[2]
    assert created["Food"].color == PALETTE[0]

    # A second upload of the same hand-made file creates nothing.
    again = _upload(db, data_dir, content)
    report = _plan(db, data_dir, again)
    assert report["counts"]["to_create"] == 0
    assert report["counts"]["duplicates_skipped"] == 4
    _apply(db, data_dir, again)
    assert len(_transactions(db)) == 4


def test_transactions_of_a_transfer_category_import_normally(
    db: WorkspaceDb, data_dir: Path
) -> None:
    transfer = _category(db, "Transfer", treatment="transfer")
    batch_id = _upload(
        db,
        data_dir,
        b"date,direction,amount,category\n2026-02-01,expense,100,Transfer\n",
    )

    report = _plan(db, data_dir, batch_id)
    assert report["mapping"][0]["category_id"] == str(transfer)
    _apply(db, data_dir, batch_id)
    assert [row.category_id for row in _transactions(db)] == [transfer]


def test_identical_rows_in_one_file_are_both_imported(db: WorkspaceDb, data_dir: Path) -> None:
    _category(db, "Food")
    content = (
        b"date,direction,amount,category,description\n"
        b"2026-02-01,expense,12.50,Food,Bakery\n"
        b"2026-02-01,expense,12.50,Food,Bakery\n"
    )

    batch_id = _upload(db, data_dir, content)
    report = _plan(db, data_dir, batch_id)
    assert report["counts"]["to_create"] == 2

    _apply(db, data_dir, batch_id)
    assert len(_transactions(db)) == 2
    assert len({row.import_key for row in _transactions(db)}) == 2


def test_transaction_validation_codes_and_skip_invalid(db: WorkspaceDb, data_dir: Path) -> None:
    _category(db, "Food")
    content = (
        b"date,direction,amount,category,currency,description,id,deleted_at\n"
        b"not-a-date,expense,1,Food,,,, \n"
        b"2026-02-01,sideways,1,Food,,,,\n"
        b"2026-02-01,expense,0,Food,,,,\n"
        b"2026-02-01,expense,1.234,Food,,,,\n"
        b"2026-02-01,expense,1,Food,USD,,,\n"
        b"2026-02-01,expense,1,,, ,,\n"
        b"2026-02-01,expense,1,Food,,bad\x01,,\n"
        b"2026-02-01,expense,1,Food,,,not-a-uuid,\n"
        b"2026-02-01,expense,1,Food,,, ,bad-timestamp\n"
        b"2026-02-05,expense,10,Food,,OK,,\n"
    )

    batch_id = _upload(db, data_dir, content)
    report = _plan(db, data_dir, batch_id)

    assert report["counts"]["invalid"] == 9
    assert report["counts"]["to_create"] == 1
    assert {entry["code"] for entry in report["errors"]} == {
        "invalid_date",
        "invalid_direction",
        "amount_not_positive",
        "too_many_decimals",
        "invalid_currency",
        "invalid_category_name",
        "invalid_description",
        "invalid_id",
        "invalid_timestamp",
    }

    with pytest.raises(Exception, match="confirm skipping invalid rows"):
        _apply(db, data_dir, batch_id)
    assert _transactions(db) == []

    _apply(db, data_dir, batch_id, skip_invalid=True)
    assert [row.description for row in _transactions(db)] == ["OK"]


def test_deleted_source_rows_are_skipped_and_counted(db: WorkspaceDb, data_dir: Path) -> None:
    _category(db, "Food")
    batch_id = _upload(
        db,
        data_dir,
        b"date,direction,amount,category,deleted_at\n"
        b"2026-02-01,expense,10,Food,2026-02-02T10:00:00Z\n"
        b"2026-02-03,expense,20,Food,\n",
    )

    report = _plan(db, data_dir, batch_id)
    assert report["counts"]["deleted_skipped"] == 1
    assert [entry["code"] for entry in report["skipped"]] == ["deleted_in_source"]
    _apply(db, data_dir, batch_id)
    assert [row.amount_minor for row in _transactions(db)] == [2000]


def test_future_transactions_are_skipped_but_future_budget_months_are_not(
    db: WorkspaceDb, data_dir: Path
) -> None:
    _category(db, "Food")
    _category(db, "Salary", kind="income")
    transaction_batch = _upload(
        db,
        data_dir,
        b"date,direction,amount,category\n2026-03-02,expense,10,Food\n",
    )
    report = _plan(db, data_dir, transaction_batch)
    assert report["counts"]["future_dates_skipped"] == 1
    assert [entry["code"] for entry in report["skipped"]] == ["future_date"]
    _apply(db, data_dir, transaction_batch)
    assert _transactions(db) == []

    budget_batch = _upload(
        db,
        data_dir,
        b"month,kind,category,planned\n2026-04,expense,Food,100\n",
    )
    report = _plan(db, data_dir, budget_batch)
    assert report["counts"]["to_create"] == 1
    assert report["skipped"] == []
    _apply(db, data_dir, budget_batch)
    assert [line.planned_minor for line in _budget_lines(db)] == [10000]
    state = _month_state(db, "2026-04")
    assert state is not None and state.plan_started_at == db.clock.now()


def test_future_is_judged_in_the_workspace_time_zone(db: WorkspaceDb, data_dir: Path) -> None:
    _category(db, "Food")
    content = b"date,direction,amount,category\n2026-03-02,expense,10,Food\n"
    utc_batch = _upload(db, data_dir, content)
    assert _plan(db, data_dir, utc_batch)["counts"]["future_dates_skipped"] == 1

    local_batch = _upload(db, data_dir, content)
    ctx = _ctx(db)
    local_ctx = replace(ctx, workspace=replace(ctx.workspace, timezone="Pacific/Kiritimati"))
    report = plan_import(db.factory, local_ctx, data_dir, local_batch, _adapters())
    assert report["counts"]["future_dates_skipped"] == 0
    assert report["counts"]["to_create"] == 1


def test_closed_months_are_skipped_and_listed(db: WorkspaceDb, data_dir: Path) -> None:
    _category(db, "Food")
    with db.factory.begin() as session:
        session.add(
            MonthState(
                workspace_id=db.workspace_id,
                month=date(2026, 2, 1),
                closed_at=db.clock.now(),
            )
        )
    batch_id = _upload(
        db,
        data_dir,
        b"date,direction,amount,category\n2026-02-01,expense,10,Food\n2026-03-01,expense,20,Food\n",
    )

    report = _plan(db, data_dir, batch_id)
    assert report["counts"]["closed_months_skipped"] == 1
    assert report["closed_months"] == ["2026-02"]
    _apply(db, data_dir, batch_id)
    assert [row.amount_minor for row in _transactions(db)] == [2000]


def test_semicolon_and_decimal_comma_options_apply_to_values(
    db: WorkspaceDb, data_dir: Path
) -> None:
    _category(db, "Food")
    batch_id = _upload(
        db,
        data_dir,
        b"date;direction;amount;category;description\n2026-02-01;expense;12,50;Food;Lunch\n",
        options={"delimiter": "semicolon", "decimal": "comma"},
    )

    report = _plan(db, data_dir, batch_id)
    assert report["counts"]["to_create"] == 1
    _apply(db, data_dir, batch_id)
    assert [row.amount_minor for row in _transactions(db)] == [1250]


def test_category_id_in_a_lone_file_is_ignored_and_the_name_is_mapped(
    db: WorkspaceDb, data_dir: Path
) -> None:
    food = _category(db, "Food")
    other = _category(db, "Other")
    batch_id = _upload(
        db,
        data_dir,
        b"date,direction,amount,category,category_id\n"
        + f"2026-02-01,expense,10,Food,{other}\n".encode(),
    )

    report = _plan(db, data_dir, batch_id)
    assert report["mapping"][0]["action"] == "map"
    assert report["mapping"][0]["category_id"] == str(food)
    _apply(db, data_dir, batch_id)
    assert [row.category_id for row in _transactions(db)] == [food]


# --- budget lines ------------------------------------------------------------


def test_budget_lines_infer_the_kind_only_when_unambiguous(db: WorkspaceDb, data_dir: Path) -> None:
    _category(db, "Rent")
    _category(db, "Salary", kind="income")
    _category(db, "Shared")
    _category(db, "Shared", kind="income")
    _category(db, "Bonus")
    with db.factory.begin() as session:
        repository = ImportCategoryMappingRepository(session, _ctx(db))
        income = _categories(db, "income")[0].id
        repository.create(
            kind="income",
            source_name_key=name_key("Pay"),
            action="map",
            category_id=income,
        )
        repository.create(
            kind="income",
            source_name_key=name_key("Bonus"),
            action="map",
            category_id=income,
        )

    batch_id = _upload(
        db,
        data_dir,
        b"month,kind,category,planned\n"
        b"2026-02,expense,Rent,100\n"
        b"2026-03,,Rent,200\n"
        b"2026-02,,Pay,300\n"
        b"2026-02,,Shared,400\n"
        b"2026-02,,Bonus,600\n"
        b"2026-02,,Nowhere,500\n",
    )

    report = _plan(db, data_dir, batch_id)
    codes = [entry["code"] for entry in report["errors"]]
    assert codes.count("kind_required") == 3
    assert report["counts"]["to_create"] == 3
    by_name = {entry["source_name"]: entry for entry in report["mapping"]}
    assert by_name["Rent"]["kind"] == "expense"
    assert by_name["Pay"]["kind"] == "income"
    assert by_name["Pay"]["action"] == "map"
    assert by_name["Pay"]["category_id"] == str(_categories(db, "income")[0].id)


def test_archived_transfer_and_zero_budget_lines(db: WorkspaceDb, data_dir: Path) -> None:
    _category(db, "Food")
    archived = _category(db, "Archived", archived=True)
    _category(db, "Transfer", treatment="transfer")
    with db.factory.begin() as session:
        ImportCategoryMappingRepository(session, _ctx(db)).create(
            kind="expense",
            source_name_key=name_key("Archived"),
            action="map",
            category_id=archived,
        )
    batch_id = _upload(
        db,
        data_dir,
        b"month,kind,category,planned\n"
        b"2026-02,expense,Archived,10\n"
        b"2026-02,expense,Transfer,20\n"
        b"2026-02,expense,Food,0\n"
        b"2026-02,expense,Food,\n"
        b"2026-02,expense,Food,30\n",
    )

    report = _plan(db, data_dir, batch_id)
    assert report["counts"]["archived_categories_skipped"] == 1
    assert report["counts"]["transfer_categories_skipped"] == 1
    assert report["counts"]["to_create"] == 1
    assert {entry["code"] for entry in report["skipped"]} == {
        "category_archived",
        "category_transfer",
    }

    _apply(db, data_dir, batch_id)
    assert [line.planned_minor for line in _budget_lines(db)] == [3000]
    state = _month_state(db, "2026-02")
    assert state is not None and state.plan_started_at == db.clock.now()


def test_rollback_clears_only_plan_markers_the_batch_stamped(
    db: WorkspaceDb, data_dir: Path
) -> None:
    _category(db, "Food")
    existing_marker = db.clock.now()
    with db.factory.begin() as session:
        session.add(
            MonthState(
                workspace_id=db.workspace_id,
                month=date(2026, 2, 1),
                plan_started_at=existing_marker,
            )
        )
    batch_id = _upload(
        db,
        data_dir,
        b"month,kind,category,planned\n2026-02,expense,Food,100\n2026-03,expense,Food,200\n",
    )
    _plan(db, data_dir, batch_id)
    _apply(db, data_dir, batch_id)
    report = _batch(db, batch_id).report
    assert report is not None
    assert report["plan_started_months"] == ["2026-03"]

    rollback_import(db.factory, _ctx(db), batch_id)

    assert _budget_lines(db) == []
    february = _month_state(db, "2026-02")
    march = _month_state(db, "2026-03")
    assert february is not None and february.plan_started_at == existing_marker
    assert march is not None and march.plan_started_at is None


def test_budget_lines_are_idempotent(db: WorkspaceDb, data_dir: Path) -> None:
    _category(db, "Food")
    content = b"month,kind,category,planned\n2026-02,expense,Food,300\n"
    first = _upload(db, data_dir, content)
    _plan(db, data_dir, first)
    _apply(db, data_dir, first)

    second = _upload(db, data_dir, content)
    report = _plan(db, data_dir, second)
    assert report["counts"]["to_create"] == 0
    assert report["counts"]["duplicates_skipped"] == 1
    _apply(db, data_dir, second)
    assert len(_budget_lines(db)) == 1


# --- categories --------------------------------------------------------------


def test_categories_csv_creates_appends_and_keeps_archive_attributes(
    db: WorkspaceDb, data_dir: Path
) -> None:
    _category(db, "Existing")
    food_id = UUID("11111111-1111-4111-8111-111111111111")
    retired_id = UUID("22222222-2222-4222-8222-222222222222")
    salary_id = UUID("33333333-3333-4333-8333-333333333333")
    content = (
        b"id,kind,name,treatment,color,sort_order,archived_at\n"
        + (
            f"{food_id},expense,Food,regular,#111111,99,\n"
            f"{retired_id},expense,Retired,savings,#222222,0,2025-12-31T18:05:00Z\n"
            f"{salary_id},income,Salary,regular,,0,\n"
        ).encode()
    )

    batch_id = _upload(db, data_dir, content)
    report = _plan(db, data_dir, batch_id)
    assert report["counts"]["to_create"] == 3
    assert report["mapping"] == []

    _apply(db, data_dir, batch_id)
    categories = _categories(db)
    expense = [category for category in categories if category.kind == "expense"]
    assert [category.name for category in expense] == ["Existing", "Food", "Retired"]
    assert [category.sort_order for category in expense] == [0, 1, 2]
    food = next(category for category in expense if category.name == "Food")
    retired = next(category for category in expense if category.name == "Retired")
    salary = next(category for category in categories if category.name == "Salary")
    assert food.import_key == f"id:{food_id}"
    assert food.color == "#111111"
    assert retired.archived_at is not None and retired.treatment == "savings"
    assert salary.sort_order == 0
    assert salary.color == PALETTE[0]

    again = _upload(db, data_dir, content)
    report = _plan(db, data_dir, again)
    assert report["counts"]["to_create"] == 0
    assert report["counts"]["duplicates_skipped"] == 3
    assert {entry["code"] for entry in report["skipped"]} == {"already_imported"}


def test_categories_csv_allows_an_active_and_an_archived_category_with_one_name(
    db: WorkspaceDb, data_dir: Path
) -> None:
    active_id = UUID("44444444-4444-4444-8444-444444444444")
    archived_id = UUID("55555555-5555-4555-8555-555555555555")
    content = (
        b"id,kind,name,treatment,color,sort_order,archived_at\n"
        + (
            f"{active_id},expense,Car,regular,#111111,0,\n"
            f"{archived_id},expense,Car,regular,#222222,1,2025-12-31T18:05:00Z\n"
        ).encode()
    )

    batch_id = _upload(db, data_dir, content)
    report = _plan(db, data_dir, batch_id)
    assert report["counts"]["to_create"] == 2
    assert report["skipped"] == []

    _apply(db, data_dir, batch_id)
    cars = [category for category in _categories(db) if category.name == "Car"]
    assert len(cars) == 2
    assert {category.archived_at is not None for category in cars} == {False, True}


def test_categories_csv_dedupes_archived_rows_without_ids(db: WorkspaceDb, data_dir: Path) -> None:
    content = (
        b"kind,name,treatment,color,archived_at\n"
        b"expense,Car,regular,#111111,2025-12-31T18:05:00Z\n"
        b"expense,Car,regular,#111111,2025-12-31T18:05:00Z\n"
    )

    batch_id = _upload(db, data_dir, content)
    report = _plan(db, data_dir, batch_id)
    assert report["counts"]["to_create"] == 1
    assert [entry["code"] for entry in report["skipped"]] == ["duplicate_in_file"]
    _apply(db, data_dir, batch_id)

    again = _upload(db, data_dir, content)
    report = _plan(db, data_dir, again)
    assert report["counts"]["to_create"] == 0
    assert {entry["code"] for entry in report["skipped"]} == {
        "already_imported",
        "duplicate_in_file",
    }
    assert len([category for category in _categories(db) if category.name == "Car"]) == 1


def test_categories_csv_lists_existing_differences_and_file_duplicates(
    db: WorkspaceDb, data_dir: Path
) -> None:
    _category(db, "Food")
    content = (
        b"id,kind,name,treatment,color,sort_order,archived_at\n"
        b",expense,Food,savings,#111111,0,\n"
        b",expense,New,regular,#111111,0,\n"
        b",expense,New,savings,#111111,0,\n"
        b"11111111-1111-4111-8111-111111111111,expense,Other,regular,,0,\n"
        b"11111111-1111-4111-8111-111111111111,expense,Other,regular,,0,\n"
    )

    report = _plan(db, data_dir, _upload(db, data_dir, content))

    assert report["counts"]["to_create"] == 2
    assert [entry["code"] for entry in report["skipped"]] == [
        "category_differs",
        "conflict_in_file",
        "duplicate_in_file",
    ]


def test_categories_csv_validation_codes(db: WorkspaceDb, data_dir: Path) -> None:
    content = (
        b"id,kind,name,treatment,color,sort_order,archived_at\n"
        b",,No kind,regular,,0,\n"
        b",invalid,Bad kind,regular,,0,\n"
        b",expense,,regular,,0,\n"
        b",expense,Bad treatment,other,,0,\n"
        b",expense,Bad colour,regular,red,0,\n"
        b",expense,Bad order,regular,,nope,\n"
        b",expense,Bad archive,regular,,0,yesterday\n"
        b"not-a-uuid,expense,Bad id,regular,,0,\n"
    )

    report = _plan(db, data_dir, _upload(db, data_dir, content))

    assert report["counts"]["invalid"] == 8
    assert {entry["code"] for entry in report["errors"]} == {
        "kind_required",
        "invalid_kind",
        "invalid_category_name",
        "invalid_treatment",
        "invalid_color",
        "invalid_sort_order",
        "invalid_timestamp",
        "invalid_id",
    }


# --- mapping step ------------------------------------------------------------


def test_invalid_mappings_block_apply_and_preview_saves_choices(
    db: WorkspaceDb, data_dir: Path
) -> None:
    _category(db, "Food")
    batch_id = _upload(
        db,
        data_dir,
        b"date,direction,amount,category\n2026-02-01,expense,10,Food\n2026-02-02,expense,20,New\n",
    )
    report = _plan(db, data_dir, batch_id)
    new_entry = next(entry for entry in report["mapping"] if entry["source_name"] == "New")

    bad: dict[str, object] = {
        "mappings": [
            {
                "kind": new_entry["kind"],
                "source_name_key": new_entry["source_name_key"],
                "action": "create",
                "category_id": "",
                "create_name": "Food",
                "create_treatment": "regular",
                "create_color": new_entry["create_color"],
            }
        ]
    }
    preview = update_import_preview(
        db.factory, _ctx(db), data_dir, batch_id, _adapters(), options=bad
    )
    assert preview["counts"]["mapping_invalid"] == 1
    assert (
        next(entry for entry in preview["mapping"] if entry["source_name"] == "New")["error"]
        == "category_exists"
    )
    assert _batch(db, batch_id).options["mappings"] == bad["mappings"]
    assert _transactions(db) == []

    with pytest.raises(ImportMappingInvalid):
        _apply(db, data_dir, batch_id, options=bad)
    assert _transactions(db) == []

    fixed = _mapping_options(report)
    fixed["mappings"][1]["action"] = "ignore"
    with pytest.raises(ImportConfirmationRequired):
        _apply(db, data_dir, batch_id, options=fixed)
    assert _transactions(db) == []

    _apply(db, data_dir, batch_id, options=fixed)
    assert [row.description for row in _transactions(db)] == [""]
    assert [row.amount_minor for row in _transactions(db)] == [1000]


def test_duplicate_and_empty_create_names_block_apply(db: WorkspaceDb, data_dir: Path) -> None:
    batch_id = _upload(
        db,
        data_dir,
        b"date,direction,amount,category\n"
        b"2026-02-01,expense,10,Alpha\n"
        b"2026-02-02,expense,20,Beta\n",
    )
    report = _plan(db, data_dir, batch_id)
    duplicate = _mapping_options(report)
    for entry in duplicate["mappings"]:
        entry["action"] = "create"
        entry["create_name"] = "Same"
    duplicate["mappings"][0]["create_name"] = ""

    preview = update_import_preview(
        db.factory, _ctx(db), data_dir, batch_id, _adapters(), options=duplicate
    )
    assert preview["counts"]["mapping_invalid"] == 1
    assert {entry["code"] for entry in preview["errors"]} == {"invalid_category_name"}
    stored = _batch(db, batch_id).options["mappings"]
    assert stored[0]["action"] == "create"
    assert stored[0]["create_name"] == ""
    with pytest.raises(ImportMappingInvalid):
        _apply(db, data_dir, batch_id, options=duplicate)

    same_name = _mapping_options(report)
    for entry in same_name["mappings"]:
        entry["action"] = "create"
        entry["create_name"] = "Same"
    preview = update_import_preview(
        db.factory, _ctx(db), data_dir, batch_id, _adapters(), options=same_name
    )
    assert preview["counts"]["mapping_invalid"] == 1
    assert {entry["code"] for entry in preview["errors"]} == {"duplicate_create_name"}
    with pytest.raises(ImportMappingInvalid):
        _apply(db, data_dir, batch_id, options=same_name)
    assert _transactions(db) == []


def test_mapping_choices_change_requires_confirmation_then_applies(
    db: WorkspaceDb, data_dir: Path
) -> None:
    _category(db, "Food")
    _category(db, "Groceries")
    batch_id = _upload(
        db,
        data_dir,
        b"date,direction,amount,category\n2026-02-01,expense,10,Food\n",
    )
    report = _plan(db, data_dir, batch_id)
    food_entry = report["mapping"][0]
    groceries = next(category for category in _categories(db) if category.name == "Groceries")
    changed = _mapping_options(report, category_id=str(groceries.id))

    with pytest.raises(ImportConfirmationRequired):
        _apply(db, data_dir, batch_id, options=changed)
    assert _transactions(db) == []
    assert _batch(db, batch_id).status == "planned"

    _apply(db, data_dir, batch_id, options=changed)
    assert [row.category_id for row in _transactions(db)] == [groceries.id]
    assert food_entry["action"] == "map"


def test_remembered_mappings_are_written_on_apply_and_removed_for_created_categories_on_rollback(
    db: WorkspaceDb, data_dir: Path
) -> None:
    food = _category(db, "Food")
    batch_id = _upload(
        db,
        data_dir,
        b"date,direction,amount,category\n"
        b"2026-02-01,expense,10,Food\n"
        b"2026-02-02,expense,20,New stuff\n",
        options={
            "delimiter": "detect",
            "decimal": "dot",
            "remember_mappings": True,
        },
    )
    _plan(db, data_dir, batch_id)
    assert _mappings(db) == []

    _apply(db, data_dir, batch_id)
    mappings = {mapping.source_name_key: mapping for mapping in _mappings(db)}
    assert mappings[name_key("Food")].category_id == food
    created = next(category for category in _categories(db) if category.name == "New stuff")
    assert mappings[name_key("New stuff")].category_id == created.id

    rollback_import(db.factory, _ctx(db), batch_id)

    remaining = {mapping.source_name_key for mapping in _mappings(db)}
    assert remaining == {name_key("Food")}
    with db.factory() as session:
        stored = session.get(Category, created.id)
        assert stored is not None and stored.archived_at is not None


def test_preview_never_writes_saved_mappings(db: WorkspaceDb, data_dir: Path) -> None:
    _category(db, "Food")
    batch_id = _upload(
        db,
        data_dir,
        b"date,direction,amount,category\n2026-02-01,expense,10,Food\n",
    )
    report = _plan(db, data_dir, batch_id)

    update_import_preview(
        db.factory,
        _ctx(db),
        data_dir,
        batch_id,
        _adapters(),
        options={**_mapping_options(report), "remember_mappings": True},
    )

    assert _mappings(db) == []


def test_viewer_cannot_upload_or_apply(db: WorkspaceDb, data_dir: Path) -> None:
    with pytest.raises(PermissionDenied):
        upload_import(
            db.factory,
            _ctx(db, "vic", "viewer"),
            data_dir=data_dir,
            filename=UPLOAD,
            content=b"date,direction,amount,category\n",
        )

    batch_id = _upload(db, data_dir, b"date,direction,amount,category\n")
    _plan(db, data_dir, batch_id)
    with pytest.raises(PermissionDenied):
        apply_import(
            db.factory,
            _ctx(db, "vic", "viewer"),
            data_dir,
            batch_id,
            _adapters(),
        )


def test_mapping_categories_include_archived_targets(db: WorkspaceDb) -> None:
    _category(db, "Food")
    _category(db, "Retired", archived=True)

    options = mapping_category_options(db.factory, _ctx(db))

    assert {option.name for option in options} == {"Food", "Retired"}
    assert {option.archived for option in options} == {False, True}


# --- closing the past months of an applied merge import ----------------------


def _import_batch(db: WorkspaceDb, *, mode: str, status: str) -> UUID:
    """A batch row without an upload, for eligibility checks."""
    with db.factory.begin() as session:
        batch = ImportBatch(
            workspace_id=db.workspace_id,
            source="archive",
            mode=mode,
            filename="archive.zip",
            file_sha256="0" * 64,
            size_bytes=0,
            options={},
            status=status,
        )
        session.add(batch)
        session.flush()
        return batch.id


def _month_closed_events(db: WorkspaceDb) -> list[AuditEvent]:
    with db.factory() as session:
        return list(
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.action == "month.closed")
                .order_by(AuditEvent.ts, AuditEvent.id)
            )
        )


def test_close_imported_months_closes_past_months_only_and_audits(
    db: WorkspaceDb, data_dir: Path
) -> None:
    _category(db, "Food")
    batch_id = _upload(
        db,
        data_dir,
        b"date,direction,amount,category\n"
        b"2026-01-10,expense,10,Food\n"
        b"2026-02-10,expense,20,Food\n"
        b"2026-02-20,expense,30,Food\n"
        b"2026-03-10,expense,40,Food\n",
    )
    _plan(db, data_dir, batch_id)
    _apply(db, data_dir, batch_id)

    closed = close_imported_months(db.factory, _ctx(db), batch_id)

    assert [str(month) for month in closed] == ["2026-01", "2026-02"]
    january = _month_state(db, "2026-01")
    february = _month_state(db, "2026-02")
    assert january is not None and january.closed_at == db.clock.now()
    assert february is not None and february.closed_at == db.clock.now()
    assert january.closed_by_user_id == db.users["ed"]
    assert february.closed_by_user_id == db.users["ed"]
    assert _month_state(db, "2026-03") is None
    events = _month_closed_events(db)
    assert sorted(event.data["month"] for event in events) == ["2026-01", "2026-02"]
    assert {event.actor_user_id for event in events} == {db.users["ed"]}


def test_close_imported_months_includes_planned_months_but_skips_current_and_future(
    db: WorkspaceDb, data_dir: Path
) -> None:
    _category(db, "Food")
    batch_id = _upload(
        db,
        data_dir,
        b"month,kind,category,planned\n"
        b"2026-01,expense,Food,10\n"
        b"2026-02,expense,Food,20\n"
        b"2026-03,expense,Food,30\n"
        b"2026-04,expense,Food,40\n",
    )
    _plan(db, data_dir, batch_id)
    _apply(db, data_dir, batch_id)

    closed = close_imported_months(db.factory, _ctx(db), batch_id)

    assert [str(month) for month in closed] == ["2026-01", "2026-02"]
    march = _month_state(db, "2026-03")
    april = _month_state(db, "2026-04")
    assert march is not None and march.closed_at is None
    assert april is not None and april.closed_at is None


def test_close_imported_months_leaves_already_closed_months_alone(
    db: WorkspaceDb, data_dir: Path
) -> None:
    _category(db, "Food")
    batch_id = _upload(
        db,
        data_dir,
        b"date,direction,amount,category\n2026-01-10,expense,10,Food\n2026-02-10,expense,20,Food\n",
    )
    _plan(db, data_dir, batch_id)
    _apply(db, data_dir, batch_id)
    assert close_month(db.factory, _ctx(db), YearMonth(2026, 1)) is True
    january = _month_state(db, "2026-01")
    assert january is not None

    closed = close_imported_months(db.factory, _ctx(db), batch_id)

    assert [str(month) for month in closed] == ["2026-02"]
    refreshed = _month_state(db, "2026-01")
    assert refreshed is not None and refreshed.closed_at == january.closed_at
    assert sorted(event.data["month"] for event in _month_closed_events(db)) == [
        "2026-01",
        "2026-02",
    ]


def test_close_imported_months_ignores_soft_deleted_transactions(
    db: WorkspaceDb, data_dir: Path
) -> None:
    _category(db, "Food")
    batch_id = _upload(
        db,
        data_dir,
        b"date,direction,amount,category\n2026-01-10,expense,10,Food\n2026-02-10,expense,20,Food\n",
    )
    _plan(db, data_dir, batch_id)
    _apply(db, data_dir, batch_id)
    with db.factory.begin() as session:
        january_row = session.scalars(
            select(Transaction).where(Transaction.date == date(2026, 1, 10))
        ).one()
        january_row.deleted_at = db.clock.now()

    closed = close_imported_months(db.factory, _ctx(db), batch_id)

    assert [str(month) for month in closed] == ["2026-02"]
    assert _month_state(db, "2026-01") is None


def test_close_imported_months_requires_an_editor_and_rechecks_the_role(
    db: WorkspaceDb, data_dir: Path
) -> None:
    _category(db, "Food")
    batch_id = _upload(
        db,
        data_dir,
        b"date,direction,amount,category\n2026-01-10,expense,10,Food\n",
    )
    _plan(db, data_dir, batch_id)
    _apply(db, data_dir, batch_id)

    with pytest.raises(PermissionDenied):
        close_imported_months(db.factory, _ctx(db, "vic", "viewer"), batch_id)
    assert _month_state(db, "2026-01") is None

    db.add_member("dee", "viewer")
    with pytest.raises(PermissionDenied):
        close_imported_months(db.factory, _ctx(db, "dee", "editor"), batch_id)
    assert _month_state(db, "2026-01") is None


def test_close_imported_months_refuses_restore_and_non_applied_batches(
    db: WorkspaceDb,
) -> None:
    restore_id = _import_batch(db, mode="restore", status="applied")
    with pytest.raises(ImportServiceError):
        close_imported_months(db.factory, _ctx(db), restore_id)

    planned_id = _import_batch(db, mode="merge", status="planned")
    with pytest.raises(ImportServiceError):
        close_imported_months(db.factory, _ctx(db), planned_id)
