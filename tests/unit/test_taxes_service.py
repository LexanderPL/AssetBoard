from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest
from sqlalchemy import func, select

from assetboard.config import Settings
from assetboard.db.models.identity import AuditEvent
from assetboard.db.models.scheduled import ScheduledItem
from assetboard.db.models.tax import TaxRuleVersion
from assetboard.db.repositories import (
    CategoryRepository,
    CurrentWorkspaceRepository,
    ScheduledOccurrenceRepository,
)
from assetboard.domain.money import Money
from assetboard.domain.percent_or_fixed import PercentOrFixed
from assetboard.domain.recurrence import Frequency, RecurrenceRule
from assetboard.domain.taxes import PaymentTiming, TaxBase, TaxKind
from assetboard.domain.transactions import TransactionDirection
from assetboard.services import income, scheduled, taxes
from assetboard.services.context import PermissionDenied
from assetboard.services.workspaces import update_settings
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def team(tmp_path):
    for db in workspace_db(tmp_path):
        db.add_member("owner", "owner")
        db.add_member("editor", "editor")
        db.add_member("viewer", "viewer")
        yield db


def expense_category(team: WorkspaceDb, name: str = "Taxes", treatment: str = "regular") -> UUID:
    ctx = team.ctx("owner", "owner")
    with team.factory.begin() as session:
        return (
            CategoryRepository(session, ctx)
            .create(
                name=name,
                name_key=name.lower(),
                kind="expense",
                treatment=treatment,
                color="#dc2626",
                sort_order=0,
            )
            .id
        )


def income_category(team: WorkspaceDb) -> UUID:
    ctx = team.ctx("owner", "owner")
    with team.factory.begin() as session:
        return (
            CategoryRepository(session, ctx)
            .create(
                name="Salary",
                name_key="salary",
                kind="income",
                treatment="regular",
                color="#16a34a",
                sort_order=0,
            )
            .id
        )


def create_rule(team: WorkspaceDb, category: UUID, *, kind: str = "self_paid") -> UUID:
    return taxes.create_rule(
        team.factory,
        team.ctx("owner", "owner"),
        name="Income tax",
        kind=kind,
        value=PercentOrFixed.percent(1200),
        payment_day=20,
        payment_timing="next_month",
        payment_category_id=category,
    )


