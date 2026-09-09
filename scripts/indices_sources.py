"""Source adapter for daily CSE index collection.

The ``dailyMarketSummery`` endpoint carries every headline index the platform
needs — ASPI, S&P SL20 and both total-return series — in one settled payload.

Unlike ``tradeSummary``, it stamps the payload with its own ``tradeDate``, so
the observed source date is read from the response rather than assumed from the
clock. That is what makes a snapshot-only endpoint safe to use: a payload for
the wrong day fails source-date validation instead of being stamped onto the
requested date. The endpoint ignores a ``date`` form field entirely — probing
2024-06-14 and 2026-03-02 returned a byte-identical current payload — so it is
usable for today and never for backfill.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

try:
    from .forward_ingestion import (
        COLOMBO_TZ,
        REQUEST_HEADERS,
        ForwardFetchResult,
        MissingSourceError,
        ReportDateError,
        raw_payload_path,
        sha256_bytes,
        _stable_json_bytes,
    )
    from .ohlcv_sources import parse_number
except ImportError:  # pragma: no cover - used when scripts are executed directly.
    from forward_ingestion import (
        COLOMBO_TZ,
        REQUEST_HEADERS,
        ForwardFetchResult,
        MissingSourceError,
        ReportDateError,
        raw_payload_path,
        sha256_bytes,
        _stable_json_bytes,
    )
    from ohlcv_sources import parse_number


CSE_DAILY_MARKET_SUMMARY_URL = "https://www.cse.lk/api/dailyMarketSummery"

# Payload field -> index code. The Milanka series are still published as null
# fields; they are listed so that a restart is picked up automatically, and a
# null simply produces no row.
SERIES_FIELDS: dict[str, str] = {
    "asi": "ASPI",
    "spp": "SL20",
    "spt": "SL20TRI",
    "triasi": "ASTRI",
    "mpi": "MPI",
    "trimpi": "MTRI",
}


def epoch_ms_to_colombo_date(value: Any) -> date | None:
    """Read the payload's own trade date.

    The endpoint reports in milliseconds since the epoch. It is converted in
    Colombo time because the trading date is a Colombo calendar date, and a UTC
    reading rolls over at the wrong moment.
    """
    number = parse_number(value)
    if number is None:
        return None
    try:
        return datetime.fromtimestamp(number / 1000, tz=timezone.utc).astimezone(COLOMBO_TZ).date()
    except (OverflowError, OSError, ValueError):
        return None


def extract_summary_row(payload: Any) -> dict[str, Any]:
    """Pull the single summary record out of the endpoint's nested envelope.

    Accepts the parsed JSON or the raw bytes/text held by the fetch result, so
    the same helper serves both fetch and normalize. The response is a list of
    lists; anything else means the shape changed and is quarantined rather than
    guessed at.
    """
    rows = payload
    if isinstance(rows, (bytes, bytearray, str)):
        try:
            rows = json.loads(rows)
        except ValueError as exc:
            raise MissingSourceError(f"dailyMarketSummery payload is not valid JSON: {exc}") from exc
    if not isinstance(rows, list):
        # A bare object means the envelope changed. Reading fields straight off
        # it would quietly accept a different response shape.
        raise MissingSourceError(
            f"dailyMarketSummery returned {type(rows).__name__}, expected a list envelope"
        )
    while isinstance(rows, list) and rows and isinstance(rows[0], list):
        rows = rows[0]
    if isinstance(rows, list):
        rows = rows[0] if rows else None
    if not isinstance(rows, dict):
        raise MissingSourceError("dailyMarketSummery returned no summary record")
    return rows


class CSEDailyMarketSummaryIndicesAdapter:
    """Official CSE settled-summary adapter for the ``indices`` family."""

    family = "indices"
    source_name = "cse_daily_market_summary_indices"
    confidence_level = "high_dated_snapshot"
    source_url = CSE_DAILY_MARKET_SUMMARY_URL

    def __init__(self, timeout: int = 20) -> None:
        self.timeout = timeout

    def fetch_for_date(self, target_date: date, raw_root: Path) -> ForwardFetchResult:
        fetch_time_utc = datetime.now(timezone.utc)
        try:
            response = requests.post(self.source_url, headers=REQUEST_HEADERS, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise MissingSourceError(f"dailyMarketSummery request failed: {exc}") from exc
        except ValueError as exc:
            raise MissingSourceError(f"dailyMarketSummery returned invalid JSON: {exc}") from exc

        row = extract_summary_row(payload)
        report_date = epoch_ms_to_colombo_date(row.get("tradeDate"))
        if report_date is None:
            raise ReportDateError("dailyMarketSummery payload carries no readable tradeDate")

        payload_bytes = _stable_json_bytes(payload)
        digest = sha256_bytes(payload_bytes)
        path = raw_payload_path(raw_root, self.family, target_date, self.source_name, digest, ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload_bytes)

        return ForwardFetchResult(
            family=self.family,
            source_name=self.source_name,
            requested_date=target_date,
            report_date=report_date,
            fetch_time_utc=fetch_time_utc,
            source_url=self.source_url,
            payload=payload_bytes,
            text=payload_bytes.decode("utf-8"),
            payload_hash=digest,
            row_count=sum(1 for field in SERIES_FIELDS if parse_number(row.get(field)) is not None),
            raw_payload_path=path,
        )

    def normalize(self, payload: Any, *, report_date: date, payload_hash: str) -> pd.DataFrame:
        row = extract_summary_row(payload)
        records: list[dict[str, Any]] = []
        for field, index_name in SERIES_FIELDS.items():
            close = parse_number(row.get(field))
            if close is None:
                # A series the exchange no longer publishes is absent, the same
                # as a blank cell in the archive. Never zero.
                continue
            records.append(
                {
                    "date": report_date.isoformat(),
                    "index_name": index_name,
                    "close": close,
                    "source": self.source_name,
                    "source_timestamp": report_date.isoformat(),
                    "raw_payload_hash": payload_hash,
                }
            )
        return pd.DataFrame(records)

    def validate_source_date(
        self,
        records: pd.DataFrame,
        target_date: date,
        report_date: date | None,
    ) -> list[str]:
        if report_date is None:
            return ["source payload carries no report date"]
        if report_date != target_date:
            return [
                "source date mismatch: "
                f"requested {target_date.isoformat()}, observed {report_date.isoformat()}"
            ]
        if records.empty:
            return ["source returned zero normalized index rows"]
        return []
