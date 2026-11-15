"""Shared helpers for the FIRE web tests (F8.9): create a plan through the setup wizard."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select


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


def create_plan(owner: Any, **overrides: str) -> UUID:
    """Walk the four wizard steps and return the created plan's id."""
    values = wizard_values(**overrides)
    for step in (1, 2, 3):
        response = post_wizard(owner, step, "next", values)
        assert response.status_code == 200, response.text
        assert f'data-fire-wizard="{step + 1}"' in response.text, response.text
    response = post_wizard(owner, 4, "create", values)
    assert response.status_code == 303, response.text
    from assetboard.db.models.fire import FireScenario

    with owner.db() as session:
        return session.scalars(
            select(FireScenario.id).where(
                FireScenario.workspace_id == owner.home_id,
                FireScenario.is_live.is_(True),
            )
        ).one()
