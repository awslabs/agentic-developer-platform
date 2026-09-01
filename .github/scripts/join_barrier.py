#!/usr/bin/env python3
"""Join the night's two grouping passes and write one plan (intent #4290, U10).

Two grouping passes run concurrently every night, one per scanner. Someone has
to say what the night's work is and in what order it should be tackled. This
module is that someone: it waits for both passes to finish, then writes exactly
one lean orchestration issue naming the night's work items in dependency order.

Three properties, each one load-bearing:


The barrier reads a durable marker, never inferred state
--------------------------------------------------------

Each grouping pass writes its own **triage shard** when it finishes filing. That
shard is the completion marker, and the shard's shape is U2's schema -- not a
second one invented here (``validate_shard`` below is U2's, called on every
marker before it is trusted).

The three cheaper signals are all wrong, and wrong in the same direction:

* *work items exist* -- true partway through filing. U9 files work items one at
  a time, so the first ``issue create`` makes this true while the rest of the
  night's items do not exist yet.
* *a comment, or a run status* -- observable before the pass's last write lands.

Each would let the plan be written from a half-filed pass, and the resulting
plan is indistinguishable from a complete one. The marker is written *after*
filing, so its presence means filing finished. Concurrency safety is U2's: the
two halves write ``triage.code-review`` and ``triage.pentest``, distinct stage
ids, therefore distinct keys, therefore no lost update.

**Present-and-empty is not the same as absent.** This is the whole point of
joining on markers, so it is spelled out rather than left to be re-derived:

* marker present, ``story_ids`` empty -- the scanner ran and found nothing. It
  SIGNALLED. It counts toward the join and contributes zero work items.
* marker absent -- the scanner never signalled. Only this can drive PARTIAL or
  a stall.

A quiet night is the common night (FR-C2), so collapsing these two is not an
edge case: it would hard-fail CI every quiet night, and on a half-quiet night
ship a plan stamped PARTIAL blaming a scanner that was working perfectly. U9's
grouping pass therefore writes its marker on BOTH paths, and the empty marker is
load-bearing rather than noise. Do not "simplify" the check below into a test for
non-empty ``story_ids``, or for the existence of work items: either restores
exactly that bug, and both still pass every positive test.


A timeout that ships a visibly-partial plan
-------------------------------------------

A hung pass must not stall the night in silence: a morning with no plan and no
failure is the one outcome nobody investigates. On expiry the plan ships with
the work items that do exist, stamped **PARTIAL**, naming the scanner that never
signalled -- in the body and in the ledger. A visibly-half plan is recoverable.

The decision is a pure function of (which markers arrived, elapsed, timeout).
The polling loop and the only clock read live in the CLI, the same split U2 uses
for its stuck rule, so every branch is testable without sleeping.


Exactly one plan per night
--------------------------

Keyed on the date, matched by exact title before any create is issued -- the
discipline U3 established and U9 reuses. Firing the barrier twice for one date
yields one orchestration issue: a retry adopts what the first attempt left
behind. Two plans for one night means the delivery stage drives both and every
fix is done twice.


What this module does NOT do
----------------------------

It does not dispatch. No ``adp-trigger``, no ``@agent-`` mention, no ``agent-*``
label -- so no run starts at creation and no ``agent/issue-N`` work branch is
ever cut for the plan's number. The plan is a coordination artifact, not a unit
of work: an agent that treated it as implementable would open a pull request
against an index. Dispatch is U11's, off this one issue.

The body is link-only and size-bounded for the same reason. It points at the
work items; it does not restate them. A large body kills the reading agent
early, and a body that restates findings quietly replaces the work items as the
source of truth.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ensure_umbrella_epic  # noqa: E402
from ensure_umbrella_epic import find_issue_by_exact_title, link_sub_issue  # noqa: E402
from security_agent_ledger import (  # noqa: E402
    LedgerError,
    build_shard,
    run_prefix,
    validate_shard,
)
from triage_group_findings import (  # noqa: E402
    BANNED_PATTERNS_PATH,
    SOURCES,
    banned_pattern_hits,
    load_banned_patterns,
)

# The plan's title. Dated, matched by exact equality: the date is the
# idempotency key, so "one plan per night" is expressible as a title lookup
# rather than as a stored id that a retry starts without.
PLAN_TITLE_TEMPLATE = "Security remediation plan — {run_date}"

# `epic` and nothing else. NOT `story` (which marks an implementable item) and
# emphatically nothing in the dispatching `agent-*` namespace: this issue is an
# index, and a label that dispatches would start a run on it at creation.
PLAN_LABEL = "epic"

# The stage id this unit writes its own shard under. Bare `orchestration`: there
# is exactly one barrier per night, so unlike the two grouping halves it needs
# no dotted suffix to stay off another writer's key.
ORCHESTRATION_STAGE = "orchestration"

# Marker stage ids: `triage.<source>`, one per scanner.
MARKER_STAGE_TEMPLATE = "triage.{source}"
MARKER_STAGE_RE = re.compile(r"^triage\.(?P<source>[a-z0-9][a-z0-9-]*)$")

# --------------------------------------------------------------------------
# Ordering.
#
# The plan must SEQUENCE the night's work, not merely list it: a delivery stage
# handed an arbitrary order works foundational items last.
#
# Two keys, in this order:
#
#   1. Scanner precedence, as declared below. Code-review findings are
#      surface-level and typically the substrate a pentest finding sits on top
#      of, so its items land first. Declared as a constant rather than taken
#      from dict or filesystem order, because the latter two are not an
#      ordering -- they are whatever the run happened to produce.
#   2. Ascending work-item number within a scanner. U9 files a pass's items in
#      the architect's plan order, so issue numbers ascend in that order:
#      sorting by number RECOVERS the architect's intended sequence rather than
#      imposing a new one. It is also stable across a retry, since an adopted
#      item keeps its original number.
# --------------------------------------------------------------------------
SOURCE_PRECEDENCE = ("code-review", "pentest")

# Barrier states.
STATE_WAITING = "waiting"  # not all markers in, time remains
STATE_READY = "ready"  # every scanner signalled
STATE_PARTIAL = "partial"  # timed out, some work items exist -> ship, stamped
STATE_STALLED = "stalled"  # timed out with NO marker at all -> hard failure

# Default wait. Generous relative to a grouping pass, which files a handful of
# issues once its architect step is done; the ceiling is here so a hung pass
# cannot stall the night forever, not to race a healthy one.
DEFAULT_TIMEOUT_SECONDS = 1800
DEFAULT_POLL_SECONDS = 30

# Hard body ceiling, in BYTES not characters -- the reading agent's context is
# consumed by encoded bytes, and the em dashes and arrows below are multi-byte.
MAX_BODY_BYTES = 3072

# Rows rendered in the table before it truncates. The calibrated band yields
# 4..6 items per scanner, so a normal night lists everything; the cap exists so
# a pathological night degrades VISIBLY (an explicit "+N more" line, and the
# full list is one click away on the dated EPIC) instead of blowing the byte
# ceiling and failing to produce a plan at all.
MAX_LISTED_ITEMS = 40

_AGENT_MENTION_RE = re.compile(r"@agent-", re.IGNORECASE)
_DISPATCHING_LABEL_RE = re.compile(r"^agent-", re.IGNORECASE)


class BarrierError(ValueError):
    """The barrier cannot produce exactly one well-formed plan."""


# --------------------------------------------------------------------------
# the gh seam
# --------------------------------------------------------------------------


def _gh(args: list[str]) -> tuple[int, str, str]:
    """Every GitHub call goes through U3's one seam, looked up at call time so a
    test that patches that single attribute intercepts the whole flow --
    including asserting that a create was never issued."""
    return ensure_umbrella_epic._gh(args)


# --------------------------------------------------------------------------
# markers
# --------------------------------------------------------------------------


def marker_stage(source: str) -> str:
    """The stage id a grouping pass signals completion under."""
    if source not in SOURCES:
        raise BarrierError(f"source {source!r} is not one of {list(SOURCES)}")
    return MARKER_STAGE_TEMPLATE.format(source=source)


def load_markers(ledger_dir: Path | str, schema: dict | None = None) -> dict:
    """Load every completion marker in a run directory, keyed by scanner.

    Each marker is validated through U2's ``validate_shard`` before it is
    trusted. A marker that does not satisfy U2's schema is an ERROR, never a
    marker that is quietly skipped: skipping it is indistinguishable from the
    pass never having signalled, which is precisely the confusion this barrier
    exists to remove.
    """
    directory = Path(ledger_dir)
    markers: dict[str, dict] = {}
    for path in sorted(directory.glob("shard-triage*.json")):
        try:
            shard = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BarrierError(f"cannot read the marker {path}: {exc}") from exc
        try:
            validate_shard(shard, schema)
        except LedgerError as exc:
            raise BarrierError(f"{path} is not a valid ledger shard: {exc}") from exc

        match = MARKER_STAGE_RE.match(shard["stage"])
        if match is None:
            # A bare `triage` marker cannot be attributed to a scanner, so it
            # cannot satisfy a per-scanner join -- and two passes sharing it
            # would overwrite each other's key anyway (U2's stage id IS the
            # concurrency boundary). Named as a wiring bug rather than ignored.
            raise BarrierError(
                f"{path} has stage {shard['stage']!r}; each grouping pass must "
                f"signal under its own {MARKER_STAGE_TEMPLATE.format(source='<source>')} "
                f"stage (one of {[marker_stage(s) for s in SOURCES]}), otherwise the "
                "barrier cannot tell which scanner finished"
            )
        source = match.group("source")
        if source not in SOURCES:
            raise BarrierError(
                f"{path} signals for unknown scanner {source!r}; expected one of "
                f"{list(SOURCES)}"
            )
        if source in markers:  # pragma: no cover - one key per stage id
            raise BarrierError(f"two markers for scanner {source!r}")
        markers[source] = shard
    return markers


def evaluate_barrier(
    markers: dict,
    *,
    elapsed_seconds: float,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    sources: tuple[str, ...] = SOURCES,
) -> dict:
    """Decide whether to wait, ship, or fail. Pure -- reads no clock.

    ``elapsed_seconds`` is a parameter, which is what makes every branch here
    (including the timeout) testable without a sleeping test.

    Membership is the ONLY test applied to a marker: a pass that ran and found
    nothing signals with an empty ``story_ids`` and must count as signalled here.
    Inspecting the marker's contents to decide whether it "really" counts is the
    bug described in the module docstring -- it makes a healthy quiet scanner
    look absent, which is a false PARTIAL on a half-quiet night and a hard
    failure on a fully quiet one.
    """
    signalled = sorted(s for s in sources if s in markers)
    unsignalled = sorted(s for s in sources if s not in markers)

    if not unsignalled:
        state = STATE_READY
    elif elapsed_seconds < timeout_seconds:
        state = STATE_WAITING
    elif signalled:
        state = STATE_PARTIAL
    else:
        # Neither pass signalled inside the window. There is no work to
        # sequence, and unlike a quiet night this is not a normal outcome: it
        # is a failure that must be visible rather than a silent no-op.
        state = STATE_STALLED

    return {
        "state": state,
        "signalled": signalled,
        "unsignalled": unsignalled,
        "partial": state == STATE_PARTIAL,
    }


# --------------------------------------------------------------------------
# the night's work items
# --------------------------------------------------------------------------


def _source_rank(source: str) -> tuple[int, str]:
    try:
        return (SOURCE_PRECEDENCE.index(source), source)
    except ValueError:  # a scanner added without declaring precedence
        return (len(SOURCE_PRECEDENCE), source)


def collect_work_items(markers: dict) -> list[dict]:
    """The night's work items, in dependency order (see SOURCE_PRECEDENCE).

    Read from the markers' own ``story_ids`` -- the same triage-owned U2 field
    U9 already writes -- so the barrier needs no second input channel that
    could disagree with the ledger.
    """
    items: list[dict] = []
    seen: set[int] = set()
    for source in sorted(markers, key=_source_rank):
        story_ids = markers[source]["fields"].get("story_ids", [])
        for number in sorted(story_ids):
            if number in seen:
                # The same item claimed by both scanners. Keep the first
                # scanner's position: an item listed twice would be driven
                # twice, which is the duplicate-work failure in miniature.
                continue
            seen.add(number)
            items.append({"number": number, "source": source})
    return items


def planned_sequence(work_items: list[dict]) -> list[int]:
    """Work-item numbers in dependency order -- the ledger's view of the plan."""
    return [item["number"] for item in work_items]


