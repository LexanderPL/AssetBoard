"""Net worth (F5.1): categories, items and append-only manual valuations.

Everyone may read; writes need ``editor`` or ``owner``. Every write runs in one ``BEGIN
IMMEDIATE`` transaction and re-reads the actor's role with ``require_role_now``. Category writes
take the exclusive workspace lock first; item and valuation writes take the shared one, so a
category archive and an item write cannot interleave on PostgreSQL.

Values are always positive in the item's currency. A liability's value is the amount owed;
F5.2 subtracts liabilities instead of adding signed values. Non-base items are listed but not
summed until an FX service exists.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.engine import run_immediate
from assetboard.db.models.networth import ItemValuation, NetWorthCategory, NetWorthItem
from assetboard.db.repositories import (
    AuditEventRepository,
    CurrentWorkspaceRepository,
    ItemValuationRepository,
    NetWorthCategoryRepository,
    NetWorthItemRepository,
    NotFound,
)
from assetboard.db.repositories.base import WorkspaceScope
from assetboard.domain.categories import InvalidCategoryName, InvalidColor, clean_name
from assetboard.domain.money import Money, minor_unit_for
from assetboard.domain.networth import (
    BulkTooLarge,
    InvalidItemName,
    InvalidNotes,
    Side,
    TooManyBulkLines,
    ValuationPoint,
    category_color,
    category_direction,
    category_name_key,
    check_bulk_limits,
    clean_item_name,
    clean_notes,
    clean_valuation_note,
    current_valuation,
    duplicate_name_lines,
    history_points,
    normalized_bulk_text,
    ordered_valuations,
    seed_for,
    sparkline_polyline,
    split_bulk_lines,
    valid_day,
)
from assetboard.domain.networth import (
    move_category as move_in_order,
)
from assetboard.domain.networth_history import ItemHistory, ItemValue, values_at
from assetboard.domain.text import text_key
from assetboard.domain.transactions import EntryError
from assetboard.domain.transactions import amount_minor as positive_amount_minor
from assetboard.i18n import gettext
from assetboard.i18n.numbers import parse_amount
from assetboard.services.context import Context, Role, require_role, require_role_now
from assetboard.services.networth_links import (
    UNAVAILABLE,
    LinkOption,
    LinkTarget,
    registry,
)

__all__ = [
    "BulkLineView",
    "BulkPreview",
    "BulkRejected",
    "CategoryView",
    "GroupView",
    "ItemView",
    "NetWorthError",
    "NetWorthPage",
    "SideView",
    "UnlinkResult",
    "ValuationView",
    "add_valuation",
    "archive_category",
    "archive_item",
    "create_bulk",
    "create_category",
    "create_item",
    "current_values",
    "delete_category",
    "get_category",
    "get_item",
    "link_options_for_side",
    "list_networth",
    "load_item_histories",
    "move_category",
    "preview_bulk",
    "restore_category",
    "restore_item",
    "seed_categories",
    "seed_into",
    "unlink_item",
    "update_category",
    "update_item",
]

# Every write locks the workspace row first (docs/architecture.md, lock order).
DIRECTIONS = frozenset({"up", "down"})


class NetWorthError(Exception):
    code = "invalid"
    field = ""


class InvalidNetWorth(NetWorthError):
    def __init__(self, field: str, code: str) -> None:
        super().__init__(code)
        self.field = field
        self.code = code


class DuplicateCategoryName(NetWorthError):
    """Another non-archived category of the same side has this name (ignoring case)."""

    code = "duplicate_name"
    field = "name"


class CategoryArchived(NetWorthError):
    code = "archived"


class AlreadySeeded(NetWorthError):
    code = "already_seeded"


class ItemArchived(NetWorthError):
    code = "item_archived"


class BulkRejected(NetWorthError):
    code = "bulk_invalid"
    field = "bulk"

    def __init__(self, preview: BulkPreview) -> None:
        super().__init__(self.code)
        self.preview = preview


@dataclass(frozen=True, slots=True)
class ValuationView:
    id: UUID
    as_of: date
    value: Money
    note: str
    created_at: datetime
    future: bool


@dataclass(frozen=True, slots=True)
class ItemView:
    id: UUID
    side: str
    category_id: UUID
    category_name: str
    category_archived: bool
    name: str
    currency: str
    notes: str
    acquired_on: date | None
    include_in_net_worth: bool
    investable: bool
    archived: bool
    archived_at: datetime | None
    current: Money | None
    current_as_of: date | None
    valuations: tuple[ValuationView, ...]
    sparkline: str
    non_base: bool
    valuation_mode: str = "manual"
    link_type: str | None = None
    link_id: UUID | None = None
    link_label: str | None = None
    link_name: str | None = None
    link_url: str | None = None
    unavailable: bool = False


@dataclass(frozen=True, slots=True)
class UnlinkResult:
    item: ItemView
    value_recorded: bool


@dataclass(frozen=True, slots=True)
class CategoryView:
    id: UUID
    side: str
    name: str
    color: str
    sort_order: int
    archived: bool
    item_count: int

    @property
    def in_use(self) -> bool:
        return self.item_count > 0


@dataclass(frozen=True, slots=True)
class GroupView:
    category: CategoryView
    items: tuple[ItemView, ...]
    subtotal: Money | None
    has_non_base: bool
    has_excluded: bool
    has_no_value: bool


@dataclass(frozen=True, slots=True)
class SideView:
    side: Side
    groups: tuple[GroupView, ...]
    archived_groups: tuple[GroupView, ...]
    archived_items: tuple[ItemView, ...]
    categories: tuple[CategoryView, ...]
    archived_categories: tuple[CategoryView, ...]
    can_seed: bool


@dataclass(frozen=True, slots=True)
class NetWorthPage:
    asset: SideView
    liability: SideView

    def side_view(self, side: Side) -> SideView:
        return self.asset if side is Side.ASSET else self.liability


@dataclass(frozen=True, slots=True)
class BulkLineView:
    number: int
    name: str
    value: str
    category: str
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    value_minor: int | None = None
    category_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class BulkPreview:
    side: Side
    text: str
    lines: tuple[BulkLineView, ...]
    input_error: str | None = None

    @property
    def can_create(self) -> bool:
        return (
            self.input_error is None
            and bool(self.lines)
            and all(not line.errors for line in self.lines)
        )


def _sides(value: str) -> Side:
    try:
        return Side(value)
    except ValueError:
        raise InvalidNetWorth("side", "invalid_side") from None


def _category_view(category: NetWorthCategory, counts: Mapping[UUID, int]) -> CategoryView:
    return CategoryView(
        id=category.id,
        side=category.side,
        name=category.name,
        color=category.color,
        sort_order=category.sort_order,
        archived=category.archived_at is not None,
        item_count=counts.get(category.id, 0),
    )


def _valuation_points(valuations: Sequence[ItemValuation]) -> list[ValuationPoint]:
    return ordered_valuations(
        [
            ValuationPoint(
                id=valuation.id,
                as_of=valuation.as_of,
                created_at=valuation.created_at,
                value_minor=valuation.value_minor,
            )
            for valuation in valuations
        ]
    )


def _valuation_views(
    valuations: Sequence[ItemValuation], today: date
) -> tuple[tuple[ValuationView, ...], int | None, date | None, str]:
    rows = {valuation.id: valuation for valuation in valuations}
    points = _valuation_points(valuations)
    views = tuple(
        ValuationView(
            id=point.id,
            as_of=point.as_of,
            value=Money(point.value_minor, rows[point.id].currency),
            note=rows[point.id].note,
            created_at=point.created_at,
            future=point.as_of > today,
        )
        for point in reversed(points)
    )
    current = current_valuation(points, today)
    current_minor = current.value_minor if current is not None else None
    current_as_of = current.as_of if current is not None else None
    sparkline = sparkline_polyline([point.value_minor for point in history_points(points, today)])
    return views, current_minor, current_as_of, sparkline


@dataclass(slots=True)
class _LoadedRows:
    categories: list[NetWorthCategory]
    items: list[NetWorthItem]
    valuations: dict[UUID, list[ItemValuation]]


def _load_rows(
    session: Session,
    scope: WorkspaceScope,
    *,
    categories: Sequence[NetWorthCategory] | None = None,
    item_ids: Collection[UUID] | None = None,
) -> _LoadedRows:
    category_rows = (
        list(categories)
        if categories is not None
        else NetWorthCategoryRepository(session, scope).ordered()
    )
    repository = NetWorthItemRepository(session, scope)
    items = repository.by_ids(item_ids) if item_ids is not None else repository.ordered()
    valuations: dict[UUID, list[ItemValuation]] = {}
    for valuation in ItemValuationRepository(session, scope).for_items([item.id for item in items]):
        valuations.setdefault(valuation.item_id, []).append(valuation)
    return _LoadedRows(category_rows, items, valuations)


def _item_history(item: NetWorthItem, valuations: Sequence[ItemValuation]) -> ItemHistory:
    return ItemHistory(
        id=item.id,
        side=Side(item.side),
        category_id=item.category_id,
        currency=item.currency,
        include_in_net_worth=item.include_in_net_worth,
        archived_at=item.archived_at,
        valuations=tuple(_valuation_points(valuations)),
        valuation_mode=item.valuation_mode,
        link_type=item.link_type,
        link_id=item.link_id,
    )


def load_item_histories(
    session: Session,
    scope: WorkspaceScope,
    *,
    categories: Sequence[NetWorthCategory] | None = None,
    item_ids: Collection[UUID] | None = None,
) -> tuple[ItemHistory, ...]:
    """Every item (or only ``item_ids``) with its valuation history, loaded once."""
    rows = _load_rows(session, scope, categories=categories, item_ids=item_ids)
    return tuple(_item_history(item, rows.valuations.get(item.id, ())) for item in rows.items)


def _item_view_at(
    item: NetWorthItem,
    category: NetWorthCategory,
    valuations: Sequence[ItemValuation],
    value: ItemValue,
    *,
    as_of: date,
    base_currency: str,
    include_history: bool,
    link_target: LinkTarget | None = None,
) -> ItemView:
    if include_history:
        views, _current_minor, _current_as_of, sparkline = _valuation_views(valuations, as_of)
    else:
        views = ()
        sparkline = ""
    current = Money(value.value_minor, item.currency) if value.value_minor is not None else None
    return ItemView(
        id=item.id,
        side=item.side,
        category_id=item.category_id,
        category_name=category.name,
        category_archived=category.archived_at is not None,
        name=item.name,
        currency=item.currency,
        notes=item.notes,
        acquired_on=item.acquired_on,
        include_in_net_worth=item.include_in_net_worth,
        investable=item.investable,
        archived=value.archived,
        archived_at=value.archived_at,
        current=current,
        current_as_of=value.value_as_of,
        valuations=views,
        sparkline=sparkline,
        non_base=item.currency != base_currency,
        valuation_mode=item.valuation_mode,
        link_type=item.link_type,
        link_id=item.link_id,
        link_label=registry.label(item.link_type),
        link_name=link_target.label if link_target is not None else None,
        link_url=link_target.href if link_target is not None else None,
        unavailable=value.unavailable,
    )


def _link_targets(
    session: Session, scope: WorkspaceScope, histories: Sequence[ItemHistory]
) -> dict[UUID, LinkTarget]:
    """Describe every linked item once for display; unknown/unavailable targets are absent."""
    targets: dict[UUID, LinkTarget] = {}
    for item in histories:
        if item.valuation_mode != "linked" or item.link_type is None or item.link_id is None:
            continue
        target = registry.describe(session, scope, item.link_type, item.link_id)
        if isinstance(target, LinkTarget):
            targets[item.id] = target
    return targets


def current_values(
    session: Session,
    scope: WorkspaceScope,
    *,
    as_of: date,
    base_currency: str,
    timezone: str,
    include_history: bool = False,
    categories: Sequence[NetWorthCategory] | None = None,
    item_ids: Collection[UUID] | None = None,
) -> list[ItemView]:
    """Every item with its value effective on ``as_of``.

    The shared read path for the F5.1 lists (``include_history=True``), the F5.2 summary
    (default) and F5.3's month-end snapshots (any ``as_of``). A valuation dated after
    ``as_of`` is ignored even when it exists in the history. The archived flag follows the
    owner's F5.3 rule: an item is archived from its workspace-local archive date onwards.
    ``item_ids`` narrows the read to a specific item for a write response.
    """
    rows = _load_rows(session, scope, categories=categories, item_ids=item_ids)
    category_by_id = {category.id: category for category in rows.categories}
    histories = tuple(_item_history(item, rows.valuations.get(item.id, ())) for item in rows.items)
    resolved = registry.resolve_many(session, scope, histories, (as_of,))
    linked = {
        item.id: resolved.get((item.id, as_of), UNAVAILABLE)
        for item in histories
        if item.valuation_mode == "linked"
    }
    values = values_at(histories, as_of, timezone=timezone, linked=linked)
    targets = _link_targets(session, scope, histories)
    return [
        _item_view_at(
            item,
            category_by_id[item.category_id],
            rows.valuations.get(item.id, ()),
            value,
            as_of=as_of,
            base_currency=base_currency,
            include_history=include_history,
            link_target=targets.get(item.id),
        )
        for item, value in zip(rows.items, values, strict=True)
    ]


def _sort_items(items: Sequence[ItemView]) -> tuple[ItemView, ...]:
    return tuple(sorted(items, key=lambda item: (text_key(item.name), str(item.id))))


def _group_view(
    category: CategoryView,
    items: Sequence[ItemView],
    *,
    base_currency: str,
) -> GroupView:
    subtotal_minor = 0
    contributed = False
    has_non_base = False
    has_excluded = False
    has_no_value = False
    for item in items:
        if item.currency != base_currency:
            has_non_base = True
        if not item.include_in_net_worth:
            has_excluded = True
            continue
        if item.current is None:
            has_no_value = True
            continue
        if item.currency == base_currency:
            subtotal_minor += item.current.amount_minor
            contributed = True
    return GroupView(
        category=category,
        items=_sort_items(items),
        subtotal=Money(subtotal_minor, base_currency) if contributed else None,
        has_non_base=has_non_base,
        has_excluded=has_excluded,
        has_no_value=has_no_value,
    )


def _side_view(
    side: Side,
    categories: Sequence[NetWorthCategory],
    items: Sequence[ItemView],
    *,
    base_currency: str,
) -> SideView:
    counts: dict[UUID, int] = {}
    for item in items:
        counts[item.category_id] = counts.get(item.category_id, 0) + 1
    category_views = {category.id: _category_view(category, counts) for category in categories}
    active_items_by_category: dict[UUID, list[ItemView]] = {}
    archived_items: list[ItemView] = []
    for item in items:
        if item.archived:
            archived_items.append(item)
        else:
            active_items_by_category.setdefault(item.category_id, []).append(item)

    groups: list[GroupView] = []
    archived_groups: list[GroupView] = []
    for category in categories:
        if category.side != side.value:
            continue
        view = category_views[category.id]
        group_items = active_items_by_category.get(category.id, [])
        if not group_items:
            continue
        group = _group_view(view, group_items, base_currency=base_currency)
        (archived_groups if view.archived else groups).append(group)

    categories_view = tuple(
        category_views[category.id]
        for category in categories
        if category.side == side.value and category.archived_at is None
    )
    archived_categories_view = tuple(
        category_views[category.id]
        for category in categories
        if category.side == side.value and category.archived_at is not None
    )
    archived_items.sort(
        key=lambda item: (text_key(item.category_name), text_key(item.name), str(item.id))
    )
    return SideView(
        side=side,
        groups=tuple(groups),
        archived_groups=tuple(archived_groups),
        archived_items=tuple(archived_items),
        categories=categories_view,
        archived_categories=archived_categories_view,
        can_seed=not any(category.side == side.value for category in categories),
    )


# Reads (every member)


def list_networth(factory: sessionmaker[Session], ctx: Context) -> NetWorthPage:
    require_role(ctx, Role.VIEWER)
    today = ctx.clock.today(ctx.workspace.timezone)
    with factory() as session:
        base_currency = CurrentWorkspaceRepository(session, ctx).get().base_currency
        categories = NetWorthCategoryRepository(session, ctx).ordered()
        items = current_values(
            session,
            ctx,
            as_of=today,
            base_currency=base_currency,
            timezone=ctx.workspace.timezone,
            include_history=True,
            categories=categories,
        )
    return NetWorthPage(
        asset=_side_view(
            Side.ASSET,
            categories,
            items,
            base_currency=base_currency,
        ),
        liability=_side_view(
            Side.LIABILITY,
            categories,
            items,
            base_currency=base_currency,
        ),
    )


def _view_after_write(
    session: Session, ctx: Context, item_id: UUID, *, base_currency: str, today: date
) -> ItemView:
    values = current_values(
        session,
        ctx,
        as_of=today,
        base_currency=base_currency,
        timezone=ctx.workspace.timezone,
        include_history=True,
        item_ids=(item_id,),
    )
    for value in values:
        if value.id == item_id:
            return value
    raise NotFound


def get_item(factory: sessionmaker[Session], ctx: Context, item_id: UUID) -> ItemView:
    require_role(ctx, Role.VIEWER)
    today = ctx.clock.today(ctx.workspace.timezone)
    with factory() as session:
        base_currency = CurrentWorkspaceRepository(session, ctx).get().base_currency
        return _view_after_write(session, ctx, item_id, base_currency=base_currency, today=today)


def get_category(factory: sessionmaker[Session], ctx: Context, category_id: UUID) -> CategoryView:
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        category = NetWorthCategoryRepository(session, ctx).get_or_raise(category_id)
        counts = NetWorthItemRepository(session, ctx).counts_by_category()
    return _category_view(category, counts)


def link_options_for_side(
    factory: sessionmaker[Session], ctx: Context, *, side: str
) -> tuple[LinkOption, ...]:
    """The registered link targets for one side; empty while no resolver is registered."""
    require_role(ctx, Role.VIEWER)
    wanted = _sides(side)
    with factory() as session:
        return registry.options_for_side(session, ctx, wanted)


# Category writes (editors and owners)


def _lock(session: Session, ctx: Context, *, exclusive: bool) -> None:
    CurrentWorkspaceRepository(session, ctx).lock(exclusive=exclusive)


def _write[T](factory: sessionmaker[Session], operation: Callable[[Session], T]) -> T:
    try:
        return run_immediate(factory, operation)
    except IntegrityError as exc:
        if "name_key" in str(exc.orig) or "uq_networth_categories_active_name" in str(exc.orig):
            raise DuplicateCategoryName from exc
        raise


def _audit(
    session: Session,
    ctx: Context,
    action: str,
    entity_type: str,
    entity_id: UUID,
    data: Mapping[str, str | int | bool],
) -> None:
    AuditEventRepository(session, ctx).record(
        actor_user_id=ctx.user.id,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        ts=ctx.clock.now(),
        data=data,
    )


def _active(categories: Sequence[NetWorthCategory]) -> list[NetWorthCategory]:
    return [category for category in categories if category.archived_at is None]


def _ensure_unique(
    repository: NetWorthCategoryRepository,
    *,
    side: str,
    key: str,
    own_id: UUID | None = None,
) -> None:
    clash = repository.active_by_name_key(side=side, name_key=key)
    if clash is not None and clash.id != own_id:
        raise DuplicateCategoryName


def create_category(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    side: str,
    name: str,
    color: str,
) -> CategoryView:
    require_role(ctx, Role.EDITOR)
    wanted_side = _sides(side)
    try:
        clean = clean_name(name)
    except InvalidCategoryName:
        raise InvalidNetWorth("name", "invalid_name") from None
    try:
        wanted_color = category_color(color)
    except InvalidColor:
        raise InvalidNetWorth("color", "invalid_color") from None
    key = category_name_key(clean)

    def write(session: Session) -> CategoryView:
        require_role_now(session, ctx, Role.EDITOR)
        _lock(session, ctx, exclusive=True)
        repository = NetWorthCategoryRepository(session, ctx)
        _ensure_unique(repository, side=wanted_side.value, key=key)
        category = repository.create(
            name=clean,
            name_key=key,
            side=wanted_side.value,
            color=wanted_color,
            sort_order=repository.next_sort_order(wanted_side.value),
        )
        _audit(
            session,
            ctx,
            "networth_category.created",
            "networth_category",
            category.id,
            {"name": clean, "side": wanted_side.value},
        )
        return _category_view(category, {})

    return _write(factory, write)


def update_category(
    factory: sessionmaker[Session],
    ctx: Context,
    category_id: UUID,
    *,
    name: str,
    color: str,
) -> CategoryView:
    require_role(ctx, Role.EDITOR)
    try:
        clean = clean_name(name)
    except InvalidCategoryName:
        raise InvalidNetWorth("name", "invalid_name") from None
    try:
        wanted_color = category_color(color)
    except InvalidColor:
        raise InvalidNetWorth("color", "invalid_color") from None
    key = category_name_key(clean)

    def write(session: Session) -> CategoryView:
        require_role_now(session, ctx, Role.EDITOR)
        _lock(session, ctx, exclusive=True)
        repository = NetWorthCategoryRepository(session, ctx)
        category = repository.get_or_raise(category_id)
        if category.archived_at is not None:
            raise CategoryArchived
        if category.name_key != key:
            _ensure_unique(repository, side=category.side, key=key, own_id=category.id)
        changes = {
            field: value
            for field, value in (("name", clean), ("color", wanted_color))
            if getattr(category, field) != value
        }
        counts = NetWorthItemRepository(session, ctx).counts_by_category()
        if not changes:
            return _category_view(category, counts)
        if "name" in changes:
            changes["name_key"] = key
        repository.update(category.id, **changes)
        data: dict[str, str | int | bool] = {
            "name": clean,
            "fields": ",".join(sorted(field for field in changes if field != "name_key")),
        }
        _audit(
            session,
            ctx,
            "networth_category.updated",
            "networth_category",
            category.id,
            data,
        )
        return _category_view(category, counts)

    return _write(factory, write)


def move_category(
    factory: sessionmaker[Session], ctx: Context, category_id: UUID, direction: str
) -> None:
    """Swap an active category with its neighbour on the same side (not audited)."""
    require_role(ctx, Role.EDITOR)
    try:
        step = category_direction(direction)
    except ValueError:
        raise InvalidNetWorth("direction", "invalid_direction") from None

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        _lock(session, ctx, exclusive=True)
        repository = NetWorthCategoryRepository(session, ctx)
        category = repository.get_or_raise(category_id)
        if category.archived_at is not None:
            raise CategoryArchived
        siblings = _active(repository.ordered(category.side))
        for position, sibling in enumerate(move_in_order(siblings, category, step)):
            if sibling.sort_order != position:
                sibling.sort_order = position
        session.flush()

    run_immediate(factory, write)


def _archive(
    session: Session,
    ctx: Context,
    category: NetWorthCategory,
    *,
    in_use: bool,
) -> None:
    category.archived_at = ctx.clock.now()
    session.flush()
    _audit(
        session,
        ctx,
        "networth_category.archived",
        "networth_category",
        category.id,
        {"name": category.name, "in_use": in_use},
    )


def archive_category(factory: sessionmaker[Session], ctx: Context, category_id: UUID) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        _lock(session, ctx, exclusive=True)
        category = NetWorthCategoryRepository(session, ctx).get_or_raise(category_id)
        if category.archived_at is None:
            in_use = NetWorthItemRepository(session, ctx).counts_by_category().get(category.id, 0)
            _archive(session, ctx, category, in_use=in_use > 0)

    run_immediate(factory, write)


def restore_category(factory: sessionmaker[Session], ctx: Context, category_id: UUID) -> None:
    """Bring an archived category back, at the end of its side's list."""
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        _lock(session, ctx, exclusive=True)
        repository = NetWorthCategoryRepository(session, ctx)
        category = repository.get_or_raise(category_id)
        if category.archived_at is None:
            return
        _ensure_unique(repository, side=category.side, key=category.name_key)
        repository.update(
            category.id,
            archived_at=None,
            sort_order=repository.next_sort_order(category.side),
        )
        _audit(
            session,
            ctx,
            "networth_category.restored",
            "networth_category",
            category.id,
            {"name": category.name},
        )

    _write(factory, write)


