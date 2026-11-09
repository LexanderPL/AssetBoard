# Net worth

**Net worth** lists what you own and what you owe. Open it from the sidebar. The section has four
tabs: **Summary**, **Assets**, **Liabilities** and **History**. The two lists group items under
their categories and show a subtotal for every category. Everyone in the workspace can read them;
editors and owners can add, change and archive things, take snapshots and rebuild history.

## Values are positive

Every value is a positive amount in the item's currency. An asset's value is what it is worth; a
liability's value is the amount you still owe. The app never stores a signed liability amount:
totals subtract the liabilities from the assets.

## Summary

The summary shows the big net-worth figure, total assets and liabilities, an assets-vs-liabilities
bar, and a donut and table for each side. Category shares are percentages of that side's own
total; they are rounded to one decimal place and need not add up to exactly 100.0%.

The same rules as the lists apply: archived items, items with **Include in net worth** off and
items with no effective value are not counted. Items under an archived category still count until
the item itself is archived. Items in another currency are shown separately in a warning and are
not converted or counted before an exchange-rate service exists. Items excluded by your own
**Include in net worth** switch are shown as a muted note, not a warning.

A side with no counted value shows no donut and a dash instead of a share. A workspace with only
liabilities therefore shows a negative net worth, the liability table, and no asset donut.

## History and snapshots

The **History** tab stores the net-worth total over time. A snapshot keeps assets, liabilities,
net worth, the currency used and the per-category breakdown as it was on its date. Stored manual
snapshots are never recalculated. Backfill can rebuild automatic rows in its range, and the
monthly automatic job leaves an existing backfilled row alone.

- **Automatic snapshots** are taken on the 1st of each month at 04:00 in the workspace time zone
  and cover the previous month's last day. A workspace with no net-worth items at all is skipped,
  and so is a month-end where none of the items had an effective value yet; archived items still
  count as items. After downtime the startup catch-up stores only the most recent month-end;
  older missed months come from **Backfill from history**.
- **Take snapshot now** (editors and owners) stores the workspace's today. Taking another manual
  snapshot on the same date replaces the earlier manual one; a zero total is stored too.
- **Backfill from history** (editors and owners) recomputes every month-end from the month of the
  earliest stored value through the previous month-end. It deletes and recreates the automatic
  and backfill rows for those dates; manual snapshots are untouched. The rebuilt rows have
  `source=backfill`, so they clearly show estimated history. No stored values means no backfill
  rows.
- When several snapshots exist for one date, **manual** wins over **automatic**, and both win over
  **backfill**. The chart and table show only the preferred source for each date.
- The chart has net worth, assets and liabilities lines; the legend toggles them. The range
  selector offers 1 year, 3 years, 5 years and all time (default: 1 year). Hovering a point shows
  that snapshot's category breakdown. The table underneath is the accessible, no-JavaScript view
  and marks manual and backfill rows.

**Archived items in snapshots.** An item counts up to its archive date: at month-end `D` it is
excluded only when its archive time falls on or before `D` in the workspace time zone. An item
archived in June therefore counts in the May month-end but not in June's; a restored item counts
in every month. Today's summary keeps the current rule, which is equivalent because an archived
item's archive date cannot be after today.

**Include in net worth** has no history in this version. The current switch applies to every
snapshot date, including backfilled ones. Changing it affects future automatic snapshots and a
rerun of the backfill, which replaces automatic rows in its range; manual snapshots are never
changed.

Snapshots keep the currency they were taken in. The chart plots only snapshots in the workspace's
current base currency; snapshots in another currency remain in the table with their currency code
and a note that they are not charted. Backfill always uses the current base currency, so rerunning
it after a base-currency change replaces the old backfill rows.

The stored breakdown is a versioned object. The history page uses the current category names and
colours and shows **Deleted category** for a category removed since the snapshot. Unknown keys in
a newer breakdown are ignored, so a later version can add markers without a migration.

## Categories

Assets and liabilities have separate category lists. Names are unique per side among active
categories, ignoring case (so "Vehicles" and "VEHICLES" clash, but an asset "Other" and a
liability "Other" are fine). Categories have a colour and can be reordered with the arrows.

A new workspace starts with these suggested categories, named in the language used when the
workspace was created:

- **Assets:** Real estate, Vehicles, Electronics, Appliances, Furniture, Jewelry & watches,
  Art & collectibles, Tools, Other.
- **Liabilities:** Installments, Mortgage, Other.

**Property** (your home, a flat you rent out, land) belongs here, as an item in **Real estate**,
not in Investments: record its value and add new values as it changes, like any other item. A
home is usually not money for financial independence, so leave **Investable** off unless you
mean to sell it for that. Estimates between values and links work as for every item.

An older workspace, or one whose categories were all deleted, does not get the suggested
categories automatically:

