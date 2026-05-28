"""
Phase 0 Reconnaissance Script
==============================
Validates all data sources and API endpoints for the CSE ML dataset project.
Run from the project root with the virtual environment activated:

    python scripts/00_recon.py

Outputs:
    docs/api_endpoints.md       - CSE API endpoint probe results
    docs/data_sources.md        - Full data source availability summary
    docs/ticker_mapping.csv     - yfinance coverage per CSE ticker
    data/raw/legacy/aspi_combined.csv - Merged ASPI history (Kaggle + CSE API)
"""

import os
import re
import sys
import time
import json
import datetime
import logging

import requests
import pandas as pd
import feedparser
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
TIMEOUT = 12
RESULTS = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get(url, params=None, timeout=TIMEOUT):
    return requests.get(url, params=params, headers=HEADERS, timeout=timeout)


def post_form(url, fields: dict, timeout=TIMEOUT):
    files = {k: (None, v) for k, v in fields.items()}
    return requests.post(url, files=files, headers=HEADERS, timeout=timeout)


def _section(title: str):
    log.info("=" * 60)
    log.info(title)
    log.info("=" * 60)


# ---------------------------------------------------------------------------
# 1. Network connectivity
# ---------------------------------------------------------------------------

def probe_network():
    _section("1. Network Connectivity")
    sites = {
        "cse.lk":           "https://www.cse.lk/api/marketStatus",
        "yahoo_finance":    "https://query1.finance.yahoo.com/v8/finance/chart/AAPL",
        "web.archive.org":  "https://web.archive.org/cdx/search/cdx?url=example.com&limit=1&output=json",
        "investing.com":    "https://www.investing.com",
        "lbo.lk":           "https://www.lankabusinessonline.com/",
        "ft.lk":            "https://www.ft.lk",
        "dailymirror.lk":   "https://www.dailymirror.lk",
        "alphavantage.co":  "https://www.alphavantage.co",
        "finnhub.io":       "https://finnhub.io",
    }
    results = {}
    for name, url in sites.items():
        try:
            r = requests.get(url, headers=HEADERS, timeout=6)
            results[name] = {"status": r.status_code, "reachable": True}
            log.info("  OK  [%d] %s", r.status_code, name)
        except requests.exceptions.Timeout:
            results[name] = {"status": None, "reachable": False, "error": "TIMEOUT"}
            log.warning("  TIMEOUT  %s", name)
        except requests.exceptions.ConnectionError as exc:
            results[name] = {"status": None, "reachable": False, "error": str(exc)[:80]}
            log.warning("  CONN_ERR %s", name)
        time.sleep(0.3)
    RESULTS["network"] = results
    return results


# ---------------------------------------------------------------------------
# 2. CSE API endpoints
# ---------------------------------------------------------------------------

def probe_cse_api():
    _section("2. CSE API Endpoints")
    base = "https://www.cse.lk/api/"
    results = {}

    tests = [
        ("chartData",         "POST", {"chartId": "1", "period": "5"}),
        ("allStock",          "POST", {}),
        ("companyInfoSummery","POST", {"symbol": "COMB.N0000"}),
        ("returnAspiSnp",     "GET",  None),
        ("marketStatus",      "GET",  None),
        ("graphData",         "POST", {"chartId": "1", "startDate": "2021-01-01"}),
        ("aspi",              "GET",  None),
        ("aspi/history",      "GET",  None),
        ("aspi/daily",        "GET",  None),
    ]

    for endpoint, method, params in tests:
        url = base + endpoint
        try:
            if method == "POST":
                r = post_form(url, params or {})
            else:
                r = get(url, params=params)

            ok = r.status_code == 200
            preview = r.text[:120].replace("\n", " ")
            results[endpoint] = {
                "method": method,
                "status": r.status_code,
                "ok": ok,
                "preview": preview,
            }
            marker = "OK " if ok else "ERR"
            log.info("  %s [%d] %s %s — %s", marker, r.status_code, method, endpoint, preview[:80])
        except Exception as exc:
            results[endpoint] = {"method": method, "status": None, "ok": False, "error": str(exc)}
            log.warning("  FAIL %s: %s", endpoint, exc)
        time.sleep(0.5)

    RESULTS["cse_api"] = results
    return results


# ---------------------------------------------------------------------------
# 3. ASPI historical data — build combined CSV
# ---------------------------------------------------------------------------

