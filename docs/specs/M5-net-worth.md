# M5 — Net worth

**Goal:** list what you own and what you owe, grouped into categories, compare the two, and track net worth over time.

**Exit criteria:** assets and liabilities (manual values with history), category groupings, a summary with net worth, and monthly snapshots producing a trend chart. Linked valuations are *designed for* here and *filled in* by M6 (portfolio) and M7 (loans).

Every agent prompt starts with: *"Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go)."*

**Data model (sketch)**
- `NetWorthCategory(id, workspace_id, side[asset|liability], name, color, sort_order, archived_at)`
- `NetWorthItem(id, workspace_id, side, category_id, name, currency, notes, valuation_mode[manual|linked], link_type?, link_id?, include_in_net_worth bool default true, investable bool default false, estimate_between bool (F5.6), acquired_on?, archived_at)` — `currency` is the item's source of truth.
- `ItemValuation(id, workspace_id, item_id, as_of date, value_minor, currency, note)` — append-only; "current manual value" = latest `as_of ≤ today`. Every valuation uses its item's currency.
- `NetWorthSnapshot(id, workspace_id, as_of date, assets_minor, liabilities_minor, net_minor, currency, breakdown JSON[{category_id, side, total_minor}], source[auto|manual|backfill])`; unique `(workspace_id, as_of, source)`.
- `link_type` values reserved now: `portfolio_asset_class`, `portfolio_holding`, `loan_balance`. Until the owning module exists, the UI does not offer linked mode.

---

## F5.1 Categories and items (assets and liabilities)

**Story:** I maintain a list of my possessions and debts, grouped by category.

**Acceptance**
- Two tabs (Assets, Liabilities) each listing items grouped under their categories with subtotals; add/edit/archive items inline; category CRUD (separate category sets for assets and liabilities), reorder.
- Item fields: name, category, current value, currency (base currency by default), notes, optional acquired date, `include_in_net_worth`, `investable` (feeds M8).
- Changing a manual value appends an `ItemValuation` (with optional note); an "as of" date can be set (back-dating allowed).
- Valuation history shown per item (small table + sparkline).
- Bulk add: paste multiple lines (`name; value; category`) into a textarea → preview → create.
- Seed categories (en + pl): assets — Real estate (F5.7), Vehicles, Electronics, Appliances, Furniture, Jewelry & watches, Art & collectibles, Tools, Other; liabilities — Installments, Mortgage, Other.
- Soft delete/archive keeps history.

**Implementation notes (F5.1).**
- Values are always positive, in the item's currency. A liability's `value_minor` is the amount
  still owed, never a negative number; F5.2 subtracts liabilities.
- An item that is not archived counts towards net worth even when its **category is archived**.
  Its items stay visible under an archived heading until each item is archived. F5.2 must follow
  the same rule.
- The item's currency is the source of truth; valuations share it (enforced by a composite
  foreign key) and it cannot change once a valuation exists.
- An item with `include_in_net_worth=false`, a non-base currency, or no effective valuation is
  listed but excluded from the category subtotal (and from F5.2 totals).
- Seeds: new workspaces get both sets in the workspace-creation transaction, recorded as
  `networth_category.seeded`; an existing workspace can add the suggested set to an empty side.
  There is no migration/backfill.
- Archiving a category with items is allowed; deleting it archives it. Restoring appends it to
  the end of its side and is refused on an active name clash.
- Bulk add accepts at most 500 non-blank lines and 64 KiB of text, previews every line, and is
  all-or-nothing.
- Inline edit and add-value swaps keep the item at the same viewport position (`data-keep-visible`
  in `app.js`); they do not force the row to the top, so saving from the middle of a long list
  does not jump the page.
