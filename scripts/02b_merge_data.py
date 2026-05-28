"""Build processed OHLCV only from accepted raw transactions."""

from __future__ import annotations

from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
ACCEPTED_ROOT = ROOT / "data/raw/ohlcv/accepted"
OUTPUT = ROOT / "data/processed/all_stocks_merged.parquet"


def process_and_merge() -> None:
    files = sorted(ACCEPTED_ROOT.glob("*/*/canonical_ohlcv.csv"))
    if not files:
        raise SystemExit(f"No accepted OHLCV transactions found under {ACCEPTED_ROOT}")

    frames = [pd.read_csv(path) for path in files]
    merged = pd.concat(frames, ignore_index=True)
    merged["date"] = pd.to_datetime(merged["date"], errors="raise")
    merged = merged.sort_values(["symbol", "date", "source_priority"]).drop_duplicates(
        subset=["symbol", "date"], keep="first"
    )

    duplicate_rows = int(merged.duplicated(subset=["symbol", "date"]).sum())
    if duplicate_rows:
        raise SystemExit(f"duplicate (symbol, date) rows after accepted merge: {duplicate_rows}")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(OUTPUT, index=False)
    print(
        f"PASS: wrote {len(merged):,} accepted OHLCV rows "
        f"({merged['symbol'].nunique():,} symbols) to {OUTPUT.relative_to(ROOT)}"
    )


if __name__ == "__main__":
    process_and_merge()
