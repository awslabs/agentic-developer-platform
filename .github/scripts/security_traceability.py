#!/usr/bin/env python3
"""The nightly run's finding-to-issue traceability ledger (intent #4290).

One file answers, for a night, the question every other artifact only answers in
part: *where did each scanner finding end up, and what has happened to it since?*
The grouping plan knows which findings clustered together; the filing step knows
which GitHub issue each cluster became; the U2 completion shard records counts.
None of them, alone, lets a human take a finding id and read off "it is in issue
#4712, which is HIGH, and a fix PR is open." This module produces the single file
that does, and it is written to progressively as the night advances:

    grouping  -> which findings clustered, and each cluster's severity
    filed     -> which GitHub issue each cluster became
    (later)   -> where each issue's fix stands (PR open, merged, won't-fix)

Two design choices matter and are deliberate:

* **Severity is COMPUTED, never model-authored.** A cluster's severity is the
  worst `risk_level` among the findings it covers -- a deterministic rollup over
  data the scanner already produced. The plan-authoring model is asked for prose
  only; it is measurably unreliable at emitting exact structured fields, and a
  criticality it could get wrong is worse than useless on a security issue.

* **The file is NEV-2-safe.** It carries finding ids, risk *levels* (a label like
  HIGH), issue numbers and slugs -- metadata about where and how severe. It never
  carries the scanner's reproduction detail (`description`/`attackScript`); that
  stays in the private run ledger, exactly as it does for the issue bodies.

The lifecycle field (`fix_status`) is writable by the nightly run only as far as
`FILED`: the night files issues, it does not fix them. `PR_OPEN`/`FIXED` are set
later by a PR-event-driven updater (`update_fix_status`), which is provided here
but not wired to a trigger in this change.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import triage_group_findings as tg  # noqa: E402

TRACEABILITY_SCHEMA_VERSION = "1"

# The stages a night's file passes through, in order. The stage recorded in the
# file is how a reader (or the final invariant check) knows how far the night got
# and therefore which fields it may trust to be populated.
STAGES = ("grouping", "filed")

# The severity vocabulary, least-to-most severe. `risk_level` on a normalized
# finding is one of these; the order IS the rollup rule, so a cluster's severity
# is `max(index)` over its findings and needs no second table to stay consistent.
SEVERITY_ORDER = ("LOW", "MEDIUM", "HIGH", "CRITICAL")

# The fix lifecycle. `PLANNED` is the grouping-stage state (the issue does not
# exist yet); `FILED` is set the moment the issue is created; the rest are set by
# `update_fix_status` as a fix moves. A closed set so a typo'd state is an error
# rather than a value a dashboard silently cannot interpret.
FIX_STATES = ("PLANNED", "FILED", "PR_OPEN", "FIXED", "WONT_FIX")


class TraceabilityError(RuntimeError):
    """The traceability ledger cannot be built, enriched, or verified."""


# --------------------------------------------------------------------------
# severity: computed from the findings, never from the model
# --------------------------------------------------------------------------


def normalize_severity(level: object) -> str:
    """Canonicalize one finding's `risk_level` to the closed vocabulary.

    Case-insensitive, whitespace-trimmed. An unrecognized level is an ERROR, not
    a default: the whole point of computing severity is that it is trustworthy,
    and silently coercing an unknown label to LOW would understate exactly the
    finding a reader most needs to see. Better to fail the build and be told the
    scanner emitted a level this module does not know.
    """
    if not isinstance(level, str) or not level.strip():
        raise TraceabilityError("a finding carries no `risk_level` to derive severity from")
    canonical = level.strip().upper()
    if canonical not in SEVERITY_ORDER:
        raise TraceabilityError(
            f"finding risk_level {level!r} is not one of {list(SEVERITY_ORDER)}; "
            "refusing to guess a severity"
        )
    return canonical


def rollup_severity(levels: list[str]) -> str:
    """A cluster's severity is the WORST of its findings' severities.

    A group of a LOW and a CRITICAL is a CRITICAL piece of work: the fix has to
    clear the most severe thing it touches. Empty input is an error -- a group
    with no findings should never have reached here (the gate rejects it), and a
    severity over nothing is meaningless.
    """
    if not levels:
        raise TraceabilityError("cannot roll up a severity over no findings")
    return max((normalize_severity(x) for x in levels), key=SEVERITY_ORDER.index)


def severity_by_finding(new_findings_path: Path | str, source: str) -> dict[str, str]:
    """Map each of tonight's `source` findings to its canonical severity.

    Reads U8's dedup result -- the same document the triage step reads -- and
    projects out only `finding_id` and `risk_level`. `risk_level` is metadata (a
    label), so this projection is NEV-2-safe: no reproduction detail is read, let
    alone retained. Selection mirrors `load_new_findings`' source filter so the
    two agree on which findings are tonight's without a second selection rule.
    """
    if source not in tg.SOURCES:
        raise TraceabilityError(f"source {source!r} is not one of {list(tg.SOURCES)}")
    path = Path(new_findings_path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TraceabilityError(f"cannot read the dedup result {path}: {exc}") from exc
    findings = document.get("new_findings")
    if not isinstance(findings, list):
        raise TraceabilityError(f"{path} has no `new_findings` array")

    out: dict[str, str] = {}
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("source") != source:
            continue
        finding_id = finding.get("finding_id")
        if not isinstance(finding_id, str) or not tg._FINDING_ID_RE.match(finding_id):
            continue
        out[finding_id] = normalize_severity(finding.get("risk_level"))
    return out


# --------------------------------------------------------------------------
# stage 1: grouping
# --------------------------------------------------------------------------


def build_grouping(
    plan: dict,
    severities: dict[str, str],
    *,
    run_id: str,
) -> dict:
    """Build the grouping-stage ledger from a gated plan and the severity map.

    Called right after the plan passes its gate, so the plan is already known to
    be a disjoint partition of tonight's findings. This records, per cluster: its
    slug, title, computed severity, and covered finding ids -- with the issue
    fields still empty because nothing is filed yet -- plus the reverse
    `findings_index` a human uses to look a single finding up. `fix_status` starts
    at PLANNED: the work is decided, the issue does not exist.
    """
    groups_out = []
    index: dict[str, dict] = {}
    for group in plan.get("groups", []):
        slug = group["slug"]
        finding_ids = sorted(group["finding_ids"])
        missing = [fid for fid in finding_ids if fid not in severities]
        if missing:
            raise TraceabilityError(
                f"group {slug!r} covers finding(s) with no known severity: {missing}; "
                "the severity map and the plan disagree on tonight's findings"
            )
        severity = rollup_severity([severities[fid] for fid in finding_ids])
        groups_out.append(
            {
                "slug": slug,
                "title": group["title"].strip(),
                "severity": severity,
                "finding_ids": finding_ids,
                "issue_number": None,
                "issue_url": None,
                "fix_status": "PLANNED",
                "fix_prs": [],
                "fixed_at": None,
            }
        )
        for fid in finding_ids:
            index[fid] = {
                "group": slug,
                "severity": severities[fid],
                "issue_number": None,
                "fix_status": "PLANNED",
            }
    return {
        "schema_version": TRACEABILITY_SCHEMA_VERSION,
        "stage": "grouping",
        "run_date": plan["run_date"],
        "run_id": run_id,
        "source": plan["source"],
        "findings_total": len(severities),
        "groups_total": len(groups_out),
        "groups": groups_out,
        "findings_index": index,
    }


# --------------------------------------------------------------------------
# stage 2: filed
# --------------------------------------------------------------------------


def enrich_filed(trace: dict, work_items: list[dict], *, repo: str) -> dict:
    """Fold the filing result into the grouping-stage ledger.

    `work_items` is what `run_triage` returns: one `{number, finding_ids}` per
    filed cluster. Each is matched back to its ledger group by its EXACT set of
    finding ids -- the plan is a disjoint partition, so a finding-id set names one
    and only one group, and matching on it is robust to any ordering difference
    between the plan and the filing loop. On a match the group (and every one of
    its findings in the reverse index) gets the issue number, the issue URL and
    `fix_status = FILED`.

    Returns a new dict; the input is not mutated, so a caller can keep the
    grouping-stage file if it wants both.
    """
    if trace.get("stage") != "grouping":
        raise TraceabilityError(
            f"can only file-enrich a grouping-stage ledger, got stage {trace.get('stage')!r}"
        )
    out = json.loads(json.dumps(trace))  # deep copy; the ledger is small
    by_ids = {frozenset(g["finding_ids"]): g for g in out["groups"]}
    for item in work_items:
        key = frozenset(item["finding_ids"])
        group = by_ids.get(key)
        if group is None:
            raise TraceabilityError(
                f"filed issue #{item['number']} covers findings {sorted(key)} that match "
                "no planned group; the plan and the filing result disagree"
            )
        number = item["number"]
        group["issue_number"] = number
        group["issue_url"] = f"https://github.com/{repo}/issues/{number}"
        group["fix_status"] = "FILED"
        for fid in group["finding_ids"]:
            out["findings_index"][fid]["issue_number"] = number
            out["findings_index"][fid]["fix_status"] = "FILED"
    out["stage"] = "filed"
    return out


# --------------------------------------------------------------------------
# lifecycle: advanced later, by a PR-event updater (not wired in this change)
# --------------------------------------------------------------------------


def update_fix_status(
    trace: dict,
    *,
    issue_number: int,
    status: str,
    pr: int | None = None,
    fixed_at: str | None = None,
) -> dict:
    """Advance one issue's fix lifecycle. Returns a new dict.

    The forward-compatible half of `fix_status`: given an issue number and a new
    state, stamp it on the group and on every finding that group covers. A `pr`
    is appended to `fix_prs` (deduplicated); `fixed_at` is recorded when a fix
    lands. Kept here, beside the states it moves between, so the future
    PR-event-driven caller has one place to reach for -- it is intentionally NOT
    triggered by the nightly run, which only ever writes as far as FILED.
    """
    if status not in FIX_STATES:
        raise TraceabilityError(f"fix_status {status!r} is not one of {list(FIX_STATES)}")
    out = json.loads(json.dumps(trace))
    matched = False
    for group in out["groups"]:
        if group.get("issue_number") != issue_number:
            continue
        matched = True
        group["fix_status"] = status
        if pr is not None and pr not in group["fix_prs"]:
            group["fix_prs"].append(pr)
        if fixed_at is not None:
            group["fixed_at"] = fixed_at
        for fid in group["finding_ids"]:
            out["findings_index"][fid]["fix_status"] = status
    if not matched:
        raise TraceabilityError(f"no group in the ledger is issue #{issue_number}")
    return out


# --------------------------------------------------------------------------
# the invariant: nothing fell through the cracks between scan and GitHub
# --------------------------------------------------------------------------


def assert_fully_traced(trace: dict) -> None:
    """Assert a filed-stage ledger accounts for every finding, exactly once.

    This is the machine-checkable proof the whole file exists to give: that the
    trip from "the scanner found N things tonight" to "they are these GitHub
    issues" lost nothing and duplicated nothing. Checks that the ledger is
    filed-stage, that the counts are self-consistent, that the groups partition
    the findings (each finding in exactly one group), and that every finding
    reached a real issue number.
    """
    if trace.get("stage") != "filed":
        raise TraceabilityError(
            f"traceability ledger is stage {trace.get('stage')!r}, not 'filed'; the "
            "run did not reach the filing stage"
        )
    groups = trace["groups"]
    index = trace["findings_index"]
    if len(groups) != trace["groups_total"]:
        raise TraceabilityError(
            f"groups_total is {trace['groups_total']} but the ledger holds {len(groups)} groups"
        )
    if len(index) != trace["findings_total"]:
        raise TraceabilityError(
            f"findings_total is {trace['findings_total']} but the reverse index holds "
            f"{len(index)} findings"
        )
    covered: set[str] = set()
    for group in groups:
        if group.get("issue_number") is None:
            raise TraceabilityError(
                f"group {group['slug']!r} reached the filed stage with no issue number"
            )
        for fid in group["finding_ids"]:
            if fid in covered:
                raise TraceabilityError(
                    f"{fid} appears in more than one group; the ledger is not a partition"
                )
            covered.add(fid)
    orphans = sorted(set(index) - covered)
    if orphans:
        raise TraceabilityError(
            f"{len(orphans)} finding(s) are in the reverse index but no group: {orphans}"
        )
    extra = sorted(covered - set(index))
    if extra:
        raise TraceabilityError(
            f"{len(extra)} finding(s) are grouped but missing from the reverse index: {extra}"
        )
    for fid, row in index.items():
        if row.get("issue_number") is None:
            raise TraceabilityError(f"finding {fid} reached the filed stage with no issue number")


# --------------------------------------------------------------------------
# io helpers
# --------------------------------------------------------------------------


def write_ledger(path: Path | str, trace: dict, *, allow_stage_regression: bool = False) -> Path:
    """Write the ledger deterministically (sorted keys), so a re-run of the same
    night produces a byte-identical file -- the same reproducibility the U2 shard
    writer keeps, and for the same reason (a diff should mean a real change).

    **Refuses to write a grouping-stage ledger over a filed-stage one** (#4792).

    That regression is what lost the 2026-08-30 record: a re-run's authoring step
    wrote its fresh grouping-stage ledger to the same path, erasing the filed
    stage that mapped 62 findings to issues #4701-#4731. The issue numbers were
    only recoverable from GitHub afterwards, and the file that exists to answer
    "where did this finding end up" answered it wrongly.

    Refusing is right rather than merging, because the two documents describe
    different groupings of the same findings -- merging them would produce a
    ledger that is a partition of nothing. The caller that genuinely means to
    replace a filed record passes ``allow_stage_regression`` and says so.
    """
    out = Path(path)
    if not allow_stage_regression and trace.get("stage") == "grouping" and out.exists():
        try:
            existing = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # An unreadable file is not a filed record to protect. Fall through
            # and overwrite it rather than failing the night over a corrupt one.
            existing = {}
        if isinstance(existing, dict) and existing.get("stage") == "filed":
            raise TraceabilityError(
                f"refusing to overwrite the filed-stage traceability ledger at {out} "
                "with a grouping-stage one: it maps this run's findings to real issue "
                "numbers and a fresh grouping would erase them. This night has "
                "already been filed -- see #4792 on why a re-run should not reach here"
            )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(trace, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return out


def read_ledger(path: Path | str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TraceabilityError(f"cannot read the traceability ledger {path}: {exc}") from exc


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _cmd_grouping(args: argparse.Namespace) -> int:
    plan = read_ledger(args.plan)
    severities = severity_by_finding(args.new_findings, args.source)
    trace = build_grouping(plan, severities, run_id=args.run_id)
    out = write_ledger(args.output, trace)
    # Counts only on the CI log (NEV-2): no titles, no paths, no finding detail.
    print(
        f"traceability grouping stage written: {out.name} "
        f"findings_total={trace['findings_total']} groups_total={trace['groups_total']}"
    )
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    trace = read_ledger(args.traceability)
    assert_fully_traced(trace)
    print(
        f"traceability ok: stage={trace['stage']} "
        f"findings_total={trace['findings_total']} groups_total={trace['groups_total']}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build and verify the nightly finding-to-issue traceability ledger "
        "(intent #4290)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    grouping = sub.add_parser(
        "grouping", help="emit the grouping-stage ledger from a gated plan"
    )
    grouping.add_argument("--plan", required=True, help="the gated grouping plan (JSON)")
    grouping.add_argument(
        "--new-findings", required=True, help="dedup_security_findings.py `diff` output"
    )
    grouping.add_argument("--source", required=True, choices=tg.SOURCES)
    grouping.add_argument("--run-id", required=True, help="the nightly run id")
    grouping.add_argument("--output", default="traceability.json")
    grouping.set_defaults(func=_cmd_grouping)

    verify = sub.add_parser(
        "verify", help="assert a filed-stage ledger accounts for every finding exactly once"
    )
    verify.add_argument("--traceability", required=True)
    verify.set_defaults(func=_cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except TraceabilityError as exc:
        print(f"::error title=Security traceability::{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
