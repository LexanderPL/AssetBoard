"""The Income page cards, the "Add income" steps and the edit page (taxes and income redesign,
PR B)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update

from assetboard.db.models.income import IncomeSource, IncomeSourceTaxRule
from assetboard.db.models.ledger import Category
from assetboard.db.models.tax import TaxRule


def _category(owner, kind: str) -> str:
    with owner.db() as session:
        return str(
            session.scalars(
                select(Category.id).where(
                    Category.workspace_id == owner.home_id,
                    Category.kind == kind,
                    Category.treatment != "transfer",
                    Category.archived_at.is_(None),
                )
            ).first()
        )


def _enable_taxes(owner) -> None:
    assert owner.post("/workspace/settings/menu", {"taxes_enabled": "1"}).status_code == 303


def _tax(owner, name: str, kind: str, rate: str, day: str = "25") -> str:
    data = {
        "name": name,
        "kind": kind,
        "mode": "percent",
        "rate_percent": rate,
        "fixed_amount": "",
        "payment_day": day,
        "payment_timing": "next_month",
        "payment_category_id": _category(owner, "expense"),
    }
    assert owner.post("/taxes", data).status_code == 303
    with owner.db() as session:
        return str(session.scalars(select(TaxRule.id).where(TaxRule.name == name)).one())


def _step(owner, data: dict[str, Any]) -> Any:
    form = {"csrf_token": owner.token("/income/new")} | data
    return owner.client.post("/income/new", data=form, follow_redirects=False)


def _fixed(owner, name: str = "Salary", **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "category_id": _category(owner, "income"),
        "kind": "fixed",
        "fixed_amount": "8,500.00",
        "pay_day": "10",
        "pay_lag_months": "0",
        "active_from_month": "1",
        "active_from_year": "2026",
    } | extra


def _variable(owner, name: str = "Contract work", **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "category_id": _category(owner, "income"),
        "kind": "variable",
        "rate_amount": "1,200.00",
        "unit_choice": "day",
        "expected_mode": "fixed",
        "expected_units": "20",
        "average_months": "3",
        "pay_day": "10",
        "pay_lag_months": "1",
        "active_from_month": "1",
        "active_from_year": "2026",
    } | extra


def _save(owner, values: dict[str, Any], step: int) -> Any:
    return _step(owner, values | {"step": str(step), "go": "next"})


def _card(page: str, name: str) -> str:
    """The HTML of the card whose heading is ``name``."""
    start = page.index(f">{name}</h2>")
    end = page.find("data-income-source=", start)
    return page[start : end if end != -1 else len(page)]


def test_wizard_steps_keep_values_and_save_without_taxes(owner):
    first = owner.client.get("/income/new")
    assert first.status_code == 200
    assert 'data-income-wizard="1"' in first.text and "How is it paid?" in first.text
    # Taxes are off: three steps, no Taxes step.
    assert ">Taxes</li>" not in first.text and "Payday" in first.text

    missing = _step(owner, _fixed(owner, name="") | {"step": "1", "go": "next"})
    assert missing.status_code == 400
    assert "Enter a name." in missing.text and 'data-income-wizard="1"' in missing.text
    assert 'value="fixed" class="mt-1 h-4 w-4 accent-primary" checked' in missing.text

    second = _save(owner, _fixed(owner), 1)
    assert 'data-income-wizard="2"' in second.text
    assert 'type="hidden" name="name" value="Salary"' in second.text
    assert "Enter the amount before the taxes you plan here." in second.text
    assert 'name="rate_amount"' not in second.text  # only the fixed amount for a fixed source

    bad = _save(owner, _fixed(owner, fixed_amount="abc"), 2)
    assert bad.status_code == 400 and "Enter a positive amount" in bad.text
    assert 'name="fixed_amount" value="abc"' in bad.text

    back = _step(owner, _fixed(owner) | {"step": "2", "go": "back"})
    assert 'data-income-wizard="1"' in back.text and 'name="name" value="Salary"' in back.text

    third = _save(owner, _fixed(owner), 2)
    assert 'data-income-wizard="3"' in third.text
    assert "Work done in January is paid on January 10." in third.text
    assert ">Save income</button>" in third.text

    reversed_range = _save(owner, _fixed(owner, active_to_month="1", active_to_year="2025"), 3)
    assert reversed_range.status_code == 400
    assert "The end month cannot be before the start month." in reversed_range.text

    saved = _save(owner, _fixed(owner), 3)
    assert saved.status_code == 303 and saved.headers["location"].endswith("/income")
    with owner.db() as session:
        source = session.scalars(select(IncomeSource)).one()
        assert (source.kind, source.fixed_minor, source.pay_day, source.pay_lag_months) == (
            "fixed",
            850_000,
            10,
            0,
        )


def test_wizard_checks_earlier_steps_again(owner):
    # A hidden value edited between steps is caught on the step it belongs to.
    response = _save(owner, _fixed(owner, fixed_amount="0"), 3)
    assert response.status_code == 400 and 'data-income-wizard="2"' in response.text


def test_wizard_per_unit_fields_and_payday_sentence(owner):
    step2 = _save(owner, _variable(owner), 1)
    assert 'data-hidden-when="expected_mode!=fixed"' in step2.text
    assert 'data-hidden-when="unit_choice!=other"' in step2.text
    no_unit = _save(owner, _variable(owner, unit_choice="other", unit_other=""), 2)
    assert "Enter what you are paid per" in no_unit.text
    sentence = owner.client.post(
        "/income/payday-sentence",
        data={
            "csrf_token": owner.token("/income/new"),
            "pay_day": "10",
            "pay_lag_months": "1",
            "active_from_month": "10",
            "active_from_year": "2026",
        },
    )
    assert sentence.text == "Work done in October is paid on November 10."


def test_wizard_tax_step_saves_the_ticked_taxes(owner):
    _enable_taxes(owner)
    vat = _tax(owner, "VAT", "added", "23")
    pit = _tax(owner, "Income tax", "self_paid", "12", day="20")
    step4 = _save(owner, _variable(owner), 3)
    assert 'data-income-wizard="4"' in step4.text
    assert "Which taxes apply to this income?" in step4.text
    assert "Added on top of what you charge; you pass it on." in step4.text
    assert "Set up a new tax" in step4.text
    assert "No tax payments are planned." in step4.text

    example = owner.client.post(
        "/income/new/example",
        data={"csrf_token": owner.token("/income/new"), "tax_rule_id": [vat, pit]}
        | _variable(owner),
    )
    assert "Example for €10,000.00 net" in example.text
    assert "You receive €12,300.00." in example.text
    assert "VAT €2,300.00 to pay on" in example.text and "Income tax €1,200.00" in example.text

    saved = _save(owner, _variable(owner) | {"tax_rule_id": [vat, pit]}, 4)
    assert saved.status_code == 303
    with owner.db() as session:
        source = session.scalars(select(IncomeSource)).one()
        assert source.unit_label == "day" and source.expected_mode == "fixed"
        rules = {str(row.tax_rule_id) for row in session.scalars(select(IncomeSourceTaxRule))}
        assert rules == {vat, pit}

    card = _card(owner.client.get("/income").text, "Contract work")
    assert (
        "€1,200.00 per day · paid on the 10th of the next month · estimate: 20 days a month" in card
    )
    assert "VAT 23% · added on top" in card and "Income tax 12% · paid by you" in card
    for header in ("Work month", "Net", "Taxes", "You receive", "Tax to pay"):
        assert f">{header}</th>" in card
    assert "+€5,520.00" in card  # 20 days × 1 200 × 23 %
    assert "€29,520.00" in card and "Income tax €2,880.00 on" in card
    assert ">estimate</span>" in card
    assert "data-no-tax-reason" not in card


def test_cards_without_taxes_and_with_taxes_switched_off(owner):
    assert _save(owner, _fixed(owner), 3).status_code == 303
    assert _save(owner, _variable(owner), 3).status_code == 303
    page = owner.client.get("/income").text
    assert "<h1" in page and ">Income<" in page
    assert "Where your money comes from and what you will receive next." in page
    assert "Add income" in page
    salary = _card(page, "Salary")
    assert "Fixed · €8,500.00 on the 10th · Income category:" in salary
    assert ">Work month</th>" not in salary and ">Net</th>" not in salary
    assert ">Paid on</th>" in salary and ">You receive</th>" in salary
    contract = _card(page, "Contract work")
    assert ">Work month</th>" in contract and ">Net</th>" not in contract
    assert "Log days worked" in contract and "Work calendar" in contract
    # Taxes are off: no why-no-tax message and no checklist.
    assert "data-no-tax-reason" not in page and "Taxes on this income" not in page


def test_why_no_tax_payment_messages(owner):
    _enable_taxes(owner)
    withheld = _tax(owner, "Salary tax", "payer_withheld", "12")
    pit = _tax(owner, "Income tax", "self_paid", "12")
    assert _save(owner, _fixed(owner, name="Flat rental"), 4).status_code == 303
    assert _save(owner, _fixed(owner) | {"tax_rule_id": withheld}, 4).status_code == 303
    assert _save(owner, _variable(owner) | {"tax_rule_id": pit}, 4).status_code == 303
    with owner.db() as session, session.begin():
        session.execute(
            update(IncomeSourceTaxRule)
            .values(generate_payment=False)
            .where(IncomeSourceTaxRule.tax_rule_id == TaxRule.id, TaxRule.name == "Income tax")
        )
    page = owner.client.get("/income").text
    rental = _card(page, "Flat rental")
    assert "no taxes are ticked on it" in rental
    with owner.db() as session:
        rental_id = session.scalars(
            select(IncomeSource.id).where(IncomeSource.name == "Flat rental")
        ).one()
    assert f"?form=taxes-{rental_id}#taxes-{rental_id}" in rental
    opened = owner.client.get(f"/income?form=taxes-{rental_id}").text
    assert f'id="taxes-{rental_id}" class="open:w-full" open' in opened
    salary = _card(page, "Salary")
    assert "the payer pays these taxes for you" in salary
    assert "Salary tax 12% · taken off by the payer" in salary
    contract = _card(page, "Contract work")
    assert "planning its payment is switched off" in contract


def test_edit_page_saves_the_sentence_style_fields(owner):
    assert _save(owner, _variable(owner), 3).status_code == 303
    with owner.db() as session:
        source_id = session.scalars(select(IncomeSource.id)).one()
    page = owner.client.get(f"/income/{source_id}/edit")
    assert page.status_code == 200
    assert 'data-hidden-when="kind=variable"' in page.text  # both kinds, one form
    assert '<option value="1" selected>of the next month</option>' in page.text
    assert "Work done in January is paid on February 10." in page.text
    values = _variable(owner) | {
        "csrf_token": owner.token(f"/income/{source_id}/edit"),
        "unit_choice": "other",
        "unit_other": "article",
        "pay_lag_months": "2",
    }
    saved = owner.client.post(f"/income/{source_id}", data=values, follow_redirects=False)
    assert saved.status_code == 303 and "error" not in saved.headers["location"]
    with owner.db() as session:
        source = session.get(IncomeSource, source_id)
        assert source is not None
        assert (source.unit_label, source.pay_lag_months) == ("article", 2)
    assert "two months later" in owner.client.get("/income").text


def test_viewer_sees_the_page_read_only(owner):
    _enable_taxes(owner)
    assert _save(owner, _variable(owner), 4).status_code == 303
    owner.set_admin_role("viewer")
    page = owner.client.get("/income").text
    assert "Contract work" in page and "Work calendar" in page
    for marker in ("data-add-income", "data-edit-income", "data-log-work", "data-income-taxes"):
        assert marker not in page
    assert "Choose taxes" not in page
    assert owner.client.get("/income/new").status_code == 403
    with owner.db() as session:
        source_id = session.scalars(select(IncomeSource.id)).one()
    assert owner.client.get(f"/income/{source_id}/edit").status_code == 403


def test_wizard_requires_csrf(owner):
    response = owner.client.post(
        "/income/new", data=_fixed(owner) | {"step": "3", "go": "next"}, follow_redirects=False
    )
    assert response.status_code == 403
    with owner.db() as session:
        assert session.scalars(select(IncomeSource)).first() is None
