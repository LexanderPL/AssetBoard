"""Investment transactions and holdings (F6.2).

Every write takes the shared workspace lock (so a base-currency change cannot interleave),
re-reads the actor's role, locks the instrument row, and rebuilds the instrument's ordered
history before committing. The rebuild rejects any sell that would exceed the quantity held at
that point, including oversells created by editing an earlier buy, back-dating a sell, deleting
a buy or restoring a deleted sell. Holdings are never stored; they are recomputed on read with
the workspace's average/FIFO setting.

Fees and unit prices are in the instrument's quote currency. ``fx_to_base`` is resolved on
write (1 for a base-currency instrument). F6.2 requires a rate for a non-base instrument; F6.3
will pre-fill it from stored FX.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.engine import run_immediate
from assetboard.db.models.investments import Instrument, InvestmentTransaction
from assetboard.db.repositories import (
    AuditEventRepository,
    CurrentWorkspaceRepository,
    InstrumentRepository,
    InvestmentTransactionRepository,
    NotFound,
)
from assetboard.domain.holdings import (
    MAX_NOTE_LENGTH,
    CostBasisMethod,
    Holding,
    InvalidHoldingTransaction,
    InvestmentTransactionType,
    Oversell,
    compute_holdings,
)
from assetboard.domain.money import Money
from assetboard.domain.transactions import (
    MAX_AMOUNT_MINOR,
    UNDO_WINDOW,
    EntryError,
    amount_minor,
    clean_description,
)
from assetboard.i18n.numbers import parse_amount
from assetboard.services.context import Context, Role, require_role, require_role_now
from assetboard.services.holding_rows import cost_basis_method, holding_transaction
from assetboard.services.investments import InstrumentView, get_instrument

__all__ = [
    "DeletedInvestmentTransaction",
    "InstrumentHistory",
    "InstrumentArchived",
    "InvestmentTransactionError",
    "InvestmentTransactionView",
    "InvalidInvestmentTransaction",
    "InvestmentUndoExpired",
    "OversellRejected",
    "TransactionList",
    "create_transaction",
    "delete_transaction",
    "instrument_history",
    "list_transactions",
    "undo_delete",
    "update_transaction",
    "workspace_has_investment_transactions",
]

PAGE_SIZE = 50
MIN_YEAR = 1900
MAX_YEAR = 2199


class InvestmentTransactionError(Exception):
    code = "invalid"
    field = ""


class InvalidInvestmentTransaction(InvestmentTransactionError):
    def __init__(self, field: str, code: str) -> None:
        super().__init__(code)
        self.field = field
        self.code = code


class OversellRejected(InvalidInvestmentTransaction):
    def __init__(self, on: date | None) -> None:
        super().__init__("quantity", "oversell")
        self.on = on


class InstrumentArchived(InvestmentTransactionError):
    field = "instrument"
    code = "instrument_archived"


class TransactionDeleted(InvestmentTransactionError):
    field = "transaction"
    code = "deleted"


class InvestmentUndoExpired(InvestmentTransactionError):
    code = "undo_expired"


@dataclass(frozen=True, slots=True)
class InvestmentTransactionView:
    id: UUID
    instrument_id: UUID
    instrument_name: str
    instrument_symbol: str | None
    instrument_type: str
    unit_label: str
    quote_currency: str
    date: date | None
    type: str
    quantity: Decimal
    unit_price: Decimal
    fee: Money
    fx_to_base: Decimal
    note: str
    instrument_archived: bool


@dataclass(frozen=True, slots=True)
class TransactionList:
    rows: tuple[InvestmentTransactionView, ...]
    page: int
    pages: int
    total: int
    has_previous: bool
    has_next: bool


@dataclass(frozen=True, slots=True)
class InstrumentHistory:
    instrument: InstrumentView
    holding: Holding
    transactions: tuple[InvestmentTransactionView, ...]
    transactions_locked: bool


@dataclass(frozen=True, slots=True)
class DeletedInvestmentTransaction:
    id: UUID
    undo_until: datetime


@dataclass(frozen=True, slots=True)
class _Values:
    day: date | None
    type: str
    quantity: Decimal
    unit_price: Decimal
    fee_minor: int
    fx_to_base: Decimal
    note: str


def _type(value: str) -> str:
    try:
        kind = InvestmentTransactionType(value)
    except ValueError:
        raise InvalidInvestmentTransaction("type", "invalid_type") from None
    if kind not in (InvestmentTransactionType.BUY, InvestmentTransactionType.SELL):
        raise InvalidInvestmentTransaction("type", "invalid_type")
    return kind.value


def _day(value: str, today: date, *, required: bool) -> date | None:
    text = value.strip()
    if not text:
        if required:
            raise InvalidInvestmentTransaction("date", "invalid_date")
        return None
    try:
        day = date.fromisoformat(text)
    except ValueError:
        raise InvalidInvestmentTransaction("date", "invalid_date") from None
    if not MIN_YEAR <= day.year <= MAX_YEAR:
        raise InvalidInvestmentTransaction("date", "invalid_date")
    if day > today:
        raise InvalidInvestmentTransaction("date", "future_date")
    return day


def _decimal(value: str, locale: str, field: str, *, allow_zero: bool) -> Decimal:
    try:
        amount = parse_amount(value, locale)
    except ValueError:
        raise InvalidInvestmentTransaction(field, f"invalid_{field}") from None
    if not amount.is_finite() or amount < 0 or (amount == 0 and not allow_zero):
        raise InvalidInvestmentTransaction(field, f"invalid_{field}")
    exponent = amount.as_tuple().exponent
    assert isinstance(exponent, int)
    scale = max(-exponent, 0)
    integer_digits = 0 if amount.is_zero() else max(amount.adjusted() + 1, 0)
    if scale > 18 or integer_digits > 20 or integer_digits + scale > 38:
        raise InvalidInvestmentTransaction(field, f"invalid_{field}")
    return amount


def _fee_minor(value: str, locale: str, currency: str) -> int:
    fee = _decimal(value, locale, "fee", allow_zero=True)
    if fee == 0:
        return 0
    try:
        minor = amount_minor(fee, currency)
    except EntryError as exc:
        raise InvalidInvestmentTransaction("fee", exc.code) from None
    if minor > MAX_AMOUNT_MINOR:
        raise InvalidInvestmentTransaction("fee", "invalid_fee")
    return minor


def _fx(value: str, locale: str, *, quote_currency: str, base_currency: str) -> Decimal:
    if quote_currency == base_currency:
        return Decimal(1)
    text = value.strip()
    if not text:
        raise InvalidInvestmentTransaction("fx_to_base", "invalid_fx")
    fx = _decimal(text, locale, "fx_to_base", allow_zero=False)
    if fx <= 0:
        raise InvalidInvestmentTransaction("fx_to_base", "invalid_fx")
    return fx


def _note(value: str) -> str:
    try:
        note = clean_description(value)
    except EntryError as exc:
        raise InvalidInvestmentTransaction("note", f"invalid_{exc.code}") from None
    if len(note) > MAX_NOTE_LENGTH:
        raise InvalidInvestmentTransaction("note", "invalid_note")
    return note


def _values_for_workspace(
    *,
    instrument: Instrument,
    base_currency: str,
    type_value: str,
    day_value: str,
    quantity_value: str,
    unit_price_value: str,
    fee_value: str,
    fx_value: str,
    note_value: str,
    locale: str,
    today: date,
    day_required: bool,
) -> _Values:
    day = _day(day_value, today, required=day_required)
    kind = _type(type_value)
    quantity = _decimal(quantity_value, locale, "quantity", allow_zero=False)
    if instrument.type == "currency":
        unit_price = Decimal(1)
    else:
        unit_price = _decimal(unit_price_value, locale, "unit_price", allow_zero=True)
    fee_minor = _fee_minor(fee_value, locale, instrument.quote_currency)
    fx = _fx(
        fx_value,
        locale,
        quote_currency=instrument.quote_currency,
        base_currency=base_currency,
    )
    return _Values(
        day=day,
        type=kind,
        quantity=quantity,
        unit_price=unit_price,
        fee_minor=fee_minor,
        fx_to_base=fx,
        note=_note(note_value),
    )


def _check_oversell(
    session: Session,
    ctx: Context,
    instrument_id: UUID,
    *,
    method: CostBasisMethod,
    base_currency: str,
) -> None:
    rows = InvestmentTransactionRepository(session, ctx).for_instrument(instrument_id)
    try:
        compute_holdings(
            [holding_transaction(row) for row in rows],
            method,
            None,
            base_currency=base_currency,
        )
    except Oversell as exc:
        raise OversellRejected(exc.on) from None
    except InvalidHoldingTransaction as exc:
        raise InvalidInvestmentTransaction("quantity", exc.code) from None


def _lock(session: Session, ctx: Context) -> None:
    CurrentWorkspaceRepository(session, ctx).lock(exclusive=False)


def _audit(
    session: Session,
    ctx: Context,
    action: str,
    row: InvestmentTransaction,
    instrument: Instrument,
) -> None:
    AuditEventRepository(session, ctx).record(
        actor_user_id=ctx.user.id,
        action=action,
        entity_type="investment_transaction",
        entity_id=str(row.id),
        ts=ctx.clock.now(),
        data={
            "instrument": instrument.name,
            "transaction_type": row.type,
            "date": row.date.isoformat() if row.date is not None else "",
            "quantity": format(row.quantity, "f"),
            "price": format(row.unit_price, "f"),
            "fee": format(Money(row.fee_minor, row.quote_currency).to_decimal(), "f"),
            "currency": row.quote_currency,
            "fx": format(row.fx_to_base, "f") if row.fx_to_base is not None else "",
        },
    )


def _view(row: InvestmentTransaction, instrument: Instrument) -> InvestmentTransactionView:
    return InvestmentTransactionView(
        id=row.id,
        instrument_id=row.instrument_id,
        instrument_name=instrument.name,
        instrument_symbol=instrument.symbol,
        instrument_type=instrument.type,
        unit_label=instrument.unit_label,
        quote_currency=row.quote_currency,
        date=row.date,
        type=row.type,
        quantity=row.quantity,
        unit_price=row.unit_price,
        fee=Money(row.fee_minor, row.quote_currency),
        fx_to_base=row.fx_to_base if row.fx_to_base is not None else Decimal(1),
        note=row.note,
        instrument_archived=instrument.archived_at is not None,
    )


def _instrument_map(session: Session, ctx: Context) -> dict[UUID, Instrument]:
    return {
        instrument.id: instrument for instrument in InstrumentRepository(session, ctx).ordered()
    }


def _zero_holding(instrument_id: UUID, base_currency: str) -> Holding:
    return Holding(
        instrument_id=instrument_id,
        quantity=Decimal(0),
        cost_basis_base=Money(0, base_currency),
        avg_cost=Decimal(0),
        realized_pnl=Money(0, base_currency),
    )


def _page_number(page: int, pages: int, total: int) -> int:
    if total == 0:
        return 1
    return min(max(page, 1), pages)


def _parse_filter_day(value: str, field: str) -> date | None:
    text = value.strip()
    if not text:
        return None
    try:
        day = date.fromisoformat(text)
    except ValueError:
        raise InvalidInvestmentTransaction(field, "invalid_date") from None
    if not MIN_YEAR <= day.year <= MAX_YEAR:
        raise InvalidInvestmentTransaction(field, "invalid_date")
    return day


def list_transactions(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    instrument_id: UUID | None,
    type_value: str,
    date_from: str,
    date_to: str,
    page: int,
) -> TransactionList:
    require_role(ctx, Role.VIEWER)
    if type_value and type_value not in {"buy", "sell"}:
        raise InvalidInvestmentTransaction("type", "invalid_type")
    start = _parse_filter_day(date_from, "from")
    end = _parse_filter_day(date_to, "to")
    if start is not None and end is not None and end < start:
        raise InvalidInvestmentTransaction("to", "invalid_date")
    wanted_page = max(page, 1)
    with factory() as session:
        repository = InvestmentTransactionRepository(session, ctx)
        total = repository.count_filtered(
            instrument_id=instrument_id,
            types=(type_value,) if type_value else (),
            date_from=start,
            date_to=end,
        )
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        current = _page_number(wanted_page, pages, total)
        rows = repository.filtered(
            instrument_id=instrument_id,
            types=(type_value,) if type_value else (),
            date_from=start,
            date_to=end,
            limit=PAGE_SIZE,
            offset=(current - 1) * PAGE_SIZE,
        )
        instruments = _instrument_map(session, ctx)
        views = tuple(_view(row, instruments[row.instrument_id]) for row in rows)
    return TransactionList(
        rows=views,
        page=current,
        pages=pages,
        total=total,
        has_previous=current > 1,
        has_next=current < pages,
    )


def get_transaction(
    factory: sessionmaker[Session],
    ctx: Context,
    transaction_id: UUID,
    *,
    locale: str,
) -> InvestmentTransactionView:
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        row = InvestmentTransactionRepository(session, ctx).get(transaction_id)
        if row is None:
            raise NotFound
        instrument = InstrumentRepository(session, ctx).get_or_raise(row.instrument_id)
        return _view(row, instrument)


def instrument_history(
    factory: sessionmaker[Session],
    ctx: Context,
    instrument_id: UUID,
    *,
    locale: str,
) -> InstrumentHistory:
    require_role(ctx, Role.VIEWER)
    instrument = get_instrument(factory, ctx, instrument_id, locale=locale)
    with factory() as session:
        workspace = CurrentWorkspaceRepository(session, ctx).get()
        rows = InvestmentTransactionRepository(session, ctx).for_instrument(instrument_id)
        model = InstrumentRepository(session, ctx).get_or_raise(instrument_id)
        base_currency = workspace.base_currency
        if rows:
            computed = compute_holdings(
                [holding_transaction(row) for row in rows],
                cost_basis_method(workspace),
                None,
                base_currency=base_currency,
            )
            holding = next(
                (item for item in computed if item.instrument_id == instrument_id),
                _zero_holding(instrument_id, base_currency),
            )
        else:
            holding = _zero_holding(instrument_id, base_currency)
        views = tuple(_view(row, model) for row in rows)
        locked = InvestmentTransactionRepository(session, ctx).any_for_instrument(instrument_id)
    return InstrumentHistory(
        instrument=instrument,
        holding=holding,
        transactions=views,
        transactions_locked=locked,
    )


def _workspace_values(session: Session, ctx: Context) -> tuple[str, CostBasisMethod]:
    workspace = CurrentWorkspaceRepository(session, ctx).get()
    return workspace.base_currency, cost_basis_method(workspace)


def create_transaction(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    instrument_id: UUID,
    type_value: str,
    date_value: str,
    quantity_value: str,
    unit_price_value: str,
    fee_value: str,
    fx_value: str,
    note_value: str,
    locale: str,
) -> InvestmentTransactionView:
    require_role(ctx, Role.EDITOR)
    today = ctx.clock.today(ctx.workspace.timezone)

    def write(session: Session) -> InvestmentTransactionView:
        require_role_now(session, ctx, Role.EDITOR)
        _lock(session, ctx)
        base_currency, method = _workspace_values(session, ctx)
        instruments = InstrumentRepository(session, ctx)
        instrument = instruments.get_for_update(instrument_id)
        if instrument is None:
            raise NotFound
        if instrument.archived_at is not None:
            raise InstrumentArchived
        values = _values_for_workspace(
            instrument=instrument,
            base_currency=base_currency,
            type_value=type_value,
            day_value=date_value,
            quantity_value=quantity_value,
            unit_price_value=unit_price_value,
            fee_value=fee_value,
            fx_value=fx_value,
            note_value=note_value,
            locale=locale,
            today=today,
            day_required=True,
        )
        row = InvestmentTransactionRepository(session, ctx).create(
            instrument_id=instrument.id,
            date=values.day,
            type=values.type,
            quantity=values.quantity,
            unit_price=values.unit_price,
            quote_currency=instrument.quote_currency,
            fee_minor=values.fee_minor,
            fx_to_base=values.fx_to_base,
            note=values.note,
        )
        session.flush()
        _check_oversell(
            session,
            ctx,
            instrument.id,
            method=method,
            base_currency=base_currency,
        )
        _audit(session, ctx, "investment_transaction.created", row, instrument)
        return _view(row, instrument)

    return run_immediate(factory, write)


def update_transaction(
    factory: sessionmaker[Session],
    ctx: Context,
    transaction_id: UUID,
    *,
    type_value: str,
    date_value: str,
    quantity_value: str,
    unit_price_value: str,
    fee_value: str,
    fx_value: str,
    note_value: str,
    locale: str,
) -> InvestmentTransactionView:
    require_role(ctx, Role.EDITOR)
    today = ctx.clock.today(ctx.workspace.timezone)

    def write(session: Session) -> InvestmentTransactionView:
        require_role_now(session, ctx, Role.EDITOR)
        _lock(session, ctx)
        base_currency, method = _workspace_values(session, ctx)
        repository = InvestmentTransactionRepository(session, ctx)
        row = repository.get_for_update(transaction_id)
        if row is None:
            raise NotFound
        if row.deleted_at is not None:
            raise TransactionDeleted
        instrument = InstrumentRepository(session, ctx).get_for_update(row.instrument_id)
        if instrument is None:
            raise NotFound
        if instrument.archived_at is not None:
            raise InstrumentArchived
        values = _values_for_workspace(
            instrument=instrument,
            base_currency=base_currency,
            type_value=type_value,
            day_value=date_value,
            quantity_value=quantity_value,
            unit_price_value=unit_price_value,
            fee_value=fee_value,
            fx_value=fx_value,
            note_value=note_value,
            locale=locale,
            today=today,
            day_required=row.date is not None,
        )
        changes = {
            field: value
            for field, value in (
                ("date", values.day),
                ("type", values.type),
                ("quantity", values.quantity),
                ("unit_price", values.unit_price),
                ("fee_minor", values.fee_minor),
                ("fx_to_base", values.fx_to_base),
                ("note", values.note),
            )
            if getattr(row, field) != value
        }
        if not changes:
            return _view(row, instrument)
        repository.update(row.id, **changes)
        session.flush()
        _check_oversell(
            session,
            ctx,
            instrument.id,
            method=method,
            base_currency=base_currency,
        )
        _audit(session, ctx, "investment_transaction.updated", row, instrument)
        return _view(row, instrument)

    return run_immediate(factory, write)


def delete_transaction(
    factory: sessionmaker[Session], ctx: Context, transaction_id: UUID
) -> DeletedInvestmentTransaction:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> DeletedInvestmentTransaction:
        require_role_now(session, ctx, Role.EDITOR)
        _lock(session, ctx)
        base_currency, method = _workspace_values(session, ctx)
        repository = InvestmentTransactionRepository(session, ctx)
        row = repository.get_for_update(transaction_id)
        if row is None:
            raise NotFound
        instrument = InstrumentRepository(session, ctx).get_for_update(row.instrument_id)
        if instrument is None:
            raise NotFound
        if instrument.archived_at is not None:
            raise InstrumentArchived
        if row.deleted_at is None:
            repository.update(row.id, deleted_at=ctx.clock.now())
            session.flush()
            _check_oversell(
                session,
                ctx,
                instrument.id,
                method=method,
                base_currency=base_currency,
            )
            _audit(session, ctx, "investment_transaction.deleted", row, instrument)
        assert row.deleted_at is not None
        return DeletedInvestmentTransaction(id=row.id, undo_until=row.deleted_at + UNDO_WINDOW)

    return run_immediate(factory, write)


def undo_delete(
    factory: sessionmaker[Session], ctx: Context, transaction_id: UUID
) -> InvestmentTransactionView:
    require_role(ctx, Role.EDITOR)
    today = ctx.clock.today(ctx.workspace.timezone)

    def write(session: Session) -> InvestmentTransactionView:
        require_role_now(session, ctx, Role.EDITOR)
        _lock(session, ctx)
        base_currency, method = _workspace_values(session, ctx)
        repository = InvestmentTransactionRepository(session, ctx)
        row = repository.get_for_update(transaction_id)
        if row is None:
            raise NotFound
        instrument = InstrumentRepository(session, ctx).get_for_update(row.instrument_id)
        if instrument is None:
            raise NotFound
        if instrument.archived_at is not None:
            raise InstrumentArchived
        if row.deleted_at is None:
            return _view(row, instrument)
        if ctx.clock.now() - row.deleted_at > UNDO_WINDOW:
            raise InvestmentUndoExpired
        if row.date is not None and row.date > today:
            raise InvalidInvestmentTransaction("date", "future_date")
        repository.update(row.id, deleted_at=None)
        session.flush()
        _check_oversell(
            session,
            ctx,
            instrument.id,
            method=method,
            base_currency=base_currency,
        )
        _audit(session, ctx, "investment_transaction.restored", row, instrument)
        return _view(row, instrument)

    return run_immediate(factory, write)


def workspace_has_investment_transactions(session: Session, ctx: Context) -> bool:
    """Part of ``services.workspaces.BASE_CURRENCY_LOCKS``: deleted rows count too."""
    return InvestmentTransactionRepository(session, ctx).any_exists()
