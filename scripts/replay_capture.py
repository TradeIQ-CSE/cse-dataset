"""Re-validate a saved capture's raw payload with the current rules.

Every run stores the exact bytes CSE served, so a session that was quarantined
by a bug in the validator is not lost — it only needs validating again once the
bug is fixed. TIQ-134 and TIQ-136 did exactly that: 20 of the first 64 captures
were rejected over rights lines the metadata step filtered out and over
CINS.X0000's zero volume, and both are fixed. Their captures still hold no
accepted rows, because acceptance was decided at capture time.

This replays one capture through today's adapter and validator and stages the
result in the layout ``publish_eod`` expects, so the backfill can deliver a
session that only became deliverable later. Nothing is fetched from CSE, and
the capture itself is never written to.
"""

from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

try:
    from .ohlcv_sources import (
        FetchResult,
        make_adapter,
        payload_hash,
        snapshot_session_date,
    )
    from .ohlcv_validation import load_metadata, validate_ohlcv_records
except ImportError:  # pragma: no cover - used when scripts are executed directly.
    from ohlcv_sources import (
        FetchResult,
        make_adapter,
        payload_hash,
        snapshot_session_date,
    )
    from ohlcv_validation import load_metadata, validate_ohlcv_records

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_METADATA = ROOT / "data/processed/company_metadata.csv"

PAYLOAD_GLOB = "data/raw/ohlcv/source_payloads/*/*/payload.json"


class ReplayError(RuntimeError):
    """The capture cannot be replayed at all."""


@dataclass
class ReplayOutcome:
    session: date
    source_name: str
    passed: bool
    accepted_rows: int
    failures: list[str]
    staged_manifest: Path | None


def find_payload(run_dir: Path) -> tuple[Path, date, str]:
    """Locate the one raw payload a capture holds, with its session and source.

    The session is read from the rows' own lastTradedTime, not the directory
    the payload sits in. Captures taken before TIQ-134 were filed under the
    run's Colombo date, so a run that started after midnight filed the previous
    session under the next day — 2026-08-28's capture is stored under
    2026-08-29. The payload itself is the only trustworthy witness of which
    session it holds, and it is also what the validator checks against.
    """
    payloads = sorted(run_dir.glob(PAYLOAD_GLOB))
    if not payloads:
        raise ReplayError(f"{run_dir} holds no raw OHLCV payload")
    if len(payloads) > 1:
        raise ReplayError(
            f"{run_dir} holds {len(payloads)} raw payloads; expected exactly one"
        )
    payload_path = payloads[0]
    source_name = payload_path.parent.name
    try:
        payload = json.loads(payload_path.read_text())
    except json.JSONDecodeError as exc:
        raise ReplayError(f"{payload_path} is not readable JSON") from exc
    session = snapshot_session_date(payload.get("reqTradeSummery") or [])
    if session is None:
        raise ReplayError(
            f"{payload_path} has no single session date in its lastTradedTime values"
        )
    return payload_path, session, source_name


def _captured_at(payload_path: Path) -> str:
    """The fetch time recorded beside the payload, in RFC 3339 UTC.

    The delivery contract needs a real capture instant; falling back to now()
    would claim a session was captured today, which is precisely the error
    TIQ-134 fixed. A capture without one cannot be delivered.
    """
    metadata_path = payload_path.parent / "metadata.json"
    if not metadata_path.is_file():
        raise ReplayError(f"{payload_path.parent} has no fetch metadata")
    metadata = json.loads(metadata_path.read_text())
    captured_at = metadata.get("fetch_time_utc")
    if not isinstance(captured_at, str) or not captured_at:
        raise ReplayError(f"{metadata_path} records no fetch_time_utc")
    try:
        datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReplayError(f"{metadata_path} has an unparseable fetch_time_utc") from exc
    return captured_at


