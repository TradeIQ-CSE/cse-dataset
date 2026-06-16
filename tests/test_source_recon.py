from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts import source_recon
from scripts.source_recon import (
    ACCEPTED_CANDIDATE,
    MINIMAL_PNG,
    QUARANTINE,
    ParsedEvidence,
    SourceProbeResult,
    SourceProbeSpec,
    attach_evidence,
    parse_response_evidence,
    required_fields_present,
    score_result,
    write_browser_failure_artifacts,
)


class SourceReconScoringTests(unittest.TestCase):
    def ohlcv_result(self, source_id: str) -> SourceProbeResult:
        return SourceProbeResult(
            source_id=source_id,
            display_name=source_id,
            family="ohlcv",
            probe_type="api",
            url="https://example.test",
            environment="test",
            reachable=True,
            status_code=200,
        )

    def test_stale_current_snapshot_is_quarantined_not_accepted(self) -> None:
        result = self.ohlcv_result("current")
        result.current_snapshot = True
        result.has_cse_coverage = True
        result.has_2026_dates = True
        result.has_verifiable_dates = False
        result.detected_fields = ["symbol", "open", "high", "low", "close", "volume"]
        result.required_fields_present = True

        scored = score_result(result)

        self.assertEqual(scored.recommendation, QUARANTINE)
        self.assertIn("current snapshot only", scored.recommendation_reason)

    def test_date_bearing_ohlcv_sample_ranks_above_current_only_snapshot(self) -> None:
        dated = self.ohlcv_result("dated")
        dated.has_cse_coverage = True
        dated.has_2026_dates = True
        dated.has_verifiable_dates = True
        dated.has_per_row_dates = True
        dated.has_report_date = False
        dated.detected_fields = ["date", "symbol", "open", "high", "low", "close", "volume"]
        dated.required_fields_present = True

        current = self.ohlcv_result("current")
        current.current_snapshot = True
        current.has_cse_coverage = True
        current.has_2026_dates = True
        current.detected_fields = ["symbol", "open", "high", "low", "close", "volume"]
        current.required_fields_present = True

        scored_dated = score_result(dated)
        scored_current = score_result(current)

        self.assertEqual(scored_dated.recommendation, ACCEPTED_CANDIDATE)
        self.assertGreater(scored_dated.total_score, scored_current.total_score)

    def test_ohlcv_cannot_be_accepted_without_verifiable_dates(self) -> None:
        result = self.ohlcv_result("undated")
        result.has_cse_coverage = True
        result.has_2026_dates = True
        result.detected_fields = ["symbol", "open", "high", "low", "close", "volume"]
        result.required_fields_present = True

        scored = score_result(result)

        self.assertEqual(scored.recommendation, QUARANTINE)
        self.assertIn("lacks verifiable", scored.recommendation_reason)

    def test_required_ohlcv_fields_need_symbol_and_close(self) -> None:
        spec = SourceProbeSpec(
            source_id="sample",
            display_name="Sample",
            family="ohlcv",
            probe_type="api",
            url="https://example.test",
            expected_fields={"date", "symbol", "open", "high", "low", "close", "volume"},
        )

        self.assertTrue(
            required_fields_present(spec, ["date", "symbol", "open", "high", "low", "close", "volume"])
        )
        self.assertFalse(required_fields_present(spec, ["date", "symbol", "close", "volume"]))

    def test_attach_evidence_sets_required_fields(self) -> None:
        spec = SourceProbeSpec(
            source_id="dated",
            display_name="Dated",
            family="ohlcv",
            probe_type="api",
            url="https://example.test",
            expected_fields={"date", "symbol", "open", "high", "low", "close", "volume"},
        )
        result = self.ohlcv_result("dated")
        evidence = ParsedEvidence(
            has_cse_coverage=True,
            has_2026_dates=True,
            has_verifiable_dates=True,
            has_per_row_dates=True,
            detected_fields=["date", "symbol", "open", "high", "low", "close", "volume"],
        )

        attach_evidence(result, spec, evidence)

        self.assertTrue(result.required_fields_present)
        self.assertTrue(result.has_per_row_dates)

    def test_historical_2026_hint_does_not_create_observed_2026_evidence(self) -> None:
        spec = SourceProbeSpec(
            source_id="hinted",
            display_name="Hinted",
            family="ohlcv",
            probe_type="api",
            url="https://example.test",
            historical_2026_hint=True,
        )

        evidence = parse_response_evidence(
            spec,
            "application/json",
            b'[{"date":"2025-12-31","symbol":"COMB.N0000","open":1,"high":1,"low":1,"close":1,"volume":1}]',
        )

        self.assertFalse(evidence.has_2026_dates)
        self.assertEqual(evidence.observed_dates, ["2025-12-31"])

    def test_yahoo_chart_payload_is_normalized_to_dated_ohlcv_rows(self) -> None:
        spec = SourceProbeSpec(
            source_id="yahoo",
            display_name="Yahoo",
            family="ohlcv",
            probe_type="api",
            url="https://example.test",
            expected_fields={"date", "symbol", "open", "high", "low", "close", "volume"},
            supports_cse_hint=True,
        )
        payload = b"""
        {
          "chart": {
            "result": [{
              "meta": {"symbol": "COMB-N0000.CM", "gmtoffset": 19800},
              "timestamp": [1767303000],
              "indicators": {
                "quote": [{
                  "open": [201.0],
                  "high": [203.0],
                  "low": [201.0],
                  "close": [203.0],
                  "volume": [150627]
                }]
              }
            }],
            "error": null
          }
        }
        """

        evidence = parse_response_evidence(spec, "application/json", payload)

        self.assertTrue(evidence.has_per_row_dates)
        self.assertTrue(evidence.has_2026_dates)
        self.assertEqual(evidence.sample_rows[0]["symbol"], "COMB-N0000.CM")
        self.assertEqual(
            evidence.detected_fields,
            ["close", "date", "high", "low", "open", "symbol", "volume"],
        )

    def test_cse_camelcase_epoch_date_fields_are_detected(self) -> None:
        spec = SourceProbeSpec(
            source_id="today",
            display_name="Today",
            family="ohlcv",
            probe_type="api",
            url="https://example.test",
            expected_fields={"date", "symbol", "open", "high", "low", "close", "volume"},
            supports_cse_hint=True,
        )
        payload = b"""
        [{
          "symbol": "ABAN.N0000",
          "open": 1058.0,
          "high": 1188.0,
          "low": 1058.0,
          "lastTradedPrice": 1102.5,
          "quantity": 1,
          "tradesTime": 1780042577709
        }]
        """

        evidence = parse_response_evidence(spec, "application/json", payload)

        self.assertTrue(evidence.has_per_row_dates)
        self.assertTrue(evidence.has_2026_dates)
        self.assertIn("close", evidence.detected_fields)
        self.assertIn("volume", evidence.detected_fields)


class SourceReconBrowserArtifactTests(unittest.TestCase):
    def test_browser_selector_failure_writes_structured_failure_and_screenshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_root = source_recon.ROOT
            source_recon.ROOT = root
            try:
                failure_path = write_browser_failure_artifacts(
                    run_dir=root / "data/recon/test",
                    source_id="browser_page",
                    url="https://example.test",
                    selectors=[{"selector": "table", "purpose": "historical table", "required": True}],
                    error="selector timeout",
                    screenshot_bytes=MINIMAL_PNG,
                )
            finally:
                source_recon.ROOT = old_root

            self.assertTrue(failure_path.exists())
            payload = failure_path.read_text()
            self.assertIn('"structured_failure": true', payload)
            self.assertTrue((root / "data/recon/test/screenshots/browser_page_failure.png").exists())


if __name__ == "__main__":
    unittest.main()
