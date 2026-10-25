# Post-1.0 backlog

Work deliberately left out of 1.0. Each item keeps enough context to be planned later with the
standard workflow (docs/specs/00-overview.md §8); none is scheduled yet.

Also deferred from M1 (see M1-identity-and-workspaces.md): ownership transfer and leaving a
workspace. (Workspace deletion was pulled forward on 2026-10-24, see ADR-0020.)

---

## Administrator password reset from the Users page

Today an administrator can reset someone's password only on the command line
(`assetboard reset-password <email>`). The Users page can create an account with a temporary
password but cannot reset an existing one.

**Acceptance**
- A "Reset password" action on the Users page, with its own confirm page, confirmed by the
  acting administrator like every other admin action (`run_confirmed`).
- Generates a temporary password exactly as account creation does (16 characters, through the
  hash gate, shown once in the response, never logged or audited), sets
  `users.must_change_password`, and ends every session of the account.
- Not offered for the acting administrator's own account (they use the password page), and not
  under `OIDC_ONLY` for accounts without a password.
- Audit action `admin.password_reset` with data keys `temporary_password` and
  `sessions_revoked`, added to `AUDIT_DATA_KEYS` and the audit view's labels.

---

## Warning before deactivating a workspace's only owner

Deactivating an account that is the only active owner of a workspace leaves that workspace
without an active owner until the account is activated again (documented in
docs/accounts.md). The confirm page does not warn about it.

**Acceptance**
- The deactivate confirm page lists the workspaces (by name) where the account is the only
  active owner, and says that nobody can manage those workspaces until it is activated again.
- Deactivation stays allowed; the warning is information, not a block.
- If ownership transfer (above) exists by then, the page links to it.

---

## Generic CSV import with column mapping

Moved out of M4 (owner decision, 2026-10-18; was the stretch feature F4.6). M4 imports only the
documented archive format and its CSV files (docs/specs/M4-ledger-export-import.md), so files
from banks or other apps must be converted first. This feature reads arbitrary CSV files
directly, on top of M4's import framework (upload limits, dry-run, `ImportBatch`, rollback,
dedupe, category mapping).

**Acceptance**
- Upload any CSV; encoding, delimiter and decimal separator are detected, with manual
  overrides; preview of the first 20 rows.
- Column mapping: date (with a date-format choice), amount, description, optional category, and
  either a direction column or a bank convention: **signed amounts** (negative = expense,
  positive = income) or separate **debit/credit columns**; no direction column needed.
- Saved mapping profiles (user-named, for example per bank), reused on the next upload.
- Dedupe as in M4 (content key with an occurrence number); category suggestions from previous
  descriptions.