def daily_epic(markers: dict) -> int | None:
    """The night's dated EPIC, as the markers report it.

    ``daily_epic`` merges ``single`` in U2's schema: both passes file under one
    dated parent, so two different values is a bug rather than something to
    reconcile. Disagreement is raised here for the same reason U2 raises it --
    a silently-resolved conflict produces a plan that is authoritative and
    wrong.
    """
    values = {
        m["fields"]["daily_epic"] for m in markers.values() if "daily_epic" in m["fields"]
    }
    if not values:
        return None
    if len(values) > 1:
        raise BarrierError(
            f"markers disagree on the night's dated EPIC: {sorted(values)}; both "
            "grouping passes must file under one dated parent"
        )
    return values.pop()


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def _table(work_items: list[dict]) -> str:
    """The ordering table: position, item, scanner, and what it waits on.

    The `Depends on` column is what makes this a SEQUENCE rather than a list --
    it names the predecessor explicitly, so the delivery stage does not have to
    infer intent from row order.
    """
    rows = []
    listed = work_items[:MAX_LISTED_ITEMS]
    for index, item in enumerate(listed):
        depends = f"#{listed[index - 1]['number']}" if index else "—"
        rows.append(f"| {index + 1} | #{item['number']} | {item['source']} | {depends} |")
    if len(work_items) > len(listed):
        rows.append(
            f"| … | _+{len(work_items) - len(listed)} more, in order, on the dated "
            "EPIC_ | | |"
        )
    return "\n".join(rows)