def delete_category(
    factory: sessionmaker[Session], ctx: Context, category_id: UUID
) -> Literal["deleted", "archived"]:
    """Delete an unused category; a category with items is archived instead."""
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> Literal["deleted", "archived"]:
        require_role_now(session, ctx, Role.EDITOR)
        _lock(session, ctx, exclusive=True)
        repository = NetWorthCategoryRepository(session, ctx)
        category = repository.get_or_raise(category_id)
        in_use = NetWorthItemRepository(session, ctx).counts_by_category().get(category.id, 0)
        if in_use:
            if category.archived_at is None:
                _archive(session, ctx, category, in_use=True)
            return "archived"
        name, side = category.name, category.side
        repository.delete(category)
        _audit(
            session,
            ctx,
            "networth_category.deleted",
            "networth_category",
            category_id,
            {"name": name, "side": side},
        )
        return "deleted"

    return run_immediate(factory, write)


# Seeding


def seed_into(
    session: Session,
    scope: WorkspaceScope,
    *,
    locale: str,
    actor_user_id: UUID | None,
    actor_type: Literal["user", "cli", "system"],
    now: datetime,
    automatic: bool,
    sides: Sequence[Side] | None = None,
) -> int:
    """Create the suggested categories in ``scope``'s workspace, named in ``locale``.

    The caller's transaction must have checked that the selected side(s) have no categories.
    The rows and the ``networth_category.seeded`` event are written together. ``automatic`` is
    set only for workspace creation, matching ``categories.seed_into``; ``side`` is present when
    exactly one side is seeded.
    """
    wanted_sides = tuple(sides) if sides is not None else (Side.ASSET, Side.LIABILITY)
    repository = NetWorthCategoryRepository(session, scope)
    total = 0
    for side in wanted_sides:
        for position, seed in enumerate(seed_for(side)):
            name = clean_name(gettext(seed.name, locale))
            repository.create(
                name=name,
                name_key=category_name_key(name),
                side=side.value,
                color=seed.color,
                sort_order=position,
            )
            total += 1
    data: dict[str, str | int | bool] = {"count": total}
    if automatic:
        data["automatic"] = True
    if len(wanted_sides) == 1:
        data["side"] = wanted_sides[0].value
    AuditEventRepository(session, scope).record(
        actor_user_id=actor_user_id,
        actor_type=actor_type,
        action="networth_category.seeded",
        entity_type="workspace",
        entity_id=str(scope.workspace.id),
        ts=now,
        data=data,
    )
    return total


