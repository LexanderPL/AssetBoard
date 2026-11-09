"""Investment income: linked ledger rows and the yearly report (F6.10)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.models.investments import Instrument, InvestmentTransaction
from assetboard.db.models.ledger import Category, Transaction
from assetboard.db.repositories import (
    AuditEventRepository,
    CategoryRepository,
    CurrentWorkspaceRepository,
    InvestmentTransactionRepository,
    TransactionRepository,
)
from assetboard.domain.categories import name_key
from assetboard.domain.holdings import (
    InvalidHoldingTransaction,
    InvestmentTransactionType,
    dividend_amounts,
)
from assetboard.domain.money import Money
from assetboard.domain.transactions import description_key
from assetboard.i18n import gettext
from assetboard.i18n.locale import SUPPORTED_LOCALES
from assetboard.services.context import Context, Role, require_role
from assetboard.services.holding_rows import holding_transaction

INCOME_SOURCE = "investment"


@dataclass(frozen=True, slots=True)
class IncomeYear:
    year: int
    gross: Money
    withholding: Money
    net: Money
    payments: int


@dataclass(frozen=True, slots=True)
class InvestmentIncomePage:
    currency: str
    years: tuple[IncomeYear, ...]
    undated_count: int
    invalid_count: int


def _active_income_categories(session: Session, ctx: Context) -> list[Category]:
    return [
        row for row in CategoryRepository(session, ctx).ordered("income") if row.archived_at is None
    ]


def income_category_options(session: Session, ctx: Context) -> list[Category]:
    return _active_income_categories(session, ctx)


def default_income_category_id(session: Session, ctx: Context) -> UUID | None:
    """The most recent linked dividend's category, else the seeded Interest category."""
    dividends = sorted(
        (
            row
            for row in InvestmentTransactionRepository(session, ctx).list()
            if row.type == InvestmentTransactionType.DIVIDEND.value
            and row.income_transaction_id is not None
        ),
        key=lambda row: (row.date or date.min, row.created_at, str(row.id)),
        reverse=True,
    )
    transactions = TransactionRepository(session, ctx)
    for dividend in dividends:
        linked = transactions.get(dividend.income_transaction_id)  # type: ignore[arg-type]
        if linked is None or linked.deleted_at is not None:
            continue
        category = CategoryRepository(session, ctx).get(linked.category_id)
        if category is not None and category.kind == "income" and category.archived_at is None:
            return category.id
    for locale in (ctx.workspace.locale, *SUPPORTED_LOCALES):
        key = name_key(gettext("Interest", locale))
        category = CategoryRepository(session, ctx).active_by_name_key(kind="income", name_key=key)
        if category is not None:
            return category.id
    return None


def _income_amounts(row: InvestmentTransaction, base_currency: str) -> tuple[Money, Money, Money]:
    amounts = dividend_amounts(holding_transaction(row), base_currency)
    return amounts.gross, amounts.withholding, amounts.net


def _description(instrument: Instrument, locale: str) -> str:
    return gettext("Investment income: %(instrument)s", locale) % {"instrument": instrument.name}


def create_linked_income(
    session: Session,
    ctx: Context,
    *,
    instrument: Instrument,
    dividend: InvestmentTransaction,
    category_id: UUID,
    base_currency: str,
    locale: str,
) -> UUID:
    """Create the net ledger income row for a dividend and return its id."""
    category = CategoryRepository(session, ctx).get(category_id)
    if category is None or category.kind != "income" or category.archived_at is not None:
        raise ValueError("an active income category is required")
    _, _, net = _income_amounts(dividend, base_currency)
    if net.amount_minor <= 0:
        raise ValueError("the net income must be positive")
    description = _description(instrument, locale)
    transaction = TransactionRepository(session, ctx).create(
        direction="income",
        day=dividend.date or ctx.clock.today(ctx.workspace.timezone),
        amount_minor=net.amount_minor,
        currency=base_currency,
        category_id=category.id,
        description=description,
        description_key=description_key(description),
        source=INCOME_SOURCE,
        created_by_user_id=ctx.user.id,
    )
    _audit(session, ctx, "transaction.created", transaction, dividend.id)
    return transaction.id


