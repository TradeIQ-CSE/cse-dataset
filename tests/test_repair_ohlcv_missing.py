from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import pandas as pd

import scripts.repair_ohlcv_missing as repair


class RepairOHLCVMissingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.old_roots = (
            repair.CANDIDATE_ROOT,
            repair.VALIDATION_ROOT,
            repair.REPAIR_VALIDATION_ROOT,
            repair.ACCEPTED_ROOT,
        )
        repair.CANDIDATE_ROOT = self.root / "candidates"
        repair.VALIDATION_ROOT = self.root / "validation"
        repair.REPAIR_VALIDATION_ROOT = self.root / "repair_validation"
        repair.ACCEPTED_ROOT = self.root / "accepted"
        self.candidate_dir = repair.CANDIDATE_ROOT / "hash"
        self.candidate_dir.mkdir(parents=True)
        self.candidate_path = self.candidate_dir / "canonical_ohlcv_candidates.csv"
        pd.DataFrame(
            [
                {
                    "date": "2025-02-06",
                    "symbol": "SAMP.N0000",
                    "open": 116.5,
                    "high": 118.0,
                    "low": pd.NA,
                    "close": 116.5,
                    "volume": 1068206,
                    "turnover": 124219032,
                    "trades": 913,
                    "source": "fixture",
                    "source_priority": 30,
                    "source_timestamp": "2025-02-06",
                    "raw_payload_hash": "a" * 64,
                    "validation_status": "candidate",
                    "validation_warnings": "",
                }
            ]
        ).to_csv(self.candidate_path, index=False)

    def tearDown(self) -> None:
        (
            repair.CANDIDATE_ROOT,
            repair.VALIDATION_ROOT,
            repair.REPAIR_VALIDATION_ROOT,
            repair.ACCEPTED_ROOT,
        ) = self.old_roots
        self.tmpdir.cleanup()

    def write_repair(self, *, low: float, high: float = 118.0) -> Path:
        path = self.root / "repairs.csv"
        pd.DataFrame(
            [
                {
                    "date": "2025-02-06",
                    "symbol": "SAMP.N0000",
                    "open": 116.5,
                    "high": high,
                    "low": low,
                    "close": 116.5,
                    "volume": 1068206,
                    "turnover": 124219032,
                    "trades": 913,
                    "repair_source_name": "fixture_alt",
                    "repair_source_url": "https://example.com/source",
                    "repair_source_date": "2025-02-06",
                    "repair_notes": "test repair",
                }
            ]
        ).to_csv(path, index=False)
        return path

    def test_exports_missing_ohlcv_targets(self) -> None:
        output = self.root / "targets.csv"

        repair.export_targets(output, candidate_path=self.candidate_path)

        targets = pd.read_csv(output)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets.iloc[0]["symbol"], "SAMP.N0000")
        self.assertEqual(targets.iloc[0]["missing_fields"], "low")

    def test_applies_matching_repair_and_accepts_date(self) -> None:
        summary = repair.apply_repairs(
            repair_file=self.write_repair(low=116.0),
            candidate_path=self.candidate_path,
        )

        self.assertEqual(summary["accepted_dates"], 1)
        self.assertEqual(summary["accepted_rows"], 1)
        accepted_path = repair.ACCEPTED_ROOT / "2025-02-06/ohlcv_missing_field_repair/canonical_ohlcv.csv"
        accepted = pd.read_csv(accepted_path)
        self.assertEqual(float(accepted.iloc[0]["low"]), 116.0)

    def test_rejects_repair_that_conflicts_with_official_fields(self) -> None:
        summary = repair.apply_repairs(
            repair_file=self.write_repair(low=116.0, high=119.0),
            candidate_path=self.candidate_path,
            allow_validation_failure=True,
        )

        self.assertEqual(summary["accepted_dates"], 0)
        self.assertTrue(any("does not match official value" in failure for failure in summary["failures"]))
        self.assertFalse((repair.ACCEPTED_ROOT / "2025-02-06/ohlcv_missing_field_repair").exists())


if __name__ == "__main__":
    unittest.main()
