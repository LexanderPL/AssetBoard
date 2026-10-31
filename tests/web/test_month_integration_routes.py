"""F3.5 month integration over HTTP, as placed by F2.7: expected rows with Confirm in the tables
of the Transactions page; on the Month summary ("Plan and Actual", owner 2026-10-31) the pending
amounts under Actual, the striped pending parts of the bars and the flow bar, the forecast as a
hover and screen-reader detail and the "Show pending" toggle; and the overdue badge on the
Upcoming nav link.

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


def _actual(row: str) -> str:
    match = re.search(r"data-actual>([^<]*)<", row)
    assert match is not None
    return match.group(1)


def _pending(row: str) -> str | None:
    """The row's "+ X pending" line, or None."""
    match = re.search(r"<span [^>]*data-pending>([^<]*)<", row)
    return match.group(1) if match else None


def _plan(owner: OwnerApp, category: UUID, amount: str, month: str = "2026-03") -> None:
    response = owner.post(
        f"/m/{month}/plan/{category}", {"amount": amount}, token_from="/m/" + month
    )
    assert response.status_code == 303


def test_expected_rows_sit_in_their_table_and_confirming_makes_them_real(rent) -> None:
    owner, category, occurrences = rent
    march = occurrences[2]
    table = _expense_table(owner.client.get("/transactions/2026-03").text)
    assert f'data-pending-occurrence="{march}"' in table
    assert ">Expected" in table and ">Confirm</summary>" in table
    assert "data-details-close" in table and ">Cancel</button>" in table
    # Only a one-time planned entry can be deleted from its expected row; a repeating rule
    # keeps its row (skip it on the Upcoming page or edit the rule instead).
    assert ">Delete</button>" not in table
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

    # The summary moves the amount from pending to Actual.
    row = _summary_row(owner.client.get("/m/2026-03").text, category)
    assert _actual(row) == "€98.50"
    assert _pending(row) is None and "If everything pending arrives" not in row


def test_viewers_see_expected_rows_without_confirm(rent) -> None:
    owner, _category, occurrences = rent
    owner.set_admin_role("viewer")
    table = _expense_table(owner.client.get("/transactions/2026-03").text)
    assert f'data-pending-occurrence="{occurrences[2]}"' in table
    assert "Confirm" not in table and ">Delete</button>" not in table