def seed_categories(factory: sessionmaker[Session], ctx: Context, *, side: str, locale: str) -> int:
    """The suggested categories for one empty side; never for a side with any category."""
    require_role(ctx, Role.EDITOR)
    wanted_side = _sides(side)

    def write(session: Session) -> int:
        require_role_now(session, ctx, Role.EDITOR)
        _lock(session, ctx, exclusive=True)
        if NetWorthCategoryRepository(session, ctx).count(wanted_side.value) > 0:
            raise AlreadySeeded
        return seed_into(
            session,
            ctx,
            locale=locale,
            actor_user_id=ctx.user.id,
            actor_type="user",
            now=ctx.clock.now(),
            automatic=False,
            sides=(wanted_side,),
        )

    try:
        return run_immediate(factory, write)
    except IntegrityError as exc:
        raise AlreadySeeded from exc


# Item writes


def _parse_decimal(value: str, locale: str) -> Decimal:
    try:
        return parse_amount(value, locale)
    except ValueError:
        raise InvalidNetWorth("value", "invalid_amount") from None


def _money(value: Decimal, currency: str) -> Money:
    """Exact minor units, using the ledger's rule: refuse extra decimals, never round."""
    if value < 0:
        raise InvalidNetWorth("value", "value_negative")
    if value == 0:
        return Money(0, currency)
    try:
        minor = positive_amount_minor(value, currency)
    except EntryError as exc:
        code = exc.code
        if code == "amount_not_positive":
            code = "value_negative"
        elif code not in {"too_many_decimals", "amount_too_large"}:
            code = "invalid_amount"
        raise InvalidNetWorth("value", code) from None
    return Money(minor, currency)


