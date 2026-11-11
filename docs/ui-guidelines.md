# AssetBoard UI guidelines

Status: adopted with 0.6.1, 2026-11-11. Baseline: v0.6.0 (`de48a75`).
This guide describes how AssetBoard screens should look and behave. It is written for
contributors adding a page, a form, a table or an action. The audit in
[`ui-audit-2026-11.md`](ui-audit-2026-11.md) lists where the current UI does not follow it.

## 1. Principles

1. **One obvious next action.** Every page and every panel has one primary action. Secondary
   actions are quieter; destructive actions look destructive.
2. **The answer appears where the click happened.** A save, delete, archive or skip must produce
   visible feedback in the current viewport. A flash message at the top of a long page is not
   enough.
3. **Show only what the decision needs.** Required fields first. Optional, advanced or
   mode-specific fields are hidden behind “More options”, a disclosure or a separate step.
4. **Same concept, same control.** Expense/income, delete, cancel, back, filter, empty state and
   table action must look and behave the same on every page.
5. **Dense but calm.** The app is a ledger, not a dashboard demo: square panels, thin rules,
   tabular numbers, restrained colour. Decoration must carry meaning.
6. **Never trap or strand the user.** Preserve scroll and open disclosures across HTMX swaps;
   offer a way back; do not ship dead links.
7. **Test at 390 × 844 and 1440 × 900.** A page is not done at one width.

## 2. Page frame

Every signed-in page uses `base.html`:

```
workspace name (small caps)                         [user menu]
H1: the destination
[primary action, left-aligned]
page intro / period (optional)
panels and tables
```

For a page with tabs, the primary action goes below the tab strip, not above it: the action
belongs to the tab currently shown.

- **One `h1` per page.** It names the destination, not the period: `Home`, `Month`,
  `Transactions`, `History`, `Upcoming payments`, `Recurring items`, `Income`, `Taxes`,
  `Net worth`, `Investments`, `Categories`, `Notifications`, `Workspace settings`.
  Put the period in a subtitle or in the page title block (`Transactions: November 2026` is the
  exception because both destination and month matter).
- The base template has a `header_actions` block, rendered as a left-aligned row directly under
  the `h1`. Use it for the page's primary action (`Add income`, `New tax`, `Invite someone`).
  Pages with tabs put the action below the tab strip instead, in a `data-page-actions` row, so
  it clearly belongs to the active tab. Do not put primary actions at the top right beside the
  user menu.
- Page widths:
  - dashboards and lists: `max-w-6xl` (the base default);
  - settings and forms: `max-w-3xl`;
  - focused confirmations: `max-w-2xl`;
  - import/report pages: `max-w-4xl`.
- Detail pages (income edit, tax edit, instrument, work calendar, recurring edit) start with one
  consistent back control. Prefer a `.btn` “Back to …” in the content header, not a one-off
  rounded link on some pages and an underlined text link on others.
- The first viewport must contain the page title, the primary action and the first data row (or
  an explicit empty state). Filters belong below the title and must not push the data off a
  phone screen.

## 3. Typography and spacing

| Role | Style |
| --- | --- |
| Page title | `text-2xl sm:text-3xl font-semibold tracking-tight` |
| Panel title | `text-base font-semibold` |
| Subsection | `text-sm font-semibold` |
| Body | `text-sm` |
| Meta / help | `text-xs text-muted` |
| Money and quantities | `tabular-nums`; right-aligned in tables |
| Field label | `text-sm font-medium text-ink` |

- Do not use `text-lg` for panel titles. It is reserved for the product name and auth pages.
- Do not use `text-xs` labels for primary form fields. The net-worth and investment forms
  currently do this; labels there are harder to scan than on the income and recurring forms.
- Page stack: `space-y-6`. Panel padding: `p-4 sm:p-6`. Large settings panels may use
  `p-6 sm:p-7`. Do not mix `p-4`, `p-5`, `p-6` and `p-7` arbitrarily.
- Field stack: `space-y-1.5` (or `gap-4` in a grid). Help text belongs directly below its field.
- Notices sit `mb-4` below the page header or above the panel they refer to.

## 4. Components

### Buttons

Use the components in `static/css/input.css` and add only size/layout utilities:

- `.btn-primary` — the one main action of a page or form.
- `.btn` — secondary actions (Cancel, Filters, Export, Back).
- `.btn-danger` — destructive actions (delete, roll back, remove), including the accept button
  of a destructive confirmation.
