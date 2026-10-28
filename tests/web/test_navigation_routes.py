"""F2.7 navigation restructure: Home, Month, Transactions and History, the redirects from the
old addresses and the sidebar."""

from __future__ import annotations

import json
import re
from uuid import UUID

import pytest
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.models.ledger import Category
from assetboard.db.models.scheduled import ScheduledOccurrence
from tests.conftest import OwnerApp

NOW = "2026-03-10T12:00:00+00:00"
MONTH = "/m/2026-03"
PAGE = "/transactions/2026-03"


@pytest.fixture
def nav(owner: OwnerApp) -> OwnerApp:
    clock = FixedClock(NOW)
    owner.app.state.clock = clock
    owner.app.state.session_factory.configure(info={"clock": clock})
    owner.clear_categories()
    return owner


def _category(app: OwnerApp, name: str, kind: str = "expense", treatment: str = "regular",
              color: str = "#16a34a") -> UUID:  # fmt: skip
    form = {"name": name, "kind": kind, "treatment": treatment, "color": color}
    assert app.post("/categories", form, token_from="/categories").status_code == 303
    with app.db() as session:
        return session.scalars(select(Category.id).where(Category.name == name)).one()


def _add(app: OwnerApp, category: UUID, amount: str, direction: str = "expense") -> None:
    form = {"direction": direction, "day": "5", "amount": amount, "description": "x",
            "category_id": str(category)}  # fmt: skip
    assert app.post(f"{MONTH}/transactions", form, token_from=MONTH).status_code == 303


def _chart(html: str, key: str) -> dict:
    match = re.search(
        rf'<script type="application/json" id="chart-{key}-data">(.*?)</script>', html, re.DOTALL
    )
    assert match is not None
    return json.loads(match.group(1))


def _recurring(app: OwnerApp, category: UUID, name: str = "Rent") -> None:
    form = {"name": name, "direction": "expense", "category_id": str(category),
            "amount": "100", "amount_mode": "estimate", "start_date": "2026-01-01",
            "freq": "monthly", "interval": "1", "month_day": "1", "month": "1",
            "weekend_policy": "none", "include_past": "1"}  # fmt: skip
    assert app.post("/recurring", form, token_from="/recurring/new").status_code == 303


# Home


def test_home_without_data_renders_empty_states(nav: OwnerApp) -> None:
    html = nav.client.get("/").text
    assert "<h1" in html and "March 2026" in html
    assert "data-open-month" not in html and "Open month" not in html
    assert "data-summary-kpis" in html and 'data-flow-segment="empty"' in html
    assert "No expenses yet this month" in html and "No income yet this month" in html
    assert 'type="application/json"' not in html  # no chart without slices
    assert "Nothing is overdue or due in the next 7 days." in html
    assert 'data-home-link aria-current="page"' in html


def test_home_charts_match_the_summary_and_have_tables(nav: OwnerApp) -> None:
    food = _category(nav, "Food", color="#ff0000")
    rent = _category(nav, "Rent")
    savings = _category(nav, "Broker", treatment="savings")
    salary = _category(nav, "Salary", "income", color="#0000ff")
    _add(nav, food, "25")
    _add(nav, rent, "75")
    _add(nav, savings, "500")
    _add(nav, salary, "2000", "income")
    html = nav.client.get("/").text
    expense = _chart(html, "expense")
    assert expense["currency"] == "EUR" and expense["total"] == "€100.00"
    assert [(s["name"], s["value"], s["amount"], s["share"], s["color"])
            for s in expense["slices"]] == [
        ("Rent", 7500, "€75.00", "75.0%", "#16a34a"),
        ("Food", 2500, "€25.00", "25.0%", "#ff0000"),
    ]  # fmt: skip
    assert [s["name"] for s in _chart(html, "income")["slices"]] == ["Salary"]
    assert "Broker" not in json.dumps(expense)  # no "Saved / invested" slice
    # The canvas is hidden from assistive technology; the table carries the same numbers.
    assert re.search(r"hidden aria-hidden=\"true\" data-chart-canvas", html)
    table = re.search(r'<section [^>]*data-donut="expense".*?</section>', html, re.DOTALL)
    assert table is not None and "data-chart-table" in table.group(0)
    assert re.findall(r"<td[^>]*>([^<]+)</td>", table.group(0)) == [
        "€75.00", "75.0%", "€25.00", "25.0%",
    ]  # fmt: skip
    assert "js/charts.js" in html and "vendor/echarts.min.js" in html