- F5.1 creates manual valuations only. The link columns are reserved for F5.4.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F5.1 from docs/specs/M5-net-worth.md: NetWorthCategory, NetWorthItem, ItemValuation models + migration, workspace-scoped repositories/services,
grouped lists with subtotals and inline editing (HTMX), append-only valuation history, bulk-paste add with preview, seed categories (en/pl).
Money is Money only. Tests: valuation append semantics, back-dated valuations, current value resolution, archive, role checks.
```

---

## F5.2 Summary: assets vs liabilities and net worth

**Acceptance**
- Pure domain function `summarize_net_worth(items_with_values) -> NetWorthSummary`: total assets, total liabilities, net worth, per-category totals and shares; items with `include_in_net_worth=false` excluded.
- Items under an **archived category** still count while the item itself is not archived (F5.1 rule).
- Summary page: big net-worth figure, assets-vs-liabilities bar, donut of assets by category, table of categories with totals and % of assets; liabilities likewise.
- "Debt-to-assets ratio" and "liquid vs illiquid" are **not** required (do not implement).
- Currency conversion for non-base-currency items uses the FX service if M6 exists; before that, non-base items are shown separately with a warning and excluded from totals (documented limitation).

**Implementation notes (F5.2).**
- `GET /net-worth/summary` is its own page; the sidebar points to it and both pages share the
  Summary / Assets / Liabilities tabs. F5.1 URLs stay unchanged.
- The shared read path is `services.networth.current_values(session, ctx, *, as_of, ...)`; the
  list uses history, the summary uses current values only, and F5.3 can pass a month-end date.
  A valuation after `as_of` is ignored.
- `summarize_net_worth` is pure and works on item IDs, sides and `Money`; names and colours are
  added in the web layer.
- Counting order: archived item → excluded → no value → non-base → counted. Items under an
  archived category still count.
- Shares are of each side's own total, in once-rounded tenths of a percent, using the existing
  share helper. A side total of zero gives `None` and a displayed dash.
- Categories with no counted item are left out. Zero-valued counted items remain in the table
  but have no share.
- The assets-vs-liabilities bar is server-rendered SVG with integer widths. Donuts reuse the
  shared top-N plus “Other categories” slicer and ECharts `charts.js`; the grouped slice is
  labelled “Other categories” because “Other” is a real seed category.
- Excluded items are a neutral note; non-base and no-value items use warning tones. No FX, no
  base-currency lock, no writes/audit, no migration.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F5.2 from docs/specs/M5-net-worth.md: pure summarize_net_worth domain function, summary page with totals, bar, donut (ECharts) and category tables.
Property tests: net = assets - liabilities; category totals sum to side totals; excluded items never counted.
```

---

## F5.3 Snapshots and net-worth history

**Story:** I see how net worth changed over time.

**Acceptance**
- Monthly APScheduler job (1st of month 04:00 workspace time) stores a `NetWorthSnapshot` (values as of the last day of the previous month). Idempotent.
- "Take snapshot now" button (editor+); the latest manual snapshot for a date replaces the previous manual one for the same date.
- **Backfill** from valuation history: for each past month-end, compute from `ItemValuation`s (latest valuation ≤ date); marked `source=backfill`. It replaces automatic rows in its range; manual rows stay immutable.
- Line chart of net worth (with assets and liabilities lines toggleable) over time; range selector (1y/3y/5y/all); hover shows the breakdown by category.
- Linked items are included in snapshots using their value at that moment (once M6/M7 exist); if a link cannot be resolved the snapshot records the item as "unavailable" and the report flags it.

**Owner decision (2026-11-02).** An item counts up to its archive date. At month-end `D` it is
excluded only when its `archived_at` falls on or before `D` in the workspace time zone; a restored
item counts in every month. Today's summary keeps the current archived rule, which is equivalent
because an archive date is never after today. `include_in_net_worth` has no history: the current
setting applies to every date, and changing it affects future snapshots and a rerun of the
backfill, never stored manual snapshots.

**Implementation notes (F5.3).**
- `NetWorthSnapshot` stores `as_of`, assets/liabilities/net minor units, currency, a versioned
  breakdown object and `source` (`auto|manual|backfill`). `UNIQUE(workspace_id, as_of, source)`
  is the race guard; `net_minor = assets_minor - liabilities_minor`.
- `domain.networth_history.values_at` selects values from rows loaded once; it is the pure path
  under `services.networth.current_values`, the auto job and the backfill. A backfill therefore
  reads items, categories and valuations once rather than once per month.
- Auto: 1st of the month at 04:00 workspace time for the previous month-end; skipped entirely
  when the workspace has no net-worth items (archived items count as items), when no item had an
  effective value on that date yet, or when any snapshot already exists for that date. Manual:
  workspace today, replaces the same date's manual row, stores zero for an empty workspace.
  Backfill: from the earliest valuation's month-end through the previous month-end; deletes and
  recreates `source=auto` and `source=backfill` rows in its range and leaves manual rows alone.
  Re-running is allowed; a rebuilt row has `source=backfill`.
