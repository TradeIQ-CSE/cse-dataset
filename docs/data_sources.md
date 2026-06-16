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

## Network Constraints

- `web.archive.org` — **TIMEOUT** (firewall/ISP block)
- All other tested domains reachable (CSE, LBO, FT, Daily Mirror, Alpha Vantage, Finnhub, Google)
- Yahoo Finance API itself is reachable (AAPL returns 200) but CSE tickers are simply not listed
