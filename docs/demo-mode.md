# Demo workspace

AssetBoard ships a deterministic demo dataset for screenshots, manual testing and the public
"Try the demo" mode. This page documents the builder, its command and the demo mode an operator
turns on for visitors.

> [!WARNING]
> Demo mode is for public, sample-only instances. Anyone who can reach the instance can sign in
> to the demo workspace without a password, and everything a visitor enters there is visible to
> other visitors until the next reset. Never enable it on an instance that holds real data.

## Configuration

Demo mode is off by default and only the operator can turn it on (there is no in-app switch).
It is configured through the environment; see the
[environment reference](deployment.md#environment-variables) for the full table.

| Variable | Default | Meaning |
| --- | --- | --- |
| `ASSETBOARD_DEMO_ENABLED` | `false` | Create the demo user/workspace at startup and show the login button. |
| `ASSETBOARD_DEMO_RESET_TIME` | `03:00` | Daily reset time, `HH:MM` (24-hour). |
| `ASSETBOARD_DEMO_TIMEZONE` | `UTC` | IANA time zone of the reset and of the demo workspace. |
| `ASSETBOARD_DEMO_IP_LIMIT` | `20` | Successful demo sign-ins per client address per hour. |

At startup with demo mode on, the log contains a warning that anyone who can reach the instance
can sign in to the demo workspace. The demo user is `demo@assetboard.invalid` (the reserved
`.invalid` domain), has no password, is never an instance administrator, and owns one workspace
flagged as the demo. It is hidden from the administrator's account list and from real users'
workspace switchers.

The first start creates the demo workspace and builds the dataset; later starts align its name,
default currency, language and time zone with the instance defaults and only rebuild when it is
empty, stale (older than 24 hours) or built by an older `DEMO_DATASET_VERSION`.

## What visitors see

- **Login button.** While demo mode is on and ready, `/login` shows a secondary **Try the demo**
  button below the password and SSO buttons with the hint "Shared sample workspace, resets daily
  at {time}". The button is a CSRF-protected `POST /login/demo`; it creates a normal session for
  the demo user (no password, no two-factor step) and lands on Home. Demo sign-ins are counted
  per client address in a dedicated throttle bucket; a refusal shows the usual throttle message.
- **Banner.** Every page of a demo session carries a non-dismissable server-rendered banner:
  "Demo workspace: shared with other visitors and reset every day at {time} ({timezone}). Don't
  enter real data."
- **Allowed.** Everything a normal workspace member can do with the sample data, including CSV
  imports and rollback, archive export, archive merge, restore into an emptied workspace (all
  within the existing upload limits), and the normal month, plan, net-worth, investment and
  report pages.
- **Restricted.** The demo user cannot change its password, email, display name, language,
  TOTP, linked SSO accounts or profile fields; the account cannot be deleted. Demo users cannot
  create, rename, delete or leave workspaces, invite anyone or change members and roles.
  Notification channels and test sends, price-provider configuration and refresh, and any email
  to the demo user are refused at the service layer with a `DemoRestricted` error (a translated
  403 page); the UI hides those controls. The scheduler skips every workspace job for the demo
  workspace except its reset. These guards are implemented by the F9.11b restriction work and
  use the same `Context` flags as the login and banner.

## Reset

The dataset is rebuilt daily at `ASSETBOARD_DEMO_RESET_TIME` in `ASSETBOARD_DEMO_TIMEZONE`, and
at startup when the last successful reset is older than 24 hours or the builder's
`DEMO_DATASET_VERSION` changed. One transaction takes the instance lock and the workspace lock,
deletes every workspace-owned row except the workspace and its membership (children first, from
the tenant-coverage registry), and runs the F9.11a builder with `T` = today in the demo time
zone. The workspace id, its membership and demo sessions stay valid, so a visitor keeps their
session across a reset.

On success the instance settings record `demo_last_reset_at` and the dataset version, clear the
last error, and append the instance-level audit event `demo.reset` with the reason (`startup`,
`scheduled` or `manual`), status and version. On failure the transaction rolls back, the old
data and stored import files stay, only the exception class is logged and stored in
`demo_last_reset_error`, and `demo.reset` records `failed`. A failed reset never blocks startup
or stops the scheduler.

Stored upload files of the old import batches are removed only after the reset commits; the new
build's own upload file is removed after commit too, and only that new file is cleaned up when a
build fails.

## Turning demo mode off and deleting the data

Turning `ASSETBOARD_DEMO_ENABLED` off removes the login button and route (the route answers 404),
revokes every demo session at startup and deactivates the demo user. The workspace and user stay
so an administrator can inspect them; no data is deleted automatically.

Instance administrators get a **Demo mode** panel on **Instance settings**:

- while demo mode is on: status, last reset and its result, dataset version, next reset, and a
  CSRF-protected **Reset now** button that runs the same service with reason `manual`;
- while demo mode is off but a demo workspace exists: the panel stays visible and offers
  **Delete demo data** (CSRF-protected, instance administrators only). It revokes demo sessions,
  removes the demo workspace and every row that belongs to it, deletes the demo user and writes
  the instance-level audit event `demo.deleted`.

## Builder

The same builder is available to operators for screenshots and manual testing:

```console
assetboard demo-seed --workspace <workspace-id> [--date YYYY-MM-DD]
```

### What the builder creates

`assetboard demo-seed` fills an **empty** workspace with data relative to a reference date `T`
(the reset date), in the workspace's locale and base currency:

- the suggested ledger, net-worth and investment categories;
- four months of ledger activity (`M-3`…`M0`), planned values for every month and `M-3` closed;
- recurring items of every kind (once, weekly, monthly, yearly) with posted, pending, skipped
  and overdue occurrences;
- an hourly income source with a filled work calendar for `M-3`…`M0` and an attached tax rule;
- net worth items (current account, savings, emergency fund, car, apartment, mortgage, credit
  card) with monthly valuations (one deliberate gap) and twelve monthly snapshots;
- a portfolio: a global ETF, an 80/20 fund, a USD stock (sells, quarterly dividends, linked
  income, a 2-for-1 split and a base-settled buy), gold, crypto, a bond-model instrument with
  terms and a currency cash holding, plus 24 months of weekly manual prices and FX rates;
- one completed CSV import through the real import path, so its history and rollback show.

Nothing is dated after `T` except planned values, scheduled occurrences and the bond maturity.
Notification channels, price-provider keys, provider errors, invitations and other members are
intentionally not created (see `DEMO_EXCLUDED_MODELS` in `domain/demo.py`).

The dataset is deterministic: the same `T`, locale and base currency produce the same rows
(compared through the workspace archive), because the pure plan in `domain/demo.py` seeds its
`random.Random` from `T` and sorts every collection. Amounts are defined once in EUR-scale and
converted with the fixed factor table in that module. `DEMO_DATASET_VERSION` is bumped whenever
the contents change; F9.11b's reset job compares it.

### Command

- `--workspace` is the id of an existing workspace. The command refuses a workspace that is not
  empty; an untouched suggested category/net-worth/investment set still counts as empty, exactly
  like archive restore.
- `--date` is `T`; it defaults to today in the workspace's time zone.
- The command prints counts only, never amounts. It does not enable a price provider or send any
  notification.
- The workspace owner's `birth_year` is filled when empty (and the default life expectancy
  becomes 88), so age-based risk modes have a profile. A real profile is never overwritten.

The builder writes everything in one `BEGIN IMMEDIATE` transaction through the normal services,
so validation, workspace locks, month guards and audit apply. Historical rows use a `Context`
whose fixed clock is at the row's date, and `M-3` is closed last because a closed month refuses
ledger writes (including linked dividend income).

## Related

- [Workspace archive format](archive-format.md) — the round-trip check uses the builder's data.
- [Import and export](import-export.md) — the generated import exercises this path.
