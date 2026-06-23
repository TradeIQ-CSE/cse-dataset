"""Generate accepted/quarantined/missing OHLCV coverage reports.

The report intentionally separates three concepts:

* accepted canonical artifacts under data/raw/*/accepted
* quarantined validation attempts under data/processed/validation
* source-observed dates from historical candidate files

The weekday calendar view is only a proxy because CSE holidays are not modeled
here. Source-observed dates are the stronger signal for real processing holes.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

try:
    from .audit_historical_ohlcv import candidate_files, parse_file
except ImportError:  # pragma: no cover - used when executed as a script.
    from audit_historical_ohlcv import candidate_files, parse_file


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_START = date(1991, 1, 1)
DEFAULT_END = datetime.now().date()
DEFAULT_HISTORICAL_ROOT = ROOT / "historical_data/csv"
DEFAULT_OUTPUT_DIR = ROOT / "data/processed/validation/ohlcv_coverage"
ACCEPTED_ROOTS = [
    ROOT / "data/raw/ohlcv/accepted",
    ROOT / "data/raw/2026_forward/accepted/ohlcv",
]
VALIDATION_ROOTS = [
    ROOT / "data/processed/validation/ohlcv",
    ROOT / "data/processed/validation/2026_forward/ohlcv",
]
BACKFILL_CANDIDATE_ROOT = ROOT / "data/processed/ohlcv_backfill/candidates"
DAILY_RUN_LOG = ROOT / "docs/daily_ohlcv_runs.jsonl"


@dataclass
class AcceptedInfo:
    sources: set[str] = field(default_factory=set)
    files: list[str] = field(default_factory=list)
    row_count: int = 0
    symbol_count: int = 0


@dataclass
class ValidationInfo:
    sources: set[str] = field(default_factory=set)
    statuses: set[str] = field(default_factory=set)
    files: list[str] = field(default_factory=list)
    row_count: int = 0
    accepted_rows: int = 0
    rejected_rows: int = 0
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class SourceObservedInfo:
    schemas: set[str] = field(default_factory=set)
    files: set[str] = field(default_factory=set)
    row_count: int = 0
    full_ohlcv_rows: int = 0


@dataclass
class RunLogInfo:
    runs: int = 0
    accepted_rows: int = 0
    rejected_rows: int = 0
    failures: list[str] = field(default_factory=list)
    urls: set[str] = field(default_factory=set)


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def count_csv_rows(path: Path) -> tuple[int, int]:
    """Return row count and distinct symbol count for a canonical CSV."""
    try:
        frame = pd.read_csv(path, usecols=lambda column: column in {"symbol"})
    except ValueError:
        frame = pd.read_csv(path)
    row_count = int(len(frame))
    symbol_count = int(frame["symbol"].nunique()) if "symbol" in frame.columns else 0
    return row_count, symbol_count


def collect_accepted(
    accepted_roots: list[Path] = ACCEPTED_ROOTS,
    *,
    start_date: date,
    end_date: date,
) -> dict[date, AcceptedInfo]:
    accepted: dict[date, AcceptedInfo] = defaultdict(AcceptedInfo)
    for root in accepted_roots:
        if not root.exists():
            continue
        for path in sorted(root.glob("*/*/canonical_ohlcv.csv")):
            try:
                target_date = parse_date(path.parts[-3])
            except ValueError:
                continue
            if target_date < start_date or target_date > end_date:
                continue
            rows, symbols = count_csv_rows(path)
            info = accepted[target_date]
            info.sources.add(path.parts[-2])
            info.files.append(display_path(path))
            info.row_count += rows
            info.symbol_count = max(info.symbol_count, symbols)
    return accepted


def validation_status(summary: dict[str, Any]) -> str:
    failures = summary.get("failures") or []
    rejected_rows = int(summary.get("rejected_rows") or 0)
    accepted_rows = int(summary.get("accepted_rows") or 0)
    status = str(summary.get("status") or "").lower()
    if status in {"quarantined", "failed", "rejected"}:
        return "quarantined"
    if failures or rejected_rows > 0 or accepted_rows == 0:
        return "quarantined"
    return "accepted_validation"


def collect_validation(
    validation_roots: list[Path] = VALIDATION_ROOTS,
    *,
    start_date: date,
    end_date: date,
) -> dict[date, ValidationInfo]:
    validation: dict[date, ValidationInfo] = defaultdict(ValidationInfo)
    for root in validation_roots:
        if not root.exists():
            continue
        for path in sorted([*root.glob("*/*/quality_summary.json"), *root.glob("*/*/forward_summary.json")]):
            try:
                target_date = parse_date(path.parts[-3])
            except ValueError:
                continue
            if target_date < start_date or target_date > end_date:
                continue
            with path.open() as handle:
                summary = json.load(handle)
            source = str(summary.get("source_name") or path.parts[-2])
            info = validation[target_date]
            status = validation_status(summary)
            info.sources.add(source)
            info.statuses.add(status)
            info.files.append(display_path(path))
            info.row_count += int(summary.get("row_count") or summary.get("candidate_rows") or 0)
            info.accepted_rows += int(summary.get("accepted_rows") or 0)
            info.rejected_rows += int(summary.get("rejected_rows") or 0)
            info.failures.extend(str(item) for item in (summary.get("failures") or []))
            info.warnings.extend(str(item) for item in (summary.get("warnings") or []))
    return validation


def collect_historical_source_observed(
    historical_root: Path = DEFAULT_HISTORICAL_ROOT,
    *,
    start_date: date,
    end_date: date,
) -> tuple[dict[date, SourceObservedInfo], list[dict[str, str]]]:
    observed: dict[date, SourceObservedInfo] = defaultdict(SourceObservedInfo)
    failures: list[dict[str, str]] = []
    for path in candidate_files(historical_root):
        try:
            parsed = parse_file(path)
        except Exception as exc:  # pragma: no cover - exact parser failures are source dependent.
            failures.append({"source_path": display_path(path), "error": str(exc)})
            continue
        if parsed.records.empty or "date" not in parsed.records.columns:
            continue
        records = parsed.records.copy()
        records["date"] = pd.to_datetime(records["date"], errors="coerce").dt.date
        records = records[(records["date"] >= start_date) & (records["date"] <= end_date)]
        if records.empty:
            continue
        full_ohlcv_available = {"open", "high", "low", "close"}.issubset(parsed.present_fields)
        for target_date, group in records.groupby("date", sort=True):
            info = observed[target_date]
            info.schemas.add(parsed.schema)
            info.files.add(display_path(path))
            info.row_count += int(len(group))
            if full_ohlcv_available:
                info.full_ohlcv_rows += int(len(group))
    return observed, failures


def collect_backfill_candidates(
    candidate_root: Path = BACKFILL_CANDIDATE_ROOT,
    *,
    start_date: date,
    end_date: date,
) -> dict[date, SourceObservedInfo]:
    observed: dict[date, SourceObservedInfo] = defaultdict(SourceObservedInfo)
    if not candidate_root.exists():
        return observed
    for path in sorted(candidate_root.glob("*/canonical_ohlcv_candidates.csv")):
        try:
            frame = pd.read_csv(path, usecols=["date"])
        except ValueError:
            continue
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
        frame = frame[(frame["date"] >= start_date) & (frame["date"] <= end_date)]
        for target_date, group in frame.groupby("date", sort=True):
            info = observed[target_date]
            info.schemas.add("canonical_ohlcv_candidate")
            info.files.add(display_path(path))
            info.row_count += int(len(group))
            info.full_ohlcv_rows += int(len(group))
    return observed


def merge_source_observed(*sources: dict[date, SourceObservedInfo]) -> dict[date, SourceObservedInfo]:
    merged: dict[date, SourceObservedInfo] = defaultdict(SourceObservedInfo)
    for source in sources:
        for target_date, info in source.items():
            target = merged[target_date]
            target.schemas.update(info.schemas)
            target.files.update(info.files)
            target.row_count += info.row_count
            target.full_ohlcv_rows += info.full_ohlcv_rows
    return merged


def collect_daily_run_log(
    path: Path = DAILY_RUN_LOG,
    *,
    start_date: date,
    end_date: date,
) -> dict[date, RunLogInfo]:
    logs: dict[date, RunLogInfo] = defaultdict(RunLogInfo)
    if not path.exists():
        return logs
    with path.open() as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            target_text = record.get("target_date")
            if not target_text:
                continue
            try:
                target_date = parse_date(str(target_text))
            except ValueError:
                continue
            if target_date < start_date or target_date > end_date:
                continue
            info = logs[target_date]
            info.runs += 1
            info.accepted_rows = max(info.accepted_rows, int(record.get("accepted_rows") or 0))
            info.rejected_rows = max(info.rejected_rows, int(record.get("rejected_rows") or 0))
            info.failures.extend(str(item) for item in (record.get("failures") or []))
            if record.get("github_run_url"):
                info.urls.add(str(record["github_run_url"]))
    return logs


def classify_date(
    *,
    accepted: AcceptedInfo | None,
    validation: ValidationInfo | None,
    source_observed: SourceObservedInfo | None,
    run_log: RunLogInfo | None,
    is_weekday: bool,
) -> str:
    if accepted and accepted.row_count > 0:
        return "accepted"
    if validation and "quarantined" in validation.statuses:
        return "quarantined"
    if source_observed and source_observed.row_count > 0:
        return "candidate_unvalidated"
    if run_log and run_log.accepted_rows > 0:
        return "run_log_only_missing_artifact"
    if is_weekday:
        return "missing_no_source_observed"
    return "weekend"


def date_range(start_date: date, end_date: date) -> list[date]:
    return [value.date() for value in pd.date_range(start_date, end_date, freq="D")]


def pct(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round((numerator / denominator) * 100, 2)


def join_values(values: set[str] | list[str], *, limit: int = 20) -> str:
    deduped = sorted({str(value) for value in values if str(value)})
    shown = deduped[:limit]
    suffix = f"; ... +{len(deduped) - limit} more" if len(deduped) > limit else ""
    return "; ".join(shown) + suffix


def build_coverage(
    *,
    start_date: date,
    end_date: date,
    accepted: dict[date, AcceptedInfo],
    validation: dict[date, ValidationInfo],
    source_observed: dict[date, SourceObservedInfo],
    run_logs: dict[date, RunLogInfo],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for target_date in date_range(start_date, end_date):
        accepted_info = accepted.get(target_date)
        validation_info = validation.get(target_date)
        observed_info = source_observed.get(target_date)
        run_info = run_logs.get(target_date)
        is_weekday = target_date.weekday() < 5
        status = classify_date(
            accepted=accepted_info,
            validation=validation_info,
            source_observed=observed_info,
            run_log=run_info,
            is_weekday=is_weekday,
        )
        has_quarantined_attempt = bool(validation_info and "quarantined" in validation_info.statuses)
        rows.append(
            {
                "date": target_date.isoformat(),
                "year": target_date.year,
                "weekday": target_date.strftime("%A"),
                "is_weekday": is_weekday,
                "status": status,
                "accepted_sources": join_values(accepted_info.sources) if accepted_info else "",
                "accepted_rows": accepted_info.row_count if accepted_info else 0,
                "accepted_symbols": accepted_info.symbol_count if accepted_info else 0,
                "accepted_files": join_values(accepted_info.files, limit=5) if accepted_info else "",
                "validation_sources": join_values(validation_info.sources) if validation_info else "",
                "validation_statuses": join_values(validation_info.statuses) if validation_info else "",
                "validation_rows": validation_info.row_count if validation_info else 0,
                "validation_accepted_rows": validation_info.accepted_rows if validation_info else 0,
                "validation_rejected_rows": validation_info.rejected_rows if validation_info else 0,
                "has_quarantined_attempt": has_quarantined_attempt,
                "validation_failures_sample": join_values(validation_info.failures, limit=5) if validation_info else "",
                "validation_warnings_sample": join_values(validation_info.warnings, limit=5) if validation_info else "",
                "source_observed": bool(observed_info and observed_info.row_count > 0),
                "source_observed_rows": observed_info.row_count if observed_info else 0,
                "source_observed_full_ohlcv_rows": observed_info.full_ohlcv_rows if observed_info else 0,
                "source_observed_schemas": join_values(observed_info.schemas) if observed_info else "",
                "source_observed_files": len(observed_info.files) if observed_info else 0,
                "daily_run_log_runs": run_info.runs if run_info else 0,
                "daily_run_log_accepted_rows": run_info.accepted_rows if run_info else 0,
                "daily_run_log_rejected_rows": run_info.rejected_rows if run_info else 0,
                "daily_run_log_failures_sample": join_values(run_info.failures, limit=5) if run_info else "",
                "daily_run_log_urls": join_values(run_info.urls, limit=3) if run_info else "",
            }
        )
    return pd.DataFrame(rows)


def summarize_by_year(coverage: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for year, group in coverage.groupby("year", sort=True):
        weekdays = int(group["is_weekday"].sum())
        source_observed_dates = int(group["source_observed"].sum())
        accepted_dates = int((group["status"] == "accepted").sum())
        quarantined_dates = int((group["status"] == "quarantined").sum())
        candidate_unvalidated_dates = int((group["status"] == "candidate_unvalidated").sum())
        run_log_only_dates = int((group["status"] == "run_log_only_missing_artifact").sum())
        missing_weekday_dates = int((group["status"] == "missing_no_source_observed").sum())
        rows.append(
            {
                "year": int(year),
                "calendar_days_in_scope": int(len(group)),
                "weekdays_calendar_proxy": weekdays,
                "source_observed_dates": source_observed_dates,
                "accepted_dates": accepted_dates,
                "quarantined_dates": quarantined_dates,
                "candidate_unvalidated_dates": candidate_unvalidated_dates,
                "run_log_only_missing_artifact_dates": run_log_only_dates,
                "missing_weekday_dates_calendar_proxy": missing_weekday_dates,
                "accepted_rows": int(group["accepted_rows"].sum()),
                "validation_rejected_rows": int(group["validation_rejected_rows"].sum()),
                "source_observed_rows": int(group["source_observed_rows"].sum()),
                "source_observed_full_ohlcv_rows": int(group["source_observed_full_ohlcv_rows"].sum()),
                "accepted_pct_of_source_observed_dates": pct(accepted_dates, source_observed_dates),
                "accepted_pct_of_weekday_calendar_proxy": pct(accepted_dates, weekdays),
            }
        )
    return pd.DataFrame(rows)


def status_counts(coverage: pd.DataFrame) -> dict[str, int]:
    return {str(key): int(value) for key, value in coverage["status"].value_counts().sort_index().items()}


def write_report(
    *,
    output_dir: Path,
    start_date: date,
    end_date: date,
    coverage: pd.DataFrame,
    by_year: pd.DataFrame,
    parse_failures: list[dict[str, str]],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    coverage.to_csv(output_dir / "ohlcv_coverage_by_date.csv", index=False)
    by_year.to_csv(output_dir / "ohlcv_coverage_by_year.csv", index=False)
    coverage[coverage["status"] == "quarantined"].to_csv(output_dir / "ohlcv_quarantined_dates.csv", index=False)
    coverage[coverage["status"] == "candidate_unvalidated"].to_csv(
        output_dir / "ohlcv_candidate_unvalidated_dates.csv",
        index=False,
    )
    coverage[coverage["status"] == "run_log_only_missing_artifact"].to_csv(
        output_dir / "ohlcv_run_log_only_missing_artifact_dates.csv",
        index=False,
    )
    coverage[coverage["status"] == "missing_no_source_observed"].to_csv(
        output_dir / "ohlcv_missing_weekday_calendar_proxy.csv",
        index=False,
    )
    pd.DataFrame(parse_failures).to_csv(output_dir / "ohlcv_source_parse_failures.csv", index=False)

    summary = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "status_counts": status_counts(coverage),
        "accepted_dates": int((coverage["status"] == "accepted").sum()),
        "quarantined_dates": int((coverage["status"] == "quarantined").sum()),
        "candidate_unvalidated_dates": int((coverage["status"] == "candidate_unvalidated").sum()),
        "run_log_only_missing_artifact_dates": int((coverage["status"] == "run_log_only_missing_artifact").sum()),
        "missing_weekday_dates_calendar_proxy": int((coverage["status"] == "missing_no_source_observed").sum()),
        "source_observed_dates": int(coverage["source_observed"].sum()),
        "source_parse_failures": len(parse_failures),
        "note": "missing_weekday_dates_calendar_proxy excludes weekends but does not exclude CSE holidays.",
    }
    (output_dir / "ohlcv_coverage_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    write_markdown_report(output_dir, summary, by_year, coverage, parse_failures)


def write_markdown_report(
    output_dir: Path,
    summary: dict[str, Any],
    by_year: pd.DataFrame,
    coverage: pd.DataFrame,
    parse_failures: list[dict[str, str]],
) -> None:
    top_missing_years = by_year.sort_values(
        ["missing_weekday_dates_calendar_proxy", "candidate_unvalidated_dates"],
        ascending=False,
    ).head(10)
    quarantined = coverage[coverage["status"] == "quarantined"].copy()
    candidate = coverage[coverage["status"] == "candidate_unvalidated"].copy()
    lines = [
        "# OHLCV Coverage Report",
        "",
        f"Generated: `{summary['generated_at_utc']}`",
        f"Scope: `{summary['start_date']}` to `{summary['end_date']}`",
        "",
        "## Interpretation",
        "",
        "- `accepted` means a canonical OHLCV CSV exists under an accepted transaction directory.",
        "- `quarantined` means validation evidence exists but no accepted artifact exists for that date.",
        "- `candidate_unvalidated` means a source file contains the date, but no validation/accepted artifact exists yet.",
        "- `run_log_only_missing_artifact` means a daily run log reports accepted rows, but no accepted canonical CSV is present locally.",
        "- `missing_no_source_observed` is a weekday calendar proxy only; CSE holidays are not removed.",
        "",
        "## Headline Counts",
        "",
        f"- Accepted dates: {summary['accepted_dates']:,}",
        f"- Quarantined dates: {summary['quarantined_dates']:,}",
        f"- Candidate but unvalidated dates: {summary['candidate_unvalidated_dates']:,}",
        f"- Run-log-only accepted dates missing artifacts: {summary['run_log_only_missing_artifact_dates']:,}",
        f"- Missing weekdays, calendar proxy: {summary['missing_weekday_dates_calendar_proxy']:,}",
        f"- Source-observed dates: {summary['source_observed_dates']:,}",
        f"- Historical source parse failures: {summary['source_parse_failures']:,}",
        "",
        "## Yearly Coverage",
        "",
        "| Year | Weekdays | Source-Observed | Accepted | Quarantined | Candidate Unvalidated | Run-Log Only | Missing Weekdays | Accepted % Source-Observed |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for _, row in by_year.iterrows():
        source_pct = row["accepted_pct_of_source_observed_dates"]
        pct_text = "" if pd.isna(source_pct) else f"{float(source_pct):.2f}"
        lines.append(
            "| {year} | {weekdays:,} | {observed:,} | {accepted:,} | {quarantined:,} | {candidate:,} | {run_log_only:,} | {missing:,} | {pct} |".format(
                year=int(row["year"]),
                weekdays=int(row["weekdays_calendar_proxy"]),
                observed=int(row["source_observed_dates"]),
                accepted=int(row["accepted_dates"]),
                quarantined=int(row["quarantined_dates"]),
                candidate=int(row["candidate_unvalidated_dates"]),
                run_log_only=int(row["run_log_only_missing_artifact_dates"]),
                missing=int(row["missing_weekday_dates_calendar_proxy"]),
                pct=pct_text,
            )
        )
    lines.extend(["", "## Largest Calendar-Proxy Holes", ""])
    for _, row in top_missing_years.iterrows():
        lines.append(
            f"- {int(row['year'])}: {int(row['missing_weekday_dates_calendar_proxy']):,} weekday dates with no observed source; "
            f"{int(row['candidate_unvalidated_dates']):,} source-observed dates still unvalidated"
        )
    lines.extend(["", "## Quarantined Dates", ""])
    if quarantined.empty:
        lines.append("- none")
    else:
        for _, row in quarantined.head(30).iterrows():
            lines.append(
                f"- `{row['date']}`: sources={row['validation_sources'] or 'unknown'}, "
                f"rejected_rows={int(row['validation_rejected_rows'])}, failures={row['validation_failures_sample'] or 'n/a'}"
            )
        if len(quarantined) > 30:
            lines.append(f"- ... {len(quarantined) - 30:,} more in `ohlcv_quarantined_dates.csv`")
    lines.extend(["", "## Candidate But Unvalidated Dates", ""])
    if candidate.empty:
        lines.append("- none")
    else:
        for _, row in candidate.head(30).iterrows():
            lines.append(
                f"- `{row['date']}`: source_rows={int(row['source_observed_rows'])}, "
                f"schemas={row['source_observed_schemas'] or 'unknown'}"
            )
        if len(candidate) > 30:
            lines.append(f"- ... {len(candidate) - 30:,} more in `ohlcv_candidate_unvalidated_dates.csv`")
    lines.extend(["", "## Output Files", ""])
    for name in [
        "ohlcv_coverage_summary.json",
        "ohlcv_coverage_by_year.csv",
        "ohlcv_coverage_by_date.csv",
        "ohlcv_quarantined_dates.csv",
        "ohlcv_candidate_unvalidated_dates.csv",
        "ohlcv_run_log_only_missing_artifact_dates.csv",
        "ohlcv_missing_weekday_calendar_proxy.csv",
        "ohlcv_source_parse_failures.csv",
    ]:
        lines.append(f"- `{name}`")
    (output_dir / "ohlcv_coverage_report.md").write_text("\n".join(lines) + "\n")


def run_report(
    *,
    start_date: date = DEFAULT_START,
    end_date: date = DEFAULT_END,
    historical_root: Path = DEFAULT_HISTORICAL_ROOT,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    accepted = collect_accepted(start_date=start_date, end_date=end_date)
    validation = collect_validation(start_date=start_date, end_date=end_date)
    historical_observed, parse_failures = collect_historical_source_observed(
        historical_root,
        start_date=start_date,
        end_date=end_date,
    )
    candidate_observed = collect_backfill_candidates(start_date=start_date, end_date=end_date)
    source_observed = merge_source_observed(historical_observed, candidate_observed)
    run_logs = collect_daily_run_log(start_date=start_date, end_date=end_date)
    coverage = build_coverage(
        start_date=start_date,
        end_date=end_date,
        accepted=accepted,
        validation=validation,
        source_observed=source_observed,
        run_logs=run_logs,
    )
    by_year = summarize_by_year(coverage)
    write_report(
        output_dir=output_dir,
        start_date=start_date,
        end_date=end_date,
        coverage=coverage,
        by_year=by_year,
        parse_failures=parse_failures,
    )
    return coverage, by_year


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate accepted/quarantined/missing OHLCV coverage report")
    parser.add_argument("--start-date", default=DEFAULT_START.isoformat())
    parser.add_argument("--end-date", default=DEFAULT_END.isoformat())
    parser.add_argument("--historical-root", type=Path, default=DEFAULT_HISTORICAL_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)
    if end_date < start_date:
        raise SystemExit("--end-date must be on or after --start-date")
    coverage, by_year = run_report(
        start_date=start_date,
        end_date=end_date,
        historical_root=args.historical_root,
        output_dir=args.output_dir,
    )
    print(
        "PASS: wrote OHLCV coverage report to "
        f"{display_path(args.output_dir)} "
        f"({int((coverage['status'] == 'accepted').sum()):,} accepted dates, "
        f"{int((coverage['status'] == 'quarantined').sum()):,} quarantined dates, "
        f"{int((coverage['status'] == 'candidate_unvalidated').sum()):,} candidate-unvalidated dates)"
    )
    print(f"Years covered: {int(by_year['year'].min())}-{int(by_year['year'].max())}")


if __name__ == "__main__":
    main()
