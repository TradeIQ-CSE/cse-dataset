"""Collect one verified daily OHLCV snapshot.

This script intentionally does not perform historical backfill. It accepts only
source payloads whose observable source date matches the target trading date.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from ohlcv_sources import COLOMBO_TZ, make_adapter, stable_payload_bytes
from ohlcv_validation import load_metadata, validate_ohlcv_records, write_validation_outputs


ROOT = Path(__file__).resolve().parents[1]
RAW_ROOT = ROOT / "data/raw/ohlcv/source_payloads"
ACCEPTED_ROOT = ROOT / "data/raw/ohlcv/accepted"
PROCESSED_ROOT = ROOT / "data/processed/daily_ohlcv"
VALIDATION_ROOT = ROOT / "data/processed/validation/ohlcv"
MANIFEST_ROOT = ROOT / "data/raw/ohlcv/manifests"
DEFAULT_METADATA = ROOT / "data/processed/company_metadata.csv"


def parse_target_date(value: str | None) -> date:
    if not value:
        return datetime.now(COLOMBO_TZ).date()
    return datetime.strptime(value, "%Y-%m-%d").date()


def load_previous_manifest() -> dict[str, Any]:
    latest = MANIFEST_ROOT / "latest_accepted.json"
    if not latest.exists():
        return {}
    return json.loads(latest.read_text())


def write_fetch_artifacts(fetch_result: Any, validation_status: str) -> None:
    payload_path = fetch_result.raw_payload_path
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    payload_path.write_bytes(stable_payload_bytes(fetch_result.payload) + b"\n")
    metadata = {
        "requested_date": fetch_result.requested_date.isoformat(),
        "observed_source_date": fetch_result.observed_source_date.isoformat()
        if fetch_result.observed_source_date
        else None,
        "fetch_time_utc": fetch_result.fetch_time_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_name": fetch_result.source_name,
        "source_url": fetch_result.source_url,
        "payload_hash": fetch_result.payload_hash,
        "row_count": fetch_result.row_count,
        "validation_status": validation_status,
        "raw_payload_path": str(payload_path.relative_to(ROOT)),
    }
    (payload_path.parent / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")


def write_accepted_artifacts(target_date: date, source_name: str, accepted: pd.DataFrame, metrics: dict[str, Any]) -> None:
    accepted_dir = ACCEPTED_ROOT / target_date.isoformat() / source_name
    accepted_dir.mkdir(parents=True, exist_ok=True)
    accepted.to_csv(accepted_dir / "canonical_ohlcv.csv", index=False)

    PROCESSED_ROOT.mkdir(parents=True, exist_ok=True)
    processed_path = PROCESSED_ROOT / f"{target_date.isoformat()}_{source_name}.parquet"
    accepted.to_parquet(processed_path, index=False)

    MANIFEST_ROOT.mkdir(parents=True, exist_ok=True)
    manifest = {
        "target_date": target_date.isoformat(),
        "source_name": source_name,
        "accepted_rows": int(len(accepted)),
        "accepted_symbols": int(accepted["symbol"].nunique()),
        "market_digest": metrics["market_digest"],
        "processed_path": str(processed_path.relative_to(ROOT)),
        "accepted_path": str((accepted_dir / "canonical_ohlcv.csv").relative_to(ROOT)),
    }
    manifest_text = json.dumps(manifest, indent=2) + "\n"
    (MANIFEST_ROOT / f"{target_date.isoformat()}_{source_name}.json").write_text(manifest_text)
    (MANIFEST_ROOT / "latest_accepted.json").write_text(manifest_text)


def write_result_manifest(
    path: Path,
    *,
    target_date: date,
    fetch_result: Any,
    result: Any,
) -> None:
    """Record exactly what this invocation produced for the delivery step.

    The publisher consumes this file instead of searching accepted directories,
    which prevents a rejected run from accidentally sending an older artifact.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    accepted_path = (
        ACCEPTED_ROOT / target_date.isoformat() / fetch_result.source_name / "canonical_ohlcv.csv"
    )
    manifest = {
        "contract_version": "1",
        "status": "accepted" if result.passed else "rejected",
        "target_date": target_date.isoformat(),
        "source_name": fetch_result.source_name,
        "captured_at": fetch_result.fetch_time_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_date_method": "last_traded_time",
        "raw_payload_hash": fetch_result.payload_hash,
        "accepted_path": str(accepted_path.relative_to(ROOT)) if result.passed else None,
        "metadata_path": str(DEFAULT_METADATA.relative_to(ROOT)),
        "validation": result.metrics,
    }
    path.write_text(json.dumps(manifest, indent=2) + "\n")


