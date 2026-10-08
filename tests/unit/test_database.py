from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from uuid import uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import Integer, MetaData, String
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import Numeric

from assetboard.db.base import (
    CONSTRAINT_NAMING_CONVENTION,
    IdMixin,
    SoftDeleteMixin,
    TimestampMixin,
    WorkspaceScopedMixin,
)
from assetboard.db.engine import create_database_engine, database_is_ready
from assetboard.db.money import money_composite
from assetboard.db.types import DecimalType
from assetboard.domain.money import Money


class TestBase(DeclarativeBase):
    __test__ = False
    metadata = MetaData(naming_convention=CONSTRAINT_NAMING_CONVENTION)


class TestWorkspace(TestBase, IdMixin, TimestampMixin):
    __test__ = False
    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(String(80), nullable=False)


class TestRecord(TestBase, IdMixin, TimestampMixin, SoftDeleteMixin, WorkspaceScopedMixin):
    __test__ = False
    __tablename__ = "test_records"

    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount: Mapped[Money] = money_composite("amount_minor", "currency")
    quantity: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)


@pytest.fixture(scope="module", autouse=True)
def create_test_tables(database_engine: Engine) -> Iterator[None]:
    TestBase.metadata.create_all(database_engine)
    yield
    TestBase.metadata.drop_all(database_engine)


def test_sqlite_engine_sets_required_pragmas(tmp_path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'pragmas.db'}")
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one().lower() == "wal"
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000
            assert connection.exec_driver_sql("PRAGMA synchronous").scalar_one() == 1
    finally:
        engine.dispose()


def test_database_engine_readiness_check(database_engine) -> None:
    assert database_is_ready(database_engine)


@pytest.mark.parametrize(
    "quantity",
    [
        Decimal("0.00924"),
        Decimal("0.123456789012345678"),
        Decimal("-123.45"),
    ],
)
def test_base_and_mixins_have_stable_constraints_and_uuid_keys(
    db_session,
    fixed_clock,
    quantity,
) -> None:
    assert CONSTRAINT_NAMING_CONVENTION["fk"] == (
        "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
    )
    workspace = TestWorkspace(name="Synthetic workspace")
    db_session.add(workspace)
    db_session.flush()

    record = TestRecord(
        workspace_id=workspace.id,
        amount=Money(1234, "EUR"),
        quantity=quantity,
    )
    db_session.add(record)
    db_session.flush()
    db_session.expire(record)

    reloaded = db_session.get(TestRecord, record.id)
    assert reloaded is not None
    assert reloaded.id == record.id
    assert reloaded.workspace_id == workspace.id
    assert reloaded.amount == Money(1234, "EUR")
    assert reloaded.quantity == quantity
    assert reloaded.created_at is not None
    assert reloaded.updated_at is not None
    assert reloaded.deleted_at is None
    assert fixed_clock.now().isoformat() == "2026-01-01T00:00:00+00:00"


def test_workspace_scoped_mixin_enforces_foreign_key(db_session) -> None:
    db_session.add(
        TestRecord(
            workspace_id=uuid4(),
            amount=Money(1, "EUR"),
            quantity=Decimal("1"),
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


@given(
    value=st.decimals(
        min_value=Decimal("-99999999999999999999.999999999999999999"),
        max_value=Decimal("99999999999999999999.999999999999999999"),
        places=18,
        allow_nan=False,
        allow_infinity=False,
    )
)
@settings(max_examples=100)
def test_decimal_type_bind_and_result_round_trip(value: Decimal) -> None:
    decimal_type = DecimalType()
    dialect = sqlite.dialect()

    bound_value = decimal_type.process_bind_param(value, dialect)
    result_value = decimal_type.process_result_value(bound_value, dialect)

    assert isinstance(bound_value, str)
    assert result_value == value


def test_decimal_type_rejects_float_input_and_uses_postgres_numeric() -> None:
    decimal_type = DecimalType()
    with pytest.raises(TypeError, match="float"):
        decimal_type.process_bind_param(0.1, sqlite.dialect())  # type: ignore[arg-type]

    postgres_type = decimal_type.load_dialect_impl(postgresql.dialect())
    assert isinstance(postgres_type, Numeric)
    assert postgres_type.precision == 38
    assert postgres_type.scale == 18


def test_decimal_type_rejects_values_outside_portable_precision() -> None:
    with pytest.raises(ValueError, match="38 digits"):
        DecimalType().process_bind_param(Decimal("0.0000000000000000001"), sqlite.dialect())
