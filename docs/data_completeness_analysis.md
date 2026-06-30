# CSE OHLCV Data Completeness Analysis

**Prepared for:** TIQ-19 — Week 1 data completeness findings  
**Updated:** 2026-06-30 (TIQ-22 — 2017–2024 backfill; TIQ-26 — 2025 quarantine repair)  
**Scope:** Historical daily OHLCV, 1991–2026  

---

## Executive Summary

The historical archive contains CSE official source files for every year from 1991 to 2025. The 2017–2024 backfill (TIQ-22) was completed on 2026-06-30, adding 1,807 accepted trading dates and producing a merged parquet of **508,425 rows across 447 symbols**. The years 2020–2022 achieved 100% acceptance; 2024 had the highest quarantine rate at 12.5%.

The remaining unvalidated era is **1991–2016** (~6,195 candidate dates). Running that era is blocked on the open-price strategy decision for years before 2017 (TIQ-21, due for Jul 4 Meelan sync).

The biggest structural gap remains **field coverage, not trading-day gaps**: open prices are absent from the source data before 2017, and high/low prices are absent before 2002. This limits what backtesting can be done pre-2017.

The second-largest issue is a **partial source file for 2011** — the official CSE export covers only 74 of ~281 listed companies (alphabetically A through E). Companies F onwards are entirely missing for 2011. Yahoo Finance gap-fill (TIQ-20) recovered 149 of the 169 remaining tickers; the 20 unrecovered are documented as exhausted (see `docs/data_sources.md`).

---

## Schema Evolution by Era

| Period | Fields available | Companies (approx.) | Source format |
|---|---|---|---|
| 1991–2000 | Close only | 197–224 | Per-company block, `SECURITY_DA, PRICE` |
| 2001 | Close, Share Volume | ~171 | Flat rows, `Date, Security ID, Closing Price, Share Volume` |
| 2002–2015 | High, Low, Close, Trades, Share Volume, Turnover | 226–285 | Per-company block (one company per section) |
| 2016 | High, Low, Close, Trade Volume, Share Volume, Turnover | ~281 | Flat multi-company rows (no Open) |
| 2017–2025 | **Open**, High, Low, Close, Trade Volume, Share Volume, Turnover | 267–281 | Flat multi-company rows |

**Key observation:** Open price is only available from 2017 onwards. Any backtest requiring opening prices must either synthesise them (e.g. assume open = prior close) or limit its scope to 2017–present.

---

## Source Files Confirmed Present

| Source folder | Years | Files | Total rows |
|---|---|---|---|
| `30Daily Shares Price List - 1991-2000/` | 1991–2000 | 9 CSVs | 194,316 |
| `31Daily Shares Price List -2001-2010/` | 2001–2010 | 10 CSVs | 388,444 |
| `32Daily Shares Price List -2011-2020/` | 2011–2020 | 15 CSVs | ~580,000 |
| `33Daily Shares Price List -2021-2025/` | 2021–2025 | 5 CSVs | ~320,000 |

---

## Acceptance Status (as of 2026-06-30)

| Period | Source-observed dates | Accepted dates | % Accepted | Status |
|---|---|---|---|---|
| 1991–2000 | ~1,961 | 0 | 0% | Candidate, never validated — blocked on TIQ-21 |
| 2001–2010 | ~2,393 | 0 | 0% | Candidate, never validated — blocked on TIQ-21 |
| 2011–2016 | ~1,441 | 0 | 0% | Candidate, never validated — blocked on TIQ-21 |
| 2017 | 241 | 232 | 96.3% | **Accepted** (TIQ-22, 9 dates quarantined) |
| 2018 | 240 | 225 | 93.8% | **Accepted** (TIQ-22, 15 dates quarantined) |
| 2019 | 241 | 225 | 93.4% | **Accepted** (TIQ-22, 16 dates quarantined) |
| 2020 | 209 | 209 | 100% | **Accepted** (TIQ-22, COVID year — 0 quarantined) |
| 2021 | 240 | 240 | 100% | **Accepted** (TIQ-22, 0 quarantined) |
| 2022 | 231 | 231 | 100% | **Accepted** (TIQ-22, 0 quarantined) |
| 2023 | 242 | 235 | 97.1% | **Accepted** (TIQ-22, 7 dates quarantined) |
| 2024 | 240 | 210 | 87.5% | **Accepted** (TIQ-22, 30 dates quarantined) |
| 2025 | 238 | 236 | 99.2% | Accepted (221 backfill + 15 repaired via TIQ-26; 2 remain quarantined) |
| 2026 | 0 (from source) | 1 | — | 12 run-log-only; daily workflow partial |

