# AssetBoard UI audit — 2026-11-11

Baseline: **v0.6.0** (`de48a75`). Reviewed against the standards in
[`ui-guidelines.md`](ui-guidelines.md).

## Resolved in 0.6.1

The seven UI-consistency pull requests after 0.6.0 fixed:

- **Invisible results and idle writes (P0):** ledger, net worth, recurring and upcoming writes
  now show a visible status or toast (the ledger and net worth keep the changed row or toast in
  view), and the submit control is busy while an HTMX request runs.
- **Create actions below the fold (P0):** Add income, New tax, Add item, Add category, New channel
  and the other create actions moved to the page header (`header_actions`).
- **Row actions and touch targets (P1):** rows use `.btn-sm`, one overflow menu and the shared
  44 px phone targets instead of bare links and 20–28 px buttons.
- **Tabs and filters on phones (P1):** both tab rows are navigation with `aria-current`, a single
  scrollable strip, and the filters are disclosures that start collapsed at phone width.
- **Page identity and navigation (P1):** one `h1` per page, Home has its own title with the month
  as a subtitle, investment pages use their section title, and Loans is a disabled item rather
  than a dead link.
- **Component consistency (P1/P2):** remaining one-off buttons use `.btn` / `.btn-primary` /
  `.btn-danger` / `.btn-sm`; settings Save buttons are inline; destructive confirmations use the
  danger tone; Account security notices are square; the investment income table is a `.sheet`;
  gain/loss colours follow one convention; copy actions say “Copied”; the named empty states use
  `empty_state`; and the temporary-password page has a Copy button.

### Deliberately deferred

- The field-label sweep (visible labels and consistent `text-sm font-medium` labels on the net
  worth, investment and category edit forms).
- The workspace-data page split (tabs or a collapsed “Details” section for import/export).
- A full empty-state review beyond the pages named above (the remaining P2 page-by-page notes,
  including conditional form fields and the admin user actions, stay in the backlog).

The audit below is the unchanged 0.6.0 snapshot.

## Method

- Read every template under `src/assetboard/web/templates/` and the shared styles/scripts.
- Ran the app with the deterministic demo dataset (one workspace with four months of ledger,
  recurring items, income/tax, net worth, portfolio, imports).
- Inspected pages in Chromium at **1440 × 900** and **390 × 844**, light theme.
- Measured real click results (scroll position, bounding box of the changed row/notice) rather
  than judging from templates alone.
- Checked DOM basics: duplicate `h1`, missing labels, tab semantics, target sizes, duplicate ids.

No production code was changed for this audit.

## Severity

- **P0** — breaks the “result visible” rule or makes a common workflow fail.
- **P1** — repeated inconsistency or a significant usability/accessibility cost.
- **P2** — polish, copy, spacing or a local inconsistency.

---

## 1. Cross-cutting findings

### P0 — results are invisible after common writes

**Transactions: adding an entry.** In `months/_ledger.html` the entry row is at the end of
`<tbody>`, after every actual, pending and skipped row. On a populated month this is far below
the fold. After a successful add, the section swap leaves the viewport at the entry row
(`scrollY = 730` in the demo), the new row is above it (`y = -111`) and the only confirmation is
`#ledger-status`, which is `sr-only`; the “Entry added.” text measured at `y = -540`. A sighted
user sees the empty entry row and cannot tell whether the entry saved. There is also no
match on a slow request: no pending state or disabled button.

**Net worth: adding an item.** `networth/_body.html` places “Add an item” after every category
group and the archived/bulk sections. In the demo the form starts at `scrollY = 1502`; after
saving, the new item is at `y = -1257` and the “Item added.” notice is above the viewport
(`y = -1593`). The user remains at the old scroll position with no visible result.

**Upcoming: skip.** Skip is a plain POST, so the page reloads at the top. On a lower item the
user was at `scrollY = 575`; after skip the page is at `0`, the item is gone, and the Upcoming
page does not render a skip notice at all. The action succeeds silently.

