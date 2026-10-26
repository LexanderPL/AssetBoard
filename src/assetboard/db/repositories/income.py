from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import exists, func, select

from assetboard.db.models.income import IncomeSource, IncomeSourceTaxRule, WorkDay, WorkLog
from assetboard.db.repositories.base import ScopedRepository


class IncomeSourceRepository(ScopedRepository[IncomeSource]):
    model = IncomeSource

    def create(self, **values: object) -> IncomeSource:
        return self.add(IncomeSource(**values))

    def ordered(self, *, active_only: bool = False) -> list[IncomeSource]:
        statement = self._scoped_select()
        if active_only:
            statement = statement.where(IncomeSource.archived_at.is_(None))
        return list(self._session.scalars(statement.order_by(IncomeSource.name, IncomeSource.id)))

    def any_exists(self) -> bool:
        return bool(
            self._session.execute(
                select(exists().where(IncomeSource.workspace_id == self._workspace_id))
            ).scalar_one()
        )

    def category_counts(self) -> dict[UUID, int]:
        rows = self._session.execute(
            select(IncomeSource.category_id, func.count())
            .where(IncomeSource.workspace_id == self._workspace_id)
            .group_by(IncomeSource.category_id)
        )
        return dict(rows.all())


class WorkDayRepository(ScopedRepository[WorkDay]):
    model = WorkDay

    def create(self, **values: object) -> WorkDay:
        return self.add(WorkDay(**values))

    def for_month(self, source_id: UUID, first: date, last: date) -> list[WorkDay]:
        """The source's day entries from ``first`` through ``last`` (inclusive), by date."""
        return list(
            self._session.scalars(
                self._scoped_select()
                .where(
                    WorkDay.income_source_id == source_id,
                    WorkDay.work_date >= first,
                    WorkDay.work_date <= last,
                )
                .order_by(WorkDay.work_date)
            )
        )


class WorkLogRepository(ScopedRepository[WorkLog]):
    model = WorkLog

    def create(self, **values: object) -> WorkLog:
        return self.add(WorkLog(**values))

    def for_source(self, source_id: UUID) -> list[WorkLog]:
        return list(
            self._session.scalars(
                self._scoped_select()
                .where(WorkLog.income_source_id == source_id)
                .order_by(WorkLog.work_month, WorkLog.id)
            )
        )

    def work_months_for_sources(self, source_ids: list[UUID]) -> dict[UUID, set[date]]:
        if not source_ids:
            return {}
        rows = self._session.execute(
            select(WorkLog.income_source_id, WorkLog.work_month).where(
                WorkLog.workspace_id == self._workspace_id,
                WorkLog.income_source_id.in_(source_ids),
            )
        )
        result: dict[UUID, set[date]] = {}
        for source_id, work_month in rows:
            result.setdefault(source_id, set()).add(work_month)
        return result


class IncomeSourceTaxRuleRepository(ScopedRepository[IncomeSourceTaxRule]):
    model = IncomeSourceTaxRule

    def create(self, **values: object) -> IncomeSourceTaxRule:
        return self.add(IncomeSourceTaxRule(**values))

    def for_source(self, source_id: UUID) -> list[IncomeSourceTaxRule]:
        statement = self._scoped_select().where(IncomeSourceTaxRule.income_source_id == source_id)
        return list(
            self._session.scalars(
                statement.order_by(IncomeSourceTaxRule.sort_order, IncomeSourceTaxRule.id)
            )
        )

    def for_source_and_rule(self, source_id: UUID, tax_rule_id: UUID) -> IncomeSourceTaxRule | None:
        statement = self._scoped_select().where(
            IncomeSourceTaxRule.income_source_id == source_id,
            IncomeSourceTaxRule.tax_rule_id == tax_rule_id,
        )
        return self._session.scalars(statement).one_or_none()

    def payment_category_counts(self) -> dict[UUID, int]:
        rows = self._session.execute(
            select(IncomeSourceTaxRule.payment_category_id, func.count())
            .where(IncomeSourceTaxRule.workspace_id == self._workspace_id)
            .group_by(IncomeSourceTaxRule.payment_category_id)
        )
        return dict(rows.all())
