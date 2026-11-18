"""Per-user Home widget preferences (F9.1)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from assetboard.db.base import Base, IdMixin, TimestampMixin, WorkspaceScopedMixin
from assetboard.db.types import UtcDateTime
from assetboard.domain.dashboard import (
    DEFAULT_NET_WORTH_RANGE,
    DEFAULT_PORTFOLIO_RANGE,
    DEFAULT_UPCOMING_LIMIT,
    NET_WORTH_RANGES,
    PORTFOLIO_RANGES,
    UPCOMING_LIMIT_MAX,
    UPCOMING_LIMIT_MIN,
)

_PORTFOLIO_RANGE_SQL = ", ".join(f"'{value}'" for value in PORTFOLIO_RANGES)
_NET_WORTH_RANGE_SQL = ", ".join(f"'{value}'" for value in NET_WORTH_RANGES)


class DashboardPreference(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """The ordered Home widgets and their settings for one member of one workspace.

    No row means the default catalogue: every shipped widget, in catalogue order, with the
    default upcoming-payment limit, Net worth range and portfolio chart range. A row whose
    ``widgets`` list is empty is a deliberate empty Home. Unknown keys are ignored on read, so a
    removed widget never breaks the page.
    """

    __tablename__ = "dashboard_preferences"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_dashboard_preferences_workspace_user"),
        CheckConstraint(
            f"upcoming_limit BETWEEN {UPCOMING_LIMIT_MIN} AND {UPCOMING_LIMIT_MAX}",
            name="upcoming_limit_valid",
        ),
        CheckConstraint(
            f"portfolio_range IN ({_PORTFOLIO_RANGE_SQL})", name="portfolio_range_valid"
        ),
        CheckConstraint(
            f"net_worth_range IN ({_NET_WORTH_RANGE_SQL})", name="net_worth_range_valid"
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    widgets: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    customised_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    # How many upcoming occurrences the Home widget lists (1-50; the rest stay on Upcoming).
    upcoming_limit: Mapped[int] = mapped_column(
        Integer(),
        nullable=False,
        default=DEFAULT_UPCOMING_LIMIT,
        server_default=str(DEFAULT_UPCOMING_LIMIT),
    )
    # The Home portfolio value-history range: one of ``PORTFOLIO_RANGES``.
    portfolio_range: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        default=DEFAULT_PORTFOLIO_RANGE,
        server_default=DEFAULT_PORTFOLIO_RANGE,
    )
    # The Home net-worth history range: one of ``NET_WORTH_RANGES``.
    net_worth_range: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        default=DEFAULT_NET_WORTH_RANGE,
        server_default=DEFAULT_NET_WORTH_RANGE,
    )
