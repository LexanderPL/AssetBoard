"""The F8.10 one-plan view page: read-only results, values, adjustments and controls."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import event, func, select, update

from assetboard.db.models.fire import FireEvent, FireScenario
from assetboard.db.models.identity import AuditEvent, Workspace
from tests.fire_helpers import create_plan, freeze_clock


def _create(owner, **overrides: str) -> UUID:
    return create_plan(owner, **overrides)


def _snapshot(owner, plan_id: UUID) -> tuple[Any, tuple[UUID, ...], int]:
    with owner.db() as session:
        row = session.get_one(FireScenario, plan_id)
        events = tuple(
            session.scalars(
                select(FireEvent.id)
                .where(FireEvent.scenario_id == plan_id)
                .order_by(FireEvent.year, FireEvent.id)
            ).all()
        )
        audits = session.scalar(select(func.count()).select_from(AuditEvent))
        return row.updated_at, events, audits or 0


def test_the_view_is_read_only_and_shows_results_values_and_adjustments(owner) -> None:
    _create(owner)
    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert "data-fire-view" in page.text
    assert "data-fire-results" in page.text
    assert "data-fire-overview" not in page.text
    assert "First year " not in page.text
    assert "data-fire-warnings" not in page.text
    assert "data-fire-metrics" in page.text
    assert "data-fire-table" in page.text
    assert "data-fire-plan-values" in page.text
    assert "data-fire-adjustments-view" in page.text
    assert "data-fire-loans" in page.text
    assert "data-fire-sensitivity" in page.text
    assert "data-fire-edit" in page.text
    assert "data-fire-reset" in page.text
    # The obsolete plan overview panel and the removed CSV action are gone.
    assert "data-fire-csv" not in page.text
    assert "Download CSV" not in page.text
    # No editor at all: no form, no inputs, no preview hooks, no suggestion cards.
    assert "data-fire-form" not in page.text
    assert "data-fire-preview" not in page.text
    assert "hx-post=" not in page.text
    assert 'name="income_manual"' not in page.text
    assert 'name="birth_year"' not in page.text
    assert "data-fire-suggestion" not in page.text
    assert "data-fire-edit-page" not in page.text


def test_a_current_year_plan_has_no_living_plan_review(owner) -> None:
    freeze_clock(owner, "2026-01-01T00:00:00+00:00")
    _create(owner)
    view = owner.client.get("/fire")
    assert view.status_code == 200
    assert "data-fire-year-review" not in view.text
    edit = owner.client.get("/fire/edit")
    assert edit.status_code == 200
    assert "data-fire-year-review" not in edit.text


def test_a_previous_year_plan_shows_the_living_plan_banner(owner) -> None:
    freeze_clock(owner, "2026-01-01T00:00:00+00:00")
    _create(owner)
    freeze_clock(owner, "2027-01-01T00:00:00+00:00")

    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert "data-fire-year-review" in page.text
    assert "This plan starts in 2027" in page.text
    assert "values last reviewed for 2026" in page.text
    assert "Review the plan inputs" in page.text

    owner.set_admin_role("editor")
    try:
        editor = owner.client.get("/fire")
        assert "data-fire-year-review" in editor.text
        assert "Review the plan inputs" in editor.text
    finally:
        owner.set_admin_role("owner")

    owner.set_admin_role("viewer")
    try:
        viewer = owner.client.get("/fire")
        assert "data-fire-year-review" in viewer.text
        assert "values last reviewed for 2026" in viewer.text
        assert "Review the plan inputs" not in viewer.text
    finally:
        owner.set_admin_role("owner")


def test_the_living_plan_review_uses_the_workspace_timezone(owner) -> None:
    freeze_clock(owner, "2027-01-01T00:30:00+00:00")
    plan_id = _create(owner)
    with owner.db() as session, session.begin():
        session.execute(
            update(FireScenario)
            .where(FireScenario.id == plan_id)
            .values(updated_at=datetime(2026, 12, 31, 23, 30, tzinfo=UTC))
        )
    # 23:30 UTC is still 2026 in UTC, so the UTC workspace shows the review.
    utc_page = owner.client.get("/fire")
    assert "data-fire-year-review" in utc_page.text
    assert "values last reviewed for 2026" in utc_page.text

    # At UTC+14 the same instant is already 2027, the current workspace year: no review.
    with owner.db() as session, session.begin():
        session.execute(
            update(Workspace)
            .where(Workspace.id == owner.home_id)
            .values(timezone="Pacific/Kiritimati")
        )
    local_page = owner.client.get("/fire")
    assert "data-fire-year-review" not in local_page.text


def test_plan_warnings_are_a_compact_stack_without_the_old_overview(owner) -> None:
    plan_id = _create(owner)
    with owner.db() as session, session.begin():
        session.get_one(FireScenario, plan_id).horizon_age = 41
    view = owner.client.get("/fire").text
    assert "data-fire-overview" not in view
    assert "data-fire-warnings" in view
    assert "data-fire-horizon-warning" in view
    assert "data-fire-income-warning" not in view
    assert view.index("data-fire-warnings") < view.index("data-fire-results")

    edit = owner.client.get("/fire/edit").text
    assert "data-fire-overview" not in edit
    assert "data-fire-warnings" in edit
    assert "data-fire-horizon-warning" in edit
    assert "data-fire-income-warning" not in edit
    assert edit.index("data-fire-warnings") < edit.index("data-fire-form")

    # Income already stopped at the current age: the compact stack names that, not the horizon.
    with owner.db() as session, session.begin():
        session.get_one(FireScenario, plan_id).horizon_age = 90
        session.get_one(FireScenario, plan_id).stop_working_age = 41
    view = owner.client.get("/fire").text
    assert "data-fire-warnings" in view
    assert "data-fire-income-warning" in view
    assert "data-fire-horizon-warning" not in view
    assert "data-fire-stop-warning" not in view
    edit = owner.client.get("/fire/edit").text
    assert "data-fire-income-warning" in edit
    assert "data-fire-horizon-warning" not in edit

    # Income stops after the horizon: only the stop-after-horizon warning is shown.
    with owner.db() as session, session.begin():
        session.get_one(FireScenario, plan_id).horizon_age = 60
        session.get_one(FireScenario, plan_id).stop_working_age = 65
    view = owner.client.get("/fire").text
    assert "data-fire-warnings" in view
    assert "data-fire-stop-warning" in view
    assert "data-fire-income-warning" not in view
    assert "data-fire-horizon-warning" not in view
    edit = owner.client.get("/fire/edit").text
    assert "data-fire-stop-warning" in edit
    assert "data-fire-income-warning" not in edit


def test_the_view_uses_nominal_values_only(owner) -> None:
    _create(owner)
    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert "data-fire-basis" not in page.text
    assert "?basis=" not in page.text
    assert 'name="basis"' not in page.text
    assert "Money basis" not in page.text
    assert "Today's money" not in page.text
    assert "Each year's money" not in page.text
    ignored = owner.client.get("/fire?basis=real")
    assert ignored.status_code == 200
    assert "data-fire-basis" not in ignored.text
    assert "Today's money" not in ignored.text
    assert "Plan progress" in ignored.text


def test_the_view_plan_values_use_ledger_based_tax_copy(owner) -> None:
    _create(owner)
    text = owner.client.get("/fire").text
    assert "Income and expenses are the amounts recorded in your ledger" in text
    assert "Income may be gross" in text
    assert "taxes you actually pay" in text
    assert "does not model taxes separately" in text
    assert "after-tax" not in text
    assert "after tax" not in text


def test_fire_templates_have_no_real_money_or_spreadsheet_wording() -> None:
    from pathlib import Path

    templates = Path(__file__).resolve().parents[2] / "src/assetboard/web/templates/fire"
    text = "\n".join(path.read_text(encoding="utf-8") for path in templates.glob("*.html"))
    for phrase in (
        "Sheet progress",
        "Today's-money progress",
        "Today's money",
        "today's money",
        "Each year's money",
        "Money basis",
        "spreadsheet",
        "real_income",
        "real_expenses",
        "real_loan_payment",
        "real_net",
        "real_gain",
        "real_cash",
        "real_net_worth",
        "real_networth_at_fire",
        "fire_number_today",
        "data-fire-basis",
        'name="basis"',
        "after-tax",
        "after tax",
        "A suggestion never fills",
        "Required. Enter 0",
        "Required. Add it to your profile",
    ):
        assert phrase not in text
    assert "real return" not in text.lower()


def test_the_metrics_show_fire_number_with_year_and_plan_progress(owner) -> None:
    _create(owner)
    page = owner.client.get("/fire")
    assert 'data-fire-number-value="' in page.text
    assert re.search(r'data-fire-number-year="\d{4}"', page.text)
    assert 'data-fire-progress="plan"' in page.text
    assert "data-fire-progress-bar" in page.text
    assert "data-fire-progress-starting" in page.text
    assert "data-fire-progress-target" in page.text
    assert "Starting portfolio" in page.text
    assert "Net worth at FIRE" in page.text
    assert "h-8 w-full" in page.text
    assert "fill-success" in page.text
    assert 'data-fire-progress="today"' not in page.text
    assert 'data-fire-progress="sheet"' not in page.text
    # The default plan is at 93.02%: a below-100 value must not be described as capped, and the
    # visible percentage plus both legend amounts are the projection's values.
    assert 'data-fire-progress="plan" data-progress-bp="9302"' in page.text
    assert "data-fire-progress-label>93.02%<" in page.text
    assert 'aria-valuenow="93"' in page.text
    assert 'aria-valuetext="93.02%"' in page.text
    assert 'width="93"' in page.text
    assert "data-fire-progress-starting>€1,000,000.00<" in page.text
    assert "data-fire-progress-target>€1,075,000.00<" in page.text
    assert "Bar capped at 100%" not in page.text
    assert "FIRE number" in page.text
    assert "Plan progress" in page.text
    assert "projected nominal net worth at FIRE" in page.text
    assert "All amounts are nominal" in page.text
    assert "Nominal amounts. The first age after income stops" in page.text
    assert "Sheet progress" not in page.text
    assert "Today's-money progress" not in page.text


def test_a_never_fire_plan_shows_dashes_and_the_reason(owner) -> None:
    _create(owner, income_manual="0", expenses_manual="1000000", portfolio_manual="0")
    page = owner.client.get("/fire")
    assert 'data-fire-number-value=""' in page.text
    assert 'data-fire-progress="plan" data-progress-bp=""' in page.text
    assert "data-fire-number-note" not in page.text
    assert "data-fire-progress-note" in page.text
    assert "Not defined: this plan does not reach FIRE." in page.text
    assert 'aria-valuetext="Not defined"' in page.text
    assert 'aria-valuenow="0"' in page.text
    assert "data-fire-progress-fill" not in page.text


def test_plan_progress_without_a_positive_target_is_muted_and_explained(owner) -> None:
    # FIRE is reached with a positive rate but net worth at FIRE is zero: the percentage is not
    # defined. The bar is muted, has no fill and the note names the non-positive target.
    _create(owner, rule="swr", expenses_manual="0", portfolio_manual="0")
    page = owner.client.get("/fire")
    assert 'data-fire-progress="plan" data-progress-bp=""' in page.text
    assert "data-fire-progress-label>—<" in page.text
    assert 'aria-valuenow="0"' in page.text
    assert 'aria-valuetext="Not defined"' in page.text
    assert "data-fire-progress-fill" not in page.text
    assert "opacity-60" in page.text
    assert "data-fire-progress-note" in page.text
    assert "Not defined: net worth at FIRE is not positive." in page.text
    assert "Not defined: this plan does not reach FIRE." not in page.text


def test_plan_progress_shows_the_exact_percentage_and_caps_the_bar(owner) -> None:
    plan_id = _create(
        owner,
        income_manual="0",
        expenses_manual="1000000",
        portfolio_manual="10000000",
    )
    with owner.db() as session, session.begin():
        session.add_all(
            [
                FireEvent(
                    workspace_id=owner.home_id,
                    scenario_id=plan_id,
                    year=2027,
                    kind="cash",
                    amount_minor=-800_000_000,
                    label="Large one-off expense",
                ),
                FireEvent(
                    workspace_id=owner.home_id,
                    scenario_id=plan_id,
                    year=2028,
                    kind="expense_change",
                    amount_minor=-90_000_000,
                    label="Lower expenses",
                ),
            ]
        )
    page = owner.client.get("/fire")
    # Starting portfolio / net worth at FIRE = 10,000,000 / 2,616,031.25 = 382.26%.
    assert 'data-fire-progress="plan" data-progress-bp="38226"' in page.text
    assert "data-fire-progress-label>382.26%<" in page.text
    assert 'aria-valuetext="382.26%"' in page.text
    assert 'aria-valuenow="100"' in page.text
    assert 'width="100"' in page.text
    assert 'height="32"' in page.text
    assert "data-fire-progress-starting>€10,000,000.00<" in page.text
    assert "data-fire-progress-target>€2,616,031.25<" in page.text
    assert "Bar capped at 100%" not in page.text


def test_the_view_has_no_real_money_or_spreadsheet_wording(owner) -> None:
    _create(owner)
    page = owner.client.get("/fire")
    text = page.text
    for phrase in (
        "Sheet progress",
        "Today's-money progress",
        "Today's money",
        "today's money",
        "Each year's money",
        "Money basis",
        "spreadsheet",
        "real_income",
        "real_expenses",
        "real_loan_payment",
        "real_net_worth",
        "real_networth_at_fire",
        "fire_number_today",
    ):
        assert phrase not in text


def test_the_metrics_and_progress_are_translated(owner) -> None:
    from assetboard.db.models.identity import User, Workspace

    _create(owner)
    with owner.db() as session, session.begin():
        session.get_one(Workspace, owner.home_id).locale = "pl"
        session.get_one(User, owner.admin_id).locale = "pl"
    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert "Kwota FIRE" in page.text
    assert "Postęp planu" in page.text
    assert "Sheet progress" not in page.text
    assert "Postęp arkusza" not in page.text


def test_the_view_writes_nothing(owner) -> None:
    plan_id = _create(owner)
    before = _snapshot(owner, plan_id)
    for path in ("/fire", "/fire?basis=real", "/fire?sensitivity=1"):
        assert owner.client.get(path).status_code == 200
    assert _snapshot(owner, plan_id) == before


def test_a_viewer_sees_no_edit_or_reset_and_no_form(owner) -> None:
    _create(owner)
    owner.set_admin_role("viewer")
    try:
        page = owner.client.get("/fire")
        assert page.status_code == 200
        assert "data-fire-view" in page.text
        assert "data-fire-plan-values" in page.text
        assert "data-fire-edit" not in page.text
        assert "data-fire-reset" not in page.text
        assert "data-fire-form" not in page.text
        assert 'name="archived_remove' not in page.text
        assert "data-adjustment-remove" not in page.text
    finally:
        owner.set_admin_role("owner")


def test_a_blocked_projection_hides_results_and_the_removed_csv_is_gone(owner) -> None:
    plan_id = _create(owner)
    with owner.db() as session:
        session.get_one(FireScenario, plan_id).currency = "USD"
        session.commit()
    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert 'data-fire-blocked="plan_currency_mismatch"' in page.text
    assert "data-fire-metrics" not in page.text
    assert "data-fire-csv" not in page.text
    assert "data-fire-blocked-reset" in page.text
    assert owner.client.get("/fire/results.csv").status_code == 404


def test_archived_adjustments_are_shown_read_only(owner) -> None:
    plan_id = _create(owner)
    with owner.db() as session, session.begin():
        session.add(
            FireEvent(
                workspace_id=owner.home_id,
                scenario_id=plan_id,
                year=2020,
                kind="cash",
                amount_minor=100_000,
                label="Old bonus",
            )
        )
    page = owner.client.get("/fire")
    assert "Archived adjustments" in page.text
    assert "Old bonus" in page.text
    assert "data-fire-archived-adjustments" in page.text
    assert "data-fire-adjustments-empty" in page.text
    assert 'name="archived_remove' not in page.text
    assert 'name="event_' not in page.text


def test_archived_adjustments_show_their_reason(owner) -> None:
    plan_id = _create(owner)
    with owner.db() as session, session.begin():
        session.add_all(
            (
                FireEvent(
                    workspace_id=owner.home_id,
                    scenario_id=plan_id,
                    year=2020,
                    kind="cash",
                    amount_minor=100_000,
                    label="Past bonus",
                ),
                FireEvent(
                    workspace_id=owner.home_id,
                    scenario_id=plan_id,
                    year=2080,
                    kind="cash",
                    amount_minor=200_000,
                    label="After horizon",
                ),
                FireEvent(
                    workspace_id=owner.home_id,
                    scenario_id=plan_id,
                    year=2026,
                    kind="saving_override",
                    amount_minor=300_000,
                    label="Current override",
                ),
            )
        )
    page = owner.client.get("/fire").text
    assert "Before the current year" in page
    assert "After the plan" in page and "last year" in page
    assert "Saving override in the current year" in page


def test_the_view_has_no_suggestion_cards(owner) -> None:
    _create(owner)
    page = owner.client.get("/fire")
    assert "data-use-suggestion" not in page.text
    assert "data-suggested-minor" not in page.text
    assert "data-fire-suggestion" not in page.text


def test_the_view_markup_is_csp_safe(owner) -> None:
    _create(owner)
    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert not re.search(r"<script(?![^>]*\bsrc=)", page.text)
    assert "onclick=" not in page.text


def test_the_view_loads_no_editor_javascript(owner) -> None:
    _create(owner)
    page = owner.client.get("/fire")
    assert "js/fire.js" not in page.text
    assert "vendor/echarts.min.js" in page.text


def test_the_removed_csv_is_not_found_and_writes_nothing(owner) -> None:
    plan_id = _create(owner)
    before = _snapshot(owner, plan_id)
    response = owner.client.get("/fire/results.csv")
    assert response.status_code == 404
    assert _snapshot(owner, plan_id) == before
    # The old id-based URL is not a redirect any more either, and a viewer gets the same 404.
    legacy = owner.client.get(f"/fire/{plan_id}/results.csv")
    assert legacy.status_code == 404
    assert _snapshot(owner, plan_id) == before
    owner.set_admin_role("viewer")
    try:
        assert owner.client.get("/fire/results.csv").status_code == 404
        assert owner.client.get(f"/fire/{plan_id}/results.csv").status_code == 404
    finally:
        owner.set_admin_role("owner")
    assert _snapshot(owner, plan_id) == before


def test_a_currency_mismatch_hides_the_review_banner_and_editor_link(owner) -> None:
    plan_id = _create(owner)
    with owner.db() as session:
        row = session.get_one(FireScenario, plan_id)
        row.currency = "USD"
        row.needs_review = True
        session.commit()
    page = owner.client.get("/fire")
    assert 'data-fire-blocked="plan_currency_mismatch"' in page.text
    assert "data-fire-needs-review" not in page.text
    assert "data-fire-edit" not in page.text
    assert "data-fire-reset" in page.text
    assert "/fire/edit" not in page.text


def test_the_view_query_count_does_not_grow_with_adjustments(owner) -> None:
    plan_id = _create(owner)
    seen = 0

    def before(*_args: object) -> None:
        nonlocal seen
        seen += 1

    def count() -> int:
        nonlocal seen
        seen = 0
        engine = owner.app.state.database_engine
        event.listen(engine, "before_cursor_execute", before)
        try:
            response = owner.client.get("/fire")
            assert response.status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", before)
        return seen

    quiet = count()
    with owner.db() as session, session.begin():
        for index in range(50):
            session.add(
                FireEvent(
                    workspace_id=owner.home_id,
                    scenario_id=plan_id,
                    year=2027 + (index % 40),
                    kind="cash",
                    amount_minor=index + 1,
                    label=f"Event {index}",
                )
            )
    loud = count()
    assert loud == quiet
    assert quiet <= 20


def test_a_viewer_sees_the_needs_review_banner_without_a_link(owner) -> None:
    plan_id = _create(owner)
    with owner.db() as session:
        session.get_one(FireScenario, plan_id).needs_review = True
        session.commit()
    owner.set_admin_role("viewer")
    try:
        page = owner.client.get("/fire")
        assert "data-fire-needs-review" in page.text
        assert "An editor or owner must review its values" in page.text
        assert "/fire/edit" not in page.text
    finally:
        owner.set_admin_role("owner")


def test_csv_and_sensitivity_without_a_plan_are_not_found(owner) -> None:
    assert owner.client.get("/fire/results.csv").status_code == 404
    assert owner.client.get("/fire/sensitivity").status_code == 404


def test_the_view_shows_live_loans_and_their_effect(owner) -> None:
    from assetboard.db.models.fire import FireLoanSetting
    from tests.fire_loan_helpers import create_loan

    plan_id = _create(owner)
    loan_id = create_loan(owner, name="Home mortgage")
    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert "data-fire-loans" in page.text
    assert "Home mortgage" in page.text
    assert f'data-fire-loan="{loan_id}"' in page.text
    assert 'data-fire-loan-state="included"' in page.text
    assert "data-fire-loan-annual" in page.text
    assert "data-fire-loan-payoff" in page.text
    assert "data-fire-loan-effect" in page.text
    assert 'name="loan_include_' not in page.text
    # F8.15 removed the loan-payments table column and the totals line; the loan badge and panel
    # remain.
    assert "data-fire-loan-payment" not in page.text
    assert "Loan payments are the part" not in page.text
    assert "Loan payments</th>" not in page.text
    assert "data-fire-loans-preview" not in page.text
    assert "Loan choices:" not in page.text
    with owner.db() as session, session.begin():
        session.add(
            FireLoanSetting(
                workspace_id=owner.home_id,
                scenario_id=plan_id,
                loan_id=loan_id,
                included=False,
            )
        )
    ignored = owner.client.get("/fire")
    assert ignored.status_code == 200
    assert 'data-fire-loan-state="ignored"' in ignored.text
    assert "Stays in expenses through the plan horizon." in ignored.text
    assert "data-fire-loans-included-total" in ignored.text


def test_archived_and_paid_off_loans_show_reasons_in_the_view(owner) -> None:
    from assetboard.domain.money import Money
    from tests.fire_loan_helpers import create_loan, owner_context

    _create(owner)
    archived = create_loan(owner, name="Archived loan")
    with owner.db() as session, session.begin():
        from assetboard.db.repositories import LoanRepository

        LoanRepository(session, owner_context(owner)).get_or_raise(
            archived
        ).archived_at = owner.app.state.clock.now()
    paid = create_loan(
        owner,
        name="Paid loan",
        principal_original=Money(100_000, "EUR"),
        term_months=1,
    )
    with owner.db() as session, session.begin():
        from assetboard.db.repositories import LoanRepository

        row = LoanRepository(session, owner_context(owner)).get_or_raise(paid)
        row.name = "Paid loan"
    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert "data-fire-loans-excluded" in page.text
    assert "Paid off; there is no payment to include." in page.text


def test_the_first_included_payoff_skips_ignored_loans(owner) -> None:
    from assetboard.db.models.fire import FireLoanSetting
    from tests.fire_loan_helpers import create_loan

    plan_id = _create(owner)
    early = create_loan(owner, name="Early loan", term_months=12)
    create_loan(owner, name="Later loan", term_months=24)
    with owner.db() as session, session.begin():
        session.add(
            FireLoanSetting(
                workspace_id=owner.home_id,
                scenario_id=plan_id,
                loan_id=early,
                included=False,
            )
        )
    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert "The first included loan payoff is in 2028." in page.text
    assert "The first included loan payoff is in 2027." not in page.text


def test_a_loan_payment_warning_is_shown_when_expenses_are_too_low(owner) -> None:
    from tests.fire_loan_helpers import create_loan

    _create(owner, expenses_manual="1000")
    create_loan(owner)
    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert "data-fire-loans-expense-warning" in page.text


def test_the_view_query_count_does_not_grow_with_loans(owner) -> None:
    from tests.fire_loan_helpers import create_loan

    _create(owner)
    create_loan(owner, name="First")
    seen = 0

    def before(*_args: object) -> None:
        nonlocal seen
        seen += 1

    def count() -> int:
        nonlocal seen
        seen = 0
        engine = owner.app.state.database_engine
        event.listen(engine, "before_cursor_execute", before)
        try:
            response = owner.client.get("/fire")
            assert response.status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", before)
        return seen

    one = count()
    create_loan(owner, name="Second")
    create_loan(owner, name="Third")
    many = count()
    assert many == one
    assert many <= 30


def test_the_loan_panel_has_an_empty_state_and_no_fake_zero(owner) -> None:
    _create(owner)
    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert "No active loans." in page.text
    assert "data-fire-loans-summary" not in page.text


def test_the_loan_panel_is_translated(owner) -> None:
    from assetboard.db.models.identity import User, Workspace
    from assetboard.services import loans as loan_service
    from tests.fire_loan_helpers import create_loan, owner_context

    _create(owner)
    create_loan(owner, name="Kredyt hipoteczny")
    archived = create_loan(owner, name="Stary kredyt")
    loan_service.archive_loan(owner.app.state.session_factory, owner_context(owner), archived)
    with owner.db() as session, session.begin():
        session.get_one(Workspace, owner.home_id).locale = "pl"
        session.get_one(User, owner.admin_id).locale = "pl"
    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert "Kredyty" in page.text
    assert "Uwzględnione w planie" in page.text
    assert "Rata roczna" in page.text or "Roczna rata" in page.text
    assert "Zarchiwizowany w Kredytach; nie jest częścią planu." in page.text


def test_a_plan_currency_mismatch_explains_the_loan_block(owner) -> None:
    from assetboard.db.models.fire import FireScenario

    plan_id = _create(owner)
    with owner.db() as session, session.begin():
        session.get_one(FireScenario, plan_id).currency = "USD"
    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert "data-fire-loans" in page.text
    assert "Loans cannot be included while the plan currency" in page.text
