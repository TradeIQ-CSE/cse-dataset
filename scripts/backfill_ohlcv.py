"""Backfill OHLCV from official CSE daily share price files.

This path is separate from both the current-day ``tradeSummary`` collector and
the 2026-forward daily report path. It converts official historical share price
files into canonical OHLCV candidates, validates one trading date at a time, and
writes accepted transactions only for dates whose full batch passes validation.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

try:
    from .ohlcv_sources import parse_number
    from .ohlcv_validation import load_metadata, validate_ohlcv_records, write_validation_outputs
except ImportError:  # pragma: no cover - used when executed as a script.
    from ohlcv_sources import parse_number
    from ohlcv_validation import load_metadata, validate_ohlcv_records, write_validation_outputs


ROOT = Path(__file__).resolve().parents[1]
SOURCE_NAME = "cse_historical_daily_share_prices"
SOURCE_PRIORITY = 30
DEFAULT_INPUT = ROOT / "historical_data/stock_data/33Daily Shares Price List -2021-2025/2025 Data.xlsx"
RAW_ROOT = ROOT / "data/raw/ohlcv/historical_source_payloads"
ACCEPTED_ROOT = ROOT / "data/raw/ohlcv/accepted"
CANDIDATE_ROOT = ROOT / "data/processed/ohlcv_backfill/candidates"
VALIDATION_ROOT = ROOT / "data/processed/validation/ohlcv"
BACKFILL_VALIDATION_ROOT = ROOT / "data/processed/validation/ohlcv_backfill"
DEFAULT_METADATA = ROOT / "data/processed/company_metadata.csv"

HEADER_ALIASES = {
    "company_id": {"company id", "company_id"},
    "main_type": {"main type", "main_type"},
    "sub_type": {"sub type", "sub_type"},
    "trading_date": {"trading date", "trading_date"},
    "high": {"price high (rs.)", "price high", "high"},
    "low": {"price low (rs.)", "price low", "low"},
    "close": {"close price (rs.)", "close price", "close"},
    "open": {"open price (rs.)", "open price", "open"},
    "trades": {"trade volume (no.)", "trade volume", "trades"},
    "volume": {"share volume (no.)", "share volume", "volume"},
    "turnover": {"turnover (rs.)", "turnover"},
}


@dataclass(frozen=True)
class BackfillResult:
    source_path: Path
    row_count: int
    candidate_dates: int
    accepted_dates: int
    quarantined_dates: int
    accepted_rows: int
    rejected_rows: int
    failures: list[str]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_header(value: Any) -> str:
    return str(value).strip().lower().replace("\n", " ").replace("_", " ")


def find_header_row(raw: pd.DataFrame) -> int:
    for idx, row in raw.iterrows():
        values = {normalize_header(value) for value in row.tolist() if str(value).strip() and str(value) != "nan"}
        if "company id" in values and "trading date" in values:
            return int(idx)
    raise ValueError("could not find daily share price header row")


def canonical_column_map(columns: list[Any]) -> dict[str, str]:
    normalized = {normalize_header(column): str(column).strip() for column in columns}
    mapping: dict[str, str] = {}
    for canonical, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                mapping[canonical] = normalized[alias]
                break
    missing = sorted(set(HEADER_ALIASES) - set(mapping))
    if missing:
        raise ValueError(f"daily share price file is missing required columns: {', '.join(missing)}")
    return mapping


def read_source_tables(path: Path) -> list[pd.DataFrame]:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        workbook = pd.ExcelFile(path)
        return [pd.read_excel(path, sheet_name=sheet, header=None, dtype=str) for sheet in workbook.sheet_names]
    if suffix == ".csv":
        return [read_ragged_csv(path)]
    raise ValueError(f"unsupported OHLCV backfill source type: {path}")


def read_ragged_csv(path: Path) -> pd.DataFrame:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle))
    width = max((len(row) for row in rows), default=0)
    padded = [row + [None] * (width - len(row)) for row in rows]
    return pd.DataFrame(padded, dtype=str)


def security_symbol(company_id: Any, main_type: Any, sub_type: Any) -> str | None:
    if pd.isna(company_id) or pd.isna(main_type) or pd.isna(sub_type):
        return None
    base = str(company_id).strip().upper()
    share_type = str(main_type).strip().upper()
    subtype_text = str(sub_type).strip()
    if not base or not share_type or subtype_text.lower() == "nan":
        return None
    try:
        subtype = f"{int(float(subtype_text)):04d}"
    except ValueError:
        subtype = subtype_text.zfill(4)[-4:]
    return f"{base}.{share_type}{subtype}"


def security_symbols(company_id: pd.Series, main_type: pd.Series, sub_type: pd.Series) -> pd.Series:
    base = company_id.astype(str).str.strip().str.upper()
    share_type = main_type.astype(str).str.strip().str.upper()
    subtype_raw = sub_type.astype(str).str.strip()
    subtype_numeric = pd.to_numeric(subtype_raw, errors="coerce")
    subtype = subtype_numeric.apply(lambda value: f"{int(value):04d}" if pd.notna(value) else None)
    fallback = subtype_raw.str.zfill(4).str[-4:]
    subtype = subtype.where(subtype.notna(), fallback)
    invalid = (
        base.eq("")
        | base.str.lower().eq("nan")
        | share_type.eq("")
        | share_type.str.lower().eq("nan")
        | subtype_raw.eq("")
        | subtype_raw.str.lower().eq("nan")
    )
    symbols = base + "." + share_type + subtype
    symbols[invalid] = None
    return symbols


def numeric_series(values: pd.Series) -> pd.Series:
    return pd.to_numeric(
        values.astype(str).str.strip().str.replace(",", "", regex=False).replace({"": None, "-": None, "nan": None}),
        errors="coerce",
    )


def normalize_daily_share_price_file(path: Path, payload_hash: str | None = None) -> pd.DataFrame:
    source_hash = payload_hash or sha256_file(path)
    frames: list[pd.DataFrame] = []
    for raw in read_source_tables(path):
        header_idx = find_header_row(raw)
        columns = [str(value).strip() for value in raw.iloc[header_idx].tolist()]
        table = raw.iloc[header_idx + 1 :].copy()
        table.columns = columns
        mapping = canonical_column_map(columns)

        out = pd.DataFrame()
        out["date"] = pd.to_datetime(table[mapping["trading_date"]], errors="coerce").dt.date.astype(str)
        out["symbol"] = security_symbols(table[mapping["company_id"]], table[mapping["main_type"]], table[mapping["sub_type"]])
        out["open"] = numeric_series(table[mapping["open"]])
        out["high"] = numeric_series(table[mapping["high"]])
        out["low"] = numeric_series(table[mapping["low"]])
        out["close"] = numeric_series(table[mapping["close"]])
        out["volume"] = numeric_series(table[mapping["volume"]])
        out["turnover"] = numeric_series(table[mapping["turnover"]])
        out["trades"] = numeric_series(table[mapping["trades"]])
        out = out[(out["symbol"].notna()) & (out["date"] != "NaT")].copy()
        out["source"] = SOURCE_NAME
        out["source_priority"] = SOURCE_PRIORITY
        out["source_timestamp"] = out["date"]
        out["raw_payload_hash"] = source_hash
        out["validation_status"] = "candidate"
        out["validation_warnings"] = ""
        frames.append(out)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def write_source_manifest(path: Path, records: pd.DataFrame, payload_hash: str) -> None:
    target_dir = RAW_ROOT / payload_hash
    target_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "source_name": SOURCE_NAME,
        "source_path": _display_path(path),
        "fetch_time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "payload_hash": payload_hash,
        "row_count": int(len(records)),
        "min_date": str(records["date"].min()) if not records.empty else None,
        "max_date": str(records["date"].max()) if not records.empty else None,
        "validation_status": "candidate",
    }
    (target_dir / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")


def write_candidates(payload_hash: str, records: pd.DataFrame) -> None:
    target_dir = CANDIDATE_ROOT / payload_hash
    target_dir.mkdir(parents=True, exist_ok=True)
    records.to_csv(target_dir / "canonical_ohlcv_candidates.csv", index=False)


def write_accepted(target_date: date, accepted: pd.DataFrame) -> None:
    accepted_dir = ACCEPTED_ROOT / target_date.isoformat() / SOURCE_NAME
    accepted_dir.mkdir(parents=True, exist_ok=True)
    accepted.to_csv(accepted_dir / "canonical_ohlcv.csv", index=False)


def write_backfill_summary(summary: dict[str, Any]) -> None:
    output_dir = BACKFILL_VALIDATION_ROOT
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "backfill_summary.json").write_text(json.dumps(summary, indent=2) + "\n")


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def run_backfill(
    *,
    source_path: Path,
    start_date: date | None = None,
    end_date: date | None = None,
    metadata_path: Path = DEFAULT_METADATA,
    dry_run: bool = False,
    allow_missing_metadata: bool = True,
    allow_validation_failure: bool = False,
) -> BackfillResult:
    payload_hash = sha256_file(source_path)
    records = normalize_daily_share_price_file(source_path, payload_hash=payload_hash)
    records["date"] = pd.to_datetime(records["date"], errors="coerce").dt.date
    if start_date:
        records = records[records["date"] >= start_date]
    if end_date:
        records = records[records["date"] <= end_date]
    records["date"] = records["date"].astype(str)

    metadata = pd.DataFrame() if allow_missing_metadata else load_metadata(metadata_path)
    failures: list[str] = []
    accepted_dates = 0
    quarantined_dates = 0
    accepted_rows = 0
    rejected_rows = 0

    if not dry_run:
        write_source_manifest(source_path, records, payload_hash)
        write_candidates(payload_hash, records)

    for target_text, group in records.groupby("date", sort=True):
        target_date = datetime.strptime(str(target_text), "%Y-%m-%d").date()
        result = validate_ohlcv_records(
            group.copy(),
            target_date=target_date,
            metadata=metadata,
            allow_missing_metadata=allow_missing_metadata,
            missing_value_threshold=0.0,
        )
        validation_dir = VALIDATION_ROOT / target_date.isoformat() / SOURCE_NAME
        if not dry_run:
            write_validation_outputs(result, output_dir=validation_dir, source_name=SOURCE_NAME)
        rejected_rows += int(len(result.rejected))
        if result.passed:
            accepted_dates += 1
            accepted_rows += int(len(result.accepted))
            if not dry_run:
                write_accepted(target_date, result.accepted)
        else:
            quarantined_dates += 1
            failures.extend([f"{target_date.isoformat()}: {failure}" for failure in result.failures])

    summary = {
        "source_name": SOURCE_NAME,
        "source_path": _display_path(source_path),
        "payload_hash": payload_hash,
        "row_count": int(len(records)),
        "candidate_dates": int(records["date"].nunique()) if not records.empty else 0,
        "accepted_dates": accepted_dates,
        "quarantined_dates": quarantined_dates,
        "accepted_rows": accepted_rows,
        "rejected_rows": rejected_rows,
        "failures": failures[:200],
        "failure_count": len(failures),
        "dry_run": dry_run,
    }
    if not dry_run:
        write_backfill_summary(summary)

    if failures and not allow_validation_failure:
        raise SystemExit(f"OHLCV backfill validation failed for {quarantined_dates} date(s)")

    return BackfillResult(
        source_path=source_path,
        row_count=int(len(records)),
        candidate_dates=int(records["date"].nunique()) if not records.empty else 0,
        accepted_dates=accepted_dates,
        quarantined_dates=quarantined_dates,
        accepted_rows=accepted_rows,
        rejected_rows=rejected_rows,
        failures=failures,
    )


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill OHLCV from official CSE daily share price files")
    parser.add_argument("--source-path", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--metadata-path", type=Path, default=DEFAULT_METADATA)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--require-metadata",
        action="store_true",
        help="Fail symbols missing current metadata. Default allows official historical symbols without current metadata.",
    )
    parser.add_argument("--allow-validation-failure", action="store_true")
    args = parser.parse_args()

    result = run_backfill(
        source_path=args.source_path,
        start_date=parse_date(args.start_date),
        end_date=parse_date(args.end_date),
        metadata_path=args.metadata_path,
        dry_run=args.dry_run,
        allow_missing_metadata=not args.require_metadata,
        allow_validation_failure=args.allow_validation_failure,
    )
    print(
        "PASS: OHLCV backfill evaluated "
        f"{result.row_count:,} rows across {result.candidate_dates:,} trading dates; "
        f"accepted {result.accepted_rows:,} rows on {result.accepted_dates:,} dates, "
        f"quarantined {result.quarantined_dates:,} dates"
    )


if __name__ == "__main__":
    main()
