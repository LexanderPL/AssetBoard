"""Shared helpers for the FIRE web tests (F8.9): create a plan through the setup wizard."""

from __future__ import annotations

import html
import re
from typing import Any
from uuid import UUID

from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.web.fire_routes import STEP_FIELDS, WIZARD_FIELDS

_HIDDEN_INPUT = re.compile(r'<input type="hidden" name="([^"]+)" value="([^"]*)">')


def freeze_clock(owner: Any, value: str) -> None:
    """Move the app and its write sessions to a fixed instant.

    The signed-in session was created at the real clock, so it can fall outside the idle
    window when the test jumps years; sign in again under the fixed clock.
    """
    clock = FixedClock(value)
    owner.app.state.clock = clock
    owner.app.state.session_factory.configure(info={"clock": clock})
    owner.client.cookies.clear()
    assert owner.web.sign_in(owner.client).status_code == 303


def wizard_values(**overrides: str) -> dict[str, str]:
    values = {
        "birth_year": "1985",
        "stop_working_age": "55",
        "horizon_age": "90",
        "income_manual": "50000",
        "expenses_manual": "30000",
        "portfolio_manual": "1000000",
        "inflation": "2.5",
        "real_return": "5",
        "income_growth": "3",
        "rule": "returns_cover_expenses",
        "swr": "4",
        "extreme_rates_confirmed": "",
    }
    values.update(overrides)
    return values


def post_wizard(
    owner: Any,
    step: int,
    go: str,
    values: dict[str, str],
    **extra: str,
) -> Any:
    return owner.post(
        "/fire/setup",
        {**values, "step": str(step), "go": go, **extra},
        token_from="/fire",
    )


def review_signature(text: str) -> str:
    """The hidden digest of the review the page rendered; Create requires the same one."""
    marker = 'name="review_signature" value="'
    start = text.index(marker) + len(marker)
    return text[start : text.index('"', start)]


def field_html(markup: str, name: str) -> str:
    """The rendered block of one form field, located by its stable ``data-fire-field`` marker."""
    marker = f'data-fire-field="{name}"'
    index = markup.index(marker)
    start = markup.rindex("<", 0, index)
    end = markup.index("</div>", index) + len("</div>")
    return markup[start:end]


def suggestion_html(markup: str, kind: str) -> str:
    """The rendered block of one suggestion card, located by its ``data-fire-suggestion`` kind."""
    marker = f'data-fire-suggestion="{kind}"'
    index = markup.index(marker)
    start = markup.rindex("<", 0, index)
    end = markup.index("</section>", index) + len("</section>")
    return markup[start:end]


def _hidden_values(text: str) -> dict[str, str]:
    """The wizard fields carried by the page's hidden inputs, as the browser would post them."""
    return {
        name: html.unescape(value)
        for name, value in _HIDDEN_INPUT.findall(text)
        if name in WIZARD_FIELDS
    }


def _step_payload(values: dict[str, str], step: int, hidden: dict[str, str]) -> dict[str, str]:
    """Only the current step's fields plus the hidden fields the previous page rendered."""
    payload = {field: values[field] for field in STEP_FIELDS[step]}
    payload.update(hidden)
    return payload


def create_plan(owner: Any, **overrides: str) -> UUID:
    """Walk the four wizard steps through their real hidden fields and review, then create."""
    values = wizard_values(**overrides)
    hidden: dict[str, str] = {}
    response = None
    for step in (1, 2, 3):
        response = post_wizard(owner, step, "next", _step_payload(values, step, hidden))
        assert response.status_code == 200, response.text
        assert f'data-fire-wizard="{step + 1}"' in response.text, response.text
        hidden = _hidden_values(response.text)
    assert response is not None
    # Step 4 is editable after its first render. Re-render it from the values this test wants,
    # exactly as a user pressing Review changes would, so Create confirms the shown summary.
    response = post_wizard(owner, 4, "review", _step_payload(values, 4, hidden))
    assert response.status_code == 200, response.text
    hidden = _hidden_values(response.text)
    response = post_wizard(
        owner,
        4,
        "create",
        _step_payload(values, 4, hidden),
        review_signature=review_signature(response.text),
    )
    assert response.status_code == 303, response.text
    from assetboard.db.models.fire import FireScenario

    with owner.db() as session:
        return session.scalars(
            select(FireScenario.id).where(
                FireScenario.workspace_id == owner.home_id,
                FireScenario.is_live.is_(True),
            )
        ).one()
