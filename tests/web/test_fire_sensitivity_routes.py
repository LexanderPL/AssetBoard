"""F8.17 sensitivity: always rendered from the saved plan; refresh only in the editor."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from assetboard.db.models.fire import FireEvent, FireScenario
from assetboard.db.models.identity import User, Workspace
from assetboard.services import fire_projection as fire_projection_service
from assetboard.services.context import Context, Role, UserRef, WorkspaceRef
from assetboard.web import fire_routes
from tests.fire_helpers import create_plan
from tests.fire_loan_helpers import create_loan


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


def _create(owner, **overrides: str) -> UUID:
    return create_plan(owner, **overrides)


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


def _fire_age(html: str) -> str:
    match = re.search(r'data-fire-fire-age-value="([^"]*)"', html)
    assert match is not None
    return match.group(1)


def _base_age(html: str) -> str:
    match = re.search(
        r'data-fire-sensitivity-base[^>]*>.*?aria-hidden="true">([^<]*)<',
        html,
        re.S,
    )
    assert match is not None
    return match.group(1).strip()


def test_the_view_renders_the_grid_without_a_load_control(owner) -> None:
    _create(owner)
    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert "data-fire-sensitivity" in page.text
    assert "data-fire-sensitivity-grid" in page.text
    assert "data-fire-sensitivity-refresh" not in page.text
    assert "data-fire-sensitivity-load" not in page.text
    assert "Show sensitivity grid" not in page.text
    assert 'hx-get="/fire/sensitivity"' not in page.text
    assert "Based on the saved plan; unsaved changes are not included." in page.text
    assert page.text.index('id="plan-inputs"') < page.text.index("data-fire-sensitivity")
    assert _base_age(page.text) == _fire_age(page.text)


def test_the_edit_renders_the_grid_and_a_refresh_control(owner) -> None:
    _create(owner)
    page = owner.client.get("/fire/edit")
    assert page.status_code == 200
    assert "data-fire-sensitivity-grid" in page.text
    assert "data-fire-sensitivity-refresh" in page.text
    assert "data-fire-sensitivity-load" not in page.text
    refresh = page.text.split("data-fire-sensitivity-refresh", 1)[0].rsplit("<button", 1)[1]
    assert 'hx-get="/fire/sensitivity?in_editor=1"' in refresh
    assert 'hx-target="#fire-sensitivity"' in refresh
    assert 'hx-swap="innerHTML"' in refresh
    assert "hidden" in refresh
    assert _base_age(page.text) == _fire_age(page.text)


def test_the_refresh_fragment_is_the_grid_body(owner) -> None:
    _create(owner)
    fragment = owner.client.get("/fire/sensitivity")
    assert fragment.status_code == 200
    assert "<html" not in fragment.text
    assert "data-fire-sensitivity-note" in fragment.text
    assert "data-fire-sensitivity-grid" in fragment.text
    assert "data-fire-sensitivity-legend" in fragment.text
    assert "data-fire-sensitivity-refresh" not in fragment.text
    assert "data-fire-sensitivity-load" not in fragment.text
    assert "<section" not in fragment.text
    assert "data-fire-sensitivity-content" not in fragment.text


def test_sensitivity_fragment_is_a_five_by_five_heat_map_with_the_base_cell(owner) -> None:
    _create(owner)
    fragment = owner.client.get("/fire/sensitivity")
    assert fragment.status_code == 200
    assert len(re.findall(r"data-fire-sensitivity-return=", fragment.text)) == 5
    assert len(re.findall(r"data-fire-sensitivity-cell(?:\s|>)", fragment.text)) == 25
    expense_rows = re.findall(
        r'<th scope="row"[^>]*data-fire-sensitivity-expense-factor="(-?\d+)"[^>]*>'
        r"(.*?)</th>",
        fragment.text,
        re.S,
    )
    assert [factor for factor, _html in expense_rows] == [
        "8000",
        "9000",
        "10000",
        "11000",
        "12000",
    ]
    # Expense changes are a percentage of the saved plan, not percentage points.
    assert "-20%" in expense_rows[0][1]
    assert "+20%" in expense_rows[-1][1]
    assert all("pp" not in html for _factor, html in expense_rows)
    assert "data-fire-sensitivity-base" in fragment.text
    assert ">Base<" in fragment.text
    assert "data-fire-sensitivity-tone=" in fragment.text
    assert 'aria-describedby="sensitivity-legend"' in fragment.text
    assert 'role="region"' in fragment.text and 'tabindex="0"' in fragment.text
    assert "overflow-x-auto" in fragment.text
    assert "sticky left-0" in fragment.text
    assert "<caption" in fragment.text
    assert "€" not in fragment.text
    assert "real return" not in fragment.text.lower()
    assert "return above inflation" in fragment.text


def test_the_grid_uses_the_saved_plan_not_posted_preview_values(owner) -> None:
    _create(owner)
    saved = owner.client.get("/fire/sensitivity").text
    saved_base = _base_age(saved)

    preview = owner.post(
        "/fire/edit/preview",
        _form(expenses_manual="80000", inflation="9"),
        token_from="/fire/edit",
    )
    assert preview.status_code == 200
    assert "data-fire-sensitivity" not in preview.text

    error = owner.post(
        "/fire/edit",
        _form(expenses_manual="80000", inflation="9", birth_year="1901"),
        token_from="/fire/edit",
    )
    assert error.status_code == 400
    assert _base_age(error.text) == saved_base
    assert _base_age(owner.client.get("/fire").text) == saved_base
    assert _base_age(owner.client.get("/fire/sensitivity").text) == saved_base


def test_the_grid_uses_saved_loan_choices_after_a_save_error(owner) -> None:
    _create(owner)
    loan_id = create_loan(owner)
    saved_base = _base_age(owner.client.get("/fire/sensitivity").text)
    response = owner.post(
        "/fire/edit",
        _form(birth_year="1901", **{f"loan_include_{loan_id}": "0"}),
        token_from="/fire/edit",
    )
    assert response.status_code == 400
    assert _base_age(response.text) == saved_base


def test_the_sensitivity_query_is_ignored(owner) -> None:
    _create(owner)
    plain = owner.client.get("/fire")
    queried = owner.client.get("/fire?sensitivity=1")
    assert plain.status_code == queried.status_code == 200
    for page in (plain, queried):
        assert "data-fire-sensitivity-grid" in page.text
        assert "data-fire-sensitivity-refresh" not in page.text
        assert "data-fire-sensitivity-load" not in page.text
    assert _base_age(queried.text) == _base_age(plain.text)


def test_viewers_see_the_grid_without_refresh_control(owner) -> None:
    _create(owner)
    owner.set_admin_role("viewer")
    try:
        page = owner.client.get("/fire")
        assert page.status_code == 200
        assert "data-fire-sensitivity-grid" in page.text
        assert "data-fire-sensitivity-refresh" not in page.text
        assert "data-fire-sensitivity-load" not in page.text
        fragment = owner.client.get("/fire/sensitivity")
        assert fragment.status_code == 200
        assert "data-fire-sensitivity-grid" in fragment.text
    finally:
        owner.set_admin_role("owner")


def test_a_blocked_view_and_fragment_show_the_block_panel_without_a_grid(owner) -> None:
    plan_id = _create(owner)
    with owner.db() as session:
        row = session.scalars(select(FireScenario).where(FireScenario.id == plan_id)).one()
        row.currency = "USD"
        session.commit()
    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert "data-fire-sensitivity-blocked" in page.text
    assert 'data-fire-sensitivity-block="plan_currency_mismatch"' in page.text
    assert "data-fire-sensitivity-grid" not in page.text
    assert "Reset plan" in page.text
    fragment = owner.client.get("/fire/sensitivity")
    assert fragment.status_code == 200
    assert "data-fire-sensitivity-blocked" in fragment.text
    assert 'data-fire-sensitivity-block="plan_currency_mismatch"' in fragment.text
    assert "data-fire-sensitivity-grid" not in fragment.text


def test_the_editor_refresh_fragment_keeps_its_blocked_link_on_the_editor(owner) -> None:
    plan_id = _create(owner)
    with owner.db() as session, session.begin():
        session.get_one(FireScenario, plan_id).horizon_age = 41
    page = owner.client.get("/fire/edit")
    assert page.status_code == 200
    assert "data-fire-sensitivity-refresh" in page.text
    assert 'href="#amounts"' in page.text

    # The refresh button asks for the editor variant, so the swapped blocked link stays in
    # place instead of reloading the editor (and dropping unsaved changes).
    editor_fragment = owner.client.get("/fire/sensitivity?in_editor=1")
    assert editor_fragment.status_code == 200
    assert 'href="#amounts"' in editor_fragment.text

    # A plain fragment request (the view or a viewer) still points at the edit page.
    page_fragment = owner.client.get("/fire/sensitivity")
    assert page_fragment.status_code == 200
    assert 'href="/fire/edit#amounts"' in page_fragment.text


def test_an_extreme_base_return_shows_the_axis_error_in_the_grid(owner) -> None:
    _create(owner, real_return="49", extreme_rates_confirmed="1")
    page = owner.client.get("/fire")
    assert "data-fire-sensitivity-error" in page.text
    assert 'data-fire-sensitivity-error-code="return_out_of_range"' in page.text
    assert "data-fire-sensitivity-grid" not in page.text
    fragment = owner.client.get("/fire/sensitivity")
    assert "data-fire-sensitivity-error" in fragment.text
    assert 'data-fire-sensitivity-error-code="return_out_of_range"' in fragment.text
    assert "data-fire-sensitivity-grid" not in fragment.text


def test_a_scaled_negative_expense_cell_shows_the_grid_error_and_year(owner) -> None:
    # Three 0.01 reductions against 0.03 of starting expenses keep the saved plan's own line at
    # zero. At the 80% level the starting line rounds to 0.02 while each 0.01 reduction rounds to
    # 0.01, so the effective line is -0.01 and the grid must fail instead of showing a FIRE cell.
    plan_id = _create(
        owner,
        income_manual="0",
        expenses_manual="0.03",
        portfolio_manual="0",
        horizon_age="43",
    )
    with owner.db() as session, session.begin():
        session.add_all(
            [
                FireEvent(
                    workspace_id=owner.home_id,
                    scenario_id=plan_id,
                    year=2027,
                    kind="expense_change",
                    amount_minor=-1,
                    label="Tiny reduction",
                )
                for _ in range(3)
            ]
        )
    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert "data-fire-sensitivity-error" in page.text
    assert 'data-fire-sensitivity-error-code="expenses_negative"' in page.text
    assert "negative in 2027" in page.text
    assert "data-fire-sensitivity-grid" not in page.text

    fragment = owner.client.get("/fire/sensitivity")
    assert fragment.status_code == 200
    assert "data-fire-sensitivity-error" in fragment.text
    assert 'data-fire-sensitivity-error-code="expenses_negative"' in fragment.text
    assert "negative in 2027" in fragment.text
    assert "data-fire-sensitivity-grid" not in fragment.text


def test_full_pages_compute_the_grid_once_and_never_reload_the_plan(owner, monkeypatch) -> None:
    _create(owner)
    calls = 0
    original = fire_routes.sensitivity

    def counted(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a full FIRE page must not call scenario_results")

    monkeypatch.setattr(fire_routes, "sensitivity", counted)
    monkeypatch.setattr(fire_projection_service, "scenario_results", boom)
    assert owner.client.get("/fire").status_code == 200
    assert calls == 1
    calls = 0
    assert owner.client.get("/fire/edit").status_code == 200
    assert calls == 1


def test_the_preview_never_computes_the_sensitivity_grid(owner, monkeypatch) -> None:
    _create(owner)
    token = owner.token("/fire/edit")
    calls = 0

    def boom(*_args: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise AssertionError("preview must not compute the sensitivity grid")

    monkeypatch.setattr(fire_routes, "sensitivity", boom)
    response = owner.client.post(
        "/fire/edit/preview",
        data={"csrf_token": token} | _form(),
    )
    assert response.status_code == 200
    assert calls == 0
    assert "data-fire-sensitivity" not in response.text


def test_the_blocked_sensitivity_link_to_the_editor_is_translated(owner) -> None:
    plan_id = _create(owner)
    with owner.db() as session, session.begin():
        session.scalars(
            select(FireScenario).where(FireScenario.id == plan_id)
        ).one().horizon_age = 41
    owner.client.headers["Accept-Language"] = "pl"
    try:
        fragment = owner.client.get("/fire/sensitivity")
        assert fragment.status_code == 200
        assert "data-fire-sensitivity-blocked" in fragment.text
        assert "Edytuj plan" in fragment.text
    finally:
        owner.client.headers.pop("Accept-Language", None)


def test_the_old_sensitivity_url_redirects(owner) -> None:
    plan_id = _create(owner)
    response = owner.client.get(f"/fire/{plan_id}/sensitivity", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/fire#sensitivity"
    assert owner.client.get(f"/fire/{uuid4()}/sensitivity").status_code == 404
