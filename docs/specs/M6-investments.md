# M6 — Investments

**Goal:** record purchase (and sale) history for stocks/ETFs, bonds, metals, crypto, currencies and real estate; see current value, gains, allocation and value over time.

**Exit criteria:** holdings are computed from transactions; prices come from pluggable providers with manual fallback; the portfolio page shows value, cost, gain, allocation by instrument and by asset class, a pie chart of stock values, and a value-over-time chart with an invested-capital line and XIRR; portfolio values feed net worth.

Every agent prompt starts with: *"Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go)."*

**Sequencing (owner decision, 2026-11-05):** F6.11 lands **before** F6.9, so the 1.4 archive covers the risk columns and `RiskTargetPolicy` in one format bump instead of F6.11 extending the archive in its own PR.

**Data model (sketch)**
- `AssetClass(id, workspace_id, name, investable bool, color, sort_order, archived_at, risk_tier[safe|growth|speculative]?)`. Seed (en/pl): Stocks, Bonds, Metals, Crypto, Currencies (all investable); Real estate, Primary residence (not investable); Other. Seeded tiers: Bonds, Metals → safe; Stocks, Currencies → growth; Crypto → speculative; non-investable classes and Other → none (F6.11).
- `Instrument(id, workspace_id, name, symbol?, type[equity|etf|bond_market|bond_model|metal|crypto|currency|real_estate|other], quote_currency, unit_label, valuation_source[provider|manual|bond_model], provider_key?, provider_symbol?, risk_tier_override[safe|growth|speculative]?, archived_at)`
- `RiskTargetPolicy(workspace_id PK, mode[fixed|age_scaled|age_in_bonds], safe_bp, growth_bp, speculative_bp, tolerance_bp)` (F6.11).
- `InstrumentAllocation(instrument_id, asset_class_id, weight_bp)`; every instrument has at least one row and its weights must sum to exactly 10 000, enforced by the service (the editor mirrors the total client-side). A single instrument can be split across classes (e.g., an 80/20 stock/bond fund).
- `InvestmentTransaction(id, workspace_id, instrument_id, date?, type[buy|sell|dividend|fee|split], quantity Decimal, unit_price Decimal, quote_currency, fee_minor, fx_to_base Decimal?, settlement_currency?, settlement_unit_price Decimal?, settlement_fee_minor?, note, deleted_at?, import_batch_id?)` (settlement columns: F6.2a). `date` is nullable only for imported legacy lots (flagged "undated"); fees are in `quote_currency`.
- Workspace setting `investment_cost_basis[average|fifo]` (F6.2; default `average`, owner-only, audited through `workspace.settings_updated`).
- `PricePoint(instrument_id, date, price Decimal, source[provider|manual|transaction])`; unique `(instrument_id, date, source)`.
- `FxRate(base_currency, quote_currency, date, rate Decimal, source)`.
- `PriceSourceConfig(workspace_id, provider_key, enabled, settings JSON encrypted)`.
- `BondTerms(instrument_id, series_code?, maturity_date, interest_mode[capitalized_annually|paid_out_annually|at_maturity_simple], rate_schedule JSON[{from_date, annual_rate_bp}], tax_rate_bp, early_redemption_fee PercentOrFixed?, face_value Money)`.

---

## F6.1 Asset classes and instruments

