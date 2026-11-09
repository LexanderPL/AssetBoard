"""Asset classes, instruments and allocation weights of the current workspace (F6.1)."""

from __future__ import annotations

from collections.abc import Collection
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import case, delete, func, select

from assetboard.db.models.investments import (
    AssetClass,
    BondTerms,
    Instrument,
    InstrumentAllocation,
    InvestmentTransaction,
    RiskTargetPolicy,
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
        risk_tier: str | None = None,
        archived_at: datetime | None = None,
        import_key: str | None = None,
        import_batch_id: UUID | None = None,
        created_at: datetime | None = None,
    ) -> AssetClass:
        row = AssetClass(
            name=name,
            name_key=name_key,
            investable=investable,
            color=color,
            sort_order=sort_order,
            risk_tier=risk_tier,
            archived_at=archived_at,
            import_key=import_key,
            import_batch_id=import_batch_id,
        )
        if created_at is not None:
            row.created_at = created_at
        return self.add(row)

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

    def for_import_batch(self, batch_id: UUID, *, lock: bool = False) -> list[AssetClass]:
        statement = self._scoped_select().where(AssetClass.import_batch_id == batch_id)
        if lock:
            statement = statement.with_for_update()
        return list(self._session.scalars(statement.order_by(AssetClass.id)))


