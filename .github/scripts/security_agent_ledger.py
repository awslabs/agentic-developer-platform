#!/usr/bin/env python3
"""Per-run ledger for the nightly security agent (intent #4290, unit U2).

Six stages record what happened during a run. They do NOT share one object:
each writes its own shard and the renderer merges shards at read time
(FR-C29). That is a structural guarantee, not a convention -- S3 has no
partial-object write, and the two architect halves run concurrently by
design, so a shared read-modify-write object would lose updates every night
rather than occasionally.

Layout (FR-C28), private bucket:

    s3://<findings-bucket>/security-agent/runs/<YYYY-MM-DD>/shard-<stage>.json

`stage` is the concurrency boundary. Concurrent writers must use distinct
stage ids (``workflow.code-review`` / ``workflow.pentest``, ``ops.<story>``)
so they land on distinct keys.

What lives here vs. in the renderer: this module writes and merges, and may
read a clock in its CLI. The renderer is a pure function and may not
(NFR-6) -- which is why timestamps travel as the ``generated_at`` ledger
field and the stuck rule is evaluated here, at write time, with ``now``
passed in explicitly.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / "security" / "ledger-schema.json"
)

RUN_PREFIX_TEMPLATE = "security-agent/runs/{run_date}"
SHARD_NAME_TEMPLATE = "shard-{stage}.json"

_TERMINAL_STATUSES = ("fixed", "stuck")


class LedgerError(ValueError):
    """A shard, or a set of shards, violates the ledger schema."""


# --------------------------------------------------------------------------
# schema access
# --------------------------------------------------------------------------


def load_schema(path: Path | str = SCHEMA_PATH) -> dict:
    """Load the ledger schema. It is the source of truth for field
    allow-lists, merge rules, the report allow-list and the stuck rule."""
    return json.loads(Path(path).read_text())


def _field_spec(schema: dict, name: str) -> dict | None:
    return schema["x-fields"].get(name)


# --------------------------------------------------------------------------
# validation
#
# A focused validator rather than `jsonschema`: the CI runner for
# `.github/scripts/tests/` installs pytest and nothing else, and adding a
# dependency to the check six units depend on buys less than it costs. The
# envelope is small and closed, so the rules below are the schema's rules.
# --------------------------------------------------------------------------


def _validate_scalar(name: str, value: object, spec: dict) -> None:
    kind = spec["type"]
    if kind == "integer":
        # bool is an int in Python; a bool here is a caller bug, not a count.
        if not isinstance(value, int) or isinstance(value, bool):
            raise LedgerError(f"field {name!r} must be an integer, got {type(value).__name__}")
        if "minimum" in spec and value < spec["minimum"]:
            raise LedgerError(f"field {name!r} must be >= {spec['minimum']}, got {value}")
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise LedgerError(f"field {name!r} must be a number, got {type(value).__name__}")
        if "minimum" in spec and value < spec["minimum"]:
            raise LedgerError(f"field {name!r} must be >= {spec['minimum']}, got {value}")
    elif kind == "array":
        if not isinstance(value, list):
            raise LedgerError(f"field {name!r} must be an array, got {type(value).__name__}")
        item_type = spec["items"]["type"]
        for item in value:
            if item_type == "string" and not isinstance(item, str):
                raise LedgerError(f"field {name!r} items must be strings")
            if item_type == "integer" and (
                not isinstance(item, int) or isinstance(item, bool)
            ):
                raise LedgerError(f"field {name!r} items must be integers")
    else:  # pragma: no cover - guards against an unhandled schema type
        raise LedgerError(f"field {name!r} has unsupported schema type {kind!r}")


def _validate_story_status_map(name: str, value: object, schema: dict) -> None:
    if not isinstance(value, dict):
        raise LedgerError(f"field {name!r} must be an object keyed by story number")
    spec = schema["x-story-status"]
    allowed = set(spec["properties"])
    for story_key, record in value.items():
        if not (isinstance(story_key, str) and story_key.isdigit()):
            raise LedgerError(
                f"field {name!r} keys must be story numbers as strings, got {story_key!r}"
            )
        if not isinstance(record, dict):
            raise LedgerError(f"story {story_key} status must be an object")
        unknown = set(record) - allowed
        if unknown:
            raise LedgerError(
                f"story {story_key} has fields outside the schema: {sorted(unknown)}"
            )
        missing = set(spec["required"]) - set(record)
        if missing:
            raise LedgerError(f"story {story_key} is missing {sorted(missing)}")
        if record["status"] not in spec["properties"]["status"]["enum"]:
            raise LedgerError(
                f"story {story_key} has invalid status {record['status']!r}"
            )
        reason = record.get("reason")
        if reason not in spec["properties"]["reason"]["enum"]:
            raise LedgerError(f"story {story_key} has invalid reason {reason!r}")
        # FR-C37: a stuck story without a stamped reason makes the report
        # unactionable, so it is a hard failure rather than a blank cell.
        if record["status"] == "stuck" and reason is None:
            raise LedgerError(f"story {story_key} is stuck but records no reason")
        if record["status"] != "stuck" and reason is not None:
            raise LedgerError(
                f"story {story_key} is {record['status']} but stamps reason {reason!r}"
            )
        _validate_scalar(
            f"{name}[{story_key}].failed_runs",
            record["failed_runs"],
            spec["properties"]["failed_runs"],
        )
        _require_pattern(
            f"{name}[{story_key}].last_transition_at",
            record["last_transition_at"],
            spec["properties"]["last_transition_at"]["pattern"],
        )


def _require_pattern(name: str, value: object, pattern: str) -> None:
    import re

    if not isinstance(value, str) or not re.match(pattern, value):
        raise LedgerError(f"{name} must match {pattern}, got {value!r}")


def validate_shard(shard: object, schema: dict | None = None) -> dict:
    """Validate one shard against the schema. Returns it, or raises.

    Rejecting unknown keys here is the write-side half of NT-11: a field the
    schema never declared cannot enter the ledger, so it can never reach the
    rendered report.
    """
    schema = schema or load_schema()
    if not isinstance(shard, dict):
        raise LedgerError(f"shard must be an object, got {type(shard).__name__}")

    unknown = set(shard) - set(schema["properties"])
    if unknown:
        raise LedgerError(f"shard has fields outside the schema: {sorted(unknown)}")
    missing = set(schema["required"]) - set(shard)
    if missing:
        raise LedgerError(f"shard is missing required fields: {sorted(missing)}")

    if shard["schema_version"] != schema["properties"]["schema_version"]["const"]:
        raise LedgerError(
            f"unsupported schema_version {shard['schema_version']!r}"
        )
    _require_pattern("run_date", shard["run_date"], schema["properties"]["run_date"]["pattern"])
    _require_pattern("stage", shard["stage"], schema["properties"]["stage"]["pattern"])
    _require_pattern(
        "generated_at",
        shard["generated_at"],
        schema["properties"]["generated_at"]["pattern"],
    )

    stage_type = shard["stage_type"]
    if stage_type not in schema["properties"]["stage_type"]["enum"]:
        raise LedgerError(f"invalid stage_type {stage_type!r}")
    if shard["stage"].split(".")[0] != stage_type:
        raise LedgerError(
            f"stage {shard['stage']!r} does not belong to stage_type {stage_type!r}"
        )

    fields = shard["fields"]
    if not isinstance(fields, dict):
        raise LedgerError("fields must be an object")
    for name, value in fields.items():
        spec = _field_spec(schema, name)
        if spec is None:
            raise LedgerError(f"field {name!r} is not in the schema")
        if stage_type not in spec["stages"]:
            raise LedgerError(
                f"stage_type {stage_type!r} may not write field {name!r} "
                f"(owners: {spec['stages']})"
            )
        if spec["type"] == "map-of-story-status":
            _validate_story_status_map(name, value, schema)
        else:
            _validate_scalar(name, value, spec)
    return shard


# --------------------------------------------------------------------------
# shard construction + placement
# --------------------------------------------------------------------------


def build_shard(
    run_date: str,
    stage: str,
    generated_at: str,
    fields: dict,
    schema: dict | None = None,
) -> dict:
    """Build a validated shard. Pure: the caller supplies ``generated_at``."""
    schema = schema or load_schema()
    shard = {
        "schema_version": schema["properties"]["schema_version"]["const"],
        "run_date": run_date,
        "stage": stage,
        "stage_type": stage.split(".")[0],
        "generated_at": generated_at,
        "fields": fields,
    }
    return validate_shard(shard, schema)


def serialize_shard(shard: dict) -> bytes:
    """Canonical bytes for a shard.

    Sorted keys and fixed separators are what make FR-C30 hold: re-running a
    stage with the same inputs produces a byte-identical object, so a retry
    is a no-op instead of a spurious diff.
    """
    return (
        json.dumps(shard, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode()


def run_prefix(run_date: str) -> str:
    """S3 prefix for a run (FR-C28)."""
    return RUN_PREFIX_TEMPLATE.format(run_date=run_date)


def shard_key(run_date: str, stage: str) -> str:
    """S3 key for one stage's shard. Distinct stage ids => distinct keys."""
    return f"{run_prefix(run_date)}/{SHARD_NAME_TEMPLATE.format(stage=stage)}"


