"""Locale-aware month and date texts from Babel's CLDR data (F2.5).

Month names come from CLDR, not from the gettext catalog: every locale Babel knows gets its own
names and grammar (Polish uses the nominative "październik 2026" for a month on its own).
"""

from __future__ import annotations

from datetime import date

from babel import Locale
from babel.core import UnknownLocaleError
from babel.dates import format_date, format_skeleton, get_day_names, get_month_names

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
