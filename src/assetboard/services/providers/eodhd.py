"""EODHD end-of-day equities and ETFs adapter (F6.4)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import cast
from urllib.parse import quote, urlencode

from assetboard.domain.prices import ProviderInstrument, ProviderQuote, ProviderRateLimit
from assetboard.domain.providers import (
    EODHD,
    EODHD_KEY,
    InvalidProviderRate,
    InvalidProviderSymbol,
    clean_provider_symbol,
    normalize_provider_decimal,
)
from assetboard.services.providers.transport import ProviderPayloadError, ProviderRuntime

EOD_URL = "https://eodhd.com/api/eod/{symbol}"
LATEST_TTL = timedelta(minutes=15)
HISTORY_TTL = timedelta(hours=24)


class EodhdProvider:
    key = EODHD_KEY
    rate_limit = ProviderRateLimit(1, 1)

    def __init__(self, runtime: ProviderRuntime) -> None:
        self.runtime = runtime

    def supports(self, instrument: ProviderInstrument) -> bool:
        if instrument.type not in {"equity", "etf"}:
            return False
        try:
            return clean_provider_symbol(EODHD, instrument.provider_symbol) is not None
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
        del quote_currency
        token = _token(settings)
        quotes: dict[str, ProviderQuote] = {}
        for symbol in symbols:
            clean = _symbol(symbol)
            query = urlencode({"api_token": token, "fmt": "json", "order": "d", "limit": 1})
            body = self.runtime.fetch(
                self.key,
                f"{EOD_URL.format(symbol=quote(clean, safe=''))}?{query}",
                headers={"Accept": "application/json"},
                ttl=LATEST_TTL,
                wait=wait,
                max_wait=300.0,
            )
            rows = _rows(body)
            if not rows:
                continue
            day, price = rows[-1]
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
        del quote_currency
        clean = _symbol(symbol)
        token = _token(settings)
        query = urlencode(
            {
                "api_token": token,
                "fmt": "json",
                "order": "a",
                "from": start.isoformat(),
                "to": end.isoformat(),
            }
        )
        body = self.runtime.fetch(
            self.key,
            f"{EOD_URL.format(symbol=quote(clean, safe=''))}?{query}",
            headers={"Accept": "application/json"},
            ttl=HISTORY_TTL,
            wait=wait,
            max_wait=300.0,
        )
        return tuple(
            ProviderQuote(symbol, day, price) for day, price in _rows(body) if start <= day <= end
        )


def _token(settings: Mapping[str, str] | None) -> str:
    token = (settings or {}).get("api_key", "").strip()
    if not token:
        raise ProviderPayloadError("missing_api_key")
    return token


def _symbol(value: str) -> str:
    try:
        cleaned = clean_provider_symbol(EODHD, value)
    except InvalidProviderSymbol:
        raise ProviderPayloadError("unsupported_symbol") from None
    if cleaned is None:
        raise ProviderPayloadError("unsupported_symbol")
    return cleaned


def _rows(body: bytes) -> list[tuple[date, Decimal]]:
    try:
        parsed = json.loads(body, parse_float=Decimal)
    except (ValueError, UnicodeDecodeError):
        raise ProviderPayloadError() from None
    if not isinstance(parsed, list):
        raise ProviderPayloadError()
    rows: list[tuple[date, Decimal]] = []
    for item in cast(list[object], parsed):
        if not isinstance(item, dict):
            raise ProviderPayloadError()
        row = cast(dict[str, object], item)
        raw_date = row.get("date")
        if not isinstance(raw_date, str):
            raise ProviderPayloadError()
        try:
            day = date.fromisoformat(raw_date)
        except ValueError:
            raise ProviderPayloadError() from None
        raw_price = row.get("adjusted_close")
        if raw_price is None:
            raw_price = row.get("close")
        if not isinstance(raw_price, (int, Decimal)):
            raise ProviderPayloadError()
        try:
            price = normalize_provider_decimal(Decimal(raw_price), positive=True)
        except InvalidProviderRate:
            raise ProviderPayloadError() from None
        rows.append((day, price))
    return sorted(rows, key=lambda item: item[0])
