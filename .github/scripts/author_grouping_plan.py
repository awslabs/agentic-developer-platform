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


Why not the architect persona's own workflow
--------------------------------------------

``agent-architect.yml`` is issue-bound by construction: its ``workflow_call``
inputs are ``issue_number``/``repo_*``, and the worker reads ``ISSUE_NUMBER``,
runs ``gh issue view``, posts a plan comment and cuts an ``agent/issue-N``
branch. There is no "emit a JSON artifact to a path" mode. On a nightly there is
no issue yet -- the plan is what *creates* the issues -- so using it would mean
inventing a placeholder issue per night to host a run, plus a branch and a
comment per night. This module is deliberately **not issue-bound**: it reads a
file, writes a file, and touches no GitHub state on any path.


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
    "ADP_GROUPING_PLAN_MODEL", "us.anthropic.claude-sonnet-4-20250514-v1:0"
)

# Fifteen prose sections across several groups. Sized so a legitimate plan is
# never truncated into malformed JSON -- a truncated response is indistinguishable
# from a model that cannot produce a plan, and both fail the night, so paying for
# headroom here is cheaper than a false hard failure.
DEFAULT_MAX_TOKENS = 16384

# Bounded. Each retry feeds the *validation error* back, so an attempt is a
# correction rather than a reroll; a plan that is still wrong after this many
# corrections is a failed night, not a plan to file.
DEFAULT_MAX_ATTEMPTS = 2

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


# --------------------------------------------------------------------------
# the prompt
# --------------------------------------------------------------------------


