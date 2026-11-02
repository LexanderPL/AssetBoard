from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
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


class ImportCategoryMapping(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """A remembered source-category choice for later CSV imports (F4.3).

    ``source_name_key`` is the F2.1 name key of the category as written in the file, so the
    mapping survives case and whitespace differences. A mapping is per kind because the same
    source name can exist as an expense and as an income category. The composite foreign key
    makes deleting a mapped category remove its saved mapping too.
    """

    __tablename__ = "import_category_mappings"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "kind",
            "source_name_key",
            name="uq_import_category_mappings_workspace_kind_name",
        ),
        CheckConstraint("kind IN ('expense', 'income')", name="kind_valid"),
        CheckConstraint("action IN ('map', 'ignore')", name="action_valid"),
        CheckConstraint(
            "(action = 'map' AND category_id IS NOT NULL) OR "
            "(action = 'ignore' AND category_id IS NULL)",
            name="category_required_for_map",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "category_id"],
            ["categories.workspace_id", "categories.id"],
            name="fk_import_category_mappings_category",
            ondelete="CASCADE",
        ),
        Index("ix_import_category_mappings_workspace_kind", "workspace_id", "kind"),
    )

    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_name_key: Mapped[str] = mapped_column(String(255), nullable=False)
    category_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
