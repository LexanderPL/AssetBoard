"""The F8.3 both-bases CSV."""

from __future__ import annotations

import csv
import io

from assetboard.domain.fire import (
    ContributionOverride,
    EventKind,
    ProjectionEvent,
    ProjectionInputs,
    Rule,
    project,
)
from assetboard.domain.money import Money
from assetboard.web.fire_csv import FIRE_RESULT_COLUMNS, fire_scenario_csv

HEADER = (
    "year,age,income,expenses,net,gain,cash,net_worth,"
    "real_income,real_expenses,real_net,real_gain,real_cash,real_net_worth,"
    "is_fire,income_stopped,currency"
)


def _inputs(**overrides: object) -> ProjectionInputs:
    values: dict[str, object] = {
        "currency": "EUR",
        "birth_year": 1985,
        "start_year": 2026,
        "horizon_age": 90,
        "stop_working_age": 55,
        "inflation_bp": 250,
        "real_return_bp": 500,
        "income_growth_bp": 300,
        "rule": Rule.RETURNS_COVER_EXPENSES,
        "swr_bp": None,
        "start_income": Money(5_000_000, "EUR"),
        "start_expenses": Money(3_000_000, "EUR"),
        "start_portfolio": Money(100_000_000, "EUR"),
    }
    values.update(overrides)
    return ProjectionInputs(**values)  # type: ignore[arg-type]


def test_the_header_and_rows_are_exact_crlf_and_include_both_bases() -> None:
    projection = project(
        _inputs(
            contributions=(ContributionOverride(2027, Money(-125_050, "EUR")),),
            events=(ProjectionEvent(2028, kind=EventKind.CASH, amount=Money(250_000, "EUR")),),
        )
    )
    text = fire_scenario_csv(projection)
    assert tuple(HEADER.split(",")) == FIRE_RESULT_COLUMNS
    assert text.startswith(HEADER + "\r\n")
    assert text.count("\r\n") == len(projection.rows) + 1
    assert "\n" not in text.replace("\r\n", "")
    assert not text.startswith("\ufeff")
    rows = list(csv.reader(io.StringIO(text, newline="")))
    assert len(rows) == len(projection.rows) + 1
    assert len(rows[1]) == len(FIRE_RESULT_COLUMNS)
    # One row per plan year, nominal and real columns, plain dot decimals and no symbols.
    assert rows[1][0] == "2026"
    assert rows[1][1] == "41"
    assert rows[1][14] in ("0", "1")
    assert rows[1][15] == "0"
    assert rows[1][16] == "EUR"
    assert all("," not in value for row in rows[1:] for value in row[:-1])


def test_negative_values_keep_their_sign_and_flags_are_one_or_zero() -> None:
    projection = project(
        _inputs(
            start_income=Money(1_000_000, "EUR"),
            start_expenses=Money(3_000_000, "EUR"),
        )
    )
    rows = list(csv.reader(io.StringIO(fire_scenario_csv(projection), newline="")))
    body = rows[1:]
    assert any(value.startswith("-") for row in body for value in row[2:14])
    fire_rows = [row for row in body if row[14] == "1"]
    assert len(fire_rows) == 1
    stopped = [row for row in body if row[15] == "1"]
    assert stopped
    assert all(row[15] in ("0", "1") for row in body)


def test_jpy_uses_whole_yen_with_no_decimal_point() -> None:
    projection = project(
        _inputs(
            currency="JPY",
            start_income=Money(5_000_000, "JPY"),
            start_expenses=Money(3_000_000, "JPY"),
            start_portfolio=Money(100_000_000, "JPY"),
        )
    )
    text = fire_scenario_csv(projection)
    rows = list(csv.reader(io.StringIO(text, newline="")))
    amounts = rows[1][2:14]
    assert any(amounts)
    assert all("." not in amount for amount in amounts)
    assert rows[1][16] == "JPY"


def test_the_start_year_net_column_reuses_the_starting_portfolio() -> None:
    projection = project(_inputs())
    rows = list(csv.reader(io.StringIO(fire_scenario_csv(projection), newline="")))
    first = rows[1]
    assert first[2:8] == [
        "50000.00",
        "30000.00",
        "1000000.00",
        "75000.00",
        "0.00",
        "1075000.00",
    ]
