"""Full exports through real scoped repositories, with synthetic tenant data."""

from collections.abc import Iterator
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from assetboard.db.models.identity import AuditEvent, Membership
from assetboard.db.models.ledger import BudgetLine, Category, MonthState, Transaction
from assetboard.db.repositories import (
    CategoryRepository,
    FireContributionRepository,
    FireEventRepository,
    FireExpenseExclusionRepository,
    FireScenarioRepository,
    ItemValuationRepository,
    LoanRepository,
    NetWorthCategoryRepository,
    NetWorthItemRepository,
    PriceSourceConfigRepository,
    RiskTargetPolicyRepository,
    TransactionRepository,
)
from assetboard.domain.archive import lineage_id, parse_archive
from assetboard.services.archive import export_workspace
from assetboard.services.context import PermissionDenied
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def team(tmp_path: Path) -> Iterator[WorkspaceDb]:
    for db in workspace_db(tmp_path):
        db.add_member("editor", "editor")
        db.add_member("viewer", "viewer")
        yield db


def test_export_complete_scoped_deterministic_and_lineage(team: WorkspaceDb) -> None:
    origin_category, origin_transaction = uuid4(), uuid4()
    other = team.add_workspace("Private")
    with team.factory.begin() as session:
        categories = []
        for workspace_id in (team.workspace_id, other):
            category = Category(
                workspace_id=workspace_id,
                name="Food",
                name_key="food",
                kind="expense",
                treatment="regular",
                color="#16a34a",
                sort_order=0,
                archived_at=team.clock.now(),
                import_key=f"id:{origin_category}",
            )
            session.add(category)
            session.flush()
            categories.append(category.id)
            session.add(
                Transaction(
                    workspace_id=workspace_id,
                    direction="expense",
                    date=date(2026, 2, 3),
                    amount_minor=1234,
                    currency="EUR",
                    category_id=category.id,
                    description="Bakery",
                    description_key="bakery",
                    source="import",
                    import_key=f"id:{origin_transaction}",
                    deleted_at=team.clock.now(),
                )
            )
            session.add(
                BudgetLine(
                    workspace_id=workspace_id,
                    month=date(2026, 2, 1),
                    category_id=category.id,
                    planned_minor=5000,
                    currency="EUR",
                )
            )
            session.add(
                MonthState(
                    workspace_id=workspace_id,
                    month=date(2026, 2, 1),
                    closed_at=team.clock.now(),
                    plan_started_at=team.clock.now(),
                )
            )
    ctx = team.ctx("editor", "editor")
    result = export_workspace(team.factory, ctx)
    assert result.filename == "assetboard-home-20260301-120000Z.zip"
    assert result.content == export_workspace(team.factory, ctx).content
    parsed, errors = parse_archive(result.content)
    assert not errors and parsed is not None
    rows = parsed.rows
    assert (
        len(rows.categories)
        == len(rows.transactions)
        == len(rows.months)
        == len(rows.budget_lines)
        == 1
    )
    assert rows.categories[0].id == str(origin_category)
    assert rows.categories[0].archived_at == "2026-03-01T12:00:00Z"
    assert rows.transactions[0].id == str(origin_transaction)
    assert rows.transactions[0].category_id == str(origin_category)
    assert rows.transactions[0].deleted_at == "2026-03-01T12:00:00Z"
    assert rows.transactions[0].amount == "12.34"
    assert rows.budget_lines[0].category_id == str(origin_category)
    assert rows.budget_lines[0].planned == "50.00"
    assert rows.months[0].closed_at == rows.months[0].plan_started_at == "2026-03-01T12:00:00Z"
    assert rows.workspace["name"] == "Home"
    with team.factory() as session:
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "workspace.exported")
        ).all()
        assert len(events) == 2
        assert all(event.workspace_id == team.workspace_id for event in events)
        assert events[0].data == {
            "categories": 1,
            "transactions": 1,
            "budget_lines": 1,
            "months": 1,
            "networth_categories": 0,
            "networth_items": 0,
            "item_valuations": 0,
            "networth_snapshots": 0,
            "asset_classes": 0,
            "instruments": 0,
            "instrument_allocations": 0,
            "investment_transactions": 0,
            "bond_terms": 0,
            "price_points": 0,
            "fx_rates": 0,
            "price_source_configs": 0,
            "reference_rates": 0,
            "reference_rate_values": 0,
            "loans": 0,
            "loan_rate_changes": 0,
            "loan_transfers": 0,
            "loan_cost_components": 0,
            "loan_overpayments": 0,
            "loan_payments": 0,
            "loan_balance_checkpoints": 0,
            "loan_scenarios": 0,
            "installment_plans": 0,
            "fire_scenarios": 0,
            "fire_contributions": 0,
            "fire_events": 0,
            "fire_expense_exclusions": 0,
        }
    # Uniqueness is per workspace, including deleted/archived rows; NULLs remain allowed.
    with pytest.raises(IntegrityError), team.factory.begin() as session:
        session.add(
            Category(
                workspace_id=team.workspace_id,
                name="Duplicate",
                name_key="duplicate",
                kind="income",
                treatment="regular",
                color="#16a34a",
                sort_order=0,
                import_key=f"id:{origin_category}",
            )
        )


