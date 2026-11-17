"""The profile's sidebar customiser and the personalised sidebar (F9.12)."""

from __future__ import annotations

import re
from datetime import date

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

from assetboard.clock import FixedClock
from assetboard.db.models.identity import Membership, User, Workspace
from assetboard.db.models.ledger import Category
from assetboard.db.models.scheduled import ScheduledOccurrence
from assetboard.domain.navigation import CANONICAL_KEYS, DEFAULT_SIDEBAR, SidebarItem
from assetboard.services.context import Role, WorkspaceNav, WorkspaceRef
from assetboard.web.rendering import (
    SIDEBAR_GLYPHS,
    SIDEBAR_LABELS,
    sidebar_labels,
    sidebar_links,
)
from tests.conftest import OwnerApp, WebHarness

DEFAULT_ORDER = list(CANONICAL_KEYS)
HOOKS = {
    "home": "home",
    "month": "month",
    "transactions": "transactions",
    "history": "history",
    "upcoming": "upcoming",
    "recurring": "recurring",
    "income": "income",
    "taxes": "taxes",
    "net_worth": "net-worth",
    "investments": "investments",
    "loans": "loans",
    "fire": "fire",
}


def nav_hooks(html: str) -> list[str]:
    match = re.search(r'<nav [^>]*aria-label="Main navigation".*?</nav>', html, re.DOTALL)
    assert match is not None
    return re.findall(r"data-([a-z-]+)-link", match.group(0))


def panel(html: str) -> str:
    match = re.search(r"data-sidebar-settings.*?</section>", html, re.DOTALL)
    assert match is not None
    return match.group(0)


def panel_keys(html: str) -> list[str]:
    return re.findall(r'data-sidebar-item="([^"]+)"', panel(html))


def row(markup: str, key: str) -> str:
    match = re.search(rf'<li [^>]*data-sidebar-item="{key}".*?</li>', markup, re.DOTALL)
    assert match is not None
    return match.group(0)


def post_sidebar(
    app: OwnerApp,
    *,
    order: list[str] | None = None,
    visible: list[str] | None = None,
    move: str = "",
    path: str = "/account/sidebar",
    token_from: str = "/account",
    csrf: bool = True,
):
    order = DEFAULT_ORDER if order is None else order
    visible = DEFAULT_ORDER if visible is None else visible
    form: dict[str, object] = {"item": list(order), "visible": list(visible)}
    if move:
        form["move"] = move
    if csrf:
        form["csrf_token"] = app.token(token_from)
    return app.client.post(path, data=form, follow_redirects=False)


def saved(app: OwnerApp) -> list[dict[str, object]] | None:
    with app.db() as session:
        user = session.scalars(select(User).where(User.id == app.admin_id)).one()
        return user.sidebar_items


def test_the_profile_lists_every_destination_with_home_always_shown(owner: OwnerApp) -> None:
    page = owner.client.get("/account").text
    settings = panel(page)
    assert 'id="sidebar-settings"' in page
    assert panel_keys(page) == DEFAULT_ORDER
    home = row(settings, "home")
    assert "checked" in home and "disabled" in home
    assert "data-sidebar-move" not in home
    assert "Always shown" in home
    # First/last non-Home move buttons are disabled; every row carries its hidden key.
    assert re.search(r'data-sidebar-move="month:up"[^>]*disabled', row(settings, "month"))
    assert re.search(r'data-sidebar-move="fire:down"[^>]*disabled', row(settings, "fire"))
    for key in DEFAULT_ORDER:
        assert f'name="item" value="{key}"' in row(settings, key)
    assert "data-sidebar-save" in settings and "data-sidebar-reset" in settings
    assert "data-sidebar-taxes-note" in row(settings, "taxes")


def test_the_sidebar_save_button_is_distinct_from_the_profile_save_button(owner: OwnerApp) -> None:
    page = owner.client.get("/account").text
    # Both panels are on the same page: the profile form's Save and the sidebar's own button
    # must not share an accessible name.
    assert '<button type="submit" class="btn-primary">Save</button>' in page
    assert "data-sidebar-save>Save sidebar</button>" in page
    assert "data-sidebar-save>Save<" not in page


