# CSE Source Recon Ranked Report

Generated: 2026-05-31T05:08:43.161789+00:00
Environment: local
Forward gap under review: 2026-01-01 to current date

| Rank | Source | Family | Probe | Status | Score | Evidence | Reason |
|---:|---|---|---|---|---:|---|---|
| 1 | `tradingview_comb_browser` | ohlcv | browser | quarantine | 4.80 | CSE, 2026, screenshot | OHLCV candidate lacks verifiable row/report dates |
| 2 | `investing_aspi_historical_browser` | indices | browser | rejected | 4.80 | CSE, 2026, screenshot | HTTP status 403 |

## Guardrails

- OHLCV sources are not accepted unless row-level or report-level dates are visible.
- Current snapshot APIs are quarantined for historical backfill even when they are official.
- Browser-scraped sources require screenshots and selector evidence before they can be recommended.
- A 2026 OHLCV adapter still needs cross-source validation near 2025-12-31 before ingestion.
