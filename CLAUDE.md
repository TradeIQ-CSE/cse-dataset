# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a validation-first data recovery pipeline for Colombo Stock Exchange (CSE) OHLCV data. The project is in active recovery — historical artifacts from a previous repository are treated as invalid until regenerated from validated source payloads. Publishing to Kaggle/Hugging Face is currently **disabled**.

**Critical invariant:** The CSE `tradeSummary` API endpoint is a current-day snapshot only. It must never be used to backfill historical dates. A row is accepted only when the observable source date matches the target date and all validation gates pass.

## Setup

```bash
uv sync
uv run python scripts/smoke_check.py
uv run python -m unittest discover -s tests
```

Requires Python 3.11 exactly (`>=3.11,<3.12`). The project uses `uv` for dependency management.

## Running Tests

```bash
# All tests
uv run python -m unittest discover -s tests

# Single test file
uv run python -m unittest tests.test_ohlcv_validation
```

## Key Commands

**Daily OHLCV (dry-run, no artifacts written):**
```bash
uv run python scripts/daily_update.py --dry-run --allow-missing-metadata
```

**Daily OHLCV (write accepted artifacts):**
```bash
uv run python scripts/daily_update.py
```

**Historical backfill from a CSE official price file:**
```bash
uv run python scripts/backfill_ohlcv.py \
  --source-path 'historical_data/csv/stock_data/33Daily Shares Price List -2021-2025/2025_Data__HighLow.csv' \
  --allow-validation-failure
uv run python scripts/02b_merge_data.py
```

**2026-forward gap fill (single date from local PDF):**
```bash
uv run python scripts/2026_forward_update.py \
  --target-date 2026-05-29 \
  --source-file path/to/cse-daily-report.pdf
```

**2026-forward gap fill (date range, official CSE PDF URL pattern):**
```bash
uv run python scripts/2026_forward_update.py \
  --start-date 2026-01-01 --end-date 2026-05-29 --allow-validation-failure
```

**Source reconnaissance (before building a new adapter):**
```bash
uv run python scripts/source_recon.py --target-date 2026-05-29
# Playwright browser probes require: uv run playwright install chromium
```

**Export missing OHLC repair targets:**
```bash
uv run python scripts/repair_ohlcv_missing.py export-targets \
  --output data/processed/validation/ohlcv_repairs/missing_ohlcv_targets.csv
```

## Architecture

### Three Distinct Data Paths

The codebase separates ingestion into three non-overlapping paths. Do not mix them:

| Path | Script | Source | Date coverage |
|---|---|---|---|
| Current-day | `scripts/daily_update.py` → `scripts/02_collect_prices.py` | CSE `tradeSummary` API | Today only |
| Historical backfill | `scripts/backfill_ohlcv.py` | Official CSE workbooks/CSVs | Through 2025-12-31 |
| 2026-forward | `scripts/2026_forward_update.py` | CSE daily PDF reports, Yahoo Finance (candidate) | 2026-01-01 onward |

### Core Modules

- **`scripts/ohlcv_sources.py`** — Source adapter base class (`OHLCVSourceAdapter`), `FetchResult` dataclass, and `CSETradeSummaryCurrentAdapter`. All adapters must separate fetching, normalization, and source-date validation.
- **`scripts/ohlcv_validation.py`** — `validate_ohlcv_records()`, `ValidationResult`, and `write_validation_outputs()`. This is the central gate: records are split into `accepted` / `rejected` DataFrames. Contains all validation logic: source/date matching, duplicate detection, OHLC bounds repair, missing-activity thresholds, metadata symbol checks, listing-date checks, and stale-digest detection.
- **`scripts/forward_ingestion.py`** — 2026-forward family ingestion engine: PDF parsing, Yahoo Finance adapter, and `run_daily_report_ohlcv_ingestion()` / `run_generic_family_ingestion()`. Defines `DATASET_FAMILIES` and the default CSE PDF URL template.
- **`scripts/backfill_ohlcv.py`** — Converts official historical workbooks (grouped multi-symbol XLS/CSV format) into canonical OHLCV candidates, validates per-date batch, and writes accepted transactions.

