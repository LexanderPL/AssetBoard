"""Web tests for F9.11b: demo startup, login button/route, banner and admin panel."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from assetboard import app as app_module
from assetboard.clock import FixedClock
from assetboard.db.models.identity import (
    InstanceSettings,
    User,
    UserSession,
    Workspace,
)
from assetboard.db.models.import_batch import ImportBatch
from assetboard.db.models.ledger import Category
from assetboard.domain.demo import DEMO_DATASET_VERSION
from assetboard.services.demo_mode import DEMO_EMAIL
from tests.conftest import WebHarness

CLOCK = FixedClock("2026-03-01T12:00:00+00:00")


def _demo_app(web: WebHarness, **overrides: object):
    values: dict[str, object] = {
        "demo_enabled": True,
        "demo_timezone": "UTC",
        "demo_reset_time": "03:00",
        "demo_ip_limit": 20,
    }
    values.update(overrides)
    app = web.app(**values)
    app.state.clock = CLOCK
    return app


def _demo_counts(factory) -> tuple[int, int, int]:
    with factory() as session:
        users = (
            session.scalar(select(func.count()).select_from(User).where(User.is_demo.is_(True)))
            or 0
        )
        workspaces = (
            session.scalar(
                select(func.count()).select_from(Workspace).where(Workspace.is_demo.is_(True))
            )
            or 0
        )
        batches = session.scalar(select(func.count()).select_from(ImportBatch)) or 0
        return int(users), int(workspaces), int(batches)


def test_startup_creates_the_demo_once_and_login_lands_on_home(web: WebHarness) -> None:
    app = _demo_app(web)
    with TestClient(app) as client:
        assert app.state.demo_ready is True
        web.complete_setup(app)
        page = client.get("/login")
        assert page.status_code == 200
        assert "data-demo-login" in page.text
        assert "Try the demo" in page.text
        assert "resets daily at 3:00 AM" in page.text
        token = web.csrf(page.text)
        response = client.post("/login/demo", data={"csrf_token": token}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        home = client.get("/")
        assert home.status_code == 200
        assert "data-demo-banner" in home.text
        assert "Demo workspace: shared with other visitors" in home.text
        assert "(UTC)" in home.text
        # Every signed-in page carries it, including the account pages.
        assert "data-demo-banner" in client.get("/account").text
        assert "data-demo-banner" in client.get("/transactions").text
        with app.state.session_factory() as session:
            user = session.scalars(select(User).where(User.is_demo.is_(True))).one()
            workspace = session.scalars(select(Workspace).where(Workspace.is_demo.is_(True))).one()
            row = session.scalars(select(UserSession).where(UserSession.user_id == user.id)).one()
            assert row.auth_method == "demo"
            assert row.current_workspace_id == workspace.id
            assert row.second_factor_verified is False
            assert user.email == DEMO_EMAIL

    # A second start finds the same pair and does not build a second dataset.
    with TestClient(_demo_app(web)) as client:
        assert client.get("/login").status_code == 200
        assert _demo_counts(app.state.session_factory) == (1, 1, 1)


def test_startup_catches_up_a_stale_or_versioned_demo_dataset(web: WebHarness) -> None:
    app = _demo_app(web)
    with TestClient(app), app.state.session_factory.begin() as session:
        workspace = session.scalars(select(Workspace).where(Workspace.is_demo.is_(True))).one()
        session.add(
            Category(
                workspace_id=workspace.id,
                name="Stale marker",
                name_key="stale marker",
                kind="expense",
                treatment="regular",
                color="#123456",
                sort_order=999,
            )
        )
        row = session.get(InstanceSettings, 1)
        assert row is not None
        row.demo_dataset_version = 0

    restarted = _demo_app(web)
    with TestClient(restarted):
        assert restarted.state.demo_ready is True
        with app.state.session_factory() as session:
            row = session.get(InstanceSettings, 1)
            assert row is not None
            assert row.demo_dataset_version == DEMO_DATASET_VERSION
            assert (
                session.scalars(select(Category).where(Category.name == "Stale marker")).first()
                is None
            )


def test_demo_login_is_csrf_protected_and_absent_when_demo_mode_is_off(
    web: WebHarness,
) -> None:
    app = web.app()
    app.state.clock = CLOCK
    with TestClient(app) as client:
        web.complete_setup(app)
        page = client.get("/login")
        assert "Try the demo" not in page.text
        token = web.csrf(page.text)
        response = client.post("/login/demo", data={"csrf_token": token}, follow_redirects=False)
        assert response.status_code == 404

    demo_app = _demo_app(web)
    with TestClient(demo_app) as client:
        token = web.csrf(client.get("/login").text)
        assert client.post("/login/demo", data={}).status_code == 403
        assert (
            client.post(
                "/login/demo", data={"csrf_token": token}, follow_redirects=False
            ).status_code
            == 303
        )


def test_the_per_address_limit_shows_the_usual_throttle_message(web: WebHarness) -> None:
    app = _demo_app(web, demo_ip_limit=1)
    with TestClient(app) as client:
        web.complete_setup(app)
        token = web.csrf(client.get("/login").text)
        assert (
            client.post(
                "/login/demo", data={"csrf_token": token}, follow_redirects=False
            ).status_code
            == 303
        )
        token = web.csrf(client.get("/").text)
        client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
        page = client.get("/login")
        token = web.csrf(page.text)
        response = client.post("/login/demo", data={"csrf_token": token}, follow_redirects=False)
        assert response.status_code == 401
        assert "Too many attempts" in response.text


def test_a_failed_bootstrap_does_not_block_startup(web: WebHarness, monkeypatch) -> None:
    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("database detail must not escape")

    monkeypatch.setattr(app_module, "ensure_demo_instance", broken)
    app = _demo_app(web)
    with TestClient(app) as client:
        assert app.state.demo_ready is False
        web.complete_setup(app)
        assert client.get("/healthz").status_code == 200
        assert client.get("/login").status_code == 200
        assert "Try the demo" not in client.get("/login").text
        token = web.csrf(client.get("/login").text)
        assert client.post("/login/demo", data={"csrf_token": token}).status_code == 404
        with app.state.session_factory() as session:
            row = session.get(InstanceSettings, 1)
            assert row is not None and row.demo_last_reset_error == "RuntimeError"


def test_admin_panel_resets_now_and_hides_the_demo_account(
    web: WebHarness,
) -> None:
    app = _demo_app(web)
    with TestClient(app) as client:
        web.complete_setup(app)
        assert web.sign_in(client).status_code == 303
        page = client.get("/admin/settings")
        assert "data-demo-panel" in page.text
        assert 'data-demo-on="true"' in page.text
        assert "data-demo-reset" in page.text
        assert "data-demo-next-reset" in page.text
        users = client.get("/admin/users")
        assert DEMO_EMAIL not in users.text
        token = web.csrf(page.text)
        assert client.post("/admin/settings/demo/reset", data={}).status_code == 403
        response = client.post(
            "/admin/settings/demo/reset",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert response.status_code == 200
        assert "The demo workspace was rebuilt." in response.text
        with app.state.session_factory() as session:
            assert session.scalars(select(User).where(User.is_demo.is_(True))).one()


def test_turning_demo_off_revokes_sessions_and_offers_delete(
    web: WebHarness,
) -> None:
    app = _demo_app(web)
    with TestClient(app) as client:
        web.complete_setup(app)
        assert web.sign_in(client).status_code == 303
        token = web.csrf(client.get("/").text)
        client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
        token = web.csrf(client.get("/login").text)
        assert (
            client.post(
                "/login/demo", data={"csrf_token": token}, follow_redirects=False
            ).status_code
            == 303
        )

    with TestClient(web.app(demo_enabled=False)) as client:
        with app.state.session_factory() as session:
            demo = session.scalars(select(User).where(User.is_demo.is_(True))).one()
            assert demo.is_active is False
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(UserSession)
                    .where(UserSession.user_id == demo.id)
                )
                or 0
            ) == 0
        assert web.sign_in(client).status_code == 303
        page = client.get("/admin/settings")
        assert "data-demo-panel" in page.text
        assert 'data-demo-on="false"' in page.text
        assert "data-demo-delete" in page.text
        assert "Delete demo data" in page.text
        token = web.csrf(page.text)
        response = client.post(
            "/admin/settings/demo/delete",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert response.status_code == 200
        assert "The demo workspace and its account were deleted." in response.text
        with app.state.session_factory() as session:
            assert session.scalars(select(User).where(User.is_demo.is_(True))).first() is None
            assert (
                session.scalars(select(Workspace).where(Workspace.is_demo.is_(True))).first()
                is None
            )
