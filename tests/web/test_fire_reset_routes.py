"""The F8.9 Reset plan page: typed confirmation, audit, roles, CSRF and the archive link."""

from __future__ import annotations

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
        "event_id_2": "",
        "event_year_2": "",
        "event_kind_2": "cash",
        "event_amount_2": "",
        "event_label_2": "",
    }
    data.update(overrides)
    return data


def _plan_with_events(owner) -> UUID:
    plan_id = create_plan(owner)
    response = owner.post(
        "/fire/edit",
        _form(
            event_year_1="2027",
            event_kind_1="cash",
            event_amount_1="1000",
            event_label_1="Bonus",
            event_year_2="2028",
            event_kind_2="expense_change",
            event_amount_2="-200",
            event_label_2="Repaid",
        ),
        token_from="/fire/edit",
    )
    assert response.status_code == 303, response.text
    return plan_id


def _counts(owner) -> tuple[int, int, int]:
    with owner.db() as session:
        return (
            session.scalar(select(func.count()).select_from(FireScenario)) or 0,
            session.scalar(select(func.count()).select_from(FireEvent)) or 0,
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "fire_plan.deleted")
            )
            or 0,
        )


def test_the_reset_page_shows_the_plan_counts_and_archive_link(owner) -> None:
    _plan_with_events(owner)
    page = owner.client.get("/fire/reset")
    assert page.status_code == 200
    assert "data-fire-reset-page" in page.text
    assert "2 adjustments" in page.text
    assert "data-fire-reset-confirm" in page.text
    assert "data-fire-archive-link" in page.text
    assert "/workspace/data" in page.text
    assert "reset" in page.text
    assert _counts(owner) == (1, 2, 0)


def test_a_wrong_or_empty_confirmation_keeps_the_plan(owner) -> None:
    _plan_with_events(owner)
    response = owner.post("/fire/reset", {"confirm": "delete"}, token_from="/fire")
    assert response.status_code == 400
    assert "data-action-error" in response.text
    assert _counts(owner) == (1, 2, 0)
    response = owner.post("/fire/reset", {"confirm": ""}, token_from="/fire")
    assert response.status_code == 400
    assert _counts(owner) == (1, 2, 0)


def test_the_confirmation_is_trimmed_and_case_insensitive_and_deletes_once(owner) -> None:
    plan_id = _plan_with_events(owner)
    response = owner.post("/fire/reset", {"confirm": "  ReSeT  "}, token_from="/fire")
    assert response.status_code == 303
    assert response.headers["location"] == "/fire?notice=reset"
    with owner.db() as session:
        assert session.get(FireScenario, plan_id) is None
        assert session.scalar(select(func.count()).select_from(FireEvent)) == 0
        audit = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "fire_plan.deleted")
        ).one()
        assert audit.entity_id == str(plan_id)
        assert audit.data["events_deleted"] == 2
        assert not any(key in audit.data for key in ("income", "expenses", "portfolio", "amount"))
    page = owner.client.get("/fire?notice=reset")
    assert page.status_code == 200
    assert "FIRE plan reset" in page.text
    assert 'data-fire-wizard="1"' in page.text


def test_a_viewer_cannot_reset(owner) -> None:
    _plan_with_events(owner)
    owner.set_admin_role(Role.VIEWER.value)
    try:
        assert owner.client.get("/fire/reset").status_code == 403
        response = owner.post("/fire/reset", {"confirm": "reset"}, token_from="/fire")
        assert response.status_code == 403
        assert _counts(owner) == (1, 2, 0)
    finally:
        owner.set_admin_role(Role.OWNER.value)


def test_reset_requires_csrf(owner) -> None:
    _plan_with_events(owner)
    response = owner.client.post("/fire/reset", data={"confirm": "reset"})
    assert response.status_code == 403
    assert _counts(owner) == (1, 2, 0)


def test_reset_without_a_plan_is_not_found(owner) -> None:
    assert owner.client.get("/fire/reset").status_code == 404
    assert owner.post("/fire/reset", {"confirm": "reset"}, token_from="/fire").status_code == 404


def test_reset_works_with_a_currency_mismatch_and_the_new_plan_uses_the_workspace_currency(
    owner,
) -> None:
    plan_id = create_plan(owner)
    with owner.db() as session:
        session.get_one(FireScenario, plan_id).currency = "USD"
        session.commit()
    page = owner.client.get("/fire/reset")
    assert "data-fire-currency-warning" in page.text
    response = owner.post("/fire/reset", {"confirm": "reset"}, token_from="/fire")
    assert response.status_code == 303
    assert _counts(owner) == (0, 0, 1)
    new_id = create_plan(owner)
    with owner.db() as session:
        assert session.get_one(FireScenario, new_id).currency == "EUR"


def test_a_second_reset_is_not_found(owner) -> None:
    create_plan(owner)
    assert owner.post("/fire/reset", {"confirm": "reset"}, token_from="/fire").status_code == 303
    assert owner.post("/fire/reset", {"confirm": "reset"}, token_from="/fire").status_code == 404


def test_a_viewer_sees_no_reset_link(owner) -> None:
    create_plan(owner)
    owner.set_admin_role(Role.VIEWER.value)
    try:
        page = owner.client.get("/fire").text
        assert "data-fire-reset" not in page
    finally:
        owner.set_admin_role(Role.OWNER.value)
