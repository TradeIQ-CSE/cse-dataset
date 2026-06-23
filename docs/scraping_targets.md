# Scraping Targets

*Recovery status updated 2026-05-31.*

## OHLCV

Official file coverage received for historical recovery is treated as ending on
`2025-12-31`. The `2026-01-01` to current-date gap must be filled through the
separate 2026-forward ingestion path, not through historical workbook
conversion and not through stale current snapshots.

The CSE `tradeSummary` endpoint remains useful for daily current-snapshot
collection, but it is **not validated as a historical API**. The recovery
pipeline must not iterate over old dates and assign the requested date to the
response.

Current rule:

- Use `https://www.cse.lk/api/tradeSummary` only for same-day/current-trading-day
  collection.
- Accept payloads only when the observed source date equals the target date.
- Store the raw payload and metadata before accepting rows.
- Reject repeated full-market digests across different target dates unless an
  independent date-bearing source confirms the market was unchanged.

Historical backfill candidates:

- Official CSE daily market summary PDFs with parsed report date and symbol rows.
- Any official CSE API discovered later that returns date-bearing OHLCV rows.
- External/open datasets only when provenance, date coverage, and cross-source
  checks are documented.
- Recon-only candidates are tracked through `scripts/source_recon.py`, which
  writes raw samples, screenshots, parsed evidence, and a ranked matrix under
  `data/recon/` without touching accepted dataset artifacts.
- Yahoo Finance chart URLs using `SYMBOL-SHARETYPE.CM` are the first
  date-bearing third-party OHLCV candidate found for the 2026-forward gap.
  A `2026-05-31` coverage audit found 280/308 local symbols with 2026 bars, but
  240 reachable symbols had at least one invalid OHLC-bound row. Yahoo data must
  therefore be treated as candidate input only, with row-level validation and
  official same-day cross-checks before acceptance.
- The guarded Yahoo adapter rejects missing/non-positive volume rows and
  same-day close mismatches against official CSE `tradeSummary`. A sample run
  for `2026-05-29` rejected all sampled rows and wrote no accepted artifact.

2026-forward implementation status:

- `scripts/2026_forward_update.py` accepts a local report file or official URL
  template and requires a single parsed report date before OHLCV rows can be
  accepted.
- If no OHLCV source is supplied, the script attempts the official CSE CDN
  pattern `https://cdn.cse.lk/cse-daily/StockMarketDaily%28SMD%29DD-MM-YYYY.pdf`.
  Missing, private, or unpublished reports are quarantined instead of failing
  CI when `--allow-validation-failure` is used.
- A live check on `2026-05-31` confirmed that `tradeSummary` and
  `dailyMarketSummery` still ignore historical requested dates and return the
  latest payload, so they remain forbidden for gap backfill.
- The same script accepts `--family` for `indices`, `market_stats`,
  `corporate_actions`, `listings`, `public_holdings`, `foreign_holdings`,
  `sector_gics`, `news_announcements`, and `macro_rates`. Each family is
  contract-validated before accepted rows are written.
- Raw payloads, candidate rows, validation reports, accepted rows, and
  quarantine summaries are kept under `data/raw/2026_forward/`,
  `data/processed/2026_forward/`, and
  `data/processed/validation/2026_forward/`.
- Missing reports, parser failures, report-date mismatches, and unconfigured
  non-OHLCV family scrapers write validation summaries instead of silently
  publishing or reusing stale files.

Other recoverable families to add as official date-bearing sources are
confirmed:

- Indices and market statistics.
- Corporate actions and listings/de-listings.
- Public holdings, foreign holdings, sector/GICS data.
- News/announcements.
- Macro/rates.

Current non-OHLCV guardrails:

- No configured source means a quarantine summary, not an empty accepted file.
- A source/report-date mismatch fails the whole batch.
- Duplicate family identity keys are rejected.
- Negative numeric rates/statistics and holding percentages above 100 are
  rejected.
- Parser failures write raw payload metadata when a payload was fetched.

## News

Lanka Business Online exposes a WordPress REST API:

- `https://www.lankabusinessonline.com/wp-json/wp/v2/posts`

CSE announcements can be used only when announcement dates are preserved through
the sentiment pipeline.

## Annual Reports

Annual report links should come from official CSE company financial report pages.
PDF parsing remains a separate fundamentals task and must not affect OHLCV
acceptance.
