"""Dashboard preferences and the missing-data scan (F9.1)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from assetboard.db.repositories import (
    AssetClassRepository,
    DashboardPreferenceRepository,
    ImportBatchRepository,
    InvestmentTransactionRepository,
)
from assetboard.services.categories import create_category
from assetboard.services.dashboard import (
    missing_data,
    read_dashboard_preferences,
    reset_dashboard_preferences,
    save_dashboard_preferences,
)
from assetboard.services.income import create_source
from assetboard.services.investment_transactions import create_transaction
from assetboard.services.investments import (
    create_instrument,
    seed_asset_classes,
)
from assetboard.services.prices import save_manual_price
from assetboard.services.workspaces import remove_member
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    yield from workspace_db(tmp_path)


@pytest.fixture
def team(db: WorkspaceDb) -> WorkspaceDb:
    db.add_member("olga", "owner")
    db.add_member("vic", "viewer")
    return db


def _owner(db: WorkspaceDb):
    return db.ctx("olga", "owner")


def _income_category(db: WorkspaceDb) -> UUID:
    return create_category(
        db.factory,
        _owner(db),
        name="Salary",
        kind="income",
        treatment="regular",
        color="#16a34a",
    ).id


def _portfolio_with_stale_price(db: WorkspaceDb) -> None:
    ctx = _owner(db)
    seed_asset_classes(db.factory, ctx, locale="en")
    with db.factory() as session:
        class_id = AssetClassRepository(session, ctx).ordered()[0].id
    instrument = create_instrument(
        db.factory,
        ctx,
        name="World ETF",
        symbol="WLD",
        type="etf",
        quote_currency="EUR",
        unit_label="share",
        allocations=[(class_id, 10_000)],
        locale="en",
    )
    create_transaction(
        db.factory,
        ctx,
        instrument_id=instrument.id,
        type_value="buy",
        date_value="2026-01-15",
        quantity_value="10",
        unit_price_value="100",
        fee_value="0",
        fx_value="",
        note_value="",
        locale="en",
    )
    save_manual_price(
        db.factory,
        ctx,
        instrument.id,
        price_id=None,
        date_value="2026-01-20",
        price_value="100",
        locale="en",
    )


def _portfolio_with_missing_price(db: WorkspaceDb) -> None:
    ctx = _owner(db)
    seed_asset_classes(db.factory, ctx, locale="en")
    with db.factory() as session:
        class_id = AssetClassRepository(session, ctx).ordered()[0].id
    instrument = create_instrument(
        db.factory,
        ctx,
        name="No price",
        symbol="NOP",
        type="etf",
        quote_currency="EUR",
        unit_label="share",
        allocations=[(class_id, 10_000)],
        locale="en",
    )
    with db.factory.begin() as session:
        InvestmentTransactionRepository(session, ctx).create(
            instrument_id=instrument.id,
            date=None,
            type="buy",
            quantity=Decimal("1"),
            unit_price=Decimal("50"),
            quote_currency="EUR",
            fee_minor=0,
            fx_to_base=None,
        )


def _planned_import(db: WorkspaceDb) -> UUID:
    with db.factory.begin() as session:
        batch = ImportBatchRepository(session, _owner(db)).create(
            source="transactions_csv",
            mode="merge",
            filename="history.csv",
            file_sha256="a" * 64,
            size_bytes=100,
            options={},
            status="planned",
            started_by_user_id=db.users["olga"],
        )
        return batch.id


def _variable_source(db: WorkspaceDb) -> UUID:
    return create_source(
        db.factory,
        _owner(db),
        name="Freelance",
        kind="variable",
        category_id=_income_category(db),
        fixed_minor=None,
        rate_minor=10_000,
        unit_label="hour",
        pay_day_value=5,
        pay_lag_months=0,
        expected_mode="manual",
        expected_units_value=None,
        average_months=3,
        active_from=date(2026, 1, 1),
        active_to=None,
    )


def test_no_row_means_the_default_catalogue(team: WorkspaceDb) -> None:
    preferences = read_dashboard_preferences(team.factory, _owner(team))
    assert preferences.customised is False
    assert preferences.widgets == (
        "month-in-out",
        "month-expenses",
        "month-income",
        "month-plan",
        "upcoming",
        "net-worth",
        "portfolio",
        "portfolio-changes",
        "portfolio-history",
        "loans",
        "fire",
        "missing-data",
    )
    assert preferences.upcoming_limit == 10
    assert preferences.portfolio_range == "1y"
    assert preferences.net_worth_range == "1y"


def test_a_later_widget_is_default_for_uncustomised_users_and_off_for_customised_ones(
    team: WorkspaceDb,
) -> None:
    ctx = _owner(team)
    known = ("month-in-out", "upcoming", "new-widget")
    defaults = ("month-in-out", "upcoming", "new-widget")
    assert (
        read_dashboard_preferences(team.factory, ctx, known=known, defaults=defaults).widgets
        == defaults
    )
    save_dashboard_preferences(team.factory, ctx, ["month-in-out"], known=known)
    assert read_dashboard_preferences(
        team.factory, ctx, known=known, defaults=defaults
    ).widgets == ("month-in-out",)


def test_save_read_and_reset_are_per_user_and_workspace(team: WorkspaceDb) -> None:
    olga = _owner(team)
    vic = team.ctx("vic", "viewer")
    save_dashboard_preferences(
        team.factory,
        olga,
        ["missing-data", "month-in-out", "unknown"],
        upcoming_limit=7,
        portfolio_range="6m",
        net_worth_range="5y",
    )
    saved = read_dashboard_preferences(team.factory, olga)
    assert saved.customised is True
    assert saved.widgets == ("missing-data", "month-in-out")
    assert saved.upcoming_limit == 7
    assert saved.portfolio_range == "6m"
    assert saved.net_worth_range == "5y"
    defaults = read_dashboard_preferences(team.factory, vic)
    assert defaults.customised is False
    assert defaults.upcoming_limit == 10 and defaults.portfolio_range == "1y"
    assert defaults.net_worth_range == "1y"

    second = team.add_workspace("Second")
    team.add_member("olga", "owner", workspace_id=second)
    second_ctx = team.ctx("olga", "owner", workspace_id=second)
    assert read_dashboard_preferences(team.factory, second_ctx).customised is False
    save_dashboard_preferences(team.factory, second_ctx, ["loans"], upcoming_limit=22)
    assert read_dashboard_preferences(team.factory, second_ctx).widgets == ("loans",)
    assert read_dashboard_preferences(team.factory, second_ctx).upcoming_limit == 22
    assert read_dashboard_preferences(team.factory, olga).widgets == (
        "missing-data",
        "month-in-out",
    )

    reset_dashboard_preferences(team.factory, olga)
    after_reset = read_dashboard_preferences(team.factory, olga)
    assert after_reset.customised is False
    assert after_reset.upcoming_limit == 10 and after_reset.portfolio_range == "1y"
    assert after_reset.net_worth_range == "1y"


def test_saving_clamps_the_upcoming_limit_and_rejects_an_unknown_range(
    team: WorkspaceDb,
) -> None:
    ctx = _owner(team)
    save_dashboard_preferences(
        team.factory,
        ctx,
        ["upcoming"],
        upcoming_limit=999,
        portfolio_range="2y",
        net_worth_range="2y",
    )
    stored = read_dashboard_preferences(team.factory, ctx)
    assert stored.upcoming_limit == 50
    assert stored.portfolio_range == "1y"
    assert stored.net_worth_range == "1y"

    save_dashboard_preferences(team.factory, ctx, ["upcoming"], upcoming_limit=0)
    assert read_dashboard_preferences(team.factory, ctx).upcoming_limit == 1


def test_unknown_and_corrupt_stored_widgets_never_break_home(team: WorkspaceDb) -> None:
    ctx = _owner(team)
    with team.factory.begin() as session:
        repository = DashboardPreferenceRepository(session, ctx)
        repository.upsert(
            user_id=team.users["olga"],
            widgets=["unknown", "month-in-out"],
            customised_at=team.clock.now(),
        )
    preferences = read_dashboard_preferences(team.factory, ctx)
    assert preferences.customised is True
    assert preferences.widgets == ("month-in-out",)

    with team.factory.begin() as session:
        repository = DashboardPreferenceRepository(session, ctx)
        row = repository.for_user(team.users["olga"])
        assert row is not None
        repository.update(row.id, widgets="not a list")
    corrupt = read_dashboard_preferences(team.factory, ctx)
    assert corrupt.customised is True
    assert corrupt.widgets[0] == "month-in-out"


def test_a_viewer_can_save_their_own_preferences(team: WorkspaceDb) -> None:
    vic = team.ctx("vic", "viewer")
    save_dashboard_preferences(team.factory, vic, ["upcoming"])
    assert read_dashboard_preferences(team.factory, vic).widgets == ("upcoming",)


def test_removing_a_member_removes_their_preferences(team: WorkspaceDb) -> None:
    vic = team.ctx("vic", "viewer")
    save_dashboard_preferences(team.factory, vic, ["loans"])
    remove_member(
        team.factory,
        _owner(team),
        membership_id=team.memberships["vic"],
    )
    with team.factory() as session:
        assert (
            DashboardPreferenceRepository(session, _owner(team)).for_user(team.users["vic"]) is None
        )


def test_missing_data_is_empty_in_an_untouched_workspace(team: WorkspaceDb) -> None:
    data = missing_data(team.factory, _owner(team))
    assert data.empty and data.stale_prices == () and data.missing_work_logs == ()


def test_missing_data_reports_stale_prices_logs_and_planned_imports(team: WorkspaceDb) -> None:
    _portfolio_with_stale_price(team)
    _variable_source(team)
    batch_id = _planned_import(team)
    data = missing_data(team.factory, _owner(team))
    assert not data.empty
    assert [(item.kind, item.name) for item in data.stale_prices] == [("price", "World ETF")]
    assert [(item.source_name, item.work_month) for item in data.missing_work_logs] == [
        ("Freelance", date(2026, 3, 1))
    ]
    assert [(item.id, item.filename) for item in data.planned_imports] == [
        (batch_id, "history.csv")
    ]


def test_missing_data_reports_a_holding_without_a_price(team: WorkspaceDb) -> None:
    _portfolio_with_missing_price(team)
    data = missing_data(team.factory, _owner(team))
    assert [(item.kind, item.state, item.name) for item in data.stale_prices] == [
        ("price", "missing", "No price")
    ]


def test_viewers_see_missing_data_but_not_pending_imports(team: WorkspaceDb) -> None:
    _portfolio_with_stale_price(team)
    _variable_source(team)
    _planned_import(team)
    data = missing_data(team.factory, team.ctx("vic", "viewer"))
    assert data.stale_prices and data.missing_work_logs
    assert data.planned_imports == ()


def test_stale_prices_use_the_workspace_today(team: WorkspaceDb) -> None:
    _portfolio_with_stale_price(team)
    data = missing_data(team.factory, _owner(team))
    assert data.stale_prices[0].as_of == date(2026, 1, 20)
    assert all(isinstance(item.name, str) for item in data.stale_prices)
