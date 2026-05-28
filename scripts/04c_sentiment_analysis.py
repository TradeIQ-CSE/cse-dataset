"""
Run VADER sentiment analysis on the combined news corpus.

Inputs:
    data/processed/news/lbo_articles_clean.csv
    data/processed/news/cse_news_clean.csv

Output:
    data/processed/news/unified_sentiment.csv
"""

import os
import logging
import pandas as pd
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
log = logging.getLogger(__name__)

LBO_CLEAN = "data/processed/news/lbo_articles_clean.csv"
CSE_CLEAN = "data/processed/news/cse_news_clean.csv"
OUTPUT    = "data/processed/news/unified_sentiment.csv"


def get_vader_compound(text: str, analyzer: SentimentIntensityAnalyzer) -> float:
    if not isinstance(text, str) or not text.strip():
        return 0.0
    return analyzer.polarity_scores(text)["compound"]


def map_label(score: float) -> str:
    if score >= 0.05:
        return "positive"
    if score <= -0.05:
        return "negative"
    return "neutral"


def main():
    os.makedirs("data/processed/news", exist_ok=True)
    analyzer = SentimentIntensityAnalyzer()

    dfs = []
    for path in [LBO_CLEAN, CSE_CLEAN]:
        if os.path.exists(path):
            df = pd.read_csv(path)
            dfs.append(df)
            log.info("Loaded %s: %d rows", path, len(df))
        else:
            log.warning("Not found: %s", path)

    if not dfs:
        log.error("No cleaned news files found. Run 04a and 04b first.")
        return

    df = pd.concat(dfs, ignore_index=True)
    log.info("Combined corpus: %d rows", len(df))

    # Ensure text column exists
    if "text" not in df.columns:
        df["text"] = df.get("title", "").fillna("")

    log.info("Scoring VADER...")
    df["vader_score"] = df["text"].apply(lambda t: get_vader_compound(t, analyzer))
    df["vader_label"] = df["vader_score"].apply(map_label)
    df["finbert_label"] = pd.NA

    # Normalise date column
    df["date"] = pd.to_datetime(df["date"], errors="coerce")

    df.to_csv(OUTPUT, index=False)
    log.info("Saved: %s (%d rows)", OUTPUT, len(df))

    # Verification
    assert df["vader_score"].notna().all(), "Some vader_score values are null"
    assert set(df["vader_label"].unique()).issubset({"positive", "neutral", "negative"})
    pos = (df["vader_label"] == "positive").sum()
    neg = (df["vader_label"] == "negative").sum()
    neu = (df["vader_label"] == "neutral").sum()
    log.info("VADER label distribution — positive: %d, neutral: %d, negative: %d", pos, neu, neg)
    log.info("Verification passed.")


if __name__ == "__main__":
    main()
