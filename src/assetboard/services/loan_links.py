"""Loan integrations (F7.6): net-worth resolver, linked liability and property LTV."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import cast
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.engine import run_immediate
from assetboard.db.models.loans import Loan
from assetboard.db.models.networth import NetWorthCategory, NetWorthItem
from assetboard.db.repositories import (
    AuditEventRepository,
    CurrentWorkspaceRepository,
    LoanPaymentRepository,
    LoanRepository,
    NetWorthCategoryRepository,
    NetWorthItemRepository,
    NotFound,
)
from assetboard.domain.categories import PALETTE, name_key
from assetboard.domain.loans import (
    LoanPaymentKind,
    LoanValidationError,
    Money,
    ScheduleAnchor,
    ScheduleError,
)
from assetboard.domain.networth import Side, Unavailable
from assetboard.i18n import gettext
from assetboard.i18n.locale import SUPPORTED_LOCALES
from assetboard.services import loan_schedule, loans
from assetboard.services.context import Context, Role, require_role, require_role_now
from assetboard.services.networth import current_values
from assetboard.services.networth_links import (
    UNAVAILABLE,
    LinkChoice,
    LinkTarget,
    WorkspaceScope,
    registry,
)
from assetboard.services.portfolio_links import real_estate_category_ids


@dataclass(frozen=True, slots=True)
class LoanLiabilityView:
    id: UUID
    name: str
    currency: str


@dataclass(frozen=True, slots=True)
class PropertyChoice:
    id: UUID
    label: str


@dataclass(frozen=True, slots=True)
class LoanPropertyView:
    as_of: date
    property_id: UUID | None
    property_name: str | None
    property_currency: str | None
    value: Money | None
    remaining: Money | None
    ltv_bp: int | None
    equity: Money | None
    ltv_reason: str | None
    equity_reason: str | None


def _today(ctx: Context) -> date:
    return ctx.clock.today(ctx.workspace.timezone)


def _half_up(value: int, multiplier: int, divisor: int) -> int:
    if value < 0 or multiplier < 0 or divisor <= 0:
        raise ValueError("half-up inputs must be non-negative")
    return (value * multiplier * 2 + divisor) // (2 * divisor)


def loan_balance_at(
    session: Session, ctx: Context, loan_id: UUID, as_of: date
) -> Money | Unavailable:
    """Remaining principal at ``as_of`` from terms, checkpoints and actual installments."""
    try:
        view = loans.load_loan_view_in_session(session, ctx, loan_id, _today(ctx))
    except NotFound:
        return UNAVAILABLE
    anchor: ScheduleAnchor | None = None
    for checkpoint in view.checkpoints:
        if checkpoint.as_of <= as_of and (anchor is None or checkpoint.as_of >= anchor.date):
            anchor = ScheduleAnchor(date=checkpoint.as_of, balance=checkpoint.remaining_principal)
    if anchor is None and view.origination_date is not None and view.principal_original is not None:
        anchor = ScheduleAnchor(date=view.origination_date, balance=view.principal_original)
    if anchor is None or as_of < anchor.date:
        return UNAVAILABLE
    try:
        inputs = loan_schedule.load_loan_schedule_inputs_in_session(session, ctx, loan_id, as_of)
        schedule = loan_schedule.schedule_from_anchor(inputs, anchor, as_of)
    except (ScheduleError, LoanValidationError):
        return UNAVAILABLE

    projected = anchor.balance
    for row in schedule.rows:
        if row.date <= as_of:
            projected = row.closing
        else:
            break

    rows_by_date = {row.date: row for row in schedule.rows if row.date <= as_of}
    payments = LoanPaymentRepository(session, ctx).for_loan(loan_id)
    actual_by_row: dict[date, int] = {}
    overpayment_only = 0
    for payment in payments:
        if payment.date <= anchor.date or payment.date > as_of:
            continue
        if payment.kind == LoanPaymentKind.OVERPAYMENT.value:
            overpayment_only += payment.total_minor
            continue
        matched = next(
            (row for row in schedule.rows if row.date >= payment.date and row.date <= as_of),
            None,
        )
        if matched is None:
            # An installment paid before its due date still reduces today's balance, even when
            # the projected row itself lies after ``as_of``.
            if payment.date <= as_of and any(row.date > as_of for row in schedule.rows):
                overpayment_only += payment.principal_minor + payment.overpayment_minor
            continue
        actual_by_row[matched.date] = actual_by_row.get(matched.date, 0) + (
            payment.principal_minor + payment.overpayment_minor
        )
    adjustment = overpayment_only
    for day, actual in actual_by_row.items():
        row = rows_by_date[day]
        adjustment += actual - (row.principal.amount_minor + row.overpayment.amount_minor)
    remaining = max(0, projected.amount_minor - adjustment)
    return Money(remaining, projected.currency)


class _LoanBalanceResolver:
    side = Side.LIABILITY

    def __init__(self, session: Session, scope: WorkspaceScope) -> None:
        self.session = session
        self.scope = scope

    def choices(self) -> Sequence[LinkChoice]:
        return tuple(
            LinkChoice(row.id, row.name)
            for row in LoanRepository(self.session, self.scope).ordered(include_archived=False)
        )

    def describe(self, link_id: UUID) -> LinkTarget | None | Unavailable:
        row = LoanRepository(self.session, self.scope).get(link_id)
        if row is None:
            return None
        return LinkTarget(row.name, row.currency, f"/loans/{row.id}")

    def resolve_many(
        self, link_ids: Collection[UUID], dates: Sequence[date]
    ) -> Mapping[tuple[UUID, date], Money | Unavailable]:
        repository = LoanRepository(self.session, self.scope)
        known = {row.id for row in repository.ordered()}
        result: dict[tuple[UUID, date], Money | Unavailable] = {}
        for link_id in link_ids:
            for day in dates:
                if link_id not in known:
                    result[(link_id, day)] = UNAVAILABLE
                else:
                    result[(link_id, day)] = loan_balance_at(
                        self.session,
                        cast(Context, self.scope),
                        link_id,
                        day,
                    )
        return result


def register_loan_resolvers() -> None:
    """Register ``loan_balance``; safe to call more than once."""
    if "loan_balance" not in registry.labels():
        registry.register(
            "loan_balance",
            gettext("Loan", "en"),
            lambda session, scope: _LoanBalanceResolver(session, scope),
        )


def _mortgage_category_keys() -> frozenset[str]:
    return frozenset(name_key(gettext("Mortgage", locale)) for locale in SUPPORTED_LOCALES)


def _mortgage_category(session: Session, ctx: Context) -> tuple[NetWorthCategory, bool]:
    repository = NetWorthCategoryRepository(session, ctx)
    keys = _mortgage_category_keys()
    for row in repository.ordered(Side.LIABILITY.value):
        if row.archived_at is None and row.name_key in keys:
            return row, False
    name = gettext("Mortgage", ctx.workspace.locale)
    category = repository.create(
        name=name,
        name_key=name_key(name),
        side=Side.LIABILITY.value,
        color=PALETTE[0],
        sort_order=repository.next_sort_order(Side.LIABILITY.value),
    )
    AuditEventRepository(session, ctx).record(
        actor_user_id=ctx.user.id,
        action="networth_category.created",
        entity_type="networth_category",
        entity_id=str(category.id),
        ts=ctx.clock.now(),
        data={"name": name, "side": Side.LIABILITY.value},
    )
    return category, True


def _liability_view(row: NetWorthItem) -> LoanLiabilityView:
    return LoanLiabilityView(id=row.id, name=row.name, currency=row.currency)


def loan_liability_view(
    factory: sessionmaker[Session], ctx: Context, loan_id: UUID
) -> LoanLiabilityView | None:
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        LoanRepository(session, ctx).get_or_raise(loan_id)
        row = NetWorthItemRepository(session, ctx).active_linked("loan_balance", loan_id)
        return None if row is None else _liability_view(row)


def create_loan_liability_item(
    factory: sessionmaker[Session], ctx: Context, loan_id: UUID
) -> LoanLiabilityView:
    """Create the one linked liability item under Mortgage; existing active link is returned."""
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> LoanLiabilityView:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        loan = LoanRepository(session, ctx).get_or_raise(loan_id)
        if loan.archived_at is not None:
            raise loans.LoanError("loan", "archived")
        items = NetWorthItemRepository(session, ctx)
        existing = items.active_linked("loan_balance", loan.id)
        if existing is not None:
            return _liability_view(existing)
        category, _created = _mortgage_category(session, ctx)
        item = items.create(
            side=Side.LIABILITY.value,
            category_id=category.id,
            name=loan.name,
            currency=loan.currency,
            valuation_mode="linked",
            link_type="loan_balance",
            link_id=loan.id,
            include_in_net_worth=True,
            estimate_between=False,
        )
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="networth_item.created",
            entity_type="networth_item",
            entity_id=str(item.id),
            ts=ctx.clock.now(),
            data={
                "name": loan.name,
                "side": Side.LIABILITY.value,
                "category": category.name,
                "currency": loan.currency,
                "link_type": "loan_balance",
            },
        )
        return _liability_view(item)

    return run_immediate(factory, write)


def set_auto_record_payments(
    factory: sessionmaker[Session], ctx: Context, loan_id: UUID, enabled: bool
) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        loan = LoanRepository(session, ctx).get_or_raise(loan_id)
        if loan.archived_at is not None:
            raise loans.LoanError("loan", "archived")
        LoanRepository(session, ctx).update(loan.id, auto_record_payments=enabled)
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="loan.auto_record_updated",
            entity_type="loan",
            entity_id=str(loan.id),
            ts=ctx.clock.now(),
            data={"enabled": enabled},
        )

    _write(factory, write)


def property_choices(
    factory: sessionmaker[Session], ctx: Context, loan_id: UUID
) -> tuple[PropertyChoice, ...]:
    require_role(ctx, Role.VIEWER)
    view = loans.get_loan(factory, ctx, loan_id)
    with factory() as session:
        return _property_choices_in_session(session, ctx, view)


def _real_estate_ids(session: Session, ctx: Context) -> set[UUID]:
    ids = real_estate_category_ids(session, ctx)
    active = {
        row.id
        for row in NetWorthCategoryRepository(session, ctx).ordered(Side.ASSET.value)
        if row.archived_at is None
    }
    return ids & active


def _property_choices_in_session(
    session: Session, ctx: Context, loan: loans.LoanView
) -> tuple[PropertyChoice, ...]:
    categories = _real_estate_ids(session, ctx)
    return tuple(
        PropertyChoice(item.id, item.name)
        for item in NetWorthItemRepository(session, ctx).ordered("asset", include_archived=False)
        if item.category_id in categories and item.currency == loan.currency
    )


def _property_item(session: Session, ctx: Context, item_id: UUID) -> NetWorthItem:
    return NetWorthItemRepository(session, ctx).get_or_raise(item_id)


def _validate_property(session: Session, ctx: Context, loan: Loan, item_id: UUID) -> NetWorthItem:
    item = _property_item(session, ctx, item_id)
    if item.archived_at is not None:
        raise loans.LoanError("property_item_id", "property_archived")
    if item.side != Side.ASSET.value:
        raise loans.LoanError("property_item_id", "property_not_asset")
    if item.category_id not in _real_estate_ids(session, ctx):
        raise loans.LoanError("property_item_id", "property_not_real_estate")
    if item.currency != loan.currency:
        raise loans.LoanError("property_item_id", "property_currency")
    return item


def set_loan_property(
    factory: sessionmaker[Session], ctx: Context, loan_id: UUID, property_item_id: UUID
) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        loan = LoanRepository(session, ctx).get_or_raise(loan_id)
        if loan.archived_at is not None:
            raise loans.LoanError("loan", "archived")
        item = _validate_property(session, ctx, loan, property_item_id)
        LoanRepository(session, ctx).update(loan.id, property_item_id=property_item_id)
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="loan.property_linked",
            entity_type="loan",
            entity_id=str(loan.id),
            ts=ctx.clock.now(),
            data={"property_item_id": str(item.id), "name": item.name},
        )

    _write(factory, write)


def clear_loan_property(factory: sessionmaker[Session], ctx: Context, loan_id: UUID) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        loan = LoanRepository(session, ctx).get_or_raise(loan_id)
        if loan.archived_at is not None:
            raise loans.LoanError("loan", "archived")
        if loan.property_item_id is not None:
            LoanRepository(session, ctx).update(loan.id, property_item_id=None)
            AuditEventRepository(session, ctx).record(
                actor_user_id=ctx.user.id,
                action="loan.property_unlinked",
                entity_type="loan",
                entity_id=str(loan.id),
                ts=ctx.clock.now(),
            )

    _write(factory, write)


def loan_property_view(
    factory: sessionmaker[Session], ctx: Context, loan_id: UUID, as_of: date
) -> LoanPropertyView:
    require_role(ctx, Role.VIEWER)
    view = loans.get_loan(factory, ctx, loan_id)
    if view.property_item_id is None:
        return LoanPropertyView(
            as_of, None, None, None, None, None, None, None, "no_property", "no_property"
        )
    with factory() as session:
        item = NetWorthItemRepository(session, ctx).get(view.property_item_id)
        if item is None:
            return LoanPropertyView(
                as_of,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                "property_missing",
                "property_missing",
            )
        if item.archived_at is not None:
            return LoanPropertyView(
                as_of,
                item.id,
                item.name,
                item.currency,
                None,
                None,
                None,
                None,
                "property_archived",
                "property_archived",
            )
        if item.currency != view.currency:
            return LoanPropertyView(
                as_of,
                item.id,
                item.name,
                item.currency,
                None,
                None,
                None,
                None,
                "property_currency",
                "property_currency",
            )
        values = current_values(
            session,
            ctx,
            as_of=as_of,
            base_currency=view.currency,
            timezone=ctx.workspace.timezone,
            item_ids={item.id},
        )
        item_view = next((row for row in values if row.id == item.id), None)
        value = item_view.current if item_view is not None else None
        remaining = loan_balance_at(session, ctx, loan_id, as_of)
        if isinstance(remaining, Unavailable):
            return LoanPropertyView(
                as_of,
                item.id,
                item.name,
                item.currency,
                value,
                None,
                None,
                None,
                "balance_unavailable",
                "balance_unavailable",
            )
        if value is None:
            return LoanPropertyView(
                as_of,
                item.id,
                item.name,
                item.currency,
                None,
                remaining,
                None,
                None,
                "no_value",
                "no_value",
            )
        if value.amount_minor == 0:
            return LoanPropertyView(
                as_of,
                item.id,
                item.name,
                item.currency,
                value,
                remaining,
                None,
                Money(-remaining.amount_minor, view.currency),
                "value_zero",
                None,
            )
        ltv = _half_up(remaining.amount_minor, 10_000, value.amount_minor)
        equity = Money(value.amount_minor - remaining.amount_minor, view.currency)
        return LoanPropertyView(
            as_of,
            item.id,
            item.name,
            item.currency,
            value,
            remaining,
            ltv,
            equity,
            None,
            None,
        )


def _write[T](factory: sessionmaker[Session], operation: Callable[[Session], T]) -> T:
    return run_immediate(factory, operation)
