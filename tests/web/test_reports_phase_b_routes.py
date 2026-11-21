"""F10.4/F10.5 report sections over HTTP: reuse, rendering and absence without data."""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from assetboard.clock import FixedClock
from assetboard.db.models.identity import User, Workspace
from assetboard.db.models.investments import InvestmentTransaction
from assetboard.db.models.networth import NetWorthItem
from assetboard.db.repositories import PricePointRepository
from assetboard.services.context import Context, Role, UserRef, WorkspaceRef
from assetboard.services.investment_transactions import create_transaction
from assetboard.services.investments import create_asset_class, create_instrument
from assetboard.services.networth import (
    add_valuation,
    create_item,
)
from assetboard.services.networth import (
    create_category as create_networth_category,
)
from tests.conftest import OwnerApp

NOW = "2026-10-14T12:00:00+00:00"


@pytest.fixture
def reports(owner: OwnerApp) -> OwnerApp:
    owner.app.state.clock = FixedClock(NOW)
    return owner


def _context(app: OwnerApp) -> Context:
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
                taxes_enabled=workspace.taxes_enabled,
            ),
            role=Role.OWNER,
            clock=app.app.state.clock,
        )


def _net_worth(app: OwnerApp) -> None:
    ctx = _context(app)
    category = create_networth_category(
        app.app.state.session_factory, ctx, side="asset", name="Investments", color="#16a34a"
    )
    item = create_item(
        app.app.state.session_factory,
        ctx,
        locale="en",
        side="asset",
        category_id=category.id,
        name="Fund",
        value="100",
        currency="EUR",
        include_in_net_worth=True,
        as_of=date(2024, 12, 31),
    )
    add_valuation(
        app.app.state.session_factory,
        ctx,
        item.id,
        locale="en",
        value="150",
        as_of=date(2025, 12, 31),
    )
    add_valuation(
        app.app.state.session_factory,
        ctx,
        item.id,
        locale="en",
        value="200",
        as_of=date(2026, 6, 30),
    )
    create_item(
        app.app.state.session_factory,
        ctx,
        locale="en",
        side="asset",
        category_id=category.id,
        name="Dollar account",
        value="50",
        currency="USD",
        include_in_net_worth=True,
        as_of=date(2024, 12, 31),
    )


def _portfolio(app: OwnerApp) -> Any:
    ctx = _context(app)
    asset_class = create_asset_class(
        app.app.state.session_factory, ctx, name="Phase B Stocks", color="#2563eb", investable=True
    )
    instrument = create_instrument(
        app.app.state.session_factory,
        ctx,
        name="World ETF",
        symbol="",
        type="etf",
        quote_currency="EUR",
        unit_label="share",
        allocations=[(asset_class.id, 10_000)],
        locale="en",
    )
    create_transaction(
        app.app.state.session_factory,
        ctx,
        instrument_id=instrument.id,
        type_value="buy",
        date_value="2024-01-01",
        quantity_value="10",
        unit_price_value="100",
        fee_value="0",
        fx_value="",
        note_value="",
        locale="en",
    )
    for day, price in (
        ("2024-12-01", "110"),
        ("2025-12-01", "120"),
        ("2026-10-01", "130"),
    ):
        with app.app.state.session_factory.begin() as session:
            PricePointRepository(session, ctx).create(
                instrument_id=instrument.id,
                date=date.fromisoformat(day),
                price=Decimal(price),
                source="manual",
            )
    return instrument.id


def _section(html: str, key: str) -> str:
    match = re.search(rf'<section[^>]*data-report-series="{key}".*?</section>', html, re.DOTALL)
    assert match is not None, key
    return match.group(0)


def _rows(section: str) -> list[str]:
    return re.findall(r"<tr data-report-series-row>.*?</tr>", section, re.DOTALL)


def _chart_data(section: str, key: str) -> dict[str, Any]:
    match = re.search(
        rf'<script type="application/json" id="reports-{key}-data">(.*?)</script>',
        section,
        re.DOTALL,
    )
    assert match is not None
    return json.loads(match.group(1))


def test_phase_b_sections_are_absent_without_data(reports: OwnerApp) -> None:
    html = reports.client.get("/reports").text
    assert "data-report-series" not in html


def test_an_excluded_only_net_worth_item_does_not_leave_an_empty_section(
    reports: OwnerApp,
) -> None:
    ctx = _context(reports)
    category = create_networth_category(
        reports.app.state.session_factory, ctx, side="asset", name="Excluded", color="#16a34a"
    )
    create_item(
        reports.app.state.session_factory,
        ctx,
        locale="en",
        side="asset",
        category_id=category.id,
        name="Excluded fund",
        value="100",
        currency="EUR",
        include_in_net_worth=False,
        as_of=date(2024, 12, 31),
    )
    html = reports.client.get("/reports?from=2024&to=2026&step=year").text
    assert 'data-report-series="net-worth"' not in html


def test_a_no_value_only_net_worth_item_does_not_leave_an_empty_section(
    reports: OwnerApp,
) -> None:
    ctx = _context(reports)
    category = create_networth_category(
        reports.app.state.session_factory, ctx, side="asset", name="No value", color="#16a34a"
    )
    with reports.db() as session, session.begin():
        session.add(
            NetWorthItem(
                workspace_id=reports.home_id,
                side="asset",
                category_id=category.id,
                name="No value fund",
                currency="EUR",
                notes="",
                valuation_mode="manual",
                link_type=None,
                link_id=None,
                include_in_net_worth=True,
                investable=False,
                estimate_between=True,
            )
        )
    html = reports.client.get("/reports?from=2024&to=2026&step=year").text
    assert 'data-report-series="net-worth"' not in html


