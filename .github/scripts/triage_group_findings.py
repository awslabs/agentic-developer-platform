#!/usr/bin/env python3
"""Group a night's new security findings into work items (intent #4290, U9).

A findings list is not work. This module turns the genuinely-new findings that
survived U8's baseline dedup into a small number of well-formed work items,
grouped by what fixing them has in common, filed as native children of that
night's dated EPIC beneath the long-lived runtime umbrella.


Who decides, and who writes
---------------------------

**The architect decides the grouping; this script materializes it.** That split
is forced, not stylistic. Per the #4559 design note (§7.1, §7.2), the nightly is
a machine-rooted run: ``is_human_rooted`` is false, so ``authorized_user_id`` is
`""` and the run holds no tenant credential, and ``target.create_issue`` has no
consumer anywhere on the EventBridge -> worker path. An agent step that tried to
file issues itself would silently no-op. So the architect's deliverable is a
**grouping plan** -- a JSON document saying which finding ids cluster into which
work item, and what that work item says -- and the CI job runs this script with
its GitHub App token to turn the plan into issues.


Why the bodies are rendered here rather than authored
----------------------------------------------------

Same reasoning as ``render_security_report.py`` in this EPIC (FR-C33): the
document is produced by a script so its properties are structural rather than
hoped for. Three of this unit's gates are only mechanically checkable because of
that choice:

* all five mandatory sections, with the plain-terms opening first, exist **by
  construction** -- ``render_body`` emits them in order and cannot omit one;
* the plan is an **allow-list** (``_GROUP_FIELDS``), so a field the architect
  invents cannot reach a body, in the same spirit as
  ``normalize_security_findings._FIELD_MAP``;
* every rendered body is scanned against a committed banned-pattern list before
  anything is created, so reproduction detail cannot reach a permanently
  retained artifact.


Two umbrellas, and conflating them breaks this unit
---------------------------------------------------

The *delivery* EPIC for the build work (#4438) and the *runtime* umbrella this
pipeline files under are both children of #615 and both read as "the umbrella
EPIC" in prose. The dated parent created here is a child of the **runtime**
umbrella, matched by exact title through ``ensure_umbrella_epic`` -- the same
exact-equality discipline, and the same code, that U3 already established.


Idempotency lives here
----------------------

#4559 §7.3: ``dedup_key`` on the dispatch envelope deduplicates nothing, and the
re-trigger guard that would have absorbed a double dispatch is skipped for this
path entirely. So "exactly one dated parent per date, safe on retry" is this
script's job. Both the dated parent and each work item are matched by exact
title before any create is issued, so a retried night adopts what the first
attempt left behind instead of filing a second copy.

Nothing here dispatches anything. There is no ``adp-trigger`` call, no
``@agent-`` mention, and no ``agent-*`` label -- wave sequencing belongs to the
orchestration stage, and a work item that self-dispatches at creation bypasses
it (a duplicate-work incident with this exact cause is on record, #3626).
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ensure_umbrella_epic  # noqa: E402
from ensure_umbrella_epic import (  # noqa: E402
    STORY_LABEL,
    UMBRELLA_TITLE,
    find_issue_by_exact_title,
    link_sub_issue,
)
from security_agent_ledger import (  # noqa: E402
    SHARD_NAME_TEMPLATE,
    LedgerError,
    build_shard,
)

BANNED_PATTERNS_PATH = (
    Path(__file__).resolve().parent.parent / "security" / "triage-banned-patterns.json"
)

PLAN_SCHEMA_VERSION = "1"

# The scanners. `source` on a normalized finding is derived from which job field
# the service populated (see normalize_security_findings.source_of), so these are
# that module's values, not a second vocabulary.
SOURCES = ("code-review", "pentest")

# The stage id this pass signals completion under, one per scanner. Declared
# HERE, in the writer, and imported by the barrier that reads it (U10 matches it
# with `MARKER_STAGE_RE`) -- a second literal in the reader is the drift this
# unit's whole defect class comes from. The dotted suffix is load-bearing: U2's
# stage id IS the concurrency boundary, so two passes sharing a bare `triage`
# would overwrite each other's shard and the barrier could not attribute either.
MARKER_STAGE_TEMPLATE = "triage.{source}"

# The dated parent's title. Distinct from UMBRELLA_TITLE, and matched by exact
# equality like it -- a date suffix is what makes "one parent per night"
# expressible as a title lookup instead of a stored id.
DAILY_EPIC_TITLE_TEMPLATE = "Security scan of the day — {run_date}"
DAILY_EPIC_LABEL = "epic"

# Work-item titles are prefixed with the run date so they are unique per night
# and stable across a retry of the same night. Without the prefix, two nights
# grouping the same recurring surface would collide on an exact-title match and
# the second night would adopt the first night's issue.
STORY_TITLE_TEMPLATE = "[Security {run_date}] {title}"

# --------------------------------------------------------------------------
# The calibrated grouping band.
#
# Bounded to #3984's observed ratio: twelve findings became five work items,
# about two to three findings each. The band is expressed as those two group
# sizes rather than as a hardcoded story count, so it scales with the night and
# so the numbers in it are the numbers the calibration actually recorded.
#
#   k >= ceil(n / MAX)  -- average group size at most MAX: rejects the
#                          degenerate "one work item per finding", which is the
#                          flood U8 exists to prevent arriving one layer later.
#   k <= ceil(n / MIN)  -- average group size at least MIN: rejects lumping a
#                          night into one unreviewable mega-item.
#
# For n=12 this yields 4..6, which is exactly the smoke criterion in #4448's
# Validation, and #3984's real answer (5) sits inside it. There is deliberately
# NO per-group size cap: #3984's real groups are 4/2/1/3/2, so a cap of three
# would reject the very fixture the band is calibrated against.
# --------------------------------------------------------------------------
MIN_FINDINGS_PER_GROUP = 2
MAX_FINDINGS_PER_GROUP = 3

# The complete set of keys a plan group may carry. A closed set, for the reason
# the module docstring gives: an undeclared field cannot reach a rendered body.
_GROUP_FIELDS = frozenset(
    {
        "slug",
        "title",
        "finding_ids",
        "problem",
        "fix_in_one_line",
        "goal",
        "motivation",
        "who_benefits",
        "who_is_impacted",
        "risks",
        "cost_footprint",
        "fix_surface",
        "approach",
        "deployment",
        "validation",
    }
)

# Keys that must be present and non-empty on every group. Everything in
# `_GROUP_FIELDS` is required: each one is a section (or a named bullet inside
# one) of the repo's mandatory issue shape, and an absent one renders an empty
# section, which the enforcement note in CLAUDE.md calls a code smell.
_REQUIRED_GROUP_FIELDS = tuple(sorted(_GROUP_FIELDS))

_RISK_FIELDS = ("bug_class", "blast_radius")

# The five mandatory headers, in the order the repo's convention fixes them.
REQUIRED_SECTIONS = (
    "## The problem in plain terms",
    "## Description",
    "## Impact analysis",
    "## Design",
    "## Deployment",
    "## Validation",
)

# Service finding ids. Same shape the ledger schema constrains
# `new_finding_ids`/`findings_covered` to, so an id that travels into a body
# also travels into the ledger.
_FINDING_ID_RE = re.compile(r"^f-[0-9a-f][0-9a-f-]*$")

# Any agent mention. Not just the personas that exist today: a mention of a
# persona added later would dispatch just as well, so the token is what is
# banned rather than a list of names.
_AGENT_MENTION_RE = re.compile(r"@agent-", re.IGNORECASE)

# Labels that dispatch. `agent-*` is the deprecated label-trigger namespace.
_DISPATCHING_LABEL_RE = re.compile(r"^agent-", re.IGNORECASE)


class TriageError(ValueError):
    """A grouping plan, or a triage request, is not well-formed."""


# --------------------------------------------------------------------------
# the gh seam
# --------------------------------------------------------------------------


def _gh(args: list[str]) -> tuple[int, str, str]:
    """Every GitHub call in this module, and in the U3 helpers it reuses, goes
    through ``ensure_umbrella_epic._gh``. Looked up at call time rather than
    imported by value so a test that patches that one attribute intercepts the
    whole flow -- including asserting that a create was never issued."""
    return ensure_umbrella_epic._gh(args)


# --------------------------------------------------------------------------
# the banned-pattern list
# --------------------------------------------------------------------------


def load_banned_patterns(path: Path | str = BANNED_PATTERNS_PATH) -> list[dict]:
    """Load the committed banned-pattern list.

    A missing or unparseable list is an ERROR, never an empty list. Degrading to
    "no patterns" would turn the strongest control in this unit into a check that
    passes having asserted nothing -- and it would do so silently, on the run
    that files documents that cannot be recalled.
    """
    file_path = Path(path)
    try:
        document = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TriageError(f"cannot read the banned-pattern list {file_path}: {exc}") from exc
    patterns = document.get("patterns")
    if not isinstance(patterns, list) or not patterns:
        raise TriageError(f"{file_path} declares no patterns; refusing to scan nothing")
    compiled = []
    for index, entry in enumerate(patterns):
        if not isinstance(entry, dict) or not entry.get("regex") or not entry.get("id"):
            raise TriageError(f"banned pattern {index} needs an `id` and a `regex`")
        try:
            compiled.append({"id": entry["id"], "regex": re.compile(entry["regex"])})
        except re.error as exc:
            raise TriageError(f"banned pattern {entry['id']!r} is not a valid regex: {exc}") from exc
    return compiled


def banned_pattern_hits(text: str, patterns: list[dict] | None = None) -> list[str]:
    """The ids of every banned pattern the text matches.

    Returns ids only. The matched TEXT is deliberately not returned or logged:
    this function's whole purpose is to keep reproduction detail out of retained
    artifacts, and a CI log is a retained artifact.
    """
    patterns = patterns if patterns is not None else load_banned_patterns()
    return [p["id"] for p in patterns if p["regex"].search(text)]


# --------------------------------------------------------------------------
# the new-findings input (U8's output)
# --------------------------------------------------------------------------


def load_new_findings(path: Path | str, source: str) -> dict:
    """Read U8's dedup result and select this scanner's genuinely-new findings.

    U8 owns dedup; this module does not re-implement it and does not second-guess
    it. `nothing_to_file` is read as the explicit signal U8 built it to be rather
    than inferred from an empty list -- the difference between "no work tonight"
    and "the stage did not run" is exactly what that flag exists to preserve.
    """
    if source not in SOURCES:
        raise TriageError(f"source {source!r} is not one of {list(SOURCES)}")
    file_path = Path(path)
    try:
        document = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TriageError(f"cannot read the dedup result {file_path}: {exc}") from exc
    if not isinstance(document, dict):
        raise TriageError(f"{file_path} must hold an object, got {type(document).__name__}")
    if "nothing_to_file" not in document:
        raise TriageError(
            f"{file_path} carries no `nothing_to_file` flag; it is not a dedup "
            "result from dedup_security_findings.py"
        )
    findings = document.get("new_findings")
    if not isinstance(findings, list):
        raise TriageError(f"{file_path} has no `new_findings` array")

    selected, ids = [], []
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("source") != source:
            continue
        finding_id = finding.get("finding_id")
        if not isinstance(finding_id, str) or not _FINDING_ID_RE.match(finding_id):
            # Recorded, not silently dropped: an id this module cannot reference
            # is a coverage gap, and the count below is what makes it visible.
            continue
        selected.append(finding)
        ids.append(finding_id)
    return {
        "run_date": document.get("run_date"),
        "source": source,
        "nothing_to_file": bool(document["nothing_to_file"]) or not selected,
        "finding_ids": sorted(set(ids)),
        "unreferenceable": len(selected) - len(set(ids)),
    }


# --------------------------------------------------------------------------
# the grouping plan
# --------------------------------------------------------------------------


def group_count_band(finding_count: int) -> tuple[int, int]:
    """The calibrated (min, max) number of work items for `n` findings.

    Derived from the two group sizes above, so the band is the calibration
    rather than a second number that can drift from it.
    """
    if finding_count <= 0:
        return (0, 0)
    return (
        math.ceil(finding_count / MAX_FINDINGS_PER_GROUP),
        math.ceil(finding_count / MIN_FINDINGS_PER_GROUP),
    )


def validate_plan(plan: object, expected_ids: list[str], *, source: str) -> dict:
    """Validate a grouping plan against the night's new findings.

    Four independent claims, each one a gate in #4448's Validation:

    1. The plan is the declared shape and carries no undeclared field.
    2. Every new finding is covered by exactly one group -- no orphan (a finding
       that silently never becomes work) and no duplicate (the same defect fixed
       twice by two agents).
    3. The number of groups falls inside the #3984-calibrated band.
    4. Nothing in the plan dispatches anything.
    """
    if not isinstance(plan, dict):
        raise TriageError(f"plan must be an object, got {type(plan).__name__}")
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise TriageError(f"unsupported plan schema_version {plan.get('schema_version')!r}")
    if plan.get("source") != source:
        raise TriageError(
            f"plan is for source {plan.get('source')!r} but this pass is {source!r}; "
            "the two scanners group separately and must not be crossed"
        )
    groups = plan.get("groups")
    if not isinstance(groups, list):
        raise TriageError("plan `groups` must be an array")

    expected = set(expected_ids)
    if not expected:
        if groups:
            raise TriageError(
                "plan proposes work items but no new findings survived dedup; "
                "a night with nothing new files nothing"
            )
        return {"groups": [], "covered": []}

    seen_slugs: set[str] = set()
    seen_ids: set[str] = set()
    for index, group in enumerate(groups):
        _validate_group(index, group, seen_slugs, seen_ids, expected)

    uncovered = sorted(expected - seen_ids)
    if uncovered:
        raise TriageError(
            f"{len(uncovered)} new finding(s) are in no group and would silently "
            f"never become work: {uncovered}"
        )

    low, high = group_count_band(len(expected))
    if not low <= len(groups) <= high:
        raise TriageError(
            f"{len(expected)} new findings grouped into {len(groups)} work items, "
            f"outside the calibrated band {low}..{high} "
            f"({MIN_FINDINGS_PER_GROUP}-{MAX_FINDINGS_PER_GROUP} findings each, "
            "bounded to #3984's observed ratio)"
        )
    return {"groups": groups, "covered": sorted(seen_ids)}


def _validate_group(
    index: int, group: object, seen_slugs: set[str], seen_ids: set[str], expected: set[str]
) -> None:
    if not isinstance(group, dict):
        raise TriageError(f"group {index} must be an object")
    unknown = set(group) - _GROUP_FIELDS
    if unknown:
        raise TriageError(f"group {index} has fields outside the schema: {sorted(unknown)}")
    missing = [f for f in _REQUIRED_GROUP_FIELDS if not group.get(f)]
    if missing:
        raise TriageError(f"group {index} is missing or empties {missing}")

    slug = group["slug"]
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
        raise TriageError(f"group {index} slug {slug!r} must be lowercase kebab-case")
    if slug in seen_slugs:
        raise TriageError(f"duplicate group slug {slug!r}")
    seen_slugs.add(slug)

    ids = group["finding_ids"]
    if not isinstance(ids, list) or not ids:
        raise TriageError(f"group {slug!r} covers no findings")
    for finding_id in ids:
        if not isinstance(finding_id, str) or not _FINDING_ID_RE.match(finding_id):
            raise TriageError(
                f"group {slug!r} references {finding_id!r}, which is not an `f-<hex>` "
                "service finding id"
            )
        if finding_id not in expected:
            raise TriageError(
                f"group {slug!r} references {finding_id} which is not a new finding "
                "tonight; a group may only cover findings that survived dedup"
            )
        if finding_id in seen_ids:
            raise TriageError(
                f"{finding_id} appears in more than one group; one defect is one "
                "piece of work"
            )
        seen_ids.add(finding_id)

    for surface in group["fix_surface"]:
        if not isinstance(surface, str) or not surface.strip():
            raise TriageError(f"group {slug!r} has an empty fix_surface entry")
    risks = group["risks"]
    if not isinstance(risks, list) or not risks:
        raise TriageError(f"group {slug!r} records no bug-class/blast-radius rows")
    for risk in risks:
        if not isinstance(risk, dict) or any(not risk.get(f) for f in _RISK_FIELDS):
            raise TriageError(f"group {slug!r} has a risk row missing {list(_RISK_FIELDS)}")
        if set(risk) - set(_RISK_FIELDS):
            raise TriageError(f"group {slug!r} has a risk row with undeclared fields")


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- `{item}`" for item in items)


def render_body(group: dict, *, run_date: str, source: str, findings_uri: str, run_id: str) -> str:
    """Render one work item's body.

    The five mandatory sections in their fixed order, plain-terms opening first.
    Findings are referenced **by identifier only** -- the summaries and the
    scanner's own detail stay in the private run ledger, and the pointer below is
    how the downstream ops agent reaches them. Inlining that detail here would
    move it from a private rendezvous into a permanently retained public-ish
    document, which is the NEV-2 boundary this pipeline is built around.
    """
    risk_rows = "\n".join(
        f"| {r['bug_class']} | {r['blast_radius']} |" for r in group["risks"]
    )
    return f"""## The problem in plain terms

