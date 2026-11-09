"""Standalone ``investment_transactions.csv`` import (F6.9)."""

from __future__ import annotations

import contextlib
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy.orm import Session

from assetboard.db.models.import_batch import ImportBatch
from assetboard.db.models.investments import AssetClass, Instrument
from assetboard.db.repositories import (
    AssetClassRepository,
    AuditEventRepository,
    InstrumentAllocationRepository,
    InstrumentRepository,
    InvestmentTransactionRepository,
)
from assetboard.domain.budget import planned_minor
from assetboard.domain.holdings import (
    MAX_NOTE_LENGTH,
    CostBasisMethod,
    HoldingTransaction,
    InvalidHoldingTransaction,
    InvestmentTransactionType,
    Oversell,
    compute_holdings,
)
from assetboard.domain.import_csv import parse_date, parse_decimal, parse_timestamp, parse_uuid
from assetboard.domain.import_upload import DEADLINE_CHECK_ROWS, ParsedUpload, ParseTimeout
from assetboard.domain.instruments import (
    TYPE_DEFAULTS,
    InstrumentType,
    clean_instrument_name,
    clean_quote_currency,
    clean_unit_label,
    name_key,
)
from assetboard.domain.transactions import EntryError, clean_description
from assetboard.i18n import gettext
from assetboard.services.context import Context
from assetboard.services.holding_rows import holding_transaction
from assetboard.services.imports import ImportAdapter
from assetboard.services.prices import stored_fx_rates_in_session

FILE = "investment_transactions.csv"
SOURCE = "investment_transactions_csv"


def _entry(
    code: str, line: int | None = None, column: str = "", **extra: object
) -> dict[str, object]:
    return {
        "file": FILE,
        "line": line or "",
        "column": column,
        "code": code,
        "message": code,
        **extra,
    }


def _clean_note(value: str) -> str:
    try:
        note = clean_description(value)
    except EntryError as error:
        raise EntryError(f"invalid_{error.code}") from None
    if len(note) > MAX_NOTE_LENGTH:
        raise EntryError("invalid_note")
    return note


def _decimal_text(value: str, *, allow_zero: bool) -> Decimal:
    """A DecimalType-range plain decimal; the archive reader already refuses exponents."""
    if value and not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        raise EntryError("invalid_decimal")
    try:
        number = Decimal(value)
    except (ValueError, ArithmeticError):
        raise EntryError("invalid_decimal") from None
    if not number.is_finite() or number < 0 or (number == 0 and not allow_zero):
        raise EntryError("invalid_decimal")
    exponent = number.as_tuple().exponent
    if not isinstance(exponent, int):
        raise EntryError("invalid_decimal")
    scale = max(-exponent, 0)
    integer_digits = 0 if number.is_zero() else max(number.adjusted() + 1, 0)
    if scale > 18 or integer_digits > 20 or integer_digits + scale > 38:
        raise EntryError("invalid_decimal")
    return number


def _number(value: str, *, decimal: str, allow_zero: bool) -> Decimal:
    parsed = parse_decimal(value, decimal=decimal)
    return _decimal_text(format(parsed, "f"), allow_zero=allow_zero)


@dataclass(frozen=True, slots=True)
class _Row:
    line: int
    instrument_name: str
    instrument_key: str
    day: date | None
    type: str
    quantity: Decimal
    unit_price: Decimal
    file_currency: str | None
    fee_text: str
    fx_to_base: Decimal | None
    note: str
    settlement_currency: str | None
    settlement_unit_price: Decimal | None
    settlement_fee_text: str
    source_id: UUID | None
    deleted_at: datetime | None


@dataclass(frozen=True, slots=True)
class _NewInstrument:
    name: str
    key: str
    type: str
    unit_label: str
    quote_currency: str
    valuation_source: str


@dataclass(frozen=True, slots=True)
class _Mapping:
    key: str
    instrument_name: str
    line: int
    action: str
    instrument_id: UUID | None
    create_name: str
    create_type: str
    create_unit_label: str
    quote_currency: str
    rows: tuple[_Row, ...]
    valid: bool
    quotes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Match:
    row: _Row
    instrument_id: UUID | None
    instrument_name: str
    new_instrument: _NewInstrument | None
    quote_currency: str
    fee_minor: int
    fx_to_base: Decimal
    unit_price: Decimal
    settlement_unit_price: Decimal | None
    settlement_fee_minor: int | None
    created_at: datetime
    transaction_id: UUID


