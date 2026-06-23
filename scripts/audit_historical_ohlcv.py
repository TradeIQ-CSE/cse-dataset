"""Audit official historical OHLCV source files for missing values.

This is read-only. It scans converted CSE daily share price CSV files, classifies
their source schema, and reports both true blanks in present source columns and
canonical OHLCV fields that are absent from older source formats.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

try:
    from .backfill_ohlcv import (
        canonical_column_map,
        find_header_row,
        HEADER_ALIASES,
        normalize_header,
        numeric_series,
        read_ragged_csv,
        security_symbols,
    )
except ImportError:  # pragma: no cover - used when executed as a script.
    from backfill_ohlcv import (
        canonical_column_map,
        find_header_row,
        HEADER_ALIASES,
        normalize_header,
        numeric_series,
        read_ragged_csv,
        security_symbols,
    )


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = ROOT / "historical_data/csv"
DEFAULT_OUT = ROOT / "data/processed/validation/ohlcv_historical_audit"
DAILY_SHARE_PRICE_DIR_MARKER = "daily shares price list"
PRICE_COLUMNS = ["open", "high", "low", "close"]
ACTIVITY_COLUMNS = ["volume", "turnover", "trades"]


@dataclass(frozen=True)
class ParsedSource:
    schema: str
    records: pd.DataFrame
    present_fields: set[str]
    warnings: list[str]


def candidate_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix.lower() == ".csv" else []

    files: list[Path] = []
    for path in sorted(root.rglob("*.csv")):
        parts = [part.lower() for part in path.parts]
        if any(DAILY_SHARE_PRICE_DIR_MARKER in part for part in parts):
            files.append(path)
    return files


def parse_file(path: Path) -> ParsedSource:
    raw = read_ragged_csv(path)
    if raw.empty:
        raise ValueError("empty csv")
    if looks_flat_ohlcv(raw):
        try:
            return parse_flat_ohlcv(raw)
        except Exception:
            pass
    if looks_close_only(raw, path):
        close_only = parse_close_only(raw)
        if not close_only.records.empty:
            return close_only
    grouped = parse_grouped_high_low(raw)
    if not grouped.records.empty:
        return grouped
    raise ValueError("unsupported daily price layout")


def sampled_text(raw: pd.DataFrame, rows: int = 25) -> str:
    if raw.empty:
        return ""
    sample = raw.head(rows).fillna("").astype(str)
    return " ".join(normalize_header(value) for value in sample.to_numpy().ravel() if str(value).strip())


def looks_flat_ohlcv(raw: pd.DataFrame) -> bool:
    text = sampled_text(raw)
    return "company id" in text and "trading date" in text and "price high" in text


def looks_close_only(raw: pd.DataFrame, path: Path) -> bool:
    text = sampled_text(raw)
    name = path.name.lower()
    return (
        "cloprc" in name
        or ("security da" in text and "price" in text)
        or ("security id" in text and "closing price" in text)
    )


def parse_flat_ohlcv(raw: pd.DataFrame) -> ParsedSource:
    header_idx = find_header_row(raw)
    columns = [str(value).strip() for value in raw.iloc[header_idx].tolist()]
    mapping = audit_column_map(columns)
    table = raw.iloc[header_idx + 1 :].copy()
    table.columns = columns

    records = pd.DataFrame()
    records["date"] = parse_dates(table[mapping["trading_date"]]).dt.date.astype(str)
    records["symbol"] = security_symbols(table[mapping["company_id"]], table[mapping["main_type"]], table[mapping["sub_type"]])
    if "open" in mapping:
        records["open"] = numeric_series(table[mapping["open"]])
    else:
        records["open"] = pd.NA
    records["high"] = numeric_series(table[mapping["high"]])
    records["low"] = numeric_series(table[mapping["low"]])
    records["close"] = numeric_series(table[mapping["close"]])
    records["volume"] = numeric_series(table[mapping["volume"]])
    records["turnover"] = numeric_series(table[mapping["turnover"]])
    records["trades"] = numeric_series(table[mapping["trades"]])
    records = records[(records["symbol"].notna()) & (records["date"] != "NaT")].copy()
    present_fields = {*PRICE_COLUMNS, *ACTIVITY_COLUMNS}
    schema = "flat_ohlcv"
    if "open" not in mapping:
        present_fields.remove("open")
        schema = "flat_high_low_close_no_open"
    return ParsedSource(
        schema=schema,
        records=records,
        present_fields=present_fields,
        warnings=[],
    )


def audit_column_map(columns: list[Any]) -> dict[str, str]:
    try:
        return canonical_column_map(columns)
    except ValueError:
        pass
    normalized = {normalize_header(column): str(column).strip() for column in columns}
    mapping: dict[str, str] = {}
    for canonical, aliases in HEADER_ALIASES.items():
        if canonical == "open":
            continue
        for alias in aliases:
            if alias in normalized:
                mapping[canonical] = normalized[alias]
                break
    missing = sorted((set(HEADER_ALIASES) - {"open"}) - set(mapping))
    if missing:
        raise ValueError(f"daily share price file is missing required columns: {', '.join(missing)}")
    for alias in HEADER_ALIASES["open"]:
        if alias in normalized:
            mapping["open"] = normalized[alias]
            break
    return mapping


def parse_grouped_high_low(raw: pd.DataFrame) -> ParsedSource:
    first_col = raw.iloc[:, 0].fillna("").astype(str).str.strip()
    company_mask = first_col.str.contains("company id", case=False, regex=False, na=False)
    if not bool(company_mask.any()):
        return ParsedSource(
            schema="grouped_high_low_close_no_open",
            records=pd.DataFrame(),
            present_fields={"high", "low", "close", "volume", "turnover", "trades"},
            warnings=[],
        )

    company = pd.Series([None] * len(raw), index=raw.index, dtype=object)
    main_type = pd.Series([None] * len(raw), index=raw.index, dtype=object)
    sub_type = pd.Series([None] * len(raw), index=raw.index, dtype=object)
    for idx in raw.index[company_mask]:
        values = [None if pd.isna(value) else str(value).strip() for value in raw.loc[idx].tolist()]
        company.loc[idx], main_type.loc[idx], sub_type.loc[idx] = grouped_symbol_parts(values)

    company = company.ffill()
    main_type = main_type.ffill()
    sub_type = sub_type.ffill()
    date_source = first_col
    date_mask = date_source.str.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}", na=False)
    dates = parse_dates(date_source)
    valid = date_mask & dates.notna() & company.notna()
    if not bool(valid.any()):
        records = pd.DataFrame()
    else:
        records = pd.DataFrame(
            {
                "date": dates[valid].dt.date.astype(str).to_numpy(),
                "symbol": [
                    symbol_from_group_parts(row_company, row_main, row_sub)
                    for row_company, row_main, row_sub in zip(company[valid], main_type[valid], sub_type[valid], strict=False)
                ],
                "open": pd.NA,
                "high": numeric_series(raw.iloc[:, 2])[valid].to_numpy(),
                "low": numeric_series(raw.iloc[:, 4])[valid].to_numpy(),
                "close": numeric_series(raw.iloc[:, 5])[valid].to_numpy(),
                "trades": numeric_series(raw.iloc[:, 6])[valid].to_numpy(),
                "volume": numeric_series(raw.iloc[:, 7])[valid].to_numpy(),
                "turnover": numeric_series(raw.iloc[:, 8])[valid].to_numpy(),
            }
        )
        records = records[records["symbol"].notna()].copy()

    return ParsedSource(
        schema="grouped_high_low_close_no_open",
        records=records,
        present_fields={"high", "low", "close", "volume", "turnover", "trades"},
        warnings=[],
    )


def grouped_symbol_parts(values: list[str | None]) -> tuple[str | None, str | None, str | None]:
    joined = " ".join(value for value in values if value)
    company = value_after_label(values, "Company Id") or regex_group(joined, r"company\s*id\s*,?\s*:?\s*([A-Za-z0-9]+)")
    main = (
        value_after_label(values, "Security Type")
        or value_after_label(values, "Type")
        or regex_group(joined, r"(?:security\s*)?type\s*:?\s*([A-Za-z])")
    )
    sub = value_after_label(values, "Sub Type") or regex_group(joined, r"sub\s*ty\s*pe\s*:?\s*([A-Za-z0-9]+)")
    return company, main, sub


def regex_group(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return clean_label_value(match.group(1)) if match else None


def parse_close_only(raw: pd.DataFrame) -> ParsedSource:
    frames: list[pd.DataFrame] = []
    volume_seen = False
    for offset in range(0, max(raw.shape[1] - 2, 0)):
        date_source = raw.iloc[:, offset].fillna("").astype(str).str.strip()
        date_mask = date_source.str.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}", na=False)
        if not bool(date_mask.any()):
            continue
        dates = parse_dates(date_source)
        symbols = raw.iloc[:, offset + 1].fillna("").astype(str).str.strip()
        close = numeric_series(raw.iloc[:, offset + 2])
        valid = date_mask & dates.notna() & symbols.ne("") & close.notna()
        if not bool(valid.any()):
            continue
        frame = pd.DataFrame(
            {
                "date": dates[valid].dt.date.astype(str).to_numpy(),
                "symbol": symbols[valid].map(normalize_legacy_symbol).to_numpy(),
                "close": close[valid].to_numpy(),
            }
        )
        if offset + 3 < raw.shape[1]:
            volume = numeric_series(raw.iloc[:, offset + 3])
            if bool(volume[valid].notna().any()):
                frame["volume"] = volume[valid].to_numpy()
                volume_seen = True
        frames.append(frame)

    records = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    for column in ["open", "high", "low", "volume", "turnover", "trades"]:
        if column not in records:
            records[column] = pd.NA
    present_fields = {"close"}
    if volume_seen:
        present_fields.add("volume")
    return ParsedSource(
        schema="close_only",
        records=records,
        present_fields=present_fields,
        warnings=["source format does not provide open/high/low/turnover/trades"],
    )


def value_after_label(values: list[str | None], label: str) -> str | None:
    normalized_label = normalize_header(label)
    for idx, value in enumerate(values):
        if value is None:
            continue
        text = str(value)
        normalized = normalize_header(text)
        if normalized.startswith(normalized_label):
            after_colon = text.split(":", 1)[1].strip() if ":" in text else ""
            if after_colon:
                return clean_label_value(after_colon)
            if idx + 1 < len(values) and values[idx + 1]:
                return clean_label_value(str(values[idx + 1]))
    return None


def clean_label_value(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip().lstrip(":,").strip()
    return cleaned or None


def looks_like_date(value: str | None) -> bool:
    return bool(value and re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}", str(value).strip()))


def parse_dates(values: Any) -> pd.Series:
    return pd.to_datetime(values, errors="coerce", format="mixed")


def symbol_from_group_parts(company_id: str | None, main_type: str | None, sub_type: str | None) -> str | None:
    if company_id is None or pd.isna(company_id):
        return None
    main = "N" if main_type is None or pd.isna(main_type) else str(main_type).strip().upper()
    subtype_text = "0000" if sub_type is None or pd.isna(sub_type) else str(sub_type).strip()
    try:
        subtype = f"{int(float(subtype_text)):04d}"
    except ValueError:
        subtype = subtype_text.zfill(4)[-4:]
    return f"{company_id.strip().upper()}.{main}{subtype}"


def normalize_legacy_symbol(symbol: str) -> str:
    text = symbol.strip().upper()
    match = re.match(r"^([A-Z0-9]+)-([A-Z])-([A-Z0-9]+)$", text)
    if match:
        base, main, subtype = match.groups()
        try:
            normalized_subtype = f"{int(float(subtype)):04d}"
        except ValueError:
            normalized_subtype = subtype.zfill(4)[-4:]
        return f"{base}.{main}{normalized_subtype}"
    return text


def parse_cell(values: list[str | None], idx: int) -> float | None:
    if idx >= len(values):
        return None
    value = values[idx]
    if value is None or str(value).strip() in {"", "-", "nan", "None"}:
        return None
    parsed = pd.to_numeric(str(value).strip().replace(",", ""), errors="coerce")
    return None if pd.isna(parsed) else float(parsed)


def audit_parsed(path: Path, parsed: ParsedSource) -> dict[str, Any]:
    records = parsed.records.copy()
    row_count = int(len(records))
    missing_counts = {
        column: int(records[column].isna().sum()) if column in records.columns else row_count
        for column in [*PRICE_COLUMNS, *ACTIVITY_COLUMNS]
    }
    blank_present_counts = {
        column: missing_counts[column]
        for column in parsed.present_fields
        if column in missing_counts and missing_counts[column] > 0
    }
    absent_canonical_fields = sorted(set([*PRICE_COLUMNS, *ACTIVITY_COLUMNS]) - parsed.present_fields)
    ohlc_available = all(column in parsed.present_fields for column in PRICE_COLUMNS)
    invalid_bounds = 0
    if {"open", "high", "low", "close"}.issubset(records.columns):
        price = records[PRICE_COLUMNS].apply(pd.to_numeric, errors="coerce")
        complete = price.notna().all(axis=1)
        invalid_bounds = int(
            (
                complete
                & (
                    (price["high"] < price[["open", "low", "close"]].max(axis=1))
                    | (price["low"] > price[["open", "high", "close"]].min(axis=1))
                )
            ).sum()
        )
    return {
        "source_path": str(path.relative_to(ROOT)),
        "schema": parsed.schema,
        "row_count": row_count,
        "date_count": int(records["date"].nunique()) if row_count and "date" in records else 0,
        "min_date": str(records["date"].min()) if row_count and "date" in records else None,
        "max_date": str(records["date"].max()) if row_count and "date" in records else None,
        "symbol_count": int(records["symbol"].nunique()) if row_count and "symbol" in records else 0,
        "ohlcv_available": ohlc_available,
        "absent_canonical_fields": ",".join(absent_canonical_fields),
        "blank_present_fields": json.dumps(blank_present_counts, sort_keys=True),
        "missing_open": missing_counts["open"],
        "missing_high": missing_counts["high"],
        "missing_low": missing_counts["low"],
        "missing_close": missing_counts["close"],
        "missing_volume": missing_counts["volume"],
        "missing_turnover": missing_counts["turnover"],
        "missing_trades": missing_counts["trades"],
        "invalid_ohlc_bounds": invalid_bounds,
        "warnings": "; ".join(sorted(set(parsed.warnings))[:10]),
    }


def missing_detail_rows(path: Path, parsed: ParsedSource) -> list[dict[str, Any]]:
    records = parsed.records.copy()
    details: list[dict[str, Any]] = []
    present = sorted(parsed.present_fields)
    if records.empty:
        return details
    missing_mask = records[present].isna().any(axis=1) if present else pd.Series(False, index=records.index)
    for _, row in records.loc[missing_mask].iterrows():
        missing_fields = [column for column in present if pd.isna(row.get(column))]
        details.append(
            {
                "source_path": str(path.relative_to(ROOT)),
                "schema": parsed.schema,
                "date": row.get("date"),
                "symbol": row.get("symbol"),
                "missing_present_fields": ",".join(missing_fields),
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "volume": row.get("volume"),
                "turnover": row.get("turnover"),
                "trades": row.get("trades"),
            }
        )
    return details


def run_audit(root: Path = DEFAULT_ROOT, output_dir: Path = DEFAULT_OUT) -> None:
    summary_rows: list[dict[str, Any]] = []
    detail_rows: list[dict[str, Any]] = []
    parse_failures: list[dict[str, str]] = []
    for path in candidate_files(root):
        try:
            parsed = parse_file(path)
        except Exception as exc:
            parse_failures.append({"source_path": str(path.relative_to(ROOT)), "error": str(exc)})
            continue
        summary_rows.append(audit_parsed(path, parsed))
        detail_rows.extend(missing_detail_rows(path, parsed))

    output_dir.mkdir(parents=True, exist_ok=True)
    summary = pd.DataFrame(summary_rows).sort_values(["min_date", "source_path"], na_position="last")
    details = pd.DataFrame(detail_rows).sort_values(["date", "symbol", "source_path"], na_position="last")
    failures = pd.DataFrame(parse_failures)
    summary.to_csv(output_dir / "historical_ohlcv_missing_summary.csv", index=False)
    details.to_csv(output_dir / "historical_ohlcv_missing_details.csv", index=False)
    failures.to_csv(output_dir / "historical_ohlcv_parse_failures.csv", index=False)
    write_markdown_report(output_dir, summary, details, failures)


def write_markdown_report(output_dir: Path, summary: pd.DataFrame, details: pd.DataFrame, failures: pd.DataFrame) -> None:
    full_ohlcv = summary[summary["ohlcv_available"]] if not summary.empty else pd.DataFrame()
    with_blanks = full_ohlcv[full_ohlcv["blank_present_fields"] != "{}"] if not full_ohlcv.empty else pd.DataFrame()
    lines = [
        "# Historical OHLCV Missing Data Audit",
        "",
        f"Generated: `{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}`",
        "",
        "## Summary",
        "",
        f"- Files parsed: {len(summary):,}",
        f"- Files not parsed: {len(failures):,}",
        f"- Full OHLCV files parsed: {len(full_ohlcv):,}",
        f"- Full OHLCV files with blanks in present fields: {len(with_blanks):,}",
        f"- Rows with blanks in present source fields: {len(details):,}",
        "",
        "## Full OHLCV Files With Blanks",
        "",
    ]
    if with_blanks.empty:
        lines.append("- none")
    else:
        for _, row in with_blanks.iterrows():
            lines.append(
                f"- `{row['source_path']}`: rows={row['row_count']:,}, "
                f"blank_present_fields={row['blank_present_fields']}"
            )
    lines.extend(["", "## Non-Full-OHLCV Source Schemas", ""])
    non_full = summary[~summary["ohlcv_available"]] if not summary.empty else pd.DataFrame()
    if non_full.empty:
        lines.append("- none")
    else:
        for schema, group in non_full.groupby("schema"):
            lines.append(f"- `{schema}`: {len(group):,} files; absent fields examples: `{group.iloc[0]['absent_canonical_fields']}`")
    lines.extend(["", "## Non-Full-OHLCV Files With Blanks In Present Fields", ""])
    non_full_with_blanks = non_full[non_full["blank_present_fields"] != "{}"] if not non_full.empty else pd.DataFrame()
    if non_full_with_blanks.empty:
        lines.append("- none")
    else:
        for _, row in non_full_with_blanks.iterrows():
            lines.append(
                f"- `{row['source_path']}`: schema={row['schema']}, rows={row['row_count']:,}, "
                f"blank_present_fields={row['blank_present_fields']}"
            )
    lines.extend(["", "## Parse Failures", ""])
    if failures.empty:
        lines.append("- none")
    else:
        for _, row in failures.iterrows():
            lines.append(f"- `{row['source_path']}`: {row['error']}")
    (output_dir / "historical_ohlcv_missing_report.md").write_text("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit historical CSE daily share price files for missing OHLCV data")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    run_audit(root=args.root, output_dir=args.output_dir)
    print(f"PASS: wrote historical OHLCV missing-data audit to {args.output_dir.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