**Acceptance**
- CRUD for asset classes and instruments; allocation editor with validation that weights sum to 100% (live total, blocks save otherwise).
- Instrument types and their defaults: `crypto` (unit "coin"), `metal` (unit "oz"), `currency` (`quote_currency` is the held currency and the unit is its code, forced even if another label is typed; the unit price is always **1** in that currency, so F6.3 fetches only the FX rate to the base currency, never a market price — cash in the base currency is price 1 at rate 1), `real_estate` (quantity 1, manual valuation), `bond_model` (valuation source `bond_model`, requires `BondTerms` — F6.5). A `bond_model` instrument may be created in F6.1 with a "terms needed" state; the form and valuation arrive in F6.5.
- Archive **asset classes and instruments** with their history instead of deleting; names are unique per workspace among active rows (case-insensitive), and asset classes can be reordered like F5.1 categories.
- Instrument allocations: at least one class per instrument, weights in basis points totalling exactly 10 000; the service enforces the cross-row rule and the page mirrors the total. Archived classes may stay on an instrument that already used them, but cannot be added to a new allocation.
- Editors/owners only for writes. New workspaces get the suggested asset classes; an older workspace with no active class gets an explicit "Add suggested asset classes" action (no automatic migration).

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
- Transaction form (buy/sell): date, instrument, quantity, unit price (instrument currency), fee, optional FX-to-base override, note. The form is usable on the global history page and on an instrument's page. Inline editing in a per-instrument history table and a global history page with filters.
- A currency instrument's unit price is always **1**; the service ignores a submitted value. Its value in base comes from FX, as recorded in F6.1.
- **FX before F6.3:** when the instrument's `quote_currency` equals the workspace base currency, `fx_to_base` is stored as `1` and the field is not shown. Otherwise the rate is required and must be `> 0`; F6.3 will pre-fill it from stored FX, at which point the spec's “defaults to stored FX” applies. The resolved rate is stored so historical FX is frozen.
- **Pure domain function** `compute_holdings(transactions, method, as_of) -> list[Holding]` with one `Holding(instrument_id, quantity, cost_basis_base, avg_cost, realized_pnl)` per instrument, grouped by instrument. Cost basis methods: `average` and `fifo` (workspace setting; default `average`). `avg_cost` is base currency per unit. Sells reduce quantity and realize P&L; selling more than held is rejected.
- **Oversell is checked across the whole ordered history on every write** — create, update, delete and undo. Inside the write transaction (workspace lock and role re-check) the service rebuilds that instrument's ordered transactions as they would be after the change, runs `compute_holdings`, and refuses with `oversell` naming the date of the first failing sell. This covers editing an earlier buy, back-dating a sell, deleting a buy and restoring a deleted sell.
- Total cost of a buy = `(quantity × unit_price + fee) × fx_to_base`, rounded half-up to base minor units once per transaction. Sell proceeds = `(quantity × unit_price − fee) × fx_to_base`, also rounded once; a sell fee may exceed the gross, so proceeds and realized P&L can be negative.
- **Rounding without drift.** Average method: the cost removed by a sell is `round_half_up(remaining_cost × quantity_sold / remaining_quantity)` from exact values, never from a stored rounded average; selling the whole position removes exactly the remaining cost and returns 0 quantity and 0 cost. FIFO: a fully used lot removes its exact cost; only a partly used lot is rounded once. All Decimal arithmetic uses a local context with enough precision; never `float`.
- Holdings are recomputed on read (no stored quantities); aggregation in Python. The workspace cost-basis setting may be switched at any time, owner-only and audited through `workspace.settings_updated`; the UI notes that realized P&L and cost basis are recalculated for the whole history. Nothing derived is stored, so switching has no lasting effect.
- **Deletion** is a soft delete with the ledger's 10-second undo. Holdings ignore deleted rows; the base-currency lock and the instrument `quote_currency`/`type` lock still count deleted rows. Editing, deleting and undoing transactions of an archived instrument are refused (`instrument_archived`), as F5.1 does for archived items; history stays visible.
- **Dates:** a transaction date after today in the workspace time zone is refused; trades are records, not plans. Dates are in the 1900–2199 range.
- Undated legacy lots (date null) are allowed only via import and shown with a warning badge; they count in current holdings (`as_of=None`) but are excluded from any dated `as_of` and from time series until dated. F6.2 cannot create them; editing one may add a date.
- Tests: golden scenario — buy 46 @ 489.5409 + fee 65.30, buy 4 @ 496.3093 + fee 14.00 → quantity 50; cost basis equals the sum of totals; sell 10 → average cost unchanged, FIFO consumes the first lot; fractional quantity `0.00924`; property tests: quantity never negative, cost basis ≥ 0, selling everything returns cost basis exactly 0 for both methods, and the accounting identity `realized + cost_removed = proceeds` with `cost_removed = total bought cost − remaining cost basis` for both methods.
- Investment transactions store `fx_to_base`, so historical FX is frozen. Once any investment transaction exists, the workspace base currency is locked (add the check to `services.workspaces.BASE_CURRENCY_LOCKS`). Deleted rows count.
- `quote_currency` and `type` are **locked once the instrument has investment transactions**; changing either would reinterpret stored prices, fees and valuations. F6.2 adds both checks to the instrument service, like the net-worth item currency rule. The database's composite foreign key to `instruments(workspace_id, id, quote_currency)` is the final guard; the service surfaces `currency_locked`/`type_locked` first.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F6.2 from docs/specs/M6-investments.md: InvestmentTransaction model + migration, transaction UI (per instrument + global history),
pure compute_holdings() with average-cost and FIFO methods, realized P&L, rejection of oversell. Use Decimal/DecimalType and Money only.
Include the golden scenario and the property tests from the spec; independent oracle: a naive reference implementation inside the test module.
```

---

## F6.2a Paid in the base currency, and bulk add of transactions

**Story:** My broker shows me only PLN amounts for ETFs quoted in USD or EUR. I want to record
exactly what I paid, without converting by hand. I also want to paste many past trades at once,
the way I bulk-add net-worth assets.

Added 2026-11-08 (owner request). It lands before F6.7. It changes only how transactions are
entered and stored; valuation, prices and the portfolio page keep their F6.3–F6.6 rules.

### Part A — amounts in the base currency ("settlement")

**Why.** Before this change, users with PLN-only broker statements entered the PLN unit price as
if it were USD, with `fx_to_base = 1`. The cost basis came out right, but the data was wrong in
two ways that change valuation:
- F6.3 derives a **transaction price** from each live transaction, so the instrument looked
  priced at "1 600 USD".
- F6.3 also derives a **transaction FX rate** (the lowest-priority FX source), so the rows said
  USD→PLN = 1.

**Data model.** `InvestmentTransaction` gains three nullable columns. They are set **all or
none**, enforced by a check constraint:
- `settlement_currency CHAR(3)`
- `settlement_unit_price` (`DecimalType`)
- `settlement_fee_minor BIGINT`, with `>= 0` and `<= MAX_AMOUNT_MINOR`

Rules:
- In this version `settlement_currency` must equal the workspace base currency. The base is
  already locked once any investment transaction exists (F6.2).
- Settlement is allowed only when the instrument's `quote_currency` differs from the base, and
  never for `currency` instruments, whose unit price is always 1.
- The migration is reversible. Existing rows stay null and behave exactly as before.

**Form.** When the instrument's quote currency differs from the base, the transaction form
(global history page and instrument page) shows a choice: **Amounts in: USD | PLN**. The labels
are the instrument's quote currency and the base currency, translatable.
- **Quote mode** (default; the F6.2 behaviour, unchanged).
- **Base mode.** The user enters:
  - quantity;
  - **unit price in the base currency**;
  - **fee in the base currency**;
  - the FX rate (base units per quote unit, required, `> 0`).
- **FX rate.** The FX field is pre-filled from stored FX for the transaction date:
  - `FxService.rate(quote, base, date)` (F6.3/F6.4), any source except `transaction`.
  - A hint shows the rate's date and source.
  - When no stored rate exists the field stays empty and is required.
  - Re-filling for a changed date uses a small Alpine (CSP build) action that calls a read-only
    JSON endpoint. With JavaScript off, the server pre-fills on the first render, and when the
    rate is missing the form returns the inline error.

**What is stored in base mode** (exact `Decimal`, 80-digit `localcontext`):
- `settlement_currency` = base
- `settlement_unit_price` = the entered base unit price
- `settlement_fee_minor` = the entered base fee
- `fx_to_base` = the rate
- `unit_price` = `settlement_unit_price / fx_to_base`, quantised half-up to the `DecimalType`
  scale (18 places)
- `fee_minor` (quote currency) = `round_half_up(settlement_fee / fx_to_base)` to quote minor units

The derived quote values keep F6.3's transaction price and FX rate correct (USD price, real
rate), and they let the row be shown in the quote currency too.

**Cost basis.** This rule overrides the F6.2 formula when a settlement is present (the
settlement currency is the base). Each is rounded once per transaction:
- buy total cost = `round_half_up(quantity × settlement_unit_price + settlement_fee)`
- sell proceeds = `round_half_up(quantity × settlement_unit_price − settlement_fee)`

Notes:
- The broker's FX spread is part of the cost, which is correct, because this is what left the
  account.
- `HoldingTransaction` gains the optional settlement fields. `compute_holdings` and
  `realized_events` use them.
- Every other F6.2 rule is unchanged: average/FIFO, rounding without drift, oversell, undated
  lots.

**History and editing.**
- A base-mode row shows the base amounts it was entered with as the primary values, with the
  derived quote unit price and the rate beside them ("1 600.00 PLN · ≈ 398.774767 USD @ 4.0123").
- Editing opens in the mode the row was saved in.
- Switching modes on an existing row:
  - **quote → base:** pre-fills base price = `unit_price × fx_to_base` and base fee = `fee × fx`.
  - **base → quote:** pre-fills the stored derived quote values and clears the settlement.

**Finding rows that need fixing.** A row whose instrument quote currency differs from the base
and whose `fx_to_base == 1` gets a **"check FX rate"** warning badge in both histories. A
history filter "FX needs checking" lists them. To fix such a row, the user opens it, switches to
base mode (the amounts carry over, since they were entered as base), takes the pre-filled stored
rate, and saves.

**Audit and archive.**
- `investment_transaction.created`/`.updated` keep their shape. Field names that changed are
  listed; no amounts are logged at INFO.
- F6.9 must export and restore the three columns. Add them to its file list when F6.9 is
  implemented.

### Part B — bulk add of transactions

- An **"Bulk add"** button on the Transactions tab (global history) reveals a textarea, in the
  same style as F5.1's bulk add. Editors and owners only.
- **Limits:** at most 500 non-blank lines and 64 KiB. Lines starting with `#` are ignored.
- **Line format.** Fields are separated by `;`. Surrounding whitespace is trimmed, and empty
  trailing fields may be left out:
  `date; instrument; type; quantity; unit price; fee; fx; amounts currency`
  - `date`: ISO `YYYY-MM-DD`, or the user's locale short date. Today or earlier, 1900–2199. Bulk
    add cannot create undated lots; those stay import-only (F6.9).
  - `instrument`: an active instrument's name or symbol, matched case-insensitively. An
    ambiguous or unknown name is a line error. Bulk add never creates instruments.
  - `type`: `buy` or `sell`, with Polish aliases `kupno`/`zakup` and `sprzedaż`/`sprzedaz`.
  - `quantity`, `unit price`, `fee`: parsed with the locale number helpers. `fee` defaults to 0.
    Currency instruments ignore the price (always 1), as the form does.
  - `amounts currency`: blank or the instrument's quote currency means quote mode. The base
    currency code means **base mode** (Part A): `unit price` and `fee` are base amounts. Any
    other code is a line error.
  - `fx`: required when the quote currency differs from the base. When blank, it is pre-filled
    from stored FX for the date (same source as the form). If no stored rate exists, the line
    gets the error "FX rate needed". When the quote currency equals the base, `fx` must be
    blank or 1.
