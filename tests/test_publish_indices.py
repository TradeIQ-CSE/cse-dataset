import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from scripts.publish_eod import DeliveryError
from scripts.publish_indices import (
    build_request,
    deliver,
    find_accepted_source,
    index_close_string,
    load_accepted_values,
)

SOURCE = "cse_daily_market_summary_indices"
TARGET = date(2026, 9, 18)


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


def valid_request():
    return {
        "trade_date": "2026-09-18",
        "calendar": {"is_trading_day": True, "source": "CSE circular 07-10-2025"},
        "values": [
            {"code": "ASPI", "close": "21056.26"},
            {"code": "SL20", "close": "5928.89"},
        ],
    }


class IndexCloseStringTests(unittest.TestCase):
    def test_keeps_the_places_the_exchange_published(self):
        self.assertEqual(index_close_string("21056.26"), "21056.26")
        self.assertEqual(index_close_string("5928.8900"), "5928.89")
        self.assertEqual(index_close_string("12591"), "12591")

    def test_renders_a_whole_number_without_an_exponent(self):
        # Decimal.normalize() would make this 2.1056E+4, which the DTO refuses.
        self.assertEqual(index_close_string("21056.0000"), "21056")

    def test_refuses_zero_and_negative_closes(self):
        for value in ("0", "0.0000", "-1.5"):
            with self.subTest(value=value):
                with self.assertRaises(DeliveryError):
                    index_close_string(value)

    def test_refuses_more_than_four_places(self):
        with self.assertRaises(DeliveryError):
            index_close_string("21056.123456")

    def test_refuses_a_missing_or_unparseable_close(self):
        for value in (None, float("nan"), "n/a"):
            with self.subTest(value=value):
                with self.assertRaises(DeliveryError):
                    index_close_string(value)


