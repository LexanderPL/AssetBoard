"""ECB euro reference rates adapter (F6.4)."""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal

from assetboard.domain.prices import ProviderRateLimit, ProviderRateQuote
from assetboard.domain.providers import ECB_KEY, ecb_quote_to_rate, normalize_provider_decimal
from assetboard.services.providers.transport import (
    ProviderPayloadError,
    ProviderRuntime,
    ProviderTransportError,
)

HISTORY_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.xml"
CACHE_TTL = timedelta(hours=1)
_NAMESPACE = "{http://www.ecb.int/vocabulary/2002-08-01/eurofxref}"


class EcbProvider:
    key = ECB_KEY
    natural_base = "EUR"
    rate_limit = ProviderRateLimit(1, 3600)

    def __init__(self, runtime: ProviderRuntime) -> None:
        self.runtime = runtime

    def fetch_latest_fx(
        self, currencies: Sequence[str], *, wait: bool = True
    ) -> Mapping[str, ProviderRateQuote]:
        wanted = {currency.upper() for currency in currencies if currency.upper() != "EUR"}
        if not wanted:
            return {}
        rows = self._rows(wait=wait)
        if not rows:
            return {}
        latest = max(rows)
        return {
            currency: ProviderRateQuote(currency, latest, ecb_quote_to_rate(rows[latest][currency]))
            for currency in wanted
            if currency in rows[latest]
        }

    def fetch_history_fx(
        self, currency: str, start: date, end: date, *, wait: bool = True
    ) -> Sequence[ProviderRateQuote]:
        code = currency.upper()
        if code == "EUR":
            return ()
        rows = self._rows(wait=wait)
        return tuple(
            ProviderRateQuote(code, day, ecb_quote_to_rate(day_rows[code]))
            for day, day_rows in sorted(rows.items())
            if start <= day <= end and code in day_rows
        )

    def _rows(self, *, wait: bool) -> dict[date, dict[str, Decimal]]:
        try:
            body = self.runtime.fetch(
                self.key, HISTORY_URL, ttl=CACHE_TTL, wait=wait, max_wait=300.0
            )
        except ProviderTransportError as error:
            if error.code == "http_404":
                return {}
            raise
        return _parse(body)


def _parse(body: bytes) -> dict[date, dict[str, Decimal]]:
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        raise ProviderPayloadError() from None
    rows: dict[date, dict[str, Decimal]] = {}
    for day_element in root.iter(f"{_NAMESPACE}Cube"):
        day_text = day_element.get("time")
        if day_text is None:
            continue
        try:
            day = date.fromisoformat(day_text)
        except ValueError:
            raise ProviderPayloadError() from None
        day_rows: dict[str, Decimal] = {}
        for rate_element in day_element.findall(f"{_NAMESPACE}Cube"):
            currency = rate_element.get("currency")
            rate = rate_element.get("rate")
            if currency is None or rate is None:
                raise ProviderPayloadError()
            try:
                value = Decimal(rate)
            except ArithmeticError:
                raise ProviderPayloadError() from None
            day_rows[currency.upper()] = normalize_provider_decimal(value, positive=True)
        if day_rows:
            rows[day] = day_rows
    return rows