- A side with no categories offers **Add suggested categories**, which adds the whole set.
- A side that already has categories offers **Add missing suggested categories** (editors and
  owners) while a suggested one is missing. It adds each suggested category that has no category
  of that name on that side, in English or Polish, at the end of the list in the suggested order.
  It never renames, reorders or restores anything: a renamed suggested category gets its
  original back next to it, and an archived one counts as present (restore it instead). Running
  it again adds nothing. This is how a workspace created before Real estate was suggested gets
  it.

Archiving a category hides it from new items but keeps its items and their history. Its items
still count towards net worth under an **Archived categories** heading until each item is
archived. **Delete** is offered only for a category with no items; deleting a category that
still has items archives it instead.

## Items

An item has a name, category, currency, notes, an optional acquired date, and two switches:

- **Include in net worth** — when off, the item stays in the list but is left out of the
  subtotals and of the summary.
- **Investable** — a marker for the FIRE projection; it does not change any total.

The currency defaults to the workspace base currency. It is the item's source of truth: every
value of that item must use the same currency, and the currency cannot change once the item has
a value. To correct a mistaken currency, archive the item and add it again.

Items whose currency is not the workspace base currency are shown with a **Not in totals**
marker. Their values are not converted until an exchange-rate service exists, so they are left
out of the category subtotal. Changing the workspace base currency does not change any stored
value; it only changes which items currently count towards the subtotals.

## Linked values

Some items can take their value from another feature instead of a manual value. The first sources
are portfolio asset classes and holdings (M6); loan balances follow in M7. Until a module registers
a resolver, the **Link to…** picker does not appear. A resolver belongs to assets or liabilities,
and only its side’s targets are offered.

- A link is chosen when the item is created. **Link to…** lists the registered targets; leave it
  at **Manual value** for an ordinary item.
- A linked item’s value is read-only. It shows a **Linked to …** chip with the target’s name and,
  when available, a link to that target, plus the target’s currency. It has no **Add a value**
  panel and cannot receive manual valuations.
- The target’s currency is the item’s currency. A non-base linked item is shown like any other
  non-base item: separately, outside the subtotal, until FX arrives.
- If the target cannot be resolved, or the resolver returns a value in another currency, the item
  shows **Value unavailable** and is never counted as zero. A snapshot records how many linked
  items were unavailable.
- **Unlink** converts the item to manual. If today’s value is available, it is kept as the first
  manual value dated today; otherwise the item becomes manual and shows **No value yet**.
  After an unlink, **Backfill from history** sees the item as manual with at most today’s value,
  so months when it was linked are not recreated in backfilled points. Manual snapshots keep the
  values stored while it was linked; automatic rows in a backfill range are rebuilt.
- Two active items cannot link to the same target. Archive one before creating the other. An
  archived linked item can only be restored while no other active item links to its target.
- Only one item can be linked to a target at a time because otherwise net worth would count it
  twice.

To replace an existing manual item with a linked one, archive the manual item and create a new
linked item. Archived items count in snapshots up to their archive date, so the history stays
continuous.

### Portfolio links

On **Net Worth → Assets**, editors and owners also see **Create net-worth items from asset
classes**. It creates one linked asset item for every active asset class with a value, and puts
them in an **Investments** category. That category is created on demand (in your workspace
language: **Inwestycje** in Polish) and appended to the asset categories; the suggested category
sets do not include it. Classes with no value, classes whose value is unavailable through a
missing price or exchange rate, and classes already linked are skipped. Run the button again
after adding a class or a value; it only creates what is missing. Classes marked *not counted in
FIRE* produce items with **Investable** off.

The picker also offers individual portfolio holdings: active instruments you currently hold.
A sold-out or archived target is no longer offered for a new link, but an item already linked to
it keeps working.

A portfolio-linked item does not store its own values:

- **Today** it shows exactly the value from the **Portfolio** page. Current holdings include
  *undated* lots imported from an older record, and prices and exchange rates are the latest
  available ones.
- **In a past snapshot or a backfill** it uses the **Performance** history: only dated
  transactions, prices and exchange rates carried forward from the latest observation on or
  before the date, and the bond model value for each date. Undated lots are not in that history,
  so the first month-end snapshot can be lower than today's value when such lots exist. This is
  the same step the Performance chart shows; the link itself is not recalculated.
- A class value is shown as **Value unavailable** if even one held instrument allocated to it is
  missing a price or exchange rate. It is never shown as a partial class total. An individual
  held instrument behaves the same way; an instrument you no longer hold is worth zero.

The Assets page also shows advisory **Possible double counting** warnings. They do not change any
total:

- when a linked asset class and a linked holding of a currently held instrument in that class
  would both be counted;
- when a workspace created before the property change still holds real estate as an investment
  instrument and also has a **Real estate** item with a positive value.

Archive the duplicate item or unlink it if you want only one of the two values in net worth.