def build_aspi_combined():
    _section("3. ASPI Historical Data")

    legacy_path   = "data/raw/legacy/CSE.csv"
    combined_path = "data/raw/legacy/aspi_combined.csv"

    # 3a. Load Kaggle legacy data
    legacy_df = pd.read_csv(legacy_path)
    legacy_df["Date"] = pd.to_datetime(legacy_df["Date"])
    legacy_df = (
        legacy_df[["Date", "Open", "High", "Low", "Close", "Volume"]]
        .dropna(subset=["Close"])
        .sort_values("Date")
    )
    log.info(
        "  Legacy CSV: %d rows  %s -> %s",
        len(legacy_df),
        legacy_df["Date"].min().date(),
        legacy_df["Date"].max().date(),
    )

    # 3b. Fetch trailing 1-year ASPI from CSE API
    api_df = pd.DataFrame()
    try:
        r = post_form("https://www.cse.lk/api/chartData", {"chartId": "1", "period": "5"})
        raw = r.json()
        rows = []
        for item in raw:
            ts = item["d"] / 1000
            date = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            rows.append({"Date": date, "Close": item["v"]})
        api_df = pd.DataFrame(rows)
        api_df["Date"] = pd.to_datetime(api_df["Date"])
        api_df["Open"] = api_df["Close"]
        api_df["High"] = api_df["Close"]
        api_df["Low"]  = api_df["Close"]
        api_df["Volume"] = None
        api_df = api_df[["Date", "Open", "High", "Low", "Close", "Volume"]]
        log.info(
            "  CSE API:    %d rows  %s -> %s",
            len(api_df),
            api_df["Date"].min().date(),
            api_df["Date"].max().date(),
        )
    except Exception as exc:
        log.warning("  CSE API fetch failed: %s", exc)

    # 3c. Merge
    frames = [f for f in [legacy_df, api_df] if not f.empty]
    combined = (
        pd.concat(frames, ignore_index=True)
        .sort_values("Date")
        .drop_duplicates("Date")
        .reset_index(drop=True)
    )
    combined.to_csv(combined_path, index=False)
    log.info(
        "  Combined:   %d rows  %s -> %s  saved to %s",
        len(combined),
        combined["Date"].min().date(),
        combined["Date"].max().date(),
        combined_path,
    )

    # 3d. Report gap
    if not api_df.empty:
        gap_start = legacy_df["Date"].max() + pd.Timedelta(days=1)
        gap_end   = api_df["Date"].min()  - pd.Timedelta(days=1)
        gap_days  = max(0, (gap_end - gap_start).days)
        log.warning(
            "  DATA GAP: %s to %s (%d days) — requires scraping",
            gap_start.date(), gap_end.date(), gap_days,
        )
        RESULTS["aspi_gap"] = {
            "gap_start": str(gap_start.date()),
            "gap_end":   str(gap_end.date()),
            "gap_days":  gap_days,
        }

    RESULTS["aspi_combined_rows"] = len(combined)
    return combined


# ---------------------------------------------------------------------------
# 4. Yahoo Finance CSE ticker coverage
# ---------------------------------------------------------------------------

def probe_yfinance():
    _section("4. Yahoo Finance CSE Ticker Coverage")

    try:
        import yfinance as yf
    except ImportError:
        log.warning("  yfinance not installed — skipping")
        return {}

    test_tickers = [
        "COMB", "JKH", "LOLC", "HNB", "SAMP",
        "DIAL", "CTC",  "LIOC", "VONE", "GREG",
        "HAYL", "HUNA", "LOFC", "NDB",  "SEYB",
        "TJL",  "PARQ", "OSEA", "BUKI", "KAPI",
    ]

    rows = []
    for t in test_tickers:
        ticker = f"{t}.CM"
        df = yf.download(ticker, start="2020-01-01", progress=False, auto_adjust=True)
        available = not df.empty
        rows.append({
            "cse_symbol":   t,
            "yahoo_ticker": ticker,
            "yf_available": available,
            "date_min":     str(df.index.min().date()) if available else "",
            "date_max":     str(df.index.max().date()) if available else "",
        })
        status = "OK  " if available else "MISS"
        log.info("  %s %s — %d rows", status, ticker, len(df))

    os.makedirs("docs", exist_ok=True)
    pd.DataFrame(rows).to_csv("docs/ticker_mapping.csv", index=False)
    hits = [r["cse_symbol"] for r in rows if r["yf_available"]]
    log.info("  Hits: %s  (saved docs/ticker_mapping.csv)", hits or "none")
    RESULTS["yfinance_hits"] = hits
    return rows


