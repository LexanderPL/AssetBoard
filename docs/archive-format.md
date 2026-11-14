# Workspace archive format (v1.8)

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
├── networth_snapshots.csv
├── asset_classes.csv
├── instruments.csv
├── instrument_allocations.csv
├── investment_transactions.csv
├── bond_terms.csv
├── price_points.csv
├── fx_rates.csv
├── price_source_configs.csv
├── reference_rates.csv
├── reference_rate_values.csv
├── loans.csv
├── loan_rate_changes.csv
├── loan_transfers.csv
├── loan_cost_components.csv
├── loan_overpayments.csv
├── loan_payments.csv
├── loan_balance_checkpoints.csv
├── loan_scenarios.csv
├── installment_plans.csv
├── fire_scenarios.csv
├── fire_contributions.csv
├── fire_events.csv
└── fire_expense_exclusions.csv
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
  columns. Minor versions only add data; a new column may be required in that version when an
  older archive can be read with a documented default.
- An **older major** version would pass through the reader's upgrade functions; 1.x is the
  first format, so none exist yet.

**Version history.** 1.0: first release. 1.1 (additive): `workspace.json` gains the optional
`taxes_enabled` and `upcoming_days` keys; a 1.0 archive reads without them, and a 1.0 reader
lists them as unknown keys (a warning). 1.2: adds the four net-worth files
(`networth_categories.csv`, `networth_items.csv`, `item_valuations.csv` and
`networth_snapshots.csv`); they are required in a 1.2 archive and absent from 1.0/1.1 archives,
which read with empty net-worth data. 1.3: `networth_items.csv` gains
`estimate_between`; a 1.2 archive reads without it and restore uses the side default (assets on,
liabilities off). 1.4 (additive): adds the eight investment files below,
`workspace.json` gains the optional `investment_cost_basis` and `risk_target_policy` keys, and
`asset_classes.csv` carries the F6.11 risk tiers. A 1.3 archive reads with no investment rows
and the default average cost basis; a 1.4 archive requires every new file (header only when
empty). 1.5 (additive): adds the ten loan files below; a 1.4 archive reads with no loan
rows, and a 1.5 archive requires every new file (header only when empty). 1.6 (additive):
`loan_rate_changes.csv` gains `mode`, `reference_rate_id`, `reference_rate` and `margin_bp`;
a 1.5 archive reads its old rows as fixed. 1.7 (additive): adds `loan_transfers.csv`;
`loans.csv` gains `drawdown_mode` and `interest_only_months`, and a 1.6 archive reads every
loan as `full_at_origination` with no interest-only phase. 1.8 (additive): adds the four FIRE
files below; a 1.7 archive reads with no FIRE scenarios, and a 1.8 archive requires every new
file (header only when empty). Schedules and projections are not archived and are recomputed
from the restored terms. Golden archives of all nine versions are in `tests/fixtures/`.

## `manifest.json`

```json
{
  "format": "workspace-archive",
  "format_version": "1.8",
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
  "upcoming_days": null,
  "investment_cost_basis": "fifo",
  "risk_target_policy": {
    "mode": "fixed",
    "safe_bp": 6000,
    "growth_bp": 3000,
    "speculative_bp": 1000,
    "tolerance_bp": 500
  }
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
| `investment_cost_basis` | 1.4 | no (absent = `average`) | `average` \| `fifo` |
| `risk_target_policy` | 1.4 | no (absent = defaults) | object; see below |

`risk_target_policy` is the workspace's saved target allocation (F6.11). It is absent when the
workspace never saved one, in which case the defaults apply. All five keys are required when
the object is present:

| Field | Values |
|---|---|
| `mode` | `fixed` \| `age_scaled` \| `age_in_bonds` |
| `safe_bp` | integer 0–10 000 |
| `growth_bp` | integer 0–10 000 |
| `speculative_bp` | integer 0–10 000; in age modes this is X |
| `tolerance_bp` | integer 0–10 000 |

The targets must total exactly 10 000 basis points in `fixed` mode. A merge reports
`risk_target_policy` and `investment_cost_basis` in its settings diff but never applies them;
only a restore does.

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
| `estimate_between` | in 1.3; absent in 1.2 | `true` \| `false`: straight-line estimates in a backfill |
| `archived_at` | no | timestamp; set = archived |

A linked item keeps its `link_type` and `link_id` on export. Restore resolves a portfolio link
against the archive's instruments and asset classes, and a `loan_balance` link against its
loans; a link whose target is missing becomes a manual item without a value and is listed with
`link_target_missing`. A missing `estimate_between` in a 1.2 archive restores the side default:
on for assets, off for liabilities.

```csv
id,side,category,category_id,name,currency,notes,acquired_on,valuation_mode,link_type,link_id,include_in_net_worth,investable,estimate_between,archived_at
c4d5e6f7-a8b9-4c0d-9e1f-2a3b4c5d6e7f,asset,Vehicles,3f9a1c2d-4b5e-4f60-8a71-2b3c4d5e6f70,Car,EUR,Family car,2025-04-01,manual,,,true,false,true,
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

