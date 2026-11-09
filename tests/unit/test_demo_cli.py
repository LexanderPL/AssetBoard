"""The ``demo-seed`` CLI (F9.11a): empty-only, deterministic date and counts-only output."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from assetboard.branding import ENV_PREFIX
from assetboard.cli import app
from assetboard.db.models.identity import User, Workspace
from assetboard.db.models.ledger import MonthState

PASSWORD = "CLI Demo Strong Password"


def _workspace_id(data_dir: Path) -> tuple[Workspace, User]:
    engine = create_engine(f"sqlite:///{data_dir / 'assetboard.db'}")
    try:
        with Session(engine) as session:
            workspace = session.scalars(select(Workspace)).one()
            user = session.scalars(select(User)).one()
            return workspace, user
    finally:
        engine.dispose()


def test_demo_seed_builds_an_empty_workspace_and_refuses_a_non_empty_one(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "demo-cli"
    env = {f"{ENV_PREFIX}_DATA_DIR": str(data_dir)}
    runner = CliRunner()
    created = runner.invoke(
        app,
        [
            "create-admin",
            "--email",
            "demo-owner@example.com",
            "--display-name",
            "Demo Owner",
            "--workspace-name",
            "Demo",
            "--password-stdin",
        ],
        input=f"{PASSWORD}\n",
        env=env,
    )
    assert created.exit_code == 0, created.output
    workspace, owner = _workspace_id(data_dir)
    assert owner.birth_year is None

    seeded = runner.invoke(
        app,
        ["demo-seed", "--workspace", str(workspace.id), "--date", "2026-11-20"],
        env=env,
    )
    assert seeded.exit_code == 0, seeded.output
    assert "Built demo workspace" in seeded.output
    assert "2026-11-20" in seeded.output
    assert "transactions=" in seeded.output
    assert "EUR" not in seeded.output  # counts only, never amounts or currency
    assert "€" not in seeded.output

    _, owner = _workspace_id(data_dir)
    assert owner.birth_year == 1988
    assert owner.life_expectancy == 88

    engine = create_engine(f"sqlite:///{data_dir / 'assetboard.db'}")
    try:
        with Session(engine) as session:
            months = {
                row.month: row.closed_at
                for row in session.scalars(
                    select(MonthState).where(MonthState.workspace_id == workspace.id)
                )
            }
    finally:
        engine.dispose()
    assert set(months) == {
        date(2026, 8, 1),
        date(2026, 9, 1),
        date(2026, 10, 1),
        date(2026, 11, 1),
    }
    assert months[date(2026, 8, 1)] is not None

    again = runner.invoke(
        app,
        ["demo-seed", "--workspace", str(workspace.id), "--date", "2026-11-20"],
        env=env,
    )
    assert again.exit_code == 1
    assert "not empty" in again.output