def test_home_upcoming_items_for_editors_and_viewers(nav: OwnerApp) -> None:
    rent = _category(nav, "Rent")
    _recurring(nav, rent)
    html = nav.client.get("/").text
    items = re.findall(r"<li [^>]*data-occurrence=\"([0-9a-f-]+)\"([^>]*)>", html)
    assert items and len(items) <= 10
    assert any("data-overdue" in attrs for _, attrs in items)  # January to March, overdue
    assert "data-home-post" in html and "data-home-skip" in html
    assert 'href="/upcoming"' in html
    occurrence = UUID(items[0][0])
    skipped = nav.post(f"/upcoming/{occurrence}/skip", {"back": "home"}, token_from="/")
    assert skipped.status_code == 303 and skipped.headers["location"] == "/"
    with nav.db() as session:
        assert session.get(ScheduledOccurrence, occurrence).status == "skipped"  # type: ignore[union-attr]
    # Anything else than "home" goes back to the Upcoming page.
    other = UUID(items[1][0])
    elsewhere = nav.post(f"/upcoming/{other}/skip", {"back": "https://evil.example"},
                         token_from="/")  # fmt: skip
    assert elsewhere.headers["location"] == "/upcoming"
    nav.set_admin_role("viewer")
    viewer = nav.client.get("/").text
    assert "data-occurrence=" in viewer
    assert "data-home-post" not in viewer and "data-home-skip" not in viewer


def test_home_upcoming_list_is_capped_at_ten(nav: OwnerApp) -> None:
    rent = _category(nav, "Rent")
    for name in ("Rent", "Gym", "Phone", "Water", "Power", "Gas"):
        _recurring(nav, rent, name)  # three occurrences each (January to March)
    html = nav.client.get("/").text
    assert len(re.findall(r"<li [^>]*data-occurrence=", html)) == 10
    assert "More items are waiting on the Upcoming page." in html


# Month and Transactions


def test_the_month_page_is_a_summary_with_both_donuts(nav: OwnerApp) -> None:
    food = _category(nav, "Food")
    salary = _category(nav, "Salary", kind="income")
    _add(nav, food, "25")
    _add(nav, salary, "900", direction="income")
    html = nav.client.get(MONTH).text
    assert 'id="month-summary"' in html and "data-summary-kpis" in html
    assert _chart(html, "expense")["slices"][0]["name"] == "Food"
    assert _chart(html, "income")["slices"][0]["name"] == "Salary"
    # Expense first, then income, as on Home.
    assert html.index('data-donut="expense"') < html.index('data-donut="income"')
    assert 'id="ledger"' not in html and "data-entry-form" not in html
    assert "data-enter-transactions" not in html and "Enter transactions" not in html
    assert f'href="{PAGE}?category={food}"' in html  # category names open the filtered list
    assert 'data-month-link aria-current="page"' in html


def test_the_transactions_page_is_for_entry_only(nav: OwnerApp) -> None:
    _category(nav, "Food")
    html = nav.client.get(PAGE).text
    assert 'id="ledger"' in html and "data-entry-form" in html
    assert "data-summary-kpis" not in html and 'id="month-summary"' not in html
    assert 'type="application/json"' not in html and "echarts" not in html
    assert 'data-transactions-link aria-current="page"' in html
    assert "data-month-picker-toggle" in html and 'type="month"' not in html
    assert 'href="/transactions/2026-02"' in html and 'href="/transactions/2026-04"' in html
    assert "Month summary" not in html


def test_the_transactions_month_picker(nav: OwnerApp) -> None:
    def location(query: str) -> str:
        response = nav.client.get(f"/transactions{query}", follow_redirects=False)
        assert response.status_code == 303
        return response.headers["location"]

    assert location("") == PAGE
    assert location("?month=2025-12&from=2026-03") == "/transactions/2025-12"
    assert location("?month=12.2025") == "/transactions/2025-12"
    assert location("?month=bogus&from=2026-01") == "/transactions/2026-01?error=invalid_month"
    assert nav.client.get("/transactions/2026-13").status_code == 404


