"""Reminder channels and their delivery log (F3.6, ADR-0021)."""

from __future__ import annotations

from datetime import date, datetime, time
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from assetboard.db.base import Base, IdMixin, TimestampMixin, WorkspaceScopedMixin
from assetboard.db.types import UtcDateTime
from assetboard.domain.notifications import MAX_LEAD_DAYS


class NotificationChannel(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """One member's Apprise destination in one workspace; the URL is stored encrypted."""

    __tablename__ = "notification_channels"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_notification_channels_workspace_id_id"),
        CheckConstraint("mode IN ('digest', 'per_item')", name="mode_valid"),
        CheckConstraint(
            f"default_lead_days BETWEEN 0 AND {MAX_LEAD_DAYS}", name="default_lead_days_valid"
        ),
        Index("ix_notification_channels_workspace_user", "workspace_id", "user_id"),
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    url_encrypted: Mapped[str] = mapped_column(Text(), nullable=False)
    scheme: Mapped[str] = mapped_column(String(32), nullable=False)
    service_name: Mapped[str] = mapped_column(String(100), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="digest")
    digest_time: Mapped[time] = mapped_column(Time(), nullable=False)
    default_lead_days: Mapped[int] = mapped_column(Integer(), nullable=False, default=3)
    notify_due_soon: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
    notify_overdue: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
    notify_work_logs: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
    include_amounts: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    last_run_on: Mapped[date | None] = mapped_column(Date(), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(32), nullable=True)


class NotificationLog(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """One reminder claimed for a channel and slot; the unique key prevents duplicates.

    ``subject_key`` is the occurrence id or ``worklog:<source id>:<YYYY-MM>``; ``sent_on`` is
    the reminder's slot (the due date, the overdue day, or the work-log trigger day).
    ``occurrence_id`` has no foreign key: regeneration deletes pending occurrences, and the log
    keeps its history.
    """

    __tablename__ = "notification_log"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "channel_id"],
            ["notification_channels.workspace_id", "notification_channels.id"],
            name="fk_notification_log_workspace_channel",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "channel_id", "kind", "subject_key", "sent_on", name="uq_notification_log_slot"
        ),
        CheckConstraint("kind IN ('due_soon', 'overdue', 'work_log')", name="kind_valid"),
        CheckConstraint("status IN ('sending', 'sent', 'failed')", name="status_valid"),
        Index("ix_notification_log_workspace_channel", "workspace_id", "channel_id", "sent_on"),
    )

    channel_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_key: Mapped[str] = mapped_column(String(80), nullable=False)
    occurrence_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    sent_on: Mapped[date] = mapped_column(Date(), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attempted_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
