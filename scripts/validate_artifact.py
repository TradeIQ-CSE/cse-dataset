"""Validate a dataset release artifact against the v1 artifact contract.

The contract is ``docs/contracts/dataset-artifact-v1.md``; this is its executable
half. An artifact (the published zip, or the same files in a directory) either
passes every check, or the first failed check is reported as
``FAIL <code>: <reason>`` with exit status 1.

Checks stop at the first failure. Later checks read data an earlier failure has
already discredited: once a checksum disagrees, the row counts and values
describe a file the manifest does not vouch for.
"""

from __future__ import annotations

import argparse
import codecs
import csv
import io
import hashlib
import json
import re
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import jsonschema
from jsonschema.exceptions import best_match

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_SCHEMA_PATH = ROOT / "data/schemas/artifact_manifest.schema.json"
MANIFEST_NAME = "manifest.json"
SUPPORTED_MAJOR = 1

INDEX_CODES = frozenset({"ASPI", "SL20", "SL20TRI", "ASTRI", "MPI", "MTRI"})
OHLCV_STATUSES = frozenset({"accepted", "quarantined"})

#: Placeholders the source pipeline has emitted for "no value", including the
#: ``Unknown`` sector that 01_collect_metadata.py hardcodes. An empty field is
#: the only null the contract allows.
NULL_MARKERS = frozenset({"nan", "none", "null", "n/a", "-", "unknown"})

