"""Export and apply audited OHLCV repair rows.

Repairs are deliberately not inferred. A repair row must come from an alternate
source and may only fill fields that are missing in the quarantined official CSE
candidate row. Any non-missing official fields must match the repair source
within a small tolerance before a repaired date batch can be accepted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

try:
    from .backfill_ohlcv import SOURCE_NAME as BACKFILL_SOURCE_NAME
    from .backfill_ohlcv import sha256_file
    from .ohlcv_sources import parse_number
    from .ohlcv_validation import validate_ohlcv_records, write_validation_outputs
except ImportError:  # pragma: no cover - used when executed directly.
    from backfill_ohlcv import SOURCE_NAME as BACKFILL_SOURCE_NAME
    from backfill_ohlcv import sha256_file
    from ohlcv_sources import parse_number
    from ohlcv_validation import validate_ohlcv_records, write_validation_outputs


ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_ROOT = ROOT / "data/processed/ohlcv_backfill/candidates"
VALIDATION_ROOT = ROOT / "data/processed/validation/ohlcv"
REPAIR_VALIDATION_ROOT = ROOT / "data/processed/validation/ohlcv_repairs"
ACCEPTED_ROOT = ROOT / "data/raw/ohlcv/accepted"
REPAIR_SOURCE_NAME = "ohlcv_missing_field_repair"
PRICE_COLUMNS = ["open", "high", "low", "close"]
ACTIVITY_COLUMNS = ["volume", "turnover", "trades"]
REPAIR_COLUMNS = [
    "date",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
    "trades",
    "repair_source_name",
    "repair_source_url",
    "repair_source_date",
    "repair_notes",
]


def latest_candidate_file() -> Path:
    files = sorted(CANDIDATE_ROOT.glob("*/canonical_ohlcv_candidates.csv"), key=lambda path: path.stat().st_mtime)
    if not files:
        raise SystemExit(f"No OHLCV backfill candidate files found under {CANDIDATE_ROOT}")
    return files[-1]


def load_candidates(path: Path | None = None) -> pd.DataFrame:
    source = path or latest_candidate_file()
    records = pd.read_csv(source)
    records["date"] = pd.to_datetime(records["date"], errors="raise").dt.strftime("%Y-%m-%d")
    return records


def rejected_missing_rows(records: pd.DataFrame) -> pd.DataFrame:
    missing_rows: list[dict[str, Any]] = []
    for target_date, group in records.groupby("date", sort=True):
        result = validate_ohlcv_records(
            group.copy(),
            target_date=datetime.strptime(target_date, "%Y-%m-%d").date(),
            metadata=None,
            allow_missing_metadata=True,
        )
        if result.passed:
            continue
        rejected = result.rejected.copy()
        if "rejection_reason" not in rejected.columns:
            continue
        rejected = rejected[rejected["rejection_reason"].str.contains("missing OHLC price", na=False)]
        for _, row in rejected.iterrows():
            missing_fields = [column for column in PRICE_COLUMNS if pd.isna(row.get(column))]
            missing_rows.append(
                {
                    "date": row["date"],
                    "symbol": row["symbol"],
                    "missing_fields": ",".join(missing_fields),
                    "official_open": row.get("open"),
                    "official_high": row.get("high"),
                    "official_low": row.get("low"),
                    "official_close": row.get("close"),
                    "official_volume": row.get("volume"),
                    "official_turnover": row.get("turnover"),
                    "official_trades": row.get("trades"),
                    "rejection_reason": row.get("rejection_reason"),
                }
            )
    return pd.DataFrame(missing_rows)


def export_targets(output_path: Path, *, candidate_path: Path | None = None) -> None:
    targets = rejected_missing_rows(load_candidates(candidate_path))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    targets.to_csv(output_path, index=False)


def load_repairs(path: Path) -> pd.DataFrame:
    repairs = pd.read_csv(path)
    missing = sorted(set(REPAIR_COLUMNS) - set(repairs.columns))
    if missing:
        raise ValueError(f"repair file is missing required columns: {', '.join(missing)}")
    repairs["date"] = pd.to_datetime(repairs["date"], errors="raise").dt.strftime("%Y-%m-%d")
    for column in [*PRICE_COLUMNS, *ACTIVITY_COLUMNS]:
        repairs[column] = repairs[column].apply(parse_number)
    return repairs


def values_match(left: Any, right: Any, *, tolerance: float = 1e-6) -> bool:
    if pd.isna(left) or pd.isna(right):
        return True
    return abs(float(left) - float(right)) <= tolerance


def repair_digest(repairs: pd.DataFrame, repair_file: Path) -> str:
    payload = {
        "repair_file_hash": sha256_file(repair_file),
        "rows": repairs.fillna("").sort_values(["date", "symbol"]).to_dict(orient="records"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def apply_repairs(
    *,
    repair_file: Path,
    candidate_path: Path | None = None,
    dry_run: bool = False,
    allow_validation_failure: bool = False,
) -> dict[str, Any]:
    candidates = load_candidates(candidate_path)
    repairs = load_repairs(repair_file)
    if "validation_warnings" not in candidates.columns:
        candidates["validation_warnings"] = ""
    candidates["validation_warnings"] = candidates["validation_warnings"].fillna("").astype(str)
    digest = repair_digest(repairs, repair_file)
    failures: list[str] = []
    repaired_dates: set[str] = set()

    candidate_index = candidates.set_index(["date", "symbol"], drop=False)
    for _, repair in repairs.iterrows():
        key = (repair["date"], repair["symbol"])
        if key not in candidate_index.index:
            failures.append(f"{repair['date']} {repair['symbol']}: no quarantined candidate row")
            continue
        original = candidate_index.loc[key]
        if isinstance(original, pd.DataFrame):
            failures.append(f"{repair['date']} {repair['symbol']}: duplicate candidate rows")
            continue

        for column in [*PRICE_COLUMNS, *ACTIVITY_COLUMNS]:
            official_value = original.get(column)
            repair_value = repair.get(column)
            if pd.notna(official_value) and pd.notna(repair_value) and not values_match(official_value, repair_value):
                failures.append(
                    f"{repair['date']} {repair['symbol']}: repair {column}={repair_value} "
                    f"does not match official value {official_value}"
                )

        if any(failure.startswith(f"{repair['date']} {repair['symbol']}:") for failure in failures):
            continue

        row_mask = (candidates["date"] == repair["date"]) & (candidates["symbol"] == repair["symbol"])
        filled: list[str] = []
        for column in PRICE_COLUMNS:
            if candidates.loc[row_mask, column].isna().any() and pd.notna(repair[column]):
                candidates.loc[row_mask, column] = repair[column]
                filled.append(column)
        if not filled:
            failures.append(f"{repair['date']} {repair['symbol']}: repair filled no missing OHLC fields")
            continue

        candidates.loc[row_mask, "source"] = REPAIR_SOURCE_NAME
        candidates.loc[row_mask, "raw_payload_hash"] = digest
        candidates.loc[row_mask, "validation_warnings"] = (
            "missing OHLC repaired from "
            f"{repair['repair_source_name']} ({repair['repair_source_url']}); fields={','.join(filled)}"
        )
        repaired_dates.add(repair["date"])

    accepted_dates = 0
    quarantined_dates = 0
    accepted_rows = 0
    rejected_rows = 0
    validation_failures: list[str] = []
    for target_date in sorted(repaired_dates):
        group = candidates[candidates["date"] == target_date].copy()
        result = validate_ohlcv_records(
            group,
            target_date=datetime.strptime(target_date, "%Y-%m-%d").date(),
            metadata=None,
            allow_missing_metadata=True,
        )
        validation_dir = VALIDATION_ROOT / target_date / REPAIR_SOURCE_NAME
        if not dry_run:
            write_validation_outputs(result, output_dir=validation_dir, source_name=REPAIR_SOURCE_NAME)
        rejected_rows += len(result.rejected)
        if result.passed:
            accepted_dates += 1
            accepted_rows += len(result.accepted)
            if not dry_run:
                accepted_dir = ACCEPTED_ROOT / target_date / REPAIR_SOURCE_NAME
                accepted_dir.mkdir(parents=True, exist_ok=True)
                result.accepted.to_csv(accepted_dir / "canonical_ohlcv.csv", index=False)
        else:
            quarantined_dates += 1
            validation_failures.extend([f"{target_date}: {failure}" for failure in result.failures])

    summary = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repair_file": _display_path(repair_file),
        "repair_file_hash": sha256_file(repair_file),
        "repair_digest": digest,
        "repair_rows": int(len(repairs)),
        "repaired_dates": len(repaired_dates),
        "accepted_dates": accepted_dates,
        "quarantined_dates": quarantined_dates,
        "accepted_rows": accepted_rows,
        "rejected_rows": rejected_rows,
        "failures": failures + validation_failures,
        "dry_run": dry_run,
    }
    if not dry_run:
        REPAIR_VALIDATION_ROOT.mkdir(parents=True, exist_ok=True)
        (REPAIR_VALIDATION_ROOT / "repair_summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    if summary["failures"] and not allow_validation_failure:
        raise SystemExit(f"OHLCV repair validation failed with {len(summary['failures'])} failure(s)")
    return summary


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Export or apply audited OHLCV missing-field repairs")
    parser.add_argument("--candidate-path", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export-targets")
    export_parser.add_argument("--output", type=Path, default=ROOT / "data/processed/validation/ohlcv_repairs/missing_ohlcv_targets.csv")

    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--repair-file", type=Path, required=True)
    apply_parser.add_argument("--dry-run", action="store_true")
    apply_parser.add_argument("--allow-validation-failure", action="store_true")

    args = parser.parse_args()
    if args.command == "export-targets":
        export_targets(args.output, candidate_path=args.candidate_path)
        print(f"PASS: wrote OHLCV repair targets to {_display_path(args.output)}")
        return

    summary = apply_repairs(
        repair_file=args.repair_file,
        candidate_path=args.candidate_path,
        dry_run=args.dry_run,
        allow_validation_failure=args.allow_validation_failure,
    )
    print(
        "PASS: evaluated "
        f"{summary['repair_rows']:,} repair rows; accepted {summary['accepted_rows']:,} rows "
        f"on {summary['accepted_dates']:,} repaired dates"
    )


if __name__ == "__main__":
    main()
