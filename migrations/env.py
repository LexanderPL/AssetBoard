from __future__ import annotations

from alembic import context
from sqlalchemy.engine import Connection

from assetboard.db.base import Base
from assetboard.db.engine import create_database_engine
from assetboard.db.migrations import configured_database_url

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=configured_database_url(config),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
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
    )


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
