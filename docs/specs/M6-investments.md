# M6 — Investments

**Goal:** record purchase (and sale) history for stocks/ETFs, bonds, metals, crypto, currencies and real estate; see current value, gains, allocation and value over time.

**Exit criteria:** holdings are computed from transactions; prices come from pluggable providers with manual fallback; the portfolio page shows value, cost, gain, allocation by instrument and by asset class, a pie chart of stock values, and a value-over-time chart with an invested-capital line and XIRR; portfolio values feed net worth.

Every agent prompt starts with: *"Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go)."*

**Data model (sketch)**
- `AssetClass(id, workspace_id, name, investable bool, color, sort_order, risk_tier[safe|growth|speculative]?)`. Seed (en/pl): Stocks, Bonds, Metals, Crypto, Currencies (all investable); Real estate, Primary residence (not investable); Other. Seeded tiers: Bonds, Metals → safe; Stocks, Currencies → growth; Crypto → speculative; non-investable classes and Other → none (F6.11).
- `Instrument(id, workspace_id, name, symbol?, type[equity|etf|bond_market|bond_model|metal|crypto|currency|real_estate|other], quote_currency, unit_label, valuation_source[provider|manual|bond_model], provider_key?, provider_symbol?, risk_tier_override[safe|growth|speculative]?, archived_at)`
- `RiskTargetPolicy(workspace_id PK, mode[fixed|age_scaled|age_in_bonds], safe_bp, growth_bp, speculative_bp, tolerance_bp)` (F6.11).
- `InstrumentAllocation(instrument_id, asset_class_id, weight_bp)`; weights per instrument must sum to 10 000. A single instrument can be split across classes (e.g., an 80/20 stock/bond fund).
- `InvestmentTransaction(id, workspace_id, instrument_id, date?, type[buy|sell|dividend|fee|split], quantity Decimal, unit_price Decimal, quote_currency, fee Money, fx_to_base Decimal?, note, import_batch_id?)`. `date` is nullable only for imported legacy lots (flagged "undated").
- `PricePoint(instrument_id, date, price Decimal, source[provider|manual|transaction])`; unique `(instrument_id, date, source)`.
- `FxRate(base_currency, quote_currency, date, rate Decimal, source)`.
- `PriceSourceConfig(workspace_id, provider_key, enabled, settings JSON encrypted)`.
- `BondTerms(instrument_id, series_code?, maturity_date, interest_mode[capitalized_annually|paid_out_annually|at_maturity_simple], rate_schedule JSON[{from_date, annual_rate_bp}], tax_rate_bp, early_redemption_fee PercentOrFixed?, face_value Money)`.

---

## F6.1 Asset classes and instruments

**Acceptance**
- CRUD for asset classes and instruments; allocation editor with validation that weights sum to 100% (live total, blocks save otherwise).
- Instrument types and their defaults: `crypto` (unit "coin"), `metal` (unit "oz"), `currency` (price = FX rate to base; unit = currency code), `real_estate` (quantity 1, manual valuation), `bond_model` (valuation source `bond_model`, requires `BondTerms` — F6.5).
- Archive instruments with history instead of deleting.
- Editors/owners only for writes.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F6.1 from docs/specs/M6-investments.md: AssetClass, Instrument, InstrumentAllocation models + migration, scoped repositories/services,
CRUD UI with an allocation editor (weights in basis points summing to 10000, live validation), seed asset classes (en/pl).
Tests: allocation validation, archive rules, tenant isolation test passes.
```

---

## F6.2 Transactions and holdings

**Story:** I record each purchase (and sale) and always know what I hold and what it cost.

**Acceptance**
- Transaction form (buy/sell): date, instrument, quantity, unit price (instrument currency), fee, optional FX-to-base override (defaults to stored FX for that date, or 1 for base currency), note. Inline editing in a per-instrument history table and a global history page with filters.
- **Pure domain function** `compute_holdings(transactions, method, as_of) -> list[Holding]` with `Holding(quantity, cost_basis_base, avg_cost, realized_pnl)`. Cost basis methods: `average` and `fifo` (workspace setting; default `average`). Sells reduce quantity and realize P&L; selling more than held is rejected.
- Total cost of a buy = `(quantity × unit_price + fee) × fx_to_base`, rounded half-up to base minor units once per transaction.
- Holdings recomputed on read (no stored quantities); aggregation in Python.
- Tests: golden scenario — buy 46 @ 489.5409 + fee 65.30, buy 4 @ 496.3093 + fee 14.00 → quantity 50; cost basis equals the sum of totals; sell 10 → average cost unchanged, FIFO consumes the first lot; fractional quantity `0.00924`; property test: quantity never negative, cost basis ≥ 0, realized + remaining cost basis = total bought cost − proceeds identity for both methods.
- Undated legacy lots (date null) are allowed only via import and shown with a warning badge; they count in holdings but are excluded from time series until dated.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F6.2 from docs/specs/M6-investments.md: InvestmentTransaction model + migration, transaction UI (per instrument + global history),
pure compute_holdings() with average-cost and FIFO methods, realized P&L, rejection of oversell. Use Decimal/DecimalType and Money only.
Include the golden scenario and the property tests from the spec; independent oracle: a naive reference implementation inside the test module.
```

