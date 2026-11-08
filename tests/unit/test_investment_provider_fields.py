"""Instrument provider assignment (F6.4b)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from assetboard.config import Settings
from assetboard.db.repositories import InstrumentRepository
from assetboard.services.context import Context, PermissionDenied
from assetboard.services.investments import (
    InvalidInvestment,
    create_asset_class,
    create_instrument,
    update_instrument,
)
from assetboard.services.price_sources import save_provider_config
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    yield from workspace_db(tmp_path)


@pytest.fixture
def team(db: WorkspaceDb) -> WorkspaceDb:
    db.add_member("olga", "owner")
    db.add_member("ed", "editor")
    db.add_member("vic", "viewer")
    return db


SETTINGS = Settings(_env_file=None, secret_key="test-secret")


def _owner(db: WorkspaceDb) -> Context:
    return db.ctx("olga", "owner")


def _class_id(db: WorkspaceDb, ctx: Context, name: str = "Crypto") -> UUID:
    return create_asset_class(db.factory, ctx, name=name, color="#2563eb", investable=True).id


def _instrument(
    db: WorkspaceDb,
    ctx: Context,
    *,
    name: str = "Bitcoin",
    type_value: str = "crypto",
    quote_currency: str = "USD",
    unit_label: str = "coin",
    provider_key: str = "",
    provider_symbol: str = "",
):
    return create_instrument(
        db.factory,
        ctx,
        name=name,
        symbol="",
        type=type_value,
        quote_currency=quote_currency,
        unit_label=unit_label,
        allocations=[(_class_id(db, ctx, name=f"Class {name}"), 10_000)],
        locale="en",
        provider_key=provider_key,
        provider_symbol=provider_symbol,
    )


def _enable(db: WorkspaceDb, ctx: Context, key: str) -> None:
    if key == "eodhd":
        save_provider_config(
            db.factory,
            ctx,
            SETTINGS,
            key,
            enabled=True,
            settings_values={"api_key": "test"},
        )
    else:
        save_provider_config(db.factory, ctx, SETTINGS, key, enabled=True)


def test_a_provider_instrument_uses_the_provider_source(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    _enable(db, ctx, "coingecko")
    view = _instrument(db, ctx, provider_key="coingecko", provider_symbol="bitcoin")
    assert (view.valuation_source, view.provider_key, view.provider_symbol) == (
        "provider",
        "coingecko",
        "bitcoin",
    )
    assert view.provider_label == "CoinGecko"


def test_a_disabled_provider_cannot_be_assigned(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    with pytest.raises(InvalidInvestment) as caught:
        _instrument(db, ctx, provider_key="coingecko", provider_symbol="bitcoin")
    assert (caught.value.field, caught.value.code) == (
        "provider_key",
        "provider_disabled",
    )


@pytest.mark.parametrize(
    ("type_value", "quote_currency", "unit_label", "symbol", "code"),
    [
        ("etf", "USD", "share", "bitcoin", "provider_unsupported"),
        ("crypto", "CHF", "coin", "bitcoin", "provider_unsupported"),
        ("crypto", "USD", "coin", "", "provider_symbol_required"),
        ("crypto", "USD", "coin", "bad symbol!", "invalid_provider_symbol"),
    ],
)
def test_provider_assignment_validation(
    db: WorkspaceDb,
    team: WorkspaceDb,
    type_value: str,
    quote_currency: str,
    unit_label: str,
    symbol: str,
    code: str,
) -> None:
    ctx = _owner(db)
    _enable(db, ctx, "coingecko")
    with pytest.raises(InvalidInvestment) as caught:
        _instrument(
            db,
            ctx,
            type_value=type_value,
            quote_currency=quote_currency,
            unit_label=unit_label,
            provider_key="coingecko",
            provider_symbol=symbol,
        )
    assert caught.value.code == code


def test_switching_provider_resets_the_history_marker(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    _enable(db, ctx, "nbp")
    instrument = _instrument(
        db,
        ctx,
        name="Gold",
        type_value="metal",
        quote_currency="PLN",
        unit_label="oz",
    )
    with db.factory.begin() as session:
        InstrumentRepository(session, ctx).update(
            instrument.id, provider_backfilled_at=datetime(2026, 3, 1, tzinfo=UTC)
        )
    updated = update_instrument(
        db.factory,
        ctx,
        instrument.id,
        name=instrument.name,
        symbol="",
        type="metal",
        quote_currency="PLN",
        unit_label="oz",
        allocations=[(instrument.allocations[0].asset_class_id, 10_000)],
        locale="en",
        provider_key="nbp",
        provider_symbol="XAU",
    )
    assert updated.provider_key == "nbp"
    with db.factory() as session:
        row = InstrumentRepository(session, ctx).get_or_raise(instrument.id)
        assert row.provider_backfilled_at is None


def test_editors_can_assign_but_viewers_cannot(db: WorkspaceDb, team: WorkspaceDb) -> None:
    owner = _owner(db)
    _enable(db, owner, "coingecko")
    editor = db.ctx("ed", "editor")
    view = _instrument(
        db, editor, name="Editor coin", provider_key="coingecko", provider_symbol="bitcoin"
    )
    assert view.provider_key == "coingecko"
    viewer = db.ctx("vic", "viewer")
    with pytest.raises(PermissionDenied):
        _instrument(
            db, viewer, name="Viewer coin", provider_key="coingecko", provider_symbol="bitcoin"
        )