def test_export_refuses_viewer_and_stale_role(team: WorkspaceDb) -> None:
    with pytest.raises(PermissionDenied):
        export_workspace(team.factory, team.ctx("viewer", "viewer"))
    ctx = team.ctx("editor", "editor")
    with team.factory.begin() as session:
        member = session.get(Membership, team.memberships["editor"])
        assert member is not None
        member.role = "viewer"
    with pytest.raises(PermissionDenied):
        export_workspace(team.factory, ctx)
    with team.factory() as session:
        assert not session.scalars(select(AuditEvent)).all()


def test_lineage_uses_origin_or_local_id() -> None:
    local, origin = uuid4(), uuid4()
    assert lineage_id(local, f"id:{origin}") == origin
    assert lineage_id(local, "c1:" + "a" * 64) == local
    assert lineage_id(local, None) == local


def test_export_orders_categories_and_entries(team: WorkspaceDb) -> None:
    from datetime import timedelta
    from uuid import UUID

    with team.factory.begin() as session:
        category_ids = []
        # Insert in the opposite order to the archive's kind/archive/order/name ordering.
        for index, (name, kind, archived, order) in enumerate(
            [
                ("Salary", "income", False, 0),
                ("Archived", "expense", True, 0),
                ("Zebra", "expense", False, 1),
                ("Apple", "expense", False, 1),
            ]
        ):
            row = Category(
                workspace_id=team.workspace_id,
                id=UUID(int=index + 1),
                name=name,
                name_key=name.lower(),
                kind=kind,
                treatment="regular",
                color="#16a34a",
                sort_order=order,
                archived_at=team.clock.now() if archived else None,
            )
            session.add(row)
            category_ids.append(row.id)
        session.flush()
        for index in (3, 2, 1):
            session.add(
                Transaction(
                    workspace_id=team.workspace_id,
                    id=UUID(int=index + 10),
                    direction="expense",
                    date=date(2026, 2, 3) if index > 1 else date(2026, 2, 2),
                    amount_minor=index,
                    currency="EUR",
                    category_id=category_ids[-1],
                    description=str(index),
                    description_key=str(index),
                    source="manual",
                    created_at=team.clock.now(),
                )
            )
        for month in (2, 1):
            for category_id in category_ids:
                session.add(
                    BudgetLine(
                        workspace_id=team.workspace_id,
                        month=date(2026, month, 1),
                        category_id=category_id,
                        planned_minor=10,
                        currency="EUR",
                    )
                )
        session.flush()
        # Explicit entry timestamp takes precedence over UUID when dates match.
        transaction = session.get(Transaction, UUID(int=12))
        assert transaction is not None
        transaction.created_at = team.clock.now() + timedelta(seconds=1)
    result = export_workspace(team.factory, team.ctx("editor", "editor"))
    parsed, errors = parse_archive(result.content)
    assert not errors and parsed is not None
    names = ["Apple", "Zebra", "Archived", "Salary"]
    assert [row.name for row in parsed.rows.categories] == names
    assert [row.description for row in parsed.rows.transactions] == ["1", "3", "2"]
    assert [(row.month, row.category) for row in parsed.rows.budget_lines] == [
        (month, name) for month in ("2026-01", "2026-02") for name in names
    ]