**Future-dated “Mark paid or received”.** The form defaults to the occurrence's due date. Most
Upcoming items are future-dated, and the server rejects a future posting date. Clicking the
primary action therefore produces an error unless the user first edits the date. The same
applies to the “Confirm” popover on future pending rows in the Month ledger. Default to today
when the due date is in the future, or require an explicit date choice before submitting.

**Recurring actions.** Pause/Resume/Delete redirect to the list with no success query
(`scheduled_routes.action`), and the list template only renders `error`. The row changes or
disappears, but there is no confirmation. The page also reloads at the top.

**Copy invitation link.** `app.js` writes to the clipboard and changes nothing in the UI
(`data-copy-target` handler). The user cannot tell whether the copy worked. The temporary
password page has no copy button at all.

### P0 — primary actions are at the bottom of long pages

- Net worth “Add an item” (`networth/_body.html`): after all groups; 2438 px page.
- Net worth “Categories on this side” and “Bulk add items”: bottom details.
- Categories “Add a category” (`categories/_body.html`): after both category lists; 1827 px.
- Notifications “New channel” (`notifications/channels.html`): after the channel list.
- Asset classes “Add an asset class” (`investments/_classes_body.html`): after the list.
- Tax “Add another tax”: after the “Other taxes” list (short page, less severe).

The base template already has a `header_actions` block, but **no template uses it**. This is the
single clearest structural inconsistency: pages invent their own action rows, or put creation at
the bottom.

### P1 — row actions have no shared pattern

The same row-level operations are rendered four ways:

| Page | Edit | Archive/Delete | Target size |
| --- | --- | --- | --- |
| Categories | bare text link | bare text link + tiny arrow buttons | 26 × 28 arrows |
| Net worth items | bare text link | bare text link | ~20 px high |
| Investments instruments | bare text link | small borderless button | ~20 px high |
| Recurring | bordered small button | bordered small button | 26 px high |
| Upcoming | `.btn` | `.btn` / `.btn-primary` | 40 px high |
| Admin users | inline text links separated by `·` | same | 19 px high |
| History | no row action (bulk only) | — | — |

The audit counted 47 targets under 28 px high on Categories, 33 on Net worth assets, 44 on
Investments transactions, 50 on History and 19 on Recurring. Some are inline text links (which
WCAG exempts), but the arrow, Edit, Archive and Delete controls are standalone targets and are
too small, especially on touch.

### P1 — one-off buttons and form controls

- 39 one-off `rounded-xl bg-primary …` primary buttons in templates, versus 66 uses of `.btn` /
  `.btn-primary`. The one-offs differ in padding (`py-2` / `py-2.5` / `py-1.5`), radius and
  hover state.
- `primary_button` renders `w-full`; used in Profile and Workspace settings. Every other form's
  Save is inline (`btn-primary`). The same form type therefore has two widths.
- Fields are built with `field`/`select` on some pages and by hand on others. Net worth and
  investment forms use `text-xs` labels and `py-1.5` controls; income and recurring forms use
  `text-sm font-medium` labels and `py-2`/`py-2.5` controls.
- Notice blocks mix `mb-4`, `mb-5`, `mb-6`, `mt-4`; some are `rounded-xl` (Account security,
  auth pages) and most are square (the rest).
- Panel padding mixes `p-4`, `p-5`, `p-6`, `p-6 sm:p-7`, `p-4 sm:p-6`.
- Section headings mix `text-base` and `text-lg` (Work calendar, Portfolio, Performance,
  Providers, Workspace data, Import rollback).

### P1 — tabs are ARIA tab widgets without tab behaviour

`networth/_tabs.html` and `investments/_tabs.html` use `role="tablist"` and `role="tab"` on
plain links. There is no roving tabindex and no ArrowLeft/ArrowRight handler in `app.js`.
Assistive technology is told this is a tab widget, but the keyboard model is a list of links.
Either use `<nav aria-label>` + `aria-current="page"` (the simplest fix) or implement the full
tab pattern.

