from __future__ import annotations

from assetboard.db.models.identity import User


def test_tax_rule_crud_versions_and_calculator_preview(owner):
    data = {
        "name": "VAT",
        "effect": "added",
        "base": "net",
        "notes": "Standard rate",
        "effective_from": "2026-01-01",
        "mode": "percent",
        "rate_percent": "23",
    }
    created = owner.post("/taxes", data)
    assert created.status_code == 303

    page = owner.client.get("/taxes")
    assert page.status_code == 200
    assert "VAT" in page.text and "Rates are entered by you" in page.text
    assert "23.00%" in page.text
    with owner.db() as session:
        from sqlalchemy import select

        from assetboard.db.models.tax import TaxRule

        rule_id = session.scalars(select(TaxRule.id)).one()

    assert (
        owner.post(
            f"/taxes/{rule_id}/versions",
            {
                "effective_from": "2027-01-01",
                "mode": "fixed",
                "fixed_amount": "10.00",
            },
        ).status_code
        == 303
    )
    preview = owner.client.get(
        "/taxes/calculator?net=1000.00&day=2026-12-31&record_as=cash_received"
    )
    assert preview.status_code == 200
    assert "Gross" in preview.text and "1,230.00" in preview.text
    next_version = owner.client.get(
        "/taxes/calculator?net=1000.00&day=2027-01-01&record_as=cash_received"
    )
    assert "1,010.00" in next_version.text
    assert (
        owner.post(
            f"/taxes/{rule_id}/edit",
            {"name": "VAT revised", "notes": "Updated note"},
        ).status_code
        == 303
    )
    assert "VAT revised" in owner.client.get("/taxes").text
    assert owner.post(f"/taxes/{rule_id}/archive").status_code == 303
    archived_preview = owner.client.get(
        "/taxes/calculator?net=1000.00&day=2027-01-01&record_as=cash_received"
    )
    assert "No tax payments" in archived_preview.text


def test_tax_rule_forms_reject_invalid_values(owner):
    invalid = owner.post(
        "/taxes",
        {
            "name": "Invalid VAT",
            "effect": "added",
            "base": "gross",
            "notes": "",
            "effective_from": "2026-01-01",
            "mode": "percent",
            "rate_percent": "23",
        },
    )
    assert invalid.status_code == 303
    assert "error=invalid_rule" in invalid.headers["location"]
    assert (
        "Enter a non-negative net amount"
        in owner.client.get("/taxes/calculator?net=-1&day=2026-01-01&record_as=net").text
    )
    assert (
        "Choose how the income is recorded"
        in owner.client.get("/taxes/calculator?net=100&day=2026-01-01&record_as=unknown").text
    )


def _rule_form(rate: str) -> dict[str, str]:
    return {
        "name": "Income tax",
        "effect": "withheld",
        "base": "net",
        "notes": "",
        "effective_from": "2026-01-01",
        "mode": "percent",
        "rate_percent": rate,
    }


def test_rates_are_entered_as_locale_aware_percentages(owner):
    from sqlalchemy import select

    from assetboard.db.models.tax import TaxRuleVersion

    owner.client.headers["Accept-Language"] = "pl"
    assert owner.post("/taxes", _rule_form("12,75")).status_code == 303
    assert "12,75%" in owner.client.get("/taxes").text
    with owner.db() as session:
        assert session.scalars(select(TaxRuleVersion.rate_bp)).one() == 1275


def test_invalid_percentages_are_refused(owner):
    for rate in ("", "-1", "12.345", "abc", "99999999999"):
        response = owner.post("/taxes", _rule_form(rate))
        assert "error=invalid_rate" in response.headers["location"], rate


def test_the_new_tax_rule_form_is_collapsed_until_it_fails(owner):
    page = owner.client.get("/taxes").text
    assert "data-new-tax-rule>" in page and "data-new-tax-rule open" not in page
    failed = owner.post("/taxes", {"name": "", "effect": "added", "base": "net",
                                   "effective_from": "2026-01-01", "mode": "percent",
                                   "rate_percent": "x"})  # fmt: skip
    assert failed.headers["location"].endswith("&form=new-rule")
    assert "data-new-tax-rule open>" in owner.client.get(failed.headers["location"]).text


def test_the_rates_warning_can_be_dismissed_for_the_account(owner):
    rules = owner.client.get("/taxes").text
    calculator = owner.client.get("/taxes/calculator?net=100&day=2026-01-01&record_as=net").text
    assert "Rates are entered by you" in rules and "Rates are entered by you" in calculator

    response = owner.post(
        "/taxes/warning/dismiss",
        {"return_to": "calculator", "net": "100", "day": "2026-01-01", "record_as": "net"},
        token_from="/taxes",
    )
    assert response.status_code == 303
    assert response.headers["location"] == (
        "/taxes/calculator?net=100&day=2026-01-01&record_as=net"
    )
    assert "Rates are entered by you" not in owner.client.get("/taxes").text
    assert "Rates are entered by you" not in owner.client.get(response.headers["location"]).text
    with owner.db() as session:
        user = session.get(User, owner.admin_id)
        assert user is not None and user.tax_warning_dismissed
