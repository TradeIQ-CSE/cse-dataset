from __future__ import annotations

import argparse
import importlib
import json
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch

# 02_collect_prices imports its sibling modules by bare name.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
collect = importlib.import_module("02_collect_prices")
collect_metadata = importlib.import_module("scripts.01_collect_metadata")
from ohlcv_sources import COLOMBO_TZ, CSETradeSummaryCurrentAdapter  # noqa: E402

FRIDAY = date(2026, 8, 28)


def trade_row(symbol: str, day: date, **fields) -> dict:
    last_traded = datetime(day.year, day.month, day.day, 11, tzinfo=COLOMBO_TZ)
    row = {
        "symbol": symbol,
        "open": 10.0,
        "high": 11.0,
        "low": 9.5,
        "closingPrice": 10.5,
        "sharevolume": 100,
        "turnover": 1050.0,
        "tradevolume": 3,
        "lastTradedTime": int(last_traded.timestamp() * 1000),
    }
    row.update(fields)
    return row


def trade_summary(rows: list[dict]) -> Mock:
    response = Mock()
    response.json.return_value = {"reqTradeSummery": rows}
    response.raise_for_status.return_value = None
    return response


class TradeSummaryAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.raw_root = Path(tmp.name)
        self.adapter = CSETradeSummaryCurrentAdapter()

    def fetch(self, rows: list[dict], requested: date):
        with patch("requests.post", return_value=trade_summary(rows)):
            return self.adapter.fetch_for_date(requested, self.raw_root)

    def test_snapshot_is_dated_by_its_rows_not_the_clock(self) -> None:
        # The run for Friday started after midnight, on Saturday.
        fetched = self.fetch([trade_row("AAA.N0000", FRIDAY), trade_row("BBB.N0000", FRIDAY)], date(2026, 8, 29))
        self.assertEqual(fetched.observed_source_date, FRIDAY)

    def test_snapshot_mixing_days_has_no_session(self) -> None:
        thursday = date(2026, 8, 27)
        rows = [trade_row("AAA.N0000", FRIDAY), trade_row("BBB.N0000", FRIDAY), trade_row("CCC.N0000", thursday)]
        self.assertIsNone(self.fetch(rows, FRIDAY).observed_source_date)

    def test_zero_activity_is_a_value(self) -> None:
        # CINS.X0000 on 2026-08-25: a crossing trade only, so no regular volume.
        row = trade_row("CINS.X0000", FRIDAY, sharevolume=0, tradevolume=0, turnover=0.0)
        fetched = self.fetch([row], FRIDAY)
        records = self.adapter.normalize(fetched.payload, fetched)
        self.assertEqual(records.loc[0, ["volume", "turnover", "trades"]].tolist(), [0.0, 0.0, 0.0])


class CollectDailyOhlcvTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.metadata = self.root / "data/processed/company_metadata.csv"
        self.metadata.parent.mkdir(parents=True)
        self.metadata.write_text("symbol,company_name,listing_date\nAAA.N0000,A PLC,\nBBB.N0000,B PLC,\n")
        self.result_path = self.root / "data/run/daily_result.json"
        paths = patch.multiple(
            collect,
            ROOT=self.root,
            RAW_ROOT=self.root / "data/raw/ohlcv/source_payloads",
            ACCEPTED_ROOT=self.root / "data/raw/ohlcv/accepted",
            PROCESSED_ROOT=self.root / "data/processed/daily_ohlcv",
            VALIDATION_ROOT=self.root / "data/processed/validation/ohlcv",
            MANIFEST_ROOT=self.root / "data/raw/ohlcv/manifests",
            DEFAULT_METADATA=self.metadata,
        )
        paths.start()
        self.addCleanup(paths.stop)

    def run_collect(self, target_date: str | None) -> dict:
        args = argparse.Namespace(
            target_date=target_date,
            source="cse_trade_summary_current",
            metadata_path=str(self.metadata),
            dry_run=False,
            allow_missing_metadata=False,
            ignore_previous_digest=False,
            result_manifest=str(self.result_path),
            missing_activity_threshold=0.0,
            allow_validation_failure=True,
        )
        rows = [trade_row("AAA.N0000", FRIDAY), trade_row("BBB.N0000", FRIDAY)]
        with patch("requests.post", return_value=trade_summary(rows)):
            collect.collect_daily_ohlcv(args)
        return json.loads(self.result_path.read_text())

    def test_undated_run_files_the_session_it_captured(self) -> None:
        result = self.run_collect(None)
        self.assertEqual((result["status"], result["target_date"]), ("accepted", "2026-08-28"))
        self.assertTrue((self.root / result["accepted_path"]).exists())
        self.assertTrue((self.root / "data/raw/ohlcv/source_payloads/2026-08-28").is_dir())

    def test_dated_run_refuses_a_different_session(self) -> None:
        # On a holiday Monday tradeSummary still serves Friday.
        result = self.run_collect("2026-08-31")
        self.assertEqual((result["status"], result["target_date"]), ("rejected", "2026-08-31"))
        self.assertFalse((self.root / "data/raw/ohlcv/accepted").exists())


class CollectMetadataTests(unittest.TestCase):
    def test_rights_lines_are_collected(self) -> None:
        listed = [{"symbol": s} for s in ("COMB.N0000", "HNBF.R0000", "BOC.D0000", "AFIN")]
        kept = [s["symbol"] for s in collect_metadata.tradable_securities(listed)]
        self.assertEqual(kept, ["COMB.N0000", "HNBF.R0000"])
        self.assertEqual(collect_metadata.SHARE_TYPES[collect_metadata.share_class("HNBF.R0000")], "Rights")


if __name__ == "__main__":
    unittest.main()