PATTERNS = {
    "date": re.compile(r"\d{4}-\d{2}-\d{2}"),
    # No sign, exponent or thousands separator, and no more than the 4 dp that
    # numeric(12,4) and numeric(14,4) store, so the checksum covers the stored value.
    "decimal": re.compile(r"\d+(?:\.\d{1,4})?"),
    # Written without a decimal point: "551062649.0" is refused.
    "integer": re.compile(r"\d+"),
    "boolean": re.compile(r"true|false"),
    "symbol": re.compile(r"[A-Z0-9]+\.[A-Z][0-9]{4}"),
    "sha256": re.compile(r"[0-9a-f]{64}"),
}
SEMVER = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class ArtifactError(Exception):
    """A failed contract check. ``code`` is stable; ``reason`` names the offending item."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class FileSpec:
    required: bool
    #: column -> (value type, nullable)
    columns: dict[str, tuple[str, bool]]
    #: Rows are unique on the key and sorted by it, compared column by column.
    key: tuple[str, ...]
    #: Columns kept after parsing, beyond the key, for the cross-file checks.
    retain: tuple[str, ...] = ()


FILE_SPECS: dict[str, FileSpec] = {
    "company_metadata.csv": FileSpec(
        required=True,
        columns={
            "symbol": ("symbol", False),
            "company_name": ("text", False),
            "delisted": ("boolean", False),
            "listing_date": ("date", True),
            "delisting_date": ("date", True),
            "sector_code": ("text", True),
            "isin": ("text", True),
            "shares_outstanding": ("integer", True),
            "board": ("text", True),
        },
        key=("symbol",),
        retain=("sector_code",),
    ),
    "trading_calendar.csv": FileSpec(
        required=True,
        columns={
            "date": ("date", False),
            "ohlcv_status": ("ohlcv_status", False),
            "ohlcv_rows": ("integer", False),
        },
        key=("date",),
        retain=("ohlcv_status", "ohlcv_rows"),
    ),
    "daily_ohlcv.csv": FileSpec(
        required=True,
        columns={
            "date": ("date", False),
            "symbol": ("symbol", False),
            "open": ("decimal", True),
            "high": ("decimal", False),
            "low": ("decimal", False),
            "close": ("decimal", False),
            "volume": ("integer", False),
            "turnover": ("decimal", True),
            "trades": ("integer", True),
            "source": ("text", False),
            "raw_payload_hash": ("sha256", False),
        },
        key=("date", "symbol"),
    ),
    "indices.csv": FileSpec(
        required=True,
        columns={
            "index_code": ("index_code", False),
            "index_name": ("text", False),
            "base_date": ("date", True),
        },
        key=("index_code",),
    ),
    "index_values.csv": FileSpec(
        required=True,
        columns={
            "date": ("date", False),
            "index_code": ("index_code", False),
            "close": ("decimal", False),
            "source": ("text", False),
            "raw_payload_hash": ("sha256", False),
        },
        key=("date", "index_code"),
    ),
    "sectors.csv": FileSpec(
        required=False,
        columns={
            "gics_code": ("text", False),
            "sector_name": ("text", False),
        },
        key=("gics_code",),
    ),
}


@dataclass(frozen=True)
class Table:
    name: str
    rows: int
    columns: dict[str, list[str]]


@dataclass(frozen=True)
class ArtifactSummary:
    dataset_version: str
    kind: str
    coverage: tuple[str, str]
    rows: dict[str, int]


def read_artifact(path: Path) -> dict[str, bytes]:
    """Return every file in the artifact, keyed by name."""
    if path.is_dir():
        files: dict[str, bytes] = {}
        for entry in sorted(path.iterdir()):
            if entry.name.startswith("."):
                continue  # Finder and editor droppings are never part of an artifact
            if not entry.is_file():
                raise ArtifactError("unlisted_file", f"{entry.name}/ is a directory; the artifact layout is flat")
            files[entry.name] = entry.read_bytes()
        return files
    if path.is_file() and zipfile.is_zipfile(path):
        files = {}
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir() or "/" in info.filename:
                    raise ArtifactError(
                        "unlisted_file",
                        f"{info.filename} is not at the archive root; the artifact layout is flat",
                    )
                # zipfile reads the last entry of a repeated name, but a streaming
                # reader gets the first, so only one copy would ever be checked.
                if info.filename in files:
                    raise ArtifactError("unlisted_file", f"{info.filename} appears more than once in the archive")
                files[info.filename] = archive.read(info)
        return files
    raise ArtifactError("artifact_unreadable", f"{path} is not a directory or a zip archive")


def load_manifest(files: dict[str, bytes]) -> dict:
    raw = files.get(MANIFEST_NAME)
    if raw is None:
        raise ArtifactError("manifest_unreadable", f"{MANIFEST_NAME} is missing")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactError("manifest_unreadable", f"{MANIFEST_NAME} is not valid JSON: {exc}") from None
    if not isinstance(manifest, dict):
        raise ArtifactError("manifest_unreadable", f"{MANIFEST_NAME} must hold a JSON object")
    return manifest


def check_contract_version(manifest: dict) -> None:
    # Read before the schema: a 2.x manifest should be reported as unsupported,
    # not as a list of v1 schema violations.
    version = manifest.get("contract_version")
    match = SEMVER.fullmatch(version) if isinstance(version, str) else None
    if match and int(match.group(1)) != SUPPORTED_MAJOR:
        raise ArtifactError(
            "unsupported_contract",
            f"contract_version {version} is not supported; this validator reads {SUPPORTED_MAJOR}.x",
        )


def check_manifest_schema(manifest: dict) -> None:
    schema = json.loads(MANIFEST_SCHEMA_PATH.read_text())
    error = best_match(jsonschema.Draft202012Validator(schema).iter_errors(manifest))
    if error is not None:
        where = "/".join(str(part) for part in error.absolute_path) or "manifest"
        raise ArtifactError("manifest_schema", f"{where}: {error.message}")


def _manifest_date(value: str, where: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ArtifactError("manifest_schema", f"{where}: {value!r} is not a real date") from None


def _dataset_version(value: str, where: str) -> tuple[date, int]:
    day, _, revision = value.partition(".")
    return _manifest_date(day, where), int(revision)


def check_manifest_rules(manifest: dict) -> None:
    """Rules relating one manifest field to another, which JSON Schema cannot express."""

    def fail(reason: str) -> None:
        raise ArtifactError("manifest_schema", reason)

    try:
        datetime.fromisoformat(manifest["created_at"].replace("Z", "+00:00"))
    except ValueError:
        fail(f"created_at: {manifest['created_at']!r} is not a real timestamp")

    # Bounds, index_series spans and corrected_dates are checked against the data
    # in check_coverage; only what the data cannot confirm is checked here.
    start = _manifest_date(manifest["coverage"]["start"], "coverage.start")
    end = _manifest_date(manifest["coverage"]["end"], "coverage.end")

    version = manifest["dataset_version"]
    version_day, revision = _dataset_version(version, "dataset_version")
    if version_day != end:
        fail(f"dataset_version {version} must start with coverage.end {end}")

    kind = manifest["kind"]
    if kind == "incremental":
        base_day, _ = _dataset_version(manifest["base_version"], "base_version")
        if start <= base_day:
            fail(f"incremental coverage.start {start} must be after base_version {manifest['base_version']}")
    elif kind == "correction":
        base_day, base_revision = _dataset_version(manifest["base_version"], "base_version")
        if base_day != version_day or revision <= base_revision:
            fail(f"correction {version} must be a later revision of base_version {manifest['base_version']}")

    paths = [entry["path"] for entry in manifest["files"]]
    repeated = sorted({path for path in paths if paths.count(path) > 1})
    if repeated:
        fail(f"files lists {', '.join(repeated)} more than once")

    codes = [series["index_code"] for series in manifest["index_series"]]
    repeated = sorted({code for code in codes if codes.count(code) > 1})
    if repeated:
        fail(f"index_series lists {', '.join(repeated)} more than once")

    for gap in manifest.get("known_gaps", []):
        if gap["file"] not in paths:
            fail(f"known gap names {gap['file']}, which files does not list")


def check_file_listing(manifest: dict, files: dict[str, bytes]) -> None:
    listed = [entry["path"] for entry in manifest["files"]]
    for name, spec in FILE_SPECS.items():
        if spec.required and name not in listed:
            raise ArtifactError("missing_file", f"{name} is required but the manifest does not list it")
    for name in listed:
        if name not in files:
            raise ArtifactError("missing_file", f"{name} is listed in the manifest but absent from the artifact")
    for name in files:
        if name != MANIFEST_NAME and name not in listed:
            raise ArtifactError("unlisted_file", f"{name} is in the artifact but not listed in the manifest")


def check_checksums(manifest: dict, files: dict[str, bytes]) -> None:
    for entry in manifest["files"]:
        actual = hashlib.sha256(files[entry["path"]]).hexdigest()
        if actual != entry["sha256"]:
            raise ArtifactError(
                "checksum_mismatch",
                f"{entry['path']}: manifest says {entry['sha256']}, file hashes to {actual}",
            )


def _is_real_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def check_value(value: str, kind: str, nullable: bool, where: str) -> None:
    if value == "":
        if nullable:
            return
        raise ArtifactError("bad_value", f"{where}: required value is empty")
    if value != value.strip():
        raise ArtifactError("bad_value", f"{where}: {value!r} has surrounding whitespace")
    if value.casefold() in NULL_MARKERS:
        raise ArtifactError("bad_value", f"{where}: {value!r} is a null placeholder; write an empty field")
    if kind == "text":
        return
    if kind == "index_code":
        valid = value in INDEX_CODES
    elif kind == "ohlcv_status":
        valid = value in OHLCV_STATUSES
    else:
        valid = PATTERNS[kind].fullmatch(value) is not None
        if valid and kind == "date":
            valid = _is_real_date(value)
    if not valid:
        raise ArtifactError("bad_value", f"{where}: {value!r} is not a valid {kind}")


def parse_table(name: str, data: bytes, spec: FileSpec) -> Table:
    """Check one CSV top to bottom: encoding, header, then each row's fields, values and key."""
    if data.startswith(codecs.BOM_UTF8):
        raise ArtifactError("bad_encoding", f"{name} starts with a UTF-8 byte order mark")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactError("bad_encoding", f"{name} is not UTF-8 (invalid byte at offset {exc.start})") from None
    if "\r" in text:
        raise ArtifactError("bad_encoding", f"{name} has \\r line endings; the contract requires \\n")

    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = next(reader, None)
        if not header:
            raise ArtifactError("header_mismatch", f"{name} is empty; a header row is required")
        repeated = sorted({column for column in header if header.count(column) > 1})
        if repeated:
            raise ArtifactError("header_mismatch", f"{name} repeats columns {', '.join(repeated)}")
        missing = [column for column in spec.columns if column not in header]
        if missing:
            raise ArtifactError("header_mismatch", f"{name} is missing columns {', '.join(missing)}")

        # Columns the contract doesn't name are allowed and skipped: a minor
        # version may add nullable columns that a v1.0 importer ignores.
        position = {column: header.index(column) for column in spec.columns}
        kept: dict[str, list[str]] = {column: [] for column in dict.fromkeys((*spec.key, *spec.retain))}
        previous: tuple[str, ...] | None = None
        rows = 0
        for record in reader:
            line = reader.line_num
            if len(record) != len(header):
                raise ArtifactError(
                    "bad_value", f"{name} line {line} has {len(record)} fields; the header has {len(header)}"
                )
            for column, (kind, nullable) in spec.columns.items():
                check_value(record[position[column]], kind, nullable, f"{name} line {line} column {column}")
            key = tuple(record[position[column]] for column in spec.key)
            # Sorted and unique together mean each key is strictly greater than
            # the one before, so a duplicate is always the adjacent row.
            if previous is not None and key == previous:
                raise ArtifactError("duplicate_key", f"{name} line {line} repeats key {', '.join(key)}")
            if previous is not None and key < previous:
                raise ArtifactError(
                    "key_order",
                    f"{name} line {line}: key {', '.join(key)} sorts before the previous row's {', '.join(previous)}",
                )
            previous = key
            for column, values in kept.items():
                values.append(record[position[column]])
            rows += 1
    except csv.Error as exc:
        raise ArtifactError("bad_value", f"{name} line {reader.line_num}: {exc}") from None
    return Table(name=name, rows=rows, columns=kept)


