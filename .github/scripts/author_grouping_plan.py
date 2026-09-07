#!/usr/bin/env python3
"""Author the nightly grouping plan that U9 files from (intent #4290, #4618).

``triage_group_findings.py file`` cannot run without ``--plan``: the grouping
decision -- which of tonight's genuinely-new findings cluster into one piece of
work, and what that work item says -- is an input to it, not something it
derives. On a nightly there is no architect awake to supply one. This module is
that producer, and the gate that protects what it feeds.


Why an agent authors it, and why there is no deterministic fallback
------------------------------------------------------------------

The tempting shortcut is a mechanical rule: chunk the sorted findings into
groups of three and the count lands on ``ceil(n / 3)``, the lower bound of
``triage_group_findings.group_count_band``. The band is genuinely satisfiable
that way. The band is not the obstacle.

``_REQUIRED_GROUP_FIELDS`` is *all fifteen* of ``_GROUP_FIELDS``, each required
non-empty, and ten of them are prose -- ``problem``, ``fix_in_one_line``,
``goal``, ``motivation``, ``who_benefits``, ``who_is_impacted``,
``cost_footprint``, ``approach``, ``deployment``, ``validation``.
``render_body`` puts every one of them **verbatim** into a GitHub issue, and a
filed issue cannot be recalled. A mechanical rule can only fill fifteen prose
sections with placeholders, so it would satisfy every mechanical gate in U9
while filing exactly the empty-section bodies CLAUDE.md calls a code smell --
nightly, permanently, under a dated EPIC. "An architect regroups later" does not
unfile a body.

So the judgment is a model call, and the protection is a gate rather than a
fallback: **a plan that does not pass fails the night.** The asymmetry is
decisive. A failed night is recoverable by re-dispatch and costs one wasted
scan; a night that files fifteen-placeholder-section issues is not recoverable
at all. Nothing in this module degrades to a default plan, and nothing writes
the output file until the gate has passed.


Two phases, because grouping and writing are different jobs
-----------------------------------------------------------

A whole-repo night is dozens of new findings, and asking for the whole plan in
one response asks the model to do two unrelated things at once: decide the
clustering (which needs to see ALL the findings) and write fifteen prose sections
per cluster (which needs to see only one cluster). Doing both in one call fails
in three separate ways, each observed on a real replay of the 2026-08-30 scan:

* **Output overflow.** ~25 work items x fifteen prose sections overran the token
  ceiling and truncated mid-string, which is indistinguishable from a model that
  cannot produce a plan (runs 34056793941, 34059125104).
* **Schema drift.** Asked for a large repeated structure, the model dropped
  ``finding_ids`` from a group, then -- corrected -- added an unlisted key
  instead, and kept trading one violation for another until the attempts ran out
  (runs 34063284310, 34063907234). Batching the findings did not fix it: the
  wall is the shape of the response, not its size.
* **Grouping quality.** A model that only ever sees a slice of the night cannot
  notice that two findings in different slices share one fix.

So the night is authored in two phases:

1. **Clustering** -- ONE call that sees every finding and returns only
   ``slug``/``title``/``finding_ids`` per cluster. Tiny output, so it cannot
   overflow; a flat three-key shape, so there is little to drift; and it sees the
   whole night, so shared-fix grouping is actually possible.
2. **Authoring** -- one call PER cluster, in parallel, each returning the prose
   for a single work item. Small output, and the model is not asked for
   ``slug``/``title``/``finding_ids`` at all: those are stamped from phase 1. The
   field the model kept dropping is therefore one it can no longer touch.

The phases' outputs are assembled and put through the *same* ``gate_plan`` a
single call's output faced. Both phases retry on their own rejection, bounded,
and a phase that stays wrong fails the night.

The split also decides WHO SEES WHAT, which is the reason it is not merely a
performance change. Phase 1 needs to know which defects share a fix, so it gets
the seven metadata fields for every finding and nothing more. Phase 2 has to
describe one defect accurately and write a test that proves it is closed, so it
gets that cluster's findings *plus the scanner's own account of them* --
``description`` and ``attackScript``, read from the raw document (see
``_DETAIL_FINDING_FIELDS``). A test cannot assert an attack no longer works if it
was written without knowing the attack. That detail is for the model to read, not
to reproduce: the prompt requires it to be expressed as an asserted behaviour,
and ``lint_body`` scans the rendered body against the banned-pattern list
regardless, so a body that echoes a request fails the night rather than filing.


Why not the architect persona's own workflow
--------------------------------------------

``agent-architect.yml`` is issue-bound by construction: its ``workflow_call``
inputs are ``issue_number``/``repo_*``, and the worker reads ``ISSUE_NUMBER``,
runs an issue lookup, posts a plan comment and cuts an ``agent/issue-N`` branch.
There is no "emit a JSON artifact to a path" mode. On a nightly there is no issue
yet -- the plan is what *creates* the issues -- so using it would mean inventing
a placeholder issue per night to host a run, plus a branch and a comment per
night. This module is deliberately **not issue-bound**: it reads a file, writes a
file, and touches no GitHub state on any path.


The gate is U9's own check, not a second opinion
------------------------------------------------

The candidate plan is put through ``validate_plan`` and then through
``lint_body(render_body(...))`` for every group -- the same two calls, in the
same order, that ``triage_group_findings.py validate`` makes. Importing them
rather than restating them is the point: a second implementation of "is this
plan fileable" is a second answer that can disagree with the one that actually
guards the filing step. The downstream ``validate`` invocation stays in the
workflow as the independent gate; this one exists so a plan that would fail it
is never written to disk in the first place.


The quiet night is the common night, and it is easy to get wrong
---------------------------------------------------------------

``--plan`` is required on the quiet path too, and ``validate_plan`` demands
``groups: []`` when nothing survived dedup. So a quiet night needs a *valid
empty plan*, not a skipped step. That path makes **no model call at all**: there
is nothing to group, and an empty plan is not a judgment.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import triage_group_findings as tg  # noqa: E402
from triage_group_findings import (  # noqa: E402
    PLAN_SCHEMA_VERSION,
    SOURCES,
    TriageError,
)

# The model that authors the plan. `bedrock:InvokeModel` is already on the
# runner role, so this needs nothing new IAM-side. Overridable by flag and by
# environment for the same reason the ingest classifier is: the pinned id is a
# capability choice that gets revised faster than this file does.
DEFAULT_MODEL_ID = os.environ.get(
    "ADP_GROUPING_PLAN_MODEL", "us.anthropic.claude-opus-5"
)

# A ceiling, not a target, and generous on purpose: a truncated response is
# indistinguishable from a model that cannot produce a plan, and both fail the
# night, so paying for headroom is cheaper than a false hard failure.
#
# Under the two-phase split neither call comes close. Clustering emits three
# short fields per cluster; authoring emits one work item's prose. The single
# knob covers both because it costs nothing when unused -- only generated tokens
# are billed.
DEFAULT_MAX_TOKENS = 32768

# Bounded. Each retry feeds the *validation error* back, so an attempt is a
# correction rather than a reroll; a phase that is still wrong after this many
# corrections is a failed night, not a plan to file. Applies per phase-1 call and
# per phase-2 cluster, so one difficult work item cannot spend the whole night's
# budget.
DEFAULT_MAX_ATTEMPTS = 4

# How many work items are authored at once in phase 2. The per-cluster calls are
# independent, so the night's authoring latency is (clusters / concurrency)
# rather than clusters -- a ~25-item night finishes in a handful of waves instead
# of twenty-five serial calls. Capped rather than unbounded because the far side
# is a metered, throttled service: exceeding its concurrency turns a working
# night into a wave of retries.
DEFAULT_MAX_CONCURRENCY = int(os.environ.get("ADP_GROUPING_MAX_CONCURRENCY", "6"))

# The complete set of per-finding fields that may enter the prompt. An
# allow-list for the same reason `normalize_security_findings._FIELD_MAP` is
# one: the findings document is produced from an open-set service schema, so a
# deny-list would pass through whatever field the service adds next -- including
# an exploit-bearing one. Everything here is metadata about WHERE and WHAT
# CLASS; nothing here is reproduction detail.
_PROMPT_FINDING_FIELDS = (
    "finding_id",
    "title",
    "risk_type",
    "risk_level",
    "confidence",
    "file_path",
    "key_files",
)

# The scanner's own account of a finding, read from the RAW findings document and
# given to phase 2 only.
#
# This is a deliberate, reviewed widening of what reaches a model, and the
# reasoning is worth stating because the default here was the opposite. The
# normalized findings document carries neither of these fields --
# `normalize_security_findings._FIELD_MAP` drops them immediately after the scan
# -- so a work item authored from it is written from a title and a file path. Two
# of the fifteen sections suffer specifically:
#
#   * `problem`/`approach` describe a defect the model has only been told the
#     NAME of, which is how a plausible-but-wrong description gets filed.
#   * `validation` is supposed to prove the fix CLOSES the defect. A test written
#     without knowing how the defect is reached cannot assert that; it can only
#     assert the new code runs.
#
# So `attackScript` is here for the same reason `description` is: a regression
# test that proves an attack no longer works has to be derived from the attack.
#
# What does NOT change is where this content may END UP. It is read for context
# and must not be reproduced: the authoring prompt requires the attack to be
# expressed as a test ASSERTION rather than a request, and `lint_body` still
# scans the rendered body against the banned-pattern list and fails the night on
# a hit. The widening is to the prompt, not to the issue.
#
# Read from the raw document rather than by adding rows to `_FIELD_MAP` on
# purpose: that map governs the normalized artifact, which also feeds the
# accepted baseline, the U2 ledger, the run report and the PR comment. Widening
# it would put exploit detail into all of them to serve one prompt.
_DETAIL_FINDING_FIELDS = ("description", "attackScript")

# Phase 1's output shape: the clustering decision and nothing else. Three keys,
# flat, so there is very little for a model to drift on.
_CLUSTER_FIELDS = ("slug", "title", "finding_ids")

# Phase 2's output shape: everything a work item needs EXCEPT the three keys
# phase 1 already decided. Derived from U9's own required-field set by
# subtraction, so the two phases between them always ask for exactly what the
# downstream gate requires -- a field added there is automatically asked of the
# authoring call rather than silently missing from it.
_AUTHORED_FIELDS = tuple(sorted(set(tg._REQUIRED_GROUP_FIELDS) - set(_CLUSTER_FIELDS)))

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class PlanAuthoringError(RuntimeError):
    """The plan could not be authored, or what was authored is not fileable."""


# --------------------------------------------------------------------------
# the night's input
# --------------------------------------------------------------------------


def prompt_findings(path: Path | str, finding_ids: list[str], source: str) -> list[dict]:
    """Project the night's findings down to the fields the prompt may carry.

    Selection is driven by the id list ``load_new_findings`` already returned,
    not by a second filter over the same document. Two selection rules over one
    input is two answers to "which findings are tonight's", and the one that
    matters is the one U9's validator will check the plan against.
    """
    wanted = set(finding_ids)
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    projected: dict[str, dict] = {}
    for finding in document.get("new_findings") or []:
        if not isinstance(finding, dict) or finding.get("source") != source:
            continue
        finding_id = finding.get("finding_id")
        if finding_id not in wanted or finding_id in projected:
            continue
        projected[finding_id] = {
            field: finding[field] for field in _PROMPT_FINDING_FIELDS if field in finding
        }
    missing = sorted(wanted - set(projected))
    if missing:
        raise PlanAuthoringError(
            f"{len(missing)} finding(s) the dedup result reports as new are not "
            f"readable back out of it: {missing}. Grouping cannot cover a finding "
            "it cannot describe"
        )
    return [projected[fid] for fid in sorted(projected)]


def detail_by_finding(path: Path | str, finding_ids: list[str]) -> dict[str, dict]:
    """Project the scanner's own account of each finding out of the RAW document.

    Joined by finding id -- the raw document's ``findingId`` is copied verbatim
    into the normalized ``finding_id`` by ``normalize_security_findings``, so the
    two documents agree on identity exactly and this is a lookup rather than a
    match.

    An allow-list again, and for a sharper reason than usual: this reads the one
    artifact in the pipeline that still holds everything the service said, so a
    deny-list here would hand the model whatever field the service adds next.
    Only the two fields named in ``_DETAIL_FINDING_FIELDS`` are taken.

    A finding with no detail is normal (the service does not always populate
    both), and is simply absent from the result: the authoring prompt renders
    what it has. A missing or unreadable DOCUMENT, by contrast, is an error --
    silently authoring the whole night from titles alone is the quality
    regression this function exists to prevent, and it would look like success.
    """
    file_path = Path(path)
    try:
        document = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanAuthoringError(
            f"cannot read the raw findings document {file_path}: {exc}. Refusing to "
            "author the night from titles alone"
        ) from exc
    if not isinstance(document, dict) or not isinstance(document.get("findings"), list):
        raise PlanAuthoringError(
            f"{file_path} has no `findings` array; it is not the scanner's raw document"
        )
    wanted = set(finding_ids)
    out: dict[str, dict] = {}
    for finding in document["findings"]:
        if not isinstance(finding, dict):
            continue
        finding_id = finding.get("findingId")
        if finding_id not in wanted:
            continue
        detail = {
            field: finding[field]
            for field in _DETAIL_FINDING_FIELDS
            if isinstance(finding.get(field), str) and finding[field].strip()
        }
        if detail:
            out[finding_id] = detail
    return out


# --------------------------------------------------------------------------
# phase 1: the prompt that decides the clustering
# --------------------------------------------------------------------------

_PROHIBITIONS = """## Two hard prohibitions