def replay_capture(
    run_dir: Path,
    *,
    staging_root: Path,
    metadata_path: Path | None = None,
    missing_activity_threshold: float = 0.0,
) -> ReplayOutcome:
    payload_path, session, source_name = find_payload(run_dir)
    captured_at = _captured_at(payload_path)
    payload = json.loads(payload_path.read_text())

    adapter = make_adapter(source_name)
    rows = payload.get("reqTradeSummery") or []
    fetch_result = FetchResult(
        source_name=source_name,
        requested_date=session,
        # The session is what the capture was filed under, which TIQ-134 made
        # the rows' own lastTradedTime rather than the run's date.
        observed_source_date=session,
        fetch_time_utc=datetime.fromisoformat(captured_at.replace("Z", "+00:00")).astimezone(
            timezone.utc
        ),
        source_url=adapter.source_url,
        payload=payload,
        payload_hash=payload_hash(payload),
        row_count=len(rows),
        raw_payload_path=payload_path,
    )

    # Today's metadata, not the capture's: the missing rights lines that
    # rejected these sessions are exactly what has since been added.
    chosen_metadata = metadata_path or DEFAULT_METADATA
    if not chosen_metadata.is_file():
        chosen_metadata = run_dir / "data/processed/company_metadata.csv"
    metadata = load_metadata(chosen_metadata)

    records = adapter.normalize(payload, fetch_result)
    result = validate_ohlcv_records(
        records,
        target_date=session,
        source_date_failures=adapter.validate_source_date(records, session),
        metadata=metadata,
        # The platform refuses a digest it has already accepted for another
        # date, so the cross-day staleness check belongs there, not to a
        # replay walking many days in one process.
        previous_manifest={},
        missing_value_threshold=missing_activity_threshold,
    )

    if not result.passed:
        return ReplayOutcome(
            session=session,
            source_name=source_name,
            passed=False,
            accepted_rows=int(len(result.accepted)),
            failures=list(result.failures),
            staged_manifest=None,
        )

    staged = staging_root / session.isoformat()
    accepted_rel = Path(
        f"data/raw/ohlcv/accepted/{session.isoformat()}/{source_name}/canonical_ohlcv.csv"
    )
    metadata_rel = Path("data/processed/company_metadata.csv")
    (staged / accepted_rel).parent.mkdir(parents=True, exist_ok=True)
    result.accepted.to_csv(staged / accepted_rel, index=False)
    (staged / metadata_rel).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(chosen_metadata, staged / metadata_rel)

    manifest_path = staged / "data/run/daily_result.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "contract_version": "1",
                "status": "accepted",
                "target_date": session.isoformat(),
                "source_name": source_name,
                "captured_at": captured_at,
                "source_date_method": "last_traded_time",
                "raw_payload_hash": fetch_result.payload_hash,
                "accepted_path": str(accepted_rel),
                "metadata_path": str(metadata_rel),
                "replayed_from": str(run_dir),
                "validation": result.metrics,
            },
            indent=2,
        )
        + "\n"
    )
    return ReplayOutcome(
        session=session,
        source_name=source_name,
        passed=True,
        accepted_rows=int(len(result.accepted)),
        failures=[],
        staged_manifest=manifest_path,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-validate one saved capture with the current rules"
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--staging-root", type=Path, required=True)
    parser.add_argument("--metadata-path", type=Path)
    parser.add_argument("--missing-activity-threshold", type=float, default=0.0)
    args = parser.parse_args()

    outcome = replay_capture(
        args.run_dir,
        staging_root=args.staging_root,
        metadata_path=args.metadata_path,
        missing_activity_threshold=args.missing_activity_threshold,
    )
    if outcome.passed:
        print(
            f"ACCEPTED {outcome.session.isoformat()}: {outcome.accepted_rows} rows "
            f"staged at {outcome.staged_manifest}"
        )
        return 0
    print(f"REJECTED {outcome.session.isoformat()}: {'; '.join(outcome.failures)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
