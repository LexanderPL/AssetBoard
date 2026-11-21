"""The Reports page over HTTP (F10.1/F10.2): default range, filters, charts/tables, notes and
access."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select

from assetboard.clock import FixedClock
from assetboard.db.models.ledger import BudgetLine, Category, Transaction
from assetboard.web.security import report_filters_cookie_name
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


def _budget_line(app: OwnerApp, category_id: UUID, month: date, minor: int) -> None:
    with app.db() as session, session.begin():
        session.execute(
            insert(BudgetLine),
            [
                {
                    "id": uuid4(),
                    "workspace_id": app.home_id,
                    "month": month,
                    "category_id": category_id,
                    "planned_minor": minor,
                    "currency": "EUR",
                    "import_batch_id": None,
                    "created_at": STAMP,
                    "updated_at": STAMP,
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


def test_the_default_page_is_the_current_year_when_there_is_no_data(
    reports: OwnerApp,
) -> None:
    html = _page(reports)
    assert "<h1" in html and ">Reports</h1>" in html
    assert 'class="mx-auto max-w-6xl space-y-6" data-reports' in html
    # UX-06: one empty state instead of 10 zero rows and a dash table.
    assert "data-empty-state" in html and "No transactions in this range" in html
    assert "Add an entry or change the years." in html
    assert "data-report-row" not in html and "data-report-total" not in html
    assert "data-report-range" not in html and "data-report-nav" not in html
    assert '<select id="report-from" name="from"' in html
    assert '<select id="report-to" name="to"' in html
    assert '<option value="2026" selected>2026</option>' in html
    assert "data-filter-disclosure" in html


def test_the_filter_form_fills_the_panel_and_aligns_controls(reports: OwnerApp) -> None:
    html = _page(reports, "from=2026&to=2026&step=month")
    form = re.search(r'<form id="report-filters".*?</form>', html, re.DOTALL)
    assert form is not None
    form_html = form.group(0)
    assert 'class="grid grid-cols-1 items-start gap-4 sm:grid-cols-2 lg:grid-cols-4"' in form_html
    assert "sm:items-end" not in form_html
    assert "sm:grid-cols-4" not in form_html
    # Every field is a min-width-zero grid item; the controls stay full width.
    assert '<div class="min-w-0">' in form_html
    assert '<div class="min-w-0 sm:col-span-2 lg:col-span-2">' in form_html  # Granularity
    assert form_html.count('<div class="min-w-0 sm:col-span-2">') == 2  # Categories, savings
    assert (
        '<div class="flex flex-wrap items-center gap-3 sm:col-span-2 lg:col-span-4">' in form_html
    )
    for control in (
        'id="report-from"',
        'id="report-to"',
        'id="report-step"',
        "data-multi-select",
    ):
        assert control in form_html
    assert "w-full" in form_html
    # Helper text sits after its control, never above it.
    assert form_html.index('id="report-from"') < form_html.index("Leave empty for all categories")
    assert form_html.index("data-multi-select") < form_html.index("Leave empty for all categories")
    assert form_html.index('name="savings"') < form_html.index(
        "Saved and invested amounts get their own category series."
    )

    # The moved trend window leaves a hidden field so Show report keeps the active window.
    assert '<input type="hidden" name="trend" value="3">' in form_html

    # With a second currency the selector appears and Granularity keeps one column.
    _tx(reports, _category(reports, "Form USD"), date(2026, 3, 5), 1_000, currency="USD")
    with_currency = re.search(
        r'<form id="report-filters".*?</form>',
        _page(reports, "from=2026&to=2026&step=month"),
        re.DOTALL,
    )
    assert with_currency is not None
    assert 'id="report-currency" name="currency"' in with_currency.group(0)
    assert "min-w-0 sm:col-span-2 lg:col-span-2" not in with_currency.group(0)

    # A field error is rendered below its control, inside the same grid item.
    invalid = _page(reports, "from=20x6&to=2026")
    assert invalid.index('id="report-from"') < invalid.index('id="report-from-error"')
    assert re.search(
        r'id="report-from-error"[^>]*>Enter a year between 1900 and 2199\.</p>',
        invalid,
    )


def test_the_year_dropdowns_only_offer_years_with_transactions(reports: OwnerApp) -> None:
    food = _category(reports, "Year Food")
    for year in (2019, 2021, 2024, 2027):
        _tx(reports, food, date(year, 1, 10), 1_000)
    _tx(reports, food, date(2017, 1, 10), 1_000, deleted=True)
    html = _page(reports)
    assert '<option value="2019" selected>2019</option>' in html
    assert '<option value="2021">2021</option>' in html
    assert '<option value="2024" selected>2024</option>' in html
    assert '<option value="2020">' not in html
    assert '<option value="2022">' not in html
    assert '<option value="2017">' not in html  # soft-deleted-only years are not offered
    assert '<option value="2027">' not in html  # future-only years are not charted
    assert "data-report-range>2019 – 2024<" in html
    assert _row_labels(html) == ["2019", "2020", "2021", "2022", "2023", "2024"]


def test_the_sidebar_entry_is_last(reports: OwnerApp) -> None:
    nav = _sidebar(_page(reports))
    assert re.search(
        r'<a class="nav-link" href="/reports"[^>]*data-reports-link[^>]*aria-current="page"', nav
    )
    assert nav.index("data-history-link") < nav.index("data-reports-link")
    assert nav.index("data-upcoming-link") < nav.index("data-reports-link")
    assert nav.index("data-fire-link") < nav.index("data-reports-link")
    assert "▦" not in nav
    assert re.search(r'data-reports-link.*?<svg class="h-5 w-5 shrink-0"', nav, re.DOTALL)


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
    assert data["links"][0].endswith("/m/2025-01")
    assert data["links"][-1].endswith("/m/2026-10")

    yearly = _page(reports, "from=2025&to=2026&step=year")
    assert _row_labels(yearly) == ["2025", "2026"]
    assert "year=2025" in yearly and "#year-overview" in yearly
    assert '"labels": ["2025", "2026"]' in yearly
    assert '"links": [' in yearly
    assert re.search(r"data-report-period>2026</a>.*?data-report-so-far>so far<", yearly, re.DOTALL)


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
    _tx(reports, _category(reports, "Golden Food"), date(2025, 3, 5), 1_000)
    html = _page(reports, "surprise=1&step=week&currency=ZZZ&from=2024&to=2026")
    assert "data-report-range>2024 – 2026<" in html
    assert len(_row_labels(html)) == 34
    assert "Amounts in EUR." in html


def test_the_currency_selector_appears_only_with_a_second_currency(reports: OwnerApp) -> None:
    food = _category(reports, "Golden Food")
    _tx(reports, food, date(2026, 3, 5), 1_000, currency="EUR")
    html = _page(reports, "from=2026&to=2026&step=month")
    assert 'id="report-currency"' not in html
    assert "Amounts in EUR." in html

    _tx(reports, food, date(2026, 3, 6), 2_000, currency="USD")
    html = _page(reports, "from=2026&to=2026&step=month")
    assert 'id="report-currency" name="currency"' in html
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
    # The note survives the empty state, but no zero rows are drawn.
    assert "2 entries are dated after today and are not charted." in html
    assert "No transactions in this range" in html
    assert "data-report-row" not in html
    assert 'data-chart-source="reports-data"' not in html


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


def test_an_empty_range_shows_one_empty_state_not_zero_rows(reports: OwnerApp) -> None:
    html = _page(reports, "from=2019&to=2020&step=year")
    assert _row_labels(html) == []
    assert "data-empty-state" in html and "No transactions in this range" in html
    assert "data-report-total" not in html
    assert "data-report-rate-row" not in html
    assert html.count("€0.00") < 4  # only the filter's currency hint, no zero table


def test_viewers_may_open_the_page(reports: OwnerApp) -> None:
    _tx(reports, _category(reports, "Golden Food"), date(2026, 3, 5), 1_000)
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
    assert data["links"][0].endswith("/m/2025-01")

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
    assert 'name="category"' in html and "data-multi-select" in html
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
    assert data["links"][0].endswith("/m/2026-01")

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
    assert f'value="{categories[0]}" checked' in html
    assert f'value="{categories[1]}" checked' not in html
    data = _category_data(html)
    assert [series["name"] for series in data["series"]] == [
        "Filter Category 3",
        "Filter Category 1",
    ]
    assert "Other (combined)" not in [series["name"] for series in data["series"]]
    assert "Showing the selected categories only." in html
    assert "The 8 largest categories are shown" not in html

    # A selected category without spending still gets a zero table row; the chart is empty.
    empty = _category(reports, "Filter Empty")
    html = _page(reports, f"from=2026&to=2026&step=month&category={empty}")
    assert "No spending for the selected categories." in html
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
    assert data["series"][-1]["name"] == "Other (combined)"
    assert data["series"][-1]["values"][0] == 3_000  # the two smallest: 1 000 + 2 000
    assert "The 8 largest categories are shown; the rest are combined as Other (combined)." in html
    rows = re.findall(r"<tr data-report-category-row>", html)
    assert len(rows) == 9
    assert _category_cells(_category_row(html, "Other (combined)"))[-1] == "€30.00"


def test_an_archived_category_appears_with_its_name(reports: OwnerApp) -> None:
    old = _category(reports, "Archived Food", archived=True)
    empty = _category(reports, "Archived Empty", archived=True)
    _tx(reports, old, date(2026, 4, 10), 4_200)

    html = _page(reports, "from=2026&to=2026&step=month")
    data = _category_data(html)
    assert [series["name"] for series in data["series"]] == ["Archived Food (archived)"]
    assert "Archived Empty" not in html
    assert f'value="{old}"' in html and "Archived Food (archived)" in html
    assert f'value="{empty}"' not in html


def test_a_range_without_spending_shows_the_category_empty_state(reports: OwnerApp) -> None:
    salary = _category(reports, "Empty Salary", "income")
    _tx(reports, salary, date(2026, 1, 10), 5_000, direction="income")
    html = _page(reports, "from=2026&to=2026&step=month")
    assert "data-report-category-section" in html
    assert "No expenses in this range." in html
    assert 'id="reports-category-data"' not in html

    with_savings = _page(reports, "from=2026&to=2026&step=month&savings=1")
    assert "No expenses or savings in this range." in with_savings


def test_a_real_category_named_other_does_not_collide_with_the_combined_row(
    reports: OwnerApp,
) -> None:
    with reports.db() as session:
        real_other = (
            session.scalars(
                select(Category).where(Category.name == "Other", Category.kind == "expense")
            )
            .one()
            .id
        )
    others = [_category(reports, f"Other Collision {index}") for index in range(1, 9)]
    _tx(reports, real_other, date(2026, 1, 10), 100_000)
    for index, category in enumerate(others, start=1):
        _tx(reports, category, date(2026, 1, 10), index * 1_000)

    html = _page(reports, "from=2026&to=2026&step=month")
    names = [series["name"] for series in _category_data(html)["series"]]
    assert "Other" in names
    assert "Other (combined)" in names
    assert len(names) == len(set(names))
    assert _category_cells(_category_row(html, "Other"))[-1] == "€1,000.00"
    assert _category_cells(_category_row(html, "Other (combined)"))[-1] == "€10.00"


def test_an_active_and_an_archived_category_with_the_same_name_are_distinct(
    reports: OwnerApp,
) -> None:
    active = _category(reports, "Duplicate Name")
    archived = _category(reports, "Duplicate Name", archived=True)
    _tx(reports, active, date(2026, 1, 10), 10_000)
    _tx(reports, archived, date(2026, 1, 11), 20_000)

    html = _page(reports, "from=2026&to=2026&step=month")
    names = [series["name"] for series in _category_data(html)["series"]]
    assert names == ["Duplicate Name (archived)", "Duplicate Name"]
    assert f'value="{archived}"' in html and "Duplicate Name (archived)" in html
    assert f'value="{active}"' in html and "Duplicate Name" in html


def test_a_selected_archived_category_without_spending_stays_in_the_filter(
    reports: OwnerApp,
) -> None:
    archived = _category(reports, "Old Filter", archived=True)
    _tx(reports, archived, date(2025, 1, 10), 10_000)
    _tx(reports, _category(reports, "Other 2026"), date(2026, 3, 5), 1_000)
    html = _page(
        reports,
        f"from=2026&to=2026&step=month&category={archived}",
    )
    assert f'value="{archived}" checked' in html and "Old Filter (archived)" in html
    assert "No spending for the selected categories." in html


def test_the_filters_are_a_phone_disclosure_with_an_active_count(reports: OwnerApp) -> None:
    food = _category(reports, "Golden Food")
    _tx(reports, food, date(2026, 3, 5), 1_000)
    html = _page(reports, f"from=2024&to=2026&step=month&category={food}&savings=1")
    panel = re.search(r"<details[^>]*data-filter-disclosure.*?</form>", html, re.DOTALL)
    assert panel is not None
    summary = re.search(r"<summary.*?</summary>", panel.group(0), re.DOTALL)
    assert summary is not None
    assert "Filters" in summary.group(0)
    assert "3 filters" in summary.group(0)  # range, category, savings
    assert 'class="btn-primary"' in panel.group(0)
    assert "Clear filters" in panel.group(0)


def test_the_filter_panel_is_closed_by_default_and_opens_on_errors(reports: OwnerApp) -> None:
    _tx(reports, _category(reports, "Panel Food"), date(2026, 3, 5), 1_000)
    valid = _page(reports, "from=2026&to=2026&step=month")
    assert '<details class="panel" data-filter-disclosure>' in valid
    assert '<details class="panel" data-filter-disclosure open>' not in valid

    invalid = _page(reports, "from=2026&to=2025")
    assert '<details class="panel" data-filter-disclosure open>' in invalid
    assert "data-report-invalid" in invalid


def _report_cookie(reports: OwnerApp) -> str:
    return report_filters_cookie_name(reports.app.state.settings)


def test_the_filter_cookie_remembers_every_resolved_field(reports: OwnerApp) -> None:
    food = _category(reports, "Cookie Food")
    _tx(reports, food, date(2024, 3, 5), 1_000)
    _tx(reports, food, date(2025, 3, 5), 2_000)
    _tx(reports, food, date(2026, 3, 5), 3_000)

    response = reports.client.get(
        f"/reports?from=2024&to=2026&step=year&savings=1&year=2025&trend=12&category={food}"
    )
    assert response.status_code == 200
    name = _report_cookie(reports)
    cookie = next(
        header
        for header in response.headers.get_list("set-cookie")
        if header.startswith(f"{name}=")
    )
    for flag in ("HttpOnly", "SameSite=lax", "Path=/", "Max-Age=31536000"):
        assert flag in cookie
    value = reports.client.cookies[name]
    assert "from=2024" in value and "to=2026" in value
    assert "step=year" in value and "savings=1" in value
    assert "year=2025" in value and "trend=12" in value and f"category={food}" in value

    html = _page(reports)
    assert "data-report-range>2024 – 2026<" in html
    assert '<option value="year" selected>Yearly</option>' in html
    assert 'name="savings" value="1" checked' in html
    assert '<option value="2025" selected>2025</option>' in html
    assert '<option value="12" selected>12-month average</option>' in html
    assert f'value="{food}" checked' in html


def test_an_explicit_report_url_overrides_and_refreshes_the_cookie(reports: OwnerApp) -> None:
    food = _category(reports, "Override Food")
    for year in (2024, 2025, 2026):
        _tx(reports, food, date(year, 3, 5), 1_000)
    reports.client.get("/reports?from=2024&to=2026&step=year&savings=1&year=2025")

    html = _page(reports, "from=2024&to=2026&step=month")
    assert "data-report-range>2024 – 2026<" in html
    assert '<option value="month" selected>Monthly</option>' in html
    assert 'name="savings" value="1" checked' not in html
    value = reports.client.cookies[_report_cookie(reports)]
    assert "step=month" in value and "savings=1" not in value
    # The refreshed preference is what the next bare request sees.
    assert '<option value="month" selected>Monthly</option>' in _page(reports)


def test_a_remembered_default_year_does_not_inflate_the_filter_count(
    reports: OwnerApp,
) -> None:
    food = _category(reports, "Badge Food")
    _tx(reports, food, date(2026, 3, 5), 1_000)
    reports.client.get("/reports?from=2026&to=2026&step=year")

    panel = re.search(r"<details[^>]*data-filter-disclosure.*?</form>", _page(reports), re.DOTALL)
    assert panel is not None
    summary = re.search(r"<summary.*?</summary>", panel.group(0), re.DOTALL)
    assert summary is not None
    assert "1 filter" in summary.group(0)  # only the granularity differs from the defaults
    assert "2 filters" not in summary.group(0)


def test_an_invalid_explicit_url_does_not_refresh_the_cookie(reports: OwnerApp) -> None:
    food = _category(reports, "Invalid Cookie Food")
    for year in (2024, 2025, 2026):
        _tx(reports, food, date(year, 3, 5), 1_000)
    reports.client.get("/reports?from=2024&to=2026&step=year&savings=1")
    before = reports.client.cookies[_report_cookie(reports)]

    invalid = reports.client.get("/reports?from=2026&to=2025")
    assert invalid.status_code == 200
    assert "data-report-invalid" in invalid.text
    assert reports.client.cookies[_report_cookie(reports)] == before
    assert '<option value="year" selected>Yearly</option>' in _page(reports)


def test_clear_filters_deletes_the_cookie_and_redirects_clean(reports: OwnerApp) -> None:
    food = _category(reports, "Clear Food")
    for year in (2024, 2025, 2026):
        _tx(reports, food, date(year, 3, 5), 1_000)
    reports.client.get("/reports?from=2024&to=2024&step=year")
    name = _report_cookie(reports)
    assert name in reports.client.cookies

    response = reports.client.get("/reports?clear=1", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/reports"
    cleared = next(
        header
        for header in response.headers.get_list("set-cookie")
        if header.startswith(f"{name}=")
    )
    assert "Max-Age=0" in cleared
    assert name not in reports.client.cookies

    html = _page(reports)
    assert "data-report-range>2024 – 2026<" in html
    assert '<option value="month" selected>Monthly</option>' in html


def test_an_explicit_view_too_large_for_the_cookie_clears_it(reports: OwnerApp) -> None:
    categories = [_category(reports, f"Overflow {index}") for index in range(85)]
    _tx(reports, categories[0], date(2026, 3, 5), 1_000)
    query = "from=2026&to=2026&step=month&" + "&".join(
        f"category={category_id}" for category_id in categories
    )
    response = reports.client.get(f"/reports?{query}")
    assert response.status_code == 200
    name = _report_cookie(reports)
    cleared = next(
        header
        for header in response.headers.get_list("set-cookie")
        if header.startswith(f"{name}=")
    )
    assert "Max-Age=0" in cleared
    assert name not in reports.client.cookies


def test_an_invalid_cookie_falls_back_to_the_current_defaults(reports: OwnerApp) -> None:
    food = _category(reports, "Fallback Food")
    for year in (2024, 2025, 2026):
        _tx(reports, food, date(year, 3, 5), 1_000)
    name = _report_cookie(reports)
    reports.client.cookies.set(
        name, "from=2026&to=2025&step=year", domain="testserver.local", path="/"
    )

    response = reports.client.get("/reports")
    html = response.text
    assert "data-report-invalid" not in html
    assert '<details class="panel" data-filter-disclosure>' in html
    assert "data-report-range>2024 – 2026<" in html
    assert '<option value="month" selected>Monthly</option>' in html
    # The stale cookie is forgotten, so the next bare request does not resolve it twice.
    cleared = next(
        header
        for header in response.headers.get_list("set-cookie")
        if header.startswith(f"{name}=")
    )
    assert "Max-Age=0" in cleared
    assert name not in reports.client.cookies

    reports.client.cookies.set(name, "x=" + "a" * 5_000)
    oversized = _page(reports)
    assert "data-report-range>2024 – 2026<" in oversized


def test_a_non_base_currency_filter_explains_the_hidden_module_sections(
    reports: OwnerApp,
) -> None:
    food = _category(reports, "Golden Food")
    _tx(reports, food, date(2026, 3, 5), 1_000)
    _tx(reports, food, date(2026, 3, 6), 2_000, currency="USD")
    html = _page(reports, "from=2026&to=2026&step=month&currency=USD")
    assert "Net worth and portfolio are shown in the workspace base currency only." in html
    assert 'data-report-series="net-worth"' not in html
    assert 'data-report-series="portfolio"' not in html


def test_the_section_nav_links_each_visible_section(reports: OwnerApp) -> None:
    _tx(reports, _category(reports, "Golden Food"), date(2026, 3, 5), 1_000)
    html = _page(reports)
    nav = re.search(r"<nav[^>]*data-report-nav.*?</nav>", html, re.DOTALL)
    assert nav is not None
    assert "overflow-x-auto" not in nav.group(0)
    assert "whitespace-nowrap" not in nav.group(0)
    assert "flex flex-wrap" in nav.group(0)
    for anchor in (
        "reports-chart-heading",
        "reports-rate-heading",
        "reports-category-heading",
        "reports-year-overview-heading",
        "reports-trends-heading",
        "reports-yoy-heading",
        "reports-accuracy-heading",
    ):
        assert f'href="#{anchor}"' in nav.group(0)


def test_a_future_only_category_filter_has_no_zero_column_table(reports: OwnerApp) -> None:
    food = _category(reports, "Future Category")
    html = _page(
        reports,
        f"from=2027&to=2027&step=year&category={food}",
    )
    assert "data-report-category-row" not in html


# F9.2 year overview, category trends, year-over-year and plan accuracy


def _seed_grid(app: OwnerApp) -> dict[str, UUID]:
    categories = {
        "food": _category(app, "Grid Food"),
        "rent": _category(app, "Grid Rent", color="#2563eb"),
        "savings": _category(app, "Grid Savings", "expense", "savings"),
        "transfer": _category(app, "Grid Transfer", "expense", "transfer"),
        "salary": _category(app, "Grid Salary", "income"),
    }
    _tx(app, categories["food"], date(2026, 1, 10), 12_000)
    _tx(app, categories["food"], date(2026, 2, 10), 13_000)
    _tx(app, categories["rent"], date(2026, 1, 11), 90_000)
    _tx(app, categories["savings"], date(2026, 1, 12), 20_000)
    _tx(app, categories["transfer"], date(2026, 1, 13), 50_000)
    _tx(app, categories["salary"], date(2026, 1, 14), 300_000, direction="income")
    return categories


def _grid_row(html: str, label: str) -> str:
    for row in re.findall(r"<tr data-report-grid-row>.*?</tr>", html, re.DOTALL):
        header = re.sub(r"<svg.*?</svg>", "", row, flags=re.DOTALL)
        if re.search(r">\s*" + re.escape(label) + r"\s*</th>", header):
            return row
    raise AssertionError(label)


def _trend_row(html: str, label: str) -> str:
    for row in re.findall(r"<tr data-report-trend-row>.*?</tr>", html, re.DOTALL):
        header = re.sub(r"<svg.*?</svg>", "", row, flags=re.DOTALL)
        if re.search(r">\s*" + re.escape(label) + r"\s*</th>", header):
            return row
    raise AssertionError(label)


def _accuracy_row(html: str, label: str) -> str:
    for row in re.findall(r"<tr data-report-accuracy-row>.*?</tr>", html, re.DOTALL):
        header = re.sub(r"<svg.*?</svg>", "", row, flags=re.DOTALL)
        if re.search(r">\s*" + re.escape(label) + r"\s*</th>", header):
            return row
    raise AssertionError(label)


def test_the_year_overview_grid_has_twelve_months_totals_and_heat(reports: OwnerApp) -> None:
    _seed_grid(reports)
    html = _page(reports, "from=2026&to=2026&step=month&year=2026")
    assert "data-report-year-overview" in html
    assert ">Year overview</h2>" in html
    assert 'id="report-year" name="year" form="report-filters"' in html
    assert '<option value="2026" selected>2026</option>' in html
    assert "data-report-grid-so-far>so far<" in html
    assert "data-report-year-csv" not in html
    assert "/reports/year-overview.csv" not in html
    for month in (
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    ):
        assert f">{month}" in html
    # Only the regular expense categories are rows; savings, transfers and income stay out.
    rows = re.findall(r"<tr data-report-grid-row>", html)
    assert len(rows) == 2
    food = _grid_row(html, "Grid Food")
    assert _cells(food)[:2] == ["€120.00", "€130.00"]
    assert _cells(food)[-1] == "€250.00"
    rent = _grid_row(html, "Grid Rent")
    assert _cells(rent)[0] == "€900.00" and _cells(rent)[-1] == "€900.00"
    totals = re.search(r"<tr [^>]*data-report-grid-totals.*?</tr>", html, re.DOTALL)
    assert totals is not None
    assert _cells(totals.group(0))[:2] == ["€1,020.00", "€130.00"]
    assert _cells(totals.group(0))[-1] == "€1,150.00"
    assert "bg-danger-tint" not in re.search(
        r'<td class="([^"]*)" data-report-grid-cell>€900\.00</td>', html
    ).group(1)  # type: ignore[union-attr]
    assert "bg-danger-shade-1" not in re.search(
        r'<td class="([^"]*)" data-report-grid-cell>€120\.00</td>', html
    ).group(1)  # type: ignore[union-attr]
    # Sparse rows (two and one non-zero months) stay neutral under the Transactions gate.
    assert all(
        "bg-danger-" not in value
        for value in re.findall(r'<td class="([^"]*)" data-report-grid-cell>', html)
    )
    zero_classes = re.findall(r'<td class="([^"]*)" data-report-grid-cell>€0\.00</td>', food)
    assert zero_classes and all("bg-danger-" not in value for value in zero_classes)
    assert "heat-" not in html
    assert "Grid Savings" not in html and "Grid Transfer" not in html
    grid_section = re.search(
        r"<section [^>]*data-report-year-overview.*?</section>", html, re.DOTALL
    )
    assert grid_section is not None and 'href="/m/2026-01"' in grid_section.group(0)

    with_savings = _page(reports, "from=2026&to=2026&step=month&year=2026&savings=1")
    assert "Grid Savings" in with_savings
    assert _cells(_grid_row(with_savings, "Grid Savings"))[0] == "€200.00"

    ignored = _page(
        reports,
        f"from=2026&to=2026&step=month&year=2026&category={uuid4()}&category=junk",
    )
    assert "Grid Food" in ignored and "data-report-grid-row" in ignored
    empty_year = _page(reports, "from=2026&to=2026&step=month&year=2024")
    assert "No spending in this year." in empty_year
    unused = _category(reports, "Unused")
    empty_selection = _page(reports, f"from=2026&to=2026&step=month&year=2026&category={unused}")
    assert _cells(_grid_row(empty_selection, "Unused"))[-1] == "€0.00"


def test_the_year_overview_csv_endpoint_is_gone(reports: OwnerApp) -> None:
    assert reports.client.get("/reports/year-overview.csv").status_code == 404


def test_the_year_overview_heat_uses_the_transactions_gate_per_category_row(
    reports: OwnerApp,
) -> None:
    scale = _category(reports, "Heat Scale")
    for month, minor in (
        (1, 100),
        (2, 100),
        (3, 100),
        (4, 100),
        (5, 200),
        (6, 300),
        (7, 450),
        (8, 600),
    ):
        _tx(reports, scale, date(2026, month, 10), minor)
    sparse = _category(reports, "Heat Sparse")
    _tx(reports, sparse, date(2026, 1, 10), 100)
    _tx(reports, sparse, date(2026, 2, 10), 1_000)
    small = _category(reports, "Heat Small")
    for month, minor in ((1, 1), (2, 1), (3, 1), (4, 4)):
        _tx(reports, small, date(2026, month, 10), minor)

    html = _page(reports, "from=2026&to=2026&step=month&year=2026")
    scale_classes = re.findall(
        r'<td class="([^"]*)" data-report-grid-cell>',
        _grid_row(html, "Heat Scale"),
    )
    assert len(scale_classes) == 12
    assert all("bg-danger-" not in value for value in scale_classes[:5])
    assert "bg-danger-shade-2" in scale_classes[5]
    assert "bg-danger-shade-3" in scale_classes[6]
    assert "bg-danger-tint" in scale_classes[7]
    assert all("bg-danger-" not in value for value in scale_classes[8:])

    sparse_classes = re.findall(
        r'<td class="([^"]*)" data-report-grid-cell>',
        _grid_row(html, "Heat Sparse"),
    )
    assert all("bg-danger-" not in value for value in sparse_classes)

    # The scale is row-local: a tiny row's outlier still stands out.
    small_classes = re.findall(
        r'<td class="([^"]*)" data-report-grid-cell>',
        _grid_row(html, "Heat Small"),
    )
    assert "bg-danger-tint" in small_classes[3]
    assert all("bg-danger-" not in value for value in small_classes[:3])


def test_category_trends_show_three_and_twelve_month_windows(reports: OwnerApp) -> None:
    food = _category(reports, "Trend Food")
    rent = _category(reports, "Trend Rent")
    for month, minor in ((1, 10_000), (2, 20_000), (3, 30_000)):
        _tx(reports, food, date(2026, month, 10), minor)
    _tx(reports, rent, date(2026, 1, 10), 5_000)

    html = _page(reports, "from=2026&to=2026&step=month&trend=3")
    assert "data-report-trends" in html
    assert ">Category trends</h2>" in html
    assert "data-report-trend-row" in html
    food_row = _trend_row(html, "Trend Food")
    cells = _cells(food_row)
    assert cells[0].startswith("—") and cells[1].startswith("—")
    assert cells[2:5] == ["€200.00", "€166.67", "€100.00"]
    assert "data-report-trend-cell" in food_row
    data = json.loads(
        re.search(
            r'<script type="application/json" id="reports-trend-data">(.*?)</script>',
            html,
            re.DOTALL,
        ).group(1)  # type: ignore[union-attr]
    )
    assert data["series"][0]["name"] == "Trend Food"
    assert data["series"][0]["values"][:5] == [None, None, 20_000, 16_667, 10_000]
    assert data["links"][0].endswith("/m/2026-01")
    assert 'data-chart="report-trend"' in html
    assert 'data-chart-source="reports-trend-data"' in html
    assert '<option value="3" selected>' in html

    twelve = _page(reports, "from=2025&to=2026&step=month&trend=12")
    twelve_cells = _cells(_trend_row(twelve, "Trend Food"))
    assert all(cell.startswith("—") for cell in twelve_cells[:11])
    assert not twelve_cells[11].startswith("—")
    assert '<option value="12" selected>' in twelve

    yearly = _page(reports, "from=2025&to=2026&step=year")
    assert "Choose monthly granularity to see rolling averages." in yearly
    assert "data-report-trend-row" not in yearly


def test_category_trends_follow_the_selected_categories(reports: OwnerApp) -> None:
    food = _category(reports, "Trend Food")
    rent = _category(reports, "Trend Rent")
    _tx(reports, food, date(2026, 1, 10), 10_000)
    _tx(reports, rent, date(2026, 1, 11), 20_000)
    html = _page(reports, f"from=2026&to=2026&step=month&category={food}")
    trend_section = re.search(r"<section [^>]*data-report-trends.*?</section>", html, re.DOTALL)
    assert trend_section is not None
    assert "Trend Food" in trend_section.group(0)
    assert "Trend Rent" not in trend_section.group(0)


def test_the_category_trends_window_is_its_own_form(reports: OwnerApp) -> None:
    food = _category(reports, "Trend Form Food")
    _tx(reports, food, date(2026, 1, 10), 10_000)
    html = _page(
        reports,
        f"from=2025&to=2026&step=month&currency=EUR&category={food}&savings=1&year=2025&trend=12",
    )
    top_form = re.search(r'<form id="report-filters".*?</form>', html, re.DOTALL)
    assert top_form is not None
    assert 'id="report-trend"' not in top_form.group(0)
    assert '<input type="hidden" name="trend" value="12">' in top_form.group(0)

    section = re.search(r"<section [^>]*data-report-trends.*?</section>", html, re.DOTALL)
    assert section is not None
    trend_form = section.group(0)
    assert '<form method="get" action="/reports"' in trend_form
    assert "data-report-trend-form" in trend_form
    assert 'id="report-trend" name="trend"' in trend_form
    assert "data-autosubmit" in trend_form
    assert 'data-autosubmit-anchor="#category-trends"' in trend_form
    assert '<option value="12" selected>12-month average</option>' in trend_form
    assert "Window:" not in trend_form
    assert '<noscript><button type="submit" formaction="/reports#category-trends"' in trend_form
    assert 'class="btn-sm">Show</button></noscript>' in trend_form
    for field, value in (
        ("from", "2025"),
        ("to", "2026"),
        ("step", "month"),
        ("currency", "EUR"),
        ("category", str(food)),
        ("savings", "1"),
        ("year", "2025"),
    ):
        assert f'<input type="hidden" name="{field}" value="{value}">' in trend_form

    without_savings = _page(reports, "from=2026&to=2026&step=month")
    plain_section = re.search(
        r"<section [^>]*data-report-trends.*?</section>", without_savings, re.DOTALL
    )
    assert plain_section is not None
    assert 'name="savings"' not in plain_section.group(0)
    assert '<input type="hidden" name="year" value="2026">' in plain_section.group(0)


def test_year_over_year_has_a_row_per_year_with_income_expense_and_savings_changes(
    reports: OwnerApp,
) -> None:
    food = _category(reports, "YOY Food")
    rent = _category(reports, "YOY Rent")
    _tx(reports, food, date(2025, 1, 10), 10_000)
    _tx(reports, food, date(2026, 1, 10), 15_000)
    _tx(reports, rent, date(2025, 1, 11), 5_000)
    _tx(reports, rent, date(2026, 1, 11), 2_000)

    html = _page(reports, "from=2025&to=2026&step=month&year=2026")
    assert "data-report-year-over-year" in html
    assert "data-report-yoy-partial" not in html
    assert "data-report-yoy-so-far" not in html
    assert "data-report-yoy-in-progress" in html
    assert "The current year is still in progress; no projection is added." in html
    assert "Investment purchases recorded in Investments are not included." in html
    rows = re.findall(r"<tr data-report-yoy-row>.*?</tr>", html, re.DOTALL)
    assert len(rows) == 2
    current = next(row for row in rows if "data-report-yoy-year>2026<" in row)
    cells = _cells(current)
    assert cells[:3] == ["€0.00", "€170.00", "€0.00"]
    assert "no income in the previous year" in cells[4]
    assert cells[5] == "+€20.00"
    assert cells[6] == "+13.3%"
    assert cells[7] == "€0.00"
    assert "no savings in the previous year" in cells[8]
    assert "#year-overview" in current
    assert "text-danger" in re.search(
        r'<td class="([^"]*)" data-report-yoy-expense-change>', current
    ).group(1)  # type: ignore[union-attr]

    previous = next(row for row in rows if "data-report-yoy-year>2025<" in row)
    previous_cells = _cells(previous)
    assert previous_cells[:3] == ["€0.00", "€150.00", "€0.00"]
    assert previous_cells[3:] == ["—No comparison"] * 6
    assert "No data for 2024." in previous
    assert "data-report-yoy-no-previous" in previous

    total = re.search(r"<tr [^>]*data-report-yoy-total.*?</tr>", html, re.DOTALL)
    assert total is not None
    assert _cells(total.group(0))[:3] == ["€0.00", "€320.00", "€0.00"]
    assert 'colspan="6"' in total.group(0)

    filtered = _page(
        reports,
        f"from=2025&to=2026&step=month&year=2026&category={food}",
    )
    filtered_row = next(
        row
        for row in re.findall(r"<tr data-report-yoy-row>.*?</tr>", filtered, re.DOTALL)
        if "data-report-yoy-year>2026<" in row
    )
    assert _cells(filtered_row) == cells


def test_year_over_year_first_range_year_keeps_changes_when_a_prior_year_exists(
    reports: OwnerApp,
) -> None:
    food = _category(reports, "First Range Food")
    _tx(reports, food, date(2025, 1, 10), 10_000)
    _tx(reports, food, date(2026, 1, 10), 20_000)

    html = _page(reports, "from=2026&to=2026&step=month&year=2026")
    row = re.search(r"<tr data-report-yoy-row>.*?</tr>", html, re.DOTALL)
    assert row is not None
    cells = _cells(row.group(0))
    assert cells[:3] == ["€0.00", "€200.00", "€0.00"]
    assert cells[5] == "+€100.00"
    assert cells[6] == "+100.0%"
    assert "No data for 2025." not in row.group(0)
    assert "data-report-yoy-no-previous" not in row.group(0)


def test_year_over_year_savings_only_year_is_not_empty(reports: OwnerApp) -> None:
    savings = _category(reports, "YOY Savings", "expense", "savings")
    _tx(reports, savings, date(2026, 1, 10), 5_000)

    html = _page(reports, "from=2026&to=2026&step=month&year=2026")
    assert "No income, expenses or savings in this range." not in html
    row = re.search(r"<tr data-report-yoy-row>.*?</tr>", html, re.DOTALL)
    assert row is not None
    assert _cells(row.group(0))[:3] == ["€0.00", "€0.00", "€50.00"]


def test_plan_accuracy_counts_planned_and_over_months(reports: OwnerApp) -> None:
    food = _category(reports, "Accuracy Food")
    rent = _category(reports, "Accuracy Rent")
    savings = _category(reports, "Accuracy Savings", "expense", "savings")
    salary = _category(reports, "Accuracy Salary", "income")
    _tx(reports, food, date(2026, 1, 10), 10_000)
    _tx(reports, food, date(2026, 2, 10), 20_000)
    _tx(reports, food, date(2026, 3, 10), 5_000)
    _tx(reports, rent, date(2026, 1, 10), 30_000)
    _tx(reports, savings, date(2026, 1, 10), 40_000)
    _tx(reports, salary, date(2026, 1, 10), 50_000, direction="income")
    _budget_line(reports, food, date(2026, 1, 1), 10_000)
    _budget_line(reports, food, date(2026, 2, 1), 15_000)
    _budget_line(reports, food, date(2026, 3, 1), 10_000)
    _budget_line(reports, rent, date(2026, 1, 1), 30_000)
    _budget_line(reports, savings, date(2026, 1, 1), 100)
    _budget_line(reports, salary, date(2026, 1, 1), 100)

    html = _page(reports, "from=2026&to=2026&step=month")
    assert "data-report-accuracy" in html
    assert ">Planning accuracy</h2>" in html
    assert "data-report-accuracy-row" in html
    assert _cells(_accuracy_row(html, "Accuracy Food")) == ["3", "1", "33.3%"]
    assert _cells(_accuracy_row(html, "Accuracy Rent")) == ["1", "0", "0.0%"]
    total = re.search(r"<tr [^>]*data-report-accuracy-total.*?</tr>", html, re.DOTALL)
    assert total is not None
    assert "All categories" in total.group(0)
    assert _cells(total.group(0)) == ["4", "1", "25.0%"]
    assert "A month counts as over when actual spending is greater than the plan." in html
    assert "Only planned regular expenses count; income and savings are excluded." in html
    score = re.search(
        r"<p class=\"text-3xl[^\"]*\" data-report-accuracy-score>(.*?)</p>", html, re.DOTALL
    )
    assert score is not None
    assert "84.6%" in score.group(1)
    bar = re.search(r"<svg[^>]*data-report-accuracy-bar>", html)
    assert bar is not None
    assert 'aria-valuenow="84"' in bar.group(0)
    assert 'aria-valuetext="84.6%"' in bar.group(0)
    assert 'width="84.6"' in html
    assert "Accuracy Savings" not in html and "Accuracy Salary" not in html
    with_savings = _page(reports, "from=2026&to=2026&step=month&savings=1")
    accuracy_section = re.search(
        r"<section [^>]*data-report-accuracy.*?</section>", with_savings, re.DOTALL
    )
    assert accuracy_section is not None
    assert "Accuracy Savings" not in accuracy_section.group(0)
    assert "Accuracy Salary" not in accuracy_section.group(0)
    assert "Accuracy Food" in accuracy_section.group(0)


def test_the_f92_sections_render_in_polish(reports: OwnerApp) -> None:
    food = _category(reports, "Polska Food")
    _tx(reports, food, date(2026, 1, 10), 10_000)
    _budget_line(reports, food, date(2026, 1, 1), 5_000)
    reports.client.headers["Accept-Language"] = "pl"
    html = _page(reports, "from=2026&to=2026&step=month&year=2026")
    assert "Przegląd roczny" in html
    assert "Trendy kategorii" in html
    assert "Rok do roku" in html
    assert "Dokładność planowania" in html
    assert "Okno trendów kategorii" in html
    assert "Średnia 3-miesięczna" in html
    assert (
        "Wydatki według kategorii i miesiąca. Mocniejsza czerwień oznacza większe wydatki "
        "w danym miesiącu." in html
    )
    assert "Zmiana dochodów" in html
    assert "Zmiana wydatków" in html
    assert "Zmiana oszczędności" in html
    assert "Przekroczenie %" in html
    assert "Wyczyść filtry" in html


def test_plan_accuracy_is_empty_without_plans(reports: OwnerApp) -> None:
    food = _category(reports, "No Plan Food")
    _tx(reports, food, date(2026, 1, 10), 10_000)
    html = _page(reports, "from=2026&to=2026&step=month")
    assert "No planned regular expenses in this range." in html
    assert "data-report-accuracy-score" not in html
    assert "data-report-accuracy-bar" not in html
