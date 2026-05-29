"""Append the latest OHLCV validation summary to tracked audit logs."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
VALIDATION_ROOT = ROOT / "data/processed/validation/ohlcv"
JSONL_LOG = ROOT / "docs/daily_ohlcv_runs.jsonl"
MD_LOG = ROOT / "docs/daily_ohlcv_runs.md"


def latest_summary_path() -> Path:
    summaries = sorted(
        VALIDATION_ROOT.glob("*/*/quality_summary.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not summaries:
        raise SystemExit(f"No OHLCV validation summaries found under {VALIDATION_ROOT}")
    return summaries[0]


def github_run_url() -> str | None:
    server = os.getenv("GITHUB_SERVER_URL")
    repo = os.getenv("GITHUB_REPOSITORY")
    run_id = os.getenv("GITHUB_RUN_ID")
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return None


def build_record(summary_path: Path) -> dict[str, Any]:
    summary = json.loads(summary_path.read_text())
    source_name = summary_path.parent.name
    record = {
        "recorded_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "target_date": summary.get("target_date"),
        "source_name": source_name,
        "github_run_id": os.getenv("GITHUB_RUN_ID"),
        "github_run_attempt": os.getenv("GITHUB_RUN_ATTEMPT"),
        "github_run_number": os.getenv("GITHUB_RUN_NUMBER"),
        "github_run_url": github_run_url(),
        "head_sha": os.getenv("GITHUB_SHA"),
        "branch": os.getenv("GITHUB_REF_NAME"),
        "row_count": summary.get("row_count"),
        "accepted_rows": summary.get("accepted_rows"),
        "rejected_rows": summary.get("rejected_rows"),
        "accepted_symbols": summary.get("accepted_symbols"),
        "source_ohlc_invalid_rows": summary.get("source_ohlc_invalid_rows"),
        "ohlc_repaired_rows": summary.get("ohlc_repaired_rows"),
        "ohlc_invalid_rows": summary.get("ohlc_invalid_rows"),
        "market_digest": summary.get("market_digest"),
        "failures": summary.get("failures", []),
        "warnings": summary.get("warnings", []),
    }
    return record


def load_records() -> list[dict[str, Any]]:
    if not JSONL_LOG.exists():
        return []
    records = []
    for line in JSONL_LOG.read_text().splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def record_key(record: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
    return (
        record.get("github_run_id"),
        record.get("github_run_attempt"),
        record.get("target_date"),
        record.get("source_name"),
    )


def write_records(records: list[dict[str, Any]]) -> None:
    records = sorted(records, key=lambda item: (item.get("target_date") or "", item.get("recorded_at_utc") or ""))
    JSONL_LOG.write_text("".join(json.dumps(record, sort_keys=True) + "\n" for record in records))

    lines = [
        "# Daily OHLCV Run Log",
        "",
        "This file is a tracked audit trail of daily validation summaries. Raw",
        "payloads and generated datasets are not committed; they remain workflow",
        "artifacts until publication gates are re-enabled.",
        "",
        "| Target Date | Status | Source | Accepted Rows | Rejected Rows | Repairs | Digest | Run |",
        "|---|---|---|---:|---:|---:|---|---|",
    ]
    for record in records[-30:]:
        failures = record.get("failures") or []
        status = "FAIL" if failures else "PASS"
        digest = (record.get("market_digest") or "")[:12]
        run_url = record.get("github_run_url")
        run = f"[{record.get('github_run_id')}]({run_url})" if run_url else "local"
        lines.append(
            "| {target_date} | {status} | `{source}` | {accepted} | {rejected} | {repairs} | `{digest}` | {run} |".format(
                target_date=record.get("target_date") or "",
                status=status,
                source=record.get("source_name") or "",
                accepted=record.get("accepted_rows") or 0,
                rejected=record.get("rejected_rows") or 0,
                repairs=record.get("ohlc_repaired_rows") or 0,
                digest=digest,
                run=run,
            )
        )
    MD_LOG.write_text("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Record latest OHLCV validation summary")
    parser.add_argument("--summary", type=Path, help="Explicit quality_summary.json path")
    args = parser.parse_args()

    summary_path = args.summary or latest_summary_path()
    new_record = build_record(summary_path)
    records = load_records()
    records = [record for record in records if record_key(record) != record_key(new_record)]
    records.append(new_record)
    write_records(records)
    print(f"Recorded OHLCV validation run for {new_record['target_date']} from {new_record['source_name']}")


if __name__ == "__main__":
    main()
