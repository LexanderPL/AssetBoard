"""Price points and FX rates of the current workspace (F6.3)."""

from __future__ import annotations

from collections.abc import Collection
from datetime import date, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, delete, func, or_, select, tuple_
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
        import_key: str | None = None,
        import_batch_id: UUID | None = None,
        created_at: datetime | None = None,
    ) -> PricePoint:
        row = PricePoint(
            instrument_id=instrument_id,
            date=date,
            price=price,
            source=source,
            provider_key=provider_key,
            import_key=import_key,
            import_batch_id=import_batch_id,
        )
        if created_at is not None:
            row.created_at = created_at
        return self.add(row)

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

    def for_instruments_in_window(
        self, instrument_ids: Collection[UUID], *, start: date, end: date
    ) -> list[PricePoint]:
        """Prices in ``[start, end]`` plus each instrument's latest observation before ``start``.

        The pre-window row keeps the F6.7 carry-forward rule exact while a short report or Home
        range does not read years of prices it never plots.
        """
        ids = list(instrument_ids)
        if not ids or start > end:
            return []
        latest = (
            select(PricePoint.instrument_id, func.max(PricePoint.date).label("date"))
            .where(
                PricePoint.workspace_id == self._workspace_id,
                PricePoint.instrument_id.in_(ids),
                PricePoint.date < start,
            )
            .group_by(PricePoint.instrument_id)
            .subquery()
        )
        statement = self._scoped_select().where(
            PricePoint.instrument_id.in_(ids),
            or_(
                and_(PricePoint.date >= start, PricePoint.date <= end),
                and_(
                    PricePoint.date < start,
                    tuple_(PricePoint.instrument_id, PricePoint.date).in_(
                        select(latest.c.instrument_id, latest.c.date)
                    ),
                ),
            ),
        )
        return list(self._session.scalars(statement))

    def latest_for_instruments_on_or_before(
        self, instrument_ids: Collection[UUID], on: date
    ) -> list[PricePoint]:
        """Only the latest observation date per instrument, including same-day sources.

        The current-value path needs one date per instrument, not its whole price history.
        """
        ids = list(instrument_ids)
        if not ids:
            return []
        latest = (
            select(PricePoint.instrument_id, func.max(PricePoint.date).label("date"))
            .where(
                PricePoint.workspace_id == self._workspace_id,
                PricePoint.instrument_id.in_(ids),
                PricePoint.date <= on,
            )
            .group_by(PricePoint.instrument_id)
            .subquery()
        )
        statement = self._scoped_select().where(
            PricePoint.instrument_id.in_(ids),
            tuple_(PricePoint.instrument_id, PricePoint.date).in_(
                select(latest.c.instrument_id, latest.c.date)
            ),
        )
        return list(self._session.scalars(statement))

    def for_import_batch(self, batch_id: UUID, *, lock: bool = False) -> list[PricePoint]:
        statement = self._scoped_select().where(PricePoint.import_batch_id == batch_id)
        if lock:
            statement = statement.with_for_update()
        return list(self._session.scalars(statement.order_by(PricePoint.id)))


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
        import_key: str | None = None,
        import_batch_id: UUID | None = None,
        created_at: datetime | None = None,
    ) -> FxRate:
        if not rate.is_finite() or rate <= 0:
            raise ValueError("FX rate must be a positive finite value")
        row = FxRate(
            base_currency=base_currency,
            quote_currency=quote_currency,
            date=date,
            rate=rate,
            source=source,
            import_key=import_key,
            import_batch_id=import_batch_id,
        )
        if created_at is not None:
            row.created_at = created_at
        return self.add(row)

    def for_base_on_or_before(self, base_currency: str, on: date) -> list[FxRate]:
        statement = self._scoped_select().where(
            FxRate.base_currency == base_currency,
            FxRate.date <= on,
        )
        return list(self._session.scalars(statement))

    def for_base_in_window(self, base_currency: str, *, start: date, end: date) -> list[FxRate]:
        """Rates in ``[start, end]`` plus each currency's latest rate before ``start``."""
        if start > end:
            return []
        latest = (
            select(FxRate.quote_currency, func.max(FxRate.date).label("date"))
            .where(
                FxRate.workspace_id == self._workspace_id,
                FxRate.base_currency == base_currency,
                FxRate.date < start,
            )
            .group_by(FxRate.quote_currency)
            .subquery()
        )
        statement = self._scoped_select().where(
            FxRate.base_currency == base_currency,
            or_(
                and_(FxRate.date >= start, FxRate.date <= end),
                and_(
                    FxRate.date < start,
                    tuple_(FxRate.quote_currency, FxRate.date).in_(
                        select(latest.c.quote_currency, latest.c.date)
                    ),
                ),
            ),
        )
        return list(self._session.scalars(statement))

    def latest_for_base_on_or_before(self, base_currency: str, on: date) -> list[FxRate]:
        """Only the latest observation date per currency, including same-day sources."""
        latest = (
            select(FxRate.quote_currency, func.max(FxRate.date).label("date"))
            .where(
                FxRate.workspace_id == self._workspace_id,
                FxRate.base_currency == base_currency,
                FxRate.date <= on,
            )
            .group_by(FxRate.quote_currency)
            .subquery()
        )
        statement = self._scoped_select().where(
            FxRate.base_currency == base_currency,
            tuple_(FxRate.quote_currency, FxRate.date).in_(
                select(latest.c.quote_currency, latest.c.date)
            ),
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

    def for_import_batch(self, batch_id: UUID, *, lock: bool = False) -> list[FxRate]:
        statement = self._scoped_select().where(FxRate.import_batch_id == batch_id)
        if lock:
            statement = statement.with_for_update()
        return list(self._session.scalars(statement.order_by(FxRate.id)))


class PriceSourceConfigRepository(ScopedRepository[PriceSourceConfig]):
    model = PriceSourceConfig

    def create(self, **values: object) -> PriceSourceConfig:
        return self.add(PriceSourceConfig(**values))

    def for_provider(
        self, provider_key: str, *, for_update: bool = False
    ) -> PriceSourceConfig | None:
        statement = self._scoped_select().where(PriceSourceConfig.provider_key == provider_key)
        if for_update:
            statement = statement.with_for_update()
        return self._session.scalars(statement).one_or_none()

    def enabled(self) -> list[PriceSourceConfig]:
        statement = self._scoped_select().where(PriceSourceConfig.enabled.is_(True))
        return list(
            self._session.scalars(
                statement.order_by(PriceSourceConfig.provider_key, PriceSourceConfig.id)
            )
        )

    def for_import_batch(self, batch_id: UUID, *, lock: bool = False) -> list[PriceSourceConfig]:
        statement = self._scoped_select().where(PriceSourceConfig.import_batch_id == batch_id)
        if lock:
            statement = statement.with_for_update()
        return list(self._session.scalars(statement.order_by(PriceSourceConfig.provider_key)))


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
