# Workspace archive format (v1.2)

This is the reference for the files that **Export and import** writes and reads: the workspace
archive (a ZIP) and the hand-made CSV files you can import on their own. It is normative: the
app's reader and writer follow it, and a test checks that its examples read cleanly and that
every column the app writes is documented here. For how to export, restore, merge and import,
see the [import and export guide](import-export.md).

Format decisions and versioning rules are recorded in
[ADR-0016](adr/0016-workspace-archive-format.md).

## Archive layout

```
assetboard-home-20261018-143000Z.zip
├── manifest.json
├── workspace.json
├── categories.csv
├── budget_lines.csv
├── transactions.csv
├── months.csv
├── networth_categories.csv
├── networth_items.csv
├── item_valuations.csv
└── networth_snapshots.csv
```

All members sit at the root of the ZIP (no folders). Every file is required in an archive, even
when it has only its header row. The file name prefix is the app's command-line name.

## Common rules

- Encoding UTF-8. Export writes no BOM; import removes one if present.
- CSV per RFC 4180: one header row; comma delimiter; `"` quotes fields that contain a comma,
  quote, CR or LF, with inner quotes doubled; export ends lines with CRLF, import accepts CRLF
  or LF. Export writes columns in the order listed; import matches names in any order.
- Dates `YYYY-MM-DD`; months `YYYY-MM`; timestamps UTC `YYYY-MM-DDTHH:MM:SS[.ffffff]Z` (import
  also accepts an explicit offset and converts it to UTC).
- Amounts: plain decimals with `.`, no grouping, no sign, no currency symbol. Export writes
  exactly the currency's minor digits (`12.50` for EUR, `1200` for JPY); import accepts fewer
  (`12.5`) and extra trailing zeros (`12.500`), and refuses more significant decimals.
- An empty optional field means no value. Enum values are lowercase English tokens and stay
  English in every interface language.
- Lone CSV uploads may instead use `;` as delimiter, and then `,` as decimal separator (chosen
  on the upload form). Files inside an archive never do.

## Versions and compatibility

The manifest's `format_version` is `MAJOR.MINOR`.

- A **newer major** version is refused ("made by a newer version of the app").
- A **newer minor** version imports with a warning that lists the ignored files, keys and
  columns. Minor versions only add optional data.
- An **older major** version would pass through the reader's upgrade functions; 1.x is the
  first format, so none exist yet.

**Version history.** 1.0: first release. 1.1 (additive): `workspace.json` gains the optional
`taxes_enabled` and `upcoming_days` keys; a 1.0 archive reads without them, and a 1.0 reader
lists them as unknown keys (a warning). 1.2: adds the four net-worth files
(`networth_categories.csv`, `networth_items.csv`, `item_valuations.csv` and
`networth_snapshots.csv`); they are required in a 1.2 archive and absent from 1.0/1.1 archives,
which read with empty net-worth data. Golden archives of all three versions are in
`tests/fixtures/`.

## `manifest.json`

```json
{
  "format": "workspace-archive",
  "format_version": "1.1",
  "app": {"name": "AssetBoard", "version": "0.4.0"},
  "exported_at": "2026-10-18T14:30:00Z",
  "base_currency": "EUR",
  "files": [
    {"path": "workspace.json", "sha256": "9f2c…", "rows": null, "bytes": 161},
    {"path": "categories.csv", "sha256": "0a41…", "rows": 24, "bytes": 2210},
    {"path": "budget_lines.csv", "sha256": "77de…", "rows": 310, "bytes": 18950},
    {"path": "transactions.csv", "sha256": "c3b0…", "rows": 4120, "bytes": 512004},
    {"path": "months.csv", "sha256": "5e19…", "rows": 30, "bytes": 1290}
  ]
}
```

`format` is independent of the product name, so it survives a rename; `app` is informational.
`sha256` is the lowercase hex digest of the member's bytes (shortened above). `rows` excludes
the header and is `null` for JSON files. The reader checks every checksum, byte count and row
count.

## `workspace.json` (restore only)

```json
{
  "name": "Home",
  "base_currency": "EUR",
  "country_code": "PL",
  "timezone": "Europe/Warsaw",
  "locale": "pl",
  "auto_copy_plan": false,
  "taxes_enabled": true,
  "upcoming_days": null
}
```

