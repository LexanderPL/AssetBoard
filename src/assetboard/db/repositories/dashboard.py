"""Home widget preferences, always through the workspace scope (F9.1)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from assetboard.db.models.dashboard import DashboardPreference
from assetboard.db.repositories.base import ScopedRepository
from assetboard.domain.dashboard import (
    DEFAULT_NET_WORTH_RANGE,
    DEFAULT_PORTFOLIO_RANGE,
    DEFAULT_UPCOMING_LIMIT,
)


class DashboardPreferenceRepository(ScopedRepository[DashboardPreference]):
    """One preference row per member in the current workspace."""

    model = DashboardPreference

    def for_user(self, user_id: UUID) -> DashboardPreference | None:
        statement = self._scoped_select().where(DashboardPreference.user_id == user_id)
        return self._session.scalars(statement).one_or_none()

    def upsert(
        self,
        *,
        user_id: UUID,
        widgets: list[str],
        customised_at: datetime,
        upcoming_limit: int = DEFAULT_UPCOMING_LIMIT,
        portfolio_range: str = DEFAULT_PORTFOLIO_RANGE,
        net_worth_range: str = DEFAULT_NET_WORTH_RANGE,
    ) -> DashboardPreference:
        row = self.for_user(user_id)
        if row is None:
            return self.add(
                DashboardPreference(
                    user_id=user_id,
                    widgets=list(widgets),
                    upcoming_limit=upcoming_limit,
                    portfolio_range=portfolio_range,
                    net_worth_range=net_worth_range,
                    customised_at=customised_at,
                )
            )
        row.widgets = list(widgets)
        row.upcoming_limit = upcoming_limit
        row.portfolio_range = portfolio_range
        row.net_worth_range = net_worth_range
        row.customised_at = customised_at
        self._session.flush()
        return row

    def delete_for_user(self, user_id: UUID) -> int:
        """Remove a member's preference (Reset, or when they leave the workspace)."""
        row = self.for_user(user_id)
        if row is None:
            return 0
        self.delete(row)
        return 1