1. **No reproduction detail.** No request lines, no `curl`, no payloads, no
   credentials, no attack-tooling names, and no text that walks a reader through
   triggering the defect. Name the defect class and the surface; the detail is in
   the private ledger. Output carrying any of these is rejected and the night
   fails. This applies to your wording too -- what you write is scanned for the
   shapes of that detail, not for intent.
2. **No `@agent-` mention anywhere.** A mention dispatches an agent the moment
   the issue is created and bypasses wave sequencing. Reference findings by id."""


def _retry_note(previous_error: str | None) -> str:
    if not previous_error:
        return ""
    return (
        "\n## Your previous attempt was REJECTED\n\n"
        f"{previous_error}\n\n"
        "Fix exactly that. Do not restructure anything the error did not name.\n"
    )


def build_cluster_prompt(
    findings: list[dict],
    *,
    source: str,
    run_date: str,
    previous_error: str | None = None,
) -> str:
    """Phase 1: group the whole night's findings, and decide nothing else.

    Every finding is in this one prompt, because that is the only way a shared
    fix across two distant findings can be noticed. The response is deliberately
    tiny -- three short fields per cluster -- so this call cannot overflow and has
    almost no surface to get structurally wrong.
    """
    low, high = tg.group_count_band(len(findings))
    return f"""You are the architect on an unattended nightly security run for the
{run_date} scan (scanner: `{source}`). Your ONLY job in this step is to decide
which findings are ONE piece of work. You are not writing any issue text yet.

