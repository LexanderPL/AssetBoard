# M4 — Ledger export and import (full workspace backup)

**Goal:** your ledger is never locked in. One documented, versioned archive format (a ZIP of
CSV files plus `manifest.json`) is used for both export and import: it is a full backup of a
workspace's ledger that restores with full fidelity, and its CSV files are simple enough that
you can convert your own spreadsheet history into them (by hand, with a script, or with an AI
assistant) and import that.

**Exit criteria:** you can export a workspace, restore the archive into an empty workspace and
get the same categories, transactions (deleted ones included), planned values, closed months
and settings back; you can import a hand-made `transactions.csv` (and `budget_lines.csv`) into
a workspace that is already in use, after a dry-run report with per-month and per-category
totals that you compare with your spreadsheet; every import can be rolled back, and repeating
an import creates nothing new.

Every agent prompt starts with: *"Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go)."*

**Out of scope (owner decisions, 2026-10-18)**
- No importer for a particular spreadsheet layout (no `openpyxl`, no sheet-name parsing, no
  synthetic workbook, no reconciliation against a workbook's cached cells, no work-log hints).
  Everyone's spreadsheet differs; users convert their data into the format below themselves.
  The repository contains no conversion script for anyone's workbook.
- No generic column-mapping importer for arbitrary CSV files and no bank-export conventions
  (negative amount = expense, no direction column, debit/credit columns): post-1.0, see
  [backlog-post-1.0.md](backlog-post-1.0.md#generic-csv-import-with-column-mapping).
- The app never calls an AI service or any other external service for import or export (§1 of
  the overview, AGENTS.md). The documented AI prompt (F4.5) is something the user may choose to
  use outside the app.

**Format decision:** [ADR-0016](../adr/0016-workspace-archive-format.md) records the archive
format and its versioning and compatibility policy. The [format reference](../archive-format.md)
at the end of this spec is normative for F4.1–F4.4; F4.5 turns it into user documentation.

**Prerequisite (backlog, from the F1.1 review):** before any upload route exists, add a pure-ASGI
middleware that caps the request body size (a small default for forms, a larger, explicit limit
only for the upload route), rejects oversized bodies with 413 without buffering them, and works
without a reverse proxy. It is part of F4.2. Until then the deployment guide asks operators to
cap body size at the proxy.

**Privacy rule:** no real personal data in the repository. Test archives and CSV files are
synthetic and built by test builders (committed golden archives under `tests/fixtures/` are
synthetic too).

**Data model (sketch)**
- `ImportBatch(id, workspace_id, source[archive|transactions_csv|budget_lines_csv|categories_csv],
  mode[restore|merge], filename, file_sha256, size_bytes, options JSON (delimiter, decimal
  separator, mapping choices and the remember flag; see F4.3),
  status[uploaded|planned|applied|rolled_back|cancelled|failed],
  started_by_user_id?, created_at, updated_at, applied_at?, rolled_back_at?, report JSON,
  previous_settings JSON?)`. `filename` is the uploaded name, sanitised and cut to 255
  characters; `previous_settings` holds the workspace settings a restore replaced (for its
  rollback). `UNIQUE(workspace_id, id)` is the target of composite foreign keys.
- F4.1 introduces the nullable `import_key` columns and their unique indexes for export lineage;
  F4.2 adds batch tracking and populates lineage during import.
- `Transaction.import_batch_id?`, `Transaction.import_key?`; `Category.import_batch_id?`,
  `Category.import_key?`; `BudgetLine.import_batch_id?` (composite foreign keys to
  `import_batches(workspace_id, id)`). `import_key` is unique per workspace when not null
  (partial unique index on SQLite and PostgreSQL) and is either `id:<uuid>` (the row came from a
  file row with an `id`: its lineage) or `c1:<sha256 hex>` (content key, see F4.2).
- `ImportCategoryMapping(id, workspace_id, kind, source_name_key, category_id?, action[map|ignore],
  created_at, updated_at)`; unique `(workspace_id, kind, source_name_key)`; composite foreign key
  to `categories(workspace_id, id)` with `ON DELETE CASCADE`. Saved mappings (F4.3).
- The uploaded file is kept, between upload and apply only, under
  `DATA_DIR/imports/<workspace_id>/<batch_id>` (mode 0600, outside any served directory), and
  deleted on apply, cancel or failure, or by an hourly job 24 hours after upload. The same job
  removes files in the workspace's folder that no `uploaded` or `planned` batch owns (left by a
  crash between commit and delete) once they are an hour old.

**Roles (decided here; see open question 1 for viewers)**

| Action | Viewer | Editor | Owner |
|---|---|---|---|
| Export the archive (F4.1) | no (open question) | yes | yes |
| Import a lone CSV or merge an archive (F4.3, F4.4) | no | yes | yes |
| Close past months after a merge import (F4.3) | no | yes | yes |
| Roll back a merge batch | no | yes | yes |
| Restore an archive into an empty workspace, incl. settings and closed months (F4.4) | no | no | yes |
| Roll back a restore | no | no | yes |

Roles are checked on the Context and again with `require_role_now` inside every write
(docs/architecture.md).

**Closed months (owner decision, 2026-10-18, closes the F2.5 open question):** rows (transactions
or planned values) dated in a month that is closed in the target workspace are **skipped and
listed** in the dry-run report; they never refuse the whole import. A restore into an empty
workspace recreates closed months itself, **after** all data is written (order in F4.4). A merge
import never closes months by itself, but its applied report offers an explicit action to close
the past months it filled (F4.3). Rolling back a batch that has rows in a month closed since then
is refused as a whole, listing those months (the same rule as F2.6's bulk recategorize); the
months must be reopened first.

---

## F4.1 Archive format and full export

**Story:** I download my whole workspace ledger as one documented file I can keep, read and
restore.

**Acceptance**
- Pure module `domain.archive` (in-memory bytes only, no DB, no clock): row types for every file
  of the [format reference](../archive-format.md), a writer (`rows -> CSV bytes`, `files ->
  manifest`) and a reader/validator (`bytes -> ParsedArchive | errors`) shared by F4.2–F4.4.
  Standard library only (`csv`, `zipfile`, `json`, `hashlib`, `io`); no new runtime dependency.
- Export service `services.archive.export_workspace(ctx)`: editors and owners; reads through the
  scoped repositories only; writes the files of the current format (1.1; `manifest.json`, `workspace.json`,
  `categories.csv`, `budget_lines.csv`, `transactions.csv`, `months.csv`) exactly as specified,
  including soft-deleted transactions (with `deleted_at`) and archived categories.
- **Deterministic output:** the same data and the same `exported_at` give byte-identical ZIP
  bytes (fixed file order, fixed row order, ZIP entry timestamps = `exported_at`, deflate, no
  extra fields). Tested against a committed golden archive built from a synthetic workspace.
- **Lineage ids:** the `id` column of `transactions.csv` and `categories.csv` is the row's origin
  id: the UUID in its `import_key` when that is `id:<uuid>`, else the row's own id. So export →
  restore elsewhere → export gives the same ids, and an archive merged back into its source
  workspace recognises every row (F4.2 dedupe).
- Manifest: `format`, `format_version`, `app` (name from `branding.APP_NAME`, version),
  `exported_at`, `base_currency`, `files` (path, SHA-256, rows, bytes). The reader verifies every
  checksum and row count and that `base_currency` equals `workspace.json`'s.
- Export page "Export and import" (`/workspace/data`, linked from the workspace menu for editors
  and owners): a "Download archive" button (POST with CSRF; the response is the ZIP, streamed
  from a temporary file, `Content-Disposition` file name
  `<branding.CLI_NAME>-<workspace-slug>-<YYYYMMDD-HHMMSS>Z.zip`, `Cache-Control: no-store`).
- Audit `workspace.exported` (counts per file, never amounts or text); group "Import and
  export". Nothing about the contents is logged.
- **Archive coverage test:** every workspace-owned model is either written to the archive or
  listed in `ARCHIVE_EXCLUDED_MODELS` with a reason (memberships, invitations, audit events,
  import batches, saved mappings, ...). Adding a workspace-owned model without deciding fails the
  test; this is how M3 and M5–M8 cannot forget their archive extension.
- **Cross-reference F2.6:** F2.6's filtered CSV export writes the `transactions.csv` columns in
  the same order (without `deleted_at`); with its default options it is a valid
  `transactions.csv`, and with the semicolon/decimal-comma options it imports with the matching
  upload options (F4.3).

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F4.1 from docs/specs/M4-ledger-export-import.md: the pure domain.archive module (row types, CSV/manifest writer,
reader/validator per the "Format reference v1.1" section and ADR-0016), the export service and the /workspace/data page
with the download button, lineage ids, deterministic ZIP output, the workspace.exported audit event and the archive
coverage test (ARCHIVE_EXCLUDED_MODELS). Standard library only. Tests: golden archive from a synthetic workspace,
byte-for-byte determinism, checksum/row-count verification, soft-deleted and archived rows exported, viewer refused.
```

---

## F4.2 Import framework: upload, limits, dry-run, batches, rollback, dedupe

**Story:** Every import is checked before anything is written, can be repeated safely, and can
be undone.

**Acceptance**
- **Body-size middleware** (prerequisite above): pure ASGI, counts bytes as they arrive, answers
  413 without buffering; the limit for every route is `ASSETBOARD_MAX_REQUEST_BYTES` (default
  1 MiB), for the upload route only `ASSETBOARD_IMPORT_MAX_UPLOAD_BYTES` (default 20 MiB). Both
  are documented in the deployment guide's environment table, and its proxy advice is updated
  (the proxy must allow the upload path's larger size).
- Upload (editors/owners; CSRF; multipart): accepts a `.zip` archive or a lone `.csv` (F4.3 says
  which). Content decides, not the extension: a ZIP must start with the local-file signature; a
  CSV must decode as UTF-8 (a leading BOM is removed; invalid UTF-8 is an error naming the line).
  The file is stored as described in the data model; an `ImportBatch` (`uploaded`) records its
  SHA-256 and size.
- **ZIP safety:** the archive is never extracted to disk; members are read in memory through a
  counting reader that stops at the limit (header sizes are not trusted). Refused, with a clear
  error: members in folders or with `/`, `\`, `..`, absolute or drive-letter names; symlinks or
  other non-regular entries; encrypted members; methods other than stored and deflate; duplicate
  names; more than 64 members; a member over 100 MiB or a total over 200 MiB uncompressed; a
  member with a compression ratio above 200:1 (members under 1 MiB exempt). Unknown member names
  are listed as warnings and ignored.
- **Parse and plan in a worker thread with a time limit** (60 s, a code constant): parsing is
  pure (`domain.archive`), planning reads the workspace through services and writes nothing.
  Over the limit the batch is `failed` with a message. At most 1 000 000 data rows per upload.
  The limit counts from when the worker starts the job (two workers; a job that cannot start
  within 60 s fails as "busy"). The worker passes an `out_of_time` callback to the CSV reader
  (checked every 1 000 rows) and to the adapter's `plan`, so a job over the limit stops itself;
  the archive reader is bounded by its size limits instead.
- **Dry-run report** shown before anything is written, stored in `ImportBatch.report`, status
  `planned`: counts (to create, duplicates skipped, conflicts skipped, skipped in closed months,
  skipped for ignored categories, invalid); a **per-month table** (month; expense total, income
  total, planned total; rows to create; rows skipped) and **per-category totals** per kind of
  what will be imported, so the user compares them with their own spreadsheet (this replaces
  the old workbook reconciliation); the closed months with their skipped rows; warnings (future
  dates, unknown columns, unknown files, settings that differ in merge mode); errors with file,
  line and column. Errors and skipped rows are downloadable as CSV
  (`file,line,column,code,message`). Every report entry carries a stable `code`; the page and
  the CSV translate known codes and otherwise show the entry's own message.
- **Invalid rows:** in merge mode the user can apply the valid rows only after ticking "Skip N
  invalid rows"; in restore mode any error blocks the apply (a restore must be complete).
- **Apply** re-parses the stored file and re-plans in the write transaction (the workspace may
  have changed since the dry-run); if the counts differ from the confirmed report, nothing is
  written and the new report is shown for confirmation again. Apply runs in one transaction
  (`run_immediate`, `require_role_now`, then the exclusive workspace lock, which also covers
  `lock_month_writes`), so a batch is all or nothing. Imported transactions get
  `source=import`, `import_batch_id` and `import_key`; amounts use the currency read from the
  workspace row inside the transaction.
- **Dedupe (idempotent re-import):**
  - A row with an `id`: it is a duplicate when the workspace has a transaction (or category)
    with that id or with `import_key = id:<id>`; if its values differ, it is a **conflict**,
    skipped and listed ("changed since the export").
  - A row without an `id`: content key `c1:` + SHA-256 of `date`, `direction`, amount in minor
    units, currency, source category name key (as in the file, before mapping), description key
    (F2.2's `description_key`) and the **occurrence number** of that same tuple within the file
    (1, 2, ...), joined with U+001F. Importing the same file twice creates nothing the second
    time, while two genuinely identical rows in one file (two coffees, same day, same price) are
    both imported; a later file that repeats a month with one more identical row adds only that
    row.
  - Soft-deleted rows keep their keys: a row deleted after an import is not brought back by
    re-importing; it is reported as "imported before and deleted".
  - Budget lines dedupe on `(month, category)`: same amount = duplicate, different amount =
    conflict (the stored value is kept, listed). Categories: see F4.3.
  - The same `id` twice in one file, or the same `(month, category)` twice: the later row is a
    duplicate (or, for a different planned value, a conflict) of the earlier one.
  - These rules are shared helpers (`services.import_dedupe`) that F4.3 and F4.4's adapters
    call; a deleted row is "imported before and deleted" whatever its values.
- **Rollback** of an applied merge batch (editors/owners; confirm page with counts): soft-deletes
  its transactions (edited ones too; the confirm page says how many were edited), removes its
  budget lines (hard delete, as F2.3 does) except lines changed after the import
  (`updated_at > applied_at`, kept and listed), and removes the categories it created through
  F2.1's rule (delete when unused, else archive). Refused as a whole when any affected month is
  closed now (the months are listed). Restore rollback: F4.4.
  F2.1 counts soft-deleted transactions as references (they keep pointing at their category),
  so a created category whose transactions this rollback soft-deletes is archived; it is
  deleted only when nothing else references it (for example, it held only removed budget lines).
  Rollback works from `import_batch_id` for every source, in the framework (not per adapter).
- **Cancel** of a planned batch deletes the stored file (`cancelled`). `/workspace/data` lists
  recent batches with status, counts and actions.
- Audit: `import.applied` and `import.rolled_back` (batch id, source, mode, counts; never
  amounts, descriptions or the file name); group "Import and export". No per-row audit events
  for imports (the rows carry `import_batch_id`). Logs never contain row contents.
- `ImportBatch` and `ImportCategoryMapping` follow docs/architecture.md (repository, tenant
  factory, coverage entries); the new columns get migrations; `alembic check` is clean.
  `ImportCategoryMapping` arrives with F4.3, which first uses it (deferred from F4.2 as agreed).
- **Adapters:** the framework calls the adapter registered for the batch's `source`
  (`ImportAdapterRegistry`, `create_app(import_adapters=...)`); F4.3 and F4.4 register theirs.
  Until one is registered an upload is checked and reported but cannot be applied.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F4.2 from docs/specs/M4-ledger-export-import.md: the pure-ASGI body-size middleware with its two settings,
ImportBatch (+ import_batch_id/import_key columns) with migrations and repositories, upload with the stored file and its
cleanup job, ZIP safety checks, parse/plan in a worker thread with a time limit, the dry-run report (per-month and
per-category totals, closed-month skips, errors CSV), apply in one transaction with re-plan, dedupe (id lineage and
content key with occurrence number), rollback and cancel. Only a trivial importer for tests here; the real importers are
F4.3 and F4.4. Tests: 413 without buffering, every ZIP refusal (zip bomb, traversal, symlink, duplicates), idempotent
re-import, identical rows in one file both imported, rollback incl. the closed-month refusal, tenant isolation.
```

---

## F4.3 Transactions, planned values and categories from CSV, with category mapping

**Story:** I bring my history in: I upload a `transactions.csv` (and `budget_lines.csv`) that I
made from my spreadsheet, map its categories onto mine, check the totals and import it.

**Acceptance**
- Lone files accepted (merge mode, editors/owners): `transactions.csv`, `budget_lines.csv`,
  `categories.csv`. The kind of file is recognised from its header, not its name; `months.csv`
  and `workspace.json` alone are refused ("only part of a full restore"). A ZIP without a
  manifest is refused (the manifest is what makes it an archive).
- Upload options for lone CSV files: delimiter **Detect** (default) / comma / semicolon, and
  decimal separator dot (default) / comma; decimal comma is offered only with semicolon.
  Detection splits the header line on `,` and `;` and picks the one that yields only known
  column names. Files inside an archive always use the canonical format; the options do not
  apply.
- Header names are matched trimmed and case-insensitively; a missing required column is an
  error; unknown columns are a warning and ignored. Values as in the
  [format reference](../archive-format.md): amounts are converted with the currency's minor
  digits and refused (never rounded) when they have too many decimals, are not positive or are
  too large (F2.2's codes); dates must be `YYYY-MM-DD` within 1900-01-01..2199-12-31; a
  `currency`, when present, must equal the workspace's base currency (row error otherwise);
  descriptions follow F2.2's rules (at most 200 characters, whitespace collapsed, control
  characters refused).
- **Category mapping step** between the dry-run and apply: the source categories (kind + name,
  with row counts and totals) → **map** to an existing category of that kind (active or
  archived: history may belong to a retired category), **create** a new one (name, treatment,
  colour), or **ignore** (its rows are skipped and listed). Defaults: a saved mapping; else the
  same name (F2.1 name key) among active categories of that kind; else "create", proposing
  `treatment=savings` for names in a savings word list (data file, en + pl: savings,
  investments, oszczędności, inwestycje, ...). A new category takes the next unused palette
  colour for its kind, in source order. The form can re-run the dry-run with the chosen mappings
  without writing anything (**Update preview**); the choices are kept in the batch's options.
  "Remember these mappings" saves them as `ImportCategoryMapping`, only when an apply succeeds
  and in its transaction. Apply is blocked until every source category is resolved: a missing or
  wrong-kind target, an empty or invalid create name, a create name that already exists, or two
  creates with the same kind + name key is a `mapping_invalid` error, shown on the page and
  refused again at apply. `categories.csv` has no mapping step.
- **Closing past imported months (post-apply action):** the report of an applied merge batch
  (a lone CSV or an archive merge; never a restore) offers **Close past imported months**
  (editors and owners; CSRF). In one transaction it re-checks the role, takes the workspace lock
  and `lock_month_writes`, and closes every month **before the current workspace month** that
  received at least one non-deleted transaction or budget line from the batch and is not closed
  already. Already closed months are left untouched; the current and future months are never
  closed. Each close sets `closed_at = clock.now()` and `closed_by_user_id`, and records the
  normal `month.closed` audit event. The result is reported as a notice ("closed N months" or
  "no past months needed closing"). A batch that is not an applied merge is refused. Rollback
  semantics do not change: a batch whose months are now closed is still refused and lists them,
  and an owner must reopen them first.
- Lone files map categories **by name**; a `category_id` in the file is ignored (only a full
  restore uses ids).
- Direction must equal the category's kind (mapping is per kind, so it does). Rows of
  `transfer` categories import normally.
- `budget_lines.csv` in merge mode: rows with `planned` zero or empty are skipped silently (zero
  means "no line", as in F2.3); a line for an archived or `transfer` category is skipped and
  listed (F2.3 takes no new lines there); a month that receives a line gets `plan_started_at`
  if it has none (the import counts as the month's first plan write, so F2.3's automatic copy
  never adds to it later).
- `categories.csv` alone (merge): creates the categories that do not resolve (by lineage id, or
  by name key and kind among active ones), appended at the end of their list in file order,
  archived ones as archived; existing categories are never changed (differences listed). Rows
  with an `id` dedupe by id; name-based in-file duplicate detection applies only among active
  rows, so an active and an archived category may share a name. An archived row without an id
  dedupes by kind and name among archived id-less rows and against existing archived categories;
  it is created only when no archived same-name category matches. An explicit `sort_order` in the
  file does not move existing categories or place new ones: merge mode always appends.
- `transactions.csv` rows with a non-empty `deleted_at` are skipped in merge mode and counted
  ("deleted in the source"); only a restore recreates deleted rows.
- A transaction dated after today in the workspace's time zone is skipped and listed with code
  `future_date` (in the app a future entry becomes a one-off planned item, so importing it as
  an actual entry would contradict that); budget lines for future months are imported normally.
- A rollback removes the saved mappings that point at categories the batch created (archived
  ones included), in the same transaction; mappings to pre-existing categories stay. It also
  clears `plan_started_at` for months whose marker this batch set, when no planned values remain
  in that month.
- The page offers **template downloads**: `transactions.csv` is the hand-made example below
  (header and its three rows); `budget_lines.csv` is the minimal `month,kind,category,planned`
  example below (header and its two rows).
- Messages and report labels are translatable (en, pl); column names and enum values
  (`expense`, `income`, `regular`, ...) are format tokens and stay English in every language.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F4.3 from docs/specs/M4-ledger-export-import.md on the F4.2 framework: lone transactions.csv, budget_lines.csv
and categories.csv import in merge mode, delimiter detection and the semicolon/decimal-comma options, value validation
reusing domain.transactions and domain.budget, the category mapping step with saved mappings and the en/pl savings word
list, closed-month skips, plan_started_at on imported months, template downloads.
Tests (synthetic files only): every validation code, BOM and CRLF, semicolon + decimal comma, mapping defaults and saved
mappings, ignored categories, archived/transfer budget lines skipped, idempotent re-import of a hand-made file.
```

---

## F4.4 Full restore into an empty workspace, and archive merge

**Story:** After a reinstall (or to move to another instance) I restore my workspace from its
archive and everything is back; I can also merge an archive's data into a workspace I already
use.

**Acceptance**
- An archive upload offers two modes:
  - **Restore** (owners only), only when the target workspace is **empty**: no categories of
    its own, no transactions (deleted included), no budget lines, no month states, and no M3
    data (recurring items and occurrences, income sources, work logs/days, tax rules and their
    versions and links). A workspace whose only categories are the untouched **automatic
    suggested set** counts as empty: the restore deletes those categories before writing the
    archive's and lists them in its report. "Untouched" means the exact suggested set for a
    supported language, created at the automatic `category.seeded` event's time, never edited,
    archived or referenced by any row. The check is a static list
    `services.archive.EMPTINESS_CHECKS` that later milestones extend (like
    `BASE_CURRENCY_LOCKS`). Members, invitations, audit events, notification channels and saved
    import mappings do not count.
  - **Merge** (editors and owners): data only (categories, transactions, budget lines) through
    the F4.3 rules and mapping step. The mapping table has one row per archive category, keyed
    by its archive id, because an archive may contain an active and an archived category with
    one name. Defaults, in order: explicit choice; lineage match (the archive category id is a
    workspace category id or `import_key = id:<id>`); saved mapping by kind and name (active
    archive categories only); active same-name category (active archive categories only); else
    create with the archive's attributes, archived state included. Merging an archive into its
    source workspace therefore creates nothing. `workspace.json` and `months.csv` are **not**
    applied; differing settings and the archive's closed months are listed as information. A
    `base_currency` different from the workspace's refuses the whole archive even when invalid
    rows could otherwise be skipped.
- **Restore order, in one transaction:** (1) settings from `workspace.json` (name, base
  currency, country, time zone, locale, automatic plan copy, and from 1.1 the Taxes switch and
  the Upcoming window, which keep their defaults when a 1.0 archive lacks them; the replaced
  values are kept in `ImportBatch.previous_settings`); settings are validated with the normal
  workspace validators and an unsupported value refuses the restore; `require_two_factor` is
  not archived and stays as it is; (2) categories with every attribute (`kind`, `treatment`,
  `color`, `sort_order`, `archived_at`) and `import_key = id:<id>`; (3) budget lines (lines on
  archived or transfer categories included: they existed in the source); a zero or empty
  `planned` is "no line" and is not an error; (4) transactions in file order (so entry order
  within a day is kept), with `import_key = id:<id>` when the row has an id, strictly
  increasing `created_at` values in file order, and soft-deleted ones with their `deleted_at`;
  (5) month states last, exactly as in `months.csv` (`plan_started_at`, `closed_at`; nothing is
  derived from the budget lines). No mapping step, no skipped rows: any error blocks the
  restore. The restore flushes all rows before `applied_at` is recorded, so its own timestamps
  never make a rollback look stale.
- Restored closed months get `closed_by_user_id` = the restoring owner (users are not in the
  archive; see open question 4). Restored data is not audited per row; a restore records one
  `workspace.restored` audit event (batch id, counts, names of the settings fields changed and
  suggested categories removed) and suppresses the generic `import.applied` event. The restore
  does not post recurring items, send notifications or trigger the automatic plan copy; month
  states are written last exactly as stored.
- **Restore rollback** (owners): allowed only while the workspace still holds exactly what the
  restore wrote. The check refuses with reasons when: a row exists without the batch's id or a
  batch row is missing; a batch row's `updated_at > applied_at`; the stored month-state set
  differs from the archive's or a month state changed after `applied_at`; the workspace row
  changed after `applied_at` ("workspace settings changed since the restore"); any M3 row was
  created after `applied_at`; or any M3 row references a batch category or transaction (the M3
  foreign keys would block the hard delete). It then **hard-deletes** the batch's rows and
  month states and puts `previous_settings` back, so the workspace is empty again and a
  corrected archive can be restored. A restore rollback leaves a truly empty workspace and
  does not re-add the suggested categories; the user can press "Add suggested categories"
  afterwards. (Hard delete is justified: the rows never existed in this workspace before, and
  soft-deleted leftovers would keep the workspace non-empty and its base currency locked.)
- Version handling per ADR-0016: an archive of a newer **major** format version is refused
  ("made by a newer version of the app"); a newer **minor** version imports with a warning
  listing the ignored files and columns; older majors pass through the reader's upgrade
  functions (none exist for 1.x).
- Round-trip test: synthetic workspace → export → restore into an empty workspace in the same
  database → export again gives byte-identical data files (`manifest.json` differs only in
  `exported_at` and the app version).

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F4.4 from docs/specs/M4-ledger-export-import.md: archive upload with restore and merge modes, the emptiness check
(EMPTINESS_CHECKS), restore in the specified order incl. settings, archived categories, soft-deleted transactions,
plan_started_at and closed months applied last, restore rollback with its "unchanged since" check and previous_settings,
merge mode reusing F4.3 with settings/closed months reported but not applied, format version handling per ADR-0016.
Tests: the round-trip test, restore refused into a non-empty workspace and for editors, merging an archive into its
source workspace creates nothing, base-currency mismatch refused, newer major refused, restore rollback allowed/refused.
```

---

## F4.5 Documentation: format reference and AI conversion guide

**Story:** I can convert my own spreadsheet into the format without reading code, optionally
with help from an AI assistant, and I understand what that means for my privacy.

**Acceptance**
- `docs/archive-format.md`: the format reference (moved there from this spec), kept in sync with `domain.archive` (a
  test parses its example rows with the reader and checks that every column the writer emits is
  documented).
- `docs/import-export.md`: the user guide: exporting; restoring (when it is possible and what it
  restores); merging; importing a lone CSV; the dry-run report and comparing its totals with a
  spreadsheet; category mapping; closed months; rollback; limits; and "Converting a
  spreadsheet" with the ready-to-copy prompt (moved there from this spec) and the privacy note.
- README "User guides" lists both; docs/workspace-settings.md links the export page;
  docs/deployment.md documents the two body-size settings (F4.2).
- The privacy note is prominent and says: the app never sends your data anywhere; pasting
  financial data into a third-party AI service is your own choice and subject to that service's
  terms; consider a local model, or remove descriptions or other details first; check the result
  with the dry-run totals.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F4.5 from docs/specs/M4-ledger-export-import.md: docs/archive-format.md and docs/import-export.md from the
"Format reference v1.1" and "AI conversion prompt" sections, README and related doc links, and a test that parses the
documented example rows with domain.archive and checks that every written column is documented.
```

Archive extensions land in later milestones as minor versions per ADR-0016. As of M8 F8.6 the
current format is 1.7: it adds the four FIRE files (`fire_scenarios.csv`,
`fire_contributions.csv`, `fire_events.csv` and `fire_expense_exclusions.csv`) with lineage ids,
restore-only merge handling and linked-input fallbacks; 1.6 archives read without them. The
normative column reference and version history stay in
[docs/archive-format.md](../archive-format.md).

---

## Format reference v1.1

**Moved (F4.5):** the normative format reference is [docs/archive-format.md](../archive-format.md):
archive layout, common rules, versions and compatibility, `manifest.json`, `workspace.json`,
every CSV file with its columns and examples, reader limits, and what is not in the archive.
It changes per ADR-0016, in the same PR as `domain.archive`; `tests/unit/test_archive_format_docs.py`
reads its examples with the reader and checks that every column and key the writer emits is
documented.

---

## AI conversion prompt

**Moved (F4.5):** the ready-to-copy prompt and the privacy note are in
[docs/import-export.md](../import-export.md#converting-a-spreadsheet). The app itself never
calls an AI or any external service.

---

## Open questions (for the owner)

1. **Viewer export.** Proposed here: owners and editors only. Viewers already see every amount
   on screen, and "your data is yours" argues for letting them export too; but an archive makes
   bulk copying trivial for someone given read-only access. Allow viewers?
2. **Soft-deleted transactions in the archive.** Decided here: included (with `deleted_at`) for
   restore fidelity: they lock the base currency and keep categories "in use" (F2.1, F2.2), and
   a later trash view can restore them. Should the export offer "leave out deleted entries" (such
   an archive then restores with slightly different behaviour)?
3. **Attachments** (receipts, documents), if they ever exist: in the ZIP under an
   `attachments/` folder (an exception to the no-folders rule, larger limits), or outside the
   archive?
4. **Who closed a month after a restore.** Decided here: the restoring owner (users are not in
   the archive). Alternative: leave it empty, shown as "by a former member".
5. **Restore into a new workspace.** Existing users cannot create another workspace yet, so a
   restore needs an empty workspace (a fresh install or a new registration). Add "Create a
   workspace from an archive" (proposed for the post-1.0 backlog)?
6. **CLI.** Should `assetboard workspace export|import` exist (for scheduled off-site exports),
   or is F9.4's database backup enough for automation?
7. **CSV formula injection in F2.6's spreadsheet-oriented export.** Decided (owner,
   2026-10-19; see [F2.6](M2-ledger-core.md#f26-transaction-search-and-csv-export)): F2.6
   offers the protection as an opt-in checkbox, off by default, so its default export stays
   lossless and re-importable. The archive never prefixes values starting with `=`, `+`, `-`
   or `@`.
8. **Rollback of rows edited after the import.** Decided here: a merge rollback soft-deletes
   every transaction of the batch, edited ones included (the confirm page says how many were
   edited). Keep edited ones instead?
9. **Order of M3 and M4.** M4 now depends only on M2 and could ship before M3; if M3 ships first,
   its archive extension (F3.8) must land before M4 counts as a full backup.
