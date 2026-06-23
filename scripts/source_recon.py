"""Non-destructive reconnaissance for 2026-forward CSE data sources.

The recon runner probes official and third-party sources, stores only recon
artifacts, and ranks each source before any ingestion adapter is considered.
It is intentionally separate from the 2026-forward ingestion path.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup
from requests import RequestException


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECON_ROOT = ROOT / "data/recon"
FORWARD_START_DATE = date(2026, 1, 1)
SPOT_CHECK_BOUNDARY = date(2025, 12, 31)
MAX_SAMPLE_BYTES = 512_000
REQUEST_HEADERS = {
    "User-Agent": "cse-dataset-v2-source-recon/0.1 (+https://github.com/nimeshk03/cse-dataset-v2)"
}

FAMILIES = {
    "ohlcv",
    "indices",
    "market_stats",
    "corporate_actions",
    "listings",
    "holdings",
    "sector_gics",
    "news_announcements",
    "macro_rates",
}

ACCEPTED_CANDIDATE = "accepted_candidate"
CROSS_CHECK_ONLY = "cross_check_only"
QUARANTINE = "quarantine"
REJECTED = "rejected"

DATE_PATTERN = re.compile(
    r"\b(?:20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]20\d{2}|"
    r"\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*,?\s+20\d{2})\b",
    re.IGNORECASE,
)
SYMBOL_PATTERN = re.compile(r"\b[A-Z]{2,8}\.[NX]\d{4}\b")
OHLCV_FIELD_ALIASES = {
    "date": {
        "date",
        "trade_date",
        "trading_date",
        "report_date",
        "timestamp",
        "time",
        "tradedate",
        "tradestime",
        "lasttradedtime",
    },
    "symbol": {"symbol", "security", "security_code", "securitycode", "ticker", "code"},
    "open": {"open", "opening", "opening_price"},
    "high": {"high", "highest", "price_high"},
    "low": {"low", "lowest", "price_low"},
    "close": {"close", "closing", "closing_price", "last", "last_traded_price", "lasttradedprice", "price"},
    "volume": {"volume", "share_volume", "sharevolume", "shares_traded", "quantity", "qty"},
}
DATE_FIELD_NAMES = {
    "date",
    "trade_date",
    "trading_date",
    "report_date",
    "published_date",
    "announcement_date",
    "as_of_date",
    "period",
    "timestamp",
    "time",
    "d",
    "tradedate",
    "tradestime",
    "lasttradedtime",
}
REPORT_DATE_PATTERN = re.compile(
    r"\b(?:report|source|as\s+of|trade|trading|market)\s+date\s*[:\-]?\s*"
    r"(?:20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]20\d{2}|"
    r"\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*,?\s+20\d{2})\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class BrowserSelector:
    selector: str
    purpose: str
    required: bool = True


@dataclass(frozen=True)
class SourceProbeSpec:
    source_id: str
    display_name: str
    family: str
    probe_type: str
    url: str
    method: str = "GET"
    params: dict[str, str] = field(default_factory=dict)
    form: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    expected_fields: set[str] = field(default_factory=set)
    supports_cse_hint: bool = False
    historical_2026_hint: bool = False
    current_snapshot: bool = False
    official: bool = False
    requires_key_env: str | None = None
    browser_selectors: tuple[BrowserSelector, ...] = ()
    notes: str = ""


@dataclass
class ParsedEvidence:
    has_cse_coverage: bool = False
    has_2026_dates: bool = False
    has_spot_check_boundary: bool = False
    has_verifiable_dates: bool = False
    has_report_date: bool = False
    has_per_row_dates: bool = False
    observed_dates: list[str] = field(default_factory=list)
    detected_fields: list[str] = field(default_factory=list)
    sample_rows: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    selector_evidence: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class SourceProbeResult:
    source_id: str
    display_name: str
    family: str
    probe_type: str
    url: str
    environment: str
    reachable: bool = False
    status_code: int | None = None
    content_type: str | None = None
    fetched_at_utc: str | None = None
    raw_sample_path: str | None = None
    parsed_sample_path: str | None = None
    screenshot_path: str | None = None
    error: str | None = None
    current_snapshot: bool = False
    official: bool = False
    requires_key_env: str | None = None
    key_available: bool = True
    has_cse_coverage: bool = False
    has_2026_dates: bool = False
    has_spot_check_boundary: bool = False
    has_verifiable_dates: bool = False
    has_report_date: bool = False
    has_per_row_dates: bool = False
    observed_dates: list[str] = field(default_factory=list)
    required_fields_present: bool = False
    detected_fields: list[str] = field(default_factory=list)
    selector_evidence: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    total_score: float = 0.0
    recommendation: str = REJECTED
    recommendation_reason: str = ""


SOURCE_SPECS: tuple[SourceProbeSpec, ...] = (
    SourceProbeSpec(
        source_id="cse_daily_report_cdn_pdf",
        display_name="CSE StockMarketDaily CDN PDF",
        family="ohlcv",
        probe_type="download",
        url="https://cdn.cse.lk/cse-daily/StockMarketDaily%28SMD%29{dd}-{mm}-{yyyy}.pdf",
        expected_fields={"date", "symbol", "open", "high", "low", "close", "volume"},
        supports_cse_hint=True,
        historical_2026_hint=True,
        official=True,
        notes="Official date-bearing report candidate; selector/parser quality decides ingestion readiness.",
    ),
    SourceProbeSpec(
        source_id="cse_trade_summary_current",
        display_name="CSE tradeSummary API",
        family="ohlcv",
        probe_type="api",
        url="https://www.cse.lk/api/tradeSummary",
        method="POST",
        form={"date": "{date}"},
        expected_fields={"symbol", "open", "high", "low", "close", "volume"},
        supports_cse_hint=True,
        current_snapshot=True,
        official=True,
        notes="Known current snapshot; must not be used for historical backfill.",
    ),
    SourceProbeSpec(
        source_id="cse_all_stock_current",
        display_name="CSE allStock API",
        family="ohlcv",
        probe_type="api",
        url="https://www.cse.lk/api/allStock",
        method="POST",
        expected_fields={"symbol", "close"},
        supports_cse_hint=True,
        current_snapshot=True,
        official=True,
    ),
    SourceProbeSpec(
        source_id="cse_today_share_price_current",
        display_name="CSE todaySharePrice API",
        family="ohlcv",
        probe_type="api",
        url="https://www.cse.lk/api/todaySharePrice",
        method="POST",
        expected_fields={"date", "symbol", "open", "high", "low", "close", "volume"},
        supports_cse_hint=True,
        current_snapshot=True,
        official=True,
        notes="Official current trading-day prices; candidate for after-close daily collection only.",
    ),
    SourceProbeSpec(
        source_id="cse_detailed_trades_current",
        display_name="CSE detailedTrades API",
        family="market_stats",
        probe_type="api",
        url="https://www.cse.lk/api/detailedTrades",
        method="POST",
        expected_fields={"symbol", "price", "qty", "trades"},
        supports_cse_hint=True,
        current_snapshot=True,
        official=True,
        notes="Official current trading-day price-bucket trades; cross-check source, not historical OHLCV.",
    ),
    SourceProbeSpec(
        source_id="cse_daily_market_summary_current",
        display_name="CSE dailyMarketSummery API",
        family="market_stats",
        probe_type="api",
        url="https://www.cse.lk/api/dailyMarketSummery",
        method="POST",
        expected_fields={"date", "turnover", "trades"},
        supports_cse_hint=True,
        current_snapshot=True,
        official=True,
        notes="Official recent/current market aggregates; spelling follows the live endpoint.",
    ),
    SourceProbeSpec(
        source_id="cse_chart_data_aspi",
        display_name="CSE chartData ASPI",
        family="indices",
        probe_type="api",
        url="https://www.cse.lk/api/chartData",
        method="POST",
        form={"chartId": "1", "period": "5"},
        expected_fields={"date", "close"},
        supports_cse_hint=True,
        historical_2026_hint=True,
        official=True,
        notes="Trailing-window index API; useful for ASPI validation, not symbol OHLCV.",
    ),
    SourceProbeSpec(
        source_id="cse_announcements_api",
        display_name="CSE announcements API",
        family="news_announcements",
        probe_type="api",
        url="https://www.cse.lk/api/announcement",
        method="POST",
        form={"page": "0", "size": "20"},
        expected_fields={"published_date", "title", "symbol"},
        supports_cse_hint=True,
        historical_2026_hint=True,
        official=True,
    ),
    SourceProbeSpec(
        source_id="tradingview_comb_browser",
        display_name="TradingView COMB.N0000 browser page",
        family="ohlcv",
        probe_type="browser",
        url="https://www.tradingview.com/symbols/CSELK-COMB.N0000/",
        expected_fields={"symbol", "close"},
        supports_cse_hint=True,
        historical_2026_hint=True,
        browser_selectors=(
            BrowserSelector("h1", "symbol heading"),
            BrowserSelector("[data-symbol='CSELK:COMB.N0000'], [data-symbol-short='COMB.N0000']", "symbol marker", False),
        ),
        notes="JS-heavy; may be cross-check only unless automation is reliable and terms risk is reviewed.",
    ),
    SourceProbeSpec(
        source_id="investing_aspi_historical_browser",
        display_name="Investing.com CSE All-Share historical data",
        family="indices",
        probe_type="browser",
        url="https://www.investing.com/indices/cse-all-share-historical-data",
        expected_fields={"date", "close", "open", "high", "low", "volume"},
        supports_cse_hint=True,
        historical_2026_hint=True,
        browser_selectors=(
            BrowserSelector("table", "historical prices table"),
            BrowserSelector("text=CSE All-Share", "market heading", False),
        ),
        notes="Browser-scraped source with visible stale-data risk; use only after screenshot/selector review.",
    ),
    SourceProbeSpec(
        source_id="marketscreener_comb_html",
        display_name="MarketScreener COMB.N0000 page",
        family="ohlcv",
        probe_type="html",
        url="https://www.marketscreener.com/quote/stock/COMMERCIAL-BANK-OF-CEYLON-20701211/",
        expected_fields={"symbol", "close"},
        supports_cse_hint=True,
        historical_2026_hint=False,
    ),
    SourceProbeSpec(
        source_id="yahoo_comb_chart",
        display_name="Yahoo Finance COMB-N0000.CM chart API",
        family="ohlcv",
        probe_type="api",
        url="https://query1.finance.yahoo.com/v8/finance/chart/COMB-N0000.CM",
        params={"period1": "1767139200", "period2": "1780185600", "interval": "1d"},
        expected_fields={"date", "open", "high", "low", "close", "volume"},
        supports_cse_hint=True,
        historical_2026_hint=True,
        notes="Third-party Yahoo Finance CSE chart endpoint; requires symbol coverage and cross-source validation before ingestion.",
    ),
    SourceProbeSpec(
        source_id="stooq_comb_csv",
        display_name="Stooq COMB daily CSV probe",
        family="ohlcv",
        probe_type="download",
        url="https://stooq.com/q/d/l/",
        params={"s": "comb.lk", "i": "d", "d1": "20260101"},
        expected_fields={"date", "open", "high", "low", "close", "volume"},
        historical_2026_hint=True,
    ),
    SourceProbeSpec(
        source_id="alpha_vantage_symbol_search",
        display_name="Alpha Vantage symbol search",
        family="ohlcv",
        probe_type="api",
        url="https://www.alphavantage.co/query",
        params={"function": "SYMBOL_SEARCH", "keywords": "COMB Colombo", "apikey": "{ALPHAVANTAGE_API_KEY}"},
        expected_fields={"symbol"},
        requires_key_env="ALPHAVANTAGE_API_KEY",
    ),
    SourceProbeSpec(
        source_id="finnhub_exchange_symbols",
        display_name="Finnhub Sri Lanka exchange symbols",
        family="listings",
        probe_type="api",
        url="https://finnhub.io/api/v1/stock/symbol",
        params={"exchange": "LK", "token": "{FINNHUB_API_KEY}"},
        expected_fields={"symbol"},
        requires_key_env="FINNHUB_API_KEY",
    ),
    SourceProbeSpec(
        source_id="eodhd_comb_demo",
        display_name="EODHD COMB.CSE demo EOD API",
        family="ohlcv",
        probe_type="api",
        url="https://eodhd.com/api/eod/COMB.CSE",
        params={"from": "2026-01-01", "api_token": "demo", "fmt": "json"},
        expected_fields={"date", "open", "high", "low", "close", "volume"},
        historical_2026_hint=True,
    ),
    SourceProbeSpec(
        source_id="nasdaq_data_link_search",
        display_name="Nasdaq Data Link CSE search",
        family="ohlcv",
        probe_type="api",
        url="https://data.nasdaq.com/api/v3/datasets.json",
        params={"query": "Colombo Stock Exchange COMB"},
        expected_fields={"date", "symbol"},
    ),
    SourceProbeSpec(
        source_id="lbo_wordpress_posts",
        display_name="LBO WordPress posts API",
        family="news_announcements",
        probe_type="api",
        url="https://www.lankabusinessonline.com/wp-json/wp/v2/posts",
        params={"per_page": "5", "orderby": "date"},
        expected_fields={"published_date", "title", "url"},
        historical_2026_hint=True,
    ),
    SourceProbeSpec(
        source_id="dailyft_business_html",
        display_name="Daily FT business page",
        family="news_announcements",
        probe_type="html",
        url="https://www.ft.lk/business",
        expected_fields={"published_date", "title", "url"},
        historical_2026_hint=True,
    ),
    SourceProbeSpec(
        source_id="daily_mirror_business_html",
        display_name="Daily Mirror business page",
        family="news_announcements",
        probe_type="html",
        url="https://www.dailymirror.lk/business",
        expected_fields={"published_date", "title", "url"},
        historical_2026_hint=True,
    ),
    SourceProbeSpec(
        source_id="cbsl_exchange_rates_html",
        display_name="CBSL exchange rates page",
        family="macro_rates",
        probe_type="html",
        url="https://www.cbsl.gov.lk/en/rates-and-indicators/exchange-rates",
        expected_fields={"date", "rate"},
        supports_cse_hint=True,
        historical_2026_hint=True,
        official=True,
    ),
    SourceProbeSpec(
        source_id="world_bank_lka_interest_rate",
        display_name="World Bank Sri Lanka real interest rate API",
        family="macro_rates",
        probe_type="api",
        url="https://api.worldbank.org/v2/country/LKA/indicator/FR.INR.RINR",
        params={"format": "json", "per_page": "5"},
        expected_fields={"date", "value"},
        supports_cse_hint=True,
        official=True,
    ),
)


def format_template(text: str, target_date: date) -> str:
    values = {
        "date": target_date.isoformat(),
        "yyyy": target_date.strftime("%Y"),
        "mm": target_date.strftime("%m"),
        "dd": target_date.strftime("%d"),
        "ALPHAVANTAGE_API_KEY": os.environ.get("ALPHAVANTAGE_API_KEY", "demo"),
        "FINNHUB_API_KEY": os.environ.get("FINNHUB_API_KEY", "demo"),
    }
    return text.format(**values)


def recon_environment() -> str:
    return "github_actions" if os.environ.get("GITHUB_ACTIONS") == "true" else "local"


def run_id(now: datetime | None = None) -> str:
    current = now or datetime.now(UTC)
    return current.strftime("%Y%m%dT%H%M%SZ")


def safe_name(source_id: str, suffix: str) -> str:
    return f"{source_id}{suffix}"


def write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload[:MAX_SAMPLE_BYTES])


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")


def artifact_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def normalized_field_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def flatten_json(value: Any, rows: list[dict[str, Any]], prefix: str = "") -> None:
    if isinstance(value, list):
        for item in value:
            flatten_json(item, rows, prefix)
        return
    if not isinstance(value, dict):
        return

    flat: dict[str, Any] = {}
    nested: list[Any] = []
    for key, item in value.items():
        name = normalized_field_name(key)
        if isinstance(item, dict):
            for nested_key, nested_item in item.items():
                if not isinstance(nested_item, (dict, list)):
                    flat[normalized_field_name(f"{name}_{nested_key}")] = nested_item
                else:
                    nested.append(nested_item)
        elif isinstance(item, list):
            nested.append(item)
        else:
            flat[name] = item
    if flat:
        rows.append(flat)
    for item in nested:
        flatten_json(item, rows, prefix)


def infer_rows_from_json(payload: Any) -> list[dict[str, Any]]:
    yahoo_rows = infer_rows_from_yahoo_chart(payload)
    if yahoo_rows:
        return yahoo_rows[:25]
    rows: list[dict[str, Any]] = []
    flatten_json(payload, rows)
    scored = sorted(rows, key=lambda row: len(set(row) & (set().union(*OHLCV_FIELD_ALIASES.values()) | DATE_FIELD_NAMES)), reverse=True)
    return scored[:25]


def infer_rows_from_yahoo_chart(payload: Any) -> list[dict[str, Any]]:
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
    timezone_offset = int(meta.get("gmtoffset") or 0)
    exchange_tz = timezone_offset
    symbol = meta.get("symbol") or ""
    rows: list[dict[str, Any]] = []
    for index, timestamp in enumerate(timestamps):
        try:
            trade_date = datetime.fromtimestamp(
                int(timestamp),
                timezone_from_offset(exchange_tz),
            ).date().isoformat()
        except (TypeError, ValueError, OSError):
            continue
        row: dict[str, Any] = {"date": trade_date, "symbol": symbol}
        for field_name in ("open", "high", "low", "close", "volume"):
            values = quote.get(field_name)
            if isinstance(values, list) and index < len(values):
                row[field_name] = values[index]
        if all(row.get(field_name) is None for field_name in ("open", "high", "low", "close", "volume")):
            continue
        rows.append(row)
    return rows


def timezone_from_offset(offset_seconds: int):
    from datetime import timezone, timedelta

    return timezone(timedelta(seconds=offset_seconds))


def infer_rows_from_csv(text: str) -> list[dict[str, Any]]:
    sample = text[:MAX_SAMPLE_BYTES].splitlines()
    if not sample:
        return []
    dialect = csv.Sniffer().sniff("\n".join(sample[:20]), delimiters=",;\t|")
    reader = csv.DictReader(sample, dialect=dialect)
    rows = []
    for row in reader:
        rows.append({normalized_field_name(key): value for key, value in row.items() if key})
        if len(rows) >= 25:
            break
    return rows


def infer_rows_from_html(text: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(text, "html.parser")
    rows: list[dict[str, Any]] = []
    for table in soup.find_all("table")[:5]:
        headers = [normalized_field_name(cell.get_text(" ", strip=True)) for cell in table.find_all("th")]
        if not headers:
            first_row = table.find("tr")
            if first_row:
                headers = [normalized_field_name(cell.get_text(" ", strip=True)) for cell in first_row.find_all(["td", "th"])]
        for tr in table.find_all("tr")[1:]:
            cells = [cell.get_text(" ", strip=True) for cell in tr.find_all(["td", "th"])]
            if headers and len(cells) >= len(headers):
                rows.append(dict(zip(headers, cells, strict=False)))
            if len(rows) >= 25:
                return rows
    if not rows:
        titles = [node.get_text(" ", strip=True) for node in soup.find_all(["h1", "h2", "h3", "a"])[:25]]
        rows = [{"title": title} for title in titles if title]
    return rows[:25]


def parse_dates_from_rows(rows: list[dict[str, Any]]) -> tuple[bool, bool, bool, list[str]]:
    has_date = False
    has_2026 = False
    has_boundary = False
    observed_dates: list[str] = []
    for row in rows:
        for key, value in row.items():
            key_name = normalized_field_name(key)
            value_text = str(value)
            epoch_ms_date = False
            normalized_date = value_text.strip()
            if key_name == "d" or key_name in DATE_FIELD_NAMES:
                try:
                    timestamp_ms = int(float(value_text))
                    epoch_ms_date = timestamp_ms >= 1_451_606_400_000
                    if epoch_ms_date:
                        normalized_date = datetime.fromtimestamp(timestamp_ms / 1000, UTC).date().isoformat()
                except ValueError:
                    epoch_ms_date = False
            if key_name in DATE_FIELD_NAMES or DATE_PATTERN.search(value_text) or epoch_ms_date:
                has_date = True
                observed_dates.append(normalized_date)
                text = normalized_date
                if "2026" in text:
                    has_2026 = True
                if SPOT_CHECK_BOUNDARY.isoformat() in text or "31/12/2025" in text or "31-Dec-2025" in text:
                    has_boundary = True
    return has_date, has_2026, has_boundary, sorted(set(observed_dates))[:20]


def detect_fields(rows: list[dict[str, Any]], text: str) -> list[str]:
    detected: set[str] = set()
    keys = {normalized_field_name(key) for row in rows for key in row}
    text_lower = text.lower()
    for canonical, aliases in OHLCV_FIELD_ALIASES.items():
        if keys & aliases or any(re.search(rf"\b{re.escape(alias)}\b", text_lower) for alias in aliases):
            detected.add(canonical)
    if "d" in keys:
        detected.add("date")
    if "v" in keys:
        detected.add("close")
    if "rate" in keys or "exchange rate" in text_lower:
        detected.add("rate")
    if "turnover" in keys or "marketturnover" in keys:
        detected.add("turnover")
    if "trades" in keys or "markettrades" in keys or "tradevolume" in keys:
        detected.add("trades")
    if "qty" in keys or "quantity" in keys:
        detected.add("qty")
    if "price" in keys:
        detected.add("price")
    if "title" in keys or "headline" in keys:
        detected.add("title")
    if "value" in keys:
        detected.add("value")
    return sorted(detected)


def parse_response_evidence(spec: SourceProbeSpec, content_type: str, payload: bytes) -> ParsedEvidence:
    text = payload[:MAX_SAMPLE_BYTES].decode("utf-8", errors="replace")
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []

    if "json" in content_type or text.lstrip().startswith(("{", "[")):
        try:
            rows = infer_rows_from_json(json.loads(text))
        except json.JSONDecodeError as exc:
            warnings.append(f"json parse failed: {exc}")
    first_line = text.splitlines()[0] if text.splitlines() else ""
    elif_csv = "csv" in content_type or spec.url.lower().endswith(".csv") or "," in first_line
    if not rows and elif_csv:
        try:
            rows = infer_rows_from_csv(text)
        except csv.Error as exc:
            warnings.append(f"csv parse failed: {exc}")
    if not rows and ("html" in content_type or "<html" in text.lower() or "<table" in text.lower()):
        rows = infer_rows_from_html(text)

    detected_fields = detect_fields(rows, text)
    row_dates, row_2026, row_boundary, observed_dates = parse_dates_from_rows(rows)
    text_dates = DATE_PATTERN.findall(text)
    has_2026 = row_2026 or any("2026" in match for match in text_dates)
    has_boundary = row_boundary or any(
        marker in text for marker in (SPOT_CHECK_BOUNDARY.isoformat(), "31/12/2025", "31-Dec-2025")
    )
    has_report_date = bool(REPORT_DATE_PATTERN.search(text))
    has_verifiable_dates = row_dates or has_report_date
    has_cse_coverage = (
        spec.supports_cse_hint
        or bool(SYMBOL_PATTERN.search(text))
        or "colombo stock exchange" in text.lower()
        or "sri lanka" in text.lower()
        or "cselk" in text.lower()
    )
    return ParsedEvidence(
        has_cse_coverage=has_cse_coverage,
        has_2026_dates=has_2026,
        has_spot_check_boundary=has_boundary,
        has_verifiable_dates=has_verifiable_dates,
        has_report_date=has_report_date,
        has_per_row_dates=row_dates,
        observed_dates=sorted(set(observed_dates + text_dates))[:20],
        detected_fields=detected_fields,
        sample_rows=rows[:10],
        row_count=len(rows),
        warnings=warnings,
    )


def required_fields_present(spec: SourceProbeSpec, detected_fields: list[str]) -> bool:
    required = set(spec.expected_fields)
    if not required:
        return False
    detected = set(detected_fields)
    if spec.family == "ohlcv":
        return {"date", "symbol", "open", "high", "low", "close", "volume"}.issubset(detected)
    return bool(required & detected) or required.issubset(detected)


def score_result(result: SourceProbeResult) -> SourceProbeResult:
    coverage = 2.0 if result.has_cse_coverage and result.has_2026_dates else 1.0 if result.has_cse_coverage else 0.0
    date_verifiability = 2.0 if result.has_per_row_dates else 1.4 if result.has_verifiable_dates else 0.0
    fields = 2.0 if result.required_fields_present else 0.6 if result.detected_fields else 0.0
    cross_source = 1.0 if result.has_spot_check_boundary else 0.0
    if result.current_snapshot:
        automation = 1.0
    elif result.probe_type in {"api", "download"}:
        automation = 2.0
    elif result.probe_type == "html":
        automation = 1.2
    elif result.probe_type == "browser":
        automation = 0.8 if result.screenshot_path else 0.4
    else:
        automation = 0.5
    risk = 2.0 if result.official else 0.7 if result.probe_type == "browser" else 1.2
    effort = 2.0 if result.probe_type in {"api", "download"} else 1.2 if result.probe_type == "html" else 0.7

    if not result.reachable:
        coverage = date_verifiability = fields = cross_source = automation = 0.0
        effort = min(effort, 0.5)

    result.scores = {
        "data_coverage": coverage,
        "date_verifiability": date_verifiability,
        "required_fields": fields,
        "cross_source_agreement": cross_source,
        "automation_stability": automation,
        "terms_rate_limit_risk": risk,
        "implementation_effort": effort,
    }
    result.total_score = round(sum(result.scores.values()), 2)
    result.recommendation, result.recommendation_reason = classify_result(result)
    return result


def classify_result(result: SourceProbeResult) -> tuple[str, str]:
    if result.requires_key_env and not result.key_available:
        return REJECTED, f"requires unset API key {result.requires_key_env}"
    if not result.reachable:
        return REJECTED, result.error or "source was not reachable"
    if result.status_code and result.status_code >= 400:
        return REJECTED, f"HTTP status {result.status_code}"
    if not result.has_cse_coverage and result.family != "macro_rates":
        return REJECTED, "no observed CSE or Sri Lanka coverage"
    if result.current_snapshot:
        return QUARANTINE, "current snapshot only; cannot fill historical 2026 gap"
    if result.family == "ohlcv":
        if not (result.has_per_row_dates or result.has_report_date):
            return QUARANTINE, "OHLCV candidate lacks verifiable row/report dates"
        if not result.required_fields_present:
            return CROSS_CHECK_ONLY, "date-bearing but missing full symbol/OHLCV evidence"
        if result.probe_type == "browser" and result.scores.get("automation_stability", 0) < 0.8:
            return QUARANTINE, "browser automation did not produce stable selector/screenshot evidence"
        return ACCEPTED_CANDIDATE, "date-bearing OHLCV source worth an ingestion adapter"
    if result.family in {"indices", "macro_rates", "news_announcements"} and result.has_verifiable_dates:
        return ACCEPTED_CANDIDATE, "date-bearing source for non-OHLCV family"
    if result.detected_fields:
        return CROSS_CHECK_ONLY, "useful fields observed but not enough for primary ingestion"
    return QUARANTINE, "reachable but unverifiable or structurally unclear"


def apply_cross_source_scores(results: list[SourceProbeResult]) -> None:
    accepted_or_dated = [
        item
        for item in results
        if item.reachable and item.has_verifiable_dates and (item.has_spot_check_boundary or item.has_2026_dates)
    ]
    families_with_checks = {item.family for item in accepted_or_dated}
    for item in results:
        if item.family in families_with_checks and item.has_verifiable_dates and item.has_2026_dates:
            item.scores["cross_source_agreement"] = max(item.scores.get("cross_source_agreement", 0.0), 0.6)
            if item.has_spot_check_boundary:
                item.scores["cross_source_agreement"] = 1.5
            item.total_score = round(sum(item.scores.values()), 2)
            item.recommendation, item.recommendation_reason = classify_result(item)


def run_http_probe(spec: SourceProbeSpec, run_dir: Path, target_date: date, timeout: int) -> SourceProbeResult:
    fetched_at = datetime.now(UTC).isoformat()
    result = SourceProbeResult(
        source_id=spec.source_id,
        display_name=spec.display_name,
        family=spec.family,
        probe_type=spec.probe_type,
        url=format_template(spec.url, target_date),
        environment=recon_environment(),
        fetched_at_utc=fetched_at,
        current_snapshot=spec.current_snapshot,
        official=spec.official,
        requires_key_env=spec.requires_key_env,
        key_available=not spec.requires_key_env or bool(os.environ.get(spec.requires_key_env)),
    )
    if spec.requires_key_env and not result.key_available:
        result.error = f"environment variable {spec.requires_key_env} is not set"
        return score_result(result)

    params = {key: format_template(value, target_date) for key, value in spec.params.items()}
    form = {key: format_template(value, target_date) for key, value in spec.form.items()}
    headers = {**REQUEST_HEADERS, **spec.headers}
    url = result.url
    if params:
        result.url = f"{url}?{urlencode(params)}"
    try:
        response = requests.request(
            spec.method,
            url,
            params=params or None,
            files={key: (None, value) for key, value in form.items()} if form else None,
            headers=headers,
            timeout=timeout,
        )
        result.reachable = True
        result.status_code = response.status_code
        result.content_type = response.headers.get("content-type", "")
        raw_path = run_dir / "raw" / safe_name(spec.source_id, response_suffix(result.content_type, url))
        write_bytes(raw_path, response.content)
        result.raw_sample_path = artifact_path(raw_path)
        if response.status_code < 400:
            evidence = parse_response_evidence(spec, result.content_type or "", response.content)
            attach_evidence(result, spec, evidence)
            parsed_path = run_dir / "parsed" / f"{spec.source_id}.json"
            write_json(parsed_path, {"source_id": spec.source_id, "sample_rows": evidence.sample_rows, "evidence": asdict(evidence)})
            result.parsed_sample_path = artifact_path(parsed_path)
    except RequestException as exc:
        result.error = str(exc)
    return score_result(result)


def response_suffix(content_type: str | None, url: str) -> str:
    lowered = (content_type or "").lower()
    if "json" in lowered:
        return ".json"
    if "html" in lowered:
        return ".html"
    if "csv" in lowered:
        return ".csv"
    if "pdf" in lowered or url.lower().endswith(".pdf"):
        return ".pdf"
    if "excel" in lowered or url.lower().endswith((".xls", ".xlsx")):
        return Path(url).suffix
    return ".bin"


def attach_evidence(result: SourceProbeResult, spec: SourceProbeSpec, evidence: ParsedEvidence) -> None:
    result.has_cse_coverage = evidence.has_cse_coverage
    result.has_2026_dates = evidence.has_2026_dates
    result.has_spot_check_boundary = evidence.has_spot_check_boundary
    result.has_verifiable_dates = evidence.has_verifiable_dates
    result.has_report_date = evidence.has_report_date
    result.has_per_row_dates = evidence.has_per_row_dates
    result.observed_dates = evidence.observed_dates
    result.detected_fields = evidence.detected_fields
    result.required_fields_present = required_fields_present(spec, evidence.detected_fields)
    result.selector_evidence = evidence.selector_evidence
    result.warnings.extend(evidence.warnings)


def run_browser_probe(spec: SourceProbeSpec, run_dir: Path, target_date: date, timeout: int) -> SourceProbeResult:
    fetched_at = datetime.now(UTC).isoformat()
    url = format_template(spec.url, target_date)
    result = SourceProbeResult(
        source_id=spec.source_id,
        display_name=spec.display_name,
        family=spec.family,
        probe_type=spec.probe_type,
        url=url,
        environment=recon_environment(),
        fetched_at_utc=fetched_at,
        current_snapshot=spec.current_snapshot,
        official=spec.official,
    )
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        result.error = f"playwright import failed: {exc}"
        return score_result(result)

    screenshot_path = run_dir / "screenshots" / f"{spec.source_id}.png"
    html_path = run_dir / "raw" / f"{spec.source_id}.html"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1366, "height": 900})
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            result.reachable = response is not None
            result.status_code = response.status if response else None
            result.content_type = response.headers.get("content-type", "") if response else "text/html"
            page.wait_for_timeout(1500)
            html = page.content()
            write_bytes(html_path, html.encode("utf-8", errors="replace"))
            result.raw_sample_path = artifact_path(html_path)
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot_path), full_page=True)
            result.screenshot_path = artifact_path(screenshot_path)
            selector_evidence: dict[str, str] = {}
            selector_errors: list[str] = []
            for selector in spec.browser_selectors:
                try:
                    locator = page.locator(selector.selector).first
                    count = page.locator(selector.selector).count()
                    text = locator.inner_text(timeout=1500)[:200] if count else ""
                    selector_evidence[selector.selector] = text or f"matched {count} nodes"
                    if selector.required and count == 0:
                        selector_errors.append(f"required selector missing: {selector.selector}")
                except (PlaywrightError, PlaywrightTimeoutError) as exc:
                    selector_evidence[selector.selector] = f"selector failed: {exc}"
                    if selector.required:
                        selector_errors.append(f"required selector failed: {selector.selector}")
            evidence = parse_response_evidence(spec, "text/html", html.encode("utf-8", errors="replace"))
            evidence.selector_evidence = selector_evidence
            evidence.has_cse_coverage = evidence.has_cse_coverage or spec.supports_cse_hint
            attach_evidence(result, spec, evidence)
            if selector_errors:
                result.error = "; ".join(selector_errors)
                result.warnings.extend(selector_errors)
            parsed_path = run_dir / "parsed" / f"{spec.source_id}.json"
            write_json(parsed_path, {"source_id": spec.source_id, "sample_rows": evidence.sample_rows, "evidence": asdict(evidence)})
            result.parsed_sample_path = artifact_path(parsed_path)
            browser.close()
    except Exception as exc:
        result.error = str(exc)
        failure_path = write_browser_failure_artifacts(
            run_dir=run_dir,
            source_id=spec.source_id,
            url=url,
            selectors=[asdict(selector) for selector in spec.browser_selectors],
            error=str(exc),
        )
        result.parsed_sample_path = artifact_path(failure_path)
    return score_result(result)


MINIMAL_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff"
    b"\xff?\x00\x05\xfe\x02\xfeA\x0f\xb2\x9d\x00\x00\x00\x00IEND\xaeB`\x82"
)


def write_browser_failure_artifacts(
    *,
    run_dir: Path,
    source_id: str,
    url: str,
    selectors: list[dict[str, Any]],
    error: str,
    screenshot_bytes: bytes | None = None,
) -> Path:
    screenshots = run_dir / "screenshots"
    parsed = run_dir / "parsed"
    screenshots.mkdir(parents=True, exist_ok=True)
    parsed.mkdir(parents=True, exist_ok=True)
    screenshot_path = screenshots / f"{source_id}_failure.png"
    if screenshot_bytes is not None:
        screenshot_path.write_bytes(screenshot_bytes)
    failure_path = parsed / f"{source_id}_browser_failure.json"
    write_json(
        failure_path,
        {
            "source_id": source_id,
            "url": url,
            "selectors": selectors,
            "error": error,
            "screenshot_path": artifact_path(screenshot_path) if screenshot_path.exists() else None,
            "structured_failure": True,
        },
    )
    return failure_path


def run_source_probe(
    spec: SourceProbeSpec,
    run_dir: Path,
    target_date: date,
    timeout: int,
    skip_browser: bool = False,
) -> SourceProbeResult:
    if spec.probe_type == "browser":
        if skip_browser:
            result = SourceProbeResult(
                source_id=spec.source_id,
                display_name=spec.display_name,
                family=spec.family,
                probe_type=spec.probe_type,
                url=format_template(spec.url, target_date),
                environment=recon_environment(),
                fetched_at_utc=datetime.now(UTC).isoformat(),
                error="browser probes skipped by CLI",
            )
            return score_result(result)
        return run_browser_probe(spec, run_dir, target_date, timeout)
    return run_http_probe(spec, run_dir, target_date, timeout)


def write_matrix(results: list[SourceProbeResult], run_dir: Path) -> None:
    metadata_dir = run_dir / "metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    json_path = metadata_dir / "ranked_source_matrix.json"
    csv_path = metadata_dir / "ranked_source_matrix.csv"
    ranked = sorted(results, key=lambda item: item.total_score, reverse=True)
    write_json(json_path, [asdict(item) for item in ranked])
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "rank",
                "source_id",
                "display_name",
                "family",
                "probe_type",
                "recommendation",
                "total_score",
                "reachable",
                "status_code",
                "has_cse_coverage",
                "has_2026_dates",
                "has_verifiable_dates",
                "has_report_date",
                "has_per_row_dates",
                "observed_dates",
                "required_fields_present",
                "current_snapshot",
                "recommendation_reason",
                "url",
            ],
        )
        writer.writeheader()
        for index, item in enumerate(ranked, start=1):
            writer.writerow(
                {
                    "rank": index,
                    "source_id": item.source_id,
                    "display_name": item.display_name,
                    "family": item.family,
                    "probe_type": item.probe_type,
                    "recommendation": item.recommendation,
                    "total_score": item.total_score,
                    "reachable": item.reachable,
                    "status_code": item.status_code,
                    "has_cse_coverage": item.has_cse_coverage,
                    "has_2026_dates": item.has_2026_dates,
                    "has_verifiable_dates": item.has_verifiable_dates,
                    "has_report_date": item.has_report_date,
                    "has_per_row_dates": item.has_per_row_dates,
                    "observed_dates": ";".join(item.observed_dates),
                    "required_fields_present": item.required_fields_present,
                    "current_snapshot": item.current_snapshot,
                    "recommendation_reason": item.recommendation_reason,
                    "url": item.url,
                }
            )
    write_report(ranked, run_dir)


def write_report(ranked: list[SourceProbeResult], run_dir: Path) -> None:
    report_path = run_dir / "ranked_source_report.md"
    lines = [
        "# CSE Source Recon Ranked Report",
        "",
        f"Generated: {datetime.now(UTC).isoformat()}",
        f"Environment: {recon_environment()}",
        f"Forward gap under review: {FORWARD_START_DATE.isoformat()} to current date",
        "",
        "| Rank | Source | Family | Probe | Status | Score | Evidence | Observed dates | Reason |",
        "|---:|---|---|---|---|---:|---|---|---|",
    ]
    for index, item in enumerate(ranked, start=1):
        evidence = ", ".join(
            label
            for label, enabled in [
                ("CSE", item.has_cse_coverage),
                ("2026", item.has_2026_dates),
                ("dates", item.has_verifiable_dates),
                ("report date", item.has_report_date),
                ("row dates", item.has_per_row_dates),
                ("fields", item.required_fields_present),
                ("screenshot", bool(item.screenshot_path)),
            ]
            if enabled
        ) or "none"
        lines.append(
            "| "
            f"{index} | `{item.source_id}` | {item.family} | {item.probe_type} | "
            f"{item.recommendation} | {item.total_score:.2f} | {evidence} | "
            f"{', '.join(item.observed_dates[:5]) or 'none'} | "
            f"{item.recommendation_reason.replace('|', '/')} |"
        )
    lines.extend(
        [
            "",
            "## Guardrails",
            "",
            "- OHLCV sources are not accepted unless row-level or report-level dates are visible.",
            "- Current snapshot APIs are quarantined for historical backfill even when they are official.",
            "- Browser-scraped sources require screenshots and selector evidence before they can be recommended.",
            "- A 2026 OHLCV adapter still needs cross-source validation near 2025-12-31 before ingestion.",
            "",
        ]
    )
    report_path.write_text("\n".join(lines))


def run_recon(
    *,
    output_root: Path = DEFAULT_RECON_ROOT,
    target_date: date | None = None,
    source_ids: set[str] | None = None,
    skip_browser: bool = False,
    timeout: int = 20,
    sleep_seconds: float = 0.25,
) -> Path:
    target = target_date or datetime.now(UTC).date()
    run_dir = output_root / run_id()
    run_dir.mkdir(parents=True, exist_ok=True)
    specs = [spec for spec in SOURCE_SPECS if source_ids is None or spec.source_id in source_ids]
    results = []
    for spec in specs:
        result = run_source_probe(spec, run_dir, target, timeout, skip_browser=skip_browser)
        results.append(result)
        time.sleep(sleep_seconds)
    apply_cross_source_scores(results)
    write_matrix(results, run_dir)
    return run_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe and rank candidate CSE data sources without ingestion.")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_RECON_ROOT)
    parser.add_argument("--target-date", type=lambda value: datetime.strptime(value, "%Y-%m-%d").date())
    parser.add_argument("--source", action="append", dest="sources", help="Source id to probe; repeatable.")
    parser.add_argument("--skip-browser", action="store_true", help="Skip Playwright probes.")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--sleep-seconds", type=float, default=0.25)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_dir = run_recon(
        output_root=args.output_root,
        target_date=args.target_date,
        source_ids=set(args.sources) if args.sources else None,
        skip_browser=args.skip_browser,
        timeout=args.timeout,
        sleep_seconds=args.sleep_seconds,
    )
    print(f"Recon artifacts written to {run_dir}")


if __name__ == "__main__":
    main()