def update_linked_income(
    session: Session,
    ctx: Context,
    *,
    instrument: Instrument,
    dividend: InvestmentTransaction,
    category_id: UUID,
    base_currency: str,
    locale: str,
) -> None:
    category = CategoryRepository(session, ctx).get(category_id)
    if category is None or category.kind != "income" or category.archived_at is not None:
        raise ValueError("an active income category is required")
    _, _, net = _income_amounts(dividend, base_currency)
    if net.amount_minor <= 0:
        raise ValueError("the net income must be positive")
    assert dividend.income_transaction_id is not None
    assert dividend.date is not None
    description = _description(instrument, locale)
    repository = TransactionRepository(session, ctx)
    transaction = repository.update(
        dividend.income_transaction_id,
        date=dividend.date,
        amount_minor=net.amount_minor,
        currency=base_currency,
        category_id=category.id,
        description=description,
        description_key=description_key(description),
    )
    _audit(session, ctx, "transaction.updated", transaction, dividend.id)


def remove_linked_income(
    session: Session, ctx: Context, dividend: InvestmentTransaction, *, now: datetime
) -> None:
    if dividend.income_transaction_id is None:
        return
    repository = TransactionRepository(session, ctx)
    transaction = repository.get(dividend.income_transaction_id)
    if transaction is not None and transaction.deleted_at is None:
        repository.update(transaction.id, deleted_at=now)
        _audit(session, ctx, "transaction.deleted", transaction, dividend.id)


def restore_linked_income(session: Session, ctx: Context, dividend: InvestmentTransaction) -> None:
    if dividend.income_transaction_id is None:
        return
    repository = TransactionRepository(session, ctx)
    transaction = repository.get(dividend.income_transaction_id)
    if transaction is not None and transaction.deleted_at is not None:
        repository.update(transaction.id, deleted_at=None)
        _audit(session, ctx, "transaction.restored", transaction, dividend.id)


def _audit(
    session: Session,
    ctx: Context,
    action: str,
    transaction: Transaction,
    dividend_id: UUID,
) -> None:
    category = CategoryRepository(session, ctx).get(transaction.category_id)
    AuditEventRepository(session, ctx).record(
        actor_user_id=ctx.user.id,
        action=action,
        entity_type="transaction",
        entity_id=str(transaction.id),
        ts=ctx.clock.now(),
        data={
            "direction": transaction.direction,
            "date": transaction.date.isoformat(),
            "amount": str(Money(transaction.amount_minor, transaction.currency).to_decimal()),
            "currency": transaction.currency,
            "category": category.name if category is not None else "",
            "source": transaction.source,
            "investment_transaction_id": str(dividend_id),
        },
    )


def investment_income_page(factory: sessionmaker[Session], ctx: Context) -> InvestmentIncomePage:
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        base = CurrentWorkspaceRepository(session, ctx).get().base_currency
        rows = [
            row
            for row in InvestmentTransactionRepository(session, ctx).live()
            if row.type == InvestmentTransactionType.DIVIDEND.value
        ]
    totals: dict[int, list[int]] = {}
    undated = 0
    invalid = 0
    for row in rows:
        if row.date is None:
            undated += 1
            continue
        try:
            gross, withholding, net = _income_amounts(row, base)
        except InvalidHoldingTransaction:
            invalid += 1
            continue
        bucket = totals.setdefault(row.date.year, [0, 0, 0, 0])
        bucket[0] += gross.amount_minor
        bucket[1] += withholding.amount_minor
        bucket[2] += net.amount_minor
        bucket[3] += 1
    years = tuple(
        IncomeYear(
            year=year,
            gross=Money(values[0], base),
            withholding=Money(values[1], base),
            net=Money(values[2], base),
            payments=values[3],
        )
        for year, values in sorted(totals.items(), reverse=True)
    )
    return InvestmentIncomePage(
        currency=base,
        years=years,
        undated_count=undated,
        invalid_count=invalid,
    )
