"""Build a versioned dataset release artifact (contract v1) from accepted outputs.

The trading calendar comes from the candidate rows of the official price files
listed in ``config/release_sources.txt``: every date those files list is a
session. Each session is accepted or quarantined according to its per-date
validation summary, and only accepted OHLCV rows are read. Index values come
from the accepted index archive, and every shipped series must have a value on
every session.

Values are normalised to the contract formats. The staging directory and the
finished zip are both run through ``validate_artifact``, and nothing is written
to the output directory unless both pass.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path

try:
    from .backfill_ohlcv import sha256_file
    from .validate_artifact import MANIFEST_NAME, ArtifactError, validate_artifact
except ImportError:  # pragma: no cover - used when executed directly.
    from backfill_ohlcv import sha256_file
    from validate_artifact import MANIFEST_NAME, ArtifactError, validate_artifact

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_VERSION = "1.0.0"
DEFAULT_START = date(2017, 1, 1)
DEFAULT_END = date(2025, 12, 31)
DEFAULT_OUTPUT = ROOT / "data/published"

#: MPI is not shipped because it has no values after 2012. MTRI is not shipped
#: because it has none from 2013 to 2024, then 56 in early 2025.
INDEX_NAMES = {
    "ASPI": "All Share Price Index",
    "SL20": "S&P Sri Lanka 20",
    "SL20TRI": "S&P Sri Lanka 20 Total Return Index",
    "ASTRI": "All Share Total Return Index",
}

#: Input spellings of "no value". The contract's only null is an empty field.
NULL_INPUTS = {"", "nan", "none", "null", "unknown"}
FOUR_DP = Decimal("0.0001")
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

PRICE_COLUMNS = (
    "date", "symbol", "open", "high", "low", "close", "volume",
    "turnover", "trades", "source", "raw_payload_hash",
)
METADATA_COLUMNS = (
    "symbol", "company_name", "delisted", "listing_date", "delisting_date",
    "sector_code", "isin", "shares_outstanding", "board",
)


class ReleaseError(Exception):
    """The inputs cannot make a valid release; nothing was written."""


@dataclass(frozen=True)
class ReleaseInputs:
    sources_list: Path = ROOT / "config/release_sources.txt"
    source_root: Path = ROOT
    candidate_root: Path = ROOT / "data/processed/ohlcv_backfill/candidates"
    accepted_root: Path = ROOT / "data/raw/ohlcv/accepted"
    validation_root: Path = ROOT / "data/processed/validation/ohlcv"
    metadata_path: Path = ROOT / "data/processed/company_metadata.csv"
    indices_path: Path = ROOT / "data/processed/indices_backfill/accepted/indices_historical.csv"


@dataclass(frozen=True)
class ReleaseSummary:
    dataset_version: str
    archive: Path
    coverage: tuple[str, str]
    rows: dict[str, int]
    sessions: int
    quarantined: list[str]
    quarantined_rows: int


def _is_null(value: object) -> bool:
    return value is None or str(value).strip().casefold() in NULL_INPUTS


def to_decimal(value: object, where: str) -> str:
    """Plain decimal with at most 4 dp, rounded half-even; empty for null."""
    if _is_null(value):
        return ""
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        raise ReleaseError(f"{where}: {value!r} is not a number") from None
    if not number.is_finite() or number < 0:
        raise ReleaseError(f"{where}: {value!r} is not a non-negative number")
    return format((number.quantize(FOUR_DP, rounding=ROUND_HALF_EVEN) + 0).normalize(), "f")


def to_integer(value: object, where: str) -> str:
    """Whole number without a decimal point; ``551062649.0`` becomes ``551062649``."""
    if _is_null(value):
        return ""
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        raise ReleaseError(f"{where}: {value!r} is not a number") from None
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        raise ReleaseError(f"{where}: {value!r} is not a whole number")
    return str(int(number))


def to_iso_date(value: object, where: str) -> str:
    """ISO date from ISO or the CSE API's ``01/JAN/1984``; empty for null."""
    if _is_null(value):
        return ""
    text = str(value).strip()
    for pattern, part in (("%Y-%m-%d", text[:10]), ("%d/%b/%Y", text)):
        try:
            return datetime.strptime(part, pattern).date().isoformat()
        except ValueError:
            continue
    raise ReleaseError(f"{where}: {value!r} is not a recognised date")


