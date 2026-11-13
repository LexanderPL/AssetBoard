"""The loan Settings page and the read-only overview split (P0 loan UI restructuring)."""

from __future__ import annotations

import html
from datetime import date

from assetboard.db.models.loans import Loan


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


def _record_payment(owner, loan_id: str, *, day: date, total: str = "100.00") -> None:
    path = f"/loans/{loan_id}/payments"
    response = owner.post(
        path,
        {
            "date": day.isoformat(),
            "kind": "installment",
            "total": total,
            "use_breakdown": "0",
            "principal": "",
            "interest": "",
            "costs": "",
            "overpayment": "",
            "note": "",
            "transaction_id": "",
        },
        token_from=path,
    )
    assert response.status_code == 303


def _add_checkpoint(owner, loan_id: str, *, day: date, balance: str) -> None:
    path = f"/loans/{loan_id}/payments"
    response = owner.post(
        f"/loans/{loan_id}/checkpoints",
        {"as_of": day.isoformat(), "remaining_principal": balance, "note": ""},
        token_from=path,
    )
    assert response.status_code == 303


def test_settings_page_is_one_card_per_concern_without_the_schedule(owner) -> None:
    loan_id = _create(owner)
    page = owner.client.get(f"/loans/{loan_id}/settings")
    assert page.status_code == 200
    assert "data-loan-settings-page" in page.text
    for marker in (
        "data-loan-terms",
        "data-loan-property-settings",
        "data-loan-rates-settings",
        "data-loan-costs-settings",
        "data-loan-overpayments-settings",
        "data-loan-upcoming-settings",
        "data-loan-archive-settings",
    ):
        assert marker in page.text
    order = [
        page.text.index('id="terms"'),
        page.text.index('id="property"'),
        page.text.index('id="rates"'),
        page.text.index('id="costs"'),
        page.text.index('id="overpayments"'),
        page.text.index('id="upcoming"'),
        page.text.index('id="archive"'),
    ]
    assert order == sorted(order)
    assert "data-loan-schedule" not in page.text
    assert 'data-chart="line"' not in page.text
    assert "data-loan-overview" not in page.text
    assert f"/loans/{loan_id}/edit" in page.text
    assert "data-edit-terms" in page.text


def test_overview_is_read_only_insight_and_keeps_the_schedule(owner) -> None:
    loan_id = _create(owner)
    page = owner.client.get(f"/loans/{loan_id}")
    assert page.status_code == 200
    assert "data-loan-overview" in page.text
    assert "data-loan-progress-bar" in page.text
    assert "data-loan-schedule" not in page.text
    assert 'data-chart="line"' not in page.text
    schedule = owner.client.get(f"/loans/{loan_id}/schedule")
    assert "data-loan-schedule" in schedule.text
    assert 'data-chart="line"' in schedule.text
    for marker in (
        'id="rate-dialog"',
        'id="cost-dialog"',
        'id="overpayment-dialog"',
        'name="property_item_id"',
        "data-loan-property-settings",
        "data-loan-upcoming-settings",
        "data-loan-archive-settings",
        "Save overpayment settings",
        "Archive loan",
    ):
        assert marker not in page.text


def test_cost_dialog_hides_the_unused_amount_field(owner) -> None:
    loan_id = _create(owner)
    page = owner.client.get(f"/loans/{loan_id}/settings?form=cost")
    assert page.status_code == 200
    assert 'id="mode"' in page.text
    assert 'data-hidden-when="mode=fixed"' in page.text
    assert 'data-hidden-when="mode=percent"' in page.text


def test_viewer_reads_settings_but_sees_no_configuration_controls(owner) -> None:
    loan_id = _create(owner)
    owner.set_admin_role("viewer")
    page = owner.client.get(f"/loans/{loan_id}/settings")
    assert page.status_code == 200
    assert "data-loan-settings-page" in page.text
    assert "Rate changes" in page.text
    for marker in (
        "data-edit-terms",
        "Create net-worth item",
        "Add rate change",
        "Add cost",
        "Add one-off overpayment",
        "Save overpayment settings",
        "Generate occurrences",
        "Record posted installments automatically",
        "data-loan-archive-settings",
    ):
        assert marker not in page.text


