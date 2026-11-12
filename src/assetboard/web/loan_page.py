"""Loan page helpers (F7.3): schedule pagination and the machine-readable CSV."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from assetboard.domain.ledger_csv import plain_amount
from assetboard.domain.loans import OverpaymentMode, OverpaymentStrategy, Schedule, ScheduleRow
from assetboard.i18n import gettext
from assetboard.i18n.dates import format_day
from assetboard.i18n.numbers import format_money, format_quantity_input
from assetboard.services.loan_scenarios import ScenarioParameters

SCHEDULE_PER_PAGE = 24


@dataclass(frozen=True, slots=True)
class SchedulePageView:
    rows: tuple[ScheduleRow, ...]
    page: int
    pages: int
    total: int
    today_is_due: bool
    highlight_next_date: date | None
    next_row: ScheduleRow | None


def schedule_page(
    schedule: Schedule | None, today: date, requested: str | None
) -> SchedulePageView:
    """The requested page, or the page containing today's/next due row by default."""
    if schedule is None or not schedule.rows:
        return SchedulePageView((), 1, 1, 0, False, None, None)
    rows = schedule.rows
    pages = max(1, (len(rows) + SCHEDULE_PER_PAGE - 1) // SCHEDULE_PER_PAGE)
    default_index = next(
        (index for index, row in enumerate(rows) if row.date >= today),
        len(rows) - 1,
    )
    default_page = default_index // SCHEDULE_PER_PAGE + 1
    try:
        wanted = int(requested) if requested else default_page
    except ValueError:
        wanted = default_page
    page = wanted if 1 <= wanted <= pages else default_page
    start = (page - 1) * SCHEDULE_PER_PAGE
    today_is_due = any(row.date == today for row in rows)
    next_row = next((row for row in rows if row.date > today), None)
    highlight_next = next_row.date if not today_is_due and next_row is not None else None
    return SchedulePageView(
        rows=tuple(rows[start : start + SCHEDULE_PER_PAGE]),
        page=page,
        pages=pages,
        total=len(rows),
        today_is_due=today_is_due,
        highlight_next_date=highlight_next,
        next_row=next_row,
    )


def _rate_text(annual_rate_bp: int) -> str:
    return format(Decimal(annual_rate_bp).scaleb(-2), "f")


def schedule_csv(schedule: Schedule) -> str:
    """All schedule rows as machine-readable CSV (UTF-8 text, CRLF, no BOM)."""
    buffer = io.StringIO()
    writer = csv.writer(
        buffer,
        delimiter=",",
        quotechar='"',
        quoting=csv.QUOTE_MINIMAL,
        lineterminator="\r\n",
    )
    writer.writerow(
        [
            "n",
            "date",
            "rate",
            "opening",
            "interest",
            "principal",
            "overpayment",
            "costs",
            "total",
            "closing",
            "currency",
        ]
    )
    for row in schedule.rows:
        writer.writerow(
            [
                row.n,
                row.date.isoformat(),
                _rate_text(row.annual_rate_bp),
                plain_amount(row.opening.amount_minor, schedule.currency),
                plain_amount(row.interest.amount_minor, schedule.currency),
                plain_amount(row.principal.amount_minor, schedule.currency),
                plain_amount(row.overpayment.amount_minor, schedule.currency),
                plain_amount(row.costs.amount_minor, schedule.currency),
                plain_amount(row.installment_total.amount_minor, schedule.currency),
                plain_amount(row.closing.amount_minor, schedule.currency),
                schedule.currency,
            ]
        )
    return buffer.getvalue()


def _shift_text(shift_bp: int, locale: str) -> str:
    points = format_quantity_input(Decimal(abs(shift_bp)).scaleb(-2), locale)
    if shift_bp > 0:
        return gettext("+%(points)s pp", locale) % {"points": points}
    return gettext("-%(points)s pp", locale) % {"points": points}


def scenario_summary(parameters: ScenarioParameters | None, locale: str) -> tuple[str, ...]:
    """A compact, translated description of the parameters that differ from the loan."""
    if parameters is None:
        return (gettext("Same as the loan", locale),)
    parts: list[str] = []
    mode = parameters.overpayment_mode
    if mode is OverpaymentMode.NONE:
        parts.append(gettext("No regular overpayment", locale))
    elif mode is OverpaymentMode.FIXED_EXTRA and parameters.overpayment_amount is not None:
        parts.append(
            gettext("Extra %(amount)s each month", locale)
            % {"amount": format_money(parameters.overpayment_amount, locale)}
        )
    elif mode is OverpaymentMode.TARGET_TOTAL and parameters.overpayment_amount is not None:
        parts.append(
            gettext("Target total %(amount)s each month", locale)
            % {"amount": format_money(parameters.overpayment_amount, locale)}
        )
    if parameters.strategy is OverpaymentStrategy.REDUCE_TERM:
        parts.append(gettext("Shorten the term", locale))
    elif parameters.strategy is OverpaymentStrategy.REDUCE_INSTALLMENT:
        parts.append(gettext("Lower the installment", locale))
    if parameters.rate_shift_bp is not None and parameters.rate_shift_from is not None:
        parts.append(
            gettext("Rate %(shift)s from %(date)s", locale)
            % {
                "shift": _shift_text(parameters.rate_shift_bp, locale),
                "date": format_day(parameters.rate_shift_from, locale),
            }
        )
    if parameters.one_off_amount is not None and parameters.one_off_date is not None:
        parts.append(
            gettext("One-off %(amount)s on %(date)s", locale)
            % {
                "amount": format_money(parameters.one_off_amount, locale),
                "date": format_day(parameters.one_off_date, locale),
            }
        )
    return tuple(parts) or (gettext("Same as the loan", locale),)


__all__ = [
    "SCHEDULE_PER_PAGE",
    "SchedulePageView",
    "scenario_summary",
    "schedule_csv",
    "schedule_page",
]