def _currency(value: str) -> str:
    code = value.strip().upper()
    try:
        minor_unit_for(code)
    except ValueError:
        raise InvalidNetWorth("currency", "invalid_currency") from None
    return code


def _date(value: date | None, field: str) -> date | None:
    if value is not None and not valid_day(value):
        raise InvalidNetWorth(field, "invalid_date")
    return value


def _item_name(value: str) -> str:
    try:
        return clean_item_name(value)
    except InvalidItemName:
        raise InvalidNetWorth("name", "invalid_name") from None


def _notes(value: str, field: str = "notes") -> str:
    try:
        return clean_notes(value)
    except InvalidNotes:
        raise InvalidNetWorth(field, "invalid_notes") from None


def _valuation_note(value: str) -> str:
    try:
        return clean_valuation_note(value)
    except InvalidNotes:
        raise InvalidNetWorth("valuation_note", "invalid_notes") from None


def _category_for_item(
    session: Session,
    ctx: Context,
    category_id: UUID,
    *,
    side: str,
    keep: UUID | None = None,
) -> NetWorthCategory:
    category = NetWorthCategoryRepository(session, ctx).get_or_raise(category_id)
    if category.side != side:
        raise InvalidNetWorth("category", "wrong_side")
    if category.archived_at is not None and category.id != keep:
        raise InvalidNetWorth("category", "category_archived")
    return category


