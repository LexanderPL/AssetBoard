"""Categories of the current workspace (F2.1)."""

from __future__ import annotations

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

    def for_import_batch(self, batch_id: UUID, *, lock: bool = False) -> list[Category]:
        statement = self._scoped_select().where(Category.import_batch_id == batch_id)
        if lock:
            statement = statement.with_for_update()
        return list(self._session.scalars(statement.order_by(Category.id)))
