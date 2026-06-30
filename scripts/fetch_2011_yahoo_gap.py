"""Fetch 2011 OHLCV data from Yahoo Finance for tickers missing from the official CSE source file.

The 2011_Data__hl.csv source file is a truncated export covering only 74 of ~281 listed
companies (alphabetically A through ETWO). This script fetches the remaining tickers that
were listed by end of 2011 from Yahoo Finance and writes them as dated candidate CSVs.

Output: data/processed/ohlcv_backfill/candidates/2011_yahoo_gap/<date>.csv
These candidates must still go through the standard backfill validation before acceptance.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
from datetime import UTC, date, datetime, time as dt_time
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from requests import RequestException

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.source_recon import REQUEST_HEADERS, infer_rows_from_yahoo_chart

YEAR = 2011
START_DATE = date(YEAR, 1, 1)
END_DATE = date(YEAR, 12, 31)
SOURCE = "yahoo_finance_chart_candidate"
SOURCE_PRIORITY = 20  # Secondary to official CSE backfill (priority 30)
OUTPUT_DIR = ROOT / "data/processed/ohlcv_backfill/candidates/2011_yahoo_gap"
METADATA_PATH = ROOT / "data/processed/company_metadata.csv"
CANONICAL_FIELDS = [
    "date", "symbol", "open", "high", "low", "close",
    "volume", "turnover", "trades",
    "source", "source_priority", "source_timestamp", "raw_payload_hash",
    "validation_status", "validation_warnings",
]
SLEEP_SECONDS = 0.5


def date_to_epoch(d: date) -> int:
    return int(datetime.combine(d, dt_time.min, tzinfo=UTC).timestamp())


def yahoo_symbol(cse_symbol: str) -> str:
    return f"{cse_symbol.replace('.', '-')}.CM"


def payload_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_target_symbols(metadata_path: Path) -> list[tuple[str, str]]:
    """Return (base_ticker, full_symbol) pairs that were listed by end of 2011
    and are not in the official 2011 source file."""
    companies_2011: set[str] = set()
    src = ROOT / "historical_data/csv/stock_data/32Daily Shares Price List -2011-2020/2011_Data__hl.csv"
    with open(src) as f:
        for line in f:
            m = re.match(r'^Company Id\s*:+\s*([A-Z]{2,8})\b', line.strip())
            if m:
                companies_2011.add(m.group(1))

    ref_src = ROOT / "historical_data/csv/stock_data/32Daily Shares Price List -2011-2020/2017_Data__2017.csv"
    df_ref = pd.read_csv(ref_src, skiprows=1, low_memory=False)
    df_ref.columns = [c.strip() for c in df_ref.columns]
    ref_universe = set(df_ref["COMPANY ID"].dropna().str.strip().unique())

    missing_base = ref_universe - companies_2011

    meta = pd.read_csv(metadata_path)
    meta["listing_date"] = pd.to_datetime(meta["listing_date"], format="%d/%b/%Y", errors="coerce")
    listed_by_2011 = meta[meta["listing_date"] <= f"{YEAR}-12-31"]

    targets = []
    for _, row in listed_by_2011.iterrows():
        base = str(row["base_ticker"]).strip()
        symbol = str(row["symbol"]).strip()
        if base in missing_base:
            targets.append((base, symbol))

    return sorted(targets)


def fetch_ohlcv(session: requests.Session, symbol: str, timeout: int = 20) -> tuple[list[dict], bytes | None]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol(symbol)}"
    params = {
        "period1": str(date_to_epoch(START_DATE)),
        "period2": str(date_to_epoch(END_DATE)),
        "interval": "1d",
        "events": "history",
    }
    try:
        resp = session.get(url, params=params, headers=REQUEST_HEADERS, timeout=timeout)
        if resp.status_code >= 400:
            return [], None
        raw = resp.content
        rows = infer_rows_from_yahoo_chart(resp.json())
        rows = [r for r in rows if str(r.get("date", "")).startswith(str(YEAR))]
        return rows, raw
    except (RequestException, json.JSONDecodeError, Exception):
        return [], None


def to_candidate_row(row: dict[str, Any], symbol: str, raw: bytes) -> dict:
    trade_date = str(row["date"])
    return {
        "date": trade_date,
        "symbol": symbol,
        "open": row.get("open"),
        "high": row.get("high"),
        "low": row.get("low"),
        "close": row.get("close"),
        "volume": row.get("volume"),
        "turnover": None,
        "trades": None,
        "source": SOURCE,
        "source_priority": SOURCE_PRIORITY,
        "source_timestamp": trade_date,
        "raw_payload_hash": payload_hash(raw),
        "validation_status": "candidate",
        "validation_warnings": "",
    }


def write_candidates(by_date: dict[str, list[dict]], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for trade_date, rows in sorted(by_date.items()):
        out_path = output_dir / f"{trade_date}.csv"
        existing: list[dict] = []
        if out_path.exists():
            with open(out_path, newline="") as f:
                existing = list(csv.DictReader(f))
        all_rows = existing + rows
        with open(out_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CANONICAL_FIELDS)
            writer.writeheader()
            writer.writerows(all_rows)


def run(dry_run: bool = False, limit: int | None = None) -> None:
    targets = load_target_symbols(METADATA_PATH)
    if limit:
        targets = targets[:limit]

    print(f"Targets: {len(targets)} symbols to fetch for {YEAR}")
    print(f"Output:  {OUTPUT_DIR}")
    if dry_run:
        print("DRY RUN — no files written")
        for base, sym in targets:
            print(f"  would fetch: {sym:20s} → {yahoo_symbol(sym)}")
        return

    session = requests.Session()
    by_date: dict[str, list[dict]] = {}
    results = {"fetched": 0, "no_data": 0, "errors": 0}

    for i, (base, symbol) in enumerate(targets, 1):
        rows, raw = fetch_ohlcv(session, symbol)
        if raw is None:
            print(f"  [{i:3d}/{len(targets)}] {symbol:20s} ERROR")
            results["errors"] += 1
        elif not rows:
            print(f"  [{i:3d}/{len(targets)}] {symbol:20s} no 2011 data")
            results["no_data"] += 1
        else:
            for row in rows:
                candidate = to_candidate_row(row, symbol, raw)
                by_date.setdefault(candidate["date"], []).append(candidate)
            print(f"  [{i:3d}/{len(targets)}] {symbol:20s} {len(rows):3d} rows")
            results["fetched"] += 1

        time.sleep(SLEEP_SECONDS)

    write_candidates(by_date, OUTPUT_DIR)

    total_rows = sum(len(v) for v in by_date.values())
    print(f"\nDone.")
    print(f"  Symbols with data  : {results['fetched']}")
    print(f"  Symbols no data    : {results['no_data']}")
    print(f"  Errors             : {results['errors']}")
    print(f"  Total candidate rows: {total_rows} across {len(by_date)} dates")
    print(f"  Candidates written to: {OUTPUT_DIR}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch 2011 Yahoo Finance gap data for missing CSE tickers.")
    parser.add_argument("--dry-run", action="store_true", help="List targets without fetching")
    parser.add_argument("--limit", type=int, help="Fetch only the first N symbols (for testing)")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(dry_run=args.dry_run, limit=args.limit)
