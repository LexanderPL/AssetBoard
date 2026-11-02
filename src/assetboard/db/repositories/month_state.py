"""Month states of the current workspace (F2.5)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete

from assetboard.db.models.ledger import MonthState
from assetboard.db.repositories.base import ScopedRepository
from assetboard.domain.months import YearMonth


class MonthStateRepository(ScopedRepository[MonthState]):
    """Rows are never deleted: reopening a month clears its closed fields (generic ``update``).

    Months are passed as ``YearMonth`` and stored as their first day, so no other day can be
    stored.
    """

    model = MonthState
    unsupported = frozenset({"delete"})

    def for_month(self, month: YearMonth) -> MonthState | None:
        statement = self._scoped_select().where(MonthState.month == month.first_day)
        return self._session.scalars(statement).one_or_none()

    def create(self, month: YearMonth) -> MonthState:
        """An open row for ``month`` (the caller then closes it)."""
        return self.add(MonthState(month=month.first_day))

    def create_restored(
        self,
        *,
        month: YearMonth,
        closed_at: datetime | None,
        closed_by_user_id: UUID | None,
        plan_started_at: datetime | None,
        created_at: datetime,
        updated_at: datetime,
    ) -> MonthState:
        """Create a month state exactly as a restore archive stores it."""
        return self.add(
            MonthState(
                month=month.first_day,
                closed_at=closed_at,
                closed_by_user_id=closed_by_user_id,
                plan_started_at=plan_started_at,
                created_at=created_at,
                updated_at=updated_at,
            )
        )

    def delete_for_months(self, months: Iterable[YearMonth]) -> int:
        """Hard-delete month states (restore rollback only; ordinary months are never removed)."""
        first_days = [month.first_day for month in months]
        if not first_days:
            return 0
        result = self._session.execute(
            delete(MonthState).where(
                MonthState.workspace_id == self._workspace_id,
                MonthState.month.in_(first_days),
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)

    def closed_months(self) -> frozenset[YearMonth]:
        """Every month of the workspace that is closed now."""
        statement = self._scoped_select().where(MonthState.closed_at.is_not(None))
        return frozenset(YearMonth.from_date(row.month) for row in self._session.scalars(statement))
