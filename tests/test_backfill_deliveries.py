import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from scripts.backfill_deliveries import (
    choose_run,
    discover_sessions,
    run,
)
from scripts.publish_eod import DeliveryError

COLOMBO_TZ = ZoneInfo("Asia/Colombo")
SESSION = date(2026, 9, 18)
SOURCE = "cse_trade_summary_current"
INDEX_SOURCE = "cse_daily_market_summary_indices"


class FakeResponse:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class RoutedSession:
    """Answers by endpoint, so one fake serves both delivery paths.

    With echo_eod it builds the price receipt from the request it was given,
    which is what the real API does and what the sender verifies; a canned
    receipt cannot know the batch_id the sender derived.
    """

    def __init__(self, *, latest=None, eod=None, indices=None, echo_eod=False):
        self.latest = latest or FakeResponse(200, {"data": None})
        self.eod = eod
        self.indices = indices
        self.echo_eod = echo_eod
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        if url.endswith("/latest"):
            return self.latest
        if url.endswith("ingestions/indices"):
            return self.indices
        if self.echo_eod:
            body = kwargs["json"]
            return FakeResponse(
                201,
                {
                    "data": {
                        "batch_id": body["batch_id"],
                        "trade_date": body["trade_date"],
                        "status": "succeeded",
                        "market_digest": body["market_digest"],
                        "records_accepted": body["validation"]["accepted"],
                    }
                },
            )
        return self.eod


def write_payload(
    run_dir: Path,
    session: date,
    *,
    captured_at: str,
    filed_under: date | None = None,
) -> None:
    """The raw snapshot a capture keeps, which the replay re-validates.

    `filed_under` differs from the session for pre-TIQ-134 captures, which were
    filed by the run's Colombo date; the rows' lastTradedTime is what actually
    names the session.
    """
    last_traded = datetime(
        session.year, session.month, session.day, 14, 0, tzinfo=COLOMBO_TZ
    )
    payload = {
        "reqTradeSummery": [
            {
                "symbol": "COMB.N0000",
                "open": 141.0,
                "high": 144.0,
                "low": 140.5,
                "closingPrice": 142.72,
                "sharevolume": 512800,
                "turnover": 73187616.0,
                "tradevolume": 259,
                "lastTradedTime": int(last_traded.timestamp() * 1000),
            }
        ]
    }
    directory = (
        run_dir
        / "data/raw/ohlcv/source_payloads"
        / (filed_under or session).isoformat()
        / SOURCE
    )
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "payload.json").write_text(json.dumps(payload))
    (directory / "metadata.json").write_text(
        json.dumps(
            {
                "requested_date": (filed_under or session).isoformat(),
                "fetch_time_utc": captured_at,
                "source_name": SOURCE,
            }
        )
    )