def _link_unique_error(error: IntegrityError) -> bool:
    message = str(error.orig).lower()
    return "networth_items" in message and "link_type" in message and "link_id" in message


def create_item(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    locale: str,
    side: str,
    category_id: UUID,
    name: str,
    value: str = "",
    currency: str = "",
    notes: str = "",
    acquired_on: date | None = None,
    include_in_net_worth: bool = True,
    investable: bool = False,
    as_of: date | None = None,
    valuation_note: str = "",
    link_type: str | None = None,
    link_id: UUID | None = None,
) -> ItemView:
    require_role(ctx, Role.EDITOR)
    wanted_side = _sides(side)
    clean = _item_name(name)
    clean_notes_value = _notes(notes)
    wanted_acquired = _date(acquired_on, "acquired_on")
    wanted_link_type = link_type.strip() if link_type else None
    if (wanted_link_type is None) != (link_id is None):
        raise InvalidNetWorth("link", "invalid_link")
    parsed: Decimal | None = None
    clean_note = ""
    wanted_as_of: date | None = None
    wanted_currency: str | None = None
    if wanted_link_type is None:
        clean_note = _valuation_note(valuation_note)
        wanted_as_of = _date(as_of, "as_of")
        parsed = _parse_decimal(value, locale)
        wanted_currency = _currency(currency) if currency.strip() else None

    def write(session: Session) -> ItemView:
        require_role_now(session, ctx, Role.EDITOR)
        _lock(session, ctx, exclusive=False)
        workspace = CurrentWorkspaceRepository(session, ctx).get()
        category = _category_for_item(session, ctx, category_id, side=wanted_side.value)
        today = ctx.clock.today(ctx.workspace.timezone)
        if wanted_link_type is not None:
            assert link_id is not None
            resolver = registry.resolver_for(session, ctx, wanted_link_type)
            if resolver is None:
                raise InvalidNetWorth("link", "invalid_link")
            if resolver.side is not wanted_side:
                raise InvalidNetWorth("link", "link_wrong_side")
            target = resolver.describe(link_id)
            if not isinstance(target, LinkTarget) or not target.label:
                raise InvalidNetWorth("link", "invalid_link")
            try:
                item_currency = _currency(target.currency)
            except InvalidNetWorth:
                raise InvalidNetWorth("link", "invalid_link") from None
            item = NetWorthItemRepository(session, ctx).create(
                side=wanted_side.value,
                category_id=category.id,
                name=clean,
                currency=item_currency,
                notes=clean_notes_value,
                acquired_on=wanted_acquired,
                valuation_mode="linked",
                link_type=wanted_link_type,
                link_id=link_id,
                include_in_net_worth=include_in_net_worth,
                investable=investable,
            )
            _audit(
                session,
                ctx,
                "networth_item.created",
                "networth_item",
                item.id,
                {
                    "name": clean,
                    "side": wanted_side.value,
                    "category": category.name,
                    "currency": item_currency,
                    "link_type": wanted_link_type,
                },
            )
            _audit(
                session,
                ctx,
                "networth_item.linked",
                "networth_item",
                item.id,
                {"link_type": wanted_link_type, "target": target.label},
            )
            return _view_after_write(
                session,
                ctx,
                item.id,
                base_currency=workspace.base_currency,
                today=today,
            )

        assert parsed is not None
        item_currency = wanted_currency or workspace.base_currency
        money = _money(parsed, item_currency)
        effective_as_of = wanted_as_of or today
        item = NetWorthItemRepository(session, ctx).create(
            side=wanted_side.value,
            category_id=category.id,
            name=clean,
            currency=item_currency,
            notes=clean_notes_value,
            acquired_on=wanted_acquired,
            valuation_mode="manual",
            link_type=None,
            link_id=None,
            include_in_net_worth=include_in_net_worth,
            investable=investable,
        )
        ItemValuationRepository(session, ctx).create(
            item_id=item.id,
            as_of=effective_as_of,
            value_minor=money.amount_minor,
            currency=item_currency,
            note=clean_note,
        )
        _audit(
            session,
            ctx,
            "networth_item.created",
            "networth_item",
            item.id,
            {
                "name": clean,
                "side": wanted_side.value,
                "category": category.name,
                "currency": item_currency,
                "amount": _amount_text(money),
                "as_of": effective_as_of.isoformat(),
            },
        )
        return _view_after_write(
            session,
            ctx,
            item.id,
            base_currency=workspace.base_currency,
            today=today,
        )

    try:
        return run_immediate(factory, write)
    except IntegrityError as exc:
        if _link_unique_error(exc):
            raise InvalidNetWorth("link", "already_linked") from exc
        raise


