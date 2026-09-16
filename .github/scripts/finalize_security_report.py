#!/usr/bin/env python3
"""Finalization state machine over the night's report (intent #4290, unit U12).

The morning reads one page. It has to stay honest while the night is still
resolving, and it has to eventually say *this is the final picture* -- but only
when that is true. This module is the state machine that decides which of those
two things the page says, and stamps the second one exactly once.


It is not a renderer, and does not contain one
----------------------------------------------

The report is produced by U2's ``render_security_report.render``, a pure
function of the merged ledger. This module calls it. There is no markup in this
file, no second view model and no second status vocabulary: ``STATUS_FINAL`` and
``STATUS_IN_PROGRESS`` are imported from U2 rather than restated, and
``assert_renderer_agrees`` fails if the status this state machine computed is not
the status that ended up in the document. Two definitions of "final" that can
disagree is how a report comes to claim a complete picture that is not one.

No agent writes the report at any point. The write path is: shards (validated
against U2's schema at write time) -> U2's pure renderer -> the bytes on disk.
An agent contributes ledger *fields*, never document text.


"Final" is a transition, not a flag
-----------------------------------

``is_final`` in U2 is a predicate: it answers "would this ledger be final?", and
answers it as many times as it is asked. That is the right shape for a renderer
and the wrong shape for a stamp. Two runs of a finalizer built on a predicate
produce two equally-authoritative final reports for one night, and nothing
records which is the record.

So the stamp here is an **exclusive create** of ``report-final.json``. Not a
check-then-write, which races; not an idempotent overwrite, which is the bug
wearing a hat. The filesystem refuses the second attempt, so double-stamping is
impossible rather than merely tested for. The stamp carries the digest of the
bytes it stamped, so "which report is the record" has an answer that survives a
later re-render attempt.

The claim happens BEFORE the report bytes are written. A failed write after a
successful claim leaves a loud, investigable state (the next attempt refuses,
naming the stamp); the other order silently overwrites a stamped record, which
is the failure this ordering exists to prevent.


Terminal-but-not-reconciled is an error, not "still in progress"
----------------------------------------------------------------

U2's ``is_final`` requires both "every item terminal" and "totals reconcile", and
returns False when either fails. Returning False for the second case is fine for
a renderer -- it means the page keeps reading *delivery in progress* -- but a
finalizer that inherited it would never stamp such a night and never complain.
Every morning would read "delivery in progress" forever, and yesterday's finished
run would be indistinguishable from today's live one.

This module therefore splits the two:

* any item non-terminal          -> ``delivery in progress``, re-render, no stamp
* every item terminal, reconciled -> ``final``, stamped once
* every item terminal, NOT reconciled -> raise

The third case is the one U11 (NFR-3) guarantees cannot linger: every item
reaches merged or stuck, so a run always arrives at case two or case three, and
case three fails loudly. Reconciliation is enforced here, not displayed.


The destination is the ledger's private prefix, and there is no other
--------------------------------------------------------------------

``report_key`` is derived from U2's ``run_prefix``, so the report lands beside
the shards it was rendered from, under the private findings bucket's
``security-agent/`` prefix. There is deliberately no destination argument, no
artifact upload and no ACL parameter anywhere in this file: a full security
picture of the platform in a world-readable location is the worst outcome in this
unit's impact table, and the way to not have that code path is to not have it.
Publication to the bucket is the existing ``publish-findings-s3`` action's job.


Nothing is written until the bytes pass the banned-pattern scan
---------------------------------------------------------------

The rendered report is scanned against the committed list in
``.github/security/triage-banned-patterns.json`` -- U9's list and U9's scanner,
not a second copy -- and a hit is a hard failure before any write. U2's
allow-list and escaping already make a hit very hard to produce; this is the
layer behind it, on a retained artifact, where a mistake cannot be recalled.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from render_security_report import (  # noqa: E402
    STATUS_FINAL,
    STATUS_IN_PROGRESS,
    build_view,
    render,
)
from security_agent_ledger import (  # noqa: E402
    LedgerError,
    load_and_merge,
    load_schema,
    reconcile,
    run_prefix,
    status_counts,
)
from triage_group_findings import (  # noqa: E402
    banned_pattern_hits,
    load_banned_patterns,
)

# Written into the run directory, beside the shards the report was rendered
# from. Constants rather than arguments -- see the module docstring on why this
# file has no destination parameter.
REPORT_NAME = "report.html"
FINAL_STAMP_NAME = "report-final.json"

# The states. Imported from U2 rather than restated, so a rename there cannot
# leave this module stamping a status the document does not carry.
STATUSES = (STATUS_IN_PROGRESS, STATUS_FINAL)

_TERMINAL_STATUSES = ("fixed", "stuck")


class FinalizationError(RuntimeError):
    """The run cannot be finalized, or has already been finalized.

    One type for both, deliberately: neither is a state a nightly run may
    proceed through quietly, and an unattended pipeline that distinguished them
    would be tempted to pass one of them.
    """


# --------------------------------------------------------------------------
# the state machine -- pure
# --------------------------------------------------------------------------


def all_items_terminal(merged: dict) -> bool:
    """True when no item can move again (``fixed`` or ``stuck``).

    A night with zero items is vacuously true: the common night (FR-C2) files
    nothing and is finished the moment the scanners are.
    """
    statuses = merged["fields"].get("story_status", {})
    return all(r["status"] in _TERMINAL_STATUSES for r in statuses.values())


def next_status(previous: str | None, merged: dict) -> str:
    """The status this re-render must carry. Pure.

    ``previous`` is the last stamped status (``None`` before the first render).
    Raises rather than returning when the transition is not one this run may
    make -- see the module docstring for why "already final" and
    "terminal but not reconciled" are both errors and not statuses.
    """
    if previous is not None and previous not in STATUSES:
        raise FinalizationError(
            f"unknown previous status {previous!r}; the statuses are {list(STATUSES)}"
        )
    if previous == STATUS_FINAL:
        raise FinalizationError(
            "this run is already stamped final; re-stamping would produce a "
            "second authoritative report for one night with nothing to say "
            "which is the record"
        )
    if not all_items_terminal(merged):
        return STATUS_IN_PROGRESS

    result = reconcile(merged)
    if not result["ok"]:
        raise FinalizationError(
            "every item is terminal but the totals do not reconcile, so the "
            "run cannot be stamped final and must not be left reading "
            f"'{STATUS_IN_PROGRESS}' forever: {_reconciliation_detail(result)}"
        )
    return STATUS_FINAL


def _reconciliation_detail(result: dict) -> str:
    """Why reconciliation failed, in counts and item numbers only.

    No titles and no finding detail: this string reaches a CI log, which anyone
    who can see the run can read.
    """
    parts = []
    if not result["identity_ok"]:
        parts.append(f"{result['accounted']} item(s) accounted for")
        if result["missing_story_ids"]:
            parts.append(f"no status recorded for {result['missing_story_ids']}")
        if result["unexpected_story_ids"]:
            parts.append(f"status recorded for unfiled {result['unexpected_story_ids']}")
    if not result["coverage_ok"]:
        parts.append(
            f"{result['uncovered_finding_count']} finding(s) covered by no item"
        )
    return "; ".join(parts) or "reconciliation failed"


def assert_renderer_agrees(status: str, merged: dict, schema: dict | None = None) -> None:
    """The document's status must be the status this state machine computed.

    The guard against the two definitions drifting apart. It cannot fail today
    -- both read the same ledger through the same predicate -- which is exactly
    the property worth pinning, because the day it can fail is the day a report
    claims a picture the state machine never agreed to.
    """
    rendered = build_view(merged, schema)["status"]
    if rendered != status:
        raise FinalizationError(
            f"the state machine computed {status!r} but the rendered report "
            f"carries {rendered!r}; the two definitions of 'final' have drifted"
        )


def assert_no_banned_patterns(document: str, patterns: list[dict] | None = None) -> None:
    """Reject a report carrying reproduction detail, before it is written.

    Pattern ids only in the message. The matched text is never surfaced -- the
    whole point is to keep it out of retained artifacts, and an error message is
    one.
    """
    hits = banned_pattern_hits(document, patterns)
    if hits:
        raise FinalizationError(
            f"the rendered report matches banned pattern(s) {hits}; refusing to "
            "write it. A report is a retained artifact, so reproduction detail "
            "that reaches one cannot be recalled"
        )


# --------------------------------------------------------------------------
# placement -- the private location, derived from U2's prefix
# --------------------------------------------------------------------------


def report_key(run_date: str) -> str:
    """The report's key under the private findings bucket (FR-C28).

    Derived from U2's ``run_prefix``, so the report cannot land anywhere the
    shards do not. There is no public-artifact counterpart to this function.
    """
    return f"{run_prefix(run_date)}/{REPORT_NAME}"


def report_path(run_dir: Path | str) -> Path:
    """The report's path inside a run directory."""
    return Path(run_dir) / REPORT_NAME