def test_export_breaks_category_ties_by_the_exported_id(team: WorkspaceDb) -> None:
    from uuid import UUID

    # Two archived "Old" categories differ only by id; the local ids sort the other way round
    # from the lineage ids, which are what a restore keeps.
    with team.factory.begin() as session:
        for local, origin in ((1, 9), (2, 8)):
            session.add(
                Category(
                    workspace_id=team.workspace_id,
                    id=UUID(int=local),
                    name="Old",
                    name_key="old",
                    kind="expense",
                    treatment="regular",
                    color="#16a34a",
                    sort_order=0,
                    archived_at=team.clock.now(),
                    import_key=f"id:{UUID(int=origin)}",
                )
            )
    result = export_workspace(team.factory, team.ctx("editor", "editor"))
    parsed, errors = parse_archive(result.content)
    assert not errors and parsed is not None
    assert [row.id for row in parsed.rows.categories] == [str(UUID(int=8)), str(UUID(int=9))]


def test_export_locks_workspace_before_reading_ledger(team: WorkspaceDb) -> None:
    from sqlalchemy import event
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.orm import ORMExecuteState, Session
    from sqlalchemy.sql import ClauseElement

    statements: list[str] = []

    def record(state: ORMExecuteState) -> None:
        assert isinstance(state.statement, ClauseElement)
        statements.append(str(state.statement.compile(dialect=postgresql.dialect())))

    event.listen(Session, "do_orm_execute", record)
    try:
        export_workspace(team.factory, team.ctx("editor", "editor"))
    finally:
        event.remove(Session, "do_orm_execute", record)
    lock = next(i for i, sql in enumerate(statements) if "FOR UPDATE" in sql)
    ledger = next(i for i, sql in enumerate(statements) if "FROM categories" in sql)
    assert lock < ledger
    assert "workspaces.id =" in statements[lock]


def test_export_includes_the_taxes_switch_and_upcoming_days(team: WorkspaceDb) -> None:
    from assetboard.db.models.identity import Workspace

    parsed, _ = parse_archive(export_workspace(team.factory, team.ctx("editor", "editor")).content)
    assert parsed is not None
    assert parsed.manifest["format_version"] == "1.8"
    assert parsed.rows.workspace["taxes_enabled"] is False
    assert parsed.rows.workspace["upcoming_days"] is None

    with team.factory.begin() as session:
        workspace = session.get_one(Workspace, team.workspace_id)
        workspace.taxes_enabled = True
        workspace.upcoming_days = 30
    parsed, errors = parse_archive(
        export_workspace(team.factory, team.ctx("editor", "editor")).content
    )
    assert errors == () and parsed is not None and parsed.warnings == ()
    assert parsed.rows.workspace["taxes_enabled"] is True
    assert parsed.rows.workspace["upcoming_days"] == 30


