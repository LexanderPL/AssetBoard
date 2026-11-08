"""Provider settings: owner-only, encrypted at rest and audited without secrets (F6.4)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from assetboard.config import Settings
from assetboard.db.models.identity import AuditEvent
from assetboard.db.repositories import InstrumentRepository, PriceSourceConfigRepository
from assetboard.services.context import Context, PermissionDenied
from assetboard.services.investments import create_asset_class, create_instrument
from assetboard.services.price_sources import (
    InvalidProviderSetting,
    MissingProviderKey,
    UnknownProvider,
    provider_page,
    provider_settings,
    save_provider_config,
)
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


def _instrument(db: WorkspaceDb, ctx: Context, name: str = "Gold"):
    class_id = create_asset_class(
        db.factory, ctx, name="Metals", color="#ca8a04", investable=True
    ).id
    return create_instrument(
        db.factory,
        ctx,
        name=name,
        symbol="",
        type="metal",
        quote_currency="PLN",
        unit_label="oz",
        allocations=[(class_id, 10_000)],
        locale="en",
    )


def _configure_provider(db: WorkspaceDb, instrument_id: UUID) -> None:
    with db.factory.begin() as session:
        InstrumentRepository(session, db.ctx("olga", "owner")).update(
            instrument_id,
            provider_key="nbp",
            provider_symbol="XAU",
            valuation_source="provider",
        )


def _events(db: WorkspaceDb) -> list[AuditEvent]:
    with db.factory() as session:
        return list(
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.action.startswith("price_source."))
                .order_by(AuditEvent.ts, AuditEvent.id)
            )
        )


def test_provider_page_lists_nbp_and_ecb_disabled(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    page = provider_page(db.factory, ctx)
    assert [card.definition.key for card in page.cards] == [
        "nbp",
        "ecb",
        "coingecko",
        "eodhd",
    ]
    assert all(not card.enabled for card in page.cards)
    assert all(card.instruments_using == 0 for card in page.cards)


def test_enabling_and_disabling_a_provider_is_audited(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    assert save_provider_config(db.factory, ctx, SETTINGS, "nbp", enabled=True)
    page = provider_page(db.factory, ctx)
    nbp = next(card for card in page.cards if card.definition.key == "nbp")
    assert nbp.enabled
    with db.factory() as session:
        assert PriceSourceConfigRepository(session, ctx).for_provider("nbp") is not None

    save_provider_config(db.factory, ctx, SETTINGS, "nbp", enabled=False)
    page = provider_page(db.factory, ctx)
    assert not next(card for card in page.cards if card.definition.key == "nbp").enabled
    events = _events(db)
    assert [event.action for event in events] == [
        "price_source.updated",
        "price_source.updated",
    ]
    assert all(event.data["provider"] == "nbp" for event in events)
    assert {event.data["enabled"] for event in events} == {True, False}
    assert all(event.data["settings_changed"] is False for event in events)


def test_settings_are_encrypted_and_bound_to_the_config(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    save_provider_config(
        db.factory,
        ctx,
        SETTINGS,
        "nbp",
        enabled=True,
        settings_values={"api_key": "super-secret-token"},
    )
    with db.factory() as session:
        row = PriceSourceConfigRepository(session, ctx).for_provider("nbp")
        assert row is not None
        assert row.settings_encrypted is not None
        assert "super-secret-token" not in row.settings_encrypted
        assert provider_settings(SETTINGS, row) == {"api_key": "super-secret-token"}

    changed = Settings(_env_file=None, secret_key="another-secret")
    with db.factory() as session:
        row = PriceSourceConfigRepository(session, ctx).for_provider("nbp")
        assert row is not None
        assert provider_settings(changed, row) is None

    assert all("super-secret-token" not in str(event.data) for event in _events(db))


def test_blank_save_keeps_the_existing_settings(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    save_provider_config(
        db.factory,
        ctx,
        SETTINGS,
        "nbp",
        enabled=True,
        settings_values={"api_key": "one"},
    )
    save_provider_config(db.factory, ctx, SETTINGS, "nbp", enabled=False)
    with db.factory() as session:
        row = PriceSourceConfigRepository(session, ctx).for_provider("nbp")
        assert row is not None and provider_settings(SETTINGS, row) == {"api_key": "one"}


def test_clearing_settings_removes_the_ciphertext(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    save_provider_config(
        db.factory,
        ctx,
        SETTINGS,
        "nbp",
        enabled=True,
        settings_values={"api_key": "one"},
    )
    save_provider_config(db.factory, ctx, SETTINGS, "nbp", enabled=True, clear_settings=True)
    with db.factory() as session:
        row = PriceSourceConfigRepository(session, ctx).for_provider("nbp")
        assert row is not None and row.settings_encrypted is None


def test_instruments_using_a_provider_are_counted(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx)
    _configure_provider(db, instrument.id)
    page = provider_page(db.factory, ctx)
    assert next(card for card in page.cards if card.definition.key == "nbp").instruments_using == 1


def test_eodhd_requires_a_key_and_stores_the_daily_budget(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    with pytest.raises(MissingProviderKey):
        save_provider_config(db.factory, ctx, SETTINGS, "eodhd", enabled=True)
    save_provider_config(
        db.factory,
        ctx,
        SETTINGS,
        "eodhd",
        enabled=True,
        settings_values={"api_key": "eodhd-secret"},
        daily_call_budget=7,
    )
    page = provider_page(db.factory, ctx)
    card = next(card for card in page.cards if card.definition.key == "eodhd")
    assert card.enabled and card.settings_configured
    assert (card.daily_call_budget, card.quota_used) == (7, 0)
    with db.factory() as session:
        row = PriceSourceConfigRepository(session, ctx).for_provider("eodhd")
        assert row is not None
        assert provider_settings(SETTINGS, row) == {"api_key": "eodhd-secret"}


def test_coingecko_key_is_optional_and_budget_must_be_positive(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    save_provider_config(db.factory, ctx, SETTINGS, "coingecko", enabled=True)
    card = next(
        card for card in provider_page(db.factory, ctx).cards if card.definition.key == "coingecko"
    )
    assert card.enabled and not card.settings_configured
    with pytest.raises(InvalidProviderSetting):
        save_provider_config(
            db.factory, ctx, SETTINGS, "coingecko", enabled=True, daily_call_budget=0
        )


def test_only_owners_may_view_or_change_providers(db: WorkspaceDb, team: WorkspaceDb) -> None:
    editor = db.ctx("ed", "editor")
    viewer = db.ctx("vic", "viewer")
    for ctx in (editor, viewer):
        with pytest.raises(PermissionDenied):
            provider_page(db.factory, ctx)
        with pytest.raises(PermissionDenied):
            save_provider_config(db.factory, ctx, SETTINGS, "nbp", enabled=True)


def test_unknown_provider_is_refused(db: WorkspaceDb, team: WorkspaceDb) -> None:
    with pytest.raises(UnknownProvider):
        save_provider_config(db.factory, _owner(db), SETTINGS, "nope", enabled=True)