def stamp_path(run_dir: Path | str) -> Path:
    """The final stamp's path inside a run directory."""
    return Path(run_dir) / FINAL_STAMP_NAME


def read_previous_status(run_dir: Path | str) -> str | None:
    """The last status stamped for this run, or None before the first render.

    ``final`` is read from the stamp rather than from the report's bytes: the
    stamp is the record of the transition, and parsing a status back out of a
    document would make the document its own audit trail.
    """
    stamp = stamp_path(run_dir)
    if stamp.exists():
        try:
            recorded = json.loads(stamp.read_text(encoding="utf-8"))["status"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise FinalizationError(
                f"{stamp} exists but records no readable status; it is the only "
                f"record that this night was finalized, so it is not safe to "
                f"treat as absent: {exc}"
            ) from exc
        if recorded != STATUS_FINAL:
            raise FinalizationError(
                f"{stamp} records {recorded!r}; the stamp is written only for "
                f"{STATUS_FINAL!r}"
            )
        return STATUS_FINAL
    if report_path(run_dir).exists():
        return STATUS_IN_PROGRESS
    return None


def claim_final(run_dir: Path | str, merged: dict, digest: str) -> Path:
    """Stamp the run final, exactly once. Raises if it was already stamped.

    The exclusivity is the filesystem's (``x`` mode), not a check above it: a
    check-then-write races two concurrent finalizers into two stamps, which is
    precisely the outcome being prevented.
    """
    path = stamp_path(run_dir)
    counts = status_counts(merged)
    record = {
        "run_date": merged["run_date"],
        "status": STATUS_FINAL,
        # The ledger's timestamp, carried as data. No clock is read here.
        "generated_at": merged["generated_at"],
        "report_sha256": digest,
        "report_key": report_key(merged["run_date"]),
        "fixed": counts["fixed"],
        "stuck": counts["stuck"],
        "in_progress": counts["in_progress"],
    }
    payload = (json.dumps(record, sort_keys=True, indent=2) + "\n").encode()
    # "x" mode IS the exactly-once guarantee; Path.write_bytes has no exclusive
    # form, which is the only reason this is an `open` call.
    try:
        with open(path, "xb") as handle:
            handle.write(payload)
    except FileExistsError as exc:
        raise FinalizationError(
            f"{path} already exists: this night was already stamped final, and "
            "stamping it twice would leave two final reports for one night"
        ) from exc
    return path


# --------------------------------------------------------------------------
# the one impure step: read shards, write the report
# --------------------------------------------------------------------------


def finalize(
    run_dir: Path | str,
    schema: dict | None = None,
    patterns: list[dict] | None = None,
) -> dict:
    """Re-render the run's report, stamping ``final`` if the night is done.

    Called on every item transition. Returns what it did; raises rather than
    writing anything when the run may not be re-rendered or the bytes do not
    pass the scan.
    """
    schema = schema or load_schema()
    # Loaded up front rather than lazily inside the scan: a missing or
    # unparseable list must fail before anything is rendered, never degrade to
    # "no patterns" on the run that writes the artifact.
    patterns = patterns if patterns is not None else load_banned_patterns()
    directory = Path(run_dir)
    merged = load_and_merge(directory, schema)

    status = next_status(read_previous_status(directory), merged)
    document = render(merged, schema)
    assert_renderer_agrees(status, merged, schema)
    assert_no_banned_patterns(document, patterns)

    payload = document.encode()
    digest = hashlib.sha256(payload).hexdigest()
    # Claimed before the bytes land: a failure between the two is loud on the
    # next attempt, where the other order would silently overwrite the record.
    stamped = status == STATUS_FINAL
    if stamped:
        claim_final(directory, merged, digest)
    report_path(directory).write_bytes(payload)

    return {
        "run_date": merged["run_date"],
        "status": status,
        "stamped": stamped,
        "report": str(report_path(directory)),
        "report_key": report_key(merged["run_date"]),
        "report_sha256": digest,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Re-render the night's security report from its ledger shards and "
            "stamp it final -- once -- when every work item is terminal "
            "(intent #4290, unit U12)."
        )
    )
    parser.add_argument(
        "--ledger-dir",
        required=True,
        help="the run's directory of shard-*.json files; the report is written here",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = finalize(args.ledger_dir)
    except (FinalizationError, LedgerError) as exc:
        print(f"::error title=Security report finalization::{exc}", file=sys.stderr)
        return 1
    # Status and placement only. No counts beyond the stamp, no item titles.
    print(
        f"run_date={result['run_date']} status={result['status']} "
        f"stamped={str(result['stamped']).lower()} key={result['report_key']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
