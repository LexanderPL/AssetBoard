"""Remembered import category mappings (F4.3)."""

from __future__ import annotations

from collections.abc import Collection
from uuid import UUID

from assetboard.db.models.import_batch import ImportCategoryMapping
from assetboard.db.repositories.base import ScopedRepository


class ImportCategoryMappingRepository(ScopedRepository[ImportCategoryMapping]):
    model = ImportCategoryMapping

    def create(
        self,
        *,
        kind: str,
        source_name_key: str,
        action: str,
        category_id: UUID | None,
    ) -> ImportCategoryMapping:
        return self.add(
            ImportCategoryMapping(
                kind=kind,
                source_name_key=source_name_key,
                action=action,
                category_id=category_id,
            )
        )

    def for_kind(self, kind: str) -> list[ImportCategoryMapping]:
        statement = (
            self._scoped_select()
            .where(ImportCategoryMapping.kind == kind)
            .order_by(ImportCategoryMapping.source_name_key, ImportCategoryMapping.id)
        )
        return list(self._session.scalars(statement))

    def get_for_source(self, *, kind: str, source_name_key: str) -> ImportCategoryMapping | None:
        statement = self._scoped_select().where(
            ImportCategoryMapping.kind == kind,
            ImportCategoryMapping.source_name_key == source_name_key,
        )
        return self._session.scalars(statement).one_or_none()

    def upsert(
        self,
        *,
        kind: str,
        source_name_key: str,
        action: str,
        category_id: UUID | None,
    ) -> ImportCategoryMapping:
        existing = self.get_for_source(kind=kind, source_name_key=source_name_key)
        if existing is None:
            return self.create(
                kind=kind,
                source_name_key=source_name_key,
                action=action,
                category_id=category_id,
            )
        if existing.action == action and existing.category_id == category_id:
            return existing
        return self.update(existing.id, action=action, category_id=category_id)

    def delete_for_categories(self, category_ids: Collection[UUID]) -> int:
        """Remove mappings that point at these categories (rollback of a batch that created
        them); mappings to categories that existed before the batch stay."""
        wanted = list(dict.fromkeys(category_ids))
        removed = 0
        for start in range(0, len(wanted), 400):
            chunk = wanted[start : start + 400]
            statement = self._scoped_select().where(ImportCategoryMapping.category_id.in_(chunk))
            for row in list(self._session.scalars(statement)):
                self.delete(row)
                removed += 1
        return removed