- `.btn-sm` — table-row actions, 32–36 px high.
- `.btn-icon` — icon-only action with an `aria-label`.

Rules:

- No new `rounded-xl bg-primary px-4 py-2 …` strings in templates. The 0.6.1 sweep replaced the
  remaining primary/secondary one-offs on the reviewed pages; report any new one in review.
- Do not use a full-width submit button on a settings form while every other form uses an
  inline button. Full-width is for auth pages and narrow first-run forms only.
- Minimum target: 44 × 44 px on phones for primary/row actions; 24 × 24 px absolute minimum on
  desktop. Arrow buttons (26 × 28 px) and bare 16 px table links fail this.
- A row has at most one visible primary action. Put Edit/Archive/Delete in a quiet action group
  or an overflow menu, not five equal-weight buttons.
- Destructive confirmation uses the danger tone: mark the `<form>` or HTMX trigger with
  `data-confirm-tone="danger"` and the global dialog swaps its accept button from `.btn-primary`
  to `.btn-danger`. Reversible archive, pause and skip actions keep the primary tone.

### Forms

- Use `field`, `select` and `month_picker` from `macros/components.html`.
- Required is the default; mark optional fields “(optional)”.
- Two columns maximum on desktop; one column below `sm`. A create form with more than five
  fields should be a dialog or a guided page, not a block at the bottom of a list.
- Hide fields that do not apply to the current mode:
  - notification digest time is irrelevant when delivery is “one message per reminder”;
  - payment schedule is irrelevant for a payer-withheld tax;
  - investment dividend fields are irrelevant for a buy.
- Use a segmented control (radio cards) for two or three mutually exclusive choices
  (expense/income, repeat frequency, tax kind). Use a select for longer lists.
- Validate on the server. On refusal, focus the first invalid field and show its message next to
  it. If a table cell is too narrow for the message, render the error as a full-width row below
  the table, not as seven wrapped lines inside an 80 px column.
- Show a pending state for HTMX writes: disable the submit control and set `aria-busy` (or show
  a small inline spinner). Do not leave the button looking idle while the request runs.

### Tables

- Data tables use `.sheet` (or `.sheet-sm` for card rows on phones). Include a `<caption>` when
  the panel heading does not already name the table; a visually hidden caption is enough.
- Right-align numbers and use `tabular-nums`. Dates and amounts use the locale formatters.
- Gains and losses use one convention: negative values `text-danger`, positive values
  `text-success-strong`, zero or absent values neutral. In portfolio and investment tables do not
  colour money that is not a gain or a loss (value, cost basis, price, totals).
- Keep an action column with an `sr-only` “Actions” header. Do not stack Edit and Delete on two
  lines when one line plus an overflow menu fits.
- On a phone, either use card rows or an explicit horizontal scroll container. Never hide
  critical columns without a visible hint. A table that is 40 rem wide inside a 390 px screen
  needs a “scroll sideways” affordance or a card layout.
- Pagination is one pattern: Previous / “Page X of Y” / Next, both sides, same labels and styles.
- Filter bars are compact. On phones, collapse advanced filters behind a “Filters” button and
  show a count of active filters. Do not put a 290 px filter panel above the first item.

### Feedback and empty states

- Every write produces one of:
  - an inline change plus a toast or status line;
  - a dialog that closes and a toast;
  - a navigation that lands on the changed row, with the row in view.
- Never rely on a flash message that is above the scroll position, and never rely on a row that
  appeared above the viewport. Measured examples are in the audit.
- Preserve scroll and open `<details>`. Reuse `data-keep-visible` for row swaps. Restore focus to
  the control that started the action, or to the next logical field.
- Use the `toast` macro for undoable actions; use a normal status block for persistent results.
- Notice and status blocks on signed-in pages are square (`border … px-4 py-3`), not rounded
  cards. The auth frame is the only place that keeps its rounded notice style.
- Empty states use `empty_state(title, message, action_label, action_url, icon=…)`. Use it for
  page-level “nothing here yet” panels; the optional icon is a short character, not an image.
  When an existing `data-*` test hook must stay, wrap the macro in a plain element with that
  attribute. A panel-level empty message inside a data table may stay in place.
- Copy actions need feedback: set `data-copied-label="{{ _('Copied') }}"` on a
  `data-copy-target` button; `app.js` swaps the label for 1.5 s and restores it.

