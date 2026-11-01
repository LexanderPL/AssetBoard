from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from assetboard.db.base import Base, IdMixin, TimestampMixin, WorkspaceScopedMixin
from assetboard.db.types import UtcDateTime


class ImportBatch(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    __tablename__ = "import_batches"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_import_batches_workspace_id_id"),
        CheckConstraint(
            "source IN ('archive', 'transactions_csv', 'budget_lines_csv', 'categories_csv')",
            name="source_valid",
        ),
        CheckConstraint("mode IN ('restore', 'merge')", name="mode_valid"),
        CheckConstraint(
            "status IN ('uploaded', 'planned', 'applied', 'rolled_back', 'cancelled', 'failed')",
            name="status_valid",
        ),
        CheckConstraint("size_bytes >= 0", name="size_valid"),
        CheckConstraint("length(file_sha256) = 64", name="sha256_length"),
    )

    source: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    options: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    applied_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    rolled_back_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    previous_settings: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
