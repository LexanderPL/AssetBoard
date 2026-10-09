from __future__ import annotations

from typing import Any, Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy.engine import Connection, make_url

import assetboard.db.models  # noqa: F401
from assetboard.db.base import Base
from assetboard.db.engine import create_database_engine
from assetboard.db.migrations import configured_database_url
from assetboard.db.types import DecimalType, UtcDateTime

config = context.config
target_metadata = Base.metadata


def render_item(
    item_type: str,
    item: Any,
    autogen_context: AutogenContext,
) -> str | Literal[False]:
    if item_type == "type" and isinstance(item, (DecimalType, UtcDateTime)):
        type_name = type(item).__name__
        autogen_context.imports.add(f"from assetboard.db.types import {type_name}")
        return f"{type_name}()"
    return False


def run_migrations_offline() -> None:
    database_url = configured_database_url(config)
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=make_url(database_url).get_backend_name() == "sqlite",
        user_module_prefix="assetboard.db.types.",
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_database_engine(configured_database_url(config))
    try:
        with engine.connect() as connection:
            configure_context(connection)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


def configure_context(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=connection.dialect.name == "sqlite",
        user_module_prefix="assetboard.db.types.",
        render_item=render_item,
    )


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