---

## F6.3 Prices, FX and provider interface

**Acceptance**
- **Step 0 (ADR, before code):** `docs/adr/00xx-price-providers.md` evaluating candidates for (a) FX (e.g., the NBP public API), (b) equities/ETFs (e.g., Stooq CSV, Yahoo via `yfinance`), (c) crypto (e.g., CoinGecko), (d) precious metals. For each: terms of service, rate limits, ticker coverage for UCITS ETFs on common exchanges, stability/fragility, licensing of the Python client. **Do not scrape sites whose terms forbid it.** Recommend defaults and which stay optional. I review the ADR before the implementation starts.
- `PriceProvider` interface: `supports(instrument) -> bool`, `fetch_latest(symbols) -> dict`, `fetch_history(symbol, start, end) -> list[PricePoint]`, `rate_limit` metadata. A `ManualProvider` (user-entered price points) and a `FakeProvider` for tests ship in this feature.
- Manual price entry UI: add/edit/delete price points per instrument (essential for real estate and illiquid assets); table + small chart.
- `PriceService.price_at(instrument, date)` = latest point ≤ date (carry forward); `FxService.rate(from, to, date)` likewise; both return a flag when the value is stale (older than `stale_after_days`, default 5) or missing.
- Price points originating from transactions (`source=transaction`) are created implicitly so an instrument always has at least its purchase price.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Step 0: write the provider-evaluation ADR described in F6.3 of docs/specs/M6-investments.md and STOP for my review (check ToS/limits/coverage; no scraping against ToS).
After I approve: implement the PriceProvider interface, ManualProvider, FakeProvider, PricePoint and FxRate models + migration, PriceService/FxService with carry-forward
and staleness flags, manual price entry UI. Tests: carry-forward, staleness flag, missing data, FX triangulation through base currency.
```

---

## F6.4 Provider implementations and scheduled refresh

**Acceptance**
- Implement the providers approved in the ADR, each behind the interface, with timeouts, retries with backoff, response caching, and a global per-provider rate limiter. HTTP is mocked in tests (recorded fixtures); no network in CI.
- Provider settings page (enable/disable, API keys encrypted at rest).
- Scheduled job (daily, after market close, per provider) refreshes latest prices and FX for instruments held (quantity > 0); "Refresh now" button; first-time **history backfill** from the first transaction date.
- Failures keep the last known price, are logged in a `provider_error` table/log, and surface as a badge on the instrument ("last updated 9 days ago — provider error").
- Instruments choose provider + provider symbol in the instrument form; a "test lookup" button shows the fetched price before saving.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F6.4 from docs/specs/M6-investments.md: the provider implementations approved in the ADR with timeouts/retries/rate limiting/caching,
PriceSourceConfig model + migration with encrypted settings, scheduled refresh + history backfill, refresh-now, error surfacing, test-lookup button.
All HTTP via an injectable client; tests use recorded fixtures only.
```

---

## F6.5 Bond model valuation

**Story:** My retail treasury bonds are valued by their terms, net of tax, instead of a market price.

