"""Loan page helpers (F7.3): schedule pagination and the machine-readable CSV."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from assetboard.domain.ledger_csv import plain_amount
from assetboard.domain.loans import Schedule, ScheduleRow

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


__all__ = ["SCHEDULE_PER_PAGE", "SchedulePageView", "schedule_csv", "schedule_page"]