def build_prompt(
    findings: list[dict],
    *,
    source: str,
    run_date: str,
    previous_error: str | None = None,
) -> str:
    """The plan-authoring instruction.

    States the closed field set, the calibrated band and the two prohibitions
    that would otherwise only be discovered at the gate. Telling the model the
    rules it is about to be checked against is not redundant with the gate: the
    gate decides whether the night proceeds, and a first attempt that already
    satisfies it is the difference between one model call and three.
    """
    low, high = tg.group_count_band(len(findings))
    fields = "\n".join(f"- `{name}`" for name in tg._REQUIRED_GROUP_FIELDS)
    retry_note = ""
    if previous_error:
        retry_note = (
            "\n## Your previous attempt was REJECTED\n\n"
            f"{previous_error}\n\n"
            "Fix exactly that. Do not restructure anything the error did not name.\n"
        )
    return f"""You are the architect on an unattended nightly security run for the
{run_date} scan (scanner: `{source}`). Group the findings below into work items
and write the prose each work item needs. Your output is filed directly as
permanently-retained GitHub issues, so there is no later pass that fixes it.

## The findings ({len(findings)})

Metadata only -- the scanner's detail deliberately stays in the private run
ledger. Group by *what fixing them has in common*, not by file or risk label: two
findings belong together when one change closes both, and apart when fixing one
does nothing for the other.

```json
{json.dumps(findings, indent=2, sort_keys=True)}
```

## Output contract

Return ONE JSON object and nothing else -- no prose before or after, no code
fence:

```
{{"groups": [ {{...}}, {{...}} ]}}
```

Produce between {low} and {high} groups (inclusive). This band is calibrated
against a real triage of twelve findings into five work items; outside it the
plan is rejected.

Every group is an object with EXACTLY these keys, all non-empty:

{fields}

Field shapes:
- `slug` -- lowercase kebab-case, unique across groups.
- `title` -- short, specific; it becomes the issue title.
- `finding_ids` -- the `f-...` ids this group covers. Every id above must appear
  in EXACTLY ONE group: an uncovered finding silently never becomes work, and a
  duplicated one gets fixed twice by two agents.
- `risks` -- a non-empty array of objects with exactly `bug_class` and
  `blast_radius`, both non-empty. These are the ways *the fix* can go wrong.
- `fix_surface` -- a non-empty array of non-empty repository paths or areas.
- everything else -- prose. `problem` is 2-4 sentences a non-engineer
  understands: what someone experiences, not which function is wrong.
  `fix_in_one_line` is one sentence. `deployment` says what must happen after
  the PR merges (which workflow fires, what needs a manual apply, how to roll
  back). `validation` says how to prove the fix works.

Write every field as if a reviewer will read only that field. Placeholder or
templated text is worse than no issue at all, because a filed issue cannot be
recalled.

## Two hard prohibitions

1. **No reproduction detail.** No request lines, no `curl`, no payloads, no
   credentials, no attack-tooling names, and no section that walks a reader
   through triggering the defect. Name the defect class and the surface; the
   detail is in the ledger. A body carrying any of these is rejected and the
   night fails. This applies to your wording too -- a body is scanned for the
   shapes of that detail, not for intent.
2. **No `@agent-` mention anywhere.** A mention dispatches an agent the moment
   the issue is created and bypasses wave sequencing. Reference findings by id.
{retry_note}
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


def parse_groups(text: str) -> list:
    """Extract the `groups` array from a model response.

    Only ``groups`` is taken from the model. ``schema_version``, ``source`` and
    ``run_date`` are stamped by this script, so the model cannot get them wrong
    and cannot cross two scanners' findings into one plan by mislabelling it.
    """
    stripped = _JSON_FENCE_RE.sub("", text.strip()).strip()
    try:
        document = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise PlanAuthoringError(
            f"the plan-authoring model did not return parseable JSON: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise PlanAuthoringError(
            f"the plan-authoring model returned a {type(document).__name__}, not an object"
        )
    groups = document.get("groups")
    if not isinstance(groups, list):
        raise PlanAuthoringError("the authored plan carries no `groups` array")
    return groups


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
    patterns: list[dict] | None = None,
) -> dict:
    """Return a plan that has already passed the gate, or raise.

    There is no third outcome. Nothing here returns a plan it could not
    validate, and nothing here substitutes a default for one -- see the module
    docstring on why a placeholder plan is worse than a failed night.
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
    patterns = patterns if patterns is not None else tg.load_banned_patterns()

    previous_error: str | None = None
    for attempt in range(1, max_attempts + 1):
        prompt = build_prompt(
            prompt_input, source=source, run_date=run_date, previous_error=previous_error
        )
        try:
            groups = parse_groups(
                invoke_model(client, prompt, model_id=model_id, max_tokens=max_tokens)
            )
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
        except (PlanAuthoringError, TriageError) as exc:
            previous_error = str(exc)
            print(
                f"attempt {attempt}/{max_attempts} rejected: {previous_error}",
                file=sys.stderr,
            )
    raise PlanAuthoringError(
        f"no valid grouping plan after {max_attempts} attempt(s); last rejection: "
        f"{previous_error}. Failing the night rather than filing an unvalidated "
        "plan -- a failed night is recoverable, a filed issue is not"
    )


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
    client = None
    if not new_findings["nothing_to_file"]:
        prompt_input = prompt_findings(
            args.new_findings, new_findings["finding_ids"], args.source
        )
        import boto3  # noqa: PLC0415 - imported late so --help works unprovisioned

        client = boto3.client("bedrock-runtime")

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
        patterns=tg.load_banned_patterns(args.banned_patterns),
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(plan, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    # Counts only. Titles and prose stay out of a CI log for the same reason
    # they stay out of anything but the issue itself (NEV-2).
    print(
        f"grouping plan written: source={args.source} "
        f"new_findings={len(new_findings['finding_ids'])} "
        f"groups={len(plan['groups'])} gate=passed"
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
        help="bounded corrections; exhaustion fails the night, it does not fall back",
    )
    parser.add_argument("--banned-patterns", default=str(tg.BANNED_PATTERNS_PATH))
    parser.set_defaults(func=_cmd_author)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (PlanAuthoringError, TriageError) as exc:
        print(f"::error title=Security grouping plan::{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
