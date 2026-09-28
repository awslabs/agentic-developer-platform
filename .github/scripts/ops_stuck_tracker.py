#!/usr/bin/env python3
"""Track each work item to a terminal state (intent #4290, unit U11).

An item that cannot go green must still end. Without a mechanical definition of
"stuck" an item stays in progress forever, the night never finalizes, and the
report never reaches a final state -- a morning with no result and no failure is
the one outcome nobody investigates. This module is that definition, applied.


The rule is U2's, not a second copy
-----------------------------------

Two independent paths make an item stuck:

* **three failed delivery runs** on the same item, or
* **twenty-four hours with no state transition**.

Both numbers live in exactly one place -- ``x-stuck-rule`` in
``.github/security/ledger-schema.json`` -- and are applied by U2's
``evaluate_story_status``. This module calls that function; it does not restate
the thresholds. A second copy is a second source of truth, and the copy that
drifts is the one that quietly stops ending runs.

The two paths are genuinely independent, which is why both are exercised: an item
whose runs keep failing hits the first without ever going stale, and an item whose
run never reports back goes stale without a single failure. Either alone would
leave the other class of item hanging forever.


The reason is stamped when the state is set
-------------------------------------------

Not derived later. "Stuck" with no reason tells the morning nothing -- a flaky
check and an impossible ask read identically -- so U2's schema rejects a stuck
record with a null reason, and the transition functions below always set one. The
reason is a closed enum, never free text: it is rendered into the report and into
a comment on a retained issue, and free text is how exploit detail reaches a
document (NT-11).


One shard per item, written not merged
--------------------------------------

Each item writes ``ops.<item>``, its own key, so concurrent delivery runs cannot
lose each other's updates (FR-C29). ``story_status`` merges by ``map-union``, and
because each item owns its own shard the union never has to resolve a conflict.

``now`` is a parameter everywhere below. The only clock read is in the CLI, so
both stuck paths -- including the 24-hour one -- are testable without sleeping.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from security_agent_ledger import (  # noqa: E402
    SHARD_NAME_TEMPLATE,
    LedgerError,
    build_shard,
    evaluate_story_status,
    load_schema,
    serialize_shard,
)

# The stage id one work item's progress is recorded under. Dotted with the item
# number, which is what keeps concurrent delivery runs on distinct keys -- the
# stage id IS the concurrency boundary (U2).
OPS_STAGE_TEMPLATE = "ops.{item}"

STATUS_IN_PROGRESS = "in_progress"
STATUS_FIXED = "fixed"
STATUS_STUCK = "stuck"

_TERMINAL = (STATUS_FIXED, STATUS_STUCK)


class TrackerError(ValueError):
    """A transition would leave an item in a state the ledger cannot express."""


def ops_stage(item: int) -> str:
    """The stage id for one work item."""
    if isinstance(item, bool) or not isinstance(item, int) or item < 1:
        raise TrackerError(f"work item must be a positive issue number, got {item!r}")
    return OPS_STAGE_TEMPLATE.format(item=item)


def initial_record(now: str) -> dict:
    """A freshly-dispatched item: in progress, no failures, transition stamped.

    ``last_transition_at`` is set at dispatch rather than left empty, because it
    is one half of the stuck rule: an item with no stamped transition can never
    age out, so it would hang forever -- the exact failure this unit exists to
    close.
    """
    return {
        "status": STATUS_IN_PROGRESS,
        "failed_runs": 0,
        "last_transition_at": now,
    }


def record_failed_run(record: dict, now: str, schema: dict | None = None) -> dict:
    """One delivery run failed on this item.

    A failed run IS a state transition, so it re-stamps the clock: otherwise the
    24-hour path would fire on an item that is actively being retried and blame
    staleness for what is really a failure loop, recording the wrong reason for
    the morning to act on.
    """
    if record["status"] in _TERMINAL:
        return dict(record)
    updated = dict(record)
    updated["failed_runs"] = record["failed_runs"] + 1
    updated["last_transition_at"] = now
    return evaluate_story_status(updated, now, schema)


def mark_fixed(record: dict, now: str) -> dict:
    """The item's pull request merged. Terminal, and never revisited.

    ``fixed`` carries no reason -- U2's schema rejects a reason on a non-stuck
    record, since a reason on a success is a report cell nobody can interpret.
    """
    if record["status"] == STATUS_STUCK:
        raise TrackerError(
            "refusing to move a stuck item to fixed without a fresh dispatch: the "
            "stuck reason is the record of why it stopped, and overwriting it loses "
            "the only explanation the morning has"
        )
    updated = dict(record)
    updated["status"] = STATUS_FIXED
    updated["reason"] = None
    updated["last_transition_at"] = now
    return {k: v for k, v in updated.items() if not (k == "reason" and v is None)}


def refresh(record: dict, now: str, schema: dict | None = None) -> dict:
    """Re-apply the stuck rule to an item that has not transitioned.

    This is the 24-hour path's trigger: nothing about the item changed, so
    ``last_transition_at`` is untouched and ``evaluate_story_status`` decides
    whether enough time has passed. Called on every sweep, which is why an item
    whose run vanished still reaches a terminal state.
    """
    return evaluate_story_status(record, now, schema)


def is_terminal(record: dict) -> bool:
    """True once the item can no longer move -- what the report reconciles on."""
    return record["status"] in _TERMINAL


def build_ops_shard(
    *,
    item: int,
    record: dict,
    run_date: str,
    generated_at: str,
    schema: dict | None = None,
) -> dict:
    """Build this item's ledger shard, validated against U2's schema.

    Validation is what enforces the "reason is stamped" rule at write time: a
    stuck record with no reason is rejected here, before it can reach the report
    as a blank cell.
    """
    return build_shard(
        run_date,
        ops_stage(item),
        generated_at,
        {"story_status": {str(item): record}},
        schema,
    )


def write_ops_shard(
    out_dir: Path | str,
    *,
    item: int,
    record: dict,
    run_date: str,
    generated_at: str,
    schema: dict | None = None,
) -> Path:
    """Write one item's shard to a run directory, returning its path."""
    shard = build_ops_shard(
        item=item,
        record=record,
        run_date=run_date,
        generated_at=generated_at,
        schema=schema,
    )
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / SHARD_NAME_TEMPLATE.format(stage=shard["stage"])
    path.write_bytes(serialize_shard(shard))
    return path