- **Preview.**
  - Every line is listed with its number, the parsed values (instrument, type, date, quantity,
    price, fee, FX used and its source, total cost or proceeds in the base currency) or its
    error.
  - Nothing is written while any line has an error.
  - The confirm step re-posts the same text. The server re-parses and re-validates it, and never
    trusts hidden preview fields.
- **Create.**
  - **All-or-nothing** in one `run_immediate` write: workspace lock, role re-check, then the
    whole-sequence oversell check (F6.2) per affected instrument, with the new rows merged into
    the existing history in date order.
  - Any failure, such as an oversell naming its line and date, or an archived instrument,
    refuses the whole batch.
  - The rows share one new `import_batch_id`. One audit event
    `investment_transaction.bulk_created` records the count and instrument ids, with no amounts.
  - The page shows a success toast with the count. There is no batch undo in v1; single rows keep
    their normal delete and undo.

### Tests

- **Settlement golden test.** Buy 4 @ 1 600.00 PLN + fee 5.00 PLN with FX 4.0123:
  - cost basis = 6 405.00 PLN exactly;
  - `unit_price` = 398.774767… (18 places, half-up);
  - quote `fee_minor` = round(5.00 / 4.0123).
- **Sells.** A partial sell under average and FIFO; selling everything returns cost basis 0.
  Mixed quote-mode and base-mode rows on one instrument.
