"""The F8.8 one-plan results page, preview fragment and CSV."""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

from sqlalchemy import func, select

from assetboard.db.models.fire import FireEvent, FireScenario
from assetboard.db.models.identity import AuditEvent
from assetboard.services.context import Role
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


def _fire_age(html: str) -> str:
    match = re.search(r'data-fire-fire-age-value="([^"]*)"', html)
    assert match is not None
    return match.group(1)


def _fire_number(html: str) -> str:
    match = re.search(r'data-fire-number-value="([^"]*)"', html)
    assert match is not None
    return match.group(1)


def _chart_data(html: str) -> list[dict[str, Any]]:
    values = re.findall(r"data-chart-json='([^']*)'", html)
    assert len(values) == 2
    return [json.loads(value) for value in values]


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


def test_the_combined_page_shows_results_first_then_the_collapsible_inputs(owner) -> None:
    plan_id = _create(owner)
    response = owner.post(
        f"/fire/{plan_id}",
        _form(
            event_year_1="2028",
            event_kind_1="expense_change",
            event_amount_1="-1200",
            event_label_1="Mortgage repaid",
        ),
        token_from="/fire?edit=1",
    )
    assert response.status_code == 303, response.text
    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert "data-fire-overview" in page.text
    assert "data-fire-results" in page.text
    assert f'hx-post="/fire/{plan_id}/preview"' in page.text
    assert 'hx-trigger="change from:#fire-form delay:300ms' in page.text
    assert 'hx-include="#fire-form"' in page.text and 'hx-sync="this:replace"' in page.text
    assert 'hx-target="#fire-results"' in page.text
    assert 'hx-swap="innerHTML"' in page.text
    assert "data-fire-metrics" in page.text
    assert 'data-fire-progress="sheet"' in page.text
    assert 'data-fire-progress="today"' in page.text
    assert "data-fire-table" in page.text
    assert 'data-fire-basis="nominal"' in page.text
    assert f"/fire/{plan_id}/results.csv" in page.text
    assert "data-fire-net-worth-points" in page.text
    assert "data-fire-expenses-gain-points" in page.text
    assert "data-chart-canvas" in page.text
    assert "data-chart-ready" not in page.text
    assert "data-fire-form" in page.text
    assert 'name="basis" value="nominal"' in page.text
    assert "Mortgage repaid" in page.text
    charts = _chart_data(page.text)
    assert charts[0]["labels"] == [str(year) for year in range(2026, 2076)]
    assert len(charts[0]["series"][0]["values"]) == 50
    assert charts[1]["series"][0]["name"] == "Expenses"
    assert charts[1]["series"][1]["name"] == "Investment gain"


def test_the_basis_toggle_switches_the_table_and_chart_to_real_values(owner) -> None:
    _create(owner)
    page = owner.client.get("/fire?basis=real")
    assert page.status_code == 200
    assert 'data-fire-basis="real"' in page.text
    assert re.search(
        r'<input[^>]*name="basis" value="real"[^>]*checked[^>]*data-fire-basis="real"',
        page.text,
    )
    assert 'href="?basis=' not in page.text


def test_a_preview_posts_the_chosen_basis_with_unsaved_values_and_writes_nothing(owner) -> None:
    plan_id = _create(owner)
    before = _snapshot(owner, plan_id)
    response = owner.post(
        f"/fire/{plan_id}/preview",
        _form(basis="real", income_manual="60000"),
        token_from="/fire?edit=1",
    )
    assert response.status_code == 200
    assert "Preview of unsaved changes" in response.text
    assert 'data-fire-basis="real"' in response.text
    assert 'id="fire-results"' not in response.text  # fragment only
    assert _snapshot(owner, plan_id) == before
    assert _fire_age(response.text) != ""


def test_never_reached_and_depletion_states_are_explained(owner) -> None:
    _create(
        owner,
        real_return="-5",
        income_manual="10000",
        expenses_manual="50000",
        portfolio_manual="100000",
        extreme_rates_confirmed="1",
    )
    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert "data-fire-sustainability" in page.text
    assert "runs out at age" in page.text or "never" in page.text.lower()