def render_body(
    *,
    run_date: str,
    work_items: list[dict],
    epic: int | None,
    ledger_uri: str,
    unsignalled: list[str],
) -> str:
    """Render the plan. Link-only, ordered, and size-bounded by construction.

    Deliberately absent: finding ids, titles, severities, fix surfaces, any
    scanner detail. The work items hold that, and the ledger pointer below is
    how the delivery agent reaches the rest. Restating it here would make this
    index the de-facto source of truth while the items it points at drift.
    """
    header = (
        f"**PARTIAL PLAN — {', '.join(unsignalled)} never signalled.** The items below "
        "are the night's work as far as it is known; that scanner's findings, if any, "
        "are missing. Investigate its grouping pass before treating this as complete.\n\n"
        if unsignalled
        else ""
    )
    epic_line = f"Dated EPIC: #{epic}\n" if epic else ""
    return f"""{header}Delivery plan for the {run_date} nightly security run: {len(work_items)} \
work item(s), in the order they should be tackled.

{epic_line}Ledger: `{ledger_uri}`

| Order | Work item | Scanner | Depends on |
|---|---|---|---|
{_table(work_items)}

Each row links the work item that holds the detail; this issue deliberately \
restates none of it. Written by `.github/scripts/join_barrier.py` once per \
night, after both grouping passes signalled completion.

This is a coordination artifact, not a unit of work: it takes no work branch \
and no pull request. Nothing is dispatched at creation.
"""


