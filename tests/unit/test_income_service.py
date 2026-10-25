from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from assetboard.config import Settings
from assetboard.db.models.scheduled import ScheduledOccurrence
from assetboard.db.repositories import (
    CategoryRepository,
    CurrentWorkspaceRepository,
    ScheduledOccurrenceRepository,
)
from assetboard.domain.money import Money
from assetboard.domain.percent_or_fixed import PercentOrFixed
from assetboard.services import income, taxes
from assetboard.services.context import PermissionDenied
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


def _source(team: WorkspaceDb, ctx, category_id, **overrides):
    values = {
        "name": "Contract work",
        "kind": "variable",
        "category_id": category_id,
        "fixed_minor": None,
        "rate_minor": 10_000,
        "unit_label": "day",
        "pay_day_value": 10,
        "pay_lag_months": 1,
        "expected_mode": "manual",
        "expected_units_value": None,
        "average_months": 3,
        "record_as": "net",
        "active_from": date(2026, 1, 1),
        "active_to": None,
    } | overrides
    return income.create_source(team.factory, ctx, **values)


def _occurrence(team: WorkspaceDb, ctx, source_id, work_month: date):
    with team.factory() as session:
        return ScheduledOccurrenceRepository(session, ctx).for_income_work_month(
            source_id, work_month
        )


def _income_months(team: WorkspaceDb, ctx, source_id) -> list[date]:
    with team.factory() as session:
        rows = ScheduledOccurrenceRepository(session, ctx).for_income_source(source_id)
        return sorted(row.work_month for row in rows if row.work_month is not None)


