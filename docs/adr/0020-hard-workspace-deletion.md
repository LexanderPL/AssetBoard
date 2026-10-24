# ADR-0020: Hard workspace deletion

**Status:** Accepted
**Date:** 2026-10-24

## Context

Owners could create workspaces (pre-release A) but not remove them. Deletion was in the
post-1.0 backlog; the owner pulled it forward. Everything else in the data model leans the
other way: financial rows are soft-deleted (overview §3.9), audit events are append-only
(enforced at flush), and every workspace-owned table is reached only through a scoped
repository (ADR-0003). Foreign keys between workspace-owned tables are composite
(`(workspace_id, parent_id)`), and `user_sessions.current_workspace_id` points at the
selected workspace. The code must stay portable to PostgreSQL, so `ON DELETE CASCADE`,
SQLite's foreign-key pragma or `ON DELETE SET NULL` cannot be what makes deletion correct.

## Decision

- **Hard delete, owners only, confirmed by the exact name.** `services.workspaces.
  delete_workspace` checks `require_role(owner)` on the Context, then inside one
  `BEGIN IMMEDIATE` transaction re-reads the actor's membership (`require_role_now`), takes
  the exclusive workspace lock, compares the typed name with the stored name (surrounding
  spaces ignored, case sensitive), and deletes. The web form is a CSRF-protected POST on a
  separate confirmation page linked from the settings page's danger zone; it recommends the
  transaction CSV export (F2.6) first.
- **What goes is found from the metadata.** `CurrentWorkspaceRepository.delete_with_all_data`
  takes every table with a `workspace_id` column, in the reverse of SQLAlchemy's dependency
  order (children first, so composite foreign keys hold after every statement on any
  database), and issues one `DELETE ... WHERE workspace_id = :id` per table. Other columns
  referencing `workspaces.id` are set to NULL explicitly (today only
  `user_sessions.current_workspace_id`; `updated_at` would be set too where a table has one).
  A non-nullable reference that is not a `workspace_id` makes it fail loudly. Then the
  workspace row is deleted. A new workspace-owned model is therefore deleted without code
  changes, and a test over every discovered model proves it (`tests/integration/
  test_tenant_isolation.py`).
- **Core statements, not ORM deletes.** The deletes are plain SQL statements built by
  SQLAlchemy, always filtered by the workspace id. They bypass the append-only flush guard on
  purpose for the deleted workspace's audit events, and the timestamp listener (no timestamped
  row is updated except as stated above). This is the only place that removes audit events.
- **The workspace's audit log is deleted with it.** Its events carry financial details
  (amounts, category names, dates) that the owner asked to remove. One instance-level event
  (`workspace_id` NULL) `workspace.delete` keeps the workspace id (`entity_id`), its name and
  the actor; no amounts and no counts. Instance administrators see it in the instance audit
  view under "Deleted workspaces".
- **Sessions fall back.** Clearing `current_workspace_id` makes each affected session use the
  oldest remaining membership (F1.2 rule) or, with none, the "No workspace" page, which offers
  "Create workspace" while creation is allowed. A session that still carries the old id in
  memory falls back the same way, because the Context checks membership on every request.
- **The last workspace may be deleted.** Nothing requires an instance to have a workspace.

## Consequences

- Deletion is irreversible; the app keeps no copy. The full archive export (M4) does not exist
  yet, so the only export offered is the transactions CSV; categories, planned values and
  recurring items are not covered by it.
- A request that built its Context before the deletion fails cleanly afterwards: reads find
  nothing (404), writes fail the role re-check (403), and PostgreSQL rejects inserts into the
  deleted workspace through the foreign keys. The recurring-items scheduler drops the
  workspace's job at its next refresh (within a minute); a run in between fails its lock and is
  logged.
- A future table that references `workspaces.id` without a `workspace_id` column must be
  nullable (or the deletion must be extended); the refusal and the reference test catch it.
- Leaving a workspace and ownership transfer remain in the post-1.0 backlog.
