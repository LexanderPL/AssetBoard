"""F8.4 linked inputs and loan-payoff suggestions over HTTP."""

from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest
from sqlalchemy import select

from assetboard.db.models.fire import FireEvent
from assetboard.db.models.identity import User, Workspace
from assetboard.db.repositories import (
    CategoryRepository,
    ItemValuationRepository,
    NetWorthCategoryRepository,
    NetWorthItemRepository,
    TransactionRepository,
)
from assetboard.domain.money import Money
from assetboard.domain.months import YearMonth
from assetboard.services import loans
from assetboard.services.context import Context, Role, UserRef, WorkspaceRef
from tests.conftest import OwnerApp


@pytest.fixture(autouse=True)
def _empty_categories(owner: OwnerApp) -> None:
    owner.clear_categories()


def _form(**overrides: str) -> dict[str, str]:
    data = {
        "name": "Linked plan",
        "birth_year": "1985",
        "inflation": "2.5",
        "real_return": "5",
        "income_growth": "3",
        "stop_working_age": "55",
        "horizon_age": "90",
        "rule": "returns_cover_expenses",
        "swr": "4",
        "income_mode": "manual",
        "income_manual": "50000",
        "expenses_mode": "manual",
        "expenses_manual": "30000",
        "portfolio_mode": "manual",
        "portfolio_manual": "1000000",
        "contribution_year_1": "",
        "contribution_amount_1": "",
        "event_id_1": "",
        "event_year_1": "",
        "event_kind_1": "cash",
        "event_amount_1": "",
        "event_label_1": "",
    }
    data.update(overrides)
    return data


def _create(owner: OwnerApp, **overrides: str) -> UUID:
    response = owner.post("/fire", _form(**overrides), token_from="/fire/new")
    assert response.status_code == 303, response.text
    return UUID(response.headers["location"].split("?")[0].rsplit("/", 1)[-1])


def _context(owner: OwnerApp, role: Role = Role.OWNER) -> Context:
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


def _category(owner: OwnerApp, name: str, *, kind: str = "expense") -> UUID:
    ctx = _context(owner)
    with owner.app.state.session_factory.begin() as session:
        row = CategoryRepository(session, ctx).create(
            name=name,
            name_key=name.casefold(),
            kind=kind,
            treatment="regular",
            color="#16a34a",
            sort_order=0,
        )
        return row.id


def _trailing_months(count: int, today: date) -> list[date]:
    month = YearMonth.from_date(today).prev()
    assert month is not None
    months: list[YearMonth] = []
    for _ in range(count):
        months.append(month)
        month = month.prev()
        assert month is not None
    return [item.first_day for item in sorted(months)]


def _spend(
    owner: OwnerApp,
    category_id: UUID,
    day: date,
    amount_minor: int,
    direction: str,
) -> None:
    ctx = _context(owner)
    with owner.app.state.session_factory.begin() as session:
        TransactionRepository(session, ctx).create(
            direction=direction,
            day=day.replace(day=10),
            amount_minor=amount_minor,
            currency="EUR",
            category_id=category_id,
            description="Synthetic",
            description_key="synthetic",
            source="manual",
            created_by_user_id=None,
        )


def _cash_item(owner: OwnerApp, name: str, value_minor: int) -> UUID:
    ctx = _context(owner)
    with owner.app.state.session_factory.begin() as session:
        category = NetWorthCategoryRepository(session, ctx).create(
            name=f"Cash {name}",
            name_key=f"cash {name}".casefold(),
            side="asset",
            color="#0ea5e9",
            sort_order=0,
        )
        item = NetWorthItemRepository(session, ctx).create(
            side="asset",
            category_id=category.id,
            name=name,
            currency="EUR",
            include_in_net_worth=True,
            investable=True,
            estimate_between=False,
        )
        ItemValuationRepository(session, ctx).create(
            item_id=item.id,
            as_of=owner.app.state.clock.today("UTC"),
            value_minor=value_minor,
            currency="EUR",
        )
        return item.id


def test_mode_selects_are_enabled_and_manual_fields_follow_the_mode(owner: OwnerApp) -> None:
    page = owner.client.get("/fire/new").text
    assert 'name="income_mode"' in page
    assert '<option value="trailing_12m">Last 12 full months</option>' in page
    assert '<option value="linked_investable">Investable assets</option>' in page
    assert (
        '<select name="income_mode" class="form-select py-2 font-normal" '
        'data-mode-select="income_mode">'
    ) in page
    assert 'name="income_manual"' in page and ' data-manual-field="income_manual"' in page

    scenario_id = _create(owner, income_mode="trailing_12m")
    linked = owner.client.get(f"/fire/{scenario_id}").text
    assert re_input(linked, "income_manual", "disabled")
    assert not re_input(linked, "income_manual", "required")
    assert '<option value="trailing_12m" selected>Last 12 full months</option>' in linked


def re_input(html: str, name: str, attribute: str) -> bool:
    import re

    match = re.search(rf'<input[^>]*name="{name}"[^>]*>', html)
    assert match is not None
    return attribute in match.group(0)


def test_exclusion_checkboxes_persist_after_a_validation_error(owner: OwnerApp) -> None:
    food = _category(owner, "Food")
    response = owner.post(
        "/fire",
        _form(
            horizon_age="30",
            expenses_mode="trailing_12m",
            **{"expense_exclusion": str(food)},
        ),
        token_from="/fire/new",
    )
    assert response.status_code == 400
    assert (
        f'<input type="checkbox" name="expense_exclusion" value="{food}" checked' in response.text
    )
    assert "data-fire-exclusions" in response.text