@dataclass(slots=True)
class _Plan:
    matches: list[_Match] = field(default_factory=list[_Match])
    mapping: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    mapping_errors: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    errors: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    skipped: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    warnings: list[dict[str, object]] = field(default_factory=list[dict[str, object]])


def _parse_rows(
    parsed: ParsedUpload, *, decimal: str, out_of_time: Callable[[], bool]
) -> tuple[list[_Row], list[dict[str, object]]]:
    header = parsed.csv_header
    rows: list[_Row] = []
    errors: list[dict[str, object]] = []
    for index, cells in enumerate(parsed.csv_rows):
        line = index + 2
        if index % DEADLINE_CHECK_ROWS == 0 and out_of_time():
            raise ParseTimeout
        record = dict(zip(header, cells, strict=True))
        row_errors: list[dict[str, object]] = []
        try:
            instrument_name = clean_instrument_name(record.get("instrument", ""))
        except ValueError:
            row_errors.append(_entry("invalid_instrument_name", line, "instrument"))
            instrument_name = ""
        try:
            type_value = InvestmentTransactionType(record.get("type", "")).value
        except ValueError:
            row_errors.append(_entry("invalid_transaction_type", line, "type"))
            type_value = "buy"
        raw_date = record.get("date", "").strip()
        day: date | None = None
        if raw_date:
            try:
                day = parse_date(raw_date)
            except EntryError as error:
                row_errors.append(_entry(error.code, line, "date"))
        try:
            quantity = _number(
                record.get("quantity", ""),
                decimal=decimal,
                allow_zero=type_value
                not in {
                    InvestmentTransactionType.BUY.value,
                    InvestmentTransactionType.SELL.value,
                },
            )
        except EntryError as error:
            row_errors.append(_entry(error.code, line, "quantity"))
            quantity = Decimal(0)
        try:
            unit_price = _number(record.get("unit_price", ""), decimal=decimal, allow_zero=True)
        except EntryError as error:
            row_errors.append(_entry(error.code, line, "unit_price"))
            unit_price = Decimal(0)
        raw_currency = record.get("quote_currency", "").strip().upper()
        file_currency: str | None = raw_currency or None
        if file_currency is not None:
            try:
                file_currency = clean_quote_currency(file_currency)
            except ValueError:
                row_errors.append(_entry("invalid_currency", line, "quote_currency"))
                file_currency = None
        raw_fee = record.get("fee", "").strip() or "0"
        try:
            fee_text = format(_number(raw_fee, decimal=decimal, allow_zero=True), "f")
        except EntryError as error:
            row_errors.append(_entry(error.code, line, "fee"))
            fee_text = "0"
        fx_value: Decimal | None = None
        raw_fx = record.get("fx_to_base", "").strip()
        if raw_fx:
            try:
                fx_value = _number(raw_fx, decimal=decimal, allow_zero=False)
            except EntryError as error:
                row_errors.append(_entry(error.code, line, "fx_to_base"))
        try:
            note = _clean_note(record.get("note", ""))
        except EntryError as error:
            row_errors.append(_entry(error.code, line, "note"))
            note = ""
        raw_settlement_currency = record.get("settlement_currency", "").strip().upper()
        settlement_currency: str | None = raw_settlement_currency or None
        if settlement_currency is not None:
            try:
                settlement_currency = clean_quote_currency(settlement_currency)
            except ValueError:
                row_errors.append(_entry("invalid_currency", line, "settlement_currency"))
                settlement_currency = None
        settlement_unit_price: Decimal | None = None
        raw_settlement_price = record.get("settlement_unit_price", "").strip()
        if raw_settlement_price:
            try:
                settlement_unit_price = _number(
                    raw_settlement_price, decimal=decimal, allow_zero=True
                )
            except EntryError as error:
                row_errors.append(_entry(error.code, line, "settlement_unit_price"))
        raw_settlement_fee = record.get("settlement_fee", "").strip()
        try:
            settlement_fee_text = (
                format(_number(raw_settlement_fee, decimal=decimal, allow_zero=True), "f")
                if raw_settlement_fee
                else ""
            )
        except EntryError as error:
            row_errors.append(_entry(error.code, line, "settlement_fee"))
            settlement_fee_text = ""
        if any(
            (
                settlement_currency is not None,
                settlement_unit_price is not None,
                bool(settlement_fee_text),
            )
        ) and not all(
            (
                settlement_currency is not None,
                settlement_unit_price is not None,
                bool(settlement_fee_text),
            )
        ):
            row_errors.append(_entry("invalid_settlement", line, "settlement_currency"))
        source_id: UUID | None = None
        raw_id = record.get("id", "").strip()
        if raw_id:
            try:
                source_id = parse_uuid(raw_id)
            except EntryError as error:
                row_errors.append(_entry(error.code, line, "id"))
        deleted_at: datetime | None = None
        raw_deleted = record.get("deleted_at", "").strip()
        if raw_deleted:
            try:
                deleted_at = parse_timestamp(raw_deleted)
            except EntryError as error:
                row_errors.append(_entry(error.code, line, "deleted_at"))
        if (
            type_value
            in {
                InvestmentTransactionType.DIVIDEND.value,
                InvestmentTransactionType.SPLIT.value,
            }
            and day is None
        ):
            row_errors.append(_entry("invalid_date", line, "date"))
        if type_value == InvestmentTransactionType.DIVIDEND.value:
            if quantity != 1:
                row_errors.append(_entry("invalid_quantity", line, "quantity"))
            if unit_price <= 0:
                row_errors.append(_entry("invalid_amount", line, "unit_price"))
        if type_value == InvestmentTransactionType.SPLIT.value:
            if quantity <= 0 or unit_price <= 0 or quantity == unit_price:
                row_errors.append(_entry("invalid_split", line, "unit_price"))
            if record.get("fx_to_base", "").strip():
                row_errors.append(_entry("invalid_split", line, "fx_to_base"))
        if type_value in {
            InvestmentTransactionType.DIVIDEND.value,
            InvestmentTransactionType.SPLIT.value,
        } and any(
            record.get(column, "").strip()
            for column in (
                "settlement_currency",
                "settlement_unit_price",
                "settlement_fee",
            )
        ):
            row_errors.append(_entry("invalid_settlement", line, "settlement_currency"))
        if row_errors:
            errors.extend(row_errors)
            continue
        rows.append(
            _Row(
                line=line,
                instrument_name=instrument_name,
                instrument_key=name_key(instrument_name),
                day=day,
                type=type_value,
                quantity=quantity,
                unit_price=unit_price,
                file_currency=file_currency,
                fee_text=fee_text,
                fx_to_base=fx_value,
                note=note,
                settlement_currency=settlement_currency,
                settlement_unit_price=settlement_unit_price,
                settlement_fee_text=settlement_fee_text,
                source_id=source_id,
                deleted_at=deleted_at,
            )
        )
    return rows, errors