def put_shard(bucket: str, shard: dict, client=None) -> str:
    """Write one shard object and return its key.

    There is deliberately no get-then-put here. The absence of a read path is
    what makes concurrent writers safe (FR-C29); adding one would reintroduce
    the lost update this design exists to prevent.
    """
    validate_shard(shard)
    if client is None:  # pragma: no cover - exercised in CI, not unit tests
        import boto3

        client = boto3.client("s3")
    key = shard_key(shard["run_date"], shard["stage"])
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=serialize_shard(shard),
        ContentType="application/json",
    )
    return key


def load_shards(ledger_dir: Path | str, schema: dict | None = None) -> list[dict]:
    """Load and validate every shard in a directory, ordered by stage id.

    Sorting by stage id -- not by filesystem order -- is what keeps the
    merge, and therefore the report, independent of directory listing order
    (NFR-6).
    """
    schema = schema or load_schema()
    directory = Path(ledger_dir)
    shards = [
        validate_shard(json.loads(p.read_text()), schema)
        for p in sorted(directory.glob("shard-*.json"))
    ]
    return sorted(shards, key=lambda s: s["stage"])


# --------------------------------------------------------------------------
# merge
# --------------------------------------------------------------------------


def _merge_value(name: str, rule: str, existing: object, incoming: object) -> object:
    if rule == "sum":
        return existing + incoming
    if rule == "max":
        return max(existing, incoming)
    if rule == "unique-list":
        merged = list(existing)
        merged.extend(x for x in incoming if x not in merged)
        return sorted(merged, key=lambda v: (isinstance(v, str), v))
    if rule == "single":
        if existing != incoming:
            raise LedgerError(
                f"field {name!r} is single-valued but two shards disagree: "
                f"{existing!r} vs {incoming!r}"
            )
        return existing
    if rule == "map-union":
        overlap = {
            k for k in incoming if k in existing and existing[k] != incoming[k]
        }
        if overlap:
            raise LedgerError(
                f"field {name!r} has conflicting records for {sorted(overlap)}"
            )
        return {**existing, **incoming}
    raise LedgerError(f"unknown merge rule {rule!r} for field {name!r}")