The same files wrap with `flex-wrap`. On a 390 px screen the seven investment tabs occupy four
rows (about 250 px) and the four net-worth tabs occupy two rows, pushing the actual content
down. They need a horizontal scroll strip or a select at phone width.

### P1 — duplicate `h1` and mismatched page titles

- `investments/_portfolio_body.html`: base `h1` is “Investments”; the panel adds a second `h1`
  “Portfolio”. Document title is “Portfolio”.
- `investments/_performance_body.html`: two `h1`s, both “Performance”.
- `investments/_providers_body.html`: base “Investments” + inner “Price providers”; title
  “Price providers”.
- `investments/_income_body.html`: base “Income” + inner “Income from investments”; title
  “Income”, which is also the ledger Income page's title.

One `h1` per page; the inner heading should be an `h2`.

### P1 — Home and Month are visually the same destination

Home's `h1` and document title are the month (“November 2026”), exactly like the Month page.
The sidebar says Home, the page heading says November, and both pages show the same flow strip
and donut pair. A user cannot tell from the top of the page which of the two they are on. Use
`Home` as the `h1` and the month as a subtitle, or give Home a distinct dashboard summary.

### P1 — forms include fields that do not apply

- **Notifications** (`notifications/channels.html`): “Daily at” is always required, even with
  “One message per reminder”. “Remind days before” is always shown, even when no “items due
  soon” reminder is enabled. `include_amounts` / `enabled` are separated from the delivery
  settings they modify.
- **Net worth add item** (`networth/_body.html`): 11 fields in one grid, including
  “Value note”, “Notes”, “Acquired on”, “Investable”, “Estimate values between entries” and
  “Include in net worth”. On a phone the section is 934 px tall, so Save is off-screen. The
  optional fields belong under “More options”, or the whole form belongs in a dialog.
- **Net worth item edit** (`networth/_macros.html`): the same 10 fields open inline inside the
  group; on a long group the form pushes everything below it and has no sectioning.
- **Workspace settings** (`workspace/settings.html`): “Investment cost basis”, “Keep investment
  asset classes linked”, “Copy the previous month's plan” and “Require two-factor
  authentication” sit in the same 806 px panel as the workspace name and currency. Basic and
  advanced settings should be separate sections (`Advanced` disclosure at minimum).
- **Recurring form** (`scheduled/form.html`): the “Options” section (automatic posting, reminder
  days, notes) is always open. It is optional information and can follow the same progressive
  disclosure as the rest of the form.

### P1 — feedback and errors

- The ledger's “Entry added.” status is `sr-only`. It should be visible and placed near the
  entry row, or the new row should be scrolled into view and briefly highlighted.
- The ledger day error wraps into a 7-line paragraph inside the 80 px Day column
  (`months/_ledger.html`), stretching the entry row far beyond the height of the other cells.
  Render it as a full-width row below the table or as a compact inline hint.
- `role="alert"` / `role="status"` are used well in most places (59 status blocks, 62 alerts),
  but many save actions have no status at all: recurring pause/resume/delete, income archive,
  category archive on some paths, notification test? (test has a notice).
- There is no HTMX pending indicator anywhere: no `hx-indicator`, no `aria-busy`, no
  `hx-disabled-elt`. On a homelab or a slow import, buttons look idle while the request runs.

### P1 — dead or misleading navigation

- The sidebar's “Loans” is `<a href="#loans">` but there is no `#loans` target. Clicking it does
  nothing (or adds a hash). “FIRE projection” is correctly a disabled item; Loans should be too
  until M7 lands.
- Home uses the same clock glyph (`◷`) as Upcoming and the same `⌕`/`≡` neighbours; the glyphs
  are decorative but not distinctive.
- There is no back control on Work calendar; every other detail page has one.

### P1 — charts, colour and data display

- Portfolio: “Unrealized gain” is not green when positive, and “Realized P&L” is black even when
  negative. The holdings table colours negative gains red. The same financial concept therefore
  changes colour by page.
- Net worth summary repeats the assets total (once in the `dl`, once above the bar). Remove one.
- The investment income table (`investments/_income_body.html`) uses `w-full text-left` without
  `.sheet`; every other data table is a sheet.
