"""The F8.10 one-plan view page: read-only results, values, adjustments and controls."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy import event, func, select

from assetboard.db.models.fire import FireEvent, FireScenario
from assetboard.db.models.identity import AuditEvent
from tests.fire_helpers import create_plan


def _create(owner, **overrides: str) -> UUID:
    return create_plan(owner, **overrides)


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


def test_the_view_is_read_only_and_shows_results_values_and_adjustments(owner) -> None:
    _create(owner)
    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert "data-fire-view" in page.text
    assert "data-fire-overview" in page.text
    assert "data-fire-results" in page.text
    assert "data-fire-metrics" in page.text
    assert "data-fire-table" in page.text
    assert "data-fire-plan-values" in page.text
    assert "data-fire-adjustments-view" in page.text
    assert "data-fire-loans" in page.text
    assert "data-fire-sensitivity" in page.text
    assert "data-fire-edit" in page.text
    assert "data-fire-reset" in page.text
    assert "data-fire-csv" in page.text
    # No editor at all: no form, no inputs, no preview hooks, no suggestion cards.
    assert "data-fire-form" not in page.text
    assert "data-fire-preview" not in page.text
    assert "hx-post=" not in page.text
    assert 'name="income_manual"' not in page.text
    assert 'name="birth_year"' not in page.text
    assert "data-fire-suggestion" not in page.text
    assert "data-fire-edit-page" not in page.text


def test_the_view_basis_links_switch_the_table_and_charts(owner) -> None:
    _create(owner)
    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert 'href="/fire?basis=real#fire-results"' in page.text
    assert 'href="/fire?basis=nominal#fire-results"' in page.text
    assert 'name="basis"' not in page.text
    page = owner.client.get("/fire?basis=real")
    assert page.status_code == 200
    assert 'data-fire-basis="real"' in page.text
    assert re.search(
        r'<a[^>]*href="/fire\?basis=real#fire-results"[^>]*aria-current="true"', page.text
    )


def test_the_view_writes_nothing(owner) -> None:
    plan_id = _create(owner)
    before = _snapshot(owner, plan_id)
    for path in ("/fire", "/fire?basis=real", "/fire?sensitivity=1"):
        assert owner.client.get(path).status_code == 200
    assert _snapshot(owner, plan_id) == before


def test_a_viewer_sees_no_edit_or_reset_and_no_form(owner) -> None:
    _create(owner)
    owner.set_admin_role("viewer")
    try:
        page = owner.client.get("/fire")
        assert page.status_code == 200
        assert "data-fire-view" in page.text
        assert "data-fire-plan-values" in page.text
        assert "data-fire-edit" not in page.text
        assert "data-fire-reset" not in page.text
        assert "data-fire-form" not in page.text
        assert 'name="archived_remove' not in page.text
        assert "data-adjustment-remove" not in page.text
    finally:
        owner.set_admin_role("owner")


def test_a_blocked_projection_hides_results_and_the_csv_link(owner) -> None:
    plan_id = _create(owner)
    with owner.db() as session:
        session.get_one(FireScenario, plan_id).currency = "USD"
        session.commit()
    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert 'data-fire-blocked="plan_currency_mismatch"' in page.text
    assert "data-fire-metrics" not in page.text
    assert "data-fire-csv" not in page.text
    assert "data-fire-blocked-reset" in page.text
    response = owner.client.get("/fire/results.csv")
    assert response.status_code == 409


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
    assert "data-fire-archived-adjustments" in page.text
    assert "data-fire-adjustments-empty" in page.text
    assert 'name="archived_remove' not in page.text
    assert 'name="event_' not in page.text


def test_archived_adjustments_show_their_reason(owner) -> None:
    plan_id = _create(owner)
    with owner.db() as session, session.begin():
        session.add_all(
            (
                FireEvent(
                    workspace_id=owner.home_id,
                    scenario_id=plan_id,
                    year=2020,
                    kind="cash",
                    amount_minor=100_000,
                    label="Past bonus",
                ),
                FireEvent(
                    workspace_id=owner.home_id,
                    scenario_id=plan_id,
                    year=2080,
                    kind="cash",
                    amount_minor=200_000,
                    label="After horizon",
                ),
                FireEvent(
                    workspace_id=owner.home_id,
                    scenario_id=plan_id,
                    year=2026,
                    kind="saving_override",
                    amount_minor=300_000,
                    label="Current override",
                ),
            )
        )
    page = owner.client.get("/fire").text
    assert "Before the current year" in page
    assert "After the plan" in page and "last year" in page
    assert "Saving override in the current year" in page


def test_the_view_has_no_suggestion_cards(owner) -> None:
    _create(owner)
    page = owner.client.get("/fire")
    assert "data-use-suggestion" not in page.text
    assert "data-suggested-minor" not in page.text
    assert "data-fire-suggestion" not in page.text


def test_the_view_markup_is_csp_safe(owner) -> None:
    _create(owner)
    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert not re.search(r"<script(?![^>]*\bsrc=)", page.text)
    assert "onclick=" not in page.text


def test_the_view_loads_no_editor_javascript(owner) -> None:
    _create(owner)
    page = owner.client.get("/fire")
    assert "js/fire.js" not in page.text
    assert "vendor/echarts.min.js" in page.text


def test_the_csv_is_id_free_and_writes_nothing(owner) -> None:
    plan_id = _create(owner)
    before = _snapshot(owner, plan_id)
    response = owner.client.get("/fire/results.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == 'attachment; filename="fire-plan.csv"'
    header = response.text.splitlines()[0]
    assert header.startswith("year,age,income,expenses,net,gain,cash,net_worth")
    assert "real_income" in header
    assert _snapshot(owner, plan_id) == before
    owner.set_admin_role("viewer")
    try:
        assert owner.client.get("/fire/results.csv").status_code == 200
    finally:
        owner.set_admin_role("owner")


def test_a_currency_mismatch_hides_the_review_banner_and_editor_link(owner) -> None:
    plan_id = _create(owner)
    with owner.db() as session:
        row = session.get_one(FireScenario, plan_id)
        row.currency = "USD"
        row.needs_review = True
        session.commit()
    page = owner.client.get("/fire")
    assert 'data-fire-blocked="plan_currency_mismatch"' in page.text
    assert "data-fire-needs-review" not in page.text
    assert "data-fire-edit" not in page.text
    assert "data-fire-reset" in page.text
    assert "/fire/edit" not in page.text


def test_the_view_query_count_does_not_grow_with_adjustments(owner) -> None:
    plan_id = _create(owner)
    seen = 0

    def before(*_args: object) -> None:
        nonlocal seen
        seen += 1

    def count() -> int:
        nonlocal seen
        seen = 0
        engine = owner.app.state.database_engine
        event.listen(engine, "before_cursor_execute", before)
        try:
            response = owner.client.get("/fire")
            assert response.status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", before)
        return seen

    quiet = count()
    with owner.db() as session, session.begin():
        for index in range(50):
            session.add(
                FireEvent(
                    workspace_id=owner.home_id,
                    scenario_id=plan_id,
                    year=2027 + (index % 40),
                    kind="cash",
                    amount_minor=index + 1,
                    label=f"Event {index}",
                )
            )
    loud = count()
    assert loud == quiet
    assert quiet <= 20


def test_a_viewer_sees_the_needs_review_banner_without_a_link(owner) -> None:
    plan_id = _create(owner)
    with owner.db() as session:
        session.get_one(FireScenario, plan_id).needs_review = True
        session.commit()
    owner.set_admin_role("viewer")
    try:
        page = owner.client.get("/fire")
        assert "data-fire-needs-review" in page.text
        assert "An editor or owner must review its values" in page.text
        assert "/fire/edit" not in page.text
    finally:
        owner.set_admin_role("owner")


def test_csv_and_sensitivity_without_a_plan_are_not_found(owner) -> None:
    assert owner.client.get("/fire/results.csv").status_code == 404
    assert owner.client.get("/fire/sensitivity").status_code == 404