def test_history_keeps_the_f26_page(nav: OwnerApp) -> None:
    html = nav.client.get("/history").text
    assert "<title>History · " in html and "data-export-open" in html
    assert (
        'action="/history/export.csv"' in html and 'data-history-link aria-current="page"' in html
    )
    nav.client.headers["Accept-Language"] = "pl"
    assert ">Historia</a>" in nav.client.get("/history").text


# Redirects


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("?q=lidl&sort=-amount&page=2", "/history?q=lidl&sort=-amount&page=2"),
        ("?category=a&category=b&min=1", "/history?category=a&category=b&min=1"),
        ("?from=2026-01-01&to=2026-02-01&direction=expense&max=9&export=1",
         "/history?from=2026-01-01&to=2026-02-01&direction=expense&max=9&export=1"),
        ("?q=x&next=https://evil.example&host=evil", "/history?q=x"),  # unknown ones dropped
    ],
)  # fmt: skip
def test_filtered_transactions_links_go_to_history(
    nav: OwnerApp, query: str, expected: str
) -> None:
    response = nav.client.get(f"/transactions{query}", follow_redirects=False)
    assert response.status_code == 302 and response.headers["location"] == expected


def test_the_export_moved_permanently(nav: OwnerApp) -> None:
    response = nav.client.get(
        "/transactions/export.csv?q=a&delimiter=semicolon&bom=1&evil=1", follow_redirects=False
    )
    assert response.status_code == 301
    assert response.headers["location"] == "/history/export.csv?q=a&delimiter=semicolon&bom=1"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("/transactions/recategorize", "/history/recategorize"),
        ("/transactions/recategorize/confirm", "/history/recategorize/confirm"),
    ],
)
def test_bulk_recategorize_posts_keep_method_and_body(nav: OwnerApp, old: str, new: str) -> None:
    response = nav.post(old, {"target": "x"}, token_from="/history")
    assert response.status_code == 308 and response.headers["location"] == new


def test_month_category_links_go_to_the_transactions_page(nav: OwnerApp) -> None:
    food = _category(nav, "Food")
    response = nav.client.get(f"{MONTH}?category={food}&edit=x", follow_redirects=False)
    assert response.status_code == 302 and response.headers["location"] == f"{PAGE}?category={food}"
    bogus = nav.client.get(f"{MONTH}?category=https://evil.example", follow_redirects=False)
    assert bogus.status_code == 302 and bogus.headers["location"] == PAGE
    assert nav.client.get(MONTH, follow_redirects=False).status_code == 200


def test_redirects_ignore_a_crafted_host(nav: OwnerApp) -> None:
    headers = {"Host": "evil.example", "X-Forwarded-Host": "evil.example"}
    for path in (
        "/transactions?q=a",
        "/transactions/export.csv",
        "/transactions",
        f"{MONTH}?category=",
    ):
        response = nav.client.get(path, headers=headers, follow_redirects=False)
        assert response.status_code in (301, 302, 303), path
        assert response.headers["location"].startswith("/"), path
        assert "evil" not in response.headers["location"], path


def test_redirects_keep_the_root_path(nav: OwnerApp) -> None:
    nav.client.app.root_path = "/money"  # type: ignore[attr-defined]
    try:
        response = nav.client.get("/transactions?q=a", follow_redirects=False)
        assert response.headers["location"] == "/money/history?q=a"
        response = nav.client.get("/transactions", follow_redirects=False)
        assert response.headers["location"] == "/money/transactions/2026-03"
    finally:
        nav.client.app.root_path = ""  # type: ignore[attr-defined]


# Sidebar


def test_the_sidebar_order_and_current_page(nav: OwnerApp) -> None:
    html = nav.client.get("/").text
    sidebar = re.search(r"<nav [^>]*aria-label=\"Main navigation\".*?</nav>", html, re.DOTALL)
    assert sidebar is not None
    order = re.findall(
        r"data-(home|month|transactions|history|recurring|upcoming)-link", sidebar.group(0)
    )
    assert order == ["home", "month", "transactions", "history", "recurring", "upcoming"]
    for path, current in (("/", "home"), (MONTH, "month"), (PAGE, "transactions"),
                          ("/history", "history")):  # fmt: skip
        page = nav.client.get(path).text
        marked = re.findall(r'data-(\w+)-link aria-current="page"', page)
        assert marked == [current], path
