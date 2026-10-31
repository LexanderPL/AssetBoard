"""The month summary over HTTP (F2.4): actual and difference columns, totals rows, KPI cards,
SVG bars, the sort toggle (cookie), refreshes after planned-value and transaction writes.

Category names never collide with seeded ones ("Golden ..."), and rows are found by id."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.models.ledger import Category
from tests.conftest import OwnerApp

NOW = "2026-10-14T00:30:00+00:00"
MONTH = "/m/2026-10"


@pytest.fixture
def summary(owner: OwnerApp) -> OwnerApp:
    owner.app.state.clock = FixedClock(NOW)  # today: October 2026
    owner.clear_categories()  # these tests build their own categories (no seeded set)
    return owner


def _category(app: OwnerApp, name: str, kind: str = "expense", treatment: str = "regular") -> UUID:
    form = {"name": name, "kind": kind, "treatment": treatment, "color": "#16a34a"}
    assert app.post("/categories", form, token_from="/categories").status_code == 303
    with app.db() as session:
        return session.scalars(
            select(Category.id).where(Category.name == name, Category.kind == kind)
        ).one()


def _plan(app: OwnerApp, category: UUID, amount: str) -> None:
    response = app.post(f"{MONTH}/plan/{category}", {"amount": amount}, token_from=MONTH)
    assert response.status_code == 303


def _spend(app: OwnerApp, category: UUID, amount: str, direction: str = "expense") -> None:
    form = {
        "direction": direction,
        "day": "7",
        "amount": amount,
        "description": "",
        "category_id": str(category),
    }
    assert app.post(f"{MONTH}/transactions", form, token_from=MONTH).status_code == 303


def _htmx(app: OwnerApp, path: str, data: dict[str, str] | None = None) -> Any:
    headers = {"HX-Request": "true", "X-CSRF-Token": app.token(MONTH)}
    return app.client.post(path, data=data or {}, headers=headers, follow_redirects=False)


def _summary(html: str) -> str:
    match = re.search(r'<section id="month-summary".*?</section>', html, re.DOTALL)
    assert match is not None
    return match.group(0)


def _row(html: str, category: UUID) -> str:
    match = re.search(rf'<tr id="plan-{category}".*?</tr>', html, re.DOTALL)
    assert match is not None
    return match.group(0)


def _cells(row: str) -> tuple[str, str]:
    actual = re.search(r"data-actual>([^<]*)<", row)
    difference = re.search(r'data-difference><span class="[^"]*">([^<]*)<', row)
    assert actual is not None and difference is not None
    return actual.group(1), difference.group(1)


def _kpis(html: str) -> list[str]:
    return re.findall(r"data-kpi[^>]*>([^<]*)<", html)


def _golden(app: OwnerApp) -> tuple[UUID, UUID, UUID]:
    food, travel = _category(app, "Golden Food"), _category(app, "Golden Travel")
    salary = _category(app, "Golden Salary", "income")
    for category, amount in ((food, "550"), (travel, "5350"), (salary, "3000")):
        _plan(app, category, amount)
    _spend(app, food, "50")
    _spend(app, food, "500")
    _spend(app, travel, "4323")
    _spend(app, salary, "3400", "income")
    return food, travel, salary


def test_the_golden_example_on_the_page(summary: OwnerApp) -> None:
    food, travel, salary = _golden(summary)
    block = _summary(summary.client.get(MONTH).text)
    assert ">Summary</h2>" in block
    for heading in ("Plan", "Actual", "Difference"):
        assert f">{heading}</th>" in block
    # Plan and Actual only: pending amounts sit under Actual, there is no Forecast column.
    assert ">Planned</th>" not in block and ">Forecast</th>" not in block
    assert "Expected (pending)" not in block and "Forecast spent" not in block
    # Nothing is pending: no pending line, no pending flow bar, no confirmed-only twin.
    assert "data-pending" not in block.replace("data-pending-toggle", "")
    assert _cells(_row(block, food)) == ("€550.00", "€0.00")
    assert _cells(_row(block, travel)) == ("€4,323.00", "+€1,027.00")
    assert _cells(_row(block, salary)) == ("€3,400.00", "+€400.00")
    assert "text-success-strong" in _row(block, travel) and "(favourable)" in _row(block, travel)
    total = re.search(r'<tr id="summary-total-expense".*?</tr>', block, re.DOTALL)
    assert total is not None
    assert re.findall(r"tabular-nums\"[^>]*>([^<]*)<", total.group(0))[:2] == [
        "€5,900.00",
        "€4,873.00",
    ]
    assert "+€1,027.00" in total.group(0)
    assert _kpis(block) == [
        "€3,400.00",
        "€4,873.00",
        "€0.00",
        "-€1,473.00",
        "0.0%",
    ]  # saved 0 of 3 400 earned
    summary.client.headers["Accept-Language"] = "pl"
    polish = _summary(summary.client.get(MONTH).text)
    assert (
        "0,0%" in polish
        and "Zaoszczędzono / zainwestowano" in polish
        and ">Podsumowanie</h2>" in polish
        and ">Różnica</th>" in polish
    )


def test_bars_show_planned_and_actual_in_svg_with_the_same_text(summary: OwnerApp) -> None:
    _golden(summary)
    block = _summary(summary.client.get(MONTH).text)
    expense = re.search(r'data-summary-bar="expense".*?</svg>', block, re.DOTALL)
    income = re.search(r'data-summary-bar="income".*?</svg>', block, re.DOTALL)
    assert expense is not None and income is not None
    assert "Spent €4,873.00 of €5,900.00 planned" in expense.group(0)
    # 4 873 / 5 900 = 82.59 %: floor 82. Planned is the scale (100): its line is at the end.
    assert re.search(r'width="100"[^>]*class="fill-subtle stroke-line-strong"', expense.group(0))
    assert re.search(r'width="82"[^>]*class="fill-primary"', expense.group(0))
    assert re.search(r'x1="100"[^>]*class="stroke-ink"[^>]*data-bar-planned', expense.group(0))
    # Income over plan: actual is the scale, planned 3 000 / 3 400 = 88.2 %: floor 88.
    assert "Earned €3,400.00 of €3,000.00 planned" in income.group(0)
    assert "(over plan)" in income.group(0)
    assert re.search(r'width="100"[^>]*class="fill-success"', income.group(0))
    assert re.search(r'x1="88"[^>]*data-bar-planned', income.group(0))
    assert 'aria-hidden="true"' in expense.group(0)
    assert " rx=" not in block  # square ends: rounded corners would stretch with the viewBox


def test_nothing_earned_shows_no_savings_rate(summary: OwnerApp) -> None:
    _spend(summary, _category(summary, "Golden Rent"), "10")
    block = _summary(summary.client.get(MONTH).text)
    assert 'data-kpi="savings-rate"' not in block and "of income" not in block
    bars = re.findall(r'width="(\d+)"[^>]*data-bar-actual', block)
    assert bars == ["100", "0"]  # spent with no plan: actual is the scale; nothing earned
    assert "data-bar-planned" not in block  # nothing planned: no plan line
    assert "Spent €10.00 (no plan)" in block and "Earned €0.00 (no plan)" in block
    assert "(over plan)" not in block and "of €0.00 planned" not in block
    # Everything spent is overspend: the whole flow bar is hatched.
    assert re.search(r'x="0" y="0" width="1000"[^>]*data-flow-segment="overspent"', block)
    assert "Spending and saving exceed earnings by €10.00." in block


def _flow(block: str) -> dict[str, tuple[int, int]]:
    return {
        key: (int(x), int(width))
        for x, width, key in re.findall(
            r'<rect x="(\d+)" y="0" width="(\d+)"[^>]*data-flow-segment="([a-z]+)"', block
        )
    }


def test_the_flow_bar_splits_earnings(summary: OwnerApp) -> None:
    rent = _category(summary, "Golden Rent")
    fund = _category(summary, "Golden Fund", treatment="savings")
    salary = _category(summary, "Golden Salary", "income")
    _spend(summary, salary, "4000", "income")
    _spend(summary, rent, "2000")
    _spend(summary, fund, "1000")
    block = _summary(summary.client.get(MONTH).text)
    assert _flow(block) == {"spent": (0, 500), "saved": (500, 250), "left": (750, 250)}
    # The savings rate is saved / earned: 1 000 of 4 000 (what is left is not saved).
    assert _kpis(block) == ["€4,000.00", "€2,000.00", "€1,000.00", "€1,000.00", "25.0%"]
    assert re.search(r'data-savings-rate>Saved / invested <span[^>]*data-kpi="savings-rate">'
                     r"25.0%</span> of income</p>", block)  # fmt: skip
    assert (
        "Earned €4,000.00: spent €2,000.00, saved or invested €1,000.00, left €1,000.00." in block
    )
    assert "data-flow-earned" not in block and "exceed earnings" not in block
    assert ">Saved / invested</dt>" in block


def test_the_flow_bar_shows_overspending(summary: OwnerApp) -> None:
    _golden(summary)
    block = _summary(summary.client.get(MONTH).text)
    # Spent 4 873 of 3 400 earned: spending is the scale; earnings end at 697.
    assert _flow(block) == {"spent": (0, 1000), "overspent": (697, 303)}
    assert re.search(r'x1="697"[^>]*data-flow-earned', block)
    assert "Spending and saving exceed earnings by €1,473.00." in block
    net = re.search(r'<dd class="([^"]*)" data-kpi="net">', block)
    assert net is not None and "text-danger" in net.group(1)


def test_rows_above_plan_are_tinted(summary: OwnerApp) -> None:
    food, travel, salary = _golden(summary)
    over = _category(summary, "Golden Fuel")
    _spend(summary, over, "12")  # no plan: above plan
    block = _summary(summary.client.get(MONTH).text)
    assert 'class="bg-danger-tint" data-over-plan="expense"' in _row(block, over)
    assert 'class="bg-success-tint" data-over-plan="income"' in _row(block, salary)
    for under in (food, travel):  # at or under plan: no tint
        assert "data-over-plan" not in _row(block, under)


def test_actuals_without_a_line_show_planned_zero(summary: OwnerApp) -> None:
    unplanned, old = _category(summary, "Golden Fuel"), _category(summary, "Golden Old")
    moving = _category(summary, "Golden Move", treatment="transfer")
    _spend(summary, unplanned, "12")
    _spend(summary, old, "3")
    _plan(summary, moving, "0")  # nothing: transfers take no new line
    assert summary.post(f"/categories/{old}/archive", {}, token_from="/categories")
    block = _summary(summary.client.get(MONTH).text)
    row = _row(block, unplanned)
    assert 'value=""' in row and 'placeholder="0.00"' in row  # editable, empty
    assert _cells(row) == ("€12.00", "-€12.00")
    archived = _row(block, old)
    assert "data-plan-input" not in archived and re.search(r"data-planned>€0.00<", archived)
    assert f"plan-{moving}" not in block
    summary.set_admin_role("viewer")
    assert re.search(
        r"data-planned>€0.00<", _row(_summary(summary.client.get(MONTH).text), unplanned)
    )


def test_transfer_lines_are_plan_only(summary: OwnerApp) -> None:
    rent = _category(summary, "Golden Rent")
    _plan(summary, rent, "100")
    form = {"name": "Golden Rent", "kind": "expense", "treatment": "transfer", "color": "#16a34a"}
    assert summary.post(f"/categories/{rent}", form, token_from="/categories").status_code == 303
    _spend(summary, rent, "40")
    block = _summary(summary.client.get(MONTH).text)
    row = _row(block, rent)
    assert "not counted" in row and "data-actual" not in row
    assert 'id="summary-total-transfer"' not in block
    assert _kpis(block)[1] == "€0.00"  # the transfer is not spent


def test_the_sort_toggle_is_kept_in_a_cookie(summary: OwnerApp) -> None:
    first, second = _category(summary, "Golden A"), _category(summary, "Golden B")
    _spend(summary, second, "5")

    def order(html: str) -> list[str]:
        rows = re.findall(r'data-plan-row="([^"]+)"', _summary(html))
        return [r for r in rows if r in (str(first), str(second))]

    page = summary.client.get(MONTH).text
    assert order(page) == [str(second), str(first)]  # largest first by default
    assert re.search(r'value="actual" aria-pressed="true"', page)
    response = summary.post(f"{MONTH}/summary/sort", {"sort": "manual"}, token_from=MONTH)
    assert response.status_code == 303 and response.headers["location"].endswith("#month-summary")
    cookie = response.headers["set-cookie"]
    assert "summary_sort=manual" in cookie and "HttpOnly" in cookie and "Max-Age=31536000" in cookie
    page = summary.client.get(MONTH).text
    assert order(page) == [str(first), str(second)]
    assert re.search(r'value="manual" aria-pressed="true"', page)
    # After a transaction write (F2.7: from the Transactions page) the order is kept.
    form = {"direction": "expense", "day": "8", "amount": "1", "description": "",
            "category_id": str(first)}  # fmt: skip
    assert _htmx(summary, f"{MONTH}/transactions", form).status_code == 200
    assert order(summary.client.get(MONTH).text) == [str(first), str(second)]
    htmx = _htmx(summary, f"{MONTH}/summary/sort", {"sort": "actual"})
    assert htmx.status_code == 200 and "summary_sort=actual" in htmx.headers["set-cookie"]
    assert order(htmx.text) == [str(second), str(first)]
    assert "data-autofocus" not in htmx.text
    assert 'id="plan-status"' not in htmx.text  # a re-sort leaves the status line alone
    bogus = summary.post(f"{MONTH}/summary/sort", {"sort": "name"}, token_from=MONTH)
    assert "summary_sort=actual" in bogus.headers["set-cookie"]


def test_the_sort_toggle_needs_a_csrf_token(summary: OwnerApp) -> None:
    response = summary.client.post(f"{MONTH}/summary/sort", data={"sort": "manual"})
    assert response.status_code == 403


def test_a_planned_value_save_refreshes_totals_and_kpis_out_of_band(summary: OwnerApp) -> None:
    food = _category(summary, "Golden Food")
    _spend(summary, food, "100")
    html = _htmx(summary, f"{MONTH}/plan/{food}", {"amount": "150"}).text
    assert html.lstrip().startswith(f'<tr id="plan-{food}"')
    assert _cells(_row(html, food)) == ("€100.00", "+€50.00")
    total = re.search(r'<tr id="summary-total-expense"[^>]*hx-swap-oob="outerHTML"', html)
    assert total is not None
    assert re.search(r'<div id="summary-kpis"[^>]*hx-swap-oob="outerHTML"', html)
    assert "Spent €100.00 of €150.00 planned" in html


def test_a_transaction_write_refreshes_actuals_and_kpis(summary: OwnerApp) -> None:
    food = _category(summary, "Golden Food")
    _plan(summary, food, "50")
    form = {"direction": "expense", "day": "7", "amount": "80", "description": "",
            "category_id": str(food)}  # fmt: skip
    # F2.7: the write answers the Transactions page (no summary); the Month page shows it.
    assert 'id="month-summary"' not in _htmx(summary, f"{MONTH}/transactions", form).text
    block = _summary(summary.client.get(MONTH).text)
    assert _cells(_row(block, food)) == ("€80.00", "-€30.00")
    assert "text-danger" in _row(block, food) and "(unfavourable)" in _row(block, food)
    assert _kpis(block)[1] == "€80.00"
