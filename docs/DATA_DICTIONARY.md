# Data Dictionary

This v2 recovery repo currently defines only the validated OHLCV recovery
schema. Previous processed, feature, and published schemas are invalid until
they are regenerated from accepted raw payloads.

## `data/schemas/ohlcv.schema.json`

Canonical OHLCV row accepted by the daily and future backfill pipelines.

| Column | Type | Description |
|---|---|---|
| `date` | date | Trading date proven by the source validation gate |
| `symbol` | string | CSE security code, for example `COMB.N0000` |
| `open` | number | Opening price |
| `high` | number | Daily high; must bound open, low, and close |
| `low` | number | Daily low; must bound open, high, and close |
| `close` | number | Closing price |
| `volume` | number | Shares traded |
| `turnover` | number | Total traded value |
| `trades` | number | Number of trades |
| `source` | string | Source adapter name |
| `source_priority` | integer | Lower values win when multiple accepted sources overlap |
| `source_timestamp` | date | Date observed from the source or current-snapshot fetch context |
| `raw_payload_hash` | string | SHA-256 hash of the immutable raw payload |
| `validation_status` | string | `candidate`, `accepted`, or rejected status in reports |
| `validation_warnings` | string | Semicolon-separated non-fatal validation warnings |
| `source_open` | number | Original source open when OHLC repair runs |
| `source_high` | number | Original source high when OHLC repair runs |
| `source_low` | number | Original source low when OHLC repair runs |
| `source_close` | number | Original source close when OHLC repair runs |
| `source_ohlc_invalid` | bool | Whether the original source OHLC bounds were invalid |
| `ohlc_repaired` | bool | Whether high/low were repaired deterministically |
| `ohlc_invalid` | bool | Whether the row remains unrepairably invalid |

## Raw Payload Metadata

Each fetched source payload is stored with metadata containing:

- requested date
- observed source date
- UTC fetch time
- source adapter and URL
- payload hash
- row count
- validation status
- raw payload path

## Validation Outputs

Each validation run writes:

- `quality_summary.json`
- `validation_report.md`
- `rejected_records.csv`

## Disabled Schemas

Feature, sentiment, macro, and published unified schemas from the previous
repository are intentionally not documented as active v2 outputs. They must be
reintroduced only after their inputs are rebuilt from validated OHLCV rows.