def test_export_includes_loans_and_maps_their_references(team: WorkspaceDb) -> None:
    from assetboard.db.repositories import (
        InstallmentPlanRepository,
        LoanBalanceCheckpointRepository,
        LoanCostComponentRepository,
        LoanOverpaymentRepository,
        LoanPaymentRepository,
        LoanRateChangeRepository,
        LoanRepository,
        LoanScenarioRepository,
        NetWorthCategoryRepository,
        NetWorthItemRepository,
        ReferenceRateRepository,
        ReferenceRateValueRepository,
        TransactionRepository,
    )

    ctx = team.ctx("editor", "editor")
    transaction_origin = uuid4()
    with team.factory.begin() as session:
        category = Category(
            workspace_id=team.workspace_id,
            name="Food",
            name_key="food",
            kind="expense",
            treatment="regular",
            color="#16a34a",
            sort_order=0,
        )
        session.add(category)
        session.flush()
        transaction = TransactionRepository(session, ctx).create(
            direction="expense",
            day=date(2026, 3, 1),
            amount_minor=200_000,
            currency="EUR",
            category_id=category.id,
            description="Mortgage",
            description_key="mortgage",
            source="manual",
            import_key=f"id:{transaction_origin}",
            created_by_user_id=team.users["editor"],
        )
        networth_category = NetWorthCategoryRepository(session, ctx).create(
            name="Property",
            name_key="property",
            side="asset",
            color="#3b6ea8",
            sort_order=0,
        )
        item = NetWorthItemRepository(session, ctx).create(
            side="asset",
            category_id=networth_category.id,
            name="Flat",
            currency="EUR",
            estimate_between=True,
        )
        rate = ReferenceRateRepository(session, ctx).create(
            name="WIBOR", name_key="wibor", notes="3M"
        )
        ReferenceRateValueRepository(session, ctx).create(
            reference_rate_id=rate.id,
            effective_from=date(2026, 1, 1),
            value_bp=575,
        )
        loan = LoanRepository(session, ctx).create(
            name="Mortgage",
            kind="annuity",
            currency="EUR",
            principal_original_minor=30_000_000,
            origination_date=date(2026, 1, 1),
            first_payment_date=date(2026, 2, 1),
            term_months=240,
            payment_day=1,
            rate_mode="variable",
            day_count="actual_365",
            margin_bp=150,
            reference_rate_id=rate.id,
            property_item_id=item.id,
            auto_record_payments=True,
            overpayment_mode="fixed_extra",
            overpayment_amount_minor=10_000,
            overpayment_strategy="reduce_term",
        )
        LoanRateChangeRepository(session, ctx).create(
            loan_id=loan.id, effective_from=date(2026, 1, 1), rate_bp=575
        )
        LoanCostComponentRepository(session, ctx).create(
            loan_id=loan.id,
            name="Fee",
            mode="fixed",
            fixed_minor=500,
            basis="opening_balance",
            effective_from=date(2026, 1, 1),
        )
        LoanOverpaymentRepository(session, ctx).create(
            loan_id=loan.id,
            date=date(2026, 6, 1),
            amount_minor=100_000,
            note="Bonus",
        )
        LoanPaymentRepository(session, ctx).create(
            loan_id=loan.id,
            date=date(2026, 3, 1),
            kind="installment",
            total_minor=200_000,
            principal_minor=100_000,
            interest_minor=99_000,
            costs_minor=1_000,
            overpayment_minor=0,
            source="manual",
            transaction_id=transaction.id,
        )
        LoanBalanceCheckpointRepository(session, ctx).create(
            loan_id=loan.id,
            as_of=date(2026, 7, 1),
            remaining_principal_minor=28_000_000,
        )
        LoanScenarioRepository(session, ctx).create(
            loan_id=loan.id,
            name="Faster",
            name_key="faster",
            overpayment_mode="fixed_extra",
            overpayment_amount_minor=20_000,
            overpayment_strategy="reduce_term",
        )
        plan_loan = LoanRepository(session, ctx).create(
            name="Phone",
            kind="installment_plan",
            currency="EUR",
            principal_original_minor=300_000,
            first_payment_date=date(2026, 4, 5),
            term_months=3,
            payment_day=5,
            rate_mode="fixed",
            day_count="monthly",
            overpayment_mode="none",
            overpayment_strategy="reduce_term",
        )
        InstallmentPlanRepository(session, ctx).create(
            loan_id=plan_loan.id,
            category_id=category.id,
            installment_minor=100_000,
            remaining_count=3,
            end_date=date(2026, 6, 5),
        )

    parsed, errors = parse_archive(export_workspace(team.factory, ctx).content)
    assert errors == () and parsed is not None
    rows = parsed.rows
    assert [row.name for row in rows.reference_rates] == ["WIBOR"]
    assert rows.reference_rate_values[0].value_bp == 575
    loan_row = next(row for row in rows.loans if row.name == "Mortgage")
    assert loan_row.id == str(loan.id)
    assert loan_row.property_item_id == str(item.id)
    assert loan_row.property_item == "Flat"
    assert loan_row.reference_rate_id == str(rate.id)
    assert loan_row.auto_record_payments == "true"
    assert loan_row.day_count == "actual_365"
    assert rows.loan_payments[0].transaction_id == str(transaction_origin)
    assert rows.installment_plans[0].category == "Food"
    assert rows.installment_plans[0].loan == "Phone"
    assert rows.loan_rate_changes[0].loan == "Mortgage"


def test_export_includes_risk_policy_and_never_provider_secrets(team: WorkspaceDb) -> None:
    ctx = team.ctx("editor", "editor")
    with team.factory.begin() as session:
        RiskTargetPolicyRepository(session, ctx).create(
            mode="age_in_bonds",
            safe_bp=0,
            growth_bp=9000,
            speculative_bp=1000,
            tolerance_bp=300,
        )
        PriceSourceConfigRepository(session, ctx).create(
            provider_key="eodhd",
            enabled=True,
            settings_encrypted="secret-api-key",
            fx_backfilled='{"USD":"2026-02-01"}',
            daily_call_budget=7,
        )
    result = export_workspace(team.factory, ctx)
    assert b"secret-api-key" not in result.content
    parsed, errors = parse_archive(result.content)
    assert errors == () and parsed is not None
    assert parsed.rows.workspace["risk_target_policy"] == {
        "mode": "age_in_bonds",
        "safe_bp": 0,
        "growth_bp": 9000,
        "speculative_bp": 1000,
        "tolerance_bp": 300,
    }
    assert [row.provider_key for row in parsed.rows.price_source_configs] == ["eodhd"]
    assert parsed.rows.price_source_configs[0].daily_call_budget == 7
    assert parsed.rows.price_source_configs[0].fx_backfilled == '{"USD":"2026-02-01"}'