### Dialogs and popovers

- Short create/edit forms (≤ 6 fields), filters, export and bulk preview belong in a modal
  `<dialog data-dialog-modal>`. Row-level edits that must stay in context may use a `<details>`
  in the row.
- Complex forms (recurring rule, income wizard, import mapping) stay on a page.
- Backdrop click closes by default; only irreversible confirmations opt out with
  `data-backdrop-static`.
- Escape closes; focus returns to the opener; the close control is visible; a tall dialog scrolls
  inside the viewport.
- Do not nest dialogs. Do not leave several inline editors open in a repeated list.

### Tabs and navigation

- Page tabs are navigation, not an ARIA tab widget, unless arrow-key and roving-tabindex
  behaviour is implemented. Use `<nav aria-label="…">` with links and `aria-current="page"`;
  the current `role="tablist"` / `role="tab"` markup without arrow keys is misleading for
  screen-reader users.
- Tabs must fit in one row or scroll horizontally. Seven tabs wrapping to four rows on a phone
  is not acceptable; use a select or a scroll strip.
- Sidebar links exist only for implemented destinations. An unimplemented destination is a
  disabled item with “Coming later”, not an anchor that jumps nowhere.
- Active state belongs on the parent section for all of its child pages.

### Charts

- A chart answers one question. Use a donut for composition, a line for a trend, a single bar
  for a comparison. Do not add a second chart for the same question.
- Provide a text summary and an accessible data table or disclosure. Do not encode meaning in
  colour alone.

## 5. Interaction matrix

| Action | Placement | Feedback | Undo |
| --- | --- | --- | --- |
| Create a primary record | action row under the title, or under the tab strip | dialog closes / row appears in view + toast | not needed |
| Add a ledger entry | end of the chronological table | new row highlighted in view + toast/status | delete offers undo |
| Edit in a row | row action | row updates in place, stays in view | cancel restores |
| Delete | row/overflow | toast with Undo; row disappears | 10 s |
| Archive | row/overflow | toast with Restore or link to archived section | restore |
| Skip a planned occurrence | row | toast “Skipped” with Undo | undo |
| Settings save | form bottom | status at the top of the form + focus stays | not needed |
| Import/export | dedicated page/dialog | progress/result panel, never silent | rollback where supported |

## 6. Accessibility

- One `h1`; logical `h2`/`h3` order.
- All inputs have visible labels, or an `sr-only` label when the table header supplies the
  context.
- Do not use colour alone (gains/losses also use signs and text).
- Async results use `role="status"` / `aria-live`; errors use `role="alert"` and are focusable.
- Focus is visible on every interactive element; the global focus ring is the baseline.
- Dialog focus is trapped by the native `<dialog>`; return focus on close.
- `aria-current` for navigation; `aria-pressed` for toggles; `aria-expanded` for disclosures.
- Row actions include the row name in their accessible name (`Edit Apartment`, not `Edit`).
- Test keyboard-only: Tab, Enter, Escape, and arrow keys where the widget claims to support
  them.

## 7. Language and i18n

- Every user-facing string goes through gettext. No hardcoded UI text, including aria-labels,
  titles and confirmation messages.
- Prefer plain language and explain finance terms once. Do not give tax or investment advice.
- Amounts, dates, quantities and percentages use the locale helpers.
- Empty states say what will appear here and what to do next.

## 8. Pull-request checklist

- [ ] One `h1`; page title matches the destination.
- [ ] Primary action in the left-aligned page action row under the title, or under the tab strip.
- [ ] No create/edit form below a long list unless it is a deliberate wizard.
- [ ] Every action has visible feedback in the current viewport.
- [ ] Scroll, focus and open disclosures survive HTMX swaps.
- [ ] Forms use the shared field/button components; no one-off button class strings.
- [ ] Optional fields are hidden until needed; no field that is irrelevant to the chosen mode.
- [ ] Row actions have adequate targets and meaningful accessible names.
- [ ] Tables use `.sheet`; phone layout checked; no horizontal page scroll.
- [ ] Tabs fit or scroll; no `role="tab"` without keyboard behaviour.
- [ ] Empty states use the shared macro.
- [ ] Loading/pending state for HTMX writes.
- [ ] Tested at 390 × 844 and 1440 × 900, light and dark.
- [ ] `make check` passes.
