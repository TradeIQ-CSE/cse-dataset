"""One-shot 2026 ASPI backfill from the CSE trailing-window chart endpoint.

``chartData`` with ``chartId=1`` returns roughly one year of ASPI points, which
is the only reachable source for 2026 dates that the daily collector missed.
It is not a published-quality source on its own: the window mixes settled
closing values with points stamped before the market opens, and only the
settled ones agree with the official archive.

So this backfill proves the source before it trusts it. Points are kept only
when the payload stamps them at or after the market close, and the run fails
outright unless every point that overlaps the official archive reproduces the
archive's close exactly. Nothing is written on a failed cross-check.

There is no S&P SL20 equivalent: ``chartId`` values other than 1 return an
empty list, so this covers ASPI only.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

try:
    from .convert_historical_indices import (
        HistoricalIndexValidationResult,
        render_validation_report,
        validate_historical_index_records,
    )
    from .forward_ingestion import (
        COLOMBO_TZ,
        FORWARD_START_DATE,
        REQUEST_HEADERS,
        MissingSourceError,
        sha256_bytes,
        _stable_json_bytes,
    )
except ImportError:  # pragma: no cover - used when scripts are executed directly.
    from convert_historical_indices import (
        HistoricalIndexValidationResult,
        render_validation_report,
        validate_historical_index_records,
    )
    from forward_ingestion import (
        COLOMBO_TZ,
        FORWARD_START_DATE,
        REQUEST_HEADERS,
        MissingSourceError,
        sha256_bytes,
        _stable_json_bytes,
    )


ROOT = Path(__file__).resolve().parents[1]
SOURCE_NAME = "cse_chart_data_aspi"
INDEX_NAME = "ASPI"
CHART_DATA_URL = "https://www.cse.lk/api/chartData"
ASPI_CHART_ID = "1"
ONE_YEAR_PERIOD = "5"

# CSE closes at 14:30 SLST. A settled closing value is stamped at or after the
# close; the endpoint also returns points stamped 08:16, before the market
# opens, and those match neither the same day's official close nor the
# previous day's.
MARKET_CLOSE = time(14, 30)

# The overlap points reproduce the archive exactly, so the bar is exact
# agreement with a tolerance only for float formatting.
CROSS_CHECK_TOLERANCE = 1e-6
DEFAULT_MIN_OVERLAP = 20

RAW_ROOT = ROOT / "data/raw/2026_forward/source_payloads/indices"
ACCEPTED_ROOT = ROOT / "data/processed/indices_backfill/accepted"
VALIDATION_ROOT = ROOT / "data/processed/validation/indices_backfill/chartdata_2026"
ARCHIVE_ACCEPTED = ROOT / "data/processed/indices_backfill/accepted/indices_historical.csv"


class CrossCheckError(RuntimeError):
    """Raised when the chart window disagrees with the official archive."""


@dataclass(frozen=True)
class ChartPoint:
    stamp: datetime
    value: float

    @property
    def trading_date(self) -> date:
        return self.stamp.date()

    @property
    def is_settled(self) -> bool:
        return self.stamp.time() >= MARKET_CLOSE


def colombo_today() -> date:
    return datetime.now(timezone.utc).astimezone(COLOMBO_TZ).date()


def fetch_chart_payload(timeout: int = 30) -> tuple[Any, bytes, str]:
    try:
        response = requests.post(
            CHART_DATA_URL,
            files={"chartId": (None, ASPI_CHART_ID), "period": (None, ONE_YEAR_PERIOD)},
            headers=REQUEST_HEADERS,
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise MissingSourceError(f"chartData request failed: {exc}") from exc
    except ValueError as exc:
        raise MissingSourceError(f"chartData returned invalid JSON: {exc}") from exc
    if not isinstance(payload, list) or not payload:
        raise MissingSourceError("chartData returned no points for chartId=1")
    raw = _stable_json_bytes(payload)
    return payload, raw, sha256_bytes(raw)


def parse_points(payload: Any) -> list[ChartPoint]:
    points: list[ChartPoint] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        stamp_ms = entry.get("d")
        value = entry.get("v")
        if stamp_ms is None or value is None:
            continue
        try:
            stamp = datetime.fromtimestamp(float(stamp_ms) / 1000, tz=timezone.utc).astimezone(COLOMBO_TZ)
            close = float(value)
        except (TypeError, ValueError, OSError, OverflowError) as exc:
            # Refuse rather than drop the point: a value the endpoint cannot
            # express as a number means the response shape changed, and
            # skipping it would quietly shrink the window instead.
            raise MissingSourceError(
                f"chartData point is not readable as a timestamp and value: {entry!r} ({exc})"
            ) from exc
        points.append(ChartPoint(stamp=stamp, value=close))
    if not points:
        raise MissingSourceError("chartData payload carried no readable points")
    return points


def build_candidates(points: list[ChartPoint], payload_hash: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": point.trading_date,
                "index_name": INDEX_NAME,
                "close": point.value,
                "source": SOURCE_NAME,
                "source_timestamp": point.trading_date,
                "raw_payload_hash": payload_hash,
                "stamp_time": point.stamp.strftime("%H:%M"),
                "is_settled": point.is_settled,
            }
            for point in points
        ]
    )


def load_archive_closes(path: Path = ARCHIVE_ACCEPTED) -> dict[date, float]:
    if not path.exists():
        raise CrossCheckError(
            f"archive closes not found at {path}; run scripts/convert_historical_indices.py first"
        )
    frame = pd.read_csv(path)
    frame = frame[frame["index_name"] == INDEX_NAME]
    dates = pd.to_datetime(frame["date"], errors="coerce").dt.date
    return {day: float(close) for day, close in zip(dates, frame["close"]) if pd.notna(day)}


def cross_check(
    candidates: pd.DataFrame,
    archive: dict[date, float],
    *,
    min_overlap: int = DEFAULT_MIN_OVERLAP,
    tolerance: float = CROSS_CHECK_TOLERANCE,
) -> dict[str, Any]:
    """Prove the source on the dates where an official value already exists.

    Only settled points are checked, because those are the only ones the
    backfill goes on to publish.
    """
    settled = candidates[candidates["is_settled"]]
    overlap = [
        (row["date"], row["close"], archive[row["date"]])
        for _, row in settled.iterrows()
        if row["date"] in archive
    ]
    mismatches = [
        {"date": day.isoformat(), "chart": chart, "archive": official}
        for day, chart, official in overlap
        if abs(chart - official) > max(tolerance, tolerance * abs(official))
    ]
    metrics = {
        "overlap_points": len(overlap),
        "mismatched_points": len(mismatches),
        "mismatches": mismatches[:10],
        "tolerance": tolerance,
    }
    if len(overlap) < min_overlap:
        raise CrossCheckError(
            f"only {len(overlap)} settled points overlap the archive; need at least {min_overlap} "
            "to prove the source"
        )
    if mismatches:
        raise CrossCheckError(
            f"{len(mismatches)} of {len(overlap)} overlapping points disagree with the official "
            f"archive; first: {mismatches[0]}"
        )
    return metrics


def split_unsettled(candidates: pd.DataFrame, *, today: date) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Separate publishable points from ones the endpoint cannot settle.

    Two kinds are held back: points stamped before the close, and the current
    Colombo date, whose value is still moving.
    """
    reasons: list[str] = []
    for _, row in candidates.iterrows():
        if not row["is_settled"]:
            reasons.append(f"point stamped {row['stamp_time']}, before the {MARKET_CLOSE.isoformat()} close")
        elif row["date"] >= today:
            reasons.append("current trading day is not settled")
        else:
            reasons.append("")
    held = pd.Series(reasons, index=candidates.index)
    rejected = candidates.loc[held != ""].copy()
    if not rejected.empty:
        rejected["rejection_reason"] = held.loc[held != ""]
    return candidates.loc[held == ""].copy(), rejected


