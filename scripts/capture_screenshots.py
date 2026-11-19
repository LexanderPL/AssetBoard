"""Capture the user-guide screenshots from the deterministic demo dataset (F9.7).

By default the script builds a throwaway instance in a temporary directory, creates the
administrator, seeds the F9.11a demo workspace for ``--reference`` and serves it on a random
loopback port. Use ``--base-url`` to capture a running instance instead; seed that instance with
``assetboard demo-seed`` first (or turn on demo mode and pass ``--demo-login``).

Every page is captured in four variants: desktop light, desktop dark, phone light and phone
dark. Output is JPEG at quality 85 to keep the committed gallery small.

Examples:
    uv run python scripts/capture_screenshots.py
    uv run python scripts/capture_screenshots.py --reference 2026-11-20
    uv run python scripts/capture_screenshots.py --base-url http://127.0.0.1:8000 --demo-login
"""

from __future__ import annotations

import argparse
import socket
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

import uvicorn
from playwright.sync_api import Browser, Page, ViewportSize, sync_playwright
from sqlalchemy import select

from assetboard.app import create_app
from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.db.migrate import upgrade_database
from assetboard.db.models.identity import User, Workspace
from assetboard.services.auth import run_initial_setup
from assetboard.services.context import Context, Role, UserRef, WorkspaceRef
from assetboard.services.demo import build_demo_workspace, plan_for_workspace

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs" / "assets" / "screenshots"
DEFAULT_REFERENCE = date(2026, 11, 20)
DEFAULT_EMAIL = "screenshots@example.com"
DEFAULT_PASSWORD = "Correct Horse Battery!"

VIEWPORTS: dict[str, ViewportSize] = {
    "desktop": {"width": 1440, "height": 900},
    "phone": {"width": 390, "height": 844},
}
THEMES = ("light", "dark")

PAGES: tuple[tuple[str, str, str], ...] = (
    ("home", "/", "Home"),
    ("month", "/m/{month}", "Month"),
    ("transactions", "/transactions/{month}", "Transactions"),
    ("history", "/history", "History"),
    ("reports", "/reports", "Reports"),
    ("net-worth", "/net-worth", "Net worth"),
    ("investments", "/investments", "Investments"),
    ("loans", "/loans", "Loans"),
    ("fire", "/fire", "FIRE"),
    ("account", "/account", "Account"),
)


def _free_port() -> int:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    port = int(listener.getsockname()[1])
    listener.close()
    return port


