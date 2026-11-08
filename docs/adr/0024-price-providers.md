# ADR-0024: Price and FX providers

**Status:** Accepted
**Date:** 2026-11-04

## Context

M6 values a portfolio from transactions plus prices (docs/specs/M6-investments.md, F6.3 and
F6.4). Prices must come from pluggable providers with a manual fallback, and the project rules
bound the choice: no external calls at runtime except features the user explicitly configures,
no scraping against a site's terms, no new runtime dependency without strong justification.
Manual price entry (F6.3) keeps working for every instrument — including real estate and
illiquid assets — no matter what is enabled.

The typical instance is a household in Poland or the EU: base currency PLN or EUR, holdings
dominated by UCITS ETFs on XETRA, LSE and GPW, plus crypto and precious metals. On 2026-11-04
sixteen candidates were evaluated from primary sources (official API docs, terms pages, PyPI,
GitHub) with live endpoint tests run the same day. Terms clauses below are quoted as found on
the linked pages; anything not confirmed is marked *unverified* and listed in the register.

The finding that shaped the decision: **no acceptable free provider for EU equities/ETFs
remains in 2026** — Stooq's CSV API is removed or anti-bot-gated, and Yahoo's terms prohibit
automated collection outright. Equities therefore ship manual-first with one paid optional.

## Evaluation summary

| Category | Provider | Key? | Free tier | UCITS ETF (XETRA/LSE/GPW) | History | ToS risk | Dep needed? | Decision |
|---|---|---|---|---|---|---|---|---|
| FX | NBP API | no | unlimited (be polite) | n/a | FX 2002–, gold 2013– (PLN/g) | low (no terms published; official public data) | no | **Built-in default** |
| FX | ECB eurofxref | no | unlimited (static files) | n/a | 1999– | lowest (free use + attribution) | no | **Built-in default** |
| FX | Frankfurter | no | no quotas | n/a | 1948– | low (MIT, provider terms pass-through) | no | Approved-optional, deferred |
| FX | exchangerate.host | yes | 100 req/mo | n/a | 19 yrs | medium (APILayer product) | no | Rejected |
| Eq/ETF | Stooq | no | — | ✅ historically (cspx.uk, vwce.de, GPW) | long | unknown — endpoints gone/anti-bot-gated | no | Rejected (re-check) |
| Eq/ETF | Yahoo/yfinance | no | — | ✅ (unverified now) | long | high — OTOS §2(d) forbids automated collection; client impersonates browsers | yes (pandas + curl_cffi) | Rejected |
| Eq/ETF | Alpha Vantage | yes | 25 req/day | weak/unverified | — | medium | no | Rejected |
| Eq/ETF | Twelve Data | yes | 800/day, 3 markets (no EU) | paid ($79+/mo or $300/yr add-on) | — | low-medium | no | Rejected |
| Eq/ETF | EODHD | yes | 20/day, 1 yr, global EOD | ✅ (unverified without key) | 30+ yrs paid | low-medium (personal plan) | no | **Optional (F6.4)** |
| Eq/ETF | FMP | yes | 250/day | unverified | — | medium | no | Rejected |
| Crypto | CoinGecko | no (public) / free key | 10k calls/mo, 100/min | n/a | 13+ yrs | low if "Powered by CoinGecko" shown & ≥24 h refresh | no | **Built-in default** |
| Crypto | Kraken | no | 1 call/s public | n/a | candles (OHLC) | low-medium (broad anti-automation wording, but the API is the sanctioned interface) | no | Approved-optional, deferred |
| Crypto | Binance | no | weight 6000/min | n/a | klines | unverified (ToS unreachable) + geo | no | Rejected |
| Metals | NBP cenyzlota | no | unlimited (be polite) | n/a (gold, PLN/g) | 2013– | low (as NBP) | no | **Built-in default (PLN)** |
| Metals | GoldAPI.io | yes | ~150 req/mo | n/a (XAU/XAG/XPT/XPD, 72 ccy) | LBMA since 1968 | low-medium (display allowed, no resale) | no | **Optional (follow-up)** |
| Metals | metals-api.com | yes | unverified | n/a | 1999– | unverified | no | Rejected |

