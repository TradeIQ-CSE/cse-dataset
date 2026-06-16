# CSE Source Recon Ranked Report

Generated: 2026-05-31T06:04:32.136154+00:00
Environment: local
Forward gap under review: 2026-01-01 to current date

| Rank | Source | Family | Probe | Status | Score | Evidence | Observed dates | Reason |
|---:|---|---|---|---|---:|---|---|---|
| 1 | `yahoo_comb_chart` | ohlcv | api | accepted_candidate | 12.70 | CSE, 2026, dates, row dates, fields | 2025-12-31, 2026-01-02, 2026-01-05, 2026-01-06, 2026-01-07 | date-bearing OHLCV source worth an ingestion adapter |
| 2 | `cse_trade_summary_current` | ohlcv | api | quarantine | 11.60 | CSE, 2026, dates, row dates, fields | 2026-05-29 | current snapshot only; cannot fill historical 2026 gap |
| 3 | `cse_today_share_price_current` | ohlcv | api | quarantine | 11.60 | CSE, 2026, dates, row dates, fields | 2026-05-29 | current snapshot only; cannot fill historical 2026 gap |
| 4 | `cse_daily_market_summary_current` | market_stats | api | quarantine | 11.60 | CSE, 2026, dates, row dates, fields | 2026-05-26, 2026-05-28 | current snapshot only; cannot fill historical 2026 gap |
| 5 | `cse_detailed_trades_current` | market_stats | api | quarantine | 8.00 | CSE, fields | none | current snapshot only; cannot fill historical 2026 gap |

## Guardrails

- OHLCV sources are not accepted unless row-level or report-level dates are visible.
- Current snapshot APIs are quarantined for historical backfill even when they are official.
- Browser-scraped sources require screenshots and selector evidence before they can be recommended.
- A 2026 OHLCV adapter still needs cross-source validation near 2025-12-31 before ingestion.
