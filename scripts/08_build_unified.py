"""
Build the unified published dataset by joining:
  - Engineered price features (all_stocks_features.parquet)
  - Macro indicators (usd_lkr, global indices, cbsl)
  - Sentiment scores (unified_sentiment.csv, aggregated daily per symbol)

Output:
    data/published/cse_unified.parquet
"""

import os
import logging
import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
log = logging.getLogger(__name__)

FEATURES   = "data/processed/all_stocks_features.parquet"
USD_LKR    = "data/processed/macro/usd_lkr_daily.csv"
GLOBAL_IDX = "data/processed/macro/global_indices.csv"
CBSL       = "data/processed/macro/cbsl_indicators.csv"
INTEREST   = "data/processed/macro/interest_rates.csv"
SENTIMENT  = "data/processed/news/unified_sentiment.csv"
OUTPUT     = "data/published/cse_unified.parquet"


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_macro() -> pd.DataFrame:
    frames = []

    if os.path.exists(USD_LKR):
        df = pd.read_csv(USD_LKR, parse_dates=["date"])
        df = df[["date", "usd_lkr"]].dropna()
        frames.append(df.set_index("date"))
        log.info("USD/LKR: %d rows", len(df))

    if os.path.exists(GLOBAL_IDX):
        df = pd.read_csv(GLOBAL_IDX, parse_dates=["date"])
        df = df.set_index("date")
        frames.append(df)
        log.info("Global indices: %d rows, cols=%s", len(df), df.columns.tolist())

    if os.path.exists(CBSL):
        df = pd.read_csv(CBSL, parse_dates=["date"])
        keep = [c for c in ["date", "gdp_growth_pct", "inflation_pct"] if c in df.columns]
        df = df[keep].dropna(subset=["date"]).set_index("date")
        frames.append(df)
        log.info("CBSL indicators: %d rows", len(df))

    if os.path.exists(INTEREST):
        df = pd.read_csv(INTEREST, parse_dates=["date"])
        keep = [c for c in ["date", "tbill_3m", "tbill_6m", "tbill_12m", "policy_rate"] if c in df.columns]
        df = df[keep].dropna(subset=["date"]).set_index("date")
        for col in [c for c in df.columns if c != "date"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        frames.append(df)
        log.info("Interest rates: %d rows", len(df))

    if not frames:
        log.warning("No macro files found — macro columns will be absent")
        return pd.DataFrame()

    macro = pd.concat(frames, axis=1).sort_index()
    # Forward-fill macro to daily granularity within a 365-day limit
    daily_idx = pd.date_range(macro.index.min(), macro.index.max(), freq="D")
    macro = macro.reindex(daily_idx).ffill(limit=365)
    macro.index.name = "date"
    log.info("Macro after forward-fill: %d daily rows, %d cols", len(macro), len(macro.columns))
    return macro.reset_index()


def load_sentiment() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (symbol_sentiment, market_sentiment) — two separate aggregations."""
    empty = pd.DataFrame()
    if not os.path.exists(SENTIMENT):
        log.warning("Sentiment file not found — sentiment columns will be absent")
        return empty, empty

    df = pd.read_csv(SENTIMENT, parse_dates=["date"])
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()

    # Symbol-level sentiment: CSE announcements that have both date and symbol
    sym_df = df.dropna(subset=["date", "symbol"])
    if not sym_df.empty:
        sym_agg = (
            sym_df.groupby(["date", "symbol"])
            .agg(
                vader_score_mean=("vader_score", "mean"),
                vader_score_max=("vader_score", "max"),
                news_count=("vader_score", "count"),
                vader_label=("vader_label", lambda x: x.mode().iloc[0] if len(x) > 0 else "neutral"),
            )
            .reset_index()
        )
        log.info("Symbol-level sentiment: %d rows", len(sym_agg))
    else:
        sym_agg = empty

    # Market-level sentiment: LBO articles (date only, no symbol)
    mkt_df = df[df["symbol"].isna()].dropna(subset=["date"])
    if not mkt_df.empty:
        mkt_agg = (
            mkt_df.groupby("date")
            .agg(
                market_vader_mean=("vader_score", "mean"),
                market_news_count=("vader_score", "count"),
            )
            .reset_index()
        )
        log.info("Market-level sentiment (LBO): %d rows", len(mkt_agg))
    else:
        mkt_agg = empty

    return sym_agg, mkt_agg


# ---------------------------------------------------------------------------
# Join
# ---------------------------------------------------------------------------

def build(price: pd.DataFrame, macro: pd.DataFrame,
          sym_sent: pd.DataFrame, mkt_sent: pd.DataFrame) -> pd.DataFrame:
    df = price.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()

    # Left-join macro on date
    if not macro.empty:
        macro["date"] = pd.to_datetime(macro["date"]).dt.normalize()
        df = df.merge(macro, on="date", how="left")
        log.info("After macro join: %d rows, %d cols", len(df), len(df.columns))

    # Left-join symbol-level sentiment on (date, symbol)
    if not sym_sent.empty:
        sym_sent["date"] = pd.to_datetime(sym_sent["date"]).dt.normalize()
        df = df.merge(sym_sent, on=["date", "symbol"], how="left")
        log.info("After symbol-sentiment join: %d rows, %d cols", len(df), len(df.columns))
    else:
        df["vader_score_mean"] = np.nan
        df["vader_score_max"]  = np.nan
        df["news_count"]       = 0
        df["vader_label"]      = np.nan
        df["finbert_label"]    = np.nan

    # Left-join market-level sentiment on date only (LBO)
    if not mkt_sent.empty:
        mkt_sent["date"] = pd.to_datetime(mkt_sent["date"]).dt.normalize()
        df = df.merge(mkt_sent, on="date", how="left")
        log.info("After market-sentiment join: %d rows, %d cols", len(df), len(df.columns))
    else:
        df["market_vader_mean"]  = np.nan
        df["market_news_count"]  = 0

    return df


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

def verify(df: pd.DataFrame):
    log.info("--- Verification ---")
    log.info("Shape:          %s", df.shape)
    log.info("Symbols:        %d", df["symbol"].nunique())
    log.info("Date range:     %s  to  %s", df["date"].min().date(), df["date"].max().date())

    price_cols = ["open", "high", "low", "close", "adj_close",
                  "return_1d", "volatility_20d", "close_to_ma50"]
    for col in price_cols:
        assert col in df.columns, f"Missing price/feature column: {col}"

    macro_cols_present = [c for c in ["usd_lkr", "sp500", "gdp_growth_pct"] if c in df.columns]
    log.info("Macro cols present: %s", macro_cols_present)
    log.info("Sentiment cols: vader_score_mean non-null = %d",
             df["vader_score_mean"].notna().sum() if "vader_score_mean" in df.columns else 0)
    log.info("Verification passed.")


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

def save(df: pd.DataFrame):
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    df.to_parquet(OUTPUT, index=False)
    log.info("Saved: %s  (%d rows, %d cols)", OUTPUT, len(df), len(df.columns))


def main():
    log.info("Loading price features...")
    price = pd.read_parquet(FEATURES)

    log.info("Loading macro...")
    macro = load_macro()

    log.info("Loading sentiment...")
    sym_sent, mkt_sent = load_sentiment()

    df = build(price, macro, sym_sent, mkt_sent)
    verify(df)
    save(df)


if __name__ == "__main__":
    main()