**Acceptance**
- `BondTerms` form per `bond_model` instrument: series code, face value per unit, maturity, interest mode, **rate schedule** (annual periods with rates; future unknown rates default to the last known rate), tax rate (user-entered, default blank → 0), optional early-redemption fee (PercentOrFixed per unit).
- Pure function `bond_value(terms, lots, as_of) -> BondValuation(gross_accrued, tax, fee, net, projected_net_at_maturity)`; interest accrues linearly by day within the current period; `capitalized_annually` compounds at period ends; `paid_out_annually` does not add to value; `at_maturity_simple` accrues simple interest.
- Golden test reproducing the spreadsheet's approach: per 100 face, 10 years, 4.35% annual compounding, tax 19% on gain → `fv − (fv − 100) × 0.19` where `fv = 100 × 1.0435^10`; the function's `projected_net_at_maturity` must equal this value to the minor unit.
- Instrument valuation for `bond_model` uses the net accrued value × quantity; the UI labels it "model value (net of tax)".
- Disclaimer: values are estimates from user-entered terms.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F6.5 from docs/specs/M6-investments.md: BondTerms model + migration, form with rate schedule, pure bond_value() with the three interest modes,
integration into instrument valuation, labels/disclaimer. Include the golden test from the spec (oracle = the closed-form formula written in the test)
and property tests (value monotonic in time for non-negative rates; tax never negative).
```

---

## F6.6 Portfolio overview and allocation

**Acceptance**
- Portfolio page: table of holdings — instrument, quantity, avg cost, price (+ date and stale badge), value (base currency), cost basis, unrealized gain (absolute and %), share of portfolio.
- Totals: portfolio value, cost basis, unrealized gain, realized P&L (YTD / all time).
- **By asset class (look-through):** value and cost per class using `InstrumentAllocation` weights; per-class gain %.
- **Pie/donut chart of stock values:** proportion of each instrument's value; a selector chooses the scope: one asset class (default Stocks) or the whole portfolio; small slices (<2%) group into "Other".
- Foreign-currency holdings are converted using FX at the valuation date; missing FX shows a visible warning and excludes the instrument from totals (never silently zero).
- Pure domain function `value_portfolio(holdings, prices, fx, allocations, as_of)`; golden test: an 80/20 stock/bond fund of value 1 000.00 contributes 800.00 to Stocks and 200.00 to Bonds, with rounding remainder assigned to the largest class so totals match exactly.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F6.6 from docs/specs/M6-investments.md: pure value_portfolio() with allocation look-through and exact-total rounding,
the portfolio page (holdings table, totals, by-class table, donut chart with scope selector), warnings for missing FX/prices.
Property tests: class totals sum exactly to the portfolio total; allocation weights respected. Charts via vendored ECharts.
```

---

## F6.7 Performance over time and XIRR

**Acceptance**
- `portfolio_series(transactions, prices, fx, start, end, step[day|week|month]) -> list[Point(date, value, net_invested)]`: quantity at each date from transactions ≤ date; price = latest ≤ date (carry forward); FX likewise. `net_invested` = cumulative buys + fees − sell proceeds in base currency.
- Chart: value line + invested-capital line (area between = gain), range selector (YTD/1y/3y/5y/all), per-class filter.
- Table of period changes (1M, 3M, YTD, 1Y, all-time): absolute and % change of value, and **contribution-adjusted** return via XIRR.
- Pure `xirr(cashflows)` (Newton with bracketing/bisection fallback; actual/365 day count). Tests: 1 000 invested on 2020-01-01 and 1 100 received on 2021-01-01 gives ≈ 9.97% (366-day year); additional vectors cross-checked against `scipy.optimize.brentq` as a **dev-only** oracle; no-solution cases return a clear "undefined" result.
- Performance requirement: 5 years × 30 instruments × weekly points computed in < 500 ms (benchmark test with generous CI margin).

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F6.7 from docs/specs/M6-investments.md: pure portfolio_series() (sorted arrays + bisect for carry-forward) and xirr(),
the performance chart (value vs invested capital), period-change table with XIRR, per-class filter.
Tests: golden XIRR vector from the spec, scipy cross-check (dev dependency only), series correctness on a hand-built scenario, performance benchmark test.
```

---

## F6.8 Net-worth links

**Acceptance**
- Register resolvers for `portfolio_asset_class` (class value, look-through, base currency) and `portfolio_holding` (instrument value) with the M5 `ValuationResolver` registry; both support historical `as_of` (used by snapshots).
- Helper button "Create net-worth items from asset classes" creates linked asset items (one per class with value > 0), skipping classes already linked; classes with `investable=false` are created with `investable=false`.
- Warning when both a class and one of its holdings are linked (double counting).
- Net-worth snapshots (M5.3) pick up linked values automatically.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F6.8 from docs/specs/M6-investments.md: ValuationResolver implementations for portfolio_asset_class and portfolio_holding (with as_of support),
the create-linked-items helper, double-counting warning. Tests: resolver values match value_portfolio(), historical as_of, snapshot integration.
```

---

## F6.9 Archive extension: investments

Replaces the former workbook "importer extension" (M4 no longer has a spreadsheet importer; owner
decision, 2026-10-18).

**Acceptance**
- New archive files (a minor format version per ADR-0016) with lineage `id` columns and
  references by id plus name as in M4: `asset_classes.csv`, `instruments.csv`,
  `instrument_allocations.csv`, `investment_transactions.csv`, `price_points.csv` (manual and
  transaction prices; provider prices optional, decide in the plan), `bond_terms.csv`,
  `price_source_configs.csv` without secrets (provider keys follow F9.3).
- Quantities and prices are plain decimals written from `Decimal` without loss (never through
  `float`); FX rates likewise.
