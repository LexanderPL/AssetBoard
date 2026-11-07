"""Asset classes, instruments and allocation weights of the current workspace (F6.1)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import case, func, select

from assetboard.db.models.investments import (
    AssetClass,
    Instrument,
    InstrumentAllocation,
    InvestmentTransaction,
)
from assetboard.db.repositories.base import ScopedRepository


class AssetClassRepository(ScopedRepository[AssetClass]):
    model = AssetClass

    def create(
        self,
        *,
        name: str,
        name_key: str,
        investable: bool,
        color: str,
        sort_order: int,
        archived_at: datetime | None = None,
    ) -> AssetClass:
        return self.add(
            AssetClass(
                name=name,
                name_key=name_key,
                investable=investable,
                color=color,
                sort_order=sort_order,
                archived_at=archived_at,
            )
        )

    def ordered(self) -> list[AssetClass]:
        """All classes in display order, archived ones included."""
        statement = self._scoped_select().order_by(
            AssetClass.sort_order, AssetClass.name_key, AssetClass.id
        )
        return list(self._session.scalars(statement))

    def active_by_name_key(self, name_key: str) -> AssetClass | None:
        """The non-archived class with this name key, if any."""
        statement = self._scoped_select().where(
            AssetClass.name_key == name_key,
            AssetClass.archived_at.is_(None),
        )
        return self._session.scalars(statement).one_or_none()

    def active_count(self) -> int:
        statement = (
            select(func.count())
            .select_from(AssetClass)
            .where(
                AssetClass.workspace_id == self._workspace_id,
                AssetClass.archived_at.is_(None),
            )
        )
        return self._session.execute(statement).scalar_one()

    def next_sort_order(self) -> int:
        """One past the last sort order, archived rows included."""
        statement = self._scoped_select()
        orders = [row.sort_order for row in self._session.scalars(statement)]
        return max(orders) + 1 if orders else 0


class InstrumentRepository(ScopedRepository[Instrument]):
    model = Instrument

    def create(
        self,
        *,
        name: str,
        name_key: str,
        symbol: str | None,
        type: str,
        quote_currency: str,
        unit_label: str,
        valuation_source: str,
        provider_key: str | None = None,
        provider_symbol: str | None = None,
        risk_tier_override: str | None = None,
        archived_at: datetime | None = None,
    ) -> Instrument:
        return self.add(
            Instrument(
                name=name,
                name_key=name_key,
                symbol=symbol,
                type=type,
                quote_currency=quote_currency,
                unit_label=unit_label,
                valuation_source=valuation_source,
                provider_key=provider_key,
                provider_symbol=provider_symbol,
                risk_tier_override=risk_tier_override,
                archived_at=archived_at,
            )
        )

    def ordered(self) -> list[Instrument]:
        """All instruments, active first, each group by name."""
        statement = self._scoped_select().order_by(
            Instrument.archived_at.is_not(None),
            Instrument.name_key,
            Instrument.id,
        )
        return list(self._session.scalars(statement))

    def active_by_name_key(self, name_key: str) -> Instrument | None:
        """The non-archived instrument with this name key, if any."""
        statement = self._scoped_select().where(
            Instrument.name_key == name_key,
            Instrument.archived_at.is_(None),
        )
        return self._session.scalars(statement).one_or_none()

    def get_for_update(self, instrument_id: UUID) -> Instrument | None:
        statement = self._scoped_select().where(Instrument.id == instrument_id).with_for_update()
        return self._session.scalars(statement).one_or_none()


class InstrumentAllocationRepository(ScopedRepository[InstrumentAllocation]):
    model = InstrumentAllocation

    def create(
        self,
        *,
        instrument_id: UUID,
        asset_class_id: UUID,
        weight_bp: int,
    ) -> InstrumentAllocation:
        return self.add(
            InstrumentAllocation(
                instrument_id=instrument_id,
                asset_class_id=asset_class_id,
                weight_bp=weight_bp,
            )
        )

    def for_instrument(self, instrument_id: UUID) -> list[InstrumentAllocation]:
        statement = (
            self._scoped_select()
            .where(InstrumentAllocation.instrument_id == instrument_id)
            .order_by(InstrumentAllocation.weight_bp.desc(), InstrumentAllocation.asset_class_id)
        )
        return list(self._session.scalars(statement))


class InvestmentTransactionRepository(ScopedRepository[InvestmentTransaction]):
    model = InvestmentTransaction

    def create(
        self,
        *,
        instrument_id: UUID,
        date: date | None,
        type: str,
        quantity: Decimal,
        unit_price: Decimal,
        quote_currency: str,
        fee_minor: int,
        fx_to_base: Decimal | None,
        note: str = "",
    ) -> InvestmentTransaction:
        return self.add(
            InvestmentTransaction(
                instrument_id=instrument_id,
                date=date,
                type=type,
                quantity=quantity,
                unit_price=unit_price,
                quote_currency=quote_currency,
                fee_minor=fee_minor,
                fx_to_base=fx_to_base,
                note=note,
            )
        )

    def for_instrument(
        self, instrument_id: UUID, *, include_deleted: bool = False
    ) -> list[InvestmentTransaction]:
        """One instrument's rows in holding order (undated first, then date and entry order)."""
        statement = self._scoped_select().where(
            InvestmentTransaction.instrument_id == instrument_id
        )
        if not include_deleted:
            statement = statement.where(InvestmentTransaction.deleted_at.is_(None))
        statement = statement.order_by(
            case((InvestmentTransaction.date.is_(None), 0), else_=1),
            InvestmentTransaction.date,
            InvestmentTransaction.created_at,
            InvestmentTransaction.id,
        )
        return list(self._session.scalars(statement))

    def live(self) -> list[InvestmentTransaction]:
        statement = (
            self._scoped_select()
            .where(InvestmentTransaction.deleted_at.is_(None))
            .order_by(
                case((InvestmentTransaction.date.is_(None), 0), else_=1),
                InvestmentTransaction.date,
                InvestmentTransaction.created_at,
                InvestmentTransaction.id,
            )
        )
        return list(self._session.scalars(statement))

    def any_for_instrument(self, instrument_id: UUID) -> bool:
        statement = (
            select(func.count())
            .select_from(InvestmentTransaction)
            .where(
                InvestmentTransaction.workspace_id == self._workspace_id,
                InvestmentTransaction.instrument_id == instrument_id,
            )
        )
        return bool(self._session.execute(statement).scalar_one())

    def instrument_ids(self) -> set[UUID]:
        statement = self._scoped_select().with_only_columns(InvestmentTransaction.instrument_id)
        return set(self._session.scalars(statement))

    def get_for_update(self, transaction_id: UUID) -> InvestmentTransaction | None:
        statement = (
            self._scoped_select()
            .where(InvestmentTransaction.id == transaction_id)
            .with_for_update()
        )
        return self._session.scalars(statement).one_or_none()

    def filtered(
        self,
        *,
        instrument_id: UUID | None,
        types: tuple[str, ...],
        date_from: date | None,
        date_to: date | None,
        limit: int,
        offset: int,
    ) -> list[InvestmentTransaction]:
        statement = self._filtered(
            instrument_id=instrument_id,
            types=types,
            date_from=date_from,
            date_to=date_to,
        )
        statement = statement.order_by(
            case((InvestmentTransaction.date.is_(None), 1), else_=0),
            InvestmentTransaction.date.desc(),
            InvestmentTransaction.created_at.desc(),
            InvestmentTransaction.id.desc(),
        )
        return list(self._session.scalars(statement.limit(limit).offset(offset)))

    def count_filtered(
        self,
        *,
        instrument_id: UUID | None,
        types: tuple[str, ...],
        date_from: date | None,
        date_to: date | None,
    ) -> int:
        statement = self._filtered(
            instrument_id=instrument_id,
            types=types,
            date_from=date_from,
            date_to=date_to,
        )
        return self._session.execute(
            select(func.count()).select_from(statement.subquery())
        ).scalar_one()

    def _filtered(
        self,
        *,
        instrument_id: UUID | None,
        types: tuple[str, ...],
        date_from: date | None,
        date_to: date | None,
    ):
        statement = self._scoped_select().where(InvestmentTransaction.deleted_at.is_(None))
        if instrument_id is not None:
            statement = statement.where(InvestmentTransaction.instrument_id == instrument_id)
        if types:
            statement = statement.where(InvestmentTransaction.type.in_(types))
        if date_from is not None:
            statement = statement.where(InvestmentTransaction.date >= date_from)
        if date_to is not None:
            statement = statement.where(InvestmentTransaction.date <= date_to)
        return statement
