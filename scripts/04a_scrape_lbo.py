"""
Scrape Lanka Business Online (LBO) articles via WordPress REST API.

Outputs:
    data/raw/news/lbo_articles_raw.csv
    data/processed/news/lbo_articles_clean.csv
"""

import os
import time
import logging
import requests
import pandas as pd
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
log = logging.getLogger(__name__)

BASE_URL  = "https://www.lankabusinessonline.com/wp-json/wp/v2/posts"
RAW_OUT   = "data/raw/news/lbo_articles_raw.csv"
CLEAN_OUT = "data/processed/news/lbo_articles_clean.csv"
MAX_PAGES = 20
PER_PAGE  = 100


def clean_html(text: str) -> str:
    if not text:
        return ""
    return BeautifulSoup(text, "html.parser").get_text(separator=" ").strip()


def scrape() -> list[dict]:
    all_articles = []
    for page in range(1, MAX_PAGES + 1):
        try:
            r = requests.get(
                BASE_URL,
                params={"per_page": PER_PAGE, "page": page},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15,
            )
            if r.status_code == 400:
                log.info("Reached last page at %d", page)
                break
            posts = r.json()
            if not posts:
                break
            for post in posts:
                all_articles.append({
                    "id":      post.get("id"),
                    "date":    post.get("date"),
                    "source":  "LBO",
                    "title":   clean_html(post.get("title", {}).get("rendered", "")),
                    "content": clean_html(post.get("content", {}).get("rendered", "")),
                    "excerpt": clean_html(post.get("excerpt", {}).get("rendered", "")),
                    "url":     post.get("link"),
                })
            log.info("Page %d: %d articles (total so far: %d)", page, len(posts), len(all_articles))
            time.sleep(1)
        except Exception as e:
            log.error("Error on page %d: %s", page, e)
            break
    return all_articles


def main():
    os.makedirs("data/raw/news", exist_ok=True)
    os.makedirs("data/processed/news", exist_ok=True)

    # Skip if raw already exists
    if os.path.exists(RAW_OUT):
        log.info("Raw file exists (%s) — loading from cache", RAW_OUT)
        raw_df = pd.read_csv(RAW_OUT)
    else:
        articles = scrape()
        raw_df = pd.DataFrame(articles)
        raw_df.to_csv(RAW_OUT, index=False)
        log.info("Saved raw: %s (%d rows)", RAW_OUT, len(raw_df))

    clean_df = raw_df[["id", "date", "source", "title", "excerpt", "url"]].copy()
    clean_df["text"] = clean_df["title"] + " " + clean_df["excerpt"]
    clean_df["symbol"] = None
    clean_df.to_csv(CLEAN_OUT, index=False)
    log.info("Saved clean: %s (%d rows)", CLEAN_OUT, len(clean_df))


if __name__ == "__main__":
    main()
