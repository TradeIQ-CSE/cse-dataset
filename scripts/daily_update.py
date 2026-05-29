"""Run the currently enabled recovery pipeline.

External publishing and broad derived-artifact rebuilds stay disabled until the
daily OHLCV path has passed validation and a small backfill sample is verified.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACCEPTED_ROOT = ROOT / "data/raw/ohlcv/accepted"


def run_step(args: list[str]) -> None:
    cmd = [sys.executable, *args]
    print(f"\n==> {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the verified daily OHLCV update")
    parser.add_argument("--target-date", help="Target trading date as YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true", help="Validate without writing OHLCV artifacts")
    parser.add_argument(
        "--allow-missing-metadata",
        action="store_true",
        help="Allow source-only dry runs before metadata has been rebuilt",
    )
    parser.add_argument(
        "--allow-validation-failure",
        action="store_true",
        help="Record rejected source payloads without failing the process",
    )
    args = parser.parse_args()

    # Metadata is regenerated before OHLCV so symbol/listing-date gates can run.
    if not args.dry_run:
        run_step(["scripts/01_collect_metadata.py"])

    collect_args = ["scripts/02_collect_prices.py"]
    if args.target_date:
        collect_args.extend(["--target-date", args.target_date])
    if args.dry_run:
        collect_args.append("--dry-run")
    if args.allow_missing_metadata:
        collect_args.append("--allow-missing-metadata")
    if args.allow_validation_failure:
        collect_args.append("--allow-validation-failure")
    run_step(collect_args)

    if not args.dry_run:
        accepted_files = list(ACCEPTED_ROOT.glob("*/*/canonical_ohlcv.csv"))
        if args.allow_validation_failure and not accepted_files:
            print("No accepted OHLCV transactions were written; skipping processed rebuild.")
            return
        run_step(["scripts/02b_merge_data.py"])


if __name__ == "__main__":
    main()
