# Workspace export and import

Owners and editors can open **Export and import** in the workspace menu.

## Downloading an archive

Select **Download archive**. The ZIP contains the current workspace's settings (including the
Taxes switch and the Upcoming window), all categories (including archived categories),
transactions (including deleted entries), planned values, and stored month states (closed
months and plan-start markers). Recurring rules, occurrences, income sources, work
logs/calendars, and tax rules await the F3.8 archive extension. Notification channels and
delivery logs are excluded; channel URLs can contain secrets.

The download is a copy: exporting does not change the ledger. The audit log records the export
and counts of each kind of row, without descriptions or amounts. Viewers cannot download a full
archive.

Keep the ZIP intact for a future restore. The archive is unencrypted and contains financial
history, including deleted entries. Store it privately. CSV text is preserved exactly,
including values beginning with spreadsheet formula characters; inspect it as text or disable
formula execution when opening it in a spreadsheet.

The archive contains `manifest.json`, `workspace.json`, `categories.csv`, `budget_lines.csv`,
`transactions.csv`, and `months.csv`. The manifest identifies format version 1.1 and records
SHA-256 checksums, byte sizes, and CSV row counts. CSV uses UTF-8, comma separators, ISO dates,
and exact decimal amounts. Members, invitations, audit events, and user accounts are excluded.
See the [normative format reference](specs/M4-ledger-export-import.md#format-reference-v11) for
columns, ordering, and compatibility rules.

Origin IDs are preserved when exporting previously imported categories and transactions.
Exports with identical ledger data, application version, and export timestamp have identical
bytes. ZIP member timestamps use UTC with the ZIP format's two-second resolution; the manifest
preserves the full export timestamp.

## Restoring an archive

Restore is for a reinstall or a move to another instance. It is available to owners only, and
only while the workspace has no data of its own. A workspace whose only categories are the
untouched **suggested set** still counts as empty: the restore removes those categories and
replaces them with the archive's. If you renamed, edited, archived, or used a suggested
category, the workspace is no longer empty. M3 data (recurring items, income sources, work
logs/calendars, tax rules) also makes the workspace non-empty, because archive format 1.1
does not carry it yet.

Restore writes, in one transaction:

1. the workspace settings from `workspace.json` (name, base currency, country, time zone,
   language, automatic plan copy, Taxes switch and Upcoming window); `require_two_factor` is
   not part of the archive and stays as it is;
2. categories with their archived state and order;
3. planned values, including those of archived and transfer categories;
4. transactions in file order, including deleted entries and their entry order;
5. month states exactly as in `months.csv` (closed months and plan-start markers) last.

Any validation error blocks the whole restore. A newer major format version is refused; a newer
minor version imports with a warning listing what was ignored. Restoring never posts recurring
items or sends notifications, and the stored month markers stop the automatic plan copy from
filling a restored month.

The restore report lists the rows it will write, the suggested categories it will remove, the
settings it will replace, and the months it will close. **Roll back import** on an applied
restore hard-deletes everything it wrote and puts the previous workspace settings back, but only
while nothing in the workspace changed after the restore. A rolled-back restore leaves a truly
empty workspace: it does not re-add the suggested categories. Use **Add suggested categories**
on the Categories page if you want them.

## Merging an archive

Merge is for bringing an archive's ledger data into a workspace you already use. Editors and
owners can merge. It writes categories, transactions and planned values only; `workspace.json`
and `months.csv` are reported but not applied. The archive's base currency must match the
workspace's, otherwise the whole archive is refused.

The mapping step has one row per archive category, so an active and an archived category with the
same name stay separate. The defaults are: an explicit choice; a lineage match (the archive
category id already exists here); a saved mapping for an active archive category; an active
category with the same name; otherwise create a category with the archive's attributes,
archived state included. Categories created by a merge are appended, and merging an archive back
into the workspace it came from creates nothing.

Merge follows the same skip rules as a hand-made CSV: rows in closed months, future-dated
transactions, deleted transactions and planned values of archived or transfer categories are
skipped and listed. A merge batch can be rolled back like any other import.

## Importing a hand-made CSV

A single CSV file can be imported into a workspace that is already in use. The app recognises
the file kind from its header, not from its filename:

- `transactions.csv`: `date,direction,amount,category,description` (the other archive columns
  are optional and `category_id` is ignored);
- `budget_lines.csv`: `month,kind,category,planned` (`currency` and `category_id` are optional
  and `category_id` is ignored);
- `categories.csv`: `kind,name` (the other archive columns are optional).

Download the template for transactions or planned values from the import form. The templates
use the minimal hand-made examples from the format reference. Only a full archive restore can
recreate `months.csv` or `workspace.json`; uploading one on its own is refused as a partial
restore.

### Upload options

- **Delimiter:** Detect (default), comma, or semicolon. Detection splits the header on `,` and
  `;` and chooses the separator whose names are known columns.
- **Decimal separator:** dot (default) or comma. A decimal comma is available only with the
  semicolon delimiter.

Header names are matched trimmed and case-insensitively. Unknown columns produce a warning and
are ignored; a missing required column refuses the upload. Amounts are never rounded: too many
decimal places, zero or negative transaction amounts, and amounts that are too large are row
errors. A `currency` column, when present, must equal the workspace's base currency.

### Review, map, and apply

Uploading validates the file and shows a dry-run report before anything is written. The report
contains the rows to create, duplicates, conflicts, closed-month and future-date skips, invalid
rows, totals by month, and totals by category.

Transactions and planned values have a category mapping step:

- **Map** a source category to an existing category of the same kind (active or archived).
- **Create** a new category with a name, treatment, and colour.
- **Ignore** the source category; its rows are skipped and listed.

The defaults are a saved mapping, then an active category with the same name, then create. Names
in the English or Polish savings word list propose the savings treatment. New categories take
the next unused palette colour, in source order. Before applying, use **Update preview** to save
changed choices and recalculate the report without writing rows. Apply posts the choices again;
if they differ from the previewed ones, the app shows the new counts for confirmation first.

Apply is refused while any mapping is invalid: a missing or wrong-kind target, an empty or
invalid new name, a name that already exists, or two new categories with the same kind and name.
The same choices can be remembered for later imports. Saved mappings are written only when an
apply succeeds, in the same transaction. Rolling back a batch removes mappings that point at
categories the batch created; mappings to categories that existed before the import stay.

Lone files map categories **by name**. A `category_id` from another workspace or from the
transactions page's CSV export is ignored.

### What is skipped

- Rows in a closed month are skipped and listed.
- A transaction dated after today in the workspace's time zone is skipped and listed with
  `future_date`. A future entry would be a one-off planned item in the app, so it is not created
  as an actual transaction. Planned values for future months import normally.
- Transactions with a non-empty `deleted_at` are skipped in merge mode and counted as deleted in
  the source.
- Planned values that are zero or empty are skipped silently. A planned value for an archived or
  transfer category is skipped and listed. Importing a planned value counts as that month's
  first plan write, so the automatic plan copy does not add to the month later.
- Invalid rows are listed with file, line, column, and a stable code. A merge import can proceed
  with the valid rows after ticking **Skip N invalid rows**.

`categories.csv` has no mapping step. It creates categories that do not resolve by lineage id,
or by name and kind among active categories, appending them at the end of their list in file
order. Archived rows are created archived. Existing categories are never changed; differences
are listed. An explicit `sort_order` does not place a merged category: merge mode always
appends.

Every import can be rolled back from the recent-imports list. Rolling back a merge batch that has
rows in a month closed since the import is refused as a whole and lists those months. If the
batch set a month's first-plan marker and no planned values remain there, rollback clears the
marker. A restore rollback is refused when any row, month state, workspace setting or M3 data
changed after the restore; see [Restoring an archive](#restoring-an-archive).