def update_item(
    factory: sessionmaker[Session],
    ctx: Context,
    item_id: UUID,
    *,
    category_id: UUID,
    name: str,
    currency: str,
    notes: str = "",
    acquired_on: date | None = None,
    include_in_net_worth: bool = True,
    investable: bool = False,
) -> ItemView:
    require_role(ctx, Role.EDITOR)
    clean = _item_name(name)
    clean_notes_value = _notes(notes)
    wanted_acquired = _date(acquired_on, "acquired_on")
    wanted_currency = _currency(currency) if currency.strip() else None

    def write(session: Session) -> ItemView:
        require_role_now(session, ctx, Role.EDITOR)
        _lock(session, ctx, exclusive=False)
        workspace = CurrentWorkspaceRepository(session, ctx).get()
        item = NetWorthItemRepository(session, ctx).get_or_raise(item_id)
        if item.archived_at is not None:
            raise ItemArchived
        category = _category_for_item(
            session, ctx, category_id, side=item.side, keep=item.category_id
        )
        item_currency = wanted_currency or item.currency
        valuations = ItemValuationRepository(session, ctx).for_item(item.id)
        if item.valuation_mode == "linked" and item_currency != item.currency:
            raise InvalidNetWorth("currency", "linked_currency")
        if item_currency != item.currency and valuations:
            raise InvalidNetWorth("currency", "currency_locked")
        changes = {
            field: value
            for field, value in (
                ("category_id", category.id),
                ("name", clean),
                ("currency", item_currency),
                ("notes", clean_notes_value),
                ("acquired_on", wanted_acquired),
                ("include_in_net_worth", include_in_net_worth),
                ("investable", investable),
            )
            if getattr(item, field) != value
        }
        if changes:
            NetWorthItemRepository(session, ctx).update(item.id, **changes)
            _audit(
                session,
                ctx,
                "networth_item.updated",
                "networth_item",
                item.id,
                {
                    "name": clean,
                    "fields": ",".join(sorted(changes)),
                },
            )
        return _view_after_write(
            session,
            ctx,
            item.id,
            base_currency=workspace.base_currency,
            today=ctx.clock.today(ctx.workspace.timezone),
        )

    return run_immediate(factory, write)


