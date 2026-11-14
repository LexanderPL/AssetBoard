from __future__ import annotations

from datetime import date
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from assetboard.config import Settings
from assetboard.db.models.identity import AuditEvent
from assetboard.db.repositories import (
    CategoryRepository,
    ItemValuationRepository,
    LoanPaymentRepository,
    NetWorthCategoryRepository,
    NetWorthItemRepository,
    NotFound,
    ScheduledItemRepository,
    ScheduledOccurrenceRepository,
)
from assetboard.domain.loans import Money
from assetboard.domain.networth import Unavailable
from assetboard.domain.scheduled import AmountMode
from assetboard.services import (
    loan_links,
    loan_payments,
    loan_schedule,
    loan_scheduled,
    loans,
    occurrences,
)
from assetboard.services.networth_links import LinkTarget
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def team(tmp_path):
    for db in workspace_db(tmp_path):
        db.add_member("owner", "owner")
        db.add_member("editor", "editor")
        db.add_member("viewer", "viewer")
        yield db


def _money(minor: int, currency: str = "EUR") -> Money:
    return Money(minor, currency)


def _create(team: WorkspaceDb, **overrides) -> UUID:
    values = {
        "entry_path": "original",
        "name": "Mortgage",
        "kind": "annuity",
        "principal_original": _money(30_000_000),
        "current_balance": None,
        "balance_date": None,
        "origination_date": date(2026, 1, 1),
        "first_payment_date": date(2026, 2, 1),
        "term_months": 360,
        "payment_day": 1,
        "rate_mode": "fixed",
        "fixed_rate_bp": 550,
        "reference_rate_id": None,
        "margin_bp": None,
        "high_rate_confirmed": False,
        "installment_override": None,
        "overpayment_mode": "none",
        "overpayment_amount": None,
        "overpayment_strategy": "reduce_term",
    }
    values.update(overrides)
    return loans.create_loan(team.factory, team.ctx("owner", "owner"), **values)


def _category(team: WorkspaceDb, name: str, side: str) -> UUID:
    ctx = team.ctx("owner", "owner")
    with team.factory.begin() as session:
        repository = NetWorthCategoryRepository(session, ctx)
        row = repository.create(
            name=name,
            name_key=name.casefold(),
            side=side,
            color="#16a34a",
            sort_order=repository.next_sort_order(side),
        )
        return row.id


def _property_item(
    team: WorkspaceDb,
    *,
    category_id: UUID,
    name: str = "Flat",
    currency: str = "EUR",
    valued: bool = True,
    side: str = "asset",
) -> UUID:
    ctx = team.ctx("owner", "owner")
    with team.factory.begin() as session:
        item = NetWorthItemRepository(session, ctx).create(
            side=side,
            category_id=category_id,
            name=name,
            currency=currency,
            estimate_between=False,
        )
        if valued:
            ItemValuationRepository(session, ctx).create(
                item_id=item.id,
                as_of=date(2026, 1, 1),
                value_minor=40_000_000,
                currency=currency,
            )
        return item.id


def _expense_category(team: WorkspaceDb, name: str = "Loan") -> UUID:
    ctx = team.ctx("owner", "owner")
    with team.factory.begin() as session:
        row = CategoryRepository(session, ctx).create(
            name=name,
            name_key=name.casefold(),
            kind="expense",
            treatment="regular",
            color="#16a34a",
            sort_order=0,
        )
        return row.id