class RiskTargetPolicyRepository(ScopedRepository[RiskTargetPolicy]):
    model = RiskTargetPolicy

    def create(
        self,
        *,
        mode: str,
        safe_bp: int,
        growth_bp: int,
        speculative_bp: int,
        tolerance_bp: int,
        import_batch_id: UUID | None = None,
        created_at: datetime | None = None,
    ) -> RiskTargetPolicy:
        row = RiskTargetPolicy(
            mode=mode,
            safe_bp=safe_bp,
            growth_bp=growth_bp,
            speculative_bp=speculative_bp,
            tolerance_bp=tolerance_bp,
            import_batch_id=import_batch_id,
        )
        if created_at is not None:
            row.created_at = created_at
        return self.add(row)

    def for_workspace(self, *, for_update: bool = False) -> RiskTargetPolicy | None:
        """The current workspace's singleton policy, if it has been saved."""
        statement = self._scoped_select()
        if for_update:
            statement = statement.with_for_update()
        return self._session.scalars(statement).one_or_none()

    def for_import_batch(self, batch_id: UUID, *, lock: bool = False) -> list[RiskTargetPolicy]:
        statement = self._scoped_select().where(RiskTargetPolicy.import_batch_id == batch_id)
        if lock:
            statement = statement.with_for_update()
        return list(self._session.scalars(statement.order_by(RiskTargetPolicy.id)))


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
        provider_backfilled_at: datetime | None = None,
        import_key: str | None = None,
        import_batch_id: UUID | None = None,
        created_at: datetime | None = None,
    ) -> Instrument:
        row = Instrument(
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
            provider_backfilled_at=provider_backfilled_at,
            import_key=import_key,
            import_batch_id=import_batch_id,
        )
        if created_at is not None:
            row.created_at = created_at
        return self.add(row)

    def ordered(self) -> list[Instrument]:
        """All instruments, active first, each group by name."""
        statement = self._scoped_select().order_by(
            Instrument.archived_at.is_not(None),
            Instrument.name_key,
            Instrument.id,
        )
        return list(self._session.scalars(statement))

    def provider_instruments(self, provider_key: str) -> list[Instrument]:
        """Active instruments configured to use one provider, in name order."""
        statement = (
            self._scoped_select()
            .where(
                Instrument.provider_key == provider_key,
                Instrument.archived_at.is_(None),
            )
            .order_by(Instrument.name_key, Instrument.id)
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

    def for_import_batch(self, batch_id: UUID, *, lock: bool = False) -> list[Instrument]:
        statement = self._scoped_select().where(Instrument.import_batch_id == batch_id)
        if lock:
            statement = statement.with_for_update()
        return list(self._session.scalars(statement.order_by(Instrument.id)))


class InstrumentAllocationRepository(ScopedRepository[InstrumentAllocation]):
    model = InstrumentAllocation

    def create(
        self,
        *,
        instrument_id: UUID,
        asset_class_id: UUID,
        weight_bp: int,
        import_key: str | None = None,
        import_batch_id: UUID | None = None,
        created_at: datetime | None = None,
    ) -> InstrumentAllocation:
        row = InstrumentAllocation(
            instrument_id=instrument_id,
            asset_class_id=asset_class_id,
            weight_bp=weight_bp,
            import_key=import_key,
            import_batch_id=import_batch_id,
        )
        if created_at is not None:
            row.created_at = created_at
        return self.add(row)

    def for_instrument(self, instrument_id: UUID) -> list[InstrumentAllocation]:
        statement = (
            self._scoped_select()
            .where(InstrumentAllocation.instrument_id == instrument_id)
            .order_by(InstrumentAllocation.weight_bp.desc(), InstrumentAllocation.asset_class_id)
        )
        return list(self._session.scalars(statement))

    def for_import_batch(self, batch_id: UUID, *, lock: bool = False) -> list[InstrumentAllocation]:
        statement = self._scoped_select().where(InstrumentAllocation.import_batch_id == batch_id)
        if lock:
            statement = statement.with_for_update()
        return list(self._session.scalars(statement.order_by(InstrumentAllocation.id)))


class BondTermsRepository(ScopedRepository[BondTerms]):
    model = BondTerms
    # Terms are edited in place until the instrument is archived; they are removed with the
    # workspace, never individually.
    unsupported = frozenset({"delete"})

    def create(self, **values: object) -> BondTerms:
        return self.add(BondTerms(**values))

    def for_instrument(self, instrument_id: UUID, *, for_update: bool = False) -> BondTerms | None:
        statement = self._scoped_select().where(BondTerms.instrument_id == instrument_id)
        if for_update:
            statement = statement.with_for_update()
        return self._session.scalars(statement).one_or_none()

    def for_instruments(self, instrument_ids: Collection[UUID]) -> list[BondTerms]:
        ids = tuple(dict.fromkeys(instrument_ids))
        if not ids:
            return []
        statement = self._scoped_select().where(BondTerms.instrument_id.in_(ids))
        return list(self._session.scalars(statement))

    def instrument_ids(self) -> set[UUID]:
        statement = self._scoped_select().with_only_columns(BondTerms.instrument_id)
        return set(self._session.scalars(statement))

    def for_import_batch(self, batch_id: UUID, *, lock: bool = False) -> list[BondTerms]:
        statement = self._scoped_select().where(BondTerms.import_batch_id == batch_id)
        if lock:
            statement = statement.with_for_update()
        return list(self._session.scalars(statement.order_by(BondTerms.id)))

    def delete_for_import_batch(self, batch_id: UUID) -> int:
        """Hard-delete a restore batch's terms; the explicit rollback path."""
        result = self._session.execute(
            delete(BondTerms).where(
                BondTerms.workspace_id == self._workspace_id,
                BondTerms.import_batch_id == batch_id,
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)


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
        settlement_currency: str | None = None,
        settlement_unit_price: Decimal | None = None,
        settlement_fee_minor: int | None = None,
        import_batch_id: UUID | None = None,
        import_key: str | None = None,
        income_transaction_id: UUID | None = None,
        created_at: datetime | None = None,
    ) -> InvestmentTransaction:
        row = InvestmentTransaction(
            instrument_id=instrument_id,
            date=date,
            type=type,
            quantity=quantity,
            unit_price=unit_price,
            quote_currency=quote_currency,
            fee_minor=fee_minor,
            fx_to_base=fx_to_base,
            note=note,
            settlement_currency=settlement_currency,
            settlement_unit_price=settlement_unit_price,
            settlement_fee_minor=settlement_fee_minor,
            import_batch_id=import_batch_id,
            import_key=import_key,
            income_transaction_id=income_transaction_id,
        )
        if created_at is not None:
            # Bulk add orders same-day rows by line; the timestamp listener keeps a set value.
            row.created_at = created_at
        return self.add(row)

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

    def live_for_instruments(self, instrument_ids: Collection[UUID]) -> list[InvestmentTransaction]:
        """Live rows for selected instruments, including undated legacy lots."""
        ids = tuple(dict.fromkeys(instrument_ids))
        if not ids:
            return []
        statement = (
            self._scoped_select()
            .where(
                InvestmentTransaction.deleted_at.is_(None),
                InvestmentTransaction.instrument_id.in_(ids),
            )
            .order_by(
                case((InvestmentTransaction.date.is_(None), 0), else_=1),
                InvestmentTransaction.date,
                InvestmentTransaction.created_at,
                InvestmentTransaction.id,
            )
        )
        return list(self._session.scalars(statement))

    def live_dated(
        self,
        on: date | None = None,
        instrument_ids: Collection[UUID] | None = None,
    ) -> list[InvestmentTransaction]:
        """Live dated transactions, optionally up to ``on`` and for selected instruments."""
        statement = self._scoped_select().where(
            InvestmentTransaction.deleted_at.is_(None),
            InvestmentTransaction.date.is_not(None),
        )
        if on is not None:
            statement = statement.where(InvestmentTransaction.date <= on)
        if instrument_ids is not None:
            statement = statement.where(InvestmentTransaction.instrument_id.in_(instrument_ids))
        statement = statement.order_by(
            InvestmentTransaction.date,
            InvestmentTransaction.created_at,
            InvestmentTransaction.id,
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

    def for_income_transaction(
        self, transaction_id: UUID, *, for_update: bool = False
    ) -> InvestmentTransaction | None:
        statement = self._scoped_select().where(
            InvestmentTransaction.income_transaction_id == transaction_id
        )
        if for_update:
            statement = statement.with_for_update()
        return self._session.scalars(statement).one_or_none()

    def income_links(self, transaction_ids: Collection[UUID]) -> dict[UUID, UUID]:
        ids = tuple(dict.fromkeys(transaction_ids))
        if not ids:
            return {}
        statement = self._scoped_select().where(
            InvestmentTransaction.income_transaction_id.in_(ids)
        )
        return {
            row.income_transaction_id: row.id
            for row in self._session.scalars(statement)
            if row.income_transaction_id is not None
        }

    def for_import_batch(
        self, batch_id: UUID, *, lock: bool = False
    ) -> list[InvestmentTransaction]:
        statement = self._scoped_select().where(InvestmentTransaction.import_batch_id == batch_id)
        if lock:
            statement = statement.with_for_update()
        return list(
            self._session.scalars(
                statement.order_by(
                    InvestmentTransaction.instrument_id,
                    InvestmentTransaction.date,
                    InvestmentTransaction.created_at,
                    InvestmentTransaction.id,
                )
            )
        )

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
