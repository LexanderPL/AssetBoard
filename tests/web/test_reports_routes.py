"""The Reports page over HTTP (F10.1/F10.2): default range, filters, charts/tables, notes and
access."""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select

from assetboard.clock import FixedClock
from assetboard.db.models.ledger import BudgetLine, Category, Transaction
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


def _csv_response(app: OwnerApp, query: str = ""):
    return app.client.get(
        "/reports/year-overview.csv" + (f"?{query}" if query else ""), follow_redirects=False
    )


def _csv_rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


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
    assert f'<option value="{old}">Archived Food (archived)</option>' in html
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
    assert f'<option value="{archived}">Duplicate Name (archived)</option>' in html
    assert f'<option value="{active}">Duplicate Name</option>' in html


def test_a_selected_archived_category_without_spending_stays_in_the_filter(
    reports: OwnerApp,
) -> None:
    archived = _category(reports, "Old Filter", archived=True)
    _tx(reports, archived, date(2025, 1, 10), 10_000)
    html = _page(
        reports,
        f"from=2026&to=2026&step=month&category={archived}",
    )
    assert f'<option value="{archived}" selected>Old Filter (archived)</option>' in html
    assert "No spending for the selected categories." in html


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
    assert "data-report-grid-year>2026<" in html
    assert "data-report-grid-so-far>so far<" in html
    assert "data-report-year-csv" in html
    assert "/reports/year-overview.csv" in html
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
    assert "heat-4" in re.search(
        r'<td class="([^"]*)" data-report-grid-cell>€900\.00</td>', html
    ).group(1)  # type: ignore[union-attr]
    assert "heat-1" in re.search(
        r'<td class="([^"]*)" data-report-grid-cell>€120\.00</td>', html
    ).group(1)  # type: ignore[union-attr]
    zero_classes = re.findall(r'<td class="([^"]*)" data-report-grid-cell>€0\.00</td>', food)
    assert zero_classes and all("heat-" not in value for value in zero_classes)
    assert "Grid Savings" not in html and "Grid Transfer" not in html

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


def test_the_year_overview_csv_matches_the_grid(reports: OwnerApp) -> None:
    categories = _seed_grid(reports)
    response = _csv_response(reports, "from=2026&to=2026&step=month&year=2026")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == (
        'attachment; filename="reports-year-overview.csv"'
    )
    assert response.headers["cache-control"] == "no-store"
    rows = _csv_rows(response.content.decode("utf-8"))
    assert rows[0][:3] == ["category_id", "category", "archived"]
    assert rows[0][3] == "2026-01" and rows[0][-2] == "2026-12" and rows[0][-1] == "total"
    food = next(row for row in rows if row[1] == "Grid Food")
    assert food[3:5] == ["120.00", "130.00"]
    assert food[-1] == "250.00"
    total = rows[-1]
    assert total[:3] == ["", "Total", "false"]
    assert total[3] == "1020.00" and total[-1] == "1150.00"

    filtered = _csv_response(
        reports,
        f"from=2026&to=2026&step=month&year=2026&category={categories['food']}",
    )
    filtered_rows = _csv_rows(filtered.content.decode("utf-8"))
    assert [row[1] for row in filtered_rows[1:-1]] == ["Grid Food"]
    assert filtered_rows[-1][-1] == "250.00"


def test_the_year_overview_csv_escapes_names_and_requires_sign_in(reports: OwnerApp) -> None:
    tricky = _category(reports, 'CSV, "Tricky"')
    _tx(reports, tricky, date(2026, 3, 4), 1_000)
    response = _csv_response(reports, "from=2026&to=2026&step=month&year=2026")
    rows = _csv_rows(response.content.decode("utf-8"))
    assert any(row[1] == 'CSV, "Tricky"' for row in rows[1:-1])

    bad = _csv_response(reports, "from=2026&to=2025")
    assert bad.status_code == 400
    assert "Fix the filters to export the year overview." in bad.text

    reports.client.cookies.clear()
    anonymous = _csv_response(reports, "from=2026&to=2026&step=month&year=2026")
    assert anonymous.status_code == 303
    assert anonymous.headers["location"].startswith("/login")


