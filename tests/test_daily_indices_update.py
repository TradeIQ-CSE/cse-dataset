from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
import json
import tempfile
import unittest

import pandas as pd

import scripts.forward_ingestion as forward
import scripts.daily_indices_update as daily
import scripts.indices_sources as sources


# tradeDate 1788805800000 is 2026-09-08 in Colombo.
TRADE_DATE_MS = 1788805800000
SETTLED_DATE = date(2026, 9, 8)


def summary_payload(**overrides) -> list:
    row = {
        "id": 26508,
        "tradeDate": TRADE_DATE_MS,
        "asi": 21542.23,
        "spp": 6054.52,
        "spt": 12858.48,
        "triasi": 33929.07,
        "mpi": None,
        "trimpi": None,
        "marketTurnover": 1466552830.0,
    }
    row.update(overrides)
    return [[row]]


class PayloadShapeTests(unittest.TestCase):
    def test_nested_envelope_is_unwrapped(self) -> None:
        row = sources.extract_summary_row(summary_payload())
        self.assertEqual(row["asi"], 21542.23)

    def test_raw_bytes_are_accepted(self) -> None:
        raw = json.dumps(summary_payload()).encode("utf-8")
        self.assertEqual(sources.extract_summary_row(raw)["asi"], 21542.23)

    def test_unexpected_shape_is_refused(self) -> None:
        with self.assertRaises(forward.ForwardIngestionError):
            sources.extract_summary_row({"unexpected": "shape"})

    def test_empty_payload_is_refused(self) -> None:
        with self.assertRaises(forward.ForwardIngestionError):
            sources.extract_summary_row([[]])