- Startup catch-up creates only the most recent previous month-end's automatic snapshot when the
  date has no snapshot; older missed months come from backfill. The job logs the workspace id and
  exception type only.
- Source priority is manual > auto > backfill. Backfill replaces automatic and backfill rows in
  its range; the monthly job never replaces an existing backfill row. A manual snapshot replaces
  the manual and backfill rows for its date and leaves auto.
- Snapshot writes run in `run_immediate`, check inside the transaction, take the workspace lock
  and treat a unique-constraint race as "already taken". A future `as_of` is refused.
- History is `GET /net-worth/history`, the fourth tab, with server-side
  `?range=1y|3y|5y|all` (default `1y`). The ECharts line chart plots only snapshots in the current
  base currency; other-currency snapshots stay in the table with their currency and a note. The
  legend toggles net worth, assets and liabilities; tooltips use current category names with a
  “Deleted category” fallback and are formatted on the server.
- Breakdown shape: `{"v": 1, "categories": [{"category_id", "side", "total_minor"}],
  "not_counted": {"non_base": n, "no_value": n}}`. The web layer ignores unknown keys so F5.4 can
  add an “unavailable” marker without a migration.
- Audit: `networth_snapshot.created` (system), `networth_snapshot.taken` (user, `replaced` flag)
  and `networth_snapshot.backfilled` (`from`, `to`, `count`); no amounts.
- Linked items remain F5.4. F5.3 snapshots manual values only. `NetWorthSnapshot` is in
  `ARCHIVE_EXCLUDED_MODELS` for F5.5.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F5.3 from docs/specs/M5-net-worth.md: NetWorthSnapshot model + migration, monthly job (idempotent), manual snapshot button,
backfill from ItemValuation history (source=backfill), history chart with range selector and category breakdown tooltip.
Tests with the fixed Clock: month-end selection, idempotency, backfill semantics, replacement rules.
```

---

## F5.4 Link framework (for portfolio and loans)

**Acceptance**
- `ValuationResolver` protocol in `services/networth_links.py`: factory-bound per workspace, with
  `side`, `resolve_many(link_ids, dates) -> {(link_id, date): Money | Unavailable}`,
  `describe(link_id)` and `choices()`; a registry where later modules register resolvers.
- UI: item form shows "Link to…" only for registered link types; linked items display value read-only with a "linked to <name>" chip and a click-through.
- A linked item cannot be given manual valuations; unlinking converts to manual keeping the last resolved value as a first valuation.
- Tests with a fake resolver.

**Implementation notes (F5.4).**
- The framework lives in `services/networth_links.py`. A resolver is registered as a
  `(session, workspace scope) -> resolver` factory so target lookups are workspace-scoped.
  Resolvers declare their `side`, implement `resolve_many(link_ids, dates)` for batched
  historical resolution, `describe(link_id)` for the label/URL/currency, and `choices()` for
  the form picker. The production registry is empty until M6/M7 register their resolvers.
- The picker is shown only for the item side with a registered resolver and offers “Manual
  value” by default. Linking happens at creation. An existing manual item with valuation
  history must be archived and replaced by a linked item; archive-date snapshots keep its
  history continuous.
- A linked item has no manual valuations and shows its value read-only with a “Linked to …”
  chip, optional click-through and target currency. Unknown targets, removed resolver modules
  and resolved values in another currency are `Unavailable`, never zero.
- Unlinking converts the item to manual. If today’s value resolves, it is appended as the first
  manual valuation dated today; if not, the item becomes manual with no value. Audit records
  `networth_item.linked` (`link_type`, target label) and `networth_item.unlinked`
  (`value_recorded`), without amounts.
- Migration `0035_f54_net_worth_links` adds a partial unique index on
  `(workspace_id, link_type, link_id)` for active linked items, on SQLite and PostgreSQL.
  Duplicate creation returns `already_linked`; restoring an archived linked item is refused
  while another active item links to the same target.
- Auto, manual and backfill snapshots resolve linked values at their `as_of`; a backfill makes
  one `resolve_many` call per link type. The breakdown keeps `v: 1` and adds
  `not_counted.unavailable`; unavailable linked items do not make an auto snapshot eligible.
- Follow-up: list pages still call `describe` once per linked item. A batched `describe_many`
  on the protocol would let M6 avoid one lookup per holding; not required for F5.4.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F5.4 from docs/specs/M5-net-worth.md: the ValuationResolver registry and the linked-item UI/service behaviour described.
Only a fake resolver exists now; real ones are added in M6 and M7. Tests for link/unlink semantics and the 'unavailable' path.
```