## The findings ({len(findings)})

Metadata only -- the scanner's detail deliberately stays in the private run
ledger. Group by *what fixing them has in common*, not by file or risk label: two
findings belong together when one change closes both, and apart when fixing one
does nothing for the other. You can see every finding of the night here, so look
for shared fixes across different files, not just within one file.

```json
{json.dumps(findings, indent=2, sort_keys=True)}
```

## Output contract

Return ONE JSON object and nothing else -- no prose before or after, no code
fence:

```
{{"clusters": [ {{...}}, {{...}} ]}}
```

Produce between {low} and {high} clusters (inclusive). This band is calibrated
against a real triage of twelve findings into five work items; outside it your
answer is rejected.

Every cluster is an object with EXACTLY these three keys and no others:

- `slug` -- lowercase kebab-case, unique across clusters. Names the shared fix.
- `title` -- short and specific; it becomes the issue title. Describe the FIX or
  the defect in plain words a non-engineer can read.
- `finding_ids` -- the `f-...` ids in this cluster.

Every id listed above must appear in EXACTLY ONE cluster. A finding in no cluster
silently never becomes work; a finding in two clusters gets fixed twice by two
agents. Both are rejected.

{_PROHIBITIONS}
{_retry_note(previous_error)}
Return the JSON object now."""


# --------------------------------------------------------------------------
# phase 2: the prompt that writes ONE work item
# --------------------------------------------------------------------------


def _detail_block(findings: list[dict], details: dict[str, dict]) -> str:
    """Render the scanner's account of each finding in this cluster, if we have it."""
    present = [(f["finding_id"], details[f["finding_id"]]) for f in findings
               if f["finding_id"] in details]
    if not present:
        return ""
    sections = []
    for finding_id, detail in present:
        body = "\n\n".join(
            f"{field}:\n{detail[field].strip()}"
            for field in _DETAIL_FINDING_FIELDS
            if field in detail
        )
        sections.append(f"### {finding_id}\n\n{body}")
    joined = "\n\n".join(sections)
    return f"""
## What the scanner found, in its own words

This is the scanner's analysis of each finding, including how the defect is
reached. It is given to you so that the fix you describe addresses the real
mechanism and so that `validation` can assert the defect is CLOSED rather than
merely that new code runs.

**Read it; do not reproduce it.** See the rule on turning an attack into a test
below, and the prohibitions at the end. This material is from a private ledger
and the issue you are writing is permanent.

{joined}
"""


