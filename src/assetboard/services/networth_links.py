"""Linked net-worth values (F5.4): resolver protocol, registry and batch resolution.

A linked item takes its value from another feature instead of appending manual valuations.
Resolvers are registered here by later modules (portfolio in M6, loans in M7). They are
factories bound to a ``(session, workspace scope)`` pair so every target lookup is
workspace-scoped.

The registry is global and empty in production until a feature registers a resolver. Tests may
register a fake and call ``reset_for_tests``; there is deliberately no public removal API.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from assetboard.db.repositories.base import WorkspaceScope
from assetboard.domain.money import Money
from assetboard.domain.networth import Side, Unavailable
from assetboard.domain.networth_history import ItemHistory

SUPPORTED_LINK_TYPES = (
    "portfolio_asset_class",
    "portfolio_holding",
    "loan_balance",
)
UNAVAILABLE = Unavailable()


@dataclass(frozen=True, slots=True)
class LinkTarget:
    """What the UI needs to describe a linked target."""

    label: str
    currency: str
    href: str | None = None


@dataclass(frozen=True, slots=True)
class LinkChoice:
    """One target offered by a resolver's picker."""

    link_id: UUID
    label: str


@dataclass(frozen=True, slots=True)
class LinkOption:
    """One picker option: the link type's label and the target's label/id."""

    link_type: str
    link_type_label: str
    choice: LinkChoice


class ValuationResolver(Protocol):
    """Resolves values for one registered link type, always for this workspace."""

    side: Side

    def resolve_many(
        self, link_ids: Collection[UUID], dates: Sequence[date]
    ) -> Mapping[tuple[UUID, date], Money | Unavailable]: ...

    def describe(self, link_id: UUID) -> LinkTarget | None | Unavailable: ...

    def choices(self) -> Sequence[LinkChoice]: ...


ResolverFactory = Callable[[Session, WorkspaceScope], ValuationResolver]


@dataclass(frozen=True, slots=True)
class LinkTypeSpec:
    key: str
    label: str
    factory: ResolverFactory


class LinkRegistry:
    """The link types available for the current workspace and request."""

    def __init__(self) -> None:
        self._types: dict[str, LinkTypeSpec] = {}

    def register(self, key: str, label: str, factory: ResolverFactory) -> None:
        """Register one resolver factory; later modules call this at import time."""
        if key not in SUPPORTED_LINK_TYPES:
            raise ValueError(f"unsupported link type: {key}")
        if key in self._types:
            raise ValueError(f"link type already registered: {key}")
        self._types[key] = LinkTypeSpec(key, label, factory)

    def reset_for_tests(self) -> None:
        """Remove every registration. Tests only; production has no unregister path."""
        self._types.clear()

    def labels(self) -> dict[str, str]:
        return {key: spec.label for key, spec in self._types.items()}

    def label(self, link_type: str | None) -> str | None:
        if link_type is None:
            return None
        spec = self._types.get(link_type)
        return spec.label if spec is not None else None

    def _resolver(
        self, session: Session, scope: WorkspaceScope, link_type: str
    ) -> ValuationResolver | None:
        spec = self._types.get(link_type)
        return spec.factory(session, scope) if spec is not None else None

    def resolver_for(
        self, session: Session, scope: WorkspaceScope, link_type: str
    ) -> ValuationResolver | None:
        return self._resolver(session, scope, link_type)

    def options_for_side(
        self, session: Session, scope: WorkspaceScope, side: Side
    ) -> tuple[LinkOption, ...]:
        """Every target offered for ``side``; empty when no resolver is registered for it."""
        options: list[LinkOption] = []
        for spec in self._types.values():
            resolver = spec.factory(session, scope)
            if resolver.side is not side:
                continue
            for choice in resolver.choices():
                options.append(LinkOption(spec.key, spec.label, choice))
        return tuple(options)

    def describe(
        self, session: Session, scope: WorkspaceScope, link_type: str, link_id: UUID
    ) -> LinkTarget | None | Unavailable:
        """A target's label/currency/URL, ``None`` when unknown, or ``Unavailable``."""
        resolver = self._resolver(session, scope, link_type)
        if resolver is None:
            return UNAVAILABLE
        return resolver.describe(link_id)

    def resolve_many(
        self,
        session: Session,
        scope: WorkspaceScope,
        histories: Sequence[ItemHistory],
        dates: Sequence[date],
    ) -> dict[tuple[UUID, date], Money | Unavailable]:
        """Resolve every linked item for every date, one resolver call per link type.

        The key is ``(item id, date)``, not the target id, so two items linked to the same
        target share the resolver result. A value in another currency counts as unavailable.
        """
        wanted_dates = tuple(dict.fromkeys(dates))
        grouped: dict[str, list[ItemHistory]] = {}
        for item in histories:
            if item.valuation_mode == "linked" and item.link_type is not None:
                grouped.setdefault(item.link_type, []).append(item)
        resolved: dict[tuple[UUID, date], Money | Unavailable] = {}
        for link_type, items in grouped.items():
            resolver = self._resolver(session, scope, link_type)
            link_ids = tuple(
                sorted({item.link_id for item in items if item.link_id is not None}, key=str)
            )
            if resolver is None or not link_ids:
                for item in items:
                    for day in wanted_dates:
                        resolved[(item.id, day)] = UNAVAILABLE
                continue
            raw = resolver.resolve_many(link_ids, wanted_dates)
            for item in items:
                for day in wanted_dates:
                    value: Money | Unavailable = UNAVAILABLE
                    if item.link_id is not None:
                        value = raw.get((item.link_id, day), UNAVAILABLE)
                    if (
                        not isinstance(value, Money)
                        or value.currency != item.currency
                        or value.amount_minor < 0
                    ):
                        value = UNAVAILABLE
                    resolved[(item.id, day)] = value
        return resolved

    def resolve_one(
        self,
        session: Session,
        scope: WorkspaceScope,
        *,
        link_type: str,
        link_id: UUID,
        currency: str,
        as_of: date,
    ) -> Money | Unavailable:
        """A single link value; a thin wrapper over ``resolve_many``."""
        resolver = self._resolver(session, scope, link_type)
        if resolver is None:
            return UNAVAILABLE
        raw = resolver.resolve_many([link_id], [as_of])
        value = raw.get((link_id, as_of), UNAVAILABLE)
        if not isinstance(value, Money) or value.currency != currency or value.amount_minor < 0:
            return UNAVAILABLE
        return value


registry = LinkRegistry()
