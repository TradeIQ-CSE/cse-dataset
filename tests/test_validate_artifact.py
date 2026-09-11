"""Contract v1 artifact validator.

Every invalid case starts from the committed valid fixture and changes one
thing, then refreshes the manifest checksums and row counts so that change is
the only thing wrong. Without the refresh nearly every case would stop at
checksum_mismatch and the later checks would go untested.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Callable

import scripts.validate_artifact as va

VALID = Path(__file__).resolve().parent / "fixtures" / "artifact" / "valid"


class ArtifactValidatorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.artifact = Path(tmp.name) / "artifact"
        shutil.copytree(VALID, self.artifact)

    def edit_manifest(self, change: Callable[[dict], None]) -> None:
        path = self.artifact / "manifest.json"
        manifest = json.loads(path.read_text())
        change(manifest)
        path.write_text(json.dumps(manifest, indent=2) + "\n")

    def rehash(self) -> None:
        def refresh(manifest: dict) -> None:
            for entry in manifest["files"]:
                path = self.artifact / entry["path"]
                if path.exists():
                    data = path.read_bytes()
                    entry["sha256"] = hashlib.sha256(data).hexdigest()
                    entry["rows"] = max(data.count(b"\n") - 1, 0)

        self.edit_manifest(refresh)

    def replace_in(self, name: str, old: str, new: str, *, rehash: bool = True) -> None:
        path = self.artifact / name
        text = path.read_text()
        self.assertIn(old, text, "the mutation must apply to the fixture")
        path.write_text(text.replace(old, new, 1))
        if rehash:
            self.rehash()

    def drop_line(self, name: str, line: str) -> None:
        self.replace_in(name, line + "\n", "")

    def assertFails(self, code: str, reason_part: str | None = None, path: Path | None = None) -> None:
        with self.assertRaises(va.ArtifactError) as ctx:
            va.validate_artifact(path or self.artifact)
        self.assertEqual(ctx.exception.code, code, ctx.exception.reason)
        if reason_part is not None:
            self.assertIn(reason_part, ctx.exception.reason)

    def zip_artifact(self, arcname: Callable[[str], str] = lambda name: name) -> Path:
        archive = self.artifact.with_suffix(".zip")
        with zipfile.ZipFile(archive, "w") as zf:
            for path in sorted(self.artifact.iterdir()):
                zf.write(path, arcname=arcname(path.name))
        return archive


class ValidArtifactTests(ArtifactValidatorTestCase):
    def test_valid_fixture_passes(self) -> None:
        summary = va.validate_artifact(self.artifact)

        self.assertEqual(summary.dataset_version, "2025-12-31.1")
        self.assertEqual(summary.coverage, ("2025-12-29", "2025-12-31"))
        self.assertEqual(summary.rows["daily_ohlcv.csv"], 6)
        self.assertEqual(summary.rows["index_values.csv"], 6)

    def test_valid_fixture_passes_as_zip(self) -> None:
        va.validate_artifact(self.zip_artifact())

    def test_newer_minor_contract_version_is_accepted(self) -> None:
        self.edit_manifest(lambda m: m.update(contract_version="1.3.0"))

        va.validate_artifact(self.artifact)

    def test_unknown_columns_are_ignored(self) -> None:
        path = self.artifact / "daily_ohlcv.csv"
        lines = path.read_text().splitlines()
        path.write_text("\n".join([lines[0] + ",note", *(line + "," for line in lines[1:])]) + "\n")
        self.rehash()

        va.validate_artifact(self.artifact)

    def test_sector_code_resolves_when_sectors_file_is_shipped(self) -> None:
        (self.artifact / "sectors.csv").write_text("gics_code,sector_name\n4010,Banks\n")
        self.edit_manifest(lambda m: m["files"].append({"path": "sectors.csv", "sha256": "0" * 64, "rows": 0}))
        self.replace_in(
            "company_metadata.csv",
            "COMB.N0000,COMMERCIAL BANK OF CEYLON PLC,false,,,,",
            "COMB.N0000,COMMERCIAL BANK OF CEYLON PLC,false,,,4010,",
        )

        summary = va.validate_artifact(self.artifact)

        self.assertEqual(summary.rows["sectors.csv"], 1)

    def test_incremental_release_after_its_base_is_accepted(self) -> None:
        self.edit_manifest(lambda m: m.update(kind="incremental", base_version="2025-12-24.1"))

        va.validate_artifact(self.artifact)

    def test_correction_as_later_revision_is_accepted(self) -> None:
        self.edit_manifest(
            lambda m: m.update(
                kind="correction",
                dataset_version="2025-12-31.2",
                base_version="2025-12-31.1",
                corrected_dates=["2025-12-31"],
            )
        )

        va.validate_artifact(self.artifact)


class ArtifactAndManifestTests(ArtifactValidatorTestCase):
    def test_path_that_is_neither_directory_nor_zip(self) -> None:
        self.assertFails("artifact_unreadable", path=self.artifact / "missing")

    def test_zip_with_nested_layout_is_refused(self) -> None:
        archive = self.zip_artifact(lambda name: f"cse-dataset/{name}")

        self.assertFails("unlisted_file", "not at the archive root", path=archive)

    def test_missing_manifest(self) -> None:
        (self.artifact / "manifest.json").unlink()

        self.assertFails("manifest_unreadable", "missing")

    def test_malformed_manifest(self) -> None:
        (self.artifact / "manifest.json").write_text('{"contract_version": ')

        self.assertFails("manifest_unreadable", "not valid JSON")

    def test_unsupported_contract_major_version(self) -> None:
        self.edit_manifest(lambda m: m.update(contract_version="2.0.0"))

        self.assertFails("unsupported_contract", "2.0.0")

    def test_manifest_missing_required_field(self) -> None:
        self.edit_manifest(lambda m: m.pop("source_commit"))

        self.assertFails("manifest_schema", "source_commit")

    def test_incremental_release_requires_base_version(self) -> None:
        self.edit_manifest(lambda m: m.update(kind="incremental"))

        self.assertFails("manifest_schema", "base_version")

    def test_full_release_cannot_name_a_base(self) -> None:
        self.edit_manifest(lambda m: m.update(base_version="2025-12-24.1"))

        self.assertFails("manifest_schema")

    def test_incremental_release_must_start_after_its_base(self) -> None:
        self.edit_manifest(lambda m: m.update(kind="incremental", base_version="2025-12-29.1"))

        self.assertFails("manifest_schema", "must be after base_version")

    def test_correction_must_be_a_later_revision_of_its_base(self) -> None:
        self.edit_manifest(
            lambda m: m.update(
                kind="correction",
                dataset_version="2025-12-31.2",
                base_version="2025-12-31.3",
                corrected_dates=["2025-12-31"],
            )
        )

        self.assertFails("manifest_schema", "later revision")

    def test_dataset_version_must_match_coverage_end(self) -> None:
        self.edit_manifest(lambda m: m.update(dataset_version="2025-12-30.1"))

        self.assertFails("manifest_schema", "coverage.end")

    def test_known_gap_must_name_a_listed_file(self) -> None:
        gap = {"file": "index_value.csv", "start": "2025-12-30", "end": "2025-12-31", "reason": "typo"}
        self.edit_manifest(lambda m: m.update(known_gaps=[gap]))

        self.assertFails("manifest_schema", "index_value.csv")

    def test_created_at_must_be_a_real_timestamp(self) -> None:
        self.edit_manifest(lambda m: m.update(created_at="2026-13-01T00:00:00Z"))

        self.assertFails("manifest_schema", "created_at")

    def test_file_listed_twice(self) -> None:
        self.edit_manifest(lambda m: m["files"].append(dict(m["files"][0])))

        self.assertFails("manifest_schema", "more than once")

    def test_index_series_listed_twice(self) -> None:
        self.edit_manifest(lambda m: m["index_series"].append(dict(m["index_series"][0])))

        self.assertFails("manifest_schema", "ASPI more than once")


class FileListingTests(ArtifactValidatorTestCase):
    def test_required_file_not_shipped(self) -> None:
        (self.artifact / "trading_calendar.csv").unlink()
        self.edit_manifest(
            lambda m: m.update(files=[f for f in m["files"] if f["path"] != "trading_calendar.csv"])
        )

        self.assertFails("missing_file", "trading_calendar.csv is required")

    def test_listed_file_absent_from_artifact(self) -> None:
        (self.artifact / "indices.csv").unlink()

        self.assertFails("missing_file", "indices.csv is listed")

    def test_file_not_listed_in_manifest(self) -> None:
        (self.artifact / "notes.txt").write_text("scratch\n")

        self.assertFails("unlisted_file", "notes.txt")

    def test_checksum_mismatch(self) -> None:
        self.replace_in("daily_ohlcv.csv", "12.6,15000", "12.7,15000", rehash=False)

        self.assertFails("checksum_mismatch", "daily_ohlcv.csv")

    def test_row_count_mismatch(self) -> None:
        def overstate(manifest: dict) -> None:
            for entry in manifest["files"]:
                if entry["path"] == "daily_ohlcv.csv":
                    entry["rows"] += 1

        self.edit_manifest(overstate)

        self.assertFails("row_count_mismatch", "daily_ohlcv.csv")


class FileFormatTests(ArtifactValidatorTestCase):
    def test_byte_order_mark(self) -> None:
        path = self.artifact / "daily_ohlcv.csv"
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
        self.rehash()

        self.assertFails("bad_encoding", "byte order mark")

    def test_crlf_line_endings(self) -> None:
        path = self.artifact / "index_values.csv"
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        self.rehash()

        self.assertFails("bad_encoding", "line endings")

    def test_missing_column(self) -> None:
        self.replace_in("daily_ohlcv.csv", ",turnover,", ",turnover_rs,")

        self.assertFails("header_mismatch", "turnover")

    def test_integer_written_as_float(self) -> None:
        self.replace_in("daily_ohlcv.csv", ",15000,", ",15000.0,")

        self.assertFails("bad_value", "column volume")

    def test_more_than_four_decimal_places(self) -> None:
        self.replace_in("index_values.csv", ",21950.12,", ",21950.12345,")

        self.assertFails("bad_value", "column close")

    def test_null_placeholder(self) -> None:
        self.replace_in(
            "company_metadata.csv",
            "AAF.N0000,ASIA ASSET FINANCE PLC,false,,,,",
            "AAF.N0000,ASIA ASSET FINANCE PLC,false,,,Unknown,",
        )

        self.assertFails("bad_value", "null placeholder")

    def test_required_value_empty(self) -> None:
        self.replace_in("daily_ohlcv.csv", ",12.4,12.6,", ",12.4,,")

        self.assertFails("bad_value", "column close: required value is empty")

    def test_impossible_date(self) -> None:
        self.replace_in(
            "company_metadata.csv",
            "AAF.N0000,ASIA ASSET FINANCE PLC,false,,",
            "AAF.N0000,ASIA ASSET FINANCE PLC,false,2025-02-30,",
        )

        self.assertFails("bad_value", "column listing_date")

    def test_symbol_format(self) -> None:
        self.replace_in("company_metadata.csv", "JKH.N0000,", "JKH,")

        self.assertFails("bad_value", "column symbol")

    def test_unknown_index_code(self) -> None:
        self.replace_in("index_values.csv", "2025-12-29,SL20,", "2025-12-29,S&P SL20,")

        self.assertFails("bad_value", "column index_code")

    def test_row_with_wrong_field_count(self) -> None:
        self.replace_in("trading_calendar.csv", "2025-12-29,accepted,3", "2025-12-29,accepted,3,")

        self.assertFails("bad_value", "fields")

    def test_duplicate_key(self) -> None:
        row = (self.artifact / "daily_ohlcv.csv").read_text().splitlines()[1]
        self.replace_in("daily_ohlcv.csv", row + "\n", row + "\n" + row + "\n")

        self.assertFails("duplicate_key", "2025-12-29, AAF.N0000")

    def test_rows_out_of_key_order(self) -> None:
        lines = (self.artifact / "index_values.csv").read_text().splitlines()
        lines[1], lines[2] = lines[2], lines[1]
        (self.artifact / "index_values.csv").write_text("\n".join(lines) + "\n")
        self.rehash()

        self.assertFails("key_order", "index_values.csv")


class CrossFileTests(ArtifactValidatorTestCase):
    def test_price_symbol_missing_from_metadata(self) -> None:
        self.drop_line("company_metadata.csv", "JKH.N0000,JOHN KEELLS HOLDINGS PLC,false,,,,,,Main")

        self.assertFails("orphan_reference", "'JKH.N0000' is not in company_metadata.csv")

    def test_price_date_missing_from_calendar(self) -> None:
        self.drop_line("trading_calendar.csv", "2025-12-31,accepted,3")

        self.assertFails("orphan_reference", "daily_ohlcv.csv date '2025-12-31'")

    def test_index_code_missing_from_indices(self) -> None:
        self.drop_line("indices.csv", "SL20,S&P Sri Lanka 20,")

        self.assertFails("orphan_reference", "'SL20' is not in indices.csv")

    def test_sector_code_without_sectors_file(self) -> None:
        self.replace_in(
            "company_metadata.csv",
            "COMB.N0000,COMMERCIAL BANK OF CEYLON PLC,false,,,,",
            "COMB.N0000,COMMERCIAL BANK OF CEYLON PLC,false,,,4010,",
        )

        self.assertFails("orphan_reference", "sectors.csv (not shipped)")

    def test_quarantined_session_marked_accepted(self) -> None:
        self.replace_in("trading_calendar.csv", "2025-12-30,quarantined,0", "2025-12-30,accepted,0")

        self.assertFails("calendar_inconsistent", "2025-12-30 is accepted but daily_ohlcv.csv has no rows")

    def test_quarantined_session_with_prices(self) -> None:
        self.replace_in("trading_calendar.csv", "2025-12-31,accepted,3", "2025-12-31,quarantined,3")

        self.assertFails("calendar_inconsistent", "2025-12-31 is quarantined")

    def test_ohlcv_rows_disagrees_with_prices(self) -> None:
        self.replace_in("trading_calendar.csv", "2025-12-29,accepted,3", "2025-12-29,accepted,2")

        self.assertFails("calendar_inconsistent", "ohlcv_rows is 2")

    def test_manifest_quarantine_count_disagrees_with_calendar(self) -> None:
        self.edit_manifest(lambda m: m["quarantine"].update(dates=0))

        self.assertFails("calendar_inconsistent", "quarantine.dates")

    def test_coverage_starts_before_first_session(self) -> None:
        self.edit_manifest(lambda m: m["coverage"].update(start="2025-12-26"))

        self.assertFails("coverage_mismatch", "coverage says 2025-12-26..2025-12-31")

    def test_index_series_disagrees_with_values(self) -> None:
        self.edit_manifest(lambda m: m["index_series"][0].update(last_date="2025-12-30"))

        self.assertFails("coverage_mismatch", "ASPI runs 2025-12-29..2025-12-31")

    def test_index_series_omits_a_shipped_code(self) -> None:
        self.edit_manifest(lambda m: m.update(index_series=m["index_series"][:1]))

        self.assertFails("coverage_mismatch", "SL20")

    def test_corrected_date_must_be_a_session(self) -> None:
        self.edit_manifest(
            lambda m: m.update(
                kind="correction",
                dataset_version="2025-12-31.2",
                base_version="2025-12-31.1",
                corrected_dates=["2025-12-28"],
            )
        )

        self.assertFails("coverage_mismatch", "2025-12-28")

    def test_known_gap_outside_coverage(self) -> None:
        gap = {"file": "index_values.csv", "start": "2025-12-30", "end": "2026-01-02", "reason": "test"}
        self.edit_manifest(lambda m: m.update(known_gaps=[gap]))

        self.assertFails("coverage_mismatch", "falls outside coverage")


class CommandLineTests(ArtifactValidatorTestCase):
    def run_main(self) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = va.main([str(self.artifact)])
        return status, out.getvalue()

    def test_valid_artifact_exits_zero(self) -> None:
        status, output = self.run_main()

        self.assertEqual(status, 0)
        self.assertTrue(output.startswith("OK 2025-12-31.1 (full)"), output)

    def test_invalid_artifact_exits_one_with_the_failure_code(self) -> None:
        self.replace_in("daily_ohlcv.csv", "12.6,15000", "12.7,15000", rehash=False)

        status, output = self.run_main()

        self.assertEqual(status, 1)
        self.assertTrue(output.startswith("FAIL checksum_mismatch: daily_ohlcv.csv"), output)


if __name__ == "__main__":
    unittest.main()
