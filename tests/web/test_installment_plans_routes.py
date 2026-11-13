"""F7.7 installment plans page."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select, update

from assetboard.db.models.ledger import Category
from assetboard.db.models.loans import InstallmentPlan, Loan
from assetboard.db.models.networth import NetWorthCategory
from assetboard.db.models.scheduled import ScheduledOccurrence


def _category(owner, name: str = "Installments") -> str:
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


def _data(**extra: str) -> dict[str, str]:
    values = {
        "name": "Laptop",
        "installment": "1,000.00",
        "remaining_count": "3",
        "payment_day": "5",
        "category_id": "",
    }
    values.update(extra)
    return values


def _create(owner, **extra: str) -> str:
    category_id = _category(owner)
    response = owner.post(
        "/loans/installments",
        _data(category_id=category_id, **extra),
    )
    assert response.status_code == 303, response.text
    name = extra.get("name", "Laptop")
    with owner.db() as session:
        return str(
            session.query(Loan)
            .filter(Loan.workspace_id == owner.home_id, Loan.name == name)
            .one()
            .id
        )


def test_installments_tab_empty_state_and_add_plan(owner) -> None:
    page = owner.client.get("/loans/installments")
    assert page.status_code == 200
    assert "data-installments-tab" in page.text
    assert "No installment plans yet." in page.text
    assert "data-new-installment-plan" in page.text
    form = owner.client.get("/loans/installments/new")
    assert form.status_code == 200
    assert 'name="installment"' in form.text
    assert "data-action-error" not in form.text

    loan_id = _create(owner)
    page = owner.client.get("/loans/installments")
    assert "data-installment-plan=" in page.text
    assert "Laptop" in page.text
    assert "3,000.00" in page.text
    assert f'href="/loans/installments/{loan_id}"' in page.text
    assert "data-open-installment-plan" in page.text
    assert "data-open-installment-plan>Laptop</a>" in page.text
    assert 'data-installment-status-badge="open"' in page.text
    assert ">Open</span>" in page.text
    detail = owner.client.get(f"/loans/installments/{loan_id}")
    assert detail.status_code == 200
    assert "data-installment-plan-detail" in detail.text
    assert "data-plan-remaining" in detail.text
    assert "3,000.00" in detail.text


def test_ordinary_loans_list_excludes_plans_and_detail_redirects(owner) -> None:
    loan_id = _create(owner, name="Kitchen")
    page = owner.client.get("/loans")
    assert "Kitchen" not in page.text
    redirect = owner.client.get(f"/loans/{loan_id}", follow_redirects=False)
    assert redirect.status_code == 303
    assert redirect.headers["location"].endswith(f"/loans/installments/{loan_id}")


def test_post_next_installment_uses_the_due_occurrence_once(owner) -> None:
    loan_id = _create(owner, remaining_count="3")
    path = f"/loans/installments/{loan_id}"
    detail = owner.client.get(path)
    assert "data-post-next-installment" not in detail.text
    no_due = owner.post(f"{path}/post-next", token_from=path)
    assert no_due.status_code == 303
    assert "error=no_due_installment" in no_due.headers["location"]
    assert "No installment is due yet." in owner.client.get(no_due.headers["location"]).text
    assert "data-plan-remaining>" in owner.client.get(path).text

    today = owner.app.state.clock.today("UTC")
    with owner.db() as session, session.begin():
        occurrence = session.scalars(
            select(ScheduledOccurrence)
            .where(ScheduledOccurrence.workspace_id == owner.home_id)
            .order_by(ScheduledOccurrence.due_date)
        ).first()
        assert occurrence is not None
        session.execute(
            update(ScheduledOccurrence)
            .where(ScheduledOccurrence.id == occurrence.id)
            .values(due_date=today)
        )
    due = owner.client.get(path)
    assert "data-post-next-installment" in due.text
    posted = owner.post(f"{path}/post-next", token_from=path)
    assert posted.status_code == 303
    assert "notice=installment_plan_posted" in posted.headers["location"]
    after = owner.client.get(posted.headers["location"])
    assert "Next installment posted." in after.text
    assert "data-plan-remaining>2<" in after.text
    assert "data-installment-progress-detail" in after.text and ">1</span>" in after.text
    again = owner.post(f"{path}/post-next", token_from=path)
    assert again.status_code == 303
    assert "error=no_due_installment" in again.headers["location"]
    assert "data-plan-remaining>2<" in owner.client.get(path).text


def test_edit_closes_and_closed_section(owner) -> None:
    loan_id = _create(owner)
    edit = owner.client.get(f"/loans/installments/{loan_id}/edit")
    assert edit.status_code == 200
    assert 'value="1000.00"' in edit.text
    category_id = _category(owner)
    updated = owner.post(
        f"/loans/installments/{loan_id}",
        _data(
            name="Laptop Pro",
            installment="1,500.00",
            remaining_count="2",
            category_id=category_id,
        ),
    )
    assert updated.status_code == 303
    page = owner.client.get(f"/loans/installments/{loan_id}")
    assert "Laptop Pro" in page.text
    assert "3,000.00" in page.text
    closed = owner.post(
        f"/loans/installments/{loan_id}",
        _data(
            name="Laptop Pro",
            installment="1,500.00",
            remaining_count="0",
            category_id=category_id,
        ),
    )
    assert closed.status_code == 303
    detail = owner.client.get(f"/loans/installments/{loan_id}")
    assert 'data-installment-status-badge="closed"' in detail.text
    assert ">Closed</span>" in detail.text
    listing = owner.client.get("/loans/installments")
    assert "data-closed-installment-plan=" in listing.text
    assert "data-installment-plan=" not in listing.text
    assert 'data-installment-status-badge="closed"' in listing.text
    assert "data-open-closed-installment-plan" in listing.text
    assert "data-open-closed-installment-plan>Laptop Pro</a>" in listing.text


def test_archive_restore_and_networth_item(owner) -> None:
    loan_id = _create(owner)
    detail = owner.client.get(f"/loans/installments/{loan_id}")
    assert "Create net-worth item" in detail.text
    response = owner.post(f"/loans/{loan_id}/networth-item")
    assert response.status_code == 303
    detail = owner.client.get(f"/loans/installments/{loan_id}")
    assert "Linked item: Laptop" in detail.text
    with owner.db() as session:
        categories = session.query(NetWorthCategory).all()
    assert any(category.name == "Installments" for category in categories)

    archived = owner.post(f"/loans/installments/{loan_id}/archive")
    assert archived.status_code == 303
    detail = owner.client.get(f"/loans/installments/{loan_id}")
    assert 'data-installment-status-badge="closed"' in detail.text
    restored = owner.post(f"/loans/installments/{loan_id}/restore")
    assert restored.status_code == 303
    detail = owner.client.get(f"/loans/installments/{loan_id}")
    assert "data-plan-remaining" in detail.text


def test_create_validation_marks_the_field(owner):
    category_id = _category(owner)
    response = owner.post("/loans/installments", _data(installment="", category_id=category_id))
    assert response.status_code == 400
    assert 'name="installment"' in response.text and "data-field-error" in response.text


def test_create_validation_keeps_values(owner) -> None:
    category_id = _category(owner)
    response = owner.post(
        "/loans/installments",
        _data(name="", category_id=category_id),
    )
    assert response.status_code == 400
    assert "data-action-error" in response.text
    with owner.db() as session:
        assert session.query(InstallmentPlan).count() == 0


def test_viewer_reads_but_does_not_write(owner) -> None:
    loan_id = _create(owner)
    owner.set_admin_role("viewer")
    listing = owner.client.get("/loans/installments")
    assert listing.status_code == 200
    assert "data-new-installment-plan" not in listing.text
    detail = owner.client.get(f"/loans/installments/{loan_id}")
    assert detail.status_code == 200
    assert "data-plan-remaining" in detail.text
    assert "Edit plan" not in detail.text
    assert "Create net-worth item" not in detail.text
