# Historical CSE Data Archive

This repository includes a raw historical archive under `historical_data/`.
The archive contains original Colombo Stock Exchange files plus mechanically
converted CSV tables. Treat these files as official source evidence, not as a
single normalized dataset.

## Scope

The CSE-originated portion of the archive contains:

- 77 original source files.
- 589 converted CSV tables.
- About 1.62 million non-empty converted rows.
- Historical coverage extending from market-wide files in 1985 through
  official historical share-price coverage ending on `2025-12-31`.

The `historical_policy_interest_rates/` files are supporting macro/rates data.
They are useful for joins and validation, but they are not direct CSE market
records.

## Main Data Families

The archive includes:

- Individual daily security price lists.
- Daily and monthly market indices.
- Daily total-return indices.
- Market ratios and market-wide statistics.
- Dividends, scrip dividends, bonus issues, rights issues, and share splits.
- New listings and de-listings.
- Listed securities and issued quantities.
- Company and sector market capitalization.
- Sector trading statistics, ratios, and domestic/foreign activity.
- Public holdings and foreign holdings.
- Block trades and crossings.
- Beta values.
- GICS daily sector indicators.
- CDS and corporate-debt activity.

Approximate coverage examples:

- Daily market indices: 1985-2025.
- Listings and de-listings: 1988-2025.
- Daily security prices: approximately 1990-2025.
- Dividends: 1991-2025.
- Daily market statistics: 1994-2025.
- Market ratios: 1996-2025.
- Total-return indices: 2004-2025.
- Block trades: 2011-2025.
- Public holdings: 2012-2025.
- GICS daily indicators: 2016-2025.

Coverage is family-specific. Do not assume every table is continuous across its
maximum date range.

## Daily Price Schema Eras

The daily share-price files are the primary historical OHLCV candidate source,
but their schema changes over time:

| Period | Source shape | Canonical impact |
|---|---|---|
| 1990-2000 | Mostly close-only | Not full OHLCV; open, high, low, volume, turnover, and trades are generally absent |
| 2001 | Close-oriented, with some volume | Still not full OHLCV; volume contains blanks |
| 2002-2015 | Grouped high, low, close, volume, turnover, trades | Opening price is generally absent |
| 2016 | Flat high, low, close format | Opening price is absent |
| 2017-2025 | Closest to full OHLCV | Contains open, high, low, close, volume, turnover, and trades, but some rows have blanks |

The historical OHLCV audit found:

- 40 parsed daily price CSVs.
- 15 full-OHLCV files.
- 6 full-OHLCV files with blanks in fields otherwise present in the source.
- 731 rows with blanks in present source fields.
- 6 empty converted worksheet CSVs.

The affected full-OHLCV years include 2017, 2018, 2019, 2023, 2024, and 2025.

## Trust Levels

Use these trust levels when describing the data:

1. Original source file: the downloaded CSE XLS, XLSX, or CSV file.
2. Converted raw CSV: a mechanical extraction of a source sheet.
3. Parsed candidate row: a row interpreted into a target schema.
4. Validated candidate row: a parsed row that passed source-specific checks.
5. Accepted canonical row: a row that passed the required validation gates and
   is eligible for processed outputs.

A converted CSV is not automatically canonical. Conversion success only means a
sheet was extracted.

## Validation Rules

Before using a historical daily price row as canonical, validate:

- Source file and sheet provenance.
- Row date and source date.
- Security identifier and share-type suffix.
- Listing/de-listing plausibility.
- Duplicate date-symbol keys.
- Numeric parsing.
- Non-negative prices, volume, turnover, and trades.
- OHLC bounds when those fields are present.
- Missing fields versus fields absent from the source schema.
- Repeated source snapshots or stale payload hashes.

Do not invent missing source fields. Repairs may only fill fields that are
blank in the official CSE row, and must preserve all non-missing official
values.

## Important Interpretation Rules

- Early historical price files are partial price history, not full OHLCV.
- Blank values mean unknown, not zero.
- Raw prices are not adjusted close unless an explicit adjustment process says
  so.
- Corporate actions must be considered before long-horizon return analysis.
- Do not collapse different CSE share classes into one company-level symbol.
- Do not join using company names alone.
- Do not use current/latest CSE endpoints as historical sources by stamping a
  requested date onto their payload.

In particular, `tradeSummary` is a current/latest market snapshot source. It is
valid only for same-day/current-trading-day checks unless the payload itself
contains independently verifiable historical dates.

## Reference Files

- `historical_data/historical_data_report.md` contains the inventory and
  conversion report.
- `historical_data/conversion_manifest.csv` records source-to-CSV conversion
  outputs.
- `data/processed/validation/ohlcv_historical_audit/historical_ohlcv_missing_report.md`
  summarizes missing OHLCV fields.
- `data/processed/validation/ohlcv_historical_audit/historical_ohlcv_missing_summary.csv`
  contains per-file audit details.
- `docs/DATA_DICTIONARY.md` defines the currently accepted canonical OHLCV
  schema.