def _seed_instance(data_dir: Path, reference: date) -> tuple[Any, int]:
    """Create the admin and fill the workspace with the demo plan before serving."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    settings = Settings(
        _env_file=None,
        data_dir=data_dir,
        database_url=f"sqlite:///{data_dir / 'screenshots.db'}",
        secret_key="screenshot-instance-key",
        base_url=base_url,
        scheduler_enabled=False,
        # Ambient ASSETBOARD_* variables must not change the committed gallery.
        default_locale="en",
        default_currency="EUR",
        demo_enabled=False,
    )
    upgrade_database(settings)
    app = create_app(settings)
    clock = FixedClock(f"{reference.isoformat()}T12:00:00+00:00")
    app.state.clock = clock
    app.state.session_factory.configure(info={"clock": clock})
    run_initial_setup(
        app.state.session_factory,
        email=DEFAULT_EMAIL,
        display_name="Screenshot Admin",
        password=DEFAULT_PASSWORD,
        workspace_name="Home",
        settings=app.state.settings,
        clock=clock,
    )
    with app.state.session_factory.begin() as session:
        user = session.scalars(select(User)).one()
        workspace = session.scalars(select(Workspace)).one()
        ctx = Context(
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
                taxes_enabled=workspace.taxes_enabled,
            ),
            role=Role.OWNER,
            clock=clock,
        )
        plan = plan_for_workspace(ctx, reference)
    build_demo_workspace(
        app.state.session_factory,
        ctx,
        plan,
        data_dir=app.state.settings.data_dir,
        horizon_months=app.state.settings.recurrence_horizon_months,
    )
    return app, port


def _start_server(app: Any, port: int) -> tuple[uvicorn.Server, threading.Thread]:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen()
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        threading.Event().wait(0.01)
    if not server.started:
        raise SystemExit("The screenshot server did not start.")
    return server, thread


def _sign_in(page: Page, base_url: str, *, demo_login: bool, email: str, password: str) -> None:
    page.goto(f"{base_url}/login", wait_until="networkidle")
    if demo_login:
        page.get_by_role("button", name="Try the demo").click()
    else:
        page.get_by_label("Email").fill(email)
        page.get_by_label("Password").fill(password)
        page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(f"{base_url}/", wait_until="networkidle")


def _capture(page: Page, base_url: str, output: Path, *, month: str) -> None:
    for slug, template, _title in PAGES:
        path = template.format(month=month)
        response = page.goto(f"{base_url}{path}", wait_until="networkidle")
        if response is None or not response.ok:
            status = "no response" if response is None else f"HTTP {response.status}"
            raise SystemExit(f"{path} did not render for {output.name}: {status}")
        page.locator("main").wait_for(state="visible")
        page.wait_for_timeout(500)  # let ECharts finish its first frame
        page.screenshot(
            path=output / f"{slug}.jpg",
            type="jpeg",
            quality=85,
            animations="disabled",
            caret="hide",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        help="Capture a running instance instead of starting a temporary one.",
    )
    parser.add_argument(
        "--demo-login",
        action="store_true",
        help="Use the Try the demo button on a demo-mode instance instead of a password.",
    )
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument(
        "--reference",
        type=date.fromisoformat,
        default=DEFAULT_REFERENCE,
        help="Demo reference date T (default: 2026-11-20).",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--browser", default="chromium")
    parser.add_argument(
        "--keep-data",
        type=Path,
        help="Keep the temporary instance's data in this directory for inspection.",
    )
    args = parser.parse_args(argv)

    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    month = args.reference.strftime("%Y-%m")

    server: uvicorn.Server | None = None
    temp: Any = None
    if args.base_url:
        base_url = args.base_url.rstrip("/")
    else:
        import tempfile

        temp = tempfile.TemporaryDirectory(prefix="assetboard-screenshots-")
        data_dir = args.keep_data or Path(temp.name)
        if args.keep_data:
            temp.cleanup()
            temp = None
        data_dir.mkdir(parents=True, exist_ok=True)
        app, port = _seed_instance(data_dir, args.reference)
        server, _thread = _start_server(app, port)
        base_url = f"http://127.0.0.1:{port}"

    return _run(
        base_url=base_url,
        server=server,
        temp=temp,
        args=args,
        output=output,
        month=month,
    )


def _run(
    *,
    base_url: str,
    server: uvicorn.Server | None,
    temp: Any,
    args: argparse.Namespace,
    output: Path,
    month: str,
) -> int:
    try:
        with sync_playwright() as playwright:
            browser: Browser = getattr(playwright, args.browser).launch()
            for theme in THEMES:
                for viewport_name, viewport in VIEWPORTS.items():
                    context = browser.new_context(
                        viewport=viewport,
                        color_scheme=theme,
                        device_scale_factor=1,
                        locale="en-US",
                        timezone_id="UTC",
                    )
                    page = context.new_page()
                    _sign_in(
                        page,
                        base_url,
                        demo_login=args.demo_login,
                        email=args.email,
                        password=args.password,
                    )
                    target = output / viewport_name / theme
                    target.mkdir(parents=True, exist_ok=True)
                    _capture(page, base_url, target, month=month)
                    context.close()
            browser.close()
    finally:
        if server is not None:
            server.should_exit = True
        if temp is not None:
            temp.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
