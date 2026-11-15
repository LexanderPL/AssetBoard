"""Pure parsing and display helpers for the unified FIRE adjustments editor (F8.11).

The module deliberately has no FastAPI or template dependency: the F8.10 edit/view pages can
call it without knowing each other's template names. It parses the plan form's adjustment rows
into the service's ``EventInput``/removal lists, and builds sorted display rows, archived reasons
and translated labels for the editor.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from uuid import UUID

from assetboard.domain.fire import EventKind, signed_amount_minor
from assetboard.domain.money import Money
from assetboard.domain.transactions import EntryError
from assetboard.i18n import gettext
from assetboard.i18n.numbers import format_amount_input, parse_amount
from assetboard.services.fire_scenarios import (
    EventInput,
    EventView,
    FireError,
    FirePlanDetail,
)

EVENT_KEY = re.compile(r"^event_(id|year|kind|amount|label|remove)_(new|[0-9]+)$")
EVENT_FIELDS = ("id", "year", "kind", "amount", "label", "remove")
ARCHIVED_REMOVE_KEY = re.compile(r"^archived_remove_([0-9]+)$")


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


KIND_ORDER = {
    EventKind.CASH.value: 0,
    EventKind.INCOME_CHANGE.value: 1,
    EventKind.EXPENSE_CHANGE.value: 2,
    EventKind.SAVING_OVERRIDE.value: 3,
}

KIND_LABELS = {
    EventKind.CASH.value: _("One-off cash flow"),
    EventKind.EXPENSE_CHANGE.value: _("Annual expense change"),
    EventKind.INCOME_CHANGE.value: _("Annual income change"),
    EventKind.SAVING_OVERRIDE.value: _("Saving override"),
}

ARCHIVED_REASON_LABELS = {
    "past": _("Before the current year"),
    "after_horizon": _("After the plan's last year"),
    "current_override": _("Saving override in the current year"),
}


@dataclass(frozen=True, slots=True)
class ParsedAdjustments:
    """The adjustment part of a posted plan form."""

    events: tuple[EventInput, ...]
    removed_event_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class AdjustmentWindow:
    """The year window the editor shows; all values are computed here, never in templates."""

    current_year: int
    last_year: int
    override_start_year: int

    @property
    def override_available(self) -> bool:
        return self.override_start_year <= self.last_year


@dataclass(frozen=True, slots=True)
class AdjustmentRow:
    """One editable adjustment row, including the trailing blank ``new`` row."""

    index: str
    position: int
    id: UUID | None
    year: int | None
    kind: str
    amount: Money | None
    amount_input: str
    label: str
    remove: bool
    is_new: bool
    error: str | None = None
    error_column: str | None = None


@dataclass(frozen=True, slots=True)
class ArchivedAdjustmentRow:
    """One read-only archived adjustment, optionally marked for deletion."""

    index: str
    id: UUID
    year: int
    kind: str
    amount: Money
    label: str
    reason: str
    reason_label: str
    remove: bool


def kind_labels(locale: str) -> dict[str, str]:
    """The four kind labels in the request locale."""
    return {code: gettext(label, locale) for code, label in KIND_LABELS.items()}


def archived_reason_label(reason: str, locale: str) -> str:
    label = ARCHIVED_REASON_LABELS.get(reason)
    return gettext(label, locale) if label is not None else ""


def adjustment_window(detail: FirePlanDetail) -> AdjustmentWindow:
    plan = detail.plan
    return AdjustmentWindow(
        current_year=plan.current_year,
        last_year=plan.last_year,
        override_start_year=plan.current_year + 1,
    )


def archived_reason(event: EventView, *, current_year: int, last_year: int) -> str:
    """Why an event is archived: before the current year, after the horizon or a rollover."""
    if event.year < current_year:
        return "past"
    if event.year > last_year:
        return "after_horizon"
    if event.kind == EventKind.SAVING_OVERRIDE.value and event.year == current_year:
        return "current_override"
    return ""


def event_sort_key(event: EventView) -> tuple[int, int, str, str]:
    """Stable display order: year, kind, label and id; no row jumps after an edit."""
    return (
        event.year,
        KIND_ORDER.get(event.kind, len(KIND_ORDER)),
        event.label.casefold(),
        str(event.id),
    )


def sorted_events(events: Iterable[EventView]) -> tuple[EventView, ...]:
    return tuple(sorted(events, key=event_sort_key))


def next_adjustment_index(rows: Iterable[AdjustmentRow]) -> str:
    """The next numeric form index; ``new`` never becomes a saved row's index."""
    numeric = [int(row.index) for row in rows if row.index != "new"]
    return str(max(numeric, default=0) + 1)