def test_save_custom_order_and_visibility(owner: OwnerApp) -> None:
    order = ["home", "investments", "month", "transactions", "history", "upcoming", "recurring",
             "income", "taxes", "net_worth", "loans", "fire"]  # fmt: skip
    visible = [key for key in order if key not in {"history", "taxes"}]
    response = post_sidebar(owner, order=order, visible=visible)
    assert response.status_code == 303
    assert response.headers["location"] == "/account?sidebar=saved#sidebar-settings"
    assert saved(owner) == [
        {"key": "home", "visible": True},
        {"key": "investments", "visible": True},
        {"key": "month", "visible": True},
        {"key": "transactions", "visible": True},
        {"key": "history", "visible": False},
        {"key": "upcoming", "visible": True},
        {"key": "recurring", "visible": True},
        {"key": "income", "visible": True},
        {"key": "taxes", "visible": False},
        {"key": "net_worth", "visible": True},
        {"key": "loans", "visible": True},
        {"key": "fire", "visible": True},
    ]
    page = owner.client.get("/account?sidebar=saved#sidebar-settings").text
    settings = panel(page)
    notice = re.search(r'<p [^>]*role="status"[^>]*data-sidebar-notice[^>]*>.*?</p>', settings,
                       re.DOTALL)  # fmt: skip
    assert notice is not None and "Your sidebar is saved." in notice.group(0)
    # The status is inside the Sidebar panel, not the OIDC notice at the top.
    assert "data-profile-notice" not in page
    assert nav_hooks(owner.client.get("/").text) == [
        "home",
        "investments",
        "month",
        "transactions",
        "upcoming",
        "recurring",
        "income",
        "net-worth",
        "loans",
        "fire",
    ]
    # A hidden destination stays reachable by its address.
    assert owner.client.get("/history").status_code == 200
    # Hidden rows keep their position in the customiser, muted.
    hidden = row(settings, "history")
    assert "checked" not in hidden and "opacity-60" in hidden


def test_move_up_and_down_by_posting(owner: OwnerApp) -> None:
    moved = post_sidebar(owner, move="income:up")
    assert moved.status_code == 303, moved.text
    custom = ["home", "month", "transactions", "history", "upcoming", "income", "recurring",
              "taxes", "net_worth", "investments", "loans", "fire"]  # fmt: skip
    assert [entry["key"] for entry in saved(owner) or []] == custom
    back = post_sidebar(owner, order=custom, move="income:down")
    assert back.status_code == 303
    assert saved(owner) is None  # the canonical list is stored as NULL
    # A malformed move is ignored rather than failing the save.
    ignored = post_sidebar(owner, move="income:sideways")
    assert ignored.status_code == 303
    assert saved(owner) is None


def test_reset_restores_the_canonical_order(owner: OwnerApp) -> None:
    assert post_sidebar(owner, order=["home", "loans", *DEFAULT_ORDER[1:-1]]).status_code == 303
    assert saved(owner) is not None
    response = post_sidebar(owner, path="/account/sidebar/reset")
    assert response.status_code == 303
    assert response.headers["location"] == "/account?sidebar=reset#sidebar-settings"
    assert saved(owner) is None
    assert nav_hooks(owner.client.get("/").text) == [
        HOOKS[key] for key in CANONICAL_KEYS if key != "taxes"
    ]
    page = owner.client.get("/account?sidebar=reset").text
    assert "Your sidebar is back to the default order." in panel(page)


def test_the_sidebar_posts_need_the_csrf_token(owner: OwnerApp) -> None:
    assert post_sidebar(owner, csrf=False).status_code == 403
    assert saved(owner) is None
    assert post_sidebar(owner, path="/account/sidebar/reset", csrf=False).status_code == 403
    assert saved(owner) is None


