"""Instance-level backup history (F9.4).

These rows are operational records about the whole installation, not workspace data: they carry
no ``workspace_id`` and are visible only to instance administrators. The tenant-isolation test
does not cover the table because no workspace can own or read its rows.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from assetboard.db.base import Base, IdMixin
from assetboard.db.types import UtcDateTime


class BackupRun(IdMixin, Base):
    """One attempt to copy the database into ``DATA_DIR/backups``.

    ``path`` is the file name inside the backup directory. ``status`` is ``running`` until the
    copy has been verified and renamed into place, then ``ok``; a failure closes the row as
    ``failed`` with a short ``error`` code (never a path or an exception message).
    """

    __tablename__ = "backup_runs"
    __table_args__ = (
        CheckConstraint("status IN ('running', 'ok', 'failed')", name="status_valid"),
        CheckConstraint("origin IN ('scheduled', 'manual', 'pre_restore')", name="origin_valid"),
        CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="size_bytes_valid"),
        Index("ix_backup_runs_started_at", "started_at"),
    )

    started_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="scheduled")
    path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    error: Mapped[str | None] = mapped_column(String(32), nullable=True)