def test_net_worth_report_section_reuses_the_derived_series(reports: OwnerApp) -> None:
    _net_worth(reports)
    html = reports.client.get("/reports?from=2024&to=2026&step=year").text
    section = _section(html, "net-worth")
    assert ">Net worth over time</h2>" in section
    assert 'data-chart="line"' in section
    data = _chart_data(section, "net-worth")
    assert data["series"][0]["values"][-1] == 20_000
    assert data["labels"] == ["2024", "2025", "2026"]
    assert len(data["links"]) == 3 and "year=2024" in data["links"][0]
    # UX-08: the warning is in the tooltips and once at section level, not on every row.
    assert any(
        "another currency was not counted" in line
        for tooltip in data["tooltips"]
        for line in tooltip
    )
    notes = re.search(r"<ul[^>]*data-report-series-notes>(.*?)</ul>", section, re.DOTALL)
    assert notes is not None
    assert notes.group(1).count("another currency was not counted") == 1
    assert "data-chart-table-toggle" in section
    assert 'id="reports-net-worth-table"' in section
    assert "data-report-series-period" in section
    assert "had no value yet" not in section
    assert "estimated value" not in section
    rows = _rows(section)
    assert len(rows) == 3
    assert all("was not counted" not in row for row in rows)
    assert "2024" in rows[0] and "2026" in rows[-1]
    assert "data-report-series-so-far" in rows[-1]
    assert 'data-report-series="portfolio"' not in html


def test_portfolio_report_section_reuses_the_performance_series(reports: OwnerApp) -> None:
    _portfolio(reports)
    html = reports.client.get("/reports?from=2024&to=2026&step=year").text
    section = _section(html, "portfolio")
    assert ">Portfolio value over time</h2>" in section
    assert 'data-chart="performance"' in section
    data = _chart_data(section, "portfolio")
    assert data["value"][-1] == 130_000
    assert data["labels"] == ["2024", "2025", "2026"]
    assert len(data["links"]) == 3 and "year=2024" in data["links"][0]
    assert all(
        "not counted" not in line and "estimated value" not in line
        for tooltip in data["tooltips"]
        for line in tooltip
    )
    assert "data-chart-table-toggle" in section
    assert 'id="reports-portfolio-table"' in section
    assert data["invested"][-1] == 100_000
    rows = _rows(section)
    assert len(rows) == 3
    assert "2024" in rows[0] and "2026" in rows[-1]
    assert "data-report-series-so-far" in rows[-1]


def test_portfolio_gain_cells_follow_the_gain_loss_convention(reports: OwnerApp) -> None:
    instrument_id = _portfolio(reports)
    ctx = _context(reports)
    with reports.app.state.session_factory.begin() as session:
        repository = PricePointRepository(session, ctx)
        point = repository.manual_on(instrument_id, date(2024, 12, 1))
        assert point is not None
        repository.update(point.id, price=Decimal("50"))
    html = reports.client.get("/reports?from=2024&to=2026&step=year").text
    rows = _rows(_section(html, "portfolio"))
    assert "text-danger" in rows[0]
    assert "text-success-strong" in rows[-1]


def test_a_non_base_only_net_worth_section_explains_itself(reports: OwnerApp) -> None:
    ctx = _context(reports)
    category = create_networth_category(
        reports.app.state.session_factory, ctx, side="asset", name="Dollars", color="#16a34a"
    )
    create_item(
        reports.app.state.session_factory,
        ctx,
        locale="en",
        side="asset",
        category_id=category.id,
        name="Dollar only",
        value="100",
        currency="USD",
        include_in_net_worth=True,
        as_of=date(2024, 12, 31),
    )
    html = reports.client.get("/reports?from=2024&to=2026&step=year").text
    section = _section(html, "net-worth")
    assert "data-chart-empty" in section
    assert "No values to chart in this range." in section
    assert "another currency was not counted" in section
    assert not _rows(section)


def test_an_undated_only_portfolio_section_explains_itself(reports: OwnerApp) -> None:
    ctx = _context(reports)
    asset_class = create_asset_class(
        reports.app.state.session_factory, ctx, name="Legacy", color="#2563eb", investable=True
    )
    instrument = create_instrument(
        reports.app.state.session_factory,
        ctx,
        name="Legacy lot",
        symbol="",
        type="etf",
        quote_currency="EUR",
        unit_label="share",
        allocations=[(asset_class.id, 10_000)],
        locale="en",
    )
    with reports.app.state.session_factory.begin() as session:
        session.add(
            InvestmentTransaction(
                workspace_id=reports.home_id,
                instrument_id=instrument.id,
                type="buy",
                date=None,
                quantity=Decimal("10"),
                unit_price=Decimal("100"),
                fee_minor=0,
                quote_currency="EUR",
                fx_to_base=Decimal("1"),
                note="",
            )
        )
    html = reports.client.get("/reports?from=2024&to=2026&step=year").text
    section = _section(html, "portfolio")
    assert "data-chart-empty" in section
    assert "undated legacy transaction" in section
    assert not _rows(section)


def test_phase_b_sections_follow_the_report_period_step(reports: OwnerApp) -> None:
    _net_worth(reports)
    _portfolio(reports)
    html = reports.client.get("/reports?from=2026&to=2026&step=month").text
    assert len(_rows(_section(html, "net-worth"))) == 10  # January through October
    assert len(_rows(_section(html, "portfolio"))) == 10
