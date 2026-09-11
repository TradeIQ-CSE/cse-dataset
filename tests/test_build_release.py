"""Release builder.

The fixture mirrors the real inputs on disk: backfill candidates for one listed
source file, per-date validation summaries, accepted canonical OHLCV files with
the extra columns the gates add, the pandas-written metadata file, and the
accepted index archive. Three sessions, the middle one quarantined.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

import scripts.build_release as br
from scripts.validate_artifact import validate_artifact

SESSIONS = ("2025-12-29", "2025-12-30", "2025-12-31")
PRICE_HASH = hashlib.sha256(b"fixture: official price file").hexdigest()
INDEX_HASH = hashlib.sha256(b"fixture: index workbook").hexdigest()
CANONICAL_COLUMNS = [
    "date", "symbol", "open", "high", "low", "close", "volume", "turnover", "trades",
    "source", "source_priority", "source_timestamp", "raw_payload_hash",
    "validation_status", "validation_warnings",
]
METADATA = """symbol,company_name,sector,board,delisted,delisting_date,listing_date,isin,market_cap,shares_outstanding,par_value,base_ticker,share_type,yahoo_ticker
AAF.N0000,ASIA ASSET FINANCE PLC,Unknown,Main,False,,01/JAN/1984,LK0001N00004,5592230280.0,5110560,1.0,AAF,Voting,AAF.CM
COMB.N0000,COMMERCIAL BANK OF CEYLON PLC,Unknown,Main,False,,,,,0,,COMB,Voting,
XTRA.N0000,NEVER TRADED PLC,Unknown,Main,False,,,,,,,XTRA,Voting,
"""


def price(day: str, symbol: str, **overrides: str) -> dict[str, str]:
    row = {
        "date": day, "symbol": symbol, "open": "12.5", "high": "12.8", "low": "12.4", "close": "12.6",
        "volume": "15000.0", "turnover": "189000.0", "trades": "12.0",
        "source": "cse_historical_daily_share_prices", "source_priority": "30", "source_timestamp": day,
        "raw_payload_hash": PRICE_HASH, "validation_status": "accepted", "validation_warnings": "",
    }
    row.update(overrides)
    return row


class ReleaseFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.output = root / "published"
        self.inputs = br.ReleaseInputs(
            sources_list=root / "release_sources.txt",
            source_root=root,
            candidate_root=root / "candidates",
            accepted_root=root / "accepted",
            validation_root=root / "validation",
            metadata_path=root / "company_metadata.csv",
            indices_path=root / "indices_historical.csv",
        )
        source = root / "official_2025.csv"
        source.write_text("fixture official price file\n")
        self.inputs.sources_list.write_text("# fixture\nofficial_2025.csv\n")
        self.candidates = self.inputs.candidate_root / br.sha256_file(source) / "canonical_ohlcv_candidates.csv"
        self.candidates.parent.mkdir(parents=True)
        # 2016-12-30 is outside the default window and must not become a session.
        days = ("2016-12-30", *SESSIONS)
        self.candidates.write_text("date,symbol\n" + "".join(f"{day},AAF.N0000\n" for day in days))

        self.accept("2025-12-29", [
            price("2025-12-29", "COMB.N0000", open="", volume="120400.0"),
            price("2025-12-29", "AAF.N0000"),
        ])
        self.quarantine("2025-12-30", rejected_rows=2)
        self.accept("2025-12-31", [price("2025-12-31", "AAF.N0000"), price("2025-12-31", "COMB.N0000")])
        self.inputs.metadata_path.write_text(METADATA)

        index_rows = []
        for day in SESSIONS:
            for code, close in (("ASPI", "21950.12"), ("SL20", "6210.4"), ("SL20TRI", "12751.01"),
                                ("ASTRI", "33638.72"), ("MTRI", "9000.5")):
                if day == "2025-12-29" and code == "SL20":
                    close = "3753.1907805221"
                index_rows.append(f"{day},{code},{close},cse_official_index_workbook,{day},{INDEX_HASH}")
        self.inputs.indices_path.write_text(
            "date,index_name,close,source,source_timestamp,raw_payload_hash\n" + "\n".join(index_rows) + "\n"
        )

    def accepted_path(self, day: str) -> Path:
        return self.inputs.accepted_root / day / "cse_historical_daily_share_prices" / "canonical_ohlcv.csv"

    def summary_path(self, day: str) -> Path:
        return self.inputs.validation_root / day / "cse_historical_daily_share_prices" / "quality_summary.json"

    def write_accepted(self, day: str, rows: list[dict[str, str]]) -> None:
        path = self.accepted_path(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CANONICAL_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)

    def accept(self, day: str, rows: list[dict[str, str]]) -> None:
        self.write_accepted(day, rows)
        self.write_summary(day, failures=[], rejected_rows=0)

    def quarantine(self, day: str, rejected_rows: int) -> None:
        self.write_summary(day, failures=[f"rejected OHLCV rows: {rejected_rows}"], rejected_rows=rejected_rows)

    def write_summary(self, day: str, *, failures: list[str], rejected_rows: int) -> None:
        path = self.summary_path(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"target_date": day, "rejected_rows": rejected_rows, "failures": failures}))

    def build(self, **overrides) -> br.ReleaseSummary:
        options = dict(
            inputs=self.inputs,
            output_dir=self.output,
            start=date(2017, 1, 1),
            end=date(2025, 12, 31),
            revision=1,
            created_at="2026-09-11T00:00:00Z",
            source_commit="0" * 40,
        )
        options.update(overrides)
        return br.build_release(**options)


def member(archive: Path, name: str) -> list[dict[str, str]]:
    with zipfile.ZipFile(archive) as bundle:
        return list(csv.DictReader(io.StringIO(bundle.read(name).decode("utf-8"))))


class ReleaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.fixture = ReleaseFixture(Path(tmp.name))

    def edit(self, path: Path, old: str, new: str) -> None:
        text = path.read_text()
        self.assertIn(old, text, "the mutation must apply to the fixture")
        path.write_text(text.replace(old, new, 1))

    def assertRefused(self, reason_part: str) -> None:
        with self.assertRaises(br.ReleaseError) as ctx:
            self.fixture.build()
        self.assertIn(reason_part, str(ctx.exception))
        self.assertFalse(self.fixture.output.exists(), "a refused build must not publish anything")


class NormaliseTests(unittest.TestCase):
    def test_integer_written_as_float_loses_the_point(self) -> None:
        self.assertEqual(br.to_integer("551062649.0", "x"), "551062649")

    def test_integer_with_a_fraction_is_refused(self) -> None:
        with self.assertRaises(br.ReleaseError):
            br.to_integer("12.5", "x")

    def test_decimal_rounds_half_even_to_four_places(self) -> None:
        self.assertEqual(br.to_decimal("3753.1907805221", "x"), "3753.1908")
        self.assertEqual(br.to_decimal("0.00005", "x"), "0")
        self.assertEqual(br.to_decimal("0.00015", "x"), "0.0002")

    def test_decimal_drops_trailing_zeros(self) -> None:
        self.assertEqual(br.to_decimal("150.0000", "x"), "150")
        self.assertEqual(br.to_decimal("12.50", "x"), "12.5")

    def test_negative_decimal_is_refused(self) -> None:
        with self.assertRaises(br.ReleaseError):
            br.to_decimal("-1", "x")

    def test_null_spellings_become_empty(self) -> None:
        self.assertEqual(br.to_text("Unknown"), "")
        self.assertEqual(br.to_decimal("nan", "x"), "")
        self.assertEqual(br.to_integer("", "x"), "")

    def test_dates_become_iso(self) -> None:
        self.assertEqual(br.to_iso_date("01/JAN/1984", "x"), "1984-01-01")
        self.assertEqual(br.to_iso_date("2025-01-02 00:00:00", "x"), "2025-01-02")
        with self.assertRaises(br.ReleaseError):
            br.to_iso_date("Jan 2 2025", "x")


class BuildTests(ReleaseTestCase):
    def test_build_publishes_an_artifact_that_passes_the_contract(self) -> None:
        summary = self.fixture.build()

        self.assertEqual(summary.dataset_version, "2025-12-31.1")
        self.assertEqual(summary.coverage, ("2025-12-29", "2025-12-31"))
        self.assertEqual(validate_artifact(summary.archive).dataset_version, "2025-12-31.1")
        self.assertEqual(
            sorted(p.name for p in self.fixture.output.iterdir()),
            ["cse-dataset-2025-12-31.1.zip", "manifest.json", "release_notes.md"],
        )

    def test_prices_are_normalised_and_sorted(self) -> None:
        rows = member(self.fixture.build().archive, "daily_ohlcv.csv")

        self.assertEqual([(r["date"], r["symbol"]) for r in rows[:2]], [("2025-12-29", "AAF.N0000"), ("2025-12-29", "COMB.N0000")])
        self.assertEqual(rows[0]["volume"], "15000")
        self.assertEqual(rows[0]["trades"], "12")
        self.assertEqual(rows[1]["open"], "")
        self.assertNotIn("validation_status", rows[0])

    def test_quarantined_session_stays_in_the_calendar(self) -> None:
        summary = self.fixture.build()

        calendar = member(summary.archive, "trading_calendar.csv")
        self.assertEqual(
            [(r["date"], r["ohlcv_status"], r["ohlcv_rows"]) for r in calendar],
            [("2025-12-29", "accepted", "2"), ("2025-12-30", "quarantined", "0"), ("2025-12-31", "accepted", "2")],
        )
        manifest = json.loads((self.fixture.output / "manifest.json").read_text())
        self.assertEqual(manifest["quarantine"], {"dates": 1, "rows": 2})
        self.assertIn("2025-12-30", (self.fixture.output / "release_notes.md").read_text())

    def test_index_values_are_rounded_and_unshipped_series_left_out(self) -> None:
        summary = self.fixture.build()

        values = member(summary.archive, "index_values.csv")
        self.assertEqual(sorted({r["index_code"] for r in values}), ["ASPI", "ASTRI", "SL20", "SL20TRI"])
        sl20 = next(r for r in values if r["index_code"] == "SL20" and r["date"] == "2025-12-29")
        self.assertEqual(sl20["close"], "3753.1908")
        self.assertEqual(len(member(summary.archive, "indices.csv")), 4)

    def test_metadata_is_normalised_and_limited_to_traded_symbols(self) -> None:
        rows = {r["symbol"]: r for r in member(self.fixture.build().archive, "company_metadata.csv")}

        self.assertEqual(sorted(rows), ["AAF.N0000", "COMB.N0000"])
        self.assertEqual(rows["AAF.N0000"]["listing_date"], "1984-01-01")
        self.assertEqual(rows["AAF.N0000"]["delisted"], "false")
        self.assertEqual(rows["AAF.N0000"]["sector_code"], "")
        self.assertEqual(rows["AAF.N0000"]["board"], "")
        self.assertEqual(rows["COMB.N0000"]["shares_outstanding"], "")

    def test_revision_sets_the_version(self) -> None:
        summary = self.fixture.build(revision=2)

        self.assertEqual(summary.dataset_version, "2025-12-31.2")
        self.assertEqual(summary.archive.name, "cse-dataset-2025-12-31.2.zip")

    def test_same_inputs_give_identical_bytes(self) -> None:
        first = self.fixture.build(output_dir=self.fixture.root / "first").archive.read_bytes()
        second = self.fixture.build(output_dir=self.fixture.root / "second").archive.read_bytes()

        self.assertEqual(first, second)


class RefusalTests(ReleaseTestCase):
    def test_listed_source_file_missing(self) -> None:
        self.fixture.inputs.sources_list.write_text("official_2024.csv\n")

        self.assertRefused("official_2024.csv does not exist")

    def test_source_never_backfilled(self) -> None:
        self.fixture.candidates.unlink()

        self.assertRefused("no backfill candidates for official_2025.csv")

    def test_session_never_validated(self) -> None:
        self.fixture.summary_path("2025-12-31").unlink()
        self.fixture.accepted_path("2025-12-31").unlink()

        self.assertRefused("2025-12-31 was never validated")

    def test_passed_session_without_accepted_output(self) -> None:
        self.fixture.accepted_path("2025-12-29").unlink()

        self.assertRefused("2025-12-29/cse_historical_daily_share_prices passed validation but has no accepted output")

    def test_stale_accepted_output_for_a_failed_session(self) -> None:
        self.fixture.write_accepted("2025-12-30", [price("2025-12-30", "AAF.N0000")])

        self.assertRefused("stale accepted output")

    def test_traded_symbol_without_metadata(self) -> None:
        self.edit(self.fixture.inputs.metadata_path, "COMB.N0000,COMMERCIAL", "COMX.N0000,COMMERCIAL")

        self.assertRefused("1 traded symbols have no metadata row (first: COMB.N0000)")

    def test_index_series_missing_a_session(self) -> None:
        self.edit(self.fixture.inputs.indices_path, "2025-12-30,ASTRI,", "2025-12-28,ASTRI,")

        self.assertRefused("ASTRI does not match the calendar")

    def test_index_value_on_a_date_that_is_not_a_session(self) -> None:
        with self.fixture.inputs.indices_path.open("a") as handle:
            handle.write(f"2025-12-28,ASPI,21900.5,cse_official_index_workbook,2025-12-28,{INDEX_HASH}\n")

        self.assertRefused("values on 1 non-sessions ['2025-12-28']")

    def test_fractional_volume_is_refused(self) -> None:
        self.fixture.write_accepted("2025-12-31", [price("2025-12-31", "AAF.N0000", volume="12.5")])

        self.assertRefused("2025-12-31 AAF.N0000 volume: '12.5' is not a whole number")

    def test_contract_failure_in_staging_is_refused(self) -> None:
        self.edit(self.fixture.inputs.metadata_path, "ASIA ASSET FINANCE PLC", "")

        self.assertRefused("staging directory failed the contract: bad_value")


if __name__ == "__main__":
    unittest.main()
