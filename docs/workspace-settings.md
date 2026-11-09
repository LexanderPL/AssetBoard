# Workspace settings

Owners open "Workspace settings" from the workspace menu. The page has:

- **Workspace name**: 1 to 120 characters.
- **Base currency**: the currency every amount in the workspace is entered and stored in.
- **Country**: optional.
- **Time zone**: decides when a day and a month begin for the workspace (for example, which
  month "Today" opens; see [Months](months.md)).
- **Formatting default**: the language used for numbers and dates where no personal preference
  applies. Each member's interface language follows their account and browser.
- **Copy the previous month's plan when a month is opened for the first time** (off by
  default): when an editor or owner opens the current month or a later one that has never had
  planned values, the previous month's planned values are copied into it once. Planned values
  of archived categories and transfers are not copied, and existing planned values are never
  overwritten. A month that once had planned values (even if they were all removed later) and
  past months are never filled automatically; they offer the copy as a button instead.
- **Investment cost basis**: **Average cost** (the default) or **FIFO** for investment holdings
  and realized gains. Holdings are recomputed on read, so switching recalculates the whole
  history; nothing derived is stored. The note under the field says so.
- **Keep investment asset classes linked in net worth automatically** (on for new workspaces,
  off for workspaces created before this setting): investment writes create and keep one linked
  asset item per active asset class with a value. Turning it off leaves existing links working
  and shows the manual linking button on the Net Worth page while a class still needs a link.

Changes are recorded in the workspace audit log under "Workspace settings" (which fields
changed, not their values).

The page holds settings only. Notification channels are on each member's Profile page, while
Categories and workspace creation/switching stay in the account menu.

## The base currency is locked once amounts exist

Amounts are stored in the base currency and are never converted. So the base currency can be
changed only while the workspace has no transactions, no planned values and no investment
transactions. Once one exists, the field is shown locked with the reason, and a change is
refused. Deleted transactions and deleted investment transactions count too: they still hold
an amount in that currency and can be restored. A removed planned value is gone for good
(there is no undo for planned values), so it no longer locks the currency: with no transaction,
no planned value and no investment transaction left, the currency can change again.

Choose the base currency before you start entering amounts. All other settings can still be
changed at any time.

## Deleting a workspace

At the bottom of the settings page, owners find "Delete workspace". It opens a confirmation
page; type the workspace's exact name there (letter case matters) and confirm.

Deleting is permanent, for every member: all transactions (deleted ones included), planned
values, categories, recurring items, closed months, members, pending invitations and the
workspace's own audit log are removed at once, and the app keeps no copy. Download a workspace
archive first on the **Export and import** page (see [Import and export](import-export.md#exporting)):
it holds the settings, categories, planned values, transactions and month states, and can be
restored into a new workspace later. Recurring items, income sources and tax rules are not in
the archive yet. The confirmation page also links to the transaction export (CSV).

Afterwards, you and the other members continue in another workspace you belong to. If you have
none left, you see the "No workspace" page, where you can create a new workspace (when the
instance allows it). Deleting the only workspace of the instance is allowed.

The instance's audit log keeps one entry, "Workspace deleted", with the workspace's name, the
date and who deleted it.
