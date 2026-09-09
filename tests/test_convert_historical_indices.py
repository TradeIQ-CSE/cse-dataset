from __future__ import annotations

from datetime import date
from pathlib import Path
import tempfile
import unittest

import pandas as pd

import scripts.convert_historical_indices as indices


# The daily index workbook shape: a title row, then labels stacked across two
# rows because ASPI and MPI sit one row below the sector names.
INDEX_HEADER = [
    "Market Indices - Daily ,,,",
    ",,,S&P Sri Lanka 20",
    ",All Share Price Index,Milanka Price Index,",
]


def write_index_file(path: Path, rows: list[str], header: list[str] | None = None) -> Path:
    path.write_text("\n".join((header or INDEX_HEADER) + rows) + "\n", encoding="utf-8")
    return path


class CombineHeaderTests(unittest.TestCase):
    def test_stacked_header_maps_each_series_to_its_own_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_index_file(
                Path(tmp) / "07Market_Indices_-_Daily__Index.csv",
                ["2013-01-02 00:00:00,5800.12,900.5,3100.75"],
            )
            frame, _ = indices.extract_index_records(path)
        by_name = dict(zip(frame["index_name"], frame["close"]))
        # Reading the labels from a single header row puts SL20's value under
        # MPI, so these three assertions fail together on that mistake.
        self.assertEqual(by_name["ASPI"], 5800.12)
        self.assertEqual(by_name["MPI"], 900.5)
        self.assertEqual(by_name["SL20"], 3100.75)

    def test_normalize_label_folds_the_renamed_sl20_header(self) -> None:
        self.assertEqual(
            indices.normalize_label("S&P Sri Lanka 20 Index"),
            indices.normalize_label("S&P Sri Lanka 20"),
        )


class SegmentTests(unittest.TestCase):
    """The workbook restarts its header when CSE switched to GICS sectors."""

    def _segmented_file(self, tmp: str) -> Path:
        return write_index_file(
            Path(tmp) / "07Market_Indices_-_Daily__Index.csv",
            [
                "2019-12-31 00:00:00,6129.21,0,2936.96",
                ',,,,"The CSE has adopted the Global Industry Classification Standard"',
                "Date,All Share Price Index,,S&P Sri Lanka 20 Index",
                "2020-01-02 00:00:00,6108.53,-2.83,2929.09",
            ],
        )

    def test_values_after_the_header_break_use_the_new_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frame, warnings = indices.extract_index_records(self._segmented_file(tmp))
        after = frame[frame["date"] == date(2020, 1, 2)]
        by_name = dict(zip(after["index_name"], after["close"]))
        self.assertEqual(by_name["ASPI"], 6108.53)
        self.assertEqual(by_name["SL20"], 2929.09)
        self.assertTrue(any("header segments" in item for item in warnings))

    def test_unlabelled_column_after_the_break_is_skipped_not_read_as_mpi(self) -> None:
        # Column 2 carries a percent change after the break, under no label.
        # Carrying the first segment's labels forward would store -2.83 as an
        # MPI index level.
        with tempfile.TemporaryDirectory() as tmp:
            frame, warnings = indices.extract_index_records(self._segmented_file(tmp))
        after = frame[frame["date"] == date(2020, 1, 2)]
        self.assertNotIn("MPI", set(after["index_name"]))
        self.assertNotIn(-2.83, set(frame["close"]))
        self.assertTrue(any("no header label" in item for item in warnings))


class CellHandlingTests(unittest.TestCase):
    def test_blank_and_not_published_markers_produce_no_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_index_file(
                Path(tmp) / "07Market_Indices_-_Daily__Index.csv",
                ["2013-01-02 00:00:00,5800.12,-,"],
            )
            frame, _ = indices.extract_index_records(path)
        names = set(frame["index_name"])
        self.assertEqual(names, {"ASPI"})
        # A discontinued series is absent, not zero.
        self.assertNotIn(0.0, set(frame["close"]))

    def test_excel_serial_date_is_parsed(self) -> None:
        self.assertEqual(indices.parse_archive_date("40428"), date(2010, 9, 7))

    def test_plain_integer_outside_the_serial_range_is_not_a_date(self) -> None:
        self.assertIsNone(indices.parse_archive_date("5"))


def candidates(rows: list[dict]) -> pd.DataFrame:
    base = {
        "source": indices.SOURCE_NAME,
        "raw_payload_hash": "a" * 64,
    }
    return pd.DataFrame(
        [{**base, "source_timestamp": row["date"], **row} for row in rows]
    )


