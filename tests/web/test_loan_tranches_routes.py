"""Partial loan transfers, conversion and the interest-only phase (F7.10)."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from assetboard.clock import FixedClock
from assetboard.db.models.loans import Loan, LoanTransfer


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
        "drawdown_mode": "full_at_origination",
        "interest_only_months": "0",
    }
    data.update(extra)
    return data


def _step(owner, data: dict[str, str], step: int):
    return owner.client.post(
        "/loans/new",
        data={"csrf_token": owner.token("/loans/new"), **data, "step": str(step), "go": "next"},
        follow_redirects=False,
    )


def _create(owner, **extra: str) -> UUID:
    data = _wizard_data(**extra)
    for step in (1, 2, 3):
        response = _step(owner, data, step)
        assert response.status_code == 200, response.text
    saved = _step(owner, data, 4)
    assert saved.status_code == 303, saved.text
    with owner.db() as session:
        return (
            session.query(Loan)
            .filter(Loan.workspace_id == owner.home_id, Loan.name == data["name"])
            .one()
            .id
        )


def _add_transfer(owner, loan_id: UUID, day: date, amount: str) -> None:
    path = f"/loans/{loan_id}/settings"
    response = owner.post(
        f"/loans/{loan_id}/transfers",
        {"date": day.isoformat(), "amount": amount, "note": "Tranche"},
        token_from=path,
    )
    assert response.status_code == 303, response.text


def test_a_tranched_loan_shows_drawn_and_undrawn_money_and_its_transfers(owner) -> None:
    owner.app.state.clock = FixedClock("2026-03-01T12:00:00+00:00")
    loan_id = _create(owner, drawdown_mode="tranches", interest_only_months="6")

    with owner.db() as session:
        loan = session.get_one(Loan, loan_id)
        assert loan.drawdown_mode == "tranches"
        assert loan.interest_only_months == 6

    page = owner.client.get("/loans")
    assert page.status_code == 200
    assert f'data-loan="{loan_id}" data-loan-status="not_drawn"' in page.text
    assert ">Not drawn</span>" in page.text

    overview = owner.client.get(f"/loans/{loan_id}")
    assert 'data-loan-status-badge="not_drawn"' in overview.text
    assert "data-overview-undrawn" in overview.text
    assert "Interest-only for the first 6 months." in overview.text

    settings = owner.client.get(f"/loans/{loan_id}/settings")
    assert "data-loan-transfers-settings" in settings.text
    assert "No transfers yet" in settings.text
    empty_schedule = owner.client.get(f"/loans/{loan_id}/schedule")
    assert empty_schedule.status_code == 200
    assert "data-schedule-error" not in empty_schedule.text

    _add_transfer(owner, loan_id, date(2026, 1, 15), "100,000.00")
    _add_transfer(owner, loan_id, date(2026, 4, 1), "50,000.00")

    with owner.db() as session:
        transfers = (
            session.query(LoanTransfer)
            .filter(LoanTransfer.workspace_id == owner.home_id)
            .order_by(LoanTransfer.date)
            .all()
        )
        assert [row.amount_minor for row in transfers] == [10_000_000, 5_000_000]

    settings = owner.client.get(f"/loans/{loan_id}/settings")
    assert settings.text.count("data-transfer=") == 2
    assert "future" in settings.text
    assert "data-transfer-summary" in settings.text

    overview = owner.client.get(f"/loans/{loan_id}")
    assert "data-overview-undrawn" in overview.text
    assert "undrawn" in overview.text
    list_page = owner.client.get("/loans")
    assert "undrawn" in list_page.text
    assert "Interest-only for the first 6 months." in overview.text

    schedule = owner.client.get(f"/loans/{loan_id}/schedule")
    assert schedule.status_code == 200
    assert "data-schedule-transfer" in schedule.text
    assert ">Loan transfer</th>" in schedule.text
    assert "100,000.00" in schedule.text


def test_settings_warn_when_the_first_payment_covers_a_longer_gap(owner) -> None:
    loan_id = _create(
        owner,
        drawdown_mode="tranches",
        first_payment_date="2026-03-01",
    )
    _add_transfer(owner, loan_id, date(2026, 1, 10), "100,000.00")
    page = owner.client.get(f"/loans/{loan_id}/settings")
    assert "data-transfer-payment-warning" in page.text


def test_a_transfer_can_be_edited_and_deleted(owner) -> None:
    owner.app.state.clock = FixedClock("2026-03-01T12:00:00+00:00")
    loan_id = _create(owner, drawdown_mode="tranches")
    _add_transfer(owner, loan_id, date(2026, 1, 15), "100,000.00")
    with owner.db() as session:
        transfer = (
            session.query(LoanTransfer).filter(LoanTransfer.workspace_id == owner.home_id).one()
        )
        transfer_id = transfer.id

    edit = owner.client.get(f"/loans/{loan_id}/settings?edit_transfer={transfer_id}")
    assert edit.status_code == 200
    assert f'data-transfer-edit="{transfer_id}"' in edit.text
    assert 'value="100000.00"' in edit.text

    response = owner.post(
        f"/loans/{loan_id}/transfers/{transfer_id}",
        {"date": "2026-01-10", "amount": "120,000.00", "note": "Updated"},
        token_from=f"/loans/{loan_id}/settings",
    )
    assert response.status_code == 303
    page = owner.client.get(f"/loans/{loan_id}/settings")
    assert "Updated" in page.text
    with owner.db() as session:
        stored = session.get_one(LoanTransfer, transfer_id)
        assert stored.amount_minor == 12_000_000
        assert stored.date == date(2026, 1, 10)

    response = owner.post(
        f"/loans/{loan_id}/transfers/{transfer_id}/delete",
        token_from=f"/loans/{loan_id}/settings",
    )
    assert response.status_code == 303
    page = owner.client.get(f"/loans/{loan_id}/settings")
    assert "No transfers yet" in page.text


def test_transfer_form_errors_are_visible_and_editing_needs_permission(owner) -> None:
    loan_id = _create(owner, drawdown_mode="tranches")
    response = owner.post(
        f"/loans/{loan_id}/transfers",
        {"date": "2026-01-15", "amount": "10,000.00", "note": "x" * 201},
        token_from=f"/loans/{loan_id}/settings",
    )
    assert response.status_code == 400
    assert "Enter a shorter note." in response.text
    assert 'id="transfer-add-note-error"' in response.text

    _add_transfer(owner, loan_id, date(2026, 1, 15), "10,000.00")
    with owner.db() as session:
        transfer = (
            session.query(LoanTransfer).filter(LoanTransfer.workspace_id == owner.home_id).one()
        )
        transfer_id = transfer.id
    owner.set_admin_role("viewer")
    page = owner.client.get(f"/loans/{loan_id}/settings?edit_transfer={transfer_id}")
    assert page.status_code == 200
    assert f'data-transfer-edit="{transfer_id}"' not in page.text


def test_a_transfer_over_the_principal_keeps_the_form_open_with_an_error(owner) -> None:
    loan_id = _create(owner, drawdown_mode="tranches", principal_original="100,000.00")
    response = owner.post(
        f"/loans/{loan_id}/transfers",
        {"date": "2026-01-15", "amount": "100,000.01", "note": ""},
        token_from=f"/loans/{loan_id}/settings",
    )
    assert response.status_code == 400
    assert "Transfers cannot exceed the loan principal." in response.text
    assert "data-transfer-add" in response.text
    assert "aria-invalid" in response.text


def test_the_wizard_review_does_not_show_a_full_principal_installment_for_tranches(
    owner,
) -> None:
    data = _wizard_data(drawdown_mode="tranches")
    review = None
    for step in (1, 2, 3):
        review = _step(owner, data, step)
        assert review.status_code == 200, review.text
    assert review is not None
    assert "Calculated after the first transfer." in review.text


def test_a_full_loan_can_switch_to_tranches_without_changing_its_balance(owner) -> None:
    loan_id = _create(owner)
    settings = owner.client.get(f"/loans/{loan_id}/settings")
    assert "Track transfers" in settings.text

    response = owner.post(
        f"/loans/{loan_id}/convert-to-tranched",
        token_from=f"/loans/{loan_id}/settings",
    )
    assert response.status_code == 303

    with owner.db() as session:
        loan = session.get_one(Loan, loan_id)
        assert loan.drawdown_mode == "tranches"
        transfer = session.query(LoanTransfer).filter(LoanTransfer.loan_id == loan_id).one()
        assert transfer.date == date(2026, 1, 1)
        assert transfer.amount_minor == 30_000_000

    settings = owner.client.get(f"/loans/{loan_id}/settings")
    assert "Track transfers" not in settings.text
    assert "data-transfer=" in settings.text
    assert "300,000.00" in settings.text


def test_a_viewer_cannot_add_or_convert_transfers(owner) -> None:
    loan_id = _create(owner, drawdown_mode="tranches")
    owner.set_admin_role("viewer")
    page = owner.client.get(f"/loans/{loan_id}/settings")
    assert page.status_code == 200
    assert "data-loan-transfers-settings" in page.text
    assert "data-transfer-add" not in page.text

    owner.set_admin_role("owner")
    full_id = _create(owner, name="Car")
    owner.set_admin_role("viewer")
    page = owner.client.get(f"/loans/{full_id}/settings")
    assert "Track transfers" not in page.text
