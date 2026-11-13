"""The F7.3 loan overview, schedule, CSV and chart page."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from uuid import UUID

from sqlalchemy import delete

from assetboard.db.models.loans import Loan, LoanRateChange


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


def test_overview_schedule_and_charts_render(owner):
    loan_id = _create(owner)
    page = owner.client.get(f"/loans/{loan_id}")
    assert page.status_code == 200
    assert "data-loan-overview" in page.text
    assert "Remaining principal" in page.text
    assert "Next payment" in page.text
    assert "data-loan-progress" in page.text
    assert "data-loan-progress-bar" in page.text
    assert "Repaid principal" in page.text
    assert "Original principal" in page.text
    assert "data-loan-schedule" in page.text
    assert page.text.count("data-schedule-row=") == 24
    assert "data-schedule-pages" in page.text and "Page 1 of" in page.text
    assert "data-schedule-csv" in page.text
    assert 'data-chart="line"' in page.text
    assert 'data-chart="loan-payments"' in page.text
    assert "data-loan-balance-points" in page.text
    assert "data-loan-payment-points" in page.text
    assert "data-chart-ready" not in page.text  # drawn by JavaScript


def test_schedule_pagination_and_default_current_page(owner):
    loan_id = _create(owner)
    second = owner.client.get(f"/loans/{loan_id}?schedule_page=2")
    assert second.status_code == 200
    assert "Page 2 of 15" in second.text
    rows = re.findall(r'data-schedule-row="(\d+)"', second.text)
    assert rows[0] == "25" and len(rows) == 24
    fallback = owner.client.get(f"/loans/{loan_id}?schedule_page=999")
    assert "Page 1 of 15" in fallback.text


def test_schedule_year_headers_repeat_across_pages(owner):
    loan_id = _create(owner)
    second = owner.client.get(f"/loans/{loan_id}?schedule_page=2")
    assert 'scope="colgroup"' in second.text
    assert 'data-schedule-year="2028"' in second.text
    assert 'data-schedule-year="2029"' in second.text
    assert second.text.index('data-schedule-year="2028"') < second.text.index(
        'data-schedule-year="2029"'
    )
    first_row = second.text.index('data-schedule-row="25"')
    assert second.text.index('data-schedule-year="2028"') < first_row


def test_planned_view_ignores_actuals_and_pagination_keeps_it(owner):
    loan_id = _create(owner)
    path = f"/loans/{loan_id}/payments"
    recorded = owner.post(
        path,
        {
            "date": "2026-02-01",
            "kind": "installment",
            "total": "100.00",
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
    assert recorded.status_code == 303

    actual = owner.client.get(f"/loans/{loan_id}?schedule_page=1")
    assert "data-schedule-actual" in actual.text
    assert "data-actual-total" in actual.text
    assert "data-schedule-planned" not in actual.text

    planned = owner.client.get(f"/loans/{loan_id}?schedule_page=1&view=planned")
    assert planned.status_code == 200
    assert 'data-schedule-view="planned" aria-current="true"' in planned.text
    assert "data-schedule-planned" in planned.text
    assert "data-schedule-actual" not in planned.text
    assert "data-actual-total" not in planned.text
    assert "data-schedule-csv" in planned.text
    assert re.search(r'href="[^"]*schedule_page=2[^"]*view=planned[^"]*#schedule"', planned.text)


def test_schedule_sticky_columns_are_marked(owner):
    loan_id = _create(owner)
    page = owner.client.get(f"/loans/{loan_id}")
    assert "data-schedule-scroll" in page.text
    assert "sm:sticky sm:left-0" in page.text
    assert "sm:sticky sm:left-12" in page.text
    assert "sm:bg-surface" in page.text


def test_today_and_next_highlights(owner):
    today = owner.app.state.clock.today("UTC")
    today_loan = _create(
        owner,
        name="Today loan",
        origination_date=(today - timedelta(days=40)).isoformat(),
        first_payment_date=today.isoformat(),
        payment_day=str(today.day),
        term_months="12",
    )
    today_page = owner.client.get(f"/loans/{today_loan}")
    assert "data-schedule-today" in today_page.text
    assert today.isoformat() in today_page.text

    next_date = today + timedelta(days=10)
    next_loan = _create(
        owner,
        name="Next loan",
        origination_date=(today - timedelta(days=20)).isoformat(),
        first_payment_date=next_date.isoformat(),
        payment_day=str(next_date.day),
        term_months="12",
    )
    next_page = owner.client.get(f"/loans/{next_loan}")
    assert "data-schedule-next" in next_page.text


def test_csv_download_is_machine_readable(owner):
    loan_id = _create(owner)
    response = owner.client.get(f"/loans/{loan_id}/schedule.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"
    text = response.text
    assert text.startswith(
        "n,date,rate,opening,interest,principal,overpayment,costs,total,closing,currency\r\n"
    )
    assert "\r\n" in text
    lines = text.split("\r\n")
    assert len(lines) == 362  # header + 360 rows + trailing empty split
    assert lines[1].startswith("1,2026-02-01,5.50,300000.00,")


def test_chart_json_has_series_baseline_and_rate_markers(owner):
    loan_id = _create(owner)
    owner.post(
        f"/loans/{loan_id}/rate-changes",
        {"effective_from": "2027-01-01", "rate_percent": "6", "high_rate_confirmed": ""},
    )
    page = owner.client.get(f"/loans/{loan_id}")
    balance = re.search(
        r'<script type="application/json" id="loan-balance-data">(.*?)</script>',
        page.text,
        re.DOTALL,
    )
    payments = re.search(
        r'<script type="application/json" id="loan-payments-data">(.*?)</script>',
        page.text,
        re.DOTALL,
    )
    assert balance is not None and payments is not None
    balance_data = json.loads(balance.group(1))
    payment_data = json.loads(payments.group(1))
    assert len(balance_data["series"]) == 1  # no configured overpayments
    assert balance_data["markers"]
    assert len(payment_data["series"]) == 3
    assert payment_data["markers"]


def test_viewer_sees_the_schedule_and_archived_loan_keeps_it(owner):
    loan_id = _create(owner)
    owner.set_admin_role("viewer")
    viewer_page = owner.client.get(f"/loans/{loan_id}")
    assert viewer_page.status_code == 200
    assert "data-loan-schedule" in viewer_page.text
    assert "Edit terms" not in viewer_page.text
    owner.set_admin_role("owner")
    owner.post(f"/loans/{loan_id}/archive")
    archived = owner.client.get(f"/loans/{loan_id}")
    assert "data-loan-schedule" in archived.text
    assert "archived" in archived.text


def test_schedule_error_warning_and_csv_conflict(owner):
    loan_id = _create(owner)
    with owner.db() as session, session.begin():
        session.execute(delete(LoanRateChange).where(LoanRateChange.loan_id == UUID(loan_id)))
    page = owner.client.get(f"/loans/{loan_id}")
    assert page.status_code == 200
    assert "data-schedule-error" in page.text
    assert "Schedule unavailable" in page.text
    assert "data-loan-schedule" not in page.text
    assert 'data-chart="line"' not in page.text
    csv = owner.client.get(f"/loans/{loan_id}/schedule.csv")
    assert csv.status_code == 409


def test_day_count_can_be_changed_on_the_edit_page(owner):
    loan_id = _create(owner)
    edit = owner.client.get(f"/loans/{loan_id}/edit")
    assert 'name="day_count"' in edit.text
    saved = owner.post(
        f"/loans/{loan_id}/edit",
        {
            "name": "Mortgage",
            "kind": "annuity",
            "principal_original": "300,000.00",
            "origination_date": "2026-01-01",
            "first_payment_date": "2026-02-01",
            "term_months": "360",
            "payment_day": "1",
            "day_count": "actual_365",
            "installment_override": "",
        },
    )
    assert saved.status_code == 303
    with owner.db() as session:
        assert session.get(Loan, UUID(loan_id)).day_count == "actual_365"


def test_chart_json_includes_the_baseline_and_overpayment_series(owner):
    loan_id = _create(
        owner,
        overpayment_mode="fixed_extra",
        overpayment_amount="100.00",
        overpayment_strategy="reduce_term",
    )
    page = owner.client.get(f"/loans/{loan_id}")
    balance = re.search(
        r'<script type="application/json" id="loan-balance-data">(.*?)</script>',
        page.text,
        re.DOTALL,
    )
    payments = re.search(
        r'<script type="application/json" id="loan-payments-data">(.*?)</script>',
        page.text,
        re.DOTALL,
    )
    assert balance is not None and payments is not None
    balance_data = json.loads(balance.group(1))
    payment_data = json.loads(payments.group(1))
    assert len(balance_data["series"]) == 2
    assert balance_data["series"][0]["name"] == "Configured plan"
    assert balance_data["series"][1]["name"] == "Without overpayments"
    assert len(payment_data["series"]) == 4
    assert payment_data["series"][-1]["name"] == "Overpayment"
    assert "Overpayment" in page.text
