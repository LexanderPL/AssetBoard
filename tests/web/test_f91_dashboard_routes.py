"""F9.1: the Home widget shell, fragments, Customise page and preferences."""

from __future__ import annotations

import html as html_module
import json
import re
from datetime import date
from pathlib import Path
from uuid import UUID

import pytest
from httpx import Response
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.models.identity import User, Workspace
from assetboard.db.models.ledger import Category
from assetboard.db.repositories import DashboardPreferenceRepository, ImportBatchRepository
from assetboard.domain.dashboard import WIDGET_KEYS
from assetboard.domain.money import Money
from assetboard.services.context import Context, Role, UserRef, WorkspaceRef
from assetboard.services.demo import build_demo_workspace, plan_for_workspace
from assetboard.services.fire_scenarios import FirePlanInput, create_fire_plan
from assetboard.web import dashboard as dashboard_module
from assetboard.web.dashboard import WIDGETS
from tests.conftest import OwnerApp

NOW = "2026-03-10T12:00:00+00:00"
MONTH = "/m/2026-03"


def _fixed(app: OwnerApp, value: str = NOW) -> None:
    clock = FixedClock(value)
    app.app.state.clock = clock
    app.app.state.session_factory.configure(info={"clock": clock})


def _ctx(app: OwnerApp) -> Context:
    with app.db() as session:
        user = session.get(User, app.admin_id)
        workspace = session.get(Workspace, app.home_id)
        assert user is not None and workspace is not None
        return Context(
            user=UserRef(
                id=user.id,
                email=user.email,
                display_name=user.display_name,
                locale=user.locale,
                is_instance_admin=user.is_instance_admin,
            ),
            workspace=WorkspaceRef(
                id=workspace.id,
                name=workspace.name,
                base_currency=workspace.base_currency,
                locale=workspace.locale,
                timezone=workspace.timezone,
                country_code=workspace.country_code,
            ),
            role=Role.OWNER,
            clock=app.app.state.clock,
        )


def _fragment(app: OwnerApp, key: str) -> str:
    response = app.client.get(f"/dashboard/widgets/{key}", headers={"HX-Request": "true"})
    assert response.status_code == 200, (key, response.text)
    return response.text


def _chart_data(fragment: str) -> dict:
    match = re.search(r"data-chart-json='([^']*)'", fragment)
    assert match is not None
    return json.loads(html_module.unescape(match.group(1)))


def _widget_order(html: str) -> list[str]:
    return re.findall(r'data-widget="([^"]+)"', html)


def _customise_order(html: str) -> tuple[list[str], str]:
    assert html.count('name="enabled"') == len(WIDGET_KEYS)
    match = re.search(r'name="order" value="([^"]*)"', html)
    assert match is not None
    return re.findall(r'data-customise-widget="([^"]+)"', html), match.group(1)


def _post_many(
    app: OwnerApp, path: str, pairs: list[tuple[str, str]], *, token_from: str
) -> Response:
    token = app.token(token_from)
    payload: dict[str, str | list[str]] = {"csrf_token": token}
    for name, value in pairs:
        if name == "enabled":
            current = payload.get("enabled")
            if isinstance(current, list):
                current.append(value)
            else:
                payload["enabled"] = [value]
        else:
            payload[name] = value
    return app.client.post(path, data=payload, follow_redirects=False)


def _category(app: OwnerApp, name: str, kind: str = "expense") -> UUID:
    form = {"name": name, "kind": kind, "treatment": "regular", "color": "#16a34a"}
    assert app.post("/categories", form, token_from="/categories").status_code == 303
    with app.db() as session:
        return session.scalars(select(Category.id).where(Category.name == name)).one()


def _add(app: OwnerApp, category: UUID, amount: str, direction: str = "expense") -> None:
    form = {
        "direction": direction,
        "day": "5",
        "amount": amount,
        "description": "x",
        "category_id": str(category),
    }
    assert app.post(f"{MONTH}/transactions", form, token_from=MONTH).status_code == 303


def _planned_import(app: OwnerApp) -> UUID:
    with app.db() as session, session.begin():
        batch = ImportBatchRepository(session, _ctx(app)).create(
            source="transactions_csv",
            mode="merge",
            filename="history.csv",
            file_sha256="a" * 64,
            size_bytes=100,
            options={},
            status="planned",
            started_by_user_id=app.admin_id,
        )
        return batch.id


def test_the_widget_registry_matches_the_domain_keys_and_templates() -> None:
    assert tuple(widget.key for widget in WIDGETS) == WIDGET_KEYS
    templates = Path(dashboard_module.__file__).parent / "templates"
    for widget in WIDGETS:
        assert (templates / widget.template).is_file(), widget.key


def test_home_shell_lists_the_default_widgets_in_order(owner: OwnerApp) -> None:
    _fixed(owner)
    html = owner.client.get("/").text
    assert _widget_order(html) == list(WIDGET_KEYS)
    assert "data-customise-link" in html
    assert "data-home-month" in html and "March 2026" in html
    assert "data-summary-kpis" not in html  # the shell has no widget data
    assert 'hx-trigger="load"' in html and 'hx-swap="outerHTML"' in html
    assert "js/charts.js" in html and "vendor/echarts.min.js" in html


