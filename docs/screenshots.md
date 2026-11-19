# Screenshots

Every screenshot on this page is generated from the deterministic demo dataset by
`scripts/capture_screenshots.py`. The images show synthetic data, a reference date of
2026-11-20 and a EUR workspace; no real financial data is involved.

```console
uv run python scripts/capture_screenshots.py
```

The script can also capture a running instance seeded with `assetboard demo-seed`:

```console
uv run python scripts/capture_screenshots.py --base-url http://127.0.0.1:8000
```

Each page is captured in four variants: desktop light (1440 × 900), desktop dark, phone light
(390 × 844) and phone dark. The committed files are JPEG at quality 85 under
`docs/assets/screenshots/<viewport>/<theme>/<page>.jpg`. Regenerate and commit them when a page's
layout changes; keep the reference date in the script unless the demo data itself changes.

## Home

The dashboard with the month in/out widget, expense and income donuts, upcoming payments and the
other widgets.

| Desktop light | Desktop dark | Phone light | Phone dark |
| --- | --- | --- | --- |
| ![Home, desktop light](assets/screenshots/desktop/light/home.jpg) | ![Home, desktop dark](assets/screenshots/desktop/dark/home.jpg) | ![Home, phone light](assets/screenshots/phone/light/home.jpg) | ![Home, phone dark](assets/screenshots/phone/dark/home.jpg) |

## Month

Planned versus actual for November 2026, the closed previous month and the summary widgets.

| Desktop light | Desktop dark | Phone light | Phone dark |
| --- | --- | --- | --- |
| ![Month, desktop light](assets/screenshots/desktop/light/month.jpg) | ![Month, desktop dark](assets/screenshots/desktop/dark/month.jpg) | ![Month, phone light](assets/screenshots/phone/light/month.jpg) | ![Month, phone dark](assets/screenshots/phone/dark/month.jpg) |

## Transactions

The inline entry row and the month's entries.

| Desktop light | Desktop dark | Phone light | Phone dark |
| --- | --- | --- | --- |
| ![Transactions, desktop light](assets/screenshots/desktop/light/transactions.jpg) | ![Transactions, desktop dark](assets/screenshots/desktop/dark/transactions.jpg) | ![Transactions, phone light](assets/screenshots/phone/light/transactions.jpg) | ![Transactions, phone dark](assets/screenshots/phone/dark/transactions.jpg) |

## History

Search, filters and the paginated list of all entries.

| Desktop light | Desktop dark | Phone light | Phone dark |
| --- | --- | --- | --- |
| ![History, desktop light](assets/screenshots/desktop/light/history.jpg) | ![History, desktop dark](assets/screenshots/desktop/dark/history.jpg) | ![History, phone light](assets/screenshots/phone/light/history.jpg) | ![History, phone dark](assets/screenshots/phone/dark/history.jpg) |

## Reports

Income against expenses and the trend, savings-rate and category reports.

| Desktop light | Desktop dark | Phone light | Phone dark |
| --- | --- | --- | --- |
| ![Reports, desktop light](assets/screenshots/desktop/light/reports.jpg) | ![Reports, desktop dark](assets/screenshots/desktop/dark/reports.jpg) | ![Reports, phone light](assets/screenshots/phone/light/reports.jpg) | ![Reports, phone dark](assets/screenshots/phone/dark/reports.jpg) |

## Net worth

Assets, liabilities and the derived value history.

| Desktop light | Desktop dark | Phone light | Phone dark |
| --- | --- | --- | --- |
| ![Net worth, desktop light](assets/screenshots/desktop/light/net-worth.jpg) | ![Net worth, desktop dark](assets/screenshots/desktop/dark/net-worth.jpg) | ![Net worth, phone light](assets/screenshots/phone/light/net-worth.jpg) | ![Net worth, phone dark](assets/screenshots/phone/dark/net-worth.jpg) |

## Investments

The portfolio, its allocation and the instrument list.

| Desktop light | Desktop dark | Phone light | Phone dark |
| --- | --- | --- | --- |
| ![Investments, desktop light](assets/screenshots/desktop/light/investments.jpg) | ![Investments, desktop dark](assets/screenshots/desktop/dark/investments.jpg) | ![Investments, phone light](assets/screenshots/phone/light/investments.jpg) | ![Investments, phone dark](assets/screenshots/phone/dark/investments.jpg) |

## Loans

The loan list with mortgage, car loan and renovation loan.

| Desktop light | Desktop dark | Phone light | Phone dark |
| --- | --- | --- | --- |
| ![Loans, desktop light](assets/screenshots/desktop/light/loans.jpg) | ![Loans, desktop dark](assets/screenshots/desktop/dark/loans.jpg) | ![Loans, phone light](assets/screenshots/phone/light/loans.jpg) | ![Loans, phone dark](assets/screenshots/phone/dark/loans.jpg) |

## FIRE

The saved FIRE plan, its adjustments and the projection.

| Desktop light | Desktop dark | Phone light | Phone dark |
| --- | --- | --- | --- |
| ![FIRE, desktop light](assets/screenshots/desktop/light/fire.jpg) | ![FIRE, desktop dark](assets/screenshots/desktop/dark/fire.jpg) | ![FIRE, phone light](assets/screenshots/phone/light/fire.jpg) | ![FIRE, phone dark](assets/screenshots/phone/dark/fire.jpg) |

## Account

Profile, language, theme, planning profile and sign-in methods.

| Desktop light | Desktop dark | Phone light | Phone dark |
| --- | --- | --- | --- |
| ![Account, desktop light](assets/screenshots/desktop/light/account.jpg) | ![Account, desktop dark](assets/screenshots/desktop/dark/account.jpg) | ![Account, phone light](assets/screenshots/phone/light/account.jpg) | ![Account, phone dark](assets/screenshots/phone/dark/account.jpg) |

## Related

- [Demo workspace](demo-mode.md): the builder, the CLI and the public demo mode.
- [User guides](dashboard.md): the pages shown above.
