"""The F8.9 setup wizard: steps, validation, suggestions, cancel and the one write (F8.9)."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import func, select, update

from assetboard.db.models.fire import FireEvent, FireScenario
from assetboard.db.models.identity import AuditEvent, User, Workspace
from assetboard.db.models.ledger import Transaction
from assetboard.db.repositories import (
    CategoryRepository,
    ItemValuationRepository,
    LoanPaymentRepository,
    LoanRepository,
    NetWorthCategoryRepository,
    NetWorthItemRepository,
    TransactionRepository,
)
from assetboard.services.context import Context, Role, UserRef, WorkspaceRef
from assetboard.services.investment_transactions import create_transaction
from assetboard.services.investments import create_asset_class, create_instrument
from assetboard.services.prices import save_manual_price
from tests.fire_helpers import create_plan, post_wizard, review_signature, wizard_values


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


def _twelve_months() -> list[date]:
    return [date(2025 + (2 + index) // 12, (2 + index) % 12 + 1, 1) for index in range(12)]


def _seed_ledger(owner) -> tuple[UUID, UUID]:
    income = _category(owner, "Salary test", kind="income")
    expenses = _category(owner, "Food test", kind="expense")
    for day in _twelve_months():
        _transaction(owner, income, day, 500_000, "income")
        _transaction(owner, expenses, day, 200_000, "expense")
    return income, expenses


def _seed_portfolio(owner, value_minor: int = 5_000_000) -> UUID:
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
            as_of=date(2026, 2, 15),
            value_minor=value_minor,
            currency="EUR",
        )
        return item.id


def _seed_asset_class(owner) -> UUID:
    ctx = _context(owner)
    factory = owner.app.state.session_factory
    asset_class = create_asset_class(
        factory,
        ctx,
        name="Stocks test",
        color="#2563eb",
        investable=True,
    )
    instrument = create_instrument(
        factory,
        ctx,
        name="Fund test",
        symbol="",
        type="etf",
        quote_currency="EUR",
        unit_label="share",
        allocations=[(asset_class.id, 10_000)],
        locale="en",
    )
    create_transaction(
        factory,
        ctx,
        instrument_id=instrument.id,
        type_value="buy",
        date_value="2026-02-27",
        quantity_value="10",
        unit_price_value="100",
        fee_value="0",
        fx_value="1",
        note_value="",
        locale="en",
    )
    save_manual_price(
        factory,
        ctx,
        instrument.id,
        price_id=None,
        date_value="2026-03-01",
        price_value="100",
        locale="en",
    )
    return asset_class.id


def _link_loan_payment(owner, transaction_id: UUID, total_minor: int) -> UUID:
    with owner.app.state.session_factory.begin() as session:
        loan = LoanRepository(session, _context(owner)).create(
            name="Mortgage test",
            kind="annuity",
            currency="EUR",
            principal_original_minor=None,
            origination_date=None,
            first_payment_date=date(2026, 1, 1),
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
            date=date(2026, 1, 1),
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


def _state(owner) -> tuple[int, int, int]:
    with owner.db() as session:
        return (
            session.scalar(select(func.count()).select_from(FireScenario)) or 0,
            session.scalar(select(func.count()).select_from(FireEvent)) or 0,
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action.like("fire_plan.%"))
            )
            or 0,
        )


def _step(response, step: int) -> str:
    assert response.status_code == 200, response.text
    assert f'data-fire-wizard="{step}"' in response.text, response.text
    return response.text


def test_the_entry_renders_the_wizard_without_a_plan(owner) -> None:
    page = owner.client.get("/fire")
    text = _step(page, 1)
    assert "Set up your FIRE plan" in text
    for marker in (
        'name="birth_year"',
        'name="horizon_age"',
        'name="stop_working_age"',
        "data-fire-wizard-summary",
        "data-wizard-steps",
    ):
        assert marker in text
    assert _state(owner) == (0, 0, 0)


def test_a_viewer_without_a_plan_sees_an_empty_state(owner) -> None:
    owner.set_admin_role(Role.VIEWER.value)
    try:
        page = owner.client.get("/fire")
        assert page.status_code == 200
        assert "data-fire-no-plan" in page.text
        assert "An editor or owner can set up" in page.text
        assert 'name="birth_year"' not in page.text
        assert owner.client.get("/fire/setup").status_code == 403
        response = owner.post("/fire/setup", {"step": "1"}, token_from="/fire")
        assert response.status_code == 403
    finally:
        owner.set_admin_role(Role.OWNER.value)


def test_wizard_steps_next_back_and_no_writes(owner) -> None:
    values = wizard_values()
    _step(post_wizard(owner, 1, "next", values), 2)
    assert _state(owner) == (0, 0, 0)
    _step(post_wizard(owner, 2, "next", values), 3)
    assert _state(owner) == (0, 0, 0)
    _step(post_wizard(owner, 3, "next", values), 4)
    assert _state(owner) == (0, 0, 0)
    _step(post_wizard(owner, 4, "back", values), 3)
    _step(post_wizard(owner, 3, "back", values), 2)
    _step(post_wizard(owner, 2, "back", values), 1)
    assert _state(owner) == (0, 0, 0)


def test_each_step_carries_the_other_steps_in_hidden_fields(owner) -> None:
    values = wizard_values()
    text = _step(post_wizard(owner, 1, "next", values), 2)
    for field, value in (
        ("birth_year", "1985"),
        ("stop_working_age", "55"),
        ("horizon_age", "90"),
        ("inflation", "2.5"),
        ("portfolio_manual", "1000000"),
    ):
        assert f'type="hidden" name="{field}" value="{value}"' in text

    text = _step(post_wizard(owner, 2, "next", values), 3)
    for field, value in (
        ("birth_year", "1985"),
        ("income_manual", "50000"),
        ("expenses_manual", "30000"),
    ):
        assert f'type="hidden" name="{field}" value="{value}"' in text

    text = _step(post_wizard(owner, 3, "next", values), 4)
    for field, value in (
        ("income_manual", "50000"),
        ("expenses_manual", "30000"),
        ("portfolio_manual", "1000000"),
    ):
        assert f'type="hidden" name="{field}" value="{value}"' in text
    assert _state(owner) == (0, 0, 0)


def test_step_validation_keeps_the_values_on_the_owning_step(owner) -> None:
    values = wizard_values(birth_year="")
    response = post_wizard(owner, 1, "next", values)
    assert response.status_code == 400
    assert 'data-fire-wizard="1"' in response.text
    assert "aria-invalid" in response.text
    assert _state(owner) == (0, 0, 0)

    values = wizard_values(horizon_age="40")
    response = post_wizard(owner, 1, "next", values)
    assert response.status_code == 400
    assert 'data-fire-wizard="1"' in response.text

    values = wizard_values(income_manual="")
    response = post_wizard(owner, 2, "next", values)
    assert response.status_code == 400
    assert 'data-fire-wizard="2"' in response.text
    assert "data-field-error" in response.text

    values = wizard_values(portfolio_manual="")
    response = post_wizard(owner, 3, "next", values)
    assert response.status_code == 400
    assert 'data-fire-wizard="3"' in response.text

    values = wizard_values(rule="swr", swr="")
    response = post_wizard(owner, 4, "create", values)
    assert response.status_code == 400
    assert 'data-fire-wizard="4"' in response.text
    assert _state(owner) == (0, 0, 0)


def test_a_tampered_hidden_field_returns_to_its_step(owner) -> None:
    values = wizard_values(birth_year="2100")
    response = post_wizard(owner, 3, "next", values)
    assert response.status_code == 400
    assert 'data-fire-wizard="1"' in response.text
    assert _state(owner) == (0, 0, 0)


def test_cancel_leaves_no_plan(owner) -> None:
    page = owner.client.get("/fire?cancelled=1")
    assert page.status_code == 200
    assert "data-fire-cancelled" in page.text
    assert "Setup cancelled" in page.text
    assert "data-fire-start-setup" in page.text
    assert _state(owner) == (0, 0, 0)


def test_create_plan_writes_once_and_redirects(owner) -> None:
    plan_id = create_plan(owner)
    with owner.db() as session:
        row = session.get_one(FireScenario, plan_id)
        assert row.currency == "EUR"
        assert row.needs_review is False
        assert row.is_live is True
        assert session.scalar(select(func.count()).select_from(FireEvent)) == 0
        actions = session.scalars(select(AuditEvent.action)).all()
        assert actions.count("fire_plan.created") == 1
    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert "data-fire-overview" in page.text


def test_the_review_shows_the_entered_values_and_no_metrics(owner) -> None:
    values = wizard_values()
    text = _step(post_wizard(owner, 3, "next", values), 4)
    assert "data-fire-review" in text
    assert "50,000.00" in text and "30,000.00" in text and "1,000,000.00" in text
    assert "Returns cover expenses" in text
    assert "data-fire-wizard-loans" in text
    assert "data-fire-metrics" not in text
    assert "data-fire-chart" not in text
    assert 'name="event_' not in text
    assert 'name="loan_' not in text


def test_changing_a_step_four_assumption_refreshes_the_review_before_creating(owner) -> None:
    values = wizard_values()
    for step in (1, 2, 3):
        post_wizard(owner, step, "next", values)
    text = _step(post_wizard(owner, 3, "next", values), 4)
    assert "Inflation 2.5%" in text

    changed = wizard_values(inflation="3.5")
    response = post_wizard(
        owner,
        4,
        "create",
        changed,
        review_signature=review_signature(text),
    )
    assert response.status_code == 200
    assert "The plan values changed" in response.text
    assert "Inflation 3.5%" in response.text
    assert _state(owner) == (0, 0, 0)

    response = post_wizard(
        owner,
        4,
        "create",
        changed,
        review_signature=review_signature(response.text),
    )
    assert response.status_code == 303
    with owner.db() as session:
        row = session.scalars(
            select(FireScenario).where(FireScenario.workspace_id == owner.home_id)
        ).one()
        assert row.inflation_bp == 350


def test_the_review_button_refreshes_without_creating(owner) -> None:
    values = wizard_values()
    for step in (1, 2, 3):
        post_wizard(owner, step, "next", values)
    text = _step(post_wizard(owner, 3, "next", values), 4)

    changed = wizard_values(real_return="6")
    response = post_wizard(
        owner,
        4,
        "review",
        changed,
        review_signature=review_signature(text),
    )
    assert response.status_code == 200
    assert "real return 6%" in response.text
    assert "data-fire-review" in response.text
    assert _state(owner) == (0, 0, 0)

    response = post_wizard(
        owner,
        4,
        "create",
        changed,
        review_signature=review_signature(response.text),
    )
    assert response.status_code == 303


def test_a_currency_change_between_steps_requires_a_fresh_review(owner) -> None:
    values = wizard_values()
    for step in (1, 2, 3):
        post_wizard(owner, step, "next", values)
    text = _step(post_wizard(owner, 3, "next", values), 4)

    with owner.db() as session:
        session.get_one(Workspace, owner.home_id).base_currency = "USD"
        session.commit()

    response = post_wizard(
        owner,
        4,
        "create",
        values,
        review_signature=review_signature(text),
    )
    assert response.status_code == 200
    assert "The plan values changed" in response.text
    assert "$50,000.00" in response.text
    assert ">USD<" in response.text
    assert _state(owner) == (0, 0, 0)

    response = post_wizard(
        owner,
        4,
        "create",
        values,
        review_signature=review_signature(response.text),
    )
    assert response.status_code == 303
    with owner.db() as session:
        row = session.scalars(
            select(FireScenario).where(FireScenario.workspace_id == owner.home_id)
        ).one()
        assert row.currency == "USD"


def test_a_tampered_step_four_amount_returns_to_the_amount_step(owner) -> None:
    values = wizard_values(income_manual="")
    response = post_wizard(owner, 4, "create", values)
    assert response.status_code == 400
    assert 'data-fire-wizard="2"' in response.text
    assert 'data-field-error="income_manual"' in response.text
    assert _state(owner) == (0, 0, 0)


def test_a_double_submit_keeps_one_plan(owner) -> None:
    create_plan(owner)
    values = wizard_values()
    response = post_wizard(owner, 4, "create", values)
    assert response.status_code == 303
    assert "error=plan_exists" in response.headers["location"]
    with owner.db() as session:
        assert session.scalar(select(func.count()).select_from(FireScenario)) == 1


def test_the_wizard_redirects_when_a_plan_exists(owner) -> None:
    create_plan(owner)
    assert owner.client.get("/fire/setup", follow_redirects=False).status_code == 303
    response = post_wizard(owner, 1, "next", wizard_values())
    assert response.status_code == 303
    assert "error=plan_exists" in response.headers["location"]


def test_the_wizard_does_not_migrate_a_legacy_row(owner) -> None:
    from tests.web.test_fire_routes import _legacy

    _legacy(owner, "First plan", is_default=True)
    _legacy(owner, "Second plan", is_default=False)
    response = owner.client.get("/fire/setup", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/fire"
    response = post_wizard(owner, 1, "next", wizard_values())
    assert response.status_code == 303
    assert "error=migration_required" in response.headers["location"]
    with owner.db() as session:
        assert session.scalar(select(func.count()).select_from(FireScenario)) == 2


def test_the_wizard_requires_csrf(owner) -> None:
    response = owner.client.post("/fire/setup", data={"step": "1", "go": "next"})
    assert response.status_code == 403
    assert _state(owner) == (0, 0, 0)


def test_suggestions_never_prefill_and_use_copies(owner) -> None:
    _seed_ledger(owner)
    values = wizard_values(income_manual="", expenses_manual="")
    text = _step(post_wizard(owner, 1, "next", values), 2)
    assert 'name="income_manual" value=""' in text
    assert 'name="expenses_manual" value=""' in text
    assert 'data-use-suggestion="income_manual"' in text
    assert 'data-use-suggestion="expenses_manual"' in text
    assert 'data-suggested-minor="6000000"' in text
    assert 'data-suggested-minor="2400000"' in text

    text = _step(post_wizard(owner, 2, "next", values, use="income_manual"), 2)
    assert 'name="income_manual" value="60000.00"' in text
    assert 'name="expenses_manual" value=""' in text
    assert _state(owner) == (0, 0, 0)

    text = _step(post_wizard(owner, 2, "next", values, use="expenses_manual"), 2)
    assert 'name="expenses_manual" value="24000.00"' in text
    assert _state(owner) == (0, 0, 0)


def test_expense_suggestion_shows_categories_and_the_loan_portion(owner) -> None:
    _, expenses = _seed_ledger(owner)
    values = wizard_values(expenses_manual="")
    text = _step(post_wizard(owner, 1, "next", values), 2)
    assert "Food test" in text
    assert "data-fire-suggestion-categories" in text
    assert 'data-fire-loan-portion="none"' in text

    from assetboard.domain.fire import trailing_window

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
    text = _step(post_wizard(owner, 1, "next", values), 2)
    assert 'data-fire-loan-portion="200000"' in text


def test_blocked_suggestions_keep_manual_entry(owner) -> None:
    values = wizard_values(income_manual="", expenses_manual="")
    text = _step(post_wizard(owner, 1, "next", values), 2)
    assert 'data-use-suggestion="income_manual"' not in text
    assert 'data-fire-suggestion-block="no_income_data"' in text
    assert 'name="income_manual"' in text
    assert 'name="expenses_manual"' in text

    values = wizard_values(income_manual="1234.56", expenses_manual="0")
    _step(post_wizard(owner, 2, "next", values), 3)


def test_portfolio_suggestion_shows_sources_and_use(owner) -> None:
    item_id = _seed_portfolio(owner)
    values = wizard_values(portfolio_manual="")
    text = _step(post_wizard(owner, 2, "next", values), 3)
    assert 'name="portfolio_manual" value=""' in text
    assert 'data-suggested-minor="5000000"' in text
    assert "Cash test" in text
    assert f'href="/net-worth?side=asset#item-{item_id}"' in text
    assert 'data-fire-suggestion-source-link="networth_item"' in text
    assert 'data-use-suggestion="portfolio_manual"' in text
    text = _step(post_wizard(owner, 3, "next", values, use="portfolio_manual"), 3)
    assert 'name="portfolio_manual" value="50000.00"' in text
    assert _state(owner) == (0, 0, 0)


def test_portfolio_suggestion_links_asset_class_sources(owner) -> None:
    class_id = _seed_asset_class(owner)
    values = wizard_values(portfolio_manual="")
    text = _step(post_wizard(owner, 2, "next", values), 3)
    assert 'data-fire-suggestion-source="portfolio_class"' in text
    assert f'href="/investments/classes#asset-class-{class_id}"' in text
    assert 'data-fire-suggestion-source-link="portfolio_class"' in text


def test_a_partial_window_shows_the_scaled_months_warning(owner) -> None:
    income = _category(owner, "One month salary test", kind="income")
    _transaction(owner, income, date(2026, 2, 1), 500_000, "income")
    values = wizard_values(income_manual="")
    text = _step(post_wizard(owner, 1, "next", values), 2)
    assert 'data-fire-suggestion-warning="scaled_months"' in text
    assert "Only 1 of the last 12 full months have data." in text


def test_a_blocked_expense_suggestion_shows_no_placeholder_zero(owner) -> None:
    expenses = _category(owner, "USD food test", kind="expense")
    for day in _twelve_months():
        _transaction(owner, expenses, day, 200_000, "expense", currency="USD")
    values = wizard_values(income_manual="", expenses_manual="")
    text = _step(post_wizard(owner, 1, "next", values), 2)
    assert 'data-fire-suggestion-block="ledger_currency_mismatch"' in text
    assert 'data-use-suggestion="expenses_manual"' not in text
    assert "data-suggested-minor" not in text
    assert 'name="expenses_manual" value=""' in text
    assert _state(owner) == (0, 0, 0)


def test_forged_event_and_loan_fields_are_ignored(owner) -> None:
    values = wizard_values()
    for step in (1, 2, 3):
        post_wizard(owner, step, "next", values)
    text = _step(post_wizard(owner, 3, "next", values), 4)
    response = post_wizard(
        owner,
        4,
        "create",
        values,
        review_signature=review_signature(text),
        event_year_1="not-a-year",
        event_kind_1="not-a-kind",
        event_amount_1="not-an-amount",
        event_label_1="Forged",
        loan_id="00000000-0000-0000-0000-000000000000",
        loan_amount="999",
    )
    assert response.status_code == 303
    with owner.db() as session:
        assert session.scalar(select(func.count()).select_from(FireEvent)) == 0


def test_unusual_rates_need_the_confirmation(owner) -> None:
    values = wizard_values(inflation="20")
    for step in (1, 2, 3):
        post_wizard(owner, step, "next", values)
    text = _step(post_wizard(owner, 3, "next", values), 4)
    assert 'name="extreme_rates_confirmed"' in text
    response = post_wizard(
        owner,
        4,
        "create",
        values,
        review_signature=review_signature(text),
    )
    assert response.status_code == 400
    assert 'data-fire-wizard="4"' in response.text
    assert _state(owner) == (0, 0, 0)
    confirmed = wizard_values(inflation="20", extreme_rates_confirmed="1")
    for step in (1, 2, 3):
        post_wizard(owner, step, "next", confirmed)
    text = _step(post_wizard(owner, 3, "next", confirmed), 4)
    response = post_wizard(
        owner,
        4,
        "create",
        confirmed,
        review_signature=review_signature(text),
    )
    assert response.status_code == 303
    assert _state(owner) == (1, 0, 1)


def test_profile_prefill(owner) -> None:
    with owner.db() as session, session.begin():
        session.execute(
            update(User)
            .where(User.id == owner.admin_id)
            .values(birth_year=1980, life_expectancy=88)
        )
    text = _step(owner.client.get("/fire"), 1)
    assert 'name="birth_year" value="1980"' in text
    assert 'name="horizon_age" value="88"' in text
    assert 'name="stop_working_age" value=""' in text


def test_the_wizard_page_has_no_adjustment_editor(owner) -> None:
    text = _step(owner.client.get("/fire"), 1)
    assert "data-fire-events" not in text
    assert 'name="event_' not in text
    assert "data-add-event" not in text
    assert "data-add-adjustment" not in text


def test_the_wizard_and_reset_render_in_polish(owner) -> None:
    owner.client.headers["Accept-Language"] = "pl"
    try:
        page = owner.client.get("/fire")
        assert page.status_code == 200
        assert "Skonfiguruj plan FIRE" in page.text
        assert "O Tobie" in page.text
        assert "Dochody i wydatki" in page.text
    finally:
        owner.client.headers.pop("Accept-Language", None)
    create_plan(owner)
    owner.client.headers["Accept-Language"] = "pl"
    try:
        page = owner.client.get("/fire/reset")
        assert page.status_code == 200
        assert "Zresetuj plan FIRE" in page.text
        assert "Aby potwierdzić, wpisz reset:" in page.text
    finally:
        owner.client.headers.pop("Accept-Language", None)
