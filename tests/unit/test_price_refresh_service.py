"""Provider refresh, FX conversion, backfill and status (F6.4)."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.db.models.identity import AuditEvent, Workspace
from assetboard.db.models.prices import PricePoint, ProviderError
from assetboard.db.repositories import (
    AssetClassRepository,
    FxRateRepository,
    InstrumentRepository,
    PricePointRepository,
    PriceSourceConfigRepository,
    ProviderErrorRepository,
)
from assetboard.domain.providers import normalize_provider_decimal
from assetboard.services.investment_transactions import create_transaction
from assetboard.services.investments import create_asset_class, create_instrument
from assetboard.services.price_refresh import (
    _prune_errors,
    _upsert_prices,
    lookup_provider,
    provider_status,
    refresh_all,
    refresh_now,
    refresh_provider,
)
from assetboard.services.price_sources import save_provider_config
from assetboard.services.providers.ecb import HISTORY_URL
from assetboard.services.providers.nbp import GOLD_LATEST
from assetboard.services.providers.registry import build_provider_runtime
from assetboard.services.providers.transport import HttpResponse
from tests.fake_provider_http import (
    FailingProviderHttpClient,
    FakeProviderHttpClient,
    FakeTime,
    ResponseValue,
)
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


class TickingClock(FixedClock):
    def __init__(self) -> None:
        super().__init__("2026-03-10T12:00:00+00:00")
        self._ticks = 0

    def now(self) -> datetime:
        self._ticks += 1
        return super().now() + timedelta(seconds=self._ticks)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    for prepared in workspace_db(tmp_path):
        clock = TickingClock()
        prepared.clock = clock
        prepared.factory.kw["info"]["clock"] = clock
        yield prepared


@pytest.fixture
def team(db: WorkspaceDb) -> WorkspaceDb:
    db.add_member("olga", "owner")
    db.add_member("ed", "editor")
    return db


SETTINGS = Settings(_env_file=None, secret_key="test-secret")
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "providers"


def _owner(db: WorkspaceDb):
    return db.ctx("olga", "owner")


def _class_id(db: WorkspaceDb, ctx) -> UUID:
    with db.factory() as session:
        existing = AssetClassRepository(session, ctx).active_by_name_key("stocks")
        if existing is not None:
            return existing.id
    return create_asset_class(db.factory, ctx, name="Stocks", color="#2563eb", investable=True).id


def _instrument(
    db: WorkspaceDb,
    ctx,
    *,
    name: str = "World ETF",
    type_value: str = "etf",
    quote_currency: str = "USD",
    unit_label: str = "share",
):
    return create_instrument(
        db.factory,
        ctx,
        name=name,
        symbol="",
        type=type_value,
        quote_currency=quote_currency,
        unit_label=unit_label,
        allocations=[(_class_id(db, ctx), 10_000)],
        locale="en",
    )


def _tx(db: WorkspaceDb, ctx, instrument_id: UUID, *, date_value="2026-03-01", fx="1"):
    return create_transaction(
        db.factory,
        ctx,
        instrument_id=instrument_id,
        type_value="buy",
        date_value=date_value,
        quantity_value="10",
        unit_price_value="10",
        fee_value="0",
        fx_value=fx,
        note_value="",
        locale="en",
    )


def _configure_provider(
    db: WorkspaceDb, ctx, instrument_id: UUID, provider_key: str = "nbp", symbol: str = "XAU"
) -> None:
    with db.factory.begin() as session:
        InstrumentRepository(session, ctx).update(
            instrument_id,
            provider_key=provider_key,
            provider_symbol=symbol,
            valuation_source="provider",
        )


def _enable(db: WorkspaceDb, ctx, key: str) -> None:
    save_provider_config(db.factory, ctx, SETTINGS, key, enabled=True)


def _set_base(db: WorkspaceDb, currency: str) -> None:
    with db.factory.begin() as session:
        workspace = session.get(Workspace, db.workspace_id)
        assert workspace is not None
        workspace.base_currency = currency


def _set_timezone(db: WorkspaceDb, timezone: str) -> None:
    with db.factory.begin() as session:
        workspace = session.get(Workspace, db.workspace_id)
        assert workspace is not None
        workspace.timezone = timezone


def _runtime(db: WorkspaceDb, http, time_source: FakeTime | None = None):
    time_source = time_source or FakeTime()
    return build_provider_runtime(
        SETTINGS,
        db.clock,
        http=http,
        sleep=time_source.sleep,
        monotonic=time_source.monotonic,
    )


def _body(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _prices(db: WorkspaceDb) -> list[PricePoint]:
    with db.factory() as session:
        return list(session.scalars(select(PricePoint).order_by(PricePoint.date)))


def _errors(db: WorkspaceDb) -> list[ProviderError]:
    with db.factory() as session:
        return list(
            session.scalars(
                select(ProviderError).order_by(ProviderError.occurred_at, ProviderError.id)
            )
        )


def test_latest_gold_price_is_converted_to_troy_ounces(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    _set_base(db, "PLN")
    instrument = _instrument(
        db, ctx, name="Gold", type_value="metal", quote_currency="PLN", unit_label="oz"
    )
    _tx(db, ctx, instrument.id)
    _configure_provider(db, ctx, instrument.id)
    _enable(db, ctx, "nbp")
    runtime = _runtime(db, FakeProviderHttpClient({GOLD_LATEST: _body("nbp_gold.json")}))

    stats = refresh_provider(db.factory, ctx, "nbp", runtime, mode="latest")

    assert (stats.status, stats.prices, stats.errors) == ("ok", 1, 0)
    point = _prices(db)[0]
    with localcontext() as context:
        context.prec = 80
        expected = Decimal("250.00") * Decimal("31.1034768")
    assert point.price == expected
    assert point.provider_key == "nbp"
    assert point.source == "provider"
    with db.factory() as session:
        config = PriceSourceConfigRepository(session, ctx).for_provider("nbp")
        assert config is not None and config.last_success_at is not None
    assert _errors(db) == []


def test_nbp_fx_is_converted_into_the_workspace_base(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    _set_base(db, "EUR")
    instrument = _instrument(db, ctx, name="US fund", quote_currency="USD")
    _tx(db, ctx, instrument.id, fx="0.90")
    _enable(db, ctx, "nbp")
    # Only FX is needed; the fake must serve the NBP table endpoints.
    fake = FakeProviderHttpClient(
        {"https://api.nbp.pl/api/exchangerates/tables/a/?format=json": _body("nbp_table_a.json")}
    )
    runtime = _runtime(db, fake)
    stats = refresh_provider(db.factory, ctx, "nbp", runtime, mode="latest")

    assert (stats.status, stats.rates, stats.errors) == ("ok", 1, 0)
    with db.factory() as session:
        row = FxRateRepository(session, ctx).rate_on("EUR", "USD", date(2026, 3, 2), "nbp")
        assert row is not None
        with localcontext() as context:
            context.prec = 80
            expected = Decimal("3.9000") / Decimal("4.3000")
        assert row.rate == normalize_provider_decimal(expected, positive=True)


def test_ecb_fx_in_pln_workspace_is_converted_through_eur(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    _set_base(db, "PLN")
    instrument = _instrument(db, ctx, name="US fund", quote_currency="USD")
    _tx(db, ctx, instrument.id, fx="4.30")
    _enable(db, ctx, "ecb")
    runtime = _runtime(db, FakeProviderHttpClient({HISTORY_URL: _body("ecb_history.xml")}))

    stats = refresh_provider(db.factory, ctx, "ecb", runtime, mode="latest")

    assert (stats.status, stats.rates, stats.errors) == ("ok", 1, 0)
    with db.factory() as session:
        row = FxRateRepository(session, ctx).rate_on("PLN", "USD", date(2026, 3, 2), "ecb")
        assert row is not None
        with localcontext() as context:
            context.prec = 80
            expected = Decimal("4.3000") / Decimal("1.1000")
        assert row.rate == normalize_provider_decimal(expected, positive=True)


def test_no_enabled_provider_makes_no_network_call(db: WorkspaceDb, team: WorkspaceDb) -> None:
    fake = FailingProviderHttpClient()
    runtime = _runtime(db, fake)
    stats = refresh_all(db.factory, "nbp", runtime, logging.getLogger())
    assert stats[0].status == "disabled"
    assert not fake.called


def test_only_held_instruments_are_refreshed(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    _set_base(db, "PLN")
    instrument = _instrument(
        db, ctx, name="Gold", type_value="metal", quote_currency="PLN", unit_label="oz"
    )
    _configure_provider(db, ctx, instrument.id)
    _enable(db, ctx, "nbp")
    fake = FailingProviderHttpClient()
    stats = refresh_provider(db.factory, ctx, "nbp", _runtime(db, fake), mode="latest")
    assert stats.status == "no_targets"
    assert not fake.called


def test_a_disabled_provider_makes_no_network_call(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    _enable(db, ctx, "nbp")
    save_provider_config(db.factory, ctx, SETTINGS, "nbp", enabled=False)
    fake = FailingProviderHttpClient()
    stats = refresh_provider(db.factory, ctx, "nbp", _runtime(db, fake))
    assert stats.status == "disabled"
    assert not fake.called


def test_an_unpublished_workspace_base_skips_fx_and_records_the_reason(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    _set_base(db, "BRL")
    instrument = _instrument(db, ctx, name="US fund", quote_currency="USD")
    _tx(db, ctx, instrument.id, fx="5.00")
    _enable(db, ctx, "nbp")
    runtime = _runtime(
        db,
        FakeProviderHttpClient(
            {
                "https://api.nbp.pl/api/exchangerates/tables/a/?format=json": _body(
                    "nbp_table_a.json"
                ),
                "https://api.nbp.pl/api/exchangerates/tables/b/?format=json": _body(
                    "nbp_table_b.json"
                ),
            }
        ),
    )

    stats = refresh_provider(db.factory, ctx, "nbp", runtime, mode="latest")

    assert stats.status == "error"
    with db.factory() as session:
        assert FxRateRepository(session, ctx).for_base_on_or_before("BRL", date(2026, 3, 10)) == []
        config = PriceSourceConfigRepository(session, ctx).for_provider("nbp")
        assert config is not None and config.last_error_code == "base_not_published"
    assert {error.code for error in _errors(db)} == {"base_not_published"}


def test_a_failure_keeps_the_last_price_and_records_an_error(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    _set_base(db, "PLN")
    instrument = _instrument(
        db, ctx, name="Gold", type_value="metal", quote_currency="PLN", unit_label="oz"
    )
    _tx(db, ctx, instrument.id)
    _configure_provider(db, ctx, instrument.id)
    _enable(db, ctx, "nbp")
    with db.factory.begin() as session:
        PricePointRepository(session, ctx).create(
            instrument_id=instrument.id,
            date=date(2026, 3, 1),
            price=Decimal("100"),
            source="provider",
            provider_key="nbp",
        )
    runtime = _runtime(
        db,
        FakeProviderHttpClient({GOLD_LATEST: HttpResponse(500, b"", {})}),
    )

    stats = refresh_provider(db.factory, ctx, "nbp", runtime, mode="latest")

    assert stats.status == "error"
    assert _prices(db)[0].price == Decimal("100")
    error = _errors(db)[0]
    assert error.code == "http_500"
    with db.factory() as session:
        row = ProviderErrorRepository(session, ctx).latest_for_provider("nbp")
        assert row is not None and row.code == "http_500"


def test_a_web_rate_limit_is_recorded_without_waiting(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    _set_base(db, "PLN")
    instrument = _instrument(
        db, ctx, name="Gold", type_value="metal", quote_currency="PLN", unit_label="oz"
    )
    _tx(db, ctx, instrument.id)
    _configure_provider(db, ctx, instrument.id)
    _enable(db, ctx, "nbp")
    runtime = _runtime(
        db,
        FakeProviderHttpClient({GOLD_LATEST: HttpResponse(429, b"", {"Retry-After": "3600"})}),
    )

    stats = refresh_provider(db.factory, ctx, "nbp", runtime, mode="latest", wait=False)

    assert (stats.status, stats.retry_after) == ("rate_limited", 3600)
    assert _errors(db)[0].code == "rate_limited"


def test_full_refresh_backfills_history_and_sets_the_marker(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    _set_base(db, "PLN")
    instrument = _instrument(
        db, ctx, name="Gold", type_value="metal", quote_currency="PLN", unit_label="oz"
    )
    _tx(db, ctx, instrument.id, date_value="2026-02-27")
    _configure_provider(db, ctx, instrument.id)
    _enable(db, ctx, "nbp")
    history_url = "https://api.nbp.pl/api/cenyzlota/2026-02-27/2026-03-10/?format=json"
    runtime = _runtime(
        db,
        FakeProviderHttpClient(
            {
                GOLD_LATEST: _body("nbp_gold.json"),
                history_url: _body("nbp_gold_history.json"),
            }
        ),
    )

    stats = refresh_provider(db.factory, ctx, "nbp", runtime, mode="full")

    assert stats.status in {"ok", "partial"}
    assert [point.date for point in _prices(db)] == [
        date(2026, 2, 27),
        date(2026, 3, 2),
    ]
    with db.factory() as session:
        row = InstrumentRepository(session, ctx).get_or_raise(instrument.id)
        assert row.provider_backfilled_at is not None


def test_full_refresh_backfills_fx_history_through_the_provider_base(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    _set_base(db, "EUR")
    instrument = _instrument(db, ctx, name="US fund", quote_currency="USD")
    _tx(db, ctx, instrument.id, date_value="2026-03-01", fx="0.90")
    _enable(db, ctx, "nbp")
    latest_url = "https://api.nbp.pl/api/exchangerates/tables/a/?format=json"
    range_url = "https://api.nbp.pl/api/exchangerates/tables/a/2026-03-01/2026-03-10/?format=json"
    runtime = _runtime(
        db,
        FakeProviderHttpClient(
            {latest_url: _body("nbp_table_a.json"), range_url: _body("nbp_table_a.json")}
        ),
    )

    stats = refresh_provider(db.factory, ctx, "nbp", runtime, mode="full")

    assert stats.status in {"ok", "partial"}
    with db.factory() as session:
        row = FxRateRepository(session, ctx).rate_on("EUR", "USD", date(2026, 3, 2), "nbp")
        assert row is not None
        with localcontext() as context:
            context.prec = 80
            expected = Decimal("3.9000") / Decimal("4.3000")
        assert row.rate == normalize_provider_decimal(expected, positive=True)


def test_three_currency_cross_matches_the_provider_snapshot(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    _set_base(db, "PLN")
    usd = _instrument(db, ctx, name="US fund", quote_currency="USD")
    chf = _instrument(db, ctx, name="Swiss fund", quote_currency="CHF")
    _tx(db, ctx, usd.id, fx="4.30")
    _tx(db, ctx, chf.id, fx="4.30")
    _enable(db, ctx, "ecb")
    runtime = _runtime(db, FakeProviderHttpClient({HISTORY_URL: _body("ecb_history.xml")}))

    refresh_provider(db.factory, ctx, "ecb", runtime, mode="latest")

    with db.factory() as session:
        repository = FxRateRepository(session, ctx)
        pln_usd = repository.rate_on("PLN", "USD", date(2026, 3, 2), "ecb")
        pln_chf = repository.rate_on("PLN", "CHF", date(2026, 3, 2), "ecb")
        assert pln_usd is not None and pln_chf is not None
        with localcontext() as context:
            context.prec = 80
            cross = pln_usd.rate / pln_chf.rate
            provider_cross = Decimal("0.9500") / Decimal("1.1000")
        assert normalize_provider_decimal(cross, positive=True) == (
            normalize_provider_decimal(provider_cross, positive=True)
        )


def test_fx_backfill_is_one_off_even_when_the_first_date_is_a_weekend(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    _set_base(db, "EUR")
    instrument = _instrument(db, ctx, name="US fund", quote_currency="USD")
    _tx(db, ctx, instrument.id, date_value="2026-02-28", fx="0.90")
    _enable(db, ctx, "nbp")
    latest_url = "https://api.nbp.pl/api/exchangerates/tables/a/?format=json"
    range_url = "https://api.nbp.pl/api/exchangerates/tables/a/2026-02-28/2026-03-10/?format=json"
    fake = FakeProviderHttpClient(
        {latest_url: _body("nbp_table_a.json"), range_url: _body("nbp_table_a.json")}
    )
    runtime = _runtime(db, fake)

    refresh_provider(db.factory, ctx, "nbp", runtime, mode="full")
    calls_after_first = len(fake.calls)
    with db.factory() as session:
        config = PriceSourceConfigRepository(session, ctx).for_provider("nbp")
        assert config is not None and config.fx_backfilled is not None
        assert "2026-02-28" in config.fx_backfilled

    refresh_provider(db.factory, ctx, "nbp", runtime, mode="full")
    assert len(fake.calls) == calls_after_first


def test_eodhd_daily_quota_skips_remaining_symbols_and_resets_next_day(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    symbols = ["AAA.US", "BBB.US", "CCC.US"]
    for index, symbol in enumerate(symbols):
        instrument = _instrument(
            db, ctx, name=f"Fund {index}", type_value="etf", quote_currency="EUR"
        )
        _tx(db, ctx, instrument.id, fx="1")
        _configure_provider(db, ctx, instrument.id, "eodhd", symbol)
    save_provider_config(
        db.factory,
        ctx,
        SETTINGS,
        "eodhd",
        enabled=True,
        settings_values={"api_key": "test-key"},
        daily_call_budget=2,
    )
    fake = FakeProviderHttpClient(lambda _url: _body("eodhd_eod.json"))
    runtime = _runtime(db, fake)

    stats = refresh_provider(db.factory, ctx, "eodhd", runtime, mode="latest")

    assert (stats.prices, stats.skipped) == (2, 1)
    assert len(fake.calls) == 2
    assert [error.code for error in _errors(db)] == ["quota_exhausted"]
    with db.factory() as session:
        config = PriceSourceConfigRepository(session, ctx).for_provider("eodhd")
        assert config is not None
        assert (config.quota_day, config.quota_used) == (date(2026, 3, 10), 2)

    save_provider_config(db.factory, ctx, SETTINGS, "eodhd", enabled=True, daily_call_budget=3)
    db.clock._value += timedelta(days=1)
    db.factory.kw["info"]["clock"] = db.clock
    refresh_provider(db.factory, ctx, "eodhd", runtime, mode="latest")
    with db.factory() as session:
        config = PriceSourceConfigRepository(session, ctx).for_provider("eodhd")
        assert config is not None
        assert (config.quota_day, config.quota_used) == (date(2026, 3, 11), 3)
    assert len(_prices(db)) == 3


def test_lookup_failures_are_informational(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    _enable(db, ctx, "coingecko")
    runtime = _runtime(db, FakeProviderHttpClient(lambda _url: HttpResponse(500, b"", {})))

    result = lookup_provider(
        db.factory,
        ctx,
        "coingecko",
        runtime,
        type_value="crypto",
        quote_currency="USD",
        unit_label="coin",
        provider_symbol="bitcoin",
    )

    assert (result.status, result.code) == ("error", "http_500")
    with db.factory() as session:
        config = PriceSourceConfigRepository(session, ctx).for_provider("coingecko")
        assert config is not None and config.last_error_code is None
        errors = list(session.scalars(select(ProviderError)))
        assert [error.code for error in errors] == ["http_500"]


GOLDAPI_SECRET = "GOLDAPI-SENTINEL-KEY-7f3a"


def _goldapi_metal(
    db: WorkspaceDb,
    ctx,
    symbol: str,
    *,
    unit_label: str = "oz",
    quote_currency: str = "PLN",
    first_trade: str = "2026-03-01",
) -> UUID:
    instrument = _instrument(
        db,
        ctx,
        name=f"Metal {symbol}",
        type_value="metal",
        quote_currency=quote_currency,
        unit_label=unit_label,
    )
    _tx(db, ctx, instrument.id, date_value=first_trade, fx="1")
    _configure_provider(db, ctx, instrument.id, "goldapi", symbol)
    return instrument.id


def _enable_goldapi(db: WorkspaceDb, ctx, budget: int | None = None) -> None:
    save_provider_config(
        db.factory,
        ctx,
        SETTINGS,
        "goldapi",
        enabled=True,
        settings_values={"api_key": GOLDAPI_SECRET},
        daily_call_budget=budget,
    )


def _goldapi_responder(url: str) -> ResponseValue:
    if url.startswith(GOLDAPI_HISTORY_PREFIX):
        return _body("goldapi_history.json")
    if url.startswith(GOLDAPI_PRICE_PREFIX):
        return _body("goldapi_latest.json")
    raise AssertionError(url)


GOLDAPI_PRICE_PREFIX = "https://www.goldapi.io/api/X"
GOLDAPI_HISTORY_PREFIX = "https://www.goldapi.io/api/history/"


def _quota(db: WorkspaceDb, ctx) -> tuple[date | None, int]:
    with db.factory() as session:
        config = PriceSourceConfigRepository(session, ctx).for_provider("goldapi")
        assert config is not None
        return config.quota_day, config.quota_used


def _last_error_code(db: WorkspaceDb, ctx) -> str | None:
    with db.factory() as session:
        config = PriceSourceConfigRepository(session, ctx).for_provider("goldapi")
        assert config is not None
        return config.last_error_code


def test_goldapi_daily_quota_skips_remaining_metals_and_resets_next_utc_day(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    _set_base(db, "PLN")
    _goldapi_metal(db, ctx, "XAU")
    _goldapi_metal(db, ctx, "XAG")
    _enable_goldapi(db, ctx, budget=1)
    fake = FakeProviderHttpClient(_goldapi_responder)
    runtime = _runtime(db, fake)

    stats = refresh_provider(db.factory, ctx, "goldapi", runtime, mode="latest")

    assert (stats.prices, stats.skipped, stats.errors) == (1, 1, 0)
    assert len(fake.calls) == 1
    assert [error.code for error in _errors(db)] == ["quota_exhausted"]
    assert _quota(db, ctx) == (date(2026, 3, 10), 1)
    assert _last_error_code(db, ctx) is None  # informational: no provider-card error

    # The same UTC day: nothing more is spent.
    refresh_provider(db.factory, ctx, "goldapi", runtime, mode="latest")
    assert len(fake.calls) == 1

    # The next UTC day the counter resets and the budget is available again.
    db.clock._value += timedelta(days=1)
    db.factory.kw["info"]["clock"] = db.clock
    runtime = _runtime(db, fake)
    stats = refresh_provider(db.factory, ctx, "goldapi", runtime, mode="latest")
    assert (stats.prices, stats.skipped) == (1, 1)
    assert len(fake.calls) == 2
    assert _quota(db, ctx) == (date(2026, 3, 11), 1)


def test_goldapi_spends_latest_calls_first_and_history_uses_the_remainder(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    _set_base(db, "PLN")
    _goldapi_metal(db, ctx, "XAU", first_trade="2026-02-27")
    _goldapi_metal(db, ctx, "XAG", first_trade="2026-02-27")
    _enable_goldapi(db, ctx, budget=3)
    fake = FakeProviderHttpClient(_goldapi_responder)

    stats = refresh_provider(db.factory, ctx, "goldapi", _runtime(db, fake), mode="full")

    urls = [url for url, _headers in fake.calls]
    assert len(urls) == 3
    assert all(url.startswith(GOLDAPI_PRICE_PREFIX) for url in urls[:2])
    assert urls[2].startswith(GOLDAPI_HISTORY_PREFIX)
    assert stats.skipped == 1
    assert [error.code for error in _errors(db)] == ["quota_exhausted"]
    assert _quota(db, ctx) == (date(2026, 3, 10), 3)
    with db.factory() as session:
        marked = [
            row.provider_backfilled_at is not None
            for row in InstrumentRepository(session, ctx).ordered()
        ]
    assert sorted(marked) == [False, True]


def test_goldapi_history_waits_for_budget_after_latest(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    _set_base(db, "PLN")
    instrument_id = _goldapi_metal(db, ctx, "XAG", first_trade="2026-02-27")
    _enable_goldapi(db, ctx, budget=1)
    fake = FakeProviderHttpClient(_goldapi_responder)

    refresh_provider(db.factory, ctx, "goldapi", _runtime(db, fake), mode="full")

    assert [url for url, _headers in fake.calls] == ["https://www.goldapi.io/api/XAG/PLN"]
    assert [error.code for error in _errors(db)] == ["quota_exhausted"]
    with db.factory() as session:
        row = InstrumentRepository(session, ctx).get_or_raise(instrument_id)
        assert row.provider_backfilled_at is None


def test_goldapi_history_is_limited_to_the_latest_90_days(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    _set_base(db, "PLN")
    instrument_id = _goldapi_metal(db, ctx, "XAG", first_trade="2020-01-01")
    _enable_goldapi(db, ctx)
    fake = FakeProviderHttpClient(_goldapi_responder)
    runtime = _runtime(db, fake)

    stats = refresh_provider(db.factory, ctx, "goldapi", runtime, mode="full")

    assert stats.status == "ok"
    history = [url for url, _headers in fake.calls if url.startswith(GOLDAPI_HISTORY_PREFIX)]
    assert history == ["https://www.goldapi.io/api/history/XAG/PLN?from=2025-12-11&to=2026-03-10"]
    assert [error.code for error in _errors(db)] == ["history_limited"]
    assert _last_error_code(db, ctx) is None  # informational: no provider-card error
    assert [point.date for point in _prices(db)] == [date(2026, 2, 27), date(2026, 3, 2)]
    with db.factory() as session:
        row = InstrumentRepository(session, ctx).get_or_raise(instrument_id)
        assert row.provider_backfilled_at is not None
    status = provider_status(db.factory, ctx, instrument_id)
    assert status is not None and status.history_limited and not status.error

    # The one-off marker prevents a retry of the older history.
    refresh_provider(db.factory, ctx, "goldapi", runtime, mode="full")
    history = [url for url, _headers in fake.calls if url.startswith(GOLDAPI_HISTORY_PREFIX)]
    assert len(history) == 1


def _history_failing(response: ResponseValue):
    def respond(url: str) -> ResponseValue:
        if url.startswith(GOLDAPI_HISTORY_PREFIX):
            return response
        return _body("goldapi_latest.json")

    return respond


def _history_calls(fake: FakeProviderHttpClient) -> int:
    return sum(1 for url, _headers in fake.calls if url.startswith(GOLDAPI_HISTORY_PREFIX))


def _backfilled(db: WorkspaceDb, ctx, instrument_id: UUID) -> bool:
    with db.factory() as session:
        row = InstrumentRepository(session, ctx).get_or_raise(instrument_id)
        return row.provider_backfilled_at is not None


@pytest.mark.parametrize(
    "response",
    [
        HttpResponse(404, b"", {}),
        HttpResponse(400, b"", {}),
        HttpResponse(422, b"", {}),
        b"<html>not json</html>",
        b'{"prices": "none"}',
    ],
)
def test_goldapi_permanent_history_failure_is_marked_and_not_retried(
    db: WorkspaceDb, team: WorkspaceDb, response: ResponseValue
) -> None:
    ctx = _owner(db)
    _set_base(db, "PLN")
    instrument_id = _goldapi_metal(db, ctx, "XAG", first_trade="2026-02-27")
    _enable_goldapi(db, ctx)
    fake = FakeProviderHttpClient(_history_failing(response))

    stats = refresh_provider(db.factory, ctx, "goldapi", _runtime(db, fake), mode="full")

    assert stats.prices == 1  # the latest price still lands
    assert _history_calls(fake) == 1
    assert [error.code for error in _errors(db)] == ["history_limited"]
    assert _last_error_code(db, ctx) is None
    assert _backfilled(db, ctx, instrument_id)

    refresh_provider(db.factory, ctx, "goldapi", _runtime(db, fake), mode="full")
    assert _history_calls(fake) == 1
    assert _quota(db, ctx) == (date(2026, 3, 10), 3)  # latest, history, latest


def test_goldapi_transient_history_failure_is_retried_next_run(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    _set_base(db, "PLN")
    instrument_id = _goldapi_metal(db, ctx, "XAG", first_trade="2026-02-27")
    _enable_goldapi(db, ctx)
    fake = FakeProviderHttpClient(_history_failing(HttpResponse(503, b"", {})))

    refresh_provider(db.factory, ctx, "goldapi", _runtime(db, fake), mode="full")

    assert [error.code for error in _errors(db)] == ["http_503"]
    assert not _backfilled(db, ctx, instrument_id)
    first_run = _history_calls(fake)
    assert first_run >= 1

    refresh_provider(db.factory, ctx, "goldapi", _runtime(db, fake), mode="full")
    assert _history_calls(fake) > first_run
    assert not _backfilled(db, ctx, instrument_id)


@pytest.mark.parametrize("status", [401, 403])
def test_goldapi_key_failure_in_history_is_shown_and_retried(
    db: WorkspaceDb, team: WorkspaceDb, status: int
) -> None:
    ctx = _owner(db)
    _set_base(db, "PLN")
    instrument_id = _goldapi_metal(db, ctx, "XAG", first_trade="2026-02-27")
    _enable_goldapi(db, ctx)
    fake = FakeProviderHttpClient(_history_failing(HttpResponse(status, b"", {})))

    refresh_provider(db.factory, ctx, "goldapi", _runtime(db, fake), mode="full")

    assert [error.code for error in _errors(db)] == [f"http_{status}"]
    assert _last_error_code(db, ctx) == f"http_{status}"
    assert not _backfilled(db, ctx, instrument_id)
    refresh_provider(db.factory, ctx, "goldapi", _runtime(db, fake), mode="full")
    assert _history_calls(fake) == 2


def test_eodhd_permanent_history_failure_is_marked_but_5xx_is_retried(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    missing = _instrument(db, ctx, name="Missing", quote_currency="EUR")
    flaky = _instrument(db, ctx, name="Flaky", quote_currency="EUR")
    for instrument, symbol in ((missing, "GONE.US"), (flaky, "FLAKY.US")):
        _tx(db, ctx, instrument.id, fx="1")
        _configure_provider(db, ctx, instrument.id, "eodhd", symbol)
    save_provider_config(
        db.factory, ctx, SETTINGS, "eodhd", enabled=True, settings_values={"api_key": "k"}
    )

    def respond(url: str) -> ResponseValue:
        if "order=d" in url:
            return _body("eodhd_eod.json")
        if "GONE.US" in url:
            return HttpResponse(404, b"", {})
        return HttpResponse(502, b"", {})

    runtime = _runtime(db, FakeProviderHttpClient(respond))
    refresh_provider(db.factory, ctx, "eodhd", runtime, mode="full")

    with db.factory() as session:
        repository = InstrumentRepository(session, ctx)
        assert repository.get_or_raise(missing.id).provider_backfilled_at is not None
        assert repository.get_or_raise(flaky.id).provider_backfilled_at is None
    assert sorted(error.code for error in _errors(db)) == ["history_limited", "http_502"]


def test_goldapi_instruments_on_the_same_metal_share_one_call(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    _set_base(db, "PLN")
    coins = _goldapi_metal(db, ctx, "XAU")
    bars = _instrument(
        db, ctx, name="Gold bars", type_value="metal", quote_currency="PLN", unit_label="g"
    )
    _tx(db, ctx, bars.id, fx="1")
    _configure_provider(db, ctx, bars.id, "goldapi", "xau")
    _enable_goldapi(db, ctx, budget=1)
    fake = FakeProviderHttpClient(_goldapi_responder)

    stats = refresh_provider(db.factory, ctx, "goldapi", _runtime(db, fake), mode="latest")

    assert (stats.prices, stats.skipped) == (2, 0)
    assert [url for url, _headers in fake.calls] == ["https://www.goldapi.io/api/XAU/PLN"]
    assert _quota(db, ctx) == (date(2026, 3, 10), 1)
    assert {point.instrument_id for point in _prices(db)} == {coins, bars.id}
    assert _errors(db) == []


def test_eodhd_instruments_on_the_same_ticker_share_one_reservation(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    for name in ("Fund A", "Fund B"):
        instrument = _instrument(db, ctx, name=name, quote_currency="EUR")
        _tx(db, ctx, instrument.id, fx="1")
        _configure_provider(db, ctx, instrument.id, "eodhd", "VWCE.DE")
    save_provider_config(
        db.factory,
        ctx,
        SETTINGS,
        "eodhd",
        enabled=True,
        settings_values={"api_key": "test-key"},
        daily_call_budget=1,
    )
    fake = FakeProviderHttpClient(lambda _url: _body("eodhd_eod.json"))

    stats = refresh_provider(db.factory, ctx, "eodhd", _runtime(db, fake), mode="latest")

    assert (stats.prices, stats.skipped) == (2, 0)
    assert len(fake.calls) == 1
    with db.factory() as session:
        config = PriceSourceConfigRepository(session, ctx).for_provider("eodhd")
        assert config is not None and config.quota_used == 1


def test_goldapi_history_inside_the_window_is_not_limited(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    _set_base(db, "PLN")
    _goldapi_metal(db, ctx, "XAG", first_trade="2026-02-27")
    _enable_goldapi(db, ctx)
    fake = FakeProviderHttpClient(_goldapi_responder)

    refresh_provider(db.factory, ctx, "goldapi", _runtime(db, fake), mode="full")

    assert any(
        url == "https://www.goldapi.io/api/history/XAG/PLN?from=2026-02-27&to=2026-03-10"
        for url, _headers in fake.calls
    )
    assert _errors(db) == []


def test_goldapi_ounce_price_is_converted_to_grams_and_kilograms(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    _set_base(db, "PLN")
    grams = _goldapi_metal(db, ctx, "XAG", unit_label="g")
    kilograms = _goldapi_metal(db, ctx, "XAU", unit_label="kg")
    _enable_goldapi(db, ctx)

    refresh_provider(
        db.factory, ctx, "goldapi", _runtime(db, FakeProviderHttpClient(_goldapi_responder))
    )

    with localcontext() as context:
        context.prec = 80
        per_gram = Decimal("123.4567") / Decimal("31.1034768")
        per_kilogram = Decimal("123.4567") * 1000 / Decimal("31.1034768")
    prices = {point.instrument_id: point.price for point in _prices(db)}
    assert prices[grams] == normalize_provider_decimal(per_gram, positive=True)
    assert prices[kilograms] == normalize_provider_decimal(per_kilogram, positive=True)


def test_goldapi_future_utc_day_is_capped_at_the_workspace_today(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    _set_timezone(db, "America/New_York")
    clock = FixedClock("2026-03-10T00:10:00+00:00")
    db.clock = clock
    db.factory.kw["info"]["clock"] = clock
    ctx = _owner(db)
    _set_base(db, "PLN")
    _goldapi_metal(db, ctx, "XAG")
    _enable_goldapi(db, ctx)
    body = b'{"metal": "XAG", "currency": "PLN", "price": 123.4567, "timestamp": 1773101400}'
    runtime = _runtime(db, FakeProviderHttpClient(lambda _url: body))

    stats = refresh_provider(db.factory, ctx, "goldapi", runtime, mode="latest")
    request_ctx = replace(ctx, workspace=replace(ctx.workspace, timezone="America/New_York"))
    result = lookup_provider(
        db.factory,
        request_ctx,
        "goldapi",
        runtime,
        type_value="metal",
        quote_currency="PLN",
        unit_label="oz",
        provider_symbol="XAG",
    )

    assert stats.status == "ok"
    assert [point.date for point in _prices(db)] == [date(2026, 3, 9)]
    assert (result.status, result.date) == ("ok", date(2026, 3, 9))


def test_goldapi_lookup_reserves_quota_and_converts_the_unit(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    _enable_goldapi(db, ctx, budget=1)
    fake = FakeProviderHttpClient(_goldapi_responder)
    runtime = _runtime(db, fake)

    def lookup():
        return lookup_provider(
            db.factory,
            ctx,
            "goldapi",
            runtime,
            type_value="metal",
            quote_currency="PLN",
            unit_label="kg",
            provider_symbol="xag",
        )

    first = lookup()
    with localcontext() as context:
        context.prec = 80
        per_kilogram = Decimal("123.4567") * 1000 / Decimal("31.1034768")
    assert (first.status, first.currency) == ("ok", "PLN")
    assert first.price == normalize_provider_decimal(per_kilogram, positive=True)
    assert _quota(db, ctx) == (date(2026, 3, 10), 1)
    second = lookup()
    assert (second.status, second.code) == ("quota_exhausted", "quota_exhausted")
    assert len(fake.calls) == 1


@pytest.mark.parametrize("status", [401, 403, 429])
def test_goldapi_key_never_reaches_logs_errors_or_audit(
    db: WorkspaceDb, team: WorkspaceDb, caplog: pytest.LogCaptureFixture, status: int
) -> None:
    ctx = _owner(db)
    _set_base(db, "PLN")
    _goldapi_metal(db, ctx, "XAG")
    _enable_goldapi(db, ctx)
    runtime = _runtime(
        db,
        FakeProviderHttpClient(
            lambda _url: HttpResponse(status, b'{"error": "' + GOLDAPI_SECRET.encode() + b'"}', {})
        ),
    )

    with caplog.at_level(logging.DEBUG):
        stats = refresh_now(db.factory, ctx, "goldapi", runtime)
        result = lookup_provider(
            db.factory,
            ctx,
            "goldapi",
            runtime,
            type_value="metal",
            quote_currency="PLN",
            unit_label="oz",
            provider_symbol="XAG",
        )
        refresh_all(db.factory, "goldapi", runtime, logging.getLogger("test.goldapi"))

    assert stats.errors >= 1
    assert result.status in {"error", "rate_limited"}
    assert GOLDAPI_SECRET not in caplog.text
    with db.factory() as session:
        errors = list(session.scalars(select(ProviderError)))
        audits = list(session.scalars(select(AuditEvent)))
        config = PriceSourceConfigRepository(session, ctx).for_provider("goldapi")
    assert errors
    for error in errors:
        stored = {column.name: getattr(error, column.name) for column in ProviderError.__table__.c}
        assert GOLDAPI_SECRET not in repr(stored)
    assert audits
    for audit in audits:
        assert GOLDAPI_SECRET not in json.dumps(audit.data, default=str)
    assert config is not None and config.settings_encrypted is not None
    assert GOLDAPI_SECRET not in config.settings_encrypted
    assert config.last_error_code is None or GOLDAPI_SECRET not in config.last_error_code


def test_coingecko_utc_timestamp_is_capped_at_the_workspace_day(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    _set_timezone(db, "America/New_York")
    clock = FixedClock("2026-03-10T00:10:00+00:00")
    db.clock = clock
    db.factory.kw["info"]["clock"] = clock
    instrument = _instrument(
        db,
        ctx,
        name="Bitcoin",
        type_value="crypto",
        quote_currency="USD",
        unit_label="coin",
    )
    _tx(db, ctx, instrument.id, fx="1")
    _configure_provider(db, ctx, instrument.id, "coingecko", "bitcoin")
    save_provider_config(db.factory, ctx, SETTINGS, "coingecko", enabled=True)
    url = (
        "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin"
        "&vs_currencies=usd&include_last_updated_at=true"
    )
    runtime = _runtime(
        db,
        FakeProviderHttpClient({url: _body("coingecko_simple_ny.json")}),
    )

    stats = refresh_provider(db.factory, ctx, "coingecko", runtime, mode="latest")

    assert stats.status == "ok"
    assert [(point.date, point.price) for point in _prices(db)] == [
        (date(2026, 3, 9), Decimal("12345.67"))
    ]
    assert _errors(db) == []


def test_coingecko_history_limit_is_recorded_and_backfill_is_marked(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    instrument = _instrument(
        db, ctx, name="Bitcoin", type_value="crypto", quote_currency="USD", unit_label="coin"
    )
    _tx(db, ctx, instrument.id, date_value="2026-01-01", fx="0.90")
    _configure_provider(db, ctx, instrument.id, "coingecko", "bitcoin")
    save_provider_config(db.factory, ctx, SETTINGS, "coingecko", enabled=True)

    def respond(url: str):
        if "simple/price" in url:
            return _body("coingecko_simple.json")
        if "market_chart" in url:
            return _body("coingecko_chart.json")
        raise AssertionError(url)

    runtime = _runtime(db, FakeProviderHttpClient(respond))
    stats = refresh_provider(db.factory, ctx, "coingecko", runtime, mode="full")

    assert stats.status == "ok"
    points = {point.date: point.price for point in _prices(db)}
    assert points == {
        date(2026, 2, 27): Decimal("100.25"),
        date(2026, 3, 2): Decimal("12345.67"),
    }
    assert [error.code for error in _errors(db)] == ["history_limited"]
    with db.factory() as session:
        row = InstrumentRepository(session, ctx).get_or_raise(instrument.id)
        assert row.provider_backfilled_at is not None
    status = provider_status(db.factory, ctx, instrument.id)
    assert status is not None and status.history_limited and not status.error


def test_a_unique_clash_is_retried_as_an_update(
    db: WorkspaceDb, team: WorkspaceDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _owner(db)
    instrument = _instrument(
        db, ctx, name="Gold", type_value="metal", quote_currency="PLN", unit_label="oz"
    )
    with db.factory.begin() as session:
        PricePointRepository(session, ctx).create(
            instrument_id=instrument.id,
            date=date(2026, 3, 2),
            price=Decimal("100"),
            source="provider",
            provider_key="nbp",
        )
    original = PricePointRepository.provider_on
    calls = {"count": 0}

    def flaky(self, instrument_id: UUID, day: date):  # type: ignore[no-untyped-def]
        calls["count"] += 1
        if calls["count"] == 1:
            return None
        return original(self, instrument_id, day)

    monkeypatch.setattr(PricePointRepository, "provider_on", flaky)
    _upsert_prices(
        db.factory,
        ctx,
        [(instrument.id, date(2026, 3, 2), Decimal("200"), "nbp")],
    )
    assert _prices(db)[0].price == Decimal("200")


def test_provider_status_tracks_errors_after_the_last_success(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    instrument = _instrument(
        db, ctx, name="Gold", type_value="metal", quote_currency="PLN", unit_label="oz"
    )
    _configure_provider(db, ctx, instrument.id)
    with db.factory.begin() as session:
        PricePointRepository(session, ctx).create(
            instrument_id=instrument.id,
            date=date(2026, 3, 1),
            price=Decimal("100"),
            source="provider",
            provider_key="nbp",
        )
        ProviderErrorRepository(session, ctx).create(
            provider_key="nbp",
            instrument_id=instrument.id,
            scope="latest",
            code="http_500",
            occurred_at=datetime(2026, 3, 10, 13, tzinfo=UTC),
            attempts=1,
        )
    status = provider_status(db.factory, ctx, instrument.id)
    assert status is not None
    assert status.error and not status.rate_limited
    assert status.last_price_date == date(2026, 3, 1)
    assert status.last_error_code == "http_500"

    with db.factory.begin() as session:
        ProviderErrorRepository(session, ctx).create(
            provider_key="nbp",
            instrument_id=instrument.id,
            scope="latest",
            code="history_limited",
            occurred_at=datetime(2026, 3, 10, 13, 30, tzinfo=UTC),
            attempts=1,
        )
    still_real = provider_status(db.factory, ctx, instrument.id)
    assert still_real is not None and still_real.error

    with db.factory.begin() as session:
        ProviderErrorRepository(session, ctx).create(
            provider_key="nbp",
            instrument_id=instrument.id,
            scope="latest",
            code="rate_limited",
            occurred_at=datetime(2026, 3, 10, 14, tzinfo=UTC),
            attempts=1,
        )
    limited = provider_status(db.factory, ctx, instrument.id)
    assert limited is not None
    assert limited.rate_limited and not limited.error


def test_a_success_after_an_error_clears_the_badge(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    instrument = _instrument(
        db, ctx, name="Gold", type_value="metal", quote_currency="PLN", unit_label="oz"
    )
    _configure_provider(db, ctx, instrument.id)
    with db.factory.begin() as session:
        PricePointRepository(session, ctx).create(
            instrument_id=instrument.id,
            date=date(2026, 3, 1),
            price=Decimal("100"),
            source="provider",
            provider_key="nbp",
        )
    with db.factory.begin() as session:
        ProviderErrorRepository(session, ctx).create(
            provider_key="nbp",
            instrument_id=instrument.id,
            scope="latest",
            code="http_500",
            occurred_at=db.clock.now(),
            attempts=1,
        )
    _upsert_prices(
        db.factory,
        ctx,
        [(instrument.id, date(2026, 3, 1), Decimal("101"), "nbp")],
    )
    status = provider_status(db.factory, ctx, instrument.id)
    assert status is not None
    assert not status.error and not status.rate_limited


def test_errors_older_than_ninety_days_are_pruned(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    instrument = _instrument(
        db, ctx, name="Gold", type_value="metal", quote_currency="PLN", unit_label="oz"
    )
    with db.factory.begin() as session:
        ProviderErrorRepository(session, ctx).create(
            provider_key="nbp",
            instrument_id=instrument.id,
            scope="latest",
            code="http_500",
            occurred_at=datetime(2025, 12, 1, tzinfo=UTC),
            attempts=1,
        )
    _prune_errors(db.factory, ctx, datetime(2026, 3, 10, tzinfo=UTC))
    assert _errors(db) == []
