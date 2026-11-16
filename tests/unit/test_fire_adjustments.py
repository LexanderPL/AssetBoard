"""The pure adjustment parser and display helpers (F8.11 backend slice)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from assetboard.domain.money import Money
from assetboard.services.fire_scenarios import (
    EventView,
    FireError,
    FirePlanDetail,
    FirePlanView,
)
from assetboard.web import fire_adjustments


def _plan(*, current_year: int = 2026, last_year: int = 2075) -> FirePlanView:
    return FirePlanView(
        id=uuid4(),
        currency="EUR",
        birth_year=last_year - 90,
        current_year=current_year,
        age_at_start=current_year - (last_year - 90),
        inflation_bp=250,
        real_return_bp=500,
        income_growth_bp=300,
        stop_working_age=55,
        horizon_age=last_year - (last_year - 90),
        rule="returns_cover_expenses",
        swr_bp=None,
        income=Money(5_000_000, "EUR"),
        expenses=Money(3_000_000, "EUR"),
        portfolio=Money(100_000_000, "EUR"),
        needs_review=False,
        currency_mismatch=False,
        event_count=0,
        archived_event_count=0,
        values_year=current_year,
    )


def _event(
    year: int,
    kind: str,
    amount_minor: int,
    label: str,
    *,
    archived: bool = False,
) -> EventView:
    return EventView(
        id=uuid4(),
        year=year,
        kind=kind,
        amount=Money(amount_minor, "EUR"),
        label=label,
        archived=archived,
    )


def _detail(
    active: tuple[EventView, ...] = (),
    archived: tuple[EventView, ...] = (),
    *,
    current_year: int = 2026,
    last_year: int = 2075,
) -> FirePlanDetail:
    return FirePlanDetail(
        plan=_plan(current_year=current_year, last_year=last_year),
        events=active + archived,
        active_events=active,
        archived_events=archived,
    )


def test_parse_adjustments_reads_all_four_kinds_and_removals() -> None:
    active = uuid4()
    removed = uuid4()
    archived = uuid4()
    parsed = fire_adjustments.parse_adjustments(
        {
            "event_id_1": str(active),
            "event_year_1": "2027",
            "event_kind_1": "cash",
            "event_amount_1": "1 234,56",
            "event_label_1": " Bonus ",
            "event_id_2": str(removed),
            "event_year_2": "2028",
            "event_kind_2": "expense_change",
            "event_amount_2": "-200",
            "event_label_2": "Repaid",
            "event_remove_2": "1",
            "event_id_3": "",
            "event_year_3": "2029",
            "event_kind_3": "income_change",
            "event_amount_3": "500",
            "event_label_3": "Rent",
            "event_id_4": "",
            "event_year_4": "2030",
            "event_kind_4": "saving_override",
            "event_amount_4": "-2000",
            "event_label_4": "Sabbatical",
            "archived_remove_1": str(archived),
        },
        "pl",
        "EUR",
    )
    assert [
        (row.kind, row.year, row.amount_minor, row.label, row.index) for row in parsed.events
    ] == [
        ("cash", 2027, 123_456, "Bonus", "1"),
        ("income_change", 2029, 50_000, "Rent", "3"),
        ("saving_override", 2030, -200_000, "Sabbatical", "4"),
    ]
    assert parsed.events[0].id == active
    assert parsed.removed_event_ids == (removed, archived)


def test_parse_adjustments_skips_empty_rows_and_unticked_archived_rows() -> None:
    parsed = fire_adjustments.parse_adjustments(
        {
            "event_id_1": "",
            "event_year_1": "",
            "event_kind_1": "cash",
            "event_amount_1": "",
            "event_label_1": "",
            "event_id_2": "",
            "event_year_2": "2027",
            "event_kind_2": "cash",
            "event_amount_2": "10",
            "event_label_2": "Removed new row",
            "event_remove_2": "1",
            "event_id_new": "",
            "event_year_new": "",
            "event_kind_new": "cash",
            "event_amount_new": "",
            "event_label_new": "",
            "archived_remove_1": "",
        },
        "en",
        "EUR",
    )
    assert parsed.events == ()
    assert parsed.removed_event_ids == ()


def test_parse_adjustments_reports_the_row_and_column() -> None:
    with pytest.raises(FireError) as year:
        fire_adjustments.parse_adjustments(
            {
                "event_year_2": "not-a-year",
                "event_kind_2": "cash",
                "event_amount_2": "1",
                "event_label_2": "Bad",
            },
            "en",
            "EUR",
        )
    assert year.value.code == "invalid_year"
    assert year.value.row == "2"
    assert year.value.column == "year"
    with pytest.raises(FireError) as amount:
        fire_adjustments.parse_adjustments(
            {
                "event_year_3": "2027",
                "event_kind_3": "cash",
                "event_amount_3": "not-money",
                "event_label_3": "Bad",
            },
            "en",
            "EUR",
        )
    assert amount.value.code == "invalid_amount"
    assert amount.value.row == "3"
    assert amount.value.column == "amount"
    with pytest.raises(FireError) as event_id:
        fire_adjustments.parse_adjustments(
            {
                "event_id_4": "not-a-uuid",
                "event_year_4": "2027",
                "event_kind_4": "cash",
                "event_amount_4": "1",
                "event_label_4": "Bad",
            },
            "en",
            "EUR",
        )
    assert event_id.value.code == "invalid_event"
    assert event_id.value.row == "4"


def test_parse_adjustments_accepts_zero_and_locale_signs() -> None:
    parsed = fire_adjustments.parse_adjustments(
        {
            "event_year_1": "2027",
            "event_kind_1": "cash",
            "event_amount_1": "0",
            "event_label_1": "Zero",
            "event_year_2": "2027",
            "event_kind_2": "cash",
            "event_amount_2": "-12,5",
            "event_label_2": "Negative",
        },
        "pl",
        "EUR",
    )
    assert [row.amount_minor for row in parsed.events] == [0, -1250]


def test_adjustment_values_order_numeric_rows_then_new() -> None:
    rows = fire_adjustments.adjustment_values(
        {
            "event_year_10": "2030",
            "event_year_2": "2027",
            "event_year_new": "",
        }
    )
    assert [row["index"] for row in rows] == ["2", "10", "new"]


def test_sorted_events_orders_by_year_kind_label_and_id() -> None:
    events = (
        _event(2028, "cash", 1, "b"),
        _event(2027, "saving_override", 1, "z"),
        _event(2027, "cash", 1, "b"),
        _event(2027, "cash", 1, "a"),
        _event(2027, "income_change", 1, "a"),
    )
    assert [
        (event.year, event.kind, event.label) for event in fire_adjustments.sorted_events(events)
    ] == [
        (2027, "cash", "a"),
        (2027, "cash", "b"),
        (2027, "income_change", "a"),
        (2027, "saving_override", "z"),
        (2028, "cash", "b"),
    ]


def test_archived_reasons_cover_past_horizon_and_current_override() -> None:
    assert (
        fire_adjustments.archived_reason(
            _event(2020, "cash", 1, "Old"), current_year=2026, last_year=2075
        )
        == "past"
    )
    assert (
        fire_adjustments.archived_reason(
            _event(2080, "cash", 1, "Late"), current_year=2026, last_year=2075
        )
        == "after_horizon"
    )
    assert (
        fire_adjustments.archived_reason(
            _event(2026, "saving_override", 1, "Now"),
            current_year=2026,
            last_year=2075,
        )
        == "current_override"
    )
    assert (
        fire_adjustments.archived_reason(
            _event(2027, "cash", 1, "Active"), current_year=2026, last_year=2075
        )
        == ""
    )


def test_archived_rows_mark_removals_and_carry_the_reason() -> None:
    past = _event(2020, "cash", 100_000, "Old bonus", archived=True)
    current = _event(2026, "saving_override", 200_000, "Now", archived=True)
    rows = fire_adjustments.archived_rows(
        _detail(archived=(current, past)), "en", removed_ids=(past.id,)
    )
    assert [(row.year, row.reason, row.remove) for row in rows] == [
        (2020, "past", True),
        (2026, "current_override", False),
    ]
    assert rows[0].reason_label == "Before the current year"
    assert rows[0].amount == Money(100_000, "EUR")


def test_active_rows_from_detail_append_the_blank_new_row() -> None:
    first = _event(2028, "cash", 100_000, "B")
    second = _event(2027, "expense_change", -20_000, "A")
    rows = fire_adjustments.active_rows_from_detail(_detail(active=(first, second)), "en")
    assert [(row.year, row.position, row.is_new) for row in rows] == [
        (2027, 1, False),
        (2028, 2, False),
        (None, 3, True),
    ]
    assert rows[-1].index == "new"
    assert rows[0].amount_input == "-200.00"
    view_rows = fire_adjustments.active_rows_from_detail(
        _detail(active=(first, second)), "en", include_blank=False
    )
    assert [row.year for row in view_rows] == [2027, 2028]
    assert fire_adjustments.next_adjustment_index(rows) == "3"
    assert fire_adjustments.saved_active_count(rows) == 2


def test_active_rows_from_values_preserve_errors_and_removal_marks() -> None:
    event_id = uuid4()
    rows = fire_adjustments.active_rows_from_values(
        {
            "event_id_3": str(event_id),
            "event_year_3": "2027",
            "event_kind_3": "cash",
            "event_amount_3": "100",
            "event_label_3": "Keep",
            "event_id_4": str(event_id),
            "event_year_4": "2028",
            "event_kind_4": "cash",
            "event_amount_4": "",
            "event_label_4": "Bad",
            "event_remove_4": "1",
        },
        "en",
        "EUR",
        error=FireError("events", "invalid_amount", row="4", column="amount"),
    )
    assert [(row.index, row.remove, row.error, row.error_column) for row in rows] == [
        ("3", False, None, None),
        ("4", True, "invalid_amount", "amount"),
        ("new", False, None, None),
    ]
    assert rows[0].amount == Money(10_000, "EUR")


def test_kind_labels_and_window_are_ready_for_the_editor() -> None:
    labels = fire_adjustments.kind_labels("en")
    assert set(labels) == {"cash", "income_change", "expense_change", "saving_override"}
    assert labels["saving_override"] == "Saving override"
    assert fire_adjustments.kind_labels("pl")["saving_override"] == "Korekta oszczędności"
    assert fire_adjustments.archived_reason_label("past", "pl") == "Przed bieżącym rokiem"
    window = fire_adjustments.adjustment_window(_detail(last_year=2026))
    assert (window.current_year, window.last_year, window.override_start_year) == (
        2026,
        2026,
        2027,
    )
    assert window.override_available is False


def test_has_pending_adjustment_detects_only_unsaved_input() -> None:
    saved = _event(2027, "cash", 100_000, "Saved")
    rows = fire_adjustments.active_rows_from_detail(_detail(active=(saved,)), "en")
    assert fire_adjustments.saved_active_count(rows) == 1
    assert fire_adjustments.has_pending_adjustment(rows) is False

    blank = fire_adjustments.active_rows_from_detail(_detail(), "en")
    assert fire_adjustments.saved_active_count(blank) == 0
    assert fire_adjustments.has_pending_adjustment(blank) is False

    filled = fire_adjustments.active_rows_from_values(
        {
            "event_id_new": "",
            "event_year_new": "2027",
            "event_kind_new": "cash",
            "event_amount_new": "",
            "event_label_new": "",
        },
        "en",
        "EUR",
    )
    assert fire_adjustments.has_pending_adjustment(filled) is True


def test_checked_archived_ids_ignore_empty_and_reject_invalid_values() -> None:
    event_id = uuid4()
    assert fire_adjustments.checked_archived_ids(
        {"archived_remove_1": str(event_id), "archived_remove_2": ""}
    ) == (event_id,)
    with pytest.raises(FireError) as error:
        fire_adjustments.checked_archived_ids({"archived_remove_1": "not-a-uuid"})
    assert error.value.code == "invalid_event"
