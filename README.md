# cse-dataset-v2

Recovery branch for a Colombo Stock Exchange dataset pipeline.

**Status:** clean-history rebuild in progress. Generated raw, processed, and
published artifacts from the previous repository are treated as invalid until
they are regenerated from validated source payloads.

## Critical Source Warning

The CSE `tradeSummary` endpoint is **not currently trusted as a historical
source**. Current evidence shows it behaves as a current/current-trading-day
snapshot unless a response includes independently verifiable source dates.

The daily OHLCV collector therefore refuses to stamp requested historical dates
onto payloads. A row is accepted only when the observable source date matches
the target date and the validation gates pass.

## Current Scope

Enabled now:

- Daily-only OHLCV collection via source adapters.
- Separate 2026-forward ingestion via date-bearing source files or URL
  templates.
- Per-family 2026-forward validation contracts for indices, market statistics,
  corporate actions, listings, holdings, sector/GICS data, news/announcements,
  and macro/rates.
- Immutable raw payload metadata with requested date, observed source date,
  fetch time, source URL, payload hash, row count, and validation status.
- Canonical OHLCV schema under `data/schemas/ohlcv.schema.json`.
- Source/date, duplicate, OHLC bound, missing activity, listing-date, metadata,
  and repeated-digest validation gates.
- CI smoke checks, unit tests, and daily OHLCV dry-run validation.

Disabled until validation is proven:

- Publication of historical and 2026-forward backfill rows.
- Kaggle and Hugging Face publishing.
- CI commits of generated data artifacts.
- Claims about generated coverage or production-ready published datasets.

## Setup

```bash
uv sync
uv run python scripts/smoke_check.py
uv run python -m unittest discover -s tests
```

## Daily OHLCV

Dry-run the source/date validation without writing artifacts:

```bash
uv run python scripts/daily_update.py --dry-run --allow-missing-metadata
```

Run the daily updater and write accepted artifacts:

```bash
uv run python scripts/daily_update.py
```

The non-dry-run path rebuilds company metadata first, validates the daily
payload, writes accepted canonical rows under `data/raw/ohlcv/accepted/`, and
builds `data/processed/all_stocks_merged.parquet` only from accepted raw
transactions. It does not load or append stale processed Parquet data.

Validated current-day batches can be delivered to the TradeIQ market-data API
using `scripts/publish_eod.py`. Delivery is disabled by default; see
[docs/eod_platform_delivery.md](docs/eod_platform_delivery.md) for its calendar,
authentication, replay, and GitHub Actions requirements.

## Historical OHLCV Backfill

Backfill official CSE daily share price files through `2025-12-31` with
date-level validation:

```bash
uv run python scripts/backfill_ohlcv.py \
  --source-path 'historical_data/csv/stock_data/33Daily Shares Price List -2021-2025/2025_Data__HighLow.csv' \
  --allow-validation-failure
uv run python scripts/02b_merge_data.py
```

The backfill parser converts `COMPANY ID`, `MAIN TYPE`, and `SUB TYPE` into
CSE-style symbols such as `AAF.N0000`, validates one trading date at a time,
and writes accepted transactions only for dates whose full batch passes. Dates
with missing OHLC fields, bad prices, duplicate symbols, or other validation
failures are quarantined under `data/processed/validation/ohlcv/` and are not
merged.

Export the exact missing OHLC repair targets:

```bash
uv run python scripts/repair_ohlcv_missing.py export-targets \
  --output data/processed/validation/ohlcv_repairs/missing_ohlcv_targets.csv
```

Apply externally verified repair rows only after adding source metadata:

```bash
uv run python scripts/repair_ohlcv_missing.py apply \
  --repair-file path/to/verified_ohlcv_repairs.csv
uv run python scripts/02b_merge_data.py
```

Repair rows may only fill fields that are missing in the official CSE row.
Any alternate-source value for an already-present official field must match the
official value, otherwise the repair is rejected. This allows third-party or
manually scraped candidates to be checked without silently overwriting official
CSE data.

