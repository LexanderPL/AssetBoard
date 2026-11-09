"""Investment transactions and holdings (F6.2).

Every write takes the shared workspace lock (so a base-currency change cannot interleave),
re-reads the actor's role, locks the instrument row, and rebuilds the instrument's ordered
history before committing. The rebuild rejects any sell that would exceed the quantity held at
that point, including oversells created by editing an earlier buy, back-dating a sell, deleting
a buy or restoring a deleted sell. Holdings are never stored; they are recomputed on read with
the workspace's average/FIFO setting.

Fees and unit prices are in the instrument's quote currency. ``fx_to_base`` is resolved on
write (1 for a base-currency instrument). A non-base instrument needs a rate: the form
pre-fills it from stored FX, and a blank rate is refused with ``fx_needed``. Bulk add (F6.2a)
passes an ``fx_lookup`` so a blank rate there is taken from stored FX for the line's date.

F6.2a "amounts in the base currency": in base mode the user enters the unit price and fee in
the workspace base currency. They are stored as the settlement, and the quote unit price and
fee are derived from them with the rate (``domain.settlement``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.engine import run_immediate
from assetboard.db.models.investments import Instrument, InvestmentTransaction
from assetboard.db.repositories import (
    AuditEventRepository,
    CategoryRepository,
    CurrentWorkspaceRepository,
    InstrumentRepository,
    InvestmentTransactionRepository,
    NotFound,
    TransactionRepository,
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
from assetboard.domain.money import Money, minor_unit_for
from assetboard.domain.settlement import derive_quote_values, needs_fx_check
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
from assetboard.services.investment_income import (
    create_linked_income,
    remove_linked_income,
    restore_linked_income,
    update_linked_income,
)
from assetboard.services.investments import InstrumentView, get_instrument
from assetboard.services.months import ensure_date_open, lock_month_writes
from assetboard.services.prices import stored_fx_rates

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
    "instrument_fx_currency",
    "instrument_history",
    "list_transactions",
    "undo_delete",
    "update_transaction",
    "workspace_has_investment_transactions",
]

PAGE_SIZE = 50
MIN_YEAR = 1900
MAX_YEAR = 2199
AMOUNTS_QUOTE = "quote"
AMOUNTS_BASE = "base"

# Stored FX (base units per quote unit) for a date, or None when no rate is stored.
FxLookup = Callable[[date], Decimal | None]


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
    settlement_unit_price: Decimal | None = None
    settlement_fee: Money | None = None
    fx_needs_check: bool = False
    income_transaction_id: UUID | None = None
    income_category_id: UUID | None = None

    @property
    def amounts(self) -> str:
        """``base`` when the row was entered in the base currency (F6.2a), else ``quote``."""
        return AMOUNTS_BASE if self.settlement_fee is not None else AMOUNTS_QUOTE

    @property
    def derived_price_display(self) -> Decimal:
        """The derived quote unit price shortened for the history (6 places, half-up)."""
        with localcontext() as context:
            context.prec = 80
            return self.unit_price.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


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
class TransactionValues:
    """Validated values of one transaction; the settlement is set in base mode (F6.2a)."""

    day: date | None
    type: str
    quantity: Decimal
    unit_price: Decimal
    fee_minor: int
    fx_to_base: Decimal
    note: str
    settlement_currency: str | None = None
    settlement_unit_price: Decimal | None = None
    settlement_fee_minor: int | None = None
    # True when a blank rate was filled in from stored FX (bulk add).
    fx_stored: bool = False


def _type(value: str) -> str:
    try:
        kind = InvestmentTransactionType(value)
    except ValueError:
        raise InvalidInvestmentTransaction("type", "invalid_type") from None
    if kind not in (
        InvestmentTransactionType.BUY,
        InvestmentTransactionType.SELL,
        InvestmentTransactionType.DIVIDEND,
        InvestmentTransactionType.SPLIT,
    ):
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


def _fits_decimal_type(amount: Decimal) -> bool:
    exponent = amount.as_tuple().exponent
    assert isinstance(exponent, int)
    scale = max(-exponent, 0)
    integer_digits = 0 if amount.is_zero() else max(amount.adjusted() + 1, 0)
    return scale <= 18 and integer_digits <= 20 and integer_digits + scale <= 38


def _decimal(
    value: str, locale: str, field: str, *, allow_zero: bool, code: str | None = None
) -> Decimal:
    error = code or f"invalid_{field}"
    try:
        amount = parse_amount(value, locale)
    except ValueError:
        raise InvalidInvestmentTransaction(field, error) from None
    if not amount.is_finite() or amount < 0 or (amount == 0 and not allow_zero):
        raise InvalidInvestmentTransaction(field, error)
    if not _fits_decimal_type(amount):
        raise InvalidInvestmentTransaction(field, error)
    return amount


def _currency_amount(value: Decimal, currency: str) -> Decimal:
    minor_unit = minor_unit_for(currency)
    quantum = Decimal(1).scaleb(-minor_unit)
    if value != value.quantize(quantum):
        raise InvalidInvestmentTransaction("unit_price", "invalid_amount_precision")
    return value


def _fee_minor(value: str, locale: str, currency: str, *, field: str = "fee") -> int:
    fee = _decimal(value or "0", locale, field, allow_zero=True, code="invalid_fee")
    if fee == 0:
        return 0
    try:
        minor = amount_minor(fee, currency)
    except EntryError as exc:
        raise InvalidInvestmentTransaction(field, exc.code) from None
    if minor > MAX_AMOUNT_MINOR:
        raise InvalidInvestmentTransaction(field, "invalid_fee")
    return minor


def _fx(
    value: str,
    locale: str,
    *,
    quote_currency: str,
    base_currency: str,
    day: date | None,
    lookup: FxLookup | None,
) -> tuple[Decimal, bool]:
    """The rate, and whether a blank rate was filled in from stored FX."""
    if quote_currency == base_currency:
        return Decimal(1), False
    text = value.strip()
    if not text:
        if day is not None and lookup is not None:
            stored = lookup(day)
            if stored is not None and stored > 0:
                return stored, True
        raise InvalidInvestmentTransaction("fx_to_base", "fx_needed")
    fx = _decimal(text, locale, "fx_to_base", allow_zero=False, code="invalid_fx")
    if fx <= 0:
        raise InvalidInvestmentTransaction("fx_to_base", "invalid_fx")
    return fx, False


def _amounts(value: str, instrument: Instrument, base_currency: str) -> str:
    wanted = value.strip() or AMOUNTS_QUOTE
    if wanted not in (AMOUNTS_QUOTE, AMOUNTS_BASE):
        raise InvalidInvestmentTransaction("amounts", "invalid_amounts")
    if wanted == AMOUNTS_BASE and (
        instrument.quote_currency == base_currency or instrument.type == "currency"
    ):
        raise InvalidInvestmentTransaction("amounts", "settlement_not_allowed")
    return wanted


def _note(value: str) -> str:
    try:
        note = clean_description(value)
    except EntryError as exc:
        raise InvalidInvestmentTransaction("note", f"invalid_{exc.code}") from None
    if len(note) > MAX_NOTE_LENGTH:
        raise InvalidInvestmentTransaction("note", "invalid_note")
    return note


def _income_category(session: Session, ctx: Context, category_id: UUID | None) -> UUID:
    if category_id is None:
        raise InvalidInvestmentTransaction("income_category", "income_category_required")
    category = CategoryRepository(session, ctx).get(category_id)
    if category is None or category.kind != "income" or category.archived_at is not None:
        raise InvalidInvestmentTransaction("income_category", "income_category_invalid")
    return category.id


def transaction_values(
    *,
    instrument: Instrument,
    base_currency: str,
    type_value: str,
    day: date | None,
    quantity_value: str,
    unit_price_value: str,
    fee_value: str,
    fx_value: str,
    note_value: str,
    locale: str,
    amounts_value: str = AMOUNTS_QUOTE,
    base_unit_price_value: str = "",
    base_fee_value: str = "",
    fx_lookup: FxLookup | None = None,
) -> TransactionValues:
    """Validate one transaction's fields for ``instrument`` (shared by the form and bulk add).

    Raises ``InvalidInvestmentTransaction`` naming the first failing field.
    """
    kind = _type(type_value)
    if kind == InvestmentTransactionType.DIVIDEND.value:
        if day is None:
            raise InvalidInvestmentTransaction("date", "invalid_date")
        gross = _decimal(unit_price_value, locale, "unit_price", allow_zero=False)
        _currency_amount(gross, instrument.quote_currency)
        withholding = _fee_minor(fee_value or "0", locale, instrument.quote_currency)
        gross_minor = int(gross.scaleb(minor_unit_for(instrument.quote_currency)))
        if withholding > gross_minor:
            raise InvalidInvestmentTransaction("fee", "invalid_fee")
        fx, stored = _fx(
            fx_value,
            locale,
            quote_currency=instrument.quote_currency,
            base_currency=base_currency,
            day=day,
            lookup=fx_lookup,
        )
        return TransactionValues(
            day=day,
            type=kind,
            quantity=Decimal(1),
            unit_price=gross,
            fee_minor=withholding,
            fx_to_base=fx,
            note=_note(note_value),
            fx_stored=stored,
        )
    if kind == InvestmentTransactionType.SPLIT.value:
        if day is None:
            raise InvalidInvestmentTransaction("date", "invalid_date")
        if instrument.type in {"currency", "bond_model"}:
            raise InvalidInvestmentTransaction("type", "invalid_split")
        new_units = _decimal(quantity_value, locale, "quantity", allow_zero=False)
        old_units = _decimal(unit_price_value, locale, "unit_price", allow_zero=False)
        if new_units == old_units:
            raise InvalidInvestmentTransaction("unit_price", "invalid_split")
        return TransactionValues(
            day=day,
            type=kind,
            quantity=new_units,
            unit_price=old_units,
            fee_minor=0,
            fx_to_base=Decimal(1),
            note=_note(note_value),
        )
    quantity = _decimal(quantity_value, locale, "quantity", allow_zero=False)
    amounts = _amounts(amounts_value, instrument, base_currency)
    note = _note(note_value)
    if amounts == AMOUNTS_BASE:
        settlement_price = _decimal(
            base_unit_price_value,
            locale,
            "base_unit_price",
            allow_zero=True,
            code="invalid_unit_price",
        )
        settlement_fee = _fee_minor(base_fee_value, locale, base_currency, field="base_fee")
        fx, stored = _fx(
            fx_value,
            locale,
            quote_currency=instrument.quote_currency,
            base_currency=base_currency,
            day=day,
            lookup=fx_lookup,
        )
        unit_price, fee_minor = derive_quote_values(
            settlement_price,
            settlement_fee,
            fx,
            base_currency=base_currency,
            quote_currency=instrument.quote_currency,
        )
        if not _fits_decimal_type(unit_price):
            raise InvalidInvestmentTransaction("base_unit_price", "invalid_unit_price")
        if fee_minor > MAX_AMOUNT_MINOR:
            raise InvalidInvestmentTransaction("base_fee", "invalid_fee")
        return TransactionValues(
            day=day,
            type=kind,
            quantity=quantity,
            unit_price=unit_price,
            fee_minor=fee_minor,
            fx_to_base=fx,
            note=note,
            settlement_currency=base_currency,
            settlement_unit_price=settlement_price,
            settlement_fee_minor=settlement_fee,
            fx_stored=stored,
        )
    if instrument.type == "currency":
        unit_price = Decimal(1)
    else:
        unit_price = _decimal(unit_price_value, locale, "unit_price", allow_zero=True)
    fee_minor = _fee_minor(fee_value, locale, instrument.quote_currency)
    fx, stored = _fx(
        fx_value,
        locale,
        quote_currency=instrument.quote_currency,
        base_currency=base_currency,
        day=day,
        lookup=fx_lookup,
    )
    return TransactionValues(
        day=day,
        type=kind,
        quantity=quantity,
        unit_price=unit_price,
        fee_minor=fee_minor,
        fx_to_base=fx,
        note=note,
        fx_stored=stored,
    )


def stored_fx_lookup(factory: sessionmaker[Session], ctx: Context, quote_currency: str) -> FxLookup:
    """Stored FX for one quote currency by date (``stored_fx_rates``, never transaction FX)."""

    def lookup(day: date) -> Decimal | None:
        return stored_fx_rates(factory, ctx, {quote_currency}, day).get(quote_currency.upper())

    return lookup


def _form_values(
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
    amounts_value: str,
    base_unit_price_value: str,
    base_fee_value: str,
) -> TransactionValues:
    day = _day(day_value, today, required=day_required)
    return transaction_values(
        instrument=instrument,
        base_currency=base_currency,
        type_value=type_value,
        day=day,
        quantity_value=quantity_value,
        unit_price_value=unit_price_value,
        fee_value=fee_value,
        fx_value=fx_value,
        note_value=note_value,
        locale=locale,
        amounts_value=amounts_value,
        base_unit_price_value=base_unit_price_value,
        base_fee_value=base_fee_value,
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
    *,
    changed: tuple[str, ...] | None = None,
) -> None:
    data: dict[str, str | int | bool] = {
        "instrument": instrument.name,
        "transaction_type": row.type,
        "date": row.date.isoformat() if row.date is not None else "",
        "quantity": format(row.quantity, "f"),
        "price": format(row.unit_price, "f"),
        "fee": format(Money(row.fee_minor, row.quote_currency).to_decimal(), "f"),
        "currency": row.quote_currency,
        "fx": format(row.fx_to_base, "f") if row.fx_to_base is not None else "",
    }
    if changed is not None:
        # F6.2a: the names of the changed fields (settlement ones included), no amounts.
        data["changed"] = ",".join(changed)
    AuditEventRepository(session, ctx).record(
        actor_user_id=ctx.user.id,
        action=action,
        entity_type="investment_transaction",
        entity_id=str(row.id),
        ts=ctx.clock.now(),
        data=data,
    )


def _view(
    row: InvestmentTransaction, instrument: Instrument, base_currency: str
) -> InvestmentTransactionView:
    settlement_fee = (
        Money(row.settlement_fee_minor, row.settlement_currency)
        if row.settlement_currency is not None and row.settlement_fee_minor is not None
        else None
    )
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
        settlement_unit_price=row.settlement_unit_price if settlement_fee is not None else None,
        settlement_fee=settlement_fee,
        income_transaction_id=row.income_transaction_id,
        fx_needs_check=needs_fx_check(
            quote_currency=row.quote_currency,
            base_currency=base_currency,
            fx_to_base=row.fx_to_base,
        ),
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
    fx_check: bool = False,
) -> TransactionList:
    """One page of live transactions; ``fx_check`` keeps only rows needing an FX check (F6.2a)."""
    require_role(ctx, Role.VIEWER)
    if type_value and type_value not in {"buy", "sell"}:
        raise InvalidInvestmentTransaction("type", "invalid_type")
    start = _parse_filter_day(date_from, "from")
    end = _parse_filter_day(date_to, "to")
    if start is not None and end is not None and end < start:
        raise InvalidInvestmentTransaction("to", "invalid_date")
    wanted_page = max(page, 1)
    types = (type_value,) if type_value else ()
    with factory() as session:
        base_currency = CurrentWorkspaceRepository(session, ctx).get().base_currency
        repository = InvestmentTransactionRepository(session, ctx)
        if fx_check:
            matching = [
                row
                for row in repository.filtered_not_in_currency(
                    base_currency,
                    instrument_id=instrument_id,
                    types=types,
                    date_from=start,
                    date_to=end,
                )
                if needs_fx_check(
                    quote_currency=row.quote_currency,
                    base_currency=base_currency,
                    fx_to_base=row.fx_to_base,
                )
            ]
            total = len(matching)
            pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
            current = _page_number(wanted_page, pages, total)
            rows = matching[(current - 1) * PAGE_SIZE : current * PAGE_SIZE]
        else:
            total = repository.count_filtered(
                instrument_id=instrument_id,
                types=types,
                date_from=start,
                date_to=end,
            )
            pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
            current = _page_number(wanted_page, pages, total)
            rows = repository.filtered(
                instrument_id=instrument_id,
                types=types,
                date_from=start,
                date_to=end,
                limit=PAGE_SIZE,
                offset=(current - 1) * PAGE_SIZE,
            )
        instruments = _instrument_map(session, ctx)
        views = tuple(_view(row, instruments[row.instrument_id], base_currency) for row in rows)
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
        base_currency = CurrentWorkspaceRepository(session, ctx).get().base_currency
        view = _view(row, instrument, base_currency)
        if row.income_transaction_id is not None:
            income = TransactionRepository(session, ctx).get(row.income_transaction_id)
            if income is not None:
                view = replace(view, income_category_id=income.category_id)
        return view


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
        views = tuple(_view(row, model, base_currency) for row in rows)
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
    amounts_value: str = AMOUNTS_QUOTE,
    base_unit_price_value: str = "",
    base_fee_value: str = "",
    record_income: bool = False,
    income_category_id: UUID | None = None,
) -> InvestmentTransactionView:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> InvestmentTransactionView:
        return _create_transaction_in_session(
            session,
            ctx,
            instrument_id=instrument_id,
            type_value=type_value,
            date_value=date_value,
            quantity_value=quantity_value,
            unit_price_value=unit_price_value,
            fee_value=fee_value,
            fx_value=fx_value,
            note_value=note_value,
            locale=locale,
            amounts_value=amounts_value,
            base_unit_price_value=base_unit_price_value,
            base_fee_value=base_fee_value,
            record_income=record_income,
            income_category_id=income_category_id,
        )

    return run_immediate(factory, write)


def _create_transaction_in_session(
    session: Session,
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
    amounts_value: str = AMOUNTS_QUOTE,
    base_unit_price_value: str = "",
    base_fee_value: str = "",
    record_income: bool = False,
    income_category_id: UUID | None = None,
) -> InvestmentTransactionView:
    """``create_transaction`` inside the caller's write transaction.

    Field parsing (``transaction_values``), settlement, linked income, the oversell check and
    audit are exactly the public write's; only the per-call transaction is skipped.
    """
    today = ctx.clock.today(ctx.workspace.timezone)
    require_role_now(session, ctx, Role.EDITOR)
    _lock(session, ctx)
    lock_month_writes(session, ctx)
    base_currency, method = _workspace_values(session, ctx)
    instruments = InstrumentRepository(session, ctx)
    instrument = instruments.get_for_update(instrument_id)
    if instrument is None:
        raise NotFound
    if instrument.archived_at is not None:
        raise InstrumentArchived
    values = _form_values(
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
        amounts_value=amounts_value,
        base_unit_price_value=base_unit_price_value,
        base_fee_value=base_fee_value,
    )
    category_id: UUID | None = None
    if record_income:
        if values.type != InvestmentTransactionType.DIVIDEND.value:
            raise InvalidInvestmentTransaction("income_category", "invalid_income")
        category_id = _income_category(session, ctx, income_category_id)
        if values.day is not None:
            ensure_date_open(session, ctx, values.day)
    repository = InvestmentTransactionRepository(session, ctx)
    row = repository.create(
        instrument_id=instrument.id,
        date=values.day,
        type=values.type,
        quantity=values.quantity,
        unit_price=values.unit_price,
        quote_currency=instrument.quote_currency,
        fee_minor=values.fee_minor,
        fx_to_base=values.fx_to_base,
        note=values.note,
        settlement_currency=values.settlement_currency,
        settlement_unit_price=values.settlement_unit_price,
        settlement_fee_minor=values.settlement_fee_minor,
    )
    if category_id is not None:
        try:
            income_id = create_linked_income(
                session,
                ctx,
                instrument=instrument,
                dividend=row,
                category_id=category_id,
                base_currency=base_currency,
                locale=locale,
            )
        except ValueError:
            raise InvalidInvestmentTransaction("income_category", "income_zero") from None
        repository.update(row.id, income_transaction_id=income_id)
    session.flush()
    _check_oversell(
        session,
        ctx,
        instrument.id,
        method=method,
        base_currency=base_currency,
    )
    _audit(session, ctx, "investment_transaction.created", row, instrument)
    return _view(row, instrument, base_currency)


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
    amounts_value: str = AMOUNTS_QUOTE,
    base_unit_price_value: str = "",
    base_fee_value: str = "",
    record_income: bool = False,
    income_category_id: UUID | None = None,
) -> InvestmentTransactionView:
    require_role(ctx, Role.EDITOR)
    today = ctx.clock.today(ctx.workspace.timezone)

    def write(session: Session) -> InvestmentTransactionView:
        require_role_now(session, ctx, Role.EDITOR)
        _lock(session, ctx)
        lock_month_writes(session, ctx)
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
        values = _form_values(
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
            amounts_value=amounts_value,
            base_unit_price_value=base_unit_price_value,
            base_fee_value=base_fee_value,
        )
        old_income_id = row.income_transaction_id
        new_income = values.type == InvestmentTransactionType.DIVIDEND.value and record_income
        if record_income and not new_income:
            raise InvalidInvestmentTransaction("income_category", "invalid_income")
        category_id = _income_category(session, ctx, income_category_id) if new_income else None
        if old_income_id is not None or new_income:
            if row.date is not None:
                ensure_date_open(session, ctx, row.date)
            if values.day is not None:
                ensure_date_open(session, ctx, values.day)
        changes: dict[str, object] = {
            field: value
            for field, value in (
                ("date", values.day),
                ("type", values.type),
                ("quantity", values.quantity),
                ("unit_price", values.unit_price),
                ("fee_minor", values.fee_minor),
                ("fx_to_base", values.fx_to_base),
                ("note", values.note),
                ("settlement_currency", values.settlement_currency),
                ("settlement_unit_price", values.settlement_unit_price),
                ("settlement_fee_minor", values.settlement_fee_minor),
            )
            if getattr(row, field) != value
        }
        if old_income_id is not None and not new_income:
            remove_linked_income(session, ctx, row, now=ctx.clock.now())
            changes["income_transaction_id"] = None
        if changes:
            repository.update(row.id, **changes)
        if new_income:
            if old_income_id is not None and row.type == InvestmentTransactionType.DIVIDEND.value:
                try:
                    update_linked_income(
                        session,
                        ctx,
                        instrument=instrument,
                        dividend=row,
                        category_id=category_id,  # type: ignore[arg-type]
                        base_currency=base_currency,
                        locale=locale,
                    )
                except ValueError:
                    raise InvalidInvestmentTransaction("income_category", "income_zero") from None
            else:
                try:
                    income_id = create_linked_income(
                        session,
                        ctx,
                        instrument=instrument,
                        dividend=row,
                        category_id=category_id,  # type: ignore[arg-type]
                        base_currency=base_currency,
                        locale=locale,
                    )
                except ValueError:
                    raise InvalidInvestmentTransaction("income_category", "income_zero") from None
                repository.update(row.id, income_transaction_id=income_id)
                changes["income_transaction_id"] = income_id
        if not changes and old_income_id == row.income_transaction_id:
            return _view(row, instrument, base_currency)
        session.flush()
        _check_oversell(
            session,
            ctx,
            instrument.id,
            method=method,
            base_currency=base_currency,
        )
        _audit(
            session,
            ctx,
            "investment_transaction.updated",
            row,
            instrument,
            changed=tuple(sorted(changes)),
        )
        return _view(row, instrument, base_currency)

    return run_immediate(factory, write)


def delete_transaction(
    factory: sessionmaker[Session], ctx: Context, transaction_id: UUID
) -> DeletedInvestmentTransaction:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> DeletedInvestmentTransaction:
        require_role_now(session, ctx, Role.EDITOR)
        _lock(session, ctx)
        lock_month_writes(session, ctx)
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
            if row.income_transaction_id is not None and row.date is not None:
                ensure_date_open(session, ctx, row.date)
                remove_linked_income(session, ctx, row, now=ctx.clock.now())
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
        lock_month_writes(session, ctx)
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
            return _view(row, instrument, base_currency)
        if ctx.clock.now() - row.deleted_at > UNDO_WINDOW:
            raise InvestmentUndoExpired
        if row.date is not None and row.date > today:
            raise InvalidInvestmentTransaction("date", "future_date")
        if row.income_transaction_id is not None and row.date is not None:
            ensure_date_open(session, ctx, row.date)
            restore_linked_income(session, ctx, row)
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
        return _view(row, instrument, base_currency)

    return run_immediate(factory, write)


def instrument_fx_currency(
    factory: sessionmaker[Session], ctx: Context, instrument_id: UUID
) -> str | None:
    """The quote currency whose FX the form asks for, or ``None`` for a base-quoted instrument.

    Read-only and workspace-scoped: another workspace's instrument is ``NotFound`` (F6.2a FX
    re-fill endpoint).
    """
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        instrument = InstrumentRepository(session, ctx).get_or_raise(instrument_id)
        base_currency = CurrentWorkspaceRepository(session, ctx).get().base_currency
        if instrument.quote_currency == base_currency:
            return None
        return instrument.quote_currency


def workspace_has_investment_transactions(session: Session, ctx: Context) -> bool:
    """Part of ``services.workspaces.BASE_CURRENCY_LOCKS``: deleted rows count too."""
    return InvestmentTransactionRepository(session, ctx).any_exists()
