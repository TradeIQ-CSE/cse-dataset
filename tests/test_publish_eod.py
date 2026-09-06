import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from scripts.publish_eod import DeliveryError, ROOT, build_request, deliver


class FakeResponse:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class PublishEodTests(unittest.TestCase):
    def make_files(self, root: Path, *, status="accepted", rejected=0):
        accepted = root / "accepted.csv"
        metadata = root / "metadata.csv"
        calendar = root / "calendar.csv"
        result = root / "result.json"
        pd.DataFrame(
            [
                {
                    "date": "2026-09-04",
                    "symbol": "TEST.N0000",
                    "open": 10,
                    "high": 12,
                    "low": 9.5,
                    "close": 11.25,
                    "volume": 1234,
                    "validation_warnings": "",
                    "ohlc_repaired": False,
                }
            ]
        ).to_csv(accepted, index=False)
        pd.DataFrame(
            [
                {
                    "symbol": "TEST.N0000",
                    "company_name": "Test PLC",
                    "shares_outstanding": 1000000,
                }
            ]
        ).to_csv(metadata, index=False)
        pd.DataFrame(
            [
                {
                    "date": "2026-09-04",
                    "is_trading_day": "true",
                    "source": "test CSE calendar",
                    "verified_at": "2026-09-01T00:00:00Z",
                }
            ]
        ).to_csv(calendar, index=False)
        result.write_text(
            json.dumps(
                {
                    "contract_version": "1",
                    "status": status,
                    "target_date": "2026-09-04",
                    "source_name": "cse_trade_summary_current",
                    "captured_at": "2026-09-04T09:18:00Z",
                    "source_date_method": "colombo_capture_date",
                    "raw_payload_hash": "b" * 64,
                    "accepted_path": str(accepted.relative_to(ROOT)),
                    "metadata_path": str(metadata.relative_to(ROOT)),
                    "validation": {
                        "row_count": 1,
                        "accepted_rows": 1,
                        "rejected_rows": rejected,
                        "ohlc_repaired_rows": 0,
                        "failures": [],
                    },
                }
            )
        )
        return result, calendar

    def test_builds_deterministic_contract_from_current_accepted_artifact(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "data") as directory:
            result, calendar = self.make_files(Path(directory))
            with patch("scripts.publish_eod._git_commit", return_value="c" * 40):
                first = build_request(result, calendar)
                second = build_request(result, calendar)

        self.assertEqual(first, second)
        self.assertEqual(first["validation"], {"processed": 1, "accepted": 1, "rejected": 0, "repaired": 0})
        self.assertEqual(first["prices"][0]["close"], "11.2500")
        self.assertEqual(first["securities"][0]["shares_outstanding"], "1000000")
        self.assertEqual(len(first["batch_id"]), 64)
        self.assertEqual(len(first["market_digest"]), 64)

    def test_refuses_rejected_current_invocation(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "data") as directory:
            result, calendar = self.make_files(Path(directory), status="rejected")
            with self.assertRaisesRegex(DeliveryError, "current collection invocation"):
                build_request(result, calendar)

    def test_refuses_before_close_capture(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "data") as directory:
            result, calendar = self.make_files(Path(directory))
            payload = json.loads(result.read_text())
            payload["captured_at"] = "2026-09-04T08:00:00Z"
            result.write_text(json.dumps(payload))
            with self.assertRaisesRegex(DeliveryError, "before the configured"):
                build_request(result, calendar)

    @patch("scripts.publish_eod.time.sleep", return_value=None)
    def test_retries_transient_post_and_returns_receipt(self, _sleep):
        request_body = {"trade_date": "2026-09-04", "market_digest": "a" * 64}
        session = FakeSession(
            [
                FakeResponse(200, {"data": None}),
                FakeResponse(503, {"error": {"code": "INTERNAL"}}),
                FakeResponse(201, {"data": {"batch_id": "b" * 64}}),
            ]
        )
        receipt = deliver(
            request_body,
            api_url="http://localhost:3001",
            token="secret",
            session=session,
        )
        self.assertEqual(receipt["batch_id"], "b" * 64)
        self.assertEqual([call[0] for call in session.calls], ["GET", "POST", "POST"])

    def test_rejects_repeated_snapshot_before_posting(self):
        request_body = {"trade_date": "2026-09-04", "market_digest": "a" * 64}
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {"data": {"trade_date": "2026-09-03", "market_digest": "a" * 64}},
                )
            ]
        )
        with self.assertRaisesRegex(DeliveryError, "repeats the accepted snapshot"):
            deliver(
                request_body,
                api_url="http://localhost:3001",
                token="secret",
                session=session,
            )
        self.assertEqual(len(session.calls), 1)


if __name__ == "__main__":
    unittest.main()