| Key | Since | Required | Values |
|---|---|---|---|
| `name` | 1.0 | yes | the workspace name |
| `base_currency` | 1.0 | yes | ISO 4217 code; must equal the manifest's `base_currency` |
| `country_code` | 1.0 | yes | ISO 3166-1 alpha-2 code, or `null` |
| `timezone` | 1.0 | yes | IANA time zone, for example `Europe/Warsaw` |
| `locale` | 1.0 | yes | interface language code, for example `en` or `pl` |
| `auto_copy_plan` | 1.0 | yes | `true` \| `false`: copy the previous month's plan automatically |
| `taxes_enabled` | 1.1 | no (absent = off) | `true` \| `false`: the workspace's Taxes switch |
| `upcoming_days` | 1.1 | no (absent = `null`) | integer 1–365, or `null` for the instance default |

Later settings are added as optional keys (a minor version). A merge reports settings that
differ from the workspace's but never applies them; only a restore does.

## `categories.csv`

| Column | Required | Values |
|---|---|---|
| `id` | in an archive; optional alone | UUID (lineage id) |
| `kind` | yes | `expense` \| `income` |
| `name` | yes | 1–80 characters |
| `treatment` | no (default `regular`) | `regular` \| `savings` \| `transfer` |
| `color` | no (default from the palette) | `#rrggbb` |
| `sort_order` | no (default: file order) | integer ≥ 0, per kind; a lone merge ignores it for placement and appends new categories in file order |
| `archived_at` | no | timestamp; set = archived |

Export order: kind (`expense`, `income`), active before archived, `sort_order`, name key, id.
Two active categories of one kind may not share a name; archived categories may.

```csv
id,kind,name,treatment,color,sort_order,archived_at
2b0e8c1e-5f7a-4c6e-9a51-3f1d2c7b9e01,expense,Food,regular,#4f9d69,0,
6a3d0f42-91b8-4e2c-8d0e-7c5b1a2f3e44,expense,Investments,savings,#3b6ea8,1,
d1c9e7a0-3b2f-4a8e-b6c5-0e9f8d7c6b55,expense,Car,regular,#a35d3b,2,2025-12-31T18:05:00Z
91f4b2c3-7d6e-4f1a-8b9c-2e3d4f5a6b77,income,Salary,regular,#2f7f5f,0,
```

## `transactions.csv`

