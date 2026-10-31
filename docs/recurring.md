# Recurring items

Open **Recurring** to define planned expenses or income once. Editors and owners can add or
change items; viewers can read the list. An item has a name, an active category (which decides
whether it is an expense or income), a fixed or estimated amount in the workspace currency,
optional notes, and a rule.

Choose weekly weekdays, a monthly day (including the last day or first/last working day), or a
yearly month and day. The interval means every N weeks, months or years. Weeks start on Monday;
intervals are anchored to the start date's week, month or year. Start/end dates are inclusive
limits on scheduled dates, before any holiday adjustment. A day that does not exist is clamped:
31 February becomes the last day of February; yearly 29 February becomes 28 February in a
non-leap year.

Choose **One time** to plan a single payment or income on one date, such as a gift or an annual
fee you pay just once. The item has one occurrence on its start date; the weekend and holiday
adjustment still applies, while the end date, maximum occurrences, interval, weekdays, day and
month are ignored (the form hides them). Its occurrence appears at once, even when the date is
farther ahead than the regular generation horizon (see below), and is not pruned as the date
approaches.
A past date needs **Include past occurrences**. Changing the date moves its pending occurrence;
once the occurrence is posted or skipped, the list shows the item as **Done** and you can
archive it. A one-time item adds to the month's Expected and Forecast columns, not to Planned.
Its expected row on the Transactions page can be confirmed or deleted; deleting removes the
planned entry and the item.

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

## Upcoming payments

Open **Upcoming** to see the **Upcoming payments** page: overdue occurrences and those due within
the next 30 days, grouped by week. Use the direction and category filters to narrow the list.
The workspace owner can change how far ahead it looks (1 to 365 days) under **Upcoming window**
in **Workspace settings**; the Home page's upcoming list uses the same window. Left empty, the
server default applies: 30 days, or what the operator set with `ASSETBOARD_UPCOMING_DAYS`.
When occurrences are overdue, the **Upcoming** link in the navigation shows how many.

Editors and owners can mark an occurrence paid or received, skip it, reschedule just that
occurrence, or edit its expected amount. Marking it paid creates a linked transaction using the
entered date and amount. The transaction can be edited from the month or transaction list; undo
the posting from the Upcoming payments page within 10 seconds to soft-delete the transaction and restore the
occurrence to pending. Deleting the transaction from the month or transaction list also returns
its occurrence to pending (keeping its date and amount changes), so it can be marked paid again;
undoing that deletion re-links the transaction if the occurrence is still pending. Editing an
occurrence's amount or date does not edit its recurring rule.

Enable **Automatically post on the due date** on a recurring item to create its transaction at
00:05 in the workspace time zone. Automatic transactions are labelled in the ledger and remain
editable. An occurrence that was posted once and returned to pending (by an undo or a deleted
transaction) is never posted automatically again; mark it paid yourself. Posting, skipping, rescheduling, and amount changes are blocked when the affected
month is closed. Reopen it first, or reschedule to an open month.

An occurrence's nominal scheduled date is stored separately from its effective or rescheduled
date, so holiday-calendar changes do not change the identity of a rescheduled or posted
occurrence.

## Reminders

Open **Notifications** from your **Profile** (owners also find it in **Workspace settings**) to
get reminders on your phone or by email. Add a channel with an
[Apprise](https://github.com/caronc/apprise) URL, for
example `ntfys://ntfy.sh/your-topic` for ntfy, `gotifys://gotify.example.com/token` for Gotify,
`tgram://bot-token/chat-id` for Telegram or `mailtos://user:password@mail.example.com` for
email, then press **Send test**. The URL is stored encrypted and shown only as `ntfys://••••••`
afterwards; leave the field empty when editing to keep it. Every member, viewers included, has
their own channels in each workspace; nobody else sees them.

Once a day, at the channel's time (workspace time zone), a channel gets:

- **Due soon**: occurrences whose due date is within the channel's number of days (default 3).
  A recurring item can set its own number under **Reminder: days before the due date**. Each
  due date is reminded once; a rescheduled occurrence once more.
- **Overdue**: occurrences past their due date, once a day and at most seven times each.
- **Days worked**: on the first day of each month for the month just ended, and three days
  before an estimated income occurrence, while a variable income source has no work log for
  that month.

Choose one digest a day or one message per reminder (at most ten; the rest arrive together).
Messages name the item and its date. Amounts are left out unless you tick **Include amounts**:
messages pass through the service you chose and any push or mail servers on the way. A failed
delivery shows on the channel and is retried the next day. Removing a member deletes their
channels in that workspace.

## Income and work logs

Open **Income** to see each income source as a card: a one-line summary, the taxes ticked on it,
and a table of the next payments (with taxes: net, taxes, what you receive and the tax payments
with their dates). **Add income** walks through four steps, each checked before the next one
opens: the name, category and how it is paid (a fixed amount, or per day or hour); the amount;
the payday ("Paid on day 10 of the next month", with a sentence that shows when one work month is
paid) and the work months it covers; and, when Taxes are on, the taxes that apply, with an
example. The steps work without JavaScript. **Edit** opens the same fields on one page, where you
can also archive the source. Variable
sources can estimate units from the workspace's working days, a fixed number, recent logged
months, or wait for a manual work log. A work month is paid on the source's configured day in
the month after its payment lag; days past the end of a short month are clamped to its last day.
Log units by work month (decimal units are supported). The next pending occurrence and any
planned tax-payment occurrences are recalculated; a posted transaction is never silently changed.

When Taxes are on (Workspace settings → Menu), tick the workspace's taxes under **Taxes on this
income** on the card; each can have a different payment day for this income. The expected income is the net
amount plus taxes added on top minus taxes the payer withholds, and taxes you pay get planned
tax-payment expenses (see [Taxes](taxes.md)). Tax rates and fixed amounts are user-entered
models, not tax advice. Pending occurrences appear as expected rows, with a
Confirm button, in the tables of the month's **Transactions** page; the **Month** summary shows
pending expected amounts and a forecast alongside actuals. Missing work logs are highlighted on
both pages when the related pay date is due within ten days or is overdue. A pending income
occurrence you rescheduled keeps its date when its amount is recalculated. When Taxes are on, a
card explains why no tax payment is planned: no taxes are ticked, the payer takes off every tax,
or planning a tax's payment is switched off for the source.

### Work calendar

A variable income source can also be logged day by day: choose **Work calendar** on its card. The calendar shows one month at a time (use **Previous month** / **Next month**);
weekends are greyed and the workspace country's public holidays are marked (without a country,
only weekends). Click a day, or focus it and press Space or Enter, to mark it worked with 1 unit;
click it again to remove it. Under **Day values** you can change a marked day to a half day
(`0.5`) or a number of hours, from just above 0 up to 24, typed the way your language writes
numbers. **Fill working days** adds 1 for every working day that has no value yet and never
changes values you entered; **Clear month** removes every day of the month after a confirmation.

The calendar is optional. While a month has at least one marked day, its work log is the sum of
the days: in the payments table its **logged** chip links to that month of the calendar, and the
month-level form refuses to change it.
Marking the first day replaces a manual work log for that month; removing the last day (or
clearing the month) removes the log, and the month goes back to its estimate. Every change
recalculates the month's pending income and tax-payment occurrences, like a month-level work log,
with the same rules: editors and owners only, nothing changes while the pay month is closed, and
posted income is not changed.
