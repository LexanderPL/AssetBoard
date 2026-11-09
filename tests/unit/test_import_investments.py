"""F6.9 standalone investment transaction import."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from assetboard.db.models.identity import AuditEvent
from assetboard.db.repositories import (
    AssetClassRepository,
    FxRateRepository,
    InstrumentAllocationRepository,
    InstrumentRepository,
    InvestmentTransactionRepository,
)
from assetboard.services.import_csv import default_import_adapters
from assetboard.services.imports import (
    ImportAdapterRegistry,
    ImportMappingInvalid,
    apply_import,
    plan_import,
    preview_rollback_import,
    rollback_import,
    update_import_preview,
    upload_import,
)
from tests.tenant.workspace_db import WorkspaceDb, workspace_db

COLUMNS = [
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
]
HEADER = ",".join(COLUMNS) + "\n"


def _row(**values: object) -> str:
    return ",".join(str(values.get(column, "")) for column in COLUMNS) + "\n"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    for prepared in workspace_db(tmp_path):
        prepared.add_member("ed", "editor")
        prepared.add_member("own", "owner")
        yield prepared


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    return path


def _ctx(db: WorkspaceDb):
    return db.ctx("ed", "editor")


def _adapters() -> ImportAdapterRegistry:
    return ImportAdapterRegistry(default_import_adapters())


def _upload(db: WorkspaceDb, data_dir: Path, content: str) -> UUID:
    return upload_import(
        db.factory,
        _ctx(db),
        data_dir=data_dir,
        filename="investment_transactions.csv",
        content=content.encode(),
        options={"delimiter": "detect", "decimal": "dot"},
    )


def _plan(db: WorkspaceDb, data_dir: Path, batch_id: UUID) -> dict[str, Any]:
    return plan_import(db.factory, _ctx(db), data_dir, batch_id, _adapters())


def _apply(db: WorkspaceDb, data_dir: Path, batch_id: UUID) -> dict[str, Any]:
    return apply_import(db.factory, _ctx(db), data_dir, batch_id, _adapters())


def _class(db: WorkspaceDb, name: str = "Other", *, order: int = 0) -> UUID:
    with db.factory.begin() as session:
        row = AssetClassRepository(session, _ctx(db)).create(
            name=name,
            name_key=name.casefold(),
            investable=True,
            color="#475569",
            sort_order=order,
        )
        return row.id


def _instrument(
    db: WorkspaceDb,
    class_id: UUID,
    name: str = "World ETF",
    *,
    type_value: str = "etf",
    quote_currency: str = "EUR",
) -> UUID:
    with db.factory.begin() as session:
        instrument = InstrumentRepository(session, _ctx(db)).create(
            name=name,
            name_key=name.casefold(),
            symbol=None,
            type=type_value,
            quote_currency=quote_currency,
            unit_label="share" if type_value != "currency" else quote_currency,
            valuation_source="bond_model" if type_value == "bond_model" else "manual",
        )
        InstrumentAllocationRepository(session, _ctx(db)).create(
            instrument_id=instrument.id,
            asset_class_id=class_id,
            weight_bp=10_000,
        )
        return instrument.id


def _mapping(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "source_name_key": "world etf",
        "action": "map",
        "instrument_id": "",
        "create_name": "World ETF",
        "create_type": "",
        "create_unit_label": "",
        "quote_currency": "EUR",
    }
    entry.update(overrides)
    return entry


def test_known_instrument_is_mapped_by_name_and_duplicate_ids_are_skipped(
    db: WorkspaceDb, data_dir: Path
) -> None:
    class_id = _class(db, "Stocks")
    instrument_id = _instrument(db, class_id)
    source_id = uuid4()
    content = HEADER + _row(
        id=source_id,
        instrument_id=instrument_id,
        instrument="World ETF",
        date="2026-02-01",
        type="buy",
        quantity="10",
        unit_price="100.5",
        quote_currency="EUR",
        fee="1.00",
        fx_to_base="1",
        note="First lot",
    )
    batch_id = _upload(db, data_dir, content)
    report = _plan(db, data_dir, batch_id)
    assert report["source"] == "investment_transactions_csv"
    assert report["counts"]["to_create"] == 1
    assert report["counts"]["mapping_invalid"] == 0
    assert report["instrument_mapping"][0]["action"] == "map"
    applied = _apply(db, data_dir, batch_id)
    assert applied["counts"]["investment_transactions_created"] == 1
    with db.factory() as session:
        rows = InvestmentTransactionRepository(session, _ctx(db)).list()
    assert len(rows) == 1
    assert rows[0].import_key == f"id:{source_id}"
    assert rows[0].fee_minor == 100

    second = _upload(db, data_dir, content)
    duplicate_report = _plan(db, data_dir, second)
    assert duplicate_report["counts"]["duplicates_skipped"] == 1
    assert duplicate_report["counts"]["to_create"] == 0


def test_unknown_instrument_is_created_with_a_type_and_unit_label(
    db: WorkspaceDb, data_dir: Path
) -> None:
    other = _class(db, "Other")
    content = HEADER + _row(
        instrument="Gold",
        date="2026-02-01",
        type="buy",
        quantity="2",
        unit_price="2000",
        quote_currency="EUR",
        fee="0",
    )
    batch_id = _upload(db, data_dir, content)
    report = _plan(db, data_dir, batch_id)
    assert report["counts"]["mapping_invalid"] == 1
    assert report["instrument_mapping"][0]["action"] == "create"
    with pytest.raises(ImportMappingInvalid):
        _apply(db, data_dir, batch_id)

    report = update_import_preview(
        db.factory,
        _ctx(db),
        data_dir,
        batch_id,
        _adapters(),
        options={
            "instrument_mappings": [
                _mapping(
                    source_name_key="gold",
                    action="create",
                    create_name="Gold",
                    create_type="metal",
                    create_unit_label="oz",
                    quote_currency="EUR",
                )
            ]
        },
    )
    assert report["counts"]["mapping_invalid"] == 0
    assert report["counts"]["to_create"] == 1
    _apply(db, data_dir, batch_id)
    with db.factory() as session:
        instruments = InstrumentRepository(session, _ctx(db)).ordered()
        gold = next(row for row in instruments if row.name == "Gold")
        assert gold.type == "metal"
        assert gold.unit_label == "oz"
        assert gold.quote_currency == "EUR"
        allocations = InstrumentAllocationRepository(session, _ctx(db)).for_instrument(gold.id)
        assert [(row.asset_class_id, row.weight_bp) for row in allocations] == [(other, 10_000)]
        event = (
            session.query(AuditEvent)
            .filter_by(action="instrument.created")
            .filter(AuditEvent.workspace_id == db.workspace_id)
            .one()
        )
        assert event.data["name"] == "Gold"


def test_fx_falls_back_to_the_latest_stored_rate_and_reports_a_missing_one(
    db: WorkspaceDb, data_dir: Path
) -> None:
    class_id = _class(db, "Stocks")
    _instrument(db, class_id, quote_currency="USD")
    with db.factory.begin() as session:
        FxRateRepository(session, _ctx(db)).create(
            base_currency="EUR",
            quote_currency="USD",
            date=date(2026, 1, 20),
            rate=Decimal("0.9"),
            source="manual",
        )
    content = (
        HEADER
        + _row(
            instrument="World ETF",
            date="2026-02-01",
            type="buy",
            quantity="1",
            unit_price="10",
            quote_currency="USD",
            fee="0",
        )
        + _row(
            instrument="World ETF",
            date="2026-01-01",
            type="buy",
            quantity="1",
            unit_price="10",
            quote_currency="USD",
            fee="0",
        )
    )
    batch_id = _upload(db, data_dir, content)
    report = _plan(db, data_dir, batch_id)
    assert report["counts"]["to_create"] == 1
    assert report["counts"]["invalid"] == 1
    assert any(entry["code"] == "fx_needed" for entry in report["errors"])


def test_settlement_rows_need_their_own_fx_and_store_the_settlement_values(
    db: WorkspaceDb, data_dir: Path
) -> None:
    class_id = _class(db, "Stocks")
    _instrument(db, class_id, quote_currency="USD")
    content = HEADER + _row(
        instrument="World ETF",
        date="2026-02-01",
        type="buy",
        quantity="1",
        unit_price="10",
        quote_currency="USD",
        fee="0",
        settlement_currency="EUR",
        settlement_unit_price="9.5",
        settlement_fee="0.50",
    )
    batch_id = _upload(db, data_dir, content)
    report = _plan(db, data_dir, batch_id)
    assert any(entry["code"] == "settlement_fx_required" for entry in report["errors"])

    content = HEADER + _row(
        instrument="World ETF",
        date="2026-02-01",
        type="buy",
        quantity="1",
        unit_price="10",
        quote_currency="USD",
        fee="0",
        fx_to_base="0.92",
        settlement_currency="EUR",
        settlement_unit_price="9.5",
        settlement_fee="0.50",
    )
    batch_id = _upload(db, data_dir, content)
    report = _plan(db, data_dir, batch_id)
    assert report["counts"]["invalid"] == 0
    _apply(db, data_dir, batch_id)
    with db.factory() as session:
        row = InvestmentTransactionRepository(session, _ctx(db)).list()[0]
    assert row.settlement_currency == "EUR"
    assert row.settlement_unit_price == Decimal("9.5")
    assert row.settlement_fee_minor == 50
    assert row.fx_to_base == Decimal("0.92")


def test_oversell_blocks_apply_until_the_instrument_is_ignored(
    db: WorkspaceDb, data_dir: Path
) -> None:
    class_id = _class(db, "Stocks")
    instrument_id = _instrument(db, class_id)
    with db.factory.begin() as session:
        InvestmentTransactionRepository(session, _ctx(db)).create(
            instrument_id=instrument_id,
            date=date(2026, 1, 1),
            type="buy",
            quantity=Decimal("1"),
            unit_price=Decimal("10"),
            quote_currency="EUR",
            fee_minor=0,
            fx_to_base=Decimal("1"),
        )
    content = HEADER + _row(
        instrument="World ETF",
        date="2026-02-01",
        type="sell",
        quantity="2",
        unit_price="10",
        quote_currency="EUR",
        fee="0",
        fx_to_base="1",
    )
    batch_id = _upload(db, data_dir, content)
    report = _plan(db, data_dir, batch_id)
    assert report["counts"]["mapping_invalid"] == 1
    assert any(entry["code"] == "oversell" for entry in report["mapping_errors"])
    with pytest.raises(ImportMappingInvalid):
        _apply(db, data_dir, batch_id)

    report = update_import_preview(
        db.factory,
        _ctx(db),
        data_dir,
        batch_id,
        _adapters(),
        options={"instrument_mappings": [_mapping(action="ignore")]},
    )
    assert report["counts"]["mapping_invalid"] == 0
    assert report["counts"]["to_create"] == 0
    _apply(db, data_dir, batch_id)


def test_undated_lots_are_imported_and_listed(db: WorkspaceDb, data_dir: Path) -> None:
    class_id = _class(db, "Stocks")
    _instrument(db, class_id)
    content = HEADER + _row(
        instrument="World ETF",
        type="buy",
        quantity="1",
        unit_price="10",
        quote_currency="EUR",
        fee="0",
        fx_to_base="1",
    )
    batch_id = _upload(db, data_dir, content)
    report = _plan(db, data_dir, batch_id)
    assert report["counts"]["to_create"] == 1
    assert [warning["code"] for warning in report["warnings"]] == ["undated_lot"]
    _apply(db, data_dir, batch_id)
    with db.factory() as session:
        assert InvestmentTransactionRepository(session, _ctx(db)).list()[0].date is None


def test_rollback_soft_deletes_transactions_and_removes_a_created_instrument(
    db: WorkspaceDb, data_dir: Path
) -> None:
    other = _class(db, "Other")
    content = HEADER + _row(
        instrument="Gold",
        date="2026-02-01",
        type="buy",
        quantity="1",
        unit_price="10",
        quote_currency="EUR",
        fee="0",
        fx_to_base="1",
    )
    batch_id = _upload(db, data_dir, content)
    _plan(db, data_dir, batch_id)
    update_import_preview(
        db.factory,
        _ctx(db),
        data_dir,
        batch_id,
        _adapters(),
        options={
            "instrument_mappings": [
                _mapping(
                    source_name_key="gold",
                    action="create",
                    create_name="Gold",
                    create_type="metal",
                    create_unit_label="oz",
                )
            ]
        },
    )
    _apply(db, data_dir, batch_id)
    preview = preview_rollback_import(db.factory, _ctx(db), batch_id)
    assert preview.counts["deleted"] == 1
    assert preview.counts["instruments_archived"] == 1
    rollback_import(db.factory, _ctx(db), batch_id)
    with db.factory() as session:
        assert InvestmentTransactionRepository(session, _ctx(db)).live() == []
        instruments = InstrumentRepository(session, _ctx(db)).list()
        assert len(instruments) == 1 and instruments[0].archived_at is not None
        assert len(InstrumentAllocationRepository(session, _ctx(db)).list()) == 1
        assert AssetClassRepository(session, _ctx(db)).get(other) is not None
