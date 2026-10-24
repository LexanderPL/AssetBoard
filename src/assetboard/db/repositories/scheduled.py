from __future__ import annotations

from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from assetboard.db.models.identity import Workspace
from assetboard.db.models.scheduled import ScheduledItem, ScheduledOccurrence
from assetboard.db.repositories.base import ScopedRepository


class ScheduledItemRepository(ScopedRepository[ScheduledItem]):
    model = ScheduledItem

    def create(self, **values: object) -> ScheduledItem:
        return self.add(ScheduledItem(**values))

    def ordered(self, *, active_only: bool = False) -> list[ScheduledItem]:
        statement = self._scoped_select().order_by(ScheduledItem.name, ScheduledItem.id)
        if active_only:
            statement = statement.where(
                ScheduledItem.paused_at.is_(None), ScheduledItem.archived_at.is_(None)
            )
        return list(self._session.scalars(statement))

    def any_exists(self) -> bool:
        """Whether the workspace has any item (occurrences cannot exist without one)."""
        statement = select(exists().where(ScheduledItem.workspace_id == self._workspace_id))
        return bool(self._session.execute(statement).scalar_one())

    def category_counts(self) -> dict[UUID, int]:
        rows = self._session.execute(
            select(ScheduledItem.category_id, func.count())
            .where(ScheduledItem.workspace_id == self._workspace_id)
            .group_by(ScheduledItem.category_id)
        )
        return dict(rows.all())


class ScheduledOccurrenceRepository(ScopedRepository[ScheduledOccurrence]):
    model = ScheduledOccurrence

    def create(self, **values: object) -> ScheduledOccurrence:
        return self.add(ScheduledOccurrence(**values))

    def for_item(self, item_id: UUID) -> list[ScheduledOccurrence]:
        return list(
            self._session.scalars(
                self._scoped_select()
                .where(ScheduledOccurrence.scheduled_item_id == item_id)
                .order_by(ScheduledOccurrence.due_date, ScheduledOccurrence.id)
            )
        )

    def category_counts(self) -> dict[UUID, int]:
        rows = self._session.execute(
            select(ScheduledOccurrence.category_id, func.count())
            .where(ScheduledOccurrence.workspace_id == self._workspace_id)
            .group_by(ScheduledOccurrence.category_id)
        )
        return dict(rows.all())


class RecurringWorkspaceReader:
    def __init__(self, session: Session) -> None:
        self.session = session

    def workspaces(self) -> list[tuple[UUID, str]]:
        return [
            (row.id, row.timezone)
            for row in self.session.execute(select(Workspace.id, Workspace.timezone))
        ]