def lint_body(body: str, patterns: list[dict] | None = None) -> None:
    """Assert the plan is fileable. Runs on the body that will actually be
    filed, before anything is created, because the body is what becomes
    permanent."""
    size = len(body.encode("utf-8"))
    if size > MAX_BODY_BYTES:
        raise BarrierError(
            f"plan body is {size} bytes, over the {MAX_BODY_BYTES}-byte ceiling; "
            "the plan is an index and a body this size stops being one"
        )
    if _AGENT_MENTION_RE.search(body):
        raise BarrierError(
            "plan body contains an `@agent-` mention, which would dispatch an agent "
            "at creation and bypass the delivery stage"
        )
    hits = banned_pattern_hits(body, patterns)
    if hits:
        raise BarrierError(
            f"plan body matches banned pattern(s) {hits}: scanner detail belongs in "
            "the private run ledger, never in a retained document"
        )


def plan_labels() -> list[str]:
    """The labels the plan carries. A closed list of exactly one.

    Closed for the reason U9's is: the failure mode is not a wrong label but a
    label that dispatches, and the assert makes that structural.
    """
    labels = [PLAN_LABEL]
    dispatching = [lab for lab in labels if _DISPATCHING_LABEL_RE.match(lab)]
    if dispatching:  # pragma: no cover - unreachable while the list is closed
        raise BarrierError(f"labels {dispatching} would dispatch an agent at creation")
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
        raise BarrierError(f"failed to create issue {title!r}: {err.strip()}")
    match = re.search(r"/issues/(\d+)", out.strip())
    if not match:
        raise BarrierError(f"could not parse issue number from: {out.strip()!r}")
    return int(match.group(1))


