from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from assetboard.config import Settings
from assetboard.db.models.identity import AuditEvent
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
        assert children[0].label == "Contract work tax payment"

    # The label is in the workspace language; a recalculation renames pending payments.
    with team.factory.begin() as session:
        CurrentWorkspaceRepository(session, ctx).update(locale="pl")
    assert not income.log_work(team.factory, ctx, source_id, date(2026, 8, 1), Decimal("19"))
    with team.factory() as session:
        repository = ScheduledOccurrenceRepository(session, ctx)
        parent = repository.for_income_work_month(source_id, date(2026, 8, 1))
        assert parent is not None
        assert [child.label for child in repository.children(parent.id)] == [
            "Contract work – płatność podatku"
        ]

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


@pytest.mark.parametrize(
    ("pay_day", "month", "shown"),
    [
        (11, date(2026, 3, 1), True),  # pays 2026-03-11: exactly 10 days after today (03-01)
        (12, date(2026, 3, 1), False),  # pays 2026-03-12: 11 days ahead, too early to ask
        (12, date(2026, 2, 1), True),  # pays 2026-02-12: past, still missing
        (1, date(2026, 3, 1), True),  # pays today
    ],
)
def test_missing_work_log_banner_window_is_ten_days_or_past(
    team: WorkspaceDb, pay_day: int, month: date, shown: bool
) -> None:
    # F3.5: the banner asks for a missing log once its pay date is within 10 days or past.
    ctx = team.ctx("owner", "owner")
    income_category, _expense = categories(team, ctx)
    source_id = _source(team, ctx, income_category, pay_day_value=pay_day)
    expected = [(source_id, "Contract work", _month_before(month))] if shown else []
    assert income.missing_work_logs(team.factory, ctx, month) == expected


def _month_before(month: date) -> date:
    return date(month.year - (month.month == 1), (month.month - 2) % 12 + 1, 1)


def test_missing_work_log_banner_skips_fixed_sources(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    income_category, _expense = categories(team, ctx)
    _source(team, ctx, income_category, kind="fixed", fixed_minor=500_000, rate_minor=None)
    assert income.missing_work_logs(team.factory, ctx, date(2026, 3, 1)) == []


def _attached(team: WorkspaceDb, ctx):
    """A source with a 12 % withheld rule attached (due day 20, one month later) and a log."""
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
    attachment_id = income.attach_tax_rule(
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
    with team.factory.begin() as session:
        other = CategoryRepository(session, ctx).create(
            name="Social security",
            name_key="social_security",
            kind="expense",
            treatment="regular",
            color="#2563eb",
            sort_order=2,
        )
        other_id = other.id
    return source_id, attachment_id, rule_id, other_id, income_category


def _tax_children(team: WorkspaceDb, ctx, source_id):
    parent = _occurrence(team, ctx, source_id, date(2026, 8, 1))
    assert parent is not None
    with team.factory() as session:
        return [
            (child.due_date, child.category_id, child.expected_minor)
            for child in ScheduledOccurrenceRepository(session, ctx).children(parent.id)
        ]


def test_updating_a_tax_attachment_resyncs_the_pending_payment(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    source_id, attachment_id, rule_id, other_id, _salary = _attached(team, ctx)
    assert [(due, amount) for due, _c, amount in _tax_children(team, ctx, source_id)] == [
        (date(2026, 10, 20), 12_000)
    ]
    # A changed rate shows that the amount is recalculated on the update, too.
    taxes.add_version(
        team.factory,
        ctx,
        rule_id,
        effective_from=date(2026, 9, 1),
        value=PercentOrFixed.percent(1900),
    )

    income.update_tax_attachment(
        team.factory,
        ctx,
        source_id,
        attachment_id,
        payment_category_id=other_id,
        due_day=5,
        due_month_offset=2,
        generate_payment=True,
    )

    assert _tax_children(team, ctx, source_id) == [(date(2026, 11, 5), other_id, 19_000)]
    listed = next(row for row in income.list_sources(team.factory, ctx) if row.id == source_id)
    attachment = listed.attachments[0]
    assert (attachment.due_day, attachment.due_month_offset) == (5, 2)
    with team.factory() as session:
        event = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "income_source_tax_rule.updated")
        ).one()
        assert event.data == {"attachment_id": str(attachment_id), "tax_rule_id": str(rule_id)}

    # Switching the payment off removes the pending payment; switching it on brings it back.
    for generate, expected in ((False, 0), (True, 1)):
        income.update_tax_attachment(
            team.factory,
            ctx,
            source_id,
            attachment_id,
            payment_category_id=other_id,
            due_day=5,
            due_month_offset=2,
            generate_payment=generate,
        )
        assert len(_tax_children(team, ctx, source_id)) == expected


@pytest.mark.parametrize(
    ("due_day", "offset", "code"),
    [(0, 1, "invalid_tax_attachment"), (32, 1, "invalid_tax_attachment"),
     (10, -1, "invalid_tax_attachment"), (10, 25, "invalid_tax_attachment")],
)  # fmt: skip
def test_updating_a_tax_attachment_validates_day_and_offset(
    team: WorkspaceDb, due_day: int, offset: int, code: str
) -> None:
    ctx = team.ctx("owner", "owner")
    source_id, attachment_id, _rule, other_id, _salary = _attached(team, ctx)
    with pytest.raises(income.IncomeError) as caught:
        income.update_tax_attachment(
            team.factory,
            ctx,
            source_id,
            attachment_id,
            payment_category_id=other_id,
            due_day=due_day,
            due_month_offset=offset,
            generate_payment=True,
        )
    assert caught.value.code == code


def test_updating_a_tax_attachment_rejects_bad_categories_and_sources(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    source_id, attachment_id, _rule, other_id, salary = _attached(team, ctx)
    other_source = _source(team, ctx, salary, name="Other")

    def update(source, category):
        income.update_tax_attachment(
            team.factory,
            ctx,
            source,
            attachment_id,
            payment_category_id=category,
            due_day=10,
            due_month_offset=1,
            generate_payment=True,
        )

    with pytest.raises(income.IncomeError) as caught:
        update(source_id, salary)  # an income category
    assert caught.value.code == "invalid_tax_category"
    with pytest.raises(income.IncomeError) as caught:
        update(other_source, other_id)  # the attachment belongs to another source
    assert caught.value.code == "invalid_tax_attachment"
    income.archive_source(team.factory, ctx, source_id)
    with pytest.raises(income.IncomeError) as caught:
        update(source_id, other_id)
    assert caught.value.code == "archived"


def test_updating_a_tax_attachment_needs_an_editor(team: WorkspaceDb) -> None:
    owner = team.ctx("owner", "owner")
    source_id, attachment_id, _rule, other_id, _salary = _attached(team, owner)

    def update(ctx):
        income.update_tax_attachment(
            team.factory,
            ctx,
            source_id,
            attachment_id,
            payment_category_id=other_id,
            due_day=5,
            due_month_offset=2,
            generate_payment=True,
        )

    with pytest.raises(PermissionDenied):
        update(team.ctx("viewer", "viewer"))
    team.add_member("dee", "viewer")  # demoted: the Context still says editor
    with pytest.raises(PermissionDenied):
        update(team.ctx("dee", "editor"))
    assert _tax_children(team, owner, source_id)[0][0] == date(2026, 10, 20)