## `asset_classes.csv` (restore only)

| Column | Required | Values |
|---|---|---|
| `id` | yes | UUID (lineage id) |
| `name` | yes | 1–80 characters; unique among active classes |
| `investable` | no (default `true`) | `true` \| `false` |
| `color` | no (default from the palette) | `#rrggbb` |
| `sort_order` | no (default: file order) | integer ≥ 0 |
| `risk_tier` | no | `safe` \| `growth` \| `speculative`; blank = unassigned |
| `archived_at` | no | timestamp; set = archived |

Export order: active before archived, `sort_order`, name key, lineage id.

```csv
id,name,investable,color,sort_order,risk_tier,archived_at
0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d,Stocks,true,#2563eb,0,growth,
1b2c3d4e-5f60-4b7c-9d8e-0f1a2b3c4d5e,Bonds,true,#16a34a,1,safe,
```

## `instruments.csv` (restore only)

| Column | Required | Values |
|---|---|---|
| `id` | yes | UUID (lineage id) |
| `name` | yes | 1–200 characters; unique among active instruments |
| `symbol` | no | at most 32 characters |
| `type` | yes | `equity` \| `etf` \| `bond_market` \| `bond_model` \| `metal` \| `crypto` \| `currency` \| `real_estate` \| `other` |
| `quote_currency` | yes | ISO 4217 code |
| `unit_label` | yes | 1–32 characters; a currency instrument's label is its code |
| `valuation_source` | yes | `provider` \| `manual` \| `bond_model` |
| `provider_key` | no | at most 32 characters |
| `provider_symbol` | no | at most 64 characters |
| `provider_backfilled_at` | no | timestamp of the last full provider backfill |
| `risk_tier_override` | no | `safe` \| `growth` \| `speculative`; blank = use the class tiers |
| `archived_at` | no | timestamp; set = archived |

Export order: active before archived, name key, lineage id.

```csv
id,name,symbol,type,quote_currency,unit_label,valuation_source,provider_key,provider_symbol,provider_backfilled_at,risk_tier_override,archived_at
2c3d4e5f-6071-4c8d-ae9f-1a2b3c4d5e6f,World ETF,VWCE,etf,EUR,share,manual,,,,,
3d4e5f60-7182-4d9e-bfa0-2b3c4d5e6f70,Treasury bond,,bond_model,EUR,bond,bond_model,,,,,
```

## `instrument_allocations.csv` (restore only)

| Column | Required | Values |
|---|---|---|
| `id` | yes | UUID (lineage id) |
| `instrument_id` | yes | instrument lineage id |
| `instrument` | yes | instrument name (must match `instrument_id`) |
| `asset_class_id` | yes | asset class lineage id |
| `asset_class` | yes | asset class name (must match `asset_class_id`) |
| `weight_bp` | yes | integer 1–10 000; every instrument's weights total exactly 10 000 |

Export order: instrument order, weight descending, asset class order, lineage id.

```csv
id,instrument_id,instrument,asset_class_id,asset_class,weight_bp
4e5f6071-8293-4eaf-80b1-3c4d5e6f7081,2c3d4e5f-6071-4c8d-ae9f-1a2b3c4d5e6f,World ETF,0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d,Stocks,10000
5f607182-93a4-4fb0-91c2-4d5e6f708192,3d4e5f60-7182-4d9e-bfa0-2b3c4d5e6f70,Treasury bond,1b2c3d4e-5f60-4b7c-9d8e-0f1a2b3c4d5e,Bonds,10000
```

## `investment_transactions.csv`

| Column | Required | Values |
|---|---|---|
| `id` | yes | UUID (lineage id) |
| `instrument_id` | yes | instrument lineage id |
| `instrument` | yes | instrument name (must match `instrument_id`) |
| `date` | yes (may be blank) | `YYYY-MM-DD`; blank = undated imported legacy lot |
| `type` | yes | `buy` \| `sell` \| `dividend` \| `fee` \| `split` |
| `quantity` | yes | plain positive decimal, at most 18 fractional digits |
| `unit_price` | yes | plain decimal ≥ 0 in `quote_currency`, at most 18 fractional digits |
| `quote_currency` | yes | must equal the instrument's quote currency |
| `fee` | no (default `0`) | decimal in `quote_currency` with its minor digits |
| `fx_to_base` | no | positive plain decimal; base-currency units per quote-currency unit |
| `note` | no | at most 200 characters |
| `settlement_currency` | no | ISO 4217 code; the three settlement fields are set together |
| `settlement_unit_price` | no | plain decimal ≥ 0 in `settlement_currency` |
| `settlement_fee` | no | decimal in `settlement_currency` with its minor digits |
| `deleted_at` | no | timestamp; set = soft-deleted |
| `income_transaction_id` | no | UUID of the linked ledger income row; dividends only |

