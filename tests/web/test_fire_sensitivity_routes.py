"""F8.5 sensitivity panel: HTMX fragment, no-JS fallback, heat map and states."""

from __future__ import annotations

import re
from uuid import UUID, uuid4

from sqlalchemy import select

from assetboard.db.models.fire import FireScenario
from assetboard.db.models.identity import User, Workspace
from assetboard.services.context import Context, Role, UserRef, WorkspaceRef
from tests.fire_helpers import create_plan


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


def test_the_results_page_offers_an_on_demand_htmx_load_with_a_no_js_fallback(owner) -> None:
    _create(owner)
    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert "data-fire-sensitivity" in page.text
    assert "data-fire-sensitivity-load" in page.text
    assert 'hx-get="/fire/sensitivity"' in page.text
    assert 'hx-target="#fire-sensitivity"' in page.text
    assert 'hx-swap="innerHTML"' in page.text
    assert 'href="/fire?sensitivity=1#sensitivity"' in page.text
    assert "Based on the saved plan; unsaved changes are not included." in page.text
    assert "data-fire-sensitivity-grid" not in page.text
    assert '<form id="fire-form"' not in page.text
    assert page.text.index('id="fire-results"') < page.text.index("data-fire-sensitivity")


def test_sensitivity_fragment_is_a_five_by_five_heat_map_with_the_base_cell(owner) -> None:
    _create(owner)
    results = owner.client.get("/fire").text
    fragment = owner.client.get("/fire/sensitivity")
    assert fragment.status_code == 200
    assert "<html" not in fragment.text
    assert "data-fire-sensitivity-note" in fragment.text
    assert "Based on the saved plan; unsaved changes are not included." in fragment.text
    assert "data-fire-sensitivity-grid" in fragment.text
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
    assert "data-fire-sensitivity-legend" in fragment.text
    assert 'aria-describedby="sensitivity-legend"' in fragment.text
    assert 'role="region"' in fragment.text and 'tabindex="0"' in fragment.text
    assert "overflow-x-auto" in fragment.text
    assert "sticky left-0" in fragment.text
    assert "<caption" in fragment.text
    assert "data-fire-sensitivity-refresh" in fragment.text
    assert 'hx-get="/fire/sensitivity"' in fragment.text
    assert 'href="/fire?sensitivity=1#sensitivity"' in fragment.text
    assert _base_age(fragment.text) == _fire_age(results)
    assert "real return" not in fragment.text.lower()
    assert "today's money" not in fragment.text.lower()
    assert "data-fire-basis" not in fragment.text
    assert "€" not in fragment.text
    assert "return above inflation" in fragment.text


def test_sensitivity_uses_the_saved_plan_and_not_an_unsaved_preview(owner) -> None:
    _create(owner)
    page = owner.client.get("/fire?sensitivity=1").text
    assert "data-fire-sensitivity-grid" in page
    saved_base = _base_age(page)
    preview = owner.post(
        "/fire/edit/preview",
        _form(expenses_manual="80000"),
        token_from="/fire/edit",
    )
    assert preview.status_code == 200
    assert "data-fire-sensitivity" not in preview.text
    after = owner.client.get("/fire/sensitivity")
    assert _base_age(after.text) == saved_base


def test_no_js_fallback_server_renders_the_loaded_grid(owner) -> None:
    _create(owner)
    page = owner.client.get("/fire?sensitivity=1")
    assert page.status_code == 200
    assert "data-fire-sensitivity-grid" in page.text
    assert "data-fire-sensitivity-load" not in page.text
    assert '<form id="fire-form"' not in page.text
    assert page.text.index("data-fire-sensitivity-grid") > page.text.index('id="fire-results"')


def test_a_saving_override_changes_the_base_cell(owner) -> None:
    _create(owner, real_return="2", expenses_manual="50000", portfolio_manual="300000")
    before = _base_age(owner.client.get("/fire/sensitivity").text)
    response = owner.post(
        "/fire/edit",
        _form(
            event_year_1="2027",
            event_kind_1="saving_override",
            event_amount_1="-5000000",
            event_label_1="Sabbatical",
        ),
        token_from="/fire/edit",
    )
    assert response.status_code == 303
    after = _base_age(owner.client.get("/fire/sensitivity").text)
    assert after != before


def test_a_blocked_scenario_shows_the_block_panel_without_a_grid(owner) -> None:
    plan_id = _create(owner)
    with owner.db() as session:
        row = session.scalars(select(FireScenario).where(FireScenario.id == plan_id)).one()
        row.currency = "USD"
        session.commit()
    fragment = owner.client.get("/fire/sensitivity")
    assert fragment.status_code == 200
    assert "data-fire-sensitivity-blocked" in fragment.text
    assert 'data-fire-sensitivity-block="plan_currency_mismatch"' in fragment.text
    assert "data-fire-sensitivity-grid" not in fragment.text
    assert "Reset plan" in fragment.text


def test_an_extreme_base_return_shows_the_axis_error(owner) -> None:
    _create(owner, real_return="49", extreme_rates_confirmed="1")
    fragment = owner.client.get("/fire/sensitivity")
    assert fragment.status_code == 200
    assert "data-fire-sensitivity-error" in fragment.text
    assert 'data-fire-sensitivity-error-code="return_out_of_range"' in fragment.text
    assert "data-fire-sensitivity-grid" not in fragment.text


def test_viewers_can_load_the_grid(owner) -> None:
    _create(owner)
    owner.set_admin_role("viewer")
    try:
        fragment = owner.client.get("/fire/sensitivity")
        assert fragment.status_code == 200
        assert "data-fire-sensitivity-grid" in fragment.text
        assert "data-fire-sensitivity-error" not in fragment.text
    finally:
        owner.set_admin_role("owner")


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


def test_the_edit_page_has_no_sensitivity_panel(owner) -> None:
    _create(owner)
    page = owner.client.get("/fire/edit")
    assert page.status_code == 200
    assert "data-fire-sensitivity" not in page.text
    assert "/fire/sensitivity" not in page.text


def test_the_old_sensitivity_url_redirects(owner) -> None:
    plan_id = _create(owner)
    response = owner.client.get(f"/fire/{plan_id}/sensitivity", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/fire/sensitivity"
    assert owner.client.get(f"/fire/{uuid4()}/sensitivity").status_code == 404
