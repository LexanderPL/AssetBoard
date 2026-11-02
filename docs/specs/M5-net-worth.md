# M5 — Net worth

**Goal:** list what you own and what you owe, grouped into categories, compare the two, and track net worth over time.

**Exit criteria:** assets and liabilities (manual values with history), category groupings, a summary with net worth, and monthly snapshots producing a trend chart. Linked valuations are *designed for* here and *filled in* by M6 (portfolio) and M7 (loans).

Every agent prompt starts with: *"Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go)."*

**Data model (sketch)**
- `NetWorthCategory(id, workspace_id, side[asset|liability], name, color, sort_order, archived_at)`
- `NetWorthItem(id, workspace_id, side, category_id, name, currency, notes, valuation_mode[manual|linked], link_type?, link_id?, include_in_net_worth bool default true, investable bool default false, acquired_on?, archived_at)` — `currency` is the item's source of truth.
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
- Seed categories (en + pl): assets — Vehicles, Electronics, Audio equipment, Instruments, Appliances, Jewelry & watches, Photography, Other; liabilities — Installments, Mortgage, Other.
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
- **Backfill** from valuation history: for each past month-end, compute from `ItemValuation`s (latest valuation ≤ date); marked `source=backfill` and replaceable by real snapshots.
- Line chart of net worth (with assets and liabilities lines toggleable) over time; range selector (1y/3y/5y/all); hover shows the breakdown by category.
- Linked items are included in snapshots using their value at that moment (once M6/M7 exist); if a link cannot be resolved the snapshot records the item as "unavailable" and the report flags it.

**Owner decision (2026-11-02).** An item counts up to its archive date. At month-end `D` it is
excluded only when its `archived_at` falls on or before `D` in the workspace time zone; a restored
item counts in every month. Today's summary keeps the current archived rule, which is equivalent
because an archive date is never after today. `include_in_net_worth` has no history: the current
setting applies to every date, and changing it affects future snapshots and a rerun of the
backfill, never stored automatic or manual snapshots.

**Implementation notes (F5.3).**
- `NetWorthSnapshot` stores `as_of`, assets/liabilities/net minor units, currency, a versioned
  breakdown object and `source` (`auto|manual|backfill`). `UNIQUE(workspace_id, as_of, source)`
  is the race guard; `net_minor = assets_minor - liabilities_minor`.
- `domain.networth_history.values_at` selects values from rows loaded once; it is the pure path
  under `services.networth.current_values`, the auto job and the backfill. A backfill therefore
  reads items, categories and valuations once rather than once per month.
- Auto: 1st of the month at 04:00 workspace time for the previous month-end; skipped entirely
  when the workspace has no net-worth items (archived items count as items) or when no item had
  an effective value on that date yet. Manual: workspace today, replaces the same date's manual
  row, stores zero for an empty workspace. Backfill: from
  the earliest valuation's month-end through the previous month-end; deletes and recreates only
  `source=backfill` rows, skipping dates with `auto` or `manual`. Re-running is allowed.
- Startup catch-up creates only the most recent previous month-end's automatic snapshot; older
  missed months come from backfill. The job logs the workspace id and exception type only.
- Source priority is manual > auto > backfill. A real snapshot replaces a backfill row for its
  date; a manual snapshot replaces the manual and backfill rows for its date and leaves auto.
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
- `ValuationResolver` interface in `services/networth/links.py`: `resolve(link_type, link_id, as_of) -> Money | Unavailable`; registry where later modules register resolvers.
- UI: item form shows "Link to…" only for registered link types; linked items display value read-only with a "linked to <name>" chip and a click-through.
- A linked item cannot be given manual valuations; unlinking converts to manual keeping the last resolved value as a first valuation.
- Tests with a fake resolver.

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

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F5.5 from docs/specs/M5-net-worth.md as an extension of the M4 archive (docs/specs/M4-ledger-export-import.md,
ADR-0016): the new files with lineage ids, export, restore order incl. links, standalone item_valuations.csv import,
emptiness checks and the extended round-trip test.
```
