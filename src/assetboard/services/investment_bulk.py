"""Bulk add of investment transactions (F6.2a part B): preview and all-or-nothing create.

The preview parses every line (``domain.investment_bulk``), resolves the instrument by its name
or symbol among active instruments, validates the values exactly like the form
(``transaction_values``, including base-currency amounts and the stored-FX fill-in) and, when
every line is valid, replays each affected instrument's live history with the new rows merged in
date order to catch an oversell. The confirm step re-posts the same text: ``create_bulk`` builds
the preview again inside its one ``run_immediate`` write (workspace lock, role re-check) and
writes nothing unless every line is valid. Hidden preview fields are never trusted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.engine import run_immediate
from assetboard.db.models.investments import Instrument
from assetboard.db.repositories import (
    AuditEventRepository,
    CurrentWorkspaceRepository,
    InstrumentRepository,
    InvestmentTransactionRepository,
)
from assetboard.domain.holdings import (
    CostBasisMethod,
    HoldingTransaction,
    InvalidHoldingTransaction,
    Oversell,
    compute_holdings,
    transaction_total,
)
from assetboard.domain.investment_bulk import (
    BulkTooLarge,
    TooManyBulkLines,
    check_bulk_limits,
    normalized_bulk_text,
    split_transaction_lines,
)
from assetboard.domain.money import Money
from assetboard.domain.text import text_key
from assetboard.i18n.dates import short_date_order
from assetboard.i18n.numbers import parse_amount
from assetboard.services.context import Context, Role, require_role, require_role_now
from assetboard.services.holding_rows import cost_basis_method, holding_transaction
from assetboard.services.investment_transactions import (
    AMOUNTS_BASE,
    AMOUNTS_QUOTE,
    InvalidInvestmentTransaction,
    InvestmentTransactionError,
    OversellRejected,
    TransactionValues,
    stored_fx_lookup,
    transaction_values,
)

__all__ = [
    "BulkLine",
    "BulkPreview",
    "BulkRejected",
    "InvalidBulk",
    "create_bulk",
    "preview_bulk",
]

# Same-day pasted rows keep their line order: each gets a creation time one microsecond apart.
_LINE_STEP = timedelta(microseconds=1)


class InvalidBulk(InvestmentTransactionError):
    field = "bulk"

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class BulkLine:
    """One pasted line: the parsed values, or the first error code."""

    number: int
    text: str
    errors: tuple[str, ...] = ()
    instrument_id: UUID | None = None
    instrument_name: str = ""
    type: str = ""
    day: date | None = None
    quantity: Decimal | None = None
    # The price and fee as entered, in ``entry_currency`` (the base currency in base mode).
    unit_price: Decimal | None = None
    fee: Money | None = None
    entry_currency: str = ""
    amounts: str = AMOUNTS_QUOTE
    fx_to_base: Decimal | None = None
    # The stored FX source when the rate was filled in, else "" (entered, or 1 for the base).
    fx_source: str = ""
    fx_as_of: date | None = None
    total: Money | None = None
    values: TransactionValues | None = None


@dataclass(frozen=True, slots=True)
class BulkPreview:
    text: str
    lines: tuple[BulkLine, ...]
    input_error: str | None = None
    # An oversell of an existing row caused by the batch (no pasted line to name).
    batch_error: str | None = None
    batch_error_on: date | None = None

    @property
    def can_create(self) -> bool:
        return (
            bool(self.lines)
            and self.input_error is None
            and self.batch_error is None
            and all(not line.errors for line in self.lines)
        )

    @property
    def error_count(self) -> int:
        return sum(1 for line in self.lines if line.errors)


class BulkRejected(InvestmentTransactionError):
    """The batch has an error; nothing was written. ``preview`` shows why."""

    code = "bulk_invalid"
    field = "bulk"

    def __init__(self, preview: BulkPreview) -> None:
        super().__init__("bulk_invalid")
        self.preview = preview


def _limits(text: str) -> None:
    try:
        check_bulk_limits(text)
    except BulkTooLarge:
        raise InvalidBulk("bulk_too_large") from None
    except TooManyBulkLines:
        raise InvalidBulk("too_many_lines") from None


def _matches(instruments: list[Instrument], wanted: str) -> list[Instrument]:
    key = text_key(wanted)
    return [
        item
        for item in instruments
        if item.name_key == key or (item.symbol and text_key(item.symbol) == key)
    ]


def _resolve_line(
    session: Session,
    ctx: Context,
    raw_number: int,
    raw_text: str,
    *,
    day: date,
    instrument_text: str,
    type_value: str,
    quantity: str,
    unit_price: str,
    fee: str,
    fx: str,
    currency: str,
    active: list[Instrument],
    archived: list[Instrument],
    base_currency: str,
    locale: str,
) -> BulkLine:
    def error(code: str) -> BulkLine:
        return BulkLine(number=raw_number, text=raw_text, errors=(code,))

    found = _matches(active, instrument_text)
    if len(found) > 1:
        return error("instrument_ambiguous")
    if not found:
        return error(
            "instrument_archived" if _matches(archived, instrument_text) else "instrument_unknown"
        )
    instrument = found[0]
    quote = instrument.quote_currency
    if currency and currency not in (quote, base_currency):
        return error("invalid_amounts_currency")
    amounts = AMOUNTS_BASE if currency == base_currency and base_currency != quote else "quote"
    if quote == base_currency and fx:
        # The base rate is always 1; any other value is a mistake worth showing.
        try:
            if parse_amount(fx, locale) != 1:
                return error("fx_not_one")
        except ValueError:
            return error("fx_not_one")
    if instrument.type != "currency" and not unit_price:
        return error("price_required")
    try:
        values = transaction_values(
            instrument=instrument,
            base_currency=base_currency,
            type_value=type_value,
            day=day,
            quantity_value=quantity,
            unit_price_value=unit_price,
            fee_value=fee or "0",
            fx_value=fx,
            note_value="",
            locale=locale,
            amounts_value=amounts,
            base_unit_price_value=unit_price,
            base_fee_value=fee or "0",
            fx_lookup=stored_fx_lookup(session, ctx, quote, base_currency),
        )
    except InvalidInvestmentTransaction as exc:
        return error(exc.code)
    if amounts == AMOUNTS_BASE:
        assert values.settlement_unit_price is not None
        assert values.settlement_fee_minor is not None
        entry_price = values.settlement_unit_price
        entry_fee = Money(values.settlement_fee_minor, base_currency)
        entry_currency = base_currency
    else:
        entry_price = values.unit_price
        entry_fee = Money(values.fee_minor, quote)
        entry_currency = quote
    return BulkLine(
        number=raw_number,
        text=raw_text,
        instrument_id=instrument.id,
        instrument_name=instrument.name,
        type=values.type,
        day=day,
        quantity=values.quantity,
        unit_price=entry_price,
        fee=entry_fee,
        entry_currency=entry_currency,
        amounts=amounts,
        fx_to_base=values.fx_to_base,
        fx_source=values.fx_source or "",
        fx_as_of=values.fx_as_of,
        values=values,
    )


def _holding_input(
    line: BulkLine, values: TransactionValues, quote: str, created_at: datetime, row_id: UUID
) -> HoldingTransaction:
    assert line.instrument_id is not None
    return HoldingTransaction(
        id=row_id,
        instrument_id=line.instrument_id,
        date=values.day,
        type=values.type,
        quantity=values.quantity,
        unit_price=values.unit_price,
        fee_minor=values.fee_minor,
        quote_currency=quote,
        fx_to_base=values.fx_to_base,
        created_at=created_at,
        settlement_currency=values.settlement_currency,
        settlement_unit_price=values.settlement_unit_price,
        settlement_fee_minor=values.settlement_fee_minor,
    )


def _build_preview(
    session: Session,
    ctx: Context,
    *,
    text: str,
    locale: str,
    today: date,
    now: datetime,
) -> BulkPreview:
    workspace = CurrentWorkspaceRepository(session, ctx).get()
    base_currency = workspace.base_currency
    method: CostBasisMethod = cost_basis_method(workspace)
    instruments = InstrumentRepository(session, ctx).ordered()
    active = [item for item in instruments if item.archived_at is None]
    archived = [item for item in instruments if item.archived_at is not None]
    order, separator = short_date_order(locale)
    raw_lines, problems = split_transaction_lines(
        text, order=order, separator=separator, today=today
    )
    lines: list[BulkLine] = [
        BulkLine(number=problem.number, text=problem.text, errors=(problem.code,))
        for problem in problems
    ]
    for raw in raw_lines:
        lines.append(
            _resolve_line(
                session,
                ctx,
                raw.number,
                raw.text,
                day=raw.day,
                instrument_text=raw.instrument,
                type_value=raw.type,
                quantity=raw.quantity,
                unit_price=raw.unit_price,
                fee=raw.fee,
                fx=raw.fx,
                currency=raw.currency,
                active=active,
                archived=archived,
                base_currency=base_currency,
                locale=locale,
            )
        )
    lines.sort(key=lambda line: line.number)
    input_error = None if normalized_bulk_text(text).strip() and lines else "empty"
    if input_error is not None or any(line.errors for line in lines):
        return BulkPreview(text=text, lines=tuple(lines), input_error=input_error)

    # Totals and the whole-sequence oversell check, with the new rows merged in date order.
    quotes = {item.id: item.quote_currency for item in active}
    new_rows: dict[UUID, list[HoldingTransaction]] = {}
    line_of: dict[UUID, int] = {}
    with_totals: list[BulkLine] = []
    for index, line in enumerate(lines):
        assert line.values is not None and line.instrument_id is not None
        row_id = uuid4()
        holding = _holding_input(
            line,
            line.values,
            quotes[line.instrument_id],
            now + _LINE_STEP * index,
            row_id,
        )
        new_rows.setdefault(line.instrument_id, []).append(holding)
        line_of[row_id] = line.number
        with_totals.append(_with_total(line, transaction_total(holding, base_currency)))
    lines = with_totals
    repository = InvestmentTransactionRepository(session, ctx)
    for instrument_id, added in new_rows.items():
        existing = [holding_transaction(row) for row in repository.for_instrument(instrument_id)]
        try:
            compute_holdings([*existing, *added], method, None, base_currency=base_currency)
        except Oversell as exc:
            number = line_of.get(exc.transaction_id) if exc.transaction_id else None
            if number is None:
                return BulkPreview(
                    text=text,
                    lines=tuple(lines),
                    batch_error="oversell",
                    batch_error_on=exc.on,
                )
            lines = [
                _with_error(line, "oversell") if line.number == number else line for line in lines
            ]
        except InvalidHoldingTransaction:
            return BulkPreview(text=text, lines=tuple(lines), batch_error="invalid")
    return BulkPreview(text=text, lines=tuple(lines))


def _with_total(line: BulkLine, total: Money) -> BulkLine:
    return BulkLine(
        number=line.number,
        text=line.text,
        errors=line.errors,
        instrument_id=line.instrument_id,
        instrument_name=line.instrument_name,
        type=line.type,
        day=line.day,
        quantity=line.quantity,
        unit_price=line.unit_price,
        fee=line.fee,
        entry_currency=line.entry_currency,
        amounts=line.amounts,
        fx_to_base=line.fx_to_base,
        fx_source=line.fx_source,
        fx_as_of=line.fx_as_of,
        total=total,
        values=line.values,
    )


def _with_error(line: BulkLine, code: str) -> BulkLine:
    return BulkLine(
        number=line.number,
        text=line.text,
        errors=(*line.errors, code),
        instrument_id=line.instrument_id,
        instrument_name=line.instrument_name,
        type=line.type,
        day=line.day,
        quantity=line.quantity,
        unit_price=line.unit_price,
        fee=line.fee,
        entry_currency=line.entry_currency,
        amounts=line.amounts,
        fx_to_base=line.fx_to_base,
        fx_source=line.fx_source,
        fx_as_of=line.fx_as_of,
        total=line.total,
        values=line.values,
    )


def preview_bulk(
    factory: sessionmaker[Session], ctx: Context, *, text: str, locale: str
) -> BulkPreview:
    """Parse and check a pasted batch without writing anything (editors and owners)."""
    require_role(ctx, Role.EDITOR)
    _limits(text)
    today = ctx.clock.today(ctx.workspace.timezone)
    with factory() as session:
        return _build_preview(
            session, ctx, text=text, locale=locale, today=today, now=ctx.clock.now()
        )


def create_bulk(factory: sessionmaker[Session], ctx: Context, *, text: str, locale: str) -> int:
    """Create every line of the batch or none of them; returns the number of rows created.

    Raises ``BulkRejected`` (with the preview) when any line, or the merged-history oversell
    check, fails. The rows share one new ``import_batch_id``; one audit event records the count
    and the instrument ids, without amounts.
    """
    require_role(ctx, Role.EDITOR)
    _limits(text)
    today = ctx.clock.today(ctx.workspace.timezone)

    def write(session: Session) -> int:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=False)
        now = ctx.clock.now()
        instruments = InstrumentRepository(session, ctx)
        # Lock the instruments the batch may touch before reading their histories.
        for item in instruments.ordered():
            if item.archived_at is None:
                instruments.get_for_update(item.id)
        preview = _build_preview(session, ctx, text=text, locale=locale, today=today, now=now)
        if not preview.can_create:
            raise BulkRejected(preview)
        workspace = CurrentWorkspaceRepository(session, ctx).get()
        base_currency = workspace.base_currency
        method = cost_basis_method(workspace)
        quotes = {item.id: item.quote_currency for item in instruments.ordered()}
        batch_id = uuid4()
        repository = InvestmentTransactionRepository(session, ctx)
        for index, line in enumerate(preview.lines):
            values = line.values
            assert values is not None and line.instrument_id is not None
            repository.create(
                instrument_id=line.instrument_id,
                date=values.day,
                type=values.type,
                quantity=values.quantity,
                unit_price=values.unit_price,
                quote_currency=quotes[line.instrument_id],
                fee_minor=values.fee_minor,
                fx_to_base=values.fx_to_base,
                note="",
                settlement_currency=values.settlement_currency,
                settlement_unit_price=values.settlement_unit_price,
                settlement_fee_minor=values.settlement_fee_minor,
                import_batch_id=batch_id,
                created_at=now + _LINE_STEP * index,
            )
        session.flush()
        affected = sorted(
            {line.instrument_id for line in preview.lines if line.instrument_id}, key=str
        )
        # The final guard: the stored histories, exactly as the single-row writes check them.
        for instrument_id in affected:
            rows = repository.for_instrument(instrument_id)
            try:
                compute_holdings(
                    [holding_transaction(row) for row in rows],
                    method,
                    None,
                    base_currency=base_currency,
                )
            except Oversell as exc:
                raise OversellRejected(exc.on) from None
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="investment_transaction.bulk_created",
            entity_type="workspace",
            entity_id=str(ctx.workspace.id),
            ts=ctx.clock.now(),
            data={
                "count": len(preview.lines),
                "instruments": ",".join(str(item) for item in affected),
                "batch": str(batch_id),
            },
        )
        return len(preview.lines)

    return run_immediate(factory, write)