def _mapping_choices(batch: ImportBatch) -> dict[str, Mapping[str, object]]:
    raw = batch.options.get("instrument_mappings")
    if not isinstance(raw, list):
        return {}
    choices: dict[str, Mapping[str, object]] = {}
    for item in cast("list[object]", raw):
        if not isinstance(item, dict):
            continue
        entry = cast("dict[str, object]", item)
        key = entry.get("source_name_key")
        if isinstance(key, str):
            choices[key] = entry
    return choices


def _clean_create_name(value: str) -> str:
    try:
        return clean_instrument_name(value)
    except ValueError:
        raise EntryError("invalid_instrument_name") from None


def _clean_create_unit(value: str, type_value: str, quote_currency: str) -> str:
    if type_value == InstrumentType.CURRENCY.value:
        return quote_currency
    try:
        return clean_unit_label(
            value, instrument_type=InstrumentType(type_value), quote_currency=quote_currency
        )
    except ValueError:
        raise EntryError("invalid_unit_label") from None


def _other_class(classes: list[AssetClass], ctx: Context) -> AssetClass | None:
    active = [row for row in classes if row.archived_at is None]
    if not active:
        return None
    wanted = {
        name_key(gettext("Other", ctx.workspace.locale)),
        name_key("Other"),
    }
    for row in active:
        if row.name_key in wanted:
            return row
    return active[0]


