#!/usr/bin/env python3
"""Baseline dedup for nightly Security Agent findings (intent #4290, unit U8).

This is the "most nights file nothing" gate (FR-C2). It takes normalized
findings, compares them against a committed baseline, and passes on only what
is genuinely new. Filing nothing is the NORMAL outcome, not an edge case.

Mirrors the model of `diff_security_findings.py` -- fingerprint sets, set
arithmetic into new/resolved/stable, a committed baseline file -- rather than
inventing a second diff shape. It deliberately does NOT mirror that module's
KEY: `extract_sarif_fingerprints` builds `f"{rule_id}:{artifact}:{line}"`,
which is the positional key the issue's impact table names as a failure mode.
That key is correct there (deterministic SAST scanners re-report an identical
rule id at an identical line) and wrong here, because the source is an LLM.


Why the key is a content fingerprint
------------------------------------

The spike (#4439) recorded `finding_id_stable: false` in
`.github/security/security-agent-profile.json` and this module READS that flag
rather than trusting this comment -- if a future service change makes ids
stable and the profile is updated, the assumption is re-checked at runtime
instead of silently rotting. Evidence behind the flag: two reviews over
overlapping source produced 57 and 43 findings with ZERO shared `findingId`.

Excluded from the key, each for a recorded reason:

* `findingId` -- per-job UUIDs. Keying on it means nothing ever matches, so
  every known finding re-files nightly: the "dedup fails open" row of the
  impact table, i.e. the exact flood this unit exists to prevent.
* `lineStart` and the DIRECTORY part of the path -- positional. The issue's
  gate is explicit: a moved file or a shifted line must not resurface a known
  finding. This is the one point where this module knowingly goes further than
  the spike's prose, which suggested `filePath` + `lineStart` was "the most
  stable available signal"; that suggestion is a positional key, and the
  issue's acceptance criterion overrides it. Basenames are kept because they
  survive a move.
* `riskType` -- the same spike found the SAME defect reported as
  INSECURE_DIRECT_OBJECT_REFERENCE in one run and PRIVILEGE_ESCALATION in the
  next, and `risk_type_is_open_set: true`.
* `riskLevel`, `confidence`, `validationStatus` -- re-scored between runs;
  keying on them resurfaces a finding whose severity was merely re-rated.

What remains: the file basenames touched, plus a normalized token signature of
the finding's title.

**Honest limitation.** Titles are LLM-authored prose, so a genuine rewording
of the same defect produces a new fingerprint and re-files once. This fails
in the direction the impact table prefers -- a duplicate is visible and
closable, whereas the "fails closed too aggressively" row (a real finding
silently suppressed and never reported) is invisible. `--report` surfaces the
resurface rate so drift is measurable rather than assumed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from normalize_security_findings import (
    SOURCES,
    NormalizationError,
    normalize_documents,
)
from security_agent_ledger import (
    SHARD_NAME_TEMPLATE,
    LedgerError,
    build_shard,
)

PROFILE_PATH = (
    Path(__file__).resolve().parent.parent / "security" / "security-agent-profile.json"
)
BASELINE_PATH = (
    Path(__file__).resolve().parent.parent / "security" / "security-agent-baseline.json"
)

BASELINE_SCHEMA_VERSION = "1"

# The closed set of fields a baseline entry may carry. The refresh path writes
# ONLY these -- see `build_baseline_entry` for why the bound matters.
_BASELINE_ENTRY_FIELDS = ("fingerprint", "accepted_on", "reason", "key_files")

# Reasons a finding may be in the baseline. A closed enum, not free text: this
# value is committed to a file that gets read in review, and free text is the
# path by which exploit detail reaches a document (the same NT-11 reasoning the
# ledger applies to its `reason` field).
_ACCEPTED_REASONS = ("accepted_risk", "false_positive", "fixed_elsewhere", "pre_existing")

# The stage id this unit records its counts under, one per scanner half. The
# dotted suffix is what the ledger schema's own description calls out: the two
# halves run CONCURRENTLY, so `workflow.code-review` and `workflow.pentest` land
# on distinct keys. A bare `workflow` would have the second half to finish
# overwrite the first's counts, and the schema merges `identified_raw` with `sum`
# specifically because each half reports its own.
WORKFLOW_STAGE_TEMPLATE = "workflow.{source}"


class DedupError(ValueError):
    """A baseline, or a dedup request, is not well-formed."""


# --------------------------------------------------------------------------
# the comparison key
# --------------------------------------------------------------------------


def assert_ids_are_unstable(profile_path: Path | str = PROFILE_PATH) -> None:
    """Re-check the spike's finding at runtime.

    If someone sets `finding_id_stable: true` in the profile, the fingerprint
    below is no longer the right key and this module must be revisited. Failing
    loudly here is cheaper than a silent behaviour change nobody notices.
    """
    try:
        profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DedupError(f"cannot read the security agent profile: {exc}") from exc
    findings = profile.get("findings") or {}
    if findings.get("finding_id_stable") is not False:
        raise DedupError(
            "the profile no longer records finding_id_stable=false; the content "
            "fingerprint in dedup_security_findings.py was chosen because ids are "
            "per-job UUIDs and must be re-derived before this module is trusted"
        )


def fingerprint(normalized: dict) -> str:
    """Content fingerprint for one normalized finding.

    Hashed over canonical JSON of the non-positional content signals, so the
    key is fixed-width and carries no prose -- a fingerprint is safe to write
    to an artifact, whereas the title it derives from is not.
    """
    payload = {
        "files": sorted(normalized.get("key_files") or []),
        "title": normalized.get("title_signature") or "",
    }
    if not payload["files"] and not payload["title"]:
        # Nothing to key on. Fall back to the id so the finding stays visible
        # and re-files, rather than colliding with every other empty finding
        # and being suppressed as already-known.
        return "f-unkeyable-" + hashlib.sha256(
            str(normalized.get("finding_id") or "").encode()
        ).hexdigest()[:16]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


# --------------------------------------------------------------------------
# baseline
# --------------------------------------------------------------------------


def empty_baseline() -> dict:
    """A baseline with nothing accepted yet."""
    return {"schema_version": BASELINE_SCHEMA_VERSION, "entries": []}


def load_baseline(path: Path | str = BASELINE_PATH) -> dict:
    """Load and validate the committed baseline.

    A MISSING file is an empty baseline -- the first run has nothing accepted
    yet, and that is legitimate. A present-but-corrupt file is an ERROR: it
    would otherwise degrade to an empty baseline, and an empty baseline means
    every known finding is new, which is the flood this unit prevents.
    """
    file_path = Path(path)
    if not file_path.exists():
        return empty_baseline()
    try:
        raw = file_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise DedupError(f"cannot read baseline {file_path}: {exc}") from exc
    if not raw:
        return empty_baseline()
    try:
        baseline = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DedupError(
            f"baseline {file_path} is present but unparseable ({exc}); refusing to "
            "treat it as empty, which would re-file every known finding"
        ) from exc
    return validate_baseline(baseline)


def validate_baseline(baseline: object) -> dict:
    """Validate the baseline's shape, rejecting unknown fields.

    Bounding what a baseline entry may contain is what stops the refresh path
    from becoming a way to silence findings without review: an entry must carry
    a fingerprint and a reason from a closed enum, and may carry nothing the
    schema did not declare.
    """
    if not isinstance(baseline, dict):
        raise DedupError(f"baseline must be an object, got {type(baseline).__name__}")
    version = baseline.get("schema_version")
    if version != BASELINE_SCHEMA_VERSION:
        raise DedupError(f"unsupported baseline schema_version {version!r}")
    entries = baseline.get("entries")
    if not isinstance(entries, list):
        raise DedupError("baseline `entries` must be an array")

    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise DedupError(f"baseline entry {index} must be an object")
        unknown = set(entry) - set(_BASELINE_ENTRY_FIELDS)
        if unknown:
            raise DedupError(
                f"baseline entry {index} has fields outside the schema: {sorted(unknown)}"
            )
        fp = entry.get("fingerprint")
        if not isinstance(fp, str) or not fp:
            raise DedupError(f"baseline entry {index} has no fingerprint")
        if fp in seen:
            raise DedupError(f"baseline has a duplicate fingerprint: {fp}")
        seen.add(fp)
        reason = entry.get("reason")
        if reason not in _ACCEPTED_REASONS:
            raise DedupError(
                f"baseline entry {index} has reason {reason!r}, not one of "
                f"{list(_ACCEPTED_REASONS)}"
            )
    return baseline


def baseline_fingerprints(baseline: dict) -> set[str]:
    """The set of accepted fingerprints."""
    return {e["fingerprint"] for e in baseline["entries"]}


def build_baseline_entry(
    normalized: dict, accepted_on: str, reason: str
) -> dict:
    """Build one baseline entry for an accepted finding.

    Carries the fingerprint, the date, a closed-enum reason and the file
    basenames -- enough for a reviewer to see WHAT is being accepted. It
    deliberately carries no title, description, or reproduction detail: this
    file is committed to the repo, and the repo is not the private rendezvous.
    """
    if reason not in _ACCEPTED_REASONS:
        raise DedupError(
            f"reason {reason!r} is not one of {list(_ACCEPTED_REASONS)}"
        )
    return {
        "fingerprint": fingerprint(normalized),
        "accepted_on": accepted_on,
        "reason": reason,
        "key_files": sorted(normalized.get("key_files") or []),
    }


def refresh_baseline(
    baseline: dict, entries: list[dict], accepted_on: str, reason: str
) -> dict:
    """Add accepted findings to the baseline, returning a NEW baseline.

    Idempotent: re-accepting a fingerprint already present is a no-op rather
    than a duplicate row, so re-running the refresh does not grow the file.
    """
    known = baseline_fingerprints(baseline)
    additions = []
    for normalized in entries:
        entry = build_baseline_entry(normalized, accepted_on, reason)
        if entry["fingerprint"] in known:
            continue
        known.add(entry["fingerprint"])
        additions.append(entry)
    refreshed = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "entries": sorted(
            baseline["entries"] + additions, key=lambda e: e["fingerprint"]
        ),
    }
    return validate_baseline(refreshed)


def serialize_baseline(baseline: dict) -> str:
    """Canonical text for the committed baseline.

    Sorted keys and a stable entry order keep an unchanged baseline
    byte-identical across runs, so the file only appears in a diff when its
    CONTENT changed -- the same idempotency reasoning as the ledger's
    `serialize_shard`.
    """
    return json.dumps(baseline, sort_keys=True, indent=2) + "\n"


# --------------------------------------------------------------------------
# the diff
# --------------------------------------------------------------------------


def dedup(normalized_findings: list[dict], baseline: dict) -> dict:
    """Split normalized findings into new vs already-known.

    Two findings from the same night that fingerprint identically are collapsed
    to one: the two halves review overlapping source, so the same defect
    reported by both is one piece of work, not two.
    """
    known = baseline_fingerprints(baseline)
    new_by_fp: dict[str, dict] = {}
    suppressed: list[str] = []

    for finding in normalized_findings:
        fp = fingerprint(finding)
        if fp in known:
            suppressed.append(fp)
            continue
        if fp in new_by_fp:
            continue
        new_by_fp[fp] = {**finding, "fingerprint": fp}

    new_findings = [new_by_fp[fp] for fp in sorted(new_by_fp)]
    return {
        "new": new_findings,
        "new_count": len(new_findings),
        "suppressed_count": len(suppressed),
        "suppressed_fingerprints": sorted(set(suppressed)),
        # Baseline entries that nothing matched tonight. Reported, not acted
        # on: a finding can be absent because it was fixed OR because the
        # scanner reworded it, and this module cannot tell those apart.
        "unmatched_baseline_fingerprints": sorted(known - set(suppressed)),
    }


def nothing_to_file(result: dict) -> bool:
    """True when no new findings survived dedup -- the common night."""
    return result["new_count"] == 0


def build_result(normalized: dict, baseline: dict, run_date: str | None = None) -> dict:
    """The document downstream stages consume.

    `nothing_to_file` is an EXPLICIT boolean, not an empty `new` list. A
    consumer that has to infer "no work" from an absent or empty field cannot
    distinguish it from "this stage did not run" -- and the difference matters,
    because one means file nothing and the other means the night is broken.
    """
    diff = dedup(normalized["findings"], baseline)
    return {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "run_date": run_date or normalized.get("run_date"),
        "nothing_to_file": nothing_to_file(diff),
        "identified_raw": normalized["identified_raw"],
        "identified_new_after_dedup": diff["new_count"],
        "dropped_by_status": normalized["dropped_by_status"],
        "suppressed_by_baseline": diff["suppressed_count"],
        "unmatched_baseline_fingerprints": diff["unmatched_baseline_fingerprints"],
        "new_findings": diff["new"],
    }


def ledger_fields(result: dict) -> dict:
    """The `workflow`-stage ledger fields for this run.

    Only fields this stage_type owns in `x-fields`. `new_finding_ids` is
    constrained to `^f-[0-9a-f]+$` by the ledger schema, so the service's ids
    travel there -- NOT the fingerprints, which are bare sha256 hex and would
    be rejected. Ids are safe in the ledger for the reason the schema gives:
    they are opaque, never titles or paths (C-8 / NEV-2).
    """
    ids = sorted(
        {
            f["finding_id"]
            for f in result["new_findings"]
            if isinstance(f.get("finding_id"), str) and f["finding_id"].startswith("f-")
        }
    )
    return {
        "identified_raw": result["identified_raw"],
        "identified_new_after_dedup": result["identified_new_after_dedup"],
        "new_finding_ids": ids,
    }


def workflow_stage(source: str) -> str:
    """The stage id this half records its counts under."""
    if source not in SOURCES:
        raise DedupError(f"source {source!r} is not one of {list(SOURCES)}")
    return WORKFLOW_STAGE_TEMPLATE.format(source=source)


def write_shard(
    ledger_dir: Path | str, *, run_date: str, source: str, generated_at: str, fields: dict
) -> Path:
    """Write this half's counts as a full U2 ledger shard.

    Same reasoning as the triage marker (`triage_group_findings.write_marker`),
    and the same defect being fixed: bare `ledger_fields()` output is not a
    shard. Every reader of this directory -- the renderer's `load_shards`, the
    report finalizer -- validates through U2's envelope and RAISES on a missing
    field, so an unwrapped file is a night whose counts are unreadable and whose
    reported cause points at the reader rather than at this writer.

    The wrapper and the filename are both derived, never a caller's string, so
    the stage id inside the shard and the key it lands on cannot disagree.
    """
    shard = build_shard(run_date, workflow_stage(source), generated_at, fields)
    out = Path(ledger_dir) / SHARD_NAME_TEMPLATE.format(stage=shard["stage"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(shard, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _cmd_diff(args: argparse.Namespace) -> int:
    if args.ledger_dir and not (args.source and args.generated_at):
        raise DedupError(
            "--ledger-dir needs --source and --generated-at: the shard's stage id "
            "is per-scanner-half (two halves sharing one id overwrite each other) "
            "and its timestamp is the caller's, so a re-run rewrites the same shard"
        )
    assert_ids_are_unstable(args.profile)
    normalized = normalize_documents(args.findings)
    baseline = load_baseline(args.baseline)
    result = build_result(normalized, baseline, run_date=args.run_date)

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    if args.ledger_dir:
        # `build_result` leaves `run_date` None when neither the flag nor the
        # document carried one. Named here rather than left to the schema's
        # pattern check, because "shard-<stage>.json under the wrong night's
        # prefix" is the failure this addresses and `None` is its only warning.
        if not result["run_date"]:
            raise DedupError(
                "cannot write a ledger shard without a run date: pass --run-date, "
                "since the shard's run_date is what places it under tonight's prefix"
            )
        out = write_shard(
            args.ledger_dir,
            run_date=result["run_date"],
            source=args.source,
            generated_at=args.generated_at,
            fields=ledger_fields(result),
        )
        print(f"wrote ledger shard {out.name}")

    # Summary only. No titles, paths or reproduction detail on a CI log, which
    # is a world-readable artifact for anyone who can see the run (NEV-2).
    print(
        f"identified_raw={result['identified_raw']} "
        f"identified_new_after_dedup={result['identified_new_after_dedup']} "
        f"suppressed_by_baseline={result['suppressed_by_baseline']} "
        f"nothing_to_file={str(result['nothing_to_file']).lower()}"
    )
    if result["nothing_to_file"]:
        print("Nothing new tonight -- nothing to file. This is the expected outcome.")
    return 0


def _cmd_refresh_baseline(args: argparse.Namespace) -> int:
    normalized = normalize_documents(args.findings)
    baseline = load_baseline(args.baseline)
    refreshed = refresh_baseline(
        baseline, normalized["findings"], args.accepted_on, args.reason
    )
    added = len(refreshed["entries"]) - len(baseline["entries"])
    Path(args.baseline).write_text(serialize_baseline(refreshed), encoding="utf-8")
    print(
        f"baseline entries: {len(baseline['entries'])} -> "
        f"{len(refreshed['entries'])} (+{added})"
    )
    print(
        "Commit this file in a pull request -- the accepted-finding record is "
        "reviewed, not applied silently."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Normalize and dedup nightly Security Agent findings"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    diff = sub.add_parser("diff", help="new-vs-baseline for one night")
    diff.add_argument(
        "--findings",
        required=True,
        nargs="+",
        help="raw findings document(s): the code-review and/or pentest output",
    )
    diff.add_argument("--baseline", default=str(BASELINE_PATH))
    diff.add_argument("--output", required=True, help="where to write the new-only result")
    # A DIRECTORY, not a file: the shard's name is derived from its stage id
    # (`shard-workflow.<source>.json`), so the id lives in code and the key it
    # lands on cannot disagree with the id inside the file.
    diff.add_argument(
        "--ledger-dir", help="directory to write this half's workflow-stage shard into"
    )
    diff.add_argument(
        "--source",
        choices=SOURCES,
        help="which scanner half is writing; required with --ledger-dir. The two "
        "halves must record under distinct stage ids or one overwrites the other",
    )
    diff.add_argument(
        "--generated-at",
        help="ISO-8601, from the caller; required with --ledger-dir",
    )
    diff.add_argument("--run-date", help="YYYY-MM-DD; defaults to the document's runDate")
    diff.add_argument("--profile", default=str(PROFILE_PATH))
    diff.set_defaults(func=_cmd_diff)

    refresh = sub.add_parser(
        "refresh-baseline",
        help="accept findings into the baseline (commit the result in a PR)",
    )
    refresh.add_argument("--findings", required=True, nargs="+")
    refresh.add_argument("--baseline", default=str(BASELINE_PATH))
    refresh.add_argument("--accepted-on", required=True, help="YYYY-MM-DD")
    refresh.add_argument("--reason", required=True, choices=_ACCEPTED_REASONS)
    refresh.set_defaults(func=_cmd_refresh_baseline)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    # `LedgerError` too: `build_shard` validates before writing, so a shard this
    # unit could not legally emit fails here, named as this stage's error, rather
    # than as a traceback or as a later reader's "invalid shard".
    except (DedupError, NormalizationError, LedgerError) as exc:
        print(f"::error title=Security findings dedup::{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
