"""Send every saved session the platform does not have yet.

The daily workflow delivers the session it just captured and nothing else, so
one failed run, one outage on the platform, or a day the validator quarantined
leaves a hole that never closes by itself. This walks the captures store from
the oldest session forward and delivers each one, which makes the daily path
self-healing: whatever did not land yesterday lands on the next run.

The platform is the authority on what it already holds. A price batch for a
date that already has prices comes back 409, which is exactly the answer
"already delivered", so no local ledger of what has been sent is kept — one
would only drift from the truth. Index values re-sent unchanged are a no-op by
contract, so they are simply sent again.

Usage:

    uv run python scripts/backfill_deliveries.py \\
      --captures-path ../cse-dataset-captures \\
      --calendar-path config/trading_calendar.csv \\
      --out-dir data/run/backfill
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import requests

try:
    from .publish_eod import DeliveryConflict, DeliveryError
    from .publish_eod import build_request as build_price_request
    from .publish_eod import deliver as deliver_prices
    from .publish_indices import build_request as build_index_request
    from .publish_indices import deliver as deliver_indices
    from .replay_capture import ReplayError, replay_capture
except ImportError:  # pragma: no cover - used when scripts are executed directly.
    from publish_eod import DeliveryConflict, DeliveryError
    from publish_eod import build_request as build_price_request
    from publish_eod import deliver as deliver_prices
    from publish_indices import build_request as build_index_request
    from publish_indices import deliver as deliver_indices
    from replay_capture import ReplayError, replay_capture

SESSION_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")

RAW_SUBROOT = Path("data/raw/2026_forward")
VALIDATION_SUBROOT = Path("data/processed/validation/2026_forward")
RESULT_MANIFEST = Path("data/run/daily_result.json")


@dataclass
class SessionOutcome:
    session: str
    run_id: str | None = None
    prices: str = "skipped"
    prices_detail: str = ""
    indices: str = "skipped"
    indices_detail: str = ""

    @property
    def failed(self) -> bool:
        return self.prices == "failed" or self.indices == "failed"


@dataclass
class BackfillReport:
    started_at: str
    sessions: list[SessionOutcome] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for outcome in self.sessions:
            for kind in (outcome.prices, outcome.indices):
                counts[kind] = counts.get(kind, 0) + 1
        return counts


def discover_sessions(
    captures_path: Path,
    *,
    since: date | None = None,
    until: date | None = None,
) -> list[date]:
    if not captures_path.is_dir():
        raise DeliveryError(f"captures path {captures_path} is not a directory")
    sessions = []
    for child in captures_path.iterdir():
        if not child.is_dir() or not SESSION_DIR.fullmatch(child.name):
            continue
        session = date.fromisoformat(child.name)
        if since and session < since:
            continue
        if until and session > until:
            continue
        sessions.append(session)
    return sorted(sessions)


def choose_run(captures_path: Path, session: date) -> Path:
    """Pick the run directory that holds this session's accepted prices.

    A session can have several runs: a manual re-run, or a scheduled run that
    started after midnight. Only one can be delivered, and two accepted runs
    with different digests would be a real disagreement about the day, so the
    choice is never guessed — the newest accepted run wins only when the others
    agree with it.
    """
    session_dir = captures_path / session.isoformat()
    candidates = []
    for run_dir in sorted(session_dir.iterdir()):
        manifest = run_dir / RESULT_MANIFEST
        if not manifest.is_file():
            continue
        try:
            result = json.loads(manifest.read_text())
        except json.JSONDecodeError:
            continue
        if result.get("status") != "accepted":
            continue
        if result.get("target_date") != session.isoformat():
            continue
        candidates.append((run_dir, result.get("validation", {}).get("market_digest")))
    if not candidates:
        raise DeliveryError(f"{session.isoformat()} has no accepted capture")
    digests = {digest for _, digest in candidates if digest}
    if len(digests) > 1:
        raise DeliveryError(
            f"{session.isoformat()} has accepted captures with different market "
            f"digests ({len(digests)}); resolve it by hand rather than picking one"
        )
    return candidates[-1][0]


def replay_session(
    captures_path: Path,
    session: date,
    *,
    staging_root: Path,
    metadata_path: Path | None = None,
) -> tuple[Path, Path]:
    """Re-validate a session's saved payloads and stage the result.

    Returns the run directory the data came from and the staged root to build
    the delivery against. Acceptance was decided at capture time, and most of
    the store predates the TIQ-134 and TIQ-136 fixes, so replaying is what
    makes those sessions deliverable at all — a capture rejected then over a
    rights line passes now without anything being re-fetched.
    """
    session_dir = captures_path / session.isoformat()
    if not session_dir.is_dir():
        raise DeliveryError(f"{session.isoformat()} is not in the captures store")

    passed: list[tuple[Path, str]] = []
    rejections: list[str] = []
    for run_dir in sorted(session_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        # One unreadable capture must cost only its own session. Everything
        # inside this boundary reads or writes files belonging to this one run,
        # so OSError and ValueError from it are as local as a ReplayError;
        # anything else is a defect or a shared misconfiguration and must stay
        # visible rather than be recorded as a rejected day.
        try:
            outcome = replay_capture(
                run_dir,
                staging_root=staging_root / run_dir.name,
                metadata_path=metadata_path,
            )
            if outcome.session != session:
                rejections.append(
                    f"{run_dir.name}: holds {outcome.session.isoformat()}, not this session"
                )
                continue
            if not outcome.passed:
                rejections.append(f"{run_dir.name}: {'; '.join(outcome.failures)}")
                continue
            manifest = json.loads(outcome.staged_manifest.read_text())
            digest = manifest.get("validation", {}).get("market_digest", "")
        except (ReplayError, OSError, ValueError) as exc:
            rejections.append(f"{run_dir.name}: {exc}")
            continue
        passed.append((run_dir, digest))

    if not passed:
        raise DeliveryError(
            f"{session.isoformat()} has no capture that replays as accepted "
            f"({'; '.join(rejections) or 'no runs found'})"
        )
    digests = {digest for _, digest in passed if digest}
    if len(digests) > 1:
        raise DeliveryError(
            f"{session.isoformat()} has runs replaying to different market digests "
            f"({len(digests)}); resolve it by hand rather than picking one"
        )
    run_dir = passed[-1][0]
    return run_dir, staging_root / run_dir.name / session.isoformat()


def find_index_run(captures_path: Path, session: date) -> Path | None:
    """The run directory holding this session's accepted index values, if any.

    Resolved independently of the price replay. Index values are validated on
    their own and delivered through their own route, so a session whose prices
    cannot be replayed must still be able to hand over the closes it does have
    — the same reason the two are separate requests in the first place.
    """
    session_dir = captures_path / session.isoformat()
    if not session_dir.is_dir():
        return None
    for run_dir in sorted(session_dir.iterdir(), reverse=True):
        summaries = (
            run_dir / VALIDATION_SUBROOT / "indices" / session.isoformat()
        ).glob("*/forward_summary.json")
        for summary_path in summaries:
            try:
                summary = json.loads(summary_path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(summary, dict) and summary.get("status") == "accepted":
                return run_dir
    return None


def deliver_session(
    run_dir: Path | None,
    session: date,
    *,
    price_root: Path | None,
    price_error: str = "",
    calendar_path: Path,
    api_url: str,
    token: str,
    out_dir: Path,
    session_obj: requests.Session,
    dry_run: bool,
    with_prices: bool,
    with_indices: bool,
) -> SessionOutcome:
    """Deliver one session's prices and index values.

    `price_root` is where the price manifest and its accepted rows live: the
    capture itself when it was accepted at the time, or the staged replay when
    it only passes under today's rules. Index values always come from the
    capture, which validated them independently of the price run.
    """
    outcome = SessionOutcome(
        session=session.isoformat(), run_id=run_dir.name if run_dir else None
    )
    session_out = out_dir / session.isoformat()

    if with_prices and price_root is None:
        outcome.prices, outcome.prices_detail = "unavailable", price_error
    elif with_prices and price_root is not None:
        try:
            body = build_price_request(
                price_root / RESULT_MANIFEST,
                calendar_path,
                expected_trade_date=session.isoformat(),
                root=price_root,
                allow_later_capture=True,
            )
        except DeliveryError as exc:
            outcome.prices, outcome.prices_detail = "failed", str(exc)
        else:
            session_out.mkdir(parents=True, exist_ok=True)
            (session_out / "eod_ingestion_request.json").write_text(
                json.dumps(body, indent=2) + "\n"
            )
            if dry_run:
                outcome.prices = "prepared"
                outcome.prices_detail = f"{len(body['prices'])} prices"
            else:
                try:
                    receipt = deliver_prices(
                        body, api_url=api_url, token=token, session=session_obj
                    )
                except DeliveryConflict as exc:
                    # The platform already has this date. That is the whole
                    # point of re-walking the store, not a failure.
                    outcome.prices = "already-held"
                    outcome.prices_detail = str(exc)[:200]
                except DeliveryError as exc:
                    outcome.prices, outcome.prices_detail = "failed", str(exc)[:500]
                else:
                    (session_out / "eod_ingestion_receipt.json").write_text(
                        json.dumps(receipt, indent=2) + "\n"
                    )
                    outcome.prices = "delivered"
                    outcome.prices_detail = f"{receipt['records_accepted']} rows"

    if with_indices and run_dir is None:
        outcome.indices = "unavailable"
        outcome.indices_detail = "no capture holds accepted index values"
    elif with_indices and run_dir is not None:
        try:
            body = build_index_request(
                session,
                calendar_path,
                raw_root=run_dir / RAW_SUBROOT,
                validation_root=run_dir / VALIDATION_SUBROOT,
            )
        except DeliveryError as exc:
            # Most sessions before 2026-09-09 have no accepted index run at
            # all, so this is ordinary, not a fault of the delivery.
            outcome.indices, outcome.indices_detail = "unavailable", str(exc)[:200]
        else:
            session_out.mkdir(parents=True, exist_ok=True)
            (session_out / "index_ingestion_request.json").write_text(
                json.dumps(body, indent=2) + "\n"
            )
            if dry_run:
                outcome.indices = "prepared"
                outcome.indices_detail = ", ".join(v["code"] for v in body["values"])
            else:
                try:
                    receipt = deliver_indices(
                        body, api_url=api_url, token=token, session=session_obj
                    )
                except DeliveryConflict as exc:
                    # A different close for a day already recorded. Never
                    # overwritten silently; it needs a human.
                    outcome.indices = "failed"
                    outcome.indices_detail = f"disagrees with stored values: {exc}"[:500]
                except DeliveryError as exc:
                    outcome.indices, outcome.indices_detail = "failed", str(exc)[:500]
                else:
                    (session_out / "index_ingestion_receipt.json").write_text(
                        json.dumps(receipt, indent=2) + "\n"
                    )
                    outcome.indices = "delivered"
                    stored = ", ".join(receipt["stored"]) or "none"
                    outcome.indices_detail = f"stored {stored}"

    return outcome


def run(
    *,
    captures_path: Path,
    calendar_path: Path,
    out_dir: Path,
    api_url: str,
    token: str,
    since: date | None = None,
    until: date | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    with_prices: bool = True,
    with_indices: bool = True,
    replay: bool = True,
    staging_root: Path | None = None,
    metadata_path: Path | None = None,
    session_obj: requests.Session | None = None,
) -> BackfillReport:
    report = BackfillReport(
        started_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    client = session_obj or requests.Session()
    sessions = discover_sessions(captures_path, since=since, until=until)
    if limit is not None:
        sessions = sessions[:limit]
    staging = staging_root or out_dir / "staging"

    for session in sessions:
        run_dir: Path | None
        price_root: Path | None
        price_error = ""
        try:
            if replay:
                run_dir, price_root = replay_session(
                    captures_path,
                    session,
                    staging_root=staging,
                    metadata_path=metadata_path,
                )
            else:
                run_dir = choose_run(captures_path, session)
                price_root = run_dir
        except DeliveryError as exc:
            # Prices cannot be prepared, but this session's index values are
            # validated and delivered independently, so they are still offered
            # rather than buried with the price failure.
            run_dir, price_root = find_index_run(captures_path, session), None
            price_error = str(exc)
        report.sessions.append(
            deliver_session(
                run_dir,
                session,
                price_root=price_root,
                price_error=price_error,
                calendar_path=calendar_path,
                api_url=api_url,
                token=token,
                out_dir=out_dir,
                session_obj=client,
                dry_run=dry_run,
                with_prices=with_prices,
                with_indices=with_indices,
            )
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deliver every saved session the platform does not hold yet"
    )
    parser.add_argument("--captures-path", type=Path, required=True)
    parser.add_argument("--calendar-path", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--api-url", default=os.getenv("TRADEIQ_INGESTION_API_URL"))
    parser.add_argument("--token", default=os.getenv("TRADEIQ_INGESTION_TOKEN"))
    parser.add_argument("--since", type=date.fromisoformat)
    parser.add_argument("--until", type=date.fromisoformat)
    parser.add_argument("--limit", type=int, help="Deliver at most this many sessions")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and write every request without sending any",
    )
    parser.add_argument("--skip-prices", action="store_true")
    parser.add_argument("--skip-indices", action="store_true")
    parser.add_argument(
        "--no-replay",
        action="store_true",
        help=(
            "Deliver only what each capture accepted at the time, instead of "
            "re-validating its saved payload with the current rules"
        ),
    )
    parser.add_argument(
        "--staging-root",
        type=Path,
        help="Where replayed sessions are staged; defaults to <out-dir>/staging",
    )
    parser.add_argument(
        "--metadata-path",
        type=Path,
        help="Company metadata for the replay; defaults to this checkout's",
    )
    args = parser.parse_args()
    if not args.dry_run and (not args.api_url or not args.token):
        raise DeliveryError("api URL and token are required")

    report = run(
        captures_path=args.captures_path,
        calendar_path=args.calendar_path,
        out_dir=args.out_dir,
        api_url=args.api_url or "",
        token=args.token or "",
        since=args.since,
        until=args.until,
        limit=args.limit,
        dry_run=args.dry_run,
        with_prices=not args.skip_prices,
        with_indices=not args.skip_indices,
        replay=not args.no_replay,
        staging_root=args.staging_root,
        metadata_path=args.metadata_path,
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "backfill_report.json").write_text(
        json.dumps(
            {
                "started_at": report.started_at,
                "summary": report.summary(),
                "sessions": [asdict(outcome) for outcome in report.sessions],
            },
            indent=2,
        )
        + "\n"
    )

    for outcome in report.sessions:
        print(
            f"{outcome.session}  prices={outcome.prices:<12} "
            f"indices={outcome.indices:<12} {outcome.prices_detail[:60]}"
        )
    print(f"summary: {report.summary()}")

    failures = [outcome for outcome in report.sessions if outcome.failed]
    if failures:
        print(f"{len(failures)} session(s) failed")
        for outcome in failures:
            print(f"  {outcome.session}: {outcome.prices_detail or outcome.indices_detail}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
