"""Convert received historical CSE workbooks to CSV and report their contents."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import tempfile
import warnings
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any
from datetime import date, datetime

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "historical_data"
DEFAULT_OUTPUT = DEFAULT_INPUT / "csv"
DEFAULT_REPORT = DEFAULT_INPUT / "historical_data_report.md"
DEFAULT_MANIFEST = DEFAULT_INPUT / "conversion_manifest.csv"


@dataclass
class SheetReport:
    source_path: str
    source_type: str
    sheet_name: str
    output_csv: str
    rows: int
    columns: int
    column_names: list[str]
    non_empty_columns: int
    numeric_columns: list[str]
    date_ranges: dict[str, dict[str, str]]
    symbol_like_columns: list[str]
    status: str
    error: str = ""


def safe_name(value: str) -> str:
    value = re.sub(r"[^\w.\- ]+", "_", value.strip())
    value = re.sub(r"\s+", "_", value)
    return value.strip("._") or "sheet"


def relative_output_path(source: Path, sheet_name: str | None, input_root: Path, output_root: Path) -> Path:
    rel = source.relative_to(input_root)
    parent = output_root / rel.parent
    stem = safe_name(rel.stem)
    if sheet_name:
        return parent / f"{stem}__{safe_name(sheet_name)}.csv"
    return parent / f"{stem}.csv"


def convert_xls_to_xlsx(source: Path, temp_root: Path) -> Path:
    outdir = temp_root / safe_name(source.stem)
    outdir.mkdir(parents=True, exist_ok=True)
    home = temp_root / "lo-home"
    home.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    cmd = [
        "libreoffice",
        "--headless",
        "--convert-to",
        "xlsx",
        "--outdir",
        str(outdir),
        str(source),
    ]
    result = subprocess.run(cmd, check=False, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "LibreOffice conversion failed").strip())
    converted = outdir / f"{source.stem}.xlsx"
    if not converted.exists():
        candidates = sorted(outdir.glob("*.xlsx"))
        if not candidates:
            raise RuntimeError("LibreOffice did not create an xlsx file")
        converted = candidates[0]
    return converted


def clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(axis=0, how="all").dropna(axis=1, how="all")
    return df


def col_label(col: Any) -> str:
    if isinstance(col, int):
        return f"column_{col + 1}"
    return str(col).strip()


def looks_date_like(value: Any) -> bool:
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return True
    text = str(value).strip()
    if not text:
        return False
    if re.fullmatch(r"[\d,.\s]+", text):
        return False
    return bool(
        re.search(r"\d{1,4}[-/]\w{1,9}[-/]\d{1,4}", text)
        or re.search(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b", text, re.I)
    )


def date_ranges(df: pd.DataFrame) -> dict[str, dict[str, str]]:
    ranges: dict[str, dict[str, str]] = {}
    for col in df.columns:
        values = df[col].dropna()
        if values.empty:
            continue
        candidates = values[values.map(looks_date_like)]
        if candidates.empty:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            parsed = pd.to_datetime(candidates, errors="coerce", dayfirst=True)
        valid = parsed.dropna()
        if valid.empty:
            continue
        valid = valid[(valid.dt.year >= 1980) & (valid.dt.year <= 2035)]
        if valid.empty or len(valid) < 3:
            continue
        ranges[col_label(col)] = {
            "min": valid.min().date().isoformat(),
            "max": valid.max().date().isoformat(),
            "valid_rows": str(len(valid)),
        }
    return ranges


def numeric_columns(df: pd.DataFrame) -> list[str]:
    numeric: list[str] = []
    for col in df.columns:
        values = df[col].dropna()
        if values.empty:
            continue
        converted = pd.to_numeric(values.astype(str).str.replace(",", "", regex=False), errors="coerce")
        if converted.notna().mean() >= 0.75:
            numeric.append(col_label(col))
    return numeric


def symbol_like_columns(df: pd.DataFrame) -> list[str]:
    tokens = ["symbol", "security", "company", "name", "ticker", "isin"]
    found: list[str] = []
    for col in df.columns:
        values = df[col].dropna().astype(str).str.lower().head(10)
        if any(any(token in value for token in tokens) for value in values):
            found.append(col_label(col))
    return found


def analyze_csv(source: Path, output_csv: Path, input_root: Path, output_root: Path, copied: bool = False) -> SheetReport:
    df = pd.read_csv(output_csv if copied else source, dtype=object, header=None, encoding_errors="replace")
    df = clean_dataframe(df)
    if not copied:
        output_csv.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(output_csv, index=False, header=False)
    rel_source = source.relative_to(input_root).as_posix()
    rel_out = output_csv.relative_to(input_root).as_posix()
    cols = [col_label(col) for col in df.columns]
    return SheetReport(
        source_path=rel_source,
        source_type=source.suffix.lower().lstrip("."),
        sheet_name="",
        output_csv=rel_out,
        rows=int(len(df)),
        columns=int(len(df.columns)),
        column_names=cols,
        non_empty_columns=int(df.notna().any(axis=0).sum()) if not df.empty else 0,
        numeric_columns=numeric_columns(df),
        date_ranges=date_ranges(df),
        symbol_like_columns=symbol_like_columns(df),
        status="converted",
    )


def workbook_reports(source: Path, input_root: Path, output_root: Path, temp_root: Path) -> list[SheetReport]:
    workbook_path = source
    if source.suffix.lower() == ".xls":
        workbook_path = convert_xls_to_xlsx(source, temp_root)

    reports: list[SheetReport] = []
    excel = pd.ExcelFile(workbook_path, engine="openpyxl")
    for sheet_name in excel.sheet_names:
        output_csv = relative_output_path(source, sheet_name, input_root, output_root)
        try:
            df = pd.read_excel(excel, sheet_name=sheet_name, dtype=object, header=None)
            df = clean_dataframe(df)
            output_csv.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(output_csv, index=False, header=False)
            reports.append(
                SheetReport(
                    source_path=source.relative_to(input_root).as_posix(),
                    source_type=source.suffix.lower().lstrip("."),
                    sheet_name=sheet_name,
                    output_csv=output_csv.relative_to(input_root).as_posix(),
                    rows=int(len(df)),
                    columns=int(len(df.columns)),
                    column_names=[col_label(col) for col in df.columns],
                    non_empty_columns=int(df.notna().any(axis=0).sum()) if not df.empty else 0,
                    numeric_columns=numeric_columns(df),
                    date_ranges=date_ranges(df),
                    symbol_like_columns=symbol_like_columns(df),
                    status="converted",
                )
            )
        except Exception as exc:
            reports.append(
                SheetReport(
                    source_path=source.relative_to(input_root).as_posix(),
                    source_type=source.suffix.lower().lstrip("."),
                    sheet_name=sheet_name,
                    output_csv=output_csv.relative_to(input_root).as_posix(),
                    rows=0,
                    columns=0,
                    column_names=[],
                    non_empty_columns=0,
                    numeric_columns=[],
                    date_ranges={},
                    symbol_like_columns=[],
                    status="failed",
                    error=str(exc),
                )
            )
    return reports


def source_files(input_root: Path, output_root: Path) -> list[Path]:
    suffixes = {".xls", ".xlsx", ".csv"}
    files = []
    for path in input_root.rglob("*"):
        if not path.is_file():
            continue
        if output_root in path.parents:
            continue
        if path.resolve() in {DEFAULT_REPORT.resolve(), DEFAULT_MANIFEST.resolve()}:
            continue
        if path.name.startswith("~$"):
            continue
        if path.suffix.lower() in suffixes:
            files.append(path)
    return sorted(files)


def write_manifest(reports: list[SheetReport], manifest_path: Path) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(asdict(reports[0]).keys()) if reports else list(SheetReport.__dataclass_fields__.keys())
    with manifest_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for report in reports:
            row = asdict(report)
            for key in ["column_names", "numeric_columns", "date_ranges", "symbol_like_columns"]:
                row[key] = json.dumps(row[key], sort_keys=True)
            writer.writerow(row)


def summarize_by_area(reports: list[SheetReport]) -> dict[str, dict[str, Any]]:
    areas: dict[str, dict[str, Any]] = {}
    for report in reports:
        area = report.source_path.split("/", 1)[0] if "/" in report.source_path else "."
        stats = areas.setdefault(area, {"tables": 0, "rows": 0, "failed": 0, "sources": set()})
        stats["tables"] += 1
        stats["rows"] += report.rows
        stats["failed"] += int(report.status != "converted")
        stats["sources"].add(report.source_path)
    for stats in areas.values():
        stats["sources"] = len(stats["sources"])
    return areas


def summarize_by_source(reports: list[SheetReport]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for report in reports:
        stats = grouped.setdefault(
            report.source_path,
            {
                "source_path": report.source_path,
                "source_type": report.source_type,
                "tables": 0,
                "rows": 0,
                "max_columns": 0,
                "failed": 0,
                "date_min": None,
                "date_max": None,
            },
        )
        stats["tables"] += 1
        stats["rows"] += report.rows
        stats["max_columns"] = max(stats["max_columns"], report.columns)
        stats["failed"] += int(report.status != "converted")
        for date_range in report.date_ranges.values():
            date_min = date_range.get("min")
            date_max = date_range.get("max")
            if date_min and (stats["date_min"] is None or date_min < stats["date_min"]):
                stats["date_min"] = date_min
            if date_max and (stats["date_max"] is None or date_max > stats["date_max"]):
                stats["date_max"] = date_max
    return [grouped[key] for key in sorted(grouped)]


def write_report(reports: list[SheetReport], report_path: Path, input_root: Path, output_root: Path, manifest: Path) -> None:
    converted = [report for report in reports if report.status == "converted"]
    failed = [report for report in reports if report.status != "converted"]
    areas = summarize_by_area(reports)
    source_count = len({report.source_path for report in reports})
    total_rows = sum(report.rows for report in converted)
    total_tables = len(converted)
    daily_price = [report for report in converted if "Daily Shares Price List" in report.source_path]

    lines = [
        "# Historical Data Inventory and Conversion Report",
        "",
        "## Summary",
        "",
        f"- Source root: `{input_root.relative_to(ROOT)}`",
        f"- CSV output root: `{output_root.relative_to(ROOT)}`",
        f"- Conversion manifest: `{manifest.relative_to(ROOT)}`",
        f"- Source files inventoried: {source_count}",
        f"- Converted tables/sheets: {total_tables}",
        f"- Failed tables/sheets: {len(failed)}",
        f"- Total non-empty rows across converted tables: {total_rows:,}",
        "",
        "## Data Areas",
        "",
        "| Area | Source Files | Converted Tables | Rows | Failed Tables |",
        "|---|---:|---:|---:|---:|",
    ]
    for area, stats in sorted(areas.items()):
        lines.append(f"| `{area}` | {stats['sources']} | {stats['tables'] - stats['failed']} | {stats['rows']:,} | {stats['failed']} |")

    lines.extend([
        "",
        "## Source File Summary",
        "",
        "| Source File | Type | Tables/Sheets | Rows | Max Columns | Detected Date Coverage | Failed Tables |",
        "|---|---|---:|---:|---:|---|---:|",
    ])
    for stats in summarize_by_source(reports):
        coverage = (
            f"{stats['date_min']} to {stats['date_max']}"
            if stats["date_min"] and stats["date_max"]
            else ""
        )
        lines.append(
            f"| `{stats['source_path']}` | `{stats['source_type']}` | {stats['tables']} | {stats['rows']:,} | {stats['max_columns']} | {coverage} | {stats['failed']} |"
        )

    lines.extend([
        "",
        "## High-Value Dataset Groups",
        "",
        "### Daily Share Price Lists",
        "",
        "These files are the primary historical OHLCV candidate source. They cover the yearly CSE daily share price lists in `stock_data/32Daily Shares Price List -2011-2020/` and `stock_data/33Daily Shares Price List -2021-2025/`.",
        "",
        "| Source | Sheet | Rows | Columns | Date Range Columns | Output CSV |",
        "|---|---|---:|---:|---|---|",
    ])
    for report in daily_price:
        date_summary = "; ".join(f"{col}: {rng['min']} to {rng['max']}" for col, rng in report.date_ranges.items()) or "n/a"
        lines.append(
            f"| `{report.source_path}` | `{report.sheet_name}` | {report.rows:,} | {report.columns} | {date_summary} | `{report.output_csv}` |"
        )

    lines.extend([
        "",
        "### Market-Wide and Corporate Action Files",
        "",
        "The stock data directory also contains corporate actions, index levels, market statistics, CDS activity, foreign activity, sector statistics, public/foreign holding, beta, and GICS files. These are useful for validating and enriching price history, but each table still needs semantic normalization before it can enter a published dataset.",
        "",
        "### Macro and Rates Files",
        "",
        "The policy/rates directory contains exchange-rate CSV data and a policy-interest-rate workbook. These can support macro joins after source freshness and date-granularity checks are defined.",
        "",
        "## Converted Tables",
        "",
        "| Source | Type | Sheet | Rows | Columns | Date Ranges | Symbol-like Columns | Numeric Columns | Output CSV |",
        "|---|---|---|---:|---:|---|---|---|---|",
    ])

    for report in converted:
        date_summary = "; ".join(f"{col}: {rng['min']} to {rng['max']}" for col, rng in report.date_ranges.items()) or ""
        symbol_cols = ", ".join(report.symbol_like_columns)
        numeric_cols = ", ".join(report.numeric_columns[:12])
        if len(report.numeric_columns) > 12:
            numeric_cols += f", ... (+{len(report.numeric_columns) - 12})"
        lines.append(
            f"| `{report.source_path}` | `{report.source_type}` | `{report.sheet_name}` | {report.rows:,} | {report.columns} | {date_summary} | {symbol_cols} | {numeric_cols} | `{report.output_csv}` |"
        )

    if failed:
        lines.extend(["", "## Conversion Failures", "", "| Source | Sheet | Error |", "|---|---|---|"])
        for report in failed:
            lines.append(f"| `{report.source_path}` | `{report.sheet_name}` | {report.error} |")

    lines.extend([
        "",
        "## Next Normalization Work",
        "",
        "- Define canonical schemas for the daily share price lists and each supporting data family.",
        "- Parse dates and security identifiers from the converted daily price CSVs, then validate against listing metadata.",
        "- Use these CSE-provided historical files as a candidate backfill source, but still run source-date, duplicate, OHLC, missing-field, and repeated-digest gates before accepting rows.",
        "- Keep converted raw CSVs separate from accepted canonical OHLCV until validation is complete.",
        "",
    ])
    report_path.write_text("\n".join(lines))


def convert_all(input_root: Path, output_root: Path, report_path: Path, manifest_path: Path) -> list[SheetReport]:
    if output_root.exists():
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    reports: list[SheetReport] = []
    with tempfile.TemporaryDirectory(prefix="cse-historical-convert-") as tmp:
        temp_root = Path(tmp)
        for source in source_files(input_root, output_root):
            try:
                if source.suffix.lower() == ".csv":
                    output_csv = relative_output_path(source, None, input_root, output_root)
                    reports.append(analyze_csv(source, output_csv, input_root, output_root))
                else:
                    reports.extend(workbook_reports(source, input_root, output_root, temp_root))
            except Exception as exc:
                reports.append(
                    SheetReport(
                        source_path=source.relative_to(input_root).as_posix(),
                        source_type=source.suffix.lower().lstrip("."),
                        sheet_name="",
                        output_csv="",
                        rows=0,
                        columns=0,
                        column_names=[],
                        non_empty_columns=0,
                        numeric_columns=[],
                        date_ranges={},
                        symbol_like_columns=[],
                        status="failed",
                        error=str(exc),
                    )
                )

    write_manifest(reports, manifest_path)
    write_report(reports, report_path, input_root, output_root, manifest_path)
    return reports


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert historical CSE workbooks to CSV")
    parser.add_argument("--input-root", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()

    reports = convert_all(args.input_root, args.output_root, args.report, args.manifest)
    converted = sum(1 for report in reports if report.status == "converted")
    failed = len(reports) - converted
    print(f"Converted {converted} tables/sheets; failed {failed}.")
    print(f"Report: {args.report}")
    print(f"Manifest: {args.manifest}")


if __name__ == "__main__":
    main()
