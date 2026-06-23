from __future__ import annotations

from datetime import date, datetime
import tempfile
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

import pandas as pd

import scripts.forward_ingestion as forward_ingestion
from scripts.forward_ingestion import (
    CSEDailyReportOHLCVSource,
    MissingSourceError,
    YahooFinanceOHLCVSource,
    cse_cross_check_rejections,
    parse_single_report_date,
    is_after_cse_market_close,
    run_generic_family_ingestion,
    run_daily_report_ohlcv_ingestion,
    run_yahoo_ohlcv_ingestion,
    yahoo_source_rejections,
)
from scripts.ohlcv_sources import CSETradeSummaryCurrentAdapter


def write_report(path: Path, report_date: str = "2026-05-29") -> None:
    path.write_text(
        "\n".join(
            [
                "Colombo Stock Exchange Daily Market Report",
                f"Report Date: {report_date}",
                "Symbol,Open,High,Low,Close,Volume,Turnover,Trades",
                "AAA.N0000,10,12,9,11,1000,11000,5",
            ]
        )
        + "\n"
    )


def metadata(path: Path) -> None:
    pd.DataFrame([{"symbol": "AAA.N0000", "listing_date": "01/Jan/2020"}]).to_csv(path, index=False)


def yahoo_payload(symbol: str, timestamp: int, close: float = 11.0) -> dict:
    return {
        "chart": {
            "result": [
                {
                    "meta": {"symbol": symbol, "gmtoffset": 19800},
                    "timestamp": [timestamp],
                    "indicators": {
                        "quote": [
                            {
                                "open": [10.0],
                                "high": [12.0],
                                "low": [9.0],
                                "close": [close],
                                "volume": [1000],
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }


class ForwardIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.old_roots = (
            forward_ingestion.RAW_ROOT,
            forward_ingestion.PROCESSED_ROOT,
            forward_ingestion.VALIDATION_ROOT,
        )
        forward_ingestion.RAW_ROOT = self.root / "raw"
        forward_ingestion.PROCESSED_ROOT = self.root / "processed"
        forward_ingestion.VALIDATION_ROOT = self.root / "validation"
        self.metadata_path = self.root / "metadata.csv"
        metadata(self.metadata_path)

    def tearDown(self) -> None:
        (
            forward_ingestion.RAW_ROOT,
            forward_ingestion.PROCESSED_ROOT,
            forward_ingestion.VALIDATION_ROOT,
        ) = self.old_roots
        self.tmpdir.cleanup()

    def test_2026_daily_report_exact_date_produces_accepted_rows(self) -> None:
        report = self.root / "daily_report.csv"
        write_report(report, "2026-05-29")

        result = run_daily_report_ohlcv_ingestion(
            target_date=date(2026, 5, 29),
            source_file=report,
            metadata_path=self.metadata_path,
            raw_root=forward_ingestion.RAW_ROOT,
            allow_before_close=True,
        )

        self.assertTrue(result.passed)
        self.assertEqual(len(result.accepted), 1)
        self.assertEqual(result.accepted.iloc[0]["date"], "2026-05-29")
        self.assertTrue(
            (
                forward_ingestion.RAW_ROOT
                / "accepted/ohlcv/2026-05-29/cse_daily_report_pdf/canonical_ohlcv.csv"
            ).exists()
        )

    def test_2026_daily_report_date_mismatch_is_quarantined(self) -> None:
        report = self.root / "stale_daily_report.csv"
        write_report(report, "2026-05-28")

        result = run_daily_report_ohlcv_ingestion(
            target_date=date(2026, 5, 29),
            source_file=report,
            metadata_path=self.metadata_path,
            raw_root=forward_ingestion.RAW_ROOT,
            allow_before_close=True,
        )

        self.assertFalse(result.passed)
        self.assertTrue(any("source report date mismatch" in failure for failure in result.failures))
        self.assertFalse(
            (
                forward_ingestion.RAW_ROOT
                / "accepted/ohlcv/2026-05-29/cse_daily_report_pdf/canonical_ohlcv.csv"
            ).exists()
        )

    def test_missing_daily_report_quarantines_without_reusing_stale_files(self) -> None:
        with self.assertRaises(MissingSourceError):
            run_daily_report_ohlcv_ingestion(
                target_date=date(2026, 5, 29),
                source_file=self.root / "missing.csv",
                metadata_path=self.metadata_path,
                raw_root=forward_ingestion.RAW_ROOT,
                allow_before_close=True,
            )

        self.assertTrue(
            (
                forward_ingestion.VALIDATION_ROOT
                / "ohlcv/2026-05-29/cse_daily_report_pdf/forward_summary.json"
            ).exists()
        )
        self.assertFalse((forward_ingestion.RAW_ROOT / "accepted").exists())

    def test_trade_summary_current_adapter_rejects_historical_target_dates(self) -> None:
        records = pd.DataFrame(
            [
                {
                    "source_timestamp": "2026-05-29",
                    "symbol": "AAA.N0000",
                }
            ]
        )

        failures = CSETradeSummaryCurrentAdapter().validate_source_date(records, date(2026, 5, 28))

        self.assertTrue(any("source date mismatch" in failure for failure in failures))

    def test_yahoo_source_normalizes_chart_rows_to_cse_symbols(self) -> None:
        timestamp = int(datetime(2026, 5, 29, 9, 30, tzinfo=forward_ingestion.COLOMBO_TZ).timestamp())
        source = YahooFinanceOHLCVSource(symbols=["AAA.N0000"])
        raw = {
            "responses": {
                "AAA.N0000": {
                    "yahoo_symbol": "AAA-N0000.CM",
                    "status_code": 200,
                    "payload": yahoo_payload("AAA-N0000.CM", timestamp),
                }
            }
        }

        records = source.normalize("not json", report_date=date(2026, 5, 29), payload_hash="unused")
        self.assertTrue(records.empty)

        records = source.normalize(
            __import__("json").dumps(raw),
            report_date=date(2026, 5, 29),
            payload_hash="abc",
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records.iloc[0]["symbol"], "AAA.N0000")
        self.assertEqual(float(records.iloc[0]["close"]), 11.0)

    def test_cse_cross_check_rejects_close_mismatch(self) -> None:
        records = pd.DataFrame(
            [{"symbol": "AAA.N0000", "close": 11.0}, {"symbol": "BBB.N0000", "close": 20.0}]
        )
        snapshot = pd.DataFrame(
            [{"symbol": "AAA.N0000", "close": 11.0}, {"symbol": "BBB.N0000", "close": 21.0}]
        )

        reasons = cse_cross_check_rejections(
            records,
            snapshot,
            target_date=date(2026, 5, 29),
            observed_source_date=date(2026, 5, 29),
            tolerance=0.01,
        )

        self.assertEqual(reasons.iloc[0], "")
        self.assertIn("cross-check mismatch", reasons.iloc[1])

    def test_yahoo_source_rejects_non_positive_volume(self) -> None:
        records = pd.DataFrame([{"volume": 1000}, {"volume": 0}, {"volume": None}])

        reasons = yahoo_source_rejections(records)

        self.assertEqual(reasons.iloc[0], "")
        self.assertIn("non-positive volume", reasons.iloc[1])
        self.assertIn("non-positive volume", reasons.iloc[2])

    @patch("scripts.forward_ingestion.requests.post")
    @patch("scripts.forward_ingestion.requests.Session")
    def test_yahoo_ingestion_accepts_rows_only_after_cse_cross_check(self, mock_session: Mock, mock_post: Mock) -> None:
        timestamp = int(datetime(2026, 5, 29, 9, 30, tzinfo=forward_ingestion.COLOMBO_TZ).timestamp())

        class FakeYahooResponse:
            status_code = 200

            def json(self) -> dict:
                return yahoo_payload("AAA-N0000.CM", timestamp)

        fake_session = Mock()
        fake_session.get.return_value = FakeYahooResponse()
        mock_session.return_value = fake_session

        cse_response = Mock()
        cse_response.json.return_value = {
            "reqTradeSummery": [
                {
                    "symbol": "AAA.N0000",
                    "open": 10.0,
                    "high": 12.0,
                    "low": 9.0,
                    "closingPrice": 11.0,
                    "sharevolume": 1000,
                    "lastTradedTime": timestamp * 1000,
                }
            ]
        }
        cse_response.raise_for_status.return_value = None
        mock_post.return_value = cse_response

        result = run_yahoo_ohlcv_ingestion(
            target_date=date(2026, 5, 29),
            metadata_path=self.metadata_path,
            raw_root=forward_ingestion.RAW_ROOT,
            allow_before_close=True,
            missing_activity_threshold=1.0,
        )

        self.assertTrue(result.passed)
        self.assertEqual(len(result.accepted), 1)
        self.assertTrue(
            (
                forward_ingestion.RAW_ROOT
                / "accepted/ohlcv/2026-05-29/yahoo_finance_chart_candidate/canonical_ohlcv.csv"
            ).exists()
        )

    def test_report_parser_requires_single_report_date(self) -> None:
        report = self.root / "ambiguous_report.csv"
        report.write_text(
            "Report Date: 2026-05-29\nReport Date: 2026-05-28\n"
            "Symbol,Open,High,Low,Close,Volume,Turnover,Trades\n"
            "AAA.N0000,10,12,9,11,1000,11000,5\n"
        )

        with self.assertRaises(forward_ingestion.ReportDateError):
            CSEDailyReportOHLCVSource(source_file=report).fetch_for_date(
                date(2026, 5, 29), raw_root=forward_ingestion.RAW_ROOT
            )

    def test_pdf_text_style_symbol_rows_normalize_without_csv_commas(self) -> None:
        report = self.root / "daily_report_text.txt"
        report.write_text(
            "Colombo Stock Exchange Daily Market Report\n"
            "Report Date: 29 May 2026\n"
            "Symbol Open High Low Close Volume Turnover Trades\n"
            "AAA.N0000 10 12 9 11 1000 11000 5\n"
        )

        result = run_daily_report_ohlcv_ingestion(
            target_date=date(2026, 5, 29),
            source_file=report,
            metadata_path=self.metadata_path,
            raw_root=forward_ingestion.RAW_ROOT,
            allow_before_close=True,
        )

        self.assertTrue(result.passed)
        self.assertEqual(float(result.accepted.iloc[0]["close"]), 11.0)

    def test_cse_stock_market_daily_header_date_wins_over_other_dates(self) -> None:
        text = (
            "Thursday, 20 November, 2025\n"
            "Index Performance\n"
            "20-Nov-25\n"
            "Daily Indicative Exchange Rate Consumer Price Inflation (CCPI)\n"
            "Quarter Rate 7.52 SRR 2.00 Gold Price 1,251,942.00\n"
            "05-May-25\n"
        )

        self.assertEqual(parse_single_report_date(text), date(2025, 11, 20))

    def test_cse_stock_market_daily_sector_rows_are_not_treated_as_ohlcv(self) -> None:
        source = CSEDailyReportOHLCVSource()
        records = source.normalize(
            "Report Date: 2026-05-29\n"
            "BANKS 1,857.45 738,254,996.80 10,526,852.00 3054 16 17\n",
            report_date=date(2026, 5, 29),
            payload_hash="abc",
        )

        self.assertTrue(records.empty)

    @patch("scripts.forward_ingestion.requests.get")
    def test_default_cse_daily_report_http_403_is_quarantined(self, mock_get: Mock) -> None:
        response = Mock()
        response.status_code = 403
        response.content = b"<Error><Code>AccessDenied</Code></Error>"
        mock_get.return_value = response

        with self.assertRaises(MissingSourceError) as context:
            run_daily_report_ohlcv_ingestion(
                target_date=date(2026, 5, 29),
                metadata_path=self.metadata_path,
                raw_root=forward_ingestion.RAW_ROOT,
                allow_before_close=True,
                allow_missing_metadata=True,
            )

        self.assertIn("not found or not public", str(context.exception))
        self.assertTrue(
            (
                forward_ingestion.VALIDATION_ROOT
                / "ohlcv/2026-05-29/cse_daily_report_pdf/forward_summary.json"
            ).exists()
        )

    def test_schedule_gate_is_after_cse_market_close(self) -> None:
        before_close = datetime(2026, 5, 29, 14, 44, tzinfo=forward_ingestion.COLOMBO_TZ)
        after_close = datetime(2026, 5, 29, 14, 45, tzinfo=forward_ingestion.COLOMBO_TZ)

        self.assertFalse(is_after_cse_market_close(before_close))
        self.assertTrue(is_after_cse_market_close(after_close))

    def test_corporate_actions_accepts_date_bearing_dividend_rows(self) -> None:
        source = self.root / "corporate_actions.csv"
        source.write_text(
            "Report Date: 2026-05-29\n"
            "symbol,announcement_date,event_type,record_date,ex_date,payment_date,amount,currency\n"
            "AAA.N0000,2026-05-29,DIVIDEND,2026-06-02,2026-06-03,2026-06-20,1.25,LKR\n"
        )

        result = run_generic_family_ingestion(
            family="corporate_actions",
            target_date=date(2026, 5, 29),
            source_file=source,
            raw_root=forward_ingestion.RAW_ROOT,
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.accepted.iloc[0]["event_type"], "DIVIDEND")
        self.assertTrue(
            (
                forward_ingestion.RAW_ROOT
                / "accepted/corporate_actions/2026-05-29/official_corporate_actions_table/canonical_corporate_actions.csv"
            ).exists()
        )

    def test_generic_family_report_date_mismatch_is_quarantined(self) -> None:
        source = self.root / "macro_rates.csv"
        source.write_text(
            "Report Date: 2026-05-28\n"
            "date,metric,value,currency\n"
            "2026-05-29,USD_LKR_SPOT,300.1,USD\n"
        )

        result = run_generic_family_ingestion(
            family="macro_rates",
            target_date=date(2026, 5, 29),
            source_file=source,
            raw_root=forward_ingestion.RAW_ROOT,
        )

        self.assertFalse(result.passed)
        self.assertTrue(any("source report date mismatch" in failure for failure in result.failures))
        self.assertFalse(
            (
                forward_ingestion.RAW_ROOT
                / "accepted/macro_rates/2026-05-29/official_macro_rates_table/canonical_macro_rates.csv"
            ).exists()
        )

    def test_macro_rates_rejects_negative_exchange_rate(self) -> None:
        source = self.root / "negative_macro_rates.csv"
        source.write_text(
            "Report Date: 2026-05-29\n"
            "date,metric,value,currency\n"
            "2026-05-29,USD_LKR_SPOT,-300.1,USD\n"
        )

        result = run_generic_family_ingestion(
            family="macro_rates",
            target_date=date(2026, 5, 29),
            source_file=source,
            raw_root=forward_ingestion.RAW_ROOT,
        )

        self.assertFalse(result.passed)
        self.assertTrue(any("negative numeric field: value" in reason for reason in result.rejected["rejection_reason"]))

    def test_public_holdings_rejects_percentage_over_100(self) -> None:
        source = self.root / "public_holdings.csv"
        source.write_text(
            "Report Date: 2026-03-31\n"
            "report_date,symbol,public_holding_pct\n"
            "2026-03-31,AAA.N0000,120\n"
        )

        result = run_generic_family_ingestion(
            family="public_holdings",
            target_date=date(2026, 3, 31),
            source_file=source,
            raw_root=forward_ingestion.RAW_ROOT,
        )

        self.assertFalse(result.passed)
        self.assertTrue(any("percentage field exceeds 100" in reason for reason in result.rejected["rejection_reason"]))

    def test_generic_family_duplicate_identity_is_rejected(self) -> None:
        source = self.root / "indices.csv"
        source.write_text(
            "Report Date: 2026-05-29\n"
            "date,index_name,close\n"
            "2026-05-29,ASPI,18000\n"
            "2026-05-29,ASPI,18000\n"
        )

        result = run_generic_family_ingestion(
            family="indices",
            target_date=date(2026, 5, 29),
            source_file=source,
            raw_root=forward_ingestion.RAW_ROOT,
        )

        self.assertFalse(result.passed)
        self.assertTrue(any("duplicate indices candidate rows" in failure for failure in result.failures))


if __name__ == "__main__":
    unittest.main()
