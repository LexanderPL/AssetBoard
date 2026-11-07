"""Price points and FX rates of the current workspace (F6.3)."""

from __future__ import annotations

from collections.abc import Collection
from datetime import date
from decimal import Decimal
from uuid import UUID

from assetboard.db.models.prices import FxRate, PricePoint
from assetboard.db.repositories.base import ScopedRepository


class PricePointRepository(ScopedRepository[PricePoint]):
    model = PricePoint

    def create(
        self,
        *,
        instrument_id: UUID,
        date: date,
        price: Decimal,
        source: str,
    ) -> PricePoint:
        return self.add(
            PricePoint(
                instrument_id=instrument_id,
                date=date,
                price=price,
                source=source,
            )
        )

    def manual_on(self, instrument_id: UUID, date: date) -> PricePoint | None:
        statement = self._scoped_select().where(
            PricePoint.instrument_id == instrument_id,
            PricePoint.date == date,
            PricePoint.source == "manual",
        )
        return self._session.scalars(statement).one_or_none()

    def for_instruments_on_or_before(
        self, instrument_ids: Collection[UUID], on: date
    ) -> list[PricePoint]:
        if not instrument_ids:
            return []
        statement = self._scoped_select().where(
            PricePoint.instrument_id.in_(instrument_ids),
            PricePoint.date <= on,
        )
        return list(self._session.scalars(statement))


class FxRateRepository(ScopedRepository[FxRate]):
    model = FxRate

    def create(
        self,
        *,
        base_currency: str,
        quote_currency: str,
        date: date,
        rate: Decimal,
        source: str,
    ) -> FxRate:
        if not rate.is_finite() or rate <= 0:
            raise ValueError("FX rate must be a positive finite value")
        return self.add(
            FxRate(
                base_currency=base_currency,
                quote_currency=quote_currency,
                date=date,
                rate=rate,
                source=source,
            )
        )

    def for_base_on_or_before(self, base_currency: str, on: date) -> list[FxRate]:
        statement = self._scoped_select().where(
            FxRate.base_currency == base_currency,
            FxRate.date <= on,
        )
        return list(self._session.scalars(statement))
