from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import and_

from assetboard.db.models.import_batch import ImportBatch
from assetboard.db.repositories.base import ScopedRepository


class ImportBatchRepository(ScopedRepository[ImportBatch]):
    model = ImportBatch
    unsupported = frozenset({"delete"})

    def create(
        self,
        *,
        source: str,
        mode: str,
        filename: str,
        file_sha256: str,
        size_bytes: int,
        options: dict[str, object],
        status: str,
        started_by_user_id: UUID | None,
    ) -> ImportBatch:
        return self.add(
            ImportBatch(
                source=source,
                mode=mode,
                filename=filename,
                file_sha256=file_sha256,
                size_bytes=size_bytes,
                options=options,
                status=status,
                started_by_user_id=started_by_user_id,
            )
        )

    def recent(self, limit: int = 20) -> list[ImportBatch]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        statement = (
            self._scoped_select()
            .order_by(ImportBatch.created_at.desc(), ImportBatch.id.desc())
            .limit(limit)
        )
        return list(self._session.scalars(statement))

    def get_for_update(self, batch_id: UUID) -> ImportBatch | None:
        statement = self._scoped_select().where(ImportBatch.id == batch_id).with_for_update()
        return self._session.scalars(statement).one_or_none()

    def expired_before(self, cutoff: datetime) -> list[ImportBatch]:
        statement = self._scoped_select().where(
            and_(
                ImportBatch.status.in_(("uploaded", "planned")),
                ImportBatch.created_at < cutoff,
            )
        )
        return list(self._session.scalars(statement))
