"""Locale-aware month and date texts from Babel's CLDR data (F2.5).

Month names come from CLDR, not from the gettext catalog: every locale Babel knows gets its own
names and grammar (Polish uses the nominative "październik 2026" for a month on its own).
"""

from __future__ import annotations

from datetime import date

from babel import Locale
from babel.core import UnknownLocaleError
from babel.dates import format_date, format_skeleton, get_day_names, get_month_names
from babel.lists import format_list

from assetboard.domain.months import YearMonth

_FALLBACK_LOCALE = "en"


def _locale(locale: str) -> str:
    try:
        return str(Locale.parse(locale.replace("-", "_")))
    except (UnknownLocaleError, ValueError):
        return _FALLBACK_LOCALE


def format_month(month: YearMonth, locale: str) -> str:
    """The month and year as CLDR writes them in running text: "October 2026",
    "październik 2026"."""
    return format_skeleton("yMMMM", month.first_day, locale=_locale(locale))


def format_month_heading(month: YearMonth, locale: str) -> str:
    """``format_month`` with a capital first letter, for a heading or page title only:
    "Październik 2026"."""
    text = format_month(month, locale)
    return text[:1].upper() + text[1:]


def format_month_after_from(month: YearMonth, locale: str) -> str:
    """The month and year after "from" in a sentence: CLDR's format-context month name, which
    some languages inflect ("from September 2026", "z września 2026")."""
    return format_date(month.first_day, format="MMMM y", locale=_locale(locale))


def format_day(day: date, locale: str) -> str:
    """A date in the locale's medium format: "Oct 14, 2026", "14 paź 2026"."""
    return format_date(day, format="medium", locale=_locale(locale))


def format_day_month(day: date, locale: str) -> str:
    """The day and month without the year, for sentences: "November 10", "10 listopada"."""
    return format_skeleton("MMMMd", day, locale=_locale(locale))


def format_day_long(day: date, locale: str) -> str:
    """A date with its weekday in the locale's full format, for screen-reader labels:
    "Monday, March 2, 2026", "poniedziałek, 2 marca 2026"."""
    return format_date(day, format="full", locale=_locale(locale))


def weekday_names(locale: str) -> list[str]:
    """Abbreviated weekday names from Monday to Sunday (CLDR stand-alone): "Mon", "pon."."""
    names = get_day_names("abbreviated", context="stand-alone", locale=_locale(locale))
    return [names[index] for index in range(7)]


def month_names(locale: str) -> list[str]:
    """Month names from January to December as CLDR writes them on their own (stand-alone),
    for a month select: "January", "styczeń"."""
    names = get_month_names("wide", context="stand-alone", locale=_locale(locale))
    return [names[index] for index in range(1, 13)]


def format_day_of_month(day: int, locale: str) -> str:
    """A day of the month as an ordinal for sentences: "25th" (English), "25." (others)."""
    if _locale(locale).split("_")[0] != "en":
        return f"{day}."
    suffix = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def format_day_and_month(day: int, month: int, locale: str) -> str:
    """A day and month written day first for a yearly date: "31 March", "31 marca". The
    year 2000 is a leap year, so 29 February exists."""
    return format_date(date(2000, month, day), format="d MMMM", locale=_locale(locale))


def format_and_list(items: list[str], locale: str) -> str:
    """Items joined as the locale writes a list: "A, B and C", "A, B i C"."""
    return format_list(items, locale=_locale(locale))
