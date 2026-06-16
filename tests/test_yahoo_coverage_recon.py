from __future__ import annotations

import unittest

from scripts.yahoo_coverage_recon import cse_symbol_to_yahoo, invalid_ohlc_count


class YahooCoverageReconTests(unittest.TestCase):
    def test_cse_symbol_to_yahoo_uses_dash_share_type_pattern(self) -> None:
        self.assertEqual(cse_symbol_to_yahoo("COMB.N0000"), "COMB-N0000.CM")
        self.assertEqual(cse_symbol_to_yahoo("AGST.X0000"), "AGST-X0000.CM")

    def test_invalid_ohlc_count_flags_bounds_and_missing_values(self) -> None:
        rows = [
            {"open": 10, "high": 11, "low": 9, "close": 10.5},
            {"open": 10, "high": 9, "low": 8, "close": 10},
            {"open": 10, "high": None, "low": 8, "close": 9},
        ]

        self.assertEqual(invalid_ohlc_count(rows), 2)


if __name__ == "__main__":
    unittest.main()