def write_capture(
    captures: Path,
    session: date,
    run_id: str,
    *,
    status="accepted",
    digest="d" * 64,
    captured_at="2026-09-18T16:44:11Z",
    with_indices=True,
    with_payload=True,
    filed_under: date | None = None,
) -> Path:
    run_dir = captures / session.isoformat() / run_id
    accepted = (
        run_dir
        / "data/raw/ohlcv/accepted"
        / session.isoformat()
        / SOURCE
    )
    accepted.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "date": session.isoformat(),
                "symbol": "COMB.N0000",
                "open": "141.0000",
                "high": "144.0000",
                "low": "140.5000",
                "close": "142.7200",
                "volume": "512800",
            }
        ]
    ).to_csv(accepted / "canonical_ohlcv.csv", index=False)

    processed = run_dir / "data/processed"
    processed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "COMB.N0000",
                "company_name": "COMMERCIAL BANK OF CEYLON PLC",
                "shares_outstanding": "1556891381",
            }
        ]
    ).to_csv(processed / "company_metadata.csv", index=False)

    result_dir = run_dir / "data/run"
    result_dir.mkdir(parents=True)
    (result_dir / "daily_result.json").write_text(
        json.dumps(
            {
                "contract_version": "1",
                "status": status,
                "target_date": session.isoformat(),
                "source_name": SOURCE,
                "captured_at": captured_at,
                "source_date_method": "last_traded_time",
                "raw_payload_hash": "a" * 64,
                "accepted_path": (
                    f"data/raw/ohlcv/accepted/{session.isoformat()}/{SOURCE}/canonical_ohlcv.csv"
                ),
                "metadata_path": "data/processed/company_metadata.csv",
                "validation": {
                    "row_count": 1,
                    "accepted_rows": 1,
                    "rejected_rows": 0,
                    "market_digest": digest,
                    "ohlc_repaired_rows": 0,
                    "failures": [],
                },
            }
        )
    )

    if with_payload:
        write_payload(
            run_dir, session, captured_at=captured_at, filed_under=filed_under
        )

    if with_indices:
        index_accepted = (
            run_dir
            / "data/raw/2026_forward/accepted/indices"
            / session.isoformat()
            / INDEX_SOURCE
        )
        index_accepted.mkdir(parents=True)
        pd.DataFrame(
            [
                {
                    "date": session.isoformat(),
                    "index_name": "ASPI",
                    "close": "21056.26",
                }
            ]
        ).to_csv(index_accepted / "canonical_indices.csv", index=False)
        summary_dir = (
            run_dir
            / "data/processed/validation/2026_forward/indices"
            / session.isoformat()
            / INDEX_SOURCE
        )
        summary_dir.mkdir(parents=True)
        (summary_dir / "forward_summary.json").write_text(
            json.dumps({"status": "accepted", "target_date": session.isoformat()})
        )
    return run_dir


def write_calendar(root: Path) -> Path:
    path = root / "calendar.csv"
    pd.DataFrame(
        [
            {
                "date": SESSION.isoformat(),
                "is_trading_day": "true",
                "source": "CSE circular 07-10-2025",
                "verified_at": "2025-10-22T00:00:00Z",
            }
        ]
    ).to_csv(path, index=False)
    return path


class DiscoverSessionsTests(unittest.TestCase):
    def test_lists_session_directories_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            captures = Path(tmp)
            for name in ("2026-09-18", "2026-06-15", "README.md", "not-a-date"):
                (captures / name).mkdir()
            sessions = discover_sessions(captures)
        self.assertEqual(
            sessions, [date(2026, 6, 15), date(2026, 9, 18)]
        )

    def test_applies_the_date_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            captures = Path(tmp)
            for name in ("2026-09-18", "2026-06-15", "2026-07-01"):
                (captures / name).mkdir()
            sessions = discover_sessions(
                captures, since=date(2026, 7, 1), until=date(2026, 7, 31)
            )
        self.assertEqual(sessions, [date(2026, 7, 1)])


class ChooseRunTests(unittest.TestCase):
    def test_skips_a_quarantined_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            captures = Path(tmp)
            write_capture(captures, SESSION, "1", status="quarantined")
            good = write_capture(captures, SESSION, "2")
            self.assertEqual(choose_run(captures, SESSION), good)

    def test_refuses_two_accepted_runs_that_disagree(self):
        with tempfile.TemporaryDirectory() as tmp:
            captures = Path(tmp)
            write_capture(captures, SESSION, "1", digest="a" * 64)
            write_capture(captures, SESSION, "2", digest="b" * 64)
            with self.assertRaises(DeliveryError) as raised:
                choose_run(captures, SESSION)
            self.assertIn("different market", str(raised.exception))

    def test_accepts_two_runs_that_agree(self):
        with tempfile.TemporaryDirectory() as tmp:
            captures = Path(tmp)
            write_capture(captures, SESSION, "1")
            second = write_capture(captures, SESSION, "2")
            self.assertEqual(choose_run(captures, SESSION), second)

    def test_reports_a_session_with_no_accepted_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            captures = Path(tmp)
            write_capture(captures, SESSION, "1", status="quarantined")
            with self.assertRaises(DeliveryError):
                choose_run(captures, SESSION)


