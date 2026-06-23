from __future__ import annotations

from datetime import date
from pathlib import Path
import json
import tempfile
import unittest

import scripts.ohlcv_coverage_report as coverage


class OHLCVCoverageReportTests(unittest.TestCase):
    def test_classifies_accepted_before_quarantined_attempts(self) -> None:
        accepted = coverage.AcceptedInfo(row_count=2)
        validation = coverage.ValidationInfo(statuses={"quarantined"}, rejected_rows=1)

        status = coverage.classify_date(
            accepted=accepted,
            validation=validation,
            source_observed=None,
            run_log=None,
            is_weekday=True,
        )

        self.assertEqual(status, "accepted")

    def test_classifies_source_observed_without_validation_as_candidate_unvalidated(self) -> None:
        observed = coverage.SourceObservedInfo(row_count=10, full_ohlcv_rows=10)

        status = coverage.classify_date(
            accepted=None,
            validation=None,
            source_observed=observed,
            run_log=None,
            is_weekday=True,
        )

        self.assertEqual(status, "candidate_unvalidated")

    def test_classifies_run_log_acceptance_without_artifact_separately(self) -> None:
        run_log = coverage.RunLogInfo(runs=1, accepted_rows=290)

        status = coverage.classify_date(
            accepted=None,
            validation=None,
            source_observed=None,
            run_log=run_log,
            is_weekday=True,
        )

        self.assertEqual(status, "run_log_only_missing_artifact")

    def test_collects_accepted_and_validation_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            accepted_csv = root / "accepted/2025-01-02/source_a/canonical_ohlcv.csv"
            accepted_csv.parent.mkdir(parents=True)
            accepted_csv.write_text("date,symbol,close\n2025-01-02,AAA.N0000,10\n2025-01-02,BBB.N0000,12\n")

            summary_path = root / "validation/2025-01-03/source_b/quality_summary.json"
            summary_path.parent.mkdir(parents=True)
            summary_path.write_text(
                json.dumps(
                    {
                        "target_date": "2025-01-03",
                        "source_name": "source_b",
                        "row_count": 2,
                        "accepted_rows": 1,
                        "rejected_rows": 1,
                        "failures": ["rejected OHLCV rows: 1"],
                    }
                )
                + "\n"
            )

            accepted = coverage.collect_accepted(
                [root / "accepted"],
                start_date=date(2025, 1, 1),
                end_date=date(2025, 1, 3),
            )
            validation = coverage.collect_validation(
                [root / "validation"],
                start_date=date(2025, 1, 1),
                end_date=date(2025, 1, 3),
            )
            by_date = coverage.build_coverage(
                start_date=date(2025, 1, 2),
                end_date=date(2025, 1, 3),
                accepted=accepted,
                validation=validation,
                source_observed={},
                run_logs={},
            )

        statuses = dict(zip(by_date["date"], by_date["status"], strict=True))
        self.assertEqual(statuses["2025-01-02"], "accepted")
        self.assertEqual(statuses["2025-01-03"], "quarantined")


if __name__ == "__main__":
    unittest.main()