def _require_known(values: Iterable[str], known: set[str], what: str, where: str) -> None:
    for value in values:
        if value not in known:
            raise ArtifactError("orphan_reference", f"{what} {value!r} is not in {where}")


def check_references(tables: dict[str, Table]) -> None:
    metadata = tables["company_metadata.csv"].columns
    ohlcv = tables["daily_ohlcv.csv"].columns
    values = tables["index_values.csv"].columns
    sessions = set(tables["trading_calendar.csv"].columns["date"])

    _require_known(ohlcv["symbol"], set(metadata["symbol"]), "daily_ohlcv.csv symbol", "company_metadata.csv")
    _require_known(ohlcv["date"], sessions, "daily_ohlcv.csv date", "trading_calendar.csv")
    _require_known(
        values["index_code"],
        set(tables["indices.csv"].columns["index_code"]),
        "index_values.csv index_code",
        "indices.csv",
    )
    _require_known(values["date"], sessions, "index_values.csv date", "trading_calendar.csv")

    sectors = tables.get("sectors.csv")
    _require_known(
        (code for code in metadata["sector_code"] if code),
        set(sectors.columns["gics_code"]) if sectors else set(),
        "company_metadata.csv sector_code",
        "sectors.csv" if sectors else "sectors.csv (not shipped)",
    )


