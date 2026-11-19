"""The year-overview report as CSV (F9.2).

Pure: no I/O. The file follows the project's CSV conventions (UTF-8, comma, RFC 4180 quoting,
CRLF line ends, plain decimals with the currency's minor digits, no locale grouping) and carries
exactly the rows and totals of the year-overview grid on ``/reports``: one row per category, the
twelve monthly amounts, a row total, and a final total row.
"""

from __future__ import annotations

import csv
import io

from assetboard.domain.ledger_csv import plain_signed_amount
from assetboard.domain.reports import CategorySeries

YEAR_OVERVIEW_COLUMNS = ("category_id", "category", "archived")


def year_overview_csv(series: CategorySeries, *, year: int) -> str:
    """The year grid as CSV text, with ISO month columns (``2026-01``) and plain amounts.

    ``series`` must be the monthly ``year_grid`` result for ``year`` (twelve periods). A combined
    "Other" row is not part of the grid and is rejected rather than exported under a name that
    the page translates.
    """
    if any(row.other for row in series.rows):
        raise ValueError("a combined category row cannot be exported from the year grid")
    if len(series.periods) != 12:
        raise ValueError("the year grid must have twelve months")
    buffer = io.StringIO()
    writer = csv.writer(
        buffer,
        delimiter=",",
        quotechar='"',
        quoting=csv.QUOTE_MINIMAL,
        lineterminator="\r\n",
    )
    month_headers = [f"{year:04d}-{month:02d}" for month in range(1, 13)]
    writer.writerow([*YEAR_OVERVIEW_COLUMNS, *month_headers, "total"])
    for row in series.rows:
        writer.writerow(
            [
                str(row.category_id),
                row.name,
                "true" if row.archived else "false",
                *(
                    plain_signed_amount(amount.amount_minor, amount.currency)
                    for amount in row.amounts
                ),
                plain_signed_amount(row.total.amount_minor, row.total.currency),
            ]
        )
    writer.writerow(
        [
            "",
            "Total",
            "false",
            *(plain_signed_amount(total.amount_minor, total.currency) for total in series.totals),
            plain_signed_amount(series.total.amount_minor, series.total.currency),
        ]
    )
    return buffer.getvalue()


__all__ = ["YEAR_OVERVIEW_COLUMNS", "year_overview_csv"]
