"""GoldAPI.io precious-metals adapter (F6.5 follow-up, ADR-0024).

GoldAPI quotes XAU, XAG, XPT and XPD per troy ounce in the instrument's currency. The API key
travels only in the ``x-access-token`` header, never in a URL, so it cannot reach logs, error
codes or the response cache key in clear text (the cache key is a SHA-256 digest).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast
from urllib.parse import urlencode

from assetboard.domain.prices import ProviderInstrument, ProviderQuote, ProviderRateLimit
from assetboard.domain.providers import (
    GOLDAPI,
    GOLDAPI_CURRENCIES,
    GOLDAPI_KEY,
    InvalidProviderRate,
    InvalidProviderSymbol,
    clean_provider_symbol,
    normalize_provider_decimal,
)
from assetboard.services.providers.transport import ProviderPayloadError, ProviderRuntime

PRICE_URL = "https://www.goldapi.io/api/{metal}/{currency}"
HISTORY_URL = "https://www.goldapi.io/api/history/{metal}/{currency}"
LATEST_TTL = timedelta(minutes=15)
HISTORY_TTL = timedelta(hours=24)
MAX_HISTORY_DAYS = 90
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class GoldApiProvider:
    key = GOLDAPI_KEY
    rate_limit = ProviderRateLimit(1, 1)

    def __init__(self, runtime: ProviderRuntime) -> None:
        self.runtime = runtime

    def supports(self, instrument: ProviderInstrument) -> bool:
        if instrument.type != "metal":
            return False
        if instrument.quote_currency.upper() not in GOLDAPI_CURRENCIES:
            return False
        try:
            return clean_provider_symbol(GOLDAPI, instrument.provider_symbol) is not None
        except InvalidProviderSymbol:
            return False

    def fetch_latest(
        self,
        symbols: Sequence[str],
        quote_currency: str = "",
        *,
        wait: bool = True,
        settings: Mapping[str, str] | None = None,
    ) -> Mapping[str, ProviderQuote]:
        token = _token(settings)
        currency = _currency(quote_currency)
        quotes: dict[str, ProviderQuote] = {}
        for symbol in symbols:
            clean = _symbol(symbol)
            body = self.runtime.fetch(
                self.key,
                PRICE_URL.format(metal=clean, currency=currency),
                headers=_headers(token),
                ttl=LATEST_TTL,
                wait=wait,
                max_wait=300.0,
            )
            row = _object(_load_json(body))
            raw_price = row.get("price")
            if raw_price is None:
                raise ProviderPayloadError()
            price = _decimal(raw_price)
            raw_timestamp = row.get("timestamp")
            # GoldAPI's timestamp is Unix seconds (UTC). The service caps the day at the
            # workspace's today, as it does for CoinGecko.
            day = (
                self.runtime.clock.today("UTC")
                if raw_timestamp is None
                else _seconds_date(raw_timestamp)
            )
            quotes[clean] = ProviderQuote(clean, day, price)
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
        clean = _symbol(symbol)
        currency = _currency(quote_currency)
        if end < start:
            return ()
        # The provider accepts at most 90 calendar days per request. This integration backfills
        # only the most recent window (one request per instrument); the service records
        # `history_limited` when the first transaction is older.
        window_start = max(start, end - timedelta(days=MAX_HISTORY_DAYS - 1))
        token = _token(settings)
        query = urlencode({"from": window_start.isoformat(), "to": end.isoformat()})
        body = self.runtime.fetch(
            self.key,
            f"{HISTORY_URL.format(metal=clean, currency=currency)}?{query}",
            headers=_headers(token),
            ttl=HISTORY_TTL,
            wait=wait,
            max_wait=300.0,
        )
        prices = _object(_load_json(body)).get("prices")
        if not isinstance(prices, list):
            raise ProviderPayloadError()
        quotes: dict[date, Decimal] = {}
        for item in cast(list[object], prices):
            row = _object(item)
            raw_date = row.get("date")
            raw_price = row.get("price")
            if not isinstance(raw_date, str) or raw_price is None:
                raise ProviderPayloadError()
            try:
                day = date.fromisoformat(raw_date)
            except ValueError:
                raise ProviderPayloadError() from None
            if window_start <= day <= end:
                quotes[day] = _decimal(raw_price)
        return tuple(ProviderQuote(clean, day, quotes[day]) for day in sorted(quotes))


def _headers(token: str) -> dict[str, str]:
    return {"x-access-token": token, "Accept": "application/json"}


def _token(settings: Mapping[str, str] | None) -> str:
    token = (settings or {}).get("api_key", "").strip()
    if not token:
        raise ProviderPayloadError("missing_api_key")
    return token


def _currency(value: str) -> str:
    currency = value.strip().upper()
    if currency not in GOLDAPI_CURRENCIES:
        raise ProviderPayloadError("unsupported_quote_currency")
    return currency


def _symbol(value: str) -> str:
    try:
        cleaned = clean_provider_symbol(GOLDAPI, value)
    except InvalidProviderSymbol:
        raise ProviderPayloadError("unsupported_symbol") from None
    if cleaned is None:
        raise ProviderPayloadError("unsupported_symbol")
    return cleaned


def _load_json(body: bytes) -> object:
    try:
        return json.loads(body, parse_float=Decimal)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ProviderPayloadError() from None


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProviderPayloadError()
    return cast(dict[str, object], value)


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ProviderPayloadError()
    try:
        return normalize_provider_decimal(Decimal(value), positive=True)
    except InvalidProviderRate:
        raise ProviderPayloadError() from None


def _seconds_date(value: object) -> date:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ProviderPayloadError()
    seconds = Decimal(value)
    if not seconds.is_finite() or seconds < 0:
        raise ProviderPayloadError()
    try:
        return (_EPOCH + timedelta(seconds=int(seconds))).date()
    except OverflowError:
        raise ProviderPayloadError() from None
