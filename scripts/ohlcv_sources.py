"""Source adapters for CSE OHLCV collection.

Adapters deliberately keep fetching, normalization, and source-date validation
separate. The collector can then persist the raw payload plus immutable fetch
metadata before any record is accepted into the dataset.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests


COLOMBO_TZ = ZoneInfo("Asia/Colombo")


@dataclass(frozen=True)
class FetchResult:
    source_name: str
    requested_date: date
    observed_source_date: date | None
    fetch_time_utc: datetime
    source_url: str
    payload: Any
    payload_hash: str
    row_count: int
    raw_payload_path: Path


class OHLCVSourceAdapter(ABC):
    source_name: str
    confidence_level: str
    source_url: str

    @abstractmethod
    def fetch_for_date(self, target_date: date, raw_root: Path) -> FetchResult:
        """Fetch raw source data for a target date."""

    @abstractmethod
    def normalize(self, payload: Any, fetch_result: FetchResult) -> pd.DataFrame:
        """Normalize a raw payload to the canonical OHLCV candidate schema."""

    @abstractmethod
    def validate_source_date(self, records: pd.DataFrame, target_date: date) -> list[str]:
        """Return source-date validation failures."""

    def raw_payload_path(self, raw_root: Path, target_date: date) -> Path:
        return raw_root / target_date.isoformat() / self.source_name / "payload.json"


def stable_payload_bytes(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def payload_hash(payload: Any) -> str:
    return hashlib.sha256(stable_payload_bytes(payload)).hexdigest()


def parse_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "N/A", "NA"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


class CSETradeSummaryCurrentAdapter(OHLCVSourceAdapter):
    """Official CSE current-snapshot adapter.

    Recon showed that this endpoint must not be treated as historical. It is
    accepted only when the requested date is the current Colombo calendar date;
    older target dates fail source-date validation instead of being stamped onto
    the payload.
    """

    source_name = "cse_trade_summary_current"
    confidence_level = "medium_current_snapshot_only"
    source_url = "https://www.cse.lk/api/tradeSummary"

    def __init__(self, timeout: int = 20) -> None:
        self.timeout = timeout

    def fetch_for_date(self, target_date: date, raw_root: Path) -> FetchResult:
        fetch_time_utc = datetime.now(timezone.utc)
        headers = {"User-Agent": "cse-dataset-v2/0.1 (+https://github.com/nimeshk03/cse-dataset-v2)"}
        response = requests.post(
            self.source_url,
            files={"date": (None, target_date.isoformat())},
            headers=headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("reqTradeSummery") or []

        # The endpoint has no trustworthy per-row date. For this current-only
        # adapter, the only observable source date is the Colombo date at fetch.
        observed_source_date = fetch_time_utc.astimezone(COLOMBO_TZ).date()

        return FetchResult(
            source_name=self.source_name,
            requested_date=target_date,
            observed_source_date=observed_source_date,
            fetch_time_utc=fetch_time_utc,
            source_url=self.source_url,
            payload=payload,
            payload_hash=payload_hash(payload),
            row_count=len(rows),
            raw_payload_path=self.raw_payload_path(raw_root, target_date),
        )

    def normalize(self, payload: Any, fetch_result: FetchResult) -> pd.DataFrame:
        rows = payload.get("reqTradeSummery") or []
        normalized: list[dict[str, Any]] = []
        for row in rows:
            symbol = row.get("symbol") or row.get("securityCode")
            if not symbol:
                continue
            normalized.append(
                {
                    "date": fetch_result.requested_date.isoformat(),
                    "symbol": str(symbol).strip(),
                    "open": parse_number(row.get("open")),
                    "high": parse_number(row.get("high")),
                    "low": parse_number(row.get("low")),
                    "close": parse_number(row.get("closingPrice") or row.get("close")),
                    "volume": parse_number(row.get("sharevolume") or row.get("volume")),
                    "turnover": parse_number(row.get("turnover")),
                    "trades": parse_number(row.get("tradevolume") or row.get("trades")),
                    "source": self.source_name,
                    "source_priority": 10,
                    "source_timestamp": fetch_result.observed_source_date.isoformat()
                    if fetch_result.observed_source_date
                    else None,
                    "raw_payload_hash": fetch_result.payload_hash,
                    "validation_status": "candidate",
                    "validation_warnings": "",
                }
            )
        return pd.DataFrame(normalized)

    def validate_source_date(self, records: pd.DataFrame, target_date: date) -> list[str]:
        if records.empty:
            return ["source returned zero normalized rows"]
        timestamps = pd.to_datetime(records["source_timestamp"], errors="coerce").dt.date.dropna().unique()
        if len(timestamps) != 1:
            return ["source timestamp is missing or inconsistent across rows"]
        if timestamps[0] != target_date:
            return [
                "source date mismatch: "
                f"requested {target_date.isoformat()}, observed {timestamps[0].isoformat()}"
            ]
        return []


ADAPTERS: dict[str, type[OHLCVSourceAdapter]] = {
    CSETradeSummaryCurrentAdapter.source_name: CSETradeSummaryCurrentAdapter,
}


def make_adapter(name: str) -> OHLCVSourceAdapter:
    try:
        return ADAPTERS[name]()
    except KeyError as exc:
        valid = ", ".join(sorted(ADAPTERS))
        raise ValueError(f"Unknown OHLCV source '{name}'. Valid sources: {valid}") from exc
