"""Recon Yahoo Finance coverage for CSE symbols without ingesting data."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time as dt_time
from pathlib import Path
from typing import Any

import requests
from requests import RequestException


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.source_recon import DEFAULT_RECON_ROOT, REQUEST_HEADERS, infer_rows_from_yahoo_chart, run_id

DEFAULT_METADATA_PATH = ROOT / "data/processed/company_metadata.csv"


@dataclass
class YahooSymbolCoverage:
    symbol: str
    yahoo_symbol: str
    status_code: int | None = None
    bar_count: int = 0
    first_date: str | None = None
    last_date: str | None = None
    has_2026_rows: bool = False
    invalid_ohlc_rows: int = 0
    latest_close: float | None = None
    error: str | None = None


def cse_symbol_to_yahoo(symbol: str) -> str:
    return f"{symbol.replace('.', '-')}.CM"


def date_to_epoch_seconds(value: date) -> int:
    return int(datetime.combine(value, dt_time.min, tzinfo=UTC).timestamp())


def load_symbols(path: Path, limit: int | None = None) -> list[str]:
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        symbols = [row["symbol"].strip() for row in reader if row.get("symbol")]
    symbols = sorted(set(symbols))
    return symbols[:limit] if limit else symbols


def invalid_ohlc_count(rows: list[dict[str, Any]]) -> int:
    invalid = 0
    for row in rows:
        try:
            open_price = float(row["open"])
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])
        except (KeyError, TypeError, ValueError):
            invalid += 1
            continue
        if high < max(open_price, low, close) or low > min(open_price, high, close):
            invalid += 1
    return invalid


def fetch_symbol_coverage(
    session: requests.Session,
    symbol: str,
    start_date: date,
    end_date: date,
    timeout: int,
) -> YahooSymbolCoverage:
    yahoo_symbol = cse_symbol_to_yahoo(symbol)
    coverage = YahooSymbolCoverage(symbol=symbol, yahoo_symbol=yahoo_symbol)
    try:
        response = session.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}",
            params={
                "period1": str(date_to_epoch_seconds(start_date)),
                "period2": str(date_to_epoch_seconds(end_date)),
                "interval": "1d",
                "events": "history",
            },
            headers=REQUEST_HEADERS,
            timeout=timeout,
        )
        coverage.status_code = response.status_code
        if response.status_code >= 400:
            coverage.error = f"HTTP status {response.status_code}"
            return coverage
        payload = response.json()
        rows = infer_rows_from_yahoo_chart(payload)
    except (RequestException, json.JSONDecodeError) as exc:
        coverage.error = str(exc)
        return coverage

    coverage.bar_count = len(rows)
    if rows:
        dates = [str(row["date"]) for row in rows if row.get("date")]
        coverage.first_date = min(dates) if dates else None
        coverage.last_date = max(dates) if dates else None
        coverage.has_2026_rows = any(value.startswith("2026-") for value in dates)
        coverage.invalid_ohlc_rows = invalid_ohlc_count(rows)
        latest_row = max(rows, key=lambda row: str(row.get("date", "")))
        try:
            coverage.latest_close = float(latest_row["close"])
        except (KeyError, TypeError, ValueError):
            coverage.latest_close = None
    return coverage


def write_outputs(run_dir: Path, results: list[YahooSymbolCoverage]) -> None:
    metadata_dir = run_dir / "metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    csv_path = metadata_dir / "yahoo_symbol_coverage.csv"
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(results[0]).keys()) if results else [])
        if results:
            writer.writeheader()
            writer.writerows(asdict(result) for result in results)

    summary = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "symbol_count": len(results),
        "reachable_symbols": sum(1 for result in results if result.status_code and result.status_code < 400),
        "symbols_with_bars": sum(1 for result in results if result.bar_count > 0),
        "symbols_with_2026_rows": sum(1 for result in results if result.has_2026_rows),
        "symbols_with_invalid_ohlc_rows": sum(1 for result in results if result.invalid_ohlc_rows > 0),
        "errors": sum(1 for result in results if result.error),
    }
    (metadata_dir / "yahoo_symbol_coverage_summary.json").write_text(json.dumps(summary, indent=2) + "\n")


def run_coverage_recon(
    *,
    metadata_path: Path = DEFAULT_METADATA_PATH,
    output_root: Path = DEFAULT_RECON_ROOT,
    start_date: date,
    end_date: date,
    limit: int | None = None,
    timeout: int = 15,
    sleep_seconds: float = 0.05,
) -> Path:
    run_dir = output_root / run_id()
    symbols = load_symbols(metadata_path, limit=limit)
    session = requests.Session()
    results = []
    for symbol in symbols:
        results.append(fetch_symbol_coverage(session, symbol, start_date, end_date, timeout))
        time.sleep(sleep_seconds)
    write_outputs(run_dir, results)
    return run_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe Yahoo Finance coverage for CSE symbols.")
    parser.add_argument("--metadata-path", type=Path, default=DEFAULT_METADATA_PATH)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_RECON_ROOT)
    parser.add_argument("--start-date", type=lambda value: datetime.strptime(value, "%Y-%m-%d").date(), required=True)
    parser.add_argument("--end-date", type=lambda value: datetime.strptime(value, "%Y-%m-%d").date(), required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--sleep-seconds", type=float, default=0.05)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_dir = run_coverage_recon(
        metadata_path=args.metadata_path,
        output_root=args.output_root,
        start_date=args.start_date,
        end_date=args.end_date,
        limit=args.limit,
        timeout=args.timeout,
        sleep_seconds=args.sleep_seconds,
    )
    print(f"Yahoo coverage recon artifacts written to {run_dir}")


if __name__ == "__main__":
    main()