class LoadAcceptedValuesTests(unittest.TestCase):
    def write_accepted(self, root: Path, rows) -> Path:
        directory = root / "accepted" / "indices" / TARGET.isoformat() / SOURCE
        directory.mkdir(parents=True)
        pd.DataFrame(rows).to_csv(directory / "canonical_indices.csv", index=False)
        return root

    def test_reads_and_sorts_the_deliverable_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.write_accepted(
                Path(tmp),
                [
                    {"date": "2026-09-18", "index_name": "SL20", "close": "5928.89"},
                    {"date": "2026-09-18", "index_name": "ASPI", "close": "21056.26"},
                ],
            )
            values = load_accepted_values(TARGET, SOURCE, raw_root=root)
        self.assertEqual(
            values,
            [
                {"code": "ASPI", "close": "21056.26"},
                {"code": "SL20", "close": "5928.89"},
            ],
        )

    def test_drops_a_series_the_platform_does_not_ship(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.write_accepted(
                Path(tmp),
                [
                    {"date": "2026-09-18", "index_name": "ASPI", "close": "21056.26"},
                    {"date": "2026-09-18", "index_name": "MPI", "close": "1234.5"},
                ],
            )
            values = load_accepted_values(TARGET, SOURCE, raw_root=root)
        self.assertEqual([value["code"] for value in values], ["ASPI"])

    def test_refuses_rows_dated_elsewhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.write_accepted(
                Path(tmp),
                [{"date": "2026-09-17", "index_name": "ASPI", "close": "21056.26"}],
            )
            with self.assertRaises(DeliveryError):
                load_accepted_values(TARGET, SOURCE, raw_root=root)

    def test_refuses_a_repeated_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.write_accepted(
                Path(tmp),
                [
                    {"date": "2026-09-18", "index_name": "ASPI", "close": "21056.26"},
                    {"date": "2026-09-18", "index_name": "ASPI", "close": "21056.27"},
                ],
            )
            with self.assertRaises(DeliveryError):
                load_accepted_values(TARGET, SOURCE, raw_root=root)

    def test_refuses_a_day_with_nothing_deliverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.write_accepted(
                Path(tmp),
                [{"date": "2026-09-18", "index_name": "MPI", "close": "1234.5"}],
            )
            with self.assertRaises(DeliveryError):
                load_accepted_values(TARGET, SOURCE, raw_root=root)

    def test_refuses_a_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(DeliveryError):
                load_accepted_values(TARGET, SOURCE, raw_root=Path(tmp))


class FindAcceptedSourceTests(unittest.TestCase):
    def write_summary(self, root: Path, source: str, status: str) -> None:
        directory = root / "indices" / TARGET.isoformat() / source
        directory.mkdir(parents=True)
        (directory / "forward_summary.json").write_text(
            json.dumps({"status": status, "target_date": TARGET.isoformat()})
        )

    def test_finds_the_accepted_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_summary(root, SOURCE, "accepted")
            self.assertEqual(
                find_accepted_source(TARGET, validation_root=root), SOURCE
            )

    def test_refuses_a_quarantined_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_summary(root, SOURCE, "quarantined")
            with self.assertRaises(DeliveryError):
                find_accepted_source(TARGET, validation_root=root)

    def test_refuses_to_choose_between_two_accepted_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_summary(root, SOURCE, "accepted")
            self.write_summary(root, "another_adapter", "accepted")
            with self.assertRaises(DeliveryError):
                find_accepted_source(TARGET, validation_root=root)

    def test_refuses_a_date_with_no_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(DeliveryError):
                find_accepted_source(TARGET, validation_root=Path(tmp))


class BuildRequestTests(unittest.TestCase):
    def test_builds_the_contract_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            accepted = root / "raw" / "accepted" / "indices" / TARGET.isoformat() / SOURCE
            accepted.mkdir(parents=True)
            pd.DataFrame(
                [{"date": "2026-09-18", "index_name": "ASPI", "close": "21056.26"}]
            ).to_csv(accepted / "canonical_indices.csv", index=False)
            calendar = root / "calendar.csv"
            pd.DataFrame(
                [
                    {
                        "date": "2026-09-18",
                        "is_trading_day": "true",
                        "source": "CSE circular 07-10-2025",
                        "verified_at": "2025-10-22T00:00:00Z",
                    }
                ]
            ).to_csv(calendar, index=False)

            body = build_request(
                TARGET,
                calendar,
                source_name=SOURCE,
                raw_root=root / "raw",
            )

        self.assertEqual(body["trade_date"], "2026-09-18")
        # verified_at belongs to the price contract; the index route takes neither
        # it nor any other extra field.
        self.assertEqual(
            body["calendar"],
            {"is_trading_day": True, "source": "CSE circular 07-10-2025"},
        )
        self.assertEqual(body["values"], [{"code": "ASPI", "close": "21056.26"}])

    def test_refuses_a_non_trading_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calendar = root / "calendar.csv"
            pd.DataFrame(
                [
                    {
                        "date": "2026-09-18",
                        "is_trading_day": "false",
                        "source": "CSE circular 07-10-2025",
                        "verified_at": "2025-10-22T00:00:00Z",
                    }
                ]
            ).to_csv(calendar, index=False)
            with self.assertRaises(DeliveryError):
                build_request(TARGET, calendar, source_name=SOURCE, raw_root=root)


class DeliverTests(unittest.TestCase):
    def test_posts_and_returns_the_receipt(self):
        session = FakeSession(
            [
                FakeResponse(
                    201,
                    {
                        "data": {
                            "trade_date": "2026-09-18",
                            "stored": ["ASPI"],
                            "unchanged": ["SL20"],
                        }
                    },
                )
            ]
        )
        receipt = deliver(
            valid_request(),
            api_url="https://tradeiqcse.tech/api/market",
            token="t",
            session=session,
        )
        self.assertEqual(receipt["stored"], ["ASPI"])
        method, url, _ = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(
            url, "https://tradeiqcse.tech/api/market/internal/v1/ingestions/indices"
        )

    def test_refuses_a_receipt_that_drops_a_code(self):
        session = FakeSession(
            [
                FakeResponse(
                    201,
                    {
                        "data": {
                            "trade_date": "2026-09-18",
                            "stored": ["ASPI"],
                            "unchanged": [],
                        }
                    },
                )
            ]
        )
        with self.assertRaises(DeliveryError):
            deliver(
                valid_request(),
                api_url="https://tradeiqcse.tech/api/market",
                token="t",
                session=session,
            )

    def test_refuses_a_receipt_for_another_date(self):
        session = FakeSession(
            [
                FakeResponse(
                    201,
                    {
                        "data": {
                            "trade_date": "2026-09-17",
                            "stored": ["ASPI", "SL20"],
                            "unchanged": [],
                        }
                    },
                )
            ]
        )
        with self.assertRaises(DeliveryError):
            deliver(
                valid_request(),
                api_url="https://tradeiqcse.tech/api/market",
                token="t",
                session=session,
            )

    def test_surfaces_a_conflict(self):
        session = FakeSession(
            [FakeResponse(409, {"error": {"message": "already has a different close"}})]
        )
        with self.assertRaises(DeliveryError) as raised:
            deliver(
                valid_request(),
                api_url="https://tradeiqcse.tech/api/market",
                token="t",
                session=session,
            )
        self.assertIn("409", str(raised.exception))

    def test_refuses_plain_http_off_localhost(self):
        with self.assertRaises(DeliveryError):
            deliver(
                valid_request(),
                api_url="http://tradeiqcse.tech/api/market",
                token="t",
                session=FakeSession([]),
            )

    def test_allows_loopback_for_a_local_dry_run(self):
        session = FakeSession(
            [
                FakeResponse(
                    201,
                    {
                        "data": {
                            "trade_date": "2026-09-18",
                            "stored": ["ASPI", "SL20"],
                            "unchanged": [],
                        }
                    },
                )
            ]
        )
        receipt = deliver(
            valid_request(),
            api_url="http://localhost:3001",
            token="t",
            session=session,
        )
        self.assertEqual(receipt["trade_date"], "2026-09-18")

    def test_refuses_an_empty_token(self):
        with self.assertRaises(DeliveryError):
            deliver(
                valid_request(),
                api_url="https://tradeiqcse.tech/api/market",
                token="",
                session=FakeSession([]),
            )


if __name__ == "__main__":
    unittest.main()