def check_calendar(tables: dict[str, Table], manifest: dict) -> None:
    """A quarantined session is still a session, and it carries no prices."""
    per_session = Counter(tables["daily_ohlcv.csv"].columns["date"])
    calendar = tables["trading_calendar.csv"].columns
    quarantined = 0
    for day, status, declared in zip(calendar["date"], calendar["ohlcv_status"], calendar["ohlcv_rows"]):
        actual = per_session.get(day, 0)
        if status == "quarantined":
            quarantined += 1
            if actual:
                raise ArtifactError(
                    "calendar_inconsistent", f"{day} is quarantined but daily_ohlcv.csv has {actual} rows for it"
                )
        elif actual == 0:
            raise ArtifactError("calendar_inconsistent", f"{day} is accepted but daily_ohlcv.csv has no rows for it")
        if int(declared) != actual:
            raise ArtifactError(
                "calendar_inconsistent", f"{day}: ohlcv_rows is {declared}, daily_ohlcv.csv has {actual}"
            )

    expected = manifest["quarantine"]["dates"]
    if quarantined != expected:
        raise ArtifactError(
            "calendar_inconsistent",
            f"manifest quarantine.dates is {expected}; trading_calendar.csv marks {quarantined} sessions quarantined",
        )


def check_coverage(tables: dict[str, Table], manifest: dict) -> None:
    def fail(reason: str) -> None:
        raise ArtifactError("coverage_mismatch", reason)

    sessions = tables["trading_calendar.csv"].columns["date"]
    start, end = manifest["coverage"]["start"], manifest["coverage"]["end"]
    if not sessions:
        fail("trading_calendar.csv lists no sessions")
    # Every price and index date is a session (check_references), so pinning the
    # calendar to the coverage bounds pins everything else inside them too.
    if (sessions[0], sessions[-1]) != (start, end):
        fail(f"trading_calendar.csv runs {sessions[0]}..{sessions[-1]}; coverage says {start}..{end}")

    spans: dict[str, tuple[str, str]] = {}
    values = tables["index_values.csv"].columns
    for day, code in zip(values["date"], values["index_code"]):
        first, last = spans.get(code, (day, day))
        spans[code] = (min(first, day), max(last, day))
    declared = {series["index_code"]: (series["first_date"], series["last_date"]) for series in manifest["index_series"]}
    for code in sorted(spans.keys() | declared.keys()):
        if code not in declared:
            fail(f"index_values.csv has {code} rows but manifest index_series does not list it")
        if code not in spans:
            fail(f"manifest index_series lists {code} but index_values.csv has no {code} rows")
        if spans[code] != declared[code]:
            fail(
                f"{code} runs {spans[code][0]}..{spans[code][1]} in index_values.csv; "
                f"manifest index_series says {declared[code][0]}..{declared[code][1]}"
            )

    session_set = set(sessions)
    for day in manifest.get("corrected_dates", []):
        if day not in session_set:
            fail(f"corrected date {day} is not a session in trading_calendar.csv")
    for gap in manifest.get("known_gaps", []):
        if gap["start"] < start or gap["end"] > end:
            fail(f"known gap {gap['start']}..{gap['end']} in {gap['file']} falls outside coverage")