def collect_daily_ohlcv(args: argparse.Namespace) -> None:
    target_date = parse_target_date(args.target_date)
    adapter = make_adapter(args.source)
    metadata = (
        pd.DataFrame()
        if args.allow_missing_metadata
        else load_metadata(Path(args.metadata_path) if args.metadata_path else DEFAULT_METADATA)
    )
    previous_manifest = {} if args.ignore_previous_digest else load_previous_manifest()

    fetch_result = adapter.fetch_for_date(target_date, RAW_ROOT)
    if not args.target_date and fetch_result.observed_source_date:
        # With no date given, the snapshot names its own session. A scheduled
        # run that starts after midnight, or one on a holiday, then files the
        # session it captured instead of stamping it with the run's date.
        target_date = fetch_result.observed_source_date
        fetch_result = replace(
            fetch_result,
            requested_date=target_date,
            raw_payload_path=adapter.raw_payload_path(RAW_ROOT, target_date),
        )
    records = adapter.normalize(fetch_result.payload, fetch_result)
    source_date_failures = adapter.validate_source_date(records, target_date)
    result = validate_ohlcv_records(
        records,
        target_date=target_date,
        source_date_failures=source_date_failures,
        metadata=metadata,
        previous_manifest=previous_manifest,
        allow_missing_metadata=args.allow_missing_metadata,
        missing_value_threshold=args.missing_activity_threshold,
    )

    validation_dir = VALIDATION_ROOT / target_date.isoformat() / adapter.source_name
    if not args.dry_run:
        write_fetch_artifacts(fetch_result, "accepted" if result.passed else "failed")
        write_validation_outputs(result, output_dir=validation_dir, source_name=adapter.source_name)
        if result.passed:
            write_accepted_artifacts(target_date, adapter.source_name, result.accepted, result.metrics)
        if args.result_manifest:
            write_result_manifest(
                Path(args.result_manifest),
                target_date=target_date,
                fetch_result=fetch_result,
                result=result,
            )

    if not result.passed:
        for failure in result.failures:
            print(f"FAIL: {failure}")
        if args.allow_validation_failure:
            print("VALIDATION_REJECTED: wrote validation report and skipped accepted OHLCV append")
            return
        raise SystemExit(1)

    print(
        "PASS: accepted "
        f"{len(result.accepted):,} OHLCV rows for {target_date.isoformat()} "
        f"from {adapter.source_name}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect verified daily CSE OHLCV")
    parser.add_argument("--target-date", help="Target trading date as YYYY-MM-DD; defaults to today's Colombo date")
    parser.add_argument("--source", default="cse_trade_summary_current")
    parser.add_argument("--metadata-path", default=str(DEFAULT_METADATA))
    parser.add_argument("--dry-run", action="store_true", help="Fetch and validate without writing artifacts")
    parser.add_argument(
        "--allow-missing-metadata",
        action="store_true",
        help="Allow source-only dry runs before company metadata has been rebuilt",
    )
    parser.add_argument("--ignore-previous-digest", action="store_true")
    parser.add_argument(
        "--result-manifest",
        help="Write an invocation-specific JSON result for the delivery step",
    )
    parser.add_argument("--missing-activity-threshold", type=float, default=0.0)
    parser.add_argument(
        "--allow-validation-failure",
        action="store_true",
        help="Exit successfully after writing validation reports for rejected source payloads",
    )
    args = parser.parse_args()
    if args.result_manifest:
        # A fetch crash must not leave a previous invocation looking current.
        Path(args.result_manifest).unlink(missing_ok=True)
    collect_daily_ohlcv(args)


if __name__ == "__main__":
    main()
