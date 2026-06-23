# CSE Source Recon Ranked Report

Generated: 2026-05-31T05:01:02.976360+00:00
Environment: local
Forward gap under review: 2026-01-01 to current date

| Rank | Source | Family | Probe | Status | Score | Evidence | Reason |
|---:|---|---|---|---|---:|---|---|
| 1 | `world_bank_lka_interest_rate` | macro_rates | api | accepted_candidate | 12.60 | CSE, 2026, dates, row dates, fields | date-bearing source for non-OHLCV family |
| 2 | `lbo_wordpress_posts` | news_announcements | api | accepted_candidate | 10.40 | CSE, 2026, dates, row dates | date-bearing source for non-OHLCV family |
| 3 | `cbsl_exchange_rates_html` | macro_rates | html | accepted_candidate | 10.40 | CSE, 2026, dates, fields | date-bearing source for non-OHLCV family |
| 4 | `marketscreener_comb_html` | ohlcv | html | accepted_candidate | 9.60 | CSE, 2026, dates, fields | date-bearing OHLCV source worth an ingestion adapter |
| 5 | `daily_mirror_business_html` | news_announcements | html | accepted_candidate | 9.60 | CSE, 2026, dates, fields | date-bearing source for non-OHLCV family |
| 6 | `cse_trade_summary_current` | ohlcv | api | quarantine | 8.00 | CSE, fields | current snapshot only; cannot fill historical 2026 gap |
| 7 | `cse_chart_data_aspi` | indices | api | quarantine | 8.00 | CSE, 2026 | reachable but unverifiable or structurally unclear |
| 8 | `cse_daily_report_cdn_pdf` | ohlcv | download | rejected | 6.00 | none | HTTP status 403 |
| 9 | `cse_announcements_api` | news_announcements | api | rejected | 6.00 | none | HTTP status 404 |
| 10 | `yahoo_comb_chart` | ohlcv | api | rejected | 5.80 | 2026 | no observed CSE or Sri Lanka coverage |
| 11 | `stooq_comb_csv` | ohlcv | download | rejected | 5.80 | 2026 | no observed CSE or Sri Lanka coverage |
| 12 | `eodhd_comb_demo` | ohlcv | api | rejected | 5.20 | none | HTTP status 403 |
| 13 | `nasdaq_data_link_search` | ohlcv | api | rejected | 5.20 | none | HTTP status 403 |
| 14 | `cse_all_stock_current` | ohlcv | api | rejected | 5.00 | none | HTTP status 404 |
| 15 | `alpha_vantage_symbol_search` | ohlcv | api | rejected | 1.70 | none | requires unset API key ALPHAVANTAGE_API_KEY |
| 16 | `finnhub_exchange_symbols` | listings | api | rejected | 1.70 | none | requires unset API key FINNHUB_API_KEY |
| 17 | `dailyft_business_html` | news_announcements | html | rejected | 1.70 | none | HTTPSConnectionPool(host='www.ft.lk', port=443): Read timed out. (read timeout=8) |
| 18 | `tradingview_comb_browser` | ohlcv | browser | rejected | 1.20 | none | BrowserType.launch: Executable doesn't exist at /home/nimesh/.cache/ms-playwright/chromium_headless_shell-1148/chrome-linux/headless_shell
╔════════════════════════════════════════════════════════════╗
║ Looks like Playwright was just installed or updated.       ║
║ Please run the following command to download new browsers: ║
║                                                            ║
║     playwright install                                     ║
║                                                            ║
║ <3 Playwright Team                                         ║
╚════════════════════════════════════════════════════════════╝ |
| 19 | `investing_aspi_historical_browser` | indices | browser | rejected | 1.20 | none | BrowserType.launch: Executable doesn't exist at /home/nimesh/.cache/ms-playwright/chromium_headless_shell-1148/chrome-linux/headless_shell
╔════════════════════════════════════════════════════════════╗
║ Looks like Playwright was just installed or updated.       ║
║ Please run the following command to download new browsers: ║
║                                                            ║
║     playwright install                                     ║
║                                                            ║
║ <3 Playwright Team                                         ║
╚════════════════════════════════════════════════════════════╝ |

## Guardrails

- OHLCV sources are not accepted unless row-level or report-level dates are visible.
- Current snapshot APIs are quarantined for historical backfill even when they are official.
- Browser-scraped sources require screenshots and selector evidence before they can be recommended.
- A 2026 OHLCV adapter still needs cross-source validation near 2025-12-31 before ingestion.