class TradeDateTests(unittest.TestCase):
    def test_epoch_is_read_in_colombo_time(self) -> None:
        self.assertEqual(sources.epoch_ms_to_colombo_date(TRADE_DATE_MS), SETTLED_DATE)

    def test_late_evening_utc_is_still_the_next_colombo_day(self) -> None:
        # 2026-09-08 20:00 UTC is 2026-09-09 in Colombo (UTC+5:30). Reading the
        # stamp in UTC would file the row under the wrong trading day.
        stamp = int(datetime(2026, 9, 8, 20, 0, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(sources.epoch_ms_to_colombo_date(stamp), date(2026, 9, 9))

    def test_unreadable_stamp_returns_none(self) -> None:
        self.assertIsNone(sources.epoch_ms_to_colombo_date(None))


class NormalizeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = sources.CSEDailyMarketSummaryIndicesAdapter()

    def test_each_published_series_becomes_one_row(self) -> None:
        frame = self.adapter.normalize(summary_payload(), report_date=SETTLED_DATE, payload_hash="a" * 64)
        by_name = dict(zip(frame["index_name"], frame["close"]))
        self.assertEqual(
            by_name,
            {"ASPI": 21542.23, "SL20": 6054.52, "SL20TRI": 12858.48, "ASTRI": 33929.07},
        )

    def test_unpublished_series_produce_no_row(self) -> None:
        frame = self.adapter.normalize(summary_payload(), report_date=SETTLED_DATE, payload_hash="a" * 64)
        # Milanka is still a null field. It must be absent, not zero.
        self.assertNotIn("MPI", set(frame["index_name"]))
        self.assertNotIn(0.0, set(frame["close"]))

    def test_a_restarted_series_is_picked_up(self) -> None:
        frame = self.adapter.normalize(
            summary_payload(mpi=1234.5), report_date=SETTLED_DATE, payload_hash="a" * 64
        )
        self.assertEqual(dict(zip(frame["index_name"], frame["close"]))["MPI"], 1234.5)

    def test_rows_carry_the_contract_columns(self) -> None:
        frame = self.adapter.normalize(summary_payload(), report_date=SETTLED_DATE, payload_hash="a" * 64)
        for column in forward.FAMILY_CONTRACTS["indices"].canonical_columns:
            self.assertIn(column, frame.columns)


class SourceDateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = sources.CSEDailyMarketSummaryIndicesAdapter()
        self.records = self.adapter.normalize(
            summary_payload(), report_date=SETTLED_DATE, payload_hash="a" * 64
        )

    def test_matching_report_date_passes(self) -> None:
        self.assertEqual(self.adapter.validate_source_date(self.records, SETTLED_DATE, SETTLED_DATE), [])

    def test_mismatched_report_date_fails(self) -> None:
        failures = self.adapter.validate_source_date(self.records, date(2026, 9, 9), SETTLED_DATE)
        self.assertTrue(failures)
        self.assertIn("source date mismatch", failures[0])

    def test_missing_report_date_fails(self) -> None:
        self.assertTrue(self.adapter.validate_source_date(self.records, SETTLED_DATE, None))

    def test_empty_records_fail(self) -> None:
        self.assertTrue(
            self.adapter.validate_source_date(pd.DataFrame(), SETTLED_DATE, SETTLED_DATE)
        )


class StubAdapter(sources.CSEDailyMarketSummaryIndicesAdapter):
    """Serves a fixed payload so the run can be exercised without the network."""

    def __init__(self, payload: list, report_date: date | None) -> None:
        super().__init__()
        self.payload = payload
        self.report_date = report_date

    def fetch_for_date(self, target_date: date, raw_root: Path) -> forward.ForwardFetchResult:
        raw = json.dumps(self.payload).encode("utf-8")
        path = raw_root / "stub.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return forward.ForwardFetchResult(
            family="indices",
            source_name=self.source_name,
            requested_date=target_date,
            report_date=self.report_date,
            fetch_time_utc=datetime.now(timezone.utc),
            source_url=self.source_url,
            payload=raw,
            text=raw.decode("utf-8"),
            payload_hash="b" * 64,
            row_count=4,
            raw_payload_path=path,
        )


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.saved = (forward.RAW_ROOT, forward.PROCESSED_ROOT, forward.VALIDATION_ROOT, daily.VALIDATION_ROOT)
        forward.RAW_ROOT = self.root / "raw"
        forward.PROCESSED_ROOT = self.root / "processed"
        forward.VALIDATION_ROOT = self.root / "validation"
        daily.VALIDATION_ROOT = forward.VALIDATION_ROOT
        self.addCleanup(self.tmpdir.cleanup)

    def tearDown(self) -> None:
        forward.RAW_ROOT, forward.PROCESSED_ROOT, forward.VALIDATION_ROOT, daily.VALIDATION_ROOT = self.saved

    def _run(self, target_date: date, report_date: date | None = SETTLED_DATE):
        return daily.run_daily_indices_ingestion(
            target_date=target_date,
            adapter=StubAdapter(summary_payload(), report_date),
            raw_root=forward.RAW_ROOT,
        )

    def test_settled_payload_for_its_own_date_is_accepted(self) -> None:
        result = self._run(SETTLED_DATE)
        self.assertTrue(result.passed)
        self.assertEqual(len(result.accepted), 4)

    def test_payload_for_another_day_is_quarantined(self) -> None:
        # The endpoint ignores a requested date and always answers with the
        # settled day, so this is the gate that stops a stale snapshot being
        # stamped onto today.
        result = self._run(date(2026, 9, 9))
        self.assertFalse(result.passed)
        self.assertEqual(len(result.accepted), 0)
        self.assertTrue(any("source date mismatch" in item for item in result.failures))

    def test_accepted_run_writes_canonical_rows(self) -> None:
        self._run(SETTLED_DATE)
        written = forward.RAW_ROOT / "accepted/indices" / SETTLED_DATE.isoformat()
        rows = pd.read_csv(next(written.rglob("canonical_indices.csv")))
        self.assertEqual(set(rows["index_name"]), {"ASPI", "SL20", "SL20TRI", "ASTRI"})

    def test_quarantined_run_writes_no_accepted_rows(self) -> None:
        self._run(date(2026, 9, 9))
        written = forward.RAW_ROOT / "accepted/indices"
        self.assertEqual(list(written.rglob("canonical_indices.csv")), [])

    def test_target_before_2026_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self._run(date(2025, 12, 31))


if __name__ == "__main__":
    unittest.main()