def _archive_item(session: Session, ctx: Context, item: NetWorthItem) -> None:
    item.archived_at = ctx.clock.now()
    session.flush()
    _audit(
        session,
        ctx,
        "networth_item.archived",
        "networth_item",
        item.id,
        {"name": item.name, "side": item.side},
    )


def archive_item(factory: sessionmaker[Session], ctx: Context, item_id: UUID) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        _lock(session, ctx, exclusive=False)
        item = NetWorthItemRepository(session, ctx).get_or_raise(item_id)
        if item.archived_at is None:
            _archive_item(session, ctx, item)

    run_immediate(factory, write)


def restore_item(factory: sessionmaker[Session], ctx: Context, item_id: UUID) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        _lock(session, ctx, exclusive=False)
        repository = NetWorthItemRepository(session, ctx)
        item = repository.get_or_raise(item_id)
        if item.archived_at is None:
            return
        if (
            item.valuation_mode == "linked"
            and item.link_type is not None
            and item.link_id is not None
            and repository.active_linked(item.link_type, item.link_id) is not None
        ):
            raise InvalidNetWorth("link", "already_linked")
        item.archived_at = None
        session.flush()
        _audit(
            session,
            ctx,
            "networth_item.restored",
            "networth_item",
            item.id,
            {"name": item.name, "side": item.side},
        )

    try:
        run_immediate(factory, write)
    except IntegrityError as exc:
        if _link_unique_error(exc):
            raise InvalidNetWorth("link", "already_linked") from exc
        raise


def unlink_item(factory: sessionmaker[Session], ctx: Context, item_id: UUID) -> UnlinkResult:
    """Convert a linked item to manual; keep today's resolved value when it is available."""
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> UnlinkResult:
        require_role_now(session, ctx, Role.EDITOR)
        _lock(session, ctx, exclusive=False)
        workspace = CurrentWorkspaceRepository(session, ctx).get()
        repository = NetWorthItemRepository(session, ctx)
        item = repository.get_or_raise(item_id)
        if item.archived_at is not None:
            raise ItemArchived
        if item.valuation_mode != "linked" or item.link_type is None or item.link_id is None:
            raise InvalidNetWorth("link", "not_linked")
        today = ctx.clock.today(ctx.workspace.timezone)
        resolved = registry.resolve_one(
            session,
            ctx,
            link_type=item.link_type,
            link_id=item.link_id,
            currency=item.currency,
            as_of=today,
        )
        value_recorded = isinstance(resolved, Money)
        if value_recorded:
            ItemValuationRepository(session, ctx).create(
                item_id=item.id,
                as_of=today,
                value_minor=resolved.amount_minor,
                currency=item.currency,
                note="",
            )
        repository.update(
            item.id,
            valuation_mode="manual",
            link_type=None,
            link_id=None,
        )
        _audit(
            session,
            ctx,
            "networth_item.unlinked",
            "networth_item",
            item.id,
            {"value_recorded": value_recorded},
        )
        view = _view_after_write(
            session,
            ctx,
            item.id,
            base_currency=workspace.base_currency,
            today=today,
        )
        return UnlinkResult(view, value_recorded)

    return run_immediate(factory, write)


