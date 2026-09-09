"""Convert official CSE index workbooks into canonical index rows.

This is the historical path for the ``indices`` family: it reads the official
archive CSVs converted by ``scripts/convert_historical_data.py`` and emits long
-format rows against the ``indices`` contract in ``forward_ingestion.py``.

The archive is a dated source, so each row carries its own trading date as
``source_timestamp``. That is the opposite of the current-day API path, where a
snapshot must be proven to match a single target date. The two paths are kept
separate for the same reason ``backfill_ohlcv.py`` is separate from
``daily_update.py``: a multi-date official file and a single-date snapshot
cannot share a validation gate.

Official file coverage ends on 2025-12-31. Anything after that belongs to the
2026-forward path.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

try:
    from .forward_ingestion import FAMILY_CONTRACTS, OFFICIAL_FILE_COVERAGE_END
    from .ohlcv_sources import parse_number
except ImportError:  # pragma: no cover - used when scripts are executed directly.
    from forward_ingestion import FAMILY_CONTRACTS, OFFICIAL_FILE_COVERAGE_END
    from ohlcv_sources import parse_number


ROOT = Path(__file__).resolve().parents[1]
SOURCE_NAME = "cse_official_index_workbook"
ARCHIVE_ROOT = ROOT / "historical_data/csv/stock_data"
DEFAULT_INDEX_FILE = ARCHIVE_ROOT / "07Market_Indices_-_Daily__Index.csv"
DEFAULT_TRI_FILE = ARCHIVE_ROOT / "09Total_Returns_Indices_-_Daily__TRI.csv"

CANDIDATE_ROOT = ROOT / "data/processed/indices_backfill/candidates"
ACCEPTED_ROOT = ROOT / "data/processed/indices_backfill/accepted"
VALIDATION_ROOT = ROOT / "data/processed/validation/indices_backfill"

EARLIEST_ARCHIVE_DATE = date(1985, 1, 2)

# Cells the exchange uses to mean "not published". They produce no row, the
# same as a blank: a discontinued series is absent, not zero and not rejected.
NULL_MARKERS = {"-", "--", "n/a", "na", "#n/a", "#n/a!", "#value!", "nil"}

# Some archive rows kept the raw Excel serial instead of a formatted date.
EXCEL_EPOCH = date(1899, 12, 30)
EXCEL_SERIAL_RANGE = (20000, 60000)

# Header labels as they appear in the official workbooks, mapped to the codes
# the platform stores. Trailing spaces in the source headers are stripped
# before lookup.
HEADLINE_SERIES: dict[str, str] = {
    "All Share Price Index": "ASPI",
    "Milanka Price Index": "MPI",
    "S&P Sri Lanka 20": "SL20",
    "ASTRI": "ASTRI",
    "MTRI": "MTRI",
    "S&P SL 20 TRI": "SL20TRI",
}

# Series inception dates that are independently known. A row before inception
# means the columns were read in the wrong position, which is the failure mode
# a stacked header invites.
SERIES_INCEPTION: dict[str, date] = {
    "SL20": date(2012, 6, 27),
    "ASTRI": date(2004, 1, 2),
}

# Series the exchange stopped publishing. Values after these dates are reported
# as warnings rather than rejected: the archive simply leaves them blank.
SERIES_DISCONTINUED: dict[str, date] = {
    "MPI": date(2023, 3, 31),
}


def normalize_label(value: str) -> str:
    """Fold header spelling differences so one label maps to one series.

    The workbook renames series across segments: "S&P Sri Lanka 20" becomes
    "S&P Sri Lanka 20 Index" after the GICS switch.
    """
    text = " ".join((value or "").split()).strip().lower()
    if text.endswith(" index"):
        text = text[: -len(" index")]
    return text


HEADLINE_BY_NORMALIZED_LABEL: dict[str, str] = {
    normalize_label(label): code for label, code in HEADLINE_SERIES.items()
}


class HistoricalIndexError(RuntimeError):
    """Raised when a source file cannot be read as an index workbook."""


@dataclass(frozen=True)
class IndexFileLayout:
    """Where the header labels and the first data row live in one workbook."""

    header_rows: tuple[int, ...]
    first_data_row: int


# File 07 splits its labels across two header rows: the sector names and
# "S&P Sri Lanka 20" sit on one, ASPI and MPI on the next. File 09 uses one.
LAYOUTS: dict[str, IndexFileLayout] = {
    "07Market_Indices_-_Daily__Index.csv": IndexFileLayout(header_rows=(1, 2), first_data_row=3),
    "09Total_Returns_Indices_-_Daily__TRI.csv": IndexFileLayout(header_rows=(1,), first_data_row=2),
}
DEFAULT_LAYOUT = IndexFileLayout(header_rows=(1,), first_data_row=2)


@dataclass(frozen=True)
class HistoricalIndexValidationResult:
    accepted: pd.DataFrame
    rejected: pd.DataFrame
    failures: list[str]
    warnings: list[str]
    metrics: dict[str, Any]

    @property
    def passed(self) -> bool:
        return not self.failures and self.rejected.empty and not self.accepted.empty


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def layout_for(path: Path) -> IndexFileLayout:
    return LAYOUTS.get(path.name, DEFAULT_LAYOUT)


def read_rows(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [row for row in csv.reader(handle)]


def combine_header(rows: list[list[str]], layout: IndexFileLayout) -> list[str]:
    """Flatten a stacked header into one label per column.

    A column's label is the first non-empty cell across the header rows, so
    file 07's two rows collapse without either row overwriting the other.
    """
    width = max((len(rows[i]) for i in layout.header_rows if i < len(rows)), default=0)
    labels: list[str] = []
    for column in range(width):
        label = ""
        for header_row in layout.header_rows:
            if header_row >= len(rows):
                continue
            cell = rows[header_row][column].strip() if column < len(rows[header_row]) else ""
            if cell and not label:
                label = cell
        labels.append(label)
    return labels


def parse_archive_date(value: str) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    # A few rows kept Excel's serial number instead of a formatted date.
    if text.isdigit() and EXCEL_SERIAL_RANGE[0] <= int(text) <= EXCEL_SERIAL_RANGE[1]:
        return EXCEL_EPOCH + timedelta(days=int(text))
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def is_null_marker(value: str) -> bool:
    return value.strip().lower() in NULL_MARKERS


def series_columns_for(labels: list[str], *, include_sectors: bool) -> dict[int, str]:
    columns: dict[int, str] = {}
    for column, label in enumerate(labels):
        if column == 0 or not label:
            continue
        code = HEADLINE_BY_NORMALIZED_LABEL.get(normalize_label(label))
        if code is not None:
            columns[column] = code
        elif include_sectors:
            columns[column] = " ".join(label.split())
    return columns


def looks_like_header(row: list[str]) -> bool:
    """True when a non-data row renames the columns below it.

    The daily index workbook restarts its header mid-file when the exchange
    switched to GICS sector indices, so labels cannot be read once at the top.
    """
    if not row:
        return False
    if normalize_label(row[0]) == "date":
        return True
    return any(normalize_label(cell) in HEADLINE_BY_NORMALIZED_LABEL for cell in row[1:] if cell.strip())


def extract_index_records(
    path: Path,
    *,
    include_sectors: bool = False,
) -> tuple[pd.DataFrame, list[str]]:
    """Read one workbook CSV into long-format candidate rows.

    The file is walked in segments: the daily index workbook restarts its
    header partway through, and the columns below each header mean different
    things. Reading labels once at the top would silently mislabel everything
    after the break.

    Blank cells and "not published" markers produce no row at all. An index
    that was not published on a given day is absent, never a zero close.
    """
    rows = read_rows(path)
    layout = layout_for(path)
    if len(rows) <= layout.first_data_row:
        raise HistoricalIndexError(f"{path.name}: no data rows below the header")

    labels = combine_header(rows, layout)
    if not labels:
        raise HistoricalIndexError(f"{path.name}: no header labels found")

    warnings: list[str] = []
    series_columns = series_columns_for(labels, include_sectors=include_sectors)
    unlabelled_with_data: set[int] = set()
    segments = 1

    payload_hash = sha256_file(path)
    records: list[dict[str, Any]] = []
    for row_number in range(layout.first_data_row, len(rows)):
        row = rows[row_number]
        if not row or not any(cell.strip() for cell in row):
            continue
        row_date = parse_archive_date(row[0] if row else "")
        if row_date is None:
            # Either a new header for the rows below, or an explanatory note.
            if looks_like_header(row):
                labels = [cell.strip() for cell in row]
                series_columns = series_columns_for(labels, include_sectors=include_sectors)
                segments += 1
            continue
        for column, code in series_columns.items():
            raw = row[column].strip() if column < len(row) else ""
            if not raw or is_null_marker(raw):
                continue
            records.append(
                {
                    "date": row_date,
                    "index_name": code,
                    "close": parse_number(raw),
                    "source": SOURCE_NAME,
                    "source_timestamp": row_date,
                    "raw_payload_hash": payload_hash,
                    "source_file": path.name,
                    "source_row": row_number + 1,
                    "source_label": labels[column] if column < len(labels) else "",
                }
            )
        # A column carrying values under no header label cannot be named, so it
        # is skipped rather than guessed at.
        for column in range(1, len(row)):
            if column in series_columns:
                continue
            label = labels[column] if column < len(labels) else ""
            if not label and row[column].strip() and not is_null_marker(row[column]):
                unlabelled_with_data.add(column)

    for column in sorted(unlabelled_with_data):
        warnings.append(f"{path.name}: column {column} has values but no header label; skipped")
    if segments > 1:
        warnings.append(f"{path.name}: {segments} header segments; labels re-read at each break")
    if not records:
        raise HistoricalIndexError(f"{path.name}: no index values were extracted")
    return pd.DataFrame(records), warnings


def validate_historical_index_records(
    records: pd.DataFrame,
    *,
    earliest_date: date = EARLIEST_ARCHIVE_DATE,
    coverage_end: date = OFFICIAL_FILE_COVERAGE_END,
) -> HistoricalIndexValidationResult:
    """Gate archive rows against the shared ``indices`` contract.

    Reuses the contract's columns, required fields and identity keys, but not
    the forward path's single-target-date rule: an archive file spans decades,
    so each row is checked against its own date instead.
    """
    contract = FAMILY_CONTRACTS["indices"]
    failures: list[str] = []
    warnings: list[str] = []

    if records.empty:
        failures.append("no indices candidate rows were produced")
        return HistoricalIndexValidationResult(
            records.copy(),
            records.copy(),
            failures,
            warnings,
            _metrics(0, 0, 0, failures, warnings, {}),
        )

    df = records.copy()
    for column in contract.canonical_columns:
        if column not in df.columns:
            df[column] = None
    df["date"] = df["date"].map(lambda value: value if isinstance(value, date) else parse_archive_date(str(value)))
    df["source_timestamp"] = df["source_timestamp"].map(
        lambda value: value if isinstance(value, date) else parse_archive_date(str(value))
    )
    df["close"] = pd.to_numeric(df["close"], errors="coerce")

    reasons: list[list[str]] = [[] for _ in range(len(df))]

    for position, (row_date, index_name, close, source_timestamp) in enumerate(
        zip(df["date"], df["index_name"], df["close"], df["source_timestamp"])
    ):
        if row_date is None:
            reasons[position].append("invalid date field: date")
        if not isinstance(index_name, str) or not index_name.strip():
            reasons[position].append("missing required field: index_name")
        if pd.isna(close):
            reasons[position].append("invalid numeric field: close")
        elif close <= 0:
            # An index level is a positive number; 0 means a blank was read as
            # a value somewhere upstream.
            reasons[position].append("non-positive index level: close")
        if row_date is not None:
            if row_date < earliest_date:
                reasons[position].append(f"date precedes archive start {earliest_date.isoformat()}")
            if row_date > coverage_end:
                reasons[position].append(f"date exceeds official coverage end {coverage_end.isoformat()}")
            inception = SERIES_INCEPTION.get(index_name) if isinstance(index_name, str) else None
            if inception is not None and row_date < inception:
                reasons[position].append(f"row predates {index_name} inception {inception.isoformat()}")
            if source_timestamp != row_date:
                reasons[position].append("source timestamp does not match row date")

    # The archive repeats a whole line here and there. An identical repeat is a
    # transcription artifact and is collapsed to one row; only a repeat that
    # disagrees on the close is a real conflict worth rejecting, because there
    # is no way to tell which value is right.
    identity = [column for column in contract.identity_columns if column in df.columns]
    exact_duplicate = df.duplicated(subset=[*identity, "close"], keep="first")
    # Every row in a group holding more than one distinct close is a conflict.
    # Testing "is a duplicate on identity but not on close" is not enough: with
    # closes A, A and B the two A rows mask each other, so one A survives on a
    # date the archive disagrees about.
    conflicting = df.groupby(identity, dropna=False)["close"].transform("nunique") > 1
    for position, is_conflict in enumerate(conflicting.tolist()):
        if is_conflict:
            reasons[position].append("conflicting duplicate row")
    if conflicting.any():
        failures.append(f"conflicting indices candidate rows: {int(conflicting.sum())}")
    collapsed = int(exact_duplicate.sum())
    if collapsed:
        warnings.append(f"collapsed {collapsed} identical repeated rows")

    rejected_mask = pd.Series([bool(entry) for entry in reasons], index=df.index)
    # Drop the collapsed repeats before splitting, so they are neither accepted
    # twice nor counted as rejections.
    rejected_mask = rejected_mask & ~exact_duplicate
    df = df.loc[~(exact_duplicate & ~rejected_mask)]
    reasons = [entry for entry, drop in zip(reasons, exact_duplicate) if not drop]
    rejected_mask = rejected_mask.loc[df.index]
    rejected = df.loc[rejected_mask].copy()
    if not rejected.empty:
        rejected["rejection_reason"] = ["; ".join(entry) for entry in reasons if entry]
        failures.append(f"rejected indices rows: {int(rejected_mask.sum())}")
    accepted = df.loc[~rejected_mask].copy()

    for index_name, discontinued_on in SERIES_DISCONTINUED.items():
        late = accepted[(accepted["index_name"] == index_name) & (accepted["date"] > discontinued_on)]
        if not late.empty:
            warnings.append(
                f"{index_name} has {len(late)} rows after its {discontinued_on.isoformat()} discontinuation"
            )

    coverage = _coverage(accepted)
    if not accepted.empty:
        accepted = accepted.sort_values(["index_name", "date"]).reset_index(drop=True)
    metrics = _metrics(len(df), len(accepted), len(rejected), failures, warnings, coverage)
    return HistoricalIndexValidationResult(accepted, rejected, failures, warnings, metrics)


def _coverage(accepted: pd.DataFrame) -> dict[str, dict[str, Any]]:
    if accepted.empty:
        return {}
    coverage: dict[str, dict[str, Any]] = {}
    for index_name, group in accepted.groupby("index_name"):
        dates = [value for value in group["date"] if isinstance(value, date)]
        coverage[str(index_name)] = {
            "rows": int(len(group)),
            "first_date": min(dates).isoformat() if dates else None,
            "last_date": max(dates).isoformat() if dates else None,
        }
    return coverage


def _metrics(
    row_count: int,
    accepted_rows: int,
    rejected_rows: int,
    failures: list[str],
    warnings: list[str],
    coverage: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "family": "indices",
        "source_name": SOURCE_NAME,
        "row_count": int(row_count),
        "accepted_rows": int(accepted_rows),
        "rejected_rows": int(rejected_rows),
        "failures": failures,
        "warnings": warnings,
        "coverage": coverage,
    }


def render_validation_report(result: HistoricalIndexValidationResult) -> str:
    metrics = result.metrics
    lines = [
        "# Historical Indices Backfill Validation",
        "",
        f"Generated: {metrics['generated_at_utc']}",
        f"Source: `{SOURCE_NAME}`",
        "",
        f"- Candidate rows: {metrics['row_count']}",
        f"- Accepted rows: {metrics['accepted_rows']}",
        f"- Rejected rows: {metrics['rejected_rows']}",
        "",
        "## Coverage",
        "",
        "| Index | Rows | First | Last |",
        "|---|---|---|---|",
    ]
    for index_name, entry in sorted(metrics.get("coverage", {}).items()):
        lines.append(f"| {index_name} | {entry['rows']} | {entry['first_date']} | {entry['last_date']} |")
    if metrics["failures"]:
        lines += ["", "## Failures", ""] + [f"- {item}" for item in metrics["failures"]]
    if metrics["warnings"]:
        lines += ["", "## Warnings", ""] + [f"- {item}" for item in metrics["warnings"]]
    return "\n".join(lines) + "\n"


def write_outputs(
    result: HistoricalIndexValidationResult,
    candidates: pd.DataFrame,
    *,
    run_digest: str,
    candidate_root: Path = CANDIDATE_ROOT,
    accepted_root: Path = ACCEPTED_ROOT,
    validation_root: Path = VALIDATION_ROOT,
) -> dict[str, Path]:
    candidate_dir = candidate_root / run_digest
    candidate_dir.mkdir(parents=True, exist_ok=True)
    accepted_root.mkdir(parents=True, exist_ok=True)
    validation_root.mkdir(parents=True, exist_ok=True)

    candidate_path = candidate_dir / "canonical_indices_candidates.csv"
    candidates.to_csv(candidate_path, index=False)

    contract = FAMILY_CONTRACTS["indices"]
    accepted_path = accepted_root / "indices_historical.csv"
    result.accepted.reindex(columns=contract.canonical_columns).to_csv(accepted_path, index=False)

    rejected_path = validation_root / "rejected_records.csv"
    result.rejected.to_csv(rejected_path, index=False)

    summary_path = validation_root / "backfill_summary.json"
    summary_path.write_text(json.dumps(result.metrics, indent=2) + "\n", encoding="utf-8")

    report_path = validation_root / "validation_report.md"
    report_path.write_text(render_validation_report(result), encoding="utf-8")

    return {
        "candidates": candidate_path,
        "accepted": accepted_path,
        "rejected": rejected_path,
        "summary": summary_path,
        "report": report_path,
    }


def run(
    source_paths: list[Path],
    *,
    include_sectors: bool = False,
) -> tuple[HistoricalIndexValidationResult, pd.DataFrame, list[str]]:
    frames: list[pd.DataFrame] = []
    warnings: list[str] = []
    for path in source_paths:
        if not path.exists():
            raise HistoricalIndexError(f"source file not found: {path}")
        frame, file_warnings = extract_index_records(path, include_sectors=include_sectors)
        frames.append(frame)
        warnings.extend(file_warnings)
    candidates = pd.concat(frames, ignore_index=True)
    result = validate_historical_index_records(candidates)
    result.metrics["warnings"].extend(warnings)
    return result, candidates, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert official CSE index workbooks to canonical index rows")
    parser.add_argument(
        "--source-path",
        type=Path,
        action="append",
        dest="source_paths",
        help="Archive CSV to read; repeatable. Defaults to the daily index and TRI workbooks.",
    )
    parser.add_argument(
        "--include-sectors",
        action="store_true",
        help="Also emit the sector index columns. Unlabelled columns are always skipped.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Validate without writing artifacts")
    parser.add_argument(
        "--allow-validation-failure",
        action="store_true",
        help="Exit 0 even when validation fails",
    )
    args = parser.parse_args()

    source_paths = args.source_paths or [DEFAULT_INDEX_FILE, DEFAULT_TRI_FILE]
    result, candidates, _ = run(source_paths, include_sectors=args.include_sectors)

    metrics = result.metrics
    print(f"candidate rows: {metrics['row_count']}")
    print(f"accepted rows:  {metrics['accepted_rows']}")
    print(f"rejected rows:  {metrics['rejected_rows']}")
    for index_name, entry in sorted(metrics.get("coverage", {}).items()):
        print(f"  {index_name:8} {entry['rows']:6} rows  {entry['first_date']} -> {entry['last_date']}")
    for item in metrics["warnings"]:
        print(f"  warning: {item}")
    for item in metrics["failures"]:
        print(f"  failure: {item}")

    if not args.dry_run:
        # Keyed by content so re-running against an updated workbook writes a
        # new directory instead of overwriting the previous candidates.
        run_digest = hashlib.sha256(
            "|".join(sha256_file(path) for path in sorted(source_paths)).encode("utf-8")
        ).hexdigest()
        paths = write_outputs(result, candidates, run_digest=run_digest)
        for label, path in paths.items():
            print(f"wrote {label}: {path.relative_to(ROOT)}")

    if metrics["failures"] and not args.allow_validation_failure:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