def test_csv_has_both_bases_and_never_writes(owner) -> None:
    plan_id = _create(owner)
    before = _snapshot(owner, plan_id)
    response = owner.client.get(f"/fire/{plan_id}/results.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.text.startswith("year,age,income,expenses,net,gain,cash,net_worth")
    assert "real_income" in response.text.splitlines()[0]
    assert _snapshot(owner, plan_id) == before


def test_a_blocked_projection_hides_results_and_returns_409_for_csv(owner) -> None:
    plan_id = _create(owner)
    with owner.db() as session:
        session.get_one(FireScenario, plan_id).currency = "USD"
        session.commit()
    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert 'data-fire-blocked="plan_currency_mismatch"' in page.text
    assert "data-fire-metrics" not in page.text
    csv_response = owner.client.get(f"/fire/{plan_id}/results.csv")
    assert csv_response.status_code == 409


def test_preview_returns_only_the_fragment_with_changed_metrics(owner) -> None:
    plan_id = _create(owner)
    saved = owner.client.get("/fire").text
    response = owner.post(
        f"/fire/{plan_id}/preview",
        _form(expenses_manual="40000"),
        token_from="/fire?edit=1",
    )
    assert response.status_code == 200
    assert "data-fire-metrics" in response.text
    assert _fire_number(response.text) != _fire_number(saved)


def test_an_invalid_preview_keeps_saved_results_and_names_the_field(owner) -> None:
    plan_id = _create(owner)
    response = owner.post(
        f"/fire/{plan_id}/preview",
        _form(income_manual="-1"),
        token_from="/fire?edit=1",
    )
    assert response.status_code == 200
    assert "data-fire-preview-error" in response.text
    assert 'data-fire-preview-error-field="income_manual"' in response.text
    assert "data-fire-metrics" in response.text


def test_preview_requires_csrf_an_editor_and_the_live_plan(owner) -> None:
    plan_id = _create(owner)
    missing = owner.client.post(f"/fire/{plan_id}/preview", data=_form())
    assert missing.status_code in (400, 403)
    owner.set_admin_role(Role.VIEWER.value)
    try:
        response = owner.post(
            f"/fire/{plan_id}/preview",
            _form(),
            token_from="/fire?edit=1",
        )
        assert response.status_code == 403
    finally:
        owner.set_admin_role(Role.OWNER.value)


def test_viewers_are_read_only(owner) -> None:
    _create(owner)
    owner.set_admin_role(Role.VIEWER.value)
    try:
        page = owner.client.get("/fire")
        assert page.status_code == 200
        assert "data-fire-form" not in page.text
        assert "data-fire-edit" not in page.text
    finally:
        owner.set_admin_role(Role.OWNER.value)


def test_an_update_error_renders_the_combined_page_with_the_form_open(owner) -> None:
    plan_id = _create(owner)
    response = owner.post(
        f"/fire/{plan_id}",
        _form(horizon_age="41"),
        token_from="/fire?edit=1",
    )
    assert response.status_code == 400
    assert "data-fire-form" in response.text
    assert 'id="inputs"' in response.text
    assert "horizon" in response.text.lower()


def test_saving_preserves_the_basis_and_events(owner) -> None:
    plan_id = _create(owner)
    response = owner.post(
        f"/fire/{plan_id}?basis=real",
        _form(
            basis="real",
            event_year_1="2028",
            event_kind_1="saving_override",
            event_amount_1="-2000",
            event_label_1="Sabbatical",
        ),
        token_from="/fire?edit=1",
    )
    assert response.status_code == 303
    assert "basis=real" in response.headers["location"]
    with owner.db() as session:
        events = session.scalars(select(FireEvent).where(FireEvent.scenario_id == plan_id)).all()
        assert [(row.year, row.kind) for row in events] == [(2028, "saving_override")]
        assert session.get_one(FireScenario, plan_id).needs_review is False


def test_the_edit_route_redirects_to_the_inputs_anchor(owner) -> None:
    plan_id = _create(owner)
    response = owner.client.get(f"/fire/{plan_id}/edit", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/fire?edit=1#inputs"


def test_another_workspace_is_a_404(owner) -> None:
    other = owner.add_workspace("Other")
    assert owner.client.get(f"/fire/{other}").status_code == 404


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
