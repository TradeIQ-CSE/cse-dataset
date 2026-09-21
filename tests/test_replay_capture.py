import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from scripts.replay_capture import ReplayError, find_payload, replay_capture
from tests.test_backfill_deliveries import SESSION, SOURCE, write_capture


def metadata_file(root: Path, symbols=("COMB.N0000",)) -> Path:
    path = root / "metadata.csv"
    pd.DataFrame(
        [
            {
                "symbol": symbol,
                "company_name": "COMMERCIAL BANK OF CEYLON PLC",
                "shares_outstanding": "1556891381",
            }
            for symbol in symbols
        ]
    ).to_csv(path, index=False)
    return path


class FindPayloadTests(unittest.TestCase):
    def test_reads_the_session_from_the_rows_not_the_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            captures = Path(tmp)
            # Filed under the next day, as runs before TIQ-134 were when they
            # started after midnight; 2026-08-28's real capture is like this.
            run_dir = write_capture(
                captures, SESSION, "1", filed_under=date(2026, 9, 19)
            )
            _, session, source = find_payload(run_dir)
        self.assertEqual(session, SESSION)
        self.assertEqual(source, SOURCE)

    def test_refuses_a_capture_with_no_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            captures = Path(tmp)
            run_dir = write_capture(captures, SESSION, "1", with_payload=False)
            with self.assertRaises(ReplayError):
                find_payload(run_dir)


class ReplayCaptureTests(unittest.TestCase):
    def test_stages_the_layout_the_publisher_expects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            captures = root / "captures"
            captures.mkdir()
            run_dir = write_capture(captures, SESSION, "1")
            outcome = replay_capture(
                run_dir,
                staging_root=root / "staging",
                metadata_path=metadata_file(root),
            )
            self.assertTrue(outcome.passed)
            self.assertEqual(outcome.accepted_rows, 1)
            manifest = json.loads(outcome.staged_manifest.read_text())
            staged_root = outcome.staged_manifest.parent.parent.parent
            self.assertEqual(manifest["status"], "accepted")
            self.assertEqual(manifest["target_date"], SESSION.isoformat())
            # The capture's own fetch time, never the replay's clock.
            self.assertEqual(manifest["captured_at"], "2026-09-18T16:44:11Z")
            self.assertTrue((staged_root / manifest["accepted_path"]).is_file())
            self.assertTrue((staged_root / manifest["metadata_path"]).is_file())

    def test_rejects_when_a_traded_symbol_has_no_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            captures = root / "captures"
            captures.mkdir()
            run_dir = write_capture(captures, SESSION, "1")
            outcome = replay_capture(
                run_dir,
                staging_root=root / "staging",
                metadata_path=metadata_file(root, symbols=("OTHER.N0000",)),
            )
        self.assertFalse(outcome.passed)
        self.assertIsNone(outcome.staged_manifest)
        self.assertTrue(
            any("metadata" in failure for failure in outcome.failures),
            outcome.failures,
        )

    def test_never_writes_into_the_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            captures = root / "captures"
            captures.mkdir()
            run_dir = write_capture(captures, SESSION, "1")
            before = sorted(p.relative_to(run_dir) for p in run_dir.rglob("*"))
            replay_capture(
                run_dir,
                staging_root=root / "staging",
                metadata_path=metadata_file(root),
            )
            after = sorted(p.relative_to(run_dir) for p in run_dir.rglob("*"))
        self.assertEqual(before, after)

    def test_refuses_a_capture_with_no_fetch_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            captures = root / "captures"
            captures.mkdir()
            run_dir = write_capture(captures, SESSION, "1")
            metadata = next(run_dir.rglob("source_payloads/*/*/metadata.json"))
            metadata.write_text(json.dumps({"source_name": SOURCE}))
            with self.assertRaises(ReplayError):
                replay_capture(
                    run_dir,
                    staging_root=root / "staging",
                    metadata_path=metadata_file(root),
                )


if __name__ == "__main__":
    unittest.main()