{group["problem"].strip()}

**The fix in one line:** {group["fix_in_one_line"].strip()}

## Description

{group["goal"].strip()}

{group["motivation"].strip()}

## Impact analysis

- **Who benefits** — {group["who_benefits"].strip()}
- **Who's impacted** — {group["who_is_impacted"].strip()}
- **What breaks if this ships with a bug**

| Bug class | Blast radius |
|---|---|
{risk_rows}

- **Cost / quota footprint** — {group["cost_footprint"].strip()}

## Design

{group["approach"].strip()}

**Fix surface**

{_bullets(group["fix_surface"])}

**Findings covered** — identifiers only. The scanner's detail is deliberately
not reproduced here; it lives in the private run ledger referenced below.

{_bullets(sorted(group["finding_ids"]))}

**Where the detail lives** — `{findings_uri}` (run `{run_id}`, scanner
`{source}`, run date `{run_date}`). Read it from there rather than asking for it
to be pasted into this issue.

## Deployment

{group["deployment"].strip()}

## Validation

{group["validation"].strip()}

---

Filed by `.github/scripts/triage_group_findings.py` for the {run_date} nightly
security run. This issue is **not** dispatched at creation — wave sequencing is
the orchestration stage's job.
"""


def render_daily_epic_body(run_date: str, umbrella: int) -> str:
    return f"""Dated parent for the {run_date} nightly security run. Every work
