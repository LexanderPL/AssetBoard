"""F5.5 standalone ``item_valuations.csv`` import and net-worth archive behaviour."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from assetboard.db.base import AppendOnlyViolationError
from assetboard.db.models.networth import ItemValuation
from assetboard.db.repositories import (
    ItemValuationRepository,
    NetWorthCategoryRepository,
    NetWorthItemRepository,
    NetWorthSnapshotRepository,
)
from assetboard.db.repositories.base import UnsupportedOperation
from assetboard.domain.networth import SEED as NETWORTH_SEED
from assetboard.services.archive import workspace_is_empty
from assetboard.services.import_csv import default_import_adapters
from assetboard.services.imports import (
    ImportAdapterRegistry,
    apply_import,
    plan_import,
    preview_rollback_import,
    rollback_import,
    upload_import,
)
from assetboard.services.networth import seed_into
from tests.tenant.workspace_db import WorkspaceDb, workspace_db

HEADER = b"item,as_of,value,currency,note,item_id\n"


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


def _upload(db: WorkspaceDb, data_dir: Path, content: bytes) -> UUID:
    return upload_import(
        db.factory,
        _ctx(db),
        data_dir=data_dir,
        filename="item_valuations.csv",
        content=content,
        options={"delimiter": "detect", "decimal": "dot"},
    )


def _plan(db: WorkspaceDb, data_dir: Path, batch_id: UUID) -> dict[str, Any]:
    return plan_import(db.factory, _ctx(db), data_dir, batch_id, _adapters())


def _apply(
    db: WorkspaceDb, data_dir: Path, batch_id: UUID, *, skip_invalid: bool = False
) -> dict[str, Any]:
    return apply_import(
        db.factory,
        _ctx(db),
        data_dir,
        batch_id,
        _adapters(),
        skip_invalid=skip_invalid,
    )


def _category(db: WorkspaceDb, name: str = "Vehicles", side: str = "asset") -> UUID:
    with db.factory.begin() as session:
        category = NetWorthCategoryRepository(session, _ctx(db)).create(
            name=name,
            name_key=name.casefold(),
            side=side,
            color="#3b6ea8",
            sort_order=0,
        )
        return category.id


def _item(
    db: WorkspaceDb,
    category_id: UUID,
    name: str,
    *,
    currency: str = "EUR",
    mode: str = "manual",
    link_id: UUID | None = None,
) -> UUID:
    with db.factory.begin() as session:
        item = NetWorthItemRepository(session, _ctx(db)).create(
            side="asset",
            category_id=category_id,
            name=name,
            currency=currency,
            valuation_mode=mode,
            link_type="portfolio_holding" if mode == "linked" else None,
            link_id=link_id,
        )
        return item.id


def _values(db: WorkspaceDb, item_id: UUID) -> list[ItemValuation]:
    with db.factory() as session:
        return ItemValuationRepository(session, _ctx(db)).for_item(item_id)


def test_import_matches_item_id_first_and_lists_unmatched_items(
    db: WorkspaceDb, data_dir: Path
) -> None:
    category_id = _category(db)
    first = _item(db, category_id, "Car")
    _item(db, category_id, "Car")
    linked = _item(db, category_id, "Brokerage", mode="linked", link_id=uuid4())
    content = (
        HEADER
        + b"Car,2026-01-01,100.00,,,\n"
        + f"Renamed,2026-01-01,100.00,,,{first}\n".encode()
        + b"Brokerage,2026-01-01,100.00,,,\n"
        + b"Missing,2026-01-01,100.00,,,\n"
        + f"Linked by id,2026-01-01,100.00,,,{linked}\n".encode()
    )
    batch_id = _upload(db, data_dir, content)
    report = _plan(db, data_dir, batch_id)

    assert report["source"] == "item_valuations_csv"
    assert report["counts"]["to_create"] == 1
    assert {entry["code"] for entry in report["skipped"]} == {
        "item_ambiguous",
        "item_linked",
        "item_not_found",
    }
    _apply(db, data_dir, batch_id)
    assert [(row.as_of, row.value_minor) for row in _values(db, first)] == [
        (date(2026, 1, 1), 10000)
    ]
    assert _values(db, linked) == []


def test_import_classifies_stored_and_in_file_duplicates_and_conflicts(
    db: WorkspaceDb, data_dir: Path
) -> None:
    category_id = _category(db)
    item_id = _item(db, category_id, "Car")
    with db.factory.begin() as session:
        ItemValuationRepository(session, _ctx(db)).create(
            item_id=item_id,
            as_of=date(2026, 1, 1),
            value_minor=10000,
            currency="EUR",
        )
    content = (
        HEADER
        + b"Car,2026-01-01,100.00,,,\n"
        + b"Car,2026-01-01,101.00,,,\n"
        + b"Car,2026-02-01,200.00,,,\n"
        + b"Car,2026-02-01,200.00,,,\n"
        + b"Car,2026-02-01,201.00,,,\n"
    )
    batch_id = _upload(db, data_dir, content)
    report = _plan(db, data_dir, batch_id)

    assert report["counts"]["to_create"] == 2
    assert report["counts"]["duplicates_skipped"] == 2
    assert report["counts"]["conflicts_skipped"] == 1
    assert [entry["code"] for entry in report["skipped"]] == [
        "already_imported",
        "changed_since_export",
        "duplicate_in_file",
    ]
    _apply(db, data_dir, batch_id)
    assert [row.value_minor for row in _values(db, item_id)] == [10000, 20000, 20100]


def test_import_keeps_same_day_corrections_from_one_file(db: WorkspaceDb, data_dir: Path) -> None:
    category_id = _category(db)
    item_id = _item(db, category_id, "Car")
    content = HEADER + b"Car,2026-01-01,100.00,,,\n" + b"Car,2026-01-01,200.00,,,\n"
    batch_id = _upload(db, data_dir, content)
    report = _plan(db, data_dir, batch_id)
    assert report["counts"]["to_create"] == 2
    assert report["counts"]["duplicates_skipped"] == 0
    assert report["counts"]["conflicts_skipped"] == 0
    _apply(db, data_dir, batch_id)

    values = _values(db, item_id)
    assert [row.value_minor for row in values] == [10000, 20000]
    assert values[0].created_at < values[1].created_at


def test_import_ids_are_lineage_and_a_second_import_creates_nothing(
    db: WorkspaceDb, data_dir: Path
) -> None:
    category_id = _category(db)
    item_id = _item(db, category_id, "Car")
    first_id, second_id = uuid4(), uuid4()
    content = (
        b"item,as_of,value,created_at,id\n"
        + f"Car,2026-01-01,100.00,2025-12-01T10:00:00Z,{first_id}\n".encode()
        + f"Car,2026-02-01,200.00,2025-12-01T11:00:00Z,{second_id}\n".encode()
    )
    batch_id = _upload(db, data_dir, content)
    _plan(db, data_dir, batch_id)
    _apply(db, data_dir, batch_id)
    values = _values(db, item_id)
    assert [row.value_minor for row in values] == [10000, 20000]
    assert [row.import_key for row in values] == [f"id:{first_id}", f"id:{second_id}"]

    again = _upload(db, data_dir, content)
    report = _plan(db, data_dir, again)
    assert report["counts"]["to_create"] == 0
    assert report["counts"]["duplicates_skipped"] == 2
    assert {entry["code"] for entry in report["skipped"]} == {"already_imported"}
    _apply(db, data_dir, again)
    assert len(_values(db, item_id)) == 2

    # The id wins even when the row's date or value changed.
    changed = b"item,as_of,value,id\n" + f"Car,2026-03-01,999.00,{first_id}\n".encode()
    changed_batch = _upload(db, data_dir, changed)
    changed_report = _plan(db, data_dir, changed_batch)
    assert changed_report["counts"]["to_create"] == 0
    assert changed_report["skipped"][0]["code"] == "already_imported"

    repeated_id = uuid4()
    repeated = _upload(
        db,
        data_dir,
        b"item,as_of,value,id\n"
        + f"Car,2026-04-01,300.00,{repeated_id}\n".encode()
        + f"Car,2026-04-02,300.00,{repeated_id}\n".encode(),
    )
    repeated_report = _plan(db, data_dir, repeated)
    assert {entry["code"] for entry in repeated_report["skipped"]} == {"duplicate_in_file"}


def test_import_refuses_currency_mismatch_and_extra_decimals(
    db: WorkspaceDb, data_dir: Path
) -> None:
    category_id = _category(db)
    item_id = _item(db, category_id, "Car")
    content = (
        HEADER
        + b"Car,2026-01-01,100.00,USD,,\n"
        + b"Car,2026-01-01,100.001,,,\n"
        + b"Car,2026-01-01,100.00,,,\n"
    )
    batch_id = _upload(db, data_dir, content)
    report = _plan(db, data_dir, batch_id)

    assert report["counts"]["invalid"] == 1
    assert report["counts"]["to_create"] == 1
    assert {entry["code"] for entry in report["skipped"]} == {"valuation_currency_mismatch"}
    assert {entry["code"] for entry in report["errors"]} == {"too_many_decimals"}
    _apply(db, data_dir, batch_id, skip_invalid=True)
    assert [row.value_minor for row in _values(db, item_id)] == [10000]


def test_import_without_created_at_stamps_the_import_time(db: WorkspaceDb, data_dir: Path) -> None:
    category_id = _category(db)
    item_id = _item(db, category_id, "Car")
    content = HEADER + b"Car,2026-01-01,100.00,,,\n" + b"Car,2026-02-01,200.00,,,\n"
    batch_id = _upload(db, data_dir, content)
    _plan(db, data_dir, batch_id)
    _apply(db, data_dir, batch_id)

    values = _values(db, item_id)
    assert [row.value_minor for row in values] == [10000, 20000]
    assert values[0].created_at <= values[1].created_at


def test_import_restores_created_at_and_rollback_deletes_append_only_rows(
    db: WorkspaceDb, data_dir: Path
) -> None:
    category_id = _category(db)
    item_id = _item(db, category_id, "Car")
    content = (
        b"item,as_of,value,created_at\n"
        b"Car,2026-01-01,100.00,2025-12-01T10:00:00Z\n"
        b"Car,2026-02-01,200.00,2025-12-01T11:00:00Z\n"
    )
    batch_id = _upload(db, data_dir, content)
    _plan(db, data_dir, batch_id)
    _apply(db, data_dir, batch_id)
    values = _values(db, item_id)
    assert [row.value_minor for row in values] == [10000, 20000]
    assert [row.created_at for row in values] == [
        datetime(2025, 12, 1, 10, 0, tzinfo=UTC),
        datetime(2025, 12, 1, 11, 0, tzinfo=UTC),
    ]

    assert preview_rollback_import(db.factory, _ctx(db), batch_id).counts["valuations"] == 2
    rollback_import(db.factory, _ctx(db), batch_id)
    assert _values(db, item_id) == []

    # The repository delete path stays unsupported, and a direct ORM delete is refused.
    with db.factory.begin() as session:
        ItemValuationRepository(session, _ctx(db)).create(
            item_id=item_id,
            as_of=date(2026, 2, 1),
            value_minor=30000,
            currency="EUR",
        )
    with db.factory() as session:
        repository = ItemValuationRepository(session, _ctx(db))
        row = repository.for_item(item_id)[0]
        with pytest.raises(UnsupportedOperation):
            repository.delete(row)
    with db.factory() as session:
        row = ItemValuationRepository(session, _ctx(db)).for_item(item_id)[0]
        stored = session.get(ItemValuation, row.id)
        assert stored is not None
        session.delete(stored)
        with pytest.raises(AppendOnlyViolationError):
            session.flush()
        session.rollback()


def _seed_networth(db: WorkspaceDb) -> UUID:
    with db.factory.begin() as session:
        seed_into(
            session,
            _ctx(db),
            locale="en",
            actor_user_id=db.users["ed"],
            actor_type="user",
            now=db.clock.now(),
            automatic=True,
        )
    with db.factory() as session:
        return NetWorthCategoryRepository(session, _ctx(db)).ordered()[0].id


def test_networth_items_make_the_workspace_non_empty(db: WorkspaceDb) -> None:
    category_id = _seed_networth(db)
    _item(db, category_id, "Car")
    with db.factory() as session:
        assert not workspace_is_empty(session, _ctx(db))


def test_item_valuations_make_the_workspace_non_empty(db: WorkspaceDb) -> None:
    category_id = _seed_networth(db)
    item_id = _item(db, category_id, "Car")
    with db.factory.begin() as session:
        ItemValuationRepository(session, _ctx(db)).create(
            item_id=item_id,
            as_of=date(2026, 1, 1),
            value_minor=10000,
            currency="EUR",
        )
    with db.factory() as session:
        assert not workspace_is_empty(session, _ctx(db))


def test_networth_snapshots_make_the_workspace_non_empty(db: WorkspaceDb) -> None:
    _seed_networth(db)
    with db.factory.begin() as session:
        NetWorthSnapshotRepository(session, _ctx(db)).create(
            as_of=date(2026, 1, 31),
            assets_minor=0,
            liabilities_minor=0,
            currency="EUR",
            breakdown={},
            source="manual",
        )
    with db.factory() as session:
        assert not workspace_is_empty(session, _ctx(db))


def test_manual_networth_seed_is_user_data(db: WorkspaceDb) -> None:
    target = db.add_workspace("Manual")
    db.add_member("ed", "editor", workspace_id=target)
    ctx = db.ctx("ed", "editor", workspace_id=target)
    with db.factory.begin() as session:
        seed_into(
            session,
            ctx,
            locale="en",
            actor_user_id=db.users["ed"],
            actor_type="user",
            now=db.clock.now(),
            automatic=False,
        )
    with db.factory() as session:
        assert not workspace_is_empty(session, ctx)


def test_suggested_networth_categories_are_empty_enough_and_removed(
    db: WorkspaceDb, data_dir: Path
) -> None:
    with db.factory.begin() as session:
        seed_into(
            session,
            _ctx(db),
            locale="en",
            actor_user_id=db.users["ed"],
            actor_type="user",
            now=db.clock.now(),
            automatic=True,
        )
    with db.factory() as session:
        assert workspace_is_empty(session, _ctx(db))
        assert len(NetWorthCategoryRepository(session, _ctx(db)).list()) == len(NETWORTH_SEED)

    with db.factory.begin() as session:
        category = NetWorthCategoryRepository(session, _ctx(db)).ordered()[0]
        NetWorthCategoryRepository(session, _ctx(db)).update(category.id, name="Changed")
    with db.factory() as session:
        assert not workspace_is_empty(session, _ctx(db))