def ensure_plan_issue(
    repo: str,
    *,
    run_date: str,
    body: str,
    epic: int | None,
    patterns: list[dict] | None = None,
) -> dict:
    """Ensure exactly one plan issue exists for ``run_date``.

    Linted first, then matched by exact title, then created. Linting before the
    lookup is deliberate: a body that must not be filed must not be filed on
    the retry either, and an early return on "already exists" would skip the
    check.
    """
    lint_body(body, patterns)
    title = PLAN_TITLE_TEMPLATE.format(run_date=run_date)
    if _AGENT_MENTION_RE.search(title):  # pragma: no cover - template is a constant
        raise BarrierError(f"plan title {title!r} contains an `@agent-` mention")

    existing = find_issue_by_exact_title(repo, title)
    if existing is not None:
        print(f"plan for {run_date} already exists: #{existing} — no create issued")
        return {
            "number": existing,
            "created": False,
            "linked": link_sub_issue(repo, epic, existing) if epic else False,
        }
    number = _create_issue(repo, title, body, plan_labels())
    print(f"created plan #{number} for {run_date}")
    return {
        "number": number,
        "created": True,
        "linked": link_sub_issue(repo, epic, number) if epic else False,
    }


def ledger_fields(work_items: list[dict], unsignalled: list[str]) -> dict:
    """This stage's ledger fields. Only fields ``orchestration`` owns in U2.

    ``unsignalled_sources`` is why the timeout is recoverable: the gap is
    recorded as data, so a reader of the ledger -- not only a reader of the
    issue body -- can tell a partial plan from a complete one.
    """
    fields: dict = {"planned_sequence": planned_sequence(work_items)}
    if unsignalled:
        fields["unsignalled_sources"] = sorted(unsignalled)
    return fields


def run_barrier(
    repo: str,
    *,
    markers: dict,
    run_date: str,
    ledger_uri: str,
    state: dict,
    patterns: list[dict] | None = None,
) -> dict:
    """Write the night's one plan from an already-decided barrier state.

    Returns a result document. Two paths file nothing:

    * ``stalled`` -- no scanner signalled. Raises, because a night with no
      marker at all is a failure to investigate, not a quiet night. The two are
      distinguishable precisely because a quiet scanner still leaves a marker.
    * no work items -- NT-5. Every pass signalled and had nothing to file, so
      there is nothing to plan: no issue, no shard, and no empty plan whose
      only content is that it is empty. This is the common night, and it exits
      cleanly rather than as a failure.
    """
    if state["state"] == STATE_WAITING:  # pragma: no cover - CLI never ships waiting
        raise BarrierError("barrier is still waiting; refusing to write a plan")
    if state["state"] == STATE_STALLED:
        raise BarrierError(
            f"no grouping pass signalled completion for {run_date} within the "
            f"timeout ({state['unsignalled']}); there is nothing to plan and this "
            "needs investigating rather than a silent skip"
        )

    work_items = collect_work_items(markers)
    if not work_items:
        print(
            f"no work items for {run_date} — filing no plan. This is expected on a "
            "night with no new findings."
        )
        return {
            "nothing_to_plan": True,
            "run_date": run_date,
            "partial": state["partial"],
            "unsignalled": state["unsignalled"],
        }

    epic = daily_epic(markers)
    body = render_body(
        run_date=run_date,
        work_items=work_items,
        epic=epic,
        ledger_uri=ledger_uri,
        unsignalled=state["unsignalled"],
    )
    plan = ensure_plan_issue(
        repo, run_date=run_date, body=body, epic=epic, patterns=patterns
    )
    return {
        "nothing_to_plan": False,
        "run_date": run_date,
        "partial": state["partial"],
        "unsignalled": state["unsignalled"],
        "daily_epic": epic,
        "plan_issue": plan["number"],
        "plan_created": plan["created"],
        "work_items": work_items,
        "ledger_fields": ledger_fields(work_items, state["unsignalled"]),
    }


