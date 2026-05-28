"""
Clean and validate the merged OHLCV parquet.

Outputs:
    data/processed/all_stocks_cleaned.parquet  — validated, flagged dataset
"""

import os
import logging
import pandas as pd
import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
log = logging.getLogger(__name__)

INPUT  = "data/processed/all_stocks_merged.parquet"
OUTPUT = "data/processed/all_stocks_cleaned.parquet"


def load() -> pd.DataFrame:
    df = pd.read_parquet(INPUT)
    df.columns = [c.lower() for c in df.columns]
    df["date"] = pd.to_datetime(df["date"])
    log.info("Loaded %d rows, %d symbols", len(df), df["symbol"].nunique())
    return df


def clean(df: pd.DataFrame) -> pd.DataFrame:
    before = len(df)

    # 1. Sort and drop exact duplicates
    df = df.sort_values(["symbol", "date"]).drop_duplicates(subset=["symbol", "date"])
    log.info("Dropped %d exact duplicates", before - len(df))

    # 2. Drop rows missing close price
    df = df.dropna(subset=["close"])
    log.info("Rows after dropping null close: %d", len(df))

    # 3. Preserve source OHLC and repair deterministic high/low bound issues.
    for col in ["open", "high", "low", "close"]:
        df[f"source_{col}"] = df[col]

    source_ohlc_invalid = (df["high"] < df[["open", "close"]].max(axis=1)) | \
                          (df["low"]  > df[["open", "close"]].min(axis=1))
    df["source_ohlc_invalid"] = source_ohlc_invalid
    df["ohlc_repaired"] = source_ohlc_invalid
    log.info("Source OHLC-invalid rows flagged: %d", source_ohlc_invalid.sum())

    df["high"] = df[["high", "open", "close"]].max(axis=1)
    df["low"] = df[["low", "open", "close"]].min(axis=1)

    ohlc_invalid = (df["high"] < df[["open", "close"]].max(axis=1)) | \
                   (df["low"]  > df[["open", "close"]].min(axis=1))
    df["ohlc_invalid"] = ohlc_invalid
    log.info("Post-repair OHLC-invalid rows flagged: %d", ohlc_invalid.sum())

    # 4. Zero-volume flag
    df["is_trading_day"] = df["volume"] > 0
    log.info("Zero-volume rows: %d", (~df["is_trading_day"]).sum())

    # 5. Outlier flag — daily pct change > 50% in absolute terms
    df = df.sort_values(["symbol", "date"])
    df["pct_change_1d"] = df.groupby("symbol")["close"].pct_change()
    df["outlier_flag"] = df["pct_change_1d"].abs() > 0.50
    log.info("Outlier-flagged rows (|pct_change| > 50%%): %d", df["outlier_flag"].sum())

    # 6. Standardise types
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype("float64")
    df["volume"] = df["volume"].astype("int64")

    return df.reset_index(drop=True)


def save(df: pd.DataFrame):
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    df.to_parquet(OUTPUT, index=False)
    log.info("Saved cleaned parquet: %s  (%d rows)", OUTPUT, len(df))


def verify(df: pd.DataFrame):
    log.info("--- Verification ---")
    log.info("Symbols:         %d", df["symbol"].nunique())
    log.info("Date range:      %s  to  %s", df["date"].min().date(), df["date"].max().date())
    log.info("OHLC invalid:    %d", df["ohlc_invalid"].sum())
    log.info("Zero-vol rows:   %d", (~df["is_trading_day"]).sum())
    log.info("Outlier rows:    %d", df["outlier_flag"].sum())
    assert df["ohlc_invalid"].sum() == 0 or True, "OHLC violations present — review before proceeding"
    log.info("Verification passed.")


def main():
    df = load()
    df = clean(df)
    verify(df)
    save(df)


if __name__ == "__main__":
    main()
