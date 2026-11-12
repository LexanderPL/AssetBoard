"""The Loans pages, two-path wizard and reference-rates page (F7.1)."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import select

from assetboard.db.models.loans import (
    Loan,
    LoanBalanceCheckpoint,
    LoanCostComponent,
    LoanOverpayment,
    LoanRateChange,
    ReferenceRate,
    ReferenceRateValue,
)


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


def _step(owner, data: dict[str, str], step: int, *, go: str = "next"):
    return owner.client.post(
        "/loans/new",
        data={"csrf_token": owner.token("/loans/new"), **data, "step": str(step), "go": go},
        follow_redirects=False,
    )


def _create_original(owner, **extra: str) -> str:
    data = _wizard_data(**extra)
    for step in (1, 2, 3):
        response = _step(owner, data, step)
        assert response.status_code == 200, response.text
    saved = _step(owner, data, 4)
    assert saved.status_code == 303, saved.text
    with owner.db() as session:
        return str(session.scalars(select(Loan.id).where(Loan.workspace_id == owner.home_id)).one())


def test_empty_loans_page_has_tabs_and_shared_empty_state(owner):
    page = owner.client.get("/loans")
    assert page.status_code == 200
    assert "data-tab-strip" in page.text
    assert "data-empty-state" in page.text
    assert "No loans yet" in page.text
    assert "data-loans-link" in owner.client.get("/").text


def test_original_path_wizard_keeps_values_and_saves(owner):
    first = owner.client.get("/loans/new")
    assert first.status_code == 200
    assert 'data-loan-wizard="1"' in first.text
    assert "I know the original terms" in first.text

    missing = _step(owner, _wizard_data(name=""), 1)
    assert missing.status_code == 400 and "Enter a name." in missing.text

    second = _step(owner, _wizard_data(), 1)
    assert 'data-loan-wizard="2"' in second.text
    assert 'type="hidden" name="name" value="Mortgage"' in second.text
    assert "Original principal" in second.text

    bad_term = _step(owner, _wizard_data(term_months="0"), 2)
    assert bad_term.status_code == 400 and "term between 1 and 600" in bad_term.text

    third = _step(owner, _wizard_data(), 2)
    assert 'data-loan-wizard="3"' in third.text
    high = _step(owner, _wizard_data(fixed_rate_percent="30"), 3)
    assert high.status_code == 400 and "above 25%" in high.text
    fourth = _step(owner, _wizard_data(high_rate_confirmed="1", fixed_rate_percent="30"), 3)
    assert 'data-loan-wizard="4"' in fourth.text

    saved = _step(
        owner,
        _wizard_data(high_rate_confirmed="1", fixed_rate_percent="30"),
        4,
    )
    assert saved.status_code == 303 and "notice=loan_created" in saved.headers["location"]
    with owner.db() as session:
        loan = session.scalars(select(Loan).where(Loan.workspace_id == owner.home_id)).one()
        assert loan.name == "Mortgage"
        assert loan.currency == "EUR"
        changes = session.scalars(
            select(LoanRateChange).where(LoanRateChange.loan_id == loan.id)
        ).all()
        assert [(row.effective_from, row.rate_bp) for row in changes] == [(date(2026, 1, 1), 3_000)]
        assert (
            session.scalars(
                select(LoanBalanceCheckpoint).where(LoanBalanceCheckpoint.loan_id == loan.id)
            ).all()
            == []
        )


def test_wizard_saves_cost_components_with_effective_dates(owner):
    data = _wizard_data(
        cost_name_1="Property insurance",
        cost_mode_1="fixed",
        cost_fixed_amount_1="75.00",
        cost_effective_from_1="2026-02-01",
        cost_name_2="Life insurance",
        cost_mode_2="percent",
        cost_rate_percent_2="0.25",
    )
    for step in (1, 2, 3):
        assert _step(owner, data, step).status_code == 200
    saved = _step(owner, data, 4)
    assert saved.status_code == 303 and "notice=loan_created" in saved.headers["location"]
    with owner.db() as session:
        loan = session.scalars(select(Loan).where(Loan.workspace_id == owner.home_id)).one()
        components = {
            row.name: row
            for row in session.scalars(
                select(LoanCostComponent).where(LoanCostComponent.loan_id == loan.id)
            )
        }
    fixed = components["Property insurance"]
    assert fixed.mode == "fixed"
    assert fixed.fixed_minor == 7_500
    assert fixed.effective_from == date(2026, 2, 1)
    percent = components["Life insurance"]
    assert percent.mode == "percent"
    assert percent.rate_bp == 25
    assert percent.effective_from == date(2026, 1, 1)


def test_wizard_rejects_a_cost_row_without_a_name_or_amount(owner):
    data = _wizard_data(
        cost_name_1="Insurance",
        cost_mode_1="fixed",
        cost_fixed_amount_1="",
    )
    for step in (1, 2, 3):
        assert _step(owner, data, step).status_code == 200
    response = _step(owner, data, 4)
    assert response.status_code == 400
    assert "Enter a cost name" in response.text
    assert 'value="Insurance"' in response.text
    with owner.db() as session:
        assert session.scalars(select(Loan)).all() == []


def test_loan_detail_renders_a_notice_and_ignores_unknown_codes(owner):
    loan_id = _create_original(owner)
    notice = owner.client.get(f"/loans/{loan_id}?notice=loan_created")
    assert notice.status_code == 200
    assert "Loan saved." in notice.text
    unknown = owner.client.get(f"/loans/{loan_id}?notice=unknown")
    assert unknown.status_code == 200


def test_payment_day_defaults_to_the_first_payment_date(owner):
    loan_id = _create_original(owner, first_payment_date="2026-02-15", payment_day="")
    with owner.db() as session:
        loan = session.get(Loan, UUID(loan_id))
        assert loan is not None
        assert loan.payment_day == 15


def test_rate_change_requires_an_explicit_rate(owner):
    loan_id = _create_original(owner)
    response = owner.post(
        f"/loans/{loan_id}/rate-changes",
        {"effective_from": "2027-01-01", "rate_percent": "", "high_rate_confirmed": ""},
    )
    assert response.status_code == 400
    assert "Enter a rate" in response.text


def test_a_rate_change_cannot_push_a_target_below_the_installment(owner):
    loan_id = _create_original(
        owner,
        overpayment_mode="target_total",
        overpayment_amount="2,000.00",
    )
    response = owner.post(
        f"/loans/{loan_id}/rate-changes",
        {
            "effective_from": "2026-01-15",
            "rate_percent": "30",
            "high_rate_confirmed": "1",
        },
    )
    assert response.status_code == 400
    assert "target total cannot be below" in response.text
    with owner.db() as session:
        assert len(session.scalars(select(LoanRateChange)).all()) == 1


def test_edit_shows_the_regular_overpayment_and_links_to_its_settings(owner):
    loan_id = _create_original(
        owner,
        principal_original="900.00",
        overpayment_mode="target_total",
        overpayment_amount="900.00",
    )
    edit = owner.client.get(f"/loans/{loan_id}/edit")
    assert edit.status_code == 200
    assert "data-edit-overpayment-note" in edit.text
    assert "Target total" in edit.text
    assert "#overpayments" in edit.text

    response = owner.post(
        f"/loans/{loan_id}/edit",
        {
            "name": "Mortgage",
            "kind": "annuity",
            "principal_original": "900,000.00",
            "origination_date": "2026-01-01",
            "first_payment_date": "2026-02-01",
            "term_months": "360",
            "payment_day": "1",
            "installment_override": "7,260.30",
        },
    )
    assert response.status_code == 400
    assert "target total cannot be below" in response.text
    assert "data-target-error-link" in response.text
    assert "#overpayments" in response.text
    assert "data-form-error" in response.text
    with owner.db() as session:
        loan = session.get(Loan, UUID(loan_id))
        assert loan is not None
        assert loan.principal_original_minor == 90_000
        assert loan.installment_override_minor is None


def test_current_balance_wizard_creates_one_checkpoint(owner):
    data = _wizard_data(
        entry_path="current",
        name="Car loan",
        kind="equal_principal",
        principal_original="20,000.00",
        current_balance="15,000.00",
        balance_date="2026-02-01",
        origination_date="",
        first_payment_date="2026-03-01",
        term_months="60",
        payment_day="1",
        fixed_rate_percent="5",
    )
    for step in (1, 2, 3):
        assert _step(owner, data, step).status_code == 200
    saved = _step(owner, data, 4)
    assert saved.status_code == 303
    with owner.db() as session:
        loan = session.scalars(select(Loan).where(Loan.workspace_id == owner.home_id)).one()
        assert loan.origination_date is None
        assert loan.principal_original_minor == 2_000_000
        checkpoints = session.scalars(
            select(LoanBalanceCheckpoint).where(LoanBalanceCheckpoint.loan_id == loan.id)
        ).all()
        assert len(checkpoints) == 1
        assert checkpoints[0].as_of == date(2026, 2, 1)
        assert checkpoints[0].remaining_principal_minor == 1_500_000


def test_detail_supports_rate_cost_and_overpayment_dialogs(owner):
    loan_id = _create_original(owner)
    detail = owner.client.get(f"/loans/{loan_id}")
    assert detail.status_code == 200
    assert "Rate changes" in detail.text and "Cost components" in detail.text
    assert "Overpayments" in detail.text and "Starting balance" not in detail.text

    rate = owner.post(
        f"/loans/{loan_id}/rate-changes",
        {"effective_from": "2027-01-01", "rate_percent": "6", "high_rate_confirmed": ""},
    )
    assert rate.status_code == 303 and "notice=rate_saved" in rate.headers["location"]
    cost = owner.post(
        f"/loans/{loan_id}/costs",
        {
            "name": "Insurance",
            "mode": "fixed",
            "fixed_amount": "25.00",
            "effective_from": "2026-01-01",
            "effective_to": "",
        },
    )
    assert cost.status_code == 303 and "notice=cost_saved" in cost.headers["location"]
    overpayment = owner.post(
        f"/loans/{loan_id}/overpayments",
        {"date": "2026-06-01", "amount": "1,000.00", "note": "Bonus"},
    )
    assert overpayment.status_code == 303
    with owner.db() as session:
        assert session.scalars(select(LoanRateChange)).all()
        component = session.scalars(select(LoanCostComponent)).one()
        assert component.fixed_minor == 2_500
        item = session.scalars(select(LoanOverpayment)).one()
        assert item.amount_minor == 100_000

    edited = owner.post(
        f"/loans/{loan_id}/edit",
        {
            "name": "Renamed mortgage",
            "kind": "annuity",
            "principal_original": "300,000.00",
            "origination_date": "2026-01-01",
            "first_payment_date": "2026-02-01",
            "term_months": "240",
            "payment_day": "1",
            "installment_override": "",
        },
    )
    assert edited.status_code == 303 and "notice=terms_saved" in edited.headers["location"]
    archived = owner.post(f"/loans/{loan_id}/archive")
    assert archived.status_code == 303 and "notice=loan_archived" in archived.headers["location"]
    assert "archived" in owner.client.get(f"/loans/{loan_id}").text
    restored = owner.post(f"/loans/{loan_id}/restore")
    assert restored.status_code == 303 and "notice=loan_restored" in restored.headers["location"]


def test_reference_rates_page_crud(owner):
    page = owner.client.get("/loans/reference-rates")
    assert page.status_code == 200 and "No reference rates yet" in page.text
    created = owner.post("/loans/reference-rates", {"name": "EURIBOR 6M", "notes": "Euro area"})
    assert (
        created.status_code == 303 and "notice=reference_rate_saved" in created.headers["location"]
    )
    with owner.db() as session:
        rate = session.scalars(select(ReferenceRate)).one()
        rate_id = rate.id
    added = owner.post(
        f"/loans/reference-rates/{rate_id}/values",
        {
            "effective_from": "2027-04-01",
            "value_percent": "4.5",
            "high_rate_confirmed": "",
        },
    )
    assert added.status_code == 303
    with owner.db() as session:
        value = session.scalars(select(ReferenceRateValue)).one()
        assert value.value_bp == 450 and value.effective_from == date(2027, 4, 1)
        value_id = value.id
    updated = owner.post(
        f"/loans/reference-rates/{rate_id}/values/{value_id}",
        {
            "effective_from": "2027-05-01",
            "value_percent": "4.75",
            "high_rate_confirmed": "",
        },
    )
    assert updated.status_code == 303
    deleted = owner.post(f"/loans/reference-rates/{rate_id}/values/{value_id}/delete")
    assert (
        deleted.status_code == 303
        and "notice=reference_value_deleted" in deleted.headers["location"]
    )


def test_loan_and_archived_titles_open_the_detail_without_an_actions_column(owner):
    loan_id = _create_original(owner)
    page = owner.client.get("/loans")
    assert f'href="/loans/{loan_id}"' in page.text
    assert "data-open-loan>Mortgage</a>" in page.text
    assert ">Open<" not in page.text
    assert "Actions" not in page.text

    owner.post(f"/loans/{loan_id}/archive")
    archived = owner.client.get("/loans")
    assert "data-open-archived-loan>Mortgage</a>" in archived.text
    assert ">Open<" not in archived.text
    assert "Actions" not in archived.text


def test_viewer_sees_loans_but_not_write_controls(owner):
    _create_original(owner)
    owner.set_admin_role("viewer")
    page = owner.client.get("/loans")
    assert page.status_code == 200
    assert "Add loan" not in page.text
    detail = owner.client.get("/loans/" + page.text.split('data-loan="')[1].split('"')[0])
    assert detail.status_code == 200
    assert "Edit terms" not in detail.text
    post = owner.post(
        "/loans/new",
        _wizard_data() | {"step": "1", "go": "next"},
    )
    assert post.status_code == 403


def test_short_forms_open_from_query_parameters_without_javascript(owner):
    loan_id = _create_original(owner)
    for form, dialog in (
        ("cost", "cost-dialog"),
        ("overpayment", "overpayment-dialog"),
        ("rate", "rate-dialog"),
    ):
        page = owner.client.get(f"/loans/{loan_id}?form={form}")
        assert page.status_code == 200
        assert f'id="{dialog}"' in page.text
        start = page.text.index(f'id="{dialog}"')
        assert " open>" in page.text[start : start + 600]


def test_current_balance_terms_edit_updates_the_checkpoint(owner):
    data = _wizard_data(
        entry_path="current",
        name="Car loan",
        kind="equal_principal",
        principal_original="20,000.00",
        current_balance="15,000.00",
        balance_date="2026-02-01",
        origination_date="",
        first_payment_date="2026-03-01",
        term_months="60",
        payment_day="1",
        fixed_rate_percent="5",
    )
    for step in (1, 2, 3):
        assert _step(owner, data, step).status_code == 200
    saved = _step(owner, data, 4)
    assert saved.status_code == 303
    with owner.db() as session:
        loan = session.scalars(select(Loan).where(Loan.workspace_id == owner.home_id)).one()
        loan_id = loan.id
    edit = owner.client.get(f"/loans/{loan_id}/edit")
    assert edit.status_code == 200 and "Current balance" in edit.text
    updated = owner.post(
        f"/loans/{loan_id}/edit",
        {
            "name": "Car loan",
            "kind": "annuity",
            "principal_original": "20,000.00",
            "current_balance": "14,000.00",
            "balance_date": "2026-02-01",
            "first_payment_date": "2026-03-01",
            "term_months": "59",
            "payment_day": "1",
            "installment_override": "260.00",
        },
    )
    assert updated.status_code == 303
    with owner.db() as session:
        checkpoint = session.scalars(
            select(LoanBalanceCheckpoint).where(LoanBalanceCheckpoint.loan_id == loan_id)
        ).one()
        assert checkpoint.remaining_principal_minor == 1_400_000
        loan = session.get(Loan, loan_id)
        assert loan is not None
        assert loan.term_months == 59 and loan.kind == "annuity"


def test_variable_loan_wizard_uses_a_reference_rate(owner):
    created = owner.post("/loans/reference-rates", {"name": "EURIBOR 6M", "notes": ""})
    assert created.status_code == 303
    with owner.db() as session:
        rate = session.scalars(select(ReferenceRate)).one()
        rate_id = rate.id
    value = owner.post(
        f"/loans/reference-rates/{rate_id}/values",
        {
            "effective_from": "2026-01-01",
            "value_percent": "4",
            "high_rate_confirmed": "",
        },
    )
    assert value.status_code == 303
    data = _wizard_data(
        name="Variable mortgage",
        rate_mode="variable",
        fixed_rate_percent="",
        reference_rate_id=str(rate_id),
        margin_percent="1.5",
    )
    for step in (1, 2, 3):
        assert _step(owner, data, step).status_code == 200
    saved = _step(owner, data, 4)
    assert saved.status_code == 303
    with owner.db() as session:
        loan = session.scalars(select(Loan).where(Loan.workspace_id == owner.home_id)).one()
        assert loan.rate_mode == "variable"
        assert loan.reference_rate_id == rate_id
        assert loan.margin_bp == 150
