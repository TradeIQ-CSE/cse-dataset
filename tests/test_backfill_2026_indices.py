from __future__ import annotations

from datetime import date, datetime, time
import tempfile
import unittest
from pathlib import Path

import pandas as pd

import scripts.backfill_2026_indices as backfill
from scripts.forward_ingestion import COLOMBO_TZ, MissingSourceError


def point(day: date, at: time, value: float) -> dict:
    stamp = datetime.combine(day, at, tzinfo=COLOMBO_TZ)
    return {"d": int(stamp.timestamp() * 1000), "v": value, "pc": None}


AFTER_CLOSE = time(14, 48)
BEFORE_OPEN = time(8, 16)


def archive_window(start: date, days: int, base: float = 20000.0) -> dict[date, float]:
    return {start + pd.Timedelta(days=i).to_pytimedelta(): base + i for i in range(days)}


def payload_from(archive: dict[date, float], at: time = AFTER_CLOSE) -> list[dict]:
    return [point(day, at, close) for day, close in sorted(archive.items())]


class PointParsingTests(unittest.TestCase):
    def test_stamp_is_read_in_colombo_time(self) -> None:
        parsed = backfill.parse_points([point(date(2026, 3, 2), AFTER_CLOSE, 21000.0)])
        self.assertEqual(parsed[0].trading_date, date(2026, 3, 2))
        self.assertTrue(parsed[0].is_settled)

    def test_point_before_the_close_is_not_settled(self) -> None:
        parsed = backfill.parse_points([point(date(2026, 3, 2), BEFORE_OPEN, 21000.0)])
        self.assertFalse(parsed[0].is_settled)

    def test_empty_payload_is_refused(self) -> None:
        with self.assertRaises(MissingSourceError):
            backfill.parse_points([])

    def test_entries_without_a_value_are_skipped(self) -> None:
        good = point(date(2026, 3, 2), AFTER_CLOSE, 21000.0)
        with self.assertRaises(MissingSourceError):
            backfill.parse_points([{"d": good["d"], "v": None}])


class CrossCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.archive = archive_window(date(2025, 10, 1), 30)

    def _candidates(self, payload: list[dict]) -> pd.DataFrame:
        return backfill.build_candidates(backfill.parse_points(payload), "a" * 64)

    def test_agreeing_window_passes(self) -> None:
        metrics = backfill.cross_check(
            self._candidates(payload_from(self.archive)), self.archive, min_overlap=20
        )
        self.assertEqual(metrics["mismatched_points"], 0)
        self.assertEqual(metrics["overlap_points"], 30)

    def test_one_disagreeing_point_fails_the_run(self) -> None:
        drifted = dict(self.archive)
        bad_day = sorted(drifted)[5]
        drifted[bad_day] = drifted[bad_day] + 25.0
        with self.assertRaises(backfill.CrossCheckError):
            backfill.cross_check(self._candidates(payload_from(drifted)), self.archive, min_overlap=20)

    def test_too_little_overlap_fails_the_run(self) -> None:
        # An unproven source must not be trusted just because nothing
        # contradicted it.
        small = archive_window(date(2025, 10, 1), 5)
        with self.assertRaises(backfill.CrossCheckError):
            backfill.cross_check(self._candidates(payload_from(small)), self.archive, min_overlap=20)

    def test_unsettled_points_are_not_used_to_prove_the_source(self) -> None:
        # 08:16 points disagree with the archive; counting them would fail a
        # run whose publishable points are all correct.
        settled = payload_from(self.archive)
        drifted = {day: close + 30.0 for day, close in self.archive.items()}
        noise = payload_from(drifted, at=BEFORE_OPEN)
        metrics = backfill.cross_check(
            self._candidates(settled + noise), self.archive, min_overlap=20
        )
        self.assertEqual(metrics["mismatched_points"], 0)


class SplitTests(unittest.TestCase):
    def _candidates(self, payload: list[dict]) -> pd.DataFrame:
        return backfill.build_candidates(backfill.parse_points(payload), "a" * 64)

    def test_point_before_the_close_is_held_back(self) -> None:
        frame = self._candidates([point(date(2026, 3, 2), BEFORE_OPEN, 21000.0)])
        keep, held = backfill.split_unsettled(frame, today=date(2026, 3, 10))
        self.assertEqual(len(keep), 0)
        self.assertIn("before the", held["rejection_reason"].iloc[0])

    def test_current_trading_day_is_held_back(self) -> None:
        frame = self._candidates([point(date(2026, 3, 10), AFTER_CLOSE, 21000.0)])
        keep, held = backfill.split_unsettled(frame, today=date(2026, 3, 10))
        self.assertEqual(len(keep), 0)
        self.assertIn("not settled", held["rejection_reason"].iloc[0])

    def test_settled_earlier_day_is_kept(self) -> None:
        frame = self._candidates([point(date(2026, 3, 9), AFTER_CLOSE, 21000.0)])
        keep, held = backfill.split_unsettled(frame, today=date(2026, 3, 10))
        self.assertEqual(len(keep), 1)
        self.assertEqual(len(held), 0)


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.archive = archive_window(date(2025, 11, 1), 40)
        self.today = date(2026, 3, 10)
        forward_days = {
            date(2026, 2, 2) + pd.Timedelta(days=i).to_pytimedelta(): 21000.0 + i for i in range(5)
        }
        self.payload = payload_from(self.archive) + payload_from(forward_days)

    def _run(self, payload=None):
        return backfill.run(
            today=self.today,
            payload=payload or self.payload,
            payload_hash="c" * 64,
            archive=self.archive,
            min_overlap=20,
        )

    def test_only_forward_rows_are_published(self) -> None:
        result, _, _ = self._run()
        days = sorted(result.accepted["date"])
        self.assertTrue(all(day >= date(2026, 1, 1) for day in days))
        self.assertEqual(len(days), 5)

    def test_archive_window_is_not_republished(self) -> None:
        # Everything through 2025-12-31 already has an official row.
        result, _, _ = self._run()
        self.assertEqual(len(set(result.accepted["date"]) & set(self.archive)), 0)

    def test_archive_window_is_not_reported_as_rejections(self) -> None:
        # Those dates are out of this backfill's scope, not bad rows. Letting
        # them reach the validator buries the real rejections in 40 rows of
        # "precedes archive start".
        result, _, _ = self._run()
        if result.rejected.empty:
            return
        self.assertEqual(len(set(result.rejected["date"]) & set(self.archive)), 0)

    def test_unsettled_points_are_reported_not_published(self) -> None:
        noisy = self.payload + [point(date(2026, 2, 20), BEFORE_OPEN, 21500.0)]
        result, held_back, _ = self._run(noisy)
        self.assertNotIn(date(2026, 2, 20), set(result.accepted["date"]))
        self.assertIn(date(2026, 2, 20), set(held_back["date"]))

    def test_a_failing_cross_check_stops_the_run(self) -> None:
        drifted = dict(self.archive)
        bad_day = sorted(drifted)[3]
        drifted[bad_day] = drifted[bad_day] + 50.0
        payload = payload_from(drifted) + payload_from(
            {date(2026, 2, 2): 21000.0}
        )
        with self.assertRaises(backfill.CrossCheckError):
            self._run(payload)

    def test_accepted_rows_carry_the_contract_columns(self) -> None:
        result, _, _ = self._run()
        for column in ["date", "index_name", "close", "source", "source_timestamp", "raw_payload_hash"]:
            self.assertIn(column, result.accepted.columns)
        self.assertEqual(set(result.accepted["index_name"]), {"ASPI"})


if __name__ == "__main__":
    unittest.main()
