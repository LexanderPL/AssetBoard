"""Categories of the current workspace (F2.1)."""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select

from assetboard.db.models.ledger import Category
from assetboard.db.repositories.base import ScopedRepository


class CategoryRepository(ScopedRepository[Category]):
    """Generic ``update`` and ``delete`` are supported: a category that nothing references can
    be deleted (the service archives referenced ones instead)."""

    model = Category

    def create(
        self,
        *,
        name: str,
        name_key: str,
        kind: str,
        treatment: str,
        color: str,
        sort_order: int,
        import_batch_id: UUID | None = None,
        import_key: str | None = None,
        archived_at: datetime | None = None,
    ) -> Category:
        return self.add(
            Category(
                name=name,
                name_key=name_key,
                kind=kind,
                treatment=treatment,
                color=color,
                sort_order=sort_order,
                import_batch_id=import_batch_id,
                import_key=import_key,
                archived_at=archived_at,
            )
        )

    def ordered(self, kind: str | None = None) -> list[Category]:
        """Categories (of one kind, or all) in display order; archived ones included."""
        statement = self._scoped_select()
        if kind is not None:
            statement = statement.where(Category.kind == kind)
        statement = statement.order_by(
            Category.kind, Category.sort_order, Category.name_key, Category.id
        )
        return list(self._session.scalars(statement))

    def active_by_name_key(self, *, kind: str, name_key: str) -> Category | None:
        """The non-archived category of ``kind`` with this name key, if any."""
        statement = self._scoped_select().where(
            Category.kind == kind,
            Category.name_key == name_key,
            Category.archived_at.is_(None),
        )
        return self._session.scalars(statement).one_or_none()

    def count(self) -> int:
        """All categories of the workspace, archived ones included."""
        statement = (
            select(func.count())
            .select_from(Category)
            .where(Category.workspace_id == self._workspace_id)
        )
        return self._session.execute(statement).scalar_one()

    def by_lineage_ids(self, row_ids: Collection[UUID]) -> list[Category]:
        """Categories whose id, or ``id:<id>`` import key, is one of ``row_ids`` (F4.3)."""
        ids = list(row_ids)
        found: dict[UUID, Category] = {}
        for start in range(0, len(ids), 400):
            chunk = ids[start : start + 400]
            statement = self._scoped_select().where(
                Category.id.in_(chunk)
                | Category.import_key.in_([f"id:{row_id}" for row_id in chunk])
            )
            for row in self._session.scalars(statement):
                found[row.id] = row
        return list(found.values())

    def next_sort_order(self, kind: str) -> int:
        """One past the last sort order of this kind (archived rows included)."""
        statement = self._scoped_select().where(Category.kind == kind)
        orders = [category.sort_order for category in self._session.scalars(statement)]
        return max(orders) + 1 if orders else 0

    def for_import_batch(self, batch_id: UUID, *, lock: bool = False) -> list[Category]:
        statement = self._scoped_select().where(Category.import_batch_id == batch_id)
        if lock:
            statement = statement.with_for_update()
        return list(self._session.scalars(statement.order_by(Category.id)))
