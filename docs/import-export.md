# Import and export

Your ledger is yours: you can download all of it as an archive, restore it after a reinstall,
merge it into another workspace, or bring in history from your own spreadsheet. Everything
happens in the app; it never sends your data anywhere.

Owners and editors find **Export and import** in the workspace menu. Viewers cannot export or
import. The exact files and columns are described in the
[archive format reference](archive-format.md).

- [Exporting](#exporting)
- [Restoring an archive](#restoring-an-archive)
- [Merging an archive](#merging-an-archive)
- [Importing a CSV file](#importing-a-csv-file)
- [The dry-run report](#the-dry-run-report)
- [Category mapping](#category-mapping)
- [What is skipped](#what-is-skipped)
- [Rolling back an import](#rolling-back-an-import)
- [Limits](#limits)
- [Converting a spreadsheet](#converting-a-spreadsheet)

## Exporting

Select **Download archive**. The ZIP contains the current workspace's settings (including the
Taxes switch, the Upcoming window, the investment cost basis and the saved risk targets), all
categories (including archived categories), transactions (including deleted entries), planned
values, stored month states (closed months and plan-start markers), all net-worth categories,
items, value histories and snapshots, and the whole investment history: asset classes,
instruments, allocations, buy/sell transactions (undated lots included), bond terms, stored
manual and provider prices, stored FX rates, and provider settings without their API keys.
Recurring items, income sources, work logs and calendars, and tax rules are not in this archive
version yet. Notification channels and delivery logs are never exported, because channel URLs
can contain secrets; provider API keys are not exported either.

The download is a copy: exporting does not change the ledger. The audit log records the export
and the number of rows of each kind, without descriptions or amounts.

Keep the ZIP intact for a future restore. The archive is unencrypted and contains your financial
history, including deleted entries, so store it privately. CSV text is kept exactly, including
values that begin with spreadsheet formula characters (`=`, `+`, `-`, `@`); inspect the files as
text, or turn off formula execution before opening them in a spreadsheet.

Imported categories and transactions keep their original IDs when you export them again, so an
archive can go through any number of restores and still be recognised. Exports of the same data,
by the same app version, at the same moment are byte-for-byte identical.

To export only transactions, for a spreadsheet, use the CSV export on the
[Transactions](transactions.md) page instead.

## Restoring an archive

Restore is for a reinstall or a move to another instance. Owners can restore, and only into a
workspace that has no data of its own yet. Choose **Restore into this workspace** before
uploading the archive.

A new workspace starts with the **suggested categories**. As long as you have not renamed,
edited, archived or used any of them, the workspace still counts as empty: the restore removes
them and puts the archive's categories in their place. Recurring items, income sources, work
logs and calendars, and tax rules make the workspace non-empty, because the archive cannot
carry them yet. The suggested net-worth categories follow the same rule as the ledger ones:
untouched, unused and seeded automatically, they are removed before the archive's net-worth
categories are written. The suggested asset classes follow the same rule: untouched, unused and
seeded automatically, they are removed before the archive's classes are written. Edited
categories, items, values, snapshots or asset classes make the workspace non-empty.

Restore writes, all at once:

1. the workspace settings from the archive (name, base currency, country, time zone, language,
   automatic plan copy, Taxes switch and Upcoming window); whether members must use two-factor
   sign-in is not in the archive and stays as it is;
2. categories, with their archived state and order;
3. planned values, including those of archived and transfer categories;
4. transactions, including deleted entries, in their original order within each day;
5. month states exactly as stored: closed months and plan-start markers;
6. net-worth categories, items and value histories, then snapshots. Values keep their
   `created_at`, so several values on one day restore in the same order and the current value
   does not change. The estimate switch is carried; a 1.2 archive restores assets with it on
   and liabilities off;
7. asset classes, instruments and their allocations, then investment transactions (including
   undated legacy lots and deleted rows), bond terms, stored prices and FX rates, and provider
   settings. Investment quantities, prices and FX rates are restored from their plain decimal
   text without going through `float`;
8. the saved risk targets and the investment cost basis.

A net-worth item linked to a portfolio class or holding is linked to the restored row when the
archive carries its target (1.4 archives do). A link whose target is not in the archive —
including every portfolio link in a 1.3 archive — becomes a manual item without a value and is
listed with `link_target_missing`.

Provider settings never include API keys. A keyless provider (NBP, ECB, CoinGecko) keeps its
saved enabled state. A provider that needs a key (EODHD, GoldAPI) is restored **disabled**, and
the report says to re-enter the API key and enable it; leaving it enabled would only fail on
every scheduled refresh. The daily call budget and the one-off backfill markers are kept.

A restore needs a completely valid archive: any error blocks it, and nothing is written. The
**Restore plan** lists what will be written, the suggested categories that will be removed, the
settings that will be replaced and the months that will be closed. Restored closed months are
recorded as closed by you, because users are not part of the archive. Restoring never posts
recurring items or sends notifications, and the stored plan-start markers stop the automatic
plan copy from filling a restored month.

## Merging an archive

Merge brings an archive's ledger data into a workspace you already use, for example to combine
two workspaces. Owners and editors can merge; it is the default mode.

A merge writes categories, transactions and planned values only. The archive's settings,
closed months, net-worth files and investment files are listed for information but never
applied; a 1.2+ archive lists its net-worth files once with a restore-only warning, and a 1.4
archive lists its investment files once with the same kind of warning. The settings diff also
reports the investment cost basis and risk targets when they differ. The archive's base currency
must match the workspace's; otherwise the whole archive is refused. Existing net-worth and
investment data is left untouched. To merge a valuation history, import its
`item_valuations.csv` alone.

Each category in the archive is a row in the [category mapping](#category-mapping) step, so an
active and an archived category with the same name stay separate. By default, a category the
workspace already has from an earlier import or restore is matched by its ID; otherwise a
remembered mapping or an active category with the same name is used; otherwise the category is
created with the archive's attributes, archived state included. Merging an archive back into the
workspace it came from creates nothing.

## Importing a CSV file

A single CSV file can be imported into a workspace that is already in use. Use this for history
kept in a spreadsheet. The app recognises the kind of file from its header row, not from its
file name:

| File | Minimal header | Notes |
|---|---|---|
| Transactions | `date,direction,amount,category,description` | `currency`, `id` and `deleted_at` are optional; `category_id` is ignored |
| Planned values | `month,kind,category,planned` | `kind` may be left out when the category name belongs to only one kind (an existing category or a remembered mapping) |
| Categories | `kind,name` | `id`, `treatment`, `color`, `sort_order` and `archived_at` are optional |
| Item valuations | `item,as_of,value` | `currency`, `note` and `item_id` are optional; `id` and `created_at` are accepted for an exported file |
| Investment transactions | `instrument,date,type,quantity,unit_price` | `quote_currency`, `fee`, `fx_to_base`, `note`, the three settlement fields and `id` are optional; `instrument_id` is ignored and the instrument is matched by name |

Download a ready-made template for transactions, planned values or item valuations from the
links under the upload form. A `months.csv` or `workspace.json` on its own is refused: only a
full archive can restore those.

Item valuations are matched by `item_id` first and by name otherwise. Names are not unique, so
add `item_id` when **item_ambiguous** is listed; it is the lineage id written by an export. A
row that matches a linked item is refused with **item_linked**. A row `id` imported before is a
duplicate even if its date or value changed. Otherwise a stored value for the same item and
date makes an identical value a duplicate and a different value a conflict, while several
rows in one file for the same item and date are imported in file order, so a same-day
correction becomes the latest value. Values must use the item's currency and exactly its minor
digits; a file without `created_at` is stamped in row order from the import time.

Investment transactions are matched by instrument name. A name that matches an active instrument
is mapped automatically; an unknown name needs the **Instrument mapping** step: map it to an
existing active instrument, create one (the type and unit label are required, and the quote
currency defaults to the file's `quote_currency` column and then the base currency), or ignore
all its rows. A created instrument is allocated 100% to the active **Other** class, or to the
first active class when there is no Other.

Each imported row is checked against the instrument's whole stored history. The import is
refused while any instrument's combined history would oversell; the report names the
instrument, line and date, and the fix is either a corrected file or **Ignore** for that
instrument in the mapping. An empty `date` creates an undated lot, listed in the report. When
`fx_to_base` is empty, the latest stored manual or provider rate on or before the date is used;
a row with no stored rate is listed as `fx_needed` and can be skipped like other invalid rows.
Settlement rows are the exception: they must carry their own `fx_to_base`. An exported
`investment_transactions.csv` from a 1.4 archive reads directly.

The file uses the existing quantity, unit price and fee columns for the extra types:

- **Dividend / interest:** `quantity=1`, `unit_price` is the gross amount in the quote currency,
  `fee` is the withholding, and `fx_to_base` is required when the quote currency is not the base
  (otherwise `1`). There are no settlement fields.
- **Split:** `quantity` is the new units and `unit_price` the old units. A 2-for-1 split is
  `2,1`; a 1-for-10 reverse split is `1,10`. Both are positive and different, `fee` is `0`, and
  there is no FX or settlement. Splits are not available for currency or bond-model instruments.

The archive also carries an optional `income_transaction_id` linking a dividend to its ledger
income row. A missing target restores the dividend unlinked and is listed in the restore report.

### Upload options

- **CSV delimiter:** Detect (default), Comma or Semicolon. Detect chooses the separator that
  turns the header into known column names.
- **Decimal separator:** Dot (default) or Comma. A decimal comma works only with the semicolon
  delimiter, as spreadsheets in many European languages save it.

Column names are matched ignoring letter case and surrounding spaces. Unknown columns produce a
warning and are ignored; a missing required column refuses the file. Amounts are never rounded:
too many decimal places, zero or negative amounts, and amounts that are too large are row
errors. A `currency` column, when present, must be the workspace's base currency. Descriptions
may have at most 200 characters.

## The dry-run report

Uploading never changes your ledger. The app checks the file and shows a report first:

- **Counts:** rows to create, duplicates skipped, conflicts skipped, rows skipped in closed
  months, future-dated rows, rows of ignored categories, invalid rows.
- **Per month:** expense total, income total, planned total, rows to create and rows skipped.
- **Per category:** the total that will be imported into each category.
- **Warnings and errors**, each with its file, line, column and a short code.

Use **Download errors and skipped rows CSV** to get every error and skipped row, with its line
number, as a spreadsheet.

**Compare the totals with your spreadsheet before applying.** If a month's expense or income total
differs from your own sum, look at the skipped and invalid rows for that month first. Typical
causes are a closed month, a date in the wrong month, or a row whose amount could not be read.
Fix the file, cancel the import and upload it again; the old report is not reused.

When the report looks right, select **Apply import**. Apply checks everything again inside one
transaction. If the workspace changed in the meantime and the counts are different, nothing is
written and the new report is shown for confirmation. An import is all or nothing.

Importing the same file again creates nothing new: rows with an `id` are recognised by it, and
rows without one by their content. Two genuinely identical rows in one file (two coffees on the
same day at the same price) are both imported. A row that was imported and then deleted is not
brought back by importing the file again.

## Category mapping

Transactions and planned values from a CSV file or a merged archive go through a mapping step.
Each category in the file is shown with its row count and total:

- **Map to an existing category** of the same kind, active or archived.
- **Create a new category** with a name, treatment and colour.
- **Ignore its rows**: they are skipped and listed.

The defaults are already selected: a remembered mapping, then an active category with the same
name, then a new category. Names such as "Savings", "Investments", "Oszczędności" or
"Inwestycje" propose the savings treatment. New categories take the next unused palette colour.

Change the choices, then select **Update preview** to recalculate the report without writing
anything. **Apply import** is unavailable while a mapping is invalid: a missing or wrong-kind
target, an empty or invalid new name, a new name that already exists, or two new categories
with the same kind and name.

Tick **Remember these mappings for later imports** to reuse your choices next time. Mappings are
saved only when the import is applied.

A CSV file maps categories **by name**: a `category_id` from another workspace is ignored. A
`categories.csv` has no mapping step. It creates the categories that do not exist yet (matched
by `id`, or by name and kind among active categories) at the end of their list, archived ones as
archived. Existing categories are never changed; differences are listed.

## What is skipped

- **Closed months:** rows dated in a closed month are skipped and listed. Reopen the month first
  if you want to import into it. A merge never closes or reopens months.
- **Future dates:** a transaction dated after today, in the workspace's time zone, is skipped.
  In the app a future entry is a one-off planned item, not an actual transaction. Planned values
  for future months import normally.
- **Deleted in the source:** transactions with a `deleted_at` are skipped in a merge or CSV
  import and counted; only a restore brings back deleted entries.
- **Planned values:** zero or empty values are skipped silently, because zero means "no plan".
  A planned value for an archived or transfer category is skipped and listed. An imported
  planned value counts as the month's first plan, so the automatic plan copy does not add to
  that month later.
- **Ignored categories:** rows of categories set to **Ignore its rows**.
- **Invalid rows:** listed with their file, line, column and code. In a merge or CSV import you
  can apply the valid rows after ticking **Skip N invalid rows**; a restore has no such option.

## Rolling back an import

Every applied import has **Roll back import** in the recent-imports list. The confirmation page
shows what will be removed.

- **CSV import or merge:** its transactions are deleted (also ones you edited since; the page
  says how many), its planned values are removed except ones you changed since, its imported
  item valuations are removed, and the categories it created are removed, or archived if they
  are still used. Remembered mappings that point at categories it created are removed. If the
  import set a month's plan-start marker and no planned values remain there, the marker is
  cleared. A rollback is refused while any affected month is closed; the page lists those
  months. An investment transaction import soft-deletes its imported rows. An instrument it
  created is deleted when it has no remaining history; otherwise it is archived, like a
  category that is still referenced.
- **Restore:** owners only. It removes everything the restore wrote, for good, and puts the
  previous workspace settings back, so the workspace is empty again and you can restore a
  corrected archive. It also removes the net-worth categories, items, valuations and snapshots
  and the investment classes, instruments, allocations, transactions, bond terms, prices, FX
  rates, provider settings and risk targets it wrote; append-only valuations are removed only
  through this explicit batch rollback. It is refused, with the reason, once anything changed
  after the restore: a new or edited entry, category, planned value, net-worth or investment
  row, a closed or reopened month, a changed setting, or a new recurring item, income source,
  work log or tax rule. A monthly automatic net-worth snapshot taken after the restore also
  counts as a new row and blocks rollback. A rolled-back restore does not bring back the
  suggested categories or asset classes; use **Add suggested categories** on the
  [Categories](categories.md) or [Net worth](net-worth.md) page, or **Add suggested asset
  classes** on the [Investments](investments.md) page, if you want them.

The audit log records every apply and rollback with counts, never with amounts, descriptions or
file names.

## Limits

- Upload size: 20 MiB by default. The instance administrator can change it
  (`ASSETBOARD_IMPORT_MAX_UPLOAD_BYTES`, see the [deployment guide](deployment.md)).
- At most 1 000 000 data rows and 64 columns per file; an archive may unpack to at most 200 MiB.
- Checking a file may take at most 60 seconds; a bigger file fails with a message. Split it by
  year and import the parts one after another.
- An uploaded file is kept only until it is applied or cancelled, and is removed automatically
  after 24 hours.
- The report lists up to 1 000 errors; the counts always include every row.

## Converting a spreadsheet

Your spreadsheet probably does not look like `transactions.csv`. You can convert it yourself
(the [format reference](archive-format.md) has every rule and example) or ask an AI assistant to
do it with the prompt below.

> **Privacy:** the app never sends your data anywhere and never calls an AI service. Pasting
> your financial data into a third-party AI service is your own choice and is governed by that
> service's terms. Consider a local model, or remove descriptions and other details you would
> rather not share first. Always check the result against the dry-run totals before applying.

Copy the prompt, then paste or attach your spreadsheet after it:

```text
Convert my personal budget spreadsheet (pasted or attached below) into CSV files for a budgeting
app. Output only the files described here, each in its own code block, then the three lists at
the end.

File 1: transactions.csv, one row per income or expense entry.
- Header, exactly: date,direction,amount,category,description
- date: YYYY-MM-DD. If an entry only has a day number, take the year and month from the
  sheet, tab or section it is in (for example a tab named "08.26" means August 2026). If you
  cannot tell the date, do not guess: list the entry under "Questions".
- direction: expense or income (lowercase).
- amount: a positive number with a dot as decimal separator, at most 2 decimals, no thousands
  separators, no currency symbol. Never output a negative or zero amount; if my sheet has a
  negative value, list it under "Questions" (refund or income?).
- category: my category name exactly as it is written in my sheet, spelled the same way every
  time. Do not invent, merge or translate categories.
- description: my description text, at most 200 characters; leave it empty if there is none.
- Put a field in double quotes if it contains a comma, a double quote or a line break, and
  double any double quote inside it.
- Keep repeated entries: two identical purchases on the same day are two rows.
- Skip totals, subtotals, summary blocks, headers, empty rows and error values such as #REF!.

File 2 (only if my sheet has planned or budget values): budget_lines.csv
- Header, exactly: month,kind,category,planned
- month: YYYY-MM. kind: expense or income. planned: a positive number formatted like amount.
- Use the same category names as in transactions.csv. Skip zero or empty planned values.

Rules for both files: UTF-8, comma as delimiter, one header row, no extra columns, no comments.

After the files:
1. "Check table": for every month, the total of expense amounts, the total of income amounts
   and the total planned, as computed from the files you produced.
2. "Skipped": every row you left out and why.
3. "Questions": anything you could not convert without guessing.
```

Then:

1. Answer the assistant's questions and let it correct the files.
2. Save each code block as a file (`transactions.csv`, `budget_lines.csv`) with UTF-8 encoding.
3. Upload `transactions.csv` first and check the dry-run report: its per-month totals should
   match the assistant's check table and, more importantly, your own spreadsheet.
4. Map the categories, apply, and repeat for `budget_lines.csv`.

If something went wrong, roll the import back, fix the file and import it again.