- **Property test.** For base-mode rows, the cost basis equals the sum of the rounded settlement
  totals, independent of FX.
- **Derived values.** The F6.3 transaction price and FX derived from a base-mode row are the
  derived quote price and the entered rate, never 1.
- **Validation.**
  - All-or-none constraint; a settlement on a base-quoted or `currency` instrument is refused.
  - FX pre-fill: from a stored rate; no stored rate gives an inline error; the endpoint is
    workspace-scoped.
  - Mode switch on edit; the "check FX rate" badge and its filter.
  - The migration is reversible.
- **Bulk add.**
  - Parsing table: separators, locales, types incl. Polish aliases, comments, missing optional
    fields, base-mode lines, FX pre-fill.
  - Limits; ambiguous and unknown instruments.
  - All-or-nothing on any error and on oversell, including an oversell caused only by the
    combination of pasted rows.
  - Re-validation on confirm.
  - Role and CSRF; one audit event; the batch id.
- **i18n.** The pl catalog is updated with no fuzzy entries, and `make i18n-update` leaves no
  diff.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F6.2a from docs/specs/M6-investments.md: settlement columns + migration, base-currency entry mode with stored-FX
pre-fill, the settlement cost-basis rule in compute_holdings/realized_events, the "check FX rate" badge and filter,
and the all-or-nothing bulk add with preview and re-validation. Include the settlement golden test and the property test.
```

---

## F6.3 Prices, FX and provider interface

**Acceptance**
- **Step 0 (ADR, before code): done — [ADR-0024](../adr/0024-price-providers.md), owner decisions of 2026-11-04.** Built-in defaults, still opt-in per workspace: **NBP** (FX vs PLN + official gold in PLN via `cenyzlota`), **ECB** (FX vs EUR) and **CoinGecko** (crypto, quoted directly in USD/EUR/PLN). Optional: **EODHD** (the only acceptable EU equities/ETFs path; keyed). **GoldAPI.io is implemented in the F6.5 follow-up; Kraken and Frankfurter are approved but deferred** to a later follow-up. Equities/ETFs are **manual-first**: no clean free EU provider exists in 2026 (Stooq's CSV API is removed or anti-bot-gated; Yahoo's terms forbid automated collection). **Do not scrape sites whose terms forbid it** — stands. Claims not verified during the evaluation are marked in the ADR's register and must be re-checked at implementation time.
- `PriceProvider` interface: `supports(instrument) -> bool`, `fetch_latest(symbols) -> dict`, `fetch_history(symbol, start, end) -> list[PricePoint]`, `rate_limit` metadata. A `ManualProvider` (user-entered price points) and a `FakeProvider` for tests ship in this feature.
- Manual price entry UI: add/edit/delete price points per instrument (essential for real estate and illiquid assets); table + small chart. Saving a manual price on an existing date updates that date's manual point; manual prices dated after today and writes to archived instruments are refused.
- `PriceService.price_at(instrument, date)` = latest point ≤ date (carry forward); `FxService.rate(from, to, date)` likewise; both return a flag when the value is stale (older than the constant `DEFAULT_STALE_AFTER_DAYS`, 5) or missing. Staleness is measured against the requested date, not today. On one date the priority is manual > provider > transaction; a newer date always wins.
- Prices and FX rates originating from transactions (`source=transaction`) are **derived at read time** from live investment transactions, never stored, so an instrument always has at least its purchase price and a deleted transaction stops contributing. Stored `price_points` rows are only `manual` or `provider`; stored `fx_rates` use the provider key as `source`. `FxRate.rate` is base-currency units per one quote-currency unit, with the workspace base currency as 1; `rate(from, to) = rate(base, from) / rate(base, to)`. A transaction's `fx_to_base` is the lowest-priority FX source. There is no FX entry UI in F6.3.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F6.3 from docs/specs/M6-investments.md (the provider set is decided in ADR-0024): the PriceProvider interface,
ManualProvider, FakeProvider, PricePoint and FxRate models + migration, PriceService/FxService with carry-forward
and staleness flags, manual price entry UI. Tests: carry-forward, staleness flag, missing data, FX triangulation through base currency.
```

