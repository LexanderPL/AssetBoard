# ADR-0006: UUID primary keys

## Context

Records may be created across local deployments and eventually synchronized or imported. Sequential
database identifiers expose insertion order and complicate merging records from different sources.

## Decision

Use UUID primary keys for persisted entities.

## Consequences

Identifiers can be generated without a central sequence and are suitable for future import and
sync workflows. UUIDs are larger than integer keys and should not be treated as authorization
secrets.
