# ADR-0018: Structured recurrence and in-process generation

**Status:** Proposed
**Date:** 2026-10-20

## Context

F3.1 needs calendar-based planned occurrences, independent from posted transactions. Rules must
clamp short months, adjust for holidays, survive edits without rewriting history, and remain
isolated by workspace. The application already requires one worker and supports SQLite and
PostgreSQL. ADR number 0017 is reserved by the pending F1.6 contribution (#127).

## Decision

- Store structured recurrence fields and implement the small weekly/monthly/yearly engine with
  standard-library calendar/date operations. Using `dateutil.rrule` directly would skip invalid
  dates rather than provide the required clamping. `python-dateutil` is only a transitive
  dependency of holidays, not a second recurrence engine.
- Count nominal scheduled dates before shifting and deduplicating them. Start/end constrain
  nominal dates; the query window constrains effective dates, inclusively. Intervals are anchored
  to the start period; weekly periods begin on Monday. Keep the existing 1900–2199 app date range.
- Obtain national/observed holiday dates locally through `holidays`; keep the domain pure by
  passing a holiday container. No country means weekends only; unsupported countries fail when
  working-day calculations are requested. Subdivisions are outside F3.1.
- Separate `ScheduledItem` and `ScheduledOccurrence` tables, both workspace-scoped with composite
  category/item foreign keys. Add only the fields consumed by F3.1: income/tax references,
  transaction links, reminder lead times and auto-post settings arrive with their features.
- Serialize generation and rule changes with the exclusive workspace lock (`BEGIN IMMEDIATE`
  on SQLite, `FOR UPDATE` on PostgreSQL), plus the unique item/due-date constraint. Every
  generator run reconciles: it deletes stale pending rows from today onward and adds missing
  ones, so the stored dates always match the current rule and holiday calendar (a country or
  holidays-data change never duplicates). Older pending rows, posted and skipped rows are
  preserved. The same lock orders category state/kind and currency changes against new
  scheduled references, and month close/reopen (F2.5) against generation.
- Rows in a closed month (F2.5) are never created, deleted or altered: backfill, regeneration,
  edits, pause and archive all skip them.
- Items whose category is archived get no new rows; the next run removes their pending rows
  from today onward. The edit form shows the archived category (disabled) instead of silently
  choosing another.
- Pausing removes pending dates from today onward. Archiving and deleting remove all pending
  dates (overdue ones too); only posted/skipped rows, and rows kept because their month is
  closed, are history. Deletion is physical when no history remains. Resuming starts from
  today. Past generation is explicit at creation only.
- A workspace country change regenerates working-day items in the same transaction. A change
  to a country without `holidays` data is refused while non-archived working-day items exist
  (simpler than a second, weekends-only meaning for a set country).
- One failing item does not stop a workspace run: each item runs in a savepoint and failures
  are logged with identifiers, the exception type and traceback (no amounts in the message).
- Use APScheduler 3's in-memory background scheduler owned by the application lifespan. Rebuild
  per-workspace 03:00 cron jobs and generate the forward horizon in a catch-up job that runs
  immediately in the background (startup does not wait for it), and reconcile new
  workspaces/time zones every minute. `ASSETBOARD_SCHEDULER_ENABLED` (default `true`) turns the
  scheduler off; the test suite does so. Startup logs a warning when `WEB_CONCURRENCY` asks
  for several workers. Shutdown waits before disposing the database engines. Trusted generation receives an explicit workspace scope and injected Clock;
  it does not impersonate an owner or depend on whether an owner is currently active.

## Consequences

The default horizon is 13 calendar months, as required by the feature, and is configurable with
`RECURRENCE_HORIZON_MONTHS`. A future workflow can consume pending rows without changing how the
recurrence engine works. Posting and reminders remain outside this change. Changing the country
or time zone affects subsequent generation; editing/saving a rule refreshes its future pending
rows under the current settings.

New runtime dependencies are **APScheduler** (MIT), the maintained in-process scheduler selected
by the stack, and **holidays** (MIT), the maintained national-calendar provider requested by the
specification. `holidays` works offline (its rules ship with the package) and is the largest
addition: about 7 MB of installed sources, about 13 MB once bytecode is compiled (0.107). The
standard library does not provide either national holiday rules or an in-process
time-zone-aware job scheduler. Both licenses are compatible with AGPL-3.0-or-later.
`python-dateutil` (Apache-2.0/BSD) is a direct test dependency only: the recurrence engine is
checked differentially against `dateutil.rrule` as an independent oracle. It was already
installed as a dependency of `holidays`. The scheduler adapter contains the third-party integration;
service/domain modules continue to pass strict type checking.

## Planned follow-ups (no schema change in F3.1)

- **F3.4:** `ScheduledOccurrence` gains a nullable `income_source_id` next to a then nullable
  `scheduled_item_id`, with a check that exactly one of them is set.
- **F3.2:** `generated_by` (rule or manual) and `transaction_id` (the posted transaction) on
  `ScheduledOccurrence`.
- **F3.2:** a nominal-date key for "reschedule one": the unique key moves from the effective
  `due_date` to the nominal date, so a rescheduled or posted row keeps its identity when the
  holiday calendar shifts the effective date. Until then a posted/skipped row (or a closed-month
  row) from today onward can sit next to a pending row for the same nominal date when the
  calendar later moves that date.

References: [APScheduler guide](https://apscheduler.readthedocs.io/en/3.x/userguide.html),
[holidays API](https://holidays.readthedocs.io/en/latest/api/).
