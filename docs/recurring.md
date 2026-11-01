# Recurring items

Open **Recurring** to define planned expenses or income once. Editors and owners can add or
change items; viewers can read the list.

## The list

The page has two sections, **Expenses** and **Income**. Each item is one row: its name, the
amount (marked **estimate** when the amount varies), the schedule as a sentence such as
"Monthly on the 10th", "Every 3 months on the 15th, from January 2027", "Yearly on 31 March"
or "Weekly on Monday, until Dec 1, 2027", the next date, and the category. A paused item is
marked **paused** and has no next date. Editors see **Edit**, **Pause** (or **Resume**) and
**Delete** on each row and a **New recurring item** button at the top. On a phone each item is
shown as a small card.

Archived items are not listed. Their payments stay in your transactions and history.

## Adding or changing an item

**New recurring item** and **Edit** open a form in a few short steps, with a **Next dates**
panel beside it (below it on a phone) that shows the schedule sentence and the next five dates
as you type:

- **What**: the name, whether it is an expense or income, and the category. Only categories
  of the chosen kind are offered; the category decides whether the item is an expense or
  income.
- **How much**: the amount in the workspace currency. Tick **The amount varies; this is an
  estimate** for bills like electricity: you confirm the real amount when it is paid.
- **How often**: **Weekly** (one or more days of the week, every N weeks), **Monthly** (a day
  of the month, the last day, or the first or last working day), **Every few months** (the same
  day choices, every N months, counted from the start month) or **Yearly** (a day and month,
  every N years). Only the fields of your choice are shown. Then choose what happens when the
  date falls on a weekend or public holiday: keep that date, or move it to the working day
  before or after.
- **When**: the start date, and when it ends: **Never**, **On a date** or **After a number of
  times**. A new item can also add the dates between a past start date and today.
- **Options**: record it as paid automatically on the due date, reminder days, notes.

If something is wrong, the form shows the message next to the field and keeps what you typed.
An older rule with both an end date and a number of times opens with **On a date**; saving it
keeps only the end date.

## How dates are calculated

The interval means every N weeks, months or years. Weeks start on Monday;
intervals are anchored to the start date's week, month or year. Start/end dates are inclusive
limits on scheduled dates, before any holiday adjustment. A day that does not exist is clamped:
31 February becomes the last day of February; yearly 29 February becomes 28 February in a
non-leap year.

For a single payment or income on one date, such as a gift or an annual fee you pay just once,
use a **one-time payment** instead: on the **Upcoming payments** page choose **Add one-time
payment** (also reachable as `/upcoming?form=one-time`) and
enter a description, the amount, expense or income, a category of that kind and the date. It
gets one occurrence on that date, even when the date is farther ahead than the regular
generation horizon (see below); a date already passed gives an overdue occurrence. One-time
payments are not listed on the Recurring page and its form does not offer **One time** for a
new item (an older one-time item opened from a link keeps its choice there). On Upcoming, a
one-time payment has **Mark paid or received**, **Edit** (description, amount, date and
category; an overdue one moves too) and **Delete**; it is shown once it is due within the
Upcoming window. It adds to the month's pending amount ("+ … pending" under Actual), not to the
Plan. Its expected row on the Transactions page can be confirmed or deleted; deleting removes
the payment. A one-time item created by the Transactions page's **Plan** button is the same.

JavaScript is needed only for the live **Next dates** panel, for showing just the fields of the
chosen frequency and for filtering the categories; without it every field is shown and the form
can still be saved. A finite or ended rule may have fewer than five dates remaining.

## Working days and holidays

Working days are Monday through Friday, excluding the workspace country's national public
holidays, including observed holidays supplied by `python-holidays`. No subdivision is selected.
If the workspace has no country, only weekends are excluded. Unsupported countries are refused
when the rule needs working-day calculations; ordinary unadjusted dates still work. Changing the
workspace country recalculates pending dates of working-day items at once; a country without
holiday data cannot be chosen while such items exist.

Choose **Keep that date**, **Move it to the working day before** or **after**. An adjustment can
cross a month or year boundary, or shift a date before the rule's start/after its end. The
optional maximum count counts the scheduled dates before shifting; dates that land on the same
effective day merge into one occurrence, so there can be fewer stored occurrences than the count.

## Generation and history

Saving immediately generates planned occurrences from today in the workspace time zone through
the same day 13 calendar months ahead, inclusive (clamped at month end). Administrators can set
`ASSETBOARD_RECURRENCE_HORIZON_MONTHS` to another positive number. **Also add the dates between the start date and today** on
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
confirmation and removes an item only if no history remains; otherwise it archives it, which hides
it from the list (its posted payments stay). Occurrences in a closed month are never created, removed or
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
occurrence, or edit its expected amount. A recurring occurrence's expected row on the month's
Transactions page also has **Skip** next to Confirm. A skipped occurrence stays in its month
there as a greyed-out **Skipped** row with **Undo**, which makes it pending again (an income
occurrence gets its planned tax payments back); skipped dates never count as pending. Undo is
refused while the month is closed or when the recurring item is archived. Marking it paid creates a linked transaction using the
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
Log units by work month (decimal units are supported) with **Log days worked**; when the
chosen month already has a log, the form shows its units and the button reads **Update** (a
link can open it for a month with `?log=<source id>&month=YYYY-MM`). In the payments table a
month with a manual log has a small **Edit** next to its units: change the units and **Save**,
or press Esc or **Cancel** to close it unchanged. The month's pending income occurrence and any
planned tax-payment occurrences are recalculated.

Once a month's income is received (its occurrence is confirmed as a transaction), its log is
read-only by default: the row shows **received**, and saving the month is refused with "This
month's income is already received. Edit the transaction instead.", linking to the transaction.
If the logged days really were wrong, choose **Edit anyway** (on the row, in the **Log days
worked** form, or in the refusal message) and save. The posted transaction is not changed
silently: a notice says "Logged days changed. The received payment was X; the new days give Y."
with **Update the transaction to Y** (editors and owners) and a link to the transaction. The
change is recorded in the audit log, and so is an update of the transaction.

When the month the income is paid in is closed, the log cannot change at all, not even with
Edit anyway: the row shows **closed**, and saving says "This month is closed. Reopen it to make
changes." with a link to the Month page, where an owner can reopen it.

When Taxes are on (Workspace settings → Menu), tick the workspace's taxes under **Taxes on this
income** on the card; each can have a different payment day for this income. The expected income is the net
amount plus taxes added on top minus taxes the payer withholds, and taxes you pay get planned
tax-payment expenses (see [Taxes](taxes.md)). Tax rates and fixed amounts are user-entered
models, not tax advice. Pending occurrences appear as expected rows, with a
Confirm button, in the tables of the month's **Transactions** page; the **Month** summary shows
the pending amounts under the actuals ("+ … pending"). Missing work logs are highlighted on
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
with the same rules: editors and owners only, and nothing changes while the pay month is closed
(the calendar then says so and links to the Month page). A month whose income is already received
is shown read-only with **Edit anyway**; after a change the calendar shows the received payment
next to the new amount, with **Update the transaction to Y**, exactly as on the Income page.