**Candidate-unvalidated dates remaining: ~6,195** (1991–2016, blocked on TIQ-21).  
**Total accepted dates: 2,044** (2017–2026).  
**Merged parquet: 512,658 rows, 447 symbols.**

---

## Identified Gaps and Issues

### Gap 1 — 2011 Partial Export (Critical)

**Severity:** High — approximately 73% of companies are missing for all of 2011.

The source file `2011_Data__hl.csv` contains only **74 of ~281 listed companies**, covering A through ETWO alphabetically. Companies from F onwards (FHL, GLD, HHL, JINS, KZOO, LOLC, etc.) are entirely absent. This is a truncated CSE export, not a partial year.

- Companies in file: 74 (AAIC, AFSL, AHPL … ATL, AUTO, BALA … ETWO)
- Companies expected: ~281 (based on 2012 data)
- Missing: ~207 companies (all F–Z tickers)
- Trading dates covered: 239 (full year is present for those 74 companies)

**Impact:** Any 2011 OHLCV backtest will have a ~73% coverage gap. This gap cannot be repaired from this source file alone; a supplementary source would be needed (e.g. re-requesting from CSE, or sourcing from a data vendor).

---

### Gap 2 — No Open Price Before 2017

**Severity:** High — affects 26 years of data (1991–2016).

Open prices are structurally absent from all source files before 2017. This is not a data quality issue; the CSE simply did not publish opening prices in its historical workbooks for those years.

| Period | Schema |
|---|---|
| 1991–2000 | Close only |
| 2001 | Close + Volume |
| 2002–2016 | High, Low, Close + Volume (no Open) |
| 2017 onward | Full OHLCV |

**Impact:** The canonical schema requires `open`. For 1991–2016, `open` must either be left null, inferred as prior day's close, or synthesised. This is a design decision that needs to be agreed with the supervisor.

**Open completeness in 2017+ (where open exists):**

| Year | Missing open rows | % of total rows |
|---|---|---|
| 2017 | 0 | 0.0% |
| 2018 | 0 | 0.0% |
| 2019 | 18 | 0.034% |
| 2020 | 0 | 0.0% |
| 2025 | 316 | 0.47% |

Missing open rows in 2017+ are negligible (individual stock-days where a company did not trade at the open).

---

### Gap 3 — 2001 Reduced Company Coverage

**Severity:** Medium — ~26% fewer companies than adjacent years.

The 2001 source file contains ~171 unique securities vs ~197 in 2000 and ~226 in 2002. The likely cause is that the 2001 file captures only securities with recorded transactions (active-trading filter), not the full listed universe.

---

### Gap 4 — 1991 Sparse Early Coverage

**Severity:** Medium — only ~120 trading dates vs ~261 expected weekdays.

The 1991 data starts from August 1991 (first recorded date: 1991-08-26). The CSE's electronic data capture likely began partway through 1991. Dates before August 1991 are not recoverable from official digital sources.

---

### Gap 5 — 2020 COVID-19 Market Closures

**Severity:** Low — expected, not a data quality issue.

53 weekdays in 2020 have no trading data, concentrated in March–May 2020:

| Month | Missing trading days |
|---|---|
| March 2020 | 12 |
| April 2020 | 22 |
| May 2020 | 7 |
| Other months | 12 |

The CSE was closed during the Sri Lanka COVID-19 lockdown. These are legitimate market non-trading days, not data gaps. They should be excluded from any trading-day calendar used in backtesting.

---

### Gap 6 — 2026 Forward Coverage Not Operational

**Severity:** High — 114 calendar-proxy missing weekdays in 2026.

The 2026-forward daily workflow (`daily_update.py`) has run but most accepted artifacts are missing locally. The daily run log records 12 dates as accepted but no local artifact exists for them. Only 1 date (2026-01-02) has an accepted artifact; 1 date (2026-05-29) is quarantined due to a failed PDF fetch.