- Investment transaction rows put Edit and Delete on two lines, making 53 rows a 3847 px page.
  The page would be far shorter with a single compact action cell.

### P2 — small consistency issues

- Empty states: the `empty_state` macro is never used; every page hand-rolls a `<p class="panel
  …">` or a panel with a different structure and no action.
- Category/instrument “in use” is plain text while other statuses are chips.
- Categories edit mode has only `sr-only` labels for Name, Kind, Treatment and Colour; the user
  has to remember the column meanings.
- Detail back links are a mix of `.btn`, one-off rounded links and underlined text.
- Admin users puts “Create an account” and “Instance settings” as bare blue links; other pages
  use primary/secondary buttons.
- Admin users' “Deactivate / Make administrator / Reset two-factor authentication” are text
  links, not dangerous-action controls. They are easy to hit and hard to read.
- Workspace data and the import report use `text-lg` section headings and dense definition lists;
  this is an advanced page, but it deserves tabs or a collapsed “Details” section.
- The notification page's “New channel” form stays expanded after a successful add and is
  refilled; the new channel is visible above, which is good, but the form remains a large block.
- Profile has a full-width Save, then a text-link “Use the saved theme on this device”; the
  secondary action is less discoverable than it needs to be.

---

## 2. Page-by-page notes

### Core ledger

| Page | What works | Main gaps |
| --- | --- | --- |
| **Home** | Clear KPI strip, good upcoming summary, primary actions on overdue items. | `h1` is the month, not Home; duplicates Month; donuts dominate; inline Mark paid expands below the item on phone. |
| **Month** | Summary and editable plan are in one place; close month has a clear warning. | 2747 px page; the category plan table starts below 900 px; “Hide pending”/order controls are 24–26 px; the same flow/donuts are duplicated on Home. |
| **Transactions** | Inline edit, autocomplete, strong row tints, pending rows clearly distinct. | Entry row is at the bottom; add result is off-screen; day error breaks the row; no pending state; tables grow long. |
| **History** | Filters/export are proper dialogs; live search; bulk change is two-step. | Search has no visible label; phone table hides Category/Kind; 50 date links under 24 px high. |

### Plans and recurring

| Page | What works | Main gaps |
| --- | --- | --- |
| **Upcoming** | Week headings outside panels; overdue is clear; edit/reschedule are available. | 5–7 equal-weight actions per item; filters always open (292 px on phone; first item at y = 574); mark-paid form on phone extends to y = 946 in an 844 px viewport; future default date errors; skip is silent; full reload loses place. |
| **Recurring** | Compact table/card layout; schedule sentence is readable; pause state is clear. | Actions are 26 px; no success notice for pause/resume/delete; full reload; archived items absent (documented but can surprise). |
| **Recurring form** | Guided sections, live next-dates panel, good explanations. | 1973 px desktop / 2712 px phone; “Options” always open; without JS all repeat panels render open; preview is below the form on phone. |

### Income and tax

| Page | What works | Main gaps |
| --- | --- | --- |
| **Income** | One card per source; next payments table; tax chips; inline log form. | Actions live in a row at the bottom of each card; inline log form expands the card; archive has no success notice; preview table scrolls sideways on phone. |
| **Add income** | Four-step wizard; only the current step's fields show; good progressive disclosure. | Each step is a full POST and returns to the top; no step summary before saving; “Back”/“Cancel” are one-off styles. |
| **Work calendar** | Day grid is readable; holiday/weekend states are clear; fill/clear actions. | No back link; month heading is `text-lg`; day buttons are only as large as the grid cell; “Save” for each day repeats a small form. |
| **Taxes** | Clean cards; rates behind “Rate changes”; “Add another tax” is a disclosure. | Adding an “other tax” is a form at the bottom; the page is very sparse for one rule; rate change forms have four fields in a row that becomes a tall stack on phone. |
| **Tax form** | Advanced “calculated on” is collapsed; payer-withheld hides schedule. | “Payment day”, timing and category are one dense row; no inline example of the resulting amount until the side panel loads. |