def test_export_includes_fire_lineage_fallbacks_children_and_no_n_plus_one(
    team: WorkspaceDb,
) -> None:
    from sqlalchemy import event
    from sqlalchemy.orm import ORMExecuteState, Session
    from sqlalchemy.sql import ClauseElement

    ctx = team.ctx("editor", "editor")
    scenario_origin = uuid4()
    contribution_origin = uuid4()
    event_origin = uuid4()
    exclusion_origin = uuid4()
    category_origin = uuid4()
    statements: list[str] = []

    def record(state: ORMExecuteState) -> None:
        assert isinstance(state.statement, ClauseElement)
        statements.append(str(state.statement))

    with team.factory.begin() as session:
        categories = CategoryRepository(session, ctx)
        food = categories.create(
            name="Food",
            name_key="food",
            kind="expense",
            treatment="regular",
            color="#4f9d69",
            sort_order=0,
            import_key=f"id:{category_origin}",
        )
        salary = categories.create(
            name="Salary",
            name_key="salary",
            kind="income",
            treatment="regular",
            color="#2f7f5f",
            sort_order=0,
        )
        housing = categories.create(
            name="Housing",
            name_key="housing",
            kind="expense",
            treatment="regular",
            color="#3b6ea8",
            sort_order=1,
        )
        transactions = TransactionRepository(session, ctx)
        transactions.create(
            direction="income",
            day=date(2026, 1, 10),
            amount_minor=300_000,
            currency="EUR",
            category_id=salary.id,
            description="January",
            description_key="january",
            source="manual",
            created_by_user_id=team.users["editor"],
        )
        transactions.create(
            direction="expense",
            day=date(2026, 1, 15),
            amount_minor=70_000,
            currency="EUR",
            category_id=food.id,
            description="Rent",
            description_key="rent",
            source="manual",
            created_by_user_id=team.users["editor"],
        )
        transactions.create(
            direction="expense",
            day=date(2026, 1, 20),
            amount_minor=70_000,
            currency="EUR",
            category_id=housing.id,
            description="Housing",
            description_key="housing",
            source="manual",
            created_by_user_id=team.users["editor"],
        )
        networth_categories = NetWorthCategoryRepository(session, ctx)
        assets = networth_categories.create(
            name="Assets", name_key="assets", side="asset", color="#3b6ea8", sort_order=0
        )
        brokerage = NetWorthItemRepository(session, ctx).create(
            side="asset",
            category_id=assets.id,
            name="Brokerage",
            currency="EUR",
            investable=True,
            include_in_net_worth=True,
            estimate_between=True,
        )
        ItemValuationRepository(session, ctx).create(
            item_id=brokerage.id,
            as_of=date(2026, 2, 1),
            value_minor=5_000_000,
            currency="EUR",
        )
        loan = LoanRepository(session, ctx).create(
            name="Mortgage",
            kind="annuity",
            currency="EUR",
            principal_original_minor=10_000_000,
            origination_date=date(2025, 1, 1),
            first_payment_date=date(2025, 2, 1),
            term_months=120,
            payment_day=1,
            rate_mode="fixed",
            day_count="monthly",
            overpayment_mode="none",
            overpayment_strategy="reduce_term",
        )
        scenarios = FireScenarioRepository(session, ctx)
        scenarios.create(
            name="Alpha archived",
            name_key="alpha archived",
            currency="EUR",
            birth_year=1985,
            start_year=2026,
            inflation_bp=250,
            real_return_bp=500,
            income_growth_bp=300,
            stop_working_age=55,
            horizon_age=90,
            rule="returns_cover_expenses",
            swr_bp=None,
            income_mode="trailing_12m",
            income_manual_minor=None,
            expenses_mode="trailing_12m",
            expenses_manual_minor=None,
            portfolio_mode="linked_investable",
            portfolio_manual_minor=None,
            is_default=False,
            archived_at=team.clock.now(),
        )
        active = scenarios.create(
            name="Zeta linked",
            name_key="zeta linked",
            currency="EUR",
            birth_year=1985,
            start_year=2026,
            inflation_bp=250,
            real_return_bp=500,
            income_growth_bp=300,
            stop_working_age=55,
            horizon_age=90,
            rule="returns_cover_expenses",
            swr_bp=None,
            income_mode="trailing_12m",
            income_manual_minor=None,
            expenses_mode="trailing_12m",
            expenses_manual_minor=None,
            portfolio_mode="linked_investable",
            portfolio_manual_minor=None,
            is_default=True,
            import_key=f"id:{scenario_origin}",
        )
        FireContributionRepository(session, ctx).create(
            scenario_id=active.id,
            year=2027,
            net_amount_minor=-120_000,
            import_key=f"id:{contribution_origin}",
        )
        FireEventRepository(session, ctx).create(
            scenario_id=active.id,
            year=2030,
            kind="expense_change",
            amount_minor=-120_000,
            label="Loan repaid: Mortgage",
            source_loan_id=loan.id,
            import_key=f"id:{event_origin}",
        )
        FireExpenseExclusionRepository(session, ctx).create(
            scenario_id=active.id,
            category_id=food.id,
            import_key=f"id:{exclusion_origin}",
        )

    event.listen(Session, "do_orm_execute", record)
    try:
        result = export_workspace(team.factory, ctx)
    finally:
        event.remove(Session, "do_orm_execute", record)
    parsed, errors = parse_archive(result.content)
    assert errors == () and parsed is not None
    rows = parsed.rows
    assert [row.name for row in rows.fire_scenarios] == ["Zeta linked", "Alpha archived"]
    exported = rows.fire_scenarios[0]
    assert exported.id == str(scenario_origin)
    assert exported.income_fallback == "36000.00"
    assert exported.expenses_fallback == "8400.00"
    assert exported.portfolio_fallback == "50000.00"
    assert rows.fire_contributions[0].id == str(contribution_origin)
    assert rows.fire_contributions[0].net_amount == "-1200.00"
    assert rows.fire_events[0].id == str(event_origin)
    assert rows.fire_events[0].source_loan_id == str(loan.id)
    assert rows.fire_expense_exclusions[0].id == str(exclusion_origin)
    assert rows.fire_expense_exclusions[0].category_id == str(category_origin)
    assert rows.fire_expense_exclusions[0].category == "Food"
    for table in (
        "fire_scenarios",
        "fire_contributions",
        "fire_events",
        "fire_expense_exclusions",
    ):
        assert sum(f"FROM {table}" in statement for statement in statements) == 1, table
    with team.factory() as session:
        audit = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "workspace.exported")
        ).one()
        assert audit.data["fire_scenarios"] == 2
        assert audit.data["fire_contributions"] == 1
        assert audit.data["fire_events"] == 1
        assert audit.data["fire_expense_exclusions"] == 1


