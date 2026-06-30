# Data Source Recon Results

Probed: 2026-02-26. Recovery boundary updated 2026-05-31.

## Recovery Coverage Boundary

The official historical files currently in this repository are treated as
source coverage through `2025-12-31`. Data from `2026-01-01` onward must be
filled through scheduled, validation-first ingestion from date-bearing official
daily or periodic sources.

## ASPI Historical Data

### Current Status

| Source | Coverage | Status | Notes |
|---|---|---|---|
| Kaggle legacy CSV (`CSE.csv`) | 1997-07-01 to 2021-02-19 | Historical candidate only | Not carried into v2 clean history. Any use requires documented provenance and validation. |
| CSE API (`chartData`) | Trailing window only | Candidate | Must be revalidated before use. |
| Combined file | 1997-07-01 to 2026-02-25 (with gap) | Legacy candidate only | Not part of accepted v2 clean history until provenance and validation are rebuilt. |
| Official historical CSE files | Through 2025-12-31 | Historical candidate | Convert and validate separately from 2026-forward ingestion. |
| **2026-forward gap** | **2026-01-01 to current date** | **Unresolved until daily/periodic source ingestion passes** | Must use date-bearing official reports/endpoints; never `tradeSummary` historical stamping. |

### Gap-Filling Candidates

| Source | Status | Blocker |
|---|---|---|
| Investing.com (Playwright scrape) | Reachable (HTTP 200) | JS-heavy page — needs Playwright with full browser. Previous attempt timed out. |
| Wayback Machine CDX API | TIMEOUT | Network/firewall blocks `web.archive.org` from this machine. |
| Yahoo Finance (yfinance) | No CSE tickers | `YFTzMissingError` for all `.CM` suffix tickers — CSE not covered. |
| Stooq | No data | ASPI ticker returned empty dataset. |
| Alpha Vantage | No CSE tickers | Premium endpoint; CSE symbols not in their database. |
| Finnhub | 403 Forbidden | CSE not in free-tier exchange coverage. |
| CSE StockMarketDaily PDFs | First 2026-forward source attempt | Default URL template is `https://cdn.cse.lk/cse-daily/StockMarketDaily%28SMD%29DD-MM-YYYY.pdf`. Missing/private/unpublished reports are quarantined; a parsed report date is required before acceptance. |

## CSE Individual Stock Price Data

### Yahoo Finance Coverage

Tested 20 major CSE tickers with `.CM` suffix. **0 out of 20 available.** Yahoo Finance does not carry Colombo Stock Exchange securities.

Primary source order for individual stock OHLCV recovery:
1. CSE current snapshot endpoints for same-day/current-trading-day data only.
2. Any official CSE endpoint discovered later that exposes date-bearing OHLCV.
3. CSE StockMarketDaily or daily-market PDFs for 2026-forward recovery, with parsed report date and extractable symbol-level rows.
4. External/open datasets only after provenance and cross-source checks.

Live checks on `2026-05-31` reconfirmed that `tradeSummary` and
`dailyMarketSummery` return latest/current payloads for historical requested
dates. They must not be used to fill the `2026-01-01` to current-date gap.

The 2026-forward pipeline stores every fetched file or payload with source URL,
fetch time, source/report date, payload hash, row count, and validation status.
Rows are normalized into candidate tables first and accepted only after
source-specific validation passes.

Forward ingestion now supports contract validation for the existing non-OHLCV
families in the historical files:

- CSE families: indices, market statistics, corporate actions, listings and
  de-listings, public holdings, foreign holdings, sector/GICS data, and
  news/announcements.
- Macro/rates families: exchange rates and policy/market rates from CBSL or
  another documented official source.

The current implementation accepts date-bearing local files or official URL
templates for those families. Source discovery still has to identify the best
official current endpoint/file for each family before scheduled collection is
enabled.

## News / Sentiment Sources

