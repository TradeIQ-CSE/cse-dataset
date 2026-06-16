# CSE Dataset v2 Recovery Plan

## Objective

Rebuild the project from a clean history, discard generated artifacts from the
previous repository, and recover the data pipeline in validation-first order.

## Priority Order

1. Keep only code, docs, dependency files, source schemas, and empty data
   directories.
2. Prove a daily-only OHLCV updater that never stamps requested dates onto
   unverified payloads.
3. Run source-specific OHLCV validation before writing accepted artifacts.
4. Rebuild processed OHLCV only from accepted raw payload transactions.
5. Build historical backfill separately from date-bearing sources after daily
   validation is proven.
6. Fill the `2026-01-01` to current-date gap from date-bearing official
   daily/periodic sources, then keep recoverable families updated forward.
7. Re-enable secondary pipelines and publishing only after validation gates pass.

## Priority: 2026 Gap Fill And Forward Updates

- Treat the received official historical files as coverage through
  `2025-12-31`; 2026 onward is not filled by workbook conversion.
- Backfill `2026-01-01` through the current date only from sources with
  verifiable source/report dates, preferably official CSE daily market
  reports/PDFs or a future official date-bearing CSE endpoint.
- Keep `tradeSummary` as current-day only. Never use it to backfill old dates
  unless CSE adds trustworthy per-row/source dates.
- Use the separate `scripts/2026_forward_update.py` path for 2026-forward
  ingestion. It stores raw payloads under `data/raw/2026_forward/`, candidate
  rows under `data/processed/2026_forward/`, and validation/quarantine reports
  under `data/processed/validation/2026_forward/`.
- The OHLCV forward path defaults to the official CSE StockMarketDaily PDF
  URL pattern
  `https://cdn.cse.lk/cse-daily/StockMarketDaily%28SMD%29DD-MM-YYYY.pdf`.
  Missing, private, or unpublished reports are quarantined rather than reused
  or silently ignored.
- Build family-specific source scrapers for OHLCV, indices, market stats,
  corporate actions, listings/de-listings, public/foreign holdings,
  sector/GICS data, news/announcements, and macro/rates as official sources are
  confirmed.
- Require each family to pass its own validation contract before accepted rows
  are written. Contracts cover required fields, report/source date, row date
  parsing, duplicate identity keys, numeric ranges, and family-specific bounds
  such as holdings percentages not exceeding 100.
- Store every fetched file or payload with source URL, fetch time, source date
  or report date, payload hash, row count, and validation status.
- Normalize raw sources into candidate tables first. Append accepted rows only
  after source-specific validation passes; quarantine date mismatches, missing
  reports, parser failures, and cross-source disagreements.
- Run the daily forward workflow after CSE market close in Sri Lanka, with
  weekly/monthly jobs added for non-daily families as their scrapers are built.

## 2026-Forward Family Contracts

| Family | Minimum identity | Validation focus | Schedule |
|---|---|---|---|
| `ohlcv` | date + symbol | source date, OHLC bounds, duplicate rows, metadata/listing checks, stale digest | Daily after market close |
| `indices` | date + index name | report date, non-negative close, duplicate index/date | Daily |
| `market_stats` | date + metric | report date, non-negative numeric values, duplicate metric/date | Daily/monthly |
| `corporate_actions` | announcement date + symbol + event type | report date, required event dates/symbol/type, non-negative amounts | Daily/monthly |
| `listings` | event date + symbol + event type | report date, expected listing/de-listing event types | Monthly or as published |
| `public_holdings` | report date + symbol | report date, holding percentage range 0-100, duplicate symbol/date | Quarterly |
| `foreign_holdings` | report date + symbol | report date, holding percentage range 0-100, duplicate symbol/date | Annual/monthly as source allows |
| `sector_gics` | date + sector + metric | report date, non-negative metrics, duplicate sector/date/metric | Daily/monthly |
| `news_announcements` | published date + symbol + title | published date, title presence, duplicate announcement identity | Daily |
| `macro_rates` | date + metric + currency | report date, non-negative exchange/rate values, duplicate metric/date | Daily/periodic |

## Historical OHLCV Backfill

- Use `scripts/backfill_ohlcv.py` for official CSE daily share price files
  through `2025-12-31`.
- The backfill path is separate from `tradeSummary` and from 2026-forward daily
  report ingestion.
- Each trading date is validated as a batch. A date with any rejected row is
  quarantined and not appended to accepted OHLCV transactions.
- Accepted rows are written under `data/raw/ohlcv/accepted/<date>/`, and
  `scripts/02b_merge_data.py` rebuilds processed OHLCV only from those accepted
  transactions.
- Missing-field repair is handled by `scripts/repair_ohlcv_missing.py`. Repairs
  may only fill missing OHLC fields and must match every non-missing official
  value from the quarantined CSE row before the full date batch can be accepted.

## Current Guardrails

- `tradeSummary` is current-snapshot only unless independently date-verified.
- Live checks on `2026-05-31` reconfirmed that `tradeSummary` and
  `dailyMarketSummery` ignore historical requested dates and return latest
  payloads, so neither may fill the 2026 gap.
- Historical workbook backfill is separate from 2026-forward ingestion.
- Publishing to Kaggle/Hugging Face is disabled.
- CI does not commit generated data.
- Generated raw, processed, and published artifacts are ignored.

## Publication Gates

Publishing remains disabled until:

- A representative 2026 gap-fill sample passes validation.
- Daily forward OHLCV passes for several real market days.
- Supporting dataset scrapers produce validation reports without silent stale
  data reuse.
