# CSE Source Recon Ranked Report

Generated: 2026-05-31T05:11:09.982901+00:00
Environment: local
Forward gap under review: 2026-01-01 to current date

| Rank | Source | Family | Probe | Status | Score | Evidence | Observed dates | Reason |
|---:|---|---|---|---|---:|---|---|---|
| 1 | `tradingview_comb_browser` | ohlcv | browser | quarantine | 3.80 | CSE, screenshot | 25.04.2022 | OHLCV candidate lacks verifiable row/report dates |
| 2 | `investing_aspi_historical_browser` | indices | browser | rejected | 3.80 | CSE, screenshot | none | HTTP status 403 |

## Guardrails

- OHLCV sources are not accepted unless row-level or report-level dates are visible.
- Current snapshot APIs are quarantined for historical backfill even when they are official.
- Browser-scraped sources require screenshots and selector evidence before they can be recommended.
- A 2026 OHLCV adapter still needs cross-source validation near 2025-12-31 before ingestion.
