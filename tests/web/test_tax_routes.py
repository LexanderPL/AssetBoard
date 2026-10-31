from __future__ import annotations

from sqlalchemy import select

from assetboard.db.models.income import IncomeSource, IncomeSourceTaxRule
from assetboard.db.models.ledger import Category
from assetboard.db.models.scheduled import ScheduledItem
from assetboard.db.models.tax import TaxRule, TaxRuleVersion


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


def _enable(owner, on: bool = True):
    return owner.post("/workspace/settings/menu", {"taxes_enabled": "1" if on else "0"})


def _new_tax(owner, **fields: str):
    data = {
        "name": "VAT",
        "kind": "added",
        "mode": "percent",
        "rate_percent": "23",
        "fixed_amount": "",
        "payment_day": "25",
        "payment_timing": "next_month",
        "payment_category_id": _category(owner, "expense"),
    } | fields
    return owner.post("/taxes", data)


def test_tax_pages_are_hidden_until_taxes_are_switched_on(owner):
    assert owner.client.get("/taxes").status_code == 404
    assert owner.client.get("/taxes/new").status_code == 404
    assert "data-taxes-link" not in owner.client.get("/").text
    settings = owner.client.get("/workspace/settings").text
    assert "data-taxes-switch" in settings and 'aria-pressed="false"' in settings
    # CSRF is required for the switch.
    refused = owner.client.post(
        "/workspace/settings/menu", data={"taxes_enabled": "1"}, follow_redirects=False
    )
    assert refused.status_code == 403
    assert _enable(owner).headers["location"].endswith("/workspace/settings?saved=1")
    assert "data-taxes-link" in owner.client.get("/").text
    page = owner.client.get("/taxes")
    assert page.status_code == 200
    assert "Only if you want taxes planned" in page.text and "data-tax-warning" not in page.text
    # The old calculator URL redirects to the Taxes page.
    old = owner.client.get("/taxes/calculator", follow_redirects=False)
    assert old.status_code == 303 and old.headers["location"].endswith("/taxes")


def test_new_tax_list_card_and_edit(owner):
    _enable(owner)
    form = owner.client.get("/taxes/new").text
    assert 'type="radio" name="kind" value="added"' in form
    assert "Who pays it to the tax office?" in form and "data-tax-example" in form
    assert _new_tax(owner).status_code == 303
    with owner.db() as session:
        rule = session.scalars(select(TaxRule)).one()
        version = session.scalars(select(TaxRuleVersion)).one()
        assert (rule.kind, rule.payment_day, rule.payment_timing) == ("added", 25, "next_month")
        assert (version.effective_from.year, version.rate_bp) == (1900, 2300)
        rule_id = rule.id
    page = owner.client.get("/taxes").text
    assert "23%" in page and "Added on top of what you charge" in page
    assert "Paid on the 25th of the month after you are paid" in page
    assert "Used by: not used yet" in page and "Rate changes" in page

    edit = owner.client.get(f"/taxes/{rule_id}/edit").text
    assert 'value="added" class="mt-1 h-4 w-4 accent-primary" checked' in edit
    saved = owner.post(
        f"/taxes/{rule_id}/edit",
        {
            "name": "Income tax",
            "kind": "payer_withheld",
            "base": "gross",
            "payment_day": "20",
            "payment_timing": "same_month",
            "payment_category_id": "",
            "rate_percent": "99",  # ignored: rates change with versions
        },
    )
    assert saved.status_code == 303
    with owner.db() as session:
        rule = session.get(TaxRule, rule_id)
        assert rule is not None
        assert (rule.name, rule.kind, rule.base, rule.payment_category_id) == (
            "Income tax",
            "payer_withheld",
            "gross",
            None,
        )
        assert session.scalars(select(TaxRuleVersion.rate_bp)).all() == [2300]
    assert "Nothing to pay yourself" in owner.client.get("/taxes").text

    # A rate change adds a version; an older date reopens the collapsed form with the error.
    ok = owner.post(
        f"/taxes/{rule_id}/versions",
        {"effective_from": "2027-01-01", "mode": "percent", "rate_percent": "19"},
    )
    assert ok.status_code == 303 and "error" not in ok.headers["location"]
    bad = owner.post(
        f"/taxes/{rule_id}/versions",
        {"effective_from": "2026-01-01", "mode": "percent", "rate_percent": "18"},
    )
    assert bad.headers["location"].endswith(f"error=version_not_newer&form=rates-{rule_id}")
    assert "data-tax-rates open" in owner.client.get(bad.headers["location"]).text


