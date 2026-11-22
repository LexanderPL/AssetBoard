"""Read-only income source and tax rule rows on the Recurring page (owner decision 2026-11-22).

The rows are mixed into the existing Expenses and Income tables; configuration stays on the
Income and Taxes pages, so these rows have no actions of their own.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.models.income import IncomeSource
from assetboard.db.models.ledger import Category
from assetboard.db.models.scheduled import ScheduledItem
from assetboard.db.models.tax import TaxRule
from tests.web.test_loan_integrations_routes import _create as create_loan
from tests.web.test_loan_integrations_routes import _expense_category as loan_expense_category
from tests.web.test_scheduled_routes import form


def _enable_taxes(owner, on: bool = True):
    return owner.post("/workspace/settings/menu", {"taxes_enabled": "1" if on else "0"})


def _category(owner, kind: str, name: str | None = None) -> tuple[str, str]:
    with owner.db() as session:
        query = select(Category).where(
            Category.workspace_id == owner.home_id,
            Category.kind == kind,
            Category.treatment != "transfer",
            Category.archived_at.is_(None),
        )
        if name is not None:
            query = query.where(Category.name == name)
        category = session.scalars(query).first()
        assert category is not None
        return str(category.id), category.name


def _income(
    owner,
    name: str,
    category_id: str,
    *,
    kind: str = "fixed",
    amount: str = "8,500.00",
    pay_day: str = "10",
    lag: str = "0",
) -> str:
    data = {
        "name": name,
        "kind": kind,
        "category_id": category_id,
        "fixed_amount": amount if kind == "fixed" else "",
        "rate_amount": amount if kind == "variable" else "",
        "unit_choice": "day",
        "expected_mode": "manual",
        "expected_units": "",
        "average_months": "3",
        "pay_day": pay_day,
        "pay_lag_months": lag,
        "active_from_month": "1",
        "active_from_year": "2026",
        "active_to_month": "",
        "active_to_year": "",
    }
    response = owner.post("/income", data, token_from="/income")
    assert response.status_code == 303, response.text
    with owner.db() as session:
        return str(session.scalars(select(IncomeSource.id).where(IncomeSource.name == name)).one())


def _tax(owner, name: str, category_id: str, *, rate: str = "23", day: str = "25") -> str:
    data = {
        "name": name,
        "kind": "added",
        "mode": "percent",
        "rate_percent": rate,
        "fixed_amount": "",
        "payment_day": day,
        "payment_timing": "next_month",
        "payment_category_id": category_id,
    }
    assert owner.post("/taxes", data).status_code == 303
    with owner.db() as session:
        return str(session.scalars(select(TaxRule.id).where(TaxRule.name == name)).one())


def _attach(owner, source_id: str, rule_id: str, due_day: str) -> None:
    response = owner.post(
        f"/income/{source_id}/taxes",
        {"tax_rule_id": rule_id, f"due_day_{rule_id}": due_day},
        token_from="/income",
    )
    assert response.status_code == 303 and "error" not in response.headers["location"]


def _row(page: str, marker: str, key: UUID | str) -> str:
    """The HTML of the table row carrying ``marker="key"``."""
    index = page.index(f'{marker}="{key}"')
    start = page.rfind("<tr", 0, index)
    return page[start : page.index("</tr>", index) + len("</tr>")]


def _section(page: str, direction: str) -> str:
    return page.split(f'data-recurring-section="{direction}"', 1)[1].split("</section>", 1)[0]


def test_income_sources_are_read_only_rows_in_the_income_table(owner):
    owner.app.state.clock = FixedClock("2026-03-01T12:00:00+00:00")
    salary_category_id, salary_category = _category(owner, "income", "Salary")
    contract_category_id, contract_category = _category(owner, "income", "Sale of items")
    salary_id = _income(
        owner, "Salary", salary_category_id, kind="fixed", amount="8,500.00", pay_day="20"
    )
    contract_id = _income(
        owner,
        "Contract work",
        contract_category_id,
        kind="variable",
        amount="1,230.00",
        pay_day="10",
        lag="1",
    )
    # A recurring income item is listed before the read-only source rows.
    item = form(
        owner,
        name="Freelance retainer",
        direction="income",
        category_id=salary_category_id,
    )
    assert owner.post("/recurring", item).status_code == 303
    with owner.db() as session:
        scheduled_id = session.scalars(
            select(ScheduledItem.id).where(ScheduledItem.name == "Freelance retainer")
        ).one()

    page = owner.client.get("/recurring").text
    income = _section(page, "income")
    assert income.index(f'data-scheduled-item="{scheduled_id}"') < income.index(
        f'data-income-source-row="{salary_id}"'
    )

    salary = _row(income, "data-income-source-row", salary_id)
    assert f'href="/income#income-source-{salary_id}"' in salary
    assert "€8,500.00" in salary and "data-estimate" not in salary
    assert "paid on the 20th of the same month" in salary
    assert '<time datetime="2026-03-20">Mar 20, 2026</time>' in salary
    assert salary_category in salary and "<svg" in salary
    # Read-only: no Edit, Pause or Delete.
    assert "<form" not in salary
    for action in ("data-edit", "data-pause", "data-delete"):
        assert action not in salary

    contract = _row(income, "data-income-source-row", contract_id)
    assert f'href="/income#income-source-{contract_id}"' in contract
    assert "€1,230.00 per day" in contract and "data-estimate" in contract
    assert "paid on the 10th of the next month" in contract
    assert '<time datetime="2026-03-10">Mar 10, 2026</time>' in contract
    assert contract_category in contract and "<svg" in contract
    assert "<form" not in contract


def test_tax_rules_are_read_only_rows_in_the_expenses_table(owner):
    owner.app.state.clock = FixedClock("2026-03-01T12:00:00+00:00")
    _enable_taxes(owner)
    income_category_id, _ = _category(owner, "income", "Salary")
    expense_category_id, expense_category = _category(owner, "expense", "Taxes")
    rule_id = _tax(owner, "VAT", expense_category_id, rate="23", day="25")
    salary_id = _income(owner, "Salary", income_category_id, kind="fixed", pay_day="10")
    contract_id = _income(owner, "Contract work", income_category_id, kind="fixed", pay_day="10")
    # Two sources with different payment-day overrides; the earliest one wins.
    _attach(owner, salary_id, rule_id, due_day="15")
    _attach(owner, contract_id, rule_id, due_day="5")

    page = owner.client.get("/recurring").text
    expenses = _section(page, "expense")
    row = _row(expenses, "data-tax-rule-row", rule_id)
    assert f'href="/taxes#tax-{rule_id}"' in row
    assert "23%" in row and "data-from-income" in row and "From income" in row
    assert "Paid on the 25th of the month after you are paid" in row
    # The payment on the 5th (20 April) is earlier than the one on the 15th.
    assert '<time datetime="2026-04-05">Apr 5, 2026</time>' in row
    assert "2026-04-15" not in row
    assert expense_category in row and "<svg" in row
    # One row per rule, with no actions.
    assert expenses.count(f'data-tax-rule-row="{rule_id}"') == 1
    assert "<form" not in row
    for action in ("data-edit", "data-pause", "data-delete"):
        assert action not in row


def test_taxes_switched_off_hides_tax_rule_rows(owner):
    _enable_taxes(owner)
    expense_category_id, _ = _category(owner, "expense", "Taxes")
    rule_id = _tax(owner, "VAT", expense_category_id)
    page = owner.client.get("/recurring").text
    assert f'data-tax-rule-row="{rule_id}"' in page
    # The rule row alone is enough: the page is not empty and the section is shown.
    assert "data-recurring-empty" not in page
    assert "<form" not in _row(page, "data-tax-rule-row", rule_id)

    assert _enable_taxes(owner, on=False).status_code == 303
    assert "data-tax-rule-row" not in owner.client.get("/recurring").text


def test_archived_sources_and_rules_are_hidden(owner):
    owner.app.state.clock = FixedClock("2026-03-01T12:00:00+00:00")
    _enable_taxes(owner)
    income_category_id, _ = _category(owner, "income", "Salary")
    expense_category_id, _ = _category(owner, "expense", "Taxes")
    rule_id = _tax(owner, "VAT", expense_category_id)
    source_id = _income(owner, "Salary", income_category_id, kind="fixed")
    _attach(owner, source_id, rule_id, due_day="5")
    page = owner.client.get("/recurring").text
    assert f'data-income-source-row="{source_id}"' in page
    assert f'data-tax-rule-row="{rule_id}"' in page

    assert owner.post(f"/income/{source_id}/archive").status_code == 303
    page = owner.client.get("/recurring").text
    assert f'data-income-source-row="{source_id}"' not in page
    # No active source pays the tax, so the rule has no next date.
    assert '<span aria-label="No next date">—</span>' in _row(page, "data-tax-rule-row", rule_id)

    assert owner.post(f"/taxes/{rule_id}/archive").status_code == 303
    assert "data-tax-rule-row" not in owner.client.get("/recurring").text


def test_income_source_rows_keep_the_page_out_of_the_empty_state(owner):
    owner.app.state.clock = FixedClock("2026-03-01T12:00:00+00:00")
    income_category_id, _ = _category(owner, "income", "Salary")
    source_id = _income(owner, "Salary", income_category_id, kind="fixed")

    page = owner.client.get("/recurring").text
    assert "data-recurring-empty" not in page and "No recurring items yet." not in page
    assert f'data-income-source-row="{source_id}"' in page
    # The create button is still there and its form still opens.
    assert "data-new-recurring" in page
    assert owner.client.get("/recurring/new").status_code == 200


def test_from_loan_rows_link_to_the_loan_detail(owner):
    loan_id = create_loan(owner)
    category_id = loan_expense_category(owner)
    assert owner.post(f"/loans/{loan_id}/scheduled-item", {"category_id": category_id}).is_redirect
    with owner.db() as session:
        item_id = session.scalars(
            select(ScheduledItem.id).where(ScheduledItem.loan_id == UUID(loan_id))
        ).one()

    page = owner.client.get("/recurring").text
    row = _row(page, "data-scheduled-item", item_id)
    assert f'href="/loans/{loan_id}"' in row
    assert "data-from-loan-link" in row and "data-from-loan" in row
    # The loan link is inside the amount cell, before the estimate chip.
    assert row.index("data-from-loan-link") < row.index("data-from-loan>")
