from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import Session

from assetboard.db.models.identity import Workspace
from assetboard.db.models.ledger import Category
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

    def get_for_update(self, occurrence_id: UUID) -> ScheduledOccurrence | None:
        statement = (
            self._scoped_select().where(ScheduledOccurrence.id == occurrence_id).with_for_update()
        )
        return self._session.scalars(statement).one_or_none()

    def linked_to_transaction_for_update(
        self, transaction_id: UUID, *, previous: bool = False
    ) -> list[ScheduledOccurrence]:
        """Occurrences posted as ``transaction_id`` (or, with ``previous``, last posted as it)."""
        column = (
            ScheduledOccurrence.previous_transaction_id
            if previous
            else ScheduledOccurrence.transaction_id
        )
        statement = (
            self._scoped_select()
            .where(column == transaction_id)
            .order_by(ScheduledOccurrence.id)
            .with_for_update()
        )
        return list(self._session.scalars(statement))

    def upcoming(
        self,
        through: date,
        *,
        direction: str | None = None,
        category_id: UUID | None = None,
    ) -> list[tuple[ScheduledOccurrence, ScheduledItem, str]]:
        statement = (
            select(ScheduledOccurrence, ScheduledItem, Category.name)
            .join(
                ScheduledItem,
                and_(
                    ScheduledItem.workspace_id == self._workspace_id,
                    ScheduledItem.id == ScheduledOccurrence.scheduled_item_id,
                ),
            )
            .join(
                Category,
                and_(
                    Category.workspace_id == self._workspace_id,
                    Category.id == ScheduledOccurrence.category_id,
                ),
            )
            .where(
                ScheduledOccurrence.workspace_id == self._workspace_id,
                ScheduledOccurrence.status == "pending",
                ScheduledOccurrence.due_date <= through,
            )
        )
        if direction is not None:
            statement = statement.where(ScheduledOccurrence.direction == direction)
        if category_id is not None:
            statement = statement.where(ScheduledOccurrence.category_id == category_id)
        return list(
            self._session.execute(
                statement.order_by(ScheduledOccurrence.due_date, ScheduledOccurrence.id)
            ).all()
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