For the complete historical archive inventory, schema-era notes, and
interpretation rules, see
[docs/historical_data.md](docs/historical_data.md). The key constraint is that
official source files are not automatically uniform or canonical: early daily
price files are partial price history, converted CSVs are raw candidates, and
accepted OHLCV rows require validation.

## 2026-Forward Gap Fill

Official historical file coverage is treated as ending on `2025-12-31`.
Rows from `2026-01-01` onward must come through the separate 2026-forward
pipeline, using date-bearing sources that pass source-specific validation.

Before building a new adapter, run source reconnaissance and review the ranked
matrix:

```bash
uv run python scripts/source_recon.py --target-date 2026-05-29
```

This probes official and third-party candidates without writing accepted
dataset artifacts. The report is saved under `data/recon/<run-id>/`; see
[docs/source_recon.md](/home/nimesh/Documents/projects/cse_dataset/docs/source_recon.md)
for the output contract and recommendation rules.

Run a single local date-bearing daily report/PDF/CSV fixture:

```bash
uv run python scripts/2026_forward_update.py \
  --target-date 2026-05-29 \
  --source-file path/to/cse-daily-report.pdf
```

Run a gap-fill range using the default official CSE StockMarketDaily PDF URL
pattern:

```bash
uv run python scripts/2026_forward_update.py \
  --start-date 2026-01-01 \
  --end-date 2026-05-29 \
  --allow-validation-failure
```

The forward path writes raw payloads to `data/raw/2026_forward/`, candidate
tables to `data/processed/2026_forward/`, and validation/quarantine summaries
to `data/processed/validation/2026_forward/`. A missing report, parser failure,
or report-date mismatch is quarantined and never reused as accepted data.
The default CSE report URL pattern is
`https://cdn.cse.lk/cse-daily/StockMarketDaily%28SMD%29{dd}-{mm}-{yyyy}.pdf`;
override it with `--source-url-template` if CSE publishes a different
date-bearing source.

Run the Yahoo Finance candidate adapter with validation and official CSE
same-day cross-checking:

```bash
uv run python scripts/2026_forward_update.py \
  --family ohlcv \
  --ohlcv-source yahoo_finance_chart_candidate \
  --target-date 2026-05-29 \
  --missing-activity-threshold 1.0 \
  --allow-validation-failure
```

Yahoo rows are not trusted by default. The adapter rejects rows with missing or
non-positive volume, validates OHLC bounds and metadata, compares same-day rows
against the official CSE `tradeSummary` snapshot when dates match, and writes
accepted artifacts only if the whole target-date batch passes.

Run a non-OHLCV family from a date-bearing official CSV/JSON/PDF-derived table:

```bash
uv run python scripts/2026_forward_update.py \
  --family corporate_actions \
  --target-date 2026-05-29 \
  --source-file path/to/cse-corporate-actions.csv
```

Supported forward families are:

```text
ohlcv, indices, market_stats, corporate_actions, listings,
public_holdings, foreign_holdings, sector_gics,
news_announcements, macro_rates
```

Each family has a contract for required columns, date fields, duplicate keys,
numeric checks, and source/report-date matching. If a source does not satisfy
the contract, the run writes rejected rows and a quarantine summary instead of
publishing partial or stale data.

## Canonical OHLCV Schema

Required fields:

```text
date, symbol, open, high, low, close, volume, turnover, trades,
source, source_priority, source_timestamp, raw_payload_hash,
validation_status, validation_warnings
```

Validation outputs are written under `data/processed/validation/ohlcv/`:

- `quality_summary.json`
- `validation_report.md`
- `rejected_records.csv`

## Recovery Order

1. Prove the daily OHLCV updater over several real runs.
2. Convert and validate official historical files through `2025-12-31`.
3. Fill `2026-01-01` onward through `scripts/2026_forward_update.py`.
4. Validate representative backfill batches against independent sources.
5. Regenerate derived features, macro joins, sentiment, and published artifacts.
6. Re-enable publishing only after validation gates run before publish.

## License

MIT License. Data is sourced from public websites/APIs. This project is not
affiliated with the Colombo Stock Exchange.
