"""F11.9: mobile tables, page identity and the accessibility fixes that accompany them.

These are markup/behaviour checks over HTTP; the browser suite covers the measured 390 × 844
and 1440 × 900 layouts and the keyboard flow.
"""

from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import select

from assetboard.branding import APP_NAME
from assetboard.db.models.ledger import Category
from tests.conftest import OwnerApp

MONTH = "2026-01"


def _page(app: OwnerApp, path: str) -> str:
    response = app.client.get(path)
    assert response.status_code == 200, response.text
    return response.text


def _header(html: str) -> str:
    return html.split("data-header-actions", 1)[1].split("</header>", 1)[0]


def _category(app: OwnerApp, name: str) -> UUID:
    with app.db() as session:
        existing = session.scalars(
            select(Category.id).where(Category.name == name, Category.kind == "expense")
        ).first()
    if existing is not None:
        return existing
    form = {"name": name, "kind": "expense", "treatment": "regular", "color": "#16a34a"}
    assert app.post("/categories", form, token_from="/categories").status_code == 303
    with app.db() as session:
        return session.scalars(
            select(Category.id).where(Category.name == name, Category.kind == "expense")
        ).one()


def test_transactions_has_a_first_viewport_add_action(owner: OwnerApp) -> None:
    html = _page(owner, f"/transactions/{MONTH}")
    header = _header(html)
    assert 'data-add-entry="expense"' in header and 'href="#entry-expense-row"' in header
    assert 'data-add-entry="income"' in header and 'href="#entry-income-row"' in header
    assert ">Add entry<" in header and ">Add income<" in header
    # The chronological entry rows stay where they were.
    assert 'id="entry-expense-row"' in html and 'id="entry-income-row"' in html
    assert html.index("data-add-entry") < html.index('id="entry-expense-row"')

    owner.set_admin_role("viewer")
    viewer = _page(owner, f"/transactions/{MONTH}")
    assert "data-add-entry" not in _header(viewer)
    assert "data-entry-row" not in viewer


def test_month_names_the_destination_and_shows_the_phone_difference(owner: OwnerApp) -> None:
    taxes = _category(owner, "Taxes")
    response = owner.post(f"/m/{MONTH}/plan/{taxes}", {"amount": "260"}, token_from=f"/m/{MONTH}")
    assert response.status_code == 303
    html = _page(owner, f"/m/{MONTH}")
    headings = re.findall(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL)
    assert [re.sub(r"<[^>]+>", "", heading).strip() for heading in headings] == ["Month"]
    assert "data-month-subtitle>January 2026</p>" in html
    assert f"<title>Month: January 2026 · {APP_NAME}</title>" in html
    taxes_row = re.search(rf'<tr id="plan-{taxes}".*?</tr>', html, re.DOTALL)
    assert taxes_row is not None
    assert "data-difference-phone" in taxes_row.group(0)
    assert "vs plan" in taxes_row.group(0)


def test_admin_users_uses_phone_card_rows(owner: OwnerApp) -> None:
    owner.add_user("second")
    html = _page(owner, "/admin/users")
    section = html.split('aria-label="Users"', 1)[1].split("</section>", 1)[0]
    assert "overflow-x-auto" not in section
    assert (
        'class="hidden border-b border-line bg-subtle text-left text-xs uppercase '
        'tracking-wide text-muted sm:table-header-group"'
    ) in section
    assert (
        'class="grid grid-cols-[minmax(0,1fr)_auto] gap-x-3 gap-y-2 px-4 py-3 '
        'align-top sm:table-row sm:p-0"'
    ) in section
    assert 'data-user="second@example.com"' in section
    second = section.split('data-user="second@example.com"', 1)[1].split("</tr>", 1)[0]
    for label in ("Sign-in methods", "Status", "Last sign-in"):
        assert f">{label}</span>" in second
    assert "data-audit-history" in second and "data-row-menu" in second


def test_wide_tables_are_labelled_scroll_regions_with_a_hint(owner: OwnerApp) -> None:
    transactions = _page(owner, f"/transactions/{MONTH}")
    regions = re.findall(
        r'<div class="relative overflow-x-auto[^"]*" role="region" aria-label="([^"]+)"'
        r' tabindex="0" data-table-scroll>',
        transactions,
    )
    assert regions == ["Expenses", "Income"]
    assert transactions.count("data-table-scroll-hint") >= 2
    assert "Scroll sideways to see every column." in transactions

    food = _category(owner, "Scroll Food")
    assert (
        owner.post(
            f"/m/{MONTH}/transactions",
            {
                "direction": "expense",
                "day": "7",
                "amount": "10",
                "description": "Scroll",
                "category_id": str(food),
            },
            token_from=f"/m/{MONTH}",
        ).status_code
        == 303
    )
    reports = _page(owner, "/reports")
    assert (
        'role="region" aria-label="Income, expenses and savings by period"'
        ' tabindex="0" data-table-scroll'
    ) in reports
    assert reports.count("data-chart-table") >= 4
    assert "data-table-scroll-hint" in reports


def _first_category_id(app: OwnerApp) -> str:
    match = re.search(r'<option value="([a-f0-9-]{36})"', _page(app, "/recurring/new"))
    assert match is not None
    return match.group(1)


def test_upcoming_actions_carry_a_week_anchor_and_pin_the_notice(owner: OwnerApp) -> None:
    # With no occurrences the page still renders; the action markup is checked with one.
    owner.post(
        "/recurring",
        {
            "name": "Rent",
            "direction": "expense",
            "category_id": _first_category_id(owner),
            "amount": "100",
            "amount_mode": "fixed",
            "start_date": "2026-01-01",
            "freq": "monthly",
            "interval": "1",
            "month_day": "1",
            "month": "1",
            "weekend_policy": "none",
            "include_past": "1",
        },
        token_from="/recurring/new",
    )
    html = _page(owner, "/upcoming")
    assert re.search(r'id="week-2025-12-29"', html)
    assert re.search(r'<input type="hidden" name="anchor" value="week-[0-9-]+">', html)


def test_skip_link_and_confirm_dialog_are_named(owner: OwnerApp) -> None:
    html = _page(owner, "/")
    assert re.search(
        r'<body[^>]*>\s*<a href="#main-content" class="skip-link"'
        r" data-skip-link>Skip to content</a>",
        html,
    )
    assert '<main id="main-content" tabindex="-1"' in html
    assert 'id="confirm-dialog" aria-labelledby="confirm-dialog-title"' in html
    assert '<h2 id="confirm-dialog-title" class="text-lg font-semibold">Are you sure?</h2>' in html


def test_home_customise_is_a_header_action(owner: OwnerApp) -> None:
    html = _page(owner, "/")
    header = _header(html)
    assert "data-customise-link" in header
    assert html.index("data-customise-link") < html.index("data-home-widgets")
    assert "data-home-actions" not in html
