from __future__ import annotations


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
