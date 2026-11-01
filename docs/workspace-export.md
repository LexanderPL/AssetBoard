# Workspace export

Owners and editors can open **Export and import** in the workspace menu and select
**Download archive**. The ZIP contains the current workspace's settings (including the Taxes switch and the
Upcoming window), all categories
(including archived categories), transactions (including deleted entries), planned values,
and stored month states (closed months and plan-start markers). Recurring rules, occurrences,
income sources, work logs/calendars, and tax rules await the F3.8 archive extension. Notification
channels and delivery logs are excluded; channel URLs can contain secrets.

The download is a copy: exporting does not change the ledger. The audit log records the
export and counts of each kind of row, without descriptions or amounts. Viewers cannot
download a full archive.

Keep the ZIP intact for future restore. Import and restore are later M4 features and are not
available on this page yet. The archive is unencrypted and contains financial history,
including deleted entries. Store it privately. CSV text is preserved exactly, including
values beginning with spreadsheet formula characters; inspect it as text or disable formula
execution when opening it in a spreadsheet.

The archive contains `manifest.json`, `workspace.json`, `categories.csv`, `budget_lines.csv`,
`transactions.csv`, and `months.csv`. The manifest identifies format version 1.1 and records
SHA-256 checksums, byte sizes, and CSV row counts. CSV uses UTF-8, comma separators, ISO dates,
and exact decimal amounts. Members, invitations, audit events, and user accounts are excluded.
See the [normative format reference](specs/M4-ledger-export-import.md#format-reference-v11)
for columns, ordering, and compatibility rules.

Origin IDs are preserved when exporting previously imported categories and transactions.
Exports with identical ledger data, application version, and export timestamp have identical
bytes. ZIP member timestamps use UTC with the ZIP format's two-second resolution; the
manifest preserves the full export timestamp.