def test_loan_balance_from_origination_checkpoint_and_actuals(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    ctx = team.ctx("owner", "owner")
    assert loan_links.loan_balance_at(team.factory(), ctx, loan_id, date(2026, 1, 1)) == _money(
        30_000_000
    )

    with team.factory() as session:
        future = loan_links.loan_balance_at(session, ctx, loan_id, date(2027, 1, 1))
    assert not isinstance(future, Unavailable)
    assert future.amount_minor < 30_000_000

    checkpoint = loan_payments.create_loan_checkpoint(
        team.factory,
        ctx,
        loan_id,
        as_of=date(2026, 2, 1),
        remaining_principal=_money(29_000_000),
    )
    with team.factory() as session:
        assert loan_links.loan_balance_at(session, ctx, loan_id, date(2026, 2, 1)) == _money(
            29_000_000
        )

    data = loan_schedule.loan_schedule(team.factory, ctx, loan_id)
    assert data.schedule is not None
    row = next(item for item in data.schedule.rows if item.date > date(2026, 2, 1))
    with team.factory() as session:
        before = loan_links.loan_balance_at(session, ctx, loan_id, row.date)
    assert not isinstance(before, Unavailable)
    loan_payments.create_loan_payment(
        team.factory,
        ctx,
        loan_id,
        date_value=row.date,
        kind="overpayment",
        total=_money(50_000),
    )
    with team.factory() as session:
        after = loan_links.loan_balance_at(session, ctx, loan_id, row.date)
    assert not isinstance(after, Unavailable)
    assert after.amount_minor == before.amount_minor - 50_000
    assert checkpoint


def test_loan_balance_counts_only_transfers_through_the_requested_date(
    team: WorkspaceDb,
) -> None:
    loan_id = _create(team, drawdown_mode="tranches", term_months=12)
    ctx = team.ctx("owner", "owner")
    loans.add_transfer(
        team.factory,
        ctx,
        loan_id,
        date_value=date(2026, 1, 15),
        amount=_money(10_000_000),
    )
    loans.add_transfer(
        team.factory,
        ctx,
        loan_id,
        date_value=date(2026, 4, 1),
        amount=_money(5_000_000),
    )
    with team.factory() as session:
        before_first = loan_links.loan_balance_at(session, ctx, loan_id, date(2026, 1, 10))
        after_first = loan_links.loan_balance_at(session, ctx, loan_id, date(2026, 1, 20))
        before_second = loan_links.loan_balance_at(session, ctx, loan_id, date(2026, 3, 1))
        after_second = loan_links.loan_balance_at(session, ctx, loan_id, date(2026, 4, 2))
    assert before_first == _money(0)
    assert after_first == _money(10_000_000)
    assert not isinstance(before_second, Unavailable)
    assert before_second.amount_minor < 10_000_000
    assert not isinstance(after_second, Unavailable)
    assert after_second.amount_minor > before_second.amount_minor


def test_loan_balance_counts_a_transfer_between_payment_rows(team: WorkspaceDb) -> None:
    loan_id = _create(team, drawdown_mode="tranches", term_months=12)
    ctx = team.ctx("owner", "owner")
    loans.add_transfer(
        team.factory,
        ctx,
        loan_id,
        date_value=date(2026, 1, 15),
        amount=_money(10_000_000),
    )
    loans.add_transfer(
        team.factory,
        ctx,
        loan_id,
        date_value=date(2026, 4, 15),
        amount=_money(5_000_000),
    )
    with team.factory() as session:
        before = loan_links.loan_balance_at(session, ctx, loan_id, date(2026, 4, 10))
        after = loan_links.loan_balance_at(session, ctx, loan_id, date(2026, 4, 16))
    assert not isinstance(before, Unavailable)
    assert not isinstance(after, Unavailable)
    assert after.amount_minor == before.amount_minor + 5_000_000


def test_a_late_checkpoint_keeps_the_interest_only_loan_usable(team: WorkspaceDb) -> None:
    loan_id = _create(
        team,
        term_months=60,
        interest_only_months=24,
        first_payment_date=date(2026, 2, 1),
    )
    ctx = team.ctx("owner", "owner")
    with team.factory.begin() as session:
        loan_payments.LoanBalanceCheckpointRepository(session, ctx).create(
            loan_id=loan_id,
            as_of=date(2029, 5, 1),
            remaining_principal_minor=20_000_000,
            note="",
        )
    with team.factory() as session:
        balance = loan_links.loan_balance_at(session, ctx, loan_id, date(2029, 6, 1))
    assert not isinstance(balance, Unavailable)
    assert balance.amount_minor < 20_000_000


def test_loan_balance_includes_an_installment_paid_before_its_due_date(
    team: WorkspaceDb,
) -> None:
    loan_id = _create(team)
    ctx = team.ctx("owner", "owner")
    _, first = next((view, row) for view, row in [(None, None)] if False) if False else (None, None)
    data = loan_schedule.loan_schedule(team.factory, ctx, loan_id)
    assert data.schedule is not None
    first = data.schedule.rows[0]
    payment_id = loan_payments.create_loan_payment(
        team.factory,
        ctx,
        loan_id,
        date_value=date(2026, 1, 15),
        kind="installment",
        total=first.installment_total,
    )
    with team.factory() as session:
        payment = LoanPaymentRepository(session, ctx).get(payment_id)
    assert payment is not None
    with team.factory() as session:
        balance = loan_links.loan_balance_at(session, ctx, loan_id, date(2026, 1, 20))
    assert not isinstance(balance, Unavailable)
    assert balance.amount_minor == 30_000_000 - (
        payment.principal_minor + payment.overpayment_minor
    )


def test_loan_balance_overpayment_only_and_unknown(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    ctx = team.ctx("owner", "owner")
    loan_payments.create_loan_payment(
        team.factory,
        ctx,
        loan_id,
        date_value=date(2026, 1, 15),
        kind="overpayment",
        total=_money(100_000),
    )
    with team.factory() as session:
        balance = loan_links.loan_balance_at(session, ctx, loan_id, date(2026, 1, 20))
        unknown = loan_links.loan_balance_at(session, ctx, uuid4(), date(2026, 1, 20))
    assert balance == _money(29_900_000)
    assert isinstance(unknown, Unavailable)


def test_loan_resolver_choices_describe_and_archived(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    ctx = team.ctx("owner", "owner")
    loans.archive_loan(team.factory, ctx, loan_id)
    chooser = team.ctx("viewer", "viewer")
    with team.factory() as session:
        resolver = loan_links._LoanBalanceResolver(session, chooser)
        assert resolver.choices() == ()
        target = resolver.describe(loan_id)
        assert isinstance(target, LinkTarget) and target.label == "Mortgage"
        unknown_id = uuid4()
        values = resolver.resolve_many([loan_id, unknown_id], [date(2026, 7, 1)])
        assert not isinstance(values[(loan_id, date(2026, 7, 1))], Unavailable)
        assert isinstance(values[(unknown_id, date(2026, 7, 1))], Unavailable)


def test_linked_liability_item_is_created_once_and_audited(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    ctx = team.ctx("owner", "owner")
    first = loan_links.create_loan_liability_item(team.factory, ctx, loan_id)
    second = loan_links.create_loan_liability_item(team.factory, ctx, loan_id)
    assert first.id == second.id
    assert (
        loan_links.loan_liability_view(team.factory, team.ctx("viewer", "viewer"), loan_id) == first
    )
    with team.factory() as session:
        actions = list(session.scalars(select(AuditEvent.action)))
        data = list(session.scalars(select(AuditEvent.data)))
    assert "networth_category.created" in actions
    assert "networth_item.created" in actions
    assert all("amount" not in str(item) for item in data)
    with team.factory() as session:
        categories = NetWorthCategoryRepository(session, ctx).ordered("liability")
    assert any(row.name == "Mortgage" for row in categories)


def test_property_link_and_ltv_view(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    ctx = team.ctx("owner", "owner")
    category_id = _category(team, "Real estate", "asset")
    property_id = _property_item(team, category_id=category_id)
    loan_links.set_loan_property(team.factory, ctx, loan_id, property_id)
    view = loan_links.loan_property_view(
        team.factory, team.ctx("viewer", "viewer"), loan_id, date(2026, 1, 1)
    )
    assert view.property_name == "Flat"
    assert view.value == _money(40_000_000)
    assert view.remaining == _money(30_000_000)
    assert view.ltv_bp == 7_500
    assert view.equity == _money(10_000_000)
    assert view.ltv_reason is None and view.equity_reason is None

    loan_links.clear_loan_property(team.factory, ctx, loan_id)
    cleared = loan_links.loan_property_view(team.factory, ctx, loan_id, date(2026, 1, 1))
    assert cleared.ltv_reason == "no_property"


def test_property_validation_and_unavailable_reasons(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    ctx = team.ctx("owner", "owner")
    asset_category = _category(team, "Real estate", "asset")
    other_category = _category(team, "Other assets", "asset")
    liability_category = _category(team, "Mortgages", "liability")
    other_id = _property_item(team, category_id=other_category)
    liability_id = _property_item(
        team, category_id=liability_category, side="liability", name="Liability"
    )
    for item_id, code in (
        (other_id, "property_not_real_estate"),
        (liability_id, "property_not_asset"),
    ):
        with pytest.raises(loans.LoanError) as raised:
            loan_links.set_loan_property(team.factory, ctx, loan_id, item_id)
        assert raised.value.code == code

    empty_id = _property_item(team, category_id=asset_category, valued=False)
    with pytest.raises(loans.LoanError) as raised:
        loan_links.set_loan_property(
            team.factory,
            ctx,
            loan_id,
            _property_item(team, category_id=asset_category, currency="USD"),
        )
    assert raised.value.code == "property_currency"

    loan_links.set_loan_property(team.factory, ctx, loan_id, empty_id)
    empty = loan_links.loan_property_view(team.factory, ctx, loan_id, date(2026, 1, 1))
    assert empty.ltv_reason == "no_value" and empty.equity_reason == "no_value"

    zero_id = _property_item(team, category_id=asset_category, valued=False)
    with team.factory.begin() as session:
        ItemValuationRepository(session, ctx).create(
            item_id=zero_id, as_of=date(2026, 1, 1), value_minor=0, currency="EUR"
        )
    loan_links.set_loan_property(team.factory, ctx, loan_id, zero_id)
    zero = loan_links.loan_property_view(team.factory, ctx, loan_id, date(2026, 1, 1))
    assert zero.ltv_reason == "value_zero"
    assert zero.equity is not None and zero.equity.amount_minor < 0

    with team.factory.begin() as session:
        NetWorthItemRepository(session, ctx).update(zero_id, archived_at=ctx.clock.now())
    archived = loan_links.loan_property_view(team.factory, ctx, loan_id, date(2026, 1, 1))
    assert archived.ltv_reason == "property_archived"


def test_property_choices_filter_to_real_estate_assets(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    real = _category(team, "Real estate", "asset")
    other = _category(team, "Other assets", "asset")
    yes = _property_item(team, category_id=real)
    _property_item(team, category_id=other)
    _property_item(team, category_id=real, currency="USD")
    choices = loan_links.property_choices(team.factory, team.ctx("viewer", "viewer"), loan_id)
    assert [choice.id for choice in choices] == [yes]


def test_liability_helper_is_tenant_scoped(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    other_workspace = team.add_workspace("Other")
    team.add_member("other", "owner", other_workspace)
    with pytest.raises(NotFound):
        loan_links.create_loan_liability_item(
            team.factory, team.ctx("other", "owner", other_workspace), loan_id
        )


def _settings(tmp_path) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path, secret_key="test")


def _loan_item_occurrences(team: WorkspaceDb, item_id: UUID):
    ctx = team.ctx("owner", "owner")
    with team.factory() as session:
        return ScheduledOccurrenceRepository(session, ctx).for_item(item_id)


def test_scheduled_generation_uses_loan_amounts_and_refreshes(team: WorkspaceDb, tmp_path) -> None:
    loan_id = _create(team)
    ctx = team.ctx("owner", "owner")
    category_id = _expense_category(team)
    settings = _settings(tmp_path)
    item_id = loan_scheduled.create_loan_schedule_item(
        team.factory, ctx, loan_id, category_id, settings
    )
    with team.factory() as session:
        item = ScheduledItemRepository(session, ctx).get(item_id)
    assert item is not None
    assert item.amount_mode == AmountMode.FROM_LOAN.value
    assert item.amount_minor == 0 and item.loan_id == loan_id

    data = loan_schedule.loan_schedule(team.factory, ctx, loan_id)
    assert data.schedule is not None
    expected = {row.date: row.installment_total.amount_minor for row in data.schedule.rows}
    rows = _loan_item_occurrences(team, item_id)
    assert rows
    assert all(row.expected_minor == expected.get(row.due_date, 0) for row in rows)

    later = next(row for row in rows if row.due_date > date(2026, 4, 1))
    old_amount = later.expected_minor
    loans.add_rate_change(
        team.factory,
        ctx,
        loan_id,
        effective_from=date(2026, 4, 1),
        rate_bp=600,
        high_rate_confirmed=False,
    )
    refreshed = {row.id: row for row in _loan_item_occurrences(team, item_id)}
    assert refreshed[later.id].expected_minor != old_amount or later.due_date < date(2026, 4, 1)


def test_scheduled_refresh_keeps_posted_skipped_and_overridden(team: WorkspaceDb, tmp_path) -> None:
    loan_id = _create(team)
    ctx = team.ctx("owner", "owner")
    item_id = loan_scheduled.create_loan_schedule_item(
        team.factory, ctx, loan_id, _expense_category(team), _settings(tmp_path)
    )
    rows = _loan_item_occurrences(team, item_id)
    posted, skipped, overridden = rows[0], rows[1], rows[2]
    with team.factory.begin() as session:
        repository = ScheduledOccurrenceRepository(session, ctx)
        repository.update(posted.id, status="posted")
        repository.update(skipped.id, status="skipped")
        repository.update(overridden.id, amount_overridden=True, expected_minor=1)
    loans.add_rate_change(
        team.factory,
        ctx,
        loan_id,
        effective_from=date(2026, 4, 1),
        rate_bp=600,
        high_rate_confirmed=False,
    )
    refreshed = {row.id: row for row in _loan_item_occurrences(team, item_id)}
    assert refreshed[posted.id].status == "posted"
    assert refreshed[skipped.id].status == "skipped"
    assert refreshed[overridden.id].expected_minor == 1
    assert refreshed[overridden.id].amount_overridden


def test_scheduled_disable_removes_only_pending_future(team: WorkspaceDb, tmp_path) -> None:
    loan_id = _create(team)
    ctx = team.ctx("owner", "owner")
    item_id = loan_scheduled.create_loan_schedule_item(
        team.factory, ctx, loan_id, _expense_category(team), _settings(tmp_path)
    )
    rows = _loan_item_occurrences(team, item_id)
    posted, pending = rows[0], rows[1]
    with team.factory.begin() as session:
        repository = ScheduledOccurrenceRepository(session, ctx)
        repository.update(posted.id, status="posted")
        repository.update(pending.id, due_date=date(2026, 1, 15), nominal_date=date(2026, 1, 15))
    loan_scheduled.disable_loan_schedule_item(team.factory, ctx, loan_id)
    remaining = {row.id: row for row in _loan_item_occurrences(team, item_id)}
    assert posted.id in remaining
    assert pending.id in remaining
    with team.factory() as session:
        item = ScheduledItemRepository(session, ctx).get(item_id)
    assert item is not None and item.archived_at is not None


def test_auto_record_posting_is_idempotent(team: WorkspaceDb, tmp_path) -> None:
    loan_id = _create(team)
    ctx = team.ctx("owner", "owner")
    item_id = loan_scheduled.create_loan_schedule_item(
        team.factory, ctx, loan_id, _expense_category(team), _settings(tmp_path)
    )
    occurrence = _loan_item_occurrences(team, item_id)[0]
    loan_links.set_auto_record_payments(team.factory, ctx, loan_id, True)
    occurrence_service = team.ctx("editor", "editor")
    transaction_id = occurrences.post_occurrence(
        team.factory,
        occurrence_service,
        occurrence.id,
        amount_minor=occurrence.expected_minor,
        day=ctx.clock.today("UTC"),
    )
    occurrences.post_occurrence(
        team.factory,
        occurrence_service,
        occurrence.id,
        amount_minor=occurrence.expected_minor,
        day=ctx.clock.today("UTC"),
    )
    with team.factory() as session:
        payments = LoanPaymentRepository(session, ctx).for_loan(loan_id)
    assert len(payments) == 1
    assert payments[0].source == "upcoming"
    assert payments[0].transaction_id == transaction_id
    assert payments[0].date == occurrence.due_date
    with team.factory() as session:
        assert len(LoanPaymentRepository(session, ctx).for_loan(loan_id)) == 1


def test_auto_record_off_creates_no_payment(team: WorkspaceDb, tmp_path) -> None:
    loan_id = _create(team)
    ctx = team.ctx("owner", "owner")
    item_id = loan_scheduled.create_loan_schedule_item(
        team.factory, ctx, loan_id, _expense_category(team), _settings(tmp_path)
    )
    occurrence = _loan_item_occurrences(team, item_id)[0]
    occurrences.post_occurrence(
        team.factory,
        ctx,
        occurrence.id,
        amount_minor=occurrence.expected_minor,
        day=ctx.clock.today("UTC"),
    )
    with team.factory() as session:
        assert LoanPaymentRepository(session, ctx).for_loan(loan_id) == []
