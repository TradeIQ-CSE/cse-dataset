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

**Historical index backfill (ASPI, S&P SL20, TRI series):**
```bash
uv run python scripts/convert_historical_indices.py --dry-run
```

**Daily index collection (defaults to the current Colombo date):**
```bash
uv run python scripts/daily_indices_update.py --target-date 2026-09-08
```

**2026 ASPI gap fill from the trailing chart window:**
```bash
uv run python scripts/backfill_2026_indices.py --dry-run
```

**Validate a release artifact (zip or directory) against contract v1:**
```bash
uv run python scripts/validate_artifact.py tests/fixtures/artifact/valid
```

**Build a release artifact (after backfilling every file in `config/release_sources.txt`):**
```bash
uv run python scripts/fill_missing_metadata.py
uv run python scripts/convert_historical_indices.py --allow-validation-failure
uv run python scripts/build_release.py   # writes data/published/
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

### Distinct Data Paths

The codebase separates ingestion into non-overlapping paths. Do not mix them:

| Path | Script | Source | Date coverage |
|---|---|---|---|
| Current-day | `scripts/daily_update.py` → `scripts/02_collect_prices.py` | CSE `tradeSummary` API | Today only |
| Historical backfill | `scripts/backfill_ohlcv.py` | Official CSE workbooks/CSVs | Through 2025-12-31 |
| 2026-forward | `scripts/2026_forward_update.py` | CSE daily PDF reports, Yahoo Finance (candidate) | 2026-01-01 onward |

Indices are collected on their own two paths, for the same reason: the official
workbook spans decades, the API serves only the settled day.

| Path | Script | Source | Date coverage |
|---|---|---|---|
| Index archive | `scripts/convert_historical_indices.py` | Official CSE index/TRI workbooks | Through 2025-12-31 |
| Index daily | `scripts/daily_indices_update.py` | CSE `dailyMarketSummery` API | 2026-01-01 onward |
| Index 2026 gap fill | `scripts/backfill_2026_indices.py` | CSE `chartData` trailing window | ASPI only, partial |

### Core Modules

- **`scripts/ohlcv_sources.py`** — Source adapter base class (`OHLCVSourceAdapter`), `FetchResult` dataclass, and `CSETradeSummaryCurrentAdapter`. All adapters must separate fetching, normalization, and source-date validation. `tradeSummary` serves the latest session whatever date is asked for, so its snapshot is dated by the rows' `lastTradedTime`; a run of `02_collect_prices.py` without `--target-date` files it under that session.
- **`scripts/ohlcv_validation.py`** — `validate_ohlcv_records()`, `ValidationResult`, and `write_validation_outputs()`. This is the central gate: records are split into `accepted` / `rejected` DataFrames. Contains all validation logic: source/date matching, duplicate detection, OHLC bounds repair, missing-activity thresholds, metadata symbol checks, listing-date checks, and stale-digest detection.
- **`scripts/indices_sources.py`** — `CSEDailyMarketSummaryIndicesAdapter` for the `indices` family. `dailyMarketSummery` ignores a `date` form field and always answers with the settled day, but it stamps the payload with its own `tradeDate`, so the observed date is read from the response and a mismatch quarantines instead of stamping.
- **`scripts/convert_historical_indices.py`** — official index workbook loader. The daily index workbook restarts its header mid-file for the GICS sector switch, so it is walked in segments; unlabelled columns are skipped, never guessed at.
- **`scripts/backfill_2026_indices.py`** — one-shot 2026 ASPI gap fill. `chartData` mixes settled closes with points stamped before the open; only the settled ones reproduce the official archive, so points are kept only when stamped at or after the 14:30 close, and the run aborts unless every point overlapping the archive matches it exactly. ASPI only — other `chartId` values return an empty list.
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

### Canonical Index Schema

Defined in `data/schemas/indices.schema.json`, matching the `indices`
`FamilyContract` in `forward_ingestion.py`:

```
date, index_name, close, source, source_timestamp, raw_payload_hash
```

Index codes: `ASPI`, `SL20`, `SL20TRI`, `ASTRI`, `MPI`, `MTRI`. Indices are
close-only in every official source. A series the exchange does not publish on
a date produces no row — never a zero close, which is how the frozen
post-discontinuation Milanka values are kept out.

### Release Artifact Contract

`docs/contracts/dataset-artifact-v1.md` is the interface to `tradeiq_cse`: a flat
zip of `manifest.json`, `company_metadata.csv`, `trading_calendar.csv`,
`daily_ohlcv.csv`, `indices.csv`, `index_values.csv` and optional `sectors.csv`,
with each file checksummed in the manifest. `scripts/validate_artifact.py`
implements it and stops at the first failure with a stable code. If the two
disagree, the document wins. The manifest schema is
`data/schemas/artifact_manifest.schema.json`.

The contract is stricter than the canonical outputs. Integers have no `.0`,
decimals have at most 4 dp, the only null is an empty field (never `Unknown`),
and dates are ISO. A publisher has to normalise values to these formats; it
must not relax the contract. A quarantined trading date stays in
`trading_calendar.csv` as a session with no prices, because an importer that
drops it treats the session as a holiday.

### Release Build

`scripts/build_release.py` makes the artifact. The trading calendar is every
date listed by the official price files in `config/release_sources.txt`. A
session is accepted or quarantined according to its per-date
`quality_summary.json`, and the build refuses when an accepted file disagrees
with its summary: a stale file from an earlier run must not ship. Every shipped
index series must have a value on every session. The build writes nothing
unless both the staging directory and the zip pass `validate_artifact`.

`scripts/fill_missing_metadata.py` adds metadata rows for traded symbols that
the active list no longer has, such as delisted companies and expired rights,
using `companyInfoSummery`. When the API doesn't know a symbol at all
(`CSEC.N0000` returns 404), it falls back to the SHORT NAME column of the
official price files. It never writes `listing_date`, because for rights and
preference lines `issueDate` is the company's date.

Sectors come from the committed `config/company_sectors.csv` and
`config/sectors.csv`. `scripts/collect_sectors.py` writes them from CSE's
`companyProfile` (each company's GICS industry group) and `allSectors` (the
groups and their codes); rerun it by hand when companies are added. The release
build never calls CSE for sectors.

`.github/workflows/release.yml` runs the whole chain on manual dispatch. With
`publish=false` it only builds and validates. With `publish=true` it creates a
`dataset-<version>` release, which fails if the tag already exists, and then
validates the downloaded asset.

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
- 2017-2025: closest to full OHLCV, but some rows have blanks. The 2017, 2018 and 2025 files, and the 2021 Q1 file, repeat the close in OPEN PRICE on every row, so `backfill_ohlcv.py` drops the open for them (`drop_copied_open`)

Backfill builds symbols like `AAF.N0000` from `COMPANY ID` + `MAIN TYPE` + `SUB TYPE`.

### 2026-Forward Families

Ten data families each with independent validation contracts in `forward_ingestion.py`:
`ohlcv`, `indices`, `market_stats`, `corporate_actions`, `listings`, `public_holdings`, `foreign_holdings`, `sector_gics`, `news_announcements`, `macro_rates`

### CI Workflow

`.github/workflows/daily_update.yml` runs weekdays at 12:30 UTC (18:00 SLST). CSE closes at 14:30 SLST, but `dailyMarketSummery` does not settle the current trading day immediately — observed still serving the previous day at 15:55 SLST and rolled over by 17:10 — so the earlier 14:45 slot quarantined every index run. `tradeSummary` still reports the same trading day at 18:00.

It smoke-checks, runs unit tests, runs daily OHLCV validation, the 2026-forward summary, and the daily index collection, all with `--allow-validation-failure`, then probes `cdn.cse.lk` reachability. Since `29314eb` the audit log is **not** committed back to `main` (`permissions: contents: read`); `docs/daily_ohlcv_runs.jsonl` is frozen at 2026-06-16 by design. Each run's capture (raw payloads, validation outputs, accepted files, run record) is uploaded as a 90-day artifact, then a separate `save` job, the only one with `contents: write`, commits it to the `captures` branch as `<session>/<run id>/`; that branch's README describes the layout. Generated data artifacts are **not** committed to `main`.

### Recovery Order

1. Prove daily OHLCV updater over several real runs
2. Convert and validate official historical files through 2025-12-31
3. Fill 2026-01-01 onward through `scripts/2026_forward_update.py`
4. Validate representative backfill batches against independent sources
5. Regenerate derived features, macro joins, sentiment, and published artifacts
6. Re-enable Kaggle/Hugging Face publishing only after validation gates run before publish
