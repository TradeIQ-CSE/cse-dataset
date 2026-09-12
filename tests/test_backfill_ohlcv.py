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

    def write_rows(self, count: int, *, differing_opens: int = 0) -> Path:
        """``count`` rows on one date whose open equals the close, except the last ``differing_opens``."""
        source = self.root / "prices.csv"
        rows = []
        for i in range(count):
            opening = "10.6" if i >= count - differing_opens else "10.5"
            rows.append(f"C{i:04d},N,0,COMPANY {i},2025-01-02,11,10,10.5,{opening},1,100,1050")
        write_source(source, rows)
        return source

    def test_an_open_that_repeats_the_close_on_every_row_is_dropped(self) -> None:
        records = backfill.normalize_daily_share_price_file(self.write_rows(backfill.OPEN_COPY_MIN_ROWS))

        self.assertTrue(records["open"].isna().all())
        self.assertTrue(records["close"].notna().all())

    def test_one_differing_open_keeps_the_column(self) -> None:
        records = backfill.normalize_daily_share_price_file(
            self.write_rows(backfill.OPEN_COPY_MIN_ROWS, differing_opens=1)
        )

        self.assertTrue(records["open"].notna().all())

    def test_a_small_file_keeps_an_open_equal_to_the_close(self) -> None:
        records = backfill.normalize_daily_share_price_file(self.write_rows(backfill.OPEN_COPY_MIN_ROWS - 1))

        self.assertTrue(records["open"].notna().all())

    def test_a_file_with_a_copied_open_is_accepted_without_one(self) -> None:
        result = backfill.run_backfill(
            source_path=self.write_rows(backfill.OPEN_COPY_MIN_ROWS),
            dry_run=False,
            allow_missing_metadata=True,
        )

        self.assertEqual(result.accepted_rows, backfill.OPEN_COPY_MIN_ROWS)
        accepted = pd.read_csv(
            backfill.ACCEPTED_ROOT / "2025-01-02/cse_historical_daily_share_prices/canonical_ohlcv.csv"
        )
        self.assertTrue(accepted["open"].isna().all())


class GroupedHighLowBlockTests(unittest.TestCase):
    def test_sub_type_label_split_after_any_letter_of_type(self) -> None:
        # 2013 splits the label as "Sub Ty,pe :  0049"; 2003/2004/2006-2009 split it as
        # "Sub T,ype :  0049" instead — a different split point that an earlier version of
        # this regex missed, silently defaulting the sub-type to "0000" and colliding
        # distinct securities (e.g. COMB's ordinary vs preference share blocks) onto one
        # symbol.
        source = Path(tempfile.mkstemp(suffix=".csv")[1])
        self.addCleanup(source.unlink)
        source.write_text(
            "\n".join(
                [
                    "Company Id :,COMB,Security,Type :  P,Sub Ty,pe :  0004,,,,,",
                    "Short Name :,COMBANK PREF,,,,,,,,,",
                    "Day,Date High,High,Date Low,Low,Closing,Trades(No.),Shares(No.),Turnover(Rs.),Last Traded,Days Traded",
                    "2013-01-02 00:00:00,2013-01-02 00:00:00,100,2013-01-02 00:00:00,98,99,2,1000,99000,2013-01-02 00:00:00,1",
                    "Company Id :,COMB,Security,Type :  D,Sub T,ype :  0016,,,,,",
                    "Short Name :,COMBANK,,,,,,,,,",
                    "Day,Date High,High,Date Low,Low,Closing,Trades(No.),Shares(No.),Turnover(Rs.),Last Traded,Days Traded",
                    "2013-01-02 00:00:00,2013-01-02 00:00:00,120,2013-01-02 00:00:00,118,119,3,2000,238000,2013-01-02 00:00:00,1",
                ]
            )
            + "\n"
        )

        records = backfill.normalize_grouped_high_low_file(source)

        self.assertEqual(
            sorted(records["symbol"]),
            ["COMB.D0016", "COMB.P0004"],
        )


if __name__ == "__main__":
    unittest.main()
