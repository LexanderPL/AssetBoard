"""The F8.3/F8.13 scenario results CSV: one nominal row per plan year.

The format is machine-readable and nominal only: UTF-8 without a BOM, comma-delimited, CRLF
line endings, plain dot decimals with exactly the currency's minor digits and no grouping or
currency symbols. Flags are ``1``/``0``; the header is fixed.
"""

from __future__ import annotations

import csv
import io

from assetboard.domain.fire import Projection
from assetboard.domain.ledger_csv import plain_signed_amount

FIRE_RESULT_COLUMNS = (
    "year",
    "age",
    "income",
    "expenses",
    "loan_payment",
    "net",
    "gain",
    "cash",
    "net_worth",
    "is_fire",
    "income_stopped",
    "currency",
)


def fire_scenario_csv(projection: Projection) -> str:
    """The whole file as text, with one nominal value per plan year."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(FIRE_RESULT_COLUMNS)
    for row in projection.rows:
        currency = row.income.currency
        writer.writerow(
            [
                row.year,
                row.age,
                plain_signed_amount(row.income.amount_minor, currency),
                plain_signed_amount(row.expenses.amount_minor, currency),
                plain_signed_amount(row.loan_payment.amount_minor, currency),
                plain_signed_amount(row.net.amount_minor, currency),
                plain_signed_amount(row.gain.amount_minor, currency),
                plain_signed_amount(row.cash.amount_minor, currency),
                plain_signed_amount(row.net_worth.amount_minor, currency),
                "1" if row.is_fire else "0",
                "1" if row.income_stopped else "0",
                currency,
            ]
        )
    return buffer.getvalue()


__all__ = ["FIRE_RESULT_COLUMNS", "fire_scenario_csv"]
