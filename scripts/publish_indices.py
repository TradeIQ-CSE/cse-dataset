"""Deliver one accepted day of CSE index closes to TradeIQ.

The sibling of ``publish_eod.py``, for the route described in the platform's
``docs/api/index-ingestion-v1.md``. It is deliberately a second script and a
second request: the price route refuses a date that already has prices, so
index values bundled with them could never arrive late or be re-sent, and a
failed index capture would hold back that day's prices.

Input is what ``daily_indices_update.py`` already writes — the per-date
``forward_summary.json`` and the accepted ``canonical_indices.csv`` beside it —
so nothing is re-fetched from CSE at delivery time. Only a run the validator
marked ``accepted`` is sent.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import pandas as pd
import requests

try:
    from .forward_ingestion import RAW_ROOT, VALIDATION_ROOT
    from .publish_eod import (
        DeliveryConflict,
        DeliveryError,
        _request_with_retries,
        assert_deliverable_target,
        load_calendar_entry,
    )
except ImportError:  # pragma: no cover - used when scripts are executed directly.
    from forward_ingestion import RAW_ROOT, VALIDATION_ROOT
    from publish_eod import (
        DeliveryConflict,
        DeliveryError,
        _request_with_retries,
        assert_deliverable_target,
        load_calendar_entry,
    )

FAMILY = "indices"

# The codes the platform ships in market_data.indices. A code it does not know
# is a 400 for the whole batch, so an unexpected series is refused here rather
# than losing the three good ones with it. MPI and MTRI appear in the historical
# workbooks but were discontinued, so they are not delivered forward.
DELIVERABLE_CODES = ("ASPI", "SL20", "SL20TRI", "ASTRI")

# Mirrors IndexCloseDto: positive, at most four decimal places, and never zero.
CLOSE_PATTERN = re.compile(r"^(?!0+(?:\.0+)?$)(?:0|[1-9]\d{0,9})(?:\.\d{1,4})?$")

MAX_VALUES = 20


def index_close_string(value: Any) -> str:
    """Normalise one close to the string form the platform's DTO accepts."""
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        raise DeliveryError("an index close is missing")
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise DeliveryError(f"invalid index close: {value!r}") from exc
    if not number.is_finite() or number <= 0:
        raise DeliveryError(f"index close must be positive: {value!r}")
    if -number.as_tuple().exponent > 4:
        raise DeliveryError(f"index close has more than four places: {value!r}")
    # normalize() would render 21056.00 as 2.1056E+4, which the DTO rejects.
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if not CLOSE_PATTERN.fullmatch(text):
        raise DeliveryError(f"index close is out of range: {value!r}")
    return text


def find_accepted_source(
    target_date: date,
    *,
    validation_root: Path = VALIDATION_ROOT,
) -> str:
    """Name the one source whose run for this date was accepted.

    Several adapters can write a candidate for the same day. Delivering two of
    them would mean sending two different closes for one index, so an ambiguous
    date is refused rather than resolved by preference order.
    """
    summary_root = validation_root / FAMILY / target_date.isoformat()
    if not summary_root.is_dir():
        raise DeliveryError(f"no index run exists for {target_date.isoformat()}")
    accepted = []
    for summary_path in sorted(summary_root.glob("*/forward_summary.json")):
        summary = json.loads(summary_path.read_text())
        if summary.get("status") == "accepted":
            accepted.append(summary_path.parent.name)
    if not accepted:
        raise DeliveryError(
            f"no accepted index run for {target_date.isoformat()}; it was quarantined"
        )
    if len(accepted) > 1:
        raise DeliveryError(
            f"{target_date.isoformat()} has accepted index runs from "
            f"{', '.join(accepted)}; refusing to choose between them"
        )
    return accepted[0]


def load_accepted_values(
    target_date: date,
    source_name: str,
    *,
    raw_root: Path = RAW_ROOT,
) -> list[dict[str, str]]:
    path = (
        raw_root
        / "accepted"
        / FAMILY
        / target_date.isoformat()
        / source_name
        / f"canonical_{FAMILY}.csv"
    )
    if not path.is_file():
        raise DeliveryError(f"accepted index rows are missing at {path}")
    frame = pd.read_csv(path, dtype=str)
    required = {"date", "index_name", "close"}
    if missing := required - set(frame.columns):
        raise DeliveryError(
            f"accepted index rows are missing columns: {', '.join(sorted(missing))}"
        )
    dates = set(frame["date"].astype(str).str.strip())
    if dates != {target_date.isoformat()}:
        raise DeliveryError(
            f"accepted index rows carry dates {sorted(dates)}, not {target_date.isoformat()}"
        )

    values: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in frame.to_dict(orient="records"):
        code = str(row["index_name"]).strip().upper()
        if code not in DELIVERABLE_CODES:
            # Not an error: the archive carries discontinued series the
            # platform never shipped, and dropping one costs nothing.
            continue
        if code in seen:
            raise DeliveryError(f"{code} appears twice for {target_date.isoformat()}")
        seen.add(code)
        values.append({"code": code, "close": index_close_string(row["close"])})

    if not values:
        raise DeliveryError(
            f"no deliverable index values for {target_date.isoformat()}; "
            f"expected one of {', '.join(DELIVERABLE_CODES)}"
        )
    if len(values) > MAX_VALUES:
        raise DeliveryError(f"{len(values)} index values exceeds the {MAX_VALUES} limit")
    values.sort(key=lambda value: value["code"])
    return values