The daily GitHub Actions workflow is running (commits to `docs/daily_ohlcv_runs.jsonl`) but accepted OHLCV CSVs are not persisted to the repo (by design — see CLAUDE.md). The local `data/raw/` directory is not current.

---

### Gap 7 — 2025 Quarantined Dates (2 remaining)

**Severity:** Low — 15 of 17 quarantined dates repaired (TIQ-26, 2026-06-30).

17 dates in 2025 had quarantined validation attempts, all caused by missing `low` price (and sometimes `open`) in the official CSE source file. Yahoo Finance was used as the repair source for 15 dates, cross-checked against official `high` and `close` values (all matched exactly).

**2 dates remain quarantined** — Yahoo Finance data is internally inconsistent for these rows:
- `MERC.N0000 2025-10-10`: Yahoo high/low=5500 vs official close=2600 (likely corporate action adjustment artefact)
- `CINS.N0000 2025-11-12`: Yahoo high/low=3300 vs official close=3358.25 (OHLC bounds violation in Yahoo data)

No alternative repair source has been identified for these two rows.

---

## Symbol Universe by Year

| Year | Unique companies in source | Notes |
|---|---|---|
| 1991–1993 | ~197 | Early sparse data; close-only |
| 1994–2000 | 204–224 | Growing listed universe |
| 2001 | ~171 | Possibly active-trading-only filter |
| 2002–2010 | 226–239 | Stable growth |
| 2011 | **74** | **TRUNCATED FILE — A through E only** |
| 2012–2016 | 279–285 | Full universe resumes |
| 2017–2020 | 267–281 | Minor delisting variation |
| 2021–2025 | 267–277 | Current-era dataset |
| Metadata (current) | 308 | Includes delisted companies |

---

## Backfill Pipeline Status

The `scripts/backfill_ohlcv.py` pipeline has now been run against 2017–2024 (TIQ-22, completed 2026-06-30). Results:

| Year | Candidate dates | Accepted | Quarantined | Rate |
|---|---|---|---|---|
| 2017 | 241 | 232 | 9 | 3.7% |
| 2018 | 240 | 225 | 15 | 6.3% |
| 2019 | 241 | 225 | 16 | 6.6% |
| 2020 | 209 | 209 | 0 | 0% |
| 2021 | 240 | 240 | 0 | 0% |
| 2022 | 231 | 231 | 0 | 0% |
| 2023 | 242 | 235 | 7 | 2.9% |
| 2024 | 240 | 210 | 30 | 12.5% |
| **Total** | **1,884** | **1,807** | **77** | **4.1%** |

The 2024 file uses `DD-MMM-YY` date format (e.g. `04-JAN-24`), handled via pandas dateutil fallback with a warning. All dates parsed correctly.

The remaining 1991–2016 era is **blocked on TIQ-21** (open-price strategy decision). The pipeline handles those eras:
- Close-only (1991–2000) → fills `open/high/low` as null
- High/Low/Close (2002–2016) → fills `open` as null

**Next action:** After TIQ-21 is resolved at the Jul 4 Meelan sync, run TIQ-23 (2002–2016) and TIQ-24 (1991–2001) to unlock the remaining ~6,195 candidate dates.

---

## Discussion Points for Jul 4 Sync (Meelan)

1. **2011 truncated file** — Should we attempt to source a complete 2011 dataset from CSE, or accept a 73%-company coverage gap for that year in the dataset?

2. **Open price strategy for 1991–2016** — Options: (a) leave `open` null and exclude from open-dependent backtests; (b) synthesise as prior close; (c) limit backtesting scope to 2017+. This is a project-scope decision.

3. **Backfill execution priority** — ~~Recommend running backfill reverse-chronologically (2024 → 1991)~~ **Done for 2017–2024 (TIQ-22)**. Remaining 1991–2016 blocked on item 2 above.

4. **2011 severity for backtesting** — If the backtesting engine requires full-market coverage, 2011 should be a documented exclusion year.

5. **Trading-day calendar** — COVID closures (2020) and standard CSE holidays (~20 days/year) mean the calendar proxy used in the coverage report over-counts missing data. A real CSE holiday calendar would sharpen the gap analysis.
