from __future__ import annotations

from collections.abc import Collection
from uuid import UUID

from sqlalchemy import func, select

from assetboard.db.models.fire import FireEvent, FireScenario
from assetboard.db.repositories.base import ScopedRepository


class FireScenarioRepository(ScopedRepository[FireScenario]):
    model = FireScenario

    def create(self, **values: object) -> FireScenario:
        return self.add(FireScenario(**values))

    def live(self) -> FireScenario | None:
        """The chosen plan of this workspace, if the migration is done."""
        statement = self._scoped_select().where(FireScenario.is_live.is_(True))
        return self._session.scalars(statement).one_or_none()

    def legacy(self) -> list[FireScenario]:
        """Old scenarios of this workspace that still await the chooser."""
        statement = self._scoped_select().where(FireScenario.is_live.is_(False))
        return list(self._session.scalars(statement))

    def legacy_count(self) -> int:
        statement = (
            select(func.count())
            .select_from(FireScenario)
            .where(
                FireScenario.workspace_id == self._workspace_id,
                FireScenario.is_live.is_(False),
            )
        )
        return self._session.scalar(statement) or 0


class FireEventRepository(ScopedRepository[FireEvent]):
    model = FireEvent

    def create(self, **values: object) -> FireEvent:
        return self.add(FireEvent(**values))

    def for_plan(self, plan_id: UUID) -> list[FireEvent]:
        statement = (
            self._scoped_select()
            .where(FireEvent.scenario_id == plan_id)
            .order_by(FireEvent.year, FireEvent.id)
        )
        return list(self._session.scalars(statement))

    def for_plans(self, plan_ids: Collection[UUID]) -> list[FireEvent]:
        """The events of several legacy scenarios in one query (archive/migration only)."""
        if not plan_ids:
            return []
        statement = (
            self._scoped_select()
            .where(FireEvent.scenario_id.in_(tuple(plan_ids)))
            .order_by(FireEvent.scenario_id, FireEvent.year, FireEvent.id)
        )
        return list(self._session.scalars(statement))

    def count_for_plan(self, plan_id: UUID) -> int:
        statement = (
            select(func.count())
            .select_from(FireEvent)
            .where(
                FireEvent.workspace_id == self._workspace_id,
                FireEvent.scenario_id == plan_id,
            )
        )
        return self._session.scalar(statement) or 0

    def counts_by_scenario(self) -> dict[UUID, int]:
        statement = (
            select(FireEvent.scenario_id, func.count())
            .where(FireEvent.workspace_id == self._workspace_id)
            .group_by(FireEvent.scenario_id)
        )
        return dict(self._session.execute(statement).all())

    def saving_override_years(self, plan_id: UUID) -> set[int]:
        statement = select(FireEvent.year).where(
            FireEvent.workspace_id == self._workspace_id,
            FireEvent.scenario_id == plan_id,
            FireEvent.kind == "saving_override",
        )
        return set(self._session.scalars(statement))