- Standalone import in merge mode: `investment_transactions.csv` (the "bring my purchase
  history" path: instrument by name, with the mapping step for unknown instruments like F4.3's
  categories, create with a type the user picks; an empty `date` makes an "undated lot" flagged
  for the user to fix). Other files are restore only.
- The new models join `EMPTINESS_CHECKS`; the M4 round-trip test covers them.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F6.9 from docs/specs/M6-investments.md as an extension of the M4 archive (docs/specs/M4-ledger-export-import.md,
ADR-0016): the new files with lineage ids and lossless decimals, export, restore order, standalone
investment_transactions.csv import with instrument mapping and undated lots, emptiness checks, extended round-trip test.
```

---

## F6.11 Risk tiers and target allocation

Owner decision (2026-10): investments are grouped into three risk tiers so users can see whether the portfolio matches their chosen risk mix.

| Tier | Name (en / pl) | Typical content |
|---|---|---|
| `safe` | Safe / Bezpieczne | bonds, gold and other metals, deposits |
| `growth` | Growth / Wzrostowe | broad ETFs, currencies, funds |
| `speculative` | Speculative / Spekulacyjne (subtitle "Play money" / "Na zabawę") | crypto, single-company stocks |

**Acceptance**
- Each asset class has an optional `risk_tier`; an instrument can override it (`risk_tier_override`), e.g. an S&P 500 ETF in the Stocks class stays `growth` while a single company's shares are set to `speculative`. Value flows to tiers through the existing `InstrumentAllocation` look-through (an 80/20 stock/bond fund contributes 80% to the Stocks class's tier and 20% to the Bonds class's tier, unless the instrument has an override, which then takes all of its value). Classes without a tier (non-investable, Other) are shown as "Unassigned" and excluded from the tier percentages.
- Target policy per workspace (`RiskTargetPolicy`), editors/owners only:
  - `fixed` (**default**): user-entered targets, default **50 / 40 / 10** (safe / growth / speculative); must sum to 100%.
  - `age_scaled` (owner's rule): speculative = fixed X% (default 10%); safe = (100% − X) × age ÷ life expectancy; growth = the rest. Example: X = 10%, age 40, life expectancy 80 → 45 / 45 / 10. At or beyond life expectancy safe is capped at 100% − X.
  - `age_in_bonds` (classic preset): safe = age%, speculative = fixed X% (default 0%, editable), growth = the rest; safe capped at 100% − X.
  - Age and life expectancy come from the **current user's profile**: `birth_year` (optional) and `life_expectancy` (default **90**, range 50–120). Age-based modes are disabled with an explanation until the user sets a birth year. Age = current year (from the injected Clock) − birth year. Both profile fields are added here if M8 has not added them yet; M8's `horizon_age` defaults to the profile's `life_expectancy`.
  - Percentages are stored as basis points; age-based targets are computed (not stored) and rounded half-up to whole basis points once, with the rounding remainder put on `growth` so targets always total exactly 10 000.
- Portfolio page section "Risk balance": per tier the current value, current %, target %, the difference in percentage points and in money (base currency), and a plain-language hint ("move about €X from Growth to Safe") for the largest gap. A tier is flagged when |current − target| > tolerance (default **5 percentage points**, editable). Tier names and the speculative subtitle are translatable; users can't rename tiers in this feature (follow-up).
- Pure domain functions: `risk_targets(policy, age, life_expectancy) -> dict[tier, bp]` and `risk_balance(class_values, tiers, overrides, targets, tolerance)`. Golden tests: the 45/45/10 example above; 50/40/10 fixed; age_in_bonds at age 35 with X = 10 → 35/55/10; capping at age ≥ life expectancy. Property tests: targets always sum to 10 000; current tier percentages sum to 10 000 when any tier has value; monotonic: an older age never lowers `safe`.
- Not investment advice: the section shows the same "illustrative, not advice" notice as the FIRE planner.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F6.11 from docs/specs/M6-investments.md: risk_tier on AssetClass (+ seeds), risk_tier_override on Instrument,
RiskTargetPolicy + migration, profile birth_year/life_expectancy if missing, pure risk_targets()/risk_balance() with golden and
property tests, the policy editor and the "Risk balance" section on the portfolio page (en/pl, tenant isolation, editors only for writes).
```

---

## F6.10 (stretch) Dividends, interest and splits

**Acceptance:** `dividend` transactions (amount, optional withholding), optionally creating a linked income `Transaction` in the ledger; `split` transactions adjust quantity and unit price history without changing cost basis; income-from-investments report per year.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F6.10 (stretch) from docs/specs/M6-investments.md: dividend and split transaction types in compute_holdings and the series,
optional ledger income creation, yearly investment-income report. Update the golden tests; keep existing behaviour unchanged.
```