item this night's triage filed is linked here as a native sub-issue, so one
night's remediation reads as a single unit.

Created and maintained by `.github/scripts/triage_group_findings.py`, which
matches this issue by exact title. Exactly one exists per date: a retried run
adopts this issue rather than filing a second parent, so the run report's
reconciliation can balance. Do not rename it.

Parent: #{umbrella} (the long-lived runtime umbrella).
"""


def lint_body(body: str, patterns: list[dict] | None = None) -> None:
    """Assert a rendered body is fileable. Raises on the first violation.

    Runs BEFORE anything is created, on the body that will actually be filed --
    not on the plan -- because the body is what becomes permanent.
    """
    position = -1
    for header in REQUIRED_SECTIONS:
        found = body.find(f"\n{header}\n") if position >= 0 else body.find(header)
        if found < 0:
            raise TriageError(f"rendered body is missing the {header!r} section")
        if found < position:
            raise TriageError(f"{header!r} is out of order in the rendered body")
        position = found
    if not body.lstrip().startswith(REQUIRED_SECTIONS[0]):
        raise TriageError("the plain-terms opening must come first in the body")
    if _AGENT_MENTION_RE.search(body):
        raise TriageError(
            "rendered body contains an `@agent-` mention, which would dispatch an "
            "agent at creation and bypass wave sequencing"
        )
    hits = banned_pattern_hits(body, patterns)
    if hits:
        raise TriageError(
            f"rendered body matches banned pattern(s) {hits}: reproduction detail "
            "must stay in the private run ledger, never in a retained document"
        )


def story_labels() -> list[str]:
    """The labels a filed work item carries. A closed list of exactly one.

    Closed, because the failure mode is not "a wrong label" but "a label that
    dispatches": the `agent-*` namespace triggers a run at creation. The
    assertion below makes that structural rather than a convention.
    """
    labels = [STORY_LABEL]
    dispatching = [lab for lab in labels if _DISPATCHING_LABEL_RE.match(lab)]
    if dispatching:  # pragma: no cover - unreachable while the list is closed
        raise TriageError(f"labels {dispatching} would dispatch an agent at creation")
    return labels


# --------------------------------------------------------------------------
# filing
# --------------------------------------------------------------------------


def _create_issue(repo: str, title: str, body: str, labels: list[str]) -> int:
    args = ["issue", "create", "--repo", repo, "--title", title, "--body", body]
    for label in labels:
        args += ["--label", label]
    rc, out, err = _gh(args)
    if rc != 0:
        raise TriageError(f"failed to create issue {title!r}: {err.strip()}")
    match = re.search(r"/issues/(\d+)", out.strip())
    if not match:
        raise TriageError(f"could not parse issue number from: {out.strip()!r}")
    return int(match.group(1))


def ensure_daily_epic(repo: str, run_date: str, umbrella: int) -> dict:
    """Ensure exactly one dated EPIC exists for `run_date`, under `umbrella`.

    Idempotent by exact title. A retry finds the existing parent and issues no
    create -- two partially-populated parents for one night is a state the run
    report's reconciliation can never balance.
    """
    title = DAILY_EPIC_TITLE_TEMPLATE.format(run_date=run_date)
    existing = find_issue_by_exact_title(repo, title)
    if existing is not None:
        print(f"dated EPIC for {run_date} already exists: #{existing}")
        # Repair a half-finished earlier run that created but never linked.
        return {
            "number": existing,
            "created": False,
            "linked": link_sub_issue(repo, umbrella, existing),
        }
    number = _create_issue(
        repo, title, render_daily_epic_body(run_date, umbrella), [DAILY_EPIC_LABEL]
    )
    print(f"created dated EPIC #{number} for {run_date}")
    return {"number": number, "created": True, "linked": link_sub_issue(repo, umbrella, number)}


def file_group(
    repo: str,
    group: dict,
    *,
    run_date: str,
    source: str,
    daily_epic: int,
    findings_uri: str,
    run_id: str,
    patterns: list[dict] | None = None,
) -> dict:
    """File one work item as a native child of the dated EPIC.

    Linted, then matched by exact title, then created. Linting before the title
    lookup is deliberate: a body that must not be filed must not be filed on the
    retry either, and an early return on "already exists" would skip the check.
    """
    title = STORY_TITLE_TEMPLATE.format(run_date=run_date, title=group["title"].strip())
    body = render_body(
        group, run_date=run_date, source=source, findings_uri=findings_uri, run_id=run_id
    )
    lint_body(body, patterns)
    if _AGENT_MENTION_RE.search(title):
        raise TriageError(f"work-item title {title!r} contains an `@agent-` mention")

    existing = find_issue_by_exact_title(repo, title)
    if existing is not None:
        print(f"work item {group['slug']!r} already filed as #{existing} — no create issued")
        return {
            "number": existing,
            "created": False,
            "linked": link_sub_issue(repo, daily_epic, existing),
            "finding_ids": sorted(group["finding_ids"]),
        }
    number = _create_issue(repo, title, body, story_labels())
    print(f"filed work item #{number} ({group['slug']})")
    return {
        "number": number,
        "created": True,
        "linked": link_sub_issue(repo, daily_epic, number),
        "finding_ids": sorted(group["finding_ids"]),
    }


def ledger_fields(daily_epic: int | None, filed: list[dict]) -> dict:
    """The `triage`-stage ledger fields for this pass.

    Only fields this stage_type owns in the U2 schema's `x-fields`. `story_ids`
    and `findings_covered` are recorded as identities, not just counts, so
    reconciliation can name WHICH story or finding is unaccounted for instead of
    reporting a delta nobody can act on.

    ``daily_epic`` is None on a night with nothing to file, and is then OMITTED
    rather than sent as a placeholder: there is no dated EPIC on a quiet night,
    the schema declares the field `minimum: 1`, and a 0 would be a value the
    ledger records as true and no reader can distinguish from a real number.
    """
    covered: set[str] = set()
    for item in filed:
        covered.update(item["finding_ids"])
    fields = {
        "stories_created": len(filed),
        "story_ids": sorted(item["number"] for item in filed),
        "findings_covered": sorted(covered),
    }
    if daily_epic is not None:
        fields["daily_epic"] = daily_epic
    return fields


def marker_stage(source: str) -> str:
    """The stage id this pass signals completion under."""
    if source not in SOURCES:
        raise TriageError(f"source {source!r} is not one of {list(SOURCES)}")
    return MARKER_STAGE_TEMPLATE.format(source=source)


def write_marker(
    ledger_dir: Path | str, *, run_date: str, source: str, generated_at: str, fields: dict
) -> Path:
    """Write this pass's completion marker as a full U2 ledger shard.

    Two things here are deliberately NOT the caller's to choose, and both were
    the defect: the shard WRAPPER and the FILENAME.

    * The wrapper. The barrier validates every marker it reads through U2's
      `validate_shard`, which raises on a missing envelope field. Writing bare
      `ledger_fields()` output produced a file this pass exited 0 on and the
      barrier rejected one job later -- a failure whose reported cause was
      "invalid shard", indistinguishable from a genuinely broken run. So the
      fields go through `build_shard`, which validates before returning: a
      marker the barrier would reject fails HERE, in the stage that wrote it.
    * The filename. Derived from the stage id via U2's `SHARD_NAME_TEMPLATE`,
      so `shard-triage.<source>.json` is a consequence of the stage id rather
      than a string a caller retypes. A hand-typed path is free to disagree
      with the stage inside the file, and to stop matching the delivery job's
      `shard-triage*.json` glob -- at which point the join silently has nothing
      to join.

    `generated_at` is the caller's, never a clock read here: it is what lets a
    re-run of the same night produce a byte-identical shard (FR-C30).
    """
    shard = build_shard(run_date, marker_stage(source), generated_at, fields)
    out = Path(ledger_dir) / SHARD_NAME_TEMPLATE.format(stage=shard["stage"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(shard, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return out


def run_triage(
    repo: str,
    *,
    plan: dict,
    new_findings: dict,
    findings_uri: str,
    run_id: str,
    run_date: str,
    umbrella_title: str = UMBRELLA_TITLE,
    patterns: list[dict] | None = None,
) -> dict:
    """Group one scanner's new findings into filed work items.

    Returns a result document. On a night with nothing new this returns BEFORE
    resolving the umbrella, so no parent and no work item is created and nothing
    at all is filed on GitHub -- NT-5.

    It does still produce ledger fields, and that is load-bearing rather than
    incidental. "This scanner ran and found nothing" and "this scanner never ran"
    are different facts, and the join barrier (U10) has to tell them apart: it
    treats a present marker as a completion signal and an ABSENT one as a scanner
    that never signalled. If the quiet path wrote no marker, then every quiet
    night -- the common night -- would look to the barrier exactly like a hung
    pass: a fully quiet night would time out and hard-fail instead of closing
    cleanly, and a half-quiet night would ship a plan stamped PARTIAL blaming a
    scanner that was healthy and simply had nothing to report.

    So the marker is written with an empty `story_ids` and no `daily_epic`. Only
    a scanner that genuinely never got here leaves no marker behind.
    """
    source = new_findings["source"]
    if new_findings["nothing_to_file"]:
        validate_plan(plan, [], source=source)
        print(f"no new {source} findings tonight — filing nothing. This is expected.")
        return {
            "nothing_to_file": True,
            "source": source,
            "run_date": run_date,
            # The completion marker for a done-with-nothing pass. No GitHub state
            # is touched to produce it; it records only that this pass finished.
            "ledger_fields": ledger_fields(None, []),
        }

    validated = validate_plan(plan, new_findings["finding_ids"], source=source)
    patterns = patterns if patterns is not None else load_banned_patterns()

    umbrella = find_issue_by_exact_title(repo, umbrella_title)
    if umbrella is None:
        raise TriageError(
            f"the runtime umbrella EPIC {umbrella_title!r} does not exist; run "
            "ensure_umbrella_epic.py first. Filing a dated parent with no home "
            "would put the night's findings on an intake path nobody watches"
        )

    epic = ensure_daily_epic(repo, run_date, umbrella)
    filed = [
        file_group(
            repo,
            group,
            run_date=run_date,
            source=source,
            daily_epic=epic["number"],
            findings_uri=findings_uri,
            run_id=run_id,
            patterns=patterns,
        )
        for group in validated["groups"]
    ]
    return {
        "nothing_to_file": False,
        "source": source,
        "run_date": run_date,
        "umbrella": umbrella,
        "daily_epic": epic["number"],
        "daily_epic_created": epic["created"],
        "work_items": filed,
        "ledger_fields": ledger_fields(epic["number"], filed),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _load_plan(path: Path | str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TriageError(f"cannot read the grouping plan {path}: {exc}") from exc


def _resolve_run_date(args: argparse.Namespace, new_findings: dict, plan: dict) -> str:
    run_date = args.run_date or new_findings.get("run_date") or plan.get("run_date")
    if not isinstance(run_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", run_date):
        raise TriageError(
            f"run date {run_date!r} is not YYYY-MM-DD; the dated parent's identity "
            "is its date, so an unresolved one would file an unmatched parent"
        )
    return run_date


def _cmd_validate(args: argparse.Namespace) -> int:
    """Validate a plan without touching GitHub. The architect's self-check."""
    new_findings = load_new_findings(args.new_findings, args.source)
    plan = _load_plan(args.plan)
    run_date = _resolve_run_date(args, new_findings, plan)
    validated = validate_plan(plan, new_findings["finding_ids"], source=args.source)
    patterns = load_banned_patterns(args.banned_patterns)
    for group in validated["groups"]:
        lint_body(
            render_body(
                group,
                run_date=run_date,
                source=args.source,
                findings_uri=args.findings_uri,
                run_id=args.run_id,
            ),
            patterns,
        )
    low, high = group_count_band(len(new_findings["finding_ids"]))
    print(
        f"plan ok: source={args.source} new_findings={len(new_findings['finding_ids'])} "
        f"work_items={len(validated['groups'])} band={low}..{high}"
    )
    return 0