### Net worth

| Page | What works | Main gaps |
| --- | --- | --- |
| **Summary** | Best page in the app: one bar, plain-language caption, clear totals, accessible text. | Assets total duplicated; “net worth” green is the only colour cue; page is short and calm. |
| **Assets/Liabilities** | Grouping by category, clear subtotals, in-place edit and valuation. | 2438 px; Add item at the bottom; item actions are bare 20 px links; 19 disclosure summaries are 16 px high; category add is another bottom form. |
| **History** | Chart + table + source badges; snapshot/backfill actions at the top. | 13 “Breakdown” targets at 16 px high; “Take snapshot now” and Backfill are plain POSTs with a full reload. |

### Investments

| Page | What works | Main gaps |
| --- | --- | --- |
| **Portfolio** | Strong summary cards, allocation donut, holdings table, risk table. | Duplicate `h1`; 2540 px and six panels; gain colours inconsistent; “Edit target allocation” is a 20 px summary below the risk table; chart scope control is at the very bottom. |
| **Instruments** | Add is a dialog; allocations and risk tiers are clear; archive is discoverable. | Row Edit/Archive are bare links; no pending state; add dialog can be tall on phone. |
| **Performance** | Range strip, class filter, chart, period returns; data disclosure. | Duplicate `h1`; range/class controls are two separate rows; chart is 320 px fixed height on phone; “Plotted data” is a 20 px summary. |
| **Income** | Clear yearly table; empty state explains what to record. | Table is not a `.sheet`; duplicate `h1`; no filter/year selection; outstanding dividends warnings are dense paragraphs. |
| **Asset classes** | Simple list; risk tier/investable chips; archive and restore. | 26 px arrow buttons; add form at the bottom; no confirmation/notice for archive. |
| **Providers** | Cards explain each provider, quota and errors; keys are masked. | Very long page (1550 px) with one form per card; no top-level save; “Enabled” and key forms are mixed; provider error codes are exposed. |
| **Transactions** | Filters, pagination, bulk add, edit in row. | 3847 px page; 53 rows; Edit/Delete stacked in a narrow cell; 44 small links; filter form consumes the top of the phone screen; tab bar wraps. |

### Account and administration

| Page | What works | Main gaps |
| --- | --- | --- |
| **Categories** | Clear split expense/income; create form has Kind/Treatment/Colour; archive/restore. | Huge empty middle; actions at the far right; arrows 26 × 28; add form at the bottom; edit row loses visible labels. |
| **Notifications** | Channels are understandable; test/delete are clear; masked URL. | New-channel form at the bottom; “Daily at” irrelevant for per-item; 514 px form; no conditional grouping. |
| **Workspace settings** | Logical top group; switch for Taxes has a good explanation; danger zone separated. | 806 px General panel mixes basic and advanced; full-width Save; advanced settings not collapsed. |
| **Members** | Member list and invitation form are clear; roles and expiry. | Invite form is below the member list; Copy gives no feedback; role change is a full reload with no notice. |
| **Export/import** | Templates, validation, mapping, rollback and reports are all present. | Very dense; `text-lg` headings; long definition lists; mapping rows are one large fieldset each; no progress state. |
| **Admin users** | Table shows sign-in methods, status and last sign-in. | Bare links instead of buttons; actions separated by `·`; no primary action in the header; 19 px targets. |
| **Admin settings** | Clear registration/update switches; password confirmation; demo panel. | Save is inline while other settings use full width; demo status is a long list; no separate “Advanced”. |
| **Audit** | Filters and pagination; details are readable. | 3724 px table; “Newest/Older” are 20 px links; details column is an unstructured key:value list; no result count. |
| **Profile** | Grouped details and planning; language/theme; highlight setting. | Full-width Save; “Use the saved theme” is a text link; planning fields not marked advanced. |
| **Account security** | Two-column layout; password form and sessions; 2FA entry point. | Inconsistent `rounded-xl` notice; password form lacks a visible username label (hidden field); “Sign out other devices” depends on confirm block. |
| **2FA** | Clear states (enabled, enrollment, recovery codes); QR and manual key. | “Continue” is a bare link; workspace switch appears only when >1 workspace; long recovery-code list. |
| **Auth pages** | One consistent auth frame; labels and errors; setup is a single clear form. | Setup is long but one-time; login offers password, provider, proxy, demo and registration in one column; no visual grouping beyond “or”. |
| **Errors** | Clear status, message and a return button. | 403 uses the same page as 404; no path back to the previous page. |
| **Invitation** | Shows workspace/role, sign-in paths, accept/register. | Long page when account creation is required; no summary of what happens after accepting. |

