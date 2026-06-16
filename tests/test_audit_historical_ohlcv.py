from __future__ import annotations

import unittest

import pandas as pd

import scripts.audit_historical_ohlcv as audit


class HistoricalOHLCVAuditTests(unittest.TestCase):
    def test_parses_flat_full_ohlcv_and_preserves_blanks(self) -> None:
        raw = pd.DataFrame(
            [
                ["title", None, None, None, None, None, None, None, None, None, None, None],
                [
                    "COMPANY ID",
                    "MAIN TYPE",
                    "SUB TYPE",
                    "SHORT NAME",
                    "TRADING DATE",
                    "PRICE HIGH (Rs.)",
                    "PRICE LOW (Rs.)",
                    "CLOSE PRICE (Rs.)",
                    "OPEN PRICE (Rs.)",
                    "TRADE VOLUME (No.)",
                    "SHARE VOLUME (No.)",
                    "TURNOVER (Rs.)",
                ],
                ["AAF", "N", "0", "ASIA ASSET", "2025-01-02", "", "26", "26.9", "26.9", "32", "27296", "730958"],
            ]
        )

        parsed = audit.parse_flat_ohlcv(raw)

        self.assertEqual(parsed.schema, "flat_ohlcv")
        self.assertEqual(parsed.records.iloc[0]["symbol"], "AAF.N0000")
        self.assertTrue(pd.isna(parsed.records.iloc[0]["high"]))
        self.assertIn("high", parsed.present_fields)

    def test_classifies_flat_high_low_close_without_open_as_structural_gap(self) -> None:
        raw = pd.DataFrame(
            [
                ["title", None, None, None, None, None, None, None, None, None, None],
                [
                    "COMPANY ID",
                    "MAIN TYPE",
                    "SUB TYPE",
                    "SHORT NAME",
                    "TRADING DATE",
                    "PRICE HIGH (Rs.)",
                    "PRICE LOW (Rs.)",
                    "CLOSE PRICE (Rs.)",
                    "TRADE VOLUME (No.)",
                    "SHARE VOLUME (No.)",
                    "TURNOVER (Rs.)",
                ],
                ["AAF", "N", "0", "ASIA ASSET", "2016-01-04", "1.7", "1.6", "1.7", "12", "126036", "201761.1"],
            ]
        )

        parsed = audit.parse_flat_ohlcv(raw)
        summary = audit.audit_parsed(audit.ROOT / "fixture.csv", parsed)

        self.assertEqual(parsed.schema, "flat_high_low_close_no_open")
        self.assertNotIn("open", parsed.present_fields)
        self.assertEqual(summary["missing_open"], 1)
        self.assertEqual(summary["blank_present_fields"], "{}")

    def test_parses_grouped_high_low_close_without_open(self) -> None:
        raw = pd.DataFrame(
            [
                ["Company Id :  AAIC", "Security", "Type :  N", "Sub Type :  0000", None, None, None, None, None],
                ["Day", "Date High", "High", "Date Low", "Low", "Closing", "Trades(No.)", "Shares(No.)", "Turnover(Rs.)"],
                ["2002-01-03", "2002-01-03", "11", "2002-01-03", "10.75", "11", "2", "2000", "22000"],
            ]
        )

        parsed = audit.parse_grouped_high_low(raw)

        self.assertEqual(parsed.schema, "grouped_high_low_close_no_open")
        self.assertEqual(parsed.records.iloc[0]["symbol"], "AAIC.N0000")
        self.assertNotIn("open", parsed.present_fields)
        self.assertEqual(float(parsed.records.iloc[0]["low"]), 10.75)

    def test_parses_close_only_blocks(self) -> None:
        raw = pd.DataFrame(
            [
                ["Date", "Security ID", "Closing Price", "Share Volume", "2001-01-02", "HNB-X-0000", "36.25"],
                ["2001-03-21", "ABAN-N-000", "25.5", "100", "2001-03-29", "HNB-D-0014", "82.5"],
            ]
        )

        parsed = audit.parse_close_only(raw)

        self.assertEqual(parsed.schema, "close_only")
        self.assertEqual(len(parsed.records), 3)
        self.assertIn("volume", parsed.present_fields)
        self.assertIn("HNB.X0000", set(parsed.records["symbol"]))


if __name__ == "__main__":
    unittest.main()
