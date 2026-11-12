from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import exists, select

from assetboard.db.models.loans import (
    Loan,
    LoanBalanceCheckpoint,
    LoanCostComponent,
    LoanOverpayment,
    LoanPayment,
    LoanRateChange,
    LoanScenario,
    ReferenceRate,
    ReferenceRateValue,
)
from assetboard.db.repositories.base import ScopedRepository


class LoanRepository(ScopedRepository[Loan]):
    model = Loan

    def create(self, **values: object) -> Loan:
        return self.add(Loan(**values))

    def ordered(self, *, include_archived: bool = True) -> list[Loan]:
        statement = self._scoped_select()
        if not include_archived:
            statement = statement.where(Loan.archived_at.is_(None))
        return list(self._session.scalars(statement.order_by(Loan.name, Loan.id)))


class LoanRateChangeRepository(ScopedRepository[LoanRateChange]):
    model = LoanRateChange

    def create(self, **values: object) -> LoanRateChange:
        return self.add(LoanRateChange(**values))

    def for_loan(self, loan_id: UUID) -> list[LoanRateChange]:
        statement = self._scoped_select().where(LoanRateChange.loan_id == loan_id)
        return list(self._session.scalars(statement.order_by(LoanRateChange.effective_from)))

    def get_for_loan(self, loan_id: UUID, change_id: UUID) -> LoanRateChange | None:
        statement = self._scoped_select().where(
            LoanRateChange.loan_id == loan_id,
            LoanRateChange.id == change_id,
        )
        return self._session.scalars(statement).one_or_none()

    def latest_on_or_before(self, loan_id: UUID, day: date) -> LoanRateChange | None:
        statement = (
            self._scoped_select()
            .where(
                LoanRateChange.loan_id == loan_id,
                LoanRateChange.effective_from <= day,
            )
            .order_by(LoanRateChange.effective_from.desc())
            .limit(1)
        )
        return self._session.scalars(statement).first()

    def exists_on(self, loan_id: UUID, day: date, *, exclude_id: UUID | None = None) -> bool:
        statement = select(
            exists().where(
                LoanRateChange.workspace_id == self._workspace_id,
                LoanRateChange.loan_id == loan_id,
                LoanRateChange.effective_from == day,
            )
        )
        if exclude_id is not None:
            statement = select(
                exists().where(
                    LoanRateChange.workspace_id == self._workspace_id,
                    LoanRateChange.loan_id == loan_id,
                    LoanRateChange.effective_from == day,
                    LoanRateChange.id != exclude_id,
                )
            )
        return bool(self._session.execute(statement).scalar_one())


class ReferenceRateRepository(ScopedRepository[ReferenceRate]):
    model = ReferenceRate

    def create(self, **values: object) -> ReferenceRate:
        return self.add(ReferenceRate(**values))

    def ordered(self) -> list[ReferenceRate]:
        statement = self._scoped_select().order_by(ReferenceRate.name, ReferenceRate.id)
        return list(self._session.scalars(statement))

    def by_name_key(self, name_key: str) -> ReferenceRate | None:
        statement = self._scoped_select().where(ReferenceRate.name_key == name_key)
        return self._session.scalars(statement).one_or_none()

    def used_by_loan(self, reference_rate_id: UUID) -> bool:
        statement = select(
            exists().where(
                Loan.workspace_id == self._workspace_id,
                Loan.reference_rate_id == reference_rate_id,
            )
        )
        return bool(self._session.execute(statement).scalar_one())


class ReferenceRateValueRepository(ScopedRepository[ReferenceRateValue]):
    model = ReferenceRateValue

    def create(self, **values: object) -> ReferenceRateValue:
        return self.add(ReferenceRateValue(**values))

    def for_rate(self, reference_rate_id: UUID) -> list[ReferenceRateValue]:
        statement = self._scoped_select().where(
            ReferenceRateValue.reference_rate_id == reference_rate_id
        )
        return list(self._session.scalars(statement.order_by(ReferenceRateValue.effective_from)))

    def get_for_rate(self, reference_rate_id: UUID, value_id: UUID) -> ReferenceRateValue | None:
        statement = self._scoped_select().where(
            ReferenceRateValue.reference_rate_id == reference_rate_id,
            ReferenceRateValue.id == value_id,
        )
        return self._session.scalars(statement).one_or_none()

    def latest_on_or_before(self, reference_rate_id: UUID, day: date) -> ReferenceRateValue | None:
        statement = (
            self._scoped_select()
            .where(
                ReferenceRateValue.reference_rate_id == reference_rate_id,
                ReferenceRateValue.effective_from <= day,
            )
            .order_by(ReferenceRateValue.effective_from.desc())
            .limit(1)
        )
        return self._session.scalars(statement).first()

    def exists_on(
        self, reference_rate_id: UUID, day: date, *, exclude_id: UUID | None = None
    ) -> bool:
        conditions = [
            ReferenceRateValue.workspace_id == self._workspace_id,
            ReferenceRateValue.reference_rate_id == reference_rate_id,
            ReferenceRateValue.effective_from == day,
        ]
        if exclude_id is not None:
            conditions.append(ReferenceRateValue.id != exclude_id)
        return bool(self._session.execute(select(exists().where(*conditions))).scalar_one())


