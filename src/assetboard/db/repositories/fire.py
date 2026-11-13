from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select

from assetboard.db.models.fire import FireContribution, FireEvent, FireScenario
from assetboard.db.repositories.base import ScopedRepository


class FireScenarioRepository(ScopedRepository[FireScenario]):
    model = FireScenario

    def create(self, **values: object) -> FireScenario:
        return self.add(FireScenario(**values))

    def active(self) -> list[FireScenario]:
        statement = (
            self._scoped_select()
            .where(FireScenario.archived_at.is_(None))
            .order_by(FireScenario.name_key, FireScenario.id)
        )
        return list(self._session.scalars(statement))

    def archived(self) -> list[FireScenario]:
        statement = (
            self._scoped_select()
            .where(FireScenario.archived_at.is_not(None))
            .order_by(FireScenario.archived_at.desc(), FireScenario.name_key, FireScenario.id)
        )
        return list(self._session.scalars(statement))

    def active_by_name_key(self, name_key: str) -> FireScenario | None:
        statement = self._scoped_select().where(
            FireScenario.archived_at.is_(None),
            FireScenario.name_key == name_key,
        )
        return self._session.scalars(statement).one_or_none()

    def active_default(self) -> FireScenario | None:
        statement = self._scoped_select().where(
            FireScenario.archived_at.is_(None),
            FireScenario.is_default.is_(True),
        )
        return self._session.scalars(statement).one_or_none()

    def first_active(self, *, exclude_id: UUID | None = None) -> FireScenario | None:
        statement = self._scoped_select().where(FireScenario.archived_at.is_(None))
        if exclude_id is not None:
            statement = statement.where(FireScenario.id != exclude_id)
        statement = statement.order_by(FireScenario.created_at, FireScenario.id).limit(1)
        return self._session.scalars(statement).first()

    def active_name_keys(self) -> set[str]:
        statement = select(FireScenario.name_key).where(
            FireScenario.workspace_id == self._workspace_id,
            FireScenario.archived_at.is_(None),
        )
        return set(self._session.scalars(statement))


class FireContributionRepository(ScopedRepository[FireContribution]):
    model = FireContribution

    def create(self, **values: object) -> FireContribution:
        return self.add(FireContribution(**values))

    def for_scenario(self, scenario_id: UUID) -> list[FireContribution]:
        statement = (
            self._scoped_select()
            .where(FireContribution.scenario_id == scenario_id)
            .order_by(FireContribution.year)
        )
        return list(self._session.scalars(statement))

    def counts_by_scenario(self) -> dict[UUID, int]:
        statement = (
            select(FireContribution.scenario_id, func.count())
            .where(FireContribution.workspace_id == self._workspace_id)
            .group_by(FireContribution.scenario_id)
        )
        return dict(self._session.execute(statement).all())


class FireEventRepository(ScopedRepository[FireEvent]):
    model = FireEvent

    def create(self, **values: object) -> FireEvent:
        return self.add(FireEvent(**values))

    def for_scenario(self, scenario_id: UUID) -> list[FireEvent]:
        statement = (
            self._scoped_select()
            .where(FireEvent.scenario_id == scenario_id)
            .order_by(FireEvent.year, FireEvent.id)
        )
        return list(self._session.scalars(statement))

    def counts_by_scenario(self) -> dict[UUID, int]:
        statement = (
            select(FireEvent.scenario_id, func.count())
            .where(FireEvent.workspace_id == self._workspace_id)
            .group_by(FireEvent.scenario_id)
        )
        return dict(self._session.execute(statement).all())
