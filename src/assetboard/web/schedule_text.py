"""A recurrence rule as a plain sentence for the Recurring page (F3.1).

"Monthly on the 10th", "Every 3 months on the 15th, from January 2027", "Yearly on 31 March",
"Weekly on Monday, until December 2027". Only locale formatting is involved: the rule is the
domain's, the words come from the gettext catalog and the month and date names from CLDR.
"""

from __future__ import annotations

from datetime import date

from assetboard.domain.months import YearMonth
from assetboard.domain.recurrence import Frequency, MonthDay, RecurrenceRule, WeekendPolicy
from assetboard.i18n import gettext, ngettext
from assetboard.i18n.dates import (
    format_and_list,
    format_day,
    format_day_and_month,
    format_day_of_month,
    format_month_after_from,
)


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when used."""
    return message


# "on <weekday>" (Polish: "w poniedziałek", "we wtorek", "w środę"), Monday first.
ON_WEEKDAY = (
    _("on Monday"),
    _("on Tuesday"),
    _("on Wednesday"),
    _("on Thursday"),
    _("on Friday"),
    _("on Saturday"),
    _("on Sunday"),
)


def _day_phrase(choice: int | MonthDay, locale: str) -> str:
    if choice == MonthDay.FIRST_WORKING:
        return gettext("on the first working day", locale)
    if choice == MonthDay.LAST_WORKING:
        return gettext("on the last working day", locale)
    if choice == -1:
        return gettext("on the last day", locale)
    return gettext("on the %(day)s", locale) % {"day": format_day_of_month(int(choice), locale)}


def _base(rule: RecurrenceRule, locale: str) -> str:
    interval = rule.interval
    if rule.freq == Frequency.ONCE:
        return gettext("Once on %(date)s", locale) % {"date": format_day(rule.start_date, locale)}
    if rule.freq == Frequency.WEEKLY:
        days = format_and_list(
            [gettext(ON_WEEKDAY[day], locale) for day in sorted(set(rule.weekdays))], locale
        )
        if interval == 1:
            return gettext("Weekly %(days)s", locale) % {"days": days}
        return ngettext(
            "Every %(num)s week %(days)s", "Every %(num)s weeks %(days)s", interval, locale
        ) % {"num": interval, "days": days}
    if rule.freq == Frequency.MONTHLY:
        day = _day_phrase(rule.month_day, locale)
        if interval == 1:
            return gettext("Monthly %(day)s", locale) % {"day": day}
        return ngettext(
            "Every %(num)s month %(day)s", "Every %(num)s months %(day)s", interval, locale
        ) % {"num": interval, "day": day}
    day_number = rule.month_day if isinstance(rule.month_day, int) and rule.month_day > 0 else 1
    when = format_day_and_month(day_number, rule.month, locale)
    if interval == 1:
        return gettext("Yearly on %(date)s", locale) % {"date": when}
    return ngettext(
        "Every %(num)s year on %(date)s", "Every %(num)s years on %(date)s", interval, locale
    ) % {"num": interval, "date": when}


def _point(day: date, freq: Frequency, locale: str) -> str:
    """A start or end in the sentence: a date for weekly rules, a month and year otherwise."""
    if freq == Frequency.WEEKLY:
        return format_day(day, locale)
    return format_month_after_from(YearMonth.from_date(day), locale)


def describe_rule(rule: RecurrenceRule, locale: str, today: date) -> str:
    """The schedule as one sentence. The start is named when it is still ahead of ``today`` or
    anchors a rule repeating every few weeks, months or years; the end when there is one."""
    text = _base(rule, locale)
    if rule.freq == Frequency.ONCE:
        return text
    if rule.start_date > today or rule.interval > 1:
        text += gettext(", from %(start)s", locale) % {
            "start": _point(rule.start_date, rule.freq, locale)
        }
    if rule.end_date is not None:
        text += gettext(", until %(end)s", locale) % {
            "end": _point(rule.end_date, rule.freq, locale)
        }
    if rule.max_count is not None:
        text += ngettext(
            ", %(num)s time in total", ", %(num)s times in total", rule.max_count, locale
        ) % {"num": rule.max_count}
    if rule.weekend_policy == WeekendPolicy.PREVIOUS:
        text += gettext("; a weekend or holiday moves it to the working day before", locale)
    elif rule.weekend_policy == WeekendPolicy.NEXT:
        text += gettext("; a weekend or holiday moves it to the working day after", locale)
    return text
