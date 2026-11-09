"""M6 net-worth link resolvers, helper and double-counting warnings (F6.8).

The resolvers use the F6.6 current valuation for the workspace's today (including undated legacy
lots) and the F6.7 historical rules for past dates. The helper creates one linked asset item per
active, positive asset class under an on-demand Investments category. Warnings are read-only and
never change totals.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal, cast
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.engine import run_immediate
from assetboard.db.models.investments import AssetClass
from assetboard.db.models.networth import NetWorthCategory
from assetboard.db.repositories import (
    AssetClassRepository,
    AuditEventRepository,
    CurrentWorkspaceRepository,
    InstrumentRepository,
    NetWorthCategoryRepository,
    NetWorthItemRepository,
)
from assetboard.domain.categories import PALETTE, name_key
from assetboard.domain.instruments import InstrumentType
from assetboard.domain.money import Money
from assetboard.domain.networth import Side, Unavailable
from assetboard.i18n import gettext
from assetboard.i18n.locale import SUPPORTED_LOCALES
from assetboard.services.context import Context, Role, require_role, require_role_now
from assetboard.services.networth_links import (
    UNAVAILABLE,
    LinkChoice,
    LinkTarget,
    WorkspaceScope,
    registry,
)
from assetboard.services.portfolio import (
    CurrentPortfolioData,
    current_portfolio_data_in_session,
)
from assetboard.services.portfolio_history import HistoricalValues, historical_values, load_history


def _(message: str) -> str:
    """Marks a message for extraction; it is translated where it is rendered."""
    return message


@dataclass(frozen=True, slots=True)
class LinkedNetWorthItem:
    """The active linked item fields needed by the warning checks."""

    link_type: str
    link_id: UUID


@dataclass(frozen=True, slots=True)
class PortfolioLinkWarning:
    code: Literal["class_holding", "legacy_property"]
    class_name: str | None = None
    holding_names: tuple[str, ...] = ()
    item_names: tuple[str, ...] = ()
    instrument_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CreatedLinks:
    created: tuple[str, ...]
    skipped: int
    category_created: bool


def _workspace_today(scope: WorkspaceScope) -> date | None:
    clock = getattr(scope, "clock", None)
    workspace = getattr(scope, "workspace", None)
    timezone = getattr(workspace, "timezone", None)
    if clock is None or timezone is None:
        return None
    return cast(Context, scope).clock.today(timezone)


def _base_currency(session: Session, scope: WorkspaceScope) -> str:
    return CurrentWorkspaceRepository(session, scope).get().base_currency


def _class_missing(data: CurrentPortfolioData, class_id: UUID) -> bool:
    for row in data.valuation.rows:
        if row.missing is None:
            continue
        if any(
            allocation.asset_class_id == class_id and allocation.instrument_id == row.instrument_id
            for allocation in data.allocations
        ):
            return True
    return False


def _current_class(data: CurrentPortfolioData, class_id: UUID) -> Money | None:
    if _class_missing(data, class_id):
        return None
    return Money(data.valuation.class_value(class_id), data.currency)


def _current_holdings(data: CurrentPortfolioData) -> dict[UUID, Money | None]:
    return {
        row.instrument_id: None if row.missing is not None else row.value
        for row in data.valuation.rows
    }


class _PortfolioAssetClassResolver:
    side = Side.ASSET

    def __init__(self, session: Session, scope: WorkspaceScope) -> None:
        self.session = session
        self.scope = scope

    def resolve_many(
        self, link_ids: Collection[UUID], dates: Sequence[date]
    ) -> Mapping[tuple[UUID, date], Money | Unavailable]:
        today = _workspace_today(self.scope)
        current_dates = [day for day in dates if today is not None and day >= today]
        historical_dates = [day for day in dates if today is None or day < today]
        current = (
            current_portfolio_data_in_session(self.session, cast(Context, self.scope))
            if current_dates
            else None
        )
        historical: HistoricalValues | None = None
        if historical_dates:
            history = load_history(
                self.session,
                cast(Context, self.scope),
                observation_dates=historical_dates,
                end=max(historical_dates),
            )
            historical = historical_values(history, historical_dates)
        known = {row.id for row in AssetClassRepository(self.session, self.scope).ordered()}
        result: dict[tuple[UUID, date], Money | Unavailable] = {}
        for class_id in link_ids:
            for day in dates:
                value: Money | None | Unavailable = UNAVAILABLE
                if class_id in known:
                    if current is not None and day in current_dates:
                        value = _current_class(current, class_id)
                    elif historical is not None:
                        value = historical.asset_class(class_id, day)
                result[(class_id, day)] = UNAVAILABLE if value is None else value
        return result

    def describe(self, link_id: UUID) -> LinkTarget | None | Unavailable:
        row = AssetClassRepository(self.session, self.scope).get(link_id)
        if row is None:
            return None
        return LinkTarget(
            row.name,
            _base_currency(self.session, self.scope),
            f"/investments/portfolio?scope={row.id}",
        )

    def choices(self) -> Sequence[LinkChoice]:
        return tuple(
            LinkChoice(row.id, row.name)
            for row in AssetClassRepository(self.session, self.scope).ordered()
            if row.archived_at is None
        )


class _PortfolioHoldingResolver:
    side = Side.ASSET

    def __init__(self, session: Session, scope: WorkspaceScope) -> None:
        self.session = session
        self.scope = scope

    def resolve_many(
        self, link_ids: Collection[UUID], dates: Sequence[date]
    ) -> Mapping[tuple[UUID, date], Money | Unavailable]:
        today = _workspace_today(self.scope)
        current_dates = [day for day in dates if today is not None and day >= today]
        historical_dates = [day for day in dates if today is None or day < today]
        current = (
            current_portfolio_data_in_session(self.session, cast(Context, self.scope))
            if current_dates
            else None
        )
        historical: HistoricalValues | None = None
        if historical_dates:
            history = load_history(
                self.session,
                cast(Context, self.scope),
                observation_dates=historical_dates,
                end=max(historical_dates),
            )
            historical = historical_values(history, historical_dates)
        known = {row.id for row in InstrumentRepository(self.session, self.scope).ordered()}
        current_values = _current_holdings(current) if current is not None else {}
        result: dict[tuple[UUID, date], Money | Unavailable] = {}
        for instrument_id in link_ids:
            for day in dates:
                value: Money | None | Unavailable = UNAVAILABLE
                if instrument_id in known:
                    if current is not None and day in current_dates:
                        value = current_values.get(instrument_id, Money(0, current.currency))
                    elif historical is not None:
                        value = historical.instrument(instrument_id, day)
                result[(instrument_id, day)] = UNAVAILABLE if value is None else value
        return result

    def describe(self, link_id: UUID) -> LinkTarget | None | Unavailable:
        row = InstrumentRepository(self.session, self.scope).get(link_id)
        if row is None:
            return None
        return LinkTarget(
            row.name,
            _base_currency(self.session, self.scope),
            f"/investments/instruments/{row.id}",
        )

    def choices(self) -> Sequence[LinkChoice]:
        ctx = cast(Context, self.scope)
        data = current_portfolio_data_in_session(self.session, ctx)
        held = {row.instrument_id for row in data.valuation.rows}
        return tuple(
            LinkChoice(row.id, row.name + (f" ({row.symbol})" if row.symbol else ""))
            for row in InstrumentRepository(self.session, self.scope).ordered()
            if row.id in held and row.archived_at is None
        )


def register_portfolio_resolvers() -> None:
    """Register both M6 link types; safe to call more than once."""
    labels = registry.labels()
    if "portfolio_asset_class" not in labels:
        registry.register(
            "portfolio_asset_class",
            _("Asset class"),
            lambda session, scope: _PortfolioAssetClassResolver(session, scope),
        )
    if "portfolio_holding" not in labels:
        registry.register(
            "portfolio_holding",
            _("Holding"),
            lambda session, scope: _PortfolioHoldingResolver(session, scope),
        )


def _investment_category_keys() -> frozenset[str]:
    return frozenset(name_key(gettext("Investments", locale)) for locale in SUPPORTED_LOCALES)


def _real_estate_keys() -> frozenset[str]:
    return frozenset(name_key(gettext("Real estate", locale)) for locale in SUPPORTED_LOCALES)


def real_estate_category_ids(session: Session, scope: WorkspaceScope) -> set[UUID]:
    keys = _real_estate_keys()
    return {
        row.id
        for row in NetWorthCategoryRepository(session, scope).ordered(Side.ASSET.value)
        if row.name_key in keys
    }


def _investments_category(session: Session, ctx: Context) -> tuple[NetWorthCategory, bool]:
    repository = NetWorthCategoryRepository(session, ctx)
    keys = _investment_category_keys()
    for row in repository.ordered(Side.ASSET.value):
        if row.archived_at is None and row.name_key in keys:
            return row, False
    name = gettext("Investments", ctx.workspace.locale)
    category = repository.create(
        name=name,
        name_key=name_key(name),
        side=Side.ASSET.value,
        color=PALETTE[0],
        sort_order=repository.next_sort_order(Side.ASSET.value),
    )
    AuditEventRepository(session, ctx).record(
        actor_user_id=ctx.user.id,
        action="networth_category.created",
        entity_type="networth_category",
        entity_id=str(category.id),
        ts=ctx.clock.now(),
        data={"name": name, "side": Side.ASSET.value},
    )
    return category, True


def create_networth_items_from_asset_classes(
    factory: sessionmaker[Session], ctx: Context
) -> CreatedLinks:
    """Create linked items for complete active classes with a value, skipping existing links."""
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> CreatedLinks:
        return _create_missing_links_in_session(session, ctx)

    return run_immediate(factory, write)


def _create_missing_links_in_session(session: Session, ctx: Context) -> CreatedLinks:
    """``create_networth_items_from_asset_classes`` inside the caller's write transaction.

    Role, lock, the current portfolio read, link creation and audit are the public helper's.
    """
    require_role_now(session, ctx, Role.EDITOR)
    CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
    data = current_portfolio_data_in_session(session, ctx)
    active = [row for row in data.classes if row.archived_at is None]
    eligible: list[AssetClass] = []
    skipped = 0
    for row in active:
        value = data.valuation.class_value(row.id)
        if value <= 0 or _class_missing(data, row.id):
            skipped += 1
            continue
        if (
            NetWorthItemRepository(session, ctx).active_linked("portfolio_asset_class", row.id)
            is not None
        ):
            skipped += 1
            continue
        eligible.append(row)
    if not eligible:
        return CreatedLinks((), skipped, False)
    category, created_category = _investments_category(session, ctx)
    created: list[str] = []
    for row in eligible:
        item = NetWorthItemRepository(session, ctx).create(
            side=Side.ASSET.value,
            category_id=category.id,
            name=row.name,
            currency=data.currency,
            valuation_mode="linked",
            link_type="portfolio_asset_class",
            link_id=row.id,
            include_in_net_worth=True,
            investable=row.investable,
            estimate_between=True,
        )
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="networth_item.created",
            entity_type="networth_item",
            entity_id=str(item.id),
            ts=ctx.clock.now(),
            data={
                "name": row.name,
                "side": Side.ASSET.value,
                "category": category.name,
                "currency": data.currency,
                "link_type": "portfolio_asset_class",
            },
        )
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="networth_item.linked",
            entity_type="networth_item",
            entity_id=str(item.id),
            ts=ctx.clock.now(),
            data={"link_type": "portfolio_asset_class", "target": row.name},
        )
        created.append(row.name)
    return CreatedLinks(tuple(created), skipped, created_category)


def portfolio_warnings(
    session: Session,
    ctx: Context,
    *,
    linked_items: Sequence[LinkedNetWorthItem],
    property_item_names: Sequence[str],
) -> tuple[PortfolioLinkWarning, ...]:
    """Current double-counting warnings; never blocks or changes a value."""
    if not linked_items and not property_item_names:
        return ()
    data = current_portfolio_data_in_session(session, ctx)
    class_ids = {item.link_id for item in linked_items if item.link_type == "portfolio_asset_class"}
    holding_ids = {item.link_id for item in linked_items if item.link_type == "portfolio_holding"}
    instruments = {row.id: row for row in data.instruments}
    held = {
        row.instrument_id
        for row in data.valuation.rows
        if row.value is not None and row.value.amount_minor > 0
    }
    warnings: list[PortfolioLinkWarning] = []
    for asset_class in data.classes:
        if asset_class.id not in class_ids:
            continue
        class_value = _current_class(data, asset_class.id)
        if class_value is None or class_value.amount_minor <= 0:
            continue
        held_names = tuple(
            instruments[instrument_id].name
            for instrument_id in sorted(holding_ids, key=str)
            if instrument_id in instruments
            and instrument_id in held
            and any(
                allocation.asset_class_id == asset_class.id
                for allocation in data.allocations
                if allocation.instrument_id == instrument_id
            )
        )
        if held_names:
            warnings.append(
                PortfolioLinkWarning(
                    code="class_holding",
                    class_name=asset_class.name,
                    holding_names=held_names,
                )
            )
    property_instruments = tuple(
        row.name
        for row in data.instruments
        if row.type == InstrumentType.REAL_ESTATE
        and row.archived_at is None
        and any(
            item.instrument_id == row.id and item.value is not None and item.value.amount_minor > 0
            for item in data.valuation.rows
        )
    )
    if property_item_names and property_instruments:
        warnings.append(
            PortfolioLinkWarning(
                code="legacy_property",
                item_names=tuple(property_item_names),
                instrument_names=property_instruments,
            )
        )
    return tuple(warnings)