def build_authoring_prompt(
    cluster: dict,
    findings: list[dict],
    *,
    source: str,
    run_date: str,
    details: dict[str, dict] | None = None,
    previous_error: str | None = None,
) -> str:
    """Phase 2: write the prose for a single, already-decided work item.

    The model is given one cluster and asked for one issue's worth of text. It is
    NOT asked for the cluster's identity or its finding ids -- those are stamped
    from phase 1 -- so the field that kept getting dropped is not in play here.

    The field guidance is written as a template a HUMAN reads, because the
    fifteen sections land verbatim in an issue that triagers, managers and
    customer-facing engineers read before any engineer does. Precision about the
    surface belongs in `fix_surface` and `approach`; the rest must make sense to
    someone who has never opened this repository.
    """
    keys = "\n".join(f"- `{name}`" for name in _AUTHORED_FIELDS)
    return f"""You are the architect on an unattended nightly security run for the
{run_date} scan (scanner: `{source}`). One work item has already been decided.
Write its issue text. Your output is filed directly as a permanently-retained
GitHub issue, so there is no later pass that fixes it.

## The work item

- Fix: **{cluster["title"]}**
- It covers these {len(cluster["finding_ids"])} finding(s):

```json
{json.dumps(findings, indent=2, sort_keys=True)}
```

The filed issue references these findings by id.
{_detail_block(findings, details or {})}
## Output contract

Return ONE JSON object and nothing else -- no prose before or after, no code
fence. It has EXACTLY these {len(_AUTHORED_FIELDS)} keys, every one present and
non-empty, and no others:

{keys}

Do not include the work item's slug, its title, or its finding ids: those are
already decided and will be attached for you. Adding them, or adding any other
key, is rejected.

## How to write it (this is the template, follow it)

Write for a reader who has never seen this codebase -- the person who triages,
prioritises, or explains this to a customer. Lead with what it means for a
person, not how the code works. Plain sentences. Spell out any acronym you use.
Do not put file paths, line numbers or function names in the human-facing fields
below; the two fields that name the surface are `fix_surface` and `approach`.

- `problem` -- 2 to 4 sentences. What can someone DO that they should not be
  able to, or what breaks for them, and what it costs them. Describe the
  experience, not the mechanism. No file paths, no function names.
- `fix_in_one_line` -- one sentence: what the change does, in plain words.
- `goal` -- one short paragraph: what this work item achieves.
- `motivation` -- one short paragraph: why it matters that this is fixed, and
  what happens if it is not.
- `who_benefits` -- REQUIRED, and the most commonly forgotten. The people or
  roles who are better off (e.g. "every tenant whose data is on the shared
  plane"). Not a list of components. There is always an answer: if nobody
  outside the team benefits, say which operators or reviewers do.
- `who_is_impacted` -- REQUIRED, and forgotten alongside the one above. The
  surfaces or teams this change touches, in plain words (e.g. "billing, support,
  anyone auditing access"). If the blast radius is small, say so explicitly
  rather than leaving it out.
- `risks` -- a non-empty array of objects with exactly `bug_class` and
  `blast_radius`, both non-empty. These are ways *the fix itself* can go wrong
  and who is hurt if it does -- not a restatement of the finding.
- `cost_footprint` -- what this adds in money, quota or resources; say "code
  only, no new resources" when that is the truth.
- `fix_surface` -- a non-empty array of repository paths or areas to change.
  This is the one place where being specific about location is the point.
- `approach` -- the concrete shape of the fix: what to change and how it is
  enforced. A developer should be able to start from this.
- `deployment` -- what must happen after the pull request merges for the fix to
  take effect: which pipeline runs, what needs a manual apply, how to roll back.
- `validation` -- how to prove the fix works. Name the tests to add and the one
  end-to-end check an operator can run. Read the rule below before writing this.

## Turning an attack into a test, without writing the attack down

`validation` must assert the defect is CLOSED, which means it has to be derived
from how the defect is reached -- but it must state the test as a BEHAVIOUR that
is asserted, never as a request, command or payload a reader could replay.

- Write: "assert that a caller whose author association is below the configured
  minimum is rejected on every dispatch path, not only on issue comments."
- Never write: the request line, the command, the header, the payload, the
  parameter values, or a numbered sequence that reproduces the defect.

The same test is expressed either way; only one of them is safe in a permanent
document. If you cannot describe a check without writing the request, describe
the invariant it protects instead.

Write every field as if a reviewer will read only that field. Placeholder or
templated text is worse than no issue at all, because a filed issue cannot be
recalled.

{_PROHIBITIONS}
{_retry_note(previous_error)}
Return the JSON object now."""