A settlement row must use the workspace base currency and is allowed only when the instrument's
quote currency differs from the base and the instrument is not a `currency` instrument. Export
order: instrument order, undated before dated, date, entry time, lineage id. Quantities, prices
and FX rates are written with `format(value, "f")`, never an exponent or `float`, with trailing
fractional zeros removed; import reads them with `Decimal(text)`.

Dividends and splits reuse the quantity, unit price and fee columns:

- `dividend`: `quantity` is exactly `1`, `unit_price` is the gross amount in the quote currency,
  `fee` is the withholding, `fx_to_base` is required when the quote currency is not the base and
  is `1` otherwise, no settlement fields. Gross must be positive and fit the currency's minor
  unit; `0 ≤ withholding ≤ gross`.
- `split`: `quantity` is the number of new units and `unit_price` the number of old units (a
  2-for-1 split is `2` and `1`; a 1-for-10 reverse split is `1` and `10`). Both are positive and
  different, `fee` is `0`, and there is no FX or settlement. Splits are refused for `currency`
  and `bond_model` instruments.
- `income_transaction_id` links a dividend to its ledger income row. A target missing from the
  archive restores the dividend unlinked and is listed in the restore report.

The file can also be imported alone in merge mode (the "bring my purchase history" path). The
minimal header is `instrument,date,type,quantity,unit_price`; `quote_currency` is optional and
defaults to the instrument's currency and then the base currency. Rows match an instrument by
name; unknown names go through the instrument mapping, where they can be created (type and unit
label required), mapped to an active instrument, or ignored. `instrument_id` is ignored in a
lone file. An empty `date` creates an undated lot. A missing `fx_to_base` uses the latest stored
manual or provider rate on or before the date and otherwise reports `fx_needed`; settlement rows
must carry their own `fx_to_base`. Apply is refused while the combined history would oversell an
instrument; the report names the instrument, line and date, and the user fixes the file or
ignores that instrument.

```csv
id,instrument_id,instrument,date,type,quantity,unit_price,quote_currency,fee,fx_to_base,note,settlement_currency,settlement_unit_price,settlement_fee,deleted_at,income_transaction_id
60718293-a4b5-40c1-82d3-5e6f708192a3,2c3d4e5f-6071-4c8d-ae9f-1a2b3c4d5e6f,World ETF,2026-09-01,buy,10,100.5,EUR,1.00,1,First lot,,,,,
```

## `bond_terms.csv` (restore only)

| Column | Required | Values |
|---|---|---|
| `id` | yes | UUID (lineage id) |
| `instrument_id` | yes | bond-model instrument lineage id |
| `instrument` | yes | instrument name (must match `instrument_id`) |
| `series_code` | no | at most 64 characters |
| `face_value` | yes | decimal > 0 in the instrument's quote currency |
| `maturity_date` | yes | `YYYY-MM-DD` |
| `interest_mode` | yes | `capitalized_annually` \| `paid_out_annually` \| `at_maturity_simple` |
| `rate_schedule` | yes | compact JSON list of `{from_date, annual_rate_bp}` |
| `tax_rate_bp` | yes | integer 0–10 000 |
| `early_redemption_fee_mode` | no | `percent` \| `fixed`; blank = no early-redemption fee |
| `early_redemption_fee_rate_bp` | no | integer 0–10 000 for `percent` |
| `early_redemption_fee` | no | decimal in the instrument's quote currency for `fixed` |

```csv
id,instrument_id,instrument,series_code,face_value,maturity_date,interest_mode,rate_schedule,tax_rate_bp,early_redemption_fee_mode,early_redemption_fee_rate_bp,early_redemption_fee
718293a4-b5c6-41d2-93e4-6f708192a3b4,3d4e5f60-7182-4d9e-bfa0-2b3c4d5e6f70,Treasury bond,EDO,1000.00,2030-01-01,capitalized_annually,"[{""annual_rate_bp"":500,""from_date"":""2026-01-01""}]",1900,percent,100,
```

## `price_points.csv` (restore only)

| Column | Required | Values |
|---|---|---|
| `id` | yes | UUID (lineage id) |
| `instrument_id` | yes | instrument lineage id |
| `instrument` | yes | instrument name (must match `instrument_id`) |
| `date` | yes | `YYYY-MM-DD` |
| `price` | yes | plain decimal ≥ 0 in the instrument's quote currency |
| `source` | yes | `manual` \| `provider` |
| `provider_key` | no | at most 32 characters; set for provider rows |

Only manual and provider observations are exported; transaction-derived prices are recomputed.
Export order: instrument order, date, source, entry time, lineage id.

```csv
id,instrument_id,instrument,date,price,source,provider_key
8293a4b5-c6d7-42e3-84f5-708192a3b4c5,2c3d4e5f-6071-4c8d-ae9f-1a2b3c4d5e6f,World ETF,2026-09-01,101.25,manual,
```

