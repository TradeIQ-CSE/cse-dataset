# Dataset artifact contract v1

Contract version **1.0.0**. Producer: this repository. Consumer: the
`tradeiq_cse` importer (tradeiq_cse#31).

This document is the whole interface between the two repositories. An importer
built from it should not need to read any cse-dataset code.
`scripts/validate_artifact.py` implements the rules. If the script and this
document disagree, the document is right and the script has a bug.

## 1. The artifact

One zip archive named `cse-dataset-<dataset_version>.zip`. All files sit at the
archive root with no directories. The validator also accepts the same files
unpacked into a directory.

| File | Required | Loads into |
|---|---|---|
| `manifest.json` | yes | `ingestion_runs` metadata |
| `company_metadata.csv` | yes | `securities` |
| `trading_calendar.csv` | yes | `trading_calendar` |
| `daily_ohlcv.csv` | yes | `daily_prices` |
| `indices.csv` | yes | `indices` |
| `index_values.csv` | yes | `index_values` |
| `sectors.csv` | no | `sectors` |

The manifest lists every other file in the archive with its checksum. A file in
the archive that the manifest doesn't list makes the artifact invalid. A listed
file this contract doesn't name is allowed: it is checksummed but not parsed. A
later minor version may add optional files this way.

## 2. Columns

Every column below must be present in the header. **Null** means the field may
be empty. Columns are matched by name, so their order doesn't matter. Columns
this contract doesn't name are allowed, and importers ignore them.

### company_metadata.csv

One row per security that appears anywhere in the artifact.

| Column | Type | Null | Notes |
|---|---|---|---|
| `symbol` | symbol | no | Key |
| `company_name` | text | no | |
| `delisted` | boolean | no | |
| `listing_date` | date | yes | |
| `delisting_date` | date | yes | |
| `sector_code` | text | yes | GICS code. Must exist in `sectors.csv`. Always empty when `sectors.csv` isn't shipped. |
| `isin` | text | yes | |
| `shares_outstanding` | integer | yes | |
| `board` | text | yes | |

### trading_calendar.csv

One row per CSE trading session in the coverage window. A date in the window
that isn't listed was not a session. See §5.

| Column | Type | Null | Notes |
|---|---|---|---|
| `date` | date | no | Key |
| `ohlcv_status` | `accepted` or `quarantined` | no | |
| `ohlcv_rows` | integer | no | Number of `daily_ohlcv.csv` rows for this date |

### daily_ohlcv.csv

Accepted rows only. Every row has already passed this repository's validation
gates.

| Column | Type | Null | Notes |
|---|---|---|---|
| `date` | date | no | Key, part 1 |
| `symbol` | symbol | no | Key, part 2 |
| `open` | decimal | yes | Empty when the official file has no usable open: blank on some rows, and absent from whole years (§10) |
| `high` | decimal | no | |
| `low` | decimal | no | |
| `close` | decimal | no | |
| `volume` | integer | no | Shares traded |
| `turnover` | decimal | yes | LKR. There is no `daily_prices` column for it. |
| `trades` | integer | yes | There is no `daily_prices` column for it. |
| `source` | text | no | Name of the source adapter |
| `raw_payload_hash` | sha256 | no | Hash of the immutable raw source this row came from |

### indices.csv

| Column | Type | Null | Notes |
|---|---|---|---|
| `index_code` | index code | no | Key |
| `index_name` | text | no | Display name |
| `base_date` | date | yes | |

### index_values.csv

Close only: no official source publishes index open, high or low values. A
series that wasn't published on a session has no row for it. It never gets a
zero or a carried-forward value.

| Column | Type | Null | Notes |
|---|---|---|---|
| `date` | date | no | Key, part 1 |
| `index_code` | index code | no | Key, part 2 |
| `close` | decimal | no | |
| `source` | text | no | |
| `raw_payload_hash` | sha256 | no | |

### sectors.csv (optional)

| Column | Type | Null | Notes |
|---|---|---|---|
| `gics_code` | text | no | Key |
| `sector_name` | text | no | |

## 3. Encoding and value formats

- UTF-8 with no byte order mark, `\n` line endings, comma-delimited, RFC 4180
  quoting, and a header row as the first line.
- An empty field is the only way to write null. The strings `nan`, `none`,
  `null`, `n/a`, `-` and `unknown` (in any letter case) are refused in every
  column, as are values with leading or trailing whitespace.

| Type | Format |
|---|---|
| date | `YYYY-MM-DD`, and must be a real calendar date |
| decimal | `^\d+(\.\d{1,4})?$`: no sign, exponent or thousands separator, and at most 4 decimal places (the scale of `numeric(12,4)` / `numeric(14,4)`) |
| integer | `^\d+$`. `1000.0` is not an integer. |
| boolean | `true` or `false` |
| symbol | `^[A-Z0-9]+\.[A-Z][0-9]{4}$`, for example `COMB.N0000` |
| sha256 | 64 lowercase hex characters |
| index code | exactly one of `ASPI`, `SL20`, `SL20TRI`, `ASTRI`, `MPI`, `MTRI` |
| text | any non-empty value that follows the rules above |

Index codes are matched exactly. `SL20TRI` is a different series from `SL20`,
so an importer that matches by prefix or substring will corrupt SL20.

## 4. Keys, order and references

Each file is unique on its key and sorted ascending by it, comparing values as
strings one key column at a time. Sorting keeps the artifact byte-for-byte
reproducible from the same inputs.

Every reference must resolve inside the artifact:

- each `daily_ohlcv.csv` symbol is in `company_metadata.csv`
- each `daily_ohlcv.csv` and `index_values.csv` date is in `trading_calendar.csv`
- each `index_values.csv` code is in `indices.csv`
- each non-empty `sector_code` is in `sectors.csv`

## 5. Trading calendar and quarantine

This repository accepts or quarantines a trading date as a whole: one row that
fails validation quarantines every row for that date. A quarantined session is
still a real session. The exchange traded, but the artifact carries no prices
for it.

- `accepted`: `ohlcv_rows` is greater than 0 and equals the number of
  `daily_ohlcv.csv` rows for that date.
- `quarantined`: `ohlcv_rows` is 0 and `daily_ohlcv.csv` has no rows for that
  date. Index values for the date can still be present.

An importer must load quarantined sessions into `trading_calendar` as trading
days. If it drops them, a backtest will step over a session as if it were a
holiday.

## 6. Manifest

`manifest.json` is a JSON object. Its schema is
`data/schemas/artifact_manifest.schema.json`.

| Field | Required | Meaning |
|---|---|---|
| `contract_version` | yes | `MAJOR.MINOR.PATCH` of this contract |
| `dataset_version` | yes | See §7 |
| `kind` | yes | `full`, `incremental` or `correction` (§7) |
| `created_at` | yes | Build time, RFC 3339 UTC with a `Z` suffix |
| `source_commit` | yes | 40-hex commit of this repository that built the artifact |
| `coverage.start`, `coverage.end` | yes | First and last session in `trading_calendar.csv` |
| `files[]` | yes | `path`, `sha256` of the file's bytes, `rows` excluding the header. One entry for every file except the manifest itself. |
| `index_series[]` | yes | `index_code`, `first_date` and `last_date` for every code in `index_values.csv`, equal to its actual first and last rows |
| `quarantine.dates` | yes | Number of `quarantined` sessions in the calendar |
| `quarantine.rows` | yes | Source rows rejected during validation. For information only: those rows aren't in the artifact, so this can't be checked. |
| `known_gaps[]` | no | `file`, optional `index_code`, `start`, `end`, `reason`: a documented hole inside coverage, such as a series that stops early |
| `base_version` | incremental, correction | The release this one builds on |
| `corrected_dates[]` | correction | Every session whose content differs from `base_version` |

Importers ignore manifest fields they don't recognise.

## 7. Versioning

**`contract_version`** follows semver. An importer accepts any version with its
own major number and refuses every other major. A minor bump only adds optional
files, nullable columns or optional manifest fields, so a 1.0 importer can read
a 1.4 artifact. Anything else is a new major: removing or renaming something,
tightening a format, or changing what a value means.

**`dataset_version`** is `<coverage.end>.<revision>`, for example
`2025-12-31.1`. A published version is immutable, and the same content is always
published under the same version.

**`kind`**:

| Kind | Content | Rules |
|---|---|---|
| `full` | The whole window | Has no `base_version` |
| `incremental` | Only sessions after `base_version` | `coverage.start` is after the base's coverage end |
| `correction` | The same window as `base_version`, with some sessions replaced | Same date as the base with a higher revision (`2025-12-31.1` → `2025-12-31.2`). `corrected_dates` lists every replaced session. |

A correction is a complete artifact rather than a patch. An importer can load it
the same way it loads a full release, and it uses `corrected_dates` to report
what changed.

## 8. What an importer must do

1. Validate the whole artifact before changing the database, either by running
   `scripts/validate_artifact.py` or by applying §1–§7. If anything fails,
   refuse the artifact and change nothing.
2. Refuse any `contract_version` whose major number isn't 1.
3. Load quarantined sessions as trading days (§5).
4. Record `dataset_version`, `kind`, `coverage` and `source_commit` in the
   ingestion run.

## 9. Validation

```bash
uv run python scripts/validate_artifact.py cse-dataset-2025-12-31.1.zip
```

The command exits 0 with a per-file row count when the artifact is valid. When
it isn't, it prints `FAIL <code>: <reason>` and exits 1. It stops at the first
failure, and the reason names the file, line and column involved.

| Code | Meaning |
|---|---|
| `artifact_unreadable` | The path is not a directory or a zip archive |
| `manifest_unreadable` | `manifest.json` is missing, isn't valid JSON, or isn't an object |
| `unsupported_contract` | `contract_version` has a major number other than 1 |
| `manifest_schema` | The manifest breaks the JSON Schema or a cross-field rule in §6–§7 |
| `missing_file` | A required file isn't listed, or a listed file isn't in the artifact |
| `unlisted_file` | A file isn't listed in the manifest, a name appears twice in the zip, or the layout isn't flat |
| `checksum_mismatch` | A file's SHA-256 differs from the manifest |
| `row_count_mismatch` | A file's row count differs from the manifest |
| `bad_encoding` | A byte order mark, bytes that aren't UTF-8, or `\r` line endings |
| `header_mismatch` | A header is missing, has a repeated column, or lacks a contract column |
| `bad_value` | A field breaks §3, or a row has the wrong number of fields |
| `duplicate_key` | Two rows share a key |
| `key_order` | Rows are not sorted by key |
| `orphan_reference` | A reference in §4 doesn't resolve |
| `calendar_inconsistent` | The calendar disagrees with the prices or with `quarantine.dates` (§5) |
| `coverage_mismatch` | The calendar's bounds, `index_series`, `corrected_dates` or `known_gaps` disagree with `coverage` or the data |

## 10. The first release

The first release is a `full` artifact covering the validated 2017–2025 window
(ADR 0007 in tradeiq_cse). Its manifest records the exact first and last
sessions.

- **Index values are limited to the same window.** The official archive goes
  back to 1985 for ASPI, but every calendar date needs an OHLCV status, and
  sessions before 2017 have none. A later minor version may give each file its
  own coverage and ship the longer history then.
- **Series shipped:** ASPI, SL20, SL20TRI and ASTRI, each with a value on
  every session. MPI isn't shipped because it has no values after 2012. MTRI
  isn't shipped either: the official workbook has no MTRI values from 2013 to
  2024, and only 56 in early 2025.
- **`board` is empty.** The metadata collector only ever wrote a hardcoded
  default, so there is no real value to ship.
- **Sectors are CSE's 20 GICS industry groups**, from `2025-12-31.3` on, taken
  from CSE's `companyProfile` and `allSectors` endpoints. `sector_code` is empty
  for a company CSE gives no industry group for. Earlier releases ship no
  `sectors.csv` and no sector codes.
- **No open price for 2017, 2018, the first quarter of 2021, and 2025**, from
  `2025-12-31.2` on. CSE's official files for those periods repeat the close in
  OPEN PRICE on every row, which is not an opening price, so `open` is empty.
  `2025-12-31.1` shipped the copied value.
