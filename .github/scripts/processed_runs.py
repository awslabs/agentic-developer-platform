#!/usr/bin/env python3
"""The log of which scan files this pipeline has already turned into issues (#4792).

The pipeline had no memory of its own work. Each run read a findings file,
grouped it, and filed issues, with nothing recording that this had happened -- so
the same file could be consumed any number of times, each time producing a fresh
set of issues. On 2026-08-30 that produced two complete sets for the same 62
findings: 31 work items from run 34117676808 and 27 more from run 34146780068,
all describing the same defects in different words.

There was a guard, and it could not work here. The filing step matches each work
item on its **exact issue title** before creating it, and the titles are written
by a model: a second run over identical findings legitimately produces a different
grouping with different wording, so nothing matches and everything files again.
The one part of the plan guaranteed to vary was the part being compared.

This module fixes it at the input instead. "Has this scan file already been turned
into issues?" has a definite yes/no answer that needs no matching, no similarity
comparison, and no interpretation of anyone's prose.


Why the version key is the file's CONTENT
-----------------------------------------

Not the date: a re-scan that republishes a *different* findings document for the
same date must still be processed, and keying on the date would skip it and lose
real findings.

Not the S3 ETag: for multipart uploads it depends on how the object was uploaded,
not only on what is in it, so the same bytes can carry different ETags.

A content hash means "the same bytes" and nothing else, which is exactly the
question being asked.


Why only success blocks
-----------------------

A ``failed`` entry is recorded for history and stays out of the way. This is what
keeps the pipeline's fail-closed behaviour usable: a night that fails must be
retryable by re-dispatch, and a log that blocked retries would convert every
transient fault into a permanently unprocessable scan.

The consequence is deliberate and accepted (#4792): a run that dies part-way
leaves no ``success`` entry, so the retry re-files whatever the dead run had
already filed. That needs a crash inside a narrow window and produces a duplicate
issue, which is visible and closable. An in-progress or per-item state would buy
little against that and cost real complexity.


Why the whole object is rewritten every time
--------------------------------------------

The findings bucket has one lifecycle rule with **no prefix filter** and a 365-day
expiration, so an object that is never rewritten eventually ages out. Rewriting
the log on every run resets its age, so the log survives for as long as the
pipeline runs at least annually. One object per run would age out entry by entry
and silently weaken the check -- which is why this stays a single document.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

SCHEMA_VERSION = "1"

# The scanners, mirrored from the triage unit rather than restated as a second
# vocabulary. Two scanners process the same date independently, so a file is only
# "already processed" for the source that processed it.
SOURCES = ("code-review", "pentest")

# The two terminal states. `success` blocks a re-run of the same bytes; `failed`
# is history only. A closed set, so a typo cannot silently become a state that
# neither blocks nor reads as a failure.
STATUSES = ("success", "failed")

# The fields an entry may carry. An allow-list, like every other artifact in this
# pipeline: this document is read to decide whether work happens, and a field
# nobody declared is a field nobody validated.
_ENTRY_FIELDS = (
    "run_id",
    "source",
    "run_date",
    "findings_key",
    "findings_version",
    "started_at",
    "finished_at",
    "status",
    "findings_total",
    "work_items_filed",
    "daily_epic",
)

_REQUIRED_ENTRY_FIELDS = (
    "run_id",
    "source",
    "run_date",
    "findings_version",
    "started_at",
    "finished_at",
    "status",
)


class ProcessedRunsError(ValueError):
    """The run log, or a request against it, is not well-formed."""


# --------------------------------------------------------------------------
# the version key
# --------------------------------------------------------------------------


def findings_version(path: Path | str) -> str:
    """``sha256:<hex>`` over the findings file's bytes.

    Streamed rather than read whole: a whole-repo findings document is hundreds
    of kilobytes today and there is no reason for this to be the thing that
    fails on a larger one.
    """
    file_path = Path(path)
    digest = hashlib.sha256()
    try:
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ProcessedRunsError(
            f"cannot read the findings file {file_path} to version it: {exc}"
        ) from exc
    return f"sha256:{digest.hexdigest()}"


# --------------------------------------------------------------------------
# the log
# --------------------------------------------------------------------------


def empty_log() -> dict:
    """A log with nothing processed yet."""
    return {"schema_version": SCHEMA_VERSION, "runs": []}


def load(path: Path | str) -> dict:
    """Read the log. A MISSING file is an EMPTY log, not an error.

    "Nothing has been processed yet" is the correct reading of an absent log, and
    it is the state of every environment before the first run. Treating it as an
    error would make the first run of a fresh deployment fail for being first.

    A file that exists but cannot be parsed IS an error: that is a corrupt log,
    and proceeding would silently re-process everything it recorded.
    """
    file_path = Path(path)
    if not file_path.exists():
        return empty_log()
    try:
        document = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProcessedRunsError(
            f"cannot read the run log {file_path}: {exc}. Refusing to treat a "
            "corrupt log as an empty one -- that would re-process every scan it records"
        ) from exc
    return validate(document)


def validate(document: object) -> dict:
    """Assert the log is the declared shape. Raises on anything unexpected."""
    if not isinstance(document, dict):
        raise ProcessedRunsError(
            f"the run log must be an object, got {type(document).__name__}"
        )
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ProcessedRunsError(
            f"unsupported run-log schema_version {document.get('schema_version')!r}"
        )
    runs = document.get("runs")
    if not isinstance(runs, list):
        raise ProcessedRunsError("the run log's `runs` must be an array")
    for index, entry in enumerate(runs):
        _validate_entry(index, entry)
    return document


def _validate_entry(index: int, entry: object) -> None:
    if not isinstance(entry, dict):
        raise ProcessedRunsError(f"run-log entry {index} must be an object")
    unknown = sorted(set(entry) - set(_ENTRY_FIELDS))
    if unknown:
        raise ProcessedRunsError(f"run-log entry {index} has undeclared fields: {unknown}")
    missing = [f for f in _REQUIRED_ENTRY_FIELDS if not entry.get(f)]
    if missing:
        raise ProcessedRunsError(f"run-log entry {index} is missing or empties {missing}")
    if entry["status"] not in STATUSES:
        raise ProcessedRunsError(
            f"run-log entry {index} has status {entry['status']!r}, "
            f"expected one of {list(STATUSES)}"
        )
    if entry["source"] not in SOURCES:
        raise ProcessedRunsError(
            f"run-log entry {index} has source {entry['source']!r}, "
            f"expected one of {list(SOURCES)}"
        )


# --------------------------------------------------------------------------
# the question this module exists to answer
# --------------------------------------------------------------------------


def already_processed(log: dict, *, version: str, source: str) -> dict | None:
    """The entry that already consumed these bytes for this scanner, or None.

    Matched on (``findings_version``, ``source``, ``status == "success"``). The
    source is part of the key because the two scanners process the same date
    independently: a file consumed by one has not been consumed by the other.
    """
    if source not in SOURCES:
        raise ProcessedRunsError(f"source {source!r} is not one of {list(SOURCES)}")
    for entry in log.get("runs", []):
        if (
            entry.get("findings_version") == version
            and entry.get("source") == source
            and entry.get("status") == "success"
        ):
            return entry
    return None


def build_entry(
    *,
    run_id: str,
    source: str,
    run_date: str,
    findings_key: str | None,
    version: str,
    started_at: str,
    finished_at: str,
    status: str,
    findings_total: int | None = None,
    work_items_filed: int | None = None,
    daily_epic: int | None = None,
) -> dict:
    """One entry, validated before it can enter the log."""
    entry = {
        "run_id": run_id,
        "source": source,
        "run_date": run_date,
        "findings_version": version,
        "started_at": started_at,
        "finished_at": finished_at,
        "status": status,
    }
    # Optional counts are OMITTED rather than sent as zero when unknown: a 0 is a
    # value a reader records as true and cannot distinguish from a real count, the
    # same reasoning the ledger applies to its absent daily_epic.
    for name, value in (
        ("findings_key", findings_key),
        ("findings_total", findings_total),
        ("work_items_filed", work_items_filed),
        ("daily_epic", daily_epic),
    ):
        if value is not None:
            entry[name] = value
    _validate_entry(len(entry), entry)
    return entry


def append(log: dict, entry: dict) -> dict:
    """Return a new log with ``entry`` added. The input is not mutated."""
    validate(log)
    _validate_entry(len(log["runs"]), entry)
    return {
        "schema_version": SCHEMA_VERSION,
        "runs": [*log["runs"], entry],
    }


def write(path: Path | str, log: dict) -> Path:
    """Write the log deterministically, so a re-run produces identical bytes.

    The whole object every time -- see the module docstring on the bucket's
    prefix-less 365-day expiration.
    """
    validate(log)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(log, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _cmd_check(args: argparse.Namespace) -> int:
    """Report whether this findings file has already been processed.

    Exit 0 either way: "already processed" is a normal outcome, not a failure.
    The answer is on stdout as `processed=true|false` for the workflow to read,
    because a step that skips the rest of a job needs a value, not an exit code.
    """
    version = findings_version(args.findings)
    entry = already_processed(load(args.log), version=version, source=args.source)
    print(f"findings_version={version}")
    if entry is None:
        print("processed=false")
        return 0
    print("processed=true")
    # Identifiers only on a CI log (NEV-2) -- no titles, no finding detail.
    print(
        f"already processed by run {entry['run_id']} on {entry['finished_at']} "
        f"(source={entry['source']}, run_date={entry['run_date']})"
    )
    return 0


def _cmd_record(args: argparse.Namespace) -> int:
    """Append this run's entry and write the log."""
    log = load(args.log)
    entry = build_entry(
        run_id=args.run_id,
        source=args.source,
        run_date=args.run_date,
        findings_key=args.findings_key,
        version=args.findings_version or findings_version(args.findings),
        started_at=args.started_at,
        finished_at=args.finished_at,
        status=args.status,
        findings_total=args.findings_total,
        work_items_filed=args.work_items_filed,
        daily_epic=args.daily_epic,
    )
    out = write(args.log, append(log, entry))
    print(
        f"recorded run {entry['run_id']} status={entry['status']} "
        f"source={entry['source']} entries={len(load(out)['runs'])}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="The log of scan files this pipeline has already turned into "
        "issues, so the same file is never processed twice (#4792)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--log",
            required=True,
            help="path to the run log; a MISSING file means nothing processed yet",
        )
        sp.add_argument("--source", required=True, choices=SOURCES)

    check = sub.add_parser("check", help="has this findings file been processed?")
    common(check)
    check.add_argument("--findings", required=True, help="the RAW findings document")
    check.set_defaults(func=_cmd_check)

    record = sub.add_parser("record", help="append this run's entry to the log")
    common(record)
    record.add_argument("--findings", help="the RAW findings document, to version it")
    record.add_argument(
        "--findings-version",
        help="pre-computed version, so `record` reuses exactly what `check` compared "
        "instead of re-hashing a file that may have changed underneath the run",
    )
    record.add_argument("--findings-key", help="the object key the findings came from")
    record.add_argument("--run-id", required=True)
    record.add_argument("--run-date", required=True, help="YYYY-MM-DD")
    record.add_argument("--started-at", required=True, help="ISO-8601")
    record.add_argument("--finished-at", required=True, help="ISO-8601")
    record.add_argument("--status", required=True, choices=STATUSES)
    record.add_argument("--findings-total", type=int)
    record.add_argument("--work-items-filed", type=int)
    record.add_argument("--daily-epic", type=int)
    record.set_defaults(func=_cmd_record)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "command", None) == "record" and not (
        args.findings or args.findings_version
    ):
        print(
            "::error title=Security run log::record needs --findings or --findings-version",
            file=sys.stderr,
        )
        return 1
    try:
        return args.func(args)
    except ProcessedRunsError as exc:
        print(f"::error title=Security run log::{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
