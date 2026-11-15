"""The F8.10 FIRE editor: save/discard, preview, suggestions, roles and URL cleanup."""

from __future__ import annotations

import re
from datetime import date
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select

from assetboard.db.models.fire import FireEvent, FireScenario
from assetboard.db.models.identity import AuditEvent, User, Workspace
from assetboard.db.models.ledger import Transaction
from assetboard.db.models.loans import LoanPayment
from assetboard.db.repositories import (
    CategoryRepository,
    ItemValuationRepository,
    LoanPaymentRepository,
    LoanRepository,
    NetWorthCategoryRepository,
    NetWorthItemRepository,
    TransactionRepository,
)
from assetboard.domain.fire import trailing_window
from assetboard.domain.money import Money
from assetboard.i18n.numbers import format_amount_input
from assetboard.services import fire_scenarios as fire_service
from assetboard.services.context import Context, Role, UserRef, WorkspaceRef
from tests.fire_helpers import create_plan


def _context(owner, role: Role = Role.OWNER) -> Context:
    with owner.db() as session:
        user = session.get_one(User, owner.admin_id)
        workspace = session.get_one(Workspace, owner.home_id)
        return Context(
            user=UserRef(
                id=user.id,
                email=user.email,
                display_name=user.display_name,
                locale="en",
                is_instance_admin=False,
            ),
            workspace=WorkspaceRef(
                id=workspace.id,
                name=workspace.name,
                base_currency=workspace.base_currency,
                locale=workspace.locale,
                timezone=workspace.timezone,
                country_code=None,
            ),
            role=role,
            clock=owner.app.state.clock,
        )


def _form(**overrides: str) -> dict[str, str]:
    data = {
        "birth_year": "1985",
        "inflation": "2.5",
        "real_return": "5",
        "income_growth": "3",
        "stop_working_age": "55",
        "horizon_age": "90",
        "rule": "returns_cover_expenses",
        "swr": "4",
        "income_manual": "50000",
        "expenses_manual": "30000",
        "portfolio_manual": "1000000",
        "adjustments_present": "1",
        "event_id_1": "",
        "event_year_1": "",
        "event_kind_1": "cash",
        "event_amount_1": "",
        "event_label_1": "",
    }
    data.update(overrides)
    return data


def _snapshot(owner, plan_id: UUID) -> tuple[Any, tuple[UUID, ...], int, int, int]:
    with owner.db() as session:
        row = session.get_one(FireScenario, plan_id)
        events = tuple(
            session.scalars(
                select(FireEvent.id)
                .where(FireEvent.scenario_id == plan_id)
                .order_by(FireEvent.year, FireEvent.id)
            ).all()
        )
        return (
            row.updated_at,
            events,
            session.scalar(select(func.count()).select_from(AuditEvent)) or 0,
            session.scalar(select(func.count()).select_from(Transaction)) or 0,
            session.scalar(select(func.count()).select_from(LoanPayment)) or 0,
        )


def _input_value(text: str, name: str) -> str:
    match = re.search(rf'<input[^>]*name="{re.escape(name)}"[^>]*value="([^"]*)"', text)
    if match is None:
        match = re.search(rf'<input[^>]*value="([^"]*)"[^>]*name="{re.escape(name)}"', text)
    assert match is not None, name
    return match.group(1)


def _category(owner, name: str, *, kind: str) -> UUID:
    with owner.app.state.session_factory.begin() as session:
        row = CategoryRepository(session, _context(owner)).create(
            name=name,
            name_key=name.casefold(),
            kind=kind,
            treatment="regular",
            color="#16a34a" if kind == "income" else "#dc2626",
            sort_order=0,
        )
        return row.id


def _transaction(
    owner,
    category_id: UUID,
    day: date,
    amount_minor: int,
    direction: str,
    *,
    currency: str = "EUR",
) -> UUID:
    with owner.app.state.session_factory.begin() as session:
        row = TransactionRepository(session, _context(owner)).create(
            direction=direction,
            day=day,
            amount_minor=amount_minor,
            currency=currency,
            category_id=category_id,
            description="Synthetic",
            description_key="synthetic",
            source="manual",
            created_by_user_id=None,
        )
        return row.id


def _window_months(owner) -> list[date]:
    window = trailing_window(owner.app.state.clock.today("UTC"))
    months: list[date] = []
    year, month = window.first_day.year, window.first_day.month
    for _ in range(12):
        months.append(date(year, month, 1))
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return months


def _seed_ledger(owner) -> tuple[UUID, UUID]:
    income = _category(owner, "Salary test", kind="income")
    expenses = _category(owner, "Food test", kind="expense")
    for day in _window_months(owner):
        _transaction(owner, income, day, 500_000, "income")
        _transaction(owner, expenses, day, 200_000, "expense")
    return income, expenses


def _seed_portfolio(owner, value_minor: int = 5_000_000) -> UUID:
    today = owner.app.state.clock.today("UTC")
    with owner.app.state.session_factory.begin() as session:
        category = NetWorthCategoryRepository(session, _context(owner)).create(
            name="Savings test",
            name_key="savings test",
            side="asset",
            color="#0ea5e9",
            sort_order=0,
        )
        item = NetWorthItemRepository(session, _context(owner)).create(
            side="asset",
            category_id=category.id,
            name="Cash test",
            currency="EUR",
            valuation_mode="manual",
            link_type=None,
            link_id=None,
            include_in_net_worth=True,
            investable=True,
            estimate_between=False,
        )
        ItemValuationRepository(session, _context(owner)).create(
            item_id=item.id,
            as_of=today.replace(day=1),
            value_minor=value_minor,
            currency="EUR",
        )
        return item.id