# --------------------------------------------------------------------------
# the model seam
# --------------------------------------------------------------------------


def invoke_model(client, prompt: str, *, model_id: str, max_tokens: int) -> str:
    """One Bedrock call. The client is injected so every path above it is
    testable without AWS, the same seam shape the ledger's S3 writer uses."""
    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
    )
    try:
        response = client.invoke_model(
            modelId=model_id,
            contentType="application/json",
            accept="application/json",
            body=body,
        )
        payload = json.loads(response["body"].read())
    except Exception as exc:  # noqa: BLE001 - any failure here is one failure mode
        raise PlanAuthoringError(f"the plan-authoring model call failed: {exc}") from exc
    blocks = payload.get("content")
    if not isinstance(blocks, list) or not blocks:
        raise PlanAuthoringError("the plan-authoring model returned no content")
    text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
    if not text.strip():
        raise PlanAuthoringError("the plan-authoring model returned an empty response")
    return text


def parse_object(text: str) -> dict:
    """Extract the one JSON object from a model response.

    Only the model's *content* is ever taken from a response.
    ``schema_version``, ``source`` and ``run_date`` are stamped by this script,
    so the model cannot get them wrong and cannot cross two scanners' findings
    into one plan by mislabelling it.
    """
    stripped = _JSON_FENCE_RE.sub("", text.strip()).strip()
    try:
        document = json.loads(stripped)
    except json.JSONDecodeError as exc:
        # A model that emits the object and then keeps talking ("Extra data: line
        # 1 column 3069") has answered correctly and added a courtesy sentence.
        # Failing the night on that is failing on manners, not on content, so the
        # first complete top-level object is extracted and the rest ignored. A
        # response with no complete object still fails -- that is a real answer we
        # cannot read (#4290 replay run 34108572024 lost three work items here).
        document = _first_json_object(stripped)
        if document is None:
            raise PlanAuthoringError(
                f"the plan-authoring model did not return parseable JSON: {exc}"
            ) from exc
    if not isinstance(document, dict):
        raise PlanAuthoringError(
            f"the plan-authoring model returned a {type(document).__name__}, not an object"
        )
    return document


def _first_json_object(text: str) -> dict | None:
    """The first complete ``{...}`` in ``text``, or None.

    Brace-counted with string/escape awareness rather than regex, because every
    field in a work item is prose that can itself contain braces and quotes.
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : index + 1])
                except json.JSONDecodeError:
                    return None
    return None


def parse_clusters(text: str) -> list:
    """Phase 1's response: the `clusters` array."""
    clusters = parse_object(text).get("clusters")
    if not isinstance(clusters, list):
        raise PlanAuthoringError("the clustering response carries no `clusters` array")
    return clusters


def parse_authored(text: str) -> dict:
    """Phase 2's response: one work item's fields, as a flat object."""
    return parse_object(text)


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------


def gate_plan(
    plan: dict,
    new_findings: dict,
    *,
    run_date: str,
    findings_uri: str,
    run_id: str,
    patterns: list[dict] | None = None,
) -> None:
    """Put a candidate plan through U9's own fileability check. Raises on failure.

    Both halves matter and neither implies the other: ``validate_plan`` checks
    the plan (shape, coverage, band, declared fields) and ``lint_body`` checks
    the *rendered body* (section order, `@agent-` mentions, banned patterns) --
    which is what actually becomes permanent.
    """
    source = new_findings["source"]
    validated = tg.validate_plan(plan, new_findings["finding_ids"], source=source)
    patterns = patterns if patterns is not None else tg.load_banned_patterns()
    for group in validated["groups"]:
        tg.lint_body(
            tg.render_body(
                group,
                run_date=run_date,
                source=source,
                findings_uri=findings_uri,
                run_id=run_id,
            ),
            patterns,
        )


# --------------------------------------------------------------------------
# authoring
# --------------------------------------------------------------------------


def empty_plan(*, source: str, run_date: str) -> dict:
    """The quiet night's plan. Valid, and empty by construction."""
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "source": source,
        "run_date": run_date,
        "groups": [],
    }


