"""Investments (F6.1): asset classes, instruments and allocation weights.

Everyone in the workspace may read; writes need ``editor``. Every write re-reads the actor's
role inside the transaction (``require_role_now``). Asset-class writes take the exclusive
workspace lock because they touch display order and the active-name rule; instrument writes take
the shared lock and lock the instrument row while replacing its allocations, so two editors
cannot leave a half-saved allocation set.

Allocation rows are the current state, not history: saving the instrument form replaces them.
The service enforces at least one class and weights totalling exactly 10 000 basis points.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.engine import run_immediate
from assetboard.db.models.investments import AssetClass, Instrument, InstrumentAllocation
from assetboard.db.repositories import (
    AssetClassRepository,
    AuditEventRepository,
    CurrentWorkspaceRepository,
    InstrumentAllocationRepository,
    InstrumentRepository,
    InvestmentTransactionRepository,
    NotFound,
    PriceSourceConfigRepository,
)
from assetboard.db.repositories.base import WorkspaceScope
from assetboard.domain.categories import Direction, move, name_key
from assetboard.domain.instruments import (
    ASSET_CLASS_SEED,
    TYPE_DEFAULTS,
    TYPE_LABELS,
    VALUATION_SOURCE_LABELS,
    InstrumentType,
    InvalidAllocation,
    InvalidAssetClassName,
    InvalidInstrumentName,
    InvalidQuoteCurrency,
    InvalidSymbol,
    InvalidUnitLabel,
    ValuationSource,
    clean_asset_class_name,
    clean_colour,
    clean_instrument_name,
    clean_quote_currency,
    clean_symbol,
    clean_unit_label,
    parse_instrument_type,
    validate_allocation,
)
from assetboard.domain.providers import (
    COINGECKO_KEY,
    InvalidProviderSymbol,
    ProviderDefinition,
    clean_provider_symbol,
    provider_definition,
    provider_supports_instrument,
)
from assetboard.i18n import gettext
from assetboard.services.context import Context, Role, require_role, require_role_now

__all__ = [
    "AllocationView",
    "AssetClassView",
    "AlreadySeeded",
    "AssetClassArchived",
    "DuplicateAssetClassName",
    "DuplicateInstrumentName",
    "InstrumentArchived",
    "InstrumentView",
    "InvestmentError",
    "InvestmentPage",
    "AllocationRejected",
    "archive_asset_class",
    "archive_instrument",
    "create_asset_class",
    "create_instrument",
    "get_instrument",
    "investment_page",
    "move_asset_class",
    "restore_asset_class",
    "restore_instrument",
    "seed_asset_classes",
    "seed_into",
    "update_asset_class",
    "update_instrument",
]

DIRECTIONS: frozenset[str] = frozenset({"up", "down"})


class InvestmentError(Exception):
    code = "invalid"
    field = ""


class InvalidInvestment(InvestmentError):
    def __init__(self, field: str, code: str) -> None:
        super().__init__(code)
        self.field = field
        self.code = code


class DuplicateAssetClassName(InvestmentError):
    code = "duplicate_name"
    field = "name"


class DuplicateInstrumentName(InvestmentError):
    code = "duplicate_name"
    field = "name"


class AssetClassArchived(InvestmentError):
    code = "archived"
    field = "class"


class InstrumentArchived(InvestmentError):
    code = "archived"
    field = "instrument"


class AlreadySeeded(InvestmentError):
    code = "already_seeded"


class AllocationRejected(InvestmentError):
    field = "allocation"

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class AssetClassView:
    id: UUID
    name: str
    investable: bool
    color: str
    sort_order: int
    archived: bool


@dataclass(frozen=True, slots=True)
class AllocationView:
    asset_class_id: UUID
    name: str
    color: str
    weight_bp: int
    archived_class: bool


@dataclass(frozen=True, slots=True)
class InstrumentView:
    id: UUID
    name: str
    symbol: str | None
    type: str
    type_label: str
    quote_currency: str
    unit_label: str
    valuation_source: str
    valuation_source_label: str
    archived: bool
    allocations: tuple[AllocationView, ...]
    terms_required: bool
    has_transactions: bool = False
    provider_key: str | None = None
    provider_symbol: str | None = None
    provider_label: str | None = None

    @property
    def allocation_total_bp(self) -> int:
        return sum(allocation.weight_bp for allocation in self.allocations)


@dataclass(frozen=True, slots=True)
class ProviderChoice:
    definition: ProviderDefinition
    enabled: bool


@dataclass(frozen=True, slots=True)
class InvestmentPage:
    classes: tuple[AssetClassView, ...]
    archived_classes: tuple[AssetClassView, ...]
    instruments: tuple[InstrumentView, ...]
    archived_instruments: tuple[InstrumentView, ...]
    provider_choices: tuple[ProviderChoice, ...] = ()
    coingecko_enabled: bool = False

    @property
    def can_seed(self) -> bool:
        """The seed offer needs no active class; archived classes may exist."""
        return not self.classes


def _class_view(row: AssetClass) -> AssetClassView:
    return AssetClassView(
        id=row.id,
        name=row.name,
        investable=row.investable,
        color=row.color,
        sort_order=row.sort_order,
        archived=row.archived_at is not None,
    )


def _allocation_view(
    row: InstrumentAllocation, classes: Mapping[UUID, AssetClass]
) -> AllocationView:
    asset_class = classes[row.asset_class_id]
    return AllocationView(
        asset_class_id=asset_class.id,
        name=asset_class.name,
        color=asset_class.color,
        weight_bp=row.weight_bp,
        archived_class=asset_class.archived_at is not None,
    )


def _instrument_view(
    row: Instrument,
    classes: Mapping[UUID, AssetClass],
    allocations: Sequence[InstrumentAllocation],
    locale: str,
    *,
    has_transactions: bool = False,
) -> InstrumentView:
    instrument_type = InstrumentType(row.type)
    source = ValuationSource(row.valuation_source)
    ordered = sorted(
        allocations,
        key=lambda allocation: (
            -allocation.weight_bp,
            classes[allocation.asset_class_id].name_key,
            str(allocation.asset_class_id),
        ),
    )
    provider = provider_definition(row.provider_key) if row.provider_key else None
    return InstrumentView(
        id=row.id,
        name=row.name,
        symbol=row.symbol,
        type=row.type,
        type_label=gettext(TYPE_LABELS[instrument_type], locale),
        quote_currency=row.quote_currency,
        unit_label=row.unit_label,
        valuation_source=row.valuation_source,
        valuation_source_label=gettext(VALUATION_SOURCE_LABELS[source], locale),
        archived=row.archived_at is not None,
        allocations=tuple(_allocation_view(allocation, classes) for allocation in ordered),
        terms_required=instrument_type is InstrumentType.BOND_MODEL,
        has_transactions=has_transactions,
        provider_key=row.provider_key,
        provider_symbol=row.provider_symbol,
        provider_label=provider.label if provider is not None else row.provider_key,
    )


def _page(
    classes: Sequence[AssetClass],
    instruments: Sequence[Instrument],
    allocations: Sequence[InstrumentAllocation],
    locale: str,
    locked_instruments: Collection[UUID] = (),
    provider_choices: tuple[ProviderChoice, ...] = (),
    coingecko_enabled: bool = False,
) -> InvestmentPage:
    classes_by_id = {row.id: row for row in classes}
    by_instrument: dict[UUID, list[InstrumentAllocation]] = {}
    for allocation in allocations:
        by_instrument.setdefault(allocation.instrument_id, []).append(allocation)
    class_views = [_class_view(row) for row in classes]
    instrument_views = [
        _instrument_view(
            row,
            classes_by_id,
            by_instrument.get(row.id, ()),
            locale,
            has_transactions=row.id in locked_instruments,
        )
        for row in instruments
    ]
    return InvestmentPage(
        classes=tuple(view for view in class_views if not view.archived),
        archived_classes=tuple(view for view in class_views if view.archived),
        instruments=tuple(view for view in instrument_views if not view.archived),
        archived_instruments=tuple(view for view in instrument_views if view.archived),
        provider_choices=provider_choices,
        coingecko_enabled=coingecko_enabled,
    )


def investment_page(factory: sessionmaker[Session], ctx: Context, *, locale: str) -> InvestmentPage:
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        classes = AssetClassRepository(session, ctx).ordered()
        instruments = InstrumentRepository(session, ctx).ordered()
        allocations = InstrumentAllocationRepository(session, ctx).list()
        locked = InvestmentTransactionRepository(session, ctx).instrument_ids()
        configs = {
            row.provider_key: row for row in PriceSourceConfigRepository(session, ctx).list()
        }
        choices = tuple(
            ProviderChoice(
                definition,
                bool(configs.get(definition.key) and configs[definition.key].enabled),
            )
            for definition in _price_provider_definitions()
        )
        coingecko = bool(configs.get(COINGECKO_KEY) is not None and configs[COINGECKO_KEY].enabled)
        return _page(
            classes,
            instruments,
            allocations,
            locale,
            locked,
            provider_choices=choices,
            coingecko_enabled=coingecko,
        )


def get_instrument(
    factory: sessionmaker[Session], ctx: Context, instrument_id: UUID, *, locale: str
) -> InstrumentView:
    page = investment_page(factory, ctx, locale=locale)
    for instrument in (*page.instruments, *page.archived_instruments):
        if instrument.id == instrument_id:
            return instrument
    raise NotFound


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


def _write[T](
    factory: sessionmaker[Session],
    operation: Callable[[Session], T],
    *,
    duplicate: InvestmentError | None = None,
) -> T:
    try:
        return run_immediate(factory, operation)
    except IntegrityError as exc:
        message = str(exc.orig)
        if duplicate is not None and ("name_key" in message or "active_name" in message):
            raise duplicate from exc
        raise


def _lock(session: Session, ctx: Context, *, exclusive: bool) -> None:
    CurrentWorkspaceRepository(session, ctx).lock(exclusive=exclusive)


def _price_provider_definitions() -> tuple[ProviderDefinition, ...]:
    from assetboard.domain.providers import PROVIDERS

    return tuple(definition for definition in PROVIDERS.values() if definition.kind != "fx")


def _active_classes(repository: AssetClassRepository) -> list[AssetClass]:
    return [row for row in repository.ordered() if row.archived_at is None]


def _next_sort_order(repository: AssetClassRepository) -> int:
    return repository.next_sort_order()


def _ensure_unique_class(
    repository: AssetClassRepository, *, key: str, own_id: UUID | None = None
) -> None:
    clash = repository.active_by_name_key(key)
    if clash is not None and clash.id != own_id:
        raise DuplicateAssetClassName


def _ensure_unique_instrument(
    repository: InstrumentRepository, *, key: str, own_id: UUID | None = None
) -> None:
    clash = repository.active_by_name_key(key)
    if clash is not None and clash.id != own_id:
        raise DuplicateInstrumentName


def create_asset_class(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    name: str,
    color: str,
    investable: bool,
) -> AssetClassView:
    require_role(ctx, Role.EDITOR)
    try:
        clean = clean_asset_class_name(name)
    except InvalidAssetClassName:
        raise InvalidInvestment("name", "invalid_name") from None
    try:
        colour = clean_colour(color)
    except ValueError:
        raise InvalidInvestment("color", "invalid_color") from None
    key = name_key(clean)

    def write(session: Session) -> AssetClassView:
        require_role_now(session, ctx, Role.EDITOR)
        _lock(session, ctx, exclusive=True)
        repository = AssetClassRepository(session, ctx)
        _ensure_unique_class(repository, key=key)
        row = repository.create(
            name=clean,
            name_key=key,
            investable=investable,
            color=colour,
            sort_order=_next_sort_order(repository),
        )
        _audit(
            session,
            ctx,
            "asset_class.created",
            "asset_class",
            row.id,
            {"name": clean, "investable": investable},
        )
        return _class_view(row)

    return _write(factory, write, duplicate=DuplicateAssetClassName())


def update_asset_class(
    factory: sessionmaker[Session],
    ctx: Context,
    class_id: UUID,
    *,
    name: str,
    color: str,
    investable: bool,
) -> AssetClassView:
    require_role(ctx, Role.EDITOR)
    try:
        clean = clean_asset_class_name(name)
    except InvalidAssetClassName:
        raise InvalidInvestment("name", "invalid_name") from None
    try:
        colour = clean_colour(color)
    except ValueError:
        raise InvalidInvestment("color", "invalid_color") from None
    key = name_key(clean)

    def write(session: Session) -> AssetClassView:
        require_role_now(session, ctx, Role.EDITOR)
        _lock(session, ctx, exclusive=True)
        repository = AssetClassRepository(session, ctx)
        row = repository.get_or_raise(class_id)
        if row.archived_at is not None:
            raise AssetClassArchived
        if row.name_key != key:
            _ensure_unique_class(repository, key=key, own_id=row.id)
        changes: dict[str, object] = {
            field: value
            for field, value in (
                ("name", clean),
                ("investable", investable),
                ("color", colour),
            )
            if getattr(row, field) != value
        }
        if not changes:
            return _class_view(row)
        if "name" in changes:
            changes["name_key"] = key
        if "provider_key" in changes or "provider_symbol" in changes:
            changes["provider_backfilled_at"] = None
        repository.update(row.id, **changes)
        _audit(
            session,
            ctx,
            "asset_class.updated",
            "asset_class",
            row.id,
            {
                "name": clean,
                "fields": ",".join(sorted(f for f in changes if f != "name_key")),
            },
        )
        return _class_view(row)

    return _write(factory, write, duplicate=DuplicateAssetClassName())


def archive_asset_class(factory: sessionmaker[Session], ctx: Context, class_id: UUID) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        _lock(session, ctx, exclusive=True)
        repository = AssetClassRepository(session, ctx)
        row = repository.get_or_raise(class_id)
        if row.archived_at is not None:
            return
        repository.update(row.id, archived_at=ctx.clock.now())
        _audit(
            session,
            ctx,
            "asset_class.archived",
            "asset_class",
            row.id,
            {"name": row.name},
        )

    run_immediate(factory, write)


def restore_asset_class(factory: sessionmaker[Session], ctx: Context, class_id: UUID) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        _lock(session, ctx, exclusive=True)
        repository = AssetClassRepository(session, ctx)
        row = repository.get_or_raise(class_id)
        if row.archived_at is None:
            return
        _ensure_unique_class(repository, key=row.name_key)
        repository.update(
            row.id,
            archived_at=None,
            sort_order=_next_sort_order(repository),
        )
        _audit(
            session,
            ctx,
            "asset_class.restored",
            "asset_class",
            row.id,
            {"name": row.name},
        )

    _write(factory, write, duplicate=DuplicateAssetClassName())


def move_asset_class(
    factory: sessionmaker[Session], ctx: Context, class_id: UUID, direction: str
) -> None:
    """Swap an active class with its neighbour (not audited, like category reordering)."""
    require_role(ctx, Role.EDITOR)
    if direction not in DIRECTIONS:
        raise InvalidInvestment("direction", "invalid_direction")
    step: Direction = "up" if direction == "up" else "down"

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        _lock(session, ctx, exclusive=True)
        repository = AssetClassRepository(session, ctx)
        row = repository.get_or_raise(class_id)
        if row.archived_at is not None:
            raise AssetClassArchived
        siblings = _active_classes(repository)
        for position, sibling in enumerate(move(siblings, row, step)):
            if sibling.sort_order != position:
                sibling.sort_order = position
        session.flush()

    run_immediate(factory, write)


def seed_into(
    session: Session,
    scope: WorkspaceScope,
    *,
    locale: str,
    actor_user_id: UUID | None,
    actor_type: Literal["user", "cli", "system"],
    now: datetime,
    automatic: bool,
) -> int:
    """Create the suggested asset classes in ``scope``'s workspace, named in ``locale``.

    Every new workspace gets the set automatically; an older workspace with no active class can
    add it with the page button. Recorded as ``asset_class.seeded`` in that workspace.
    """
    repository = AssetClassRepository(session, scope)
    for position, seed in enumerate(ASSET_CLASS_SEED):
        name = clean_asset_class_name(gettext(seed.name, locale))
        repository.create(
            name=name,
            name_key=name_key(name),
            investable=seed.investable,
            color=seed.color,
            sort_order=position,
        )
    data: dict[str, str | int | bool] = {"count": len(ASSET_CLASS_SEED)}
    if automatic:
        data["automatic"] = True
    AuditEventRepository(session, scope).record(
        actor_user_id=actor_user_id,
        actor_type=actor_type,
        action="asset_class.seeded",
        entity_type="workspace",
        entity_id=str(scope.workspace.id),
        ts=now,
        data=data,
    )
    return len(ASSET_CLASS_SEED)


def seed_asset_classes(factory: sessionmaker[Session], ctx: Context, *, locale: str) -> int:
    """The suggested classes, named in ``locale``; only when no active class exists."""
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> int:
        require_role_now(session, ctx, Role.EDITOR)
        _lock(session, ctx, exclusive=True)
        if AssetClassRepository(session, ctx).active_count() > 0:
            raise AlreadySeeded
        return seed_into(
            session,
            ctx,
            locale=locale,
            actor_user_id=ctx.user.id,
            actor_type="user",
            now=ctx.clock.now(),
            automatic=False,
        )

    try:
        return run_immediate(factory, write)
    except IntegrityError as exc:
        raise AlreadySeeded from exc


def _instrument_values(
    *,
    name: str,
    symbol: str,
    type_value: str,
    quote_currency: str,
    unit_label: str,
    locale: str,
    provider_key: str = "",
    provider_symbol: str = "",
) -> tuple[
    str,
    str,
    str | None,
    InstrumentType,
    str,
    str,
    ValuationSource,
    str | None,
    str | None,
]:
    try:
        instrument_type = parse_instrument_type(type_value)
    except ValueError:
        raise InvalidInvestment("type", "invalid_type") from None
    try:
        clean_name_value = clean_instrument_name(name)
    except InvalidInstrumentName:
        raise InvalidInvestment("name", "invalid_name") from None
    try:
        symbol_value = clean_symbol(symbol)
    except InvalidSymbol:
        raise InvalidInvestment("symbol", "invalid_symbol") from None
    try:
        currency = clean_quote_currency(quote_currency)
    except InvalidQuoteCurrency:
        raise InvalidInvestment("quote_currency", "invalid_currency") from None
    defaults = TYPE_DEFAULTS[instrument_type]
    if defaults.unit_from_quote_currency:
        unit = currency
    elif unit_label.strip():
        try:
            unit = clean_unit_label(
                unit_label, instrument_type=instrument_type, quote_currency=currency
            )
        except InvalidUnitLabel:
            raise InvalidInvestment("unit_label", "invalid_unit_label") from None
    else:
        unit = gettext(defaults.unit_label, locale)
    source = defaults.valuation_source
    provider_key_value: str | None = None
    provider_symbol_value: str | None = None
    submitted_key = provider_key.strip().lower()
    submitted_symbol = provider_symbol.strip()
    if instrument_type not in {InstrumentType.CURRENCY, InstrumentType.BOND_MODEL}:
        if submitted_key:
            definition = provider_definition(submitted_key)
            if definition is None or definition.kind == "fx":
                raise InvalidInvestment("provider_key", "invalid_provider")
            try:
                provider_symbol_value = clean_provider_symbol(definition, submitted_symbol)
            except InvalidProviderSymbol:
                raise InvalidInvestment("provider_symbol", "invalid_provider_symbol") from None
            if provider_symbol_value is None and definition.symbol_required:
                raise InvalidInvestment("provider_symbol", "provider_symbol_required")
            if not provider_supports_instrument(
                definition,
                type=instrument_type.value,
                quote_currency=currency,
                provider_symbol=provider_symbol_value,
            ):
                raise InvalidInvestment("provider_key", "provider_unsupported")
            provider_key_value = definition.key
            source = ValuationSource.PROVIDER
        elif submitted_symbol:
            raise InvalidInvestment("provider_key", "invalid_provider")
    return (
        clean_name_value,
        name_key(clean_name_value),
        symbol_value,
        instrument_type,
        currency,
        unit,
        source,
        provider_key_value,
        provider_symbol_value,
    )


def _allocation_rows(
    submitted: Sequence[tuple[UUID, int]],
    classes: Mapping[UUID, AssetClass],
    *,
    existing: set[UUID],
) -> list[tuple[UUID, int]]:
    try:
        validate_allocation(submitted)
    except InvalidAllocation as exc:
        raise AllocationRejected(exc.code) from None
    for class_id, _weight in submitted:
        asset_class = classes.get(class_id)
        if asset_class is None:
            raise AllocationRejected("class_unknown")
        if asset_class.archived_at is not None and class_id not in existing:
            raise AllocationRejected("class_archived")
    return list(submitted)


def create_instrument(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    name: str,
    symbol: str,
    type: str,
    quote_currency: str,
    unit_label: str,
    allocations: Sequence[tuple[UUID, int]],
    locale: str,
    provider_key: str = "",
    provider_symbol: str = "",
) -> InstrumentView:
    require_role(ctx, Role.EDITOR)
    (
        clean,
        key,
        symbol_value,
        instrument_type,
        currency,
        unit,
        source,
        provider_key_value,
        provider_symbol_value,
    ) = _instrument_values(
        name=name,
        symbol=symbol,
        type_value=type,
        quote_currency=quote_currency,
        unit_label=unit_label,
        locale=locale,
        provider_key=provider_key,
        provider_symbol=provider_symbol,
    )

    def write(session: Session) -> InstrumentView:
        require_role_now(session, ctx, Role.EDITOR)
        _lock(session, ctx, exclusive=False)
        instrument_repository = InstrumentRepository(session, ctx)
        _ensure_unique_instrument(instrument_repository, key=key)
        if provider_key_value is not None:
            config = PriceSourceConfigRepository(session, ctx).for_provider(provider_key_value)
            if config is None or not config.enabled:
                raise InvalidInvestment("provider_key", "provider_disabled")
        classes = {row.id: row for row in AssetClassRepository(session, ctx).ordered()}
        rows = _allocation_rows(allocations, classes, existing=set())
        row = instrument_repository.create(
            name=clean,
            name_key=key,
            symbol=symbol_value,
            type=instrument_type.value,
            quote_currency=currency,
            unit_label=unit,
            valuation_source=source.value,
            provider_key=provider_key_value,
            provider_symbol=provider_symbol_value,
        )
        allocation_repository = InstrumentAllocationRepository(session, ctx)
        for class_id, weight_bp in rows:
            allocation_repository.create(
                instrument_id=row.id,
                asset_class_id=class_id,
                weight_bp=weight_bp,
            )
        _audit(
            session,
            ctx,
            "instrument.created",
            "instrument",
            row.id,
            {
                "name": clean,
                "type": instrument_type.value,
                "currency": currency,
            },
        )
        _audit(
            session,
            ctx,
            "instrument_allocation.updated",
            "instrument",
            row.id,
            {"count": len(rows)},
        )
        return _instrument_view(
            row,
            classes,
            allocation_repository.for_instrument(row.id),
            locale,
        )

    return _write(factory, write, duplicate=DuplicateInstrumentName())


def update_instrument(
    factory: sessionmaker[Session],
    ctx: Context,
    instrument_id: UUID,
    *,
    name: str,
    symbol: str,
    type: str,
    quote_currency: str,
    unit_label: str,
    allocations: Sequence[tuple[UUID, int]],
    locale: str,
    provider_key: str = "",
    provider_symbol: str = "",
) -> InstrumentView:
    require_role(ctx, Role.EDITOR)
    (
        clean,
        key,
        symbol_value,
        instrument_type,
        currency,
        unit,
        source,
        provider_key_value,
        provider_symbol_value,
    ) = _instrument_values(
        name=name,
        symbol=symbol,
        type_value=type,
        quote_currency=quote_currency,
        unit_label=unit_label,
        locale=locale,
        provider_key=provider_key,
        provider_symbol=provider_symbol,
    )

    def write(session: Session) -> InstrumentView:
        require_role_now(session, ctx, Role.EDITOR)
        _lock(session, ctx, exclusive=False)
        instrument_repository = InstrumentRepository(session, ctx)
        row = instrument_repository.get_for_update(instrument_id)
        if row is None:
            raise NotFound
        if row.archived_at is not None:
            raise InstrumentArchived
        if provider_key_value is not None:
            config = PriceSourceConfigRepository(session, ctx).for_provider(provider_key_value)
            if (config is None or not config.enabled) and row.provider_key != provider_key_value:
                raise InvalidInvestment("provider_key", "provider_disabled")
        has_transactions = InvestmentTransactionRepository(session, ctx).any_for_instrument(row.id)
        if has_transactions and row.quote_currency != currency:
            raise InvalidInvestment("quote_currency", "currency_locked")
        if has_transactions and row.type != instrument_type.value:
            raise InvalidInvestment("type", "type_locked")
        classes = {row.id: row for row in AssetClassRepository(session, ctx).ordered()}
        allocation_repository = InstrumentAllocationRepository(session, ctx)
        existing_rows = allocation_repository.for_instrument(row.id)
        existing = {allocation.asset_class_id for allocation in existing_rows}
        rows = _allocation_rows(allocations, classes, existing=existing)
        if row.name_key != key:
            _ensure_unique_instrument(instrument_repository, key=key, own_id=row.id)
        changes: dict[str, object] = {
            field: value
            for field, value in (
                ("name", clean),
                ("symbol", symbol_value),
                ("type", instrument_type.value),
                ("quote_currency", currency),
                ("unit_label", unit),
                ("valuation_source", source.value),
                ("provider_key", provider_key_value),
                ("provider_symbol", provider_symbol_value),
            )
            if getattr(row, field) != value
        }
        submitted_weights = dict(rows)
        existing_weights = {
            allocation.asset_class_id: allocation.weight_bp for allocation in existing_rows
        }
        allocations_changed = submitted_weights != existing_weights
        if not changes and not allocations_changed:
            return _instrument_view(
                row,
                classes,
                existing_rows,
                locale,
                has_transactions=has_transactions,
            )
        if "name" in changes:
            changes["name_key"] = key
        if "provider_key" in changes or "provider_symbol" in changes:
            changes["provider_backfilled_at"] = None
        if changes:
            instrument_repository.update(row.id, **changes)
        if allocations_changed:
            # Replace the set; the unique constraint is the final guard for two concurrent saves.
            for allocation in existing_rows:
                allocation_repository.delete(allocation)
            for class_id, weight_bp in rows:
                allocation_repository.create(
                    instrument_id=row.id,
                    asset_class_id=class_id,
                    weight_bp=weight_bp,
                )
        if changes:
            _audit(
                session,
                ctx,
                "instrument.updated",
                "instrument",
                row.id,
                {
                    "name": clean,
                    "fields": ",".join(sorted(f for f in changes if f != "name_key")),
                },
            )
        if allocations_changed:
            _audit(
                session,
                ctx,
                "instrument_allocation.updated",
                "instrument",
                row.id,
                {"count": len(rows)},
            )
        return _instrument_view(
            row,
            classes,
            allocation_repository.for_instrument(row.id),
            locale,
            has_transactions=has_transactions,
        )

    return _write(factory, write, duplicate=DuplicateInstrumentName())


def archive_instrument(factory: sessionmaker[Session], ctx: Context, instrument_id: UUID) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        _lock(session, ctx, exclusive=False)
        repository = InstrumentRepository(session, ctx)
        row = repository.get_for_update(instrument_id)
        if row is None:
            raise NotFound
        if row.archived_at is not None:
            return
        repository.update(row.id, archived_at=ctx.clock.now())
        _audit(
            session,
            ctx,
            "instrument.archived",
            "instrument",
            row.id,
            {"name": row.name},
        )

    run_immediate(factory, write)


def restore_instrument(factory: sessionmaker[Session], ctx: Context, instrument_id: UUID) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        _lock(session, ctx, exclusive=False)
        repository = InstrumentRepository(session, ctx)
        row = repository.get_for_update(instrument_id)
        if row is None:
            raise NotFound
        if row.archived_at is None:
            return
        _ensure_unique_instrument(repository, key=row.name_key)
        repository.update(row.id, archived_at=None)
        _audit(
            session,
            ctx,
            "instrument.restored",
            "instrument",
            row.id,
            {"name": row.name},
        )

    _write(factory, write, duplicate=DuplicateInstrumentName())
