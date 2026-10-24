from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest
from sqlalchemy import func, select

from assetboard.db.models.identity import AuditEvent
from assetboard.db.models.tax import TaxRuleVersion
from assetboard.domain.money import Money
from assetboard.domain.percent_or_fixed import PercentOrFixed
from assetboard.domain.taxes import RecordAs
from assetboard.services import taxes
from assetboard.services.context import PermissionDenied
from assetboard.services.workspaces import update_settings
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def team(tmp_path):
    for db in workspace_db(tmp_path):
        db.add_member("owner", "owner")
        db.add_member("viewer", "viewer")
        yield db


def create_rule(team: WorkspaceDb, *, rate_bp: int = 1200) -> UUID:
    return taxes.create_rule(
        team.factory,
        team.ctx("owner", "owner"),
        name="Income tax",
        effect="withheld",
        base="net",
        notes="Annual tax estimate",
        effective_from=date(2026, 1, 1),
        value=PercentOrFixed.percent(rate_bp),
    )


def test_tax_rule_versions_preview_and_archive(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    rule_id = create_rule(team)
    with team.factory() as session:
        assert not taxes.workspace_has_fixed_tax_amounts(session, ctx)
    assert taxes.list_rules(team.factory, ctx)[0].versions[0].value == PercentOrFixed.percent(1200)

    taxes.add_version(
        team.factory,
        ctx,
        rule_id,
        effective_from=date(2027, 1, 1),
        value=PercentOrFixed.percent(1500),
    )
    with pytest.raises(taxes.TaxRuleError, match="version_not_newer"):
        taxes.add_version(
            team.factory,
            ctx,
            rule_id,
            effective_from=date(2026, 6, 1),
            value=PercentOrFixed.percent(1400),
        )

    earlier = taxes.preview(
        team.factory,
        ctx,
        net=Money(100_000, "EUR"),
        day=date(2026, 10, 1),
        record_as=RecordAs.NET,
    )
    later = taxes.preview(
        team.factory,
        ctx,
        net=Money(100_000, "EUR"),
        day=date(2027, 1, 1),
        record_as=RecordAs.NET,
    )
    assert earlier.calculation.withheld[0].amount == Money(12_000, "EUR")
    assert later.calculation.withheld[0].amount == Money(15_000, "EUR")
    assert not earlier.missing_rules

    taxes.update_rule(team.factory, ctx, rule_id, name="Updated income tax", notes="")
    assert taxes.list_rules(team.factory, ctx)[0].name == "Updated income tax"
    taxes.archive_rule(team.factory, ctx, rule_id)
    assert taxes.list_rules(team.factory, ctx)[0].archived
    archived_preview = taxes.preview(
        team.factory,
        ctx,
        net=Money(100_000, "EUR"),
        day=date(2027, 1, 1),
        record_as=RecordAs.NET,
    )
    assert not archived_preview.calculation.withheld

    with team.factory() as session:
        assert session.scalar(select(func.count()).select_from(TaxRuleVersion)) == 2
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 4


def test_preview_reports_rules_without_an_effective_version(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    create_rule(team)
    result = taxes.preview(
        team.factory,
        ctx,
        net=Money(100_000, "EUR"),
        day=date(2025, 12, 31),
        record_as=RecordAs.NET,
    )
    assert result.missing_rules == ("Income tax",)
    assert result.calculation.withheld == ()


def test_service_validates_values_and_roles(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    for fields in (
        {"effect": "added", "base": "gross"},
        {"effect": "bogus", "base": "net"},
    ):
        with pytest.raises(taxes.TaxRuleError):
            taxes.create_rule(
                team.factory,
                ctx,
                name="Invalid",
                notes="",
                effective_from=date(2026, 1, 1),
                value=PercentOrFixed.percent(100),
                **fields,
            )
    with pytest.raises(PermissionDenied):
        taxes.create_rule(
            team.factory,
            team.ctx("viewer", "viewer"),
            name="Forbidden",
            effect="withheld",
            base="net",
            notes="",
            effective_from=date(2026, 1, 1),
            value=PercentOrFixed.percent(100),
        )

    with pytest.raises(taxes.TaxRuleError, match="invalid_amount"):
        taxes.preview(
            team.factory,
            ctx,
            net=Money(-1, "EUR"),
            day=date(2026, 1, 1),
            record_as=RecordAs.NET,
        )


def test_fixed_tax_versions_use_workspace_base_currency(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    rule_id = taxes.create_rule(
        team.factory,
        ctx,
        name="Fixed levy",
        effect="withheld",
        base="net",
        notes="",
        effective_from=date(2026, 1, 1),
        value=PercentOrFixed.fixed_amount(Money(1000, "EUR")),
    )
    with pytest.raises(taxes.TaxRuleError, match="invalid_value"):
        taxes.add_version(
            team.factory,
            ctx,
            rule_id,
            effective_from=date(2027, 1, 1),
            value=PercentOrFixed.fixed_amount(Money(1000, "USD")),
        )
    preview = taxes.preview(
        team.factory,
        ctx,
        net=Money(50_000, "EUR"),
        day=date(2026, 6, 1),
        record_as=RecordAs.NET,
    )
    assert preview.calculation.withheld[0].amount == Money(1000, "EUR")


def test_fixed_tax_version_rejects_a_stale_workspace_currency(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    update_settings(
        team.factory,
        ctx,
        name=ctx.workspace.name,
        base_currency="PLN",
        country_code=ctx.workspace.country_code,
        timezone=ctx.workspace.timezone,
        locale=ctx.workspace.locale,
        auto_copy_plan=False,
        require_two_factor=False,
    )
    with pytest.raises(taxes.TaxRuleError, match="invalid_value"):
        taxes.create_rule(
            team.factory,
            ctx,
            name="Fixed levy",
            effect="withheld",
            base="net",
            notes="",
            effective_from=date(2026, 1, 1),
            value=PercentOrFixed.fixed_amount(Money(1000, "EUR")),
        )
