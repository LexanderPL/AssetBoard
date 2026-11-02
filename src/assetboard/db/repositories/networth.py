"""Net-worth categories, items and append-only valuations (F5.1)."""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from uuid import UUID

from assetboard.db.models.networth import ItemValuation, NetWorthCategory, NetWorthItem
from assetboard.db.repositories.base import ScopedRepository


class NetWorthCategoryRepository(ScopedRepository[NetWorthCategory]):
    model = NetWorthCategory

    def create(
        self,
        *,
        name: str,
        name_key: str,
        side: str,
        color: str,
        sort_order: int,
        archived_at: datetime | None = None,
    ) -> NetWorthCategory:
        return self.add(
            NetWorthCategory(
                name=name,
                name_key=name_key,
                side=side,
                color=color,
                sort_order=sort_order,
                archived_at=archived_at,
            )
        )

    def ordered(self, side: str | None = None) -> list[NetWorthCategory]:
        """Categories (of one side, or both) in display order; archived ones included."""
        statement = self._scoped_select()
        if side is not None:
            statement = statement.where(NetWorthCategory.side == side)
        statement = statement.order_by(
            NetWorthCategory.side,
            NetWorthCategory.sort_order,
            NetWorthCategory.name_key,
            NetWorthCategory.id,
        )
        return list(self._session.scalars(statement))

    def active_by_name_key(self, *, side: str, name_key: str) -> NetWorthCategory | None:
        """The non-archived category of ``side`` with this name key, if any."""
        statement = self._scoped_select().where(
            NetWorthCategory.side == side,
            NetWorthCategory.name_key == name_key,
            NetWorthCategory.archived_at.is_(None),
        )
        return self._session.scalars(statement).one_or_none()

    def count(self, side: str | None = None) -> int:
        """All categories of the workspace (or one side), archived ones included."""
        statement = self._scoped_select()
        if side is not None:
            statement = statement.where(NetWorthCategory.side == side)
        return len(list(self._session.scalars(statement)))

    def next_sort_order(self, side: str) -> int:
        """One past the last sort order of this side (archived rows included)."""
        statement = self._scoped_select().where(NetWorthCategory.side == side)
        orders = [category.sort_order for category in self._session.scalars(statement)]
        return max(orders) + 1 if orders else 0


class NetWorthItemRepository(ScopedRepository[NetWorthItem]):
    model = NetWorthItem

    def create(self, **values: object) -> NetWorthItem:
        return self.add(NetWorthItem(**values))

    def ordered(
        self, side: str | None = None, *, include_archived: bool = True
    ) -> list[NetWorthItem]:
        """Items in creation order (the service sorts names within a category)."""
        statement = self._scoped_select()
        if side is not None:
            statement = statement.where(NetWorthItem.side == side)
        if not include_archived:
            statement = statement.where(NetWorthItem.archived_at.is_(None))
        statement = statement.order_by(
            NetWorthItem.side,
            NetWorthItem.created_at,
            NetWorthItem.id,
        )
        return list(self._session.scalars(statement))

    def counts_by_category(self) -> dict[UUID, int]:
        """How many items (archived ones included) each category has."""
        counts: dict[UUID, int] = {}
        for item in self._session.scalars(self._scoped_select()):
            counts[item.category_id] = counts.get(item.category_id, 0) + 1
        return counts


class ItemValuationRepository(ScopedRepository[ItemValuation]):
    model = ItemValuation
    unsupported = frozenset({"update", "delete"})

    def create(self, **values: object) -> ItemValuation:
        return self.add(ItemValuation(**values))

    def for_item(self, item_id: UUID) -> list[ItemValuation]:
        statement = self._scoped_select().where(ItemValuation.item_id == item_id)
        return list(
            self._session.scalars(
                statement.order_by(
                    ItemValuation.as_of,
                    ItemValuation.created_at,
                    ItemValuation.id,
                )
            )
        )

    def for_items(self, item_ids: Collection[UUID]) -> list[ItemValuation]:
        ids = list(item_ids)
        if not ids:
            return []
        rows: list[ItemValuation] = []
        for start in range(0, len(ids), 400):
            chunk = ids[start : start + 400]
            statement = (
                self._scoped_select()
                .where(ItemValuation.item_id.in_(chunk))
                .order_by(
                    ItemValuation.item_id,
                    ItemValuation.as_of,
                    ItemValuation.created_at,
                    ItemValuation.id,
                )
            )
            rows.extend(self._session.scalars(statement))
        return rows
