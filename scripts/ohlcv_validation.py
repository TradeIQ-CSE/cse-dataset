"""Validation gates for daily and backfill OHLCV candidates."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


CANONICAL_OHLCV_COLUMNS = [
    "date",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
    "trades",
    "source",
    "source_priority",
    "source_timestamp",
    "raw_payload_hash",
    "validation_status",
    "validation_warnings",
]


@dataclass(frozen=True)
class ValidationResult:
    accepted: pd.DataFrame
    rejected: pd.DataFrame
    failures: list[str]
    warnings: list[str]
    metrics: dict[str, Any]

    @property
    def passed(self) -> bool:
        return not self.failures and self.rejected.empty and not self.accepted.empty


def market_digest(records: pd.DataFrame) -> str:
    cols = ["date", "symbol", "open", "high", "low", "close", "volume", "turnover", "trades"]
    available = [col for col in cols if col in records.columns]
    canonical = records[available].sort_values(["symbol", "date"]).to_json(
        orient="records", date_format="iso", double_precision=8
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_metadata(path: Path | None) -> pd.DataFrame:
    if not path or not path.exists():
        return pd.DataFrame()
    metadata = pd.read_csv(path)
    if "symbol" not in metadata.columns:
        raise ValueError(f"metadata file is missing required column 'symbol': {path}")
    if "listing_date" in metadata.columns:
        listing_dates = pd.to_datetime(metadata["listing_date"], format="%d/%b/%Y", errors="coerce")
        fallback = pd.to_datetime(metadata.loc[listing_dates.isna(), "listing_date"], errors="coerce")
        listing_dates.loc[listing_dates.isna()] = fallback
        metadata["listing_date"] = listing_dates.dt.date
    return metadata


def _mark_rejected(reasons: list[list[str]], mask: pd.Series, reason: str) -> None:
    for idx, rejected in enumerate(mask.fillna(False).tolist()):
        if rejected:
            reasons[idx].append(reason)


def validate_ohlcv_records(
    records: pd.DataFrame,
    *,
    target_date: date,
    source_date_failures: list[str] | None = None,
    extra_rejection_reasons: pd.Series | list[str] | None = None,
    metadata: pd.DataFrame | None = None,
    previous_manifest: dict[str, Any] | None = None,
    allow_missing_metadata: bool = False,
    missing_value_threshold: float = 0.0,
    required_activity_columns: list[str] | None = None,
    low_variation_min_rows: int = 60,
    low_variation_max_distinct_closes: int = 2,
) -> ValidationResult:
    failures: list[str] = list(source_date_failures or [])
    warnings: list[str] = []

    if records.empty:
        return ValidationResult(
            accepted=records.copy(),
            rejected=records.copy(),
            failures=[*failures, "no OHLCV records were fetched"],
            warnings=warnings,
            metrics={"row_count": 0, "target_date": target_date.isoformat()},
        )

    df = records.copy()
    for column in CANONICAL_OHLCV_COLUMNS:
        if column not in df.columns:
            df[column] = None
    df = df[CANONICAL_OHLCV_COLUMNS]
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    df["source_timestamp"] = pd.to_datetime(df["source_timestamp"], errors="coerce").dt.date
    for column in ["open", "high", "low", "close", "volume", "turnover", "trades"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    for column in ["open", "high", "low", "close"]:
        df[f"source_{column}"] = df[column]

    rejection_reasons: list[list[str]] = [[] for _ in range(len(df))]
    if extra_rejection_reasons is not None:
        extra = pd.Series(extra_rejection_reasons, index=df.index).fillna("").astype(str)
        for idx, reason in enumerate(extra.tolist()):
            if reason.strip():
                rejection_reasons[idx].append(reason.strip())

    bad_date = df["date"] != target_date
    _mark_rejected(rejection_reasons, bad_date, "record date does not match target date")

    source_mismatch = df["source_timestamp"] != target_date
    _mark_rejected(rejection_reasons, source_mismatch, "source timestamp does not match target date")

    duplicate_mask = df.duplicated(subset=["symbol", "date"], keep=False)
    _mark_rejected(rejection_reasons, duplicate_mask, "duplicate (symbol, date)")
    if duplicate_mask.any():
        failures.append(f"duplicate (symbol, date) rows: {int(duplicate_mask.sum())}")

    required_price_cols = ["open", "high", "low", "close"]
    missing_price_mask = df[required_price_cols].isna().any(axis=1)
    _mark_rejected(rejection_reasons, missing_price_mask, "missing OHLC price")

    negative_prices = df[required_price_cols].notna().all(axis=1) & (df[required_price_cols] < 0).any(axis=1)
    _mark_rejected(rejection_reasons, negative_prices, "negative OHLC price")

    invalid_bounds = (
        df[required_price_cols].notna().all(axis=1)
        & ~negative_prices
        & (
            (df["high"] < df[["open", "low", "close"]].max(axis=1))
            | (df["low"] > df[["open", "high", "close"]].min(axis=1))
        )
    )
    repaired_count = int(invalid_bounds.sum())
    if repaired_count:
        df.loc[invalid_bounds, "high"] = df.loc[invalid_bounds, ["open", "high", "low", "close"]].max(axis=1)
        df.loc[invalid_bounds, "low"] = df.loc[invalid_bounds, ["open", "high", "low", "close"]].min(axis=1)
    df["source_ohlc_invalid"] = invalid_bounds
    df["ohlc_repaired"] = invalid_bounds
    df["ohlc_invalid"] = negative_prices

    missing_value_cols = required_activity_columns or ["volume", "turnover", "trades"]
    missing_value_rate = float(df[missing_value_cols].isna().any(axis=1).mean())
    if missing_value_rate > missing_value_threshold:
        failures.append(
            "missing volume/turnover/trades rate "
            f"{missing_value_rate:.2%} exceeds {missing_value_threshold:.2%}"
        )
    _mark_rejected(
        rejection_reasons,
        df[missing_value_cols].isna().any(axis=1),
        "missing market activity fields",
    )

    metadata = metadata if metadata is not None else pd.DataFrame()
    if metadata.empty:
        message = "metadata unavailable; cannot validate symbols or listing dates"
        if allow_missing_metadata:
            warnings.append(message)
        else:
            failures.append(message)
    else:
        meta_symbols = set(metadata["symbol"].astype(str))
        missing_meta = ~df["symbol"].astype(str).isin(meta_symbols)
        _mark_rejected(rejection_reasons, missing_meta, "symbol missing metadata")
        if missing_meta.any():
            failures.append(f"symbols missing metadata: {int(missing_meta.sum())} rows")

        if "listing_date" in metadata.columns:
            listings = metadata[["symbol", "listing_date"]].dropna()
            merged = df[["symbol", "date"]].merge(listings, on="symbol", how="left")
            before_listing = merged["listing_date"].notna() & (merged["date"] < merged["listing_date"])
            _mark_rejected(rejection_reasons, before_listing, "price date predates listing date")
            if before_listing.any():
                failures.append(f"rows before listing date: {int(before_listing.sum())}")

    digest = market_digest(df)
    previous_manifest = previous_manifest or {}
    previous_digest = previous_manifest.get("market_digest")
    previous_date = previous_manifest.get("target_date")
    if previous_digest and previous_digest == digest and previous_date != target_date.isoformat():
        failures.append(
            "full-market digest repeats previous accepted trading day "
            f"({previous_date}); requires independent confirmation"
        )

    low_variation_symbols: list[str] = []
    for symbol, group in df.groupby("symbol"):
        if len(group) >= low_variation_min_rows and group["close"].nunique(dropna=True) <= low_variation_max_distinct_closes:
            low_variation_symbols.append(str(symbol))
    if low_variation_symbols:
        failures.append(
            "per-symbol close variation too low over long period: "
            + ", ".join(sorted(low_variation_symbols)[:20])
        )

    rejected_mask = pd.Series([bool(reason_list) for reason_list in rejection_reasons], index=df.index)
    rejected = df.loc[rejected_mask].copy()
    if rejected.empty:
        rejected = pd.DataFrame(columns=[*CANONICAL_OHLCV_COLUMNS, "rejection_reason"])
    else:
        rejected["rejection_reason"] = [
            "; ".join(reason_list) for reason_list in rejection_reasons if reason_list
        ]
    accepted = df.loc[~rejected_mask].copy()
    accepted["date"] = accepted["date"].astype(str)
    accepted["source_timestamp"] = accepted["source_timestamp"].astype(str)
    accepted["validation_status"] = "accepted"
    accepted["validation_warnings"] = "; ".join(warnings)

    row_count = int(len(df))
    accepted_count = int(len(accepted))
    rejected_count = int(len(rejected))
    if rejected_count:
        failures.append(f"rejected OHLCV rows: {rejected_count}")

    metrics = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "target_date": target_date.isoformat(),
        "row_count": row_count,
        "accepted_rows": accepted_count,
        "rejected_rows": rejected_count,
        "symbols": int(df["symbol"].nunique()),
        "accepted_symbols": int(accepted["symbol"].nunique()) if not accepted.empty else 0,
        "market_digest": digest,
        "duplicate_symbol_date_rows": int(duplicate_mask.sum()),
        "source_ohlc_invalid_rows": repaired_count + int(negative_prices.sum()),
        "ohlc_repaired_rows": repaired_count,
        "ohlc_invalid_rows": int(negative_prices.sum()),
        "missing_activity_rate": missing_value_rate,
        "warnings": warnings,
        "failures": failures,
    }
    return ValidationResult(accepted=accepted, rejected=rejected, failures=failures, warnings=warnings, metrics=metrics)


def write_validation_outputs(
    result: ValidationResult,
    *,
    output_dir: Path,
    source_name: str,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "quality_summary.json").write_text(json.dumps(result.metrics, indent=2) + "\n")
    result.rejected.to_csv(output_dir / "rejected_records.csv", index=False)

    lines = [
        "# OHLCV Validation Report",
        "",
        f"Source: `{source_name}`",
        f"Target date: `{result.metrics.get('target_date')}`",
        f"Generated: `{result.metrics.get('generated_at_utc')}`",
        "",
        "## Summary",
        "",
        f"- Rows: {result.metrics.get('row_count', 0):,}",
        f"- Accepted: {result.metrics.get('accepted_rows', 0):,}",
        f"- Rejected: {result.metrics.get('rejected_rows', 0):,}",
        f"- Symbols: {result.metrics.get('symbols', 0):,}",
        f"- Market digest: `{result.metrics.get('market_digest', '')}`",
        "",
        "## Failures",
        "",
    ]
    lines.extend([f"- {failure}" for failure in result.failures] or ["- none"])
    lines.extend(["", "## Warnings", ""])
    lines.extend([f"- {warning}" for warning in result.warnings] or ["- none"])
    (output_dir / "validation_report.md").write_text("\n".join(lines) + "\n")