def test_widget_fragment_headers_and_unknown_key(owner: OwnerApp) -> None:
    _fixed(owner)
    non_htmx = owner.client.get("/dashboard/widgets/month-in-out", follow_redirects=False)
    assert non_htmx.status_code == 303
    assert non_htmx.headers["location"].startswith("/m/")
    fragment = owner.client.get("/dashboard/widgets/month-in-out", headers={"HX-Request": "true"})
    assert fragment.status_code == 200
    assert "HX-Request" in fragment.headers["vary"]
    assert (
        owner.client.get("/dashboard/widgets/nope", headers={"HX-Request": "true"}).status_code
        == 404
    )


@pytest.mark.parametrize("key", WIDGET_KEYS)
def test_every_widget_renders_without_data(owner: OwnerApp, key: str) -> None:
    _fixed(owner)
    html = _fragment(owner, key)
    assert f'data-home-widget="{key}"' in html
    assert "Traceback" not in html


@pytest.mark.parametrize(
    ("key", "empty_text"),
    [
        ("month-expenses", "No expenses yet this month"),
        ("month-income", "No income yet this month"),
        ("month-in-out", 'data-flow-segment="empty"'),
        ("upcoming", "Nothing is overdue or due in the next"),
        ("net-worth", "No values yet"),
        ("portfolio", "No holdings are valued yet"),
        ("portfolio-changes", "No portfolio history yet"),
        ("loans", "No active loans yet"),
        ("fire", "No FIRE plan yet"),
        ("missing-data", "Nothing needs attention."),
    ],
)
def test_widget_empty_states(owner: OwnerApp, key: str, empty_text: str) -> None:
    _fixed(owner)
    assert empty_text in _fragment(owner, key)


def test_every_widget_links_to_its_module(owner: OwnerApp) -> None:
    _fixed(owner)
    expected = {
        "month-in-out": MONTH,
        "month-expenses": MONTH,
        "month-income": MONTH,
        "month-plan": MONTH,
        "upcoming": "/upcoming",
        "net-worth": "/net-worth/summary",
        "portfolio": "/investments/portfolio",
        "portfolio-changes": "/investments/portfolio",
        "loans": "/loans",
        "fire": "/fire",
        "missing-data": "/workspace/data",
    }
    for key, href in expected.items():
        assert f'href="{href}"' in _fragment(owner, key), key


def test_month_widgets_render_actual_data(owner: OwnerApp) -> None:
    _fixed(owner)
    owner.clear_categories()
    food = _category(owner, "Food", "expense")
    salary = _category(owner, "Salary", "income")
    _add(owner, food, "25")
    _add(owner, salary, "2000", "income")

    flow = _fragment(owner, "month-in-out")
    assert "data-summary-kpis" in flow and 'data-kpi="earned"' in flow
    assert "Food" not in flow  # the flow card does not carry category slices

    expenses = _fragment(owner, "month-expenses")
    expense_data = _chart_data(expenses)
    assert [slice_["name"] for slice_ in expense_data["slices"]] == ["Food"]
    income = _fragment(owner, "month-income")
    income_data = _chart_data(income)
    assert [slice_["name"] for slice_ in income_data["slices"]] == ["Salary"]

    plan = _fragment(owner, "month-plan")
    assert "data-forecast-line" in plan and "Forecast spent" in plan
    assert "data-savings-rate" in plan


def test_the_plan_fragment_defines_its_own_overspend_pattern(owner: OwnerApp) -> None:
    _fixed(owner)
    owner.clear_categories()
    food = _category(owner, "Food", "expense")
    salary = _category(owner, "Salary", "income")
    _add(owner, food, "150")
    _add(owner, salary, "100", "income")
    plan = _fragment(owner, "month-plan")
    assert 'id="home-plan-overspend-hatch"' in plan
    assert "url(#home-plan-overspend-hatch)" in plan


