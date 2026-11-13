"""The F7.4 loan what-if scenario page."""

from __future__ import annotations

import json
import re
from uuid import UUID

from sqlalchemy import delete

from assetboard.db.models.identity import Workspace
from assetboard.db.models.loans import Loan, LoanRateChange, LoanScenario


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


def _row(number: int, **values: str) -> dict[str, str]:
    row = {
        "name": "",
        "overpayment_mode": "same",
        "overpayment_amount": "",
        "overpayment_strategy": "same",
        "rate_shift": "",
        "rate_shift_from": "",
        "one_off_amount": "",
        "one_off_date": "",
    }
    row.update(values)
    return {f"{key}_{number}": value for key, value in row.items()}


def test_the_page_renders_the_comparison_chart_form_and_detail_link(owner) -> None:
    loan_id = _create(owner)
    page = owner.client.get(f"/loans/{loan_id}/scenarios")
    assert page.status_code == 200
    assert "data-scenario-comparison" in page.text
    assert "Baseline" in page.text and "Current plan" in page.text
    assert page.text.count("data-scenario-form-row=") == 3
    assert "data-scenario-chart" in page.text
    assert "data-saved-scenarios" in page.text
    assert "data-no-saved-scenarios" in page.text
    assert "data-chart-ready" not in page.text
    detail = owner.client.get(f"/loans/{loan_id}")
    assert "data-loan-scenarios" in detail.text
    assert f"/loans/{loan_id}/scenarios" in detail.text


def test_scenario_labels_use_the_ui_locale_not_the_workspace_locale(owner) -> None:
    loan_id = _create(owner)
    with owner.db() as session, session.begin():
        workspace = session.get(Workspace, owner.home_id)
        assert workspace is not None
        workspace.locale = "pl"
    page = owner.client.get(f"/loans/{loan_id}/scenarios")
    assert page.status_code == 200
    assert "Baseline" in page.text
    assert "Scenariusz bazowy" not in page.text


def test_scenario_field_errors_are_marked_on_the_card(owner):
    loan_id = _create(owner)
    page = owner.client.get(f"/loans/{loan_id}/scenarios")
    assert page.status_code == 200
    response = owner.post(
        f"/loans/{loan_id}/scenarios",
        {
            "name_1": "",
            "overpayment_mode_1": "fixed_extra",
            "overpayment_amount_1": "not-a-number",
            "overpayment_strategy_1": "same",
            "rate_shift_1": "",
            "rate_shift_from_1": "",
            "one_off_amount_1": "",
            "one_off_date_1": "",
        },
    )
    assert response.status_code == 400
    assert 'name="overpayment_amount_1"' in response.text
    assert "data-field-error" in response.text


def test_what_if_cards_offer_presets_and_a_clear_action(owner) -> None:
    loan_id = _create(owner)
    page = owner.client.get(f"/loans/{loan_id}/scenarios")
    assert page.status_code == 200
    assert page.text.count("data-scenario-card=") == 3
    assert page.text.count("data-scenario-preset=") == 9
    assert page.text.count("data-scenario-clear>") == 3
    assert "Add 10% of the installment" in page.text
    assert "Raise the rate by 1 pp" in page.text
    assert "One-off 1,000" in page.text
    assert re.search(r'data-scenario-preset="overpayment" data-amount="[0-9.,]+"', page.text)
    assert re.search(
        r'data-scenario-preset="rate" data-shift="1" data-date="\d{4}-\d{2}-\d{2}"',
        page.text,
    )
    assert re.search(
        r'data-scenario-preset="one_off" data-amount="[0-9.,]+" data-date="\d{4}-\d{2}-\d{2}"',
        page.text,
    )
    assert "data-scenario-overpayment-mode" in page.text


def test_saved_scenarios_show_their_delta_against_the_current_plan(owner) -> None:
    loan_id = _create(owner)
    path = f"/loans/{loan_id}/scenarios"
    owner.post(
        path,
        _row(
            1,
            name="Extra",
            overpayment_mode="fixed_extra",
            overpayment_amount="100.00",
        ),
        token_from=path,
    )
    page = owner.client.get(path)
    assert page.status_code == 200
    assert "data-saved-delta>" in page.text
    assert "data-saved-interest-delta=" in page.text
    assert "data-saved-months-delta=" in page.text
    assert "Interest vs the current plan" in page.text
    assert "Saves" in page.text
    assert "sooner" in page.text


def test_an_unfavourable_saved_delta_is_marked(owner) -> None:
    loan_id = _create(
        owner,
        overpayment_mode="fixed_extra",
        overpayment_amount="100.00",
    )
    path = f"/loans/{loan_id}/scenarios"
    owner.post(path, _row(1, name="No extra", overpayment_mode="none"), token_from=path)
    page = owner.client.get(path)
    assert page.status_code == 200
    assert 'data-saved-interest-delta="-' in page.text
    assert "Adds" in page.text
    assert "later" in page.text


