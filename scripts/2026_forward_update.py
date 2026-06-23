"""Run validation-first 2026-forward ingestion jobs."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from pathlib import Path

from forward_ingestion import (
    COLOMBO_TZ,
    DATASET_FAMILIES,
    DEFAULT_METADATA,
    DEFAULT_CSE_DAILY_REPORT_URL_TEMPLATE,
    FORWARD_START_DATE,
    ForwardIngestionError,
    MissingSourceError,
    run_daily_report_ohlcv_ingestion,
    run_generic_family_ingestion,
    run_yahoo_ohlcv_ingestion,
    write_forward_summary,
)


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def date_range(start: date, end: date) -> list[date]:
    if end < start:
        raise ValueError(f"end date {end.isoformat()} is before start date {start.isoformat()}")
    days: list[date] = []
    current = start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    return days


def target_dates(args: argparse.Namespace) -> list[date]:
    if args.start_date or args.end_date:
        start = parse_date(args.start_date) if args.start_date else FORWARD_START_DATE
        end = parse_date(args.end_date) if args.end_date else datetime.now(COLOMBO_TZ).date()
        return date_range(start, end)
    if args.target_date:
        return [parse_date(args.target_date)]
    return [datetime.now(COLOMBO_TZ).date()]


def run_family_placeholder(family: str, target_date: date, allow_validation_failure: bool) -> bool:
    write_forward_summary(
        family=family,
        target_date=target_date,
        source_name="not_configured",
        status="quarantined",
        failures=[f"{family} 2026-forward source scraper is not configured yet"],
    )
    if allow_validation_failure:
        print(f"QUARANTINED: {family} source is not configured for {target_date.isoformat()}")
        return False
    raise MissingSourceError(f"{family} 2026-forward source scraper is not configured yet")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run 2026-forward source ingestion")
    parser.add_argument("--family", default="ohlcv", choices=sorted(DATASET_FAMILIES))
    parser.add_argument(
        "--ohlcv-source",
        default="cse_daily_report_pdf",
        choices=["cse_daily_report_pdf", "yahoo_finance_chart_candidate"],
        help="OHLCV source adapter to run when --family=ohlcv.",
    )
    parser.add_argument("--target-date", help="Single target date as YYYY-MM-DD")
    parser.add_argument("--start-date", help="Range start date as YYYY-MM-DD; defaults to 2026-01-01 with --end-date")
    parser.add_argument("--end-date", help="Range end date as YYYY-MM-DD; defaults to today's Colombo date")
    parser.add_argument("--source-file", type=Path, help="Local date-bearing daily report/PDF/CSV source")
    parser.add_argument(
        "--source-url-template",
        help=(
            "Official date-bearing source URL template; supports {date}, {yyyymmdd}, "
            "{ddmmyyyy}, {yyyy}, {mm}, {dd}. Defaults to the CSE StockMarketDaily "
            "PDF template for OHLCV."
        ),
    )
    parser.add_argument("--metadata-path", type=Path, default=DEFAULT_METADATA)
    parser.add_argument("--dry-run", action="store_true", help="Validate without writing accepted rows")
    parser.add_argument("--allow-missing-metadata", action="store_true")
    parser.add_argument("--allow-validation-failure", action="store_true")
    parser.add_argument("--allow-before-close", action="store_true")
    parser.add_argument("--missing-activity-threshold", type=float, default=0.0)
    parser.add_argument("--yahoo-limit", type=int, help="Limit Yahoo symbols for recon/test runs.")
    parser.add_argument("--cse-cross-check-tolerance", type=float, default=0.01)
    args = parser.parse_args()

    any_failure = False
    for target_date in target_dates(args):
        try:
            if args.family != "ohlcv":
                if args.source_file or args.source_url_template:
                    result = run_generic_family_ingestion(
                        family=args.family,
                        target_date=target_date,
                        source_file=args.source_file,
                        source_url_template=args.source_url_template,
                        dry_run=args.dry_run,
                    )
                    accepted = result.passed
                    if accepted:
                        print(
                            "PASS: accepted "
                            f"{len(result.accepted):,} 2026-forward {args.family} rows for {target_date.isoformat()}"
                        )
                    else:
                        for failure in result.failures:
                            print(f"FAIL: {failure}")
                else:
                    accepted = run_family_placeholder(args.family, target_date, args.allow_validation_failure)
            else:
                if args.ohlcv_source == "yahoo_finance_chart_candidate":
                    result = run_yahoo_ohlcv_ingestion(
                        target_date=target_date,
                        metadata_path=args.metadata_path,
                        allow_missing_metadata=args.allow_missing_metadata,
                        dry_run=args.dry_run,
                        allow_before_close=args.allow_before_close,
                        missing_activity_threshold=args.missing_activity_threshold,
                        yahoo_limit=args.yahoo_limit,
                        cse_cross_check_tolerance=args.cse_cross_check_tolerance,
                    )
                else:
                    source_url_template = args.source_url_template or DEFAULT_CSE_DAILY_REPORT_URL_TEMPLATE
                    result = run_daily_report_ohlcv_ingestion(
                        target_date=target_date,
                        source_file=args.source_file,
                        source_url_template=source_url_template,
                        metadata_path=args.metadata_path,
                        allow_missing_metadata=args.allow_missing_metadata,
                        dry_run=args.dry_run,
                        allow_before_close=args.allow_before_close,
                        missing_activity_threshold=args.missing_activity_threshold,
                    )
                accepted = result.passed
                if accepted:
                    print(
                        "PASS: accepted "
                        f"{len(result.accepted):,} 2026-forward OHLCV rows for {target_date.isoformat()}"
                    )
                else:
                    for failure in result.failures:
                        print(f"FAIL: {failure}")
            if not accepted:
                any_failure = True
        except ForwardIngestionError as exc:
            any_failure = True
            print(f"QUARANTINED: {target_date.isoformat()}: {exc}")
            if not args.allow_validation_failure:
                raise SystemExit(1) from exc

    if any_failure and not args.allow_validation_failure:
        raise SystemExit(1)
    if any_failure:
        print("VALIDATION_REJECTED: wrote 2026-forward validation summaries and skipped accepted append")


if __name__ == "__main__":
    main()
