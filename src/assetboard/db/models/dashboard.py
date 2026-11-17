"""Per-user Home widget preferences (F9.1)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import JSON, ForeignKey, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from assetboard.db.base import Base, IdMixin, TimestampMixin, WorkspaceScopedMixin
from assetboard.db.types import UtcDateTime


class DashboardPreference(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """The ordered list of enabled Home widgets for one member of one workspace.

    No row means the default catalogue: every shipped widget, in catalogue order. A row whose
    ``widgets`` list is empty is a deliberate empty Home. Unknown keys are ignored on read, so a
    removed widget never breaks the page.
    """

    __tablename__ = "dashboard_preferences"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_dashboard_preferences_workspace_user"),
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    widgets: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    customised_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
