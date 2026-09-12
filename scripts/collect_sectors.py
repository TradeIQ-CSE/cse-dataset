"""Fetch each company's GICS industry group from CSE into ``config/``.

``companyProfile`` names a company's group (``reqComSumInfo[0].sector``, e.g.
``Banks``) and ``allSectors`` lists the 20 groups with their GICS codes. Run it
by hand when companies are added: releases read the committed files and never
call CSE for sectors.
"""

from __future__ import annotations

import argparse
import csv
import re
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
API = "https://www.cse.lk/api/"
HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT_SECONDS = 15
SLEEP_SECONDS = 0.2


def group_key(name: str) -> str:
    """Profiles spell a group loosely ("FOOD BEVERAGE & TOBACCO", "Materials (1510)",
    "Diversified Financial"), so groups are compared by their letters alone. Old
    pre-GICS sector names ("Trading") stay unmatched."""
    return re.sub(r"[^a-z]", "", name.lower()).removesuffix("s")


def write(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch company GICS industry groups from CSE")
    parser.add_argument(
        "--symbols-from", type=Path, required=True, help="CSV with a symbol column, e.g. a release's company_metadata.csv"
    )
    parser.add_argument("--out-dir", type=Path, default=ROOT / "config")
    args = parser.parse_args()

    session = requests.Session()
    groups = session.post(API + "allSectors", json={}, headers=HEADERS, timeout=TIMEOUT_SECONDS).json()
    names = {group["indexCode"]: group["name"] for group in groups if group.get("indexCode")}
    codes = {group_key(name): code for code, name in names.items()}

    with args.symbols_from.open(newline="", encoding="utf-8") as handle:
        symbols = sorted({row["symbol"].strip() for row in csv.DictReader(handle)})
    mapped: list[list[str]] = []
    unmapped: dict[str, str] = {}
    for symbol in symbols:
        time.sleep(SLEEP_SECONDS)
        try:
            reply = session.post(
                API + "companyProfile", files={"symbol": (None, symbol)}, headers=HEADERS, timeout=TIMEOUT_SECONDS
            )
            reply.raise_for_status()
            name = ((reply.json().get("reqComSumInfo") or [{}])[0].get("sector") or "").strip()
        except (requests.RequestException, ValueError) as exc:
            name = f"error: {exc}"
        if group_key(name) in codes:
            mapped.append([symbol, codes[group_key(name)]])
        else:
            unmapped[symbol] = name or "no sector"

    write(args.out_dir / "sectors.csv", ["gics_code", "sector_name"], sorted(names.items()))
    write(args.out_dir / "company_sectors.csv", ["symbol", "gics_code"], mapped)
    print(f"{len(mapped)} of {len(symbols)} symbols mapped to {len(codes)} industry groups")
    for symbol, reason in unmapped.items():
        print(f"  unmapped {symbol}: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