def read_ops_record(ledger_dir: Path | str, item: int) -> dict | None:
    """This item's current record, or None if it was never dispatched."""
    path = Path(ledger_dir) / SHARD_NAME_TEMPLATE.format(stage=ops_stage(item))
    if not path.exists():
        return None
    try:
        shard = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TrackerError(f"cannot read {path}: {exc}") from exc
    try:
        return shard["fields"]["story_status"][str(item)]
    except (KeyError, TypeError) as exc:
        raise TrackerError(
            f"{path} records no status for work item #{item}"
        ) from exc


def apply_transition(
    record: dict | None,
    *,
    event: str,
    now: str,
    schema: dict | None = None,
) -> dict:
    """Apply one event to one item's record. The single decision point.

    ``dispatched`` is idempotent on an existing record: a retried dispatch must
    not reset ``failed_runs``, or an item could fail three times, be retried, and
    never reach the failure ceiling -- which is the "never ends" bug wearing a
    different hat.
    """
    if event == "dispatched":
        return dict(record) if record is not None else initial_record(now)
    if record is None:
        raise TrackerError(
            f"work item has no record to apply {event!r} to; it must be dispatched "
            "before its progress can be tracked"
        )
    if event == "failed":
        return record_failed_run(record, now, schema)
    if event == "merged":
        return mark_fixed(record, now)
    if event == "sweep":
        return refresh(record, now, schema)
    raise TrackerError(
        f"unknown event {event!r}; the events are dispatched, failed, merged and sweep"
    )


# --------------------------------------------------------------------------
# CLI -- the only clock read
# --------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cmd_transition(args: argparse.Namespace) -> int:
    schema = load_schema()
    now = args.now or _utc_now_iso()
    record = apply_transition(
        read_ops_record(args.ledger_dir, args.item),
        event=args.event,
        now=now,
        schema=schema,
    )
    write_ops_shard(
        args.ledger_dir,
        item=args.item,
        record=record,
        run_date=args.run_date,
        generated_at=now,
        schema=schema,
    )
    # Item number, status and reason only. No titles, no finding detail: a CI log
    # is readable by anyone who can see the run.
    print(
        f"item={args.item} status={record['status']} "
        f"reason={record.get('reason') or 'none'} "
        f"failed_runs={record['failed_runs']} terminal={str(is_terminal(record)).lower()}"
    )
    return 0


def _cmd_sweep(args: argparse.Namespace) -> int:
    """Re-apply the stuck rule to every non-terminal item in the run.

    This is what makes the 24-hour path fire on an item whose delivery run
    vanished: nothing else would ever look at it again.
    """
    schema = load_schema()
    now = args.now or _utc_now_iso()
    directory = Path(args.ledger_dir)
    moved: list[int] = []
    for path in sorted(directory.glob("shard-ops.*.json")):
        suffix = path.name[len("shard-ops.") : -len(".json")]
        if not suffix.isdigit():
            continue
        item = int(suffix)
        record = read_ops_record(directory, item)
        if record is None or is_terminal(record):  # pragma: no cover - glob-matched
            continue
        updated = refresh(record, now, schema)
        if updated != record:
            write_ops_shard(
                directory,
                item=item,
                record=updated,
                run_date=args.run_date,
                generated_at=now,
                schema=schema,
            )
            moved.append(item)
    print(f"swept={len(moved)} newly_stuck={moved}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Track a night's work items to a terminal state, applying the "
        "stuck rule (intent #4290, unit U11)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    transition = sub.add_parser("transition", help="apply one event to one item")
    transition.add_argument("--ledger-dir", required=True)
    transition.add_argument("--run-date", required=True, help="YYYY-MM-DD")
    transition.add_argument("--item", type=int, required=True)
    transition.add_argument(
        "--event", required=True, choices=["dispatched", "failed", "merged", "sweep"]
    )
    transition.add_argument("--now", help="ISO-8601; defaults to the current time")
    transition.set_defaults(func=_cmd_transition)

    sweep = sub.add_parser(
        "sweep", help="re-apply the stuck rule to every non-terminal item"
    )
    sweep.add_argument("--ledger-dir", required=True)
    sweep.add_argument("--run-date", required=True, help="YYYY-MM-DD")
    sweep.add_argument("--now", help="ISO-8601; defaults to the current time")
    sweep.set_defaults(func=_cmd_sweep)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (TrackerError, LedgerError) as exc:
        print(f"::error title=Security ops tracker::{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
