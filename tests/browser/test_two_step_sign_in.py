"""Two-step sign-in with TOTP in Chromium (F1.6): the password page has no code field, a
correct password leads to the code page, a wrong code keeps the user there, and the right
code signs in. No CSP violations."""

from __future__ import annotations

import os
from pathlib import Path

import pyotp
import pytest
from playwright.sync_api import expect, sync_playwright

from assetboard.app import create_app
from assetboard.config import Settings
from assetboard.db.models.totp import TotpFactor
from assetboard.services import totp
from assetboard.services.auth import run_initial_setup
from tests.conftest import live_server

pytestmark = pytest.mark.skipif(
    os.environ.get("ASSETBOARD_RUN_BROWSER_TESTS") != "1",
    reason="Set ASSETBOARD_RUN_BROWSER_TESTS=1 to run the Chromium tests",
)

PASSWORD = "Correct Horse Battery!"
VIOLATION_RECORDER = """
window.__cspViolations = [];
document.addEventListener("securitypolicyviolation", (event) => {
  window.__cspViolations.push(event.violatedDirective + " " + (event.blockedURI || "inline"));
});
"""


def test_two_step_sign_in_with_an_authenticator_code(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'browser.db'}",
            secret_key="browser-test-key",
        )
    )
    with live_server(app) as base:
        state = app.state
        user, _workspace = run_initial_setup(
            state.session_factory,
            email="admin@example.com",
            display_name="Browser Admin",
            password=PASSWORD,
            workspace_name="Browser Home",
            settings=state.settings,
            clock=state.clock,
        )
        secret = pyotp.random_base32()
        with state.session_factory.begin() as session:
            session.add(
                TotpFactor(
                    user_id=user.id,
                    encrypted_secret=totp.encrypt_secret(
                        state.settings, user.id, secret, state.clock.now()
                    ),
                    enabled_at=state.clock.now(),
                    last_step=-1,
                )
            )
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context()
            context.add_init_script(VIOLATION_RECORDER)
            page = context.new_page()
            page.goto(f"{base}/login", wait_until="networkidle")
            expect(page.locator('input[name="second_factor"]')).to_have_count(0)
            page.get_by_label("Email").fill("admin@example.com")
            page.get_by_label("Password").fill(PASSWORD)
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_url(f"{base}/login/verify")
            expect(page.get_by_role("heading", level=1)).to_have_text(
                "Enter the code from your authenticator app"
            )
            code_field = page.get_by_label("Authenticator or recovery code")
            expect(code_field).to_have_attribute("autocomplete", "one-time-code")
            expect(page.locator("[data-start-over]")).to_have_attribute("href", "/login")
            assert not any(c.get("name") == "assetboard_session" for c in context.cookies())

            code_field.fill("000000")
            page.get_by_role("button", name="Continue").click()
            expect(page.get_by_role("alert")).to_be_visible()
            expect(page.get_by_label("Authenticator or recovery code")).to_be_visible()

            page.get_by_label("Authenticator or recovery code").fill(pyotp.TOTP(secret).now())
            page.get_by_role("button", name="Continue").click()
            page.wait_for_url(f"{base}/")
            names = {c.get("name") for c in context.cookies()}
            assert "assetboard_session" in names and "assetboard_login_ticket" not in names
            assert page.evaluate("window.__cspViolations") == []
            browser.close()