def saved_active_count(rows: Iterable[AdjustmentRow]) -> int:
    """How many rows came from the saved plan (new or blank rows do not count)."""
    return sum(1 for row in rows if row.id is not None)


def adjustment_values(values: Mapping[str, str]) -> list[dict[str, str]]:
    """The posted adjustment rows in form order, with ``new`` last."""
    indexes = sorted(
        {match.group(2) for key in values if (match := EVENT_KEY.match(key))},
        key=_index_sort,
    )
    return [
        {
            "index": index,
            **{field: values.get(f"event_{field}_{index}", "") for field in EVENT_FIELDS},
        }
        for index in indexes
    ]


def checked_archived_ids(values: Mapping[str, str]) -> tuple[UUID, ...]:
    """The UUIDs of archived rows whose Remove checkbox was ticked."""
    removed: list[UUID] = []
    for key, value in values.items():
        if ARCHIVED_REMOVE_KEY.match(key) is None or not value:
            continue
        removed.append(_uuid(value, row=None))
    return tuple(removed)


def parse_adjustments(values: Mapping[str, str], locale: str, currency: str) -> ParsedAdjustments:
    """Parse every adjustment row and archived removal checkbox of a posted plan form.

    Rows whose Remove box is ticked are returned as removals, not as edits. A new row with no
    saved id is simply dropped when it is ticked. Validation of ownership, duplicates and the
    plan window is the service's job.
    """
    events: list[EventInput] = []
    removed: list[UUID] = []
    for row in adjustment_values(values):
        index = row["index"]
        if row["remove"] == "1":
            event_id = _optional_uuid(row["id"], row=index)
            if event_id is not None:
                removed.append(event_id)
            continue
        if not any(row[field].strip() for field in ("year", "amount", "label")):
            continue
        year = _year(row["year"], row=index)
        amount = _signed_amount(row["amount"].strip(), locale, currency, row=index, column="amount")
        events.append(
            EventInput(
                year=year,
                kind=row["kind"].strip(),
                amount_minor=amount,
                label=row["label"].strip(),
                id=_optional_uuid(row["id"], row=index),
                index=index,
            )
        )
    removed.extend(checked_archived_ids(values))
    return ParsedAdjustments(events=tuple(events), removed_event_ids=tuple(removed))


def active_rows_from_detail(
    detail: FirePlanDetail,
    locale: str,
    *,
    error: FireError | None = None,
    include_blank: bool = True,
) -> tuple[AdjustmentRow, ...]:
    """Saved active rows and, for the editor, the trailing blank row."""
    rows: list[AdjustmentRow] = []
    for position, event in enumerate(sorted_events(detail.active_events), start=1):
        index = str(position)
        rows.append(
            AdjustmentRow(
                index=index,
                position=position,
                id=event.id,
                year=event.year,
                kind=event.kind,
                amount=event.amount,
                amount_input=format_amount_input(event.amount, locale),
                label=event.label,
                remove=False,
                is_new=False,
                error=_row_error(error, index),
                error_column=_row_error_column(error, index),
            )
        )
    if include_blank:
        rows.append(_blank_row(len(rows) + 1))
    return tuple(rows)


