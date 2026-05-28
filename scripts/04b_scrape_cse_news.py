"""
Scrape per-company news from the CSE internal API.

Outputs:
    data/raw/news/cse_news_raw.csv
    data/processed/news/cse_news_clean.csv
"""

import os
import time
import logging
import re
import requests
import pandas as pd
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
log = logging.getLogger(__name__)

RAW_OUT   = "data/raw/news/cse_news_raw.csv"
CLEAN_OUT = "data/processed/news/cse_news_clean.csv"
METADATA  = "data/processed/company_metadata.csv"
HEADERS   = {"User-Agent": "Mozilla/5.0"}


def clean_html(text: str) -> str:
    if not text:
        return ""
    return BeautifulSoup(str(text), "html.parser").get_text(separator=" ").strip()


def parse_possible_date(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return pd.NaT
    if isinstance(value, (int, float)) and value > 10_000:
        return pd.to_datetime(value, unit="ms", errors="coerce")
    return pd.to_datetime(value, errors="coerce")


def extract_date(item: dict) -> tuple[object, str]:
    for field in [
        "date",
        "createdDate",
        "publishedDate",
        "uploadedDate",
        "newsDate",
        "announcementDate",
        "dateOfAnnouncement",
    ]:
        parsed = parse_possible_date(item.get(field))
        if pd.notna(parsed):
            return parsed, f"api_field:{field}"

    for field in ["path", "url", "link"]:
        value = item.get(field)
        if not value:
            continue
        match = re.search(r"(20\d{2})[/-](\d{1,2})[/-](\d{1,2})", str(value))
        if match:
            parsed = pd.to_datetime("-".join(match.groups()), errors="coerce")
            if pd.notna(parsed):
                return parsed, f"url:{field}"

    return pd.NaT, "missing_from_api_payload"


def get_security_ids() -> dict[str, str]:
    try:
        r = requests.get("https://www.cse.lk/api/allSecurityCode", headers=HEADERS, timeout=15)
        codes = r.json()
        return {item["symbol"]: str(item["id"]) for item in codes if "symbol" in item and "id" in item}
    except Exception as e:
        log.error("Could not fetch security codes: %s", e)
        return {}


def get_company_news(symbol: str, sec_id: str) -> list[dict]:
    try:
        url = f"https://www.cse.lk/api/news/web?top=false&type=BN&security={sec_id}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            items = r.json().get("BN", [])
            rows = []
            for item in items:
                raw_date, date_source = extract_date(item)
                rows.append({
                    "id":      item.get("id"),
                    "date":    raw_date,
                    "date_missing_reason": None if pd.notna(raw_date) else date_source,
                    "date_source": date_source if pd.notna(raw_date) else None,
                    "source":  "CSE",
                    "symbol":  symbol,
                    "title":   clean_html(item.get("fileText", "")),
                    "content": None,
                    "url":     item.get("path"),
                })
            return rows
    except Exception as e:
        log.warning("Error fetching news for %s: %s", symbol, e)
    return []


def main():
    os.makedirs("data/raw/news", exist_ok=True)
    os.makedirs("data/processed/news", exist_ok=True)

    if os.path.exists(RAW_OUT):
        log.info("Raw file exists (%s) — loading from cache", RAW_OUT)
        raw_df = pd.read_csv(RAW_OUT)
    else:
        meta = pd.read_csv(METADATA)
        symbols = meta["symbol"].dropna().unique().tolist()

        log.info("Fetching security ID mapping...")
        sec_map = get_security_ids()
        log.info("Got %d security IDs", len(sec_map))

        all_rows = []
        for i, sym in enumerate(symbols):
            sec_id = sec_map.get(sym)
            if not sec_id:
                continue
            rows = get_company_news(sym, sec_id)
            all_rows.extend(rows)
            if i % 50 == 0:
                log.info("Progress: %d / %d symbols, %d articles so far", i, len(symbols), len(all_rows))
            time.sleep(0.3)

        raw_df = pd.DataFrame(all_rows)
        raw_df.to_csv(RAW_OUT, index=False)
        log.info("Saved raw: %s (%d rows)", RAW_OUT, len(raw_df))

    if "date_missing_reason" not in raw_df.columns:
        raw_df["date_missing_reason"] = raw_df["date"].apply(
            lambda v: None if pd.notna(v) and str(v).strip() else "missing_from_api_payload"
        )
    if "date_source" not in raw_df.columns:
        raw_df["date_source"] = raw_df["date"].apply(
            lambda v: "legacy_date_column" if pd.notna(v) and str(v).strip() else None
        )

    clean_df = raw_df[["id", "date", "date_source", "date_missing_reason", "source", "symbol", "title", "url"]].copy()
    clean_df["date"] = pd.to_datetime(clean_df["date"], errors="coerce")
    clean_df["text"] = clean_df["title"].fillna("")
    clean_df.to_csv(CLEAN_OUT, index=False)
    log.info("Saved clean: %s (%d rows)", CLEAN_OUT, len(clean_df))


if __name__ == "__main__":
    main()