def run(
    *,
    today: date | None = None,
    payload: Any | None = None,
    payload_hash: str | None = None,
    archive: dict[date, float] | None = None,
    min_overlap: int = DEFAULT_MIN_OVERLAP,
) -> tuple[HistoricalIndexValidationResult, pd.DataFrame, dict[str, Any]]:
    today = today or colombo_today()
    if payload is None:
        payload, _, payload_hash = fetch_chart_payload()
    candidates = build_candidates(parse_points(payload), payload_hash or "")
    archive = load_archive_closes() if archive is None else archive

    check = cross_check(candidates, archive, min_overlap=min_overlap)

    publishable, held_back = split_unsettled(candidates, today=today)
    # The archive already owns everything through 2025-12-31; this backfill
    # only covers what the daily collector was not running for.
    forward = publishable[publishable["date"] >= FORWARD_START_DATE].drop(
        columns=["stamp_time", "is_settled"]
    )
    result = validate_historical_index_records(
        forward,
        earliest_date=FORWARD_START_DATE,
        coverage_end=today,
    )
    result.metrics["cross_check"] = check
    result.metrics["source_name"] = SOURCE_NAME
    result.metrics["held_back_points"] = int(len(held_back))
    return result, held_back, check


def write_outputs(
    result: HistoricalIndexValidationResult,
    held_back: pd.DataFrame,
    raw_payload: bytes,
    payload_hash: str,
    *,
    accepted_root: Path = ACCEPTED_ROOT,
    validation_root: Path = VALIDATION_ROOT,
    raw_root: Path = RAW_ROOT,
) -> dict[str, Path]:
    accepted_root.mkdir(parents=True, exist_ok=True)
    validation_root.mkdir(parents=True, exist_ok=True)
    payload_dir = raw_root / "chartdata_2026" / SOURCE_NAME
    payload_dir.mkdir(parents=True, exist_ok=True)

    payload_path = payload_dir / f"{payload_hash}.json"
    payload_path.write_bytes(raw_payload)

    accepted_path = accepted_root / "indices_2026_chartdata.csv"
    result.accepted.to_csv(accepted_path, index=False)

    held_path = validation_root / "held_back_points.csv"
    held_back.to_csv(held_path, index=False)

    rejected_path = validation_root / "rejected_records.csv"
    result.rejected.to_csv(rejected_path, index=False)

    summary_path = validation_root / "backfill_summary.json"
    summary_path.write_text(json.dumps(result.metrics, indent=2, default=str) + "\n", encoding="utf-8")

    report_path = validation_root / "validation_report.md"
    report_path.write_text(render_validation_report(result), encoding="utf-8")

    return {
        "payload": payload_path,
        "accepted": accepted_path,
        "held_back": held_path,
        "rejected": rejected_path,
        "summary": summary_path,
        "report": report_path,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill 2026 ASPI closes from the CSE chart endpoint")
    parser.add_argument("--dry-run", action="store_true", help="Validate without writing artifacts")
    parser.add_argument(
        "--allow-validation-failure",
        action="store_true",
        help="Exit 0 even when rows are rejected",
    )
    parser.add_argument(
        "--min-overlap",
        type=int,
        default=DEFAULT_MIN_OVERLAP,
        help="Minimum settled points that must overlap the archive before the source is trusted",
    )
    args = parser.parse_args()

    try:
        payload, raw, payload_hash = fetch_chart_payload()
        result, held_back, check = run(
            payload=payload, payload_hash=payload_hash, min_overlap=args.min_overlap
        )
    except (MissingSourceError, CrossCheckError) as exc:
        print(f"refused: {exc}")
        return 1

    metrics = result.metrics
    print(f"cross-check:   {check['overlap_points']} overlapping points, "
          f"{check['mismatched_points']} mismatched")
    print(f"held back:     {metrics['held_back_points']} unsettled points")
    print(f"candidate rows:{metrics['row_count']:>5}")
    print(f"accepted rows: {metrics['accepted_rows']:>5}")
    print(f"rejected rows: {metrics['rejected_rows']:>5}")
    for index_name, entry in sorted(metrics.get("coverage", {}).items()):
        print(f"  {index_name:8} {entry['rows']:5} rows  {entry['first_date']} -> {entry['last_date']}")
    for item in metrics["failures"]:
        print(f"  failure: {item}")

    if not args.dry_run:
        paths = write_outputs(result, held_back, raw, payload_hash)
        for label, path in paths.items():
            print(f"wrote {label}: {path.relative_to(ROOT)}")

    if metrics["failures"] and not args.allow_validation_failure:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