def active_rows_from_values(
    values: Mapping[str, str],
    locale: str,
    currency: str,
    *,
    error: FireError | None = None,
) -> tuple[AdjustmentRow, ...]:
    """Posted values for an error re-render, preserving every row and removal mark."""
    rows: list[AdjustmentRow] = []
    for position, raw in enumerate(adjustment_values(values), start=1):
        index = raw["index"]
        event_id = _optional_uuid(raw["id"], row=index, strict=False)
        amount_input = raw["amount"].strip()
        rows.append(
            AdjustmentRow(
                index=index,
                position=position,
                id=event_id,
                year=_int_or_none(raw["year"]),
                kind=raw["kind"].strip() or EventKind.CASH.value,
                amount=_parsed_amount(amount_input, locale, currency),
                amount_input=amount_input,
                label=raw["label"].strip(),
                remove=raw["remove"] == "1",
                is_new=event_id is None,
                error=_row_error(error, index),
                error_column=_row_error_column(error, index),
            )
        )
    if not any(row.index == "new" for row in rows):
        rows.append(_blank_row(len(rows) + 1))
    return tuple(rows)


def archived_rows(
    detail: FirePlanDetail,
    locale: str,
    *,
    removed_ids: Iterable[UUID] = (),
) -> tuple[ArchivedAdjustmentRow, ...]:
    """The archived history in display order, with its reason and removal marks."""
    checked = set(removed_ids)
    rows: list[ArchivedAdjustmentRow] = []
    for position, event in enumerate(sorted_events(detail.archived_events), start=1):
        reason = archived_reason(
            event,
            current_year=detail.plan.current_year,
            last_year=detail.plan.last_year,
        )
        rows.append(
            ArchivedAdjustmentRow(
                index=str(position),
                id=event.id,
                year=event.year,
                kind=event.kind,
                amount=event.amount,
                label=event.label,
                reason=reason,
                reason_label=archived_reason_label(reason, locale),
                remove=event.id in checked,
            )
        )
    return tuple(rows)


def _index_sort(index: str) -> tuple[int, int]:
    return (1, 0) if index == "new" else (0, int(index))


def _year(text: str, *, row: str) -> int:
    try:
        return int(text.strip())
    except ValueError:
        raise FireError("events", "invalid_year", row=row, column="year") from None


def _signed_amount(text: str, locale: str, currency: str, *, row: str, column: str) -> int:
    try:
        amount = parse_amount(text, locale)
        return signed_amount_minor(amount, currency)
    except (ValueError, EntryError):
        raise FireError("events", "invalid_amount", row=row, column=column) from None


def _parsed_amount(text: str, locale: str, currency: str) -> Money | None:
    if not text:
        return None
    try:
        return Money(signed_amount_minor(parse_amount(text, locale), currency), currency)
    except (ValueError, EntryError):
        return None


def _optional_uuid(value: str, *, row: str | None, strict: bool = True) -> UUID | None:
    text = value.strip()
    if not text:
        return None
    try:
        return UUID(text)
    except ValueError:
        if not strict:
            return None
        raise FireError("events", "invalid_event", row=row) from None


def _uuid(value: str, *, row: str | None) -> UUID:
    try:
        return UUID(value.strip())
    except ValueError:
        raise FireError("events", "invalid_event", row=row) from None


def _int_or_none(text: str) -> int | None:
    try:
        return int(text.strip())
    except ValueError:
        return None


def _blank_row(position: int) -> AdjustmentRow:
    return AdjustmentRow(
        index="new",
        position=position,
        id=None,
        year=None,
        kind=EventKind.CASH.value,
        amount=None,
        amount_input="",
        label="",
        remove=False,
        is_new=True,
    )


def _row_error(error: FireError | None, index: str) -> str | None:
    if error is None or error.row != index:
        return None
    return error.code


def _row_error_column(error: FireError | None, index: str) -> str | None:
    if error is None or error.row != index:
        return None
    return error.column


__all__ = [
    "EVENT_FIELDS",
    "EVENT_KEY",
    "AdjustmentRow",
    "AdjustmentWindow",
    "ArchivedAdjustmentRow",
    "ParsedAdjustments",
    "active_rows_from_detail",
    "active_rows_from_values",
    "adjustment_values",
    "adjustment_window",
    "archived_reason",
    "archived_reason_label",
    "archived_rows",
    "checked_archived_ids",
    "event_sort_key",
    "kind_labels",
    "next_adjustment_index",
    "parse_adjustments",
    "saved_active_count",
    "sorted_events",
]