def _stored_fx(session: Session, ctx: Context, currency: str, day: date | None) -> Decimal | None:
    if day is None:
        return None
    return stored_fx_rates_in_session(session, ctx, {currency}, day).get(currency.upper())


def _mappings(
    session: Session,
    ctx: Context,
    batch: ImportBatch,
    rows: list[_Row],
    errors: list[dict[str, object]],
    *,
    base_currency: str,
) -> tuple[list[_Mapping], AssetClass | None]:
    grouped: dict[str, list[_Row]] = {}
    for row in rows:
        grouped.setdefault(row.instrument_key, []).append(row)
    instruments = InstrumentRepository(session, ctx).ordered()
    active_by_key = {row.name_key: row for row in instruments if row.archived_at is None}
    active_by_id = {row.id: row for row in instruments if row.archived_at is None}
    classes = AssetClassRepository(session, ctx).ordered()
    target_class = _other_class(classes, ctx)
    choices = _mapping_choices(batch)
    mappings: list[_Mapping] = []
    for key, group_rows in grouped.items():
        first = group_rows[0]
        existing = active_by_key.get(key)
        choice = choices.get(key, {})
        action = str(choice.get("action", "map" if existing is not None else "create"))
        if action not in {"map", "create", "ignore"}:
            action = "ignore"
        raw_instrument_id = str(choice.get("instrument_id", "") or "")
        instrument_id: UUID | None = None
        if raw_instrument_id:
            try:
                instrument_id = UUID(raw_instrument_id)
            except ValueError:
                instrument_id = None
        chosen = active_by_id.get(instrument_id) if instrument_id is not None else None
        if action == "map" and chosen is None:
            # Fall back to the current active instrument with this name.
            chosen = existing
            instrument_id = chosen.id if chosen is not None else None
        quotes = tuple(sorted({row.file_currency for row in group_rows if row.file_currency}))
        quote_currency = (
            quotes[0]
            if quotes
            else (chosen.quote_currency if chosen is not None else base_currency)
        )
        create_name = str(choice.get("create_name", first.instrument_name) or first.instrument_name)
        create_type = str(choice.get("create_type", "") or "")
        create_unit_label = str(choice.get("create_unit_label", "") or "")
        raw_quote = str(choice.get("quote_currency", "") or "").strip().upper()
        if raw_quote:
            try:
                quote_currency = clean_quote_currency(raw_quote)
            except ValueError:
                errors.append(_entry("invalid_currency", first.line, "quote_currency"))
        valid = True
        line = first.line
        if len(quotes) > 1:
            errors.append(
                _entry(
                    "instrument_currency_mismatch",
                    line,
                    "quote_currency",
                    instrument=first.instrument_name,
                )
            )
            valid = False
        if action == "map":
            if chosen is None:
                errors.append(_entry("instrument_not_found", line, "instrument"))
                valid = False
            elif quotes and quotes[0] != chosen.quote_currency:
                errors.append(
                    _entry(
                        "instrument_currency_mismatch",
                        line,
                        "quote_currency",
                        instrument=first.instrument_name,
                    )
                )
                valid = False
            else:
                quote_currency = chosen.quote_currency
        elif action == "create":
            if target_class is None:
                errors.append(_entry("no_active_asset_class", line, "instrument"))
                valid = False
            try:
                create_name = _clean_create_name(create_name)
            except EntryError as error:
                errors.append(_entry(error.code, line, "create_name"))
                valid = False
            else:
                if name_key(create_name) in active_by_key:
                    errors.append(_entry("duplicate_instrument_name", line, "create_name"))
                    valid = False
            if create_type not in {kind.value for kind in InstrumentType}:
                errors.append(_entry("invalid_instrument_type", line, "create_type"))
                valid = False
            elif (
                create_type
                in {
                    InstrumentType.CURRENCY.value,
                    InstrumentType.BOND_MODEL.value,
                }
                and quote_currency == ""
            ):
                errors.append(_entry("invalid_currency", line, "quote_currency"))
                valid = False
            else:
                try:
                    create_unit_label = _clean_create_unit(
                        create_unit_label, create_type, quote_currency
                    )
                except EntryError as error:
                    errors.append(_entry(error.code, line, "create_unit_label"))
                    valid = False
            try:
                quote_currency = clean_quote_currency(quote_currency)
            except ValueError:
                errors.append(_entry("invalid_currency", line, "quote_currency"))
                valid = False
        mappings.append(
            _Mapping(
                key=key,
                instrument_name=first.instrument_name,
                line=line,
                action=action,
                instrument_id=chosen.id if chosen is not None else None,
                create_name=create_name,
                create_type=create_type,
                create_unit_label=create_unit_label,
                quote_currency=quote_currency,
                rows=tuple(group_rows),
                valid=valid,
                quotes=quotes,
            )
        )
    return mappings, target_class


