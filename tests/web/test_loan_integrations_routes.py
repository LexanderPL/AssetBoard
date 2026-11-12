"""F7.6 loan integrations: net worth, property and scheduled generation."""

from __future__ import annotations

from datetime import date
from uuid import UUID, uuid4

from assetboard.db.models.ledger import Category
from assetboard.db.models.loans import Loan
from assetboard.db.models.networth import ItemValuation, NetWorthCategory, NetWorthItem
from assetboard.db.models.scheduled import ScheduledItem, ScheduledOccurrence


def _wizard_data(**extra: str) -> dict[str, str]:
    data = {
        "entry_path": "original",
        "name": "Mortgage",
        "kind": "annuity",
        "principal_original": "300,000.00",
        "origination_date": "2026-01-01",
        "first_payment_date": "2026-02-01",
        "term_months": "360",
        "payment_day": "1",
        "rate_mode": "fixed",
        "fixed_rate_percent": "5.5",
        "reference_rate_id": "",
        "margin_percent": "",
        "high_rate_confirmed": "",
        "installment_override": "",
        "overpayment_mode": "none",
        "overpayment_amount": "",
        "overpayment_strategy": "reduce_term",
        "current_balance": "",
        "balance_date": "",
    }
    data.update(extra)
    return data


def _step(owner, data: dict[str, str], step: int):
    return owner.client.post(
        "/loans/new",
        data={"csrf_token": owner.token("/loans/new"), **data, "step": str(step), "go": "next"},
        follow_redirects=False,
    )


def _create(owner, **extra: str) -> str:
    data = _wizard_data(**extra)
    for step in (1, 2, 3):
        response = _step(owner, data, step)
        assert response.status_code == 200, response.text
    saved = _step(owner, data, 4)
    assert saved.status_code == 303, saved.text
    name = extra.get("name", "Mortgage")
    with owner.db() as session:
        return str(
            session.query(Loan)
            .filter(Loan.workspace_id == owner.home_id, Loan.name == name)
            .one()
            .id
        )


def _category(session, owner, name: str, side: str) -> NetWorthCategory:
    row = (
        session.query(NetWorthCategory)
        .filter(
            NetWorthCategory.workspace_id == owner.home_id,
            NetWorthCategory.side == side,
            NetWorthCategory.name == name,
        )
        .one_or_none()
    )
    if row is not None:
        return row
    row = NetWorthCategory(
        workspace_id=owner.home_id,
        name=name,
        name_key=name.casefold(),
        side=side,
        color="#16a34a",
        sort_order=0,
    )
    session.add(row)
    session.flush()
    return row


def _property_item(owner, *, name: str = "Flat", currency: str = "EUR") -> str:
    with owner.db() as session, session.begin():
        category = _category(session, owner, "Real estate", "asset")
        item = NetWorthItem(
            workspace_id=owner.home_id,
            side="asset",
            category_id=category.id,
            name=name,
            currency=currency,
            notes="",
            valuation_mode="manual",
            include_in_net_worth=True,
            investable=False,
            estimate_between=False,
        )
        session.add(item)
        session.flush()
        session.add(
            ItemValuation(
                workspace_id=owner.home_id,
                item_id=item.id,
                as_of=date(2026, 1, 1),
                value_minor=40_000_000,
                currency=currency,
                note="",
            )
        )
        return str(item.id)


def _expense_category(owner, name: str = "Housing") -> str:
    with owner.db() as session, session.begin():
        row = Category(
            workspace_id=owner.home_id,
            name=f"{name} {uuid4().hex[:6]}",
            name_key=f"{name} {uuid4().hex}".casefold(),
            kind="expense",
            treatment="regular",
            color="#16a34a",
            sort_order=0,
        )
        session.add(row)
        session.flush()
        return str(row.id)


def test_integrations_section_creates_networth_item(owner) -> None:
    loan_id = _create(owner)
    page = owner.client.get(f"/loans/{loan_id}")
    assert "data-loan-integrations" in page.text
    assert "Create net-worth item" in page.text
    response = owner.post(f"/loans/{loan_id}/networth-item")
    assert response.status_code == 303
    page = owner.client.get(f"/loans/{loan_id}")
    assert "Linked item: Mortgage" in page.text
    assert "data-linked-item" in page.text
    with owner.db() as session:
        items = session.query(NetWorthItem).filter_by(workspace_id=owner.home_id).all()
    assert len(items) == 1
    assert items[0].side == "liability"
    assert items[0].link_type == "loan_balance"
    assert str(items[0].link_id) == loan_id


def test_property_link_ltv_equity_and_clear(owner) -> None:
    loan_id = _create(owner)
    property_id = _property_item(owner)
    response = owner.post(
        f"/loans/{loan_id}/property",
        {"property_item_id": property_id},
    )
    assert response.status_code == 303
    page = owner.client.get(f"/loans/{loan_id}?property_as_of=2026-01-01")
    assert "Flat" in page.text
    assert "75%" in page.text
    assert "100,000.00" in page.text
    assert "data-property-ltv-unavailable" not in page.text
    cleared = owner.post(f"/loans/{loan_id}/property/clear")
    assert cleared.status_code == 303
    page = owner.client.get(f"/loans/{loan_id}")
    assert "No property is linked to this loan." in page.text


def test_property_validation_error_is_shown(owner) -> None:
    loan_id = _create(owner)
    property_id = _property_item(owner, currency="USD")
    response = owner.post(f"/loans/{loan_id}/property", {"property_item_id": property_id})
    assert response.status_code == 303
    page = owner.client.get(response.headers["location"])
    with owner.db() as session:
        assert session.get(Loan, UUID(loan_id)).property_item_id is None
    assert "The property and the loan must use the same currency." in page.text


def test_scheduled_generation_and_disable(owner) -> None:
    loan_id = _create(owner)
    category_id = _expense_category(owner)
    response = owner.post(
        f"/loans/{loan_id}/scheduled-item",
        {"category_id": category_id},
    )
    assert response.status_code == 303
    page = owner.client.get(f"/loans/{loan_id}")
    assert "Recurring installments" in page.text
    assert "Refresh occurrences" in page.text
    assert "data-scheduled-generation" in page.text
    with owner.db() as session:
        item = session.query(ScheduledItem).filter_by(loan_id=UUID(loan_id)).one()
        assert item.amount_mode == "from_loan"
        assert session.query(ScheduledOccurrence).filter_by(scheduled_item_id=item.id).count() > 0
    disabled = owner.post(f"/loans/{loan_id}/scheduled-item/disable")
    assert disabled.status_code == 303
    page = owner.client.get(f"/loans/{loan_id}")
    assert "Generation is not active for this loan." in page.text


def test_auto_record_setting_and_viewer_read_only(owner) -> None:
    loan_id = _create(owner)
    response = owner.post(f"/loans/{loan_id}/auto-record", {"enabled": "1"})
    assert response.status_code == 303
    with owner.db() as session:
        assert session.get(Loan, UUID(loan_id)).auto_record_payments is True
    owner.set_admin_role("viewer")
    page = owner.client.get(f"/loans/{loan_id}")
    assert page.status_code == 200
    assert "data-loan-integrations" in page.text
    assert "Create net-worth item" not in page.text
    assert "Generate occurrences" not in page.text
    assert "Record posted installments automatically" not in page.text