def test_missing_work_log_ignores_months_before_the_source_starts(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    income_category, _expense = categories(team, ctx)
    _source(team, ctx, income_category, active_from=date(2026, 3, 1))
    assert income.missing_work_logs(team.factory, ctx, date(2026, 3, 1)) == []


@pytest.mark.parametrize(
    ("lag", "due"),
    [(0, date(2026, 8, 10)), (1, date(2026, 9, 10)), (2, date(2026, 10, 10))],
)
def test_pay_lag_moves_the_occurrence_to_the_pay_month(
    team: WorkspaceDb, lag: int, due: date
) -> None:
    ctx = team.ctx("owner", "owner")
    income_category, _expense = categories(team, ctx)
    source_id = _source(team, ctx, income_category, pay_lag_months=lag)
    income.log_work(team.factory, ctx, source_id, date(2026, 8, 1), Decimal("7.5"))
    occurrence = _occurrence(team, ctx, source_id, date(2026, 8, 1))
    assert occurrence is not None
    assert occurrence.due_date == due
    assert occurrence.expected_minor == 75_000


def test_occurrence_is_estimated_until_logged_then_confirmed(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    income_category, _expense = categories(team, ctx)
    # Clock: 2026-03-01. Lag 1: February's work is paid on 2026-03-10, still ahead.
    source_id = _source(
        team, ctx, income_category, expected_mode="fixed", expected_units_value=Decimal("10")
    )
    income.generate_workspace(team.factory, ctx, team.clock, Settings(_env_file=None))
    february = _occurrence(team, ctx, source_id, date(2026, 2, 1))
    assert february is not None
    assert (february.due_date, february.amount_state) == (date(2026, 3, 10), "estimated")
    assert february.expected_minor == 100_000

    income.log_work(team.factory, ctx, source_id, date(2026, 2, 1), Decimal("12"))
    february = _occurrence(team, ctx, source_id, date(2026, 2, 1))
    assert february is not None
    assert (february.amount_state, february.expected_minor) == ("confirmed", 120_000)


def test_manual_mode_generates_estimated_zero(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    income_category, _expense = categories(team, ctx)
    source_id = _source(team, ctx, income_category, pay_lag_months=0)
    income.generate_workspace(team.factory, ctx, team.clock, Settings(_env_file=None))
    march = _occurrence(team, ctx, source_id, date(2026, 3, 1))
    assert march is not None
    assert (march.amount_state, march.expected_minor) == ("estimated", 0)


def test_active_to_ends_generation(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    income_category, _expense = categories(team, ctx)
    source_id = _source(team, ctx, income_category, pay_lag_months=0, active_to=date(2026, 5, 1))
    income.generate_workspace(team.factory, ctx, team.clock, Settings(_env_file=None))
    assert _income_months(team, ctx, source_id) == [
        date(2026, 3, 1),
        date(2026, 4, 1),
        date(2026, 5, 1),
    ]


def test_skipping_the_parent_removes_its_pending_tax_payments(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    income_category, expense_category = categories(team, ctx)
    rule_id = taxes.create_rule(
        team.factory,
        ctx,
        name="Income tax",
        effect="withheld",
        base="net",
        notes="",
        effective_from=date(2026, 1, 1),
        value=PercentOrFixed.percent(1200),
    )
    source_id = _source(team, ctx, income_category)
    income.attach_tax_rule(
        team.factory,
        ctx,
        source_id,
        tax_rule_id=rule_id,
        payment_category_id=expense_category,
        due_day=20,
        due_month_offset=1,
        generate_payment=True,
    )
    income.log_work(team.factory, ctx, source_id, date(2026, 8, 1), Decimal("10"))
    parent = _occurrence(team, ctx, source_id, date(2026, 8, 1))
    assert parent is not None
    with team.factory() as session:
        children = ScheduledOccurrenceRepository(session, ctx).children(parent.id)
        assert [(c.due_date, c.expected_minor) for c in children] == [(date(2026, 10, 20), 12_000)]

    from assetboard.services.occurrences import skip_occurrence

    skip_occurrence(team.factory, ctx, parent.id)
    with team.factory() as session:
        assert ScheduledOccurrenceRepository(session, ctx).children(parent.id) == []


def test_relogging_keeps_a_rescheduled_due_date(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    income_category, _expense = categories(team, ctx)
    source_id = _source(team, ctx, income_category)
    income.log_work(team.factory, ctx, source_id, date(2026, 8, 1), Decimal("2"))
    occurrence = _occurrence(team, ctx, source_id, date(2026, 8, 1))
    assert occurrence is not None
    from assetboard.services.occurrences import reschedule_occurrence

    reschedule_occurrence(team.factory, ctx, occurrence.id, date(2026, 9, 14))
    income.log_work(team.factory, ctx, source_id, date(2026, 8, 1), Decimal("3"))
    occurrence = _occurrence(team, ctx, source_id, date(2026, 8, 1))
    assert occurrence is not None
    assert (occurrence.due_date, occurrence.expected_minor) == (date(2026, 9, 14), 30_000)


def test_viewers_cannot_change_income(team: WorkspaceDb) -> None:
    owner = team.ctx("owner", "owner")
    income_category, _expense = categories(team, owner)
    source_id = _source(team, owner, income_category)
    viewer = team.ctx("viewer", "viewer")
    assert [row.id for row in income.list_sources(team.factory, viewer)] == [source_id]
    with pytest.raises(PermissionDenied):
        _source(team, viewer, income_category)
    with pytest.raises(PermissionDenied):
        income.log_work(team.factory, viewer, source_id, date(2026, 8, 1), Decimal("1"))
    with pytest.raises(PermissionDenied):
        income.archive_source(team.factory, viewer, source_id)


def test_working_days_need_a_supported_holiday_country(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    income_category, _expense = categories(team, ctx)
    with team.factory.begin() as session:
        CurrentWorkspaceRepository(session, ctx).update(country_code="ZZ")
    with pytest.raises(income.IncomeError) as error:
        _source(team, ctx, income_category, expected_mode="working_days")
    assert error.value.code == "unsupported_holidays"


# F3.3 golden example fed by the F3.4 rate x units: 1 230.00 x 19 days = 23 370.00 net.
GOLDEN_PAYMENTS = {
    "cash_received": (2_874_510, [537_510, 280_440, 100_000]),
    "net": (2_337_000, [280_440, 100_000]),
    "take_home": (1_956_560, []),
}


@pytest.mark.parametrize("record_as", sorted(GOLDEN_PAYMENTS))
def test_golden_tax_rules_through_an_income_source(team: WorkspaceDb, record_as: str) -> None:
    ctx = team.ctx("owner", "owner")
    income_category, expense_category = categories(team, ctx)
    rules = [
        ("VAT", "added", PercentOrFixed.percent(2300)),
        ("Income tax", "withheld", PercentOrFixed.percent(1200)),
        ("Levy", "withheld", PercentOrFixed.fixed_amount(Money(100_000, "PLN"))),
    ]
    source_id = _source(team, ctx, income_category, rate_minor=123_000, record_as=record_as)
    for name, effect, value in rules:
        rule_id = taxes.create_rule(
            team.factory,
            ctx,
            name=name,
            effect=effect,
            base="net",
            notes="",
            effective_from=date(2026, 1, 1),
            value=value,
        )
        income.attach_tax_rule(
            team.factory,
            ctx,
            source_id,
            tax_rule_id=rule_id,
            payment_category_id=expense_category,
            due_day=20,
            due_month_offset=1,
            generate_payment=True,
        )
    income.log_work(team.factory, ctx, source_id, date(2026, 8, 1), Decimal("19"))
    parent = _occurrence(team, ctx, source_id, date(2026, 8, 1))
    assert parent is not None
    ledger, payments = GOLDEN_PAYMENTS[record_as]
    assert (parent.due_date, parent.expected_minor) == (date(2026, 9, 10), ledger)
    with team.factory() as session:
        children = ScheduledOccurrenceRepository(session, ctx).children(parent.id)
        assert sorted((c.expected_minor for c in children), reverse=True) == payments
        assert all(c.due_date == date(2026, 10, 20) for c in children)
        assert all(c.generated_by == "tax" and c.direction == "expense" for c in children)
