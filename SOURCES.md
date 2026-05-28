# Sources

This dataset is assembled from public sources.

| Source | Usage | Notes |
|---|---|---|
| Colombo Stock Exchange (`cse.lk`) | Company metadata, daily OHLCV candidate payloads, corporate calendar, news, annual report links | `tradeSummary` is treated as current-snapshot only unless the payload has independently verifiable source dates. It is not trusted for historical backfill. |
| Lanka Business Online | Financial/business news | Uses the public WordPress REST API. |
| World Bank | USD/LKR and macro indicators | Annual indicators are forward-filled when joined to daily market rows. |
| stooq.com | Global index levels | Used for S&P 500, Nikkei 225, and Hang Seng daily closes. |
| CBSL | Interest rate target source | Automated ingestion still needs source freshness reporting before publication. |

This project is not affiliated with or endorsed by the Colombo Stock Exchange or any listed data source.
