"""A workspace-scoped fake resolver for the F5.4 link tests."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from uuid import UUID

from sqlalchemy.orm import Session

from assetboard.db.repositories.base import WorkspaceScope
from assetboard.domain.money import Money
from assetboard.domain.networth import Side, Unavailable
from assetboard.services.networth_links import (
    LinkChoice,
    LinkTarget,
    registry,
)


@dataclass
class FakeTarget:
    workspace_id: UUID
    link_id: UUID
    label: str
    currency: str = "EUR"
    href: str | None = None
    values: dict[date, int] = field(default_factory=dict)
    resolution_currency: str | None = None


@dataclass
class FakeLinkSpec:
    side: Side
    targets: list[FakeTarget]
    calls: list[tuple[UUID, frozenset[UUID], tuple[date, ...]]] = field(default_factory=list)


class FakeLinkResolver:
    def __init__(self, session: Session, scope: WorkspaceScope, spec: FakeLinkSpec) -> None:
        self._session = session
        self._scope = scope
        self._spec = spec
        self.side = spec.side

    def _target(self, link_id: UUID) -> FakeTarget | None:
        for target in self._spec.targets:
            if target.workspace_id == self._scope.workspace.id and target.link_id == link_id:
                return target
        return None

    def resolve_many(
        self, link_ids: Collection[UUID], dates: Sequence[date]
    ) -> Mapping[tuple[UUID, date], Money | Unavailable]:
        self._spec.calls.append((self._scope.workspace.id, frozenset(link_ids), tuple(dates)))
        result: dict[tuple[UUID, date], Money | Unavailable] = {}
        for link_id in link_ids:
            target = self._target(link_id)
            for day in dates:
                if target is None or day not in target.values:
                    result[(link_id, day)] = Unavailable()
                    continue
                result[(link_id, day)] = Money(
                    target.values[day],
                    target.resolution_currency or target.currency,
                )
        return result

    def describe(self, link_id: UUID) -> LinkTarget | None | Unavailable:
        target = self._target(link_id)
        if target is None:
            return None
        return LinkTarget(target.label, target.currency, target.href)

    def choices(self) -> Sequence[LinkChoice]:
        return tuple(
            LinkChoice(target.link_id, target.label)
            for target in self._spec.targets
            if target.workspace_id == self._scope.workspace.id
        )


class FakeLinks:
    def __init__(self) -> None:
        self.specs: list[FakeLinkSpec] = []

    def register(
        self,
        *,
        side: Side = Side.ASSET,
        link_type: str = "portfolio_asset_class",
        label: str = "Fake source",
        targets: Sequence[FakeTarget] = (),
    ) -> FakeLinkSpec:
        spec = FakeLinkSpec(side, list(targets))
        registry.register(
            link_type,
            label,
            lambda session, scope: FakeLinkResolver(session, scope, spec),
        )
        self.specs.append(spec)
        return spec

    def reset(self) -> None:
        registry.reset_for_tests()
        self.specs.clear()