class RunTests(unittest.TestCase):
    def test_dry_run_prepares_both_requests_without_sending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            captures = root / "captures"
            captures.mkdir()
            write_capture(captures, SESSION, "1")
            session_obj = RoutedSession()
            report = run(
                captures_path=captures,
                calendar_path=write_calendar(root),
                out_dir=root / "out",
                api_url="",
                token="",
                dry_run=True,
                session_obj=session_obj,
            )
        self.assertEqual(session_obj.calls, [])
        self.assertEqual(report.sessions[0].prices, "prepared")
        self.assertEqual(report.sessions[0].indices, "prepared")

    def test_a_session_the_platform_already_holds_is_not_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            captures = root / "captures"
            captures.mkdir()
            write_capture(captures, SESSION, "1")
            session_obj = RoutedSession(
                eod=FakeResponse(409, {"error": {"message": "date already ingested"}}),
                indices=FakeResponse(
                    201,
                    {
                        "data": {
                            "trade_date": SESSION.isoformat(),
                            "stored": [],
                            "unchanged": ["ASPI"],
                        }
                    },
                ),
            )
            report = run(
                captures_path=captures,
                calendar_path=write_calendar(root),
                out_dir=root / "out",
                api_url="https://tradeiqcse.tech/api/market",
                token="t",
                session_obj=session_obj,
            )
        outcome = report.sessions[0]
        self.assertEqual(outcome.prices, "already-held")
        self.assertEqual(outcome.indices, "delivered")
        self.assertFalse(outcome.failed)

    def test_delivers_a_session_the_platform_lacks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            captures = root / "captures"
            captures.mkdir()
            write_capture(captures, SESSION, "1")
            session_obj = RoutedSession(
                echo_eod=True,
                indices=FakeResponse(
                    201,
                    {
                        "data": {
                            "trade_date": SESSION.isoformat(),
                            "stored": ["ASPI"],
                            "unchanged": [],
                        }
                    },
                ),
            )
            out_dir = root / "out"
            report = run(
                captures_path=captures,
                calendar_path=write_calendar(root),
                out_dir=out_dir,
                api_url="https://tradeiqcse.tech/api/market",
                token="t",
                session_obj=session_obj,
            )
            outcome = report.sessions[0]
            self.assertEqual(outcome.prices, "delivered")
            self.assertEqual(outcome.prices_detail, "1 rows")
            self.assertEqual(outcome.indices, "delivered")
            written = out_dir / SESSION.isoformat()
            for name in (
                "eod_ingestion_request.json",
                "eod_ingestion_receipt.json",
                "index_ingestion_request.json",
                "index_ingestion_receipt.json",
            ):
                self.assertTrue((written / name).is_file(), name)

    def test_a_capture_taken_after_midnight_is_still_deliverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            captures = root / "captures"
            captures.mkdir()
            # 00:20 Colombo the next day: the daily path refuses this, the
            # backfill must not, because it is after the session's close.
            write_capture(
                captures, SESSION, "1", captured_at="2026-09-18T18:50:00Z"
            )
            report = run(
                captures_path=captures,
                calendar_path=write_calendar(root),
                out_dir=root / "out",
                api_url="",
                token="",
                dry_run=True,
                session_obj=RoutedSession(),
            )
        self.assertEqual(report.sessions[0].prices, "prepared")

    def test_a_capture_taken_before_the_close_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            captures = root / "captures"
            captures.mkdir()
            # 10:00 Colombo, mid-session.
            write_capture(
                captures, SESSION, "1", captured_at="2026-09-18T04:30:00Z"
            )
            report = run(
                captures_path=captures,
                calendar_path=write_calendar(root),
                out_dir=root / "out",
                api_url="",
                token="",
                dry_run=True,
                session_obj=RoutedSession(),
            )
        self.assertEqual(report.sessions[0].prices, "failed")
        self.assertIn("precedes", report.sessions[0].prices_detail)

    def test_a_session_without_index_values_is_not_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            captures = root / "captures"
            captures.mkdir()
            write_capture(captures, SESSION, "1", with_indices=False)
            report = run(
                captures_path=captures,
                calendar_path=write_calendar(root),
                out_dir=root / "out",
                api_url="",
                token="",
                dry_run=True,
                session_obj=RoutedSession(),
            )
        outcome = report.sessions[0]
        self.assertEqual(outcome.indices, "unavailable")
        self.assertFalse(outcome.failed)


if __name__ == "__main__":
    unittest.main()
