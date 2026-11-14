"""F8.5 scenario comparison over HTTP: selector, rows, chart, errors and roles."""

from __future__ import annotations

import json
import re
from uuid import UUID

from assetboard.db.models.fire import FireScenario
from assetboard.db.models.identity import User, Workspace
from assetboard.db.repositories import FireScenarioRepository
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


def _rows(html: str) -> list[str]:
    return re.findall(r'data-fire-comparison-row="([^"]+)"', html)


def _chart(html: str) -> dict:
    match = re.search(r"data-chart-json='([^']*)'", html)
    assert match is not None
    return json.loads(match.group(1))


def _checked(html: str, scenario_id: UUID) -> bool:
    match = re.search(rf'<input[^>]*name="scenario"[^>]*value="{scenario_id}"[^>]*>', html)
    assert match is not None
    return " checked" in match.group(0)


def _row(html: str, scenario_id: UUID) -> str:
    match = re.search(
        rf'<tr[^>]*data-fire-comparison-row="{scenario_id}"[^>]*>(.*?)</tr>',
        html,
        re.S,
    )
    assert match is not None
    return match.group(1)


def _archive(owner, scenario_id: UUID) -> None:
    response = owner.post(f"/fire/{scenario_id}/archive", token_from=f"/fire/{scenario_id}")
    assert response.status_code == 303


def _foreign_scenario(owner, workspace_id: UUID) -> UUID:
    with owner.db() as session:
        user = session.get_one(User, owner.admin_id)
        workspace = session.get_one(Workspace, workspace_id)
        ctx = Context(
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
            role=Role.OWNER,
            clock=owner.app.state.clock,
        )
    with owner.app.state.session_factory.begin() as session:
        row = FireScenarioRepository(session, ctx).create(
            name="Foreign plan",
            name_key="foreign plan",
            currency="EUR",
            birth_year=1985,
            start_year=2026,
            inflation_bp=250,
            real_return_bp=500,
            income_growth_bp=300,
            stop_working_age=55,
            horizon_age=90,
            rule="returns_cover_expenses",
            swr_bp=None,
            income_mode="manual",
            income_manual_minor=5_000_000,
            expenses_mode="manual",
            expenses_manual_minor=3_000_000,
            portfolio_mode="manual",
            portfolio_manual_minor=100_000_000,
            is_default=True,
            archived_at=None,
        )
        return row.id


def test_the_compare_page_is_not_captured_by_the_scenario_route(owner) -> None:
    page = owner.client.get("/fire/compare")
    assert page.status_code == 200
    assert "data-fire-compare-selector-section" in page.text
    assert "data-fire-compare-empty" in page.text
    assert "No scenarios to compare yet." in page.text
    assert "All scenarios" in page.text


def test_the_selector_preselects_the_default_scenario_and_links_from_list_and_results(
    owner,
) -> None:
    first = _create(owner, name="First")
    second = _create(owner, name="Second")
    default = owner.post(f"/fire/{second}/default", token_from=f"/fire/{second}")
    assert default.status_code == 303
    page = owner.client.get("/fire/compare")
    assert page.status_code == 200
    assert _checked(page.text, second)
    assert not _checked(page.text, first)
    assert _rows(page.text) == [str(second)]
    assert 'method="get"' in page.text
    chart = _chart(page.text)
    assert [series["name"] for series in chart["series"]] == ["Second"]
    form = page.text.split("data-fire-compare-selector", 1)[1].split("</form>", 1)[0]
    assert "csrf_token" not in form
    assert "data-fire-compare-option" in page.text
    assert "Compare at most three" not in page.text

    list_page = owner.client.get("/fire").text
    assert "data-fire-compare-link" in list_page
    assert 'href="/fire/compare"' in list_page
    result_page = owner.client.get(f"/fire/{first}").text
    assert "data-fire-compare" in result_page
    assert f'href="/fire/compare?scenario={first}"' in result_page


def test_two_and_three_selected_scenarios_render_in_query_order(owner) -> None:
    first = _create(owner, name="First")
    second = _create(owner, name="Second")
    third = _create(owner, name="Third")
    page = owner.client.get(f"/fire/compare?scenario={third}&scenario={first}")
    assert page.status_code == 200
    assert _rows(page.text) == [str(third), str(first)]
    chart = _chart(page.text)
    assert [series["name"] for series in chart["series"]] == ["Third", "First"]
    assert "js/charts.js" in page.text
    assert "data-fire-comparison-points" in page.text

    three = owner.client.get(f"/fire/compare?scenario={third}&scenario={first}&scenario={second}")
    assert _rows(three.text) == [str(third), str(first), str(second)]
    assert len(_chart(three.text)["series"]) == 3