class LoanCostComponentRepository(ScopedRepository[LoanCostComponent]):
    model = LoanCostComponent

    def create(self, **values: object) -> LoanCostComponent:
        return self.add(LoanCostComponent(**values))

    def for_loan(self, loan_id: UUID) -> list[LoanCostComponent]:
        statement = self._scoped_select().where(LoanCostComponent.loan_id == loan_id)
        return list(
            self._session.scalars(
                statement.order_by(LoanCostComponent.effective_from, LoanCostComponent.name)
            )
        )

    def get_for_loan(self, loan_id: UUID, component_id: UUID) -> LoanCostComponent | None:
        statement = self._scoped_select().where(
            LoanCostComponent.loan_id == loan_id,
            LoanCostComponent.id == component_id,
        )
        return self._session.scalars(statement).one_or_none()


class LoanOverpaymentRepository(ScopedRepository[LoanOverpayment]):
    model = LoanOverpayment

    def create(self, **values: object) -> LoanOverpayment:
        return self.add(LoanOverpayment(**values))

    def for_loan(self, loan_id: UUID) -> list[LoanOverpayment]:
        statement = self._scoped_select().where(LoanOverpayment.loan_id == loan_id)
        return list(
            self._session.scalars(statement.order_by(LoanOverpayment.date, LoanOverpayment.id))
        )

    def get_for_loan(self, loan_id: UUID, overpayment_id: UUID) -> LoanOverpayment | None:
        statement = self._scoped_select().where(
            LoanOverpayment.loan_id == loan_id,
            LoanOverpayment.id == overpayment_id,
        )
        return self._session.scalars(statement).one_or_none()


class LoanBalanceCheckpointRepository(ScopedRepository[LoanBalanceCheckpoint]):
    model = LoanBalanceCheckpoint

    def create(self, **values: object) -> LoanBalanceCheckpoint:
        return self.add(LoanBalanceCheckpoint(**values))

    def for_loan(self, loan_id: UUID) -> list[LoanBalanceCheckpoint]:
        statement = self._scoped_select().where(LoanBalanceCheckpoint.loan_id == loan_id)
        return list(
            self._session.scalars(
                statement.order_by(LoanBalanceCheckpoint.as_of, LoanBalanceCheckpoint.id)
            )
        )

    def latest_on_or_before(self, loan_id: UUID, day: date) -> LoanBalanceCheckpoint | None:
        statement = (
            self._scoped_select()
            .where(
                LoanBalanceCheckpoint.loan_id == loan_id,
                LoanBalanceCheckpoint.as_of <= day,
            )
            .order_by(LoanBalanceCheckpoint.as_of.desc(), LoanBalanceCheckpoint.id.desc())
            .limit(1)
        )
        return self._session.scalars(statement).first()

    def get_for_loan(self, loan_id: UUID, checkpoint_id: UUID) -> LoanBalanceCheckpoint | None:
        statement = self._scoped_select().where(
            LoanBalanceCheckpoint.loan_id == loan_id,
            LoanBalanceCheckpoint.id == checkpoint_id,
        )
        return self._session.scalars(statement).one_or_none()

    def exists_on(self, loan_id: UUID, day: date, *, exclude_id: UUID | None = None) -> bool:
        conditions = [
            LoanBalanceCheckpoint.workspace_id == self._workspace_id,
            LoanBalanceCheckpoint.loan_id == loan_id,
            LoanBalanceCheckpoint.as_of == day,
        ]
        if exclude_id is not None:
            conditions.append(LoanBalanceCheckpoint.id != exclude_id)
        return bool(self._session.execute(select(exists().where(*conditions))).scalar_one())


class LoanScenarioRepository(ScopedRepository[LoanScenario]):
    model = LoanScenario

    def create(self, **values: object) -> LoanScenario:
        return self.add(LoanScenario(**values))

    def for_loan(self, loan_id: UUID) -> list[LoanScenario]:
        statement = self._scoped_select().where(LoanScenario.loan_id == loan_id)
        return list(self._session.scalars(statement.order_by(LoanScenario.name, LoanScenario.id)))

    def get_for_loan(self, loan_id: UUID, scenario_id: UUID) -> LoanScenario | None:
        statement = self._scoped_select().where(
            LoanScenario.loan_id == loan_id,
            LoanScenario.id == scenario_id,
        )
        return self._session.scalars(statement).one_or_none()

    def by_name_key(self, loan_id: UUID, name_key: str) -> LoanScenario | None:
        statement = self._scoped_select().where(
            LoanScenario.loan_id == loan_id,
            LoanScenario.name_key == name_key,
        )
        return self._session.scalars(statement).one_or_none()


class LoanPaymentRepository(ScopedRepository[LoanPayment]):
    model = LoanPayment

    def create(self, **values: object) -> LoanPayment:
        return self.add(LoanPayment(**values))

    def for_loan(self, loan_id: UUID) -> list[LoanPayment]:
        statement = self._scoped_select().where(LoanPayment.loan_id == loan_id)
        return list(
            self._session.scalars(
                statement.order_by(LoanPayment.date.desc(), LoanPayment.created_at.desc())
            )
        )

    def get_for_loan(self, loan_id: UUID, payment_id: UUID) -> LoanPayment | None:
        statement = self._scoped_select().where(
            LoanPayment.loan_id == loan_id,
            LoanPayment.id == payment_id,
        )
        return self._session.scalars(statement).one_or_none()

    def for_loan_on_date(self, loan_id: UUID, day: date) -> list[LoanPayment]:
        statement = self._scoped_select().where(
            LoanPayment.loan_id == loan_id,
            LoanPayment.date == day,
        )
        return list(self._session.scalars(statement.order_by(LoanPayment.created_at)))