No candidate needs a Python client: everything approved is plain HTTP plus the standard
library. The only real client in the field, `yfinance` (Apache-2.0), is rejected on terms
grounds, not licensing.

**FX.** Two official, keyless endpoints cover both common base currencies: NBP (PLN base, plus
gold in PLN) and ECB (EUR base, the cleanest terms of everything evaluated — free use with
citation). Frankfurter is a well-mannered MIT aggregator over 106 official sources and is
self-hostable, but the defaults stay with the institutions themselves. exchangerate.host's 100
free requests/month cannot refresh a portfolio daily.

**Equities/ETFs.** Stooq's snapshot endpoint `q/l/` answers 404 and its history endpoint
`q/d/l/` answers with a JavaScript proof-of-work browser-verification challenge (both verified
live on 2026-11-04 from a datacenter IP); passing it would mean reimplementing the challenge —
circumvention. Yahoo's Terms of Service §2(d) forbid collecting data "using any automated
means … for any purpose without our express, prior permission" and building "any database,
archive… that competes with or constitutes a material substitute for the Services"; `yfinance`
works through Yahoo's unofficial internal endpoints and ships a `curl_cffi`
browser-TLS-impersonation fallback (live test from here: HTTP 429). Alpha Vantage (25
calls/day), Twelve Data (no EU exchanges on free) and FMP (no EU edge) do not help. The only
acceptable path is EODHD — keyed and quota'd, hence optional.

**Crypto.** CoinGecko's keyless public API quotes directly in USD/EUR/PLN (verified live) with
13+ years of daily history, and its terms are explicit about the two obligations that follow.
Kraken's documented public API gives venue-exact prices at 1 request/second. Binance's terms
page returns HTTP 202 with an empty body to non-browser clients (unverifiable), and access is
geo-fragile.

**Metals.** NBP publishes official gold prices in PLN per gram. GoldAPI.io covers
XAU/XAG/XPT/XPD in 72 currencies including PLN and EUR with the LBMA AM fix back to 1968. Its
terms were read on 2026-11-08: display inside the application is allowed, resale or a competing
API is not, and no attribution is required. metals-api.com inverts its rate convention
(`rates.XAU` = ounces per USD — a Decimal footgun) and its free quota is unverified.

## Decision

**Built-in defaults — still opt-in per workspace.** The three providers below ship built-in
and are enabled per workspace through `PriceSourceConfig` (F6.4). A fresh workspace has none
of them on, and any instrument can always be priced manually.