class ValidationTests(unittest.TestCase):
    def test_valid_row_is_accepted(self) -> None:
        result = indices.validate_historical_index_records(
            candidates([{"date": date(2015, 6, 1), "index_name": "ASPI", "close": 7000.0}])
        )
        self.assertEqual(len(result.accepted), 1)
        self.assertTrue(result.passed)

    def test_zero_close_is_rejected(self) -> None:
        result = indices.validate_historical_index_records(
            candidates([{"date": date(2015, 6, 1), "index_name": "MPI", "close": 0.0}])
        )
        self.assertEqual(len(result.accepted), 0)
        self.assertIn("non-positive", result.rejected["rejection_reason"].iloc[0])

    def test_sl20_row_before_inception_is_rejected(self) -> None:
        # A column-shift bug shows up as SL20 values in years the index did
        # not exist.
        result = indices.validate_historical_index_records(
            candidates([{"date": date(2005, 1, 3), "index_name": "SL20", "close": 2500.0}])
        )
        self.assertEqual(len(result.accepted), 0)
        self.assertIn("inception", result.rejected["rejection_reason"].iloc[0])

    def test_sl20_row_on_inception_day_is_accepted(self) -> None:
        result = indices.validate_historical_index_records(
            candidates([{"date": date(2012, 6, 27), "index_name": "SL20", "close": 2500.0}])
        )
        self.assertEqual(len(result.accepted), 1)

    def test_row_after_official_coverage_end_is_rejected(self) -> None:
        result = indices.validate_historical_index_records(
            candidates([{"date": date(2026, 1, 2), "index_name": "ASPI", "close": 15000.0}])
        )
        self.assertEqual(len(result.accepted), 0)
        self.assertIn("coverage end", result.rejected["rejection_reason"].iloc[0])

    def test_identical_repeated_row_is_collapsed_to_one_accepted_row(self) -> None:
        result = indices.validate_historical_index_records(
            candidates(
                [
                    {"date": date(2010, 6, 30), "index_name": "ASPI", "close": 4612.46},
                    {"date": date(2010, 6, 30), "index_name": "ASPI", "close": 4612.46},
                ]
            )
        )
        self.assertEqual(len(result.accepted), 1)
        self.assertEqual(len(result.rejected), 0)
        self.assertTrue(any("collapsed" in item for item in result.warnings))

    def test_conflicting_repeated_row_is_rejected(self) -> None:
        result = indices.validate_historical_index_records(
            candidates(
                [
                    {"date": date(2010, 3, 12), "index_name": "ASTRI", "close": 4472.57},
                    {"date": date(2010, 3, 12), "index_name": "ASTRI", "close": 4487.20},
                ]
            )
        )
        self.assertEqual(len(result.accepted), 0)
        self.assertEqual(len(result.rejected), 2)

    def test_a_repeated_value_does_not_rescue_a_disputed_date(self) -> None:
        # Closes A, A and B for one date and index. Checking "duplicate on
        # identity but not on close" lets the two A rows mask each other, so
        # one A is accepted on a date the source disagrees about.
        result = indices.validate_historical_index_records(
            candidates(
                [
                    {"date": date(2010, 3, 12), "index_name": "ASTRI", "close": 4472.57},
                    {"date": date(2010, 3, 12), "index_name": "ASTRI", "close": 4472.57},
                    {"date": date(2010, 3, 12), "index_name": "ASTRI", "close": 4487.20},
                ]
            )
        )
        self.assertEqual(len(result.accepted), 0)

    def test_source_timestamp_must_match_the_row_date(self) -> None:
        frame = candidates([{"date": date(2015, 6, 1), "index_name": "ASPI", "close": 7000.0}])
        frame.loc[0, "source_timestamp"] = date(2015, 6, 2)
        result = indices.validate_historical_index_records(frame)
        self.assertEqual(len(result.accepted), 0)
        self.assertIn("source timestamp", result.rejected["rejection_reason"].iloc[0])

    def test_accepted_rows_carry_the_contract_columns(self) -> None:
        result = indices.validate_historical_index_records(
            candidates([{"date": date(2015, 6, 1), "index_name": "ASPI", "close": 7000.0}])
        )
        contract = indices.FAMILY_CONTRACTS["indices"]
        for column in contract.canonical_columns:
            self.assertIn(column, result.accepted.columns)


if __name__ == "__main__":
    unittest.main()