def _plan(
    session: Session,
    ctx: Context,
    batch: ImportBatch,
    parsed: ParsedUpload,
    *,
    base_currency: str,
    out_of_time: Callable[[], bool],
) -> _Plan:
    plan = _Plan()
    decimal = "comma" if batch.options.get("decimal") == "comma" else "dot"
    rows, errors = _parse_rows(parsed, decimal=decimal, out_of_time=out_of_time)
    plan.errors.extend(errors)
    mappings, _target_class = _mappings(
        session, ctx, batch, rows, plan.errors, base_currency=base_currency
    )
    instruments = InstrumentRepository(session, ctx).ordered()
    active_by_id = {row.id: row for row in instruments if row.archived_at is None}
    stored_ids: set[UUID] = set()
    for transaction in InvestmentTransactionRepository(session, ctx).list():
        stored_ids.add(transaction.id)
        if transaction.import_key and transaction.import_key.startswith("id:"):
            with contextlib.suppress(ValueError):
                stored_ids.add(UUID(transaction.import_key[3:]))
    seen_ids: set[UUID] = set()
    now = ctx.clock.now()
    today = ctx.clock.today(ctx.workspace.timezone)
    match_index = 0
    seen_create_keys: set[str] = set()
    for mapping in mappings:
        entry: dict[str, object] = {
            "source_name_key": mapping.key,
            "instrument": mapping.instrument_name,
            "line": mapping.line,
            "action": mapping.action,
            "instrument_id": str(mapping.instrument_id or ""),
            "create_name": mapping.create_name,
            "create_type": mapping.create_type,
            "create_unit_label": mapping.create_unit_label,
            "quote_currency": mapping.quote_currency,
            "rows": len(mapping.rows),
        }
        plan.mapping.append(entry)
        if not mapping.valid:
            plan.mapping_errors.append(
                _entry("invalid_instrument_mapping", mapping.line, "instrument")
            )
            continue
        if mapping.action == "create":
            create_key = name_key(mapping.create_name)
            if create_key in seen_create_keys:
                plan.mapping_errors.append(
                    _entry("duplicate_instrument_name", mapping.line, "create_name")
                )
                continue
            seen_create_keys.add(create_key)
        if mapping.action == "ignore":
            plan.skipped.extend(
                _entry(
                    "ignored_instrument", row.line, "instrument", instrument=mapping.instrument_name
                )
                for row in mapping.rows
            )
            continue
        instrument = (
            active_by_id.get(mapping.instrument_id)
            if mapping.action == "map" and mapping.instrument_id is not None
            else None
        )
        if mapping.action == "map" and instrument is None:
            plan.mapping_errors.append(_entry("instrument_not_found", mapping.line, "instrument"))
            continue
        new_instrument: _NewInstrument | None = None
        if mapping.action == "create":
            assert mapping.create_type
            defaults = TYPE_DEFAULTS[InstrumentType(mapping.create_type)]
            new_instrument = _NewInstrument(
                name=mapping.create_name,
                key=name_key(mapping.create_name),
                type=mapping.create_type,
                unit_label=mapping.create_unit_label,
                quote_currency=mapping.quote_currency,
                valuation_source=defaults.valuation_source.value,
            )
        quote_currency = (
            instrument.quote_currency if instrument is not None else mapping.quote_currency
        )
        instrument_name = instrument.name if instrument is not None else mapping.create_name
        instrument_id = instrument.id if instrument is not None else None
        for row in mapping.rows:
            if row.deleted_at is not None:
                plan.skipped.append(_entry("deleted_in_source", row.line, "deleted_at"))
                continue
            if row.day is not None and row.day > today:
                plan.skipped.append(_entry("future_date", row.line, "date"))
                continue
            if row.file_currency is not None and row.file_currency != quote_currency:
                plan.errors.append(
                    _entry("instrument_currency_mismatch", row.line, "quote_currency")
                )
                continue
            if row.source_id is not None:
                if row.source_id in seen_ids:
                    plan.skipped.append(_entry("duplicate_in_file", row.line, "id"))
                    continue
                seen_ids.add(row.source_id)
                if row.source_id in stored_ids:
                    plan.skipped.append(_entry("already_imported", row.line, "id"))
                    continue
            try:
                fee_minor = planned_minor(
                    _decimal_text(row.fee_text, allow_zero=True), quote_currency
                )
            except (EntryError, ValueError) as error:
                code = getattr(error, "code", "invalid_amount")
                plan.errors.append(_entry(code, row.line, "fee"))
                continue
            target_type = (
                instrument.type
                if instrument is not None
                else (new_instrument.type if new_instrument is not None else "")
            )
            if row.type == InvestmentTransactionType.SPLIT.value:
                if target_type in {
                    InstrumentType.CURRENCY.value,
                    InstrumentType.BOND_MODEL.value,
                }:
                    plan.errors.append(_entry("split_not_allowed", row.line, "type"))
                    continue
                if fee_minor != 0:
                    plan.errors.append(_entry("invalid_fee", row.line, "fee"))
                    continue
                unit_price = row.unit_price
                fx_to_base = Decimal(1)
                settlement_fee_minor = None
            elif row.type == InvestmentTransactionType.DIVIDEND.value:
                try:
                    gross_minor = planned_minor(row.unit_price, quote_currency)
                except (EntryError, ValueError) as error:
                    code = getattr(error, "code", "invalid_amount")
                    plan.errors.append(_entry(code, row.line, "unit_price"))
                    continue
                if gross_minor <= 0:
                    plan.errors.append(_entry("invalid_amount", row.line, "unit_price"))
                    continue
                if fee_minor > gross_minor:
                    plan.errors.append(_entry("withholding_exceeds_gross", row.line, "fee"))
                    continue
                if quote_currency == base_currency:
                    fx_to_base = Decimal(1)
                elif row.fx_to_base is None:
                    plan.errors.append(_entry("fx_needed", row.line, "fx_to_base"))
                    continue
                else:
                    fx_to_base = row.fx_to_base
                unit_price = row.unit_price
                settlement_fee_minor = None
            else:
                unit_price = row.unit_price
                if (
                    instrument is not None and instrument.type == InstrumentType.CURRENCY.value
                ) or (
                    new_instrument is not None
                    and new_instrument.type == InstrumentType.CURRENCY.value
                ):
                    unit_price = Decimal(1)
                fx_to_base = row.fx_to_base
                if quote_currency == base_currency:
                    fx_to_base = fx_to_base or Decimal(1)
                elif fx_to_base is None:
                    if row.settlement_currency is not None:
                        plan.errors.append(_entry("settlement_fx_required", row.line, "fx_to_base"))
                        continue
                    fx_to_base = _stored_fx(session, ctx, quote_currency, row.day)
                    if fx_to_base is None:
                        plan.errors.append(
                            _entry(
                                "fx_needed",
                                row.line,
                                "fx_to_base",
                                currency=quote_currency,
                                date=row.day.isoformat() if row.day else "",
                            )
                        )
                        continue
                if row.settlement_currency is not None:
                    if row.settlement_currency != base_currency:
                        plan.errors.append(
                            _entry("invalid_settlement", row.line, "settlement_currency")
                        )
                        continue
                    if (
                        quote_currency == base_currency
                        or (
                            instrument is not None
                            and instrument.type == InstrumentType.CURRENCY.value
                        )
                        or (
                            new_instrument is not None
                            and new_instrument.type == InstrumentType.CURRENCY.value
                        )
                    ):
                        plan.errors.append(
                            _entry("invalid_settlement", row.line, "settlement_currency")
                        )
                        continue
                    try:
                        settlement_fee_minor = planned_minor(
                            _decimal_text(row.settlement_fee_text, allow_zero=True),
                            base_currency,
                        )
                    except (EntryError, ValueError) as error:
                        code = getattr(error, "code", "invalid_amount")
                        plan.errors.append(_entry(code, row.line, "settlement_fee"))
                        continue
                else:
                    settlement_fee_minor = None
            transaction_id = row.source_id or uuid.uuid4()
            match_index += 1
            plan.matches.append(
                _Match(
                    row=row,
                    instrument_id=instrument_id,
                    instrument_name=instrument_name,
                    new_instrument=new_instrument,
                    quote_currency=quote_currency,
                    fee_minor=fee_minor,
                    fx_to_base=fx_to_base,
                    unit_price=unit_price,
                    settlement_unit_price=row.settlement_unit_price,
                    settlement_fee_minor=settlement_fee_minor,
                    created_at=now + timedelta(microseconds=match_index),
                    transaction_id=transaction_id,
                )
            )
            if row.day is None:
                plan.warnings.append(
                    _entry("undated_lot", row.line, "date", instrument=instrument_name)
                )
    # An oversell in the combined history blocks apply until the file or mapping changes.
    by_instrument: dict[str, list[_Match]] = {}
    for match in plan.matches:
        by_instrument.setdefault(match.instrument_name, []).append(match)
    existing_rows: dict[UUID, list[Any]] = {}
    for row in InvestmentTransactionRepository(session, ctx).live():
        if row.type not in {
            InvestmentTransactionType.BUY.value,
            InvestmentTransactionType.SELL.value,
            InvestmentTransactionType.SPLIT.value,
        }:
            continue
        existing_rows.setdefault(row.instrument_id, []).append(row)
    for matches in by_instrument.values():
        quantity_matches = [
            match
            for match in matches
            if match.row.type
            in {
                InvestmentTransactionType.BUY.value,
                InvestmentTransactionType.SELL.value,
                InvestmentTransactionType.SPLIT.value,
            }
        ]
        if not quantity_matches:
            continue
        instrument = (
            active_by_id.get(quantity_matches[0].instrument_id)
            if quantity_matches[0].instrument_id
            else None
        )
        rows_to_check: list[HoldingTransaction] = []
        if instrument is not None:
            rows_to_check.extend(_holding(row) for row in existing_rows.get(instrument.id, []))
        line_by_id: dict[UUID, int] = {}
        for match in quantity_matches:
            line_by_id[match.transaction_id] = match.row.line
            rows_to_check.append(_incoming_holding(match))
        try:
            compute_holdings(
                rows_to_check,
                CostBasisMethod.AVERAGE,
                None,
                base_currency=base_currency,
            )
        except Oversell as exc:
            line = line_by_id.get(exc.transaction_id) if exc.transaction_id else None
            plan.mapping_errors.append(
                _entry(
                    "oversell",
                    line,
                    "quantity",
                    instrument=matches[0].instrument_name,
                    date=exc.on.isoformat() if exc.on else "",
                )
            )
        except InvalidHoldingTransaction:
            plan.mapping_errors.append(
                _entry(
                    "invalid_investment_history",
                    matches[0].row.line,
                    "quantity",
                    instrument=matches[0].instrument_name,
                )
            )
    return plan