---

## 3. Fields that should be hidden, grouped or removed

| Page | Field(s) | Recommendation |
| --- | --- | --- |
| Notifications | “Daily at” | Hide when delivery is “One message per reminder”. |
| Notifications | “Remind days before” | Show only when “Items due soon” is ticked. |
| Notifications | “Include amounts” / “Enabled” | Group with delivery settings; keep the privacy explanation. |
| Net worth add item | Value note, Notes, Acquired on, Investable, Estimate between | Put under “More options”; keep Name, Category, Value, Currency, As of visible. |
| Net worth add item | “Link to…” | Keep visible only when linkable sources exist; already conditional, but the help text dominates. |
| Workspace settings | Investment cost basis, auto-link, auto-copy, 2FA | Move to an “Advanced” section or separate panel. |
| Recurring form | Options (auto-post, reminder, notes) | Collapse behind “More options”; keep the schedule summary visible. |
| Recurring form | Repeat panels | With JS they hide correctly; without JS they all render. Add `hidden` defaults server-side and let JS reveal. |
| Tax form | Payment day/timing/category | Only show for non-withheld taxes (already hidden via `data-hidden-when`); ensure no-JS fallback. |
| Investment transaction form | Dividend/split/amounts fields | Already JS-filtered; provide a server-rendered default and a no-JS note. |
| Categories edit | Kind/Treatment | Show visible labels, or move editing into a dialog with labels. |

---

## 4. Suggested order of work

1. **Fix result visibility** (P0): visible status/toast + scroll/focus to the changed row for
   ledger add, net worth add, recurring actions, upcoming skip. Add a pending state to HTMX
   writes.
2. **Move create actions to the top** (P0): use `header_actions` for Add income/New tax/Add
   instrument/Add item/Add category/New channel; make the ledger entry row sticky or add a top
   “Add entry” control.
3. **Reduce Upcoming** (P0/P1): one primary “Mark paid”, an overflow menu for
   reschedule/amount/edit rule, skip/delete with undo, filters collapsed on phone, date defaults
   to today for future items.
4. **Unify row actions and target sizes** (P1): `.btn-sm`, one overflow pattern, 44 px on phone.
5. **Fix mobile tabs and filters** (P1): scrollable tab strip or select; filters collapsed;
   no horizontal page scroll.
6. **Fix page identity** (P1): one `h1`, Home vs Month, investment `h1`s, nav `aria-current`.
7. **Consolidate components** (P1): replace one-off buttons/fields/notices; use `empty_state`;
   use one loading pattern.
8. **Finish the field cleanup** (P1): notification conditional fields, net worth “More options”,
   workspace “Advanced”, recurring options.
9. **Polish** (P2): tables, copy feedback, back links, heading sizes, colour semantics.

## 5. What is already good

- Money is consistently tabular and formatted; the ledger's row shading is meaningful.
- The Month plan and summary are unusually complete; in-place plan editing with focus retention
  is well thought out.
- The net-worth summary bar is the right amount of chart: one question, one answer, accessible
  text.
- History's filters/export/bulk dialogs are a model for the rest of the app: opener, modal,
  backdrop close, Escape, server-rendered fallback.
- The recurring form's guided sections and live preview are good progressive disclosure.
- The app has no horizontal page overflow at 390 px in the pages reviewed.
- Security-sensitive flows (confirm with password, typed workspace deletion, CSRF, masked
  secrets) are thoughtfully designed.