def test_export_blanks_blocked_fire_fallbacks(team: WorkspaceDb) -> None:
    ctx = team.ctx("editor", "editor")
    with team.factory.begin() as session:
        FireScenarioRepository(session, ctx).create(
            name="Blocked plan",
            name_key="blocked plan",
            currency="EUR",
            birth_year=1990,
            start_year=2026,
            inflation_bp=250,
            real_return_bp=500,
            income_growth_bp=300,
            stop_working_age=55,
            horizon_age=95,
            rule="swr",
            swr_bp=400,
            income_mode="trailing_12m",
            income_manual_minor=None,
            expenses_mode="trailing_12m",
            expenses_manual_minor=None,
            portfolio_mode="linked_investable",
            portfolio_manual_minor=None,
            is_default=True,
        )

    parsed, errors = parse_archive(export_workspace(team.factory, ctx).content)
    assert errors == () and parsed is not None
    scenario = parsed.rows.fire_scenarios[0]
    assert scenario.income_fallback == ""
    assert scenario.expenses_fallback == ""
    assert scenario.portfolio_fallback == ""


def test_export_writes_header_only_fire_files(team: WorkspaceDb) -> None:
    import io
    import zipfile

    from assetboard.domain.archive import CSV_COLUMNS

    result = export_workspace(team.factory, team.ctx("editor", "editor"))
    parsed, errors = parse_archive(result.content)
    assert errors == () and parsed is not None
    assert parsed.rows.fire_scenarios == ()
    assert parsed.rows.fire_contributions == ()
    assert parsed.rows.fire_events == ()
    assert parsed.rows.fire_expense_exclusions == ()
    with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
        for name in (
            "fire_scenarios.csv",
            "fire_contributions.csv",
            "fire_events.csv",
            "fire_expense_exclusions.csv",
        ):
            assert archive.read(name) == (",".join(CSV_COLUMNS[name]) + "\r\n").encode()
