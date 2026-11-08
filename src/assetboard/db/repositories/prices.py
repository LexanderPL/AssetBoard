"""Price points and FX rates of the current workspace (F6.3)."""

from __future__ import annotations

from collections.abc import Collection
from datetime import date, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult

from assetboard.db.models.prices import FxRate, PricePoint, PriceSourceConfig, ProviderError
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
        provider_key: str | None = None,
    ) -> PricePoint:
        return self.add(
            PricePoint(
                instrument_id=instrument_id,
                date=date,
                price=price,
                source=source,
                provider_key=provider_key,
            )
        )

    def manual_on(self, instrument_id: UUID, date: date) -> PricePoint | None:
        statement = self._scoped_select().where(
            PricePoint.instrument_id == instrument_id,
            PricePoint.date == date,
            PricePoint.source == "manual",
        )
        return self._session.scalars(statement).one_or_none()

    def provider_on(self, instrument_id: UUID, date: date) -> PricePoint | None:
        statement = self._scoped_select().where(
            PricePoint.instrument_id == instrument_id,
            PricePoint.date == date,
            PricePoint.source == "provider",
        )
        return self._session.scalars(statement).one_or_none()

    def latest_provider_for_instrument(
        self, instrument_id: UUID, provider_key: str
    ) -> PricePoint | None:
        statement = (
            self._scoped_select()
            .where(
                PricePoint.instrument_id == instrument_id,
                PricePoint.source == "provider",
                PricePoint.provider_key == provider_key,
            )
            .order_by(
                PricePoint.date.desc(),
                PricePoint.created_at.desc(),
                PricePoint.id.desc(),
            )
            .limit(1)
        )
        return self._session.scalars(statement).one_or_none()

    def any_provider_for_instrument(self, instrument_id: UUID, provider_key: str) -> bool:
        statement = (
            select(PricePoint.id)
            .where(
                PricePoint.workspace_id == self._workspace_id,
                PricePoint.instrument_id == instrument_id,
                PricePoint.source == "provider",
                PricePoint.provider_key == provider_key,
            )
            .limit(1)
        )
        return self._session.scalars(statement).first() is not None

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

    def rate_on(
        self, base_currency: str, quote_currency: str, date: date, source: str
    ) -> FxRate | None:
        statement = self._scoped_select().where(
            FxRate.base_currency == base_currency,
            FxRate.quote_currency == quote_currency,
            FxRate.date == date,
            FxRate.source == source,
        )
        return self._session.scalars(statement).one_or_none()

    def for_base_source_on_or_before(
        self, base_currency: str, source: str, on: date
    ) -> list[FxRate]:
        statement = self._scoped_select().where(
            FxRate.base_currency == base_currency,
            FxRate.source == source,
            FxRate.date <= on,
        )
        return list(self._session.scalars(statement))


class PriceSourceConfigRepository(ScopedRepository[PriceSourceConfig]):
    model = PriceSourceConfig

    def create(self, **values: object) -> PriceSourceConfig:
        return self.add(PriceSourceConfig(**values))

    def for_provider(self, provider_key: str) -> PriceSourceConfig | None:
        statement = self._scoped_select().where(PriceSourceConfig.provider_key == provider_key)
        return self._session.scalars(statement).one_or_none()

    def enabled(self) -> list[PriceSourceConfig]:
        statement = self._scoped_select().where(PriceSourceConfig.enabled.is_(True))
        return list(
            self._session.scalars(
                statement.order_by(PriceSourceConfig.provider_key, PriceSourceConfig.id)
            )
        )


class ProviderErrorRepository(ScopedRepository[ProviderError]):
    model = ProviderError
    # The daily refresh prunes old rows; only in-place edits are unsupported.
    unsupported = frozenset({"update"})

    def create(self, **values: object) -> ProviderError:
        return self.add(ProviderError(**values))

    def latest_for_provider(self, provider_key: str) -> ProviderError | None:
        statement = (
            self._scoped_select()
            .where(ProviderError.provider_key == provider_key)
            .order_by(ProviderError.occurred_at.desc(), ProviderError.id.desc())
            .limit(1)
        )
        return self._session.scalars(statement).one_or_none()

    def latest_for_instrument(
        self,
        provider_key: str,
        instrument_id: UUID,
        *,
        exclude: Collection[str] = (),
    ) -> ProviderError | None:
        statement = self._scoped_select().where(
            ProviderError.provider_key == provider_key,
            (ProviderError.instrument_id == instrument_id)
            | (ProviderError.instrument_id.is_(None)),
        )
        if exclude:
            statement = statement.where(ProviderError.code.not_in(exclude))
        statement = statement.order_by(
            ProviderError.occurred_at.desc(), ProviderError.id.desc()
        ).limit(1)
        return self._session.scalars(statement).one_or_none()

    def prune(self, before: datetime) -> int:
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                delete(ProviderError).where(
                    ProviderError.workspace_id == self._workspace_id,
                    ProviderError.occurred_at < before,
                )
            ),
        )
        return int(result.rowcount or 0)
