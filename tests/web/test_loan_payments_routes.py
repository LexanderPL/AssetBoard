"""The F7.5 actual payments and checkpoint page."""

from __future__ import annotations

import re
from datetime import date
from uuid import UUID, uuid4

from assetboard.db.models.ledger import Category, Transaction
from assetboard.db.models.loans import Loan, LoanBalanceCheckpoint, LoanPayment


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


def _payment(
    *,
    day: str = "2026-02-01",
    kind: str = "installment",
    total: str = "100.00",
    breakdown: bool = True,
    principal: str = "80.00",
    interest: str = "15.00",
    costs: str = "5.00",
    overpayment: str = "0.00",
    note: str = "",
    transaction_id: str = "",
) -> dict[str, str]:
    return {
        "date": day,
        "kind": kind,
        "total": total,
        "use_breakdown": "1" if breakdown else "0",
        "principal": principal,
        "interest": interest,
        "costs": costs,
        "overpayment": overpayment,
        "note": note,
        "transaction_id": transaction_id,
    }


def _add_expense(owner, amount_minor: int = 10_000) -> str:
    with owner.db() as session, session.begin():
        category = Category(
            workspace_id=owner.home_id,
            name=f"Loan expense {uuid4().hex[:6]}",
            name_key=f"loan expense {uuid4().hex}",
            kind="expense",
            treatment="regular",
            color="#16a34a",
            sort_order=0,
        )
        session.add(category)
        session.flush()
        transaction = Transaction(
            workspace_id=owner.home_id,
            direction="expense",
            date=date(2026, 1, 15),
            amount_minor=amount_minor,
            currency="EUR",
            category_id=category.id,
            description="Bank payment",
            description_key="bank payment",
            source="manual",
            created_by_user_id=None,
        )
        session.add(transaction)
        session.flush()
        return str(transaction.id)


def test_payments_page_and_detail_link(owner) -> None:
    loan_id = _create(owner)
    page = owner.client.get(f"/loans/{loan_id}/payments")
    assert page.status_code == 200
    assert "data-loan-payments-list" in page.text
    assert "data-loan-checkpoints-list" in page.text
    assert "data-no-payments" in page.text
    assert "data-no-checkpoints" in page.text
    assert "data-dialog-modal" in page.text
    assert "Payments & balance" in page.text
    assert "data-payments-intro" in page.text
    assert "Actual payments" in page.text
    assert "Balance checkpoints" in page.text
    detail = owner.client.get(f"/loans/{loan_id}")
    assert "data-loan-payments" in detail.text
    assert f"/loans/{loan_id}/payments" in detail.text


def test_record_payment_and_show_actuals_on_the_schedule(owner) -> None:
    loan_id = _create(owner)
    path = f"/loans/{loan_id}/payments"
    response = owner.post(
        path,
        _payment(note="February statement"),
        token_from=path,
    )
    print(response.text)
    assert response.status_code == 303
    page = owner.client.get(path)
    assert "data-payment=" in page.text
    assert "February statement" in page.text
    with owner.db() as session:
        payment = session.query(LoanPayment).filter_by(loan_id=UUID(loan_id)).one()
        assert payment.total_minor == 10_000
        assert payment.principal_minor == 8_000
        assert payment.interest_minor == 1_500
        assert payment.costs_minor == 500
        assert payment.overpayment_minor == 0

    detail = owner.client.get(f"/loans/{loan_id}?schedule_page=1")
    assert "data-schedule-actual" in detail.text
    assert 'data-actual-total="10000"' in detail.text


def test_explicit_breakdown_validation_keeps_values(owner) -> None:
    loan_id = _create(owner)
    path = f"/loans/{loan_id}/payments"
    response = owner.post(
        path,
        _payment(
            total="100.00", principal="50.00", interest="10.00", costs="5.00", overpayment="0.00"
        ),
        token_from=path,
    )
    assert response.status_code == 400
    assert "must add up to the payment total" in response.text
    assert 'value="100.00"' in response.text
    assert 'value="50.00"' in response.text
    assert "data-action-error" in response.text


def test_link_a_recent_expense_transaction(owner) -> None:
    loan_id = _create(owner)
    transaction_id = _add_expense(owner, amount_minor=10_000)
    path = f"/loans/{loan_id}/payments"
    page = owner.client.get(path)
    assert "Bank payment" in page.text
    response = owner.post(
        path,
        _payment(transaction_id=transaction_id),
        token_from=path,
    )
    assert response.status_code == 303
    page = owner.client.get(path)
    assert "Bank payment" in page.text
    with owner.db() as session:
        payment = session.query(LoanPayment).filter_by(loan_id=UUID(loan_id)).one()
        assert str(payment.transaction_id) == transaction_id


def test_edit_and_delete_payment(owner) -> None:
    loan_id = _create(owner)
    path = f"/loans/{loan_id}/payments"
    owner.post(path, _payment(), token_from=path)
    with owner.db() as session:
        payment_id = str(session.query(LoanPayment).filter_by(loan_id=UUID(loan_id)).one().id)
    edit = owner.client.get(f"{path}?form=payment&edit={payment_id}")
    assert edit.status_code == 200
    assert 'value="100.00"' in edit.text
    updated = owner.post(
        f"/loans/{loan_id}/payments/{payment_id}",
        _payment(total="120.00", principal="100.00", interest="15.00", costs="5.00"),
        token_from=path,
    )
    assert updated.status_code == 303
    with owner.db() as session:
        payment = session.get(LoanPayment, UUID(payment_id))
        assert payment is not None and payment.total_minor == 12_000
    deleted = owner.post(f"/loans/{loan_id}/payments/{payment_id}/delete")
    assert deleted.status_code == 303
    with owner.db() as session:
        assert session.query(LoanPayment).count() == 0