def validate_artifact(path: Path) -> ArtifactSummary:
    files = read_artifact(path)
    manifest = load_manifest(files)
    check_contract_version(manifest)
    check_manifest_schema(manifest)
    check_manifest_rules(manifest)
    check_file_listing(manifest, files)
    check_checksums(manifest, files)

    declared_rows = {entry["path"]: entry["rows"] for entry in manifest["files"]}
    tables: dict[str, Table] = {}
    for name, spec in FILE_SPECS.items():
        if name not in declared_rows:
            continue
        table = parse_table(name, files[name], spec)
        if table.rows != declared_rows[name]:
            raise ArtifactError(
                "row_count_mismatch", f"{name}: manifest says {declared_rows[name]} rows, file has {table.rows}"
            )
        tables[name] = table

    check_references(tables)
    check_calendar(tables, manifest)
    check_coverage(tables, manifest)
    return ArtifactSummary(
        dataset_version=manifest["dataset_version"],
        kind=manifest["kind"],
        coverage=(manifest["coverage"]["start"], manifest["coverage"]["end"]),
        rows={name: table.rows for name, table in tables.items()},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a dataset release artifact against contract v1")
    parser.add_argument("artifact", type=Path, help="Artifact .zip, or a directory holding the same files")
    args = parser.parse_args(argv)

    try:
        summary = validate_artifact(args.artifact)
    except ArtifactError as exc:
        print(f"FAIL {exc.code}: {exc.reason}")
        return 1

    print(f"OK {summary.dataset_version} ({summary.kind}), {summary.coverage[0]}..{summary.coverage[1]}")
    for name, rows in summary.rows.items():
        print(f"  {name:22} {rows:>9,} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
