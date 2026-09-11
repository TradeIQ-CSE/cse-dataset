"""Add metadata rows for symbols that traded but are no longer in the active list.

01_collect_metadata.py covers only securities listed today, so a price history
that includes delisted companies and expired rights lines has symbols with no
metadata row, and the artifact contract requires one for every traded symbol.
``companyInfoSummery`` still answers for those symbols; this fills them in from
it and marks them delisted when ``allSecurityCode`` no longer lists them.

A few symbols are unknown to the API altogether (CSEC.N0000 answers 404). For
those the name falls back to the SHORT NAME column of the official price files
the symbol traded in, which is abbreviated but is the exchange's own label.

Only fields a source states are written. ``listing_date`` stays empty: for rights
and preference lines ``issueDate`` is the company's date, not the line's
(AAF.R0000 and AAF.P0000 both answer 12/JAN/2012), so it cannot be trusted as a
listing date for any filled row.
"""

from __future__ import annotations

import argparse
import csv
import time
from datetime import date
from pathlib import Path
from typing import Callable

import requests

try:
    from .backfill_ohlcv import find_header_row, normalize_header, read_source_tables, security_symbols
except ImportError:  # pragma: no cover - used when executed directly.
    from backfill_ohlcv import find_header_row, normalize_header, read_source_tables, security_symbols

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_METADATA = ROOT / "data/processed/company_metadata.csv"
DEFAULT_ACCEPTED_ROOT = ROOT / "data/raw/ohlcv/accepted"
DEFAULT_SOURCES = ROOT / "config/release_sources.txt"
ACTIVE_URL = "https://www.cse.lk/api/allSecurityCode"
INFO_URL = "https://www.cse.lk/api/companyInfoSummery"
HEADERS = {"User-Agent": "Mozilla/5.0"}
REQUEST_TIMEOUT_SECONDS = 15
REQUEST_SLEEP_SECONDS = 0.1


def accepted_symbols(accepted_root: Path, start: date | None = None, end: date | None = None) -> set[str]:
    """Every symbol in an accepted OHLCV file whose trading date is inside the window."""
    symbols: set[str] = set()
    for path in sorted(accepted_root.glob("*/*/canonical_ohlcv.csv")):
        day = path.parent.parent.name
        if (start and day < start.isoformat()) or (end and day > end.isoformat()):
            continue
        with path.open(newline="", encoding="utf-8") as handle:
            symbols.update(row["symbol"].strip() for row in csv.DictReader(handle))
    return symbols


def official_short_names(sources: list[Path]) -> dict[str, str]:
    """SHORT NAME for each symbol in the flat-layout official price files; a later file wins."""
    names: dict[str, str] = {}
    for path in sources:
        for raw in read_source_tables(path):
            try:
                header_idx = find_header_row(raw)
            except ValueError:
                continue  # the 2002-2015 block layout has no SHORT NAME column; no release source uses it
            columns = [normalize_header(value) for value in raw.iloc[header_idx].tolist()]
            if "short name" not in columns:
                continue
            table = raw.iloc[header_idx + 1 :]
            symbols = security_symbols(
                table.iloc[:, columns.index("company id")],
                table.iloc[:, columns.index("main type")],
                table.iloc[:, columns.index("sub type")],
            )
            for symbol, name in zip(symbols, table.iloc[:, columns.index("short name")]):
                text = str(name or "").strip()
                if symbol and text and text.lower() not in {"nan", "none"}:
                    names[symbol] = text
    return names


def metadata_row(
    symbol: str, info: dict, *, active: set[str], columns: list[str], fallback_name: str = ""
) -> dict[str, str] | None:
    """A metadata row from companyInfoSummery, or None when neither it nor the price files name the symbol."""
    name = str(info.get("name") or "").strip() or fallback_name
    if not name:
        return None
    quantity = info.get("quantityIssued")
    values = {
        "symbol": symbol,
        "company_name": name,
        # Same spelling as the pandas-written rows from 01_collect_metadata.py.
        "delisted": "False" if symbol in active else "True",
        "isin": str(info.get("isin") or ""),
        # Rights lines answer 0 issued, which means unknown rather than none.
        "shares_outstanding": str(quantity) if quantity else "",
        "base_ticker": symbol.split(".")[0],
    }
    return {column: values.get(column, "") for column in columns}


def fill_missing_metadata(
    metadata_path: Path,
    symbols: set[str],
    *,
    fetch_info: Callable[[str], dict],
    active: set[str],
    fallback_names: dict[str, str] | None = None,
) -> tuple[list[str], list[dict[str, str]], list[dict[str, str]], list[str]]:
    """Return (columns, existing rows, added rows, unresolved symbols). Existing rows are never changed."""
    with metadata_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        columns = list(reader.fieldnames or [])
        rows = list(reader)

    known = {row["symbol"] for row in rows}
    added: list[dict[str, str]] = []
    unresolved: list[str] = []
    for symbol in sorted(symbols - known):
        row = metadata_row(
            symbol,
            fetch_info(symbol),
            active=active,
            columns=columns,
            fallback_name=(fallback_names or {}).get(symbol, ""),
        )
        if row is None:
            unresolved.append(symbol)
        else:
            added.append(row)
    return columns, rows, added, unresolved


def main() -> int:
    parser = argparse.ArgumentParser(description="Add metadata rows for traded symbols the active list no longer has")
    parser.add_argument("--metadata-path", type=Path, default=DEFAULT_METADATA)
    parser.add_argument("--accepted-root", type=Path, default=DEFAULT_ACCEPTED_ROOT)
    parser.add_argument(
        "--sources", type=Path, default=DEFAULT_SOURCES, help="Official price files to take fallback names from"
    )
    parser.add_argument("--start-date", type=date.fromisoformat, default=None)
    parser.add_argument("--end-date", type=date.fromisoformat, default=None)
    parser.add_argument("--dry-run", action="store_true", help="Report without writing the metadata file")
    args = parser.parse_args()

    session = requests.Session()
    response = session.get(ACTIVE_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    active = {item["symbol"] for item in response.json()}

    def fetch_info(symbol: str) -> dict:
        time.sleep(REQUEST_SLEEP_SECONDS)
        try:
            reply = session.post(
                INFO_URL, files={"symbol": (None, symbol)}, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS
            )
            reply.raise_for_status()
            return reply.json().get("reqSymbolInfo") or {}
        except (requests.RequestException, ValueError) as exc:
            print(f"  {symbol}: {exc}")
            return {}

    lines = args.sources.read_text(encoding="utf-8").splitlines()
    sources = [ROOT / line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    symbols = accepted_symbols(args.accepted_root, args.start_date, args.end_date)
    columns, rows, added, unresolved = fill_missing_metadata(
        args.metadata_path,
        symbols,
        fetch_info=fetch_info,
        active=active,
        fallback_names=official_short_names(sources),
    )

    delisted = sum(row.get("delisted") == "True" for row in added)
    print(f"traded symbols: {len(symbols)}; already in metadata: {len(symbols) - len(added) - len(unresolved)}")
    print(f"filled: {len(added)} ({delisted} delisted); unresolved: {len(unresolved)}")
    for symbol in unresolved:
        print(f"  unresolved: {symbol}")

    if added and not args.dry_run:
        with args.metadata_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
            writer.writeheader()
            writer.writerows([*rows, *added])
        print(f"wrote {args.metadata_path}")

    return 1 if unresolved else 0


if __name__ == "__main__":
    raise SystemExit(main())