## `fx_rates.csv` (restore only)

| Column | Required | Values |
|---|---|---|
| `id` | yes | UUID (lineage id) |
| `base_currency` | yes | must equal the workspace base currency |
| `quote_currency` | yes | ISO 4217 code |
| `date` | yes | `YYYY-MM-DD` |
| `rate` | yes | positive plain decimal: base-currency units per one quote-currency unit |
| `source` | yes | `manual` or a provider key |

Only stored manual and provider rates are exported; transaction-derived FX is recomputed. Export
order: base, quote, date, source, entry time, lineage id.

```csv
id,base_currency,quote_currency,date,rate,source
93a4b5c6-d7e8-43f4-a506-8192a3b4c5d6,EUR,USD,2026-09-01,0.92,nbp
```

## `price_source_configs.csv` (restore only)

| Column | Required | Values |
|---|---|---|
| `id` | yes | UUID (lineage id) |
| `provider_key` | yes | provider key, unique per workspace |
| `enabled` | yes | `true` \| `false` |
| `daily_call_budget` | yes | integer 1–100 000 |
| `fx_backfilled` | no | compact JSON object of currency → date; the one-off FX backfill marker |

Provider API keys and other encrypted settings are **never** exported (F9.3). On restore a
keyless provider (NBP, ECB, CoinGecko) keeps its saved enabled state. A provider that requires a
key (EODHD, GoldAPI) is restored **disabled** and the restore report says to re-enter the API key
and enable it; leaving it enabled would only log `missing_api_key` on every scheduled run.

```csv
id,provider_key,enabled,daily_call_budget,fx_backfilled
a4b5c6d7-e8f9-44a5-b617-92a3b4c5d6e7,nbp,true,20,"{""USD"":""2026-09-01""}"
```

## `reference_rates.csv` (restore only)

