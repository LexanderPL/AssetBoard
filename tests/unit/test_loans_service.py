from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest
from sqlalchemy import select

from assetboard.db.models.loans import Loan
from assetboard.db.repositories import (
    LoanBalanceCheckpointRepository,
    LoanRateChangeRepository,
    ReferenceRateValueRepository,
)
from assetboard.domain.loans import DayCount, LoanKind, LoanValidationError
from assetboard.domain.money import Money
from assetboard.services import loan_schedule, loans
from assetboard.services.context import PermissionDenied
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def team(tmp_path):
    for db in workspace_db(tmp_path):
        db.add_member("owner", "owner")
        db.add_member("editor", "editor")
        db.add_member("viewer", "viewer")
        yield db


def _money(minor: int) -> Money:
    return Money(minor, "EUR")


def _original_loan_kwargs(**overrides):
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
    return values


def _original_loan(team: WorkspaceDb, ctx=None, **overrides):
    return loans.create_loan(
        team.factory,
        ctx or team.ctx("owner", "owner"),
        **_original_loan_kwargs(**overrides),
    )


def _current_loan(team: WorkspaceDb, **overrides):
    values = {
        "entry_path": "current",
        "name": "Car loan",
        "kind": "equal_principal",
        "principal_original": _money(20_000_000),
        "current_balance": _money(15_000_000),
        "balance_date": date(2026, 2, 1),
        "origination_date": None,
        "first_payment_date": date(2026, 3, 1),
        "term_months": 60,
        "payment_day": 1,
        "rate_mode": "fixed",
        "fixed_rate_bp": 500,
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


def _reference_rate(team: WorkspaceDb, name: str = "EURIBOR 6M") -> UUID:
    rate_id = loans.create_reference_rate(
        team.factory, team.ctx("owner", "owner"), name=name, notes=""
    )
    loans.add_reference_rate_value(
        team.factory,
        team.ctx("owner", "owner"),
        rate_id,
        effective_from=date(2026, 1, 1),
        value_bp=400,
        high_rate_confirmed=False,
    )
    return rate_id


def test_wizard_preview_uses_the_current_balance_and_respects_an_override(
    team: WorkspaceDb,
) -> None:
    original = loans.preview_first_installment(
        entry_path="original",
        principal_original=_money(30_000_000),
        current_balance=None,
        term_months=360,
        kind=LoanKind.ANNUITY,
        rate_bp=550,
        installment_override=None,
    )
    assert original == _money(170_337)
    current = loans.preview_first_installment(
        entry_path="current",
        principal_original=_money(30_000_000),
        current_balance=_money(20_000_000),
        term_months=240,
        kind=LoanKind.ANNUITY,
        rate_bp=550,
        installment_override=None,
    )
    assert current == _money(137_577)
    override = loans.preview_first_installment(
        entry_path="current",
        principal_original=None,
        current_balance=_money(20_000_000),
        term_months=240,
        kind=LoanKind.ANNUITY,
        rate_bp=550,
        installment_override=_money(120_000),
    )
    assert override == _money(120_000)
    with pytest.raises(loans.LoanError) as missing:
        loans.preview_first_installment(
            entry_path="current",
            principal_original=None,
            current_balance=None,
            term_months=240,
            kind=LoanKind.ANNUITY,
            rate_bp=550,
            installment_override=None,
        )
    assert missing.value.code == "invalid_balance"


def test_original_path_creates_a_loan_and_an_initial_fixed_rate(team: WorkspaceDb) -> None:
    loan_id = _original_loan(team)
    view = loans.get_loan(team.factory, team.ctx("viewer", "viewer"), loan_id)
    assert view.entry_path == "original"
    assert view.principal_original == _money(30_000_000)
    assert view.current_balance is None
    assert view.rate_mode.value == "fixed"
    assert view.current_rate_bp == 550
    assert view.first_installment is not None
    with team.factory() as session:
        loan = session.get(Loan, loan_id)
        assert loan is not None and loan.currency == "EUR"
        changes = LoanRateChangeRepository(session, team.ctx("owner", "owner")).for_loan(loan_id)
        assert [(row.effective_from, row.rate_bp) for row in changes] == [(date(2026, 1, 1), 550)]
        assert (
            LoanBalanceCheckpointRepository(session, team.ctx("owner", "owner")).for_loan(loan_id)
            == []
        )


def test_current_balance_path_creates_one_checkpoint(team: WorkspaceDb) -> None:
    loan_id = _current_loan(team)
    view = loans.get_loan(team.factory, team.ctx("viewer", "viewer"), loan_id)
    assert view.entry_path == "current"
    assert view.current_balance == _money(15_000_000)
    assert view.balance_date == date(2026, 2, 1)
    assert view.origination_date is None
    assert view.term_months == 60
    with team.factory() as session:
        checkpoints = LoanBalanceCheckpointRepository(session, team.ctx("owner", "owner")).for_loan(
            loan_id
        )
        assert len(checkpoints) == 1
        assert checkpoints[0].as_of == date(2026, 2, 1)
        assert checkpoints[0].remaining_principal_minor == 15_000_000
        changes = LoanRateChangeRepository(session, team.ctx("owner", "owner")).for_loan(loan_id)
        assert [row.effective_from for row in changes] == [date(2026, 2, 1)]


def test_variable_loan_uses_reference_rate_and_margin(team: WorkspaceDb) -> None:
    rate_id = _reference_rate(team)
    loan_id = _original_loan(
        team,
        name="Variable mortgage",
        rate_mode="variable",
        fixed_rate_bp=None,
        reference_rate_id=rate_id,
        margin_bp=150,
    )
    view = loans.get_loan(team.factory, team.ctx("viewer", "viewer"), loan_id)
    assert view.rate_mode.value == "variable"
    assert view.reference_rate_id == rate_id
    assert view.reference_rate_name == "EURIBOR 6M"
    assert view.margin_bp == 150
    assert view.current_rate_bp == 550
    assert view.rate_changes == ()


def test_target_total_must_not_be_below_the_first_installment(team: WorkspaceDb) -> None:
    with pytest.raises(loans.LoanError) as error:
        _original_loan(
            team,
            overpayment_mode="target_total",
            overpayment_amount=_money(100_000),
        )
    assert error.value.code == "target_below_installment"
    loan_id = _original_loan(
        team,
        overpayment_mode="target_total",
        overpayment_amount=_money(200_000),
    )
    view = loans.get_loan(team.factory, team.ctx("owner", "owner"), loan_id)
    assert view.overpayment_mode.value == "target_total"
    assert view.overpayment_amount == _money(200_000)


def test_a_target_below_an_override_is_a_validation_error_not_a_database_error(
    team: WorkspaceDb,
) -> None:
    with pytest.raises(loans.LoanError) as created:
        _original_loan(
            team,
            installment_override=_money(200_000),
            overpayment_mode="target_total",
            overpayment_amount=_money(100_000),
        )
    assert created.value.code == "target_below_installment"
    with team.factory() as session:
        assert session.scalars(select(Loan)).all() == []


def test_editing_terms_cannot_leave_a_target_below_the_new_override(
    team: WorkspaceDb,
) -> None:
    loan_id = _original_loan(
        team,
        principal_original=_money(90_000),
        overpayment_mode="target_total",
        overpayment_amount=_money(90_000),
    )
    ctx = team.ctx("owner", "owner")
    with pytest.raises(loans.LoanError) as error:
        loans.update_loan_terms(
            team.factory,
            ctx,
            loan_id,
            name="Mortgage",
            kind="annuity",
            principal_original=_money(90_000_000),
            current_balance=None,
            balance_date=None,
            origination_date=date(2026, 1, 1),
            first_payment_date=date(2026, 2, 1),
            term_months=360,
            payment_day=1,
            installment_override=_money(726_030),
        )
    assert error.value.code == "target_below_installment"
    view = loans.get_loan(team.factory, ctx, loan_id)
    assert view.principal_original == _money(90_000)
    assert view.installment_override is None
    assert view.overpayment_amount == _money(90_000)


def test_a_reference_rate_change_cannot_push_a_target_below_the_installment(
    team: WorkspaceDb,
) -> None:
    rate_id = _reference_rate(team)
    _original_loan(
        team,
        name="Variable mortgage",
        rate_mode="variable",
        fixed_rate_bp=None,
        reference_rate_id=rate_id,
        margin_bp=100,
        overpayment_mode="target_total",
        overpayment_amount=_money(200_000),
    )
    with pytest.raises(loans.LoanError) as error:
        loans.add_reference_rate_value(
            team.factory,
            team.ctx("owner", "owner"),
            rate_id,
            effective_from=date(2026, 1, 15),
            value_bp=3_000,
            high_rate_confirmed=True,
        )
    assert error.value.code == "target_below_installment"
    with team.factory() as session:
        values = ReferenceRateValueRepository(session, team.ctx("owner", "owner")).for_rate(rate_id)
        assert [value.effective_from for value in values] == [date(2026, 1, 1)]


def test_validation_rules(team: WorkspaceDb) -> None:
    with pytest.raises(LoanValidationError):
        _original_loan(team, term_months=0)
    with pytest.raises(loans.LoanError) as mismatch:
        _original_loan(team, first_payment_date=date(2026, 2, 2))
    assert mismatch.value.code == "invalid_first_payment"
    with pytest.raises(loans.LoanError) as high:
        _original_loan(team, fixed_rate_bp=3_000)
    assert high.value.code == "high_rate_confirmation_required"
    assert _original_loan(team, fixed_rate_bp=3_000, high_rate_confirmed=True)
    with pytest.raises(LoanValidationError):
        _original_loan(team, fixed_rate_bp=10_001)
    rate_id = _reference_rate(team)
    with pytest.raises(LoanValidationError):
        _original_loan(
            team,
            margin_bp=-1,
            rate_mode="variable",
            fixed_rate_bp=None,
            reference_rate_id=rate_id,
        )
    with pytest.raises(loans.LoanError) as override:
        _original_loan(team, kind="equal_principal", installment_override=_money(100_000))
    assert override.value.code == "override_annuity_only"


def test_rate_changes_are_scoped_and_keep_a_rate_by_the_first_payment(team: WorkspaceDb) -> None:
    loan_id = _original_loan(team)
    ctx = team.ctx("owner", "owner")
    loans.add_rate_change(
        team.factory,
        ctx,
        loan_id,
        effective_from=date(2027, 1, 1),
        rate_bp=600,
        high_rate_confirmed=False,
    )
    with pytest.raises(loans.LoanError) as duplicate:
        loans.add_rate_change(
            team.factory,
            ctx,
            loan_id,
            effective_from=date(2027, 1, 1),
            rate_bp=650,
            high_rate_confirmed=False,
        )
    assert duplicate.value.code == "rate_change_exists"
    with pytest.raises(loans.LoanError) as before:
        loans.add_rate_change(
            team.factory,
            ctx,
            loan_id,
            effective_from=date(2025, 12, 1),
            rate_bp=650,
            high_rate_confirmed=False,
        )
    assert before.value.code == "rate_change_before_start"
    view = loans.get_loan(team.factory, ctx, loan_id)
    initial = view.rate_changes[0]
    with pytest.raises(loans.LoanError) as missing:
        loans.delete_rate_change(team.factory, ctx, loan_id, initial.id)
    assert missing.value.code == "rate_change_missing"


def test_create_loan_accepts_validated_cost_components(team: WorkspaceDb) -> None:
    loan_id = _original_loan(
        team,
        costs=(
            loans.CostComponentInput(
                name="Property insurance",
                mode="fixed",
                rate_bp=None,
                fixed=_money(7_500),
                effective_from=None,
                effective_to=None,
            ),
            loans.CostComponentInput(
                name="Life insurance",
                mode="percent",
                rate_bp=25,
                fixed=None,
                effective_from=date(2026, 2, 1),
                effective_to=date(2026, 12, 31),
            ),
        ),
    )
    view = loans.get_loan(team.factory, team.ctx("owner", "owner"), loan_id)
    assert [row.name for row in view.cost_components] == [
        "Property insurance",
        "Life insurance",
    ]
    fixed, percent = view.cost_components
    assert fixed.effective_from == date(2026, 1, 1)
    assert fixed.value.fixed == _money(7_500)
    assert percent.effective_from == date(2026, 2, 1)
    assert percent.value.rate_bp == 25


def test_costs_and_one_off_overpayments_crud(team: WorkspaceDb) -> None:
    loan_id = _original_loan(team)
    ctx = team.ctx("owner", "owner")
    loans.add_cost_component(
        team.factory,
        ctx,
        loan_id,
        name="Life insurance",
        mode="percent",
        rate_bp=10,
        fixed=None,
        effective_from=date(2026, 1, 1),
        effective_to=None,
    )
    loans.add_cost_component(
        team.factory,
        ctx,
        loan_id,
        name="Bank fee",
        mode="fixed",
        rate_bp=None,
        fixed=_money(2_500),
        effective_from=date(2026, 1, 1),
        effective_to=date(2026, 12, 31),
    )
    view = loans.get_loan(team.factory, ctx, loan_id)
    assert len(view.cost_components) == 2
    assert {row.name for row in view.cost_components} == {"Life insurance", "Bank fee"}
    component = view.cost_components[0]
    loans.update_cost_component(
        team.factory,
        ctx,
        loan_id,
        component.id,
        name="Updated fee",
        mode="fixed",
        rate_bp=None,
        fixed=_money(3_000),
        effective_from=date(2026, 1, 1),
        effective_to=None,
    )
    loans.add_overpayment(
        team.factory,
        ctx,
        loan_id,
        date_value=date(2026, 6, 1),
        amount=_money(100_000),
        note="Bonus",
    )
    view = loans.get_loan(team.factory, ctx, loan_id)
    assert view.overpayments[0].amount == _money(100_000)
    loans.update_overpayment(
        team.factory,
        ctx,
        loan_id,
        view.overpayments[0].id,
        date_value=date(2026, 7, 1),
        amount=_money(120_000),
        note="Bonus",
    )
    loans.delete_overpayment(team.factory, ctx, loan_id, view.overpayments[0].id)
    assert loans.get_loan(team.factory, ctx, loan_id).overpayments == ()


def test_reference_rates_are_workspace_scoped_and_future_values_are_editable(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("owner", "owner")
    rate_id = _reference_rate(team)
    with pytest.raises(loans.LoanError) as duplicate_name:
        loans.create_reference_rate(team.factory, ctx, name="EURIBOR 6M")
    assert duplicate_name.value.code == "reference_rate_exists"
    with pytest.raises(loans.LoanError) as duplicate_date:
        loans.add_reference_rate_value(
            team.factory,
            ctx,
            rate_id,
            effective_from=date(2026, 1, 1),
            value_bp=450,
            high_rate_confirmed=False,
        )
    assert duplicate_date.value.code == "value_exists"
    loans.add_reference_rate_value(
        team.factory,
        ctx,
        rate_id,
        effective_from=date(2026, 4, 1),
        value_bp=450,
        high_rate_confirmed=False,
    )
    rate = loans.get_reference_rate(team.factory, ctx, rate_id)
    future = rate.values[-1]
    loans.update_reference_rate_value(
        team.factory,
        ctx,
        rate_id,
        future.id,
        effective_from=date(2026, 5, 1),
        value_bp=475,
        high_rate_confirmed=False,
    )
    rate = loans.get_reference_rate(team.factory, ctx, rate_id)
    assert rate.values[-1].effective_from == date(2026, 5, 1)
    past = rate.values[0]
    with pytest.raises(loans.LoanError) as past_value:
        loans.delete_reference_rate_value(team.factory, ctx, rate_id, past.id)
    assert past_value.value.code == "value_not_future"
    loans.add_reference_rate_value(
        team.factory,
        ctx,
        rate_id,
        effective_from=date(2026, 6, 1),
        value_bp=425,
        high_rate_confirmed=False,
    )
    with team.factory() as session:
        values = ReferenceRateValueRepository(session, ctx).for_rate(rate_id)
        assert [row.effective_from for row in values] == [
            date(2026, 1, 1),
            date(2026, 5, 1),
            date(2026, 6, 1),
        ]


def test_reference_rate_view_lists_used_by_loans_and_a_sparkline(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    rate_id = _reference_rate(team)
    first = _original_loan(
        team,
        name="Variable mortgage",
        rate_mode="variable",
        fixed_rate_bp=None,
        reference_rate_id=rate_id,
        margin_bp=100,
    )
    second = _original_loan(
        team,
        name="Variable car loan",
        rate_mode="variable",
        fixed_rate_bp=None,
        reference_rate_id=rate_id,
        margin_bp=150,
    )
    loans.archive_loan(team.factory, ctx, second)
    loans.add_reference_rate_value(
        team.factory,
        ctx,
        rate_id,
        effective_from=date(2026, 4, 1),
        value_bp=450,
        high_rate_confirmed=False,
    )
    loans.add_reference_rate_value(
        team.factory,
        ctx,
        rate_id,
        effective_from=date(2026, 7, 1),
        value_bp=425,
        high_rate_confirmed=False,
    )
    page = loans.list_reference_rates(team.factory, team.ctx("viewer", "viewer"))
    rate = next(row for row in page.rates if row.id == rate_id)
    assert [row.name for row in rate.used_by] == ["Variable car loan", "Variable mortgage"]
    assert {row.id for row in rate.used_by} == {first, second}
    assert [row.archived for row in rate.used_by] == [True, False]
    assert rate.sparkline[0][0] == 0 and rate.sparkline[-1][0] == 100
    assert all(0 <= y <= 24 for _, y in rate.sparkline)
    assert len(rate.sparkline) == 3
    fetched = loans.get_reference_rate(team.factory, team.ctx("viewer", "viewer"), rate_id)
    assert fetched.used_by == rate.used_by
    assert fetched.sparkline == rate.sparkline
    other = loans.create_reference_rate(team.factory, ctx, name="WIRON")
    empty = loans.get_reference_rate(team.factory, ctx, other)
    assert empty.used_by == ()
    assert empty.sparkline == ()


def test_reference_rate_delete_is_refused_while_used(team: WorkspaceDb) -> None:
    rate_id = _reference_rate(team)
    _original_loan(
        team,
        rate_mode="variable",
        fixed_rate_bp=None,
        reference_rate_id=rate_id,
        margin_bp=100,
    )
    with pytest.raises(loans.LoanError) as in_use:
        loans.delete_reference_rate(team.factory, team.ctx("owner", "owner"), rate_id)
    assert in_use.value.code == "reference_rate_in_use"
    other = loans.create_reference_rate(team.factory, team.ctx("owner", "owner"), name="WIRON")
    loans.delete_reference_rate(team.factory, team.ctx("owner", "owner"), other)


def test_archive_and_restore_are_read_only_while_archived(team: WorkspaceDb) -> None:
    loan_id = _original_loan(team)
    ctx = team.ctx("owner", "owner")
    loans.archive_loan(team.factory, ctx, loan_id)
    assert loans.get_loan(team.factory, ctx, loan_id).archived
    with pytest.raises(loans.LoanError) as archived:
        loans.update_loan_terms(
            team.factory,
            ctx,
            loan_id,
            name="Renamed",
            kind="annuity",
            principal_original=_money(30_000_000),
            current_balance=None,
            balance_date=None,
            origination_date=date(2026, 1, 1),
            first_payment_date=date(2026, 2, 1),
            term_months=360,
            payment_day=1,
            installment_override=None,
        )
    assert archived.value.code == "archived"
    loans.restore_loan(team.factory, ctx, loan_id)
    assert not loans.get_loan(team.factory, ctx, loan_id).archived


def test_viewer_cannot_write_and_stale_role_is_refused(team: WorkspaceDb) -> None:
    owner_ctx = team.ctx("owner", "owner")
    loan_id = _original_loan(team)
    viewer = team.ctx("viewer", "viewer")
    assert loans.list_loans(team.factory, viewer).active
    assert loans.get_loan(team.factory, viewer, loan_id).name == "Mortgage"
    with pytest.raises(PermissionDenied):
        _original_loan(team, viewer, name="Viewer loan")
    team.add_member("stale", "viewer")
    stale = team.ctx("stale", "editor")
    with pytest.raises(PermissionDenied):
        loans.create_loan(team.factory, stale, **_original_loan_kwargs())
    loans.archive_loan(team.factory, owner_ctx, loan_id)


def test_update_terms_changes_kind_and_the_current_balance(team: WorkspaceDb) -> None:
    loan_id = _current_loan(team)
    ctx = team.ctx("owner", "owner")
    loans.update_loan_terms(
        team.factory,
        ctx,
        loan_id,
        name="Updated car loan",
        kind="annuity",
        principal_original=_money(20_000_000),
        current_balance=_money(14_000_000),
        balance_date=date(2026, 2, 1),
        origination_date=None,
        first_payment_date=date(2026, 3, 1),
        term_months=59,
        payment_day=1,
        installment_override=_money(260_000),
    )
    view = loans.get_loan(team.factory, ctx, loan_id)
    assert view.name == "Updated car loan"
    assert view.kind.value == "annuity"
    assert view.current_balance == _money(14_000_000)
    assert view.term_months == 59
    assert view.installment_override == _money(260_000)
    assert view.first_installment == _money(260_000)


def test_overpayment_settings_validate_the_target(team: WorkspaceDb) -> None:
    loan_id = _original_loan(team)
    ctx = team.ctx("owner", "owner")
    with pytest.raises(loans.LoanError) as below:
        loans.set_overpayment_settings(
            team.factory,
            ctx,
            loan_id,
            mode="target_total",
            amount=_money(100_000),
            strategy="reduce_term",
        )
    assert below.value.code == "target_below_installment"
    loans.set_overpayment_settings(
        team.factory,
        ctx,
        loan_id,
        mode="target_total",
        amount=_money(200_000),
        strategy="reduce_installment",
    )
    view = loans.get_loan(team.factory, ctx, loan_id)
    assert view.overpayment_mode.value == "target_total"
    assert view.overpayment_strategy.value == "reduce_installment"
    loans.set_overpayment_settings(
        team.factory, ctx, loan_id, mode="none", amount=None, strategy="reduce_term"
    )
    assert loans.get_loan(team.factory, ctx, loan_id).overpayment_amount is None


def test_reference_rate_values_also_require_high_rate_confirmation(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    rate_id = _reference_rate(team)
    with pytest.raises(loans.LoanError) as high:
        loans.add_reference_rate_value(
            team.factory,
            ctx,
            rate_id,
            effective_from=date(2026, 4, 1),
            value_bp=3_000,
            high_rate_confirmed=False,
        )
    assert high.value.code == "high_rate_confirmation_required"
    loans.add_reference_rate_value(
        team.factory,
        ctx,
        rate_id,
        effective_from=date(2026, 4, 1),
        value_bp=3_000,
        high_rate_confirmed=True,
    )


def test_a_reference_rate_name_whose_key_is_too_long_is_refused(team: WorkspaceDb) -> None:
    with pytest.raises(LoanValidationError) as invalid:
        loans.create_reference_rate(team.factory, team.ctx("owner", "owner"), name="ß" * 200)
    assert invalid.value.code == "invalid_name"


def test_update_variable_terms_changes_reference_rate_and_margin(team: WorkspaceDb) -> None:
    first_id = _reference_rate(team, "EURIBOR 6M")
    second_id = _reference_rate(team, "WIBOR 3M")
    loan_id = _original_loan(
        team,
        name="Variable mortgage",
        rate_mode="variable",
        fixed_rate_bp=None,
        reference_rate_id=first_id,
        margin_bp=150,
    )
    ctx = team.ctx("owner", "owner")
    loans.update_loan_terms(
        team.factory,
        ctx,
        loan_id,
        name="Variable mortgage",
        kind="annuity",
        principal_original=_money(30_000_000),
        current_balance=None,
        balance_date=None,
        origination_date=date(2026, 1, 1),
        first_payment_date=date(2026, 2, 1),
        term_months=360,
        payment_day=1,
        installment_override=None,
        reference_rate_id=second_id,
        margin_bp=200,
    )
    view = loans.get_loan(team.factory, ctx, loan_id)
    assert view.reference_rate_id == second_id
    assert view.reference_rate_name == "WIBOR 3M"
    assert view.margin_bp == 200
    assert view.current_rate_bp == 600


def test_day_count_defaults_and_updates(team: WorkspaceDb) -> None:
    loan_id = _original_loan(team)
    ctx = team.ctx("owner", "owner")
    assert loans.get_loan(team.factory, ctx, loan_id).day_count is DayCount.MONTHLY
    update = {
        "name": "Mortgage",
        "kind": "annuity",
        "principal_original": _money(30_000_000),
        "current_balance": None,
        "balance_date": None,
        "origination_date": date(2026, 1, 1),
        "first_payment_date": date(2026, 2, 1),
        "term_months": 360,
        "payment_day": 1,
        "installment_override": None,
    }
    loans.update_loan_terms(team.factory, ctx, loan_id, **update, day_count="actual_365")
    assert loans.get_loan(team.factory, ctx, loan_id).day_count is DayCount.ACTUAL_365
    loans.update_loan_terms(team.factory, ctx, loan_id, **update, day_count=None)
    assert loans.get_loan(team.factory, ctx, loan_id).day_count is DayCount.ACTUAL_365

    actual = _original_loan(team, name="Actual loan", day_count="actual_365")
    assert loans.get_loan(team.factory, ctx, actual).day_count is DayCount.ACTUAL_365


def test_tranched_loan_transfers_are_editable_and_capped_at_the_principal(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("owner", "owner")
    loan_id = _original_loan(
        team,
        ctx,
        principal_original=_money(30_000_000),
        term_months=60,
        first_payment_date=date(2026, 2, 1),
        drawdown_mode="tranches",
        interest_only_months=2,
    )
    view = loans.get_loan(team.factory, ctx, loan_id)
    assert view.drawdown_mode.value == "tranches"
    assert view.interest_only_months == 2
    assert view.transfers == ()
    row = next(row for row in loans.list_loans(team.factory, ctx).active if row.loan.id == loan_id)
    assert row.status == "not_drawn"

    loans.add_transfer(
        team.factory,
        ctx,
        loan_id,
        date_value=date(2026, 1, 15),
        amount=_money(10_000_000),
        note="First",
    )
    loans.add_transfer(
        team.factory,
        ctx,
        loan_id,
        date_value=date(2026, 3, 15),
        amount=_money(5_000_000),
        note="Future",
    )
    view = loans.get_loan(team.factory, ctx, loan_id)
    assert [row.amount.amount_minor for row in view.transfers] == [10_000_000, 5_000_000]
    assert [row.future for row in view.transfers] == [False, True]
    row = next(row for row in loans.list_loans(team.factory, ctx).active if row.loan.id == loan_id)
    assert row.figures is not None
    assert row.figures.drawn_principal == _money(10_000_000)
    assert row.figures.undrawn_principal == _money(20_000_000)
    assert row.status == "active"

    with pytest.raises(loans.LoanError) as error:
        loans.add_transfer(
            team.factory,
            ctx,
            loan_id,
            date_value=date(2026, 4, 1),
            amount=_money(20_000_001),
        )
    assert error.value.code == "drawdown_exceeds_principal"

    first = view.transfers[0]
    loans.update_transfer(
        team.factory,
        ctx,
        loan_id,
        first.id,
        date_value=date(2026, 1, 10),
        amount=_money(12_000_000),
        note="Updated",
    )
    view = loans.get_loan(team.factory, ctx, loan_id)
    assert view.transfers[0].amount == _money(12_000_000)
    assert view.transfers[0].note == "Updated"

    loans.delete_transfer(team.factory, ctx, loan_id, view.transfers[1].id)
    view = loans.get_loan(team.factory, ctx, loan_id)
    assert len(view.transfers) == 1


def test_a_later_first_payment_is_flagged_after_the_first_transfer(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    loan_id = _original_loan(
        team,
        ctx,
        first_payment_date=date(2026, 3, 1),
        drawdown_mode="tranches",
    )
    loans.add_transfer(
        team.factory,
        ctx,
        loan_id,
        date_value=date(2026, 1, 10),
        amount=_money(10_000_000),
    )
    view = loans.get_loan(team.factory, ctx, loan_id)
    assert view.transfer_payment_gap is True


def test_converting_a_full_loan_seeds_a_neutral_transfer(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    loan_id = _original_loan(team, ctx)
    viewer = team.ctx("viewer", "viewer")
    with pytest.raises(PermissionDenied):
        loans.convert_loan_to_tranched(team.factory, viewer, loan_id)

    loans.convert_loan_to_tranched(team.factory, ctx, loan_id)
    view = loans.get_loan(team.factory, ctx, loan_id)
    assert view.drawdown_mode.value == "tranches"
    assert len(view.transfers) == 1
    transfer = view.transfers[0]
    assert transfer.date == date(2026, 1, 1)
    assert transfer.amount == _money(30_000_000)
    assert transfer.note == ""

    with pytest.raises(loans.LoanError) as error:
        loans.convert_loan_to_tranched(team.factory, ctx, loan_id)
    assert error.value.code == "already_tranched"


def test_conversion_keeps_a_checkpoint_and_the_interest_only_clock(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    loan_id = _original_loan(
        team,
        ctx,
        term_months=60,
        interest_only_months=24,
        first_payment_date=date(2026, 2, 1),
    )
    with team.factory.begin() as session:
        LoanBalanceCheckpointRepository(session, ctx).create(
            loan_id=loan_id,
            as_of=date(2026, 2, 15),
            remaining_principal_minor=29_500_000,
            note="",
        )
    before = loan_schedule.loan_schedule(team.factory, ctx, loan_id)
    assert before.schedule is not None and before.summary is not None

    loans.convert_loan_to_tranched(team.factory, ctx, loan_id)
    view = loans.get_loan(team.factory, ctx, loan_id)
    assert view.transfers[0].date == date(2026, 1, 1)
    assert view.transfers[0].amount == _money(30_000_000)
    after = loan_schedule.loan_schedule(team.factory, ctx, loan_id)
    assert after.schedule is not None and after.summary is not None
    assert after.summary.paid_principal == _money(500_000)
    assert after.summary.undrawn_principal == _money(0)
    first_amortizing = next(
        row.date for row in after.schedule.rows if row.principal.amount_minor > 0
    )
    assert first_amortizing == date(2028, 2, 1)


def test_shortening_the_term_cannot_strand_a_transfer(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    loan_id = _original_loan(
        team,
        ctx,
        term_months=12,
        drawdown_mode="tranches",
    )
    loans.add_transfer(
        team.factory,
        ctx,
        loan_id,
        date_value=date(2026, 1, 15),
        amount=_money(1_000_000),
    )
    loans.add_transfer(
        team.factory,
        ctx,
        loan_id,
        date_value=date(2026, 10, 1),
        amount=_money(1_000_000),
    )
    with pytest.raises(loans.LoanError) as error:
        loans.update_loan_terms(
            team.factory,
            ctx,
            loan_id,
            name="Mortgage",
            kind="annuity",
            principal_original=_money(30_000_000),
            current_balance=None,
            balance_date=None,
            origination_date=date(2026, 1, 1),
            first_payment_date=date(2026, 2, 1),
            term_months=6,
            payment_day=1,
            installment_override=None,
        )
    assert error.value.field == "term_months"
    assert error.value.code == "transfer_after_final_payment"


def test_a_transfer_after_the_last_scheduled_payment_is_refused(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    loan_id = _original_loan(
        team,
        ctx,
        principal_original=_money(1_000_000),
        term_months=2,
        first_payment_date=date(2026, 2, 1),
        drawdown_mode="tranches",
    )
    loans.add_transfer(
        team.factory,
        ctx,
        loan_id,
        date_value=date(2026, 1, 15),
        amount=_money(500_000),
    )
    with pytest.raises(loans.LoanError) as error:
        loans.add_transfer(
            team.factory,
            ctx,
            loan_id,
            date_value=date(2026, 4, 15),
            amount=_money(500_000),
        )
    assert error.value.code == "transfer_after_final_payment"