def test_reordering_keeps_the_current_marker_and_overdue_badge(owner: OwnerApp) -> None:
    clock = FixedClock("2026-03-10T12:00:00+00:00")
    owner.app.state.clock = clock
    owner.app.state.session_factory.configure(info={"clock": clock})
    with owner.db() as session, session.begin():
        category_id = session.scalars(
            select(Category.id).where(Category.workspace_id == owner.home_id).limit(1)
        ).one()
        session.add(
            ScheduledOccurrence(
                workspace_id=owner.home_id,
                direction="expense",
                category_id=category_id,
                nominal_date=date(2026, 1, 1),
                due_date=date(2026, 1, 1),
                expected_minor=1000,
                currency="EUR",
                amount_state="estimated",
                status="pending",
                generated_by="income",
                label="Overdue test",
            )
        )
    order = ["home", "upcoming", "history", *[key for key in DEFAULT_ORDER if key not in
                                              {"home", "upcoming", "history"}]]  # fmt: skip
    assert post_sidebar(owner, order=order).status_code == 303
    page = owner.client.get("/upcoming").text
    assert nav_hooks(page)[:3] == ["home", "upcoming", "history"]
    assert 'data-upcoming-link aria-current="page"' in page
    assert 'data-overdue-badge="1"' in page
    history = owner.client.get("/history").text
    assert 'data-history-link aria-current="page"' in history


def test_taxes_is_listed_but_only_rendered_when_the_workspace_enables_it(owner: OwnerApp) -> None:
    order = ["home", "taxes", *[key for key in DEFAULT_ORDER if key not in {"home", "taxes"}]]
    assert post_sidebar(owner, order=order).status_code == 303
    page = owner.client.get("/").text
    assert "data-taxes-link" not in page
    assert "taxes" in panel_keys(owner.client.get("/account").text)
    with owner.db() as session, session.begin():
        session.execute(update(Workspace).where(Workspace.id == owner.home_id)
                        .values(taxes_enabled=True))  # fmt: skip
    page = owner.client.get("/").text
    assert nav_hooks(page)[:3] == ["home", "taxes", "month"]
    assert "data-taxes-link" in page


def test_the_customiser_works_without_a_workspace(owner: OwnerApp) -> None:
    with owner.db() as session, session.begin():
        session.execute(delete(Membership))
    page = owner.client.get("/account").text
    assert panel_keys(page) == DEFAULT_ORDER
    assert "data-sidebar-form" in panel(page)
    response = post_sidebar(owner, order=["home", "loans", *DEFAULT_ORDER[1:-1]])
    assert response.status_code == 303
    assert saved(owner) is not None
    home = owner.client.get("/account").text
    assert nav_hooks(home) == ["home"]


def test_the_customiser_works_for_viewers(owner: OwnerApp) -> None:
    owner.set_admin_role("viewer")
    page = owner.client.get("/account").text
    assert "data-sidebar-form" in panel(page)
    assert post_sidebar(owner, order=["home", "income", *DEFAULT_ORDER[1:-2]]).status_code == 303
    assert saved(owner) is not None


def test_the_saved_sidebar_follows_the_user_across_workspaces(owner: OwnerApp) -> None:
    order = ["home", "loans", *[key for key in DEFAULT_ORDER if key not in {"home", "loans"}]]
    visible = [key for key in order if key != "history"]
    assert post_sidebar(owner, order=order, visible=visible).status_code == 303
    second = owner.add_workspace("Second")
    owner.join(owner.admin_id, second, "owner")
    switched = owner.post("/workspace/switch", {"workspace_id": str(second)})
    assert switched.status_code == 303
    # The preference belongs to the user, not to the workspace they are in.
    assert nav_hooks(owner.client.get("/").text) == [
        "home", "loans", "month", "transactions", "upcoming", "recurring",
        "income", "net-worth", "investments", "fire",
    ]  # fmt: skip
    assert panel_keys(owner.client.get("/account").text) == order
    assert saved(owner) == [{"key": key, "visible": key != "history"} for key in order]


def _request_for(owner: OwnerApp) -> Request:
    return Request(
        {
            "type": "http",
            "app": owner.app,
            "method": "GET",
            "path": "/",
            "root_path": "",
            "scheme": "http",
            "server": ("testserver", 80),
            "query_string": b"",
            "headers": [],
        }
    )