def test_a_repeating_expected_row_cannot_be_deleted(rent) -> None:
    owner, _category, occurrences = rent
    response = owner.post(
        f"/upcoming/{occurrences[2]}/delete",
        {"return_month": "2026-03"},
        token_from="/transactions/2026-03",
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/transactions/2026-03?error=not_planned"
    assert (
        "Only a one-time planned entry can be deleted here."
        in owner.client.get("/transactions/2026-03?error=not_planned").text
    )
    with owner.db() as session:
        assert session.get(ScheduledOccurrence, occurrences[2]) is not None


def test_summary_shows_plan_and_actual_with_pending(rent) -> None:
    owner, category, _occurrences = rent
    html = owner.client.get("/m/2026-03").text
    summary = _block(html, r'<section id="month-summary".*?</section>')
    # Two columns, Plan and Actual (and Difference); no Expected or Forecast column or line.
    headings = re.findall(r'<th scope="col"[^>]*>([^<]*)</th>', summary)
    assert headings == ["Category", "Plan", "Actual", "Difference"]
    assert "Expected (pending)" not in summary and ">Forecast</th>" not in summary
    assert "Forecast spent" not in summary and "forecast earned" not in summary
    # The pending amount is a lighter line under the confirmed one, with the forecast as its
    # tooltip and screen-reader text.
    row = _summary_row(summary, category)
    assert _actual(row) == "€0.00"
    assert _pending(row) == "+ €100.00 pending"
    assert 'title="If everything pending arrives: €100.00"' in row
    assert '<span class="sr-only"> (If everything pending arrives: €100.00)</span>' in row
    total = _block(summary, r'<tr id="summary-total-expense".*?</tr>')
    assert ">+ €100.00 pending<" in total
    # KPIs: Actual with "+ X pending"; left if everything pending arrives.
    assert re.search(r'data-pending data-kpi-pending="spent">\+ €100.00 pending<', summary)
    assert 'data-kpi-pending="earned"' not in summary  # nothing earned is pending
    assert re.search(r'data-kpi-pending="net">-€100.00 if everything pending arrives<', summary)
    assert "Pending: earned €0.00, spent €100.00, saved or invested €0.00." in summary
    # The flow bar ("Spent vs earned") draws the pending spending striped; the confirmed-only
    # twin is hidden until the toggle hides the pending parts.
    assert re.search(r"data-flow-heading>Spent vs earned<", summary)
    assert re.search(
        r'fill="url\(#flow-pending-danger\)"[^>]*data-pending-flow-segment="spent-pending"', summary
    )
    assert re.search(r'<svg class="[^"]* hidden"[^>]*data-flow-bar data-pending-off>', summary)
    # The toggle is "Show pending" (app.js hides everything marked data-pending).
    assert re.search(r'data-pending-toggle aria-pressed="true"[^>]*>Show pending</button>', summary)
    assert "data-forecast-toggle" not in summary
    # Home keeps its forecast line and second bar (a follow-up), and has no toggle.
    home = owner.client.get("/").text
    assert "data-pending-toggle" not in home and "Forecast spent" in home
    assert "data-pending" not in home


def test_pending_over_plan_is_a_softer_hint(rent) -> None:
    owner, category, _occurrences = rent
    _plan(owner, category, "50")  # 0 spent, 100 pending: over plan only if it arrives
    summary = _block(
        owner.client.get("/m/2026-03").text, r'<section id="month-summary".*?</section>'
    )
    row = _summary_row(summary, category)
    assert "data-over-plan" not in row  # confirmed is not over plan: no tint
    assert re.search(
        r'text-warning-strong" data-pending data-pending-over>'
        r"Over plan if everything pending arrives<",
        row,
    )
    bar = _block(summary, r'data-summary-bar="expense".*?</svg>')
    assert "Spent €0.00 of €50.00 planned" in bar and "(over plan)" not in bar
    assert "+ €100.00 pending" in bar and "(If everything pending arrives: €100.00)" in bar
    assert "(over plan if everything pending arrives)" in bar
    # The scale is the forecast (100): plan at 50, the striped pending part from 0 to 100.
    assert re.search(r'x1="50"[^>]*data-bar-planned', bar)
    assert re.search(
        r'<rect x="0" y="3" width="100" height="10" fill="url\(#bar-pending-expense\)" '
        r"data-pending data-bar-pending><title>If everything pending arrives: €100.00</title>",
        bar,
    )
    _plan(owner, category, "150")  # within plan even when it arrives: no hint
    summary = _block(
        owner.client.get("/m/2026-03").text, r'<section id="month-summary".*?</section>'
    )
    assert "data-pending-over" not in summary
    assert "+ €100.00 pending" in _summary_row(summary, category)


def test_a_future_month_labels_the_chart_as_expected(rent) -> None:
    owner, category, _occurrences = rent
    future = _block(
        owner.client.get("/m/2026-04").text, r'<section id="month-summary".*?</section>'
    )
    assert re.search(r"data-flow-heading>Expected spent vs expected earned<", future)
    assert _pending(_summary_row(future, category)) == "+ €100.00 pending"
    assert 'data-pending-flow-segment="spent-pending"' in future
    current = _block(
        owner.client.get("/m/2026-03").text, r'<section id="month-summary".*?</section>'
    )
    assert re.search(r"data-flow-heading>Spent vs earned<", current)
    # A month with nothing pending has no pending parts at all.
    owner.post(f"/upcoming/{_occurrences[0]}/skip", token_from="/upcoming")
    january = _block(
        owner.client.get("/m/2026-01").text, r'<section id="month-summary".*?</section>'
    )
    assert re.search(r"data-flow-heading>Spent vs earned<", january)
    assert "data-pending-off" not in january and "data-pending-flow-segment" not in january
    assert _pending(_summary_row(january, category)) is None
    owner.client.headers["Accept-Language"] = "pl"
    polish = _block(
        owner.client.get("/m/2026-04").text, r'<section id="month-summary".*?</section>'
    )
    assert "Pokaż oczekujące" in polish and "oczekuje" in polish


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


def test_skip_a_repeating_expected_row_and_undo_it(rent) -> None:
    owner, category, occurrences = rent
    march = occurrences[2]
    table = _expense_table(owner.client.get("/transactions/2026-03").text)
    row = _block(table, rf'<tr [^>]*data-pending-occurrence="{march}".*?</tr>')
    assert "data-skip" in row and f"/upcoming/{march}/skip" in row
    assert 'name="return_month" value="2026-03"' in row
    assert _pending(_summary_row(owner.client.get("/m/2026-03").text, category)) is not None

    skipped = owner.post(
        f"/upcoming/{march}/skip", {"return_month": "2026-03"}, token_from="/transactions/2026-03"
    )
    assert skipped.headers["location"] == "/transactions/2026-03?notice=skipped"
    page = owner.client.get(skipped.headers["location"]).text
    assert "Skipped. It no longer counts as pending." in page
    table = _expense_table(page)
    assert f'data-pending-occurrence="{march}"' not in table
    row = _block(table, rf'<tr [^>]*data-skipped-occurrence="{march}".*?</tr>')
    assert ">Skipped<" in row and "data-unskip" in row and ">Undo</button>" in row
    # A skipped date does not count as pending in the Month summary.
    assert _pending(_summary_row(owner.client.get("/m/2026-03").text, category)) is None
    with owner.db() as session:
        stored = session.get(ScheduledOccurrence, march)
        assert stored is not None and stored.status == "skipped"

    undone = owner.post(
        f"/upcoming/{march}/unskip", {"return_month": "2026-03"}, token_from="/transactions/2026-03"
    )
    assert undone.headers["location"] == "/transactions/2026-03?notice=skip_undone"
    table = _expense_table(owner.client.get("/transactions/2026-03").text)
    assert f'data-pending-occurrence="{march}"' in table
    assert "data-skipped-occurrence" not in table
    assert _pending(_summary_row(owner.client.get("/m/2026-03").text, category)) is not None

    again = owner.post(
        f"/upcoming/{march}/unskip", {"return_month": "2026-03"}, token_from="/transactions/2026-03"
    )
    assert again.headers["location"] == "/transactions/2026-03?notice=skip_undone"


def test_viewers_see_skipped_rows_without_undo(rent) -> None:
    owner, _category, occurrences = rent
    owner.post(
        f"/upcoming/{occurrences[2]}/skip",
        {"return_month": "2026-03"},
        token_from="/transactions/2026-03",
    )
    owner.set_admin_role("viewer")
    table = _expense_table(owner.client.get("/transactions/2026-03").text)
    assert f'data-skipped-occurrence="{occurrences[2]}"' in table
    assert "data-unskip" not in table and "data-skip " not in table
    refused = owner.post(
        f"/upcoming/{occurrences[2]}/unskip",
        {"return_month": "2026-03"},
        token_from="/transactions/2026-03",
    )
    assert refused.status_code == 403
    with owner.db() as session:
        stored = session.get(ScheduledOccurrence, occurrences[2])
        assert stored is not None and stored.status == "skipped"
