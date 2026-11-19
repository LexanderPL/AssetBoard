"""The year-overview CSV (F9.2): conventional CSV, plain amounts, the grid's rows and totals."""

from __future__ import annotations

import csv
import io
from uuid import uuid4

import pytest

from assetboard.domain.money import Money
from assetboard.domain.months import YearMonth
from assetboard.domain.report_csv import YEAR_OVERVIEW_COLUMNS, year_overview_csv
from assetboard.domain.reports import CategorySeries, CategorySpend


def _series(*rows: CategorySpend, totals: tuple[int, ...] | None = None) -> CategorySeries:
    periods = tuple(YearMonth(2026, month) for month in range(1, 13))
    amounts = totals or tuple(
        sum(row.amounts[index].amount_minor for row in rows) for index in range(12)
    )
    return CategorySeries(
        periods=periods,
        rows=rows,
        totals=tuple(Money(value, "EUR") for value in amounts),
        total=Money(sum(amounts), "EUR"),
    )


def _row(amounts: list[int], *, name: str = "Food", archived: bool = False) -> CategorySpend:
    money = tuple(Money(value, "EUR") for value in amounts)
    return CategorySpend(
        category_id=uuid4(),
        name=name,
        color="#16a34a",
        amounts=money,
        total=Money(sum(amounts), "EUR"),
        archived=archived,
    )


def test_the_csv_has_iso_month_columns_plain_amounts_and_crlf() -> None:
    food = _row([1_234, 0, 20_005, 0, 0, 0, 0, 0, 0, 0, 0, 0])
    rent = _row([90_000] * 12, name="Rent")
    text = year_overview_csv(_series(food, rent), year=2026)
    assert text.endswith("\r\n")
    assert "\n" not in text.replace("\r\n", "")
    lines = text.split("\r\n")
    assert lines[0] == (
        "category_id,category,archived,2026-01,2026-02,2026-03,2026-04,2026-05,2026-06,"
        "2026-07,2026-08,2026-09,2026-10,2026-11,2026-12,total"
    )
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[1][:3] == [str(food.category_id), "Food", "false"]
    assert rows[1][3] == "12.34"
    assert rows[1][4] == "0.00"
    assert rows[1][5] == "200.05"
    assert rows[1][-1] == "212.39"
    assert rows[2][1] == "Rent" and rows[2][-1] == "10800.00"
    assert rows[3][:3] == ["", "Total", "false"]
    assert rows[3][3] == "912.34"
    assert rows[3][-1] == "11012.39"


def test_the_csv_quotes_commas_quotes_and_newlines_and_marks_archived() -> None:
    tricky = _row([100] + [0] * 11, name='A, "B"\nC', archived=True)
    text = year_overview_csv(_series(tricky), year=2026)
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[1][1] == 'A, "B"\nC'
    assert rows[1][2] == "true"
    assert len(rows[2]) == len(YEAR_OVERVIEW_COLUMNS) + 12 + 1


def test_an_empty_year_exports_the_header_and_a_zero_total_row() -> None:
    text = year_overview_csv(
        CategorySeries(
            tuple(YearMonth(2026, month) for month in range(1, 13)),
            (),
            tuple(Money(0, "EUR") for _ in range(12)),
            Money(0, "EUR"),
        ),
        year=2026,
    )
    rows = list(csv.reader(io.StringIO(text)))
    assert len(rows) == 2
    assert rows[1][:3] == ["", "Total", "false"]
    assert set(rows[1][3:]) == {"0.00"}


def test_a_combined_row_or_a_non_year_series_is_refused() -> None:
    combined = CategorySpend(
        category_id=None,
        name="",
        color="",
        amounts=tuple(Money(0, "EUR") for _ in range(12)),
        total=Money(0, "EUR"),
    )
    with pytest.raises(ValueError):
        year_overview_csv(_series(combined), year=2026)
    with pytest.raises(ValueError):
        year_overview_csv(
            CategorySeries((2026,), (), (Money(0, "EUR"),), Money(0, "EUR")), year=2026
        )
    with pytest.raises(ValueError):
        year_overview_csv(
            CategorySeries(
                tuple(YearMonth(2026, month) for month in range(1, 12)),
                (),
                tuple(Money(0, "EUR") for _ in range(11)),
                Money(0, "EUR"),
            ),
            year=2026,
        )
