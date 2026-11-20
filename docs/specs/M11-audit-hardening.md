# M11 — Audit hardening

**Goal:** close the actionable defects from the 2026-11-20 deep audit
([`docs/audits/2026-11-20-deep-audit.md`](../audits/2026-11-20-deep-audit.md)) in small, reviewable
PRs. The audit's “missing capabilities” table is input for a separate product milestone; it is not
part of M11.

**Source:** `b68cc3f` audit, 63 findings. This milestone covers the correctness, UX, accessibility,
security, performance and documentation findings that can be fixed without changing the product's
scope (accounts, split transactions, bank CSV import, multi-currency, API, etc. stay in the backlog).

**Rules:** one feature per PR; P1 before P2 before P3; every acceptance criterion traceable to a
test; `make check` green; UI changes also run the browser suite; Polish catalog stays 100% and the
new strings are translated; no release tag or image publish while the owner's v1.0 hold is in place.

## Features

| ID | Feature | Audit findings | Priority | Main acceptance |
|---|---|---|---|---|
| F11.1 | Polish and i18n correctness | A11Y-03, A11Y-05, A11Y-06 | P1 | `gettext("")` returns empty; risk table never shows the PO header; setup focuses the first invalid field and its language choice is unambiguous |
| F11.2 | Restore correctness | COR-01, COR-10 | P1 | Fixed tax amounts use the archive currency; restore availability counts active data, not tombstones/open month states, or explains the blockers |
| F11.3 | Backup security | SEC-01, SEC-08 | P1 | Backup create/download require re-authentication and write an audit event; demo reset/delete use the same confirmation rule |
| F11.4 | Current-balance loan checkpoints | COR-12 | P1 | A later checkpoint re-anchors the balance but the rate timeline/IO clock still starts at the loan start; schedule, linked net worth, LTV and FIRE keep working |
| F11.5 | Installment-plan lifecycle | COR-13, COR-14, COR-15 | P2 | Linked valuation respects `as_of`; post/undo/delete/auto-post keep the count symmetric; overdue installments remain postable |
| F11.6 | Historical property LTV | COR-17 | P2 | Archive date is compared with `as_of`; `estimate_between` is applied to historical values; the existing test is corrected |
| F11.7 | Closed-month occurrence integrity | COR-02, COR-03, COR-04 | P2 | Reschedule checks the old due month; skip checks every child month; unskip rejects paused/archived definitions |
| F11.8 | Reports page UX | UX-06, UX-07, UX-08, UX-09, UX-10, UX-13, UX-15 | P1/P2 | Empty range is one empty state; filters collapse on phones; warnings are section-level; series tables use the existing disclosure; gain colours follow the convention; hidden non-base/undated states explain themselves |
| F11.9 | Mobile tables and page identity | UX-01, UX-02, UX-03, UX-04, UX-05, UX-12, A11Y-01, A11Y-02, UX-17 | P1/P2 | Transactions has a first-viewport add control; Month shows the difference on phones; admin users and wide tables fit/scroll accessibly; Month h1 is “Month”; confirm dialog has a name; skip link exists; Home customise is reachable |
| F11.10 | Charts and demo data | UX-11, COR-08, COR-09, COR-11, UX-14, UX-16 | P2/P3 | Last chart label fits; demo net worth reconciles with loans; demo taxes are visible where tax data exists; “Other” labels are distinct; empty Home/FIRE states are honest |
| F11.11 | Auth, logging and config hardening | SEC-02, SEC-03, SEC-04, SEC-05, SEC-06, SEC-07 | P2/P3 | Successful logins do not self-lock the account (or the documented trade-off is changed); log paths are escaped; expired sessions are pruned; demo imports are bounded; broad proxy CIDRs and short secret keys are refused/warned |
| F11.12 | Performance and benchmarks | OPS-01, OPS-02, OPS-03 | P2/P3 | Home net-worth/portfolio reads are bounded; Reports reads the report range, not all history; benchmark budgets match the spec or the spec is amended |
| F11.13 | Documentation and release | DOC-01, DOC-02, DOC-03, DOC-04, DOC-05, DOC-06 | P2/P3 | Main README status, release branch, Reports screenshot, security checklist wording, Taxes docstring and demo backlog are consistent |
| F11.14 | Prior-audit UI items | PA-01, PA-02, PA-03, PA-04 | P2/P3 | Net-worth add form is grouped; notification fields are conditional; summary totals are not repeated three times; disclosure targets are 44 px on phones |
| F11.15 | Small correctness fixes | COR-05, COR-06, COR-07, COR-16, COR-18, COR-19 | P2/P3 | Domain block difference follows the spec; no-plan bars are neutral; future-dated undo is not offered; FIFO avg cost is correct; same-date XIRR is undefined; zero-total categories follow the chosen spec rule |

## Suggested order

1. F11.1, F11.2, F11.3, F11.4 — P1 correctness/security/i18n.
2. F11.5–F11.7 — loan/installment/closed-month data integrity.
3. F11.8–F11.10 — the visible UX, mobile, charts and demo issues.
4. F11.11–F11.15 — hardening, performance, docs and prior-audit polish.

## Out of scope

- The audit's missing capabilities: accounts/reconciliation, split transactions, bank CSV import
  with mapping, multi-currency ledger entry, trash view, rules, tags, attachments, API/metrics,
  passkeys/admin MFA and goals/debt-payoff planning. These need product decisions and separate
  specs.
- The v1.0 release itself. The owner's hold remains; M11 does not create a tag, publish an image or
  merge the release PR.

## Definition of done

1. Every acceptance criterion is covered by a test (unit, web or browser as appropriate).
2. `make check` is green; migrations are included only if a schema change is needed (most features
   should not need one).
3. UI strings are translated; the Polish catalog stays 100% and the new plural forms are checked.
4. The relevant user docs and the audit finding status are updated; the changelog follows
   Conventional Commits.
5. The PR description lists assumptions, deviations and follow-ups; no unrelated refactor.