| Source | Method | Coverage | Status |
|---|---|---|---|
| Lanka Business Online (LBO) | WordPress REST API | 2021-03-30 to present — 1,438 posts, 288 pages | Working. Endpoint: `https://www.lankabusinessonline.com/wp-json/wp/v2/posts` |
| LBO RSS feed | feedparser | Latest 10 entries only | Working. URL: `https://www.lankabusinessonline.com/feed/` |
| Daily FT | WordPress REST API | Not available — 404 | `ft.lk` does not expose WP REST API. Requires Playwright or direct HTML scrape. |
| Daily FT RSS | feedparser | Not available — 404 | RSS feed returns 404. |
| Daily Mirror | feedparser | 0 entries (200 OK but empty feed) | RSS feed returns empty. Requires investigation. |
| Wayback Machine | CDX API | TIMEOUT | Blocked from this machine. |

## Third-Party APIs

| API | Key Set | CSE Coverage | Notes |
|---|---|---|---|
| Alpha Vantage | Yes (`.env`) | None | CSE symbols not indexed. Premium endpoint required even for basic queries. |
| Finnhub | Yes (`.env`) | None | CSE not in free-tier exchange list. Returns 403 for all CSE tickers. |
| EODHD (EOD Historical Data) | Demo token only | None | Demo token confirmed working (AAPL.US 2011 data returns correctly). CSE is not present in their exchange catalogue — returns 403 for `COMB.CSE`. Paid subscription unlikely to help without explicit CSE listing. |
| Nasdaq Data Link | None | Unknown | Cloudflare bot protection blocks all direct API access. |
| MarketScreener | None | Unknown | Returns 403 Forbidden for CSE stock pages. |
| Stooq.com | None | Unknown | Cloudflare blocks access; `comb.lk` symbol returns "Access denied". |

## 2011 Gap-Fill Source Exhaustion (TIQ-20)

The 2011 OHLCV gap was caused by the official CSE source file (`2011_Data__hl.csv`)
covering only 74 of 281 companies (A–E alphabetically). Yahoo Finance recovered 149 of
the remaining 169 missing tickers using the `TICKER-SHARETYPE.CM` format.

The 20 unrecovered tickers were investigated against every source in this codebase.
Status as of 2026-06-30:

- **5 X/U-series** (preference shares, unit trusts): structurally unrecoverable — global
  aggregators do not track these instrument types for CSE.
- **15 N-series**: mostly 2011 IPOs listed June–November 2011; a few older companies
  (SEMB listed 1993, SWAD listed 1970) that had full-year 2011 trading but no global
  aggregator coverage. None of the tested sources carry them.

All tested sources exhausted:

| Source | Outcome |
|---|---|
| Yahoo Finance `TICKER-TYPE.CM` | 149/169 (20 errors) |
| Alpha Vantage | No `.CM` exchange support |
| Finnhub | No CSE coverage |
| Stooq.com | Cloudflare blocked |
| EODHD | CSE not in exchange list; 403 Forbidden |
| Nasdaq Data Link | Cloudflare blocked |
| Wayback Machine CDX | Network timeout (ISP block) |
| Investing.com (browser) | JS date-picker unautomatable |
| MarketScreener | 403 Forbidden |
| TradingView (browser) | ToS risk; browser-only |
| CSE historical API endpoints (guessed) | All 404 |

The 88.2% gap recovery (149/169 additional tickers, 223/281 total 2011 universe)
is the ceiling achievable from publicly available digital sources. Further recovery
would require a paid EODHD/Bloomberg subscription or a direct data request to
the Colombo Stock Exchange.

## Network Constraints

- `web.archive.org` — **TIMEOUT** (firewall/ISP block)
- `data.nasdaq.com` — Cloudflare bot protection blocks API access
- `stooq.com` — Cloudflare blocks access
- `marketscreener.com` — 403 Forbidden
- All other tested domains reachable (CSE, LBO, FT, Daily Mirror, Alpha Vantage, Finnhub, Google)
- Yahoo Finance API reachable; CSE tickers work with `TICKER-SHARETYPE.CM` format (e.g. `COMB-N0000.CM`)
  but 20 specific 2011 tickers (mostly recent IPOs) are absent from their database
