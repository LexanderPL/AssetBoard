"""F8.5 sensitivity panel: HTMX fragment, no-JS fallback, heat map and states."""

from __future__ import annotations

import re
from datetime import date
from uuid import UUID

from assetboard.db.models.identity import User, Workspace
from assetboard.db.repositories import CategoryRepository, TransactionRepository
from assetboard.services.context import Context, Role, UserRef, WorkspaceRef


def _form(**overrides: str) -> dict[str, str]:
    data = {
        "name": "Retire at 55",
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


def _create(owner, **overrides: str) -> UUID:
    response = owner.post("/fire", _form(**overrides), token_from="/fire/new")
    assert response.status_code == 303, response.text
    return UUID(response.headers["location"].split("?")[0].rsplit("/", 1)[-1])


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


def _category(owner, name: str) -> UUID:
    with owner.app.state.session_factory.begin() as session:
        row = CategoryRepository(session, _context(owner)).create(
            name=name,
            name_key=name.casefold(),
            kind="expense",
            treatment="regular",
            color="#16a34a",
            sort_order=0,
        )
        return row.id


def _spend(owner, category_id: UUID, amount_minor: int) -> None:
    with owner.app.state.session_factory.begin() as session:
        TransactionRepository(session, _context(owner)).create(
            direction="expense",
            day=date(2026, 2, 10),
            amount_minor=amount_minor,
            currency="EUR",
            category_id=category_id,
            description="Synthetic",
            description_key="synthetic",
            source="manual",
            created_by_user_id=None,
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
    scenario_id = _create(owner)
    page = owner.client.get(f"/fire/{scenario_id}")
    assert page.status_code == 200
    assert "data-fire-sensitivity" in page.text
    assert "data-fire-sensitivity-load" in page.text
    assert f'hx-get="/fire/{scenario_id}/sensitivity"' in page.text
    assert 'hx-target="#fire-sensitivity"' in page.text
    assert 'hx-swap="innerHTML"' in page.text
    assert f'href="/fire/{scenario_id}?sensitivity=1#sensitivity"' in page.text
    assert "Based on the saved plan; unsaved changes are not included." in page.text
    assert "data-fire-sensitivity-grid" not in page.text

    form_start = page.text.index('<form id="fire-form"')
    form_end = page.text.index("</form>", form_start)
    panel = page.text.index("data-fire-sensitivity")
    assert panel > form_end
    assert page.text.index('id="fire-results"') < panel


def test_sensitivity_fragment_is_a_five_by_five_heat_map_with_the_base_cell(owner) -> None:
    scenario_id = _create(owner)
    results = owner.client.get(f"/fire/{scenario_id}").text
    fragment = owner.client.get(f"/fire/{scenario_id}/sensitivity")
    assert fragment.status_code == 200
    assert "<html" not in fragment.text
    assert "data-fire-sensitivity-note" in fragment.text
    assert "Based on the saved plan; unsaved changes are not included." in fragment.text
    assert "data-fire-sensitivity-grid" in fragment.text
    assert len(re.findall(r"data-fire-sensitivity-return=", fragment.text)) == 5
    assert len(re.findall(r"data-fire-sensitivity-cell(?:\s|>)", fragment.text)) == 25
    assert "data-fire-sensitivity-base" in fragment.text
    assert "data-fire-sensitivity-tone=" in fragment.text
    assert "data-fire-sensitivity-legend" in fragment.text
    assert 'aria-describedby="sensitivity-legend"' in fragment.text
    assert 'role="region"' in fragment.text and 'tabindex="0"' in fragment.text
    assert "overflow-x-auto" in fragment.text
    assert "sticky left-0" in fragment.text
    assert "<caption" in fragment.text
    assert "data-fire-sensitivity-refresh" in fragment.text
    assert f'hx-get="/fire/{scenario_id}/sensitivity"' in fragment.text
    assert f'href="/fire/{scenario_id}?sensitivity=1#sensitivity"' in fragment.text
    assert _base_age(fragment.text) == _fire_age(results)
    assert "Sheet progress" not in fragment.text


def test_sensitivity_uses_the_saved_plan_and_not_an_unsaved_preview(owner) -> None:
    scenario_id = _create(owner)
    page = owner.client.get(f"/fire/{scenario_id}?sensitivity=1").text
    assert "data-fire-sensitivity-grid" in page
    saved_base = _base_age(page)
    preview = owner.post(
        f"/fire/{scenario_id}/preview",
        _form(expenses_manual="80000"),
        token_from=f"/fire/{scenario_id}",
    )
    assert preview.status_code == 200
    assert "data-fire-sensitivity" not in preview.text
    after = owner.client.get(f"/fire/{scenario_id}/sensitivity")
    assert _base_age(after.text) == saved_base


def test_no_js_fallback_server_renders_the_loaded_grid(owner) -> None:
    scenario_id = _create(owner)
    page = owner.client.get(f"/fire/{scenario_id}?sensitivity=1")
    assert page.status_code == 200
    assert "data-fire-sensitivity-grid" in page.text
    assert "data-fire-sensitivity-load" not in page.text
    form_start = page.text.index('<form id="fire-form"')
    form_end = page.text.index("</form>", form_start)
    assert page.text.index("data-fire-sensitivity-grid") > form_end


def test_linked_expenses_and_exclusions_resolve_for_the_base_cell(owner) -> None:
    counted = _category(owner, "Test groceries")
    excluded = _category(owner, "Test unusual")
    _spend(owner, counted, 100_000)
    _spend(owner, excluded, 50_000)
    scenario_id = _create(
        owner,
        expenses_mode="trailing_12m",
        expenses_manual="",
        **{"expense_exclusion": str(excluded)},
    )
    results = owner.client.get(f"/fire/{scenario_id}").text
    fragment = owner.client.get(f"/fire/{scenario_id}/sensitivity")
    assert fragment.status_code == 200
    assert "data-fire-sensitivity-grid" in fragment.text
    assert _base_age(fragment.text) == _fire_age(results)


def test_a_blocked_scenario_shows_the_block_panel_without_a_grid(owner) -> None:
    scenario_id = _create(owner, income_mode="trailing_12m")
    fragment = owner.client.get(f"/fire/{scenario_id}/sensitivity")
    assert fragment.status_code == 200
    assert "data-fire-sensitivity-blocked" in fragment.text
    assert 'data-fire-sensitivity-block="no_income_data"' in fragment.text
    assert "data-fire-sensitivity-grid" not in fragment.text
    assert "Open plan inputs" in fragment.text


def test_an_extreme_base_return_shows_the_axis_error(owner) -> None:
    scenario_id = _create(owner, real_return="49", extreme_rates_confirmed="1")
    fragment = owner.client.get(f"/fire/{scenario_id}/sensitivity")
    assert fragment.status_code == 200
    assert "data-fire-sensitivity-error" in fragment.text
    assert 'data-fire-sensitivity-error-code="return_out_of_range"' in fragment.text
    assert "data-fire-sensitivity-grid" not in fragment.text


def test_archived_scenarios_and_viewers_can_load_the_grid(owner) -> None:
    scenario_id = _create(owner)
    archived = owner.post(f"/fire/{scenario_id}/archive", token_from=f"/fire/{scenario_id}")
    assert archived.status_code == 303
    owner.set_admin_role("viewer")
    fragment = owner.client.get(f"/fire/{scenario_id}/sensitivity")
    assert fragment.status_code == 200
    assert "data-fire-sensitivity-grid" in fragment.text
    assert "data-fire-sensitivity-error" not in fragment.text