def test_configuration_writes_redirect_to_settings_with_anchors(owner) -> None:
    loan_id = _create(owner)
    path = f"/loans/{loan_id}/settings"
    rate = owner.post(
        f"/loans/{loan_id}/rate-changes",
        {"effective_from": "2027-01-01", "rate_percent": "6", "high_rate_confirmed": ""},
        token_from=path,
    )
    assert rate.status_code == 303
    assert rate.headers["location"].startswith(f"/loans/{loan_id}/settings?notice=rate_saved")
    assert rate.headers["location"].endswith("#rates")

    cost = owner.post(
        f"/loans/{loan_id}/costs",
        {
            "name": "Insurance",
            "mode": "fixed",
            "fixed_amount": "25.00",
            "effective_from": "2026-01-01",
            "effective_to": "",
        },
        token_from=path,
    )
    assert cost.status_code == 303
    assert cost.headers["location"].startswith(f"/loans/{loan_id}/settings?notice=cost_saved")
    assert cost.headers["location"].endswith("#costs")

    overpayment = owner.post(
        f"/loans/{loan_id}/overpayments",
        {"date": "2026-06-01", "amount": "1,000.00", "note": "Bonus"},
        token_from=path,
    )
    assert overpayment.status_code == 303
    assert overpayment.headers["location"].startswith(
        f"/loans/{loan_id}/settings?notice=overpayment_item_saved"
    )
    assert overpayment.headers["location"].endswith("#overpayments")

    settings = owner.post(
        f"/loans/{loan_id}/overpayment-settings",
        {
            "overpayment_mode": "fixed_extra",
            "overpayment_amount": "100.00",
            "overpayment_strategy": "reduce_term",
        },
        token_from=path,
    )
    assert settings.status_code == 303
    assert settings.headers["location"].startswith(
        f"/loans/{loan_id}/settings?notice=overpayment_saved"
    )
    assert settings.headers["location"].endswith("#overpayments")

    archived = owner.post(f"/loans/{loan_id}/archive", token_from=path)
    assert archived.status_code == 303
    assert archived.headers["location"].endswith("#archive")
    restored = owner.post(f"/loans/{loan_id}/restore", token_from=path)
    assert restored.status_code == 303
    assert restored.headers["location"].endswith("#archive")


def test_invalid_configuration_keeps_the_settings_dialog_open(owner) -> None:
    loan_id = _create(owner)
    path = f"/loans/{loan_id}/settings"
    response = owner.post(
        f"/loans/{loan_id}/rate-changes",
        {"effective_from": "2027-01-01", "rate_percent": "", "high_rate_confirmed": ""},
        token_from=path,
    )
    assert response.status_code == 400
    assert "Enter a rate" in response.text
    assert "data-loan-settings-page" in response.text
    start = response.text.index('id="rate-dialog"')
    assert " open>" in response.text[start : start + 700]


def test_high_rate_confirmation_only_appears_when_needed(owner) -> None:
    loan_id = _create(owner)
    page = owner.client.get(f"/loans/{loan_id}/settings?form=rate")
    assert page.status_code == 200
    assert "I understand this rate is above 25%" not in page.text
    path = f"/loans/{loan_id}/settings"
    response = owner.post(
        f"/loans/{loan_id}/rate-changes",
        {"effective_from": "2027-01-01", "rate_percent": "26", "high_rate_confirmed": ""},
        token_from=path,
    )
    assert response.status_code == 400
    assert "I understand this rate is above 25%" in response.text
    saved = owner.post(
        f"/loans/{loan_id}/rate-changes",
        {"effective_from": "2027-01-01", "rate_percent": "26", "high_rate_confirmed": "1"},
        token_from=path,
    )
    assert saved.status_code == 303


def test_invalid_settings_fields_are_marked_next_to_the_input(owner) -> None:
    loan_id = _create(owner)
    path = f"/loans/{loan_id}/settings"
    rate = owner.post(
        f"/loans/{loan_id}/rate-changes",
        {"effective_from": "2027-01-01", "rate_percent": "101", "high_rate_confirmed": "1"},
        token_from=path,
    )
    assert rate.status_code == 400
    assert 'data-field-error="rate_percent"' in rate.text
    assert 'aria-describedby="settings-rate-rate_percent-error"' in rate.text
    assert "data-action-error" not in rate.text

    cost = owner.post(
        f"/loans/{loan_id}/costs",
        {
            "name": "Insurance",
            "mode": "fixed",
            "fixed_amount": "",
            "effective_from": "2026-01-01",
            "effective_to": "",
        },
        token_from=path,
    )
    assert cost.status_code == 400
    assert 'data-field-error="fixed_amount"' in cost.text
    assert 'aria-describedby="settings-cost-fixed_amount-error"' in cost.text

    overpayment = owner.post(
        f"/loans/{loan_id}/overpayments",
        {"date": "2026-06-01", "amount": "", "note": ""},
        token_from=path,
    )
    assert overpayment.status_code == 400
    assert 'data-field-error="amount"' in overpayment.text
    assert 'aria-describedby="settings-overpayment-amount-error"' in overpayment.text

    settings = owner.post(
        f"/loans/{loan_id}/overpayment-settings",
        {
            "overpayment_mode": "fixed_extra",
            "overpayment_amount": "",
            "overpayment_strategy": "reduce_term",
        },
        token_from=path,
    )
    assert settings.status_code == 303
    assert "field=overpayment_amount" in settings.headers["location"]
    marked = owner.client.get(settings.headers["location"])
    assert 'data-field-error="overpayment_amount"' in marked.text
    assert 'aria-describedby="settings-overpayment_amount-error"' in marked.text
    assert "data-action-error" not in marked.text