def test_customise_saves_the_enabled_widgets_in_order(owner: OwnerApp) -> None:
    _fixed(owner)
    page = owner.client.get("/dashboard/customise").text
    order, order_csv = _customise_order(page)
    assert order == list(WIDGET_KEYS)
    chosen = ["loans", "fire"]
    response = _post_many(
        owner,
        "/dashboard/customise",
        [("order", order_csv), *(("enabled", key) for key in chosen)],
        token_from="/dashboard/customise",
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/?saved=1"
    assert _widget_order(owner.client.get("/").text) == chosen
    with owner.db() as session:
        row = DashboardPreferenceRepository(session, _ctx(owner)).for_user(owner.admin_id)
        assert row is not None and row.widgets == chosen

    reset = owner.post("/dashboard/customise/reset", token_from="/dashboard/customise")
    assert reset.status_code == 303 and reset.headers["location"] == "/?reset=1"
    assert _widget_order(owner.client.get("/").text) == list(WIDGET_KEYS)


def test_move_previews_the_draft_without_saving(owner: OwnerApp) -> None:
    _fixed(owner)
    page = owner.client.get("/dashboard/customise").text
    _order, order_csv = _customise_order(page)
    response = _post_many(
        owner,
        "/dashboard/customise/move",
        [
            ("order", order_csv),
            ("move", "loans:up"),
            ("enabled", "month-in-out"),
            ("enabled", "loans"),
        ],
        token_from="/dashboard/customise",
    )
    assert response.status_code == 200
    moved, _new_order = _customise_order(response.text)
    assert moved.index("loans") < moved.index("fire")
    with owner.db() as session:
        assert DashboardPreferenceRepository(session, _ctx(owner)).for_user(owner.admin_id) is None


def test_unknown_stored_keys_are_ignored_and_new_widgets_stay_off(owner: OwnerApp) -> None:
    _fixed(owner)
    with owner.db() as session, session.begin():
        DashboardPreferenceRepository(session, _ctx(owner)).upsert(
            user_id=owner.admin_id,
            widgets=["unknown", "month-in-out"],
            customised_at=owner.app.state.clock.now(),
        )
    home = owner.client.get("/").text
    assert _widget_order(home) == ["month-in-out"]
    page = owner.client.get("/dashboard/customise").text
    assert "unknown" not in page
    assert 'value="month-in-out" class="mt-1 h-4 w-4 shrink-0 accent-primary" checked' in page
    assert page.count('name="enabled"') == len(WIDGET_KEYS)
    assert page.count(" checked") == 1


def test_an_empty_saved_list_renders_the_empty_home(owner: OwnerApp) -> None:
    _fixed(owner)
    with owner.db() as session, session.begin():
        DashboardPreferenceRepository(session, _ctx(owner)).upsert(
            user_id=owner.admin_id, widgets=[], customised_at=owner.app.state.clock.now()
        )
    home = owner.client.get("/").text
    assert "data-home-empty" in home and "data-widget=" not in home


def test_a_viewer_can_save_and_missing_data_hides_imports(owner: OwnerApp) -> None:
    _fixed(owner)
    _planned_import(owner)
    assert "data-missing-imports" in _fragment(owner, "missing-data")
    owner.set_admin_role("viewer")
    viewer_fragment = _fragment(owner, "missing-data")
    assert "data-missing-imports" not in viewer_fragment
    assert "history.csv" not in viewer_fragment

    page = owner.client.get("/dashboard/customise").text
    _order, order_csv = _customise_order(page)
    response = _post_many(
        owner,
        "/dashboard/customise",
        [("order", order_csv), ("enabled", "upcoming")],
        token_from="/dashboard/customise",
    )
    assert response.status_code == 303


def test_customise_posts_require_csrf(owner: OwnerApp) -> None:
    _fixed(owner)
    page = owner.client.get("/dashboard/customise").text
    _order, order_csv = _customise_order(page)
    assert owner.client.post("/dashboard/customise", data={"order": order_csv}).status_code == 403
    assert (
        owner.client.post("/dashboard/customise/reset", data={"csrf_token": "bad"}).status_code
        == 403
    )


def test_a_demo_workspace_renders_every_widget_with_data(owner: OwnerApp) -> None:
    _fixed(owner, "2026-11-20T12:00:00+00:00")
    ctx = _ctx(owner)
    plan = plan_for_workspace(ctx, date(2026, 11, 20))
    build_demo_workspace(
        owner.app.state.session_factory,
        ctx,
        plan,
        data_dir=owner.app.state.settings.data_dir,
        horizon_months=owner.app.state.settings.recurrence_horizon_months,
    )
    create_fire_plan(
        owner.app.state.session_factory,
        ctx,
        FirePlanInput(
            birth_year=1988,
            income=Money(6_000_000, "EUR"),
            expenses=Money(4_000_000, "EUR"),
            portfolio=Money(10_000_000, "EUR"),
        ),
    )
    _planned_import(owner)
    owner.app.state.clock = FixedClock("2026-11-20T12:00:00+00:00")
    owner.app.state.session_factory.configure(info={"clock": owner.app.state.clock})

    assert 'data-kpi="earned"' in _fragment(owner, "month-in-out")
    assert "data-chart-json" in _fragment(owner, "month-expenses")
    assert "data-chart-json" in _fragment(owner, "month-income")
    assert "data-forecast-line" in _fragment(owner, "month-plan")
    assert "data-occurrence=" in _fragment(owner, "upcoming")
    net_worth = _fragment(owner, "net-worth")
    assert "data-chart-json" in net_worth
    assert len(_chart_data(net_worth)["labels"]) >= 12
    portfolio = _fragment(owner, "portfolio")
    assert "data-portfolio-value" in portfolio and "data-portfolio-gain" in portfolio
    assert "data-donut" in portfolio
    assert "data-change-day" in _fragment(owner, "portfolio-changes")
    loans = _fragment(owner, "loans")
    assert "data-loan-row" in loans and "Payoff" in loans and "repaid" in loans
    fire = _fragment(owner, "fire")
    assert "data-fire-progress" in fire and "data-fire-age" in fire
    missing = _fragment(owner, "missing-data")
    assert "data-missing-imports" in missing and "data-missing-prices" in missing