def test_a_named_row_saves_and_reuses_the_name(owner) -> None:
    loan_id = _create(owner)
    path = f"/loans/{loan_id}/scenarios"
    first = owner.post(
        path,
        _row(
            1,
            name="Overpay",
            overpayment_mode="fixed_extra",
            overpayment_amount="100.00",
        ),
        token_from=path,
    )
    assert first.status_code == 200
    assert "data-saved-scenario=" in first.text
    assert "data-action-notice" in first.text
    with owner.db() as session:
        rows = list(session.query(LoanScenario).filter_by(loan_id=UUID(loan_id)))
    assert len(rows) == 1
    scenario_id = rows[0].id

    second = owner.post(
        path,
        _row(
            1,
            name="overpay",
            overpayment_mode="fixed_extra",
            overpayment_amount="200.00",
        ),
        token_from=path,
    )
    assert second.status_code == 200
    with owner.db() as session:
        rows = list(session.query(LoanScenario).filter_by(loan_id=UUID(loan_id)))
    assert len(rows) == 1 and rows[0].id == scenario_id
    assert rows[0].overpayment_amount_minor == 20_000
    assert second.text.count("data-saved-scenario=") == 1


def test_an_unnamed_row_is_ephemeral_and_keeps_values_on_error(owner) -> None:
    loan_id = _create(owner)
    path = f"/loans/{loan_id}/scenarios"
    response = owner.post(
        path,
        _row(
            1,
            one_off_amount="5,000.00",
            one_off_date="2026-07-01",
            overpayment_mode="fixed_extra",
            overpayment_amount="50.00",
        ),
        token_from=path,
    )
    assert response.status_code == 200
    assert "What-if 1" in response.text
    assert "data-saved-scenario=" not in response.text
    with owner.db() as session:
        assert session.query(LoanScenario).count() == 0

    invalid = owner.post(
        path,
        _row(1, name="Broken", rate_shift="1.5", rate_shift_from=""),
        token_from=path,
    )
    assert invalid.status_code == 400
    assert "Enter both the rate shift and the date it starts" in invalid.text
    assert 'value="Broken"' in invalid.text
    assert 'value="1.5"' in invalid.text
    with owner.db() as session:
        assert session.query(LoanScenario).count() == 0


def test_a_valid_shift_and_lump_sum_are_compared(owner) -> None:
    loan_id = _create(owner)
    path = f"/loans/{loan_id}/scenarios"
    response = owner.post(
        path,
        _row(
            1,
            name="Rate up",
            rate_shift="1.5",
            rate_shift_from="2026-07-01",
            one_off_amount="5,000.00",
            one_off_date="2026-09-01",
        ),
        token_from=path,
    )
    assert response.status_code == 200
    assert "data-saved-scenario=" in response.text
    assert "+1.5 pp" in response.text
    assert "One-off" in response.text
    assert "data-scenario-row-error" not in response.text


def test_saved_scenarios_are_deleted_with_a_confirmation(owner) -> None:
    loan_id = _create(owner)
    path = f"/loans/{loan_id}/scenarios"
    owner.post(path, _row(1, name="Overpay"), token_from=path)
    with owner.db() as session:
        scenario_id = str(session.query(LoanScenario).filter_by(loan_id=UUID(loan_id)).one().id)
    page = owner.client.get(path)
    assert f"/loans/{loan_id}/scenarios/{scenario_id}/delete" in page.text
    assert "data-confirm-tone" in page.text
    deleted = owner.post(f"/loans/{loan_id}/scenarios/{scenario_id}/delete")
    assert deleted.status_code == 303
    assert deleted.headers["location"].endswith("notice=scenario_deleted#scenarios")
    with owner.db() as session:
        assert session.query(LoanScenario).count() == 0


def test_a_viewer_reads_but_does_not_get_save_or_delete_controls(owner) -> None:
    loan_id = _create(owner)
    path = f"/loans/{loan_id}/scenarios"
    owner.post(path, _row(1, name="Overpay"), token_from=path)
    owner.set_admin_role("viewer")
    page = owner.client.get(path)
    assert page.status_code == 200
    assert "data-scenario-comparison" in page.text
    assert "data-scenario-chart" in page.text
    assert "button" not in page.text.split("data-saved-scenarios")[1].split("</section>")[0]
    assert "data-scenario-name" not in page.text
    assert "data-scenario-form" in page.text
    assert "data-scenario-preset" in page.text


def test_a_broken_loan_shows_a_page_error_and_no_chart(owner) -> None:
    loan_id = _create(owner)
    with owner.db() as session, session.begin():
        session.execute(delete(LoanRateChange).where(LoanRateChange.loan_id == UUID(loan_id)))
    page = owner.client.get(f"/loans/{loan_id}/scenarios")
    assert page.status_code == 200
    assert "data-scenario-page-error" in page.text
    assert "data-scenario-comparison" in page.text
    assert "data-scenario-chart" not in page.text
    assert 'data-chart="line"' not in page.text


def test_the_chart_json_contains_every_series(owner) -> None:
    loan_id = _create(owner)
    path = f"/loans/{loan_id}/scenarios"
    page = owner.post(
        path,
        _row(
            1,
            name="Saved",
            overpayment_mode="fixed_extra",
            overpayment_amount="100.00",
        )
        | _row(2, one_off_amount="1,000.00", one_off_date="2026-06-01"),
        token_from=path,
    )
    assert page.status_code == 200
    source = re.search(
        r'<script type="application/json" id="scenario-balance-data">(.*?)</script>',
        page.text,
        re.DOTALL,
    )
    assert source is not None
    data = json.loads(source.group(1))
    names = [series["name"] for series in data["series"]]
    assert names[:2] == ["Baseline", "Current plan"]
    assert "Saved" in names and "What-if 1" in names
    assert len(data["labels"]) > 1
    assert data["y_labels"]
    assert "data-scenario-balance-points" in page.text