---

## F6.4 Provider implementations and scheduled refresh

**Acceptance**
- Implement the providers approved in [ADR-0024](../adr/0024-price-providers.md): **NBP, ECB and CoinGecko** (the built-in defaults, still opt-in per workspace) and **EODHD** (optional, keyed). **GoldAPI.io** is implemented in the F6.5 follow-up; **Kraken and Frankfurter are approved but deferred**. Each sits behind the interface with timeouts, retries with backoff, response caching, and a global per-provider rate limiter (NBP self-limited ≤1 req/s, ECB fetched ≤1/hour, CoinGecko stored data refreshed ≥ every 24 h). HTTP is mocked in tests (recorded fixtures); no network in CI.
- Provider settings page (enable/disable, API keys encrypted at rest). The attribution UI from ADR-0024 lands with this page: ECB-sourced rates cite "European Central Bank" wherever they are shown, and a visible "Powered by CoinGecko" badge appears while that provider is enabled (translatable en/pl; the CoinGecko message keeps its exact required wording).
- Provider settings are per workspace and owner-only; editors may assign an enabled price provider and use test lookup. NBP publishes PLN-based FX and gold (gold is PLN/g and converted to the instrument unit); ECB publishes EUR-based FX. Rates are converted into the workspace base currency when the provider publishes that base, and skipped with a visible `base_not_published` warning otherwise. The conversion runs in an 80-digit `localcontext`; a direct conversion and the same conversion through the provider's base must agree to Decimal precision.
- NBP and ECB run daily at 18:10 and 18:20 UTC; CoinGecko at 00:10 UTC and EODHD at 22:40 UTC in the follow-up. The daily job refreshes latest values and backfills history from the first transaction date; enabling a provider also queues a one-off backfill. "Refresh now" is latest-only and never waits behind a long rate limit in a web request; scheduled/background work may wait. Cache TTLs: NBP latest 6 h/history 24 h, ECB 1 h (one full history XML serves both latest and history), CoinGecko latest 5 min/history 24 h, EODHD latest 15 min/history 24 h.
- Failed provider calls keep the last known price, are recorded in `provider_errors` (explicitly pruned after 90 days) and surface on the instrument. Provider upserts are short `run_immediate` transactions under the workspace lock; a unique clash on `(instrument, date, source)` is retried as an update. A workspace with no provider enabled makes no outbound calls.
- EODHD has a per-workspace daily call budget (default 20) counted per UTC day; latest prices are fetched first, then history uses the remainder, and the rest is skipped with `quota_exhausted` while last-known prices remain. CoinGecko history is requested as far back as the public/demo API allows (the current Basic plan is limited to the past two years per its documentation); when it cannot cover the first transaction date, the instrument records `history_limited` and the one-off backfill marker prevents daily retries. EODHD prices use adjusted closes; GPW/XETRA/LSE coverage stays marked unverified until checked with a real key.
- Scheduled job (daily, after market close, per provider) refreshes latest prices and FX for instruments held (quantity > 0); "Refresh now" button; first-time **history backfill** from the first transaction date.
- Failures keep the last known price, are logged in a `provider_error` table/log, and surface as a badge on the instrument ("last updated 9 days ago — provider error").
- Instruments choose provider + provider symbol in the instrument form; a "test lookup" button shows the fetched price before saving.
- **GoldAPI.io follow-up (implemented after F6.4):** optional keyed `XAU/XAG/XPT/XPD` spot prices in the documented currencies (PLN, EUR, USD, GBP and CHF included; BTC and XAG are not instrument currencies and are excluded), quoted per troy ounce and converted to the instrument unit (`oz`, `g`, `kg`; exact Decimal in an 80-digit context). Latest `GET https://www.goldapi.io/api/{metal}/{currency}`; the Unix `timestamp` gives the UTC date, capped at the workspace's today. Key required, encrypted at rest and sent only in `x-access-token` (never in URLs, logs, errors, audit data or HTML), 1 req/s, latest 15 min/history 24 h cache, 22:50 UTC schedule, default 4 calls/day (owner-adjustable) under the same per-workspace UTC-day budget as EODHD: latest first, history from the remainder, test lookups reserve one call, `quota_exhausted` is informational. The history endpoint allows at most 90 days per request; this integration backfills the latest 90-day window once (one request per instrument) and records `history_limited` when the first transaction is older. A permanent history failure (HTTP 400/404/422, unusable payload) also records `history_limited` and sets the backfill marker; key failures (401/403) stay visible errors and transient ones (network, 5xx, rate limits) retry. Instruments sharing a symbol and currency share one call. Terms: display in the app is allowed, resale or a competing API is not, and no attribution is required; the free quota is about 150 requests/month.

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
- `BondTerms` form per `bond_model` instrument: series code, face value per unit, maturity, interest mode, **rate schedule** (annual periods with rates; future unknown rates default to the last known rate), tax rate (user-entered, default blank → 0), optional early-redemption fee (PercentOrFixed per unit). One editable row per instrument. The face value and a fixed fee are in the instrument's `quote_currency`; no currency is stored on the terms, so changing the currency before transactions exist simply re-labels them.
- The rate schedule is non-empty with strictly increasing, exact anniversaries of the first `from_date` (29 February falls on 28 February in common years); rates are `0..100000` bp. Maturity must be after the first date and need not be an anniversary, so the last period may be partial (for example a 3-month OTS ending at maturity). The schedule is JSON, validated with Pydantic in the service.
- Pure function `bond_value(terms, lots, as_of) -> BondValuation(gross_accrued, tax, fee, net, projected_net_at_maturity)` returns totals across the supplied lots. Each output line is rounded half-up once to the currency's minor unit, so the displayed lines may differ by one minor unit; compounding uses an 80-digit `localcontext` and an integer exponent, and the partial period is linear.
- Valuation rules: `net = face + interest − fee − tax`, with `0 ≤ fee ≤ interest`; `tax = max(0, interest − fee) × tax_rate`; the early-redemption fee is capped at accrued interest so net never falls below face. Current net is a redeem-now value and includes the fee; `projected_net_at_maturity` excludes it. At or after maturity accrual freezes, there is no fee, and `net == projected_net_at_maturity`.
- Interest accrues linearly by elapsed day within each period using actual/actual from `max(purchase date, first from_date)`; a partial final period is prorated over the full annual period. `capitalized_annually` compounds at period ends; `paid_out_annually` keeps face plus the current period's accrued interest and resets at each period end (paid coupons are not part of the value; users record them as income in the ledger); `at_maturity_simple` sums each period's rate × elapsed fraction with no compounding. Undated legacy lots are excluded from a dated `as_of`, as in F6.2; in a current (undated) valuation they accrue from the schedule's first `from_date`.
- Lots are consumed FIFO for interest entitlement regardless of the workspace's cost-basis setting; that setting is about cost, not interest.
- Missing terms make `price_at`/`latest_prices` fall back to the transaction-derived price and return `terms_missing` alongside `stale`; once terms exist the model value is the only source (manual prices are refused, and provider assignment is already refused for `bond_model`).
- Golden test reproducing the spreadsheet's approach: per 100 face, 10 years, 4.35% annual compounding, tax 19% on gain → `fv − (fv − 100) × 0.19` where `fv = 100 × 1.0435^10`; the function's `projected_net_at_maturity` must equal this value to the minor unit, and the test checks `fv` itself as a Decimal to 18 places.
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
- **As built (owner review of the plan, 2026-11):**
  - A missing **price** is handled like missing FX. In both cases the instrument's value **and its cost basis** are left out of every total (portfolio, classes, shares, chart), so the totals and gain % stay consistent. The row stays with a warning, and a note counts the excluded holdings. A stale price or FX rate is valued, with a badge.
  - Look-through rounding: every class except the largest weight gets `floor(amount × weight / 10 000)`, and the largest gets the remainder. Equal largest weights are broken by the class's sort order, then its id. Value and cost are split separately.
  - Realized P&L: `domain.holdings.realized_events` lists each sell's realized P&L from the same ordered pass as `compute_holdings`. All time is their sum; YTD sums the events dated from 1 January of the workspace's current year. YTD is not computed by subtracting a dated holdings run, which excludes undated legacy lots.
  - Chart:
    - With a class scope, each instrument's slice is its look-through part in that class. With the whole-portfolio scope, it is the full value.
    - Small slices use the shared donut slicer (`category_slices`, now with a minimum-share option).
    - Slice colours come from the categorical palette by position. Class colours stay on the class table.
  - Default scope: the active class whose name key matches the seed name of Stocks in any supported language (en "Stocks", pl "Akcje"). If there is none, or nothing is held in it, the whole portfolio. An unknown scope falls back to that default.
  - The domain function takes the base currency as a keyword: `value_portfolio(holdings, prices, fx, allocations, as_of, *, base_currency)`.

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
decision, 2026-10-18). It lands after F6.11 (see the sequencing note at the top).

