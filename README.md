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
- Immutable raw payload metadata with requested date, observed source date,
  fetch time, source URL, payload hash, row count, and validation status.
- Canonical OHLCV schema under `data/schemas/ohlcv.schema.json`.
- Source/date, duplicate, OHLC bound, missing activity, listing-date, metadata,
  and repeated-digest validation gates.
- CI smoke checks, unit tests, and daily OHLCV dry-run validation.

Disabled until validation is proven:

- Historical OHLCV backfill.
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
2. Add a separate historical backfill pipeline using only date-bearing sources.
3. Validate representative backfill batches against independent sources.
4. Regenerate derived features, macro joins, sentiment, and published artifacts.
5. Re-enable publishing only after validation gates run before publish.

## License

MIT License. Data is sourced from public websites/APIs. This project is not
affiliated with the Colombo Stock Exchange.