def test_linked_panel_shows_breakdown_warnings_as_of_and_source_links(owner: OwnerApp) -> None:
    today = owner.app.state.clock.today("UTC")
    food = _category(owner, "Food")
    salary = _category(owner, "Salary", kind="income")
    unusual = _category(owner, "Unusual")
    for month in _trailing_months(5, today):
        _spend(owner, food, month, 100_000, "expense")
        _spend(owner, unusual, month, 900_000, "expense")
    for month in _trailing_months(7, today):
        _spend(owner, salary, month, 500_000, "income")
    item_id = _cash_item(owner, "Cash savings", 2_500_000)

    scenario_id = _create(
        owner,
        income_mode="trailing_12m",
        expenses_mode="trailing_12m",
        portfolio_mode="linked_investable",
        **{"expense_exclusion": str(unusual)},
    )
    page = owner.client.get(f"/fire/{scenario_id}")
    assert page.status_code == 200
    html = page.text
    assert "data-fire-linked" in html
    assert "data-fire-linked-income" in html and "data-fire-linked-expense" in html
    assert "data-fire-linked-portfolio" in html
    assert f"{item_id}" in html
    assert "Cash savings" in html
    assert 'data-fire-warning="scaled_months"' in html
    assert 'data-fire-warning="start_year_not_current"' not in html
    assert "data-fire-linked-excluded" in html
    assert f'data-fire-excluded-category="{unusual}"' in html
    assert "data-fire-source-link" in html
    assert "data-fire-metrics" in html


def test_preview_resolves_posted_linked_values_and_writes_nothing(owner: OwnerApp) -> None:
    today = owner.app.state.clock.today("UTC")
    food = _category(owner, "Food")
    for month in _trailing_months(5, today):
        _spend(owner, food, month, 100_000, "expense")
    scenario_id = _create(owner)
    preview = owner.post(
        f"/fire/{scenario_id}/preview",
        _form(expenses_mode="trailing_12m"),
        token_from=f"/fire/{scenario_id}",
    )
    assert preview.status_code == 200
    assert "data-fire-linked-expense" in preview.text
    assert 'data-fire-warning="scaled_months"' in preview.text
    assert "data-fire-preview-note" in preview.text
    assert "data-fire-blocked" not in preview.text
    with owner.db() as session:
        from assetboard.db.models.fire import FireScenario

        assert session.get_one(FireScenario, scenario_id).expenses_mode == "manual"
    after = owner.client.get(f"/fire/{scenario_id}").text
    assert "data-fire-linked-expense" not in after


def _create_loan(owner: OwnerApp) -> UUID:
    today = owner.app.state.clock.today("UTC")
    return loans.create_loan(
        owner.app.state.session_factory,
        _context(owner),
        entry_path="original",
        name="Mortgage",
        kind="annuity",
        principal_original=Money(24_000_000, "EUR"),
        current_balance=None,
        balance_date=None,
        origination_date=date(today.year - 1, 1, 1),
        first_payment_date=date(today.year - 1, 2, 1),
        term_months=240,
        payment_day=1,
        rate_mode="fixed",
        fixed_rate_bp=0,
        reference_rate_id=None,
        margin_bp=None,
        high_rate_confirmed=False,
        installment_override=None,
        overpayment_mode="none",
        overpayment_amount=None,
        overpayment_strategy="reduce_term",
    )


def test_loan_suggestion_card_and_one_click_event(owner: OwnerApp) -> None:
    loan_id = _create_loan(owner)
    scenario_id = _create(owner)
    page = owner.client.get(f"/fire/{scenario_id}")
    assert f'data-fire-loan-suggestion="{loan_id}"' in page.text
    assert 'data-fire-loan-state="available"' in page.text
    assert "data-fire-loan-add" in page.text
    assert "Unsaved changes in Plan inputs will be lost." in page.text
    added = owner.post(
        f"/fire/{scenario_id}/loan-payoff",
        {"loan_id": str(loan_id)},
        token_from=f"/fire/{scenario_id}",
    )
    assert added.status_code == 303
    assert "notice=loan_event_added" in added.headers["location"]
    with owner.db() as session:
        event = session.scalars(select(FireEvent).where(FireEvent.scenario_id == scenario_id)).one()
        assert event.kind == "expense_change" and event.amount_minor < 0
        assert event.label == "Loan repaid: Mortgage"

    repeated = owner.post(
        f"/fire/{scenario_id}/loan-payoff",
        {"loan_id": str(loan_id)},
        token_from=f"/fire/{scenario_id}",
    )
    assert repeated.status_code == 303
    assert "error=loan_event_exists" in repeated.headers["location"]
    page = owner.client.get(f"/fire/{scenario_id}?error=loan_event_exists")
    assert 'data-fire-loan-state="already_added"' in page.text
    assert "data-fire-loan-add" not in page.text


def test_loan_suggestion_is_csrf_protected_and_hidden_for_viewers_and_archives(
    owner: OwnerApp,
) -> None:
    loan_id = _create_loan(owner)
    scenario_id = _create(owner)
    without_token = owner.client.post(
        f"/fire/{scenario_id}/loan-payoff", data={"loan_id": str(loan_id)}
    )
    assert without_token.status_code == 403

    owner.set_admin_role("viewer")
    viewer = owner.client.get(f"/fire/{scenario_id}")
    assert 'data-fire-loan-state="available"' in viewer.text
    assert "data-fire-loan-add" not in viewer.text
    assert "Only editors can add a payoff event." in viewer.text

    owner.set_admin_role("owner")
    owner.post(f"/fire/{scenario_id}/archive", token_from=f"/fire/{scenario_id}")
    archived = owner.client.get(f"/fire/{scenario_id}")
    assert "data-fire-loan-suggestion" in archived.text
    assert "data-fire-loan-add" not in archived.text
    assert "This scenario is archived and read-only." in archived.text