def test_checkpoints_list_variance_edit_and_delete(owner) -> None:
    loan_id = _create(owner)
    path = f"/loans/{loan_id}/payments"
    response = owner.post(
        f"/loans/{loan_id}/checkpoints",
        {"as_of": "2026-02-01", "remaining_principal": "290,000.00", "note": "Statement"},
        token_from=path,
    )
    assert response.status_code == 303
    page = owner.client.get(path)
    assert "data-checkpoint=" in page.text
    assert "Projected balance" in page.text
    assert "Variance" in page.text
    assert "Statement" in page.text
    with owner.db() as session:
        checkpoint_id = str(
            session.query(LoanBalanceCheckpoint).filter_by(loan_id=UUID(loan_id)).one().id
        )
    edit = owner.client.get(f"{path}?form=checkpoint&edit={checkpoint_id}")
    assert edit.status_code == 200
    updated = owner.post(
        f"/loans/{loan_id}/checkpoints/{checkpoint_id}",
        {"as_of": "2026-02-02", "remaining_principal": "289,000.00", "note": "Updated"},
        token_from=path,
    )
    assert updated.status_code == 303
    page = owner.client.get(path)
    assert "Updated" in page.text
    deleted = owner.post(f"/loans/{loan_id}/checkpoints/{checkpoint_id}/delete")
    assert deleted.status_code == 303
    with owner.db() as session:
        assert session.query(LoanBalanceCheckpoint).count() == 0


def test_checkpoint_errors_keep_values(owner) -> None:
    loan_id = _create(owner)
    path = f"/loans/{loan_id}/payments"
    owner.post(
        f"/loans/{loan_id}/checkpoints",
        {"as_of": "2026-02-01", "remaining_principal": "290,000.00", "note": ""},
        token_from=path,
    )
    duplicate = owner.post(
        f"/loans/{loan_id}/checkpoints",
        {"as_of": "2026-02-01", "remaining_principal": "280,000.00", "note": ""},
        token_from=path,
    )
    assert duplicate.status_code == 400
    assert "already exists on that date" in duplicate.text
    assert 'value="280,000.00"' in duplicate.text


def test_quick_record_query_prefills_the_payment_dialog(owner) -> None:
    loan_id = _create(owner)
    page = owner.client.get(
        f"/loans/{loan_id}/payments?form=payment&payment_date=2026-03-01&payment_total=100.00"
    )
    assert page.status_code == 200
    assert 'value="2026-03-01"' in page.text
    assert 'value="100.00"' in page.text
    assert 'id="payment-dialog"' in page.text


def test_viewer_can_read_but_not_change_payments_or_checkpoints(owner) -> None:
    loan_id = _create(owner)
    path = f"/loans/{loan_id}/payments"
    owner.post(path, _payment(), token_from=path)
    owner.set_admin_role("viewer")
    page = owner.client.get(path)
    assert page.status_code == 200
    assert "data-payment=" in page.text
    assert "data-dialog-modal" not in page.text
    assert "Record payment" not in page.text


def test_payment_variance_is_rendered_per_installment(owner) -> None:
    loan_id = _create(owner)
    path = f"/loans/{loan_id}/payments"
    # The projected first installment is about 1,703.37: 100.00 is less, 5,000.00 is more.
    owner.post(path, _payment(day="2026-02-15", total="100.00"), token_from=path)
    owner.post(
        path,
        _payment(
            day="2026-02-01",
            total="5,000.00",
            principal="4,000.00",
            interest="1,000.00",
            costs="0.00",
        ),
        token_from=path,
    )
    page = owner.client.get(path)
    assert page.status_code == 200
    assert 'data-payment-variance="less"' in page.text
    assert "Less than projected" in page.text
    assert 'data-payment-variance="more"' in page.text
    assert "More than projected" in page.text
    assert re.search(r'data-payment-total-variance="-\d+"', page.text)
    assert re.search(r'data-payment-total-variance="\d+"', page.text)
    assert "data-payment-principal-variance=" in page.text


def test_overpayment_only_payment_has_no_variance_row(owner) -> None:
    loan_id = _create(owner)
    path = f"/loans/{loan_id}/payments"
    owner.post(
        path,
        _payment(
            day="2026-02-01",
            kind="overpayment",
            total="200.00",
            principal="0.00",
            interest="0.00",
            costs="0.00",
            overpayment="200.00",
        ),
        token_from=path,
    )
    page = owner.client.get(path)
    assert page.status_code == 200
    assert 'data-payment-variance="unmatched"' in page.text
    assert "No schedule row to compare" in page.text


def test_current_path_starting_checkpoint_is_labelled_and_cannot_be_deleted(owner) -> None:
    loan_id = _create(
        owner,
        entry_path="current",
        principal_original="",
        origination_date="",
        current_balance="200,000.00",
        balance_date="2026-02-01",
        first_payment_date="2026-03-01",
        term_months="120",
    )
    path = f"/loans/{loan_id}/payments"
    page = owner.client.get(path)
    assert "data-checkpoint-starting" in page.text
    assert "Starting balance" in page.text
    with owner.db() as session:
        checkpoint_id = str(
            session.query(LoanBalanceCheckpoint).filter_by(loan_id=UUID(loan_id)).one().id
        )
    response = owner.post(f"/loans/{loan_id}/checkpoints/{checkpoint_id}/delete")
    assert response.status_code == 400
    assert "starting balance checkpoint is required" in response.text
    with owner.db() as session:
        assert session.query(LoanBalanceCheckpoint).count() == 1
