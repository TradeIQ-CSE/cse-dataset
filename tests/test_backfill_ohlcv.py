from __future__ import annotations

from datetime import date
from pathlib import Path
import tempfile
import unittest

import pandas as pd

import scripts.backfill_ohlcv as backfill


def write_source(path: Path, rows: list[str]) -> None:
    path.write_text(
        "\n".join(
            [
                "DAILY HIGH, LOW AND CLOSING PRICES 2025",
                "COMPANY ID,MAIN TYPE,SUB TYPE,SHORT NAME,TRADING DATE,PRICE HIGH (Rs.),PRICE LOW (Rs.),CLOSE PRICE (Rs.),OPEN PRICE (Rs.),TRADE VOLUME (No.),SHARE VOLUME (No.),TURNOVER (Rs.)",
                *rows,
            ]
        )
        + "\n"
    )


class BackfillOHLCVTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.old_roots = (
            backfill.RAW_ROOT,
            backfill.ACCEPTED_ROOT,
            backfill.CANDIDATE_ROOT,
            backfill.VALIDATION_ROOT,
            backfill.BACKFILL_VALIDATION_ROOT,
        )
        backfill.RAW_ROOT = self.root / "raw_source"
        backfill.ACCEPTED_ROOT = self.root / "accepted"
        backfill.CANDIDATE_ROOT = self.root / "candidates"
        backfill.VALIDATION_ROOT = self.root / "validation"
        backfill.BACKFILL_VALIDATION_ROOT = self.root / "backfill_validation"

    def tearDown(self) -> None:
        (
            backfill.RAW_ROOT,
            backfill.ACCEPTED_ROOT,
            backfill.CANDIDATE_ROOT,
            backfill.VALIDATION_ROOT,
            backfill.BACKFILL_VALIDATION_ROOT,
        ) = self.old_roots
        self.tmpdir.cleanup()

    def test_normalizes_official_daily_share_price_rows(self) -> None:
        source = self.root / "2025.csv"
        write_source(
            source,
            ["AAF,N,0,ASIA ASSET,2025-01-02,27.2,26,26.9,26.9,32,27296,730958"],
        )

        records = backfill.normalize_daily_share_price_file(source)

        self.assertEqual(len(records), 1)
        self.assertEqual(records.iloc[0]["symbol"], "AAF.N0000")
        self.assertEqual(records.iloc[0]["date"], "2025-01-02")
        self.assertEqual(float(records.iloc[0]["close"]), 26.9)

    def test_accepts_valid_date_batch_and_writes_transaction(self) -> None:
        source = self.root / "valid.csv"
        write_source(
            source,
            [
                "AAF,N,0,ASIA ASSET,2025-01-02,27.2,26,26.9,26.9,32,27296,730958",
                "ABAN,N,0,ABANS,2025-01-02,180,175,178,176,9,1000,178000",
            ],
        )

        result = backfill.run_backfill(
            source_path=source,
            start_date=date(2025, 1, 2),
            end_date=date(2025, 1, 2),
            dry_run=False,
            allow_missing_metadata=True,
        )

        self.assertEqual(result.accepted_dates, 1)
        self.assertEqual(result.accepted_rows, 2)
        self.assertTrue(
            (
                backfill.ACCEPTED_ROOT
                / "2025-01-02/cse_historical_daily_share_prices/canonical_ohlcv.csv"
            ).exists()
        )

    def test_quarantines_entire_date_when_any_row_fails(self) -> None:
        source = self.root / "bad.csv"
        write_source(
            source,
            [
                "AAF,N,0,ASIA ASSET,2025-01-02,27.2,26,26.9,26.9,32,27296,730958",
                "ABAN,N,0,ABANS,2025-01-02,180,-1,178,176,9,1000,178000",
            ],
        )

        result = backfill.run_backfill(
            source_path=source,
            start_date=date(2025, 1, 2),
            end_date=date(2025, 1, 2),
            dry_run=False,
            allow_missing_metadata=True,
            allow_validation_failure=True,
        )

        self.assertEqual(result.accepted_dates, 0)
        self.assertEqual(result.quarantined_dates, 1)
        self.assertFalse(
            (
                backfill.ACCEPTED_ROOT
                / "2025-01-02/cse_historical_daily_share_prices/canonical_ohlcv.csv"
            ).exists()
        )
        rejected = pd.read_csv(
            backfill.VALIDATION_ROOT
            / "2025-01-02/cse_historical_daily_share_prices/rejected_records.csv"
        )
        self.assertTrue(any("negative OHLC price" in reason for reason in rejected["rejection_reason"]))


if __name__ == "__main__":
    unittest.main()
