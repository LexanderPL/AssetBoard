"""Net-worth categories, items and append-only valuations (F5.1)."""

from __future__ import annotations

from collections.abc import Collection
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import delete, func, select

from assetboard.db.models.networth import (
    ItemValuation,
    NetWorthCategory,
    NetWorthItem,
    NetWorthSnapshot,
)
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
        import_batch_id: UUID | None = None,
        import_key: str | None = None,
    ) -> NetWorthCategory:
        return self.add(
            NetWorthCategory(
                name=name,
                name_key=name_key,
                side=side,
                color=color,
                sort_order=sort_order,
                archived_at=archived_at,
                import_batch_id=import_batch_id,
                import_key=import_key,
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
        statement = (
            select(func.count())
            .select_from(NetWorthCategory)
            .where(NetWorthCategory.workspace_id == self._workspace_id)
        )
        if side is not None:
            statement = statement.where(NetWorthCategory.side == side)
        return self._session.execute(statement).scalar_one()

    def next_sort_order(self, side: str) -> int:
        """One past the last sort order of this side (archived rows included)."""
        statement = self._scoped_select().where(NetWorthCategory.side == side)
        orders = [category.sort_order for category in self._session.scalars(statement)]
        return max(orders) + 1 if orders else 0

    def for_import_batch(self, batch_id: UUID, *, lock: bool = False) -> list[NetWorthCategory]:
        statement = self._scoped_select().where(NetWorthCategory.import_batch_id == batch_id)
        if lock:
            statement = statement.with_for_update()
        return list(self._session.scalars(statement.order_by(NetWorthCategory.id)))


class NetWorthItemRepository(ScopedRepository[NetWorthItem]):
    model = NetWorthItem

    def create(
        self,
        *,
        side: str,
        category_id: UUID,
        name: str,
        currency: str,
        notes: str = "",
        acquired_on: date | None = None,
        valuation_mode: str = "manual",
        link_type: str | None = None,
        link_id: UUID | None = None,
        include_in_net_worth: bool = True,
        investable: bool = False,
        archived_at: datetime | None = None,
        import_batch_id: UUID | None = None,
        import_key: str | None = None,
    ) -> NetWorthItem:
        return self.add(
            NetWorthItem(
                side=side,
                category_id=category_id,
                name=name,
                currency=currency,
                notes=notes,
                acquired_on=acquired_on,
                valuation_mode=valuation_mode,
                link_type=link_type,
                link_id=link_id,
                include_in_net_worth=include_in_net_worth,
                investable=investable,
                archived_at=archived_at,
                import_batch_id=import_batch_id,
                import_key=import_key,
            )
        )

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

    def by_ids(self, item_ids: Collection[UUID]) -> list[NetWorthItem]:
        """Only the requested items, in the same order as ``ordered``."""
        ids = list(item_ids)
        if not ids:
            return []
        statement = (
            self._scoped_select()
            .where(NetWorthItem.id.in_(ids))
            .order_by(NetWorthItem.side, NetWorthItem.created_at, NetWorthItem.id)
        )
        return list(self._session.scalars(statement))

    def counts_by_category(self) -> dict[UUID, int]:
        """How many items (archived ones included) each category has."""
        counts: dict[UUID, int] = {}
        for item in self._session.scalars(self._scoped_select()):
            counts[item.category_id] = counts.get(item.category_id, 0) + 1
        return counts

    def active_linked(self, link_type: str, link_id: UUID) -> NetWorthItem | None:
        """The active item linking to this target, if any."""
        statement = self._scoped_select().where(
            NetWorthItem.valuation_mode == "linked",
            NetWorthItem.link_type == link_type,
            NetWorthItem.link_id == link_id,
            NetWorthItem.archived_at.is_(None),
        )
        return self._session.scalars(statement).one_or_none()

    def for_import_batch(self, batch_id: UUID, *, lock: bool = False) -> list[NetWorthItem]:
        statement = self._scoped_select().where(NetWorthItem.import_batch_id == batch_id)
        if lock:
            statement = statement.with_for_update()
        return list(self._session.scalars(statement.order_by(NetWorthItem.id)))

    def by_lineage_ids(self, row_ids: Collection[UUID]) -> list[NetWorthItem]:
        """Items whose id, or ``id:<id>`` import key, is one of ``row_ids`` (F5.5)."""
        ids = list(row_ids)
        found: dict[UUID, NetWorthItem] = {}
        for start in range(0, len(ids), 400):
            chunk = ids[start : start + 400]
            statement = self._scoped_select().where(
                NetWorthItem.id.in_(chunk)
                | NetWorthItem.import_key.in_([f"id:{row_id}" for row_id in chunk])
            )
            for row in self._session.scalars(statement):
                found[row.id] = row
        return list(found.values())


class ItemValuationRepository(ScopedRepository[ItemValuation]):
    model = ItemValuation
    unsupported = frozenset({"update", "delete"})

    def create(
        self,
        *,
        item_id: UUID,
        as_of: date,
        value_minor: int,
        currency: str,
        note: str = "",
        import_batch_id: UUID | None = None,
        import_key: str | None = None,
        created_at: datetime | None = None,
    ) -> ItemValuation:
        row = ItemValuation(
            item_id=item_id,
            as_of=as_of,
            value_minor=value_minor,
            currency=currency,
            note=note,
            import_batch_id=import_batch_id,
            import_key=import_key,
        )
        if created_at is not None:
            row.created_at = created_at
        return self.add(row)

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

    def earliest_as_of(self) -> date | None:
        """The first valuation date in this workspace, or ``None`` when there are none."""
        return self._session.execute(
            select(func.min(ItemValuation.as_of)).where(
                ItemValuation.workspace_id == self._workspace_id
            )
        ).scalar_one()

    def for_import_batch(self, batch_id: UUID, *, lock: bool = False) -> list[ItemValuation]:
        statement = self._scoped_select().where(ItemValuation.import_batch_id == batch_id)
        if lock:
            statement = statement.with_for_update()
        return list(
            self._session.scalars(
                statement.order_by(ItemValuation.item_id, ItemValuation.as_of, ItemValuation.id)
            )
        )

    def delete_for_import_batch(self, batch_id: UUID) -> int:
        """Hard-delete a restore/merge batch's valuations; the append-only guard applies to ORM
        deletes only, and this is the one explicit rollback path (like workspace deletion)."""
        result = self._session.execute(
            delete(ItemValuation).where(
                ItemValuation.workspace_id == self._workspace_id,
                ItemValuation.import_batch_id == batch_id,
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)


class NetWorthSnapshotRepository(ScopedRepository[NetWorthSnapshot]):
    model = NetWorthSnapshot
    # A snapshot is replaced as a whole (delete and recreate), never edited field by field.
    unsupported = frozenset({"update"})

    def create(
        self,
        *,
        as_of: date,
        assets_minor: int,
        liabilities_minor: int,
        currency: str,
        breakdown: dict[str, object],
        source: str,
        import_batch_id: UUID | None = None,
        import_key: str | None = None,
    ) -> NetWorthSnapshot:
        return self.add(
            NetWorthSnapshot(
                as_of=as_of,
                assets_minor=assets_minor,
                liabilities_minor=liabilities_minor,
                net_minor=assets_minor - liabilities_minor,
                currency=currency,
                breakdown=breakdown,
                source=source,
                import_batch_id=import_batch_id,
                import_key=import_key,
            )
        )

    def for_date(self, as_of: date) -> list[NetWorthSnapshot]:
        """Every source stored for one date."""
        statement = self._scoped_select().where(NetWorthSnapshot.as_of == as_of)
        return list(self._session.scalars(statement.order_by(NetWorthSnapshot.source)))

    def for_range(self, start: date | None) -> list[NetWorthSnapshot]:
        """Snapshots from ``start`` onwards (all when ``None``), oldest first."""
        statement = self._scoped_select()
        if start is not None:
            statement = statement.where(NetWorthSnapshot.as_of >= start)
        return list(
            self._session.scalars(
                statement.order_by(
                    NetWorthSnapshot.as_of,
                    NetWorthSnapshot.source,
                    NetWorthSnapshot.created_at,
                    NetWorthSnapshot.id,
                )
            )
        )

    def for_import_batch(self, batch_id: UUID, *, lock: bool = False) -> list[NetWorthSnapshot]:
        statement = self._scoped_select().where(NetWorthSnapshot.import_batch_id == batch_id)
        if lock:
            statement = statement.with_for_update()
        return list(self._session.scalars(statement.order_by(NetWorthSnapshot.id)))
