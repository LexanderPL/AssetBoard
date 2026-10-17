"""Transactions of the current workspace (F2.2)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy import Integer, case, cast, exists, extract, func, select

from assetboard.db.models.ledger import Transaction
from assetboard.db.repositories.base import ScopedRepository
from assetboard.domain.months import YearMonth


@dataclass(frozen=True, slots=True)
class CategoryCounts:
    references: int  # every row, soft-deleted ones included
    active: int  # rows that are not deleted


@dataclass(frozen=True, slots=True)
class DescriptionMatch:
    """One (description, category) pair among non-deleted rows, with its use count."""

    description_key: str
    description: str
    category_id: UUID
    uses: int
    last_date: date


class TransactionRepository(ScopedRepository[Transaction]):
    """Rows are soft-deleted (``deleted_at``), never removed: generic ``delete`` is unsupported.

    Queries for the month page and autocomplete skip soft-deleted rows; ``get`` returns them
    (the service decides what a deleted row means), and the usage counts include them because
    they still reference their category.
    """

    model = Transaction
    unsupported = frozenset({"delete"})

    def create(
        self,
        *,
        direction: str,
        day: date,
        amount_minor: int,
        currency: str,
        category_id: UUID,
        description: str,
        description_key: str,
        source: str,
        created_by_user_id: UUID | None,
    ) -> Transaction:
        return self.add(
            Transaction(
                direction=direction,
                date=day,
                amount_minor=amount_minor,
                currency=currency,
                category_id=category_id,
                description=description,
                description_key=description_key,
                source=source,
                created_by_user_id=created_by_user_id,
            )
        )

    def for_month(self, month: YearMonth, direction: str | None = None) -> list[Transaction]:
        """Non-deleted rows dated in ``month``, by date and then entry order."""
        statement = self._scoped_select().where(
            Transaction.deleted_at.is_(None),
            Transaction.date >= month.first_day,
            Transaction.date <= month.last_day,
        )
        if direction is not None:
            statement = statement.where(Transaction.direction == direction)
        statement = statement.order_by(Transaction.date, Transaction.created_at, Transaction.id)
        return list(self._session.scalars(statement))

    def counts_by_category(self) -> dict[UUID, CategoryCounts]:
        """Per category: all rows (deleted included) and non-deleted rows."""
        active = func.sum(case((Transaction.deleted_at.is_(None), 1), else_=0))
        statement = (
            select(Transaction.category_id, func.count(), active)
            .where(Transaction.workspace_id == self._workspace_id)
            .group_by(Transaction.category_id)
        )
        return {
            category_id: CategoryCounts(references=int(total), active=int(live or 0))
            for category_id, total, live in self._session.execute(statement)
        }

    def active_months_by_category(self) -> dict[UUID, frozenset[YearMonth]]:
        """Per category: the months that have non-deleted rows."""
        year = cast(extract("year", Transaction.date), Integer)
        month = cast(extract("month", Transaction.date), Integer)
        statement = (
            select(Transaction.category_id, year, month)
            .where(
                Transaction.workspace_id == self._workspace_id,
                Transaction.deleted_at.is_(None),
            )
            .group_by(Transaction.category_id, year, month)
        )
        months: dict[UUID, set[YearMonth]] = {}
        for category_id, y, m in self._session.execute(statement):
            months.setdefault(category_id, set()).add(YearMonth(int(y), int(m)))
        return {category_id: frozenset(found) for category_id, found in months.items()}

    def any_exists(self) -> bool:
        """Whether the workspace has any transaction, soft-deleted ones included."""
        statement = select(exists().where(Transaction.workspace_id == self._workspace_id))
        return bool(self._session.execute(statement).scalar_one())

    def description_matches(
        self, *, direction: str, key_prefix: str, limit: int
    ) -> list[DescriptionMatch]:
        """Non-deleted rows of ``direction`` whose description key starts with ``key_prefix``.

        Grouped by (key, spelling, category), most used first; ``%`` and ``_`` are literal.
        """
        uses = func.count().label("uses")
        statement = (
            select(
                Transaction.description_key,
                Transaction.description,
                Transaction.category_id,
                uses,
                func.max(Transaction.date),
            )
            .where(
                Transaction.workspace_id == self._workspace_id,
                Transaction.deleted_at.is_(None),
                Transaction.direction == direction,
                Transaction.description_key != "",
                Transaction.description_key.startswith(key_prefix, autoescape=True),
            )
            .group_by(Transaction.description_key, Transaction.description, Transaction.category_id)
            .order_by(uses.desc(), Transaction.description_key, Transaction.category_id)
            .limit(limit)
        )
        return [
            DescriptionMatch(key, description, category_id, int(count), last)
            for key, description, category_id, count, last in self._session.execute(statement)
        ]
