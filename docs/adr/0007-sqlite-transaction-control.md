# ADR 0007: SQLite Transaction Control

## Context

Python's sqlite3 driver historically starts transactions implicitly and may omit a transaction for DDL. This makes transactional DDL and SQLAlchemy savepoints unreliable, including for migrations and rollback-based tests.

## Decision

Disable sqlite3's implicit transaction management on connect by setting `isolation_level` to `None`. Use SQLAlchemy's `begin` event to emit an explicit `BEGIN`. Apply SQLite pragmas when each DBAPI connection is created.

## Consequences

Schema changes and nested transactions use consistent SQLAlchemy transaction boundaries. The engine owns transaction starts, so all SQLite connections must be created through `create_database_engine`.
