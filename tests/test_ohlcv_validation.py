from __future__ import annotations

from datetime import date
import unittest

import pandas as pd

from scripts.ohlcv_validation import validate_ohlcv_records


def rows(target: str = "2026-05-28") -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": target,
                "symbol": "AAA.N0000",
                "open": 10.0,
                "high": 12.0,
                "low": 9.0,
                "close": 11.0,
                "volume": 1000,
                "turnover": 11000,
                "trades": 5,
                "source": "fixture",
                "source_priority": 10,
                "source_timestamp": target,
                "raw_payload_hash": "a" * 64,
                "validation_status": "candidate",
                "validation_warnings": "",
            }
        ]
    )


def metadata() -> pd.DataFrame:
    return pd.DataFrame([{"symbol": "AAA.N0000", "listing_date": date(2020, 1, 1)}])


class OHLCVValidationTests(unittest.TestCase):
    def test_date_mismatch_is_rejected(self) -> None:
        result = validate_ohlcv_records(
            rows("2026-05-27"),
            target_date=date(2026, 5, 28),
            metadata=metadata(),
        )
        self.assertFalse(result.passed)
        self.assertIn("rejected OHLCV rows: 1", result.failures)
        self.assertTrue(
            any("record date does not match target date" in reason for reason in result.rejected["rejection_reason"])
        )

    def test_repeated_snapshot_digest_is_rejected(self) -> None:
        records = rows()
        first = validate_ohlcv_records(records, target_date=date(2026, 5, 28), metadata=metadata())
        result = validate_ohlcv_records(
            records,
            target_date=date(2026, 5, 28),
            metadata=metadata(),
            previous_manifest={"target_date": "2026-05-27", "market_digest": first.metrics["market_digest"]},
        )
        self.assertFalse(result.passed)
        self.assertTrue(any("full-market digest repeats" in failure for failure in result.failures))

    def test_repairable_ohlc_is_accepted_with_source_values_preserved(self) -> None:
        records = rows()
        records.loc[0, "high"] = 8.0
        result = validate_ohlcv_records(records, target_date=date(2026, 5, 28), metadata=metadata())
        self.assertTrue(result.passed)
        self.assertEqual(result.metrics["ohlc_repaired_rows"], 1)
        self.assertEqual(float(result.accepted.iloc[0]["source_high"]), 8.0)
        self.assertEqual(float(result.accepted.iloc[0]["high"]), 11.0)

    def test_negative_ohlc_is_rejected_with_reason(self) -> None:
        records = rows()
        records.loc[0, "low"] = -1.0
        result = validate_ohlcv_records(records, target_date=date(2026, 5, 28), metadata=metadata())
        self.assertFalse(result.passed)
        self.assertIn("negative OHLC price", set(result.rejected["rejection_reason"]))

    def test_rows_before_listing_date_are_rejected(self) -> None:
        meta = pd.DataFrame([{"symbol": "AAA.N0000", "listing_date": date(2027, 1, 1)}])
        result = validate_ohlcv_records(rows(), target_date=date(2026, 5, 28), metadata=meta)
        self.assertFalse(result.passed)
        self.assertIn("price date predates listing date", set(result.rejected["rejection_reason"]))

    def test_low_variation_over_long_period_fails(self) -> None:
        records = pd.concat([rows() for _ in range(60)], ignore_index=True)
        result = validate_ohlcv_records(
            records,
            target_date=date(2026, 5, 28),
            metadata=metadata(),
            low_variation_min_rows=60,
        )
        self.assertFalse(result.passed)
        self.assertTrue(any("per-symbol close variation too low" in failure for failure in result.failures))


if __name__ == "__main__":
    unittest.main()
