from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path

import scripts.fill_missing_metadata as fm

HEADER = (
    "symbol,company_name,sector,board,delisted,delisting_date,listing_date,isin,market_cap,"
    "shares_outstanding,par_value,base_ticker,share_type,yahoo_ticker"
)
AAF_ROW = "AAF.N0000,ASIA ASSET FINANCE PLC,Unknown,Main,False,,01/JAN/1984,LK0001N00004,1.0,5110560,1.0,AAF,Voting,AAF.CM"


class FillMissingMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.metadata = self.root / "company_metadata.csv"
        self.metadata.write_text(f"{HEADER}\n{AAF_ROW}\n")
        self.fetched: list[str] = []

    def fetch(self, replies: dict[str, dict]):
        def fetch_info(symbol: str) -> dict:
            self.fetched.append(symbol)
            return replies.get(symbol, {})

        return fetch_info

    def write_accepted(self, day: str, symbols: list[str]) -> None:
        path = self.root / "accepted" / day / "cse_historical_daily_share_prices" / "canonical_ohlcv.csv"
        path.parent.mkdir(parents=True)
        path.write_text("date,symbol\n" + "".join(f"{day},{symbol}\n" for symbol in symbols))

    def test_accepted_symbols_are_read_inside_the_window_only(self) -> None:
        self.write_accepted("2016-12-30", ["OLD.N0000"])
        self.write_accepted("2017-01-02", ["AAF.N0000", "AINV.N0000"])

        symbols = fm.accepted_symbols(self.root / "accepted", date(2017, 1, 1), date(2025, 12, 31))

        self.assertEqual(symbols, {"AAF.N0000", "AINV.N0000"})

    def test_delisted_symbol_is_filled_from_the_api(self) -> None:
        reply = {"name": "ADAM INVESTMENTS PLC", "isin": "LK0999N00001", "quantityIssued": 1000, "issueDate": "01/JAN/2010"}

        columns, rows, added, unresolved = fm.fill_missing_metadata(
            self.metadata, {"AAF.N0000", "AINV.N0000"}, fetch_info=self.fetch({"AINV.N0000": reply}), active=set()
        )

        self.assertEqual(unresolved, [])
        self.assertEqual(len(added), 1)
        row = added[0]
        self.assertEqual(list(row), columns)
        self.assertEqual(row["symbol"], "AINV.N0000")
        self.assertEqual(row["company_name"], "ADAM INVESTMENTS PLC")
        self.assertEqual(row["delisted"], "True")
        self.assertEqual(row["isin"], "LK0999N00001")
        self.assertEqual(row["shares_outstanding"], "1000")

    def test_listing_date_is_never_written(self) -> None:
        reply = {"name": "ASIA ASSET FINANCE PLC", "issueDate": "12/JAN/2012"}

        _, _, added, _ = fm.fill_missing_metadata(
            self.metadata, {"AAF.R0000"}, fetch_info=self.fetch({"AAF.R0000": reply}), active=set()
        )

        self.assertEqual(added[0]["listing_date"], "")

    def test_symbol_still_listed_is_not_marked_delisted(self) -> None:
        reply = {"name": "ASIA ASSET FINANCE PLC"}

        _, _, added, _ = fm.fill_missing_metadata(
            self.metadata, {"AAF.R0000"}, fetch_info=self.fetch({"AAF.R0000": reply}), active={"AAF.R0000"}
        )

        self.assertEqual(added[0]["delisted"], "False")

    def test_zero_issued_is_written_as_unknown(self) -> None:
        reply = {"name": "ASIA ASSET FINANCE PLC", "quantityIssued": 0}

        _, _, added, _ = fm.fill_missing_metadata(
            self.metadata, {"AAF.R0000"}, fetch_info=self.fetch({"AAF.R0000": reply}), active=set()
        )

        self.assertEqual(added[0]["shares_outstanding"], "")

    def test_existing_rows_are_left_alone_and_not_fetched(self) -> None:
        with self.metadata.open(newline="") as handle:
            before = list(csv.DictReader(handle))

        _, rows, added, _ = fm.fill_missing_metadata(
            self.metadata, {"AAF.N0000"}, fetch_info=self.fetch({}), active=set()
        )

        self.assertEqual(rows, before)
        self.assertEqual(added, [])
        self.assertEqual(self.fetched, [])

    def test_symbol_the_api_cannot_name_takes_the_official_short_name(self) -> None:
        _, _, added, unresolved = fm.fill_missing_metadata(
            self.metadata,
            {"CSEC.N0000"},
            fetch_info=self.fetch({}),
            active=set(),
            fallback_names={"CSEC.N0000": "DUNAMIS CAPITAL"},
        )

        self.assertEqual(unresolved, [])
        self.assertEqual(added[0]["company_name"], "DUNAMIS CAPITAL")

    def test_api_name_is_preferred_over_the_short_name(self) -> None:
        _, _, added, _ = fm.fill_missing_metadata(
            self.metadata,
            {"AINV.N0000"},
            fetch_info=self.fetch({"AINV.N0000": {"name": "ADAM INVESTMENTS PLC"}}),
            active=set(),
            fallback_names={"AINV.N0000": "ADAM INVEST"},
        )

        self.assertEqual(added[0]["company_name"], "ADAM INVESTMENTS PLC")

    def test_symbol_named_nowhere_is_unresolved(self) -> None:
        _, _, added, unresolved = fm.fill_missing_metadata(
            self.metadata, {"GONE.N0000"}, fetch_info=self.fetch({}), active=set(), fallback_names={}
        )

        self.assertEqual(added, [])
        self.assertEqual(unresolved, ["GONE.N0000"])

    def test_short_names_come_from_the_official_price_files(self) -> None:
        header = (
            "COMPANY ID, MAIN TYPE ,SUB TYPE, SHORT NAME ,TRADING DATE, PRICE HIGH (Rs.), PRICE LOW (Rs.),"
            "CLOSE PRICE (Rs.),OPEN PRICE (Rs.),TRADE VOLUME (No.) ,SHARE VOLUME (No.) ,TURNOVER (Rs.)"
        )
        older = self.root / "2017.csv"
        older.write_text(
            f'"DAILY HIGH, LOW AND CLOSING PRICES 2017",,,,,,,,,,,\n{header}\n'
            "CSEC,N,0,DUNAMIS CAP,2017-01-02,1,1,1,1,1,1,1\n"
            "AFS,R,0000,ALPHA FIRE,2017-01-02,1,1,1,1,1,1,1\n"
        )
        newer = self.root / "2019.csv"
        newer.write_text(f"TITLE,,,,,,,,,,,\n{header}\nCSEC,N,0000,DUNAMIS CAPITAL,29-OCT-19,1,1,1,1,1,1,1\n")

        names = fm.official_short_names([older, newer])

        self.assertEqual(names, {"CSEC.N0000": "DUNAMIS CAPITAL", "AFS.R0000": "ALPHA FIRE"})


if __name__ == "__main__":
    unittest.main()
