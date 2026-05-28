"""
Minimal runtime smoke check for local setup and CI.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pyarrow  # noqa: F401
import requests  # noqa: F401
import vaderSentiment  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]


def read_parquet(path: Path) -> None:
    if not path.exists():
        print(f"SKIP missing artifact: {path}")
        return
    df = pd.read_parquet(path)
    if df.empty:
        raise SystemExit(f"Artifact is empty: {path}")
    print(f"OK {path}: {len(df):,} rows")


def main() -> None:
    read_parquet(ROOT / "data/processed/all_stocks_merged.parquet")
    read_parquet(ROOT / "data/published/cse_unified.parquet")
    schema = ROOT / "data/schemas/ohlcv.schema.json"
    if not schema.exists():
        raise SystemExit(f"Missing schema: {schema}")
    print(f"OK schema present: {schema.relative_to(ROOT)}")
    print("OK core dependencies imported")


if __name__ == "__main__":
    main()
