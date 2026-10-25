from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import and_, delete, func, or_, select

from assetboard.db.models.notifications import NotificationChannel, NotificationLog
from assetboard.db.repositories.base import ScopedRepository


class NotificationChannelRepository(ScopedRepository[NotificationChannel]):
    """Channels of the current workspace. User-facing reads always name the owning user."""

    model = NotificationChannel

    def create(self, **values: object) -> NotificationChannel:
        return self.add(NotificationChannel(**values))

    def for_user(self, user_id: UUID) -> list[NotificationChannel]:
        statement = self._scoped_select().where(NotificationChannel.user_id == user_id)
        return list(
            self._session.scalars(
                statement.order_by(NotificationChannel.name, NotificationChannel.id)
            )
        )

    def owned(self, channel_id: UUID, user_id: UUID) -> NotificationChannel | None:
        statement = self._scoped_select().where(
            NotificationChannel.id == channel_id, NotificationChannel.user_id == user_id
        )
        return self._session.scalars(statement).one_or_none()

    def count_for_user(self, user_id: UUID) -> int:
        statement = select(func.count()).where(
            NotificationChannel.workspace_id == self._workspace_id,
            NotificationChannel.user_id == user_id,
        )
        return int(self._session.execute(statement).scalar_one())

    def enabled(self) -> list[NotificationChannel]:
        statement = self._scoped_select().where(NotificationChannel.enabled.is_(True))
        return list(
            self._session.scalars(
                statement.order_by(NotificationChannel.created_at, NotificationChannel.id)
            )
        )

    def delete_for_user(self, user_id: UUID) -> int:
        """Remove a former member's channels (their log rows go with them)."""
        rows = self.for_user(user_id)
        for row in rows:
            self._session.delete(row)
        self._session.flush()
        return len(rows)


class NotificationLogRepository(ScopedRepository[NotificationLog]):
    model = NotificationLog

    def create(self, **values: object) -> NotificationLog:
        return self.add(NotificationLog(**values))

    def since(self, channel_id: UUID, first_slot: date) -> list[NotificationLog]:
        statement = self._scoped_select().where(
            NotificationLog.channel_id == channel_id, NotificationLog.sent_on >= first_slot
        )
        return list(self._session.scalars(statement.order_by(NotificationLog.id)))

    def overdue_sent_counts(self, channel_id: UUID) -> dict[str, int]:
        rows = self._session.execute(
            select(NotificationLog.subject_key, func.count())
            .where(
                NotificationLog.workspace_id == self._workspace_id,
                NotificationLog.channel_id == channel_id,
                NotificationLog.kind == "overdue",
                NotificationLog.status == "sent",
            )
            .group_by(NotificationLog.subject_key)
        )
        return {key: int(count) for key, count in rows}

    def slot(
        self, channel_id: UUID, kind: str, subject_key: str, sent_on: date
    ) -> NotificationLog | None:
        statement = self._scoped_select().where(
            NotificationLog.channel_id == channel_id,
            NotificationLog.kind == kind,
            NotificationLog.subject_key == subject_key,
            NotificationLog.sent_on == sent_on,
        )
        return self._session.scalars(statement).one_or_none()

    def stale_sending(self, attempted_before: datetime) -> list[NotificationLog]:
        statement = self._scoped_select().where(
            NotificationLog.status == "sending", NotificationLog.attempted_at < attempted_before
        )
        return list(self._session.scalars(statement.order_by(NotificationLog.id)))

    def recent_for_user(self, user_id: UUID, limit: int) -> list[tuple[NotificationLog, str]]:
        statement = (
            select(NotificationLog, NotificationChannel.name)
            .join(
                NotificationChannel,
                and_(
                    NotificationChannel.workspace_id == self._workspace_id,
                    NotificationChannel.id == NotificationLog.channel_id,
                ),
            )
            .where(
                NotificationLog.workspace_id == self._workspace_id,
                NotificationChannel.user_id == user_id,
            )
            .order_by(NotificationLog.attempted_at.desc(), NotificationLog.id)
            .limit(limit)
        )
        return [(row, name) for row, name in self._session.execute(statement)]

    def prune(self, before: date) -> int:
        """Delete this workspace's log rows with a slot before ``before`` that no rule needs.

        Sent overdue reminders stay: they count toward the overdue cap for as long as their
        occurrence is pending (at most seven per occurrence and channel).
        """
        result = self._session.scalars(
            delete(NotificationLog)
            .where(
                NotificationLog.workspace_id == self._workspace_id,
                NotificationLog.sent_on < before,
                or_(NotificationLog.kind != "overdue", NotificationLog.status != "sent"),
            )
            .returning(NotificationLog.id)
            .execution_options(synchronize_session=False)
        ).all()
        return len(result)