| Column | Required | Values |
|---|---|---|
| `date` | yes | `YYYY-MM-DD`, from 1900-01-01 to 2199-12-31 |
| `direction` | yes | `expense` \| `income` (= the category's kind) |
| `amount` | yes | decimal > 0 |
| `currency` | no (default: base currency) | must equal the base currency |
| `category` | yes | category name (of the direction's kind) |
| `description` | no | at most 200 characters; whitespace is collapsed, control characters are refused |
| `category_id` | no (always set in an archive) | UUID from `categories.csv`; needed because archived categories may share a name. Ignored in a lone CSV, which maps by `category` name |
| `id` | no | UUID (lineage id; enables exact dedupe) |
| `deleted_at` | no | timestamp; set = soft-deleted (restored as deleted; skipped in merge) |

Export order: date, then entry order (`created_at`, id). The transactions page's CSV export
writes the same columns without `deleted_at`. In merge mode a row dated after today in the
workspace's time zone is skipped and listed with `future_date`.

Archive example:

```csv
date,direction,amount,currency,category,description,category_id,id,deleted_at
2026-09-01,income,3400.00,EUR,Salary,September salary,91f4b2c3-7d6e-4f1a-8b9c-2e3d4f5a6b77,0b6e2f1a-4c3d-4e5f-8a9b-1c2d3e4f5a01,
2026-09-02,expense,50.00,EUR,Food,Groceries,2b0e8c1e-5f7a-4c6e-9a51-3f1d2c7b9e01,5c7d8e9f-0a1b-4c2d-9e3f-4a5b6c7d8e02,
2026-09-02,expense,4.20,EUR,Food,"Bakery, rolls",2b0e8c1e-5f7a-4c6e-9a51-3f1d2c7b9e01,7e8f9a0b-1c2d-4e3f-8a4b-5c6d7e8f9a03,2026-09-03T07:12:44Z
```

Hand-made file with the minimal columns (the template download; the two identical rows are both
imported):

```csv
date,direction,amount,category,description
2025-01-03,expense,12.5,Food,Bakery
2025-01-03,expense,12.5,Food,Bakery
2025-01-31,income,3000,Salary,
```

The same first row with semicolons and a decimal comma (upload options "semicolon" and
"comma"):

```csv
date;direction;amount;category;description
2025-01-03;expense;12,50;Food;Bakery
```

## `budget_lines.csv`

| Column | Required | Values |
|---|---|---|
| `month` | yes | `YYYY-MM` |
| `kind` | no, if the name resolves to exactly one kind (an existing category or a saved mapping); otherwise `kind_required` | `expense` \| `income` |
| `category` | yes | category name |
| `planned` | yes | decimal ≥ 0 (0 or empty = no line, skipped) |
| `currency` | no (default: base currency) | must equal the base currency |
| `category_id` | no (always set in an archive) | UUID from `categories.csv`; ignored in a lone CSV, which maps by `category` name |

Export order: month, kind, category order. At most one row per (month, category).

```csv
month,kind,category,planned,currency,category_id
2026-09,expense,Food,550.00,EUR,2b0e8c1e-5f7a-4c6e-9a51-3f1d2c7b9e01
2026-09,income,Salary,3000.00,EUR,91f4b2c3-7d6e-4f1a-8b9c-2e3d4f5a6b77
```

Hand-made file with the minimal columns (the template download):

```csv
month,kind,category,planned
2026-09,expense,Food,550.00
2026-09,income,Salary,3000.00
```

## `networth_categories.csv` (restore only)

| Column | Required | Values |
|---|---|---|
| `id` | yes | UUID (lineage id) |
| `side` | yes | `asset` \| `liability` |
| `name` | yes | 1–80 characters; unique per side among active categories |
| `color` | no (default from the palette) | `#rrggbb` |
| `sort_order` | no (default: file order) | integer ≥ 0, per side |
| `archived_at` | no | timestamp; set = archived |

```csv
id,side,name,color,sort_order,archived_at
3f9a1c2d-4b5e-4f60-8a71-2b3c4d5e6f70,asset,Vehicles,#3b6ea8,0,
```

## `networth_items.csv` (restore only)

| Column | Required | Values |
|---|---|---|
| `id` | yes | UUID (lineage id) |
| `side` | yes | `asset` \| `liability`; must equal the category's side |
| `category` | yes | category name (must match `category_id`) |
| `category_id` | yes | UUID from `networth_categories.csv` |
| `name` | yes | 1–200 characters |
| `currency` | yes | ISO 4217 code; every valuation must use it |
| `notes` | no | at most 200 characters |
| `acquired_on` | no | `YYYY-MM-DD` |
| `valuation_mode` | no (default `manual`) | `manual` \| `linked` |
| `link_type` | no | `portfolio_asset_class` \| `portfolio_holding` \| `loan_balance` |
| `link_id` | no | UUID of the link target's lineage id |
| `include_in_net_worth` | no (default `true`) | `true` \| `false` |
| `investable` | no (default `false`) | `true` \| `false` |
| `archived_at` | no | timestamp; set = archived |

A linked item keeps its `link_type` and `link_id` on export. Restore clears both and writes a
manual item without a value, because the target's module has not yet contributed its archive
files; the item is listed with `link_target_missing`. The M6/M7 archive extensions will resolve
link target lineage ids.

```csv
id,side,category,category_id,name,currency,notes,acquired_on,valuation_mode,link_type,link_id,include_in_net_worth,investable,archived_at
c4d5e6f7-a8b9-4c0d-9e1f-2a3b4c5d6e7f,asset,Vehicles,3f9a1c2d-4b5e-4f60-8a71-2b3c4d5e6f70,Car,EUR,Family car,2025-04-01,manual,,,true,false,
```

## `item_valuations.csv`

| Column | Required | Values |
|---|---|---|
| `id` | in an archive; optional alone | UUID (lineage id) |
| `item_id` | in an archive; optional alone | item lineage id; when present and found, wins over `item` |
| `item` | yes | item name |
| `as_of` | yes | `YYYY-MM-DD`; future dates are allowed |
| `value` | yes | decimal ≥ 0, exactly the currency's minor digits |
| `currency` | in an archive; optional alone | must equal the item's currency |
| `note` | no | at most 200 characters |
| `created_at` | no | UTC timestamp; preserves same-day entry order |

A value's currency must equal its item's currency, and a linked item has no manual values. The
same item may have several values on one date; export orders them by `as_of`, then `created_at`,
then `id`, and that order decides the current value. Restore writes `created_at` explicitly so
the order survives. A file without it is stamped from the import time in row order.

Hand-made file with the minimal columns (the template download):

```csv
item,as_of,value
Car,2026-01-31,12000.00
Car,2026-02-28,11500.00
```

The file can also be imported alone in merge mode (the template has the minimal columns). Rows
match an item by `item_id` first, then by name; names are not unique, so `item_ambiguous` is
common unless `item_id` is present. A row that matches a linked item is refused with
`item_linked`. An `id` already imported into the workspace is a duplicate (`already_imported`)
even if the row's date or value changed, and an `id` repeated in the file is `duplicate_in_file`.

Otherwise a stored value for the same item and date makes an identical row a duplicate
(`already_imported`) and a different value a conflict (`changed_since_export`); both are listed
and the stored history is kept. Several rows in one file for the same item and date with no
stored clash are all imported, in file order, so a same-day correction becomes the latest value.
Only an exactly identical row (date, value, note and `created_at`) is `duplicate_in_file`.

```csv
id,item_id,item,as_of,value,currency,note,created_at
e6f7a8b9-c0d1-4e2f-9a3b-4c5d6e7f8091,c4d5e6f7-a8b9-4c0d-9e1f-2a3b4c5d6e7f,Car,2026-09-01,12000.00,EUR,Bought,2026-09-01T08:00:00Z
```

## `networth_snapshots.csv` (restore only)

| Column | Required | Values |
|---|---|---|
| `id` | yes | UUID (lineage id) |
| `as_of` | yes | `YYYY-MM-DD`; one snapshot per date and source |
| `assets` | yes | decimal ≥ 0 |
| `liabilities` | yes | decimal ≥ 0 |
| `currency` | yes | ISO 4217 code |
| `breakdown` | yes | compact JSON object; category ids are lineage ids and unknown ids are kept |
| `source` | yes | `auto` \| `manual` \| `backfill` |

`net` is always recomputed as `assets - liabilities`, never read from the file. The breakdown
uses sorted keys and no extra whitespace so exports are deterministic:

```json
{"categories":[{"category_id":"3f9a1c2d-4b5e-4f60-8a71-2b3c4d5e6f70","side":"asset","total_minor":1200000}],"not_counted":{"non_base":0,"no_value":0,"unavailable":0},"v":1}
```

```csv
id,as_of,assets,liabilities,currency,breakdown,source
a8b9c0d1-e2f3-4a4b-9c5d-6e7f8091a2b3,2026-08-31,12000.00,0.00,EUR,"{""categories"":[],""not_counted"":{""non_base"":0,""no_value"":0,""unavailable"":0},""v"":1}",auto
```

## `months.csv` (restore only)

| Column | Required | Values |
|---|---|---|
| `month` | yes | `YYYY-MM` |
| `closed_at` | no | timestamp; set = month closed |
| `plan_started_at` | no | timestamp (the automatic plan copy's marker) |

One row per stored month state; a month without a row is open and has no marker.

```csv
month,closed_at,plan_started_at
2026-08,2026-09-02T19:40:00Z,2026-07-28T08:00:00Z
2026-09,,2026-09-01T06:30:12Z
```

## Reader limits

An uploaded archive is never extracted to disk. It is refused when it has members in folders or
with unsafe names, symlinks, encrypted members, compression methods other than stored and
deflate, duplicate names, more than 64 members, a member over 100 MiB or a total over 200 MiB
uncompressed, or a member (1 MiB or larger) compressed more than 200:1. Unknown member names are
listed as warnings and ignored. A file may hold at most 1 000 000 data rows and 64 columns.

## Not in the archive (format 1.2)

Members, invitations, users and their sessions, the audit log, import batches and saved
mappings, and who created or closed what. Recurring items, income sources, work logs and
calendars, and tax rules are added by a later archive extension as new files (a minor version);
until then, a restore requires a workspace without them. Notification channels are never
exported, because their URLs can contain secrets.

Net-worth files are restore-only in a full archive merge: they are ignored and listed once as a
warning. Only a lone `item_valuations.csv` merges.