def to_boolean(value: object, where: str) -> str:
    text = str(value).strip().casefold()
    if text in {"true", "false"}:
        return text
    raise ReleaseError(f"{where}: {value!r} is not a boolean")


def to_text(value: object) -> str:
    return "" if _is_null(value) else str(value).strip()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, header: tuple[str, ...], rows: list[list[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def release_sources(inputs: ReleaseInputs) -> list[Path]:
    lines = inputs.sources_list.read_text(encoding="utf-8").splitlines()
    sources = [inputs.source_root / line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    for source in sources:
        if not source.exists():
            raise ReleaseError(f"release source {source} does not exist")
    return sources


def session_dates(inputs: ReleaseInputs, start: date, end: date) -> list[str]:
    """Every date the configured official price files list, inside the window."""
    sessions: set[str] = set()
    for source in release_sources(inputs):
        candidates = inputs.candidate_root / sha256_file(source) / "canonical_ohlcv_candidates.csv"
        if not candidates.exists():
            raise ReleaseError(f"no backfill candidates for {source.name}; run backfill_ohlcv.py on it first")
        sessions.update(
            row["date"] for row in read_csv(candidates) if start.isoformat() <= row["date"] <= end.isoformat()
        )
    if not sessions:
        raise ReleaseError(f"the release sources list no sessions between {start} and {end}")
    return sorted(sessions)


def session_outcome(inputs: ReleaseInputs, day: str) -> tuple[list[Path], int]:
    """Accepted OHLCV files for one session, and its rejected row count when none passed.

    Each validated source leaves a summary. A source that passed must have left an
    accepted file and one that failed must not, so an accepted file left over from
    an earlier run of a date that now fails is caught here instead of being shipped.
    """
    summaries = sorted((inputs.validation_root / day).glob("*/quality_summary.json"))
    if not summaries:
        raise ReleaseError(f"{day} was never validated; run backfill_ohlcv.py on its source file")
    accepted: list[Path] = []
    rejected = 0
    for summary_path in summaries:
        source = summary_path.parent.name
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        passed = not summary.get("failures")
        output = inputs.accepted_root / day / source / "canonical_ohlcv.csv"
        if passed and not output.exists():
            raise ReleaseError(f"{day}/{source} passed validation but has no accepted output")
        if not passed and output.exists():
            raise ReleaseError(f"{day}/{source} failed validation but has a stale accepted output; re-run its backfill")
        if passed:
            accepted.append(output)
        else:
            rejected += int(summary.get("rejected_rows", 0))
    return accepted, (0 if accepted else rejected)


def price_row(raw: dict[str, str], where: str) -> list[str]:
    return [
        to_text(raw["date"]),
        to_text(raw["symbol"]),
        to_decimal(raw.get("open"), f"{where} open"),
        to_decimal(raw.get("high"), f"{where} high"),
        to_decimal(raw.get("low"), f"{where} low"),
        to_decimal(raw.get("close"), f"{where} close"),
        to_integer(raw.get("volume"), f"{where} volume"),
        to_decimal(raw.get("turnover"), f"{where} turnover"),
        to_integer(raw.get("trades"), f"{where} trades"),
        to_text(raw.get("source")),
        to_text(raw.get("raw_payload_hash")),
    ]


def metadata_row(raw: dict[str, str]) -> list[str]:
    where = f"company_metadata.csv {raw.get('symbol')}"
    shares = to_integer(raw.get("shares_outstanding"), f"{where} shares_outstanding")
    return [
        to_text(raw["symbol"]),
        to_text(raw.get("company_name")),
        to_boolean(raw.get("delisted"), f"{where} delisted"),
        to_iso_date(raw.get("listing_date"), f"{where} listing_date"),
        to_iso_date(raw.get("delisting_date"), f"{where} delisting_date"),
        "",  # sector_code: no per-company GICS mapping has been sourced
        to_text(raw.get("isin")),
        "" if shares == "0" else shares,
        "",  # board: 01_collect_metadata.py only ever wrote a hardcoded "Main"
    ]


def build_staging(
    staging: Path, inputs: ReleaseInputs, start: date, end: date
) -> tuple[list[str], list[str], int, dict[str, int]]:
    """Write every data file into ``staging``. Returns sessions, quarantined dates, rejected rows, row counts."""
    sessions = session_dates(inputs, start, end)
    calendar: list[list[str]] = []
    quarantined: list[str] = []
    quarantined_rows = 0
    symbols: set[str] = set()
    price_rows = 0

    # Streamed one session at a time: the full window is over half a million rows.
    with (staging / "daily_ohlcv.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(PRICE_COLUMNS)
        for day in sessions:
            accepted, rejected = session_outcome(inputs, day)
            rows = [
                price_row(raw, f"{day} {raw.get('symbol')}")
                for path in accepted
                for raw in read_csv(path)
            ]
            rows.sort(key=lambda row: row[1])
            writer.writerows(rows)
            symbols.update(row[1] for row in rows)
            price_rows += len(rows)
            if accepted:
                calendar.append([day, "accepted", str(len(rows))])
            else:
                calendar.append([day, "quarantined", "0"])
                quarantined.append(day)
                quarantined_rows += rejected
    write_csv(staging / "trading_calendar.csv", ("date", "ohlcv_status", "ohlcv_rows"), calendar)

    metadata = {row["symbol"].strip(): row for row in read_csv(inputs.metadata_path)}
    missing = sorted(symbols - metadata.keys())
    if missing:
        raise ReleaseError(
            f"{len(missing)} traded symbols have no metadata row (first: {', '.join(missing[:5])}); "
            "run fill_missing_metadata.py"
        )
    write_csv(staging / "company_metadata.csv", METADATA_COLUMNS, [metadata_row(metadata[s]) for s in sorted(symbols)])

    session_set = set(sessions)
    values: dict[str, list[list[str]]] = {code: [] for code in INDEX_NAMES}
    for raw in read_csv(inputs.indices_path):
        code, day = raw["index_name"].strip(), raw["date"].strip()
        if code in values and start.isoformat() <= day <= end.isoformat():
            where = f"{day} {code}"
            values[code].append(
                [day, code, to_decimal(raw["close"], f"{where} close"), to_text(raw["source"]), to_text(raw["raw_payload_hash"])]
            )
    for code, rows in values.items():
        dates = {row[0] for row in rows}
        if dates != session_set:
            absent, extra = sorted(session_set - dates), sorted(dates - session_set)
            raise ReleaseError(
                f"{code} does not match the calendar: no value on {len(absent)} sessions {absent[:3]}, "
                f"values on {len(extra)} non-sessions {extra[:3]}"
            )
    index_rows = sorted((row for rows in values.values() for row in rows), key=lambda row: (row[0], row[1]))
    write_csv(staging / "index_values.csv", ("date", "index_code", "close", "source", "raw_payload_hash"), index_rows)
    write_csv(
        staging / "indices.csv",
        ("index_code", "index_name", "base_date"),
        [[code, INDEX_NAMES[code], ""] for code in sorted(INDEX_NAMES)],
    )

    rows = {
        "company_metadata.csv": len(symbols),
        "daily_ohlcv.csv": price_rows,
        "index_values.csv": len(index_rows),
        "indices.csv": len(INDEX_NAMES),
        "trading_calendar.csv": len(sessions),
    }
    return sessions, quarantined, quarantined_rows, rows


def write_manifest(
    staging: Path,
    *,
    sessions: list[str],
    quarantined: list[str],
    quarantined_rows: int,
    rows: dict[str, int],
    revision: int,
    created_at: str,
    source_commit: str,
) -> dict:
    manifest = {
        "contract_version": CONTRACT_VERSION,
        "dataset_version": f"{sessions[-1]}.{revision}",
        "kind": "full",
        "created_at": created_at,
        "source_commit": source_commit,
        "coverage": {"start": sessions[0], "end": sessions[-1]},
        "files": [
            {"path": name, "sha256": hashlib.sha256((staging / name).read_bytes()).hexdigest(), "rows": rows[name]}
            for name in sorted(rows)
        ],
        "index_series": [
            {"index_code": code, "first_date": sessions[0], "last_date": sessions[-1]} for code in sorted(INDEX_NAMES)
        ],
        "quarantine": {"dates": len(quarantined), "rows": quarantined_rows},
        "known_gaps": [],
    }
    (staging / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def write_zip(staging: Path, archive: Path) -> None:
    """Same inputs, same bytes: fixed entry order, timestamp and permissions."""
    names = [MANIFEST_NAME, *sorted(p.name for p in staging.iterdir() if p.name != MANIFEST_NAME)]
    with zipfile.ZipFile(archive, "w") as bundle:
        for name in names:
            info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o644 << 16
            bundle.writestr(info, (staging / name).read_bytes())


def require_valid(target: Path, label: str) -> None:
    try:
        validate_artifact(target)
    except ArtifactError as exc:
        raise ReleaseError(f"{label} failed the contract: {exc.code}: {exc.reason}") from None


def release_notes(manifest: dict, summary: ReleaseSummary) -> str:
    start, end = summary.coverage
    accepted = summary.sessions - len(summary.quarantined)
    lines = [
        f"# CSE dataset {summary.dataset_version}",
        "",
        f"Full release covering {start} to {end}, built from `{manifest['source_commit'][:7]}` "
        f"against artifact contract {manifest['contract_version']}.",
        "",
        "| File | Rows |",
        "|---|---:|",
        *(f"| `{name}` | {count:,} |" for name, count in sorted(summary.rows.items())),
        "",
        f"- Trading sessions: {summary.sessions:,} ({accepted:,} accepted, {len(summary.quarantined):,} quarantined)",
        f"- Index series: {', '.join(sorted(INDEX_NAMES))}, each with a value on every session",
        f"- Source rows rejected on quarantined sessions: {summary.quarantined_rows:,}",
        "",
        "## Quarantined sessions",
        "",
        "A quarantined session is a real trading day with no prices in this release: at least one row "
        "in the official file failed validation, so the whole date was held back. Index values are still present.",
        "",
    ]
    by_year: dict[str, list[str]] = {}
    for day in summary.quarantined:
        by_year.setdefault(day[:4], []).append(day)
    lines += [f"- **{year}** ({len(days)}): {', '.join(days)}" for year, days in sorted(by_year.items())]
    if not by_year:
        lines.append("None.")
    lines += [
        "",
        "## Check it",
        "",
        "```bash",
        f"uv run python scripts/validate_artifact.py cse-dataset-{summary.dataset_version}.zip",
        "```",
        "",
    ]
    return "\n".join(lines)


def build_release(
    *,
    inputs: ReleaseInputs,
    output_dir: Path,
    start: date,
    end: date,
    revision: int,
    created_at: str,
    source_commit: str,
) -> ReleaseSummary:
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "staging"
        staging.mkdir()
        sessions, quarantined, quarantined_rows, rows = build_staging(staging, inputs, start, end)
        manifest = write_manifest(
            staging,
            sessions=sessions,
            quarantined=quarantined,
            quarantined_rows=quarantined_rows,
            rows=rows,
            revision=revision,
            created_at=created_at,
            source_commit=source_commit,
        )
        version = manifest["dataset_version"]
        archive = Path(tmp) / f"cse-dataset-{version}.zip"
        require_valid(staging, "staging directory")
        write_zip(staging, archive)
        require_valid(archive, "zip")

        summary = ReleaseSummary(
            dataset_version=version,
            archive=output_dir / archive.name,
            coverage=(sessions[0], sessions[-1]),
            rows=rows,
            sessions=len(sessions),
            quarantined=quarantined,
            quarantined_rows=quarantined_rows,
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(archive, summary.archive)
        shutil.copyfile(staging / MANIFEST_NAME, output_dir / MANIFEST_NAME)
        (output_dir / "release_notes.md").write_text(release_notes(manifest, summary), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a versioned dataset release artifact (contract v1)")
    parser.add_argument("--start-date", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--end-date", type=date.fromisoformat, default=DEFAULT_END)
    parser.add_argument("--revision", type=int, default=1, help="Revision for this coverage end date")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--created-at", help="Pin the manifest timestamp (RFC 3339 UTC) for a reproducible build")
    parser.add_argument("--source-commit", help="Commit to record; defaults to the checked-out HEAD")
    args = parser.parse_args()

    if args.revision < 1:
        parser.error("--revision must be 1 or more")
    created_at = args.created_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    source_commit = args.source_commit or subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()

    try:
        summary = build_release(
            inputs=ReleaseInputs(),
            output_dir=args.output_dir,
            start=args.start_date,
            end=args.end_date,
            revision=args.revision,
            created_at=created_at,
            source_commit=source_commit,
        )
    except ReleaseError as exc:
        print(f"refused: {exc}")
        return 1

    print(f"built {summary.archive}")
    print(f"  coverage {summary.coverage[0]}..{summary.coverage[1]}, {summary.sessions:,} sessions, "
          f"{len(summary.quarantined):,} quarantined")
    for name, count in sorted(summary.rows.items()):
        print(f"  {name:22} {count:>9,} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
