# Demo workspace

AssetBoard ships a deterministic demo dataset for screenshots, manual testing and the
"Try the demo" mode. This page documents the builder and its command; the public login button,
service restrictions and the daily reset are **F9.11b** and extend the same builder.

## What the builder creates

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

## Command

```console
assetboard demo-seed --workspace <workspace-id> [--date YYYY-MM-DD]
```

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
