"""Home widget preferences, always through the workspace scope (F9.1)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from assetboard.db.models.dashboard import DashboardPreference
from assetboard.db.repositories.base import ScopedRepository


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
    ) -> DashboardPreference:
        row = self.for_user(user_id)
        if row is None:
            return self.add(
                DashboardPreference(
                    user_id=user_id, widgets=list(widgets), customised_at=customised_at
                )
            )
        row.widgets = list(widgets)
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