---

## F5.5 Archive extension: net worth

Replaces the former workbook "importer extension" (M4 no longer has a spreadsheet importer; owner
decision, 2026-10-18).

**Acceptance**
- New archive files (a minor format version per ADR-0016) with lineage `id` columns and
  references by id plus name as in M4: `networth_categories.csv`, `networth_items.csv`
  (including `valuation_mode`, `link_type` and the link target's lineage id), `item_valuations.csv`,
  `networth_snapshots.csv` (breakdown as one row per category, or a JSON column; decide in the plan).
- Restore recreates them in dependency order; a linked item whose target is not in the archive
  (its module's files missing) is restored as manual and listed.
- Standalone import in merge mode: `item_valuations.csv` (by item name; same date and value =
  duplicate, different value = conflict, listed), so a valuation history kept elsewhere can be
  brought in. Other files are restore only.
- The new models join `EMPTINESS_CHECKS`; the M4 round-trip test covers them.

**Implementation notes (F5.5).**
- The archive format is 1.2. `networth_categories.csv`, `networth_items.csv`,
  `item_valuations.csv` and `networth_snapshots.csv` are required in 1.2 and absent from
  1.0/1.1 archives, which read with empty net-worth data. The old golden archives stay;
  `workspace-archive-v1.2.zip` is added.
- Snapshot `breakdown` is one JSON column, compact with sorted keys. Category ids in it are
  lineage ids on export and are mapped back to local ids on restore; unknown ids are kept.
- `item_valuations.csv` carries `created_at` so same-day entry order survives restore. A lone
  file without it is stamped from the import time in row order.
- A lone `item_valuations.csv` matches by `item_id` first and by name otherwise. A linked item
  is refused with `item_linked`; ambiguous names use `item_ambiguous`; the docs point to
  `item_id`. The currency must equal the item's and use exactly its minor digits; future dates
  are allowed. Duplicates and conflicts are judged against stored values at write time.
- Linked items restore as manual with the link cleared and no value, listed as
  `link_target_missing`; export still writes `link_type`/`link_id` for the M6/M7 extensions.
- All four models have `import_key`/`import_batch_id` with partial unique indexes on SQLite and
  PostgreSQL. Restore removes a batch's append-only valuations through an explicit
  workspace-and-batch-scoped SQL delete; repository `update`/`delete` stay unsupported.
- Untouched suggested net-worth categories use the ledger rule (the current seed or the seed before F5.7, without Real estate, both count): automatic
  `networth_category.seeded`, exact suggested set, unedited, unused, within five seconds. A
  later manual seed makes them user data.
- A full archive merge is restore-only for net-worth files and lists that once; only a lone
  `item_valuations.csv` merges. All four models join `EMPTINESS_CHECKS` and restore rollback.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F5.5 from docs/specs/M5-net-worth.md as an extension of the M4 archive (docs/specs/M4-ledger-export-import.md,
ADR-0016): the new files with lineage ids, export, restore order incl. links, standalone item_valuations.csv import,
emptiness checks and the extended round-trip test.
```

---

## F5.6 Estimated values between entries (backfill)

Owner decision, 2026-11-03: backfill estimates the months between two recorded values with a
straight line, switchable per item.

**Story:** my car was worth 100 000 four years ago and is worth 60 000 now. I recorded only those
two values, but the history should show it losing value month by month, not a flat line with
one drop.

**Rule**
- An item has a switch, **Estimate values between entries** (`estimate_between`).
- With the switch on, a backfilled month-end `D` that falls strictly between two of the item's
  recorded values gets a straight-line estimate:
  `v0 + (v1 − v0) × (D − d0) / (d1 − d0)`.
  - `d0` is the latest recorded date before `D` and `v0` its value.
  - `d1` is the earliest recorded date after `D` and `v1` its value.
  - The fraction is counted in calendar days.
  - The result is rounded half up to the currency's minor unit once, using exact integer
    arithmetic (no float).
- The value used for a date is the item's effective value on that date (the latest entry on
  that day, F5.1 order).
- Example: 100 000.00 on 2022-11-03 and 60 000.00 on 2026-11-03 give about 90 000 after one
  year, 80 000 after two and 70 000 after three.
- Nothing is estimated before the first recorded value or after the last one:
  - before the first value the item has no value (F5.3);
  - after the last value the last value carries forward (F5.3).
- `d1` must be on or before today in the workspace's time zone. A future-dated value is never
  used as an end point.
- A month-end that equals a recorded date uses that recorded value; it is not an estimate.
- With the switch off, an item keeps the F5.3 rule (latest value on or before `D`).

**Where it applies**
- **Backfilled snapshots** (`source=backfill`). They replace automatic rows in the backfill range
  (owner decision, 2026-11-04); manual snapshots stay exactly as recorded.
- The item's current value, Assets/Liabilities lists, summary (F5.2), valuation history table and
  sparkline stay unchanged.
- Linked items (F5.4) ignore the switch, because their resolver supplies real values.
- The archive rule (F5.3) and the include setting still apply as before.
- Changing the switch does not rewrite anything by itself. The item form says that a re-run of
  **Backfill from history** applies it to past months.

**Defaults**
- New asset items: **on**, including items created by bulk add.
- New liability items: **off**. Manual debts usually change in steps, and loans become linked
  items in M7.
- The migration sets existing items the same way (assets on, liabilities off).
- The switch is editable on every manual item at any time. It is hidden on linked items.

**Marking estimates**
- The snapshot breakdown gets one additive key: `not_counted` stays as it is, and a top-level
  `"estimated": n` counts the items whose value at that date was estimated. `v` stays 1, and
  readers ignore unknown keys (F5.3).
- The history table marks a backfilled row that contains estimates ("includes 3 estimated
  values"), and the chart tooltip shows the same line.
- Points are not drawn differently. The table note is the accessible marker.

**Acceptance**
- `NetWorthItem.estimate_between` boolean, not null; migration with server defaults by side.
- Item form checkbox with help text, on create and edit for manual items. It is shown in the
  item row's details, not as a separate column. Changing it is recorded in the
  `networth_item.updated` fields; the value is not audited.
- A pure domain function (in `domain/networth_history.py`) returns an item's value on `D` and
  whether it was estimated, given the item's ordered valuation points, `D`, today and the switch.
  `values_at` uses it when called for backfill, and its other callers keep the F5.3 behaviour.
- Backfill uses it. Automatic snapshots created by the monthly job do not estimate; a backfill
  run replaces automatic rows in its range with estimates. Manual snapshots never estimate. The
  breakdown carries `estimated`.
- History table and tooltip show the estimated count.
- Archive: `networth_items.csv` gains the `estimate_between` column (`true|false`) as a minor
  format version (1.3, ADR-0016).
  - A 1.2 archive reads without it, and restore applies the side defaults above.
  - Document it in `docs/archive-format.md`, with the version history, and update the golden
    archive and the docs test.
- Docs: `docs/net-worth.md` explains the switch, the straight-line rule, the defaults, that only
  backfill uses it, and that backfill must be re-run after changing it.

**Tests (golden oracle: a hand-computed table in the test module)**
- The example above at every month-end between the two dates, exact minor units.
- An end point exactly on a month-end, a same-day correction as an end point (the later entry
  wins), a rising value, a value of 0 at either end, and a JPY item (0 minor digits).
- Switch off: identical to F5.3 for the same data.
- No estimate before the first value or after the last.
- A future-dated value is never used as `d1`.
- Linked items ignore the switch.
- An archived item is still excluded from its archive date.
- Auto and manual snapshots are unchanged by the switch.
- Property tests: an estimate lies between `v0` and `v1`, is monotonic between them, and equals
  `v0`/`v1` at `d0`/`d1`. Rounding happens once.
- Defaults by side on create, on bulk add, in the migration and on restore of a 1.2 archive.
- Archive round trip keeps the switch.

**Implementation notes (F5.6).**
- `NetWorthItem.estimate_between` is non-null; migration `0037_f56_estimate_between` adds it
  with `server_default=sa.true()`, sets liabilities to `sa.false()`, then removes the default.
  New assets (including bulk add) default on and liabilities off; the item form edits it on
  manual items and hides it on linked items.
- `domain.networth_history.value_on` returns `(value_minor, estimated)` and uses the effective
  value on `d0`/`d1` (latest entry that day). It only interpolates strictly between them, never
  before the first value, and never uses an end date after today. The formula is
  `(v0 × (d1 − D) + v1 × (D − d0)) / (d1 − d0)` with day ordinals, rounded half up once with
  `(2·num + den) // (2·den)`.
- `values_at(backfill=True, today=...)` uses the switch only for backfill; every other caller
  keeps F5.3. Auto and manual snapshots therefore ignore the switch.
- The breakdown always writes `estimated`; it counts only items actually in the snapshot totals
  (not archived, included, base currency, with a value).
- The history table and tooltip show “includes N estimated values”. The table note is the
  accessible marker; points are not drawn differently.
- Archive format 1.3 adds the required `estimate_between` column. A 1.2 archive reads without
  it and restores side defaults. The v1.2 golden stays; `workspace-archive-v1.3.zip` is added.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F5.6 from docs/specs/M5-net-worth.md: the per-item estimate_between switch (model, migration with side defaults,
item form), straight-line estimates between recorded values in backfill only, the "estimated" breakdown count shown in
history, and the archive 1.3 column. Golden table and property tests as listed.
```

---

## F5.7 Real estate in net worth

**Story:** My home and other property are part of what I own, not investments. I record them in
net worth, with their value history, and later see the mortgage against them.

**Owner decision 2026-11-09.** Property is tracked as a net-worth asset item, not as an M6
instrument. M6 stops seeding the Real estate and Primary residence asset classes (see the M6 data
model), and F7.6 links a loan to a net-worth item.

**Acceptance**
- The asset seed gains **Real estate** (pl "Nieruchomości"), first in the asset order. New
  workspaces get it in the workspace-creation transaction with the rest of the set.
- **"Add missing suggested categories"** action per side (editors and owners): it creates each
  suggested category whose name key (in either supported language) has no category on that side
  (an archived one counts as present), appended in seed order, in one `run_immediate` write with the workspace lock and a role
  re-check. It never renames, restores or reorders existing categories, and it is idempotent
  (a second run adds nothing). Audited as `networth_category.seeded` with the created names, like
  the empty-side action. It replaces the empty-side-only action: on an empty side it adds the
  whole set, as before.
- Existing workspaces are not migrated; the action is how they get Real estate.
- Property items use the existing F5.1 item fields and valuation history, F5.4 links and F5.6
  estimates; nothing about items changes. `investable` defaults to off for new Real estate items
  (a home is not FIRE money), as for every new asset item.
- M6 (same change): the asset-class seed drops Real estate and Primary residence; Other becomes
  investable; the portfolio page counts every class and badges non-investable ones ("not
  counted in FIRE"). Existing workspaces keep their classes and instruments untouched (user data).
  The F6.8 helper skips archived classes and warns about double counting between a `real_estate`
  instrument and a Real estate net-worth item.
- Archive restore keeps accepting the pre-F5.7 untouched seed sets on both sides (net-worth
  categories without Real estate; asset classes with Real estate and Primary residence) as empty.

**As built:**
- Real estate takes a palette colour the other suggested categories don't use; the others keep
  their colours, so every shipped set (`ASSET_SEED_V1`, `ASSET_SEED_V2`, `ASSET_SEED`) still
  compares exactly in the restore check.
- The audit event lists the created names as one comma-separated `names` string (audit data
  holds scalars). A run that creates nothing writes no event, so a no-op click never turns an
  untouched seed into user data for restore.
- The empty-side offer keeps its "Add suggested categories" wording; a non-empty side shows
  "Add missing suggested categories" only while a suggested category is missing.
- The asset-class seed keeps the pre-F5.7 set as `ASSET_CLASS_SEED_V1` (in
  `ASSET_CLASS_SEEDS`) for the F6.9 restore check; asset classes are not in the archive yet.

**Tests:**
- Seed sets in en and pl, with Real estate first.
- The add-missing action on:
  - an empty side (the whole set);
  - a partial side (only the missing ones);
  - a side where a suggested category was renamed (the original name is added) or archived.
    An archived category counts as present and is not re-created; the user restores it.
- Idempotence, role, CSRF and the audit event.
- Restore treats the old and new untouched seed sets as empty.
- The M6 seed has neither property class, and Other is investable.
- The portfolio badge shows on a non-investable class, and every class still counts in the
  totals.
- The F6.8 helper skips archived classes (when F6.8 lands).

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F5.7 from docs/specs/M5-net-worth.md together with its M6 seed changes: the Real estate net-worth seed (en/pl),
the "Add missing suggested categories" action, the new M6 asset-class seed (no Real estate / Primary residence, Other investable),
the "not counted in FIRE" badge on the portfolio class table, and restore emptiness accepting old and new seed sets.
```