def test_terms_edit_uses_settings_for_back_cancel_and_save(owner) -> None:
    loan_id = _create(owner)
    edit = owner.client.get(f"/loans/{loan_id}/edit")
    assert edit.status_code == 200
    assert f"/loans/{loan_id}/settings#terms" in edit.text
    assert "Back to Settings" in edit.text
    saved = owner.post(
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
        token_from=f"/loans/{loan_id}/edit",
    )
    assert saved.status_code == 303
    assert saved.headers["location"] == f"/loans/{loan_id}/settings?notice=terms_saved#terms"


def test_quick_record_link_prefills_the_payment_dialog(owner) -> None:
    loan_id = _create(owner)
    detail = owner.client.get(f"/loans/{loan_id}")
    assert "data-quick-record" in detail.text
    assert "payment_date=" in detail.text
    assert "payment_total=" in detail.text
    link = detail.text.split("data-quick-record", 1)[0].rsplit('href="', 1)[1].split('"', 1)[0]
    payments = owner.client.get(html.unescape(link))
    assert payments.status_code == 200
    next_date = html.unescape(link).split("payment_date=", 1)[1].split("&", 1)[0]
    assert f'value="{next_date}"' in payments.text
    assert 'name="total" value="' in payments.text
    assert "data-dialog-modal" in payments.text

    today = owner.app.state.clock.today("UTC").isoformat()
    fallback = owner.client.get(
        f"/loans/{loan_id}/payments?form=payment&payment_date=not-a-date&payment_total=nonsense"
    )
    assert fallback.status_code == 200
    assert f'value="{today}"' in fallback.text


def test_plan_vs_actual_without_actuals_payments_and_checkpoints(owner) -> None:
    loan_id = _create(owner)
    empty = owner.client.get(f"/loans/{loan_id}")
    assert 'data-plan-status="none"' in empty.text
    assert "No actual payments recorded yet." in empty.text

    _record_payment(owner, loan_id, day=date(2026, 2, 1))
    with_payment = owner.client.get(f"/loans/{loan_id}")
    assert "data-plan-last-payment" in with_payment.text
    assert "100.00" in with_payment.text
    assert 'data-plan-status="none"' in with_payment.text

    _add_checkpoint(owner, loan_id, day=date(2026, 2, 1), balance="250,000.00")
    ahead = owner.client.get(f"/loans/{loan_id}")
    assert 'data-plan-status="ahead"' in ahead.text
    assert "Ahead of the projected balance" in ahead.text
    assert "data-plan-checkpoint" in ahead.text


def test_plan_vs_actual_is_behind_when_the_checkpoint_is_higher(owner) -> None:
    loan_id = _create(owner)
    _add_checkpoint(owner, loan_id, day=date(2026, 2, 1), balance="310,000.00")
    page = owner.client.get(f"/loans/{loan_id}")
    assert 'data-plan-status="behind"' in page.text
    assert "Behind the projected balance" in page.text


def test_plan_vs_actual_is_on_track_when_the_checkpoint_matches(owner) -> None:
    loan_id = _create(owner)
    _add_checkpoint(owner, loan_id, day=date(2026, 1, 1), balance="300,000.00")
    page = owner.client.get(f"/loans/{loan_id}")
    assert 'data-plan-status="on_track"' in page.text
    assert "On track with the projected balance" in page.text


def test_overview_property_card_is_read_only_with_a_manage_link(owner) -> None:
    loan_id = _create(owner)
    page = owner.client.get(f"/loans/{loan_id}")
    assert "data-loan-property-summary" in page.text
    assert "data-manage-property" in page.text
    assert f"/loans/{loan_id}/settings#property" in page.text
    assert 'name="property_item_id"' not in page.text
    assert "active real-estate asset item" not in page.text
    settings = owner.client.get(f"/loans/{loan_id}/settings")
    assert f"/loans/{loan_id}/settings#property" in settings.text
    assert "active real-estate asset item" in settings.text


def test_payment_and_checkpoint_dialogs_stay_on_the_payments_page(owner) -> None:
    loan_id = _create(owner)
    for form, anchor in (("payment", "payment-dialog"), ("checkpoint", "checkpoint-dialog")):
        response = owner.client.get(
            f"/loans/{loan_id}/settings?form={form}", follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"] == (f"/loans/{loan_id}/payments?form={form}#{anchor}")
    settings = owner.client.get(f"/loans/{loan_id}/settings")
    assert 'id="payment-dialog"' not in settings.text
    assert 'id="checkpoint-dialog"' not in settings.text