# --------------------------------------------------------------------------
# CLI -- the only clock read, and the only sleep
# --------------------------------------------------------------------------


def wait_for_markers(
    ledger_dir: Path | str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    monotonic=time.monotonic,
    sleep=time.sleep,
    schema: dict | None = None,
) -> tuple[dict, dict]:
    """Poll until every scanner signalled or the timeout expires.

    The clock and the sleep are injected so this loop is exercised in tests
    rather than mocked away wholesale; the decision itself stays in
    ``evaluate_barrier``, which reads no clock at all.
    """
    started = monotonic()
    while True:
        markers = load_markers(ledger_dir, schema)
        state = evaluate_barrier(
            markers,
            elapsed_seconds=monotonic() - started,
            timeout_seconds=timeout_seconds,
        )
        if state["state"] != STATE_WAITING:
            return markers, state
        print(
            f"waiting for {state['unsignalled']} to signal completion "
            f"(have {state['signalled']})"
        )
        sleep(poll_seconds)


def _cmd_wait(args: argparse.Namespace) -> int:
    """Join, then write the night's one plan."""
    markers, state = wait_for_markers(
        args.ledger_dir,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
    )
    if state["partial"]:
        print(
            f"::warning title=Partial security plan::{state['unsignalled']} never "
            f"signalled for {args.run_date}; shipping a plan marked PARTIAL"
        )
    result = run_barrier(
        args.repo,
        markers=markers,
        run_date=args.run_date,
        ledger_uri=args.ledger_uri,
        state=state,
        patterns=load_banned_patterns(args.banned_patterns),
    )
    # Issue numbers and counts only; no titles or finding detail on a CI log,
    # which is readable by anyone who can see the run.
    if result["nothing_to_plan"]:
        print(f"nothing_to_plan=true run_date={args.run_date}")
        return 0
    print(
        f"nothing_to_plan=false plan_issue={result['plan_issue']} "
        f"work_items={len(result['work_items'])} partial={str(result['partial']).lower()}"
    )
    if args.ledger_fields:
        shard = build_shard(
            args.run_date,
            ORCHESTRATION_STAGE,
            args.generated_at,
            result["ledger_fields"],
        )
        out = Path(args.ledger_fields)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(shard, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Join the night's grouping passes and write exactly one "
        "orchestration plan (intent #4290, unit U10)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    wait = sub.add_parser("wait", help="wait for both markers, then write the plan")
    wait.add_argument("--repo", required=True, help="Repository (owner/name)")
    wait.add_argument("--run-date", required=True, help="YYYY-MM-DD")
    wait.add_argument(
        "--ledger-dir", required=True, help="directory holding the run's shards"
    )
    wait.add_argument(
        "--ledger-uri", required=True, help="s3:// URI of the private run ledger prefix"
    )
    wait.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    wait.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    wait.add_argument("--generated-at", required=True, help="ISO-8601, from the caller")
    wait.add_argument("--ledger-fields", help="write the orchestration shard here")
    wait.add_argument(
        "--banned-patterns",
        default=str(BANNED_PATTERNS_PATH),
        help="banned-pattern list (defaults to U9's committed one)",
    )
    wait.set_defaults(func=_cmd_wait)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "run_date", None) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.run_date):
        print(
            f"::error title=Security plan barrier::run date {args.run_date!r} is not "
            "YYYY-MM-DD; the plan's identity is its date, so an unresolved one would "
            "file an unmatched second plan",
            file=sys.stderr,
        )
        return 1
    try:
        return args.func(args)
    except (BarrierError, LedgerError) as exc:
        print(f"::error title=Security plan barrier::{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
