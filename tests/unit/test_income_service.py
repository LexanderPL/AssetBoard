from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from assetboard.db.models.scheduled import ScheduledOccurrence
from assetboard.db.repositories import (
    CategoryRepository,
    CurrentWorkspaceRepository,
    ScheduledOccurrenceRepository,
)
from assetboard.domain.percent_or_fixed import PercentOrFixed
from assetboard.services import income, taxes
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def team(tmp_path):
    for db in workspace_db(tmp_path):
        db.add_member("owner", "owner")
        db.add_member("viewer", "viewer")
        yield db


def categories(team: WorkspaceDb, ctx):
    with team.factory.begin() as session:
        if ctx.workspace.base_currency != "PLN":
            CurrentWorkspaceRepository(session, ctx).update(base_currency="PLN")
        repository = CategoryRepository(session, ctx)
        income_category = repository.create(
            name="Salary",
            name_key="salary",
            kind="income",
            treatment="regular",
            color="#16a34a",
            sort_order=0,
        )
        expense_category = repository.create(
            name="Tax",
            name_key="tax",
            kind="expense",
            treatment="regular",
            color="#dc2626",
            sort_order=1,
        )
        return income_category.id, expense_category.id


def test_income_source_golden_example_generates_tax_payment(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    income_category, expense_category = categories(team, ctx)
    rule_id = taxes.create_rule(
        team.factory,
        ctx,
        name="VAT",
        effect="added",
        base="net",
        notes="",
        effective_from=date(2026, 1, 1),
        value=PercentOrFixed.percent(2300),
    )
    source_id = income.create_source(
        team.factory,
        ctx,
        name="Contract work",
        kind="variable",
        category_id=income_category,
        fixed_minor=None,
        rate_minor=123_000,
        unit_label="day",
        pay_day_value=10,
        pay_lag_months=1,
        expected_mode="manual",
        expected_units_value=None,
        average_months=3,
        record_as="cash_received",
        active_from=date(2026, 1, 1),
        active_to=None,
    )
    income.attach_tax_rule(
        team.factory,
        ctx,
        source_id,
        tax_rule_id=rule_id,
        payment_category_id=expense_category,
        due_day=20,
        due_month_offset=0,
        generate_payment=True,
    )
    assert not income.log_work(team.factory, ctx, source_id, date(2026, 8, 1), Decimal("19"))

    with team.factory() as session:
        parent = ScheduledOccurrenceRepository(session, ctx).for_income_work_month(
            source_id, date(2026, 8, 1)
        )
        assert parent is not None
        assert parent.due_date == date(2026, 9, 10)
        assert parent.expected_minor == 2_874_510
        assert parent.amount_state == "confirmed"
        children = ScheduledOccurrenceRepository(session, ctx).children(parent.id)
        assert len(children) == 1
        assert children[0].generated_by == "tax"
        assert children[0].due_date == date(2026, 9, 20)
        assert children[0].expected_minor == 537_510
        assert children[0].category_id == expense_category

    listed = income.list_sources(team.factory, ctx)
    source = next(row for row in listed if row.id == source_id)
    assert source.work_logs[0].units == Decimal("19")


def test_posted_income_occurrence_is_not_recalculated(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    income_category, _expense_category = categories(team, ctx)
    source_id = income.create_source(
        team.factory,
        ctx,
        name="Work",
        kind="variable",
        category_id=income_category,
        fixed_minor=None,
        rate_minor=10_000,
        unit_label="day",
        pay_day_value=10,
        pay_lag_months=1,
        expected_mode="manual",
        expected_units_value=None,
        average_months=3,
        record_as="net",
        active_from=date(2026, 1, 1),
        active_to=None,
    )
    income.log_work(team.factory, ctx, source_id, date(2026, 8, 1), Decimal("2"))
    with team.factory() as session:
        occurrence = ScheduledOccurrenceRepository(session, ctx).for_income_work_month(
            source_id, date(2026, 8, 1)
        )
        assert occurrence is not None
        occurrence_id, amount = occurrence.id, occurrence.expected_minor
    from assetboard.services.occurrences import post_occurrence

    post_occurrence(
        team.factory,
        ctx,
        occurrence_id,
        amount_minor=amount,
        day=date(2026, 9, 10),
    )
    assert income.log_work(team.factory, ctx, source_id, date(2026, 8, 1), Decimal("7"))
    with team.factory() as session:
        stored = session.scalar(
            select(ScheduledOccurrence).where(ScheduledOccurrence.id == occurrence_id)
        )
        assert stored is not None and stored.expected_minor == amount
        assert stored.status == "posted"


def test_missing_work_log_tracks_pay_month_and_disappears_after_logging(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("owner", "owner")
    income_category, _expense_category = categories(team, ctx)
    source_id = income.create_source(
        team.factory,
        ctx,
        name="Contract work",
        kind="variable",
        category_id=income_category,
        fixed_minor=None,
        rate_minor=10_000,
        unit_label="day",
        pay_day_value=10,
        pay_lag_months=1,
        expected_mode="manual",
        expected_units_value=None,
        average_months=3,
        record_as="net",
        active_from=date(2026, 1, 1),
        active_to=None,
    )

    assert income.missing_work_logs(team.factory, ctx, date(2026, 3, 1)) == [
        (source_id, "Contract work", date(2026, 2, 1))
    ]
    income.log_work(team.factory, ctx, source_id, date(2026, 2, 1), Decimal("7.5"))
    assert income.missing_work_logs(team.factory, ctx, date(2026, 3, 1)) == []
