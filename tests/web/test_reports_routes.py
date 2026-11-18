"""The Reports page over HTTP (F10.1/F10.2): default range, filters, charts/tables, notes and
access."""

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


def _category(
    app: OwnerApp,
    name: str,
    kind: str = "expense",
    treatment: str = "regular",
    *,
    archived: bool = False,
    color: str = "#16a34a",
) -> UUID:
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
                    "color": color,
                    "sort_order": 0,
                    "import_key": None,
                    "import_batch_id": None,
                    "archived_at": STAMP if archived else None,
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
    assert "data-report-category-section" in html


# F10.2 savings rate


def _rate_data(html: str) -> dict:
    match = re.search(
        r'<script type="application/json" id="reports-rate-data">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match is not None
    return json.loads(match.group(1))


def _rate_row(html: str, label: str) -> str:
    for row in re.findall(r"<tr data-report-rate-row>.*?</tr>", html, re.DOTALL):
        if re.search(r"data-report-rate-period>" + re.escape(label) + "<", row):
            return row
    raise AssertionError(label)


def _cells(row: str) -> list[str]:
    cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
    return [re.sub(r"<[^>]+>", "", cell).strip() for cell in cells]


def _seed_rate_golden(app: OwnerApp) -> None:
    salary = _category(app, "Rate Salary", "income")
    food = _category(app, "Rate Food")
    savings = _category(app, "Rate Savings", "expense", "savings")
    for month in range(1, 13):
        _tx(app, salary, date(2025, month, 5), 300_000, direction="income")
        _tx(app, food, date(2025, month, 6), 120_000)
        _tx(app, savings, date(2025, month, 7), 180_000)  # 60.0 % saved
    _tx(app, salary, date(2026, 1, 5), 300_000, direction="income")
    _tx(app, food, date(2026, 1, 6), 420_000)  # saved 0: 0.0 %
    _tx(app, food, date(2026, 2, 6), 10_000)  # nothing earned: no rate


def test_the_rate_line_table_and_rolling_rate(reports: OwnerApp) -> None:
    _seed_rate_golden(reports)
    html = _page(reports, "from=2025&to=2026&step=month")
    assert 'data-chart="report-rate"' in html
    assert "data-report-rate-section" in html
    assert "Savings rate by period" in html
    assert "12-month rolling rate" in html

    data = _rate_data(html)
    assert [series["key"] for series in data["series"]] == ["rate", "rolling"]
    assert [series["name"] for series in data["series"]] == [
        "Savings rate",
        "12-month rolling rate",
    ]
    # The rolling line starts at the 12th month of the range (December 2025).
    assert data["series"][1]["values"][:11] == [None] * 11
    assert data["series"][1]["values"][11] == 600
    assert data["series"][0]["values"][11] == 600
    # January 2026 saves nothing (0.0 %) and February has no rate, so the line has a gap.
    assert data["series"][0]["values"][12] == 0
    assert data["series"][0]["values"][13] is None
    assert data["y_labels"]["0"] == "0.0%"
    assert data["series"][0]["amounts"][13] == ""

    january = _rate_row(html, "January 2026")
    assert _cells(january) == ["0.0%", "55.0%"]
    february = _rate_row(html, "February 2026")
    assert _cells(february)[0] == "—Not available: nothing earned"
    # The rolling window still has earnings even though February itself has none.
    assert _cells(february)[1] == "54.5%"

    total = re.search(r"<tr [^>]*data-report-rate-total.*?</tr>", html, re.DOTALL)
    assert total is not None
    assert "55.4%" in total.group(0)  # 2 160 000 saved over 3 900 000 earned


def test_a_yearly_rate_comes_from_the_year_sums(reports: OwnerApp) -> None:
    salary = _category(reports, "Rate Salary", "income")
    savings = _category(reports, "Rate Savings", "expense", "savings")
    _tx(reports, salary, date(2026, 1, 5), 400_000, direction="income")
    _tx(reports, savings, date(2026, 1, 6), 200_000)
    _tx(reports, salary, date(2026, 2, 5), 100_000, direction="income")

    html = _page(reports, "from=2026&to=2026&step=year")
    data = _rate_data(html)
    assert [series["key"] for series in data["series"]] == ["rate"]
    assert data["series"][0]["values"] == [400]
    assert data["y_labels"]["400"] == "40.0%"
    assert "data-report-rolling-rate" not in html
    row = _rate_row(html, "2026")
    assert _cells(row) == ["40.0%"]
    assert "25.0%" not in html


def test_a_range_without_earnings_has_no_rate_chart(reports: OwnerApp) -> None:
    food = _category(reports, "Rate Food")
    _tx(reports, food, date(2026, 3, 6), 10_000)
    html = _page(reports, "from=2026&to=2026&step=month")
    assert "data-report-rate-section" in html
    assert "No savings rate for the charted entries." in html
    assert 'id="reports-rate-data"' not in html
    assert "Not available: nothing earned" in html
    assert "data-report-rolling-rate" not in html  # fewer than 12 months
    total = re.search(r"<tr [^>]*data-report-rate-total.*?</tr>", html, re.DOTALL)
    assert total is not None and "—" in total.group(0)


# F10.3 expenses by category


def _category_data(html: str) -> dict:
    match = re.search(
        r'<script type="application/json" id="reports-category-data">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match is not None
    return json.loads(match.group(1))


def _category_row(html: str, label: str) -> str:
    for row in re.findall(r"<tr data-report-category-row>.*?</tr>", html, re.DOTALL):
        header = re.sub(r"<svg.*?</svg>", "", row, flags=re.DOTALL)
        if re.search(r">\s*" + re.escape(label) + r"\s*</th>", header):
            return row
    raise AssertionError(label)


def _category_cells(row: str) -> list[str]:
    cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
    return [re.sub(r"<[^>]+>", "", cell).strip() for cell in cells]


def test_the_category_section_has_a_stacked_chart_and_its_table(reports: OwnerApp) -> None:
    food = _category(reports, "Golden Food")
    rent = _category(reports, "Golden Rent", color="#2563eb")
    salary = _category(reports, "Golden Salary", "income")
    savings = _category(reports, "Golden Savings", "expense", "savings")
    _tx(reports, food, date(2026, 1, 10), 12_000)
    _tx(reports, food, date(2026, 2, 10), 13_000)
    _tx(reports, rent, date(2026, 1, 11), 90_000)
    _tx(reports, salary, date(2026, 1, 12), 300_000, direction="income")
    _tx(reports, savings, date(2026, 1, 13), 20_000)

    html = _page(reports, "from=2026&to=2026&step=month")
    assert "data-report-category-section" in html
    assert ">Expenses by category over time</h2>" in html
    assert "Spending by category and period" in html
    assert 'data-chart="report-category-bars"' in html
    assert 'name="category"' in html and "multiple" in html
    assert "Golden Food" in html and "Golden Rent" in html
    assert "Golden Salary" not in html  # income is not offered
    assert "Golden Savings" not in html  # savings only with the option
    assert 'name="savings"' in html

    data = _category_data(html)
    assert data["stack"] == "spending"
    assert [series["name"] for series in data["series"]] == ["Golden Rent", "Golden Food"]
    assert [series["color"] for series in data["series"]] == ["#2563eb", "#16a34a"]
    assert data["series"][0]["values"][0] == 90_000
    assert data["series"][1]["values"][:2] == [12_000, 13_000]
    assert data["labels"][0] == "January 2026"
    assert "Golden Rent: €900.00" in data["tooltips"][0]
    assert "Total: €1,020.00" in data["tooltips"][0]

    rent_row = _category_row(html, "Golden Rent")
    assert _category_cells(rent_row)[-1] == "€900.00"
    food_row = _category_row(html, "Golden Food")
    assert _category_cells(food_row)[:2] == ["€120.00", "€130.00"]
    totals = re.search(r"<tr [^>]*data-report-category-totals.*?</tr>", html, re.DOTALL)
    assert totals is not None
    assert re.search(r"data-report-category-totals>.*?€1,020\.00", totals.group(0), re.DOTALL)
    assert "The 8 largest categories are shown" not in html  # only two categories

    with_savings = _page(reports, "from=2026&to=2026&step=month&savings=1")
    assert 'name="savings" value="1" checked' in with_savings
    assert "Golden Savings" in with_savings
    savings_data = _category_data(with_savings)
    assert {series["name"] for series in savings_data["series"]} == {
        "Golden Rent",
        "Golden Food",
        "Golden Savings",
    }
    assert savings_data["tooltips"][0][-1] == "Total: €1,220.00"


def test_the_category_filter_is_repeatable_and_shows_no_other(reports: OwnerApp) -> None:
    categories = [_category(reports, f"Filter Category {index}") for index in range(1, 11)]
    for index, category in enumerate(categories, start=1):
        _tx(reports, category, date(2026, 1, 10), index * 1_000)

    query = "from=2026&to=2026&step=month&" + "&".join(
        f"category={categories[index]}" for index in (0, 2)
    )
    html = _page(reports, query)
    assert f'<option value="{categories[0]}" selected>' in html
    assert f'<option value="{categories[1]}" selected>' not in html
    data = _category_data(html)
    assert [series["name"] for series in data["series"]] == [
        "Filter Category 3",
        "Filter Category 1",
    ]
    assert "Other" not in [series["name"] for series in data["series"]]
    assert "Showing the selected categories only." in html
    assert "The 8 largest categories are shown" not in html

    # A selected category without spending still gets a zero table row; the chart is empty.
    empty = _category(reports, "Filter Empty")
    html = _page(reports, f"from=2026&to=2026&step=month&category={empty}")
    assert "No expenses in this range." in html
    assert 'id="reports-category-data"' not in html
    assert ">Other</th>" not in html
    assert _category_cells(_category_row(html, "Filter Empty"))[-1] == "€0.00"


def test_unknown_category_ids_are_ignored_and_top_eight_gets_other(reports: OwnerApp) -> None:
    categories = [_category(reports, f"Top Category {index:02d}") for index in range(1, 11)]
    for index, category in enumerate(categories, start=1):
        _tx(reports, category, date(2026, 1, 10), index * 1_000)

    html = _page(
        reports,
        f"from=2026&to=2026&step=month&category=junk&category={uuid4()}",
    )
    data = _category_data(html)
    assert len(data["series"]) == 9
    assert data["series"][-1]["name"] == "Other"
    assert data["series"][-1]["values"][0] == 3_000  # the two smallest: 1 000 + 2 000
    assert "The 8 largest categories are shown; the rest are combined as Other." in html
    rows = re.findall(r"<tr data-report-category-row>", html)
    assert len(rows) == 9
    assert _category_cells(_category_row(html, "Other"))[-1] == "€30.00"


def test_an_archived_category_appears_with_its_name(reports: OwnerApp) -> None:
    old = _category(reports, "Archived Food", archived=True)
    empty = _category(reports, "Archived Empty", archived=True)
    _tx(reports, old, date(2026, 4, 10), 4_200)

    html = _page(reports, "from=2026&to=2026&step=month")
    data = _category_data(html)
    assert [series["name"] for series in data["series"]] == ["Archived Food"]
    assert "Archived Empty" not in html
    assert f'<option value="{old}">Archived Food</option>' in html
    assert f'<option value="{empty}">' not in html


def test_a_range_without_spending_shows_the_category_empty_state(reports: OwnerApp) -> None:
    salary = _category(reports, "Empty Salary", "income")
    _tx(reports, salary, date(2026, 1, 10), 5_000, direction="income")
    html = _page(reports, "from=2026&to=2026&step=month")
    assert "data-report-category-section" in html
    assert "No expenses in this range." in html
    assert 'id="reports-category-data"' not in html

    with_savings = _page(reports, "from=2026&to=2026&step=month&savings=1")
    assert "No expenses or savings in this range." in with_savings