1. **NBP Web API** (<https://api.nbp.pl/>) — FX against PLN (mid rates of tables A/B) and
   official gold in PLN per gram through `api/cenyzlota`; JSON or XML; keyless. FX archives
   since 2002-01-02, gold since 2013-01-02; one query covers at most 93 days (HTTP 400
   beyond); HTTPS-only since 2025-08-01. No API-specific terms are published (the footer
   reserves all rights; Frankfurter's provider-terms survey lists NBP as "None published") —
   official public reference data, treated as permitted for personal use, with politeness
   limits and an "Narodowy Bank Polski" credit in the sources list. No published rate limit,
   so the provider self-limits to ≤1 request/second and caches aggressively. Gram-to-troy-ounce
   conversion (× 31.1034768) is tested domain logic. Live checks on 2026-11-04: EUR mid
   4.4233 PLN and 493.58 PLN/g gold, both for 2026-11-03.
2. **ECB euro reference rates** (<https://www.ecb.europa.eu/stats/eurofxref/>) — daily
   reference rates of ~30 currencies against EUR, published around 16:00 CET on working days,
   full history since 1999 as static CSV/XML; keyless. The disclaimer allows free use provided
   the information "must appear accurately and the ECB must be cited as the source", and notes
   the rates "are published for information purposes only". EUR base only (see triangulation);
   EUR/RUB publication has been suspended since 2022-03-01. No documented limit, so it is
   fetched at most once an hour — the schedule needs it once a day. Live check: the daily XML
   for 2026-11-03 with PLN 4.4190 and USD 1.1179.
3. **CoinGecko** (<https://docs.coingecko.com/>) — crypto through the keyless public API:
   `/simple/price` quotes directly in USD, EUR and PLN (verified live on 2026-11-04 — no FX
   hop), `/coins/{id}/market_chart` gives 13+ years of daily history. The API terms (v. 5
   Sept 2025) grant a "limited, non-exclusive, non-assignable, non-transferable and
   irrevocable license", allow charging for products that incorporate the API but forbid
   selling, renting, leasing, sub-licensing, re-distributing or syndicating access to it, and
   set two obligations we implement: the attribution below and the storage clause "You should
   refresh the cache at least every 24 hours". Rate limits "may be varied by CoinGecko at any
   time in its sole discretion", and the public no-key endpoint's exact limit is undocumented
   (unverified) — plan for roughly 5–15 calls/minute, and the provider settings page documents
   the free Demo key (100 calls/min, 10,000 calls/month) as the stable option. The daily
   refresh job satisfies the storage clause; the app never re-sells or proxies API access —
   every instance calls for itself.

**Optional, implemented in F6.4: EODHD** (<https://eodhd.com/pricing>) — the only acceptable
EU equities/ETFs path in 2026, keyed and quota'd, therefore never a default. Free "Sandbox":
20 API calls/day, one year of global EOD for stocks/ETFs/forex/crypto/indices, 15-minute
delayed stocks, commodities included (metals unverified without a key). Paid "All-World"
$19.99/month ($199/year): 100,000 calls/day and 30+ years of EOD, so one month covers a full
history backfill. European exchanges use ticker suffixes (`.L`, `.DE`, `.WA`) per their docs,
but the exchange-list page 404'd during the research, so GPW/XETRA/LSE coverage is unverified
without a key and must be confirmed at implementation time. Personal and commercial plans are
separate; redistribution is treated as not permitted (terms text not fetched — unverified).
Plain JSON; the official SDK is not needed.

**Implemented optional (F6.5 follow-up):**

- **GoldAPI.io** (<https://www.goldapi.io/>) — spot XAU/XAG/XPT/XPD in 72 currencies including
  PLN and EUR, historical LBMA AM fix since 1968, key via `x-access-token`, free plan listed as
  about 150 shared requests/month (older plan text said 300). The terms were read on 2026-11-08:
  displaying the data inside the application is allowed, resale or a competing data API is not,
  and no attribution is required. Priced per troy ounce; the adapter converts to grams or
  kilograms. History is limited to 90 days per request, so this first integration backfills the
  latest 90-day window and records `history_limited`.

**Approved-optional, deferred to a follow-up minor release (not in F6.4):**

- **Kraken public API** (<https://www.kraken.com/features/api>) — documented public REST
  market data (Ticker, OHLC, Trades), keyless; "Calling the public endpoints at a frequency of
  1 per second (or less) would remain within the rate limits". The EEA terms' definition of
  "Site" includes "our associated websites, APIs and mobile applications" and broadly prohibit
  "any other forms of automation to engage in any activity on Our Content" and third-party
  applications interacting with "Our Content" without prior written consent — while the same
  company markets the public API with published rate limits as the sanctioned interface
  (documented since 2011; live check: XBT/USD 77,839.10). Risk assessed as low for personal
  use of the documented endpoints; optional, never a default.
- **Frankfurter** (<https://frankfurter.dev/>) — MIT-licensed open-source aggregator (v2)
  over 106 central-bank and official sources including ECB and NBP, 228 currencies, data back
  to 1948, provider-scoped routes, self-hostable via Docker (maintained by a new team, last
  push 2026-10-30). "There are no quotas. Requests are rate-limited to prevent abuse, but
  there are no monthly or daily caps", and storing daily observations locally is explicitly
  encouraged. Optional as JSON convenience and the self-host escape hatch; the defaults stay
  with the official endpoints.

**Rejected.** Stooq (CSV snapshot endpoint removed, history endpoint behind a proof-of-work
anti-bot challenge — using it would require circumvention; terms unverifiable; re-check
periodically, residential behaviour unverified). Yahoo Finance / `yfinance` (Terms of Service
§2(d) forbid automated collection without prior permission and building a substitute database;
the de-facto client evades bot controls with browser-TLS impersonation and would add pandas
and curl_cffi; rejected as a default and as a bundled optional). Alpha Vantage (25 calls/day;
the terms page 404s — unverified). Twelve Data (free tier has no EU exchanges; EU data from
$79/month, the Cboe Europe add-on from $300/year, ISIN lookup from $24,000/year). FMP (no EU
edge over EODHD; free-tier EU coverage unverified). exchangerate.host (APILayer product, key
required, 100 requests/month). Binance (terms unverifiable to non-browser clients;
geo-fragile; rate-limit violations escalate to automated IP bans "from 2 minutes to 3 days";
quotes mostly against USDT; no advantage over CoinGecko and Kraken). metals-api.com (key on
every call, inverted rate convention, free quota unverified; dominated by GoldAPI.io).

**Equities/ETFs in v1 are manual-first.** No clean free provider exists in 2026, so UCITS ETFs
and stocks are valued by manual price entry (F6.3), with EODHD optional for automated quotes
or a long backfill. A **generic user-configured HTTP provider** — the user supplies an
endpoint and field mapping at their own risk — is the escape hatch for sources the app cannot
ship, recorded in the post-1.0 backlog (docs/specs/backlog-post-1.0.md). The UI never names
specific prohibited providers.

**Zero new runtime dependencies.** Every provider above is plain HTTP plus the standard
library: NBP as JSON (XML available), ECB as XML/CSV through `xml.etree`, CoinGecko, EODHD and
the deferred providers as JSON. This satisfies the dependency rule outright, and F6.4's
injectable HTTP client with recorded fixtures keeps tests off the network.

**FX triangulation through the workspace base currency** is confirmed policy. NBP quotes
PLN-base, ECB quotes EUR-base, and neither offers direct crosses (nothing above quotes, say,
USD/CHF): every other pair derives through the workspace base currency, as F6.3 already
specifies and tests.

**Attribution is a UI obligation**, delivered with the provider settings page in F6.4.
Wherever ECB-sourced rates are shown, the source is cited as "European Central Bank" (their
copyright condition). While CoinGecko is enabled, a visible "Powered by CoinGecko" badge is
shown — the terms demand "displaying prominently the message 'Powered by CoinGecko' in a
legible font … no smaller than font size 10". NBP is credited as "Narodowy Bank Polski" in the
sources list (no published requirement; a courtesy). Attribution strings are translatable
(en/pl); the CoinGecko message keeps the exact wording the terms require.

**Politeness and caching.** NBP: no published limit — self-limited to ≤1 request/second with
aggressive response caching. ECB: no published limit — fetched at most once an hour; the daily
job needs it once a day. CoinGecko: stored data refreshed at least every 24 hours by the
scheduled daily job (their storage clause), with F6.3/F6.4 staleness badges keeping displayed
data marked. Exact response-cache TTLs are left to the F6.4 plan; the research suggested 6–12
hours for NBP/ECB "latest" and 5–15 minutes for CoinGecko behind the ≥24 h storage rule.

## Unverified-claims register

Carried over from the research. None of these may be asserted as fact in docs, code or the UI
without re-checking at implementation time:

- Stooq terms text (the site is challenge-gated; the regulations page 404s).
- Stooq behaviour from residential IPs (the live tests ran from a datacenter IP).
- Yahoo endpoint behaviour from residential IPs (the datacenter test got HTTP 429).
- Binance ToS text (HTTP 202 with an empty body to non-browser clients).
- Alpha Vantage ToS page (404).
- FMP, Twelve Data and EODHD free-tier EU-exchange coverage (all key-gated).
- EODHD terms text (redistribution is *treated as* forbidden until read) and EODHD metals
  symbols on the free tier.
- CoinGecko public no-key rate limit (undocumented; plan conservatively).
- metals-api.com free quota.
- NBP rate limits (unwritten; the ≤1/s self-limit is ours, not theirs).
- GoldAPI.io date-range history endpoint path and response shape
  (`/api/history/{metal}/{currency}?from=…&to=…` returning `prices: [{date, price}]`): the
  documentation page and OpenAPI file could not be fetched from the build environment, so the
  adapter follows the integration plan and its recorded fixture; confirm with a real key.

## Implementation notes (F6.4)

- NBP, ECB, CoinGecko and EODHD ship in F6.4. The provider runtime uses
  the standard library only (`urllib.request`, `xml.etree`, `json`), with a 10 s timeout, bounded
  responses, HTTPS-only URLs, retry/backoff, a bounded TTL response cache and process-wide rate
  limits (NBP 1/s, ECB 1/h).
- Chosen cache TTLs: NBP latest 6 h/history 24 h; ECB 1 h (the full history XML is the single
  source for latest and history); CoinGecko latest 5 min/history 24 h; EODHD latest 15 min/history
  24 h.
- Daily schedule (UTC): NBP 18:10, ECB 18:20, CoinGecko 00:10, EODHD 22:40. "Refresh now" is
  latest-only and never waits behind a long rate limit; scheduled/background jobs may wait.
- Provider observations are converted into the workspace base currency when the provider
  publishes that base: NBP `PLN per X ÷ PLN per base`, ECB `EUR per X ÷ EUR per base`, in an
  80-digit `localcontext`. When the base is not published, FX from that provider is skipped with
  `base_not_published`.
- Provider settings are owner-only and encrypted with the shared bound-secret helper under a
  `price-source-settings` purpose. Errors store only short codes; URLs and response bodies never
  reach logs or audit data.
- Recent ECB history uses `eurofxref-hist-90d.xml`; older backfills use the full file. A
  per-currency `fx_backfilled` marker on the provider config keeps FX history one-off even when
  the first trade falls on a weekend or before the provider's history starts.
- CoinGecko's current documentation (re-checked 2026-11-08) restricts the Basic plan's history
  to the past two years; the app requests `days=max`, marks `history_limited` when the returned
  range starts later than the first transaction, and does not promise a fixed lookback. EODHD
  EU-exchange coverage still needs a real key; the UI marks it unverified. The unverified-claims
  register above remains binding.
- EODHD uses a per-workspace UTC-day call budget (default 20), spending latest calls before
  history and skipping the remainder with `quota_exhausted`. One reservation is taken per
  adapter request, cache hits included, and the reservation row is locked with `SELECT … FOR
  UPDATE` on PostgreSQL; transport retries are not counted as extra reservations.
- GoldAPI.io ships in the F6.5 follow-up: key in the `x-access-token` header only (never in a
  URL, log, error code, audit event or page), 1 request/second, latest 15 min/history 24 h cache,
  daily schedule 22:50 UTC, and a default 4 calls/day to stay under the free plan's ~150 monthly
  requests (the owner can raise it). It shares EODHD's per-workspace UTC-day budget: latest calls
  are reserved first, history uses what is left, a test lookup takes one reservation, and an
  exhausted budget skips the rest with an informational `quota_exhausted`.
- GoldAPI latest is `GET https://www.goldapi.io/api/{metal}/{currency}` (checked on 2026-11-08
  against the provider's own published examples); `timestamp` is Unix seconds and is dated in
  UTC, then capped at the workspace's today as for CoinGecko. Prices are per troy ounce and are
  converted to `oz`, `g` or `kg` (1 oz t = 31.1034768 g) in an 80-digit context. Quote currencies
  are the provider's documented list minus BTC and XAG, which are not instrument currencies.
- GoldAPI history uses the date-range endpoint, at most 90 days per request. This integration
  makes one request per instrument for the most recent 90-day window; when the first transaction
  is older it records the informational `history_limited`, and the one-off backfill marker
  prevents retries. No schema change was needed.
- Quota'd providers (EODHD, GoldAPI.io) spend one reservation and one call per provider symbol
  and quote currency, shared by every instrument on it. A history request that cannot succeed
  on retry (HTTP 400/404/422, an unusable payload, an unsupported symbol or currency) records
  the informational `history_limited` and sets the one-off backfill marker, so it never burns
  the daily budget again. Key failures (HTTP 401/403, a missing key) stay visible errors and
  are retried once the key is fixed; network, timeout, 5xx and rate limits are retried on the
  next run.

## Open items

- **Stooq re-check**: re-test the CSV endpoints periodically, including from a residential IP
  (which may behave differently — unverified). If they return, Stooq is the natural free EU
  ETF source and this ADR should be revisited.
- **EODHD coverage**: verify GPW/XETRA/LSE symbols — and the commodities endpoints, if used —
  with a real key while implementing F6.4, before the UI promises them.
- **Deferred providers**: Kraken and Frankfurter land in a follow-up minor release;
  `PriceSourceConfig`'s encrypted settings JSON already models keyed providers.
- **Cache TTLs** per provider are settled in the F6.4 plan.
- **GoldAPI.io history**: confirm the date-range endpoint and its 90-day limit with a real key;
  a backfill older than 90 days (several windows per instrument) is a possible follow-up.

## Consequences

- Households get official, keyless FX in both common base currencies (PLN and EUR), crypto
  quoted directly in their currency, official gold in PLN, optional GoldAPI metals including
  silver in PLN/EUR, and a workable optional path for EU ETFs — with no new dependency and
  nothing shipped that works against a provider's terms.
- EU equities/ETFs have **no automated default**: users who want automatic ETF prices must
  configure EODHD (the free tier fits a small portfolio with strict caching; one paid month
  covers a 30-year backfill) or wait for the backlog's generic HTTP provider. Manual entry
  covers everything meanwhile. This is a conscious product decision, not an oversight.
- Obligations taken on: the attribution UI (ECB citation, CoinGecko badge), CoinGecko's
  ≥24 h storage refresh, per-provider politeness limits, stale-data marking. The attribution
  UI lands with the provider settings page, not later — shipping CoinGecko without the badge
  would breach its terms.
- Fragility: NBP is a single institution without an SLA (one breaking change already: it went
  HTTPS-only on 2025-08-01); ECB has published the same format since 1999; CoinGecko may vary
  its rate limits at any time but is the strongest operator evaluated (~10B requests/month,
  SOC 2 Type 2, status page); EODHD is commercial and its plans can change. F6.4's failure
  handling — keep the last known price, record provider errors, show staleness badges — is
  the mitigation. Any provider change that breaks these assumptions means amending or
  superseding this ADR.
- The register and open items above bind the implementation: marked claims are re-checked
  before they are asserted anywhere.

References: [NBP API](https://api.nbp.pl/),
[ECB euro reference rates](https://www.ecb.europa.eu/stats/eurofxref/),
[ECB disclaimer and copyright](https://www.ecb.europa.eu/home/disclaimer/html/index.en.html),
[CoinGecko API terms](https://www.coingecko.com/en/api_terms),
[CoinGecko status](https://status.coingecko.com/),
[EODHD pricing](https://eodhd.com/pricing), [GoldAPI.io](https://www.goldapi.io/),
[Kraken API](https://www.kraken.com/features/api),
[Kraken EEA terms](https://www.kraken.com/legal/eea-terms),
[Kraken rate limits](https://support.kraken.com/hc/en-us/articles/206548367-Spot-API-rate-limit-accounts),
[Frankfurter](https://frankfurter.dev/) and its [licence survey](https://frankfurter.dev/license/),
[Yahoo Terms of Service](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html),
[yfinance](https://pypi.org/project/yfinance/),
[Alpha Vantage](https://www.alphavantage.co/premium/),
[Twelve Data pricing](https://twelvedata.com/pricing),
[FMP pricing](https://site.financialmodelingprep.com/pricing-plans),
[exchangerate.host](https://exchangerate.host/),
[Binance API docs](https://github.com/binance/binance-spot-api-docs),
[metals-api](https://metals-api.com/).