def test_four_scenarios_show_an_error_and_keep_the_selector_usable(owner) -> None:
    ids = [_create(owner, name=f"Plan {index}") for index in range(4)]
    query = "&".join(f"scenario={scenario_id}" for scenario_id in ids)
    page = owner.client.get(f"/fire/compare?{query}")
    assert page.status_code == 200
    assert "data-fire-compare-error" in page.text
    assert 'data-fire-compare-error-code="too_many_scenarios"' in page.text
    assert _rows(page.text) == []
    assert "data-chart-json" not in page.text
    for scenario_id in ids:
        assert _checked(page.text, scenario_id)


def test_an_invalid_selection_is_not_found(owner) -> None:
    assert owner.client.get("/fire/compare?scenario=not-a-uuid").status_code == 404


def test_a_cross_workspace_scenario_is_not_found(owner) -> None:
    other = owner.add_workspace("Other")
    foreign = _foreign_scenario(owner, other)
    assert owner.client.get(f"/fire/compare?scenario={foreign}").status_code == 404


def test_mixed_currencies_show_a_page_error_without_table_or_chart(owner) -> None:
    euro = _create(owner, name="Euro plan")
    with owner.db() as session, session.begin():
        workspace = session.get_one(Workspace, owner.home_id)
        workspace.base_currency = "USD"
    dollar = _create(owner, name="Dollar plan")
    page = owner.client.get(f"/fire/compare?scenario={euro}&scenario={dollar}")
    assert page.status_code == 200
    assert 'data-fire-compare-error-code="currency_mismatch"' in page.text
    assert _rows(page.text) == []
    assert "data-chart-json" not in page.text
    assert _checked(page.text, euro) and _checked(page.text, dollar)


def test_blocked_and_never_rows_keep_the_table_and_all_blocked_has_no_chart(owner) -> None:
    blocked = _create(owner, name="Blocked", income_mode="trailing_12m")
    never = _create(
        owner,
        name="Never",
        inflation="0",
        real_return="0",
        income_growth="0",
        stop_working_age="41",
        income_manual="0",
        expenses_manual="30000",
        portfolio_manual="10000",
    )
    page = owner.client.get(f"/fire/compare?scenario={blocked}&scenario={never}")
    assert page.status_code == 200
    assert _rows(page.text) == [str(blocked), str(never)]
    blocked_row = _row(page.text, blocked)
    assert ">Linked<" in blocked_row
    assert ">Default<" in blocked_row
    assert 'data-fire-comparison-block="no_income_data"' in page.text
    assert "No regular income transactions" in page.text
    assert 'data-fire-comparison-status="never"' in page.text
    assert "Never before age 90" in page.text
    assert "data-chart-json" in page.text

    only_blocked = owner.client.get(f"/fire/compare?scenario={blocked}")
    assert _rows(only_blocked.text) == [str(blocked)]
    assert "data-fire-comparison-chart-empty" in only_blocked.text
    assert "data-chart-json" not in only_blocked.text


def test_archived_viewers_basis_toggle_and_mixed_start_years(owner) -> None:
    first = _create(owner, name="First")
    archived = _create(owner, name="Archived")
    _archive(owner, archived)
    with owner.db() as session, session.begin():
        first_row = session.get_one(FireScenario, first)
        first_row.start_year = 2025
        first_row.birth_year = 1984
    owner.set_admin_role("viewer")
    page = owner.client.get(f"/fire/compare?scenario={first}&scenario={archived}&basis=real")
    assert page.status_code == 200
    assert "data-fire-mixed-start" in page.text
    assert 'aria-current="true"' in page.text
    assert 'data-fire-comparison-basis-link="real"' in page.text
    assert ">Archived<" in _row(page.text, archived)
    assert ">Default<" in _row(page.text, first)
    assert "data-fire-comparison-table" in page.text
    form = page.text.split("data-fire-compare-selector", 1)[1].split("</form>", 1)[0]
    assert "csrf_token" not in form
    chart = _chart(page.text)
    assert [series["name"] for series in chart["series"]] == ["First", "Archived (archived)"]
    assert "In today's money." in page.text


def test_archived_only_selection_works(owner) -> None:
    scenario = _create(owner, name="Old plan")
    _archive(owner, scenario)
    page = owner.client.get("/fire/compare")
    assert page.status_code == 200
    assert _rows(page.text) == [str(scenario)]
    assert "Archived" in page.text
