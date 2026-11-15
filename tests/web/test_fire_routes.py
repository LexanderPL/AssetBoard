"""The F8.8 one-plan FIRE entry, create form and chooser."""

from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import select

from assetboard.db.models.fire import FireScenario
from assetboard.services.context import Role


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
    response = owner.post("/fire", _form(**overrides), token_from="/fire")
    assert response.status_code == 303, response.text
    return UUID(response.headers["location"].split("?")[0].rsplit("/", 1)[-1])


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


def test_the_nav_link_and_the_create_form(owner) -> None:
    home = owner.client.get("/").text
    assert 'href="/fire"' in home and "data-fire-link" in home
    assert "Unavailable" not in home

    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert "Create your FIRE plan" in page.text
    assert "not financial advice" in page.text
    for marker in (
        'name="birth_year"',
        'name="horizon_age"',
        'name="stop_working_age"',
        'name="inflation"',
        'name="real_return"',
        'name="income_growth"',
        'name="rule"',
        'name="swr"',
        'name="income_manual"',
        'name="expenses_manual"',
        'name="portfolio_manual"',
    ):
        assert marker in page.text
    assert 'name="name"' not in page.text
    assert 'name="income_mode"' not in page.text
    assert 'name="contribution_amount_1"' not in page.text


def test_the_create_page_has_no_adjustments_editor(owner) -> None:
    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert "data-fire-events" not in page.text
    assert "data-event-table" not in page.text
    assert "data-add-event" not in page.text
    assert 'name="event_year_1"' not in page.text
    assert 'name="event_amount_1"' not in page.text
    assert 'name="event_label_1"' not in page.text
    assert 'name="event_kind_1"' not in page.text
    assert 'name="event_id_1"' not in page.text


def test_create_redirects_to_the_plan_and_writes_once(owner) -> None:
    plan_id = _create(owner)
    page = owner.client.get(f"/fire/{plan_id}")
    assert page.status_code == 200
    assert "FIRE plan" in page.text
    assert "data-fire-overview" in page.text
    assert _scenario_count(owner) == 1
    assert owner.client.get("/fire", follow_redirects=False).status_code == 303


def test_a_second_create_is_refused(owner) -> None:
    _create(owner)
    response = owner.post("/fire", _form(), token_from="/fire")
    assert response.status_code == 400
    assert "already has a FIRE plan" in response.text
    assert _scenario_count(owner) == 1


def test_events_are_not_allowed_on_create(owner) -> None:
    response = owner.post(
        "/fire",
        _form(
            event_year_1="2027",
            event_kind_1="cash",
            event_amount_1="100",
            event_label_1="Bonus",
        ),
        token_from="/fire",
    )
    assert response.status_code == 400
    assert "Create the plan first" in response.text
    assert "data-fire-events" not in response.text
    assert 'name="event_year_1"' not in response.text
    assert _scenario_count(owner) == 0


def test_a_viewer_reads_but_cannot_create(owner) -> None:
    owner.set_admin_role(Role.VIEWER.value)
    try:
        page = owner.client.get("/fire")
        assert page.status_code == 200
        assert "Create your FIRE plan" in page.text
        response = owner.post("/fire", _form(), token_from="/fire")
        assert response.status_code == 403
        assert _scenario_count(owner) == 0
    finally:
        owner.set_admin_role(Role.OWNER.value)


def test_one_legacy_plan_auto_migrates_for_an_editor(owner) -> None:
    legacy_id = _legacy(owner, "Old plan", is_default=True)
    page = owner.client.get("/fire", follow_redirects=True)
    assert page.status_code == 200
    assert "converted to the one-plan format" in page.text
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


def test_compare_redirects_and_old_actions_are_gone(owner) -> None:
    plan_id = _create(owner)
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
        response = owner.post(path, {}, token_from=f"/fire/{plan_id}")
        assert response.status_code in (404, 405)


def test_a_currency_mismatch_blocks_the_page(owner) -> None:
    plan_id = _create(owner)
    with owner.db() as session:
        session.get_one(FireScenario, plan_id).currency = "USD"
        session.commit()
    page = owner.client.get(f"/fire/{plan_id}")
    assert page.status_code == 200
    assert 'data-fire-blocked="plan_currency_mismatch"' in page.text
    assert "The projection is unavailable" in page.text


def test_the_needs_review_banner_links_to_the_inputs(owner) -> None:
    plan_id = _create(owner)
    with owner.db() as session:
        session.get_one(FireScenario, plan_id).needs_review = True
        session.commit()
    page = owner.client.get(f"/fire/{plan_id}")
    assert "data-fire-needs-review" in page.text
    assert f"/fire/{plan_id}?edit=1#inputs" in page.text
