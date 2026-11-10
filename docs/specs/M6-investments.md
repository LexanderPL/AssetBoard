# M6 — Investments

**Goal:** record purchase (and sale) history for stocks/ETFs, bonds, metals, crypto and currencies (property lives in net worth, F5.7); see current value, gains, allocation and value over time.

**Exit criteria:** holdings are computed from transactions; prices come from pluggable providers with manual fallback; the portfolio page shows value, cost, gain, allocation by instrument and by asset class, a pie chart of stock values, and a value-over-time chart with an invested-capital line and XIRR; portfolio values feed net worth.

Every agent prompt starts with: *"Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go)."*

**Sequencing (owner decision, 2026-11-05):** F6.11 lands **before** F6.9, so the 1.4 archive covers the risk columns and `RiskTargetPolicy` in one format bump instead of F6.11 extending the archive in its own PR.

**Data model (sketch)**
- `AssetClass(id, workspace_id, name, investable bool, color, sort_order, archived_at, risk_tier[safe|growth|speculative]?)`. Seed (en/pl): Stocks, Bonds, Metals, Crypto, Currencies, Other (all investable). **Owner decision 2026-11-09:** property is tracked in net worth (F5.7), not as an investment, so Real estate and Primary residence are no longer seeded; workspaces seeded before keep them as user data. `investable` only decides what FIRE (M8) counts as the starting portfolio (e.g. an emergency fund or money earmarked for a purchase); it never hides anything in Investments. Seeded tiers: Bonds, Metals → safe; Stocks, Currencies → growth; Crypto → speculative; Other and non-investable classes → none (F6.11).
- `Instrument(id, workspace_id, name, symbol?, type[equity|etf|bond_market|bond_model|metal|crypto|currency|real_estate|other], quote_currency, unit_label, valuation_source[provider|manual|bond_model], provider_key?, provider_symbol?, risk_tier_override[safe|growth|speculative]?, archived_at)`
- `RiskTargetPolicy(id PK, workspace_id unique, mode[fixed|age_scaled|age_in_bonds], safe_bp, growth_bp, speculative_bp, tolerance_bp)` (F6.11): one row per workspace, following the repository/tenant-isolation conventions rather than a `workspace_id` primary key.
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
- The add form sits behind an **Add instrument** button and opens in a modal dialog (without JavaScript the button is a link to `?form=instrument`, which renders the dialog open in the page flow). A refused save reopens the dialog with the values; a successful save closes it. The form's swap only replaces `#investments-body` (no forced scroll).

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
- Transaction form (buy/sell): date, instrument, quantity, unit price (instrument currency), fee, optional FX-to-base override, note. The form is usable on the global history page and on an instrument's page. Inline editing in a per-instrument history table and a global history page with filters. On the global Transactions tab the add form and the bulk add sit behind buttons that open modal dialogs; a refused form reopens its dialog, and a successful save closes it. Neither opener forces a scroll.
- A currency instrument's unit price is always **1**; the service ignores a submitted value. Its value in base comes from FX, as recorded in F6.1.
- **FX before F6.3:** when the instrument's `quote_currency` equals the workspace base currency, `fx_to_base` is stored as `1` and the field is not shown. Otherwise the rate is required and must be `> 0`; F6.3 will pre-fill it from stored FX, at which point the spec's “defaults to stored FX” applies. The resolved rate is stored so historical FX is frozen. (Implemented: a new transaction's FX field starts with the latest stored manual or provider rate on or before the form's date, today on a fresh form; transaction-derived rates are not used, a refused form keeps the submitted value, changing the date clears an untouched pre-fill, and the user can change it.)
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
  - Re-filling for a changed date (or instrument) uses a small CSP-safe script in
    `static/js/investments.js` that calls a read-only, workspace-scoped JSON endpoint
    (`GET /investments/fx-rate?instrument_id=…&date=…`, built on `stored_fx_rates`). An
    untouched pre-fill is cleared at once and then re-filled with the new date's stored rate
    when one exists; a rate the user typed is kept. (As built: the app's other investment forms
    use plain delegated listeners in that file, not Alpine components, so this follows them.)
    With JavaScript off, the server pre-fills on the first render, and when the rate is missing
    the form returns the inline error.
  - As built (#243): the hint reads "Stored rate for {date}" (the form's date) rather than
    naming the observation's own date and source; `stored_fx_rates` returns only the rate.

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
  derived quote unit price (rounded to the quote currency's minor unit) and the rate beside them
  ("1 600.00 PLN · ≈ 398.77 USD @ 4.0123").
- Editing opens in the mode the row was saved in.
- Switching modes on an existing row:
  - **quote → base:** pre-fills base price = `unit_price × fx_to_base` and base fee = `fee × fx`.
  - **base → quote:** pre-fills the stored derived quote values and clears the settlement.

**Audit and archive.**
- `investment_transaction.created`/`.updated` keep their shape. `.updated` adds `changed`, the
  comma-separated names of the changed fields (settlement columns included); no amounts are
  logged at INFO.
- F6.9 must export and restore the three columns. Add them to its file list when F6.9 is
  implemented.

### Part B — bulk add of transactions

- An **"Bulk add"** button on the Transactions tab (global history) opens a modal dialog with a
  textarea, in the same style as F5.1's bulk add. Editors and owners only. A preview or an error
  re-renders the dialog open (as a modal after HTMX swaps it in); creating or leaving the page
  closes it. Without JavaScript the button is a link to `?form=bulk`, which renders the dialog
  open in the page flow.
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
    `investment_transaction.bulk_created` records the count, the instrument ids and the batch id,
    with no amounts. (As built: `import_batch_id` is a plain nullable column without a foreign
    key, since bulk add records no `import_batches` row and must not appear in the import
    history with its rollback; F6.9 decides whether its standalone import adds one.)
  - Same-date pasted rows keep their line order (each gets a creation time one microsecond
    after the previous line).
  - The page shows a success toast with the count. There is no batch undo in v1; single rows keep
    their normal delete and undo.

### Tests

- **Settlement golden test.** Buy 4 @ 1 600.00 PLN + fee 5.00 PLN with FX 4.0123:
  - cost basis = 6 405.00 PLN exactly;
  - `unit_price` = 398.773770655235151908 (18 places, half-up);
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
  - Mode switch on edit.
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
pre-fill, the settlement cost-basis rule in compute_holdings/realized_events,
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
- EODHD has a per-workspace daily call budget (default 20) counted per UTC day; latest prices are fetched first, then history uses the remainder, and the rest is skipped with `quota_exhausted` while last-known prices remain. CoinGecko history is requested as far back as the public/demo API allows (the current Basic plan is limited to the past two years per its documentation); when it cannot cover the first transaction date, the instrument records `history_limited` and the one-off backfill marker prevents daily retries. EODHD prices use the raw `close`, falling back to `adjusted_close` only when `close` is missing (F6.10: adjusted closes are expressed in today's units and include dividend adjustments, so they cannot be carried across splits); history backfilled before F6.10 may already hold adjusted closes and a re-backfill is a follow-up. GPW/XETRA/LSE coverage stays marked unverified until checked with a real key.
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
- **Non-investable classes (owner decision 2026-11-09):** every class counts in the portfolio totals, the class table and the chart. A class with `investable=false` gets a "not counted in FIRE" badge in the class table; nothing is excluded or hidden.
- **As built (owner review of the plan, 2026-11):**
  - A missing **price** is handled like missing FX. In both cases the instrument's value **and its cost basis** are left out of every total (portfolio, classes, shares, chart), so the totals and gain % stay consistent. The row stays with a warning, and a note counts the excluded holdings. A stale price or FX rate is valued, with a badge.
  - Look-through rounding: every class except the largest weight gets `floor(amount × weight / 10 000)`, and the largest gets the remainder. Equal largest weights are broken by the class's sort order, then its id. Value and cost are split separately.
  - Realized P&L: `domain.holdings.realized_events` lists each sell's realized P&L from the same ordered pass as `compute_holdings`. All time is their sum; YTD sums the events dated from 1 January of the workspace's current year. YTD is not computed by subtracting a dated holdings run, which excludes undated legacy lots.
  - Chart:
    - The totals box also shows a compact look-through **asset-class donut**: positive class values in the classes' colours, archived classes included, plus an *Unallocated* slice when an instrument has no allocation. Its total is the portfolio value and it has no small-slice grouping.
    - The instrument donut below is titled **Holdings by instrument**.
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
- `portfolio_series(transactions, prices, fx, start, end, step[day|week|month]) -> list[Point(date, value, net_invested)]`: quantity at each date from transactions ≤ date; price = latest ≤ date (carry forward); FX likewise. Lead gaps (no observation on or before the point) can be backfilled from the earliest later observation when the caller asks for it; the Performance chart does, so it does not open with a zero dip. Genuinely missing data (no observation at all) stays incomplete. `net_invested` = cumulative buys + fees − sell proceeds − net dividends in base currency. Undated legacy lots are excluded from the series and counted in a page note. A point with a held instrument missing a price or FX rate is marked incomplete and leaves both that instrument's value and net invested out.
- F6.10 extends the series: a split scales the quantity from its date (`new/old`), dividends do not change quantity, and a raw price observed at `p` is multiplied by `old/new` for every split in `(p, date]`. Dividends are positive XIRR cash flows equal to their net base amount; splits are not cash flows. Splits and dividends are never transaction-price observations.
- Chart: value line + invested-capital line (area between = gain), range selector (YTD/1y/3y/5y/all; default 1y), per-class filter (default whole portfolio, look-through weights applied to value and net invested). Leading points before the first dated transaction are dropped rather than drawn as a zero prefix. An incomplete point is a gap in both lines (`null`, never connected); its plotted-data row shows “—” and says it is incomplete. Ranges up to 90 days step daily; longer ranges step weekly, and all-time spans over five years step monthly. Bond-model instruments are priced with `bond_value` for the lots held at each point.
- Table of period changes (1M, 3M, YTD, 1Y, all-time): absolute and % change of value, net flows, **Δ value − net flows**, and the contribution-adjusted return via XIRR. The start value is a negative cashflow at the end of the start date (after that day's trades), later buys are negative and sells positive, and the end value is positive. All-time starts at zero on the day before the first dated transaction. Periods under 365 days show the period money-weighted return `(1 + r)^(days/365) − 1`; periods of 365 days or more (1 year, or an all-time history at least that long) show the annualised XIRR. An incomplete start or end makes the XIRR undefined.
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
- Register resolvers for `portfolio_asset_class` (class value, look-through, base currency) and `portfolio_holding` (instrument value) with the M5 `ValuationResolver` registry. Registration is idempotent and runs in the app factory, so web reads and the scheduler's automatic snapshot job both run with the resolvers registered.
- Value dates follow the two portfolio rules:
  - On or after the workspace's today, use the F6.6 current valuation: current holdings include undated legacy lots and take the latest prices and FX rates. The linked class or instrument value matches the **Portfolio** page exactly.
  - For past dates (manual snapshots dated earlier, backfill, the automatic month-end snapshot), use the F6.7 series rules: dated transactions only, prices and FX carried forward from the latest observation on or before the date, and the per-date bond model.
  - Because the historical path excludes undated legacy lots, the first historical snapshot after such lots appear can be lower than today's value. The resolver introduces no step of its own; this one is inherent to F6.7 and is documented for users. F6.8 keeps the conservative series rule: there is no leading-observation backfill, so a date before the first known observation stays `Unavailable` rather than borrowing a later value.
- A class link is `Unavailable` when any held instrument allocated to it lacks a price or FX rate. A holding link is `Unavailable` when the instrument is held and missing a price or FX rate, and zero when it is not held. Resolvers never return a silently partial class value.
- Sold-out and archived targets still resolve existing links. Pickers offer active asset classes and active instruments with quantity above zero; a sold-out or archived target is no longer offered for new links.
- Helper button "Create net-worth items from asset classes" creates linked asset items, one per active class with a complete value above zero, skipping archived classes, classes with a missing price or FX member and classes already linked. Classes with `investable=false` are created with `investable=false`. The items go into a dedicated **Investments** asset category (pl **Inwestycje**), created on demand when no active category with that name key exists in any supported locale, appended at the end of the category list and audited as `networth_category.created` in the same write. It is not added to the suggested category seed set. The helper re-checks existing links inside its locked write, so two simultaneous clicks create no duplicates. Each item gets one `networth_item.created` and one `networth_item.linked` audit event; `estimate_between` keeps the side default.
- Workspace setting `auto_link_portfolio`: off for workspaces created before the setting (migration default), on for new workspaces (set explicitly at creation). When on, investment writes (instruments and allocations, transactions including bulk and standalone import, and the risk policy save) ensure one linked item per active class with a complete value above zero in the same transaction, using the same Investments category logic. Classes with an active **or archived** linked item are skipped, so archiving an item is respected. When off, the Net Worth Assets page shows the manual helper only while at least one eligible unlinked class exists.
- Warnings appear only on **Net Worth → Assets**, are advisory and never block or change a value:
  - when an active class link and an active holding link for one of its currently held instruments would both count in net worth;
  - when an active `real_estate` instrument and an active item in the **Real estate** category both have a positive current value (legacy workspaces seeded before 2026-11-09).
- Net-worth snapshots (M5.3) pick up linked values automatically: a manual snapshot dated today uses the current path, while past, backfilled and automatic month-end snapshots use the historical path.
- `resolve_many` loads the portfolio history once for all requested dates and targets; a 60 month-end × 30 instrument resolution stays well below two seconds in CI.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F6.8 from docs/specs/M6-investments.md: ValuationResolver implementations for portfolio_asset_class and portfolio_holding
(current F6.6 path for today, F6.7 history for past dates), the create-linked-items helper with an on-demand Investments category,
the double-counting warnings on Net Worth -> Assets. Tests: resolver values match the Portfolio page, historical as_of, snapshots through
the scheduler job, the helper's audit/idempotence/concurrency, missing data, warnings and resolution timing.
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
  `float`); FX rates likewise. Values are written with `format(value, "f")` and trailing
  fractional zeros removed, and read with `Decimal(text)`; a Hypothesis round-trip test covers
  quantity, unit price, price, rate, `fx_to_base` and `settlement_unit_price` across the
  `DecimalType` range.
- Standalone import in merge mode: `investment_transactions.csv` (the "bring my purchase
  history" path: instrument by name, with the mapping step for unknown instruments like F4.3's
  categories, create with a type the user picks; an empty `date` makes an "undated lot" flagged
  for the user to fix). Other files are restore only.
- The standalone import refuses to apply while any instrument's combined stored and incoming
  history oversells, naming the instrument, line and date; the user fixes the file or chooses
  **Ignore** for that instrument in the mapping. Rows are never silently skipped to make the
  history fit. FX fallback uses the latest stored manual or provider rate on or before the date
  and otherwise reports `fx_needed`; it is never used for a settlement row, which must carry its
  own `fx_to_base`.
- Provider settings are archived without secrets. Keyless providers (NBP, ECB, CoinGecko)
  restore with their saved enabled state; providers that need a key (EODHD, GoldAPI) restore
  disabled with the report line "Re-enter the API key and enable {provider}". The daily call
  budget and the `fx_backfilled` marker are kept; `provider_backfilled_at` on an instrument is
  kept too.
- Net-worth items linked to a portfolio class or holding resolve their lineage ids in a 1.4
  archive; a 1.3 archive (or any missing target) keeps the existing `link_target_missing`
  fallback and restores the item as manual without a value. Profile `birth_year` and
  `life_expectancy` are user data, not workspace data, and are not archived.
- The new investment data models join `EMPTINESS_CHECKS`; the M4 round-trip test covers them. An untouched suggested asset-class set (the current seed or the pre-2026-11-09 seed with Real estate and Primary residence) counts as empty for restore, like the ledger and net-worth suggested sets (the exact set in a supported locale, unedited, unused, with the automatic `asset_class.seeded` event); otherwise every fresh workspace would refuse a restore. `RiskTargetPolicy` is a workspace setting carried in `workspace.json`: restore applies it and merge reports it in the settings diff, but a saved policy does not make the workspace non-empty. `ProviderError` stays excluded.

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
- **Apply suggested risk tiers** on the Asset classes tab (editors/owners): one locked, audited write sets the seed tier on every class whose `name_key` matches a suggested class name in English or Polish (current or pre-2026-11-09 set) and whose tier is empty. It never overwrites a tier, is idempotent, and the action is shown only while at least one such class has no tier.
- Target policy per workspace (`RiskTargetPolicy`), editors/owners only:
  - `fixed` (**default**): user-entered targets, default **50 / 40 / 10** (safe / growth / speculative); must sum to 100%.
  - `age_scaled` (owner's rule): speculative = fixed X% (default 10%); safe = (100% − X) × age ÷ life expectancy; growth = the rest. Example: X = 10%, age 40, life expectancy 80 → 45 / 45 / 10. At or beyond life expectancy safe is capped at 100% − X.
  - `age_in_bonds` (classic preset): safe = age%, speculative = fixed X% (default 0%, editable), growth = the rest; safe capped at 100% − X.
  - Age and life expectancy come from the **current user's profile**: `birth_year` (optional) and `life_expectancy` (default **90**, range 50–120). Age-based modes are disabled with an explanation until the user sets a birth year. Age = current year (from the injected Clock) − birth year. Both profile fields are added here if M8 has not added them yet; M8's `horizon_age` defaults to the profile's `life_expectancy`.
  - Percentages are stored as basis points; age-based targets are computed (not stored) and rounded half-up to whole basis points once, with the rounding remainder put on `growth` so targets always total exactly 10 000. The fixed targets stay stored in `safe_bp`/`growth_bp`/`speculative_bp` when an age mode is selected; age modes use `speculative_bp` as X. With no saved row, the first age-mode save proposes X = 10% for `age_scaled` and 0% for `age_in_bonds`; once saved, switching modes keeps the user's X.
- Portfolio page section "Risk balance": per tier the current value, current %, target %, the difference in percentage points and in money (base currency), and a plain-language hint ("move about €X from Growth to Safe") for the largest gap. A tier is flagged when |current − target| > tolerance (default **5 percentage points**, editable). Tier names and the speculative subtitle are translatable; users can't rename tiers in this feature (follow-up).
  - Each tier's target value is `round_half_up(assigned_total × target_bp / 10 000)`, rounded once.
  - The difference is current value minus target value, in money.
  - The hint moves `min(largest overweight amount, largest underweight amount)` from the most overweight tier to the most underweight tier. No hint is shown when the largest absolute gap is within the tolerance.
  - The risk balance uses the same included rows as the Portfolio page: a holding excluded there for a missing price or FX rate is excluded from the tiers too. A class without a tier (including a non-investable or Other class) appears under **Unassigned** and is excluded from the tier percentages.
  - **As built (UI polish):** the balance is recomputed on every request; there is no daily cache. When the unassigned amount is above zero, the Portfolio page shows a warning with that amount and share, a sentence that the classes need a risk tier and a link to the Asset classes tab. When `suggested_risk_tier_changes` finds matching empty classes, the warning also offers a one-click **Apply suggested risk tiers** action (`POST /investments/portfolio/apply-risk-tiers`), which redirects back with a notice.
- Pure domain functions: `risk_targets(policy, age, life_expectancy) -> dict[tier, bp]` and `risk_balance(class_values, tiers, overrides, targets, tolerance)`. Golden tests: the 45/45/10 example above; 50/40/10 fixed; age_in_bonds at age 35 with X = 10 → 35/55/10; capping at age ≥ life expectancy. Property tests: targets always sum to 10 000; current tier percentages sum to 10 000 when any tier has value; monotonic: an older age never lowers `safe`.
- Not investment advice: the section shows the same "illustrative, not advice" notice as the FIRE planner. The age used for the current viewer comes from their profile; the page says so and links to Profile when a birth year is missing. Profile `birth_year`/`life_expectancy` (M8 uses the latter as the default `horizon_age`) are added here because M8 has not landed yet.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F6.11 from docs/specs/M6-investments.md: risk_tier on AssetClass (+ seeds), risk_tier_override on Instrument,
RiskTargetPolicy + migration, profile birth_year/life_expectancy if missing, pure risk_targets()/risk_balance() with golden and
property tests, the policy editor and the "Risk balance" section on the portfolio page (en/pl, tenant isolation, editors only for writes).
```

---

## F6.10 (stretch) Dividends, interest and splits

**Acceptance**
- Storage reuses `quantity`, `unit_price` and `fee_minor`; no new amount or ratio columns.
  - **Dividend / interest:** `quantity = 1`, `unit_price` is the gross amount in the quote
    currency, `fee_minor` is the withholding. Gross must be positive and fit the currency's
    minor unit; `0 ≤ withholding ≤ gross`; the date is required; there are no settlement
    fields; `fx_to_base` is required when the quote currency is not the base and is `1`
    otherwise.
  - **Split:** `quantity` is the number of new units and `unit_price` the number of old units
    (a 2-for-1 split is `2` and `1`; a 1-for-10 reverse split is `1` and `10`). Both are
    positive and different, `fee_minor = 0`, and there is no FX or settlement. The date is
    required. Splits are refused for `currency` and `bond_model` instruments. A split with no
    holding is accepted as a no-op.
  - The same validation applies in the transaction form, the F6.9 standalone CSV import and
    archive restore, and is documented in `docs/archive-format.md` and
    `docs/import-export.md`.
- **Linked ledger income** (the "Record as income" option on a dividend):
  - `gross_base = round_half_up(gross × fx)` and
    `withholding_base = round_half_up(withholding × fx)`, each rounded once per row; the net
    is `gross_base − withholding_base`, so the ledger row equals the report's net exactly.
  - Recording income requires an active income category. The form preselects the category of
    the most recent linked dividend, otherwise the seeded "Interest" category matched through
    its seed `name_key` in en/pl (never a literal), otherwise no choice.
  - The ledger row uses `source = 'investment'` and is managed only through its dividend.
    Editing or deleting it from the month page is refused with a link to the dividend, and the
    row shows a "from investment income" badge. The new source value is added to the
    `transactions.source` CHECK with a SQLite-safe batch migration.
  - Creating, editing, deleting or undoing a linked dividend takes `lock_month_writes` and
    raises `MonthClosed` when the old or the new ledger month is closed. Unlinked dividends and
    splits do not touch the ledger and are unaffected.
  - Undo restores the dividend and its linked income together. Changing the dividend's date,
    amount, fee or category updates the linked row in the same locked write. Unticking
    "Record as income" soft-deletes the linked row; editing a dividend into another type does
    the same.
- **Split arithmetic:**
  - Scale each FIFO lot (and the average-method quantity) by `new/old` inside an 80-digit
    context; quantize the resulting holding quantity once to the DecimalType scale (18 decimal
    places, `ROUND_HALF_UP`). Cost basis and realized P&L are unchanged. Undated legacy lots
    come before every dated row, so splits apply to them.
  - Oversell validation covers the whole sequence including splits in form edits, the F6.9
    import and the F6.9 rollback. Editing or deleting a split can make a later sell oversell
    and is refused with the existing `OversellRejected` error.
  - Cash-in-lieu for fractional shares is a normal sell the user records; it is not modelled.
- **Price carry-forward across splits:** every stored price observation (manual, provider or
  transaction-derived) is a raw price per unit as of its date. When an observation dated `p`
  values a holding on `d > p`, multiply it by `old/new` for each split in `(p, d]` through the
  pure `split_factor(splits, from_date, to_date)` helper. Use it in the F6.6 current
  valuation, the F6.7 series, the F6.8 net-worth history and the F6.3 transaction-price
  fallback. Quantities at `d` are historical: splits `≤ d` are applied. EODHD therefore stores
  the raw `close`, falling back to `adjusted_close` only when `close` is missing (see F6.4);
  history backfilled before F6.10 may already be adjusted and a re-backfill is a follow-up.
- **Performance (F6.7):** the net dividend in base is a positive XIRR cash flow on its date
  and counts in the net flows, reducing `net_invested`; splits are not cash flows. Dividends
  and splits are never transaction-price observations.
- **Income report:** a new **Income** tab under Investments, readable by viewers, grouping
  dividends by calendar year (newest first) with gross, withholding, net and a payment count.
  Linked ledger rows are not counted a second time. Plain fee rows stay unsupported and out of
  scope. Undated dividends are excluded from the years and noted.
- **Archive:** keep format 1.4 (it has not been released); add an optional
  `income_transaction_id` column to `investment_transactions.csv`. Restore resolves it to the
  restored ledger row and marks that row `source = 'investment'`. A missing target restores the
  dividend unlinked and adds an `income_target_missing` report line. The merge flow stays
  restore-only for investment files. If a release ships before this merges, bump to 1.5.
- **Tests:** golden splits (1-for-3 exact, 3-for-2 FIFO), the split/inverse property within
  1e-18 with an identical cost basis, split-adjusted carry-forward without a jump in
  F6.6/F6.7/F6.8, EODHD raw close, linked-income rounding edge, closed-month refusal on
  create/edit/delete/undo, the ledger edit/delete guard, the CHECK migration upgrade and
  downgrade, an XIRR dividend vector, F6.9 dividend/split import and rollback with splits, the
  archive round trip with a linked dividend and the missing-link fallback, and the Income tab.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F6.10 (stretch) from docs/specs/M6-investments.md: dividend and split transaction types in compute_holdings and the series,
the optional linked ledger income with its closed-month and source guard, split-aware price carry-forward,
and the yearly investment-income report. Keep existing behaviour unchanged apart from the F6.7 dividend flows.
```
