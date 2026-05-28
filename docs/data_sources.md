# Data Source Recon Results

Probed: 2026-02-26.

## ASPI Historical Data

### Current Status

| Source | Coverage | Status | Notes |
|---|---|---|---|
| Kaggle legacy CSV (`CSE.csv`) | 1997-07-01 to 2021-02-19 | Historical candidate only | Not carried into v2 clean history. Any use requires documented provenance and validation. |
| CSE API (`chartData`) | Trailing window only | Candidate | Must be revalidated before use. |
| Combined file | 1997-07-01 to 2026-02-25 (with gap) | `data/raw/legacy/aspi_combined.csv` | 5,750 rows. |
| **GAP** | **2021-02-20 to 2025-02-24** | **Unresolved — ~1,465 days** | No free API covers this window. Requires scraping. |

### Gap-Filling Candidates

| Source | Status | Blocker |
|---|---|---|
| Investing.com (Playwright scrape) | Reachable (HTTP 200) | JS-heavy page — needs Playwright with full browser. Previous attempt timed out. |
| Wayback Machine CDX API | TIMEOUT | Network/firewall blocks `web.archive.org` from this machine. |
| Yahoo Finance (yfinance) | No CSE tickers | `YFTzMissingError` for all `.CM` suffix tickers — CSE not covered. |
| Stooq | No data | ASPI ticker returned empty dataset. |
| Alpha Vantage | No CSE tickers | Premium endpoint; CSE symbols not in their database. |
| Finnhub | 403 Forbidden | CSE not in free-tier exchange coverage. |
| CSE Daily Market Summary PDFs | Viable | PDFs available on `cse.lk` — requires `pdfplumber` scraper. To be implemented in Phase 1. |

## CSE Individual Stock Price Data

### Yahoo Finance Coverage

Tested 20 major CSE tickers with `.CM` suffix. **0 out of 20 available.** Yahoo Finance does not carry Colombo Stock Exchange securities.

Primary source order for individual stock OHLCV recovery:
1. CSE current snapshot endpoints for same-day/current-trading-day data only.
2. Any official CSE endpoint discovered later that exposes date-bearing OHLCV.
3. CSE Daily Market Summary PDFs for historical backfill, with parsed report date.
4. External/open datasets only after provenance and cross-source checks.

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