## Value history

Adding an item records its first value. Every later value change is appended to the item's
history, with the date it applies to and an optional note. Nothing in the history is edited or
deleted.

- **Current value** is the latest value whose **as of** date is today or earlier. A future-dated
  value is kept in the history but does not become current until that date.
- **Back-dating** is allowed. A value dated before the current one appears in the history but
  does not change the current value.
- Several values may share a date. The one entered last is current for that date.
- When an item has no value effective yet, it shows **No value yet** and is left out of the
  subtotal.

Each item's **Value history** shows a small table and a sparkline. The sparkline uses the latest
value per date up to today.

Because history is append-only, a wrong amount can be corrected by adding a newer value for the
same date. A wrong **date** cannot be removed; the note should explain the correction. The
backfill in a later version will use the stored dates, so this is a known follow-up for the
owner.

## Bulk add

The **Bulk add items** panel accepts one item per line:

```
Name; value; category
Car; 12 000; Vehicles
Camera; 1 250,50; Electronics
```

- Values use the interface number format (`1 234,56`, `1,234.56`, `12,5`).
- The workspace base currency and today's date are used; notes and the switches stay at their
  defaults.
- A line must have exactly three semicolon-separated parts. Blank lines are ignored.
- The category must be an active category of the selected side, matched by name ignoring case.
- At most 500 non-blank lines and 64 KiB of text are accepted.
- **Preview** checks every line and shows per-line errors. **Create** is all-or-nothing: if any
  line is invalid, no item is created.
- Item names need not be unique. Duplicate names in the batch, or names already used by an
  active item, are shown as warnings but do not block creation.

## Archiving and roles

**Archive** on an item hides it from the active lists and from today's net worth, but keeps its
value history. Stored snapshots keep the item up to its archive date (see **History and
snapshots**). **Restore** brings it back. A category and an item are archived independently.

Viewers can read everything but see no add, edit, archive or bulk controls. Editors and owners
can change data. Changes are recorded in the workspace audit log under **Net worth** (values and
field names only; notes are not copied there).

## Estimates between values

Each manual item has **Estimate values between entries**. With it on, **Backfill from history**
fills a month-end that falls strictly between two recorded values with a straight line:
`v0 + (v1 - v0) × (D - d0) / (d1 - d0)`, counted in calendar days and rounded once, half up,
to the currency's minor unit. The date `d0` is the latest recorded date before `D` and `d1` the
earliest recorded date after it, using the latest entry on that day (the same rule as F5.1). The
end date must be on or before today, so a future value is never used. A month-end that is a
recorded date uses the recorded value; before the first value there is no value, and after the
last one the last value carries forward.

The switch affects **backfilled snapshots**. Manual snapshots stay exactly as recorded. An
automatic snapshot is replaced by a backfilled estimate when you rerun **Backfill from history**
for its date; until then it keeps the recorded value. The monthly automatic job does not overwrite
a backfilled row. The current value, lists, summary and sparkline are unaffected. Linked items
ignore the switch because their resolver supplies the value. Changing the switch does not rewrite
stored snapshots by itself: run **Backfill from history** again to apply it. New assets default on
and new liabilities off; the migration applies those defaults to existing items. The same
defaults apply when a 1.2 archive is restored.

A backfilled snapshot that used estimates says “includes N estimated values” in the history
table and its chart tooltip. The breakdown also stores an `estimated` count.

## Archives and imports

Net worth is part of the workspace archive (format 1.3): categories, items, value histories and
snapshots are exported with lineage ids. A restore recreates them in dependency order. Values
keep their `created_at`, so several values for one item on one day come back in the same order
and the current value does not change. Snapshot breakdowns keep their category lineage ids; a
restored breakdown points at the restored category.

A linked item whose target is not in the archive is restored as a manual item with the link
cleared and no value, and listed as `link_target_missing`. The export still writes `link_type`
and `link_id`; the M6/M7 archive extensions will resolve those target ids. The **Link to…**
picker stays hidden until a resolver is registered.

A full archive merge ignores the net-worth files and lists that once; only a lone
`item_valuations.csv` merges. It matches an item by `item_id` first and by name otherwise. Names
are not unique, so prefer the `item_id` column (the lineage id an export writes) when a name is
ambiguous. Rows matching a linked item are refused, and the value currency and minor digits must
match the item. For the same item and date, an identical value is a duplicate and a different
value a conflict; the stored history is kept. Without `created_at`, values are stamped in file
order from the import time.

Restoring removes untouched suggested net-worth categories exactly as it removes the ledger
suggested categories: only the automatic `networth_category.seeded` set, unedited, unused and
created within five seconds of the event. Every set ever suggested counts, so a workspace
created before Real estate was suggested is still recognised. A later manual **Add suggested
categories** or **Add missing suggested categories** makes that set user data.
