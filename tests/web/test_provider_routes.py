"""Provider settings page and provider badges over HTTP (F6.4)."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.models.identity import AuditEvent, Workspace
from assetboard.db.models.investments import AssetClass, Instrument
from assetboard.db.models.prices import PricePoint, PriceSourceConfig, ProviderError
from assetboard.services.price_refresh import RefreshStats
from assetboard.services.providers.nbp import GOLD_LATEST
from assetboard.services.providers.registry import build_provider_runtime
from assetboard.services.providers.transport import HttpResponse, ProviderTransportError
from tests.conftest import OwnerApp
from tests.fake_provider_http import FakeProviderHttpClient, FakeTime

NOW = "2026-03-10T12:00:00+00:00"
INSTRUMENTS = "/investments/instruments"
CLASSES = "/investments/classes"
PROVIDERS = "/investments/providers"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "providers"


@pytest.fixture(autouse=True)
def _fixed_clock(owner: OwnerApp) -> None:
    clock = FixedClock(NOW)
    owner.app.state.clock = clock
    owner.app.state.session_factory.configure(info={"clock": clock})
    owner.clear_asset_classes()


def _class_id(owner: OwnerApp, name: str = "Metals") -> UUID:
    with owner.db() as session:
        existing = session.scalars(select(AssetClass.id).where(AssetClass.name == name)).first()
        if existing is not None:
            return existing
    response = owner.post(
        CLASSES, {"name": name, "color": "#ca8a04", "investable": "1"}, token_from=CLASSES
    )
    assert response.status_code == 303, response.text
    with owner.db() as session:
        return session.scalars(select(AssetClass.id).where(AssetClass.name == name)).one()


def _instrument(
    owner: OwnerApp,
    name: str,
    *,
    type_value: str = "metal",
    quote_currency: str = "PLN",
) -> UUID:
    response = owner.post(
        INSTRUMENTS,
        {
            "name": name,
            "symbol": "XAU",
            "type": type_value,
            "quote_currency": quote_currency,
            "unit_label": "oz",
            f"weight_{_class_id(owner)}": "100",
        },
        token_from=INSTRUMENTS,
    )
    assert response.status_code == 303, response.text
    with owner.db() as session:
        return session.scalars(select(Instrument.id).where(Instrument.name == name)).one()


def _base_to_pln(owner: OwnerApp) -> None:
    with owner.db() as session, session.begin():
        workspace = session.get(Workspace, owner.home_id)
        assert workspace is not None
        workspace.base_currency = "PLN"


def _configure_provider(owner: OwnerApp, instrument: UUID) -> None:
    with owner.db() as session, session.begin():
        row = session.get(Instrument, instrument)
        assert row is not None
        row.provider_key = "nbp"
        row.provider_symbol = "XAU"
        row.valuation_source = "provider"


def _enable_nbp(owner: OwnerApp) -> None:
    with owner.db() as session, session.begin():
        session.add(
            PriceSourceConfig(
                workspace_id=owner.home_id,
                provider_key="nbp",
                enabled=True,
                settings_encrypted=None,
            )
        )


def _buy(owner: OwnerApp, instrument: UUID) -> None:
    response = owner.post(
        "/investments/transactions",
        {
            "instrument_id": str(instrument),
            "type": "buy",
            "date": "2026-03-01",
            "quantity": "10",
            "unit_price": "10",
            "fee": "0",
            "fx_to_base": "1",
            "note": "",
        },
        token_from="/investments/transactions",
    )
    assert response.status_code == 303, response.text


def test_the_provider_page_is_owner_only_and_lists_nbp_and_ecb(owner: OwnerApp) -> None:
    page = owner.client.get(PROVIDERS).text
    assert "Price providers" in page
    assert 'data-provider-card="nbp"' in page
    assert 'data-provider-card="ecb"' in page
    assert "Narodowy Bank Polski" in page
    assert "European Central Bank" in page
    assert "data-providers-tab" in page

    owner.set_admin_role("editor")
    assert owner.client.get(PROVIDERS).status_code == 403
    instruments = owner.client.get(INSTRUMENTS).text
    assert "data-providers-tab" not in instruments


def test_enabling_a_provider_saves_it_and_queues_a_backfill(
    owner: OwnerApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    queued: list[tuple[UUID, str]] = []
    monkeypatch.setattr(
        owner.app.state.recurring_jobs,
        "request_backfill",
        lambda workspace_id, provider_key: queued.append((workspace_id, provider_key)),
    )
    response = owner.post(PROVIDERS + "/nbp", {"enabled": "1"}, token_from=PROVIDERS)
    assert (response.status_code, response.headers["location"]) == (
        303,
        f"{PROVIDERS}?notice=saved",
    )
    assert queued == [(owner.home_id, "nbp")]
    with owner.db() as session:
        row = session.scalars(
            select(PriceSourceConfig).where(PriceSourceConfig.provider_key == "nbp")
        ).one()
        assert row.enabled

    disabled = owner.post(PROVIDERS + "/nbp", {}, token_from=PROVIDERS)
    assert disabled.status_code == 303
    with owner.db() as session:
        row = session.scalars(
            select(PriceSourceConfig).where(PriceSourceConfig.provider_key == "nbp")
        ).one()
        assert not row.enabled


def test_refresh_now_reports_a_successful_latest_refresh(owner: OwnerApp) -> None:
    _base_to_pln(owner)
    instrument = _instrument(owner, "Gold")
    _buy(owner, instrument)
    _configure_provider(owner, instrument)
    _enable_nbp(owner)
    client = FakeProviderHttpClient(
        {
            GOLD_LATEST: (FIXTURES / "nbp_gold.json").read_bytes(),
        }
    )
    owner.app.state.provider_runtime = build_provider_runtime(
        owner.app.state.settings, owner.app.state.clock, http=client
    )
    response = owner.client.post(
        PROVIDERS + "/nbp/refresh",
        data={"csrf_token": owner.token(PROVIDERS)},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Refresh complete: 1 prices and 0 FX rates." in response.text


def test_refresh_now_surfaces_rate_limiting(
    owner: OwnerApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "assetboard.web.provider_routes.refresh_now",
        lambda *_args, **_kwargs: RefreshStats(
            provider_key="nbp", status="rate_limited", retry_after=120
        ),
    )
    response = owner.client.post(
        PROVIDERS + "/nbp/refresh",
        data={"csrf_token": owner.token(PROVIDERS)},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Rate limited; try again in 2 minutes." in response.text


def test_the_instrument_page_shows_a_provider_error_badge(owner: OwnerApp) -> None:
    _base_to_pln(owner)
    instrument = _instrument(owner, "Gold")
    _configure_provider(owner, instrument)
    _enable_nbp(owner)
    with owner.db() as session, session.begin():
        point = PricePoint(
            workspace_id=owner.home_id,
            instrument_id=instrument,
            date=date(2026, 3, 1),
            price=Decimal("100"),
            source="provider",
            provider_key="nbp",
        )
        session.add(point)
        session.flush()
        session.add(
            ProviderError(
                workspace_id=owner.home_id,
                provider_key="nbp",
                instrument_id=instrument,
                scope="latest",
                code="http_500",
                occurred_at=datetime(2026, 3, 10, 13, tzinfo=UTC),
                attempts=1,
            )
        )
    page = owner.client.get(f"{INSTRUMENTS}/{instrument}").text
    assert "Last updated 9 days ago — provider error." in page


def test_the_provider_card_shows_the_base_currency_mismatch(owner: OwnerApp) -> None:
    _enable_nbp(owner)
    with owner.db() as session, session.begin():
        row = session.scalars(
            select(PriceSourceConfig).where(PriceSourceConfig.provider_key == "nbp")
        ).one()
        row.last_error_code = "base_not_published"
    page = owner.client.get(PROVIDERS).text
    assert "This provider does not publish your base currency" in page
    assert "data-base-mismatch" in page


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (ProviderTransportError("network"), "network"),
        (HttpResponse(500, b"", {}), "http_500"),
        (b"SENTINEL-123", "malformed_response"),
    ],
)
def test_provider_failures_do_not_leak_request_urls(
    owner: OwnerApp,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    response,
    code: str,
) -> None:
    sentinel = "SENTINEL-123"
    url = f"https://api.nbp.pl/api/cenyzlota/?format=json&apikey={sentinel}"
    monkeypatch.setattr("assetboard.services.providers.nbp.GOLD_LATEST", url)
    _base_to_pln(owner)
    instrument = _instrument(owner, "Gold")
    _buy(owner, instrument)
    _configure_provider(owner, instrument)
    _enable_nbp(owner)
    time_source = FakeTime()
    owner.app.state.provider_runtime = build_provider_runtime(
        owner.app.state.settings,
        owner.app.state.clock,
        http=FakeProviderHttpClient({url: response}),
        sleep=time_source.sleep,
        monotonic=time_source.monotonic,
    )
    with caplog.at_level(logging.DEBUG):
        page = owner.client.post(
            PROVIDERS + "/nbp/refresh",
            data={"csrf_token": owner.token(PROVIDERS)},
            follow_redirects=True,
        ).text
        instrument_page = owner.client.get(f"{INSTRUMENTS}/{instrument}").text
    assert sentinel not in page
    assert sentinel not in instrument_page
    assert sentinel not in caplog.text
    with owner.db() as session:
        errors = list(session.scalars(select(ProviderError)))
        assert errors and errors[-1].code == code
        assert all(sentinel not in str(error.code) for error in errors)
        events = list(
            session.scalars(select(AuditEvent).where(AuditEvent.action == "price_source.refreshed"))
        )
        assert events and all(sentinel not in str(event.data) for event in events)


def test_htmx_save_returns_the_provider_fragment(owner: OwnerApp) -> None:
    response = owner.client.post(
        PROVIDERS + "/nbp",
        data={"csrf_token": owner.token(PROVIDERS), "enabled": "1"},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "<html" not in response.text
    assert 'data-provider-card="nbp"' in response.text