def _link_loan_payment(owner, transaction_id: UUID, total_minor: int) -> UUID:
    today = owner.app.state.clock.today("UTC")
    with owner.app.state.session_factory.begin() as session:
        loan = LoanRepository(session, _context(owner)).create(
            name="Mortgage test",
            kind="annuity",
            currency="EUR",
            principal_original_minor=None,
            origination_date=None,
            first_payment_date=today.replace(day=1),
            term_months=12,
            payment_day=1,
            rate_mode="fixed",
            day_count="monthly",
            margin_bp=None,
            reference_rate_id=None,
            installment_override_minor=None,
            overpayment_mode="none",
            overpayment_amount_minor=None,
            overpayment_strategy="reduce_term",
            archived_at=None,
        )
        payment = LoanPaymentRepository(session, _context(owner)).create(
            loan_id=loan.id,
            date=today.replace(day=1),
            kind="installment",
            total_minor=total_minor,
            principal_minor=total_minor,
            interest_minor=0,
            costs_minor=0,
            overpayment_minor=0,
            note="",
            source="manual",
            transaction_id=transaction_id,
        )
        return payment.id


def _legacy(owner, name: str) -> UUID:
    import json

    payload = {
        "v": 1,
        "name": name,
        "name_key": name.casefold(),
        "start_year": 2026,
        "is_default": True,
        "archived_at": None,
        "income_mode": "manual",
        "expenses_mode": "manual",
        "portfolio_mode": "manual",
        "income_manual_minor": 5_000_000,
        "expenses_manual_minor": 3_000_000,
        "portfolio_manual_minor": 100_000_000,
        "expense_exclusion_ids": [],
    }
    with owner.db() as session, session.begin():
        row = FireScenario(
            workspace_id=owner.home_id,
            currency="EUR",
            birth_year=1985,
            inflation_bp=250,
            real_return_bp=500,
            income_growth_bp=300,
            stop_working_age=55,
            horizon_age=90,
            rule="returns_cover_expenses",
            swr_bp=None,
            income_minor=5_000_000,
            expenses_minor=3_000_000,
            portfolio_minor=100_000_000,
            needs_review=True,
            is_live=False,
            legacy=json.dumps(payload, sort_keys=True),
        )
        session.add(row)
        session.flush()
        return row.id


def _queries(owner, operation) -> int:
    from sqlalchemy import event

    seen = 0

    def before(*_args: object) -> None:
        nonlocal seen
        seen += 1

    engine = owner.app.state.database_engine
    event.listen(engine, "before_cursor_execute", before)
    try:
        operation()
    finally:
        event.remove(engine, "before_cursor_execute", before)
    return seen


def test_the_editor_renders_saved_values_results_and_preview_wiring(owner) -> None:
    plan_id = create_plan(owner)
    page = owner.client.get("/fire/edit")
    assert page.status_code == 200
    assert "data-fire-edit-page" in page.text
    assert "data-fire-form" in page.text
    assert 'action="/fire/edit"' in page.text
    assert 'hx-post="/fire/edit/preview"' in page.text
    assert 'hx-trigger="change from:#fire-form delay:300ms' in page.text
    assert 'hx-include="#fire-form"' in page.text and 'hx-target="#fire-results"' in page.text
    assert 'hx-swap="innerHTML"' in page.text and 'hx-sync="this:replace"' in page.text
    assert 'data-fire-dirty="0"' in page.text
    assert "data-fire-save" in page.text
    assert "data-fire-discard" in page.text
    assert 'href="/fire/edit?discarded=1"' in page.text
    assert "data-fire-view-plan" in page.text
    assert "data-fire-metrics" in page.text
    assert "data-fire-table" in page.text
    assert "data-fire-suggestion" in page.text
    assert "data-fire-sensitivity" not in page.text
    assert "data-fire-csv" not in page.text
    assert "data-fire-reset" not in page.text
    assert 'name="adjustments_present" value="1"' in page.text
    assert "data-fire-adjustments" in page.text
    assert "data-add-adjustment" in page.text
    assert "data-add-event" not in page.text
    assert "data-event-table" not in page.text
    assert _input_value(page.text, "income_manual") == "50000.00"
    assert _input_value(page.text, "expenses_manual") == "30000.00"
    assert _input_value(page.text, "portfolio_manual") == "1000000.00"
    assert f'action="/fire/{plan_id}"' not in page.text
    assert f'hx-post="/fire/{plan_id}' not in page.text
    assert "js/fire.js" in page.text


def test_the_editor_does_not_write_on_get(owner) -> None:
    plan_id = create_plan(owner)
    before = _snapshot(owner, plan_id)
    assert owner.client.get("/fire/edit").status_code == 200
    assert _snapshot(owner, plan_id) == before


def test_the_editor_requires_an_editor_and_csrf(owner) -> None:
    create_plan(owner)
    missing = owner.client.post("/fire/edit", data=_form())
    assert missing.status_code in (400, 403)
    missing_preview = owner.client.post("/fire/edit/preview", data=_form())
    assert missing_preview.status_code in (400, 403)
    owner.set_admin_role(Role.VIEWER.value)
    try:
        assert owner.client.get("/fire/edit").status_code == 403
        response = owner.post("/fire/edit", _form(), token_from="/fire")
        assert response.status_code == 403
        response = owner.post("/fire/edit/preview", _form(), token_from="/fire")
        assert response.status_code == 403
    finally:
        owner.set_admin_role(Role.OWNER.value)


