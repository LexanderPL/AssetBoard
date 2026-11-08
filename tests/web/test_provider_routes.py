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
from assetboard.services.price_refresh import LookupResult, RefreshStats
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
    provider_key: str = "",
    provider_symbol: str = "",
) -> UUID:
    response = owner.post(
        INSTRUMENTS,
        {
            "name": name,
            "symbol": "XAU",
            "type": type_value,
            "quote_currency": quote_currency,
            "unit_label": "coin"
            if type_value == "crypto"
            else ("share" if type_value == "etf" else "oz"),
            "provider_key": provider_key,
            "provider_symbol": provider_symbol,
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
    _enable_provider(owner, "nbp")


def _enable_provider(owner: OwnerApp, key: str) -> None:
    with owner.db() as session, session.begin():
        session.add(
            PriceSourceConfig(
                workspace_id=owner.home_id,
                provider_key=key,
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
    assert 'data-provider-card="goldapi"' in page
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


def test_eodhd_requires_a_key_and_stores_the_budget(owner: OwnerApp) -> None:
    refused = owner.post(
        PROVIDERS + "/eodhd",
        {"enabled": "1", "daily_call_budget": "7"},
        token_from=PROVIDERS,
    )
    assert refused.status_code == 400
    assert "Enter an API key and a daily call budget of at least 1." in refused.text

    response = owner.post(
        PROVIDERS + "/eodhd",
        {"enabled": "1", "api_key": "EODHD-SECRET", "daily_call_budget": "7"},
        token_from=PROVIDERS,
    )
    assert response.status_code == 303
    page = owner.client.get(response.headers["location"]).text
    assert "EODHD-SECRET" not in page
    assert "Calls today (UTC)" in page
    assert "0 / 7" in page
    assert "Leave empty to keep it." in page
    with owner.db() as session:
        row = session.scalars(
            select(PriceSourceConfig).where(PriceSourceConfig.provider_key == "eodhd")
        ).one()
        assert row.enabled and row.daily_call_budget == 7
        assert row.settings_encrypted is not None
        assert "EODHD-SECRET" not in row.settings_encrypted


GOLDAPI_KEY_SENTINEL = "GOLDAPI-SENTINEL-KEY-7f3a"
GOLDAPI_XAG_PLN = "https://www.goldapi.io/api/XAG/PLN"
GOLDAPI_XAU_PLN = "https://www.goldapi.io/api/XAU/PLN"


def _enable_goldapi(owner: OwnerApp, budget: str = "") -> None:
    data = {"enabled": "1", "api_key": GOLDAPI_KEY_SENTINEL}
    if budget:
        data["daily_call_budget"] = budget
    response = owner.post(PROVIDERS + "/goldapi", data, token_from=PROVIDERS)
    assert response.status_code == 303, response.text


def _goldapi_runtime(
    owner: OwnerApp, responses, time_source: FakeTime | None = None
) -> FakeProviderHttpClient:
    client = FakeProviderHttpClient(responses)
    time_source = time_source or FakeTime()
    owner.app.state.provider_runtime = build_provider_runtime(
        owner.app.state.settings,
        owner.app.state.clock,
        http=client,
        sleep=time_source.sleep,
        monotonic=time_source.monotonic,
    )
    return client


def _goldapi_card(page: str) -> str:
    start = page.index('data-provider-card="goldapi"')
    return page[start : page.index("</article>", start)]


def test_goldapi_card_requires_a_key_and_never_echoes_it(owner: OwnerApp) -> None:
    card = _goldapi_card(owner.client.get(PROVIDERS).text)
    assert 'name="api_key" type="password"' in card
    assert 'name="daily_call_budget" type="number" min="1" max="100000" value="4"' in card
    assert "0 / 4" in card
    assert 'href="https://www.goldapi.io/api-documentation"' in card
    assert "data-provider-source-link" in card
    assert "data-coingecko-badge" not in card
    refused = owner.post(PROVIDERS + "/goldapi", {"enabled": "1"}, token_from=PROVIDERS)
    assert refused.status_code == 400

    _enable_goldapi(owner)

    page = owner.client.get(PROVIDERS).text
    card = _goldapi_card(page)
    assert GOLDAPI_KEY_SENTINEL not in page
    assert "A key is configured. Leave empty to keep it." in card
    assert "GoldAPI symbols: XAU, XAG, XPT and XPD" in card
    assert "History covers at most the last 90 days." in card
    assert "data-provider-history-limit" in card
    assert "0 / 4" in card
    with owner.db() as session:
        row = session.scalars(
            select(PriceSourceConfig).where(PriceSourceConfig.provider_key == "goldapi")
        ).one()
        assert row.enabled and row.daily_call_budget == 4
        assert row.settings_encrypted is not None
        assert GOLDAPI_KEY_SENTINEL not in row.settings_encrypted

    # The owner can raise the budget without re-entering the key.
    response = owner.post(
        PROVIDERS + "/goldapi",
        {"enabled": "1", "daily_call_budget": "10"},
        token_from=PROVIDERS,
    )
    assert response.status_code == 303
    assert "0 / 10" in _goldapi_card(owner.client.get(PROVIDERS).text)


def test_goldapi_test_lookup_reads_the_ounce_price(owner: OwnerApp) -> None:
    _enable_goldapi(owner)
    client = _goldapi_runtime(
        owner, {GOLDAPI_XAG_PLN: (FIXTURES / "goldapi_latest.json").read_bytes()}
    )
    lookup = owner.post(
        "/investments/provider-lookup",
        {
            "provider_key": "goldapi",
            "provider_symbol": "XAG",
            "type": "metal",
            "quote_currency": "PLN",
            "unit_label": "oz",
        },
        token_from=INSTRUMENTS,
    )
    assert lookup.status_code == 200
    assert "Provider price: 123.4567 PLN as of Mar 2, 2026" in lookup.text
    assert GOLDAPI_KEY_SENTINEL not in lookup.text
    assert [url for url, _headers in client.calls] == [GOLDAPI_XAG_PLN]
    assert "1 / 4" in _goldapi_card(owner.client.get(PROVIDERS).text)


def test_goldapi_refresh_now_stores_the_price_and_counts_the_call(owner: OwnerApp) -> None:
    _base_to_pln(owner)
    _enable_goldapi(owner)
    instrument = _instrument(owner, "Silver", provider_key="goldapi", provider_symbol="XAG")
    _buy(owner, instrument)
    _goldapi_runtime(owner, {GOLDAPI_XAG_PLN: (FIXTURES / "goldapi_latest.json").read_bytes()})

    response = owner.client.post(
        PROVIDERS + "/goldapi/refresh",
        data={"csrf_token": owner.token(PROVIDERS)},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Refresh complete: 1 prices and 0 FX rates." in response.text
    assert "1 / 4" in _goldapi_card(response.text)
    with owner.db() as session:
        points = list(session.scalars(select(PricePoint)))
    assert [(point.date, point.price, point.provider_key) for point in points] == [
        (date(2026, 3, 2), Decimal("123.4567"), "goldapi")
    ]


def test_goldapi_quota_warning_when_the_budget_is_spent(owner: OwnerApp) -> None:
    _base_to_pln(owner)
    _enable_goldapi(owner, budget="1")
    for name, symbol in (("Gold", "XAU"), ("Silver", "XAG")):
        instrument = _instrument(owner, name, provider_key="goldapi", provider_symbol=symbol)
        _buy(owner, instrument)
    body = (FIXTURES / "goldapi_latest.json").read_bytes()
    client = _goldapi_runtime(owner, {GOLDAPI_XAG_PLN: body, GOLDAPI_XAU_PLN: body})

    response = owner.client.post(
        PROVIDERS + "/goldapi/refresh",
        data={"csrf_token": owner.token(PROVIDERS)},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert len(client.calls) == 1
    card = _goldapi_card(response.text)
    assert "1 / 1" in card
    assert "data-provider-quota-exhausted" in card
    assert "Daily call budget exhausted; remaining instruments keep their last prices." in card


@pytest.mark.parametrize("status", [401, 403])
def test_goldapi_failure_does_not_leak_the_key(
    owner: OwnerApp, caplog: pytest.LogCaptureFixture, status: int
) -> None:
    _base_to_pln(owner)
    _enable_goldapi(owner)
    instrument = _instrument(owner, "Silver", provider_key="goldapi", provider_symbol="XAG")
    _buy(owner, instrument)
    echoed = b'{"error": "bad key ' + GOLDAPI_KEY_SENTINEL.encode() + b'"}'
    time_source = FakeTime()
    _goldapi_runtime(owner, lambda _url: HttpResponse(status, echoed, {}), time_source)
    with caplog.at_level(logging.DEBUG):
        page = owner.client.post(
            PROVIDERS + "/goldapi/refresh",
            data={"csrf_token": owner.token(PROVIDERS)},
            follow_redirects=True,
        ).text
        time_source.value += 2  # past the 1 request/second window; web calls never sleep
        lookup = owner.post(
            "/investments/provider-lookup",
            {
                "provider_key": "goldapi",
                "provider_symbol": "XAG",
                "type": "metal",
                "quote_currency": "PLN",
                "unit_label": "oz",
            },
            token_from=INSTRUMENTS,
        ).text
        instrument_page = owner.client.get(f"{INSTRUMENTS}/{instrument}").text
    assert f"http_{status}" in lookup
    for text in (page, lookup, instrument_page, caplog.text):
        assert GOLDAPI_KEY_SENTINEL not in text
    with owner.db() as session:
        errors = list(session.scalars(select(ProviderError)))
        assert errors and {error.code for error in errors} == {f"http_{status}"}
        events = list(session.scalars(select(AuditEvent)))
        assert events and all(GOLDAPI_KEY_SENTINEL not in str(event.data) for event in events)
        row = session.scalars(
            select(PriceSourceConfig).where(PriceSourceConfig.provider_key == "goldapi")
        ).one()
        assert row.last_error_code == f"http_{status}"


def test_coingecko_shows_the_required_attribution_badge(owner: OwnerApp) -> None:
    _enable_provider(owner, "coingecko")
    providers = owner.client.get(PROVIDERS).text
    assert "data-coingecko-badge" in providers
    assert "Powered by CoinGecko" in providers
    instruments = owner.client.get(INSTRUMENTS).text
    assert "data-coingecko-badge" in instruments
    assert "Powered by CoinGecko" in instruments


def test_provider_fields_create_an_instrument_and_test_lookup(
    owner: OwnerApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_provider(owner, "coingecko")
    queued: list[tuple[UUID, str]] = []
    monkeypatch.setattr(
        owner.app.state.recurring_jobs,
        "request_backfill",
        lambda workspace_id, provider_key: queued.append((workspace_id, provider_key)),
    )
    instrument = _instrument(
        owner,
        "Bitcoin",
        type_value="crypto",
        quote_currency="USD",
        provider_key="coingecko",
        provider_symbol="bitcoin",
    )
    assert queued == [(owner.home_id, "coingecko")]
    page = owner.client.get(f"{INSTRUMENTS}/{instrument}").text
    assert "CoinGecko (bitcoin)" in page
    assert "Powered by CoinGecko" in page
    instruments_page = owner.client.get(INSTRUMENTS).text
    assert 'name="provider_key"' in instruments_page
    assert "data-provider-lookup-button" in instruments_page

    url = (
        "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin"
        "&vs_currencies=usd&include_last_updated_at=true"
    )
    owner.app.state.provider_runtime = build_provider_runtime(
        owner.app.state.settings,
        owner.app.state.clock,
        http=FakeProviderHttpClient({url: (FIXTURES / "coingecko_simple.json").read_bytes()}),
    )
    lookup = owner.post(
        "/investments/provider-lookup",
        {
            "provider_key": "coingecko",
            "provider_symbol": "bitcoin",
            "type": "crypto",
            "quote_currency": "USD",
            "unit_label": "coin",
        },
        token_from=INSTRUMENTS,
    )
    assert lookup.status_code == 200
    assert "Provider price: 12345.67 USD as of Mar 2, 2026" in lookup.text
    with owner.db() as session:
        assert len(list(session.scalars(select(Instrument)))) == 1


def test_lookup_rate_limiting_has_a_clear_message(
    owner: OwnerApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "assetboard.web.provider_routes.lookup_provider",
        lambda *_args, **_kwargs: LookupResult(
            "rate_limited", code="rate_limited", retry_after=120
        ),
    )
    response = owner.post(
        "/investments/provider-lookup",
        {
            "provider_key": "coingecko",
            "provider_symbol": "bitcoin",
            "type": "crypto",
            "quote_currency": "USD",
            "unit_label": "coin",
        },
        token_from=INSTRUMENTS,
    )
    assert response.status_code == 200
    assert "Rate limited; try again in 2 minutes." in response.text


def test_a_disabled_provider_cannot_be_assigned_over_http(owner: OwnerApp) -> None:
    response = owner.post(
        INSTRUMENTS,
        {
            "name": "Bitcoin",
            "symbol": "",
            "type": "crypto",
            "quote_currency": "USD",
            "unit_label": "coin",
            "provider_key": "coingecko",
            "provider_symbol": "bitcoin",
            f"weight_{_class_id(owner)}": "100",
        },
        token_from=INSTRUMENTS,
    )
    assert response.status_code == 400
    assert "Enable this provider in Providers before assigning it." in response.text


def test_eodhd_failure_does_not_leak_the_key(
    owner: OwnerApp, caplog: pytest.LogCaptureFixture
) -> None:
    key = "EODHD-SECRET-TOKEN"
    response = owner.post(
        PROVIDERS + "/eodhd",
        {"enabled": "1", "api_key": key, "daily_call_budget": "5"},
        token_from=PROVIDERS,
    )
    assert response.status_code == 303
    instrument = _instrument(
        owner,
        "VWCE",
        type_value="etf",
        quote_currency="EUR",
        provider_key="eodhd",
        provider_symbol="VWCE.DE",
    )
    _buy(owner, instrument)
    time_source = FakeTime()
    owner.app.state.provider_runtime = build_provider_runtime(
        owner.app.state.settings,
        owner.app.state.clock,
        http=FakeProviderHttpClient(lambda _url: HttpResponse(500, b"", {})),
        sleep=time_source.sleep,
        monotonic=time_source.monotonic,
    )
    with caplog.at_level(logging.DEBUG):
        page = owner.client.post(
            PROVIDERS + "/eodhd/refresh",
            data={"csrf_token": owner.token(PROVIDERS)},
            follow_redirects=True,
        ).text
        instrument_page = owner.client.get(f"{INSTRUMENTS}/{instrument}").text
    assert key not in page
    assert key not in instrument_page
    assert key not in caplog.text
    with owner.db() as session:
        errors = list(session.scalars(select(ProviderError)))
        assert errors and errors[-1].code == "http_500"
        assert all(key not in str(error.code) for error in errors)
        events = list(
            session.scalars(select(AuditEvent).where(AuditEvent.action == "price_source.refreshed"))
        )
        assert events and all(key not in str(event.data) for event in events)


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