# ---------------------------------------------------------------------------
# 5. LBO archive probe
# ---------------------------------------------------------------------------

def probe_lbo():
    _section("5. LBO (Lanka Business Online) Archive")
    results = {}

    # WordPress REST API
    url = "https://www.lankabusinessonline.com/wp-json/wp/v2/posts"
    try:
        r = get(url, params={"per_page": 5, "page": 1, "orderby": "date", "order": "desc"})
        posts = r.json()
        total       = int(r.headers.get("X-WP-Total", 0))
        total_pages = int(r.headers.get("X-WP-TotalPages", 0))
        first_date  = posts[0].get("date", "") if posts else ""
        # oldest post — ascending order page 1 gives earliest entries
        r2 = get(url, params={"per_page": 1, "page": 1, "orderby": "date", "order": "asc"})
        posts2 = r2.json()
        last_date = posts2[0].get("date", "") if posts2 else ""

        results["wp_rest_api"] = {
            "status": r.status_code,
            "total_posts": total,
            "total_pages": total_pages,
            "newest": first_date,
            "oldest": last_date,
        }
        log.info(
            "  WP REST API: %d posts, %d pages  %s -> %s",
            total, total_pages, last_date[:10], first_date[:10],
        )
    except Exception as exc:
        results["wp_rest_api"] = {"error": str(exc)}
        log.warning("  WP REST API failed: %s", exc)

    # RSS feed
    try:
        feed = feedparser.parse("https://www.lankabusinessonline.com/feed/")
        entries = feed.entries
        results["rss"] = {
            "status": feed.status if hasattr(feed, "status") else None,
            "entry_count": len(entries),
            "latest_title": entries[0].get("title", "") if entries else "",
            "latest_date":  entries[0].get("published", "") if entries else "",
        }
        log.info("  RSS: %d entries, latest=%s", len(entries),
                 entries[0].get("published", "")[:22] if entries else "N/A")
    except Exception as exc:
        results["rss"] = {"error": str(exc)}
        log.warning("  RSS failed: %s", exc)

    RESULTS["lbo"] = results
    return results


# ---------------------------------------------------------------------------
# 6. Daily FT probe
# ---------------------------------------------------------------------------

def probe_dailyft():
    _section("6. Daily FT Archive")
    results = {}

    # WordPress REST API
    url = "https://www.ft.lk/wp-json/wp/v2/posts"
    try:
        r = get(url, params={"per_page": 5, "page": 1})
        if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
            posts = r.json()
            total = int(r.headers.get("X-WP-Total", 0))
            results["wp_rest_api"] = {"status": 200, "total_posts": total}
            log.info("  WP REST API: %d posts", total)
        else:
            results["wp_rest_api"] = {"status": r.status_code, "available": False}
            log.info("  WP REST API: %d — not available", r.status_code)
    except Exception as exc:
        results["wp_rest_api"] = {"error": str(exc)}
        log.warning("  WP REST API failed: %s", exc)

    # RSS feed
    try:
        feed = feedparser.parse("https://www.ft.lk/feed/")
        results["rss"] = {
            "status": feed.status if hasattr(feed, "status") else None,
            "entry_count": len(feed.entries),
        }
        log.info("  RSS: status=%s entries=%d",
                 getattr(feed, "status", "?"), len(feed.entries))
    except Exception as exc:
        results["rss"] = {"error": str(exc)}

    # HTML scrape check (capital-markets section)
    try:
        r = get("https://www.ft.lk/capital-markets/page/1")
        soup = BeautifulSoup(r.text, "html.parser")
        articles = soup.select("article")
        results["html_scrape"] = {
            "status": r.status_code,
            "article_tags_found": len(articles),
            "note": "JS-rendered SPA — requires Playwright for full extraction",
        }
        log.info("  HTML scrape: [%d] article tags=%d  (JS-rendered, needs Playwright)",
                 r.status_code, len(articles))
    except Exception as exc:
        results["html_scrape"] = {"error": str(exc)}

    RESULTS["dailyft"] = results
    return results


# ---------------------------------------------------------------------------
# 7. Alpha Vantage probe
# ---------------------------------------------------------------------------