def test_invalid_new_tax_is_shown_again_with_an_error(owner):
    _enable(owner)
    response = _new_tax(owner, name="Social", kind="self_paid", payment_category_id="")
    assert response.status_code == 400
    assert "Choose an expense category that is not a transfer." in response.text
    assert 'value="Social"' in response.text
    response = _new_tax(owner, rate_percent="1.234")
    assert response.status_code == 400 and "at most two decimal places" in response.text


def test_example_partial_for_each_kind(owner):
    _enable(owner)
    base = {
        "name": "PIT",
        "mode": "percent",
        "rate_percent": "12",
        "payment_day": "20",
        "payment_timing": "next_month",
    }
    added = owner.post("/taxes/example", base | {"kind": "added"}, token_from="/taxes/new")
    assert "+ PIT" in added.text and "11,200.00" in added.text
    paid = owner.post("/taxes/example", base | {"kind": "self_paid"}, token_from="/taxes/new")
    assert "PIT (to pay later)" in paid.text and "8,800.00" in paid.text
    withheld = owner.post(
        "/taxes/example", base | {"kind": "payer_withheld"}, token_from="/taxes/new"
    )
    assert "nothing is planned" in withheld.text and "8,800.00" in withheld.text
    empty = owner.post("/taxes/example", base | {"rate_percent": ""}, token_from="/taxes/new")
    assert "data-example-empty" in empty.text


def test_other_taxes_are_recurring_items_marked_as_taxes(owner):
    _enable(owner)
    data = {
        "name": "Vehicle tax",
        "amount": "480.00",
        "how_often": "quarterly",
        "due_date": "2027-01-31",
        "category_id": _category(owner, "expense"),
    }
    assert owner.post("/taxes/other", data).status_code == 303
    with owner.db() as session:
        item = session.scalars(select(ScheduledItem)).one()
        assert (item.is_tax, item.freq, item.interval, item.amount_minor, item.direction) == (
            True,
            "monthly",
            3,
            48_000,
            "expense",
        )
    page = owner.client.get("/taxes").text
    assert "Vehicle tax" in page and "every quarter" in page and "Next: Jan 31, 2027" in page
    assert f"/recurring/{item.id}/edit" in page
    assert "Vehicle tax" in owner.client.get("/recurring").text
    bad = owner.post("/taxes/other", data | {"amount": "x"})
    assert bad.headers["location"].endswith("error=invalid_other_tax&form=other-tax")


def test_income_checklist_and_switch_refusal(owner):
    _enable(owner)
    assert _new_tax(owner, name="PIT", kind="self_paid", rate_percent="12").status_code == 303
    form = {
        "name": "Contract work",
        "kind": "fixed",
        "category_id": _category(owner, "income"),
        "fixed_amount": "1,000.00",
        "unit_label": "day",
        "pay_day": "10",
        "pay_lag_months": "0",
        "expected_mode": "manual",
        "average_months": "3",
        "active_from_month": "1",
        "active_from_year": "2026",
    }
    assert owner.post("/income", form, token_from="/income").is_redirect
    with owner.db() as session:
        source_id = session.scalars(select(IncomeSource.id)).one()
        rule_id = session.scalars(select(TaxRule.id)).one()
    page = owner.client.get("/income").text
    assert "Taxes on this income" in page and 'name="record_as"' not in page
    saved = owner.post(
        f"/income/{source_id}/taxes",
        {"tax_rule_id": str(rule_id), f"due_day_{rule_id}": "15"},
        token_from="/income",
    )
    assert saved.status_code == 303 and "error" not in saved.headers["location"]
    with owner.db() as session:
        attachment = session.scalars(select(IncomeSourceTaxRule)).one()
        assert (attachment.due_day, attachment.payment_category_id) == (15, None)
    assert "Tax to pay: PIT" in owner.client.get("/income").text

    refused = _enable(owner, on=False)
    assert refused.status_code == 400
    assert "data-taxes-in-use" in refused.text and "Contract work" in refused.text

    # Unticking everything lets the switch go off; the tax parts of income disappear.
    assert owner.post(f"/income/{source_id}/taxes", {}, token_from="/income").is_redirect
    assert _enable(owner, on=False).status_code == 303
    income_page = owner.client.get("/income").text
    assert "Taxes on this income" not in income_page
    hidden = owner.post(f"/income/{source_id}/taxes", {}, token_from="/income")
    assert hidden.status_code == 404
