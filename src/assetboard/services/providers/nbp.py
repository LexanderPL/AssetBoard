"""NBP Web API adapter (F6.4): PLN FX tables and the official PLN gold price."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import cast

from assetboard.domain.prices import (
    ProviderInstrument,
    ProviderQuote,
    ProviderRateLimit,
    ProviderRateQuote,
)
from assetboard.domain.providers import NBP_KEY, nbp_gold_symbol, normalize_provider_decimal
from assetboard.services.providers.transport import (
    ProviderPayloadError,
    ProviderRuntime,
    ProviderTransportError,
)

TABLE_A_LATEST = "https://api.nbp.pl/api/exchangerates/tables/a/?format=json"
TABLE_B_LATEST = "https://api.nbp.pl/api/exchangerates/tables/b/?format=json"
TABLE_A_RANGE = "https://api.nbp.pl/api/exchangerates/tables/a/{start}/{end}/?format=json"
TABLE_B_RANGE = "https://api.nbp.pl/api/exchangerates/tables/b/{start}/{end}/?format=json"
GOLD_LATEST = "https://api.nbp.pl/api/cenyzlota/?format=json"
GOLD_RANGE = "https://api.nbp.pl/api/cenyzlota/{start}/{end}/?format=json"

LATEST_TTL = timedelta(hours=6)
HISTORY_TTL = timedelta(hours=24)
MAX_RANGE_DAYS = 93


class NbpProvider:
    key = NBP_KEY
    natural_base = "PLN"
    rate_limit = ProviderRateLimit(1, 1)

    def __init__(self, runtime: ProviderRuntime) -> None:
        self.runtime = runtime

    def supports(self, instrument: ProviderInstrument) -> bool:
        return (
            instrument.type == "metal"
            and instrument.quote_currency.upper() == "PLN"
            and nbp_gold_symbol(instrument.provider_symbol) == "XAU"
        )

    def fetch_latest(
        self,
        symbols: Sequence[str],
        quote_currency: str = "",
        *,
        wait: bool = True,
    ) -> Mapping[str, ProviderQuote]:
        if not symbols:
            return {}
        if quote_currency and quote_currency.upper() != "PLN":
            raise ProviderPayloadError("unsupported_quote_currency")
        if any(nbp_gold_symbol(symbol) != "XAU" for symbol in symbols):
            raise ProviderPayloadError("unsupported_symbol")
        try:
            body = self.runtime.fetch(
                self.key, GOLD_LATEST, ttl=LATEST_TTL, wait=wait, max_wait=300.0
            )
        except ProviderTransportError as error:
            if error.code == "http_404":
                return {}
            raise
        rows = _gold_rows(body)
        if not rows:
            return {}
        day, price = rows[-1]
        return {"XAU": ProviderQuote("XAU", day, price)}

    def fetch_history(
        self,
        symbol: str,
        start: date,
        end: date,
        quote_currency: str = "",
        *,
        wait: bool = True,
    ) -> Sequence[ProviderQuote]:
        if nbp_gold_symbol(symbol) != "XAU":
            raise ProviderPayloadError("unsupported_symbol")
        if quote_currency and quote_currency.upper() != "PLN":
            raise ProviderPayloadError("unsupported_quote_currency")
        quotes: list[ProviderQuote] = []
        for first, last in _chunks(start, end):
            try:
                body = self.runtime.fetch(
                    self.key,
                    GOLD_RANGE.format(start=first.isoformat(), end=last.isoformat()),
                    ttl=HISTORY_TTL,
                    wait=wait,
                    max_wait=300.0,
                )
            except ProviderTransportError as error:
                if error.code == "http_404":
                    continue
                raise
            quotes.extend(
                ProviderQuote("XAU", day, price)
                for day, price in _gold_rows(body)
                if start <= day <= end
            )
        return tuple(sorted(quotes, key=lambda quote: (quote.date, quote.symbol)))

    def fetch_latest_fx(
        self, currencies: Sequence[str], *, wait: bool = True
    ) -> Mapping[str, ProviderRateQuote]:
        wanted = {currency.upper() for currency in currencies if currency.upper() != "PLN"}
        if not wanted:
            return {}
        rates = self._latest_table(TABLE_A_LATEST, wait=wait)
        missing = wanted - set(rates)
        if missing:
            rates.update(self._latest_table(TABLE_B_LATEST, wait=wait, only=missing))
        return {currency: rates[currency] for currency in wanted if currency in rates}

    def fetch_history_fx(
        self, currency: str, start: date, end: date, *, wait: bool = True
    ) -> Sequence[ProviderRateQuote]:
        code = currency.upper()
        if code == "PLN":
            return ()
        quotes: list[ProviderRateQuote] = []
        for first, last in _chunks(start, end):
            rows = self._range_table(TABLE_A_RANGE, code, first, last, wait=wait)
            if not rows:
                rows = self._range_table(TABLE_B_RANGE, code, first, last, wait=wait)
            quotes.extend(rows)
        return tuple(sorted(quotes, key=lambda quote: (quote.date, quote.currency)))

    def _latest_table(
        self, url: str, *, wait: bool, only: set[str] | None = None
    ) -> dict[str, ProviderRateQuote]:
        try:
            body = self.runtime.fetch(self.key, url, ttl=LATEST_TTL, wait=wait, max_wait=300.0)
        except ProviderTransportError as error:
            if error.code == "http_404":
                return {}
            raise
        return _latest_table_quotes(body, only=only)

    def _range_table(
        self, url: str, code: str, start: date, end: date, *, wait: bool
    ) -> list[ProviderRateQuote]:
        try:
            body = self.runtime.fetch(
                self.key,
                url.format(start=start.isoformat(), end=end.isoformat()),
                ttl=HISTORY_TTL,
                wait=wait,
                max_wait=300.0,
            )
        except ProviderTransportError as error:
            if error.code == "http_404":
                return []
            raise
        return _range_table_quotes(body, code, start, end)


def _chunks(start: date, end: date) -> list[tuple[date, date]]:
    if end < start:
        return []
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        last = min(cursor + timedelta(days=MAX_RANGE_DAYS - 1), end)
        chunks.append((cursor, last))
        cursor = last + timedelta(days=1)
    return chunks


def _load_json(body: bytes) -> object:
    try:
        return json.loads(body, parse_float=Decimal)
    except (ValueError, UnicodeDecodeError):
        raise ProviderPayloadError() from None


def _decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return normalize_provider_decimal(value, positive=True)
    if isinstance(value, int):
        return normalize_provider_decimal(Decimal(value), positive=True)
    raise ProviderPayloadError()


def _gold_rows(body: bytes) -> list[tuple[date, Decimal]]:
    parsed = _load_json(body)
    if not isinstance(parsed, list):
        raise ProviderPayloadError()
    rows: list[tuple[date, Decimal]] = []
    for item in cast(list[object], parsed):
        if not isinstance(item, dict):
            raise ProviderPayloadError()
        row = cast(dict[str, object], item)
        day = _date(row.get("data"))
        price = _decimal(row.get("cena"))
        rows.append((day, price))
    return sorted(rows, key=lambda row: row[0])


def _date(value: object) -> date:
    if not isinstance(value, str):
        raise ProviderPayloadError()
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ProviderPayloadError() from None


def _latest_table_quotes(body: bytes, *, only: set[str] | None) -> dict[str, ProviderRateQuote]:
    parsed = _load_json(body)
    if not isinstance(parsed, list) or not parsed:
        raise ProviderPayloadError("no_data")
    latest: dict[str, ProviderRateQuote] = {}
    for item in cast(list[object], parsed):
        if not isinstance(item, dict):
            raise ProviderPayloadError()
        table = cast(dict[str, object], item)
        day = _date(table.get("effectiveDate"))
        rates = table.get("rates")
        if not isinstance(rates, list):
            raise ProviderPayloadError()
        for rate_item in cast(list[object], rates):
            if not isinstance(rate_item, dict):
                raise ProviderPayloadError()
            rate = cast(dict[str, object], rate_item)
            code = rate.get("code")
            if not isinstance(code, str):
                raise ProviderPayloadError()
            normalized = code.upper()
            if only is not None and normalized not in only:
                continue
            value = _decimal(rate.get("mid"))
            previous = latest.get(normalized)
            if previous is None or day > previous.date:
                latest[normalized] = ProviderRateQuote(normalized, day, value)
    return latest


def _range_table_quotes(body: bytes, code: str, start: date, end: date) -> list[ProviderRateQuote]:
    parsed = _load_json(body)
    if not isinstance(parsed, list):
        raise ProviderPayloadError()
    quotes: list[ProviderRateQuote] = []
    for item in cast(list[object], parsed):
        if not isinstance(item, dict):
            raise ProviderPayloadError()
        table = cast(dict[str, object], item)
        day = _date(table.get("effectiveDate"))
        if not start <= day <= end:
            continue
        rates = table.get("rates")
        if not isinstance(rates, list):
            raise ProviderPayloadError()
        for rate_item in cast(list[object], rates):
            if not isinstance(rate_item, dict):
                raise ProviderPayloadError()
            rate = cast(dict[str, object], rate_item)
            rate_code = rate.get("code")
            if not isinstance(rate_code, str):
                raise ProviderPayloadError()
            if rate_code.upper() != code:
                continue
            quotes.append(ProviderRateQuote(code, day, _decimal(rate.get("mid"))))
    return quotes
