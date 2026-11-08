"""CoinGecko crypto adapter (F6.4)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast
from urllib.parse import quote, urlencode

from assetboard.domain.prices import ProviderInstrument, ProviderQuote, ProviderRateLimit
from assetboard.domain.providers import (
    COINGECKO_KEY,
    InvalidProviderRate,
    normalize_provider_decimal,
)
from assetboard.services.providers.transport import ProviderPayloadError, ProviderRuntime

SIMPLE_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
MARKET_CHART_URL = "https://api.coingecko.com/api/v3/coins/{id}/market_chart"
LATEST_TTL = timedelta(minutes=5)
HISTORY_TTL = timedelta(hours=24)
SUPPORTED_QUOTES = frozenset({"USD", "EUR", "PLN"})
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class CoinGeckoProvider:
    key = COINGECKO_KEY
    rate_limit = ProviderRateLimit(10, 60)

    def __init__(self, runtime: ProviderRuntime) -> None:
        self.runtime = runtime

    def supports(self, instrument: ProviderInstrument) -> bool:
        return (
            instrument.type == "crypto"
            and instrument.quote_currency.upper() in SUPPORTED_QUOTES
            and bool((instrument.provider_symbol or "").strip())
        )

    def fetch_latest(
        self,
        symbols: Sequence[str],
        quote_currency: str = "",
        *,
        wait: bool = True,
        settings: Mapping[str, str] | None = None,
    ) -> Mapping[str, ProviderQuote]:
        ids = sorted({symbol.strip().lower() for symbol in symbols if symbol.strip()})
        if not ids:
            return {}
        vs = quote_currency.upper()
        if vs not in SUPPORTED_QUOTES:
            raise ProviderPayloadError("unsupported_quote_currency")
        query = urlencode(
            {
                "ids": ",".join(ids),
                "vs_currencies": vs.lower(),
                "include_last_updated_at": "true",
            }
        )
        body = self.runtime.fetch(
            self.key,
            f"{SIMPLE_PRICE_URL}?{query}",
            headers=_headers(settings),
            ttl=LATEST_TTL,
            wait=wait,
            max_wait=300.0,
        )
        parsed = _load_json(body)
        if not isinstance(parsed, dict):
            raise ProviderPayloadError()
        data = cast(dict[str, object], parsed)
        quotes: dict[str, ProviderQuote] = {}
        for symbol in ids:
            item = data.get(symbol)
            if not isinstance(item, dict):
                continue
            values = cast(dict[str, object], item)
            raw_price = values.get(vs.lower())
            if raw_price is None:
                continue
            price = _decimal(raw_price)
            raw_timestamp = values.get("last_updated_at")
            day = (
                _seconds_date(raw_timestamp)
                if isinstance(raw_timestamp, (int, Decimal))
                else self.runtime.clock.today("UTC")
            )
            quotes[symbol] = ProviderQuote(symbol, day, price)
        return quotes

    def fetch_history(
        self,
        symbol: str,
        start: date,
        end: date,
        quote_currency: str = "",
        *,
        wait: bool = True,
        settings: Mapping[str, str] | None = None,
    ) -> Sequence[ProviderQuote]:
        vs = quote_currency.upper()
        if vs not in SUPPORTED_QUOTES:
            raise ProviderPayloadError("unsupported_quote_currency")
        coin = symbol.strip().lower()
        if not coin:
            raise ProviderPayloadError("unsupported_symbol")
        query = urlencode({"vs_currency": vs.lower(), "days": "max", "interval": "daily"})
        body = self.runtime.fetch(
            self.key,
            f"{MARKET_CHART_URL.format(id=quote(coin, safe=''))}?{query}",
            headers=_headers(settings),
            ttl=HISTORY_TTL,
            wait=wait,
            max_wait=300.0,
        )
        parsed = _load_json(body)
        if not isinstance(parsed, dict):
            raise ProviderPayloadError()
        prices = cast(dict[str, object], parsed).get("prices")
        if not isinstance(prices, list):
            raise ProviderPayloadError()
        by_date: dict[date, Decimal] = {}
        for point in cast(list[object], prices):
            if not isinstance(point, list):
                raise ProviderPayloadError()
            values = cast(list[object], point)
            if len(values) != 2:
                raise ProviderPayloadError()
            timestamp, raw_price = values
            if not isinstance(timestamp, (int, Decimal)):
                raise ProviderPayloadError()
            day = _milliseconds_date(timestamp)
            if start <= day <= end:
                by_date[day] = _decimal(raw_price)
        return tuple(ProviderQuote(coin, day, by_date[day]) for day in sorted(by_date))


def _headers(settings: Mapping[str, str] | None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    key = (settings or {}).get("api_key", "").strip()
    if key:
        headers["x-cg-demo-api-key"] = key
    return headers


def _load_json(body: bytes) -> object:
    try:
        return json.loads(body, parse_float=Decimal)
    except (ValueError, UnicodeDecodeError):
        raise ProviderPayloadError() from None


def _decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        try:
            return normalize_provider_decimal(value, positive=True)
        except InvalidProviderRate:
            raise ProviderPayloadError() from None
    if isinstance(value, int):
        try:
            return normalize_provider_decimal(Decimal(value), positive=True)
        except InvalidProviderRate:
            raise ProviderPayloadError() from None
    raise ProviderPayloadError()


def _seconds_date(value: object) -> date:
    if not isinstance(value, (int, Decimal)):
        raise ProviderPayloadError()
    return (_EPOCH + timedelta(seconds=int(value))).date()


def _milliseconds_date(value: object) -> date:
    if not isinstance(value, (int, Decimal)):
        raise ProviderPayloadError()
    return (_EPOCH + timedelta(milliseconds=int(value))).date()
