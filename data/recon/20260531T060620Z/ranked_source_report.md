# CSE Source Recon Ranked Report

Generated: 2026-05-31T06:06:45.321861+00:00
Environment: local
Forward gap under review: 2026-01-01 to current date

| Rank | Source | Family | Probe | Status | Score | Evidence | Observed dates | Reason |
|---:|---|---|---|---|---:|---|---|---|
| 1 | `yahoo_comb_chart` | ohlcv | api | accepted_candidate | 12.70 | CSE, 2026, dates, row dates, fields | 2025-12-31, 2026-01-02, 2026-01-05, 2026-01-06, 2026-01-07 | date-bearing OHLCV source worth an ingestion adapter |
| 2 | `world_bank_lka_interest_rate` | macro_rates | api | accepted_candidate | 12.60 | CSE, 2026, dates, row dates, fields | 2021, 2022, 2023, 2024, 2025 | date-bearing source for non-OHLCV family |
| 3 | `cse_trade_summary_current` | ohlcv | api | quarantine | 11.60 | CSE, 2026, dates, row dates, fields | 2026-05-29 | current snapshot only; cannot fill historical 2026 gap |
| 4 | `cse_today_share_price_current` | ohlcv | api | quarantine | 11.60 | CSE, 2026, dates, row dates, fields | 2026-05-29 | current snapshot only; cannot fill historical 2026 gap |
| 5 | `cse_daily_market_summary_current` | market_stats | api | quarantine | 11.60 | CSE, 2026, dates, row dates, fields | 2026-05-26, 2026-05-28 | current snapshot only; cannot fill historical 2026 gap |
| 6 | `cse_chart_data_aspi` | indices | api | accepted_candidate | 11.00 | CSE, dates, row dates, fields | 2025-06-02, 2025-06-03, 2025-06-04, 2025-06-05, 2025-06-06 | date-bearing source for non-OHLCV family |
| 7 | `lbo_wordpress_posts` | news_announcements | api | accepted_candidate | 10.40 | CSE, 2026, dates, row dates | 2026-05-29T06:30:10, 2026-05-29T10:29:07, 2026-05-29T10:39:29, 2026-05-29T19:09:08, 2026-05-30T08:51:57 | date-bearing source for non-OHLCV family |
| 8 | `cse_detailed_trades_current` | market_stats | api | quarantine | 8.00 | CSE, fields | none | current snapshot only; cannot fill historical 2026 gap |
| 9 | `marketscreener_comb_html` | ohlcv | html | quarantine | 7.60 | CSE, 2026, fields | 2013-08-27, 2013-12-31, 2014-12-31, 2023-05-01, 2023-07-31 | OHLCV candidate lacks verifiable row/report dates |
| 10 | `daily_mirror_business_html` | news_announcements | html | cross_check_only | 7.60 | CSE, 2026, fields | 04 Sep 2023, 27 May 2026, 28 May 2026, 29 May 2026, 31 March 2026 | useful fields observed but not enough for primary ingestion |
| 11 | `cbsl_exchange_rates_html` | macro_rates | html | cross_check_only | 7.40 | CSE, fields | 03.03.2023, 07.03.2023 | useful fields observed but not enough for primary ingestion |
| 12 | `cse_daily_report_cdn_pdf` | ohlcv | download | rejected | 6.00 | none | none | HTTP status 403 |
| 13 | `cse_announcements_api` | news_announcements | api | rejected | 6.00 | none | none | HTTP status 404 |
| 14 | `stooq_comb_csv` | ohlcv | download | rejected | 5.80 | none | none | no observed CSE or Sri Lanka coverage |
| 15 | `eodhd_comb_demo` | ohlcv | api | rejected | 5.20 | none | none | HTTP status 403 |
| 16 | `nasdaq_data_link_search` | ohlcv | api | rejected | 5.20 | none | none | HTTP status 403 |
| 17 | `cse_all_stock_current` | ohlcv | api | rejected | 5.00 | none | none | HTTP status 404 |
| 18 | `alpha_vantage_symbol_search` | ohlcv | api | rejected | 1.70 | none | none | requires unset API key ALPHAVANTAGE_API_KEY |
| 19 | `finnhub_exchange_symbols` | listings | api | rejected | 1.70 | none | none | requires unset API key FINNHUB_API_KEY |
| 20 | `dailyft_business_html` | news_announcements | html | rejected | 1.70 | none | none | HTTPSConnectionPool(host='www.ft.lk', port=443): Read timed out. (read timeout=15) |
| 21 | `tradingview_comb_browser` | ohlcv | browser | rejected | 1.20 | none | none | browser probes skipped by CLI |
| 22 | `investing_aspi_historical_browser` | indices | browser | rejected | 1.20 | none | none | browser probes skipped by CLI |

## Guardrails

- OHLCV sources are not accepted unless row-level or report-level dates are visible.
- Current snapshot APIs are quarantined for historical backfill even when they are official.
- Browser-scraped sources require screenshots and selector evidence before they can be recommended.
- A 2026 OHLCV adapter still needs cross-source validation near 2025-12-31 before ingestion.
