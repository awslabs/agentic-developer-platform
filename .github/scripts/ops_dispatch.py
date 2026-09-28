#!/usr/bin/env python3
"""Hand the night's plan off, then drive its items (intent #4290, unit U11).

By this point the night has a plan (U10) naming well-formed work items in
dependency order. Somebody has to pick each one up. This module is the handoff
and the fan-out: one root dispatch that starts the delivery role, then one
dispatch per work item, bounded by the night's item count, with prose-only
progress on the plan issue.


Two transports, and picking the wrong one destroys the chain
------------------------------------------------------------

The dispatch mechanism is ruled by ``docs/design-notes/4559-ci-eventbridge-dispatch.md``
(#4559), not chosen here:

* **The root, once per night**: a single ``aws events put-events`` to the default
  bus, which the webhook Lambda turns into the ``operations`` run. A GitHub
  Actions job cannot originate a chain any other way -- ``adp-trigger`` and
  ``POST /agent/trigger`` are both verified closed to root-minting (a fresh call
  has no lineage, a fabricated one is rejected ``422 unknown_chain``), and that
  fail-closed behaviour is the control, not a gap to work around.
* **Every hop inside the night**: ``adp-trigger --persona <p> --issue <N>``.

They are not interchangeable. ``put-events`` *mints* a root: ``chain_depth`` is
forced to 0 and lineage starts fresh. So a per-item ``put-events`` would make
every item its own root -- the chain-depth cap and the cross-persona loop guard
would never engage, the night's runs would not be linked in lineage, and the
"exactly one root dispatch" smoke check would fail while every unit test still
passed. That is why ``build_root_event`` and ``build_item_dispatch`` are separate
functions with separate call sites, and why the CLI exposes the root emit as its
own subcommand that refuses to run twice for a date.

There is no third path. **No ``@agent-`` mention and no ``agent-*`` label appears
anywhere in this module**, and the gate asserts their absence rather than trusting
it: a mention or a label dispatches at *write* time, so an item that carried one
would start its own run outside the sequencing that exists to stop two agents
doing the same work (#3626).


What must never be a dispatch target
------------------------------------

The plan issue and the night's dated parent are coordination artifacts -- an
index and a container. An agent dispatched onto one treats it as implementable
and opens a pull request against an index. ``validate_item_target`` rejects both
by number before any command is constructed, and rejects anything that is not on
the night's planned sequence at all.


The bound is a count, not a convention
--------------------------------------

Per-item runs are ``<= len(planned_sequence)``. #4559 §7.3 is explicit that the
envelope's ``dedup_key`` deduplicates nothing (it names a channel key; SQS keys on
arrival time, and the near-miss guards are skipped entirely for ``Service``-typed
senders), so platform dedup cannot be the fan-out bound. The bound is enforced
here, in code, and asserted as a number: unbounded fan-out from one night would
consume the platform's whole runner capacity.


Machine-rooted: App token only
------------------------------

The night runs ``is_human_rooted=false``, so every run in the chain gets
``authorized_user_id=""`` and **no vault or tenant credential** -- a deliberate
#3174 policy (#4559 §7.1), not a bug to flag-flip around. Everything this module
and the delivery loop need is a GitHub App installation-token operation: filing,
commenting, pushing a branch, opening and merging a pull request. Nothing here
reaches for a stored credential, and the gate asserts that too.


Idempotency lives here, in the CI job
-------------------------------------

Same place U9 puts "exactly one dated parent per date, safe on retry". The root
emit is guarded by a durable per-date marker, and per-item dispatch skips items
that already have an ``ops.<item>`` ledger shard. Both are re-run safe, because a
nightly that retries is normal.

The only clock read and the only subprocess calls live in the CLI, so every
decision below is testable without dispatching anything.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess  # nosec B404
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ensure_umbrella_epic  # noqa: E402
import ops_stuck_tracker  # noqa: E402
from security_agent_ledger import (  # noqa: E402
    LedgerError,
    load_and_merge,
    run_prefix,
)
from triage_group_findings import (  # noqa: E402
    BANNED_PATTERNS_PATH,
    banned_pattern_hits,
    load_banned_patterns,
)

# --------------------------------------------------------------------------
# the root dispatch (once per night)
# --------------------------------------------------------------------------

# The event's match fields. These two, and only these two, are what the
# EventBridge rule pattern keys on (#4559 §2.2), so they are constants rather
# than parameters: a caller that could vary them could miss the rule entirely
# and the night would report nothing, silently.
ROOT_EVENT_SOURCE = "adp.security-agent"
ROOT_EVENT_DETAIL_TYPE = "ADP Agent Dispatch"

# Deliberately NOT sent: persona, service_identity, and the target repo. All
# three are Terraform literals in the rule's InputTransformer, which is the
# primary control on this path (#4559 §5) -- `events:PutEvents` cannot be scoped
# to an event source, so the transformer is what stops a compromised runner
# choosing a persona, an identity, or a target repo. Adding any of them to the
# detail below, even "for clarity", deletes that control.
DEFAULT_ROOT_REASON = "nightly security remediation plan ready"

# The delivery persona each work item is dispatched to.
DELIVERY_PERSONA = "developer"

# Durable per-date marker for the root emit. Named so it is NOT matched by the
# ledger's `shard-*.json` glob: it is an idempotency record, not a ledger shard,
# and it must not join the merge the report reconciles against.
ROOT_MARKER_NAME = "root-dispatch.json"

_RUN_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_AGENT_MENTION_RE = re.compile(r"@agent-", re.IGNORECASE)
_DISPATCHING_LABEL_RE = re.compile(r"^agent-", re.IGNORECASE)

# Characters allowed in a value that is substituted into the EventBridge rule's
# InputTransformer template. EventBridge does NOT escape substituted values: it
# splices them in as raw text. So a value carrying JSON metacharacters can close
# the string it sits in and append a duplicate key -- and since JSON parsers take
# the last occurrence of a repeated key, an injected `"persona"` would override
# the Terraform literal that #4559 §5 calls the primary control on this path.
# `events:PutEvents` cannot be scoped to an event source, so that literal is what
# stops a compromised runner choosing its own persona, identity or target repo;
# defeating it is a privilege escalation, not a formatting bug.
#
# Quotes and braces are the injection primitives and are rejected. `:` is allowed
# because, with quotes and braces already excluded, a bare colon cannot form a
# JSON key -- and prose reasons legitimately read "stage 2: dispatch".
_EVENT_FIELD_ALLOWED_RE = re.compile(r"^[A-Za-z0-9 .,:_-]+$")

# Prose for each terminal reason. A closed map keyed by U2's reason enum: the
# reason is rendered into a comment on a permanently retained issue, so free text
# here is the path by which scanner detail reaches a document (NT-11 / NEV-2).
_REASON_PROSE = {
    "failed_run_limit": (
        "three delivery runs failed on it, which is the failure ceiling"
    ),
    "no_transition_timeout": (
        "it went twenty-four hours without a state change, which is the staleness "
        "ceiling"
    ),
}


class DispatchError(ValueError):
    """A dispatch would be unbounded, mis-targeted, or off the sanctioned path."""


# --------------------------------------------------------------------------
# seams -- the only places this module leaves the process
# --------------------------------------------------------------------------


def _gh(args: list[str]) -> tuple[int, str, str]:
    """Every GitHub call goes through U3's one seam, looked up at call time so a
    test that patches that single attribute intercepts all of them -- including
    asserting that a comment was never posted."""
    return ensure_umbrella_epic._gh(args)


def _aws(args: list[str]) -> tuple[int, str, str]:
    """The AWS CLI seam. Separate from ``_gh`` on purpose: this is the one place
    the root event leaves the runner, so a test can assert exactly one call."""
    proc = subprocess.run(  # nosec: B603, B607
        ["aws", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _adp_trigger(args: list[str]) -> tuple[int, str, str]:
    """The per-hop dispatch seam. ``adp-trigger`` and nothing else -- it reads
    lineage from the pod environment and SigV4-signs, so the spawned run stays
    linked under the night's one root."""
    proc = subprocess.run(  # nosec: B603, B607
        ["adp-trigger", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


# --------------------------------------------------------------------------
# the root event
# --------------------------------------------------------------------------


def build_root_event(
    *,
    run_date: str,
    issue_number: int,
    reason: str = DEFAULT_ROOT_REASON,
    patterns: list[dict] | None = None,
) -> dict:
    """Build the night's ONE ``put-events`` entry.

    ``issue_number`` is required and must name a real issue. #4559 §7.2: nothing
    on this path consumes ``target.create_issue``, and the agent worker runs
    ``gh issue view $ISSUE_NUMBER`` at startup -- so an empty number does not
    degrade, it kills the run before it does anything. The CI job therefore
    creates the plan issue first (U10) and passes its number here.

    ``detail`` is caller-controlled and lands in a DynamoDB row and CloudWatch
    logs, so it carries the three fields the transformer reads and nothing else,
    and ``reason`` is scanned for scanner detail before it is accepted (NEV-2).
    """
    if not _RUN_DATE_RE.match(run_date or ""):
        raise DispatchError(
            f"run date {run_date!r} is not YYYY-MM-DD; the night's identity is its "
            "date, and an unresolved one files the marker under the wrong key"
        )
    if isinstance(issue_number, bool) or not isinstance(issue_number, int):
        raise DispatchError(
            f"issue_number must be an integer, got {type(issue_number).__name__}"
        )
    if issue_number < 1:
        raise DispatchError(
            f"issue_number is {issue_number}; the EventBridge path attaches an agent "
            "to an EXISTING issue and cannot create one, and the worker dies on an "
            "empty issue number (#4559 §7.2). Create the plan issue first and pass "
            "its number"
        )
    # Not `lint_prose`: this value is substituted into the rule's InputTransformer
    # template unescaped, so it is held to the stricter allowlist that cannot
    # close a JSON string and inject a duplicate `persona`/`service_identity`.
    lint_event_field(reason, patterns, what="root dispatch reason")

    detail = {
        "reason": reason,
        "run_date": run_date,
        "issue_number": issue_number,
    }
    return {
        "Source": ROOT_EVENT_SOURCE,
        "DetailType": ROOT_EVENT_DETAIL_TYPE,
        "Detail": json.dumps(detail, sort_keys=True),
    }


def root_marker_key(run_date: str) -> str:
    """Where the per-date root marker lives, relative to the run's prefix."""
    return f"{run_prefix(run_date)}/{ROOT_MARKER_NAME}"


def build_root_marker(*, run_date: str, issue_number: int, emitted_at: str) -> dict:
    """The durable record that this night's root already fired."""
    return {
        "run_date": run_date,
        "issue_number": issue_number,
        "emitted_at": emitted_at,
        "source": ROOT_EVENT_SOURCE,
        "detail_type": ROOT_EVENT_DETAIL_TYPE,
    }


def decide_root_dispatch(
    *, run_date: str, issue_number: int, existing_marker: dict | None
) -> dict:
    """Whether to emit the night's root event. Pure.

    Absent marker -> emit. Present marker -> skip: the night already has its one
    root, and a second emit would mint a second chain at ``chain_depth=0``,
    breaking the single-root lineage assertion (and doubling the fan-out, since
    the envelope's ``dedup_key`` deduplicates nothing -- #4559 §7.3).

    A marker naming a *different* issue means two plans exist for one night. That
    is raised rather than reconciled: whichever one this call picked, the other
    night's items would be driven by nobody.
    """
    if existing_marker is None:
        return {"emit": True, "reason": "no root dispatch recorded for this date"}
    recorded = existing_marker.get("issue_number")
    if recorded != issue_number:
        raise DispatchError(
            f"{run_date} already dispatched its root against issue #{recorded}, but "
            f"this call names #{issue_number}; two plans for one night means the "
            "delivery role drives one of them and the other is never picked up"
        )
    return {"emit": False, "reason": f"root already dispatched for {run_date}"}


def emit_root_event(entry: dict, runner=None) -> dict:
    """Emit one entry via ``aws events put-events``, and check the result.

    ``put-events`` returns HTTP 200 with a per-entry error list, so a rejected
    entry looks exactly like a success to anything checking only the exit code --
    and the night would then wait forever for a run that was never started. The
    ``FailedEntryCount`` check below is what makes the failure loud.
    """
    runner = runner or _aws
    rc, out, err = runner(
        ["events", "put-events", "--entries", json.dumps([entry]), "--output", "json"]
    )
    if rc != 0:
        raise DispatchError(f"put-events failed: {err.strip() or out.strip()}")
    try:
        response = json.loads(out or "{}")
    except json.JSONDecodeError as exc:
        raise DispatchError(f"unparseable put-events response: {exc}") from exc
    if response.get("FailedEntryCount", 0):
        raise DispatchError(
            f"put-events accepted the call but rejected the entry: "
            f"{response.get('Entries')}"
        )
    return response


# --------------------------------------------------------------------------
# the night's plan, as the ledger reports it
# --------------------------------------------------------------------------


def load_plan(ledger_dir: Path | str) -> dict:
    """Read the night's ordering from U10's orchestration shard.

    The plan's order is taken from the ledger rather than re-derived, so the
    delivery order is the one the barrier committed to. Re-deriving it here would
    be a second ordering that can disagree with the plan the morning reads.
    """
    merged = load_and_merge(ledger_dir)
    fields = merged["fields"]
    sequence = fields.get("planned_sequence")
    if not sequence:
        raise DispatchError(
            "the ledger records no planned_sequence for this run; U10's barrier "
            "writes it, and without an order there is nothing to drive"
        )
    return {
        "run_date": merged["run_date"],
        "planned_sequence": list(sequence),
        "daily_epic": fields.get("daily_epic"),
    }


def already_dispatched(ledger_dir: Path | str) -> list[int]:
    """Work items that already have an ``ops.<item>`` shard.

    The shard is written when an item is dispatched, so its presence is the
    durable "already started" record -- which is what makes a retried operations
    run adopt the night in progress instead of dispatching every item a second
    time.
    """
    numbers: list[int] = []
    for path in sorted(Path(ledger_dir).glob("shard-ops.*.json")):
        suffix = path.name[len("shard-ops.") : -len(".json")]
        if suffix.isdigit():
            numbers.append(int(suffix))
    return sorted(numbers)


# --------------------------------------------------------------------------
# per-item dispatch
# --------------------------------------------------------------------------


def validate_item_target(
    number: int,
    *,
    planned_sequence: list[int],
    plan_issue: int,
    daily_epic: int | None = None,
) -> None:
    """Reject any target that is not one of the night's work items.

    Three rejections, each a real failure mode rather than a type check:

    * **the plan issue** -- an index. An agent dispatched onto it opens a pull
      request against a coordination artifact.
    * **the dated parent** -- a container, for the same reason.
    * **anything off the planned sequence** -- an item this night did not create.
      This is also where the fan-out bound comes from: dispatch is closed over
      the plan, so it cannot exceed it.
    """
    if isinstance(number, bool) or not isinstance(number, int):
        raise DispatchError(
            f"dispatch target must be an integer issue number, got {number!r}"
        )
    if number == plan_issue:
        raise DispatchError(
            f"refusing to dispatch on #{number}: that is the night's plan, a "
            "coordination artifact and not a unit of work. An agent pointed at it "
            "would open a pull request against an index"
        )
    if daily_epic is not None and number == daily_epic:
        raise DispatchError(
            f"refusing to dispatch on #{number}: that is the night's dated parent, "
            "a container for the work items and not one of them"
        )
    if number not in planned_sequence:
        raise DispatchError(
            f"refusing to dispatch on #{number}: it is not one of this night's "
            f"work items ({planned_sequence}). Per-item dispatch is bounded by the "
            "night's plan"
        )


def build_item_dispatch(
    number: int,
    *,
    repo: str,
    reason: str,
    persona: str = DELIVERY_PERSONA,
    patterns: list[dict] | None = None,
) -> list[str]:
    """The argv for one per-item hop. ``adp-trigger``, and nothing else.

    This is the ONLY dispatch construction for an in-night hop. It is not
    ``put-events`` (that would mint a second root -- see the module docstring),
    it is not a comment, and it is not a label.
    """
    lint_prose(reason, patterns, what="dispatch reason")
    return [
        "--persona",
        persona,
        "--issue",
        str(number),
        "--repo",
        repo,
        "--reason",
        reason,
    ]


def bounded_dispatch_plan(
    *,
    planned_sequence: list[int],
    plan_issue: int,
    daily_epic: int | None,
    repo: str,
    reason: str,
    dispatched: list[int] | None = None,
    patterns: list[dict] | None = None,
) -> dict:
    """The per-item dispatches to issue, in the plan's order, bounded by it.

    Returns the argv list per item plus the two numbers the bound is asserted
    from, so the caller checks a value rather than trusting a loop.
    """
    seen = set(dispatched or ())
    pending: list[dict] = []
    for number in planned_sequence:
        validate_item_target(
            number,
            planned_sequence=planned_sequence,
            plan_issue=plan_issue,
            daily_epic=daily_epic,
        )
        if number in seen:
            continue
        seen.add(number)
        pending.append(
            {
                "number": number,
                "argv": build_item_dispatch(
                    number, repo=repo, reason=reason, patterns=patterns
                ),
            }
        )
    plan = {
        "dispatches": pending,
        "bound": len(set(planned_sequence)),
        "count": len(pending) + len(set(dispatched or ()) & set(planned_sequence)),
    }
    assert_within_bound(plan)
    return plan


def assert_within_bound(plan: dict) -> None:
    """Per-item runs must be ``<=`` the items the night created.

    Asserted, because this bound is the only thing between a findings night and a
    platform-wide capacity incident: one run per item is fine, one run per item
    per retry is not.
    """
    if plan["count"] > plan["bound"]:
        raise DispatchError(
            f"per-item dispatch count {plan['count']} exceeds the night's "
            f"{plan['bound']} work item(s); the fan-out bound is the night's plan"
        )


def dispatch_items(plan: dict, runner=None) -> list[dict]:
    """Issue the planned per-item hops, recording the outcome of each."""
    runner = runner or _adp_trigger
    results: list[dict] = []
    for item in plan["dispatches"]:
        rc, _, err = runner(item["argv"])
        results.append(
            {
                "number": item["number"],
                "dispatched": rc == 0,
                "error": err.strip() if rc != 0 else None,
            }
        )
    return results


# --------------------------------------------------------------------------
# status updates on the plan
# --------------------------------------------------------------------------


def lint_prose(text: str, patterns: list[dict] | None = None, *, what: str) -> None:
    """Assert a string is safe to put in a comment, an event, or a log.

    Two independent checks. An ``@agent-`` token would make the status update
    itself a dispatch -- every state change would start a run, which is the
    fan-out this unit is bounded against. A banned-pattern hit would put
    reproduction detail in a permanently retained artifact (NEV-2).
    """
    if not isinstance(text, str) or not text.strip():
        raise DispatchError(f"{what} must be a non-empty string")
    if _AGENT_MENTION_RE.search(text):
        raise DispatchError(
            f"{what} contains an `@agent-` mention, which would itself dispatch an "
            "agent; refer to a role in prose without the mention prefix"
        )
    hits = banned_pattern_hits(text, patterns)
    if hits:
        raise DispatchError(
            f"{what} matches banned pattern(s) {hits}: scanner detail belongs in the "
            "private run ledger, never in a retained document"
        )


def lint_event_field(
    text: str, patterns: list[dict] | None = None, *, what: str
) -> None:
    """Assert a string is safe to substitute into the EventBridge input template.

    Strictly narrower than ``lint_prose``, and deliberately a separate function
    rather than a widening of it. The two guard different things:

    * ``lint_prose`` guards *documents* -- comment bodies, which legitimately
      contain ``#`` for issue references and parentheses for asides.
    * this guards *template substitution*, where any quote or brace is an
      injection primitive.

    Folding the allowlist into ``lint_prose`` would reject every status comment
    this module renders (they all carry ``#<number>``), so the checks are layered
    instead: the prose checks run first, then the allowlist.
    """
    lint_prose(text, patterns, what=what)
    if not _EVENT_FIELD_ALLOWED_RE.match(text):
        rejected = sorted(set(re.sub(r"[A-Za-z0-9 .,:_-]", "", text)))
        raise DispatchError(
            f"{what} contains character(s) {rejected} that are not allowed in an "
            "EventBridge event field. Substituted values are spliced into the rule's "
            "input template unescaped, so a quote or brace can append a duplicate "
            "`persona` or `service_identity` key that overrides the Terraform "
            "literal pinning them (#4559 §5). Use letters, digits, spaces and "
            ". , : _ - only"
        )


def render_status_comment(
    *,
    number: int,
    status: str,
    reason: str | None = None,
    patterns: list[dict] | None = None,
) -> str:
    """One prose status line for one item state change.

    Prose only, and rendered rather than authored so the properties hold by
    construction: no mention token (which would dispatch), no table, no finding
    detail. Roles are named in prose -- "the delivery role", never
    "at-agent-developer".
    """
    if status == "in_progress":
        body = (
            f"Work item #{number} is in progress. The delivery role has picked it "
            "up and will take it to a pull request."
        )
    elif status == "fixed":
        body = (
            f"Work item #{number} is fixed. Its pull request merged and its checks "
            "passed."
        )
    elif status == "stuck":
        if reason not in _REASON_PROSE:
            raise DispatchError(
                f"work item #{number} is stuck but records reason {reason!r}; a stuck "
                "item without a recorded reason leaves nobody able to tell a flaky "
                "check from an impossible ask"
            )
        body = (
            f"Work item #{number} is stuck: {_REASON_PROSE[reason]}. It will not be "
            "driven further tonight and needs a human to look at it."
        )
    else:
        raise DispatchError(
            f"unknown status {status!r} for work item #{number}; the states are "
            "in_progress, fixed and stuck"
        )
    lint_prose(body, patterns, what="status comment")
    return body


def post_status_comment(
    repo: str, plan_issue: int, body: str, patterns: list[dict] | None = None
) -> None:
    """Comment on the plan issue. Linted again here, on the exact bytes posted."""
    lint_prose(body, patterns, what="status comment")
    rc, _, err = _gh(
        ["issue", "comment", str(plan_issue), "--repo", repo, "--body", body]
    )
    if rc != 0:
        raise DispatchError(
            f"failed to comment on the plan #{plan_issue}: {err.strip()}"
        )


def dispatching_labels(labels: list[str]) -> list[str]:
    """Labels that would dispatch an agent at write time.

    Exposed so the gate can assert this module applies none: label-based dispatch
    is one of the two prohibited paths, and the assertion is structural rather
    than a promise.
    """
    return [lab for lab in labels if _DISPATCHING_LABEL_RE.match(lab)]


# --------------------------------------------------------------------------
# CLI -- the only clock read and the only process launches
# --------------------------------------------------------------------------


def _utc_now_iso() -> str:
    """Clock read. CLI-only, by design -- every decision above takes ``now``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_marker(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DispatchError(f"cannot read the root marker {path}: {exc}") from exc


def _cmd_root_dispatch(args: argparse.Namespace) -> int:
    """Emit the night's ONE root event, or report that it already fired."""
    patterns = load_banned_patterns(args.banned_patterns)
    marker_path = Path(args.marker_dir) / ROOT_MARKER_NAME
    decision = decide_root_dispatch(
        run_date=args.run_date,
        issue_number=args.issue_number,
        existing_marker=_read_marker(marker_path),
    )
    if not decision["emit"]:
        print(f"root_dispatched=false reason={decision['reason']}")
        return 0

    entry = build_root_event(
        run_date=args.run_date,
        issue_number=args.issue_number,
        reason=args.reason,
        patterns=patterns,
    )
    emit_root_event(entry)
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        json.dumps(
            build_root_marker(
                run_date=args.run_date,
                issue_number=args.issue_number,
                emitted_at=_utc_now_iso(),
            ),
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"root_dispatched=true issue={args.issue_number} marker={marker_path}")
    return 0


def _cmd_dispatch_items(args: argparse.Namespace) -> int:
    """Dispatch the night's items, bounded by the plan, one hop each."""
    patterns = load_banned_patterns(args.banned_patterns)
    plan_doc = load_plan(args.ledger_dir)
    plan = bounded_dispatch_plan(
        planned_sequence=plan_doc["planned_sequence"],
        plan_issue=args.plan_issue,
        daily_epic=plan_doc["daily_epic"],
        repo=args.repo,
        reason=args.reason,
        dispatched=already_dispatched(args.ledger_dir),
        patterns=patterns,
    )
    results = dispatch_items(plan)

    out_dir = Path(args.ledger_dir)
    failed = [r["number"] for r in results if not r["dispatched"]]
    for result in results:
        if not result["dispatched"]:
            continue
        # The ops shard IS the "started" record the next run reads, so it is
        # written on dispatch rather than at first progress: a crash between the
        # two would otherwise re-dispatch the item.
        ops_stuck_tracker.write_ops_shard(
            out_dir,
            item=result["number"],
            record=ops_stuck_tracker.initial_record(_utc_now_iso()),
            run_date=plan_doc["run_date"],
            generated_at=_utc_now_iso(),
        )
        post_status_comment(
            args.repo,
            args.plan_issue,
            render_status_comment(
                number=result["number"], status="in_progress", patterns=patterns
            ),
            patterns,
        )
    # Counts and issue numbers only: a CI log is readable by anyone who can see
    # the run.
    print(
        f"per_item_dispatches={len(results)} bound={plan['bound']} "
        f"failed={len(failed)}"
    )
    if failed:
        print(
            f"::error title=Security ops dispatch::failed to dispatch work item(s) "
            f"{failed}",
            file=sys.stderr,
        )
        return 1
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    """Post one prose status update for one item state change."""
    patterns = load_banned_patterns(args.banned_patterns)
    body = render_status_comment(
        number=args.item, status=args.status, reason=args.reason, patterns=patterns
    )
    post_status_comment(args.repo, args.plan_issue, body, patterns)
    print(f"status_posted item={args.item} status={args.status}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hand the night's plan to the delivery role and drive its "
        "work items (intent #4290, unit U11)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    root = sub.add_parser(
        "root-dispatch",
        help="emit the night's ONE root event (aws events put-events)",
    )
    root.add_argument("--run-date", required=True, help="YYYY-MM-DD")
    root.add_argument(
        "--issue-number",
        type=int,
        required=True,
        help="the plan issue, which must already exist (#4559 §7.2)",
    )
    root.add_argument("--reason", default=DEFAULT_ROOT_REASON)
    root.add_argument(
        "--marker-dir",
        required=True,
        help="directory holding the per-date root-dispatch marker",
    )
    root.set_defaults(func=_cmd_root_dispatch)

    items = sub.add_parser(
        "dispatch-items", help="dispatch each work item via adp-trigger"
    )
    items.add_argument("--repo", required=True, help="Repository (owner/name)")
    items.add_argument("--plan-issue", type=int, required=True)
    items.add_argument(
        "--ledger-dir", required=True, help="directory holding the run's shards"
    )
    items.add_argument("--reason", default="nightly security remediation")
    items.set_defaults(func=_cmd_dispatch_items)

    status = sub.add_parser("status", help="post one prose status update")
    status.add_argument("--repo", required=True)
    status.add_argument("--plan-issue", type=int, required=True)
    status.add_argument("--item", type=int, required=True)
    status.add_argument(
        "--status", required=True, choices=["in_progress", "fixed", "stuck"]
    )
    status.add_argument("--reason", choices=sorted(_REASON_PROSE))
    status.set_defaults(func=_cmd_status)

    for command in (root, items, status):
        command.add_argument(
            "--banned-patterns",
            default=str(BANNED_PATTERNS_PATH),
            help="banned-pattern list (defaults to U9's committed one)",
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (DispatchError, LedgerError) as exc:
        print(f"::error title=Security ops dispatch::{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