def merge_shards(shards: list[dict], schema: dict | None = None) -> dict:
    """Merge shards into one ledger, per the schema's merge rules.

    Conflicts are raised, never resolved by "last writer wins" -- a silently
    resolved conflict is how a report ends up authoritative and wrong.
    """
    schema = schema or load_schema()
    if not shards:
        raise LedgerError("cannot merge an empty shard set")

    run_dates = {s["run_date"] for s in shards}
    if len(run_dates) != 1:
        raise LedgerError(f"shards span multiple runs: {sorted(run_dates)}")

    stages = [s["stage"] for s in shards]
    duplicates = {s for s in stages if stages.count(s) > 1}
    if duplicates:
        raise LedgerError(f"duplicate stage shards: {sorted(duplicates)}")

    fields: dict = {}
    for shard in sorted(shards, key=lambda s: s["stage"]):
        for name, value in shard["fields"].items():
            if name not in fields:
                fields[name] = value
                continue
            rule = _field_spec(schema, name)["merge"]
            fields[name] = _merge_value(name, rule, fields[name], value)

    return {
        "schema_version": schema["properties"]["schema_version"]["const"],
        "run_date": run_dates.pop(),
        # The report's timestamp: the newest writer's, carried as data.
        "generated_at": max(s["generated_at"] for s in shards),
        "stages": sorted(stages),
        "fields": fields,
    }


def load_and_merge(ledger_dir: Path | str, schema: dict | None = None) -> dict:
    """Convenience: load a run's shards from disk and merge them."""
    schema = schema or load_schema()
    return merge_shards(load_shards(ledger_dir, schema), schema)


# --------------------------------------------------------------------------
# reconciliation + status
# --------------------------------------------------------------------------


def status_counts(merged: dict) -> dict:
    """Count stories by status from the merged ledger."""
    statuses = merged["fields"].get("story_status", {})
    counts = {"fixed": 0, "stuck": 0, "in_progress": 0}
    for record in statuses.values():
        counts[record["status"]] += 1
    return counts


