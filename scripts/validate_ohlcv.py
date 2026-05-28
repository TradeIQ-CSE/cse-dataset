"""Validate accepted OHLCV artifacts without fetching remote sources."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import pandas as pd

from ohlcv_validation import load_metadata, validate_ohlcv_records, write_validation_outputs


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_METADATA = ROOT / "data/processed/company_metadata.csv"


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a canonical OHLCV CSV")
    parser.add_argument("input", type=Path)
    parser.add_argument("--target-date", required=True)
    parser.add_argument("--metadata-path", default=str(DEFAULT_METADATA))
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/processed/validation/ohlcv/manual")
    parser.add_argument("--allow-missing-metadata", action="store_true")
    args = parser.parse_args()

    target_date = datetime.strptime(args.target_date, "%Y-%m-%d").date()
    records = pd.read_csv(args.input)
    metadata = load_metadata(Path(args.metadata_path))
    result = validate_ohlcv_records(
        records,
        target_date=target_date,
        metadata=metadata,
        allow_missing_metadata=args.allow_missing_metadata,
    )
    write_validation_outputs(result, output_dir=args.output_dir, source_name="manual")
    if not result.passed:
        for failure in result.failures:
            print(f"FAIL: {failure}")
        raise SystemExit(1)
    print(f"PASS: {len(result.accepted):,} accepted OHLCV rows")


if __name__ == "__main__":
    main()