def add_valuation(
    factory: sessionmaker[Session],
    ctx: Context,
    item_id: UUID,
    *,
    locale: str,
    value: str,
    as_of: date | None = None,
    note: str = "",
) -> ItemView:
    require_role(ctx, Role.EDITOR)
    parsed = _parse_decimal(value, locale)
    clean_note = _valuation_note(note)
    wanted_as_of = _date(as_of, "as_of")

    def write(session: Session) -> ItemView:
        require_role_now(session, ctx, Role.EDITOR)
        _lock(session, ctx, exclusive=False)
        workspace = CurrentWorkspaceRepository(session, ctx).get()
        item = NetWorthItemRepository(session, ctx).get_or_raise(item_id)
        if item.archived_at is not None:
            raise ItemArchived
        if item.valuation_mode != "manual":
            raise InvalidNetWorth("valuation_mode", "linked_value_readonly")
        money = _money(parsed, item.currency)
        today = ctx.clock.today(ctx.workspace.timezone)
        effective_as_of = wanted_as_of or today
        ItemValuationRepository(session, ctx).create(
            item_id=item.id,
            as_of=effective_as_of,
            value_minor=money.amount_minor,
            currency=item.currency,
            note=clean_note,
        )
        _audit(
            session,
            ctx,
            "networth_item.valuation_added",
            "networth_item",
            item.id,
            {
                "as_of": effective_as_of.isoformat(),
                "amount": _amount_text(money),
                "currency": item.currency,
            },
        )
        return _view_after_write(
            session,
            ctx,
            item.id,
            base_currency=workspace.base_currency,
            today=today,
        )

    return run_immediate(factory, write)


def _amount_text(money: Money) -> str:
    return str(money.to_decimal())


# Bulk add


def _bulk_preview(
    session: Session,
    ctx: Context,
    *,
    side: Side,
    text: str,
    locale: str,
) -> BulkPreview:
    workspace = CurrentWorkspaceRepository(session, ctx).get()
    base_currency = workspace.base_currency
    categories = NetWorthCategoryRepository(session, ctx).ordered(side.value)
    active = {
        category.name_key: category for category in categories if category.archived_at is None
    }
    archived = {category.name_key for category in categories if category.archived_at is not None}
    existing_names = {
        text_key(item.name)
        for item in NetWorthItemRepository(session, ctx).ordered(side.value, include_archived=False)
    }
    raw_lines, problems = split_bulk_lines(text)
    duplicates = duplicate_name_lines(raw_lines)
    lines: list[BulkLineView] = [
        BulkLineView(number=problem.number, name="", value="", category="", errors=(problem.code,))
        for problem in problems
    ]
    for raw in raw_lines:
        errors: list[str] = []
        value_minor: int | None = None
        try:
            parsed = parse_amount(raw.value, locale)
            money = _money(parsed, base_currency)
            value_minor = money.amount_minor
        except ValueError:
            errors.append("invalid_amount")
        except InvalidNetWorth as exc:
            errors.append(exc.code)
        category = active.get(category_name_key(raw.category))
        category_id: UUID | None = None
        if category is not None:
            category_id = category.id
        elif category_name_key(raw.category) in archived:
            errors.append("category_archived")
        else:
            errors.append("category_unknown")
        warnings: list[str] = []
        key = text_key(raw.name)
        if raw.number in duplicates:
            warnings.append("duplicate_name")
        if key in existing_names:
            warnings.append("existing_name")
        lines.append(
            BulkLineView(
                number=raw.number,
                name=raw.name,
                value=raw.value,
                category=raw.category,
                errors=tuple(errors),
                warnings=tuple(warnings),
                value_minor=value_minor,
                category_id=category_id,
            )
        )
    lines.sort(key=lambda line: line.number)
    input_error = None if normalized_bulk_text(text).strip() else "empty"
    return BulkPreview(
        side=side,
        text=text,
        lines=tuple(lines),
        input_error=input_error,
    )


def preview_bulk(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    side: str,
    text: str,
    locale: str,
) -> BulkPreview:
    require_role(ctx, Role.EDITOR)
    wanted_side = _sides(side)
    try:
        check_bulk_limits(text)
    except BulkTooLarge:
        raise InvalidNetWorth("bulk", "bulk_too_large") from None
    except TooManyBulkLines:
        raise InvalidNetWorth("bulk", "too_many_lines") from None
    with factory() as session:
        return _bulk_preview(session, ctx, side=wanted_side, text=text, locale=locale)


def create_bulk(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    side: str,
    text: str,
    locale: str,
) -> int:
    require_role(ctx, Role.EDITOR)
    wanted_side = _sides(side)
    try:
        check_bulk_limits(text)
    except BulkTooLarge:
        raise InvalidNetWorth("bulk", "bulk_too_large") from None
    except TooManyBulkLines:
        raise InvalidNetWorth("bulk", "too_many_lines") from None

    def write(session: Session) -> int:
        require_role_now(session, ctx, Role.EDITOR)
        _lock(session, ctx, exclusive=False)
        workspace = CurrentWorkspaceRepository(session, ctx).get()
        preview = _bulk_preview(session, ctx, side=wanted_side, text=text, locale=locale)
        if not preview.can_create:
            raise BulkRejected(preview)
        today = ctx.clock.today(ctx.workspace.timezone)
        items = NetWorthItemRepository(session, ctx)
        valuations = ItemValuationRepository(session, ctx)
        for line in preview.lines:
            if line.value_minor is None or line.category_id is None:
                raise BulkRejected(preview)
            item = items.create(
                side=wanted_side.value,
                category_id=line.category_id,
                name=line.name,
                currency=workspace.base_currency,
                notes="",
                acquired_on=None,
                valuation_mode="manual",
                link_type=None,
                link_id=None,
                include_in_net_worth=True,
                investable=False,
            )
            valuations.create(
                item_id=item.id,
                as_of=today,
                value_minor=line.value_minor,
                currency=workspace.base_currency,
                note="",
            )
        _audit(
            session,
            ctx,
            "networth_item.bulk_created",
            "workspace",
            ctx.workspace.id,
            {"side": wanted_side.value, "count": len(preview.lines)},
        )
        return len(preview.lines)

    return run_immediate(factory, write)
