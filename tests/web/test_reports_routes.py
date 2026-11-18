"""The Reports page over HTTP (F10.1): default range, filters, chart/table, notes and access."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert

from assetboard.clock import FixedClock
from assetboard.db.models.ledger import Category, Transaction
from tests.conftest import OwnerApp

NOW = "2026-10-14T12:00:00+00:00"
STAMP = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def reports(owner: OwnerApp) -> OwnerApp:
    owner.app.state.clock = FixedClock(NOW)
    return owner


def _category(app: OwnerApp, name: str, kind: str = "expense", treatment: str = "regular") -> UUID:
    category_id = uuid4()
    with app.db() as session, session.begin():
        session.execute(
            insert(Category),
            [
                {
                    "id": category_id,
                    "workspace_id": app.home_id,
                    "name": name,
                    "name_key": name.casefold(),
                    "kind": kind,
                    "treatment": treatment,
                    "color": "#16a34a",
                    "sort_order": 0,
                    "import_key": None,
                    "import_batch_id": None,
                    "archived_at": None,
                    "created_at": STAMP,
                    "updated_at": STAMP,
                }
            ],
        )
    return category_id


def _tx(
    app: OwnerApp,
    category_id: UUID,
    day: date,
    minor: int,
    *,
    direction: str = "expense",
    currency: str = "EUR",
    deleted: bool = False,
) -> None:
    with app.db() as session, session.begin():
        session.execute(
            insert(Transaction),
            [
                {
                    "id": uuid4(),
                    "workspace_id": app.home_id,
                    "direction": direction,
                    "date": day,
                    "amount_minor": minor,
                    "currency": currency,
                    "category_id": category_id,
                    "description": "Synthetic",
                    "description_key": "synthetic",
                    "import_key": None,
                    "import_batch_id": None,
                    "source": "manual",
                    "created_by_user_id": None,
                    "created_at": STAMP,
                    "updated_at": STAMP,
                    "deleted_at": STAMP if deleted else None,
                }
            ],
        )


def _page(app: OwnerApp, query: str = "") -> str:
    response = app.client.get("/reports" + (f"?{query}" if query else ""))
    assert response.status_code == 200, response.text
    return response.text


def _row_labels(html: str) -> list[str]:
    return re.findall(r"data-report-period>([^<]*)<", html)


def _sidebar(html: str) -> str:
    match = re.search(r'<nav [^>]*aria-label="Main navigation".*?</nav>', html, re.DOTALL)
    assert match is not None
    return match.group(0)


def test_the_default_page_is_the_current_and_two_previous_years(reports: OwnerApp) -> None:
    html = _page(reports)
    assert "<h1" in html and ">Reports</h1>" in html
    assert "data-report-range>2024 – 2026<" in html
    assert len(_row_labels(html)) == 34  # January 2024 through October 2026
    for heading in ("Period", "Earned", "Spent", "Saved", "Net"):
        assert f">{heading}</th>" in html
    assert "data-report-total" in html
    assert "data-chart-empty" in html  # no data yet
    assert "data-chart-table-toggle" in html
    assert 'data-chart-source="reports-data"' not in html  # no JSON without a chart


def test_the_sidebar_entry_follows_history(reports: OwnerApp) -> None:
    nav = _sidebar(_page(reports))
    assert re.search(
        r'<a class="nav-link" href="/reports"[^>]*data-reports-link[^>]*aria-current="page"', nav
    )
    assert nav.index("data-history-link") < nav.index("data-reports-link")
    assert nav.index("data-reports-link") < nav.index("data-upcoming-link")
    assert "▦" in nav


def test_monthly_and_yearly_steps_and_month_links(reports: OwnerApp) -> None:
    food = _category(reports, "Golden Food")
    salary = _category(reports, "Golden Salary", "income")
    _tx(reports, food, date(2026, 1, 10), 12_000)
    _tx(reports, food, date(2026, 2, 10), 13_000)
    _tx(reports, salary, date(2026, 2, 10), 300_000, direction="income")

    html = _page(reports, "from=2025&to=2026&step=month")
    assert len(_row_labels(html)) == 22  # January 2025 through October 2026 (today)
    assert "data-report-range>2025 – 2026<" in html
    assert 'href="/m/2026-01"' in html and "data-report-period>January 2026<" in html
    assert "data-report-so-far>so far<" in html
    assert 'data-chart="report-bars"' in html
    assert 'data-chart-source="reports-data"' in html
    assert "data-chart-table-toggle" in html and "hidden" in html
    table_wrapper = re.search(r'<div id="reports-table"[^>]*>', html)
    assert table_wrapper is not None and "sr-only" not in table_wrapper.group(0)
    chart_source = re.search(
        r'<script type="application/json" id="reports-data">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert chart_source is not None
    data = json.loads(chart_source.group(1))
    assert [series["key"] for series in data["series"]] == ["earned", "spent", "saved"]
    assert data["labels"][0] == "January 2025" and data["labels"][-1] == "October 2026"

    yearly = _page(reports, "from=2025&to=2026&step=year")
    assert _row_labels(yearly) == ["2025", "2026"]
    assert 'href="/m/2025"' not in yearly
    assert '"labels": ["2025", "2026"]' in yearly
    assert re.search(
        r"data-report-period>2026</span>.*?data-report-so-far>so far<", yearly, re.DOTALL
    )


def test_errors_mark_the_field_and_hide_the_chart(reports: OwnerApp) -> None:
    html = _page(reports, "from=2026&to=2025")
    assert "The first year must be the same as or before the last year." in html
    assert 'aria-invalid="true"' in html
    assert "data-report-section" not in html
    assert 'data-chart="report-bars"' not in html

    too_long = _page(reports, "from=1996&to=2026")
    assert "Choose at most 30 years." in too_long
    assert "data-report-section" not in too_long

    malformed = _page(reports, "from=20x6&to=2026")
    assert "Enter a year between 1900 and 2199." in malformed
    assert "data-report-section" not in malformed


def test_unknown_parameters_and_values_are_ignored(reports: OwnerApp) -> None:
    html = _page(reports, "surprise=1&step=week&currency=ZZZ&from=2024&to=2026")
    assert "data-report-range>2024 – 2026<" in html
    assert len(_row_labels(html)) == 34
    assert "Amounts in EUR." in html


def test_the_currency_selector_appears_only_with_a_second_currency(reports: OwnerApp) -> None:
    food = _category(reports, "Golden Food")
    _tx(reports, food, date(2026, 3, 5), 1_000, currency="EUR")
    html = _page(reports, "from=2026&to=2026&step=month")
    assert 'name="currency"' not in html
    assert "Amounts in EUR." in html

    _tx(reports, food, date(2026, 3, 6), 2_000, currency="USD")
    html = _page(reports, "from=2026&to=2026&step=month")
    assert 'name="currency"' in html
    assert '<option value="EUR" selected>EUR</option>' in html
    assert '<option value="USD">USD</option>' in html
    assert "entry in another currency is left out" in html

    dollars = _page(reports, "from=2026&to=2026&step=month&currency=USD")
    assert '<option value="USD" selected>USD</option>' in dollars
    assert "entry in another currency is left out" in dollars


def test_future_rows_are_noted_and_do_not_make_a_chart(reports: OwnerApp) -> None:
    food = _category(reports, "Golden Food")
    _tx(reports, food, date(2026, 10, 20), 10_000)
    _tx(reports, food, date(2026, 11, 2), 20_000)
    html = _page(reports, "from=2026&to=2026&step=month")
    assert "2 entries are dated after today and are not charted." in html
    assert "data-chart-empty" in html
    assert 'data-chart-source="reports-data"' not in html
    assert all(value == "€0.00" for value in re.findall(r"data-report-net>([^<]*)<", html))


def test_soft_deleted_and_transfer_rows_do_not_count(reports: OwnerApp) -> None:
    food = _category(reports, "Golden Food")
    transfer = _category(reports, "Golden Transfer", "expense", "transfer")
    savings = _category(reports, "Golden Savings", "expense", "savings")
    _tx(reports, food, date(2026, 4, 4), 10_000)
    _tx(reports, food, date(2026, 4, 5), 99_000, deleted=True)
    _tx(reports, transfer, date(2026, 4, 6), 50_000)
    _tx(reports, savings, date(2026, 4, 7), 20_000)
    html = _page(reports, "from=2026&to=2026&step=month")
    row = re.search(
        r"<tr data-report-row>.*?data-report-period>April 2026<.*?</tr>", html, re.DOTALL
    )
    assert row is not None
    cells = re.findall(r"<td[^>]*>(.*?)</td>", row.group(0), re.DOTALL)
    amounts = [re.sub(r"<[^>]+>", "", cell).strip() for cell in cells]
    # Earned, spent, saved and net: the transfer adds nothing, the deleted row never counts.
    assert amounts[-4:] == ["€0.00", "€100.00", "€200.00", "-€300.00"]
    assert "€990.00" not in html and "€500.00" not in html


def test_an_empty_range_shows_zeros_and_the_total_row(reports: OwnerApp) -> None:
    html = _page(reports, "from=2019&to=2020&step=year")
    assert _row_labels(html) == ["2019", "2020"]
    assert "data-chart-empty" in html
    assert "data-report-total" in html
    assert html.count("€0.00") >= 4


def test_viewers_may_open_the_page(reports: OwnerApp) -> None:
    user_id = reports.add_user("vic")
    reports.join(user_id, reports.home_id, "viewer")
    reports.client.cookies.clear()
    response = reports.web.sign_in(
        reports.client, email="vic@example.com", password=reports.web.admin_password
    )
    assert response.status_code == 303
    html = _page(reports)
    assert ">Reports</h1>" in html
    assert re.search(r'<a class="nav-link" href="/reports"', _sidebar(html))