- Synthetic sample files only in tests (no real bank exports).

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement "Generic CSV import with column mapping" from docs/specs/backlog-post-1.0.md on top of the M4 import
framework: detection with overrides, preview, column mapping incl. signed amounts and debit/credit columns, saved
mapping profiles, dedupe and category suggestions. Synthetic sample CSVs for tests only.
```

---

## Public read-only demo

Owner decision, 2026-10-25. An instance administrator can switch on a demo: a read-only
workspace with synthetic sample data that anyone can open from the login page without an
account, reset on a schedule. Meant for a public showcase instance, never for a household's own
instance.

**Why post-1.0:** it opens an unauthenticated way into the app, so it builds on 1.0 work that
does not exist yet: F9.7's synthetic demo builder (`assetboard demo-seed`), F9.6's role × route
authorization matrix and rate-limit review, and F9.1's dashboard (what a visitor should see).
Doing it earlier would mean building a second sample-data generator and hardening one route
family ahead of the security review.

**Acceptance**
- **Off by default, instance-admin toggle.** A "Public demo" setting on the instance settings
  page (`/admin/settings`), confirmed through `run_confirmed` and audited (`admin.demo_enabled`,
  `admin.demo_disabled`). A fresh install, an upgrade and a restore never turn it on; there is
  no environment variable that enables it without the admin page (an operator setting may only
  forbid it).
- **Demo workspace.** Turning it on creates one dedicated workspace marked as the demo
  (a flag or an instance setting holding its id; decide in the plan), filled by the F9.7
  builder with dates relative to the Clock, so the current month always has data. It has no
  members; it is not listed in any user's workspace switcher and is excluded from instance
  counts and from first-run setup (an instance with only the demo still runs setup).
- **Entry.** While the demo is on, the login page shows "Try the demo". It links to `GET /demo`,
  which starts a **demo session**: its own cookie (HttpOnly, SameSite=Lax, `Secure` per
  `BASE_URL`, at most 1 hour, not renewed past that), bound to the demo workspace with the
  viewer role. If the browser already has a signed-in session, `/demo` changes nothing and
  redirects to `/` (a link can never replace a real session). While the demo is off, `/demo`
  is a 404 and the button is hidden.
- **Read-only.** Every write is refused in the **service layer** (`DemoReadOnly`, a 403 page),
  not only hidden in the UI: the demo principal acts as a viewer and additionally fails every
  role check above viewer. Account pages (profile, password, two-factor), workspace settings,
  members, invitations, the audit log, every `/admin` route, workspace switching and creation,
  and every API route (F9.5) refuse a demo session. Reading pages, charts, reports and the
  History CSV export are allowed.
- **Isolation.** The workspace of a demo session comes from the session record only, never from
  the URL, a form or a header, so no request can point it at another workspace. The automated
  tenant-isolation test and F9.6's authorization matrix get a "demo" column: every non-GET
  route refuses, every admin/account/workspace-management route refuses, and every read of
  another workspace's ids returns nothing.
- **Reset on a schedule.** An APScheduler job (default daily at 03:00 in the instance time
  zone, configurable on the same settings page) replaces the demo workspace's data with a fresh
  build in one transaction, keeping the workspace id so open demo sessions keep working. A
  failed reset leaves the previous data and is reported to administrators like a failed backup
  (F9.4). Other scheduled jobs (auto-post, notifications, snapshots) skip the demo workspace;
  it has no notification channels or price providers, so it makes no external calls.
- **No email, no PII.** All data is synthetic (no real names, places or accounts). The demo
  needs no email address and sends no mail. If `Context` needs a user, it is a system account
  that cannot sign in by any method (no password, no external identity, a reserved
  non-deliverable `.invalid` address) and is not shown as a person on the Users page. Demo
  visits record no IP address or user agent beyond the in-memory rate-limit buckets, and are
  not audited one by one.
- **Rate-limited.** Starting demo sessions is limited per client address (default 10 an hour)
  and in total (default 200 an hour); requests of a demo session are limited (default 120 a
  minute); the History export is limited separately (default 5 an hour). Limits reuse the
  existing throttle and answer with `Retry-After`.
- **Turning it off** ends every demo session at once and deletes the demo workspace (the
  ADR-0020 hard deletion).
- Every page of a demo session shows a banner: "Demo: read-only sample data, reset daily"
  (wording follows the schedule), with a link back to the login page.

**Security notes**
- CSRF: the admin toggle is a POST with the usual CSRF token. `GET /demo` changes no data and
  carries no token; it only issues the demo cookie, and refusing to replace an existing session
  keeps it from being used to switch a signed-in victim into another account (login CSRF).
- The demo session is a separate principal type, not a membership: it cannot be promoted,
  invited or given a second workspace, and no code path turns it into a user session.
- The demo workspace is an ordinary workspace for the scoped repositories, so the existing
  `workspace_id` enforcement isolates it in both directions; nothing special-cases its id in
  queries.
- CSP, security headers and the no-inline-scripts rule apply unchanged. Nothing about demo
  sessions is logged at INFO or above beyond "demo session started" counts.

**Open questions (for the owner)**
1. Reset time and frequency: daily at 03:00, or every few hours for a busy showcase?
2. Should the demo allow writes in a private copy per visitor (a throwaway workspace deleted
   when the session ends) instead of strict read-only? Proposed: no, read-only only.
3. Allow the History CSV export in the demo, or refuse it to keep load low?
4. Language: the demo data's category names in the instance's default locale, or per the
   visitor's browser language at reset time (one language per reset)?

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement "Public read-only demo" from docs/specs/backlog-post-1.0.md: the instance-admin toggle (off by default, confirmed,
audited), the demo workspace built by the F9.7 builder, GET /demo issuing a separate short-lived demo session that never
replaces a signed-in one, read-only enforcement in the service layer, refusal of account/admin/workspace-management/API
routes, the scheduled reset job, rate limits, the banner. Extend the tenant-isolation test and the authorization matrix
with a demo column. No real personal data; no email; no external calls.
```