def probe_alphavantage():
    _section("7. Alpha Vantage")
    av_key = os.getenv("ALPHA_VANTAGE_KEY", "")
    if not av_key:
        log.warning("  ALPHA_VANTAGE_KEY not set in .env — skipping")
        RESULTS["alphavantage"] = {"error": "key_not_set"}
        return {}

    results = {}
    # Symbol search for CSE tickers
    for sym in ["COMB.CM", "JKH.CM", "COMB"]:
        try:
            r = get(
                "https://www.alphavantage.co/query",
                params={"function": "SYMBOL_SEARCH", "keywords": sym, "apikey": av_key},
            )
            matches = r.json().get("bestMatches", [])
            cse_matches = [m for m in matches if "COLOMBO" in str(m).upper() or ".CM" in m.get("1. symbol", "")]
            results[sym] = {"matches": len(matches), "cse_matches": len(cse_matches)}
            log.info("  SYMBOL_SEARCH %s: %d matches, %d CSE", sym, len(matches), len(cse_matches))
        except Exception as exc:
            results[sym] = {"error": str(exc)}
        time.sleep(1.2)

    RESULTS["alphavantage"] = results
    return results


# ---------------------------------------------------------------------------
# 8. Finnhub probe
# ---------------------------------------------------------------------------

def probe_finnhub():
    _section("8. Finnhub")
    fh_key = os.getenv("FINNHUB_KEY", "")
    if not fh_key:
        log.warning("  FINNHUB_KEY not set in .env — skipping")
        RESULTS["finnhub"] = {"error": "key_not_set"}
        return {}

    try:
        import finnhub
    except ImportError:
        log.warning("  finnhub-python not installed — skipping")
        RESULTS["finnhub"] = {"error": "not_installed"}
        return {}

    client = finnhub.Client(api_key=fh_key)
    results = {}
    start_ts = int(pd.Timestamp("2024-01-01").timestamp())
    end_ts   = int(pd.Timestamp("2024-06-01").timestamp())

    for sym in ["COMB.CM", "JKH.CM", "CSE:COMB"]:
        try:
            res = client.stock_candles(sym, "D", start_ts, end_ts)
            status = res.get("s", "unknown")
            count  = len(res.get("c", []))
            results[sym] = {"status": status, "candle_count": count}
            log.info("  %s: status=%s candles=%d", sym, status, count)
        except Exception as exc:
            results[sym] = {"error": str(exc)[:100]}
            log.info("  %s: %s", sym, str(exc)[:80])
        time.sleep(0.5)

    RESULTS["finnhub"] = results
    return results


# ---------------------------------------------------------------------------
# 9. Save consolidated results JSON
# ---------------------------------------------------------------------------

def save_results():
    _section("9. Saving Results")
    os.makedirs("docs", exist_ok=True)
    out_path = "docs/recon_results.json"
    with open(out_path, "w") as f:
        json.dump(RESULTS, f, indent=2, default=str)
    log.info("  Saved: %s", out_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("Phase 0 Reconnaissance — CSE ML Dataset")
    log.info("Project root: %s", os.getcwd())

    probe_network()
    probe_cse_api()
    build_aspi_combined()
    probe_yfinance()
    probe_lbo()
    probe_dailyft()
    probe_alphavantage()
    probe_finnhub()
    save_results()

    log.info("=" * 60)
    log.info("Recon complete. See docs/recon_results.json for full output.")
    log.info("=" * 60)

    # Print summary table
    print("\n--- PHASE 0 SUMMARY ---")
    print(f"{'Source':<30} {'Status'}")
    print("-" * 55)
    nw = RESULTS.get("network", {})
    for name in ["cse.lk", "lbo.lk", "ft.lk", "web.archive.org", "investing.com"]:
        info = nw.get(name, {})
        status = f"OK [{info.get('status','')}]" if info.get("reachable") else f"FAIL ({info.get('error','')})"
        print(f"  Network: {name:<22} {status}")

    aspi_gap = RESULTS.get("aspi_gap", {})
    if aspi_gap:
        print(f"\n  ASPI gap: {aspi_gap['gap_start']} to {aspi_gap['gap_end']} ({aspi_gap['gap_days']} days)")
    else:
        print(f"\n  ASPI combined rows: {RESULTS.get('aspi_combined_rows','?')}")

    lbo = RESULTS.get("lbo", {}).get("wp_rest_api", {})
    print(f"\n  LBO WP REST API: {lbo.get('total_posts','?')} posts  {lbo.get('oldest','?')[:10]} -> {lbo.get('newest','?')[:10]}")

    yf_hits = RESULTS.get("yfinance_hits", [])
    print(f"\n  Yahoo Finance CSE hits: {len(yf_hits)}/20")


if __name__ == "__main__":
    main()