def test_a_viewer_can_export_the_year_overview_csv(reports: OwnerApp) -> None:
    _seed_grid(reports)
    user_id = reports.add_user("vic")
    reports.join(user_id, reports.home_id, "viewer")
    reports.client.cookies.clear()
    assert (
        reports.web.sign_in(
            reports.client, email="vic@example.com", password=reports.web.admin_password
        ).status_code
        == 303
    )
    response = _csv_response(reports, "from=2026&to=2026&step=month&year=2026")
    assert response.status_code == 200


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


def test_year_over_year_compares_a_category_or_the_total(reports: OwnerApp) -> None:
    food = _category(reports, "YOY Food")
    rent = _category(reports, "YOY Rent")
    _tx(reports, food, date(2025, 1, 10), 10_000)
    _tx(reports, food, date(2026, 1, 10), 15_000)
    _tx(reports, rent, date(2025, 1, 11), 5_000)
    _tx(reports, rent, date(2026, 1, 11), 2_000)

    html = _page(reports, f"from=2025&to=2026&step=month&year=2026&yoy_category={food}")
    assert "data-report-year-over-year" in html
    assert "data-report-yoy-partial" in html
    row = re.search(r"<tr data-report-yoy-row>.*?</tr>", html, re.DOTALL)
    assert row is not None
    assert _cells(row.group(0)) == ["€150.00", "€100.00", "€50.00", "50.0%"]
    assert "text-danger" in re.search(r'<td class="([^"]*)" data-report-yoy-change>', html).group(1)  # type: ignore[union-attr]
    assert "YOY Food" in row.group(0)

    total = _page(reports, "from=2025&to=2026&step=month&year=2026")
    total_row = re.search(r"<tr data-report-yoy-row>.*?</tr>", total, re.DOTALL)
    assert total_row is not None
    assert "Total (all categories)" in total_row.group(0)
    assert _cells(total_row.group(0))[3] == "13.3%"

    ignored = _page(reports, f"from=2025&to=2026&step=month&year=2026&yoy_category={uuid4()}")
    assert "Total (all categories)" in ignored

    no_previous = _page(reports, f"from=2025&to=2026&step=month&year=2025&yoy_category={rent}")
    no_previous_row = re.search(r"<tr data-report-yoy-row>.*?</tr>", no_previous, re.DOTALL)
    assert no_previous_row is not None
    assert "Not available: nothing spent in the previous year." in no_previous_row.group(0)
    assert '<option value="2025" selected>' in no_previous


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
    assert ">Planned vs actual accuracy</h2>" in html
    assert "data-report-accuracy-row" in html
    assert _cells(_accuracy_row(html, "Accuracy Food")) == ["3", "1", "33.3%"]
    assert _cells(_accuracy_row(html, "Accuracy Rent")) == ["1", "0", "0.0%"]
    total = re.search(r"<tr [^>]*data-report-accuracy-total.*?</tr>", html, re.DOTALL)
    assert total is not None
    assert "All categories" in total.group(0)
    assert _cells(total.group(0)) == ["4", "1", "25.0%"]
    assert "A month counts as over when actual spending is greater than the plan." in html
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
    assert "Dokładność planu i rzeczywistości" in html
    assert "Okno: 3 mies." in html
    assert "Zmiana %" in html
    assert "Przekroczenie %" in html


def test_plan_accuracy_is_empty_without_plans(reports: OwnerApp) -> None:
    food = _category(reports, "No Plan Food")
    _tx(reports, food, date(2026, 1, 10), 10_000)
    html = _page(reports, "from=2026&to=2026&step=month")
    assert "No planned regular expenses in this range." in html
