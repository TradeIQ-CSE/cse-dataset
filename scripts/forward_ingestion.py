"""Validation-first 2026-forward source ingestion.

This module is intentionally separate from both historical workbook conversion
and the current-day ``tradeSummary`` collector. It accepts only date-bearing
source files, writes immutable raw payload artifacts, normalizes candidate rows,
and publishes accepted rows only after validation succeeds.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from requests import RequestException

try:
    import pdfplumber
except ImportError:  # pragma: no cover - dependency is declared, but keep imports robust.
    pdfplumber = None

try:
    from .ohlcv_sources import parse_number
    from .ohlcv_validation import load_metadata, validate_ohlcv_records, write_validation_outputs
except ImportError:  # pragma: no cover - used when scripts are executed directly.
    from ohlcv_sources import parse_number
    from ohlcv_validation import load_metadata, validate_ohlcv_records, write_validation_outputs


ROOT = Path(__file__).resolve().parents[1]
FORWARD_START_DATE = date(2026, 1, 1)
OFFICIAL_FILE_COVERAGE_END = date(2025, 12, 31)
COLOMBO_TZ = ZoneInfo("Asia/Colombo")
CSE_MARKET_CLOSE_BUFFER = time(14, 45)

RAW_ROOT = ROOT / "data/raw/2026_forward"
PROCESSED_ROOT = ROOT / "data/processed/2026_forward"
VALIDATION_ROOT = ROOT / "data/processed/validation/2026_forward"
DEFAULT_METADATA = ROOT / "data/processed/company_metadata.csv"
DEFAULT_CSE_DAILY_REPORT_URL_TEMPLATE = (
    "https://cdn.cse.lk/cse-daily/StockMarketDaily%28SMD%29{dd}-{mm}-{yyyy}.pdf"
)
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
CSE_TODAY_SHARE_PRICE_URL = "https://www.cse.lk/api/todaySharePrice"
CSE_TRADE_SUMMARY_URL = "https://www.cse.lk/api/tradeSummary"
REQUEST_HEADERS = {"User-Agent": "cse-dataset-v2/0.1 (+https://github.com/nimeshk03/cse-dataset-v2)"}

DATASET_FAMILIES = {
    "ohlcv",
    "indices",
    "market_stats",
    "corporate_actions",
    "listings",
    "public_holdings",
    "foreign_holdings",
    "sector_gics",
    "news_announcements",
    "macro_rates",
}


class ForwardIngestionError(RuntimeError):
    """Base exception for quarantine-worthy forward-ingestion failures."""


class MissingSourceError(ForwardIngestionError):
    """Raised when a source is not configured or cannot be fetched."""


class ReportDateError(ForwardIngestionError):
    """Raised when a payload does not expose exactly one report date."""


@dataclass(frozen=True)
class ForwardFetchResult:
    family: str
    source_name: str
    requested_date: date
    report_date: date | None
    fetch_time_utc: datetime
    source_url: str
    payload: bytes
    text: str
    payload_hash: str
    row_count: int
    raw_payload_path: Path


@dataclass(frozen=True)
class GenericValidationResult:
    accepted: pd.DataFrame
    rejected: pd.DataFrame
    failures: list[str]
    warnings: list[str]
    metrics: dict[str, Any]

    @property
    def passed(self) -> bool:
        return not self.failures and self.rejected.empty and not self.accepted.empty


@dataclass(frozen=True)
class FamilyContract:
    family: str
    canonical_columns: list[str]
    required_columns: list[str]
    date_columns: list[str]
    identity_columns: list[str]
    numeric_columns: list[str]
    nonnegative_columns: list[str]
    aliases: dict[str, list[str]]
    allowed_values: dict[str, set[str]] | None = None


FAMILY_CONTRACTS: dict[str, FamilyContract] = {
    "indices": FamilyContract(
        family="indices",
        canonical_columns=["date", "index_name", "close", "source", "source_timestamp", "raw_payload_hash"],
        required_columns=["date", "index_name", "close"],
        date_columns=["date"],
        identity_columns=["date", "index_name"],
        numeric_columns=["close"],
        nonnegative_columns=["close"],
        aliases={
            "date": ["date", "trade_date", "report_date"],
            "index_name": ["index_name", "index", "index_code", "name"],
            "close": ["close", "closing", "closing_value", "value"],
        },
    ),
    "market_stats": FamilyContract(
        family="market_stats",
        canonical_columns=["date", "metric", "value", "source", "source_timestamp", "raw_payload_hash"],
        required_columns=["date", "metric", "value"],
        date_columns=["date"],
        identity_columns=["date", "metric"],
        numeric_columns=["value"],
        nonnegative_columns=["value"],
        aliases={
            "date": ["date", "trade_date", "report_date"],
            "metric": ["metric", "statistic", "field", "name"],
            "value": ["value", "amount", "total"],
        },
    ),
    "corporate_actions": FamilyContract(
        family="corporate_actions",
        canonical_columns=[
            "announcement_date",
            "symbol",
            "event_type",
            "record_date",
            "ex_date",
            "payment_date",
            "amount",
            "currency",
            "source",
            "source_timestamp",
            "raw_payload_hash",
        ],
        required_columns=["announcement_date", "symbol", "event_type"],
        date_columns=["announcement_date", "record_date", "ex_date", "payment_date"],
        identity_columns=["announcement_date", "symbol", "event_type"],
        numeric_columns=["amount"],
        nonnegative_columns=["amount"],
        aliases={
            "announcement_date": ["announcement_date", "date_of_announcement", "dateofannouncement", "date"],
            "symbol": ["symbol", "security", "security_code", "securitycode", "code"],
            "event_type": ["event_type", "announcement_category", "announcementcategory", "type", "category"],
            "record_date": ["record_date", "recorddate"],
            "ex_date": ["ex_date", "xd", "x_date"],
            "payment_date": ["payment_date", "paymentdate"],
            "amount": ["amount", "amount_per_share", "dividend", "rate"],
            "currency": ["currency", "ccy"],
        },
    ),
    "listings": FamilyContract(
        family="listings",
        canonical_columns=["event_date", "symbol", "event_type", "company", "source", "source_timestamp", "raw_payload_hash"],
        required_columns=["event_date", "symbol", "event_type"],
        date_columns=["event_date"],
        identity_columns=["event_date", "symbol", "event_type"],
        numeric_columns=[],
        nonnegative_columns=[],
        aliases={
            "event_date": ["event_date", "listing_date", "delisting_date", "date"],
            "symbol": ["symbol", "security", "security_code", "securitycode", "code"],
            "event_type": ["event_type", "type", "listing_status", "status"],
            "company": ["company", "company_name", "name"],
        },
        allowed_values={"event_type": {"listing", "listed", "new listing", "de-listing", "delisting", "de listed"}},
    ),
    "public_holdings": FamilyContract(
        family="public_holdings",
        canonical_columns=["report_date", "symbol", "public_holding_pct", "source", "source_timestamp", "raw_payload_hash"],
        required_columns=["report_date", "symbol", "public_holding_pct"],
        date_columns=["report_date"],
        identity_columns=["report_date", "symbol"],
        numeric_columns=["public_holding_pct"],
        nonnegative_columns=["public_holding_pct"],
        aliases={
            "report_date": ["report_date", "date", "quarter_end", "as_of_date"],
            "symbol": ["symbol", "security", "security_code", "securitycode", "code"],
            "public_holding_pct": ["public_holding_pct", "public_holding", "percentage", "holding_pct"],
        },
    ),
    "foreign_holdings": FamilyContract(
        family="foreign_holdings",
        canonical_columns=["report_date", "symbol", "foreign_holding_pct", "source", "source_timestamp", "raw_payload_hash"],
        required_columns=["report_date", "symbol", "foreign_holding_pct"],
        date_columns=["report_date"],
        identity_columns=["report_date", "symbol"],
        numeric_columns=["foreign_holding_pct"],
        nonnegative_columns=["foreign_holding_pct"],
        aliases={
            "report_date": ["report_date", "date", "year_end", "as_of_date"],
            "symbol": ["symbol", "security", "security_code", "securitycode", "code"],
            "foreign_holding_pct": ["foreign_holding_pct", "foreign_holding", "percentage", "holding_pct"],
        },
    ),
    "sector_gics": FamilyContract(
        family="sector_gics",
        canonical_columns=["date", "sector", "metric", "value", "source", "source_timestamp", "raw_payload_hash"],
        required_columns=["date", "sector", "metric", "value"],
        date_columns=["date"],
        identity_columns=["date", "sector", "metric"],
        numeric_columns=["value"],
        nonnegative_columns=["value"],
        aliases={
            "date": ["date", "report_date", "trade_date"],
            "sector": ["sector", "gics_sector", "industry_group"],
            "metric": ["metric", "field", "name"],
            "value": ["value", "amount", "index_value"],
        },
    ),
    "news_announcements": FamilyContract(
        family="news_announcements",
        canonical_columns=["published_date", "symbol", "title", "url", "source", "source_timestamp", "raw_payload_hash"],
        required_columns=["published_date", "title"],
        date_columns=["published_date"],
        identity_columns=["published_date", "symbol", "title"],
        numeric_columns=[],
        nonnegative_columns=[],
        aliases={
            "published_date": ["published_date", "announcement_date", "date", "uploaded_date"],
            "symbol": ["symbol", "security", "security_code", "securitycode", "code"],
            "title": ["title", "headline", "file_text", "filetext"],
            "url": ["url", "path", "link"],
        },
    ),
    "macro_rates": FamilyContract(
        family="macro_rates",
        canonical_columns=["date", "metric", "value", "currency", "source", "source_timestamp", "raw_payload_hash"],
        required_columns=["date", "metric", "value"],
        date_columns=["date"],
        identity_columns=["date", "metric", "currency"],
        numeric_columns=["value"],
        nonnegative_columns=["value"],
        aliases={
            "date": ["date", "rate_date", "report_date", "period"],
            "metric": ["metric", "rate_type", "indicator", "currency_code", "currency"],
            "value": ["value", "rate", "buying_rate", "selling_rate", "mid_rate"],
            "currency": ["currency", "currency_code", "ccy"],
        },
    ),
}


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def is_after_cse_market_close(now: datetime | None = None) -> bool:
    current = now or datetime.now(COLOMBO_TZ)
    local = current.astimezone(COLOMBO_TZ)
    return local.time() >= CSE_MARKET_CLOSE_BUFFER


def require_forward_target_date(target_date: date) -> None:
    if target_date < FORWARD_START_DATE:
        raise ValueError(
            "2026-forward ingestion only accepts targets from "
            f"{FORWARD_START_DATE.isoformat()} onward; got {target_date.isoformat()}"
        )


def format_source_template(template: str, target_date: date) -> str:
    return template.format(
        date=target_date.isoformat(),
        yyyymmdd=target_date.strftime("%Y%m%d"),
        ddmmyyyy=target_date.strftime("%d%m%Y"),
        yyyy=target_date.strftime("%Y"),
        mm=target_date.strftime("%m"),
        dd=target_date.strftime("%d"),
    )


def _decode_payload(payload: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def _extract_pdf_text(path: Path) -> str:
    if pdfplumber is None:
        return ""
    try:
        with pdfplumber.open(path) as pdf:
            pages = [(page.extract_text() or "") for page in pdf.pages]
        return "\n".join(pages)
    except Exception:
        return ""


def payload_text(payload: bytes, payload_path: Path | None = None) -> str:
    if payload_path and payload_path.suffix.lower() == ".pdf":
        pdf_text = _extract_pdf_text(payload_path)
        if pdf_text.strip():
            return pdf_text
    return _decode_payload(payload)


DATE_PATTERNS = [
    re.compile(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b"),
    re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](20\d{2})\b"),
    re.compile(
        r"\b(\d{1,2})\s+"
        r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r"\s+(20\d{2})\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(\d{1,2})\s+"
        r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r",?\s+(20\d{2})\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(\d{1,2})[- ]"
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
        r"[- ](\d{2})\b",
        re.IGNORECASE,
    ),
]
LABELED_REPORT_DATE_PATTERN = re.compile(
    r"\b(?:report|source|as\s+of|rate|trade)\s+date\s*[:\-]?\s*"
    r"((?:20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})|"
    r"(?:\d{1,2}[-/.]\d{1,2}[-/.]20\d{2})|"
    r"(?:\d{1,2}\s+"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+20\d{2}))",
    re.IGNORECASE,
)

MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def parse_report_dates(text: str) -> set[date]:
    dates: set[date] = set()
    for match in DATE_PATTERNS[0].finditer(text):
        year, month, day = (int(part) for part in match.groups())
        try:
            dates.add(date(year, month, day))
        except ValueError:
            continue
    for match in DATE_PATTERNS[1].finditer(text):
        day, month, year = (int(part) for part in match.groups())
        try:
            dates.add(date(year, month, day))
        except ValueError:
            continue
    for match in DATE_PATTERNS[2].finditer(text):
        day_text, month_text, year_text = match.groups()
        month = MONTHS[month_text[:3].lower()]
        try:
            dates.add(date(int(year_text), month, int(day_text)))
        except ValueError:
            continue
    for match in DATE_PATTERNS[3].finditer(text):
        day_text, month_text, year_text = match.groups()
        month = MONTHS[month_text[:3].lower()]
        try:
            dates.add(date(int(year_text), month, int(day_text)))
        except ValueError:
            continue
    for match in DATE_PATTERNS[4].finditer(text):
        day_text, month_text, year_text = match.groups()
        month = MONTHS[month_text[:3].lower()]
        try:
            dates.add(date(2000 + int(year_text), month, int(day_text)))
        except ValueError:
            continue
    return dates


def parse_single_report_date(text: str) -> date:
    header_date = parse_cse_daily_report_header_date(text)
    if header_date:
        return header_date

    labeled_dates: set[date] = set()
    for match in LABELED_REPORT_DATE_PATTERN.finditer(text):
        labeled_dates.update(parse_report_dates(match.group(1)))
    if len(labeled_dates) == 1:
        return next(iter(labeled_dates))
    if len(labeled_dates) > 1:
        rendered = ", ".join(sorted(day.isoformat() for day in labeled_dates))
        raise ReportDateError(f"source payload contains multiple labeled report dates: {rendered}")

    dates = parse_report_dates(text)
    if not dates:
        raise ReportDateError("source payload does not contain a parseable report date")
    if len(dates) > 1:
        rendered = ", ".join(sorted(day.isoformat() for day in dates))
        raise ReportDateError(f"source payload contains multiple report dates: {rendered}")
    return next(iter(dates))


def parse_cse_daily_report_header_date(text: str) -> date | None:
    """Parse the date printed in the header of CSE StockMarketDaily PDFs.

    The new CSE report format starts with lines such as
    ``Thursday, 20 November, 2025`` and often also includes ``20-Nov-25``.
    Parsing the header first avoids treating unrelated macro dates elsewhere in
    the report as the report date.
    """

    header = "\n".join(text.splitlines()[:25])
    weekday_date = re.search(
        r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
        r"(\d{1,2})\s+"
        r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r",?\s+(20\d{2})\b",
        header,
        re.IGNORECASE,
    )
    if weekday_date:
        day_text, month_text, year_text = weekday_date.groups()
        try:
            return date(int(year_text), MONTHS[month_text[:3].lower()], int(day_text))
        except ValueError:
            return None

    compact_dates = parse_report_dates(header)
    return next(iter(compact_dates)) if len(compact_dates) == 1 else None


def _extension_from_location(location: str) -> str:
    suffix = Path(urlparse(location).path).suffix.lower()
    if suffix in {".csv", ".txt", ".json", ".pdf", ".xls", ".xlsx"}:
        return suffix
    return ".bin"


def raw_payload_path(raw_root: Path, family: str, target_date: date, source_name: str, digest: str, ext: str) -> Path:
    return raw_root / "source_payloads" / family / target_date.isoformat() / source_name / f"{digest}{ext}"


def _date_to_epoch_seconds(value: date) -> int:
    return int(datetime.combine(value, time.min, tzinfo=timezone.utc).timestamp())


def cse_symbol_to_yahoo(symbol: str) -> str:
    return f"{str(symbol).strip().replace('.', '-')}.CM"


def yahoo_symbol_to_cse(symbol: str) -> str:
    text = str(symbol).strip()
    if text.endswith(".CM"):
        text = text[:-3]
    return text.replace("-", ".")


def _stable_json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _extract_yahoo_chart_rows(payload: Any, fallback_yahoo_symbol: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    chart = payload.get("chart")
    if not isinstance(chart, dict):
        return []
    results = chart.get("result")
    if not isinstance(results, list) or not results:
        return []
    result = results[0]
    if not isinstance(result, dict):
        return []
    timestamps = result.get("timestamp")
    indicators = result.get("indicators", {})
    quotes = indicators.get("quote") if isinstance(indicators, dict) else None
    if not isinstance(timestamps, list) or not isinstance(quotes, list) or not quotes:
        return []
    quote = quotes[0]
    if not isinstance(quote, dict):
        return []
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    yahoo_symbol = str(meta.get("symbol") or fallback_yahoo_symbol)
    offset_seconds = int(meta.get("gmtoffset") or 0)
    exchange_tz = timezone(timedelta(seconds=offset_seconds))

    rows: list[dict[str, Any]] = []
    for index, timestamp in enumerate(timestamps):
        try:
            trade_date = datetime.fromtimestamp(int(timestamp), exchange_tz).date().isoformat()
        except (TypeError, ValueError, OSError):
            continue
        row: dict[str, Any] = {"date": trade_date, "symbol": yahoo_symbol_to_cse(yahoo_symbol)}
        for field_name in ("open", "high", "low", "close", "volume"):
            values = quote.get(field_name)
            if isinstance(values, list) and index < len(values):
                row[field_name] = values[index]
        if all(row.get(field_name) is None for field_name in ("open", "high", "low", "close", "volume")):
            continue
        rows.append(row)
    return rows


class CSEDailyReportOHLCVSource:
    """Date-bearing CSE daily report/PDF source for 2026-forward OHLCV.

    The adapter can read a local file for testing/manual runs or fetch a URL
    template that contains date placeholders. Candidate rows are accepted only
    if the report date parsed from the payload equals the target date.
    """

    source_name = "cse_daily_report_pdf"
    family = "ohlcv"
    source_priority = 20

    def __init__(
        self,
        *,
        source_file: Path | None = None,
        source_url_template: str | None = None,
        timeout: int = 30,
    ) -> None:
        self.source_file = source_file
        self.source_url_template = source_url_template
        self.timeout = timeout

    def fetch_for_date(self, target_date: date, raw_root: Path = RAW_ROOT) -> ForwardFetchResult:
        require_forward_target_date(target_date)
        fetch_time_utc = datetime.now(timezone.utc)

        if self.source_file:
            if not self.source_file.exists():
                raise MissingSourceError(f"missing daily report file: {self.source_file}")
            payload = self.source_file.read_bytes()
            source_url = self.source_file.resolve().as_uri()
            ext = self.source_file.suffix.lower() or ".bin"
        elif self.source_url_template:
            source_url = format_source_template(self.source_url_template, target_date)
            try:
                response = requests.get(
                    source_url,
                    headers={"User-Agent": "cse-dataset-v2/0.1 (+https://github.com/nimeshk03/cse-dataset-v2)"},
                    timeout=self.timeout,
                )
            except RequestException as exc:
                raise MissingSourceError(f"daily report fetch failed at {source_url}: {exc}") from exc
            if response.status_code in {403, 404}:
                raise MissingSourceError(f"daily report not found or not public at {source_url}")
            try:
                response.raise_for_status()
            except RequestException as exc:
                raise MissingSourceError(f"daily report fetch failed at {source_url}: {exc}") from exc
            payload = response.content
            ext = _extension_from_location(source_url)
        else:
            raise MissingSourceError("no 2026-forward daily report source was configured")

        digest = sha256_bytes(payload)
        payload_path = raw_payload_path(raw_root, self.family, target_date, self.source_name, digest, ext)
        payload_path.parent.mkdir(parents=True, exist_ok=True)
        if not payload_path.exists():
            payload_path.write_bytes(payload)

        text = payload_text(payload, payload_path)
        try:
            report_date = parse_single_report_date(text)
        except ReportDateError:
            failed_fetch = ForwardFetchResult(
                family=self.family,
                source_name=self.source_name,
                requested_date=target_date,
                report_date=None,
                fetch_time_utc=fetch_time_utc,
                source_url=source_url,
                payload=payload,
                text=text,
                payload_hash=digest,
                row_count=0,
                raw_payload_path=payload_path,
            )
            write_fetch_metadata(failed_fetch, "quarantined")
            raise
        rows = self.normalize(text, report_date=report_date, payload_hash=digest)

        return ForwardFetchResult(
            family=self.family,
            source_name=self.source_name,
            requested_date=target_date,
            report_date=report_date,
            fetch_time_utc=fetch_time_utc,
            source_url=source_url,
            payload=payload,
            text=text,
            payload_hash=digest,
            row_count=len(rows),
            raw_payload_path=payload_path,
        )

    def normalize(self, payload: bytes | str, *, report_date: date, payload_hash: str) -> pd.DataFrame:
        text = payload if isinstance(payload, str) else _decode_payload(payload)
        table = _read_embedded_csv_table(text)
        normalized: list[dict[str, Any]] = []
        for _, row in table.iterrows():
            record_date = _first_value(row, ["date", "trade_date", "report_date"])
            candidate_date = _coerce_date(record_date) or report_date
            symbol = _first_value(row, ["symbol", "security", "security_code", "securitycode", "code"])
            if not symbol:
                continue
            normalized.append(
                {
                    "date": candidate_date.isoformat(),
                    "symbol": str(symbol).strip(),
                    "open": parse_number(_first_value(row, ["open", "opening_price", "opening"])),
                    "high": parse_number(_first_value(row, ["high", "highest"])),
                    "low": parse_number(_first_value(row, ["low", "lowest"])),
                    "close": parse_number(_first_value(row, ["close", "closing_price", "closingprice", "closing"])),
                    "volume": parse_number(_first_value(row, ["volume", "sharevolume", "shares_traded"])),
                    "turnover": parse_number(_first_value(row, ["turnover", "value"])),
                    "trades": parse_number(_first_value(row, ["trades", "tradevolume", "no_of_trades"])),
                    "source": self.source_name,
                    "source_priority": self.source_priority,
                    "source_timestamp": report_date.isoformat(),
                    "raw_payload_hash": payload_hash,
                    "validation_status": "candidate",
                    "validation_warnings": "",
                }
            )
        return pd.DataFrame(normalized)

    def validate_source_date(self, records: pd.DataFrame, target_date: date, report_date: date | None) -> list[str]:
        failures: list[str] = []
        if report_date is None:
            failures.append("source report date is missing")
        elif report_date != target_date:
            failures.append(
                "source report date mismatch: "
                f"requested {target_date.isoformat()}, report {report_date.isoformat()}"
            )
        if records.empty:
            failures.append("source returned zero normalized rows")
        return failures


class YahooFinanceOHLCVSource:
    """Third-party Yahoo Finance chart candidate for 2026-forward OHLCV.

    The adapter fetches one target trading date across the local CSE metadata
    universe. Rows are candidates only; acceptance still depends on OHLCV
    validation and optional official CSE same-day cross-checks.
    """

    source_name = "yahoo_finance_chart_candidate"
    family = "ohlcv"
    source_priority = 80

    def __init__(self, *, symbols: list[str], timeout: int = 15, sleep_seconds: float = 0.03) -> None:
        self.symbols = symbols
        self.timeout = timeout
        self.sleep_seconds = sleep_seconds

    def fetch_for_date(self, target_date: date, raw_root: Path = RAW_ROOT) -> ForwardFetchResult:
        require_forward_target_date(target_date)
        if not self.symbols:
            raise MissingSourceError("Yahoo candidate source has no symbols to fetch")

        fetch_time_utc = datetime.now(timezone.utc)
        period1 = _date_to_epoch_seconds(target_date)
        period2 = _date_to_epoch_seconds(target_date + timedelta(days=1))
        payload: dict[str, Any] = {
            "source": self.source_name,
            "requested_date": target_date.isoformat(),
            "period1": period1,
            "period2": period2,
            "responses": {},
            "errors": {},
        }

        session = requests.Session()
        for symbol in self.symbols:
            yahoo_symbol = cse_symbol_to_yahoo(symbol)
            url = YAHOO_CHART_URL.format(symbol=yahoo_symbol)
            try:
                response = session.get(
                    url,
                    params={"period1": str(period1), "period2": str(period2), "interval": "1d", "events": "history"},
                    headers=REQUEST_HEADERS,
                    timeout=self.timeout,
                )
                if response.status_code >= 400:
                    payload["errors"][symbol] = {"yahoo_symbol": yahoo_symbol, "status_code": response.status_code}
                    continue
                payload["responses"][symbol] = {
                    "yahoo_symbol": yahoo_symbol,
                    "status_code": response.status_code,
                    "payload": response.json(),
                }
            except (RequestException, json.JSONDecodeError) as exc:
                payload["errors"][symbol] = {"yahoo_symbol": yahoo_symbol, "error": str(exc)}
            if self.sleep_seconds:
                import time as _time

                _time.sleep(self.sleep_seconds)

        payload_bytes = _stable_json_bytes(payload)
        digest = sha256_bytes(payload_bytes)
        payload_path = raw_payload_path(raw_root, self.family, target_date, self.source_name, digest, ".json")
        payload_path.parent.mkdir(parents=True, exist_ok=True)
        if not payload_path.exists():
            payload_path.write_bytes(payload_bytes)

        records = self.normalize(payload_bytes, report_date=target_date, payload_hash=digest)
        return ForwardFetchResult(
            family=self.family,
            source_name=self.source_name,
            requested_date=target_date,
            report_date=target_date,
            fetch_time_utc=fetch_time_utc,
            source_url=YAHOO_CHART_URL,
            payload=payload_bytes,
            text=payload_path.read_text(),
            payload_hash=digest,
            row_count=len(records),
            raw_payload_path=payload_path,
        )

    def normalize(self, payload: bytes | str, *, report_date: date, payload_hash: str) -> pd.DataFrame:
        text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            return pd.DataFrame()

        normalized: list[dict[str, Any]] = []
        responses = raw.get("responses", {}) if isinstance(raw, dict) else {}
        if not isinstance(responses, dict):
            return pd.DataFrame()
        for cse_symbol, wrapper in responses.items():
            if not isinstance(wrapper, dict):
                continue
            yahoo_symbol = str(wrapper.get("yahoo_symbol") or cse_symbol_to_yahoo(cse_symbol))
            rows = _extract_yahoo_chart_rows(wrapper.get("payload"), fallback_yahoo_symbol=yahoo_symbol)
            for row in rows:
                candidate_date = _coerce_date(row.get("date"))
                if candidate_date != report_date:
                    continue
                normalized.append(
                    {
                        "date": candidate_date.isoformat(),
                        "symbol": str(row.get("symbol") or cse_symbol).strip(),
                        "open": parse_number(row.get("open")),
                        "high": parse_number(row.get("high")),
                        "low": parse_number(row.get("low")),
                        "close": parse_number(row.get("close")),
                        "volume": parse_number(row.get("volume")),
                        "turnover": None,
                        "trades": None,
                        "source": self.source_name,
                        "source_priority": self.source_priority,
                        "source_timestamp": report_date.isoformat(),
                        "raw_payload_hash": payload_hash,
                        "validation_status": "candidate",
                        "validation_warnings": f"third-party yahoo_symbol={yahoo_symbol}; turnover/trades unavailable",
                    }
                )
        return pd.DataFrame(normalized)

    def validate_source_date(self, records: pd.DataFrame, target_date: date, report_date: date | None) -> list[str]:
        failures: list[str] = []
        if report_date != target_date:
            failures.append(
                "source report date mismatch: "
                f"requested {target_date.isoformat()}, report {report_date.isoformat() if report_date else 'missing'}"
            )
        if records.empty:
            failures.append("Yahoo source returned zero normalized rows")
        return failures


class DateBearingTableSource:
    """Generic date-bearing table source for non-OHLCV forward families."""

    def __init__(
        self,
        *,
        family: str,
        source_file: Path | None = None,
        source_url_template: str | None = None,
        source_name: str | None = None,
        timeout: int = 30,
    ) -> None:
        if family not in FAMILY_CONTRACTS:
            raise ValueError(f"No forward-ingestion contract is defined for family: {family}")
        self.family = family
        self.contract = FAMILY_CONTRACTS[family]
        self.source_file = source_file
        self.source_url_template = source_url_template
        self.source_name = source_name or f"official_{family}_table"
        self.timeout = timeout

    def fetch_for_date(self, target_date: date, raw_root: Path = RAW_ROOT) -> ForwardFetchResult:
        require_forward_target_date(target_date)
        fetch_time_utc = datetime.now(timezone.utc)

        if self.source_file:
            if not self.source_file.exists():
                raise MissingSourceError(f"missing {self.family} source file: {self.source_file}")
            payload = self.source_file.read_bytes()
            source_url = self.source_file.resolve().as_uri()
            ext = self.source_file.suffix.lower() or ".bin"
        elif self.source_url_template:
            source_url = format_source_template(self.source_url_template, target_date)
            try:
                response = requests.get(
                    source_url,
                    headers={"User-Agent": "cse-dataset-v2/0.1 (+https://github.com/nimeshk03/cse-dataset-v2)"},
                    timeout=self.timeout,
                )
            except RequestException as exc:
                raise MissingSourceError(f"{self.family} source fetch failed at {source_url}: {exc}") from exc
            if response.status_code in {403, 404}:
                raise MissingSourceError(f"{self.family} source not found or not public at {source_url}")
            try:
                response.raise_for_status()
            except RequestException as exc:
                raise MissingSourceError(f"{self.family} source fetch failed at {source_url}: {exc}") from exc
            payload = response.content
            ext = _extension_from_location(source_url)
        else:
            raise MissingSourceError(f"no 2026-forward {self.family} source was configured")

        digest = sha256_bytes(payload)
        payload_path = raw_payload_path(raw_root, self.family, target_date, self.source_name, digest, ext)
        payload_path.parent.mkdir(parents=True, exist_ok=True)
        if not payload_path.exists():
            payload_path.write_bytes(payload)

        text = payload_text(payload, payload_path)
        try:
            report_date = parse_single_report_date(text)
        except ReportDateError:
            failed_fetch = ForwardFetchResult(
                family=self.family,
                source_name=self.source_name,
                requested_date=target_date,
                report_date=None,
                fetch_time_utc=fetch_time_utc,
                source_url=source_url,
                payload=payload,
                text=text,
                payload_hash=digest,
                row_count=0,
                raw_payload_path=payload_path,
            )
            write_fetch_metadata(failed_fetch, "quarantined")
            raise

        records = self.normalize(text, report_date=report_date, payload_hash=digest)
        return ForwardFetchResult(
            family=self.family,
            source_name=self.source_name,
            requested_date=target_date,
            report_date=report_date,
            fetch_time_utc=fetch_time_utc,
            source_url=source_url,
            payload=payload,
            text=text,
            payload_hash=digest,
            row_count=len(records),
            raw_payload_path=payload_path,
        )

    def normalize(self, payload: bytes | str, *, report_date: date, payload_hash: str) -> pd.DataFrame:
        text = payload if isinstance(payload, str) else _decode_payload(payload)
        raw = _read_generic_table(text)
        return normalize_family_table(
            raw,
            self.contract,
            report_date=report_date,
            source_name=self.source_name,
            payload_hash=payload_hash,
        )

    def validate_source_date(self, records: pd.DataFrame, target_date: date, report_date: date | None) -> list[str]:
        failures: list[str] = []
        if report_date is None:
            failures.append("source report date is missing")
        elif report_date != target_date:
            failures.append(
                "source report date mismatch: "
                f"requested {target_date.isoformat()}, report {report_date.isoformat()}"
            )
        if records.empty:
            failures.append(f"source returned zero normalized {self.family} rows")
        return failures


def _normalize_header(value: Any) -> str:
    text = str(value).strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def _read_embedded_csv_table(text: str) -> pd.DataFrame:
    lines = [line for line in text.splitlines() if line.strip()]
    header_index: int | None = None
    for idx, line in enumerate(lines):
        lowered = line.lower()
        if ("symbol" in lowered or "security" in lowered) and "," in line:
            header_index = idx
            break
    if header_index is None:
        return _read_whitespace_ohlcv_rows(text)

    csv_text = "\n".join(lines[header_index:])
    rows = list(csv.reader(io.StringIO(csv_text)))
    if not rows:
        return pd.DataFrame()
    width = len(rows[0])
    clean_rows = [row for row in rows if len(row) == width]
    if len(clean_rows) < 2:
        return pd.DataFrame()
    header = [_normalize_header(col) for col in clean_rows[0]]
    return pd.DataFrame(clean_rows[1:], columns=header)


def _read_generic_table(text: str) -> pd.DataFrame:
    stripped = text.strip()
    if not stripped:
        return pd.DataFrame()
    if stripped[0] in "[{":
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                for value in data.values():
                    if isinstance(value, list):
                        data = value
                        break
            if isinstance(data, list):
                return pd.json_normalize(data).rename(columns=lambda col: _normalize_header(col))
        except json.JSONDecodeError:
            pass
    return _read_csv_table_from_first_comma_header(text)


def _read_csv_table_from_first_comma_header(text: str) -> pd.DataFrame:
    lines = [line for line in text.splitlines() if line.strip()]
    header_index = next((idx for idx, line in enumerate(lines) if "," in line), None)
    if header_index is None:
        return pd.DataFrame()

    csv_text = "\n".join(lines[header_index:])
    rows = list(csv.reader(io.StringIO(csv_text)))
    if not rows:
        return pd.DataFrame()
    width = len(rows[0])
    clean_rows = [row for row in rows if len(row) == width]
    if len(clean_rows) < 2:
        return pd.DataFrame()
    header = [_normalize_header(col) for col in clean_rows[0]]
    return pd.DataFrame(clean_rows[1:], columns=header)


def normalize_family_table(
    raw: pd.DataFrame,
    contract: FamilyContract,
    *,
    report_date: date,
    source_name: str,
    payload_hash: str,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for _, row in raw.iterrows():
        out: dict[str, Any] = {}
        for canonical in contract.canonical_columns:
            if canonical in {"source", "source_timestamp", "raw_payload_hash"}:
                continue
            out[canonical] = _first_value(row, contract.aliases.get(canonical, [canonical]))

        for column in contract.date_columns:
            parsed = _coerce_date(out.get(column))
            out[column] = parsed.isoformat() if parsed else None
        for column in contract.numeric_columns:
            out[column] = parse_number(out.get(column))

        if "currency" in contract.canonical_columns and not out.get("currency"):
            metric = out.get("metric")
            out["currency"] = str(metric).strip().upper() if metric else None
        out["source"] = source_name
        out["source_timestamp"] = report_date.isoformat()
        out["raw_payload_hash"] = payload_hash
        rows.append(out)

    return pd.DataFrame(rows, columns=contract.canonical_columns)


SYMBOL_ROW_PATTERN = re.compile(
    r"^(?P<symbol>[A-Z0-9]{2,}\.[A-Z0-9]{2,})\s+"
    r"(?P<open>-|[\d,]+(?:\.\d+)?)\s+"
    r"(?P<high>-|[\d,]+(?:\.\d+)?)\s+"
    r"(?P<low>-|[\d,]+(?:\.\d+)?)\s+"
    r"(?P<close>-|[\d,]+(?:\.\d+)?)\s+"
    r"(?P<volume>-|[\d,]+(?:\.\d+)?)"
    r"(?:\s+(?P<turnover>-|[\d,]+(?:\.\d+)?))?"
    r"(?:\s+(?P<trades>-|[\d,]+(?:\.\d+)?))?"
    r"\b"
)


def _read_whitespace_ohlcv_rows(text: str) -> pd.DataFrame:
    rows: list[dict[str, str | None]] = []
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line.strip())
        match = SYMBOL_ROW_PATTERN.match(line)
        if match:
            rows.append(match.groupdict())
    return pd.DataFrame(rows)


def _first_value(row: pd.Series, names: list[str]) -> Any:
    for name in names:
        normalized = _normalize_header(name)
        if normalized in row and not pd.isna(row[normalized]) and str(row[normalized]).strip() != "":
            return row[normalized]
    return None


def _coerce_date(value: Any) -> date | None:
    if value is None or str(value).strip() == "":
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def write_fetch_metadata(fetch: ForwardFetchResult, validation_status: str) -> Path:
    metadata = {
        "requested_date": fetch.requested_date.isoformat(),
        "source_date": fetch.report_date.isoformat() if fetch.report_date else None,
        "report_date": fetch.report_date.isoformat() if fetch.report_date else None,
        "fetch_time_utc": fetch.fetch_time_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "family": fetch.family,
        "source_name": fetch.source_name,
        "source_url": fetch.source_url,
        "payload_hash": fetch.payload_hash,
        "row_count": fetch.row_count,
        "validation_status": validation_status,
        "raw_payload_path": _display_path(fetch.raw_payload_path),
    }
    metadata_path = fetch.raw_payload_path.with_name(
        f"{fetch.fetch_time_utc.strftime('%Y%m%dT%H%M%S%fZ')}_{fetch.payload_hash}.metadata.json"
    )
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
    return metadata_path


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def write_candidate_rows(family: str, target_date: date, source_name: str, records: pd.DataFrame) -> Path:
    candidate_dir = PROCESSED_ROOT / "candidates" / family / target_date.isoformat() / source_name
    candidate_dir.mkdir(parents=True, exist_ok=True)
    path = candidate_dir / f"candidate_{family}.csv"
    records.to_csv(path, index=False)
    return path


def write_accepted_rows(
    family: str,
    target_date: date,
    source_name: str,
    accepted: pd.DataFrame,
    *,
    raw_root: Path = RAW_ROOT,
) -> Path:
    accepted_dir = raw_root / "accepted" / family / target_date.isoformat() / source_name
    accepted_dir.mkdir(parents=True, exist_ok=True)
    path = accepted_dir / f"canonical_{family}.csv"
    accepted.to_csv(path, index=False)
    return path


def write_forward_summary(
    *,
    family: str,
    target_date: date,
    source_name: str,
    status: str,
    failures: list[str],
    warnings: list[str] | None = None,
    accepted_rows: int = 0,
    candidate_rows: int = 0,
) -> Path:
    summary_dir = VALIDATION_ROOT / family / target_date.isoformat() / source_name
    summary_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "family": family,
        "target_date": target_date.isoformat(),
        "source_name": source_name,
        "status": status,
        "candidate_rows": candidate_rows,
        "accepted_rows": accepted_rows,
        "failures": failures,
        "warnings": warnings or [],
    }
    path = summary_dir / "forward_summary.json"
    path.write_text(json.dumps(summary, indent=2) + "\n")
    return path


def write_generic_validation_outputs(result: GenericValidationResult, *, output_dir: Path, source_name: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "quality_summary.json").write_text(json.dumps(result.metrics, indent=2) + "\n")
    result.rejected.to_csv(output_dir / "rejected_records.csv", index=False)
    lines = [
        "# Forward Family Validation Report",
        "",
        f"Family: `{result.metrics.get('family')}`",
        f"Source: `{source_name}`",
        f"Target date: `{result.metrics.get('target_date')}`",
        f"Generated: `{result.metrics.get('generated_at_utc')}`",
        "",
        "## Summary",
        "",
        f"- Rows: {result.metrics.get('row_count', 0):,}",
        f"- Accepted: {result.metrics.get('accepted_rows', 0):,}",
        f"- Rejected: {result.metrics.get('rejected_rows', 0):,}",
        "",
        "## Failures",
        "",
    ]
    lines.extend([f"- {failure}" for failure in result.failures] or ["- none"])
    lines.extend(["", "## Warnings", ""])
    lines.extend([f"- {warning}" for warning in result.warnings] or ["- none"])
    (output_dir / "validation_report.md").write_text("\n".join(lines) + "\n")


def validate_generic_family_records(
    records: pd.DataFrame,
    *,
    family: str,
    target_date: date,
    source_date_failures: list[str] | None = None,
) -> GenericValidationResult:
    if family not in FAMILY_CONTRACTS:
        raise ValueError(f"No forward-ingestion contract is defined for family: {family}")
    contract = FAMILY_CONTRACTS[family]
    failures = list(source_date_failures or [])
    warnings: list[str] = []

    if records.empty:
        failures.append(f"no {family} candidate rows were produced")
        metrics = _generic_metrics(family, target_date, 0, 0, 0, failures, warnings)
        return GenericValidationResult(records.copy(), records.copy(), failures, warnings, metrics)

    df = records.copy()
    for column in contract.canonical_columns:
        if column not in df.columns:
            df[column] = None
    df = df[contract.canonical_columns]
    for column in ("source", "source_timestamp", "raw_payload_hash"):
        if column not in df.columns:
            df[column] = None
    df["source_timestamp"] = pd.to_datetime(df["source_timestamp"], errors="coerce").dt.date
    for column in contract.date_columns:
        df[column] = pd.to_datetime(df[column], errors="coerce").dt.date
    for column in contract.numeric_columns:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    rejection_reasons: list[list[str]] = [[] for _ in range(len(df))]
    for idx, mismatch in enumerate((df["source_timestamp"] != target_date).fillna(True).tolist()):
        if mismatch:
            rejection_reasons[idx].append("source timestamp does not match target date")

    for column in contract.required_columns:
        missing = df[column].isna() | (df[column].astype(str).str.strip() == "")
        for idx, is_missing in enumerate(missing.fillna(True).tolist()):
            if is_missing:
                rejection_reasons[idx].append(f"missing required field: {column}")

    for column in contract.date_columns:
        source_has_value = records[column].notna() if column in records.columns else pd.Series(False, index=df.index)
        invalid = source_has_value & df[column].isna()
        for idx, is_invalid in enumerate(invalid.fillna(False).tolist()):
            if is_invalid:
                rejection_reasons[idx].append(f"invalid date field: {column}")

    for column in contract.numeric_columns:
        source_has_value = records[column].notna() if column in records.columns else pd.Series(False, index=df.index)
        invalid = source_has_value & df[column].isna()
        for idx, is_invalid in enumerate(invalid.fillna(False).tolist()):
            if is_invalid:
                rejection_reasons[idx].append(f"invalid numeric field: {column}")

    for column in contract.nonnegative_columns:
        negative = df[column].notna() & (df[column] < 0)
        for idx, is_negative in enumerate(negative.fillna(False).tolist()):
            if is_negative:
                rejection_reasons[idx].append(f"negative numeric field: {column}")

    for column in ["public_holding_pct", "foreign_holding_pct"]:
        if column in df.columns:
            over_100 = df[column].notna() & (df[column] > 100)
            for idx, is_over in enumerate(over_100.fillna(False).tolist()):
                if is_over:
                    rejection_reasons[idx].append(f"percentage field exceeds 100: {column}")

    for column, allowed in (contract.allowed_values or {}).items():
        values = df[column].astype(str).str.strip().str.lower()
        invalid_allowed = df[column].notna() & ~values.isin(allowed)
        for idx, is_invalid in enumerate(invalid_allowed.fillna(False).tolist()):
            if is_invalid:
                rejection_reasons[idx].append(f"unexpected value for {column}")

    duplicate_subset = [column for column in contract.identity_columns if column in df.columns]
    duplicate_mask = df.duplicated(subset=duplicate_subset, keep=False) if duplicate_subset else df.duplicated(keep=False)
    for idx, duplicate in enumerate(duplicate_mask.tolist()):
        if duplicate:
            rejection_reasons[idx].append("duplicate candidate row")
    if duplicate_mask.any():
        failures.append(f"duplicate {family} candidate rows: {int(duplicate_mask.sum())}")

    rejected_mask = pd.Series([bool(reasons) for reasons in rejection_reasons], index=df.index)
    rejected = df.loc[rejected_mask].copy()
    if not rejected.empty:
        rejected["rejection_reason"] = [
            "; ".join(reason_list) for reason_list in rejection_reasons if reason_list
        ]
    accepted = df.loc[~rejected_mask].copy()
    if rejected_mask.any():
        failures.append(f"rejected {family} rows: {int(rejected_mask.sum())}")

    for column in contract.date_columns:
        if column in accepted.columns:
            accepted[column] = accepted[column].astype(str)
    if "source_timestamp" in accepted.columns:
        accepted["source_timestamp"] = accepted["source_timestamp"].astype(str)
    metrics = _generic_metrics(family, target_date, len(df), len(accepted), len(rejected), failures, warnings)
    return GenericValidationResult(accepted, rejected, failures, warnings, metrics)


def _generic_metrics(
    family: str,
    target_date: date,
    row_count: int,
    accepted_rows: int,
    rejected_rows: int,
    failures: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    return {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "family": family,
        "target_date": target_date.isoformat(),
        "row_count": int(row_count),
        "accepted_rows": int(accepted_rows),
        "rejected_rows": int(rejected_rows),
        "failures": failures,
        "warnings": warnings,
    }


def metadata_symbols(metadata: pd.DataFrame, *, limit: int | None = None) -> list[str]:
    if metadata.empty or "symbol" not in metadata.columns:
        return []
    symbols = sorted(set(metadata["symbol"].dropna().astype(str).str.strip()))
    return symbols[:limit] if limit else symbols


def fetch_cse_trade_summary_cross_check(
    *,
    target_date: date,
    raw_root: Path = RAW_ROOT,
    timeout: int = 15,
) -> tuple[pd.DataFrame, date | None, list[str]]:
    fetch_time_utc = datetime.now(timezone.utc)
    warnings: list[str] = []
    try:
        response = requests.post(CSE_TRADE_SUMMARY_URL, headers=REQUEST_HEADERS, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except (RequestException, json.JSONDecodeError) as exc:
        return pd.DataFrame(), None, [f"CSE same-day cross-check unavailable: {exc}"]

    payload_bytes = _stable_json_bytes(payload)
    digest = sha256_bytes(payload_bytes)
    payload_path = raw_payload_path(raw_root, "ohlcv", target_date, "cse_trade_summary_cross_check", digest, ".json")
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    if not payload_path.exists():
        payload_path.write_bytes(payload_bytes)
    cross_fetch = ForwardFetchResult(
        family="ohlcv",
        source_name="cse_trade_summary_cross_check",
        requested_date=target_date,
        report_date=None,
        fetch_time_utc=fetch_time_utc,
        source_url=CSE_TRADE_SUMMARY_URL,
        payload=payload_bytes,
        text=payload_path.read_text(),
        payload_hash=digest,
        row_count=len(payload.get("reqTradeSummery") or []) if isinstance(payload, dict) else 0,
        raw_payload_path=payload_path,
    )
    write_fetch_metadata(cross_fetch, "cross_check")

    rows: list[dict[str, Any]] = []
    observed_dates: list[date] = []
    payload_rows = payload.get("reqTradeSummery") if isinstance(payload, dict) else []
    if isinstance(payload_rows, list):
        for row in payload_rows:
            if not isinstance(row, dict):
                continue
            symbol = row.get("symbol")
            trades_time = parse_number(row.get("lastTradedTime") or row.get("tradesTime"))
            observed_date = None
            if trades_time is not None:
                observed_date = datetime.fromtimestamp(trades_time / 1000, COLOMBO_TZ).date()
                observed_dates.append(observed_date)
            if not symbol:
                continue
            rows.append(
                {
                    "symbol": str(symbol).strip(),
                    "date": observed_date,
                    "open": parse_number(row.get("open")),
                    "high": parse_number(row.get("high")),
                    "low": parse_number(row.get("low")),
                    "close": parse_number(row.get("closingPrice") or row.get("price") or row.get("lastTradedPrice")),
                    "volume": parse_number(row.get("sharevolume") or row.get("quantity")),
                }
            )
    observed_source_date = max(set(observed_dates), key=observed_dates.count) if observed_dates else None
    if observed_source_date != target_date:
        warnings.append(
            "CSE same-day cross-check skipped: "
            f"official snapshot date {observed_source_date.isoformat() if observed_source_date else 'missing'} "
            f"does not match target {target_date.isoformat()}"
        )
    return pd.DataFrame(rows), observed_source_date, warnings


def cse_cross_check_rejections(
    records: pd.DataFrame,
    cse_snapshot: pd.DataFrame,
    *,
    target_date: date,
    observed_source_date: date | None,
    tolerance: float = 0.01,
) -> pd.Series:
    reasons = pd.Series([""] * len(records), index=records.index)
    if records.empty or cse_snapshot.empty or observed_source_date != target_date:
        return reasons

    snapshot = cse_snapshot.dropna(subset=["symbol"]).drop_duplicates(subset=["symbol"], keep="last")
    by_symbol = snapshot.set_index("symbol")
    for idx, row in records.iterrows():
        symbol = str(row.get("symbol", "")).strip()
        if symbol not in by_symbol.index:
            reasons.at[idx] = "symbol missing from CSE same-day cross-check snapshot"
            continue
        yahoo_close = parse_number(row.get("close"))
        cse_close = parse_number(by_symbol.at[symbol, "close"])
        if yahoo_close is None or cse_close is None:
            reasons.at[idx] = "missing close for CSE same-day cross-check"
            continue
        if abs(yahoo_close - cse_close) > tolerance:
            reasons.at[idx] = (
                "CSE same-day close cross-check mismatch: "
                f"candidate {yahoo_close}, official {cse_close}"
            )
    return reasons


def yahoo_source_rejections(records: pd.DataFrame) -> pd.Series:
    reasons = pd.Series([""] * len(records), index=records.index)
    if records.empty:
        return reasons
    volumes = pd.to_numeric(records.get("volume"), errors="coerce")
    non_positive_volume = volumes.isna() | (volumes <= 0)
    for idx, rejected in enumerate(non_positive_volume.fillna(True).tolist()):
        if rejected:
            reasons.iloc[idx] = "Yahoo candidate has missing or non-positive volume"
    return reasons


def combine_rejection_reasons(*series_items: pd.Series) -> pd.Series:
    if not series_items:
        return pd.Series(dtype=str)
    combined = pd.Series([""] * len(series_items[0]), index=series_items[0].index)
    for series in series_items:
        aligned = series.reindex(combined.index).fillna("").astype(str)
        for idx, reason in aligned.items():
            if reason.strip():
                combined.at[idx] = f"{combined.at[idx]}; {reason}".strip("; ")
    return combined


def run_daily_report_ohlcv_ingestion(
    *,
    target_date: date,
    source_file: Path | None = None,
    source_url_template: str | None = None,
    metadata_path: Path = DEFAULT_METADATA,
    raw_root: Path = RAW_ROOT,
    allow_missing_metadata: bool = False,
    dry_run: bool = False,
    allow_before_close: bool = False,
    missing_activity_threshold: float = 0.0,
) -> GenericValidationResult:
    require_forward_target_date(target_date)
    if not allow_before_close and target_date == datetime.now(COLOMBO_TZ).date() and not is_after_cse_market_close():
        raise ForwardIngestionError("2026-forward daily ingestion must run after CSE market close")

    adapter = CSEDailyReportOHLCVSource(
        source_file=source_file,
        source_url_template=source_url_template or DEFAULT_CSE_DAILY_REPORT_URL_TEMPLATE,
    )
    metadata = pd.DataFrame() if allow_missing_metadata else load_metadata(metadata_path)

    try:
        fetch = adapter.fetch_for_date(target_date, raw_root=raw_root)
        records = adapter.normalize(fetch.text, report_date=fetch.report_date or target_date, payload_hash=fetch.payload_hash)
    except ForwardIngestionError as exc:
        write_forward_summary(
            family="ohlcv",
            target_date=target_date,
            source_name=adapter.source_name,
            status="quarantined",
            failures=[str(exc)],
        )
        raise

    source_date_failures = adapter.validate_source_date(records, target_date, fetch.report_date)
    result = validate_ohlcv_records(
        records,
        target_date=target_date,
        source_date_failures=source_date_failures,
        metadata=metadata,
        allow_missing_metadata=allow_missing_metadata,
        missing_value_threshold=missing_activity_threshold,
    )
    status = "accepted" if result.passed else "quarantined"

    if not dry_run:
        write_fetch_metadata(fetch, status)
        write_candidate_rows("ohlcv", target_date, adapter.source_name, records)
        validation_dir = VALIDATION_ROOT / "ohlcv" / target_date.isoformat() / adapter.source_name
        write_validation_outputs(result, output_dir=validation_dir, source_name=adapter.source_name)
        if result.passed:
            write_accepted_rows("ohlcv", target_date, adapter.source_name, result.accepted, raw_root=raw_root)

    write_forward_summary(
        family="ohlcv",
        target_date=target_date,
        source_name=adapter.source_name,
        status=status,
        failures=result.failures,
        warnings=result.warnings,
        candidate_rows=len(records),
        accepted_rows=len(result.accepted),
    )

    return GenericValidationResult(
        accepted=result.accepted,
        rejected=result.rejected,
        failures=result.failures,
        warnings=result.warnings,
        metrics=result.metrics,
    )


def run_yahoo_ohlcv_ingestion(
    *,
    target_date: date,
    metadata_path: Path = DEFAULT_METADATA,
    raw_root: Path = RAW_ROOT,
    allow_missing_metadata: bool = False,
    dry_run: bool = False,
    allow_before_close: bool = False,
    missing_activity_threshold: float = 1.0,
    yahoo_limit: int | None = None,
    cse_cross_check_tolerance: float = 0.01,
) -> GenericValidationResult:
    require_forward_target_date(target_date)
    if not allow_before_close and target_date == datetime.now(COLOMBO_TZ).date() and not is_after_cse_market_close():
        raise ForwardIngestionError("2026-forward Yahoo ingestion must run after CSE market close")

    metadata = pd.DataFrame() if allow_missing_metadata else load_metadata(metadata_path)
    symbols = metadata_symbols(metadata, limit=yahoo_limit)
    if not symbols and allow_missing_metadata:
        raise MissingSourceError("Yahoo candidate source requires metadata symbols even when metadata validation is disabled")

    adapter = YahooFinanceOHLCVSource(symbols=symbols)
    try:
        fetch = adapter.fetch_for_date(target_date, raw_root=raw_root)
        records = adapter.normalize(fetch.text, report_date=target_date, payload_hash=fetch.payload_hash)
    except ForwardIngestionError as exc:
        write_forward_summary(
            family="ohlcv",
            target_date=target_date,
            source_name=adapter.source_name,
            status="quarantined",
            failures=[str(exc)],
        )
        raise

    source_date_failures = adapter.validate_source_date(records, target_date, fetch.report_date)
    cse_snapshot, cse_snapshot_date, cross_check_warnings = fetch_cse_trade_summary_cross_check(
        target_date=target_date,
        raw_root=raw_root,
    )
    yahoo_rejections = yahoo_source_rejections(records)
    cross_check_rejections = cse_cross_check_rejections(
        records,
        cse_snapshot,
        target_date=target_date,
        observed_source_date=cse_snapshot_date,
        tolerance=cse_cross_check_tolerance,
    )
    extra_rejections = combine_rejection_reasons(yahoo_rejections, cross_check_rejections)
    result = validate_ohlcv_records(
        records,
        target_date=target_date,
        source_date_failures=source_date_failures,
        extra_rejection_reasons=extra_rejections,
        metadata=metadata,
        allow_missing_metadata=allow_missing_metadata,
        missing_value_threshold=missing_activity_threshold,
        required_activity_columns=["volume"],
    )
    warnings = [*result.warnings, *cross_check_warnings]
    returned_symbols = set(records["symbol"].astype(str)) if "symbol" in records.columns else set()
    if len(returned_symbols) < len(symbols):
        missing_count = len(set(symbols) - returned_symbols)
        warnings.append(f"Yahoo candidate missing {missing_count} metadata symbols for {target_date.isoformat()}")
    result.metrics["warnings"] = warnings
    result.metrics["cse_cross_check_date"] = cse_snapshot_date.isoformat() if cse_snapshot_date else None
    result.metrics["cse_cross_check_rows"] = int(len(cse_snapshot))
    result.metrics["cse_cross_check_rejected_rows"] = int((cross_check_rejections != "").sum())
    result.metrics["yahoo_source_rejected_rows"] = int((yahoo_rejections != "").sum())
    result.metrics["metadata_symbols_requested"] = int(len(symbols))
    result.metrics["candidate_symbols_returned"] = int(len(returned_symbols))
    status = "accepted" if result.passed else "quarantined"

    if not dry_run:
        write_fetch_metadata(fetch, status)
        write_candidate_rows("ohlcv", target_date, adapter.source_name, records)
        validation_dir = VALIDATION_ROOT / "ohlcv" / target_date.isoformat() / adapter.source_name
        write_validation_outputs(result, output_dir=validation_dir, source_name=adapter.source_name)
        if result.passed:
            write_accepted_rows("ohlcv", target_date, adapter.source_name, result.accepted, raw_root=raw_root)

    write_forward_summary(
        family="ohlcv",
        target_date=target_date,
        source_name=adapter.source_name,
        status=status,
        failures=result.failures,
        warnings=warnings,
        candidate_rows=len(records),
        accepted_rows=len(result.accepted),
    )

    return GenericValidationResult(
        accepted=result.accepted,
        rejected=result.rejected,
        failures=result.failures,
        warnings=warnings,
        metrics=result.metrics,
    )


def run_generic_family_ingestion(
    *,
    family: str,
    target_date: date,
    source_file: Path | None = None,
    source_url_template: str | None = None,
    raw_root: Path = RAW_ROOT,
    dry_run: bool = False,
    source_name: str | None = None,
) -> GenericValidationResult:
    require_forward_target_date(target_date)
    adapter = DateBearingTableSource(
        family=family,
        source_file=source_file,
        source_url_template=source_url_template,
        source_name=source_name,
    )

    try:
        fetch = adapter.fetch_for_date(target_date, raw_root=raw_root)
        records = adapter.normalize(fetch.text, report_date=fetch.report_date or target_date, payload_hash=fetch.payload_hash)
    except ForwardIngestionError as exc:
        write_forward_summary(
            family=family,
            target_date=target_date,
            source_name=adapter.source_name,
            status="quarantined",
            failures=[str(exc)],
        )
        raise

    source_date_failures = adapter.validate_source_date(records, target_date, fetch.report_date)
    result = validate_generic_family_records(
        records,
        family=family,
        target_date=target_date,
        source_date_failures=source_date_failures,
    )
    status = "accepted" if result.passed else "quarantined"

    if not dry_run:
        write_fetch_metadata(fetch, status)
        write_candidate_rows(family, target_date, adapter.source_name, records)
        validation_dir = VALIDATION_ROOT / family / target_date.isoformat() / adapter.source_name
        write_generic_validation_outputs(result, output_dir=validation_dir, source_name=adapter.source_name)
        if result.passed:
            write_accepted_rows(family, target_date, adapter.source_name, result.accepted, raw_root=raw_root)

    write_forward_summary(
        family=family,
        target_date=target_date,
        source_name=adapter.source_name,
        status=status,
        failures=result.failures,
        warnings=result.warnings,
        candidate_rows=len(records),
        accepted_rows=len(result.accepted),
    )
    return result
