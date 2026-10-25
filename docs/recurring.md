# Recurring items

Open **Recurring** to define planned expenses or income once. Editors and owners can add or
change items; viewers can read the list. An item has a name, an active category matching its
direction, a fixed or estimated amount in the workspace currency, optional notes, and a rule.

Choose weekly weekdays, a monthly day (including the last day or first/last working day), or a
yearly month and day. The interval means every N weeks, months or years. Weeks start on Monday;
intervals are anchored to the start date's week, month or year. Start/end dates are inclusive
limits on scheduled dates, before any holiday adjustment. A day that does not exist is clamped:
31 February becomes the last day of February; yearly 29 February becomes 28 February in a
non-leap year.

The form previews the next six effective due dates as you change the rule. JavaScript is needed
only for the live preview; the form can be saved without it. A finite or ended rule may have
fewer than six dates remaining.

## Working days and holidays

Working days are Monday through Friday, excluding the workspace country's national public
holidays, including observed holidays supplied by `python-holidays`. No subdivision is selected.
If the workspace has no country, only weekends are excluded. Unsupported countries are refused
when the rule needs working-day calculations; ordinary unadjusted dates still work. Changing the
workspace country recalculates pending dates of working-day items at once; a country without
holiday data cannot be chosen while such items exist.

Choose **Keep the date**, **Previous working day**, or **Next working day**. An adjustment can
cross a month or year boundary, or shift a date before the rule's start/after its end. The
optional maximum count counts the scheduled dates before shifting; dates that land on the same
effective day merge into one occurrence, so there can be fewer stored occurrences than the count.

## Generation and history

Saving immediately generates planned occurrences from today in the workspace time zone through
the same day 13 calendar months ahead, inclusive (clamped at month end). Administrators can set
`ASSETBOARD_RECURRENCE_HORIZON_MONTHS` to another positive number. **Include past occurrences** on
creation also backfills dates from the start of the rule; it is off by default. Generation is
idempotent: repeating a job never creates a second occurrence for the same item and nominal
scheduled date. The nominal date remains stable when its effective date changes due to a
holiday-calendar update or a one-off reschedule.

An APScheduler job runs daily at 03:00 in each workspace's time zone. Each run recalculates
pending dates from today onward, so a changed holiday calendar moves dates instead of adding
new ones. Startup catches up the forward horizon in the background; new workspaces and
time-zone changes are picked up within one minute. Use one application process/worker, as
required by the deployment guide. A failing item is logged with its identifier, without
financial data, and does not stop the other items; the next run retries it.

Editing replaces only **pending** occurrences whose due date is today or later. Overdue,
posted and skipped occurrences keep their identity, amount and other data. Pausing removes
pending occurrences from today onward and stops generation. Archiving or deleting removes all
pending occurrences, overdue ones included; posted and skipped occurrences are history.
Resuming regenerates the forward horizon without creating new overdue dates. Deleting asks for
confirmation and removes an item only if no history remains; otherwise it archives it. Archived
items remain visible for history. Occurrences in a closed month are never created, removed or
changed; reopen the month first.

Categories referenced by rules or occurrences cannot change direction or be deleted; deleting
the category archives it instead. Stored recurring amounts also prevent changing the workspace's
base currency. An item whose category is archived creates no new dates, and its pending dates from today
onward are removed on the next daily run. The edit form shows the archived category as the
current, unavailable choice; choose an active category to save the rule again.

## Upcoming occurrences

Open **Upcoming** to see overdue occurrences and those due within the next 30 days, grouped by
week. Use the direction and category filters to narrow the inbox. The operator may set
`ASSETBOARD_UPCOMING_DAYS` to a value from 1 to 365 to change the forward window.

Editors and owners can mark an occurrence paid or received, skip it, reschedule just that
occurrence, or edit its expected amount. Marking it paid creates a linked transaction using the
entered date and amount. The transaction can be edited from the month or transaction list; undo
the posting from the inbox within 10 seconds to soft-delete the transaction and restore the
occurrence to pending. Editing an occurrence's amount or date does not edit its recurring rule.

Enable **Automatically post on the due date** on a recurring item to create its transaction at
00:05 in the workspace time zone. Automatic transactions are labelled in the ledger and remain
editable. Posting, skipping, rescheduling, and amount changes are blocked when the affected
month is closed. Reopen it first, or reschedule to an open month.

An occurrence's nominal scheduled date is stored separately from its effective or rescheduled
date, so holiday-calendar changes do not change the identity of a rescheduled or posted
occurrence.