def build_request(
    target_date: date,
    calendar_path: Path,
    *,
    source_name: str | None = None,
    raw_root: Path = RAW_ROOT,
    validation_root: Path = VALIDATION_ROOT,
) -> dict[str, Any]:
    source = source_name or find_accepted_source(
        target_date, validation_root=validation_root
    )
    calendar = load_calendar_entry(calendar_path, target_date.isoformat())
    return {
        "trade_date": target_date.isoformat(),
        # The route takes is_trading_day and source only; verified_at belongs to
        # the price contract, and a stray field would be silently stripped.
        "calendar": {
            "is_trading_day": calendar["is_trading_day"],
            "source": calendar["source"],
        },
        "values": load_accepted_values(target_date, source, raw_root=raw_root),
    }


def deliver(
    request_body: dict[str, Any],
    *,
    api_url: str,
    token: str,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    assert_deliverable_target(api_url, token)
    client = session or requests.Session()
    endpoint = urljoin(api_url.rstrip("/") + "/", "internal/v1/ingestions/indices")
    response = _request_with_retries(
        client, "POST", endpoint, token=token, body=request_body
    )
    if response.status_code not in {200, 201}:
        try:
            details = response.json()
        except ValueError:
            details = response.text[:500]
        message = f"index ingestion failed with HTTP {response.status_code}: {details}"
        # A 409 here means a different close is already recorded for one of
        # these codes, which is a real disagreement about a past day, not the
        # benign "already have it" the price route reports.
        if response.status_code == 409:
            raise DeliveryConflict(message)
        raise DeliveryError(message)
    return _validated_receipt(response, request_body)


def _validated_receipt(
    response: requests.Response,
    request_body: dict[str, Any],
) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise DeliveryError("index ingestion returned an invalid receipt") from exc
    receipt = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(receipt, dict):
        raise DeliveryError("index ingestion returned no receipt")
    if receipt.get("trade_date") != request_body["trade_date"]:
        raise DeliveryError("index receipt trade_date does not match the request")
    stored = receipt.get("stored")
    unchanged = receipt.get("unchanged")
    if not isinstance(stored, list) or not isinstance(unchanged, list):
        raise DeliveryError("index receipt does not report stored and unchanged codes")
    # Every code sent must come back in one list or the other. Anything else
    # means the day is only partly recorded, which a green run must not claim.
    settled = {str(code) for code in stored} | {str(code) for code in unchanged}
    sent = {value["code"] for value in request_body["values"]}
    if settled != sent:
        raise DeliveryError(
            f"index receipt accounts for {sorted(settled)}, not the {sorted(sent)} sent"
        )
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish one accepted day of index closes to TradeIQ"
    )
    parser.add_argument(
        "--target-date",
        type=date.fromisoformat,
        required=True,
        help="Trading date to deliver, as YYYY-MM-DD",
    )
    parser.add_argument("--calendar-path", type=Path, required=True)
    parser.add_argument(
        "--source-name",
        help="Adapter whose accepted rows to send; discovered when omitted",
    )
    parser.add_argument("--api-url", default=os.getenv("TRADEIQ_INGESTION_API_URL"))
    parser.add_argument("--token", default=os.getenv("TRADEIQ_INGESTION_TOKEN"))
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and write the request without sending it",
    )
    args = parser.parse_args()
    if not args.dry_run and (not args.api_url or not args.token):
        raise DeliveryError("api URL and token are required")

    request_body = build_request(
        args.target_date,
        args.calendar_path,
        source_name=args.source_name,
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "index_ingestion_request.json").write_text(
        json.dumps(request_body, indent=2) + "\n"
    )
    codes = ", ".join(value["code"] for value in request_body["values"])
    if args.dry_run:
        print(f"DRY RUN: {len(request_body['values'])} values for "
              f"{request_body['trade_date']} ({codes})")
        return

    receipt = deliver(request_body, api_url=args.api_url, token=args.token)
    (args.out_dir / "index_ingestion_receipt.json").write_text(
        json.dumps(receipt, indent=2) + "\n"
    )
    stored = ", ".join(receipt["stored"]) or "none"
    unchanged = ", ".join(receipt["unchanged"]) or "none"
    print(
        f"DELIVERED indices for {receipt['trade_date']}: "
        f"stored {stored}; already held {unchanged}"
    )


if __name__ == "__main__":
    main()