def test_the_editor_redirects_without_a_plan(owner) -> None:
    response = owner.client.get("/fire/edit", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/fire"
    response = owner.post("/fire/edit", _form(), token_from="/fire")
    assert response.status_code == 303
    assert response.headers["location"] == "/fire"


def test_the_editor_redirects_for_legacy_rows_without_migrating(owner) -> None:
    legacy_id = _legacy(owner, "Old plan")
    response = owner.client.get("/fire/edit", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/fire"
    with owner.db() as session:
        row = session.get_one(FireScenario, legacy_id)
        assert row.is_live is False
        assert row.legacy is not None


def test_a_currency_mismatch_redirects_to_the_view(owner) -> None:
    plan_id = create_plan(owner)
    with owner.db() as session:
        session.get_one(FireScenario, plan_id).currency = "USD"
        session.commit()
    response = owner.client.get("/fire/edit", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/fire"
    before = _snapshot(owner, plan_id)
    response = owner.post("/fire/edit", _form(), token_from="/fire/edit")
    assert response.status_code == 303
    assert response.headers["location"] == "/fire"
    assert _snapshot(owner, plan_id)[1:] == before[1:]
    preview = owner.post("/fire/edit/preview", _form(), token_from="/fire/edit")
    assert preview.status_code == 200
    assert "data-fire-preview-error" in preview.text
    assert 'data-fire-blocked="plan_currency_mismatch"' in preview.text


def test_a_noop_save_is_accepted(owner) -> None:
    plan_id = create_plan(owner)
    response = owner.post("/fire/edit", _form(), token_from="/fire/edit")
    assert response.status_code == 303
    assert response.headers["location"] == "/fire?notice=saved"
    with owner.db() as session:
        row = session.get_one(FireScenario, plan_id)
        assert (row.income_minor, row.expenses_minor, row.portfolio_minor) == (
            5_000_000,
            3_000_000,
            100_000_000,
        )


def test_a_plan_that_vanishes_between_load_and_submit_is_handled(owner) -> None:
    plan_id = create_plan(owner)
    token = owner.token("/fire/edit")
    fire_service.delete_fire_plan(owner.app.state.session_factory, _context(owner), confirmed=True)
    response = owner.client.post(
        "/fire/edit",
        data={"csrf_token": token} | _form(),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/fire"
    preview = owner.client.post(
        "/fire/edit/preview",
        data={"csrf_token": token} | _form(),
        follow_redirects=False,
    )
    assert preview.status_code == 404
    with owner.db() as session:
        assert session.get(FireScenario, plan_id) is None


def test_save_persists_returns_to_the_view_and_clears_needs_review(owner) -> None:
    plan_id = create_plan(owner)
    with owner.db() as session:
        session.get_one(FireScenario, plan_id).needs_review = True
        session.commit()
    page = owner.client.get("/fire/edit")
    assert "data-fire-needs-review" in page.text
    response = owner.post("/fire/edit", _form(income_manual="55000"), token_from="/fire/edit")
    assert response.status_code == 303
    assert response.headers["location"] == "/fire?notice=saved"
    with owner.db() as session:
        row = session.get_one(FireScenario, plan_id)
        assert row.income_minor == 5_500_000
        assert row.needs_review is False
    page = owner.client.get("/fire?notice=saved")
    assert "data-action-notice" in page.text
    assert "FIRE plan saved." in page.text
    assert "data-fire-needs-review" not in page.text


def test_preview_does_not_clear_needs_review(owner) -> None:
    plan_id = create_plan(owner)
    with owner.db() as session:
        session.get_one(FireScenario, plan_id).needs_review = True
        session.commit()
    assert owner.post("/fire/edit/preview", _form(), token_from="/fire/edit").status_code == 200
    with owner.db() as session:
        assert session.get_one(FireScenario, plan_id).needs_review is True


def test_save_preserves_the_basis_and_events(owner) -> None:
    plan_id = create_plan(owner)
    response = owner.post(
        "/fire/edit",
        _form(
            basis="real",
            event_year_1="2028",
            event_kind_1="saving_override",
            event_amount_1="-2000",
            event_label_1="Sabbatical",
        ),
        token_from="/fire/edit",
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/fire?notice=saved&basis=real"
    with owner.db() as session:
        events = session.scalars(select(FireEvent).where(FireEvent.scenario_id == plan_id)).all()
        assert [(row.year, row.kind) for row in events] == [(2028, "saving_override")]


def test_a_save_error_preserves_values_and_marks_the_form_dirty(owner) -> None:
    plan_id = create_plan(owner)
    before = _snapshot(owner, plan_id)
    response = owner.post(
        "/fire/edit", _form(horizon_age="41", income_manual="55000"), token_from="/fire/edit"
    )
    assert response.status_code == 400
    assert "data-fire-form" in response.text
    assert 'data-fire-dirty="1"' in response.text
    assert "data-action-error" in response.text
    assert 'data-field-error="horizon_age"' in response.text
    assert 'aria-invalid="true"' in response.text
    assert _input_value(response.text, "income_manual") == "55000"
    assert "data-fire-suggestion" in response.text
    assert _snapshot(owner, plan_id) == before


def test_a_save_error_after_a_currency_change_redirects_without_writing(owner) -> None:
    plan_id = create_plan(owner)
    with owner.db() as session, session.begin():
        workspace = session.get_one(Workspace, owner.home_id)
        workspace.base_currency = "USD"
    before = _snapshot(owner, plan_id)
    response = owner.post("/fire/edit", _form(), token_from="/fire/edit")
    assert response.status_code == 303
    assert response.headers["location"] == "/fire"
    assert _snapshot(owner, plan_id)[1:] == before[1:]


def test_discard_reloads_the_saved_values_with_a_notice(owner) -> None:
    plan_id = create_plan(owner)
    before = _snapshot(owner, plan_id)
    page = owner.client.get("/fire/edit?discarded=1")
    assert page.status_code == 200
    assert "Unsaved changes discarded." in page.text
    assert 'data-fire-dirty="0"' in page.text
    assert _input_value(page.text, "income_manual") == "50000.00"
    assert _snapshot(owner, plan_id) == before


def test_a_truncated_save_without_adjustment_fields_is_rejected(owner) -> None:
    plan_id = create_plan(owner)
    response = owner.post(
        "/fire/edit",
        _form(
            event_year_1="2028",
            event_kind_1="cash",
            event_amount_1="1000",
            event_label_1="Bonus",
        ),
        token_from="/fire/edit",
    )
    assert response.status_code == 303
    with owner.db() as session:
        assert session.scalar(select(func.count()).select_from(FireEvent)) == 1
    partial = {
        key: value
        for key, value in _form().items()
        if not key.startswith("event_") and key != "adjustments_present"
    }
    response = owner.post("/fire/edit", partial, token_from="/fire/edit")
    assert response.status_code == 400
    assert "The adjustment rows are missing." in response.text
    assert 'data-field-error="events"' in response.text
    with owner.db() as session:
        assert session.scalar(select(func.count()).select_from(FireEvent)) == 1
    assert plan_id


def test_save_can_remove_every_adjustment_row(owner) -> None:
    plan_id = create_plan(owner)
    with owner.db() as session, session.begin():
        event = FireEvent(
            workspace_id=owner.home_id,
            scenario_id=plan_id,
            year=2028,
            kind="cash",
            amount_minor=100_000,
            label="Removed",
        )
        session.add(event)
        session.flush()
        event_id = event.id
    # The real form still carries the marker when the user removes every row, including the
    # blank one; that is a deliberate empty list, not a truncated request.
    form = {key: value for key, value in _form().items() if not key.startswith("event_")}
    form.update(
        {
            "event_id_1": str(event_id),
            "event_remove_1": "1",
        }
    )
    response = owner.post("/fire/edit", form, token_from="/fire/edit")
    assert response.status_code == 303
    with owner.db() as session:
        assert session.scalar(select(func.count()).select_from(FireEvent)) == 0


def test_omitting_an_active_adjustment_is_an_error_not_a_deletion(owner) -> None:
    plan_id = create_plan(owner)
    with owner.db() as session, session.begin():
        first = FireEvent(
            workspace_id=owner.home_id,
            scenario_id=plan_id,
            year=2027,
            kind="cash",
            amount_minor=100_000,
            label="First",
        )
        second = FireEvent(
            workspace_id=owner.home_id,
            scenario_id=plan_id,
            year=2028,
            kind="cash",
            amount_minor=200_000,
            label="Second",
        )
        session.add_all((first, second))
        session.flush()
        first_id = first.id
    form = {key: value for key, value in _form().items() if not key.startswith("event_")}
    form.update(
        {
            "event_id_1": str(first_id),
            "event_year_1": "2027",
            "event_kind_1": "cash",
            "event_amount_1": "1000",
            "event_label_1": "First",
        }
    )
    response = owner.post("/fire/edit", form, token_from="/fire/edit")
    assert response.status_code == 400
    assert "no longer part of the plan" in response.text
    with owner.db() as session:
        assert session.scalar(select(func.count()).select_from(FireEvent)) == 2


def test_save_adds_edits_and_removes_adjustments_in_one_transaction(owner) -> None:
    plan_id = create_plan(owner)
    with owner.db() as session, session.begin():
        first = FireEvent(
            workspace_id=owner.home_id,
            scenario_id=plan_id,
            year=2027,
            kind="cash",
            amount_minor=100_000,
            label="First",
        )
        second = FireEvent(
            workspace_id=owner.home_id,
            scenario_id=plan_id,
            year=2028,
            kind="cash",
            amount_minor=200_000,
            label="Second",
        )
        session.add_all((first, second))
        session.flush()
        first_id, second_id = first.id, second.id
    response = owner.post(
        "/fire/edit",
        _form(
            income_manual="55000",
            event_id_1=str(first_id),
            event_year_1="2027",
            event_kind_1="cash",
            event_amount_1="1500",
            event_label_1="First updated",
            event_id_2=str(second_id),
            event_year_2="2028",
            event_kind_2="cash",
            event_amount_2="2000",
            event_label_2="Second",
            event_remove_2="1",
            event_id_3="",
            event_year_3="2029",
            event_kind_3="saving_override",
            event_amount_3="-500",
            event_label_3="New override",
        ),
        token_from="/fire/edit",
    )
    assert response.status_code == 303
    with owner.db() as session:
        assert session.get_one(FireScenario, plan_id).income_minor == 5_500_000
        rows = session.scalars(
            select(FireEvent).where(FireEvent.scenario_id == plan_id).order_by(FireEvent.year)
        ).all()
        assert [(row.id, row.year, row.kind, row.amount_minor, row.label) for row in rows] == [
            (first_id, 2027, "cash", 150_000, "First updated"),
            (rows[1].id, 2029, "saving_override", -50_000, "New override"),
        ]
        assert second_id not in {row.id for row in rows}


def test_a_row_validation_error_keeps_values_and_marks_the_control(owner) -> None:
    plan_id = create_plan(owner)
    response = owner.post(
        "/fire/edit",
        _form(
            income_manual="55000",
            event_year_1="2028",
            event_kind_1="cash",
            event_amount_1="not-money",
            event_label_1="Bad amount",
        ),
        token_from="/fire/edit",
    )
    assert response.status_code == 400
    assert 'data-adjustment-error="invalid_amount"' in response.text
    assert (
        'name="event_amount_1"' in response.text
        and 'aria-describedby="adjustment-error-1"' in response.text
    )
    assert "Bad amount" in response.text
    assert _input_value(response.text, "income_manual") == "55000"
    with owner.db() as session:
        assert session.get_one(FireScenario, plan_id).income_minor == 5_000_000
        assert session.scalar(select(func.count()).select_from(FireEvent)) == 0


def test_duplicate_saving_override_marks_the_second_year_field(owner) -> None:
    create_plan(owner)
    response = owner.post(
        "/fire/edit",
        _form(
            event_id_1="",
            event_year_1="2028",
            event_kind_1="saving_override",
            event_amount_1="1000",
            event_label_1="First",
            event_id_2="",
            event_year_2="2028",
            event_kind_2="saving_override",
            event_amount_2="2000",
            event_label_2="Second",
        ),
        token_from="/fire/edit",
    )
    assert response.status_code == 400
    assert "A year can have only one saving override." in response.text
    assert 'data-adjustment-error="duplicate_saving_override"' in response.text
    assert 'id="event-year-2"' in response.text
    assert 'aria-describedby="adjustment-error-2"' in response.text


def test_archived_adjustments_are_read_only_and_deleted_only_by_the_checkbox(owner) -> None:
    plan_id = create_plan(owner)
    with owner.db() as session, session.begin():
        old = FireEvent(
            workspace_id=owner.home_id,
            scenario_id=plan_id,
            year=2020,
            kind="cash",
            amount_minor=100_000,
            label="Old bonus",
        )
        session.add(old)
        session.flush()
        old_id = old.id
    page = owner.client.get("/fire/edit").text
    assert "data-fire-archived-adjustments" in page
    assert "Old bonus" in page
    assert "Before the current year" in page
    assert f'name="archived_remove_1" value="{old_id}"' in page
    archived_section = page.split("data-fire-archived-adjustments", 1)[1]
    assert "event_year_" not in archived_section
    response = owner.post(
        "/fire/edit",
        _form(archived_remove_1=str(old_id)),
        token_from="/fire/edit",
    )
    assert response.status_code == 303
    with owner.db() as session:
        assert session.scalar(select(func.count()).select_from(FireEvent)) == 0


def test_the_empty_adjustment_hint_distinguishes_archived_history_and_pending_rows(
    owner,
) -> None:
    plan_id = create_plan(owner)
    page = owner.client.get("/fire/edit").text
    assert "No adjustments yet. Use the new row below" in page
    assert "No current adjustments" not in page

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
    page = owner.client.get("/fire/edit").text
    assert "No current adjustments. Use the new row below" in page
    assert "No adjustments yet" not in page
    assert "An adjustment archived only because it is after the plan's last year" not in page
    view = owner.client.get("/fire").text
    assert "No current adjustments." in view
    assert "No adjustments yet." not in view

    # A partially filled saved form is not empty, even though no row is saved yet.
    response = owner.post(
        "/fire/edit",
        _form(
            event_year_1="2027",
            event_kind_1="cash",
            event_amount_1="not-money",
            event_label_1="Pending row",
        ),
        token_from="/fire/edit",
    )
    assert response.status_code == 400
    assert "Pending row" in response.text
    assert "data-fire-adjustments-empty" not in response.text


def test_an_archived_adjustment_applies_when_the_submitted_horizon_covers_it(
    owner,
) -> None:
    plan_id = create_plan(owner, horizon_age="80")  # birth year 1985 -> last year 2065
    with owner.db() as session, session.begin():
        later = FireEvent(
            workspace_id=owner.home_id,
            scenario_id=plan_id,
            year=2070,
            kind="cash",
            amount_minor=100_000,
            label="Later bonus",
        )
        session.add(later)
        session.flush()
        later_id = later.id
    page = owner.client.get("/fire/edit").text
    assert "After the plan" in page
    assert (
        "An adjustment archived only because it is after the plan's last year applies again" in page
    )

    preview = owner.post(
        "/fire/edit/preview",
        _form(horizon_age="90"),
        token_from="/fire/edit",
    )
    assert preview.status_code == 200
    assert "data-fire-preview-error" not in preview.text
    assert ">2070<" in preview.text
    assert "€1,000.00" in preview.text

    response = owner.post("/fire/edit", _form(horizon_age="90"), token_from="/fire/edit")
    assert response.status_code == 303
    view = owner.client.get("/fire").text
    assert "Later bonus" in view
    assert "data-fire-archived-adjustments" not in view
    assert "data-fire-adjustments-empty" not in view
    with owner.db() as session:
        assert session.get_one(FireEvent, later_id).year == 2070


def test_the_adjustments_editor_has_no_bulk_paste_or_contribution_controls(owner) -> None:
    create_plan(owner)
    page = owner.client.get("/fire/edit").text
    section = page.split("data-fire-adjustments", 1)[1].split("data-fire-actions", 1)[0]
    assert "<textarea" not in section
    assert "Paste" not in section
    assert "Contribution" not in section
    assert 'name="contribution' not in page
    assert "data-add-adjustment" in section
    assert "event_remove_new" not in section


def test_all_four_kinds_render_and_save_from_the_single_editor(owner) -> None:
    create_plan(owner)
    page = owner.client.get("/fire/edit").text
    for label in (
        "One-off cash flow",
        "Annual income change",
        "Annual expense change",
        "Saving override",
    ):
        assert label in page
    response = owner.post(
        "/fire/edit",
        _form(
            event_year_1="2027",
            event_kind_1="cash",
            event_amount_1="1000",
            event_label_1="Bonus",
            event_year_2="2028",
            event_kind_2="income_change",
            event_amount_2="500",
            event_label_2="Rent",
            event_year_3="2029",
            event_kind_3="expense_change",
            event_amount_3="-200",
            event_label_3="Loan repaid",
            event_year_4="2030",
            event_kind_4="saving_override",
            event_amount_4="-3000",
            event_label_4="Sabbatical",
        ),
        token_from="/fire/edit",
    )
    assert response.status_code == 303
    view = owner.client.get("/fire").text
    for label in ("Bonus", "Rent", "Loan repaid", "Sabbatical"):
        assert label in view


def test_a_forged_scenario_id_and_use_are_ignored(owner) -> None:
    plan_id = create_plan(owner)
    response = owner.post(
        "/fire/edit",
        _form(
            income_manual="55000",
            scenario_id=str(uuid4()),
            use="income_manual",
        ),
        token_from="/fire/edit",
    )
    assert response.status_code == 303
    with owner.db() as session:
        assert session.get_one(FireScenario, plan_id).income_minor == 5_500_000


def test_the_old_post_routes_are_gone(owner) -> None:
    plan_id = create_plan(owner)
    response = owner.post(f"/fire/{plan_id}", _form(), token_from="/fire/edit")
    assert response.status_code in (404, 405)
    response = owner.post(f"/fire/{plan_id}/preview", _form(), token_from="/fire/edit")
    assert response.status_code in (404, 405)


def test_preview_returns_changed_results_and_writes_nothing(owner) -> None:
    plan_id = create_plan(owner)
    before = _snapshot(owner, plan_id)
    response = owner.post(
        "/fire/edit/preview",
        _form(expenses_manual="40000", basis="real"),
        token_from="/fire/edit",
    )
    assert response.status_code == 200
    assert "Preview of unsaved changes" in response.text
    assert 'data-fire-basis="real"' in response.text
    assert "data-fire-metrics" in response.text
    assert "data-fire-table" in response.text
    assert "data-fire-chart" in response.text
    assert "<html" not in response.text
    assert 'id="fire-results"' not in response.text
    assert "data-fire-suggestion" not in response.text
    assert "data-suggested-minor" not in response.text
    assert _snapshot(owner, plan_id) == before


def test_preview_includes_unsaved_adjustment_rows_and_writes_nothing(owner) -> None:
    plan_id = create_plan(owner)
    before = _snapshot(owner, plan_id)
    response = owner.post(
        "/fire/edit/preview",
        _form(
            event_year_1="2028",
            event_kind_1="cash",
            event_amount_1="10000",
            event_label_1="Bonus",
        ),
        token_from="/fire/edit",
    )
    assert response.status_code == 200
    assert "Preview of unsaved changes" in response.text
    assert "data-fire-preview-error" not in response.text
    assert _snapshot(owner, plan_id) == before


def test_preview_marks_the_specific_invalid_row_control(owner) -> None:
    create_plan(owner)
    response = owner.post(
        "/fire/edit/preview",
        _form(
            event_year_1="2028",
            event_kind_1="cash",
            event_amount_1="not-money",
            event_label_1="Bad",
        ),
        token_from="/fire/edit",
    )
    assert response.status_code == 200
    assert 'data-fire-preview-error-field="event_amount_1"' in response.text
    assert "data-fire-preview-error" in response.text


def test_preview_marks_an_archived_row_for_deletion_without_writing(owner) -> None:
    plan_id = create_plan(owner)
    with owner.db() as session, session.begin():
        old = FireEvent(
            workspace_id=owner.home_id,
            scenario_id=plan_id,
            year=2020,
            kind="cash",
            amount_minor=100_000,
            label="Old bonus",
        )
        session.add(old)
        session.flush()
        old_id = old.id
    before = _snapshot(owner, plan_id)
    response = owner.post(
        "/fire/edit/preview",
        _form(archived_remove_1=str(old_id)),
        token_from="/fire/edit",
    )
    assert response.status_code == 200
    assert "data-fire-preview-error" not in response.text
    assert _snapshot(owner, plan_id) == before
    with owner.db() as session:
        assert session.get_one(FireEvent, old_id).label == "Old bonus"


def test_an_invalid_preview_keeps_the_saved_projection_and_names_the_field(owner) -> None:
    plan_id = create_plan(owner)
    response = owner.post(
        "/fire/edit/preview",
        _form(income_manual="-1"),
        token_from="/fire/edit",
    )
    assert response.status_code == 200
    assert "data-fire-preview-error" in response.text
    assert 'data-fire-preview-error-field="income_manual"' in response.text
    assert "data-fire-metrics" in response.text
    with owner.db() as session:
        assert session.get_one(FireScenario, plan_id).income_minor == 5_000_000


def test_the_edit_page_and_suggestion_cards_never_write(owner) -> None:
    plan_id = create_plan(owner)
    _seed_ledger(owner)
    _seed_portfolio(owner)
    before = _snapshot(owner, plan_id)
    assert owner.client.get("/fire/edit").status_code == 200
    assert owner.client.get("/fire/edit?basis=real").status_code == 200
    assert _snapshot(owner, plan_id) == before


def test_suggestions_show_all_three_cards_and_never_prefill(owner) -> None:
    _seed_ledger(owner)
    _seed_portfolio(owner)
    create_plan(owner)
    page = owner.client.get("/fire/edit")
    text = page.text
    assert 'data-fire-suggestion="income"' in text
    assert 'data-fire-suggestion="expenses"' in text
    assert 'data-fire-suggestion="portfolio"' in text
    assert 'data-suggested-minor="6000000"' in text
    assert 'data-suggested-minor="2400000"' in text
    assert 'data-suggested-minor="5000000"' in text
    assert "data-fire-suggestion-window" in text
    assert "data-fire-suggestion-as-of" in text
    assert 'data-use-suggestion="income_manual"' in text
    assert 'data-suggested-input="60000.00"' in text
    assert 'data-use-suggestion="expenses_manual"' in text
    assert 'data-use-suggestion="portfolio_manual"' in text
    assert 'data-fire-suggestion-source-link="networth_item"' in text
    assert "/net-worth?side=asset#item-" in text
    assert 'data-fire-suggestion-differs="1"' in text
    assert 'data-fire-suggestion-saved-minor="5000000"' in text
    # The saved amounts stay in the inputs; the suggestion never fills them by itself.
    assert _input_value(text, "income_manual") == "50000.00"
    assert _input_value(text, "expenses_manual") == "30000.00"
    assert _input_value(text, "portfolio_manual") == "1000000.00"
    assert 'name="use"' not in text


def test_use_carries_a_locale_formatted_suggested_input(owner) -> None:
    _seed_ledger(owner)
    create_plan(owner)
    owner.client.headers["Accept-Language"] = "pl"
    try:
        text = owner.client.get("/fire/edit").text
        assert 'data-suggested-input="60000,00"' in text
        assert "Dane na dzień" in text
    finally:
        owner.client.headers.pop("Accept-Language", None)
    text = owner.client.get("/fire/edit").text
    assert 'data-suggested-input="60000.00"' in text


def test_saved_vs_suggested_note_reports_match_and_difference(owner) -> None:
    _seed_ledger(owner)
    create_plan(owner, income_manual="60000")
    text = owner.client.get("/fire/edit").text
    assert 'data-fire-suggestion-differs="0"' in text
    response = owner.post("/fire/edit", _form(income_manual="50000"), token_from="/fire/edit")
    assert response.status_code == 303
    text = owner.client.get("/fire/edit").text
    assert 'data-fire-suggestion-differs="1"' in text
    assert "This differs from the suggestion" in text


def test_expense_suggestion_shows_categories_and_the_loan_portion(owner) -> None:
    _, expenses = _seed_ledger(owner)
    create_plan(owner)
    text = owner.client.get("/fire/edit").text
    assert "Food test" in text
    assert "data-fire-suggestion-categories" in text
    assert 'data-fire-loan-portion="none"' in text
    window = trailing_window(owner.app.state.clock.today("UTC"))
    with owner.db() as session:
        first = session.scalars(
            select(Transaction.id)
            .where(
                Transaction.category_id == expenses,
                Transaction.date >= window.first_day,
                Transaction.date <= window.last_day,
            )
            .order_by(Transaction.date)
        ).first()
    assert first is not None
    _link_loan_payment(owner, first, 200_000)
    text = owner.client.get("/fire/edit").text
    assert 'data-fire-loan-portion="200000"' in text


def test_blocked_suggestions_keep_manual_entry_and_save(owner) -> None:
    create_plan(owner)
    text = owner.client.get("/fire/edit").text
    assert 'data-fire-suggestion-block="no_income_data"' in text
    assert 'data-use-suggestion="income_manual"' not in text
    assert "data-suggested-minor" not in text
    assert _input_value(text, "income_manual") == "50000.00"
    response = owner.post("/fire/edit", _form(), token_from="/fire/edit")
    assert response.status_code == 303


def test_the_editor_query_count_does_not_grow_with_events_or_ledger_rows(owner) -> None:
    plan_id = create_plan(owner)
    # Warm the suggestion path so the baseline already includes the category and loan-portion
    # queries a workspace with data needs; adding rows must not grow the count further.
    income = _category(owner, "Warm salary", kind="income")
    food = _category(owner, "Warm food", kind="expense")
    day = _window_months(owner)[0]
    _transaction(owner, income, day, 500_000, "income")
    _transaction(owner, food, day, 200_000, "expense")
    quiet = _queries(owner, lambda: owner.client.get("/fire/edit"))
    for index in range(50):
        with owner.db() as session, session.begin():
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
    food = _category(owner, "Loud food", kind="expense")
    salary = _category(owner, "Loud salary", kind="income")
    for day in _window_months(owner):
        _transaction(owner, food, day, 1_000, "expense")
        _transaction(owner, salary, day, 2_000, "income")
    loud = _queries(owner, lambda: owner.client.get("/fire/edit"))
    assert loud == quiet
    assert quiet <= 45


def test_the_preview_query_count_does_not_grow_with_events(owner) -> None:
    plan_id = create_plan(owner)
    token = owner.token("/fire/edit")

    def payload() -> dict[str, str]:
        form = {key: value for key, value in _form().items() if not key.startswith("event_")}
        with owner.db() as session:
            events = session.scalars(
                select(FireEvent)
                .where(FireEvent.scenario_id == plan_id)
                .order_by(FireEvent.year, FireEvent.id)
            ).all()
        for index, event in enumerate(events, start=1):
            form.update(
                {
                    f"event_id_{index}": str(event.id),
                    f"event_year_{index}": str(event.year),
                    f"event_kind_{index}": event.kind,
                    f"event_amount_{index}": format_amount_input(
                        Money(event.amount_minor, "EUR"), "en"
                    ),
                    f"event_label_{index}": event.label,
                }
            )
        return {"csrf_token": token} | form

    def preview() -> Any:
        return owner.client.post("/fire/edit/preview", data=payload(), follow_redirects=False)

    quiet = _queries(owner, preview)
    for index in range(50):
        with owner.db() as session, session.begin():
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
    loud = _queries(owner, preview)
    assert loud == quiet
    assert quiet <= 20


def test_the_edit_and_view_copy_state_after_tax_cash_flows(owner) -> None:
    create_plan(owner)
    edit = owner.client.get("/fire/edit").text
    view = owner.client.get("/fire").text
    assert "Use the after-tax amount." in edit
    assert "Enter your after-tax income" in edit
    assert "before tax" not in edit
    assert "does not model taxes separately" in view
    assert "after-tax" in view
    assert "no taxes" not in view


def test_the_edit_page_renders_in_polish(owner) -> None:
    create_plan(owner)
    owner.client.headers["Accept-Language"] = "pl"
    try:
        page = owner.client.get("/fire/edit")
        assert page.status_code == 200
        assert "Edytuj plan FIRE" in page.text
        assert "Zapisz zmiany" in page.text
        assert "Odrzuć zmiany" in page.text
        assert "Niezapisane zmiany" in page.text
        assert "Użyj proponowanej wartości" in page.text
        assert "Korekty" in page.text
        assert "Dodaj korektę" in page.text
        assert "Korekta oszczędności" in page.text
    finally:
        owner.client.headers.pop("Accept-Language", None)


def test_the_edit_markup_is_csp_safe(owner) -> None:
    create_plan(owner)
    page = owner.client.get("/fire/edit")
    assert not re.search(r"<script(?![^>]*\bsrc=)", page.text)
    assert "onclick=" not in page.text
    assert "js/fire.js" in page.text


def test_another_workspace_has_its_own_editor_state(owner) -> None:
    create_plan(owner)
    other = owner.add_workspace("Other")
    owner.join(owner.admin_id, other, "owner")
    response = owner.post("/workspace/switch", {"workspace_id": str(other)}, token_from="/")
    assert response.status_code == 303
    assert owner.client.get("/fire/edit", follow_redirects=False).status_code == 303
    page = owner.client.get("/fire")
    assert 'data-fire-wizard="1"' in page.text
    response = owner.post(
        "/workspace/switch", {"workspace_id": str(owner.home_id)}, token_from="/fire"
    )
    assert response.status_code == 303
    assert owner.client.get("/fire/edit").status_code == 200


def _loan_settings(owner, plan_id: UUID) -> list[tuple[UUID, bool]]:
    from assetboard.db.models.fire import FireLoanSetting

    with owner.db() as session:
        return [
            (row.loan_id, row.included)
            for row in session.scalars(
                select(FireLoanSetting).where(FireLoanSetting.scenario_id == plan_id)
            )
        ]


def test_the_editor_shows_loan_toggles_and_preview_never_writes(owner) -> None:
    from tests.fire_loan_helpers import create_loan

    plan_id = create_plan(owner)
    loan_id = create_loan(owner)
    page = owner.client.get("/fire/edit")
    assert page.status_code == 200
    assert f'name="loan_include_{loan_id}"' in page.text
    assert re.search(rf'name="loan_include_{loan_id}"[^>]*value="1"[^>]*checked', page.text)
    assert "data-fire-loan-annual" in page.text
    preview = owner.post(
        "/fire/edit/preview",
        _form(**{f"loan_include_{loan_id}": "0"}),
        token_from="/fire/edit",
    )
    assert preview.status_code == 200
    assert "data-fire-loans-preview" in preview.text
    assert "data-fire-loans-ignored-total" in page.text
    assert _loan_settings(owner, plan_id) == []


def test_loan_toggles_save_with_save_changes_and_discard_restores(owner) -> None:
    from tests.fire_loan_helpers import create_loan

    plan_id = create_plan(owner)
    loan_id = create_loan(owner)
    response = owner.post(
        "/fire/edit",
        _form(**{f"loan_include_{loan_id}": "0"}),
        token_from="/fire/edit",
    )
    assert response.status_code == 303
    assert _loan_settings(owner, plan_id) == [(loan_id, False)]
    view = owner.client.get("/fire")
    assert 'data-fire-loan-state="ignored"' in view.text
    discarded = owner.client.get("/fire/edit?discarded=1")
    assert f'name="loan_include_{loan_id}"' in discarded.text
    assert re.search(rf'name="loan_include_{loan_id}"[^>]*value="0"[^>]*checked', discarded.text)


def test_a_save_error_keeps_posted_loan_toggles(owner) -> None:
    from tests.fire_loan_helpers import create_loan

    create_plan(owner)
    loan_id = create_loan(owner)
    response = owner.post(
        "/fire/edit",
        _form(birth_year="1901", **{f"loan_include_{loan_id}": "0"}),
        token_from="/fire/edit",
    )
    assert response.status_code == 400
    assert re.search(rf'name="loan_include_{loan_id}"[^>]*value="0"[^>]*checked', response.text)


def test_an_unknown_loan_toggle_is_rejected_without_writing(owner) -> None:
    plan_id = create_plan(owner)
    response = owner.post(
        "/fire/edit",
        _form(**{"loan_include_00000000-0000-0000-0000-000000000000": "0"}),
        token_from="/fire/edit",
    )
    assert response.status_code == 400
    assert "data-action-error" in response.text
    assert _loan_settings(owner, plan_id) == []
