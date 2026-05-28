"""
Validate the published dataset and write both human and machine-readable quality
reports.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
UNIFIED = ROOT / "data/published/cse_unified.parquet"
SUMMARY = ROOT / "data/published/quality_summary.json"
REPORT = ROOT / "DATA_QUALITY_REPORT.md"
DIVIDENDS = ROOT / "data/processed/fundamentals/dividends.csv"
INTEREST = ROOT / "data/processed/macro/interest_rates.csv"


def load_previous_summary() -> dict:
    if SUMMARY.exists():
        try:
            return json.loads(SUMMARY.read_text())
        except Exception:
            return {}
    try:
        raw = subprocess.check_output(
            ["git", "show", "HEAD:data/published/quality_summary.json"],
            cwd=ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return json.loads(raw)
    except Exception:
        return {}


def null_summary(df: pd.DataFrame) -> pd.DataFrame:
    total = len(df)
    values = df.isna().sum()
    out = pd.DataFrame({
        "null_count": values,
        "null_pct": (values / total * 100).round(2),
    })
    return out.query("null_count > 0").sort_values("null_pct", ascending=False)


def compute_metrics(df: pd.DataFrame) -> dict:
    df["date"] = pd.to_datetime(df["date"])
    total_rows = int(len(df))
    ohlc_invalid = int(df["ohlc_invalid"].sum()) if "ohlc_invalid" in df else 0
    duplicate_rows = int(df.duplicated(subset=["symbol", "date"]).sum())
    max_date = df["date"].max().date()
    today = datetime.now(timezone.utc).date()

    dividends = pd.read_csv(DIVIDENDS) if DIVIDENDS.exists() else pd.DataFrame()
    interest = pd.read_csv(INTEREST) if INTEREST.exists() else pd.DataFrame()
    dividend_amount_rows = int(pd.to_numeric(dividends.get("amount_per_share", pd.Series(dtype=float)), errors="coerce").notna().sum()) if not dividends.empty else 0
    interest_rows = int(len(interest.dropna(subset=["date"]))) if "date" in interest.columns else 0
    interest_value_rows = 0
    interest_date_max = None
    interest_staleness_days = None
    if not interest.empty:
        rate_cols = [c for c in ["tbill_3m", "tbill_6m", "tbill_12m", "policy_rate"] if c in interest.columns]
        if rate_cols:
            interest_value_rows = int(interest[rate_cols].apply(pd.to_numeric, errors="coerce").notna().any(axis=1).sum())
        if "date" in interest.columns:
            interest_dates = pd.to_datetime(interest["date"], errors="coerce").dropna()
            if not interest_dates.empty:
                interest_date_max = interest_dates.max().date().isoformat()
                interest_staleness_days = int((today - interest_dates.max().date()).days)

    metrics = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_rows": total_rows,
        "symbols": int(df["symbol"].nunique()),
        "date_min": str(df["date"].min().date()),
        "date_max": str(max_date),
        "columns": int(len(df.columns)),
        "duplicate_symbol_date_rows": duplicate_rows,
        "ohlc_invalid_rows": ohlc_invalid,
        "source_ohlc_invalid_rows": int(df["source_ohlc_invalid"].sum()) if "source_ohlc_invalid" in df else ohlc_invalid,
        "ohlc_repaired_rows": int(df["ohlc_repaired"].sum()) if "ohlc_repaired" in df else 0,
        "ohlc_invalid_pct": round(ohlc_invalid / total_rows, 6) if total_rows else 0.0,
        "volume_zscore_null_pct": round(df["volume_zscore"].isna().mean(), 6) if "volume_zscore" in df else 1.0,
        "adj_close_adjusted_rows": int(df["adj_close_adjusted"].sum()) if "adj_close_adjusted" in df else 0,
        "adj_close_adjusted_symbols": int(df.loc[df["adj_close_adjusted"], "symbol"].nunique()) if "adj_close_adjusted" in df else 0,
        "dividend_rows": int(len(dividends)),
        "dividend_amount_rows": dividend_amount_rows,
        "interest_rate_rows": interest_rows,
        "interest_rate_value_rows": interest_value_rows,
        "interest_rate_date_max": interest_date_max,
        "interest_rate_staleness_days": interest_staleness_days,
        "tbill_3m_rows": int(df["tbill_3m"].notna().sum()) if "tbill_3m" in df else 0,
        "policy_rate_rows": int(df["policy_rate"].notna().sum()) if "policy_rate" in df else 0,
        "symbol_sentiment_rows": int(df["vader_score_mean"].notna().sum()) if "vader_score_mean" in df else 0,
        "symbol_sentiment_symbols": int(df.loc[df["vader_score_mean"].notna(), "symbol"].nunique()) if "vader_score_mean" in df else 0,
        "market_sentiment_rows": int(df["market_vader_mean"].notna().sum()) if "market_vader_mean" in df else 0,
        "sp500_rows": int(df["sp500"].notna().sum()) if "sp500" in df else 0,
        "max_date_staleness_days": int((today - max_date).days),
    }
    return metrics


def check_gates(metrics: dict, previous: dict, args: argparse.Namespace) -> list[str]:
    failures = []

    if metrics["total_rows"] <= 0:
        failures.append("unified dataset has zero rows")
    if metrics["duplicate_symbol_date_rows"] > 0:
        failures.append(f"duplicate (symbol, date) rows: {metrics['duplicate_symbol_date_rows']:,}")
    if not args.allow_stale and metrics["max_date_staleness_days"] > args.max_staleness_days:
        failures.append(
            f"max date is stale by {metrics['max_date_staleness_days']} days "
            f"(limit {args.max_staleness_days})"
        )
    if metrics["ohlc_invalid_pct"] > args.max_ohlc_invalid_pct:
        failures.append(
            f"OHLC-invalid rate {metrics['ohlc_invalid_pct']:.2%} exceeds "
            f"{args.max_ohlc_invalid_pct:.2%}"
        )
    if metrics["volume_zscore_null_pct"] > args.max_volume_zscore_null_pct:
        failures.append(
            f"volume_zscore null rate {metrics['volume_zscore_null_pct']:.2%} exceeds "
            f"{args.max_volume_zscore_null_pct:.2%}"
        )
    if INTEREST.exists():
        interest = pd.read_csv(INTEREST)
        required = {"date", "tbill_3m", "tbill_6m", "tbill_12m", "policy_rate", "source"}
        missing = sorted(required - set(interest.columns))
        if missing:
            failures.append(f"interest_rates.csv missing columns: {', '.join(missing)}")

    prev_rows = previous.get("total_rows")
    if prev_rows:
        min_rows = int(prev_rows * (1 - args.max_row_drop_pct))
        if metrics["total_rows"] < min_rows:
            failures.append(
                f"row count dropped from {prev_rows:,} to {metrics['total_rows']:,}"
            )

    prev_symbols = previous.get("symbols")
    if prev_symbols and metrics["symbols"] < prev_symbols - args.max_symbol_drop:
        failures.append(
            f"symbol count dropped from {prev_symbols} to {metrics['symbols']}"
        )

    return failures


def write_report(df: pd.DataFrame, metrics: dict, failures: list[str]) -> None:
    nulls = null_summary(df)
    by_symbol = (
        df.groupby("symbol")
        .agg(
            first_date=("date", "min"),
            last_date=("date", "max"),
            trading_days=("is_trading_day", "sum"),
            ohlc_invalid=("ohlc_invalid", "sum"),
        )
        .reset_index()
    )
    thin = by_symbol[by_symbol["trading_days"] < 250]

    lines = [
        "# CSE Dataset - Data Quality Report",
        "",
        f"Generated: {metrics['generated_at_utc']}",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Total rows | {metrics['total_rows']:,} |",
        f"| Symbols | {metrics['symbols']} |",
        f"| Date range | {metrics['date_min']} to {metrics['date_max']} |",
        f"| Columns | {metrics['columns']} |",
        f"| Duplicate `(symbol, date)` rows | {metrics['duplicate_symbol_date_rows']:,} |",
        f"| OHLC-invalid rows | {metrics['ohlc_invalid_rows']:,} ({metrics['ohlc_invalid_pct']:.2%}) |",
        f"| Source OHLC-invalid rows | {metrics['source_ohlc_invalid_rows']:,} |",
        f"| OHLC-repaired rows | {metrics['ohlc_repaired_rows']:,} |",
        f"| `volume_zscore` null rate | {metrics['volume_zscore_null_pct']:.2%} |",
        f"| Adjusted-close rows | {metrics['adj_close_adjusted_rows']:,} |",
        f"| Adjusted-close symbols | {metrics['adj_close_adjusted_symbols']:,} |",
        f"| Dividend rows with amount | {metrics['dividend_amount_rows']:,} / {metrics['dividend_rows']:,} |",
        f"| Interest-rate source rows | {metrics['interest_rate_value_rows']:,} / {metrics['interest_rate_rows']:,} |",
        f"| Interest-rate max date | {metrics['interest_rate_date_max'] or 'n/a'} |",
        f"| Interest-rate staleness | {metrics['interest_rate_staleness_days'] if metrics['interest_rate_staleness_days'] is not None else 'n/a'} days |",
        f"| Rows with T-bill 3M | {metrics['tbill_3m_rows']:,} |",
        f"| Rows with policy rate | {metrics['policy_rate_rows']:,} |",
        f"| Rows with symbol sentiment | {metrics['symbol_sentiment_rows']:,} |",
        f"| Symbols with sentiment | {metrics['symbol_sentiment_symbols']:,} |",
        f"| Rows with macro data (sp500 non-null) | {metrics['sp500_rows']:,} |",
        f"| Rows with market sentiment | {metrics['market_sentiment_rows']:,} |",
        f"| Max-date staleness | {metrics['max_date_staleness_days']} days |",
        "",
        "## Validation Gates",
        "",
    ]
    if failures:
        lines.extend(f"- FAIL: {failure}" for failure in failures)
    else:
        lines.append("- PASS: all configured quality gates passed")

    lines.extend([
        "",
        "## Null Values by Column",
        "",
        "| Column | Null Count | Null % |",
        "|---|---|---|",
    ])
    for col, row in nulls.iterrows():
        lines.append(f"| `{col}` | {int(row['null_count']):,} | {row['null_pct']}% |")

    lines.extend([
        "",
        "## Symbols with Thin Coverage (< 250 trading days)",
        "",
        "| Symbol | First Date | Last Date | Trading Days |",
        "|---|---|---|---|",
    ])
    for _, row in thin.iterrows():
        lines.append(
            f"| {row['symbol']} | {str(row['first_date'])[:10]} | "
            f"{str(row['last_date'])[:10]} | {int(row['trading_days'])} |"
        )

    lines.extend([
        "",
        "## Known Limitations",
        "",
        "- `usd_lkr` is annual World Bank data forward-filled to daily rows.",
        "- `interest_rates.csv` is populated from a manual CBSL CSV/XLSX import under `data/raw/macro/`.",
        "- `adj_close` applies only dividend rows with parsed `amount_per_share`; missing amounts are reported.",
        "- `vader_label` is derived from VADER thresholds; true `finbert_label` is reserved for model inference.",
        "- Symbol-level sentiment is partial and depends on dated CSE announcement records.",
        "- `source_*` OHLC columns preserve original CSE values where high/low repairs were needed.",
        "",
    ])

    REPORT.write_text("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate published CSE dataset")
    parser.add_argument("--allow-stale", action="store_true")
    parser.add_argument("--max-staleness-days", type=int, default=7)
    parser.add_argument("--max-row-drop-pct", type=float, default=0.01)
    parser.add_argument("--max-symbol-drop", type=int, default=1)
    parser.add_argument("--max-ohlc-invalid-pct", type=float, default=0.0311)
    parser.add_argument("--max-volume-zscore-null-pct", type=float, default=0.20)
    args = parser.parse_args()

    if not UNIFIED.exists():
        raise SystemExit(f"Missing unified parquet: {UNIFIED}")

    previous = load_previous_summary()
    df = pd.read_parquet(UNIFIED)
    metrics = compute_metrics(df)
    failures = check_gates(metrics, previous, args)

    SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY.write_text(json.dumps({**metrics, "failures": failures}, indent=2) + "\n")
    write_report(df, metrics, failures)

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        raise SystemExit(1)
    print("PASS: dataset validation succeeded")


if __name__ == "__main__":
    main()