def _holding(row: object) -> HoldingTransaction:
    return holding_transaction(row)  # type: ignore[arg-type]


def _incoming_holding(match: _Match) -> HoldingTransaction:
    return HoldingTransaction(
        id=match.transaction_id,
        instrument_id=match.instrument_id or UUID(int=0),
        date=match.row.day,
        type=match.row.type,
        quantity=match.row.quantity,
        unit_price=match.unit_price,
        fee_minor=match.fee_minor,
        quote_currency=match.quote_currency,
        fx_to_base=match.fx_to_base,
        created_at=match.created_at,
        settlement_currency=match.row.settlement_currency,
        settlement_unit_price=match.settlement_unit_price,
        settlement_fee_minor=match.settlement_fee_minor,
    )


class InvestmentTransactionsCsvAdapter(ImportAdapter):
    """``investment_transactions.csv`` alone, merged into the existing history (F6.9)."""

    source = SOURCE

    def plan(
        self,
        session: Session,
        ctx: Context,
        batch: ImportBatch,
        parsed: ParsedUpload,
        *,
        base_currency: str,
        out_of_time: Callable[[], bool] = lambda: False,
    ) -> dict[str, Any]:
        plan = _plan(
            session,
            ctx,
            batch,
            parsed,
            base_currency=base_currency,
            out_of_time=out_of_time,
        )
        duplicates = sum(1 for entry in plan.skipped if entry["code"] == "already_imported")
        return {
            "counts": {
                "to_create": len(plan.matches),
                "duplicates_skipped": duplicates,
                "conflicts_skipped": 0,
                "invalid": len(plan.errors),
                "mapping_invalid": len(plan.mapping_errors),
                "instruments_to_create": len(
                    {match.new_instrument.key for match in plan.matches if match.new_instrument}
                ),
            },
            "months": [],
            "categories": [],
            "mapping": [],
            "mapping_errors": plan.mapping_errors,
            "instrument_mapping": plan.mapping,
            "closed_months": [],
            "warnings": plan.warnings,
            "skipped": plan.skipped,
            "errors": plan.errors,
        }

    def apply(
        self,
        session: Session,
        ctx: Context,
        batch: ImportBatch,
        parsed: ParsedUpload,
        *,
        base_currency: str,
        skip_invalid: bool,
    ) -> dict[str, int]:
        del skip_invalid
        plan = _plan(
            session,
            ctx,
            batch,
            parsed,
            base_currency=base_currency,
            out_of_time=lambda: False,
        )
        if plan.mapping_errors:
            raise ValueError("the import still has invalid instrument mappings")
        classes = AssetClassRepository(session, ctx).ordered()
        target_class = _other_class(classes, ctx)
        if any(match.new_instrument is not None for match in plan.matches) and target_class is None:
            raise ValueError("the workspace has no active asset class")
        instruments = InstrumentRepository(session, ctx)
        active_by_id = {row.id: row for row in instruments.ordered() if row.archived_at is None}
        allocations = InstrumentAllocationRepository(session, ctx)
        created_instruments: dict[str, Instrument] = {}
        audit = AuditEventRepository(session, ctx)
        now = ctx.clock.now()
        for match in plan.matches:
            spec = match.new_instrument
            if spec is None or spec.key in created_instruments:
                continue
            instrument = instruments.create(
                name=spec.name,
                name_key=spec.key,
                symbol=None,
                type=spec.type,
                quote_currency=spec.quote_currency,
                unit_label=spec.unit_label,
                valuation_source=spec.valuation_source,
                import_batch_id=batch.id,
            )
            created_instruments[spec.key] = instrument
            assert target_class is not None
            allocations.create(
                instrument_id=instrument.id,
                asset_class_id=target_class.id,
                weight_bp=10_000,
                import_batch_id=batch.id,
            )
            audit.record(
                actor_user_id=ctx.user.id,
                action="instrument.created",
                entity_type="instrument",
                entity_id=str(instrument.id),
                ts=now,
                data={
                    "name": instrument.name,
                    "type": instrument.type,
                    "currency": instrument.quote_currency,
                    "risk_tier_override": "",
                },
            )
            audit.record(
                actor_user_id=ctx.user.id,
                action="instrument_allocation.updated",
                entity_type="instrument",
                entity_id=str(instrument.id),
                ts=now,
                data={"count": 1},
            )
        transactions = InvestmentTransactionRepository(session, ctx)
        created = 0
        for match in plan.matches:
            instrument = (
                active_by_id.get(match.instrument_id)
                if match.instrument_id is not None
                else created_instruments.get(match.new_instrument.key)  # type: ignore[union-attr]
            )
            if instrument is None:
                continue
            transactions.create(
                instrument_id=instrument.id,
                date=match.row.day,
                type=match.row.type,
                quantity=match.row.quantity,
                unit_price=match.unit_price,
                quote_currency=match.quote_currency,
                fee_minor=match.fee_minor,
                fx_to_base=match.fx_to_base,
                note=match.row.note,
                settlement_currency=match.row.settlement_currency,
                settlement_unit_price=match.settlement_unit_price,
                settlement_fee_minor=match.settlement_fee_minor,
                import_batch_id=batch.id,
                import_key=f"id:{match.row.source_id}" if match.row.source_id else None,
                created_at=match.created_at,
            )
            created += 1
        from assetboard.services.portfolio_links import ensure_networth_items_for_asset_classes

        ensure_networth_items_for_asset_classes(session, ctx)
        return {
            "created": created,
            "duplicates": sum(1 for entry in plan.skipped if entry["code"] == "already_imported"),
            "conflicts": 0,
            "skipped": len(plan.skipped) + len(plan.errors),
            "instruments_created": len(created_instruments),
            "investment_transactions_created": created,
        }
