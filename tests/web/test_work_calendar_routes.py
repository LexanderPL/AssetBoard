"""F3.7 work calendar pages: HTMX fragments, redirects without JavaScript, CSRF and roles."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from assetboard.db.models.income import IncomeSource, WorkDay, WorkLog
from tests.web.test_income_routes import _form, _income_category

HTMX = {"HX-Request": "true"}


def _source_id(owner) -> str:
    assert (
        owner.post("/income", _form(_income_category(owner)), token_from="/income").status_code
        == 303
    )
    with owner.db() as session:
        return str(session.scalars(select(IncomeSource.id)).one())


def _htmx_post(owner, path: str, data: dict[str, str] | None = None):
    form = {"csrf_token": owner.token("/income")} | (data or {})
    return owner.client.post(path, data=form, headers=HTMX, follow_redirects=False)


def test_calendar_page_toggle_edit_fill_and_clear(owner):
    source_id = _source_id(owner)
    base = f"/income/{source_id}/calendar"
    page = owner.client.get(f"{base}?month=2026-08")
    assert page.status_code == 200
    assert "<html" in page.text and 'id="work-calendar"' in page.text
    assert 'data-day="2026-08-01"' in page.text and "data-weekend" in page.text
    assert "?month=2026-07" in page.text and "?month=2026-09" in page.text

    toggled = _htmx_post(owner, f"{base}/days/2026-08-03/toggle")
    assert toggled.status_code == 200
    assert "<html" not in toggled.text
    assert 'aria-pressed="true"' in toggled.text
    assert "Month total: 1 (day)" in toggled.text

    saved = _htmx_post(owner, f"{base}/days/2026-08-03", {"units": "0,5"})
    assert saved.status_code == 200 and "Month total: 0.5 (day)" in saved.text

    refused = _htmx_post(owner, f"{base}/days/2026-08-03", {"units": "25"})
    assert refused.status_code == 409
    assert "at most 24" in refused.text

    filled = _htmx_post(owner, f"{base}/fill", {"month": "2026-08"})
    assert filled.status_code == 200
    # August 2026 has 21 weekdays (no country set); 3 August keeps its 0.5.
    assert "Month total: 20.5 (day)" in filled.text
    with owner.db() as session:
        log = session.scalars(select(WorkLog)).one()
        assert (log.units, log.derived_from_days) == (Decimal("20.5"), True)

    income_page = owner.client.get("/income")
    assert "data-derived-log" in income_page.text and "data-work-calendar-link" in income_page.text
    manual = owner.post(
        f"/income/{source_id}/work-logs",
        {"work_month": "2026-08-01", "units": "3"},
        token_from="/income",
    )
    assert "error=derived_work_log" in manual.headers["location"]

    cleared = owner.post(f"{base}/clear", {"month": "2026-08"}, token_from="/income")
    assert cleared.status_code == 303
    assert cleared.headers["location"].endswith(f"{base}?month=2026-08")
    with owner.db() as session:
        assert session.scalars(select(WorkDay)).first() is None
        assert session.scalars(select(WorkLog)).first() is None


def test_calendar_navigation_returns_a_fragment(owner):
    source_id = _source_id(owner)
    fragment = owner.client.get(f"/income/{source_id}/calendar?month=2026-09", headers=HTMX)
    assert fragment.status_code == 200
    assert "<html" not in fragment.text and 'data-day="2026-09-30"' in fragment.text


def test_calendar_posts_require_csrf(owner):
    source_id = _source_id(owner)
    response = owner.client.post(
        f"/income/{source_id}/calendar/days/2026-08-03/toggle", follow_redirects=False
    )
    assert response.status_code == 403
    with owner.db() as session:
        assert session.scalars(select(WorkDay)).first() is None


def test_viewers_see_the_calendar_without_controls(owner):
    source_id = _source_id(owner)
    owner.set_admin_role("viewer")
    page = owner.client.get(f"/income/{source_id}/calendar?month=2026-08")
    assert page.status_code == 200
    assert "aria-pressed" not in page.text and "data-calendar-fill" not in page.text
    refused = owner.post(
        f"/income/{source_id}/calendar/days/2026-08-03/toggle", token_from="/income"
    )
    assert refused.status_code == 403
