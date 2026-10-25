"""Reminder message texts in the recipient's language (F3.6).

Plain text: no markup reaches the notification services. Amounts appear only when the channel
asks for them (``include_amounts``).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from assetboard.branding import APP_NAME
from assetboard.domain.months import YearMonth
from assetboard.domain.notifications import Reminder, ReminderKind
from assetboard.i18n import gettext, ngettext
from assetboard.i18n.dates import format_day, format_month
from assetboard.i18n.numbers import format_money


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


def reminder_line(reminder: Reminder, *, today: date, locale: str, include_amounts: bool) -> str:
    if reminder.kind is ReminderKind.WORK_LOG:
        assert reminder.work_month is not None
        # The same words as the banner on the Month and Transactions pages.
        return gettext(_("Enter days worked for %(month)s for %(name)s."), locale) % {
            "month": format_month(YearMonth.from_date(reminder.work_month), locale),
            "name": reminder.label,
        }
    assert reminder.due_date is not None
    if reminder.kind is ReminderKind.OVERDUE:
        line = gettext(_("Overdue since %(date)s: %(label)s"), locale) % {
            "date": format_day(reminder.due_date, locale),
            "label": reminder.label,
        }
    elif reminder.due_date == today:
        line = gettext(_("Due today: %(label)s"), locale) % {"label": reminder.label}
    else:
        line = gettext(_("Due %(date)s: %(label)s"), locale) % {
            "date": format_day(reminder.due_date, locale),
            "label": reminder.label,
        }
    if include_amounts and reminder.amount is not None:
        line = f"{line} · {format_money(reminder.amount, locale)}"
    return line


def reminder_message(
    reminders: Sequence[Reminder],
    *,
    today: date,
    locale: str,
    workspace_name: str,
    include_amounts: bool,
    link: str | None,
) -> tuple[str, str]:
    """``(title, body)`` for one message holding ``reminders``."""
    count = len(reminders)
    title = ngettext(
        "%(app)s · %(workspace)s: %(num)d reminder",
        "%(app)s · %(workspace)s: %(num)d reminders",
        count,
        locale,
    ) % {"app": APP_NAME, "workspace": workspace_name, "num": count}
    lines = [
        reminder_line(reminder, today=today, locale=locale, include_amounts=include_amounts)
        for reminder in reminders
    ]
    if link:
        lines += ["", link]
    return title, "\n".join(lines)


def test_message(*, locale: str, workspace_name: str) -> tuple[str, str]:
    title = gettext(_("%(app)s · %(workspace)s: test"), locale) % {
        "app": APP_NAME,
        "workspace": workspace_name,
    }
    body = gettext(
        _("This is a test message from %(app)s. Reminders will arrive on this channel."), locale
    ) % {"app": APP_NAME}
    return title, body
