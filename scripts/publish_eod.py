"""Deliver one explicitly accepted EOD batch to TradeIQ's market-data API."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import time
from datetime import date, datetime, time as clock_time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[1]
COLOMBO_TZ = ZoneInfo("Asia/Colombo")
MAX_REQUEST_BYTES = 2 * 1024 * 1024
SHA256 = re.compile(r"[a-f0-9]{64}")


class DeliveryError(RuntimeError):
    """A non-retryable EOD delivery failure."""


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise DeliveryError(f"expected a JSON object in {path}")
    return value


def decimal_string(value: Any, *, nullable: bool = False) -> str | None:
    if value is None or pd.isna(value):
        if nullable:
            return None
        raise DeliveryError("required numeric value is missing")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise DeliveryError(f"invalid decimal value: {value!r}") from exc
    if not number.is_finite() or number < 0:
        raise DeliveryError(f"numeric value must be finite and non-negative: {value!r}")
    quantized = number.quantize(Decimal("0.0001"))
    if number != quantized:
        raise DeliveryError(f"decimal value has more than four places: {value!r}")
    return f"{quantized:.4f}"


def integer_string(value: Any, *, nullable: bool = False) -> str | None:
    if value is None or pd.isna(value) or str(value).strip() == "":
        if nullable:
            return None
        raise DeliveryError("required integer value is missing")
    try:
        number = Decimal(str(value).replace(",", ""))
    except InvalidOperation as exc:
        raise DeliveryError(f"invalid integer value: {value!r}") from exc
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        raise DeliveryError(f"value must be a non-negative integer: {value!r}")
    return str(int(number))


def canonical_digest(trade_date: str, prices: list[dict[str, Any]]) -> str:
    canonical = [
        {
            "date": trade_date,
            "symbol": row["symbol"],
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
        }
        for row in sorted(prices, key=lambda item: item["symbol"])
    ]
    encoded = json.dumps(canonical, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_calendar_entry(path: Path, trade_date: str) -> dict[str, Any]:
    calendar = pd.read_csv(path, dtype=str).fillna("")
    required = {"date", "is_trading_day", "source", "verified_at"}
    missing = required - set(calendar.columns)
    if missing:
        raise DeliveryError(f"calendar is missing columns: {', '.join(sorted(missing))}")
    rows = calendar.loc[calendar["date"] == trade_date]
    if len(rows) != 1:
        raise DeliveryError(f"calendar must contain exactly one entry for {trade_date}")
    row = rows.iloc[0]
    if row["is_trading_day"].strip().lower() not in {"true", "1", "yes"}:
        raise DeliveryError(f"calendar marks {trade_date} as a non-trading day")
    try:
        datetime.fromisoformat(row["verified_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise DeliveryError("calendar verified_at must be an RFC 3339 timestamp") from exc
    if not row["source"].strip():
        raise DeliveryError("calendar source must be recorded")
    return {
        "is_trading_day": True,
        "source": row["source"].strip(),
        "verified_at": row["verified_at"].strip(),
    }


def assert_after_close(captured_at: str, trade_date: str, close_time: str) -> None:
    try:
        parsed = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("captured_at has no timezone")
        captured = parsed.astimezone(COLOMBO_TZ)
        hours, minutes = (int(part) for part in close_time.split(":"))
        close = clock_time(hours, minutes)
    except (ValueError, TypeError) as exc:
        raise DeliveryError("invalid captured_at or close-time value") from exc
    if captured.date().isoformat() != trade_date:
        raise DeliveryError(
            f"capture date {captured.date().isoformat()} does not match target {trade_date}"
        )
    if captured.time().replace(tzinfo=None) < close:
        raise DeliveryError(f"capture occurred before the configured {close_time} Colombo close")


def assert_expected_trade_date(
    trade_date: str,
    expected_trade_date: str | None = None,
) -> None:
    expected = expected_trade_date or datetime.now(COLOMBO_TZ).date().isoformat()
    try:
        parsed_trade_date = date.fromisoformat(trade_date)
        parsed_expected_date = date.fromisoformat(expected)
    except (TypeError, ValueError) as exc:
        raise DeliveryError("target and expected trading dates must be valid ISO dates") from exc
    if parsed_trade_date != parsed_expected_date:
        raise DeliveryError(
            f"manifest trading date {trade_date} does not match expected date {expected}"
        )


def _git_commit() -> str | None:
    configured = os.getenv("GITHUB_SHA")
    if configured:
        return configured
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def build_request(
    result_manifest_path: Path,
    calendar_path: Path,
    *,
    close_time: str = "14:30",
    expected_trade_date: str | None = None,
) -> dict[str, Any]:
    result = load_json(result_manifest_path)
    if result.get("contract_version") != "1" or result.get("status") != "accepted":
        raise DeliveryError("the current collection invocation was not fully accepted")
    trade_date = str(result.get("target_date", ""))
    captured_at = str(result.get("captured_at", ""))
    assert_after_close(captured_at, trade_date, close_time)
    assert_expected_trade_date(trade_date, expected_trade_date)
    calendar = load_calendar_entry(calendar_path, trade_date)

    validation = result.get("validation")
    if not isinstance(validation, dict):
        raise DeliveryError("result manifest has no validation summary")
    accepted_count = validation.get("accepted_rows")
    processed_count = validation.get("row_count")
    rejected_count = validation.get("rejected_rows")
    if (
        not isinstance(accepted_count, int)
        or accepted_count <= 0
        or accepted_count != processed_count
        or rejected_count != 0
        or validation.get("failures")
    ):
        raise DeliveryError("result manifest does not describe a fully accepted batch")

    accepted_path_value = result.get("accepted_path")
    metadata_path_value = result.get("metadata_path")
    if not isinstance(accepted_path_value, str) or not isinstance(metadata_path_value, str):
        raise DeliveryError("result manifest does not identify accepted data and metadata")
    accepted_path = (ROOT / accepted_path_value).resolve()
    metadata_path = (ROOT / metadata_path_value).resolve()
    if ROOT not in accepted_path.parents or ROOT not in metadata_path.parents:
        raise DeliveryError("result manifest paths must stay inside the dataset checkout")

    accepted = pd.read_csv(accepted_path)
    metadata = pd.read_csv(metadata_path)
    required_prices = {"date", "symbol", "open", "high", "low", "close", "volume"}
    if missing := required_prices - set(accepted.columns):
        raise DeliveryError(f"accepted OHLCV is missing columns: {', '.join(sorted(missing))}")
    if "symbol" not in metadata.columns or "company_name" not in metadata.columns:
        raise DeliveryError("metadata must contain symbol and company_name")
    if len(accepted) != accepted_count or set(accepted["date"].astype(str)) != {trade_date}:
        raise DeliveryError("accepted artifact count or trading date does not match its manifest")
    if accepted["symbol"].duplicated().any():
        raise DeliveryError("accepted artifact contains duplicate symbols")

    prices: list[dict[str, Any]] = []
    for row in accepted.to_dict(orient="records"):
        warnings = row.get("validation_warnings")
        warning_list = [] if warnings is None or pd.isna(warnings) else [
            part.strip() for part in str(warnings).split(";") if part.strip()
        ]
        repaired_raw = row.get("ohlc_repaired", False)
        repaired = (
            repaired_raw
            if isinstance(repaired_raw, bool)
            else str(repaired_raw).strip().lower() in {"true", "1", "yes"}
        )
        prices.append(
            {
                "symbol": str(row["symbol"]).strip().upper(),
                "open": decimal_string(row.get("open"), nullable=True),
                "high": decimal_string(row["high"]),
                "low": decimal_string(row["low"]),
                "close": decimal_string(row["close"]),
                "volume": integer_string(row["volume"]),
                "validation_warnings": warning_list,
                "ohlc_repaired": repaired,
            }
        )

    normalized_symbols = [row["symbol"] for row in prices]
    if len(set(normalized_symbols)) != len(normalized_symbols):
        raise DeliveryError("accepted artifact contains duplicate canonical symbols")

    metadata = metadata.copy()
    metadata["symbol"] = metadata["symbol"].astype(str).str.strip().str.upper()
    metadata_by_symbol = metadata.drop_duplicates("symbol").set_index("symbol")
    securities: list[dict[str, Any]] = []
    for symbol in sorted(row["symbol"] for row in prices):
        if symbol not in metadata_by_symbol.index:
            raise DeliveryError(f"metadata is missing symbol {symbol}")
        row = metadata_by_symbol.loc[symbol]
        company_name = str(row.get("company_name", "")).strip()
        if not company_name or company_name.lower() == "nan":
            raise DeliveryError(f"metadata has no company name for {symbol}")
        security: dict[str, Any] = {
            "symbol": symbol,
            "company_name": company_name,
            "cse_code": symbol,
        }
        shares = integer_string(row.get("shares_outstanding"), nullable=True)
        if shares is not None:
            security["shares_outstanding"] = shares
        securities.append(security)

    market_digest = canonical_digest(trade_date, prices)
    raw_hash = str(result.get("raw_payload_hash", ""))
    if not re.fullmatch(r"[a-f0-9]{64}", raw_hash):
        raise DeliveryError("result manifest has an invalid raw payload hash")
    source_name = str(result.get("source_name", "")).strip()
    if not source_name:
        raise DeliveryError("result manifest has no source name")
    batch_seed = json.dumps(
        {
            "contract_version": "1",
            "trade_date": trade_date,
            "source_name": source_name,
            "raw_payload_hash": raw_hash,
            "market_digest": market_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    batch_id = hashlib.sha256(batch_seed.encode("utf-8")).hexdigest()
    run_url = os.getenv("GITHUB_SERVER_URL")
    repository = os.getenv("GITHUB_REPOSITORY")
    run_id = os.getenv("GITHUB_RUN_ID")
    action_run_url = (
        f"{run_url}/{repository}/actions/runs/{run_id}"
        if run_url and repository and run_id
        else None
    )
    producer_commit = _git_commit()
    request_body = {
        "contract_version": "1",
        "batch_id": batch_id,
        "trade_date": trade_date,
        "source": {
            "name": source_name,
            "captured_at": captured_at,
            "source_date_method": str(result.get("source_date_method", "")),
            "raw_payload_hash": raw_hash,
            **({"producer_commit": producer_commit} if producer_commit else {}),
            **({"action_run_url": action_run_url} if action_run_url else {}),
        },
        "calendar": calendar,
        "validation": {
            "processed": processed_count,
            "accepted": accepted_count,
            "rejected": rejected_count,
            "repaired": int(validation.get("ohlc_repaired_rows", 0)),
        },
        "securities": securities,
        "prices": prices,
        "market_digest": market_digest,
    }
    encoded = json.dumps(request_body, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_REQUEST_BYTES:
        raise DeliveryError(f"request is {len(encoded)} bytes; maximum is {MAX_REQUEST_BYTES}")
    return request_body


def _request_with_retries(
    session: requests.Session,
    method: str,
    url: str,
    *,
    token: str,
    body: dict[str, Any] | None = None,
    attempts: int = 5,
    timeout: float = 30,
) -> requests.Response:
    headers = {"Authorization": f"Bearer {token}"}
    for attempt in range(attempts):
        try:
            response = session.request(method, url, headers=headers, json=body, timeout=timeout)
        except (requests.Timeout, requests.ConnectionError):
            if attempt == attempts - 1:
                raise
        else:
            if response.status_code not in {429, 500, 502, 503, 504}:
                return response
            if attempt == attempts - 1:
                return response
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                time.sleep(min(float(retry_after), 30.0))
                continue
        time.sleep(min(2**attempt + random.random(), 30.0))
    raise AssertionError("retry loop exhausted")


def deliver(
    request_body: dict[str, Any],
    *,
    api_url: str,
    token: str,
    session: requests.Session | None = None,
    prefer_existing_receipt: bool = False,
) -> dict[str, Any]:
    try:
        parsed_api_url = urlsplit(api_url)
        parsed_api_url.port
    except ValueError as exc:
        raise DeliveryError("the ingestion API URL is invalid") from exc
    local_http = (
        parsed_api_url.scheme == "http"
        and parsed_api_url.hostname in {"localhost", "127.0.0.1", "::1"}
    )
    if (
        not parsed_api_url.hostname
        or (parsed_api_url.scheme != "https" and not local_http)
        or parsed_api_url.username is not None
        or parsed_api_url.password is not None
        or parsed_api_url.query
        or parsed_api_url.fragment
    ):
        raise DeliveryError("the ingestion API must use HTTPS outside localhost")
    if not token:
        raise DeliveryError("the ingestion token is empty")
    batch_id = request_body.get("batch_id")
    trade_date = request_body.get("trade_date")
    market_digest = request_body.get("market_digest")
    if not isinstance(batch_id, str) or not SHA256.fullmatch(batch_id):
        raise DeliveryError("the ingestion request has an invalid batch_id")
    if not isinstance(trade_date, str):
        raise DeliveryError("the ingestion request has no trade_date")
    if not isinstance(market_digest, str) or not SHA256.fullmatch(market_digest):
        raise DeliveryError("the ingestion request has an invalid market_digest")
    client = session or requests.Session()
    endpoint = urljoin(api_url.rstrip("/") + "/", "internal/v1/ingestions/eod")

    if prefer_existing_receipt:
        existing = _request_with_retries(
            client,
            "GET",
            f"{endpoint}/{batch_id}",
            token=token,
        )
        if existing.status_code == 200:
            return _validated_receipt(existing, request_body)
        if existing.status_code != 404:
            raise DeliveryError(
                f"batch-receipt lookup failed with HTTP {existing.status_code}"
            )

    latest = _request_with_retries(client, "GET", endpoint + "/latest", token=token)
    if latest.status_code != 200:
        raise DeliveryError(f"latest-receipt check failed with HTTP {latest.status_code}")
    latest_data = latest.json().get("data")
    if (
        isinstance(latest_data, dict)
        and latest_data.get("trade_date") != request_body["trade_date"]
        and latest_data.get("market_digest") == request_body["market_digest"]
    ):
        raise DeliveryError(
            f"market digest repeats the accepted snapshot from {latest_data.get('trade_date')}"
        )

    response = _request_with_retries(client, "POST", endpoint, token=token, body=request_body)
    if response.status_code not in {200, 201}:
        try:
            details = response.json()
        except ValueError:
            details = response.text[:500]
        raise DeliveryError(f"ingestion failed with HTTP {response.status_code}: {details}")
    return _validated_receipt(response, request_body)


def _validated_receipt(
    response: requests.Response,
    request_body: dict[str, Any],
) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise DeliveryError("ingestion API returned an invalid receipt") from exc
    receipt = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(receipt, dict):
        raise DeliveryError("ingestion API returned no durable receipt")
    if receipt.get("batch_id") != request_body.get("batch_id"):
        raise DeliveryError("ingestion receipt batch_id does not match the request")
    if receipt.get("trade_date") != request_body.get("trade_date"):
        raise DeliveryError("ingestion receipt trade_date does not match the request")
    if receipt.get("market_digest") != request_body.get("market_digest"):
        raise DeliveryError("ingestion receipt market_digest does not match the request")
    records_accepted = receipt.get("records_accepted")
    if (
        not isinstance(records_accepted, int)
        or isinstance(records_accepted, bool)
        or records_accepted < 0
    ):
        raise DeliveryError("ingestion receipt has an invalid records_accepted value")
    validation = request_body.get("validation")
    expected_records = validation.get("accepted") if isinstance(validation, dict) else None
    if isinstance(expected_records, int) and records_accepted != expected_records:
        raise DeliveryError("ingestion receipt accepted count does not match the request")
    if receipt.get("status") != "succeeded":
        raise DeliveryError("ingestion receipt does not report a successful delivery")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish one accepted EOD batch to TradeIQ")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--result-manifest", type=Path)
    source.add_argument("--replay-request", type=Path)
    parser.add_argument("--calendar-path", type=Path)
    parser.add_argument("--api-url", default=os.getenv("TRADEIQ_INGESTION_API_URL"))
    parser.add_argument("--token", default=os.getenv("TRADEIQ_INGESTION_TOKEN"))
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--close-time", default="14:30")
    parser.add_argument(
        "--expected-trade-date",
        help="Expected YYYY-MM-DD date; defaults to today's Asia/Colombo date",
    )
    args = parser.parse_args()
    if not args.api_url or not args.token:
        raise DeliveryError("api URL and token are required")
    if args.replay_request:
        request_body = load_json(args.replay_request)
    else:
        if not args.calendar_path:
            raise DeliveryError("--calendar-path is required when building a request")
        request_body = build_request(
            args.result_manifest,
            args.calendar_path,
            close_time=args.close_time,
            expected_trade_date=args.expected_trade_date,
        )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    request_path = args.out_dir / "eod_ingestion_request.json"
    request_path.write_text(json.dumps(request_body, indent=2) + "\n")
    receipt = deliver(
        request_body,
        api_url=args.api_url,
        token=args.token,
        prefer_existing_receipt=args.replay_request is not None,
    )
    (args.out_dir / "eod_ingestion_receipt.json").write_text(
        json.dumps(receipt, indent=2) + "\n"
    )
    print(
        f"DELIVERED: {receipt['records_accepted']} rows for {receipt['trade_date']} "
        f"(batch {receipt['batch_id']})"
    )


if __name__ == "__main__":
    main()