### Data Directory Layout

```
data/
  raw/
    ohlcv/
      source_payloads/      # immutable raw API payloads (current-day)
      accepted/<date>/      # accepted canonical OHLCV CSVs
      manifests/            # per-run fetch manifests with payload hash
      historical_source_payloads/  # backfill raw payloads
    2026_forward/           # immutable raw 2026-forward payloads
  processed/
    all_stocks_merged.parquet   # built only from accepted raw transactions
    company_metadata.csv
    validation/
      ohlcv/                # quality_summary.json, validation_report.md, rejected_records.csv
      2026_forward/         # quarantine summaries per family
    2026_forward/           # candidate tables before acceptance
    ohlcv_backfill/candidates/
  recon/<run-id>/           # source recon artifacts (not committed)
```

### Canonical OHLCV Schema

Defined in `data/schemas/ohlcv.schema.json`. Required columns:

```
date, symbol, open, high, low, close, volume, turnover, trades,
source, source_priority, source_timestamp, raw_payload_hash,
validation_status, validation_warnings
```

`source_timestamp` must equal `target_date`; this is what enforces the current-snapshot-only constraint. Optional columns added by validation: `source_open/high/low/close`, `source_ohlc_invalid`, `ohlc_repaired`, `ohlc_invalid`.

### Validation Gates

`validate_ohlcv_records()` runs these checks in order:
1. Source/date match — `record.date == target_date` and `source_timestamp == target_date`
2. Duplicate `(symbol, date)` pairs
3. Missing OHLC prices (→ rejected row)
4. Negative OHLC prices (→ `ohlc_invalid`)
5. OHLC bounds repair — `high` and `low` are corrected when violated; original values saved in `source_*` columns
6. Missing activity fields (`volume`, `turnover`, `trades`) — configurable threshold
7. Symbol metadata validation — symbol must exist in `company_metadata.csv`
8. Listing-date validation — price date must not predate the company's listing date
9. Stale-digest detection — full-market SHA-256 digest must not repeat across different trading days

### Historical Data Archive

`historical_data/` contains official CSE source files (XLS, XLSX) and converted CSVs. Daily price schema changed over time:

- 1990-2000: close-only
- 2001: close + some volume
- 2002-2015: high/low/close/volume/turnover/trades (no open)
- 2016: high/low/close (no open)
- 2017-2025: closest to full OHLCV, but some rows have blanks

Backfill builds symbols like `AAF.N0000` from `COMPANY ID` + `MAIN TYPE` + `SUB TYPE`.

### 2026-Forward Families

Ten data families each with independent validation contracts in `forward_ingestion.py`:
`ohlcv`, `indices`, `market_stats`, `corporate_actions`, `listings`, `public_holdings`, `foreign_holdings`, `sector_gics`, `news_announcements`, `macro_rates`

### CI Workflow

`.github/workflows/daily_update.yml` runs weekdays at 09:15 UTC (after CSE market close at 14:30 SLST). It smoke-checks, runs unit tests, runs daily OHLCV validation with `--allow-validation-failure`, records the audit log to `docs/daily_ohlcv_runs.jsonl` and `docs/daily_ohlcv_runs.md`, and commits those audit files to `main`. Generated data artifacts are **not** committed to git.

### Recovery Order

1. Prove daily OHLCV updater over several real runs
2. Convert and validate official historical files through 2025-12-31
3. Fill 2026-01-01 onward through `scripts/2026_forward_update.py`
4. Validate representative backfill batches against independent sources
5. Regenerate derived features, macro joins, sentiment, and published artifacts
6. Re-enable Kaggle/Hugging Face publishing only after validation gates run before publish