def _gate_clusters(clusters: object, expected_ids: list[str]) -> list[dict]:
    """Validate phase 1's clustering before a single word is written for it.

    Checks the same three claims ``validate_plan`` makes about coverage and the
    band, on the only fields phase 1 produces. Catching a bad clustering HERE is
    what makes the two-phase split pay: an uncovered finding or an out-of-band
    count costs one cheap re-prompt instead of a wave of prose calls that must
    then be thrown away.
    """
    if not isinstance(clusters, list) or not clusters:
        raise PlanAuthoringError("the clustering produced no clusters")
    expected = set(expected_ids)
    seen_slugs: set[str] = set()
    seen_ids: set[str] = set()
    for index, cluster in enumerate(clusters):
        if not isinstance(cluster, dict):
            raise PlanAuthoringError(f"cluster {index} must be an object")
        unknown = sorted(set(cluster) - set(_CLUSTER_FIELDS))
        if unknown:
            raise PlanAuthoringError(f"cluster {index} has fields outside the schema: {unknown}")
        missing = [f for f in _CLUSTER_FIELDS if not cluster.get(f)]
        if missing:
            raise PlanAuthoringError(f"cluster {index} is missing or empties {missing}")

        slug = cluster["slug"]
        if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
            raise PlanAuthoringError(f"cluster {index} slug {slug!r} must be lowercase kebab-case")
        if slug in seen_slugs:
            raise PlanAuthoringError(f"duplicate cluster slug {slug!r}")
        seen_slugs.add(slug)
        if not isinstance(cluster["title"], str) or not cluster["title"].strip():
            raise PlanAuthoringError(f"cluster {slug!r} has no title")

        ids = cluster["finding_ids"]
        if not isinstance(ids, list) or not ids:
            raise PlanAuthoringError(f"cluster {slug!r} covers no findings")
        for finding_id in ids:
            if not isinstance(finding_id, str) or finding_id not in expected:
                raise PlanAuthoringError(
                    f"cluster {slug!r} references {finding_id!r}, which is not one of "
                    "tonight's new findings"
                )
            if finding_id in seen_ids:
                raise PlanAuthoringError(
                    f"{finding_id} appears in more than one cluster; one defect is one "
                    "piece of work"
                )
            seen_ids.add(finding_id)

    uncovered = sorted(expected - seen_ids)
    if uncovered:
        raise PlanAuthoringError(
            f"{len(uncovered)} finding(s) are in no cluster and would silently never "
            f"become work: {uncovered}"
        )
    low, high = tg.group_count_band(len(expected))
    if not low <= len(clusters) <= high:
        raise PlanAuthoringError(
            f"{len(expected)} new findings clustered into {len(clusters)} work items, "
            f"outside the calibrated band {low}..{high}"
        )
    return clusters


def _form_clusters(
    client,
    findings: list[dict],
    expected_ids: list[str],
    *,
    source: str,
    run_date: str,
    model_id: str,
    max_tokens: int,
    max_attempts: int,
) -> list[dict]:
    """Phase 1, with its bounded correction loop."""
    previous_error: str | None = None
    for attempt in range(1, max_attempts + 1):
        prompt = build_cluster_prompt(
            findings, source=source, run_date=run_date, previous_error=previous_error
        )
        try:
            return _gate_clusters(
                parse_clusters(
                    invoke_model(client, prompt, model_id=model_id, max_tokens=max_tokens)
                ),
                expected_ids,
            )
        except (PlanAuthoringError, TriageError) as exc:
            previous_error = str(exc)
            print(
                f"clustering attempt {attempt}/{max_attempts} rejected: {previous_error}",
                file=sys.stderr,
            )
    raise PlanAuthoringError(
        f"no valid clustering of {len(expected_ids)} findings after {max_attempts} "
        f"attempt(s); last rejection: {previous_error}"
    )


def _author_one(
    client,
    cluster: dict,
    findings: list[dict],
    *,
    source: str,
    run_date: str,
    findings_uri: str,
    run_id: str,
    details: dict[str, dict] | None = None,
    model_id: str,
    max_tokens: int,
    max_attempts: int,
    patterns: list[dict],
) -> dict:
    """Phase 2 for one cluster: write it, gate it, or raise after its attempts run out.

    The returned group is the model's prose with phase 1's identity attached --
    built with ``dict(...)`` so ``slug``, ``title`` and ``finding_ids`` come from
    the clustering decision and NOT from this response. That is the whole point of
    the split: the keys the model kept dropping are not its to supply.

    Gated per work item with ``_validate_group`` plus ``lint_body(render_body())``
    -- the same two checks the merged plan will face -- so a rejection is
    correctable here, against the one item that caused it.
    """
    previous_error: str | None = None
    for attempt in range(1, max_attempts + 1):
        prompt = build_authoring_prompt(
            cluster,
            findings,
            source=source,
            run_date=run_date,
            details=details,
            previous_error=previous_error,
        )
        try:
            response = parse_authored(
                invoke_model(client, prompt, model_id=model_id, max_tokens=max_tokens)
            )
            # PROJECTED to the allow-list, not rejected for carrying extras.
            #
            # Rejecting bought nothing and cost a whole attempt. The group below is
            # built from named fields, so a key the model invented could never have
            # reached a rendered body -- the closed-set gate in `_validate_group`
            # is still the backstop that proves it. Meanwhile "invented an extra
            # key" was the single largest rejection class on the first real
            # whole-repo run (#4290 replay 34108572024: seven of them, mostly
            # `*_note_placeholder*` keys this prompt's own no-placeholder rule
            # appears to induce), and every one of those attempts was spent
            # re-authoring prose that was already correct.
            #
            # What is NOT relaxed is absence: a required field that is missing or
            # empty is still a rejection, because that is content the issue needs.
            authored = {k: v for k, v in response.items() if k in _AUTHORED_FIELDS}
            group = dict(
                authored,
                slug=cluster["slug"],
                title=cluster["title"],
                finding_ids=list(cluster["finding_ids"]),
            )
            # A fresh `seen_*` per item: cross-item slug and id uniqueness is
            # phase 1's guarantee and is re-checked by `gate_plan` over the merged
            # plan. Re-checking it here would reject a valid item for a collision
            # it cannot see.
            tg._validate_group(0, group, set(), set(), set(cluster["finding_ids"]))
            tg.lint_body(
                tg.render_body(
                    group,
                    run_date=run_date,
                    source=source,
                    findings_uri=findings_uri,
                    run_id=run_id,
                ),
                patterns,
            )
            return group
        except (PlanAuthoringError, TriageError) as exc:
            previous_error = str(exc)
            print(
                f"work item {cluster['slug']!r} attempt {attempt}/{max_attempts} "
                f"rejected: {previous_error}",
                file=sys.stderr,
            )
    raise PlanAuthoringError(
        f"no valid text for work item {cluster['slug']!r} after {max_attempts} "
        f"attempt(s); last rejection: {previous_error}"
    )


