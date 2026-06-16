# Sources

This dataset is assembled from public sources.

| Source | Usage | Notes |
|---|---|---|
| Colombo Stock Exchange (`cse.lk`) | Official historical archive files, company metadata, daily OHLCV candidate payloads, 2026-forward daily/periodic reports, corporate calendar, news, annual report links | Official historical file coverage is treated as ending on `2025-12-31`. The archive includes many data families and changing schemas; see `docs/historical_data.md`. `tradeSummary` is current-snapshot only unless the payload has independently verifiable source dates. It is not trusted for historical backfill. |
| Lanka Business Online | Financial/business news | Uses the public WordPress REST API. |
| World Bank | USD/LKR and macro indicators | Annual indicators are forward-filled when joined to daily market rows. |
| stooq.com | Global index levels | Used for S&P 500, Nikkei 225, and Hang Seng daily closes. |
| CBSL | Interest rate target source | Automated ingestion still needs source freshness reporting before publication. |

Recon-only candidates currently include TradingView, Investing.com,
MarketScreener, Yahoo Finance chart APIs, EODHD, Nasdaq Data Link, Alpha
Vantage, and Finnhub. Yahoo Finance is the first third-party candidate with
observed 2026 dated OHLCV rows for CSE symbols, but it is not a published
dataset source until a dedicated adapter rejects invalid OHLC rows and
cross-checks accepted rows against official CSE same-day snapshots or another
dated source.

This project is not affiliated with or endorsed by the Colombo Stock Exchange or any listed data source.