| Column | Required | Values |
|---|---|---|
| `id` | yes | UUID (lineage id; also the loan files' reference id) |
| `name` | yes | 1–200 characters; case-folded names are unique per workspace |
| `notes` | no | at most 200 characters |

Export order: name, id. A variable-rate loan references the series by `reference_rate_id`; the
`reference_rate` name is the human-readable companion.

```csv
id,name,notes
b5c6d7e8-f9a0-45b6-8c28-a3b4c5d6e7f8,WIBOR,3M fixing
```

## `reference_rate_values.csv`

| Column | Required | Values |
|---|---|---|
| `id` | in an archive; optional alone | UUID (lineage id) |
| `reference_rate_id` | in an archive; optional alone | UUID from `reference_rates.csv` |
| `reference_rate` | yes | the series name; a lone import matches it case-insensitively and creates it when absent |
| `effective_from` | yes | `YYYY-MM-DD` |
| `value_bp` | yes | integer 0–10 000 (annual basis points) |

Export order: reference-rate order, effective date, id. One value per series and date.

This is the only loan file that can be imported alone in merge mode. An identical stored value
for the series and date is a duplicate (`already_imported`); a different value is a conflict
(`reference_rate_value_differs`) and the stored value is kept. A series named by the file but
absent from the workspace is created.

```csv
id,reference_rate_id,reference_rate,effective_from,value_bp
c6d7e8f9-a0b1-46c7-9d39-b4c5d6e7f809,b5c6d7e8-f9a0-45b6-8c28-a3b4c5d6e7f8,WIBOR,2026-01-01,575
```

## `loans.csv` (restore only)

| Column | Required | Values |
|---|---|---|
| `id` | yes | UUID (lineage id) |
| `name` | yes | 1–200 characters |
| `kind` | yes | `annuity` \| `equal_principal` \| `installment_plan` |
| `currency` | yes | must equal the workspace base currency |
| `principal_original` | no | decimal > 0 |
| `origination_date` | no | `YYYY-MM-DD`; must be before the first payment |
| `first_payment_date` | yes | `YYYY-MM-DD` |
| `term_months` | yes | integer 0–600; 0 only for an installment plan (a plan's term follows its remaining count) |
| `payment_day` | yes | integer 1–31; shorter months use their last day |
| `rate_mode` | yes | `fixed` \| `variable` |
| `day_count` | yes | `monthly` \| `actual_365` |
| `drawdown_mode` | in 1.7 | `full_at_origination` (default for older archives) \| `tranches` |
| `interest_only_months` | in 1.7 | integer 0–599; must be shorter than `term_months`; 0 for an installment plan or older archives |
| `margin_bp` | variable only | integer 0–10 000 |
| `reference_rate_id` | variable only | UUID from `reference_rates.csv` |
| `reference_rate` | variable only | the series name (must match `reference_rate_id`) |
| `property_item_id` | no | net-worth item lineage id |
| `property_item` | no | the item name (must match `property_item_id`) |
| `auto_record_payments` | no (default `false`) | `true` \| `false` |
| `installment_override` | no | decimal > 0; `annuity` only |
| `overpayment_mode` | yes | `none` \| `fixed_extra` \| `target_total` |
| `overpayment_amount` | when the mode is not `none` | decimal > 0 |
| `overpayment_strategy` | yes | `reduce_term` \| `reduce_installment` |
| `archived_at` | no | timestamp; set = archived |

Export order: name, kind, active before archived, id. A fixed loan has no reference rate or
margin; a variable loan requires both. Actual payments, checkpoints and planned overpayments
are separate files. Schedules are never stored: they are rebuilt from these terms.

```csv
id,name,kind,currency,principal_original,origination_date,first_payment_date,term_months,payment_day,rate_mode,day_count,drawdown_mode,interest_only_months,margin_bp,reference_rate_id,reference_rate,property_item_id,property_item,auto_record_payments,installment_override,overpayment_mode,overpayment_amount,overpayment_strategy,archived_at
d7e8f9a0-b1c2-47d8-ae4a-c5d6e7f8091a,Home mortgage,annuity,EUR,300000.00,2026-01-01,2026-02-01,360,1,fixed,monthly,full_at_origination,0,,,,c4d5e6f7-a8b9-4c0d-9e1f-2a3b4c5d6e7f,Car,true,,fixed_extra,100.00,reduce_term,
e8f9a0b1-c2d3-48e9-bf5b-d6e7f8091a2b,Car loan,annuity,EUR,50000.00,2026-01-15,2026-02-15,24,15,variable,actual_365,tranches,0,150,b5c6d7e8-f9a0-45b6-8c28-a3b4c5d6e7f8,WIBOR,,,false,,none,,reduce_term,
f9a0b1c2-d3e4-49fa-806c-e7f8091a2b3c,Phone,installment_plan,EUR,3000.00,,2026-04-05,3,5,fixed,monthly,full_at_origination,0,,,,,,false,,none,,reduce_term,
```

A `tranches` loan records each transfer in `loan_transfers.csv`; the sum may not exceed
`principal_original`, and the undrawn part is not a debt. `full_at_origination` treats the
whole original principal as drawn on `origination_date`. `interest_only_months` is part of
`term_months`; the first actual transfer starts the interest-only phase.

## `loan_rate_changes.csv` (restore only)

Each row starts a rate segment: a fixed rate or a reference rate plus margin. A fixed row has
`rate_bp`; a variable row has `reference_rate_id` and `margin_bp` instead.

| Column | Required | Values |
|---|---|---|
| `id` | yes | UUID (lineage id) |
| `loan_id` | yes | UUID from `loans.csv` |
| `loan` | yes | the loan name (must match `loan_id`) |
| `effective_from` | yes | `YYYY-MM-DD`; one row per loan and date |
| `rate_bp` | fixed only | integer 0–10 000 (annual basis points) |
| `mode` | in 1.6 | `fixed` (default for older archives) \| `variable` |
| `reference_rate_id` | variable only | UUID from `reference_rates.csv` |
| `reference_rate` | variable only | the reference rate name (must match the id) |
| `margin_bp` | variable only | integer 0–10 000 (annual basis points) |

Export order: loan order, effective date, id.

```csv
id,loan_id,loan,effective_from,rate_bp,mode,reference_rate_id,reference_rate,margin_bp
a0b1c2d3-e4f5-4a0b-917d-f8091a2b3c4d,d7e8f9a0-b1c2-47d8-ae4a-c5d6e7f8091a,Home mortgage,2026-01-01,575,fixed,,,
b1c2d3e4-f5a6-4b1c-928e-a8091a2b3c4d,e8f9a0b1-c2d3-48e9-bf5b-d6e7f8091a2b,Car loan,2027-01-15,,variable,b5c6d7e8-f9a0-45b6-8c28-a3b4c5d6e7f8,WIBOR,150
```

## `loan_transfers.csv` (restore only)

| Column | Required | Values |
|---|---|---|
| `id` | yes | UUID (lineage id) |
| `loan_id` | yes | UUID of a `tranches` row in `loans.csv` |
| `loan` | yes | the loan name (must match `loan_id`) |
| `date` | yes | `YYYY-MM-DD`; on or after the loan start and before its final payment |
| `amount` | yes | decimal > 0; all transfers together may not exceed `principal_original` |
| `note` | no | up to 200 characters |

Export order: loan order, date, id. Future-dated rows are allowed: they affect projections and
the schedule but not today's balance.

```csv
id,loan_id,loan,date,amount,note
b0c1d2e3-f4a5-4b6c-9d7e-8192a3b4c5d6,e8f9a0b1-c2d3-48e9-bf5b-d6e7f8091a2b,Car loan,2026-01-15,50000.00,First tranche
```

## `loan_cost_components.csv` (restore only)

| Column | Required | Values |
|---|---|---|
| `id` | yes | UUID (lineage id) |
| `loan_id` | yes | UUID from `loans.csv` |
| `loan` | yes | the loan name (must match `loan_id`) |
| `name` | yes | 1–200 characters |
| `mode` | yes | `percent` \| `fixed` |
| `rate_bp` | percent only | integer 0–10 000 (annual basis points) |
| `fixed_amount` | fixed only | decimal > 0 |
| `basis` | yes | `opening_balance` |
| `effective_from` | yes | `YYYY-MM-DD` |
| `effective_to` | no | `YYYY-MM-DD`, on or after `effective_from` |

Export order: loan order, effective date, name, id.

```csv
id,loan_id,loan,name,mode,rate_bp,fixed_amount,basis,effective_from,effective_to
b1c2d3e4-f5a6-4b1c-828e-091a2b3c4d5e,d7e8f9a0-b1c2-47d8-ae4a-c5d6e7f8091a,Home mortgage,Account fee,percent,20,,opening_balance,2026-01-01,
```

## `loan_overpayments.csv` (restore only)

| Column | Required | Values |
|---|---|---|
| `id` | yes | UUID (lineage id) |
| `loan_id` | yes | UUID from `loans.csv` |
| `loan` | yes | the loan name (must match `loan_id`) |
| `date` | yes | `YYYY-MM-DD` |
| `amount` | yes | decimal > 0 |
| `note` | no | at most 200 characters |

Export order: loan order, date, id. These are planned one-off overpayments, not actual payments.

```csv
id,loan_id,loan,date,amount,note
c2d3e4f5-a6b7-4c2d-939f-1a2b3c4d5e6f,d7e8f9a0-b1c2-47d8-ae4a-c5d6e7f8091a,Home mortgage,2026-06-01,5000.00,Bonus
```

## `loan_payments.csv` (restore only)

| Column | Required | Values |
|---|---|---|
| `id` | yes | UUID (lineage id) |
| `loan_id` | yes | UUID from `loans.csv` |
| `loan` | yes | the loan name (must match `loan_id`) |
| `date` | yes | `YYYY-MM-DD` |
| `kind` | yes | `installment` \| `overpayment` |
| `total` | yes | decimal > 0 |
| `principal` | yes | decimal ≥ 0 |
| `interest` | yes | decimal ≥ 0 |
| `costs` | yes | decimal ≥ 0 |
| `overpayment` | yes | decimal ≥ 0; `total` must equal their sum |
| `note` | no | at most 200 characters |
| `source` | yes | `manual` \| `upcoming` |
| `transaction_id` | no | transaction lineage id from `transactions.csv` |

Export order: loan order, date, entry order, id. The transaction id is mapped to the restored
ledger row; an archive that names a missing transaction is refused.

```csv
id,loan_id,loan,date,kind,total,principal,interest,costs,overpayment,note,source,transaction_id
d3e4f5a6-b7c8-4d3e-84a0-2b3c4d5e6f70,d7e8f9a0-b1c2-47d8-ae4a-c5d6e7f8091a,Home mortgage,2026-03-01,installment,2000.00,1000.00,900.00,0.00,100.00,,manual,5c7d8e9f-0a1b-4c2d-9e3f-4a5b6c7d8e02
```

## `loan_balance_checkpoints.csv` (restore only)

| Column | Required | Values |
|---|---|---|
| `id` | yes | UUID (lineage id) |
| `loan_id` | yes | UUID from `loans.csv` |
| `loan` | yes | the loan name (must match `loan_id`) |
| `as_of` | yes | `YYYY-MM-DD`; one checkpoint per loan and date |
| `remaining_principal` | yes | decimal ≥ 0 |
| `note` | no | at most 200 characters |

Export order: loan order, date, id.

```csv
id,loan_id,loan,as_of,remaining_principal,note
e4f5a6b7-c8d9-4e4f-95b1-3c4d5e6f7081,d7e8f9a0-b1c2-47d8-ae4a-c5d6e7f8091a,Home mortgage,2026-07-01,280000.00,Bank statement
```

## `loan_scenarios.csv` (restore only)

| Column | Required | Values |
|---|---|---|
| `id` | yes | UUID (lineage id) |
| `loan_id` | yes | UUID from `loans.csv` |
| `loan` | yes | the loan name (must match `loan_id`) |
| `name` | yes | 1–200 characters; unique per loan, case-folded |
| `overpayment_mode` | no | `none` \| `fixed_extra` \| `target_total`; all three overpayment fields are together |
| `overpayment_amount` | with a non-`none` mode | decimal > 0 |
| `overpayment_strategy` | no | `reduce_term` \| `reduce_installment` |
| `rate_shift_bp` | no | integer −10 000 to 10 000, not 0; requires `rate_shift_from` |
| `rate_shift_from` | no | `YYYY-MM-DD` |
| `one_off_date` | no | `YYYY-MM-DD`; requires `one_off_amount` |
| `one_off_amount` | no | decimal > 0 |

Export order: loan order, name, id.

```csv
id,loan_id,loan,name,overpayment_mode,overpayment_amount,overpayment_strategy,rate_shift_bp,rate_shift_from,one_off_date,one_off_amount
f5a6b7c8-d9e0-4f50-86c2-4d5e6f708192,d7e8f9a0-b1c2-47d8-ae4a-c5d6e7f8091a,Home mortgage,Faster,fixed_extra,300.00,reduce_term,,,,
```

## `installment_plans.csv` (restore only)

| Column | Required | Values |
|---|---|---|
| `id` | yes | UUID (lineage id) |
| `loan_id` | yes | UUID of an `installment_plan` row in `loans.csv`; one plan per loan |
| `loan` | yes | the loan name (must match `loan_id`) |
| `category_id` | yes | UUID from `categories.csv` |
| `category` | yes | the category name (must match `category_id`) |
| `installment` | yes | decimal > 0, the fixed monthly installment |
| `remaining_count` | yes | integer 0–600; 0 means closed |
| `end_date` | yes | `YYYY-MM-DD` |
| `closed_at` | no | timestamp; set = closed |

Export order: loan order, id. A plan has no interest schedule; its net-worth liability is
`installment × remaining_count`.

```csv
id,loan_id,loan,category_id,category,installment,remaining_count,end_date,closed_at
a6b7c8d9-e0f1-4a61-97d3-5e6f708192a3,f9a0b1c2-d3e4-49fa-806c-e7f8091a2b3c,Phone,2b0e8c1e-5f7a-4c6e-9a51-3f1d2c7b9e01,Food,1000.00,3,2026-06-05,
```

## `fire_scenarios.csv` (restore only)

A FIRE plan, its rates, the three starting amounts and how each amount is resolved. The
currency is the scenario's own; it may differ from the archive base currency, but then every
linked starting value falls back on restore.

| Column | Required | Values |
|---|---|---|
| `id` | yes | UUID (lineage id) |
| `name` | yes | 1–200 characters; active names are unique ignoring case |
| `currency` | yes | ISO 4217 code |
| `birth_year` | yes | integer 1900–2199, not after `start_year` |
| `start_year` | yes | integer 1900–2199 |
| `inflation_bp` | yes | integer −2000 to 5000 (basis points) |
| `real_return_bp` | yes | integer −2000 to 5000 |
| `income_growth_bp` | yes | integer −2000 to 5000 |
| `stop_working_age` | yes | integer 0–120, not before the start age |
| `horizon_age` | yes | integer 1–120, after the start age; the last year stays within 2199 |
| `rule` | yes | `returns_cover_expenses` \| `swr` |
| `swr_bp` | with the `swr` rule | integer 50–2000; blank for `returns_cover_expenses` |
| `income_mode` | yes | `manual` \| `trailing_12m` |
| `income_manual` | with `manual` | decimal ≥ 0; blank for a linked mode |
| `income_fallback` | linked only | decimal ≥ 0 resolved at export, or blank when export could not resolve it |
| `expenses_mode` | yes | `manual` \| `trailing_12m` |
| `expenses_manual` | with `manual` | decimal ≥ 0; blank for a linked mode |
| `expenses_fallback` | linked only | decimal ≥ 0 resolved at export, or blank when blocked |
| `portfolio_mode` | yes | `manual` \| `linked_investable` |
| `portfolio_manual` | with `manual` | decimal ≥ 0; blank for a linked mode |
| `portfolio_fallback` | linked only | decimal ≥ 0 resolved at export, or blank when blocked |
| `is_default` | no (default `false`) | `true` \| `false`; an archived scenario cannot be the default, and at most one active default is allowed |
| `archived_at` | no | timestamp; set = archived |

Export order: active before archived, name key, lineage id. A linked starting value is exported
as its resolved value in the scenario currency, so a restore can still show a sensible amount
when the archive lacks the ledger, portfolio or price data. The fallback is blank when the
export itself was blocked (for example no data in the trailing window); a restore then uses the
archived fallback, or 0 with a warning when it is blank too. The amount columns are validated
in the scenario's currency, never the archive base currency.

```csv
id,name,currency,birth_year,start_year,inflation_bp,real_return_bp,income_growth_bp,stop_working_age,horizon_age,rule,swr_bp,income_mode,income_manual,income_fallback,expenses_mode,expenses_manual,expenses_fallback,portfolio_mode,portfolio_manual,portfolio_fallback,is_default,archived_at
d1e2f3a4-b5c6-4d7e-8f90-1a2b3c4d5e6f,Lean FIRE,EUR,1985,2026,250,500,300,55,90,swr,400,manual,3000.00,,manual,2000.00,,manual,100000.00,,true,
e2f3a4b5-c6d7-4e8f-90a1-2b3c4d5e6f70,Coast plan,EUR,1990,2026,250,500,300,60,95,returns_cover_expenses,,trailing_12m,,2500.00,trailing_12m,,1800.00,linked_investable,,80000.00,false,2026-09-30T20:00:00Z
```

## `fire_contributions.csv` (restore only)

A yearly net-saving override for one scenario. The amount is signed: positive saves, negative
dissaves.

| Column | Required | Values |
|---|---|---|
| `id` | yes | UUID (lineage id) |
| `scenario_id` | yes | UUID from `fire_scenarios.csv` |
| `scenario` | yes | the scenario name (must match `scenario_id`) |
| `year` | yes | the year after the start through the horizon year |
| `net_amount` | yes | signed decimal in the scenario currency; at most its minor digits |

Export order: scenario order, year, lineage id. A scenario has at most one contribution per
year.

```csv
id,scenario_id,scenario,year,net_amount
f3a4b5c6-d7e8-4f90-a1b2-3c4d5e6f7081,d1e2f3a4-b5c6-4d7e-8f90-1a2b3c4d5e6f,Lean FIRE,2027,1200.00
```

## `fire_events.csv` (restore only)

A one-off cash flow or a permanent annual expense/income change in a plan year.

| Column | Required | Values |
|---|---|---|
| `id` | yes | UUID (lineage id) |
| `scenario_id` | yes | UUID from `fire_scenarios.csv` |
| `scenario` | yes | the scenario name (must match `scenario_id`) |
| `year` | yes | the start year through the horizon year |
| `kind` | yes | `cash` \| `expense_change` \| `income_change` |
| `amount` | yes | signed decimal in the scenario currency; at most its minor digits |
| `label` | yes | 1–200 characters |
| `source_loan_id` | no | loan lineage id of a one-click payoff event |

Export order: scenario order, year, lineage id. A missing source loan clears the link and keeps
the event, with a restore warning.

```csv
id,scenario_id,scenario,year,kind,amount,label,source_loan_id
a4b5c6d7-e8f9-4a01-b2c3-4d5e6f708192,d1e2f3a4-b5c6-4d7e-8f90-1a2b3c4d5e6f,Lean FIRE,2030,cash,-5000.00,Home renovation,d7e8f9a0-b1c2-47d8-ae4a-c5d6e7f8091a
```

## `fire_expense_exclusions.csv` (restore only)

Regular expense categories left out of a scenario's linked trailing expenses.

| Column | Required | Values |
|---|---|---|
| `id` | yes | UUID (lineage id) |
| `scenario_id` | yes | UUID from `fire_scenarios.csv` |
| `scenario` | yes | the scenario name (must match `scenario_id`) |
| `category_id` | yes | UUID from `categories.csv`; a missing, wrong-kind or non-regular category skips the exclusion on restore |
| `category` | yes | the category name (must match `category_id` when it is present) |

Export order: scenario order, category order, lineage id. A scenario excludes a category at
most once. A category missing from the archive, or one that is not a regular expense category,
is skipped with a warning; a present category whose name differs from the `category` companion
is an archive error. If the scenario's expenses are linked, the skipped exclusion makes them
fall back to the archived manual amount rather than silently changing the total.

```csv
id,scenario_id,scenario,category_id,category
b5c6d7e8-f9a0-4b12-83c4-5d6e7f8091a2,e2f3a4b5-c6d7-4e8f-90a1-2b3c4d5e6f70,Coast plan,2b0e8c1e-5f7a-4c6e-9a51-3f1d2c7b9e01,Food
```

## Reader limits


An uploaded archive is never extracted to disk. It is refused when it has members in folders or
with unsafe names, symlinks, encrypted members, compression methods other than stored and
deflate, duplicate names, more than 64 members, a member over 100 MiB or a total over 200 MiB
uncompressed, or a member (1 MiB or larger) compressed more than 200:1. Unknown member names are
listed as warnings and ignored. A file may hold at most 1 000 000 data rows and 64 columns.

## Not in the archive (format 1.8)

Members, invitations, users and their sessions, the audit log, import batches and saved
mappings, and who created or closed what. Recurring items, income sources, work logs and
calendars, and tax rules are added by a later archive extension as new files (a minor version);
until then, a restore requires a workspace without them. Notification channels are never
exported, because their URLs can contain secrets. Provider error logs are operational and not
portable. FIRE projections, sensitivity grids and loan schedules are derived data: they are
recomputed from the archived inputs and are never stored in the archive.

The profile's `birth_year` and `life_expectancy` are user data, not workspace data, so they are
not archived. `investment_cost_basis` and `risk_target_policy` are workspace settings and live
in `workspace.json`.

Net-worth, investment, loan and FIRE files are restore-only in a full archive merge: they are
ignored and listed once as a warning (`networth_restore_only`, `investment_restore_only`,
`loan_restore_only` and `fire_restore_only`). A lone `item_valuations.csv`, a lone
`investment_transactions.csv` and a lone `reference_rate_values.csv` can be merged. The other
loan and FIRE files are refused as standalone uploads (`loans.csv` alone has no importer), because
their rows reference loans, scenarios and planned values that a merge does not carry.