def test_create_with_kinds_first_version_from_the_start_and_defaults(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    category = expense_category(team)
    rule_id = create_rule(team, category)
    withheld = taxes.create_rule(
        team.factory,
        ctx,
        name="Salary tax",
        kind="payer_withheld",
        base="gross",
        value=PercentOrFixed.percent(1700),
        payment_category_id=category,  # ignored: nothing is planned
    )
    vat = taxes.create_rule(
        team.factory,
        ctx,
        name="VAT",
        kind="added",
        base="gross",  # added taxes are always on net
        value=PercentOrFixed.percent(2300),
        payment_day=25,
        payment_category_id=category,
    )
    views = {view.id: view for view in taxes.list_rules(team.factory, ctx)}
    first = views[rule_id]
    assert first.versions[0].effective_from == date(1900, 1, 1)
    assert (first.kind, first.payment_day, first.payment_timing) == (
        TaxKind.SELF_PAID,
        20,
        PaymentTiming.NEXT_MONTH,
    )
    assert first.payment_category_id == category
    assert (views[withheld].base, views[withheld].payment_category_id) == (TaxBase.GROSS, None)
    assert views[vat].base == TaxBase.NET
    assert first.current == PercentOrFixed.percent(1200)
    assert not first.locked and first.used_by == ()


def test_validation_and_roles(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    category = expense_category(team)
    transfer = expense_category(team, "Moves", treatment="transfer")
    base = {"name": "X", "value": PercentOrFixed.percent(100), "payment_category_id": category}
    cases = [
        ({"kind": "bogus"}, "invalid_rule"),
        ({"kind": "self_paid", "payment_day": 32}, "invalid_schedule"),
        ({"kind": "self_paid", "payment_timing": "yearly"}, "invalid_schedule"),
        ({"kind": "self_paid", "payment_category_id": None}, "invalid_category"),
        ({"kind": "added", "payment_category_id": transfer}, "invalid_category"),
        ({"kind": "self_paid", "name": " "}, "invalid_name"),
    ]
    for fields, code in cases:
        with pytest.raises(taxes.TaxRuleError, match=code):
            taxes.create_rule(team.factory, ctx, **(base | fields))  # type: ignore[arg-type]
    with pytest.raises(PermissionDenied):
        taxes.create_rule(team.factory, team.ctx("viewer", "viewer"), kind="self_paid", **base)


def test_edit_kind_schedule_and_lock_after_posted_income(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    category = expense_category(team)
    rule_id = create_rule(team, category)
    taxes.update_rule(
        team.factory,
        ctx,
        rule_id,
        name="Flat tax",
        kind="payer_withheld",
        base="gross",
        payment_day=5,
        payment_timing="after_quarter",
    )
    view = taxes.get_rule(team.factory, ctx, rule_id)
    assert (view.name, view.kind, view.base, view.payment_timing) == (
        "Flat tax",
        TaxKind.PAYER_WITHHELD,
        TaxBase.GROSS,
        PaymentTiming.AFTER_QUARTER,
    )
    with pytest.raises(taxes.TaxRuleError, match="invalid_category"):
        taxes.update_rule(team.factory, ctx, rule_id, name="Flat tax", kind="self_paid")

    # A posted income occurrence of a source using the tax locks kind and base.
    source_id = income.create_source(
        team.factory,
        ctx,
        name="Contract",
        kind="fixed",
        category_id=income_category(team),
        fixed_minor=100_000,
        rate_minor=None,
        unit_label="day",
        pay_day_value=10,
        pay_lag_months=0,
        expected_mode="manual",
        expected_units_value=None,
        average_months=3,
        active_from=date(2026, 1, 1),
        active_to=None,
    )
    income.attach_tax_rule(team.factory, ctx, source_id, tax_rule_id=rule_id)
    income.generate_workspace(team.factory, ctx, ctx.clock, Settings(_env_file=None))
    with team.factory.begin() as session:
        occurrence = next(
            row for row in ScheduledOccurrenceRepository(session, ctx).for_income_source(source_id)
        )
        occurrence.status = "posted"
    view = taxes.get_rule(team.factory, ctx, rule_id)
    assert view.locked and view.used_by == ("Contract",)
    with pytest.raises(taxes.TaxRuleError, match="kind_locked"):
        taxes.update_rule(
            team.factory, ctx, rule_id, name="Flat tax", kind="payer_withheld", base="net"
        )
    # Name and schedule still change.
    taxes.update_rule(
        team.factory, ctx, rule_id, name="Renamed", kind="payer_withheld", base="gross"
    )
    assert taxes.get_rule(team.factory, ctx, rule_id).name == "Renamed"


def test_versions_archive_and_fixed_amounts(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    category = expense_category(team)
    rule_id = create_rule(team, category)
    with team.factory() as session:
        assert not taxes.workspace_has_fixed_tax_amounts(session, ctx)
    taxes.add_version(
        team.factory,
        ctx,
        rule_id,
        effective_from=date(2027, 1, 1),
        value=PercentOrFixed.fixed_amount(Money(1000, "EUR")),
    )
    with pytest.raises(taxes.TaxRuleError, match="version_not_newer"):
        taxes.add_version(
            team.factory,
            ctx,
            rule_id,
            effective_from=date(2026, 6, 1),
            value=PercentOrFixed.percent(1400),
        )
    with pytest.raises(taxes.TaxRuleError, match="invalid_value"):
        taxes.add_version(
            team.factory,
            ctx,
            rule_id,
            effective_from=date(2028, 1, 1),
            value=PercentOrFixed.fixed_amount(Money(1000, "USD")),
        )
    with team.factory() as session:
        assert taxes.workspace_has_fixed_tax_amounts(session, ctx)
    taxes.archive_rule(team.factory, ctx, rule_id)
    assert taxes.list_rules(team.factory, ctx)[0].archived
    with pytest.raises(taxes.TaxRuleError, match="archived"):
        taxes.update_rule(team.factory, ctx, rule_id, name="X", kind="payer_withheld")
    with team.factory() as session:
        assert session.scalar(select(func.count()).select_from(TaxRuleVersion)) == 2


def test_fixed_tax_rejects_a_stale_workspace_currency(team: WorkspaceDb) -> None:
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
            kind="payer_withheld",
            value=PercentOrFixed.fixed_amount(Money(1000, "EUR")),
        )


@pytest.mark.parametrize(
    ("kind", "received", "payment"),
    [
        ("added", 1_120_000, 120_000),
        ("self_paid", 1_000_000, 120_000),
        ("payer_withheld", 880_000, 0),
    ],
)
def test_example_for_10000_net(team: WorkspaceDb, kind: str, received: int, payment: int) -> None:
    ctx = team.ctx("owner", "owner")  # the fake clock's today is in 2026
    example = taxes.example(
        ctx,
        name="Tax",
        kind=TaxKind(kind),
        base=TaxBase.NET,
        value=PercentOrFixed.percent(1200),
        payment_day=25,
        payment_timing=PaymentTiming.NEXT_MONTH,
    )
    today = ctx.clock.today(ctx.workspace.timezone)
    assert example.paid_on.day == 10 and example.paid_on > today
    assert example.payment_date.day == 25
    assert example.breakdown.net == Money(1_000_000, "EUR")
    assert example.breakdown.ledger_amount == Money(received, "EUR")
    assert sum(line.amount.amount_minor for line in example.breakdown.payments) == payment


def test_taxes_switch_roles_and_refusal_while_attached(team: WorkspaceDb) -> None:
    owner = team.ctx("owner", "owner")
    with pytest.raises(PermissionDenied):
        taxes.set_taxes_enabled(team.factory, team.ctx("editor", "editor"), True)
    taxes.set_taxes_enabled(team.factory, owner, True)
    with team.factory() as session:
        assert CurrentWorkspaceRepository(session, owner).get().taxes_enabled
    category = expense_category(team)
    rule_id = create_rule(team, category)
    source_id = income.create_source(
        team.factory,
        owner,
        name="Contract",
        kind="fixed",
        category_id=income_category(team),
        fixed_minor=100_000,
        rate_minor=None,
        unit_label="day",
        pay_day_value=10,
        pay_lag_months=0,
        expected_mode="manual",
        expected_units_value=None,
        average_months=3,
        active_from=date(2026, 1, 1),
        active_to=None,
    )
    income.attach_tax_rule(team.factory, owner, source_id, tax_rule_id=rule_id)
    assert taxes.sources_with_taxes(team.factory, owner) == ("Contract",)
    with pytest.raises(taxes.TaxesInUse) as refused:
        taxes.set_taxes_enabled(team.factory, owner, False)
    assert refused.value.sources == ("Contract",)
    income.archive_source(team.factory, owner, source_id)  # archived sources do not count
    taxes.set_taxes_enabled(team.factory, owner, False)
    with team.factory() as session:
        assert not CurrentWorkspaceRepository(session, owner).get().taxes_enabled
        actions = session.scalars(select(AuditEvent.action)).all()
    assert actions.count("workspace.settings_updated") == 2


def test_other_tax_is_a_recurring_expense_marked_as_tax(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    category = expense_category(team)
    settings = Settings(_env_file=None)
    item_id = scheduled.save_item(
        team.factory,
        ctx,
        scheduled.ScheduledInput(
            name="Vehicle tax",
            direction=TransactionDirection.EXPENSE,
            category_id=category,
            amount=Money(48_000, "EUR"),
            rule=RecurrenceRule(
                start_date=date(2027, 1, 31), freq=Frequency.YEARLY, month=1, month_day=31
            ),
        ),
        settings,
        require_kind="expense",
        is_tax=True,
    )
    [other] = scheduled.list_tax_items(team.factory, ctx)
    assert (other.id, other.name, other.amount, other.freq) == (
        item_id,
        "Vehicle tax",
        Money(48_000, "EUR"),
        Frequency.YEARLY,
    )
    assert other.category_name == "Taxes" and other.next_due == date(2027, 1, 31)
    # An edit through the recurring form (is_tax omitted) keeps the flag.
    values = scheduled.get_item(team.factory, ctx, item_id)
    scheduled.save_item(team.factory, ctx, values, settings, item_id=item_id)
    with team.factory() as session:
        assert session.get(ScheduledItem, item_id).is_tax  # type: ignore[union-attr]