def _nav(owner: OwnerApp, *, sidebar: tuple[SidebarItem, ...] = DEFAULT_SIDEBAR) -> WorkspaceNav:
    return WorkspaceNav(
        current=WorkspaceRef(
            id=owner.home_id,
            name="Home",
            base_currency="EUR",
            locale="en",
            timezone="UTC",
            country_code=None,
            taxes_enabled=True,
        ),
        role=Role.OWNER,
        choices=(),
        sidebar=sidebar,
    )


def test_every_canonical_key_has_a_label_glyph_and_link_rule(owner: OwnerApp) -> None:
    assert set(SIDEBAR_LABELS) == set(CANONICAL_KEYS)
    assert set(SIDEBAR_GLYPHS) == set(CANONICAL_KEYS)
    assert set(sidebar_labels("en")) == set(CANONICAL_KEYS)
    # With Taxes enabled every canonical key must reach the sidebar builder, so a destination
    # added to CANONICAL_KEYS without an href/current rule fails here instead of disappearing.
    assert [link.key for link in sidebar_links(_request_for(owner), _nav(owner))] == list(
        CANONICAL_KEYS
    )


def test_a_canonical_key_without_a_link_rule_fails_loudly(owner: OwnerApp) -> None:
    nav = _nav(owner, sidebar=(SidebarItem("future_destination", True),))
    with pytest.raises(KeyError, match="future_destination"):
        sidebar_links(_request_for(owner), nav)


def test_polish_labels(owner: OwnerApp) -> None:
    page = owner.client.get("/account", headers={"Accept-Language": "pl"}).text
    settings = panel(page)
    assert "Pasek boczny" in settings
    assert "Zawsze widoczna" in settings
    assert "Miesiąc" in settings
    assert "Zapisz pasek boczny" in settings


def test_unknown_and_missing_stored_keys_normalise(owner: OwnerApp) -> None:
    with owner.db() as session, session.begin():
        session.execute(
            update(User)
            .where(User.id == owner.admin_id)
            .values(
                sidebar_items=[
                    {"key": "bogus", "visible": True},
                    {"key": "loans", "visible": False},
                    "junk",
                ]
            )
        )
    page = owner.client.get("/account").text
    assert panel_keys(page) == ["home", "loans", *[k for k in CANONICAL_KEYS
                                                  if k not in {"home", "loans"}]]  # fmt: skip
    loans = row(panel(page), "loans")
    assert "checked" not in loans and "opacity-60" in loans
    assert nav_hooks(owner.client.get("/").text) == [
        "home",
        "month",
        "transactions",
        "history",
        "upcoming",
        "recurring",
        "income",
        "net-worth",
        "investments",
        "fire",
    ]


def test_unknown_sidebar_status_query_is_ignored(owner: OwnerApp) -> None:
    page = owner.client.get("/account?sidebar=evil").text
    assert "data-sidebar-notice" not in page


def _demo_app(web: WebHarness):
    app = web.app(
        demo_enabled=True,
        demo_timezone="UTC",
        demo_reset_time="03:00",
        demo_ip_limit=20,
    )
    app.state.clock = FixedClock("2026-03-01T12:00:00+00:00")
    return app


def test_demo_sessions_see_the_read_only_panel_and_posts_are_refused(web: WebHarness) -> None:
    app = _demo_app(web)
    with TestClient(app) as client:
        web.complete_setup(app)
        token = web.csrf(client.get("/login").text)
        assert (
            client.post(
                "/login/demo", data={"csrf_token": token}, follow_redirects=False
            ).status_code
            == 303
        )
        page = client.get("/account")
        assert page.status_code == 200
        assert "data-sidebar-settings" in page.text
        assert "data-sidebar-form" not in page.text
        assert "data-sidebar-save" not in page.text
        assert "data-sidebar-reset" not in page.text
        assert "Not available in the demo workspace." in page.text
        token = web.csrf(page.text)
        refused = client.post(
            "/account/sidebar",
            data={"csrf_token": token, "item": "home"},
            follow_redirects=False,
        )
        assert refused.status_code == 403
        assert "not available in the demo workspace" in refused.text.lower()
        with app.state.session_factory() as session:
            demo = session.scalars(select(User).where(User.is_demo.is_(True))).one()
            assert demo.sidebar_items is None
