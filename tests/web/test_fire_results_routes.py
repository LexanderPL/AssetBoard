"""The F8.3 combined FIRE results page, preview fragment and CSV."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select

from assetboard.db.models.fire import FireContribution, FireEvent, FireScenario
from assetboard.db.models.identity import AuditEvent, Workspace


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


def _fire_age(html: str) -> str:
    match = re.search(r'data-fire-fire-age-value="([^"]*)"', html)
    assert match is not None
    return match.group(1)


def _chart_data(html: str) -> list[dict[str, Any]]:
    values = re.findall(r"data-chart-json='([^']*)'", html)
    assert len(values) == 2
    return [json.loads(value) for value in values]


def _scenario_snapshot(
    owner, scenario_id: UUID
) -> tuple[datetime, tuple[UUID, ...], tuple[UUID, ...], int]:
    """The saved row, child identities and audit count a preview must leave untouched."""
    with owner.db() as session:
        row = session.get_one(FireScenario, scenario_id)
        contributions = session.scalars(
            select(FireContribution.id)
            .where(FireContribution.scenario_id == scenario_id)
            .order_by(FireContribution.year)
        ).all()
        events = session.scalars(
            select(FireEvent.id)
            .where(FireEvent.scenario_id == scenario_id)
            .order_by(FireEvent.year, FireEvent.id)
        ).all()
        audits = session.scalar(select(func.count()).select_from(AuditEvent))
        assert audits is not None
        return row.updated_at, tuple(contributions), tuple(events), audits


def test_the_combined_page_shows_results_first_then_the_collapsible_inputs(owner) -> None:
    scenario_id = _create(
        owner,
        contribution_year_1="2027",
        contribution_amount_1="12000",
        event_year_1="2028",
        event_kind_1="expense_change",
        event_amount_1="-1200",
        event_label_1="Mortgage repaid",
    )
    page = owner.client.get(f"/fire/{scenario_id}")
    assert page.status_code == 200
    assert "data-fire-overview" in page.text
    assert "data-fire-results" in page.text
    assert f'hx-post="/fire/{scenario_id}/preview"' in page.text
    assert 'hx-trigger="change from:#fire-form delay:300ms' in page.text
    assert 'hx-include="#fire-form"' in page.text and 'hx-sync="this:replace"' in page.text
    assert 'hx-target="#fire-results"' in page.text
    assert 'hx-swap="innerHTML"' in page.text
    assert "data-fire-metrics" in page.text
    assert 'data-fire-progress="sheet"' in page.text
    assert 'data-fire-progress="today"' in page.text
    assert "Sheet progress" in page.text and "Today's-money progress" in page.text
    assert "data-fire-table" in page.text
    assert 'data-fire-basis="nominal"' in page.text
    assert "data-fire-zero-note" in page.text
    assert f"/fire/{scenario_id}/results.csv" in page.text
    assert "data-fire-net-worth-points" in page.text
    assert "data-fire-expenses-gain-points" in page.text
    assert "data-chart-canvas" in page.text
    assert "data-chart-ready" not in page.text
    assert "data-fire-form" in page.text
    assert 'name="basis" value="nominal"' in page.text
    assert "Mortgage repaid" in page.text and "12,000" in page.text
    # No cash event in this plan, so the table has no cash column.
    assert not re.search(r">Cash</th>", page.text)

    charts = _chart_data(page.text)
    assert charts[0]["labels"] == [str(year) for year in range(2026, 2076)]
    assert len(charts[0]["series"][0]["values"]) == 50
    assert charts[1]["series"][0]["name"] == "Expenses"
    assert charts[1]["series"][1]["name"] == "Investment gain"
    assert charts[0]["mark_points"]
    assert charts[1]["markers"]


def test_the_basis_toggle_switches_the_table_and_chart_to_real_values(owner) -> None:
    scenario_id = _create(owner)
    page = owner.client.get(f"/fire/{scenario_id}?basis=real")
    assert page.status_code == 200
    assert 'data-fire-basis="real"' in page.text
    assert re.search(
        r'<input[^>]*name="basis" value="real"[^>]*checked[^>]*data-fire-basis="real"',
        page.text,
    )
    assert 'href="?basis=' not in page.text
    assert "Today's money" in page.text
    charts = _chart_data(page.text)
    assert charts[0]["series"][0]["name"] == "Net worth in today's money"
    nominal = _chart_data(owner.client.get(f"/fire/{scenario_id}").text)
    assert charts[0]["series"][0]["values"] != nominal[0]["series"][0]["values"]
    # The expenses-versus-gain chart stays nominal in both views.
    assert charts[1]["series"] == nominal[1]["series"]

    # Viewers keep plain links (no form, no preview), and those links keep the open panel.
    owner.set_admin_role("viewer")
    viewer = owner.client.get(f"/fire/{scenario_id}?basis=real&edit=1")
    assert viewer.status_code == 200
    assert re.search(r'aria-current="true"[^>]*data-fire-basis="real"', viewer.text)
    assert 'href="?basis=nominal&edit=1#fire-results"' in viewer.text
    assert 'name="basis"' not in viewer.text


def test_the_basis_control_is_part_of_the_form_not_a_navigation(owner) -> None:
    scenario_id = _create(owner)
    page = owner.client.get(f"/fire/{scenario_id}").text
    form_start = page.index('<form id="fire-form"')
    toggle = page.index("data-fire-basis-toggle")
    form_end = page.index("</form>", form_start)
    assert form_start < toggle < form_end
    assert 'type="hidden" name="basis"' not in page
    assert 'hx-preserve="true"' in page[toggle - 200 : toggle + 400]


def test_a_preview_posts_the_chosen_basis_with_the_unsaved_values_and_writes_nothing(
    owner,
) -> None:
    scenario_id = _create(owner)
    before = _scenario_snapshot(owner, scenario_id)
    saved_age = _fire_age(owner.client.get(f"/fire/{scenario_id}").text)
    preview = owner.post(
        f"/fire/{scenario_id}/preview",
        _form(basis="real", expenses_manual="80000"),
        token_from=f"/fire/{scenario_id}",
    )
    assert preview.status_code == 200
    assert "data-fire-preview-note" in preview.text
    assert 'data-fire-basis="real"' in preview.text
    assert re.search(
        r'<input[^>]*name="basis" value="real"[^>]*checked[^>]*data-fire-basis="real"',
        preview.text,
    )
    assert _fire_age(preview.text) == "43"
    assert _fire_age(preview.text) != saved_age
    charts = _chart_data(preview.text)
    assert charts[0]["series"][0]["name"] == "Net worth in today's money"
    assert _scenario_snapshot(owner, scenario_id) == before


def test_never_reached_and_depletion_states_are_explained(owner) -> None:
    scenario_id = _create(
        owner,
        real_return="0",
        inflation="0",
        income_growth="0",
        stop_working_age="41",
        income_manual="0",
        expenses_manual="30000",
        portfolio_manual="100000",
    )
    page = owner.client.get(f"/fire/{scenario_id}")
    assert page.status_code == 200
    assert "data-fire-never" in page.text
    assert "returns never cover expenses before age 90" in page.text
    assert "data-fire-depletion" in page.text
    assert "runs out at age 45" in page.text
    assert 'data-fire-row="fire"' not in page.text
    assert 'data-fire-depleted="true"' in page.text
    assert "data-fire-badge" not in page.text


def test_csv_has_both_bases_and_never_writes(owner) -> None:
    scenario_id = _create(
        owner,
        contribution_year_1="2027",
        contribution_amount_1="-1250.50",
        event_year_1="2028",
        event_kind_1="cash",
        event_amount_1="25000",
        event_label_1="Inheritance",
    )
    page = owner.client.get(f"/fire/{scenario_id}")
    assert re.search(r">Cash</th>", page.text)
    before = _scenario_snapshot(owner, scenario_id)
    response = owner.client.get(f"/fire/{scenario_id}/results.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == (
        f'attachment; filename="fire-scenario-{scenario_id}.csv"'
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    text = response.text
    assert text.startswith(
        "year,age,income,expenses,net,gain,cash,net_worth,"
        "real_income,real_expenses,real_net,real_gain,real_cash,real_net_worth,"
        "is_fire,income_stopped,currency\r\n"
    )
    rows = text.split("\r\n")
    assert len(rows) == 50 + 2  # header, 2026..2075 rows, trailing empty split
    assert rows[1].startswith("2026,41,50000.00,30000.00,1000000.00,")
    assert any(cell.startswith("-") for row in rows[1:] for cell in row.split(",")[2:14])
    assert "1" in [row.split(",")[14] for row in rows[1:] if row]
    assert _scenario_snapshot(owner, scenario_id) == before


def test_a_blocked_projection_hides_results_and_returns_409_for_csv(owner) -> None:
    scenario_id = _create(owner)
    with owner.db() as session, session.begin():
        row = session.get_one(FireScenario, scenario_id)
        row.income_mode = "trailing_12m"
        row.income_manual_minor = None
    page = owner.client.get(f"/fire/{scenario_id}")
    assert page.status_code == 200
    assert 'data-fire-blocked="no_income_data"' in page.text
    assert 'data-fire-blocked-field="income_manual"' in page.text
    assert "No regular income transactions" in page.text
    assert "data-fire-metrics" not in page.text
    assert "data-chart-json" not in page.text
    assert "data-fire-table" not in page.text
    csv = owner.client.get(f"/fire/{scenario_id}/results.csv")
    assert csv.status_code == 409


def test_a_blocked_plan_still_has_the_chart_scripts_a_preview_needs(owner) -> None:
    scenario_id = _create(owner)
    with owner.db() as session, session.begin():
        row = session.get_one(FireScenario, scenario_id)
        row.income_mode = "trailing_12m"
        row.income_manual_minor = None
    blocked = owner.client.get(f"/fire/{scenario_id}")
    assert 'data-fire-blocked="no_income_data"' in blocked.text
    assert "js/charts.js" in blocked.text
    assert "vendor/echarts.min.js" in blocked.text

    # Correcting the input in the preview brings the charts back even though the saved page had
    # none when it loaded; the script bootstrap must already be there.
    preview = owner.post(
        f"/fire/{scenario_id}/preview",
        _form(),
        token_from=f"/fire/{scenario_id}",
    )
    assert preview.status_code == 200
    assert "data-fire-charts" in preview.text
    assert "data-chart-json" in preview.text


def test_csv_is_readable_for_viewers_and_archived_scenarios(owner) -> None:
    scenario_id = _create(owner)
    owner.set_admin_role("viewer")
    viewer = owner.client.get(f"/fire/{scenario_id}/results.csv")
    assert viewer.status_code == 200
    assert viewer.text.startswith("year,age,income,")

    owner.set_admin_role("owner")
    owner.post(f"/fire/{scenario_id}/archive", token_from=f"/fire/{scenario_id}")
    archived = owner.client.get(f"/fire/{scenario_id}/results.csv")
    assert archived.status_code == 200
    assert archived.text.startswith("year,age,income,")


def test_preview_returns_only_the_fragment_with_changed_metrics_and_no_writes(owner) -> None:
    scenario_id = _create(owner)
    page = owner.client.get(f"/fire/{scenario_id}").text
    before = _scenario_snapshot(owner, scenario_id)
    saved_age = _fire_age(page)
    preview = owner.post(
        f"/fire/{scenario_id}/preview",
        _form(expenses_manual="80000"),
        token_from=f"/fire/{scenario_id}",
    )
    assert preview.status_code == 200
    assert "<html" not in preview.text.lower()
    assert 'id="fire-results"' not in preview.text
    assert "data-fire-preview-note" in preview.text
    assert "data-fire-metrics" in preview.text
    assert _fire_age(preview.text) == "43"
    assert _fire_age(preview.text) != saved_age
    assert _scenario_snapshot(owner, scenario_id) == before


def test_an_invalid_preview_keeps_the_saved_results_and_names_the_field(owner) -> None:
    scenario_id = _create(owner)
    saved_age = _fire_age(owner.client.get(f"/fire/{scenario_id}").text)
    before = _scenario_snapshot(owner, scenario_id)
    response = owner.post(
        f"/fire/{scenario_id}/preview",
        _form(horizon_age="30"),
        token_from=f"/fire/{scenario_id}",
    )
    assert response.status_code == 200
    assert "data-fire-preview-error" in response.text
    assert 'data-fire-preview-error-field="horizon_age"' in response.text
    assert "older than your age" in response.text
    assert "data-fire-metrics" in response.text
    assert _fire_age(response.text) == saved_age
    assert _scenario_snapshot(owner, scenario_id) == before


def test_preview_requires_csrf_an_editor_and_an_active_scenario(owner) -> None:
    scenario_id = _create(owner)
    without_token = owner.client.post(f"/fire/{scenario_id}/preview", data=_form())
    assert without_token.status_code == 403

    owner.set_admin_role("viewer")
    viewer = owner.post(f"/fire/{scenario_id}/preview", _form(), token_from=f"/fire/{scenario_id}")
    assert viewer.status_code == 403

    owner.set_admin_role("owner")
    archived = owner.post(f"/fire/{scenario_id}/archive", token_from=f"/fire/{scenario_id}")
    assert archived.status_code == 303
    blocked = owner.post(f"/fire/{scenario_id}/preview", _form(), token_from=f"/fire/{scenario_id}")
    assert blocked.status_code == 200
    assert "Archived scenarios are read-only" in blocked.text
    assert "data-fire-preview-error" in blocked.text
    assert "data-fire-metrics" in blocked.text


def test_viewers_and_archived_scenarios_are_read_only(owner) -> None:
    scenario_id = _create(owner)
    owner.set_admin_role("viewer")
    viewer = owner.client.get(f"/fire/{scenario_id}")
    assert viewer.status_code == 200
    assert "data-fire-results" in viewer.text
    assert "data-fire-metrics" in viewer.text
    assert "hx-post" not in viewer.text
    assert "hx-trigger" not in viewer.text
    assert "hx-sync" not in viewer.text
    assert f'hx-get="/fire/{scenario_id}/sensitivity"' in viewer.text
    assert 'hx-target="#fire-sensitivity"' in viewer.text
    assert "data-fire-form" not in viewer.text
    assert "data-fire-save" not in viewer.text
    assert 'name="basis"' not in viewer.text
    assert 'href="?basis=real#fire-results"' in viewer.text
    assert "data-fire-edit" not in viewer.text
    assert "data-fire-archive" not in viewer.text
    assert "Your role can read this plan but not change it." in viewer.text

    owner.set_admin_role("owner")
    owner.post(f"/fire/{scenario_id}/archive", token_from=f"/fire/{scenario_id}")
    archived = owner.client.get(f"/fire/{scenario_id}")
    assert archived.status_code == 200
    assert "hx-post" not in archived.text
    assert "data-fire-form" not in archived.text
    assert "data-fire-save" not in archived.text
    assert 'name="basis"' not in archived.text
    assert 'href="?basis=real#fire-results"' in archived.text
    assert "data-fire-metrics" in archived.text
    assert "data-fire-table" in archived.text
    assert "This scenario is archived and read-only" in archived.text


def test_an_update_error_renders_the_combined_page_with_the_form_open(owner) -> None:
    scenario_id = _create(owner)
    saved_age = _fire_age(owner.client.get(f"/fire/{scenario_id}").text)
    before = _scenario_snapshot(owner, scenario_id)
    response = owner.post(
        f"/fire/{scenario_id}",
        _form(horizon_age="30"),
        token_from=f"/fire/{scenario_id}",
    )
    assert response.status_code == 400
    assert "data-fire-results" in response.text
    assert "data-fire-metrics" in response.text
    assert re.search(r'<details id="inputs"[^>]* open', response.text)
    assert 'name="horizon_age" value="30"' in response.text
    assert _fire_age(response.text) == saved_age
    assert _scenario_snapshot(owner, scenario_id) == before


def test_saving_preserves_the_basis_children_and_the_saved_projection(owner) -> None:
    scenario_id = _create(
        owner,
        contribution_year_1="2027",
        contribution_amount_1="12000",
        event_year_1="2028",
        event_kind_1="expense_change",
        event_amount_1="-1200",
        event_label_1="Mortgage repaid",
    )
    response = owner.post(
        f"/fire/{scenario_id}",
        _form(
            name="Retire at 56",
            basis="real",
            contribution_year_1="2027",
            contribution_amount_1="12000",
            event_year_1="2028",
            event_kind_1="expense_change",
            event_amount_1="-1200",
            event_label_1="Mortgage repaid",
        ),
        token_from=f"/fire/{scenario_id}",
    )
    assert response.status_code == 303
    assert "basis=real" in response.headers["location"]
    page = owner.client.get(response.headers["location"])
    assert page.status_code == 200
    assert 'data-fire-basis="real"' in page.text
    assert "Retire at 56" in page.text
    assert "data-fire-metrics" in page.text
    with owner.db() as session:
        assert len(list(session.query(FireContribution).filter_by(scenario_id=scenario_id))) == 1
        event = session.query(FireEvent).filter_by(scenario_id=scenario_id).one()
        assert (event.label, event.amount_minor) == ("Mortgage repaid", -120_000)


def test_the_edit_route_redirects_to_the_inputs_anchor_and_archived_plans_are_refused(
    owner,
) -> None:
    scenario_id = _create(owner)
    edit = owner.client.get(f"/fire/{scenario_id}/edit", follow_redirects=False)
    assert edit.status_code == 303
    assert edit.headers["location"] == f"/fire/{scenario_id}?edit=1#inputs"
    opened = owner.client.get(f"/fire/{scenario_id}?edit=1")
    assert re.search(r'<details id="inputs"[^>]* open', opened.text)

    owner.post(f"/fire/{scenario_id}/archive", token_from=f"/fire/{scenario_id}")
    refused = owner.client.get(f"/fire/{scenario_id}/edit", follow_redirects=False)
    assert refused.status_code == 303
    assert refused.headers["location"] == f"/fire/{scenario_id}?error=archived"


def test_another_workspaces_scenario_is_a_404(owner) -> None:
    other = owner.add_workspace("Other")
    owner.join(owner.admin_id, other, "owner")
    owner.post("/workspace/switch", {"workspace_id": str(other)})
    other_scenario = _create(owner)
    owner.post("/workspace/switch", {"workspace_id": str(owner.home_id)})
    assert owner.client.get(f"/fire/{other_scenario}").status_code == 404
    assert owner.client.get(f"/fire/{other_scenario}/results.csv").status_code == 404


def test_the_scenario_page_keeps_its_currency_for_csv(owner) -> None:
    with owner.db() as session, session.begin():
        workspace = session.get_one(Workspace, owner.home_id)
        workspace.base_currency = "JPY"
    scenario_id = _create(
        owner,
        income_manual="5000000",
        expenses_manual="3000000",
        portfolio_manual="100000000",
    )
    response = owner.client.get(f"/fire/{scenario_id}/results.csv")
    assert response.status_code == 200
    rows = response.text.split("\r\n")
    assert rows[1].split(",")[2] == "5000000"
    assert "." not in ",".join(rows[1].split(",")[2:14])
    assert rows[1].split(",")[16] == "JPY"
