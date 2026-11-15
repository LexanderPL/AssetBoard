"""Shared helpers for F8.12 web and browser tests: a valid loan and its Context."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import select

from assetboard.db.models.identity import User, Workspace
from assetboard.domain.money import Money
from assetboard.services import loans
from assetboard.services.context import Context, Role, UserRef, WorkspaceRef


def owner_context(owner: Any) -> Context:
    with owner.db() as session:
        user = session.scalars(select(User).where(User.id == owner.admin_id)).one()
        workspace = session.scalars(select(Workspace).where(Workspace.id == owner.home_id)).one()
    return Context(
        user=UserRef(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            locale=user.locale,
            is_instance_admin=True,
        ),
        workspace=WorkspaceRef(
            id=workspace.id,
            name=workspace.name,
            base_currency=workspace.base_currency,
            locale=workspace.locale,
            timezone=workspace.timezone,
            country_code=workspace.country_code,
        ),
        role=Role.OWNER,
        clock=owner.app.state.clock,
    )


def create_loan(owner: Any, **overrides: object) -> UUID:
    values: dict[str, Any] = {
        "entry_path": "original",
        "name": "Mortgage",
        "kind": "annuity",
        "principal_original": Money(5_000_000, "EUR"),
        "current_balance": None,
        "balance_date": None,
        "origination_date": date(2026, 1, 1),
        "first_payment_date": date(2026, 2, 1),
        "term_months": 24,
        "payment_day": 1,
        "rate_mode": "fixed",
        "fixed_rate_bp": 600,
        "reference_rate_id": None,
        "margin_bp": None,
        "high_rate_confirmed": False,
        "installment_override": None,
        "overpayment_mode": "none",
        "overpayment_amount": None,
        "overpayment_strategy": "reduce_term",
        "drawdown_mode": "full_at_origination",
        "interest_only_months": 0,
    }
    values.update(overrides)
    return loans.create_loan(  # type: ignore[arg-type]
        owner.app.state.session_factory, owner_context(owner), **values
    )


def loan_toggle(loan_id: UUID, included: bool) -> str:
    return f"loan_include_{loan_id}={'1' if included else '0'}"
