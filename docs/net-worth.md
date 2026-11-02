# Net worth

**Net worth** lists what you own and what you owe. Open it from the sidebar. The section has
three tabs: **Summary**, **Assets** and **Liabilities**. The two lists group items under their
categories and show a subtotal for every category. Everyone in the workspace can read them;
editors and owners can add, change and archive things.

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

## Categories

Assets and liabilities have separate category lists. Names are unique per side among active
categories, ignoring case (so "Vehicles" and "VEHICLES" clash, but an asset "Other" and a
liability "Other" are fine). Categories have a colour and can be reordered with the arrows.

A new workspace starts with these suggested categories, named in the language used when the
workspace was created:

- **Assets:** Vehicles, Electronics, Audio equipment, Instruments, Appliances, Jewelry &
  watches, Photography, Other.
- **Liabilities:** Installments, Mortgage, Other.

An older workspace, or one whose categories were all deleted, does not get them automatically.
A side with no categories offers **Add suggested categories**; the action is optional and does
not come back once the side has a category.

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
Camera; 1 250,50; Photography
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

**Archive** on an item hides it from the active lists and from net worth, but keeps its value
history; **Restore** brings it back. A category and an item are archived independently.

Viewers can read everything but see no add, edit, archive or bulk controls. Editors and owners
can change data. Changes are recorded in the workspace audit log under **Net worth** (values and
field names only; notes are not copied there).

## Not included yet

The link columns for portfolio assets and loans are reserved for a later version; the page
creates manual values only. Net worth data is not yet part of the workspace archive, and a
restore or merge leaves existing net-worth data untouched. Both arrive in later M5 features.
