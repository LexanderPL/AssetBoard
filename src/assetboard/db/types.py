from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import Numeric, Text
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator, TypeEngine


class DecimalType(TypeDecorator[Decimal]):
    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(Text())
        if dialect.name == "postgresql":
            return dialect.type_descriptor(Numeric(precision=38, scale=18, asdecimal=True))
        return dialect.type_descriptor(Numeric(precision=38, scale=18, asdecimal=True))

    def process_bind_param(
        self,
        value: Decimal | None,
        dialect: Dialect,
    ) -> str | Decimal | None:
        if value is None:
            return None
        if isinstance(value, float):
            raise TypeError("DecimalType does not accept float values")
        if not isinstance(value, Decimal):
            raise TypeError("DecimalType requires a Decimal value")
        if not value.is_finite():
            raise ValueError("DecimalType requires a finite Decimal value")

        exponent = value.as_tuple().exponent
        if not isinstance(exponent, int):
            raise ValueError("DecimalType requires a finite Decimal value")
        scale = max(-exponent, 0)
        integer_digits = 0 if value.is_zero() else max(value.adjusted() + 1, 0)
        if scale > 18 or integer_digits > 20 or integer_digits + scale > 38:
            raise ValueError("DecimalType supports at most 38 digits with 18 decimal places")

        return str(value) if dialect.name == "sqlite" else value

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, float):
            raise TypeError("DecimalType cannot load float values")
        return value if isinstance(value, Decimal) else Decimal(str(value))