def _author_all(
    client,
    clusters: list[dict],
    findings_by_id: dict[str, dict],
    *,
    max_concurrency: int,
    **kwargs,
) -> list[dict]:
    """Phase 2 across every cluster, in parallel, preserving phase 1's order.

    Order is preserved deliberately: the plan's group order is what the filed
    issues' order follows, and a plan whose order changed between two runs over
    the same findings would make a re-run's diff unreadable.

    A cluster that cannot be authored fails the night. The exception propagates
    out of the pool rather than being collected into a partial plan -- for the
    same reason nothing here falls back to a default: a plan missing a part is not
    fileable, and a failed night is recoverable where a filed issue is not.
    """
    if len(clusters) == 1 or max_concurrency <= 1:
        return [
            _author_one(client, c, [findings_by_id[i] for i in c["finding_ids"]], **kwargs)
            for c in clusters
        ]
    # botocore clients are safe to call from multiple threads once constructed,
    # so one client is shared rather than one per worker.
    workers = min(max_concurrency, len(clusters))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                _author_one,
                client,
                cluster,
                [findings_by_id[i] for i in cluster["finding_ids"]],
                **kwargs,
            )
            for cluster in clusters
        ]
        return [future.result() for future in futures]


def author_plan(
    client,
    new_findings: dict,
    prompt_input: list[dict],
    *,
    run_date: str,
    findings_uri: str,
    run_id: str,
    model_id: str = DEFAULT_MODEL_ID,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    details: dict[str, dict] | None = None,
    patterns: list[dict] | None = None,
) -> dict:
    """Return a plan that has already passed the gate, or raise.

    There is no third outcome. Nothing here returns a plan it could not
    validate, and nothing here substitutes a default for one -- see the module
    docstring on why a placeholder plan is worse than a failed night.

    Two phases (see the module docstring): one call decides the clustering over
    every finding, then one call per cluster writes that work item's prose, in
    parallel. The assembled groups are put through the *same* ``gate_plan`` a
    single-call plan would face -- the band, full coverage, unique slugs and
    per-body lint are all re-checked on the merged result, so the split changes
    how the plan is produced, not what it must satisfy to be filed.
    """
    source = new_findings["source"]
    if new_findings["nothing_to_file"]:
        plan = empty_plan(source=source, run_date=run_date)
        # Gated like any other plan rather than trusted because this module
        # built it: `validate_plan` is what asserts a quiet night proposes no
        # work items, and skipping it here would make the quiet path -- the
        # common path -- the one path with no check on it.
        gate_plan(
            plan,
            new_findings,
            run_date=run_date,
            findings_uri=findings_uri,
            run_id=run_id,
            patterns=patterns,
        )
        return plan

    if max_attempts < 1:
        raise PlanAuthoringError("max_attempts must be at least 1")
    if max_concurrency < 1:
        raise PlanAuthoringError("max_concurrency must be at least 1")
    patterns = patterns if patterns is not None else tg.load_banned_patterns()

    expected_ids = new_findings["finding_ids"]
    findings_by_id = {f["finding_id"]: f for f in prompt_input}

    try:
        clusters = _form_clusters(
            client,
            prompt_input,
            expected_ids,
            source=source,
            run_date=run_date,
            model_id=model_id,
            max_tokens=max_tokens,
            max_attempts=max_attempts,
        )
        groups = _author_all(
            client,
            clusters,
            findings_by_id,
            max_concurrency=max_concurrency,
            details=details,
            source=source,
            run_date=run_date,
            findings_uri=findings_uri,
            run_id=run_id,
            model_id=model_id,
            max_tokens=max_tokens,
            max_attempts=max_attempts,
            patterns=patterns,
        )
    except (PlanAuthoringError, TriageError) as exc:
        # Either phase failing fails the whole night. Re-framed at the night level
        # so the reason is the same fail-closed one every path here gives: file
        # nothing rather than an unvalidated plan.
        raise PlanAuthoringError(
            f"no valid grouping plan after {max_attempts} attempt(s) per call: "
            f"{exc}. Failing the night rather than filing an unvalidated plan -- a "
            "failed night is recoverable, a filed issue is not"
        ) from exc

    plan = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "source": source,
        "run_date": run_date,
        "groups": groups,
    }
    gate_plan(
        plan,
        new_findings,
        run_date=run_date,
        findings_uri=findings_uri,
        run_id=run_id,
        patterns=patterns,
    )
    return plan


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _cmd_author(args: argparse.Namespace) -> int:
    new_findings = tg.load_new_findings(args.new_findings, args.source)
    run_date = args.run_date or new_findings.get("run_date")
    if not isinstance(run_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", run_date):
        raise PlanAuthoringError(
            f"run date {run_date!r} is not YYYY-MM-DD; the rendered bodies and the "
            "dated parent are keyed by it, so an unresolved one cannot be gated"
        )

    output = Path(args.output)
    # A failed run must not leave a plan behind for the filing step to pick up.
    # An earlier attempt's file, or a hand-written one, would otherwise survive
    # this script's failure and be filed as if it had passed the gate.
    output.unlink(missing_ok=True)

    prompt_input: list[dict] = []
    details: dict[str, dict] | None = None
    client = None
    if not new_findings["nothing_to_file"]:
        prompt_input = prompt_findings(
            args.new_findings, new_findings["finding_ids"], args.source
        )
        # The scanner's own account of each finding, for phase 2 only. Optional so
        # the producer still runs where the raw document is not available (a
        # hand-driven re-author, a test); supplied on every real night, because a
        # `validation` section written without it cannot assert the defect is
        # closed. See `_DETAIL_FINDING_FIELDS`.
        if args.raw_findings:
            details = detail_by_finding(args.raw_findings, new_findings["finding_ids"])
            print(
                f"scanner detail available for {len(details)}/"
                f"{len(new_findings['finding_ids'])} finding(s)"
            )
        import boto3  # noqa: PLC0415 - imported late so --help works unprovisioned
        from botocore.config import Config  # noqa: PLC0415

        # A non-streaming invoke_model does not return until the whole response is
        # generated. botocore's default 60s read timeout fires long before that on
        # a real call, so every attempt died as a "Read timeout on endpoint URL"
        # (#4290 replay run 34056074448), never reaching the model's actual output.
        # Give the call real room. botocore's own retries are pinned to a single
        # attempt so a genuine timeout surfaces to the visible attempt loops above
        # instead of being retried invisibly under the step.
        client = boto3.client(
            "bedrock-runtime",
            config=Config(
                read_timeout=1200,
                connect_timeout=15,
                retries={"max_attempts": 1, "mode": "standard"},
            ),
        )

    plan = author_plan(
        client,
        new_findings,
        prompt_input,
        run_date=run_date,
        findings_uri=args.findings_uri,
        run_id=args.run_id,
        model_id=args.model_id,
        max_tokens=args.max_tokens,
        max_attempts=args.max_attempts,
        max_concurrency=args.max_concurrency,
        details=details,
        patterns=tg.load_banned_patterns(args.banned_patterns),
    )

    # The grouping stage of the night's traceability ledger: which findings
    # clustered together and each cluster's computed severity (worst risk level
    # among its findings). The filing step later folds in the issue numbers.
    # `severity_by_finding` reads only ids and risk levels -- metadata, NEV-2-safe.
    #
    # BUILT BEFORE THE PLAN IS WRITTEN, deliberately. Anything that can fail here
    # -- an unreadable findings document, a risk level outside the vocabulary --
    # must fail while this step has still written nothing, or a failed run leaves
    # a plan on disk for the filing step to pick up as though it had passed. That
    # is the invariant the unlink at the top of this function exists to hold.
    trace = None
    sec_traceability = None
    if args.traceability:
        import security_traceability as sec_traceability  # noqa: PLC0415

        severities = sec_traceability.severity_by_finding(args.new_findings, args.source)
        trace = sec_traceability.build_grouping(plan, severities, run_id=args.run_id)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(plan, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    # Counts only. Titles and prose stay out of a CI log for the same reason
    # they stay out of anything but the issue itself (NEV-2).
    print(
        f"grouping plan written: source={args.source} "
        f"new_findings={len(new_findings['finding_ids'])} "
        f"groups={len(plan['groups'])} gate=passed"
    )

    if trace is not None:
        trace_out = sec_traceability.write_ledger(args.traceability, trace)
        print(
            f"traceability grouping stage written: {trace_out.name} "
            f"findings_total={trace['findings_total']} groups_total={trace['groups_total']}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Author the nightly grouping plan that triage_group_findings.py "
        "files from, gated by that module's own fileability check (intent #4290)"
    )
    parser.add_argument(
        "--new-findings",
        required=True,
        help="dedup_security_findings.py `diff` output for tonight",
    )
    parser.add_argument("--source", required=True, choices=SOURCES)
    parser.add_argument("--output", default="plan.json", help="where to write the plan")
    parser.add_argument(
        "--findings-uri",
        required=True,
        help="s3:// URI of the private run ledger prefix; rendered into each body, "
        "so the gate needs it to lint what will actually be filed",
    )
    parser.add_argument("--run-id", required=True, help="the nightly run id")
    parser.add_argument("--run-date", help="YYYY-MM-DD; defaults to the input document's")
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        help="bounded corrections per call; exhaustion fails the night, it does not "
        "fall back",
    )
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=DEFAULT_MAX_CONCURRENCY,
        help="how many work items are written at once in phase 2; capped because the "
        "far side is a throttled service",
    )
    parser.add_argument(
        "--raw-findings",
        help="the scanner's RAW findings document for this run. Its `description` "
        "and `attackScript` are given to the per-work-item authoring call only, so "
        "the fix addresses the real mechanism and `validation` can assert the defect "
        "is closed. Never rendered into an issue: the body is lint-scanned against "
        "the banned-pattern list either way",
    )
    parser.add_argument("--banned-patterns", default=str(tg.BANNED_PATTERNS_PATH))
    parser.add_argument(
        "--traceability",
        help="also write the grouping stage of the night's finding-to-issue "
        "traceability ledger here (finding ids, cluster slugs, computed severity); "
        "the filing step enriches the same file with issue numbers",
    )
    parser.set_defaults(func=_cmd_author)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Imported here, not at module top, to avoid the import cycle with the
    # traceability module (it imports triage_group_findings, which this imports).
    from security_traceability import TraceabilityError  # noqa: PLC0415

    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (PlanAuthoringError, TriageError, TraceabilityError) as exc:
        print(f"::error title=Security grouping plan::{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