def reconcile(merged: dict) -> dict:
    """Check the reconciliation identity (FR-C32 / NFR-5).

    Two independent claims:

    1. ``fixed + stuck + in_progress == stories_created`` -- every story
       filed is accounted for by exactly one status.
    2. ``identified_new_after_dedup`` is fully covered by the stories filed.

    Where finding ids are recorded, claim 2 is checked by identity, so the
    result names which finding is uncovered. Where they are not, only the
    weaker count-level statement is checkable and that is what is checked --
    reported as such rather than dressed up as an id-level result.
    """
    fields = merged["fields"]
    counts = status_counts(merged)
    stories_created = fields.get("stories_created", 0)
    accounted = counts["fixed"] + counts["stuck"] + counts["in_progress"]

    story_ids = set(fields.get("story_ids", []))
    tracked_ids = {int(k) for k in fields.get("story_status", {})}
    missing = sorted(story_ids - tracked_ids)
    unexpected = sorted(tracked_ids - story_ids)

    new_ids = set(fields.get("new_finding_ids", []))
    covered_ids = set(fields.get("findings_covered", []))
    if new_ids:
        uncovered = new_ids - covered_ids
        coverage_ok = not uncovered
        uncovered_count = len(uncovered)
    else:
        # No ids to compare. The only honest statement left: findings needing
        # a story got at least one.
        uncovered_count = 0
        coverage_ok = not (
            fields.get("identified_new_after_dedup", 0) > 0 and stories_created == 0
        )

    identity_ok = accounted == stories_created and not missing and not unexpected
    return {
        "ok": identity_ok and coverage_ok,
        "identity_ok": identity_ok,
        "coverage_ok": coverage_ok,
        "accounted": accounted,
        "missing_story_ids": missing,
        "unexpected_story_ids": unexpected,
        "uncovered_finding_count": uncovered_count,
    }


def is_final(merged: dict) -> bool:
    """True when the run may be stamped ``final`` (FR-C36).

    Every story terminal AND reconciliation holding. Both, because a run
    whose totals do not add up is not finished being understood even if no
    story is still moving. A night with zero stories -- the common case per
    FR-C2 -- is vacuously final.
    """
    statuses = merged["fields"].get("story_status", {})
    if any(r["status"] not in _TERMINAL_STATUSES for r in statuses.values()):
        return False
    return reconcile(merged)["ok"]


def evaluate_story_status(
    record: dict, now: str, schema: dict | None = None
) -> dict:
    """Apply the stuck rule (FR-C37 / D-19) to one story's record.

    Two independent paths to stuck: ``failed_runs >= 3``, or 24h with no
    state transition. ``now`` is a parameter, so the rule is testable and no
    clock is read below the CLI. Already-terminal records pass through --
    ``fixed`` is not revisited because time passed.
    """
    schema = schema or load_schema()
    rule = schema["x-stuck-rule"]
    if record["status"] in _TERMINAL_STATUSES:
        return dict(record)

    updated = dict(record)
    if record["failed_runs"] >= rule["failed_runs_threshold"]:
        updated["status"] = "stuck"
        updated["reason"] = "failed_run_limit"
        return updated

    elapsed = _parse_ts(now) - _parse_ts(record["last_transition_at"])
    if elapsed >= timedelta(hours=rule["no_transition_hours"]):
        updated["status"] = "stuck"
        updated["reason"] = "no_transition_timeout"
        return updated
    return updated


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _cmd_write(args: argparse.Namespace) -> int:
    fields = json.loads(Path(args.fields).read_text() if args.fields_file else args.fields)
    generated_at = args.generated_at or _utc_now_iso()
    shard = build_shard(args.run_date, args.stage, generated_at, fields)
    if args.out_dir:
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        path = out / SHARD_NAME_TEMPLATE.format(stage=args.stage)
        path.write_bytes(serialize_shard(shard))
        print(path)
    else:
        print(put_shard(args.bucket, shard))
    return 0


def _cmd_merge(args: argparse.Namespace) -> int:
    merged = load_and_merge(args.ledger_dir)
    payload = {
        **merged,
        "reconciliation": reconcile(merged),
        "final": is_final(merged),
    }
    text = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(text)
    else:
        print(text, end="")
    return 0 if payload["reconciliation"]["ok"] or not args.strict else 1


def _utc_now_iso() -> str:
    """Clock read. CLI-only, by design -- see the module docstring."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Security agent run ledger")
    sub = parser.add_subparsers(dest="command", required=True)

    write = sub.add_parser("write", help="write one stage's shard")
    write.add_argument("--run-date", required=True)
    write.add_argument("--stage", required=True)
    write.add_argument("--fields", required=True, help="JSON object, or a path with --fields-file")
    write.add_argument("--fields-file", action="store_true")
    write.add_argument("--generated-at", help="ISO-8601; defaults to now")
    write.add_argument("--bucket", help="findings bucket (omit with --out-dir)")
    write.add_argument("--out-dir", help="write locally instead of to S3")
    write.set_defaults(func=_cmd_write)

    merge = sub.add_parser("merge", help="merge a run's shards and reconcile")
    merge.add_argument("--ledger-dir", required=True)
    merge.add_argument("--out")
    merge.add_argument("--strict", action="store_true", help="exit 1 if totals do not reconcile")
    merge.set_defaults(func=_cmd_merge)

    args = parser.parse_args(argv)
    if args.command == "write" and not args.bucket and not args.out_dir:
        parser.error("write requires --bucket or --out-dir")
    try:
        return args.func(args)
    except LedgerError as exc:
        print(f"ledger error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
