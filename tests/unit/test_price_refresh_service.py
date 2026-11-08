"""Provider refresh, FX conversion, backfill and status (F6.4)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.db.models.identity import Workspace
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
    provider_status,
    refresh_all,
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
            code="rate_limited",
            occurred_at=datetime(2026, 3, 10, 14, tzinfo=UTC),
            attempts=1,
        )
    limited = provider_status(db.factory, ctx, instrument.id)
    assert limited is not None
    assert limited.rate_limited and not limited.error


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
