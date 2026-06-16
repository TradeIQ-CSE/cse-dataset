# Source Recon

`scripts/source_recon.py` probes candidate sources for the `2026-01-01` forward
gap without writing into ingestion or accepted dataset paths.

## Run Locally

```bash
uv run python scripts/source_recon.py
```

Useful options:

- `--skip-browser` skips Playwright probes.
- `--source SOURCE_ID` limits the run to one source; repeat for multiple.
- `--target-date YYYY-MM-DD` controls date-template probes such as CSE daily PDFs.
- `--output-root PATH` writes recon artifacts somewhere other than `data/recon/`.

Browser probes require Chromium:

```bash
uv run playwright install chromium
```

Probe Yahoo Finance symbol coverage across the local CSE metadata universe:

```bash
uv run python scripts/yahoo_coverage_recon.py \
  --start-date 2026-01-01 \
  --end-date 2026-05-31
```

## Outputs

Each run writes a timestamped directory under `data/recon/`:

- `raw/`: response samples and browser HTML.
- `screenshots/`: Playwright screenshots for JS-heavy sources.
- `parsed/`: parsed sample rows, selector evidence, and structured failures.
- `metadata/ranked_source_matrix.csv`: ranked source matrix.
- `metadata/ranked_source_matrix.json`: full scored metadata.
- `ranked_source_report.md`: human-readable recommendation report with observed
  date samples.
- `metadata/yahoo_symbol_coverage.csv`: optional Yahoo coverage audit by CSE
  symbol when `scripts/yahoo_coverage_recon.py` is run.
- `metadata/yahoo_symbol_coverage_summary.json`: optional Yahoo coverage counts.

## Recommendation Rules

- OHLCV sources cannot be `accepted_candidate` unless row-level or report-level
  dates are visible.
- Source capability hints do not count as observed 2026 evidence; the matrix only
  marks `has_2026_dates` when a 2026 date appears in the payload or page sample.
- Current snapshot APIs are labeled current-only and quarantined for historical
  backfill.
- Browser-scraped sources need screenshot and selector evidence before they can
  move beyond quarantine/cross-check use.
- Third-party OHLCV candidates still need cross-source spot checks near
  `2025-12-31` before any ingestion adapter is built.

## Current Findings

Live recon on `2026-05-31` found Yahoo Finance chart URLs using the pattern
`SYMBOL-SHARETYPE.CM`, for example `COMB-N0000.CM`. The endpoint returns
row-level dated OHLCV bars for 2026 and ranks as an `accepted_candidate` for a
future adapter.

The full Yahoo coverage audit for `2026-01-01` through `2026-05-31` found:

- 308 local CSE metadata symbols tested.
- 280 symbols reachable with 2026 bars.
- 28 symbols returned `404`.
- 240 reachable symbols had at least one row with invalid OHLC bounds.

Conclusion: Yahoo is the first viable 2026-forward OHLCV candidate, but it must
be ingested only behind strict validation, rejected-row reporting, and
cross-checks against official same-day CSE snapshots or another dated source.

The first guarded Yahoo adapter run on `2026-05-31` for target date
`2026-05-29` intentionally wrote no accepted rows: Yahoo returned zero-volume
candidate rows and all sampled rows failed the official CSE `tradeSummary`
same-day close cross-check. The adapter keeps those rows in candidate and
rejected-record artifacts only.

## GitHub Actions Dry Run

Use the manual `Source Recon Dry Run` workflow to run the same probe from
GitHub Actions. It installs Chromium, writes recon artifacts, and uploads the
run directory as an artifact so network differences are visible without
committing generated data.