**Acceptance**
- New archive files (a minor format version per ADR-0016) with lineage `id` columns and
  references by id plus name as in M4: `asset_classes.csv`, `instruments.csv`,
  `instrument_allocations.csv`, `investment_transactions.csv`, `price_points.csv` (manual and
  provider prices), `fx_rates.csv` (stored manual and provider FX rates), `bond_terms.csv`,
  `price_source_configs.csv` without secrets (provider keys follow F9.3). Manual prices and
  stored FX rates join the archive here; transaction-derived prices and rates are recomputed
  from the archived transactions and are never exported.
- Quantities and prices are plain decimals written from `Decimal` without loss (never through
  `float`); FX rates likewise.
- Standalone import in merge mode: `investment_transactions.csv` (the "bring my purchase
  history" path: instrument by name, with the mapping step for unknown instruments like F4.3's
  categories, create with a type the user picks; an empty `date` makes an "undated lot" flagged
  for the user to fix). Other files are restore only.
- The new models join `EMPTINESS_CHECKS`; the M4 round-trip test covers them. An untouched suggested asset-class set counts as empty for restore, like the ledger and net-worth suggested sets (the exact set in a supported locale, unedited, unused, with the automatic `asset_class.seeded` event); otherwise every fresh workspace would refuse a restore.

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
