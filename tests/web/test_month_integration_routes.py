"""F3.5 month integration over HTTP, as placed by F2.7: expected rows with Confirm in the tables
of the Transactions page, the Expected and Forecast columns, forecast KPIs and the forecast toggle
on the Month summary, and the overdue badge on the Upcoming nav link.

The missing-work-log banner on both pages is covered in tests/web/test_income_routes.py and its
window in tests/unit/test_income_service.py."""

from __future__ import annotations

import re
from uuid import UUID

import pytest
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.models.ledger import Transaction
from assetboard.db.models.scheduled import ScheduledOccurrence
from tests.conftest import OwnerApp
from tests.web.test_upcoming_routes import create_recurring


@pytest.fixture
def rent(owner: OwnerApp) -> tuple[OwnerApp, UUID, list[UUID]]:
    """A monthly 100.00 expense on day 1 from January 2026; today is 2026-03-01, so January
    and February are overdue and March is due today."""
    clock = FixedClock("2026-03-01T12:00:00+00:00")
    owner.app.state.clock = clock
    owner.app.state.session_factory.configure(info={"clock": clock})
    assert create_recurring(owner, include_past="1").status_code == 303
    with owner.db() as session:
        rows = session.scalars(
            select(ScheduledOccurrence).order_by(ScheduledOccurrence.due_date)
        ).all()
        category = rows[0].category_id
        occurrences = [row.id for row in rows]
    return owner, category, occurrences


def _block(html: str, pattern: str) -> str:
    match = re.search(pattern, html, re.DOTALL)
    assert match is not None
    return match.group(0)


def _expense_table(html: str) -> str:
    return _block(html, r'<section id="ledger-expense".*?</section>')


def _summary_row(html: str, category: UUID) -> str:
    return _block(html, rf'<tr id="plan-{category}".*?</tr>')


def _cell(row: str, name: str) -> str:
    match = re.search(rf"<td [^>]*data-{name}>([^<]*)<", row)
    assert match is not None
    return match.group(1)


def test_expected_rows_sit_in_their_table_and_confirming_makes_them_real(rent) -> None:
    owner, category, occurrences = rent
    march = occurrences[2]
    table = _expense_table(owner.client.get("/transactions/2026-03").text)
    assert f'data-pending-occurrence="{march}"' in table
    assert ">Expected" in table and ">Confirm</summary>" in table
    assert 'name="return_month" value="2026-03"' in table
    income_table = _block(
        owner.client.get("/transactions/2026-03").text, r'<section id="ledger-income".*?</section>'
    )
    assert "data-pending-occurrence" not in income_table

    confirmed = owner.post(
        f"/upcoming/{march}/post",
        {"day": "2026-03-01", "amount": "98.50", "return_month": "2026-03"},
        token_from="/transactions/2026-03",
    )
    assert confirmed.status_code == 303
    with owner.db() as session:
        transaction = session.scalars(select(Transaction)).one()
    table = _expense_table(owner.client.get("/transactions/2026-03").text)
    assert f'data-pending-occurrence="{march}"' not in table
    assert f'data-transaction="{transaction.id}"' in table

    # The summary moves the amount from Expected to Actual.
    row = _summary_row(owner.client.get("/m/2026-03").text, category)
    assert "98.50" in _cell(row, "actual")
    assert "98.50" in _cell(row, "forecast")


def test_viewers_see_expected_rows_without_confirm(rent) -> None:
    owner, _category, occurrences = rent
    owner.set_admin_role("viewer")
    table = _expense_table(owner.client.get("/transactions/2026-03").text)
    assert f'data-pending-occurrence="{occurrences[2]}"' in table
    assert "Confirm" not in table


def test_summary_expected_and_forecast_columns_kpis_and_toggle(rent) -> None:
    owner, category, _occurrences = rent
    html = owner.client.get("/m/2026-03").text
    summary = _block(html, r'<section id="month-summary".*?</section>')
    assert ">Expected (pending)</th>" in summary and ">Forecast</th>" in summary
    row = _summary_row(summary, category)
    assert "100.00" in _cell(row, "forecast-column data-expected")
    assert "100.00" in _cell(row, "forecast-column data-forecast")
    assert "Forecast spent" in summary and "forecast earned" in summary
    # The toggle hides both forecast columns and the forecast KPI line (app.js).
    assert 'data-forecast-toggle aria-pressed="true"' in summary
    assert len(re.findall(r"<th [^>]*data-forecast-column", summary)) == 2
    assert re.search(r"<p [^>]*data-forecast-column>[^<]*Forecast spent", summary)
    # Home shares the flow bar and its forecast line, but has no toggle.
    home = owner.client.get("/").text
    assert "data-forecast-toggle" not in home


def test_overdue_badge_counts_overdue_occurrences_and_links_to_upcoming(rent) -> None:
    owner, _category, occurrences = rent

    def badge() -> str | None:
        link = _block(owner.client.get("/m/2026-03").text, r"<a [^>]*data-upcoming-link.*?</a>")
        assert 'href="/upcoming"' in link
        match = re.search(r'data-overdue-badge="(\d+)"', link)
        return match.group(1) if match else None

    assert badge() == "2"  # January and February; March is due today, not overdue
    assert "2 overdue occurrences" in owner.client.get("/m/2026-03").text
    owner.post(f"/upcoming/{occurrences[0]}/skip", token_from="/upcoming")
    assert badge() == "1"
    assert "1 overdue occurrence<" in owner.client.get("/m/2026-03").text
    owner.post(
        f"/upcoming/{occurrences[1]}/post",
        {"day": "2026-02-01", "amount": "100"},
        token_from="/upcoming",
    )
    assert badge() is None
