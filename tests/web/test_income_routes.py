from __future__ import annotations

from sqlalchemy import select

from assetboard.db.models.income import IncomeSource
from assetboard.db.models.ledger import Category


def _income_category(owner) -> str:
    with owner.db() as session:
        return str(
            session.scalars(
                select(Category.id).where(
                    Category.workspace_id == owner.home_id,
                    Category.kind == "income",
                    Category.archived_at.is_(None),
                )
            ).first()
        )


def _form(category_id: str) -> dict[str, str]:
    return {
        "name": "Contract work",
        "kind": "variable",
        "category_id": category_id,
        "rate_amount": "1,230.00",
        "unit_label": "day",
        "pay_day": "10",
        "pay_lag_months": "1",
        "expected_mode": "manual",
        "expected_units": "",
        "average_months": "3",
        "record_as": "cash_received",
        "active_from": "2026-01-01",
    }


def test_income_source_and_work_log_forms(owner):
    created = owner.post("/income", _form(_income_category(owner)), token_from="/income")
    assert created.status_code == 303
    assert "error" not in created.headers["location"]
    with owner.db() as session:
        source = session.scalars(select(IncomeSource)).one()
        assert source.rate_minor == 123_000

    logged = owner.post(
        f"/income/{source.id}/work-logs",
        {"work_month": "2026-08-01", "units": "7.5"},
        token_from="/income",
    )
    assert logged.status_code == 303
    assert "error" not in logged.headers["location"]

    page = owner.client.get("/income")
    assert page.status_code == 200
    assert "Contract work" in page.text
    assert "Record as: Cash received (gross)" in page.text
    assert "· 7.5</li>" in page.text


def test_income_forms_require_csrf(owner):
    response = owner.client.post(
        "/income", data=_form(_income_category(owner)), follow_redirects=False
    )
    assert response.status_code == 403
    with owner.db() as session:
        assert session.scalars(select(IncomeSource)).first() is None
