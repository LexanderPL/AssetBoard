"""The F8.9 one-plan entry, view, chooser and legacy URLs."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

from sqlalchemy import select

from assetboard.db.models.fire import FireScenario
from assetboard.services.context import Role
from tests.fire_helpers import create_plan


def _scenario_count(owner) -> int:
    with owner.db() as session:
        return len(
            session.scalars(
                select(FireScenario).where(FireScenario.workspace_id == owner.home_id)
            ).all()
        )


def _legacy(owner, name: str, *, is_default: bool, archived: bool = False) -> UUID:
    payload = {
        "v": 1,
        "name": name,
        "name_key": name.casefold(),
        "start_year": 2026,
        "is_default": is_default,
        "archived_at": "2026-01-01T00:00:00Z" if archived else None,
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


def test_the_nav_link_and_the_wizard_entry(owner) -> None:
    home = owner.client.get("/").text
    assert 'href="/fire"' in home and "data-fire-link" in home
    assert "Unavailable" not in home

    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert 'data-fire-wizard="1"' in page.text
    assert "Set up your FIRE plan" in page.text
    for marker in (
        'name="birth_year"',
        'name="horizon_age"',
        'name="stop_working_age"',
        "data-fire-wizard-form",
    ):
        assert marker in page.text
    assert 'name="name"' not in page.text
    assert 'name="income_mode"' not in page.text


def test_the_entry_renders_the_view_for_a_plan(owner) -> None:
    create_plan(owner)
    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert "data-fire-results" in page.text
    assert 'href="/fire/edit"' in page.text
    assert "/fire/results.csv" not in page.text
    assert "data-fire-reset" in page.text
    assert "data-fire-edit" in page.text
    assert "data-fire-form" not in page.text
    assert 'name="income_manual"' not in page.text
    assert "data-fire-plan-values" in page.text


def test_the_id_url_redirects_to_the_view(owner) -> None:
    plan_id = create_plan(owner)
    response = owner.client.get(f"/fire/{plan_id}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/fire"
    response = owner.client.get(f"/fire/{plan_id}?basis=real&sensitivity=1", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/fire?sensitivity=1#sensitivity"
    response = owner.client.get(f"/fire/{plan_id}?edit=1&basis=real", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/fire/edit"
    response = owner.client.get(f"/fire/{plan_id}/edit", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/fire/edit"
    response = owner.client.get(f"/fire/{plan_id}/results.csv", follow_redirects=False)
    assert response.status_code == 404
    response = owner.client.get(f"/fire/{plan_id}/sensitivity", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/fire/sensitivity"
    assert owner.client.get(f"/fire/{uuid4()}").status_code == 404
    assert owner.client.get("/fire/not-a-uuid").status_code == 404


def test_the_edit_query_redirects_to_the_editor(owner) -> None:
    create_plan(owner)
    response = owner.client.get("/fire?edit=1&basis=real", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/fire/edit"


def test_the_edit_query_is_ignored_without_a_plan(owner) -> None:
    page = owner.client.get("/fire?edit=1")
    assert page.status_code == 200
    assert 'data-fire-wizard="1"' in page.text


def test_a_viewer_reads_the_view_without_edit_or_reset(owner) -> None:
    create_plan(owner)
    owner.set_admin_role(Role.VIEWER.value)
    try:
        page = owner.client.get("/fire")
        assert page.status_code == 200
        assert "data-fire-results" in page.text
        assert "data-fire-edit" not in page.text
        assert "data-fire-reset" not in page.text
        assert "data-fire-form" not in page.text
        assert "data-fire-plan-values" in page.text
        assert owner.client.get("/fire/reset").status_code == 403
        assert owner.client.get("/fire/edit").status_code == 403
        assert owner.client.get("/fire?edit=1", follow_redirects=True).status_code == 403
    finally:
        owner.set_admin_role(Role.OWNER.value)


def test_one_legacy_plan_auto_migrates_for_an_editor(owner) -> None:
    legacy_id = _legacy(owner, "Old plan", is_default=True)
    page = owner.client.get("/fire", follow_redirects=True)
    assert page.status_code == 200
    assert "converted to the one-plan format" in page.text
    assert "data-fire-results" in page.text
    with owner.db() as session:
        row = session.get_one(FireScenario, legacy_id)
        assert row.is_live is True
        assert row.legacy is None


def test_multiple_legacy_plans_show_the_chooser_and_delete_the_others(owner) -> None:
    first = _legacy(owner, "First plan", is_default=True)
    second = _legacy(owner, "Second plan", is_default=False)
    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert "This workspace has old FIRE scenarios" in page.text
    assert "First plan" in page.text and "Second plan" in page.text
    assert "data-fire-choose-form" in page.text

    response = owner.post(
        "/fire/choose",
        {"scenario_id": str(second), "confirm": "1"},
        token_from="/fire",
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/fire?notice=migrated"
    with owner.db() as session:
        assert session.get(FireScenario, first) is None
        assert session.get_one(FireScenario, second).is_live is True


def test_the_chooser_requires_confirmation(owner) -> None:
    _legacy(owner, "First plan", is_default=True)
    second = _legacy(owner, "Second plan", is_default=False)
    response = owner.post(
        "/fire/choose",
        {"scenario_id": str(second)},
        token_from="/fire",
    )
    assert response.status_code == 303
    assert "error=choice_not_confirmed" in response.headers["location"]
    assert _scenario_count(owner) == 2


def test_a_viewer_sees_the_chooser_without_controls(owner) -> None:
    _legacy(owner, "First plan", is_default=True)
    _legacy(owner, "Second plan", is_default=False)
    owner.set_admin_role(Role.VIEWER.value)
    try:
        page = owner.client.get("/fire")
        assert page.status_code == 200
        assert "An editor or owner must choose" in page.text
        assert "data-fire-choose-confirm" not in page.text
    finally:
        owner.set_admin_role(Role.OWNER.value)


def test_the_old_create_post_is_gone(owner) -> None:
    response = owner.post("/fire", {"birth_year": "1985"}, token_from="/fire")
    assert response.status_code == 405
    assert _scenario_count(owner) == 0


def test_compare_redirects_and_old_actions_are_gone(owner) -> None:
    plan_id = create_plan(owner)
    compare = owner.client.get("/fire/compare", follow_redirects=False)
    assert compare.status_code == 303
    assert compare.headers["location"] == "/fire"
    for path in (
        f"/fire/{plan_id}/duplicate",
        f"/fire/{plan_id}/default",
        f"/fire/{plan_id}/archive",
        f"/fire/{plan_id}/restore",
        f"/fire/{plan_id}/loan-payoff",
    ):
        response = owner.post(path, {}, token_from="/fire")
        assert response.status_code in (404, 405)


def test_the_view_has_no_legacy_controls(owner) -> None:
    create_plan(owner)
    page = owner.client.get("/fire").text
    for marker in (
        "data-fire-compare",
        "data-fire-duplicate",
        "data-fire-default",
        "data-fire-archive",
        "data-fire-restore",
        "Compare scenarios",
        "Duplicate scenario",
        "Make default",
    ):
        assert marker not in page


def test_a_currency_mismatch_blocks_the_page_and_offers_reset(owner) -> None:
    plan_id = create_plan(owner)
    with owner.db() as session:
        session.get_one(FireScenario, plan_id).currency = "USD"
        session.commit()
    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert 'data-fire-blocked="plan_currency_mismatch"' in page.text
    assert "The projection is unavailable" in page.text
    assert "data-fire-blocked-reset" in page.text
    assert "data-fire-reset" in page.text
    assert "data-fire-edit" not in page.text
    edit = owner.client.get("/fire/edit", follow_redirects=False)
    assert edit.status_code == 303
    assert edit.headers["location"] == "/fire"


def test_the_needs_review_banner_links_to_the_editor(owner) -> None:
    plan_id = create_plan(owner)
    with owner.db() as session:
        session.get_one(FireScenario, plan_id).needs_review = True
        session.commit()
    page = owner.client.get("/fire")
    assert "data-fire-needs-review" in page.text
    assert "/fire/edit#amounts" in page.text


def test_the_new_alias_redirects(owner) -> None:
    response = owner.client.get("/fire/new", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/fire"