def _cmd_file(args: argparse.Namespace) -> int:
    # Checked BEFORE anything is filed. A marker this pass cannot write is a
    # wiring bug, and discovering it after the issues exist means the retry that
    # fixes it runs against GitHub state the first attempt already created.
    if args.ledger_dir and not args.generated_at:
        raise TriageError(
            "--ledger-dir needs --generated-at: the marker's timestamp is the "
            "caller's, so re-running a night rewrites the same shard rather than "
            "a differing one"
        )
    new_findings = load_new_findings(args.new_findings, args.source)
    plan = _load_plan(args.plan)
    run_date = _resolve_run_date(args, new_findings, plan)
    result = run_triage(
        args.repo,
        plan=plan,
        new_findings=new_findings,
        findings_uri=args.findings_uri,
        run_id=args.run_id,
        run_date=run_date,
        patterns=load_banned_patterns(args.banned_patterns),
    )
    # Counts and issue numbers only. No titles, paths or finding detail on a CI
    # log, which is readable by anyone who can see the run (NEV-2).
    if result["nothing_to_file"]:
        print(f"nothing_to_file=true source={args.source}")
    else:
        print(
            f"nothing_to_file=false source={args.source} "
            f"daily_epic={result['daily_epic']} "
            f"stories_created={result['ledger_fields']['stories_created']}"
        )
    # Written on BOTH paths. The quiet path's marker is what tells the join
    # barrier this pass completed rather than hung -- see run_triage. An early
    # return here was the bug: it made a healthy quiet scanner indistinguishable
    # from one that never ran.
    if args.ledger_dir:
        out = write_marker(
            args.ledger_dir,
            run_date=run_date,
            source=args.source,
            generated_at=args.generated_at,
            fields=result["ledger_fields"],
        )
        print(f"wrote completion marker {out.name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Group a night's new security findings into work items under "
        "that night's dated EPIC (intent #4290, unit U9)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--plan", required=True, help="the architect's grouping plan (JSON)")
        sp.add_argument(
            "--new-findings",
            required=True,
            help="dedup_security_findings.py `diff` output for tonight",
        )
        sp.add_argument("--source", required=True, choices=SOURCES)
        sp.add_argument("--run-date", help="YYYY-MM-DD; defaults to the input documents'")
        sp.add_argument(
            "--findings-uri",
            required=True,
            help="s3:// URI of the private run ledger prefix holding the detail",
        )
        sp.add_argument("--run-id", required=True, help="the nightly run id, for traceability")
        sp.add_argument("--banned-patterns", default=str(BANNED_PATTERNS_PATH))

    validate = sub.add_parser("validate", help="check a plan; touches no GitHub state")
    common(validate)
    validate.set_defaults(func=_cmd_validate)

    file_cmd = sub.add_parser("file", help="materialize the plan as issues")
    common(file_cmd)
    file_cmd.add_argument("--repo", required=True, help="Repository (owner/name)")
    # A DIRECTORY, not a file. The shard's name is derived from its stage id
    # (`shard-triage.<source>.json`), so the stage id lives in code and cannot
    # drift from the filename or from the delivery job's marker glob.
    file_cmd.add_argument(
        "--ledger-dir", help="directory to write this pass's completion marker shard into"
    )
    file_cmd.add_argument(
        "--generated-at",
        help="ISO-8601, from the caller; required with --ledger-dir. Supplied rather "
        "than read from a clock here so a re-run writes a byte-identical shard",
    )
    file_cmd.set_defaults(func=_cmd_file)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    # `LedgerError` too: `build_shard` validates the marker before it is written,
    # so a bad `--generated-at` or an unownable field surfaces as this stage's
    # named error rather than as a traceback the barrier is later blamed for.
    except (TriageError, LedgerError) as exc:
        print(f"::error title=Security findings triage::{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
