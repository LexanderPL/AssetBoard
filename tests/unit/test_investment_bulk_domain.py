"""Bulk add line splitting (F6.2a part B): separators, dates, types, comments and limits."""

from __future__ import annotations

from datetime import date

import pytest

from assetboard.domain.investment_bulk import (
    BulkTooLarge,
    InvalidBulkDate,
    TooManyBulkLines,
    parse_bulk_date,
    parse_bulk_type,
    split_transaction_lines,
)
from assetboard.i18n.dates import short_date_order

TODAY = date(2026, 3, 10)


def _split(text: str, locale: str = "en"):
    order, separator = short_date_order(locale)
    return split_transaction_lines(text, order=order, separator=separator, today=TODAY)


@pytest.mark.parametrize(
    ("text", "locale", "expected"),
    [
        ("2026-01-05", "en", date(2026, 1, 5)),
        ("2026-1-5", "pl", date(2026, 1, 5)),
        ("1/5/2026", "en", date(2026, 1, 5)),
        ("5.01.2026", "pl", date(2026, 1, 5)),
        ("05.01.2026", "pl", date(2026, 1, 5)),
        ("2026-03-10", "en", TODAY),
    ],
)
def test_dates_are_iso_or_the_locale_short_date(text: str, locale: str, expected: date) -> None:
    order, separator = short_date_order(locale)
    assert parse_bulk_date(text, order, separator, TODAY) == expected


@pytest.mark.parametrize(
    ("text", "locale", "code"),
    [
        ("2026-03-11", "en", "future_date"),
        ("1/5/26", "en", "invalid_date"),
        ("5.01.2026", "en", "invalid_date"),
        ("31.02.2026", "pl", "invalid_date"),
        ("1899-12-31", "en", "invalid_date"),
        ("yesterday", "en", "invalid_date"),
    ],
)
def test_bad_dates_are_line_errors(text: str, locale: str, code: str) -> None:
    order, separator = short_date_order(locale)
    with pytest.raises(InvalidBulkDate) as caught:
        parse_bulk_date(text, order, separator, TODAY)
    assert caught.value.code == code


@pytest.mark.parametrize(
    ("word", "kind"),
    [
        ("buy", "buy"),
        ("BUY", "buy"),
        ("kupno", "buy"),
        ("Zakup", "buy"),
        ("sell", "sell"),
        ("sprzedaż", "sell"),
        ("SPRZEDAŻ", "sell"),
        ("sprzedaz", "sell"),
        ("dividend", None),
    ],
)
def test_types_and_polish_aliases(word: str, kind: str | None) -> None:
    assert parse_bulk_type(word) == kind


def test_lines_fields_comments_and_optional_trailing_fields() -> None:
    text = (
        "# date; instrument; type; quantity; price\n"
        "\n"
        "2026-01-05 ; World ETF ; buy ; 10 ; 100.5\n"
        "  # an indented comment\n"
        "2026-01-06;World ETF;sell;2;101;1.5;;\n"
        "2026-01-07;US ETF;kupno;4;1600;5;4.0123;pln\n"
    )
    lines, problems = _split(text)
    assert problems == ()
    assert [line.number for line in lines] == [3, 5, 6]
    first, second, third = lines
    assert (first.day, first.instrument, first.type, first.quantity, first.unit_price) == (
        date(2026, 1, 5),
        "World ETF",
        "buy",
        "10",
        "100.5",
    )
    assert (first.fee, first.fx, first.currency) == ("", "", "")
    assert (second.type, second.fee, second.fx) == ("sell", "1.5", "")
    assert (third.type, third.fx, third.currency) == ("buy", "4.0123", "PLN")


def test_polish_numbers_and_dates_pass_through_unparsed() -> None:
    [line], problems = _split("5.01.2026; ETF; zakup; 1,5; 1 234,56; 0,5", "pl")
    assert problems == ()
    assert (line.day, line.quantity, line.unit_price, line.fee) == (
        date(2026, 1, 5),
        "1,5",
        "1 234,56",
        "0,5",
    )


@pytest.mark.parametrize(
    ("line", "code"),
    [
        ("2026-01-05; ETF; buy", "wrong_format"),
        ("2026-01-05; ETF; buy; 1; 2; 3; 4; USD; extra", "wrong_format"),
        ("; ETF; buy; 1", "date_required"),
        ("2026-13-01; ETF; buy; 1", "invalid_date"),
        ("2026-04-01; ETF; buy; 1", "future_date"),
        ("2026-01-05; ; buy; 1", "instrument_required"),
        ("2026-01-05; ETF; split; 1", "invalid_type"),
        ("2026-01-05; ETF; buy; ", "quantity_required"),
    ],
)
def test_structural_line_errors(line: str, code: str) -> None:
    lines, problems = _split(f"2026-01-05; ETF; buy; 1; 10\n{line}")
    assert len(lines) == 1
    assert [(problem.number, problem.code) for problem in problems] == [(2, code)]


def test_limits_are_checked_before_parsing() -> None:
    with pytest.raises(TooManyBulkLines):
        _split("2026-01-05; ETF; buy; 1; 1\n" * 501)
    assert len(_split("2026-01-05; ETF; buy; 1; 1\n" * 500)[0]) == 500
    with pytest.raises(BulkTooLarge):
        _split("#" + "x" * (64 * 1024))
