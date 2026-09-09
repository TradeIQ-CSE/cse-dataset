"""Daily forward collection of the CSE headline indices.

Runs the settled ``dailyMarketSummery`` payload through the shared ``indices``
family contract and writes candidate, accepted and quarantine artifacts beside
the OHLCV run. The source is snapshot-only, so the target date defaults to the
current Colombo date and a payload stamped with any other date is quarantined.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from pathlib import Path

try:
    from .forward_ingestion import (
        COLOMBO_TZ,
        RAW_ROOT,
        VALIDATION_ROOT,
        ForwardIngestionError,
        GenericValidationResult,
        require_forward_target_date,
        validate_generic_family_records,
        write_accepted_rows,
        write_candidate_rows,
        write_fetch_metadata,
        write_forward_summary,
        write_generic_validation_outputs,
    )
    from .indices_sources import CSEDailyMarketSummaryIndicesAdapter
except ImportError:  # pragma: no cover - used when scripts are executed directly.
    from forward_ingestion import (
        COLOMBO_TZ,
        RAW_ROOT,
        VALIDATION_ROOT,
        ForwardIngestionError,
        GenericValidationResult,
        require_forward_target_date,
        validate_generic_family_records,
        write_accepted_rows,
        write_candidate_rows,
        write_fetch_metadata,
        write_forward_summary,
        write_generic_validation_outputs,
    )
    from indices_sources import CSEDailyMarketSummaryIndicesAdapter

FAMILY = "indices"


def colombo_today() -> date:
    return datetime.now(timezone.utc).astimezone(COLOMBO_TZ).date()


def run_daily_indices_ingestion(
    *,
    target_date: date,
    adapter: CSEDailyMarketSummaryIndicesAdapter | None = None,
    raw_root: Path = RAW_ROOT,
    dry_run: bool = False,
) -> GenericValidationResult:
    require_forward_target_date(target_date)
    adapter = adapter or CSEDailyMarketSummaryIndicesAdapter()

    try:
        fetch = adapter.fetch_for_date(target_date, raw_root=raw_root)
        records = adapter.normalize(
            fetch.payload,
            report_date=fetch.report_date or target_date,
            payload_hash=fetch.payload_hash,
        )
    except ForwardIngestionError as exc:
        write_forward_summary(
            family=FAMILY,
            target_date=target_date,
            source_name=adapter.source_name,
            status="quarantined",
            failures=[str(exc)],
        )
        raise

    source_date_failures = adapter.validate_source_date(records, target_date, fetch.report_date)
    result = validate_generic_family_records(
        records,
        family=FAMILY,
        target_date=target_date,
        source_date_failures=source_date_failures,
    )
    status = "accepted" if result.passed else "quarantined"

    if not dry_run:
        write_fetch_metadata(fetch, status)
        write_candidate_rows(FAMILY, target_date, adapter.source_name, records)
        validation_dir = VALIDATION_ROOT / FAMILY / target_date.isoformat() / adapter.source_name
        write_generic_validation_outputs(result, output_dir=validation_dir, source_name=adapter.source_name)
        if result.passed:
            write_accepted_rows(FAMILY, target_date, adapter.source_name, result.accepted, raw_root=raw_root)

    write_forward_summary(
        family=FAMILY,
        target_date=target_date,
        source_name=adapter.source_name,
        status=status,
        failures=result.failures,
        warnings=result.warnings,
        candidate_rows=len(records),
        accepted_rows=len(result.accepted),
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect the CSE headline indices for one trading date")
    parser.add_argument(
        "--target-date",
        type=date.fromisoformat,
        default=None,
        help="Trading date to collect; defaults to the current Colombo date.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Validate without writing artifacts")
    parser.add_argument(
        "--allow-validation-failure",
        action="store_true",
        help="Exit 0 even when the payload is quarantined",
    )
    args = parser.parse_args()

    target_date = args.target_date or colombo_today()
    try:
        result = run_daily_indices_ingestion(target_date=target_date, dry_run=args.dry_run)
    except ForwardIngestionError as exc:
        print(f"quarantined: {exc}")
        return 0 if args.allow_validation_failure else 1

    print(f"target date:   {target_date.isoformat()}")
    print(f"candidate rows:{result.metrics['row_count']:>4}")
    print(f"accepted rows: {result.metrics['accepted_rows']:>4}")
    print(f"rejected rows: {result.metrics['rejected_rows']:>4}")
    for _, row in result.accepted.iterrows():
        print(f"  {row['index_name']:8} {row['close']}")
    for item in result.failures:
        print(f"  failure: {item}")

    if result.failures and not args.allow_validation_failure:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
