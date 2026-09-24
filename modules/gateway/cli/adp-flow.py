#!/usr/bin/env python3
"""AI-DLC flow planning, readback and human approval through the shared ADP session.

The gateway owns validation, policy authority and scheduling. This client renders
revision-bound previews and never substitutes local approval for server checks.
See flow.md for command contracts, deployment prerequisites and limitations.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common  # noqa: E402

Api = common.Api
CliError = common.CliError

NAME = "flow"

# Prefix-free, matching every other helper: `adp_common.gateway_url()` appends
# `/api`, and CloudFront strips that before the origin. The routes are registered
# at `/orchestration/...` (src/orchestration/routes.py's prefix note, pinned by
# tests/test_route_prefix_convention.py), so `/api/orchestration/...` from here
# would double the prefix.
FLOWS = "/orchestration/flows"
GATES = "/orchestration/gates"
# Inert draft registration (`draft_routes.py`). This, NOT `FLOWS`, is where a
# document from disk goes: a POST to `FLOWS` records an approval decision under
# the caller's identity in the same call that creates the graph, so the operator
# approves a plan whose effective shape — the transforms add a dominating
# acceptance gate and possibly a gate per wave — they have not yet seen. The draft
# path produces the same graph with nothing armed, which is what makes a preview
# followed by a separate, revision-bound acceptance possible at all.
DRAFTS = "/orchestration/flows/drafts"
# Dry-run registration: what registering WOULD produce, computed and stored
# nowhere (the route takes no database session at all). Used as `create`'s
# pre-flight so a document that cannot be registered is reported before any row
# exists — and, for a document that CAN be registered, as the only source of the
# proposed policy bounds and the derived execution shape. `GET /flows/{id}` reports
# the policy in force, which for an inert draft is correctly none, so the bounds an
# operator must read before granting them are available here and nowhere else.
DRAFTS_PREVIEW = "/orchestration/flows/drafts/preview"
# The hosted intake conversation (`src/orchestration/intake_routes.py`). This is
# what makes `start` a real command rather than a report of a missing capability:
# turns go onto the same queue and the same `intent-refinement` agent the dashboard
# talks to, so a conversation begun here is the same conversation the SPA shows.
INTAKE = "/orchestration/intake/sessions"
# Server-side derivation of a plan document from a refined intent
# (`intake_routes.plan_from_session`, #5331 blocker 4). This is why `start` can
# reach a plan at all: there is no client-side producer of a plan document, and
# there must not be one. A CLI that assembled nodes, edges and a repository
# binding locally would be a second implementation of rules the server owns
# (`validate_proposal`'s six checks, the address grammar, the wave/eval
# cardinality), and the repository name it wrote would be an unverified grant.
# The route writes nothing, so calling it is safe to retry.
INTAKE_PLAN = INTAKE + "/{session_id}/plan"
# The execution ledger for one flow (`GET /flows/{id}/execution`, #5145). This is
# the ONLY surface that answers "did the engine actually pick this up?". The gate
# approve response says a gate moved — `node_id`, `status`, `state`,
# `decision_id` — and says nothing whatsoever about dispatch, so reporting
# "the engine is running your work" on the strength of it would be a claim the
# CLI cannot support.
EXECUTION = "/orchestration/flows/{flow_id}/execution"

# The authenticated deployment-capability read. The engine is a fail-closed opt-in
# flag (`FEATURE_ORCHESTRATION_ENGINE_ENABLED`, src/features/routes.py), so an
# absent flag means "off" and a client must say so rather than let a 404 from a
# route that was never mounted read as "no such flow".
FEATURES = "/features"
ENGINE_FEATURE = "orchestration_engine"

# Statuses `GET /flows` accepts (src/orchestration/display_state.py FlowStatus).
# Validated locally so a typo is the usage error it is, reported before a gateway
# is resolved, instead of an opaque 422 from the server's enum coercion.
FLOW_STATUSES = ("attention_needed", "awaiting_you", "running", "queued", "complete", "empty")

# Node states that mean a human is being waited on, and states that mean the work
# needs attention. Both from src/orchestration/state.py's NodeState; `rejected`
# and `skipped` are deliberately absent there and so absent here.
BLOCKED_STATES = ("failed", "halted", "rejected_at_gate")
GATE_STATE = "awaiting_gate"
# The node_ref registration gives the acceptance gate it puts in front of the whole
# graph (`registration.ACCEPTANCE_GATE_REF`). Matched on the ref rather than on
# "the only gate awaiting an answer", because a document may legitimately declare
# its own gates and a deployment with gate-every-wave enabled adds more — picking
# the wrong one would accept the plan by answering a gate that releases only part
# of it.
ACCEPTANCE_GATE_REF = "accept"
# "Next eligible work" is what the engine may dispatch without anything else
# finishing first. `ready` is exactly that set; `pending` is explicitly not
# (predecessors unsatisfied), and showing it as eligible would tell a user work
# was about to start when it cannot.
ELIGIBLE_STATE = "ready"

# `watch` bounds. A terminal left open must not poll a tenant's API forever, and
# an unbounded retry loop on a gateway that is down is indistinguishable from a
# hang. Interrupting or exiting detaches only — see DETACHED.
WATCH_INTERVAL_SECONDS = 10
WATCH_MAX_POLLS = 360
WATCH_MAX_CONSECUTIVE_ERRORS = 3

# `start`'s conversation bounds. A planning reply is a model call, so the interval
# is short and the ceiling is minutes rather than hours — and hitting it is
# reported as "not yet, resume with this id", never as a failure, because the turn
# is enqueued and the agent will still answer it.
INTAKE_POLL_SECONDS = 2
INTAKE_MAX_POLLS = 90
# How many question-and-answer rounds one invocation will drive. Bounded so a
# misbehaving agent cannot hold a terminal open indefinitely; the conversation is
# durable, so the limit costs a `--resume` rather than the work.
INTAKE_MAX_TURNS = 20

# Bounds on waiting for the engine to take up an accepted plan. Admission is
# asynchronous — acceptance arms the flow and a separate tick dispatches it — so
# there is a real window in which the plan is accepted and no execution row
# exists yet. Short and few: this confirms a handoff, it does not follow the work
# (`adp flow watch` does that), and exhausting the budget is reported as "not
# confirmed yet" with the watch command, never as a failure, because the
# acceptance is durable and already recorded.
DISPATCH_POLL_SECONDS = 3
DISPATCH_MAX_POLLS = 10

DETACHED = (
    "Detached. Hosted execution is unaffected: this did NOT approve, cancel, pause or "
    "restart anything the engine already accepted. Run 'adp flow watch FLOW_ID' to reattach."
)

# Stated once, and referenced by the approval path's output, because a reader who
# believes this guard is atomic will draw a conclusion it does not support.
STALE_GUARD_LIMIT = (
    "The approval was bound to that plan revision and sent as a server-side precondition: ADP "
    "compared it against the plan in force inside the same transaction that moved the gate, so a "
    "concurrent amendment could not have collected this approval. A revision that had already moved "
    "is refused with the gate untouched, and the attempt is recorded as a refusal decision."
)

_READY_TO_PLAN = (
    "The intent is captured. Nothing has been registered, approved or started — that is deliberate: "
    "this conversation produces a DRAFT, and approving a specific plan revision is a separate act by "
    "you. Rolling the two together would mean accepting execution bounds nobody reviewed.\n\n"
    "Next:\n"
    "  * Continue refining it:            adp flow start --resume {session_id}\n"
    "  * Generate a bounded plan preview: adp flow start --resume {session_id} --plan\n"
    "  * Register a plan document inert, preview its effective graph, then accept it:\n"
    "      adp flow create --file plan.json\n"
    "  * Follow whatever is already running: adp flow list / show FLOW_ID / watch FLOW_ID"
)


def progress(message):
    """Progress, warnings and prompts go to stderr, so stdout stays parseable."""
    print(message, file=sys.stderr)


class LazyApi:
    """Resolve the gateway on the FIRST request, not at startup.

    Same reason as `adp-superplane.py`'s: several verbs reject their own arguments
    before any request is due, and `start` is the clearest case — a bare
    `adp flow start` with no outcome, or `--resume` with nothing to resume, is a
    usage error the caller must see as one. Building the transport eagerly made
    those answer "reinstall the CLI" instead, which is both wrong and unactionable
    for someone who simply omitted an argument. Resolving on first use keeps the
    gateway a requirement of the request rather than of the invocation.
    """

    def __init__(self):
        self._api = None

    def request(self, method, path, body=None, **kwargs):
        if self._api is None:
            self._api = Api()
        return self._api.request(method, path, body, **kwargs)


def segment(value):
    """One path segment. `safe=""` so an id carrying a slash cannot add a segment."""
    return urllib.parse.quote(str(value), safe="")


def query(path, params):
    pairs = {key: value for key, value in params.items() if value is not None}
    return path + ("?" + urllib.parse.urlencode(pairs) if pairs else "")


def flow_id_argument(value):
    """Accept a flow id shaped like one the server issues.

    Checked locally so a shell mishap (a stray flag, a pasted URL) is reported as
    the usage error it is rather than being sent as a path segment. The server is
    still the authority on existence and tenancy — a well-formed id from another
    tenant is its 404 to give, not ours to guess.
    """
    text = (value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", text):
        raise CliError(
            "Use a flow id as ADP issued it, for example 'adp flow show 7f3c2a10'. Run 'adp flow list' to see yours.",
            "usage_error",
            1,
        )
    return text


def engine_available(api):
    """Whether this deployment serves the orchestration engine at all.

    Read BEFORE the first flow call on the paths where the difference matters. The
    engine flag is fail-closed, so a gateway without it mounts the routes but the
    capability is off; distinguishing that from "no such flow" is the difference
    between "ask your administrator to enable the engine" and "check the id".

    Unreadable features are NOT treated as absent: a flag endpoint that failed is
    not evidence the engine is off, and reporting it as off would send a user to
    their administrator over a transient error. The caller proceeds and the real
    call reports the real failure.
    """
    try:
        return bool(api.request("GET", FEATURES).get("features", {}).get(ENGINE_FEATURE, False))
    except CliError:
        return True


def require_engine(api, command):
    if not engine_available(api):
        return common.envelope(
            "unavailable",
            command,
            {"capability": ENGINE_FEATURE, "supported": False},
            "This deployment does not have the orchestration engine enabled. Ask an ADP administrator "
            "to enable it; nothing was submitted, approved or started by this command.",
        )
    return None


# --- cost, which is three-valued -------------------------------------------


def cost_text(cost):
    """Render a three-valued cost without ever inventing a number.

    `unknown` must not become `$0.00`: "we did not measure this" and "this spent
    nothing" are different facts, and the story requires preserving the
    distinction. `amount_usd` arrives as a STRING so `Numeric(10, 6)` keeps its
    sub-cent precision — it is printed, never floated.
    """
    if not isinstance(cost, dict):
        return "unknown (not reported)"
    status = cost.get("status")
    if status == "known":
        amount = cost.get("amount_usd")
        return f"${amount} USD" if amount is not None else "unknown (no amount reported)"
    if status == "none_incurred":
        return "none incurred"
    reason = cost.get("reason")
    return "unknown" + (f" ({reason})" if reason else "")


# --- readback ---------------------------------------------------------------


def flow_rows(payload):
    return [
        {
            "id": flow.get("id"),
            "title": flow.get("title"),
            "status": flow.get("status"),
            "awaiting_gate_count": flow.get("awaiting_gate_count"),
            "stalled_count": flow.get("stalled_count"),
            "total_nodes": flow.get("total_nodes"),
            "cost": cost_text(flow.get("delivery_cost")),
        }
        for flow in payload.get("flows", [])
    ]


def list_flows(args, api):
    unavailable = require_engine(api, "flow list")
    if unavailable:
        return unavailable
    payload = api.request(
        "GET",
        query(FLOWS, {"status": args.status, "limit": args.limit, "offset": args.offset, "needs_me": "true" if args.needs_me else None}),
    )
    detail = {
        "flows": flow_rows(payload),
        "total": payload.get("total"),
        "limit": payload.get("limit"),
        "offset": payload.get("offset"),
        "status_counts": payload.get("status_counts", {}),
    }
    if not detail["flows"]:
        # A tenant with no flows is a complete answer, not a failure — but it is
        # also the state where "did my filter hide them?" is the real question, so
        # the next action names the filter when one is set.
        filtered = f" with status {args.status}" if args.status else ""
        return common.envelope(
            "ok",
            "flow list",
            detail,
            f"No flows{filtered}. Start one in the ADP dashboard, or run 'adp flow create --file plan.json'.",
        )
    return common.envelope("ok", "flow list", detail, "Run 'adp flow show FLOW_ID' for a flow's graph, gates and next eligible work.")


def node_summary(node):
    return {
        "id": node.get("id"),
        "ref": node.get("node_ref"),
        "kind": node.get("kind"),
        "state": node.get("state"),
        "title": node.get("title"),
        "issue_url": node.get("issue_url"),
        "cost": cost_text(node.get("cost")),
    }


def graph_readback(payload):
    """One flow reduced to the four questions a follower actually asks.

    Progress, what is blocked, what a human owes a decision on, and what may run
    next. Deliberately a projection and not the raw graph: the full node list is
    available with --json, and a readable dump of every node buries the gate that
    needs answering.

    `stalled` is its own server-derived boolean and is NOT a node state, so a
    stalled node is reported as blocked even while its state still reads running.
    Filtering on state alone would hide exactly the node an operator is looking
    for.
    """
    nodes = payload.get("nodes", [])
    gates = [node for node in nodes if node.get("state") == GATE_STATE]
    blocked = [node for node in nodes if node.get("state") in BLOCKED_STATES or node.get("stalled")]
    eligible = [node for node in nodes if node.get("state") == ELIGIBLE_STATE]
    counts: dict[str, int] = {}
    for node in nodes:
        state = node.get("state") or "unknown"
        counts[state] = counts.get(state, 0) + 1
    policy = payload.get("execution_policy")
    return {
        "flow_id": payload.get("flow_id"),
        "title": payload.get("title"),
        "state": payload.get("state"),
        "node_states": counts,
        "total_nodes": len(nodes),
        "outstanding_gates": [node_summary(node) for node in gates],
        "blocked": [
            dict(
                node_summary(node),
                configuration_problem=node.get("configuration_problem"),
                binding_hold=node.get("binding_hold"),
            )
            for node in blocked
        ],
        "next_eligible": [node_summary(node) for node in eligible],
        "cost": cost_text(payload.get("cost")),
        # The policy the engine is actually deciding with, not a second reading of
        # the plan document. Absent is reported as absent: an empty dict here would
        # read as "nothing authorized", which is a different claim.
        "execution_policy": {
            "autonomous_actions": policy.get("autonomous_actions"),
            "human_decisions": policy.get("human_decisions"),
            "repository_ids": policy.get("repository_ids"),
            "environment_connection_ids": policy.get("environment_connection_ids"),
            "expires_at": policy.get("expires_at"),
            "limits": policy.get("limits"),
        }
        if isinstance(policy, dict)
        else None,
    }


def show_next_action(detail):
    """Worst news first, matching how the server derives a flow's status.

    A blocked node outranks a gate and a gate outranks eligible work, because a
    person who is not told about the block will wait on work that cannot clear
    it. Ordering this by graph position instead would bury the blocker.
    """
    if detail["blocked"]:
        return f"{len(detail['blocked'])} node(s) need attention. Inspect them with --json, then resume or re-plan in the dashboard."
    if detail["outstanding_gates"]:
        gate = detail["outstanding_gates"][0]
        return (
            f"A decision is waiting: 'adp flow gate approve {gate['id']}' (or 'gate reject'). "
            f"Read the plan first with 'adp flow plans {detail['flow_id']}'."
        )
    if detail["next_eligible"]:
        return (
            f"{len(detail['next_eligible'])} item(s) are eligible to run; the engine schedules them. "
            f"Follow with 'adp flow watch {detail['flow_id']}'."
        )
    return "Nothing is waiting on you. Follow progress with 'adp flow watch " + str(detail["flow_id"]) + "'."


def show_flow(args, api):
    unavailable = require_engine(api, "flow show")
    if unavailable:
        return unavailable
    payload = api.request("GET", f"{FLOWS}/{segment(args.flow_id)}")
    detail = graph_readback(payload)
    if args.json:
        # --json is a snapshot for a machine, so it carries the full graph as the
        # server gave it alongside the projection. A consumer that needs every
        # node must not have to call the API a second time to get it.
        detail["graph"] = payload
    return common.envelope("ok", "flow show", detail, show_next_action(detail))


def watch_flow(args, api):
    """Re-read `show` on a bounded interval. Exiting detaches, nothing more.

    Polling, not streaming: there is no server-side change feed for a flow, and
    inventing one client-side would be a second progress contract. Bounded on both
    axes — total polls and consecutive failures — so a terminal left open cannot
    hammer a tenant's API and a gateway that is down fails with a legible message
    instead of retrying invisibly forever.

    The readable stream prints only when something changed, because a watch that
    reprints an identical block every ten seconds trains a reader to stop looking
    at it.
    """
    unavailable = require_engine(api, "flow watch")
    if unavailable:
        return unavailable
    flow_id = segment(args.flow_id)
    previous = None
    failures = 0
    detail = None
    # The last --json snapshot not yet written; see the --json branch below.
    pending = None

    def flush_pending_before_exit():
        """Do not lose the newest successful poll when the watch exits in error.

        The normal return leaves this snapshot for ``main()`` to emit once.  An
        exception bypasses that return, though, so the buffer must be written
        here before the error envelope is reported.  Otherwise Ctrl-C or a later
        gateway failure erases a poll the client already completed.
        """
        if args.json and pending is not None:
            print(json.dumps(pending), flush=True)

    def wait_for_next_poll():
        try:
            time.sleep(WATCH_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            flush_pending_before_exit()
            raise

    for poll in range(WATCH_MAX_POLLS):
        try:
            detail = graph_readback(api.request("GET", f"{FLOWS}/{flow_id}"))
            failures = 0
        except KeyboardInterrupt:
            flush_pending_before_exit()
            raise
        except CliError as exc:
            # A refusal is not a blip. Auth and permission failures will not fix
            # themselves by waiting, so they end the watch immediately rather than
            # burning the retry budget on a certain answer.
            if exc.exit_code in (2, 3) or exc.code == "usage_error":
                flush_pending_before_exit()
                raise
            failures += 1
            if failures >= WATCH_MAX_CONSECUTIVE_ERRORS:
                flush_pending_before_exit()
                raise CliError(
                    f"Lost contact with ADP after {failures} consecutive attempts: {exc} "
                    "Hosted execution is unaffected; reattach with 'adp flow watch' when the gateway is reachable.",
                    "gateway_unavailable",
                ) from None
            progress(f"Retrying after a failed poll ({failures}/{WATCH_MAX_CONSECUTIVE_ERRORS}): {exc}")
            wait_for_next_poll()
            continue

        if args.json:
            # One JSON object per line: a stream a consumer can read
            # incrementally. A single array would only be parseable once the
            # watch ended, which defeats watching.
            #
            # Each poll is emitted one step LATE — when the next one arrives —
            # because main() has exactly one `common.emit` that prints this
            # function's return value, i.e. the final snapshot. Printing every
            # poll here as well put that last object on stdout twice, so a
            # consumer reading line by line saw a phantom repeat of the terminal
            # state and could count one poll as two. Deferring is what makes the
            # count right on EVERY exit path: whichever poll turns out to be the
            # last one is left unprinted here and emitted once by main(), and
            # which poll that is cannot be known in advance — a watch can also
            # end by exhausting its budget, or after a failed poll retried.
            if pending is not None:
                print(json.dumps(pending), flush=True)
            pending = common.envelope("ok", "flow watch", detail, show_next_action(detail))
        else:
            snapshot = json.dumps(detail, sort_keys=True)
            if snapshot != previous:
                progress(f"— {detail['state']} · {detail['total_nodes']} node(s) · {detail['cost']}")
                progress(show_next_action(detail))
            previous = snapshot

        if args.once or detail["state"] in ("complete", "passed"):
            break
        if poll + 1 < WATCH_MAX_POLLS:
            wait_for_next_poll()
    else:
        progress(f"Stopped after {WATCH_MAX_POLLS} polls. {DETACHED}")

    return common.envelope("ok", "flow watch", detail or {}, show_next_action(detail) if detail else DETACHED)


def list_plans(args, api):
    unavailable = require_engine(api, "flow plans")
    if unavailable:
        return unavailable
    versions = api.request("GET", f"{FLOWS}/{segment(args.flow_id)}/plans")
    current = next((plan for plan in reversed(versions) if plan.get("superseded_at") is None), None)
    acceptance = "unknown"
    if current and current.get("accepted_by_decision_id"):
        # The storage column also points to PLAN_DRAFTED. A decision ID alone
        # proves attribution, not approval. Resolve actual human decisions.
        decisions = api.request("GET", f"{FLOWS}/{segment(args.flow_id)}/decisions")
        source = next((d for d in decisions if d.get("id") == current["accepted_by_decision_id"]), {})
        approvals = {"plan_accepted", "plan_amended", "gate_approved"}

        def human_approval(decision):
            return decision.get("actor_kind") == "human" and decision.get("kind", "").lower() in approvals

        if human_approval(source):
            acceptance = "accepted"
        elif source.get("kind", "").lower() == "plan_drafted":
            # A policyless draft can be accepted by answering its gate without
            # writing a new plan row. Draft revision refuses any prior approval.
            acceptance = "accepted" if any(human_approval(d) for d in decisions) else "proposed"
    elif current:
        acceptance = "proposed"
    rows = [
        {
            "version": plan.get("version"),
            "plan_hash": plan.get("plan_hash"),
            "accepted_by_decision_id": plan.get("accepted_by_decision_id"),
            "superseded_at": plan.get("superseded_at"),
            "created_at": plan.get("created_at"),
            "current_proposal": plan is current and acceptance == "proposed",
        }
        for plan in versions
    ]
    detail = {"flow_id": args.flow_id, "versions": rows}
    if args.json:
        detail["documents"] = versions
    accepted = [row for row in rows if row["superseded_at"] is None and acceptance == "accepted"]
    proposed = [row for row in rows if row["current_proposal"]]
    detail["accepted_version"] = accepted[-1]["version"] if accepted else None
    detail["proposed_version"] = proposed[-1]["version"] if proposed else None
    detail["current_version"] = current["version"] if current else None
    detail["acceptance_status"] = acceptance
    if proposed:
        row = proposed[-1]
        action = (
            f"Version {row['version']} is proposed and not accepted. Read it with --json, then bind your approval to it: "
            f"'adp flow gate approve GATE_ID --expect-plan-hash {row['plan_hash']}'."
        )
    elif accepted:
        action = "The current plan has a recorded human approval; see flow show for pause state and remaining gates."
    else:
        action = "Acceptance could not be verified from these records. Inspect flow decisions and flow show before proceeding."
    return common.envelope("ok", "flow plans", detail, action)


def list_decisions(args, api):
    unavailable = require_engine(api, "flow decisions")
    if unavailable:
        return unavailable
    decisions = api.request("GET", f"{FLOWS}/{segment(args.flow_id)}/decisions")
    detail = {
        "flow_id": args.flow_id,
        "decisions": [
            {
                "id": decision.get("id"),
                "kind": decision.get("kind"),
                "node_id": decision.get("node_id"),
                # Attribution is the point of this read: who decided, in what role,
                # and whether a human or a service did it.
                "actor_id": decision.get("actor_id"),
                "actor_role": decision.get("actor_role"),
                "actor_kind": decision.get("actor_kind"),
                "from_state": decision.get("from_state"),
                "to_state": decision.get("to_state"),
                "reason": decision.get("reason") or decision.get("rejection_reason"),
                "created_at": decision.get("created_at"),
            }
            for decision in decisions
        ],
    }
    return common.envelope("ok", "flow decisions", detail, None)


def flow_cost(args, api):
    unavailable = require_engine(api, "flow cost")
    if unavailable:
        return unavailable
    payload = api.request("GET", f"{FLOWS}/{segment(args.flow_id)}/cost")
    detail = {
        "flow_id": payload.get("flow_id"),
        "status": payload.get("status"),
        "cost": cost_text(payload),
        "total_tokens": payload.get("total_tokens"),
        "call_count": payload.get("call_count"),
        "node_count": payload.get("node_count"),
        "unknown_node_count": payload.get("unknown_node_count"),
        "partial": payload.get("partial"),
        "reason": payload.get("reason"),
    }
    if args.json:
        detail["nodes"] = payload.get("nodes", [])
    # A partial rollup that reads as a total is the misreport this guards against:
    # the figure is real, but it is a floor, and a reader deciding a budget on it
    # needs to know that before the number.
    action = None
    if payload.get("partial") or payload.get("unknown_node_count"):
        action = f"{payload.get('unknown_node_count')} node(s) have unmeasured spend, so this is a lower bound, not a total."
    return common.envelope("ok", "flow cost", detail, action)


# --- controls ---------------------------------------------------------------


def confirm(prompt, assume_yes):
    """Ask before an authorizing write. Absent a terminal, refuse rather than assume.

    A non-interactive caller that has not passed --yes has stated no intent, and
    choosing an answer for it is precisely what the story forbids ("noninteractive
    mode ... without hanging or silently choosing an answer").
    """
    if assume_yes:
        return
    if not sys.stdin.isatty():
        raise CliError(
            "This needs your explicit approval and there is no terminal to ask on. Re-run with --yes to state that intent. Nothing was approved.",
            "confirmation_required",
            1,
        )
    progress(prompt)
    if input("Type 'yes' to continue: ").strip().lower() != "yes":
        raise CliError("Cancelled. Nothing was approved, submitted or started.", "cancelled", 1)


def find_gate(api, gate_id):
    """Locate a gate across the caller's visible flows, to show WHAT is being approved.

    There is no `GET /gates/{id}` route, so the plan a gate belongs to is not
    directly addressable. Approving a bare id would mean approving something the
    user has not seen, which is the exact failure this story exists to prevent —
    so the flow list is walked to resolve it.

    Bounded to the first page: this is a courtesy read to enrich a prompt, and a
    full crawl of a tenant's flows on every approval would be a real cost for a
    prompt string. Not finding the gate is not an error — the server remains the
    authority, and it will refuse an id that is not answerable.
    """
    try:
        listing = api.request("GET", query(FLOWS, {"limit": 100}))
    except CliError:
        return None, None
    for summary in listing.get("flows", []):
        if not summary.get("awaiting_gate_count"):
            continue
        try:
            graph = api.request("GET", f"{FLOWS}/{segment(summary['id'])}")
        except CliError:
            continue
        for node in graph.get("nodes", []):
            if node.get("id") == gate_id:
                return graph, node
    return None, None


def check_plan_revision(api, flow, expected_hash):
    """Refuse an approval whose plan is not the revision the user reviewed.

    A pre-check, no longer the guarantee: the hash is also sent to the server as
    `expected_plan_hash`, which compares it inside the same transaction that moves
    the gate (see STALE_GUARD_LIMIT). This is kept in front of that for one reason
    — it runs BEFORE the confirmation prompt, so an operator whose plan has already
    moved is told so instead of being asked to consent and then handed a 409. It
    fails CLOSED, refusing when it cannot confirm rather than approving on an
    assumption, which is also why a failure to resolve the flow is an error here.
    """
    versions = api.request("GET", f"{FLOWS}/{segment(flow)}/plans")
    live = {plan.get("plan_hash") for plan in versions if plan.get("superseded_at") is None}
    if expected_hash not in live:
        raise CliError(
            f"Plan revision {expected_hash} is not the current plan for this flow, so it was NOT approved. "
            "Re-read the plan with 'adp flow plans FLOW_ID', review the current revision, then approve that hash.",
            "stale_plan_revision",
        )


def request_gate_answer(api, gate_id, *, approve, body):
    """Submit a gate answer and make an expired reviewed policy actionable."""
    try:
        return api.request(
            "POST",
            f"{GATES}/{segment(gate_id)}/{'approve' if approve else 'reject'}",
            body,
        )
    except CliError as exc:
        if exc.code == "execution_policy_expired":
            raise CliError(
                "The reviewed execution policy has expired, so nothing was approved. "
                "Continue planning or request a newly derived plan, review its new revision and policy, then approve that exact revision.",
                exc.code,
                exc.exit_code,
                status_code=exc.status_code,
            ) from None
        raise


def answer_gate(args, api):
    """Approve or reject one gate, showing what it is attached to first."""
    command = "flow gate " + args.gate_action
    unavailable = require_engine(api, command)
    if unavailable:
        return unavailable

    approving = args.gate_action == "approve"
    graph, node = find_gate(api, args.gate_id)
    context = {"gate_id": args.gate_id}
    if node is not None:
        context.update(
            flow_id=graph.get("flow_id"),
            flow_title=graph.get("title"),
            gate_title=node.get("title"),
            gate_state=node.get("state"),
        )
        if node.get("state") != GATE_STATE:
            # Reported before the write, so a user is not told "already answered"
            # by a 409 after the fact.
            progress(f"This gate is '{node.get('state')}', not awaiting a decision. The server will refuse if it is not answerable.")

    if args.expect_plan_hash is not None:
        # Presence, not truthiness. `--expect-plan-hash ""` is a request to bind
        # the approval to a revision, and the empty string is the one value that
        # can never match a live plan hash. Skipping the check for it approved
        # unconditionally with exit 0 while the operator had asked to be guarded —
        # typically `--expect-plan-hash "$(...)"` whose command substitution
        # produced nothing. Fail CLOSED and say which value was unusable, the
        # same way check_plan_revision() refuses when it cannot confirm.
        if not args.expect_plan_hash.strip():
            raise CliError(
                "--expect-plan-hash was given an empty value, so the plan revision could not be checked "
                "and nothing was approved. Read the current revision with 'adp flow plans FLOW_ID' and pass its hash, "
                "or omit --expect-plan-hash to approve without a revision check.",
                "usage_error",
                1,
            )
        if graph is None:
            raise CliError(
                "Could not resolve which flow this gate belongs to, so the plan revision could not be checked and nothing was approved. "
                "Confirm the gate id with 'adp flow show FLOW_ID'.",
                "plan_revision_unverified",
            )
        if node is None or node.get("state") == GATE_STATE:
            check_plan_revision(api, graph["flow_id"], args.expect_plan_hash)
        context["approved_plan_hash"] = args.expect_plan_hash

    if approving:
        what = context.get("gate_title") or args.gate_id
        confirm(
            f"Approving '{what}'"
            + (f" on flow {context.get('flow_title')}" if context.get("flow_title") else "")
            + ".\nThis authorizes the engine to schedule the work this gate releases, within the flow's accepted policy.",
            args.yes,
        )

    body = {"reason": args.reason}
    if args.expect_plan_hash is not None:
        # The server-side precondition. The local `check_plan_revision` above
        # already refused an obviously-moved revision so the operator learns that
        # before the confirmation prompt rather than from a 409 after it — but this
        # is the enforcement point: it is compared inside the same transaction that
        # moves the gate, which is the part no client re-read can do.
        body["expected_plan_hash"] = args.expect_plan_hash
    result = request_gate_answer(api, args.gate_id, approve=approving, body=body)
    detail = dict(
        context,
        status=result.get("status"),
        state=result.get("state"),
        decision_id=result.get("decision_id"),
        actor_kind=result.get("actor_kind"),
        message=result.get("message"),
    )
    # Reported only on the server's own readback, never inferred from a 200: the
    # story requires that work is called started only when the server confirms it.
    if approving:
        action = (
            "Approved. The engine schedules eligible work from here — you do not trigger each item. "
            f"Follow it with 'adp flow watch {context.get('flow_id') or 'FLOW_ID'}'."
        )
        if args.expect_plan_hash:
            action += "\n" + STALE_GUARD_LIMIT
    else:
        action = "Rejected. Successors stay pending; the node remains re-openable."
        if args.expect_plan_hash:
            # Stated on rejection too. Rejecting a revision you did not read
            # misattributes a decision just as approving one does, and the server
            # applies the same precondition to both, so a reader of this output
            # should be told the rejection was bound.
            action += "\n" + STALE_GUARD_LIMIT
    return common.envelope("ok", command, detail, action)


# --- submitting a prepared plan -------------------------------------------


def read_plan_file(path):
    """Read a prepared plan document from disk.

    Bounded and validated as an object before it is sent, so a wrong file (a log,
    a truncated download) is a local error naming the file rather than a 422
    listing violations for a document the user did not mean to submit.
    """
    source = Path(path)
    try:
        if source.stat().st_size > 4_000_000:
            raise CliError(f"{path} is too large to be a plan document. Nothing was submitted.", "usage_error", 1)
        document = json.loads(source.read_text())
    except OSError as exc:
        raise CliError(f"Could not read {path}. Nothing was submitted.", "usage_error", 1) from exc
    except ValueError as exc:
        raise CliError(f"{path} is not valid JSON. Nothing was submitted.", "usage_error", 1) from exc
    if not isinstance(document, dict) or not document.get("flow_slug"):
        raise CliError(
            f"{path} does not look like a plan document (no 'flow_slug'). Nothing was submitted.",
            "usage_error",
            1,
        )
    return document


def preview_of(api, flow_id, plan_hash, dry_run=None):
    """Read back the graph the transforms actually produced, plus its bounded policy.

    The preview half of `create`. Reads the registered draft rather than rendering
    the submitted document, because those are not the same graph: registration
    inserts an acceptance gate in front of every root and, when the deployment
    enables it, a gate per wave. A preview drawn from the local file would omit
    exactly the human controls the operator most needs to see, and would be a
    second implementation of the transforms in the layer least able to notice when
    it drifts from the server's.

    `graph_readback` is reused verbatim so the preview and `adp flow show` cannot
    disagree about the same flow. The acceptance gate is resolved out of it by ref
    so the confirmation can name what will be answered.

    `dry_run` is the preview body from the pre-flight, and it is REQUIRED for the
    authority half of this to be correct. `GET /flows/{id}` reports
    `execution_policy` from `load_in_force_policy`, which for an inert draft is
    deliberately `None` — the demoted policy is not authority in force, and that
    route is right to say so. Reading the bounds from there would therefore render
    "this plan authorizes no autonomous action on its own" over a plan whose
    acceptance grants a full policy, at the exact moment of consent and in the
    safe-sounding direction. The proposed bounds, and the derived execution shape
    (waves, staging, who may conclude each node), come from the dry run because that
    is the only surface that reports them before acceptance.

    Best-effort by design: a failure here is reported and does NOT approve
    anything. Losing the preview must never degrade into approving unseen — see
    `create_flow`, which refuses in that case.
    """
    graph = api.request("GET", f"{FLOWS}/{segment(flow_id)}")
    readback = graph_readback(graph)
    gate = next(
        (node for node in graph.get("nodes", []) if node.get("node_ref") == ACCEPTANCE_GATE_REF),
        None,
    )
    dry_run = dry_run if isinstance(dry_run, dict) else {}
    return {
        "flow_id": flow_id,
        "plan_hash": plan_hash,
        # The effective graph: what the engine will schedule, gates included.
        "effective_graph": readback,
        # What is in force NOW, straight from the readback. For a freshly registered
        # draft this is `None` and that is the true answer to "what may run today":
        # nothing. Kept distinct from the field below rather than merged, because
        # collapsing "granted" and "requested" is how a preview comes to show
        # authority that was never accepted.
        "execution_policy": readback.get("execution_policy"),
        # What accepting WOULD grant. This is the field a consent decision rests on.
        "proposed_execution_policy": dry_run.get("proposed_execution_policy"),
        # True when the plan declares no policy at all — legacy unbounded semantics,
        # not "restricted to nothing". Carried from the server's own boolean rather
        # than inferred from a null here; see `DraftPreviewResponse`.
        "execution_is_unbounded": dry_run.get("execution_is_unbounded"),
        # The derived execution order: which waves run concurrently, and what each
        # waits on. Absent from the flow readback, which returns nodes and edges but
        # never says which of them proceed at the same time.
        "waves": dry_run.get("waves") or [],
        "nodes": dry_run.get("nodes") or [],
        "acceptance_gate_id": gate.get("id") if gate else None,
        "acceptance_gate_title": gate.get("title") if gate else None,
    }


def preview_text(preview):
    """Render a preview for a human about to authorize it.

    Deliberately leads with the gates and the policy bounds rather than the node
    count. What an operator needs before consenting is "what will this be allowed
    to do, to which repositories, up to what spend" — a node total does not answer
    any of that. An absent policy is stated in words, because a blank line there
    would read as "unrestricted" when it means the opposite.

    Four things, in the order a consent decision needs them: what authority
    accepting grants, what will run and in what order, who is still in the loop, and
    what it has cost. The order is the argument — a reader who stops after the first
    section has read the most consequential part.
    """
    graph = preview.get("effective_graph") or {}
    lines = [
        "",
        f"Effective graph for revision {preview.get('plan_hash')} (as ADP compiled it, including gates it inserted):",
        f"  {graph.get('total_nodes')} node(s) · states {json.dumps(graph.get('node_states') or {}, sort_keys=True)}",
        f"  human gates awaiting an answer: {len(graph.get('outstanding_gates') or [])}",
    ]
    for gate in graph.get("outstanding_gates") or []:
        lines.append(f"    - {gate.get('ref')}: {gate.get('title')}")
    # The PROPOSED bounds, not the in-force ones: this text is read before accepting,
    # and nothing is in force yet by design. `policy_lines` handles the absent case
    # in words; `execution_is_unbounded` distinguishes the two ways it can be absent.
    lines += policy_lines(preview.get("proposed_execution_policy"))
    if preview.get("execution_is_unbounded"):
        lines.append(
            "    ^ this plan declares NO execution policy, which means legacy UNBOUNDED semantics: "
            "no repository restriction, no action allowlist, no spend ceiling and no expiry."
        )
    for epic in preview.get("epic_metadata") or []:
        lines.append(f"  {epic.get('title') or epic.get('epic_ref')} ({epic.get('epic_ref')}):")
        lines.append(f"    {epic.get('description') or ''}")
    lines += wave_lines(preview.get("waves") or [])
    lines += conclusion_lines(preview.get("nodes") or [])
    lines.append(f"  cost so far: {graph.get('cost')}")
    return "\n".join(lines)


def wave_lines(waves):
    """The execution order, with concurrency named.

    Waves sharing a `stage` have no dependency path between them, so the engine may
    run them at the same time — which is what an operator judging blast radius needs
    and what a flat node list cannot express. Grouped by stage rather than printed
    one per line for exactly that reason: "these three start together" is the fact,
    and three consecutive lines read as a sequence.

    `stage: null` means the document's wave dependencies form a cycle (possible even
    over an acyclic node graph), so the order is genuinely undetermined. Said in
    those words rather than omitted — a wave missing from this list would look like
    work that is not in the plan.
    """
    if not waves:
        return []
    lines = ["  execution order (waves at the same stage run concurrently):"]

    def label(wave):
        ref = f"{wave.get('epic_ref')}/{wave.get('wave_ref')}"
        return f"{wave['title']} ({ref})" if wave.get("title") else ref

    by_stage = {}
    for wave in waves:
        by_stage.setdefault(wave.get("stage"), []).append(wave)
    staged = [stage for stage in by_stage if stage is not None]
    for stage in sorted(staged):
        labels = ", ".join(label(wave) for wave in by_stage[stage])
        concurrent = " (concurrent)" if len(by_stage[stage]) > 1 else ""
        lines.append(f"    stage {stage}{concurrent}: {labels}")
    for wave in by_stage.get(None, []):
        lines.append(f"    stage unknown: {label(wave)} — its wave dependencies form a cycle, so ADP cannot say when it runs.")
    for wave in waves:
        if wave.get("description"):
            lines.append(f"    {label(wave)}: {wave['description']}")
    return lines


def conclusion_lines(nodes):
    """Who or what may mark each node done — counted, then itemized for the
    machine-concluded ones.

    A total is enough for the human-supervised nodes: the operator stays in the loop
    for those and will see them again. The machine-accepted ones are the opposite —
    they conclude unattended — so each is named. That asymmetry is the point of
    printing this at all, and a summary that gave both the same treatment would bury
    the set that matters in the set that does not.
    """
    if not nodes:
        return []
    tally = {}
    for node in nodes:
        tally.setdefault(node.get("concluded_by") or "undetermined", []).append(node)
    lines = ["  who may mark work complete:"]
    for authority in sorted(tally):
        lines.append(f"    {authority}: {len(tally[authority])} node(s)")
    unattended = tally.get("machine_evaluation") or []
    for node in unattended:
        lines.append(f"      - {node.get('address')} concludes WITHOUT a human")
    undetermined = tally.get("undetermined") or []
    for node in undetermined:
        lines.append(f"      - {node.get('address')}: ADP has no conclusion rule for this node kind")
    return lines


def policy_lines(policy):
    """The bounds a plan asks for, field by field, or the absence stated in words.

    Shared by the registered-draft preview and the pre-flight dry run so the bounds
    an operator reads before a write and the bounds they read before accepting are
    rendered identically — two renderers would eventually disagree about the same
    policy, and this is the text a consent decision rests on.

    Every field is printed even when empty. An omitted `repository_ids` reads as
    "no repositories", which is the opposite of the unbounded meaning it may carry,
    so the field is shown with whatever the server summarized rather than skipped.
    """
    if not isinstance(policy, dict):
        # Said in words rather than left blank: an empty space here reads as
        # "unrestricted" when it means a plan that authorizes nothing on its own.
        return ["  policy: none declared — this plan authorizes no autonomous action on its own."]
    return [
        "  policy bounds this plan asks for:",
        f"    autonomous actions: {policy.get('autonomous_actions')}",
        f"    decisions reserved for a human: {policy.get('human_decisions')}",
        f"    repositories: {policy.get('repository_ids')}",
        f"    environment connections: {policy.get('environment_connection_ids')}",
        f"    limits: {policy.get('limits')}",
        f"    expires: {expiry_text(policy.get('expires_at'))}",
    ]


def expiry_text(expires_at):
    """The expiry, with a past one named as past rather than printed as a timestamp.

    A bare ISO timestamp does not tell a reader whether the authority they are about
    to approve is still live — comparing it to now is work, and it is work done at
    the one moment the reader is focused on something else. An expiry already behind
    us is the case that matters: accepting it is refused by the server, and without
    this the operator reads a plausible-looking date, approves, and gets a refusal
    they have to decode.

    An unparseable or absent value is passed through verbatim. Guessing at a
    malformed expiry would be the one wrong thing to do here: a reader shown
    "(already expired)" for a value this code simply failed to read has been told
    something the server never said.
    """
    if not isinstance(expires_at, str):
        return f"{expires_at}"
    try:
        moment = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return expires_at
    if moment.tzinfo is None:
        # Every expiry ADP writes is UTC; reading a naive one as local time would
        # shift the comparison by the offset and could call a dead grant live.
        moment = moment.replace(tzinfo=timezone.utc)
    if moment <= datetime.now(tz=timezone.utc):
        return f"{expires_at} — ALREADY EXPIRED: accepting this plan is refused. Request a new plan and accept that one."
    return expires_at


def registration_refusal(api, document):
    """Dry-run the registration; return an envelope to stop on, or None to proceed.

    `POST /orchestration/flows/drafts/preview` computes what registration would
    produce and takes no database session at all, so this costs nothing and can
    create nothing. It is run BEFORE the write so **a document that would be
    rejected is rejected here, with every violation at once**. Registration answers
    one 422 carrying its violations too, so this is not new information — but it
    arrives before a flow row exists, and as a list rather than as a failure the
    operator has to interpret as "nothing was written, probably".

    **A policy-bearing document is no longer refused here, and that is the change.**
    It used to be, and correctly for the server it was written against: draft
    registration compiles as `ActorKind.SERVICE`, `accept_execution_policy` refuses a
    policy-bearing document from a non-human actor, so registration 422'd. The inert
    path now DEMOTES a submitted policy into `proposed_execution_policy` instead of
    stamping it, so the document registers, carries its bounds verbatim for review,
    and grants nothing until a human accepts the revision — exactly the sequence
    `create` exists to perform. Refusing it now would be refusing the case this verb
    is most needed for, and would push its author toward the two workarounds that
    were always the real hazard: stripping the policy (registering the work without
    its limits) or posting to `POST /orchestration/flows` (approving bounds in the
    same call that compiles them, unseen).

    What must NOT be lost with that refusal is the reason it was loud. The bounds
    still have to be read before they are granted — so they are rendered from this
    dry run, before any row exists, and again from the registered draft at the
    confirmation. The control moved from "refuse the document" to "show the authority
    and require a bound human answer"; it did not relax.

    Returns `(dry_run, refusal)`. `refusal` is an envelope to stop on or None to
    proceed; `dry_run` is the preview body itself, which the caller keeps because it
    is the ONLY place the proposed bounds and the derived execution shape are
    available — `GET /flows/{id}` reports the policy *in force*, and for an inert
    draft that is deliberately none. Returning only a refusal, as this did while a
    policy-bearing document was refused outright, means the one path that now
    proceeds is the path with no bounds to show.

    An unreachable preview returns `(None, None)`. The caller then refuses
    registration and acceptance because it cannot display the proposed policy.
    """
    try:
        preview = api.request("POST", DRAFTS_PREVIEW, document)
    except CliError:
        return None, None

    if not isinstance(preview, dict):
        return None, None

    detail = {
        "would_register": preview.get("would_register"),
        "violations": preview.get("violations") or [],
        "plan_hash": preview.get("plan_hash"),
        "plan_hash_is_bindable": preview.get("plan_hash_is_bindable"),
        "proposed_execution_policy": preview.get("proposed_execution_policy"),
        "execution_is_unbounded": preview.get("execution_is_unbounded"),
        # Restated from the preview's own response rather than asserted here: the
        # route declares that it wrote nothing, and this output should carry that
        # statement rather than a claim the CLI made on its behalf.
        "wrote_nothing": preview.get("wrote_nothing"),
        "accepted": False,
    }

    if preview.get("would_register") is False:
        violations = detail["violations"]
        # Printed here, not merely carried in `detail`: the human-readable arm of
        # `emit` shows `detail` as indented JSON, which is a poor way to read a list
        # of prose violations, and a caller who sees "fix the violations" needs them
        # on the screen. `--json` callers get the same list in the envelope.
        progress("\n".join(["", "This plan would be REJECTED. Nothing was registered:", *(f"  - {violation}" for violation in violations)]))
        return preview, common.envelope(
            "failed",
            "flow create",
            detail,
            f"This plan would be rejected ({len(violations)} violation(s)), so it was NOT registered and nothing "
            "was approved. Fix the violations listed above and re-run; every one of them is reported together so "
            "they need not be fixed one at a time.",
        )

    return preview, None


def dispatch_state(api, flow_id):
    """What the engine has actually done with this flow, from the execution ledger.

    Returns `(confirmed, summary)`. `confirmed` is True only on positive evidence
    that the engine took the work up: an execution row whose phase has moved past
    `admitted`, or an action the ledger records as `dispatched`.

    Why this is read at all
    -----------------------
    The gate approve response reports that a GATE moved. It carries `node_id`,
    `status`, `state`, `decision_id` and `actor_kind`, and nothing in it is about
    dispatch. Treating it as confirmation would let the CLI tell an operator "the
    engine is now delivering your plan" on the strength of a decision row — in a
    deployment where the tick is not running, where admission refuses the work, or
    where the policy denies it, that sentence is false and the operator has no
    reason to doubt it. So the claim is made only against the ledger that would
    have to exist for it to be true.

    Why `admitted` alone is not enough
    ----------------------------------
    `ExecutionPhase.ADMITTED` means "ledger identity exists; no work started yet",
    and the enum separates it from `PREPARING` precisely because that window is
    real. It is honest progress and is reported as such, but it is not the engine
    having dispatched anything.

    Why an empty ledger is not failure
    ----------------------------------
    `legacy: true` with no executions means *no durable execution record*. That is
    emphatically not success — but it is also not proof of failure, because
    admission is asynchronous and this may simply be early. It is reported as
    unconfirmed, with the acceptance stated as durable, which is the true position.
    """
    try:
        payload = api.request("GET", EXECUTION.format(flow_id=segment(flow_id)))
    except CliError as exc:
        # Not fatal, and deliberately not silent. The acceptance already happened
        # and is recorded server-side; failing to READ the consequence must not be
        # reported as failing to accept, which would send an operator to re-approve
        # a plan that is already armed.
        return False, {"confirmed": False, "unreadable": str(exc), "executions": 0, "legacy": None}

    executions = payload.get("executions") or []
    # `legacy` is the server's own word for "this flow has no execution rows at
    # all". Carried through rather than inferred from an empty list, because an
    # empty PAGE of a non-empty ledger is a different thing.
    summary = {
        "confirmed": False,
        "executions": payload.get("total", len(executions)),
        "legacy": payload.get("legacy"),
        "phases": sorted({execution.get("phase") for execution in executions if execution.get("phase")}),
        "dispatched_actions": 0,
        "blocked": [],
        "next_check_at": None,
        "server_time": payload.get("server_time"),
    }

    for execution in executions:
        for action in execution.get("actions") or []:
            if action.get("status") == "dispatched":
                summary["dispatched_actions"] += 1
        block = execution.get("block")
        if isinstance(block, dict) and block.get("code"):
            # Surfaced, because a blocked execution IS the engine having taken the
            # work up — and the operator needs to know something is waiting on
            # them rather than reading "accepted" and walking away.
            summary["blocked"].append({"code": block.get("code"), "owner": block.get("owner"), "needs": block.get("required_input")})
        if summary["next_check_at"] is None:
            summary["next_check_at"] = execution.get("next_check_at")

    moved_past_admission = any(phase not in ("", "admitted") for phase in summary["phases"])
    summary["confirmed"] = bool(summary["dispatched_actions"]) or moved_past_admission
    return summary["confirmed"], summary


def dispatch_confirmation(api, flow_id):
    """Poll the execution ledger briefly for evidence the engine took the plan up.

    Bounded and short. This confirms a handoff; following the work is `watch`'s
    job, and a verb that blocked for the duration of delivery would be a different
    command. Exhausting the budget returns the last summary unconfirmed — the
    acceptance stands regardless, so there is nothing here to retry and nothing to
    roll back.
    """
    confirmed, summary = dispatch_state(api, flow_id)
    for poll in range(DISPATCH_MAX_POLLS - 1):
        if confirmed or summary.get("unreadable"):
            break
        try:
            time.sleep(DISPATCH_POLL_SECONDS)
        except KeyboardInterrupt:
            # Detaching from a confirmation read changes nothing server-side.
            summary["interrupted"] = True
            break
        confirmed, summary = dispatch_state(api, flow_id)
        if poll == 0 and not confirmed:
            progress("Accepted. Waiting for the engine to take the work up...")
    return confirmed, summary


def dispatch_text(flow_id, confirmed, summary):
    """State what the engine did, or say plainly that it is not yet confirmed.

    Three outcomes, kept distinct because they need different things from the
    reader: confirmed dispatch (nothing to do), a block (they must act), and no
    durable record yet (wait and watch). The last one is the one worth being
    careful about — the tempting phrasing is "your plan is running", and the
    honest phrasing names the acceptance as the thing that is certain.
    """
    if summary.get("unreadable"):
        return (
            f"The plan was accepted and that is recorded, but the engine's execution ledger could not be read "
            f"({summary['unreadable']}), so this run cannot confirm the engine took the work up. Nothing needs "
            f"re-approving. Check with 'adp flow watch {flow_id}'."
        )
    if confirmed:
        lines = [f"Confirmed by ADP: the engine has taken the work up ({summary['executions']} execution record(s))."]
        if summary["dispatched_actions"]:
            lines.append(f"  {summary['dispatched_actions']} action(s) dispatched.")
        if summary["phases"]:
            lines.append(f"  Phase: {', '.join(summary['phases'])}.")
        for block in summary["blocked"]:
            lines.append(f"  WAITING on {block['owner'] or 'someone'}: {block['code']} — needs {block['needs'] or 'input'}.")
        lines.append(f"Follow it with 'adp flow watch {flow_id}'.")
        return "\n".join(lines)
    return (
        f"Accepted — and that acceptance is durable and recorded. The engine has NOT yet been confirmed to take the "
        f"work up: no execution record has appeared within {DISPATCH_MAX_POLLS * DISPATCH_POLL_SECONDS}s. That is "
        f"normal when admission is queued, and it is NOT a confirmation that anything is running. Do not re-approve; "
        f"watch it instead: 'adp flow watch {flow_id}'."
    )


def revise_draft(args, api):
    """Preview or save one exact draft revision; never answer an execution gate."""
    command = "flow draft " + args.draft_action
    if args.expect_plan_version < 1 or not re.fullmatch(r"[0-9a-f]{64}", args.expect_plan_hash):
        raise CliError("Use a positive --expect-plan-version and the current 64-character --expect-plan-hash.", "usage_error", 1)
    if args.draft_action == "save" and not re.fullmatch(r"[0-9a-f]{64}", args.expect_proposal_hash):
        raise CliError("Use the 64-character --expect-proposal-hash returned by draft preview.", "usage_error", 1)
    body = {
        "proposal": read_plan_file(args.file),
        "expected_plan_version": args.expect_plan_version,
        "expected_plan_hash": args.expect_plan_hash,
        "reason": args.reason,
    }
    if args.draft_action == "save":
        body["expected_proposal_hash"] = args.expect_proposal_hash
    unavailable = require_engine(api, command)
    if unavailable:
        return unavailable
    operation = "preview" if args.draft_action == "preview" else "revise"
    try:
        result = api.request("POST", f"{FLOWS}/{segment(args.flow_id)}/draft/{operation}", body)
    except CliError as exc:
        if exc.status_code == 404 and exc.code != "flow_not_found":
            raise CliError(
                "This gateway does not support registered draft revisions. Upgrade the gateway and CLI; no fallback was attempted.",
                "draft_revision_unavailable",
                4,
            ) from None
        if exc.status_code == 409:
            raise CliError(
                f"Draft revision refused ({exc.code}). Read the current flow and preview again. No revision was saved.", exc.code, 4
            ) from None
        raise
    if result.get("execution_authorized") is not False or result.get("flow_id") != args.flow_id:
        raise CliError(
            "Unexpected draft revision response. Read the flow before retrying; no execution approval was requested.", "invalid_draft_response", 5
        )
    if args.draft_action == "preview":
        next_action = (
            f"Preview only: {len(result.get('added_nodes', []))} nodes added, {len(result.get('removed_nodes', []))} superseded. "
            f"Review the effective proposal and policy. Save with --expect-proposal-hash {result.get('proposal_hash')}; "
            "retain the same base plan version/hash and file. Saving does not approve execution."
        )
    else:
        next_action = (
            f"Draft revision {result.get('plan_version')} saved. "
            "The gate remains unanswered and the pause setting is unchanged. No execution was approved."
        )
    return common.envelope("ready", command, result, next_action)


def create_flow(args, api):
    """Dry-run a plan from disk, register it INERT, preview it, then accept it explicitly.

    Four steps, deliberately not one. A dry run first (`registration_refusal`), which
    writes nothing and turns the two refusals the operator would otherwise meet as a
    422 on a write — an invalid document, and a policy-bearing one the inert path
    cannot carry — into answers that arrive before any row exists. Then the document
    goes to
    `POST /orchestration/flows/drafts`, which re-validates it under the same
    authoritative rules as every other path (so this verb still does no local
    validation beyond "is this a plan document at all" — a second, weaker copy of
    the rules that could disagree with the server's is worse than none) and
    produces a graph that executes nothing: an acceptance gate dominates every
    root, and only a human answer moves it.

    Then the effective graph is read BACK from the server and shown. This is the
    step that makes the acceptance meaningful, because the graph the operator
    approves is not the document they wrote — the transforms add the gates. A
    plan submitted straight to `POST /orchestration/flows` records the approval in
    the same call that compiles it, so the operator necessarily approves a shape
    they have not seen; that is the hole this closes.

    Then the acceptance is a separate, revision-bound answer to that gate, sent
    with `expected_plan_hash` so the server refuses it if the plan moved. `--yes`
    states the intent for a script, and --expect-plan-hash must name the revision
    the caller already reviewed. Without it, --yes only returns a preview.

    Retry-safe throughout: an identical resubmission is the server's own
    idempotency case (`already_registered`, nothing written a second time), and an
    acceptance whose response was lost re-answers the same gate with the same hash,
    which the server replays as the ORIGINAL successful decision — same 200, same
    decision id, no second approval row. A retry therefore reports `ok` here rather
    than an error, which is the point: a caller whose response was lost must be able
    to learn the outcome it already has instead of being handed a conflict it cannot
    act on. A different actor, or the opposite verb, is not that retry and is still
    refused. So a dropped connection yields no duplicate flow, plan, decision or
    approval, and no false conflict either.
    """
    unavailable = require_engine(api, "flow create")
    if unavailable:
        return unavailable
    return register_preview_accept(
        api, read_plan_file(args.file), verb="flow create", reason=args.reason, assume_yes=args.yes, expected_hash=args.expect_plan_hash
    )


def register_preview_accept(api, document, *, verb, reason, assume_yes, expected_hash=None):
    """Register a document inert, preview the effective graph, accept it, confirm dispatch.

    Shared by `create` (document from disk) and `start` (document derived by the
    server from a refined intent) — one implementation, deliberately. The steps
    here ARE the consent contract: what makes an acceptance meaningful is that the
    effective graph and the bounds being granted were rendered first, and that the
    approval is bound to the revision that was rendered. A second copy of that
    sequence would be a second place for those properties to be quietly weakened,
    and the weakened copy would be the one on the newer, less-reviewed path.

    `verb` only names the envelope, so each command still reports as itself.
    """
    nodes = document.get("nodes") or []
    title = document.get("title") or document.get("flow_slug")

    # Step 0 — dry run, writing nothing. Two things are learned here that cannot be
    # learned after a write, and both would otherwise be reported as a bare 422 on
    # the registration itself.
    dry_run, refusal = registration_refusal(api, document)
    if refusal is not None:
        return refusal
    if not dry_run or not dry_run.get("plan_hash") or dry_run.get("plan_hash_is_bindable") is not True:
        raise CliError("ADP could not provide a revision-bound preview. Upgrade the gateway before registering this plan.", "preview_unavailable", 4)
    if expected_hash is not None and expected_hash != dry_run["plan_hash"]:
        raise CliError("The plan differs from --expect-plan-hash. Review the new preview before approving it.", "stale_plan_revision", 4)
    if assume_yes and not expected_hash:
        return common.envelope(
            "pending",
            verb,
            {"preview": dry_run, "plan_hash": dry_run["plan_hash"], "accepted": False},
            "Review this preview, then supply --yes --expect-plan-hash HASH to approve that exact revision. Nothing was registered or approved.",
        )

    # Step 1 — register inert. No confirmation is asked for first: this writes a
    # graph that cannot run, and prompting before the operator has seen the
    # effective shape would be asking them to consent to something unrendered.
    progress(f"Registering '{title}' ({len(nodes)} node(s) as written) as an inert draft. Nothing can run until you accept it.")
    registered = api.request("POST", query(DRAFTS, {"reason": reason}), document)
    flow_id = registered.get("flow_id")
    plan_hash = registered.get("plan_hash")
    detail = {
        "flow_id": flow_id,
        "plan_version": registered.get("plan_version"),
        "plan_hash": plan_hash,
        "decision_id": registered.get("decision_id"),
        "nodes_created": registered.get("nodes_created"),
        "edges_created": registered.get("edges_created"),
        # True when this exact document was already registered. Surfaced rather
        # than hidden: a retry whose first response was lost must be able to tell
        # that it re-read its own result instead of creating a second flow.
        "already_registered": registered.get("already_registered"),
        "acceptance_gate_address": registered.get("acceptance_gate_address"),
        "flow_url": registered.get("flow_url"),
        # Stated on every arm below, including the ones that stop early, because
        # "did this arm anything?" is the question a reader of this output has.
        "accepted": False,
    }

    if not flow_id or not plan_hash:
        # Without both, neither the preview nor the binding is possible, and
        # accepting unbound is the fallback this must not take.
        raise CliError(
            "ADP registered the draft but did not return a flow id and plan revision, so it could not be previewed "
            "or accepted. Nothing was approved. Check 'adp flow list' before retrying.",
            "incomplete_registration",
        )
    if plan_hash != dry_run["plan_hash"]:
        raise CliError(
            f"Registration changed the previewed revision. Flow {flow_id} remains unapproved; review its current plan before accepting.",
            "stale_plan_revision",
            4,
        )

    # Step 2 — preview the EFFECTIVE graph, from the server.
    try:
        preview = preview_of(api, flow_id, plan_hash, dry_run)
    except CliError as exc:
        # Fail closed. The draft stands and is inert, so stopping here costs the
        # operator a re-run, whereas approving what could not be displayed is
        # precisely the "approved something I had not read" failure.
        raise CliError(
            f"The draft was registered as flow {flow_id} and is inert, but its effective graph could not be read back "
            f"({exc}), so it was NOT accepted. Review it with 'adp flow show {flow_id}', then accept its gate with "
            f"'adp flow gate approve GATE_ID --expect-plan-hash {plan_hash}'.",
            "preview_unavailable",
            4,
        ) from None
    detail["preview"] = preview

    gate_id = preview.get("acceptance_gate_id")
    if not gate_id:
        # Nothing to answer. Either the deployment's transforms produced no
        # acceptance gate or it has already been answered; both are the server's
        # statement about this flow, and inventing an approval target is not the
        # CLI's call to make.
        detail["accepted"] = False
        return common.envelope(
            "unavailable",
            verb,
            detail,
            f"The plan is registered as flow {flow_id} and is inert, but no unanswered acceptance gate was found on it, "
            f"so nothing was accepted. Inspect it with 'adp flow show {flow_id}'.",
        )

    # Step 3 — explicit, revision-bound human acceptance.
    progress(preview_text(preview))
    if not assume_yes and not sys.stdin.isatty():
        return common.envelope(
            "pending",
            verb,
            detail,
            f"Review plan {plan_hash}, then run: adp flow gate approve {gate_id} --expect-plan-hash {plan_hash} --yes. Nothing was approved.",
        )
    confirm(
        f"Accepting plan revision {plan_hash} on flow {flow_id} by answering '{preview.get('acceptance_gate_title') or gate_id}'.\n"
        "This is the step that arms the engine: it authorizes ADP to schedule the work above, within the policy shown, "
        "and the approval is recorded against your identity.",
        assume_yes,
    )
    answer = request_gate_answer(
        api,
        gate_id,
        approve=True,
        # The binding, server-enforced. Sent even under --yes: a script that
        # accepts whatever is live is the concurrent-edit hole, and the hash it
        # sends is the one this run previewed.
        body={"reason": reason, "expected_plan_hash": plan_hash},
    )
    detail["acceptance"] = {
        "gate_id": gate_id,
        "status": answer.get("status"),
        "state": answer.get("state"),
        "decision_id": answer.get("decision_id"),
        "actor_kind": answer.get("actor_kind"),
        "message": answer.get("message"),
    }
    detail["accepted"] = True

    # Step 4 — confirm the handoff against the engine's own ledger, not against the
    # gate response. `answer` above says a gate moved; it says nothing about
    # dispatch, and "the engine is delivering your plan" is not a claim this verb
    # may make without evidence. See `dispatch_state`.
    confirmed, dispatch = dispatch_confirmation(api, flow_id)
    detail["dispatch"] = dispatch
    detail["dispatch_confirmed"] = confirmed
    return common.envelope(
        "ok",
        verb,
        detail,
        f"Accepted revision {plan_hash}. The engine schedules eligible work from here — you do not trigger each item.\n"
        f"{dispatch_text(flow_id, confirmed, dispatch)}\n{STALE_GUARD_LIMIT}",
    )


def session_state(api, session_id):
    """Read one intake conversation back, or the caller's newest one.

    `session_id=None` means "whichever conversation I was last in", which is what
    `--resume` without an argument resolves: the id is server-minted and a caller
    who lost it has no other handle. A 404 on that path is "you have none", not an
    error — a first-time user has no conversation and that is not a failure.
    """
    path = f"{INTAKE}/{segment(session_id)}" if session_id else f"{INTAKE}/latest"
    try:
        return api.request("GET", path)
    except CliError as exc:
        if exc.code == "session_not_found" or getattr(exc, "status_code", None) == 404:
            return None
        raise


def render_draft(draft):
    """The refinement artifact, as lines a person reads.

    Rendered field by field rather than dumped as JSON, because this is the thing
    the user is being asked to confirm is right before it becomes a plan, and a
    wall of braces is not reviewable. Unknown keys are shown rather than dropped:
    the agent owns this shape and may add to it, and silently hiding a field it
    added would mean the operator approves something they were not shown.
    """
    if not draft:
        return "  (nothing captured yet)"
    order = ("intent", "motivation", "outcomes", "constraints", "openQuestions")
    labels = {
        "intent": "Intent",
        "motivation": "Why",
        "outcomes": "Outcomes",
        "constraints": "Constraints",
        "openQuestions": "Open questions",
    }
    lines = []
    for key in list(order) + sorted(set(draft) - set(order) - {"updatedAt"}):
        value = draft.get(key)
        if not value:
            continue
        label = labels.get(key, key)
        if isinstance(value, list):
            lines.append(f"  {label}:")
            lines.extend(f"    - {item}" for item in value)
        else:
            lines.append(f"  {label}: {value}")
    return "\n".join(lines) or "  (nothing captured yet)"


def open_questions(state):
    """What the agent still needs decided, as a list of strings.

    Reads `open_questions` — the API's field, sourced from the draft's
    `openQuestions`, which is where the refinement persona is instructed to put
    them. There is no single "pending question": a turn can come back needing two
    things decided, and collapsing that to one would silently drop the rest.
    """
    raw = (state or {}).get("open_questions") or []
    return [str(item).strip() for item in raw if str(item).strip()]


def render_questions(questions):
    """One per line, so a script's operator sees all of them, not just the first."""
    return "\n".join(f"  {text}" for text in questions)


def await_reply(api, session_id, *, after, task_id=None):
    """Poll one conversation until the agent has answered, or give up saying so.

    Bounded for the same reason `watch` is: a terminal left open must not poll a
    tenant's API forever, and an unbounded wait on a worker that died is
    indistinguishable from a hang.

    Completion is `updated_at` moving past what we sent on, NOT the mere presence
    of a `last_response`: a resumed conversation already has the previous reply in
    that field, so keying on presence would return the *old* answer instantly and
    the user would think the agent had responded to a message it never saw.

    Returns the session state, with `timed_out` set when the bound was hit. Timing
    out is not a failure of the conversation — the turn is enqueued and the agent
    will still answer it — so the caller reports the session id to resume with
    rather than treating the work as lost.
    """
    for _ in range(INTAKE_MAX_POLLS):
        time.sleep(INTAKE_POLL_SECONDS)
        state = session_state(api, session_id)
        if state is None:
            continue
        completed = (
            state.get("last_response_task_id") == task_id
            if task_id and "last_response_task_id" in state
            else int(state.get("updated_at") or 0) > after
        )
        if not state.get("working") and completed:
            return state
    return {"timed_out": True, "session_id": session_id}


def turn_result(api, session_id, *, after, prompt_for_answer, task_id=None):
    """One round trip: wait for the agent, then show what came back.

    `prompt_for_answer` is False for a non-interactive caller. That distinction is
    the story's own requirement: a script must be told an answer is needed and
    exit, never block on a terminal nobody is watching and never invent an answer.
    """
    state = await_reply(api, session_id, after=after, task_id=task_id)
    if state.get("timed_out"):
        return None, (
            f"The planning agent has not replied yet. Nothing is lost — your message is queued and the "
            f"conversation is saved. Reattach with:\n  adp flow start --resume {session_id}"
        )

    reply = (state.get("last_response") or "").strip()
    if reply:
        progress("\n" + reply)
    draft = state.get("draft") or {}
    if draft:
        progress("\nThe plan so far:\n" + render_draft(draft))

    if not state.get("draft_available", True):
        # Said out loud rather than shown as an empty draft: a user told their plan
        # is blank when it is merely unreadable would start over and lose the
        # refinement they already did.
        progress("\nThe draft could not be read from this deployment, so what is shown above may be incomplete. The conversation itself is intact.")

    questions = open_questions(state)
    if questions and not prompt_for_answer:
        # Reported as structure AND as a resumable id, so a script can act. This
        # is `pending` (exit 4), not failure: the conversation is healthy and
        # waiting on a human, which is a different thing from broken.
        return state, (
            f"The planning agent needs these decided before it can continue:\n\n{render_questions(questions)}\n\n"
            f"Answer them interactively, or send the answer directly:\n"
            f"  adp flow start --resume {session_id}\n"
            f"Nothing has been registered, approved or started."
        )
    return state, None


def converse(api, session_id, *, interactive, first_state=None):
    """Drive the refinement conversation until the draft is settled or blocked.

    The loop is the point of the command. A plan good enough to approve does not
    come out of one sentence — the agent asks what repository, what the boundary
    is, what must not change — so the CLI has to be able to answer back, and each
    answer is a new revision of the same durable draft rather than a new
    conversation.

    Returns `(state, next_action)`; a non-None `next_action` means the loop stopped
    for a reason the caller must report instead of continuing.
    """
    state = first_state
    for _ in range(INTAKE_MAX_TURNS):
        questions = open_questions(state)
        if not questions:
            return state, None
        if not interactive:
            return state, (
                f"The planning agent needs these decided before it can continue:\n\n{render_questions(questions)}\n\n"
                f"Resume the conversation to answer them:\n  adp flow start --resume {session_id}\n"
                f"Nothing has been registered, approved or started."
            )
        progress("\n" + render_questions(questions))
        try:
            answer = input("> ").strip()
        except EOFError:
            # Input ended mid-conversation. Treated as a detach, not a cancel: the
            # conversation is durable and the id is how it is picked back up.
            return None, f"Input ended. The conversation is saved — resume it with:\n  adp flow start --resume {session_id}"
        if not answer:
            return None, f"No answer given, so nothing was sent and nothing was started. Resume with:\n  adp flow start --resume {session_id}"
        sent_at = int((state or {}).get("updated_at") or 0)
        retry_token = hashlib.sha256(json.dumps([session_id, state.get("last_response_task_id") or sent_at, answer]).encode()).hexdigest()
        progress(f"Sending planning turn {retry_token}.")
        sent = api.request("POST", f"{INTAKE}/{segment(session_id)}/turns", {"message": answer, "retry_token": retry_token})
        progress("\nThinking...")
        state, stop = turn_result(api, session_id, after=sent_at, prompt_for_answer=interactive, task_id=sent.get("task_id"))
        if stop:
            return state, stop
    return None, (
        f"Reached this command's turn limit ({INTAKE_MAX_TURNS}) with the plan still being refined. "
        f"The conversation is saved; continue it with:\n  adp flow start --resume {session_id}"
    )


def start_flow(args, api):
    """Refine a durable hosted conversation, preview a plan and request acceptance.

    Interactive starts continue to preview by default. Scripts use --plan and
    receive pending questions or an inert preview; approval requires --yes and
    the previously reviewed --expect-plan-hash.
    """
    interactive = sys.stdin.isatty() and not args.json
    args.plan = args.plan or (interactive and not args.refine_only)
    if args.plan:
        unavailable = require_engine(api, "flow start")
        if unavailable:
            return unavailable
    resuming = args.resume is not None
    detail = {"session_id": None, "resumed": resuming, "repo": args.repo, "issue": args.issue, "planned": bool(args.plan)}

    if resuming:
        # `--resume` with no value resolves the caller's newest conversation. An
        # empty string is that case, not an id to look up: sending "" would address
        # the collection path.
        state = session_state(api, args.resume or None)
        if state is None:
            raise CliError(
                "No planning conversation to resume. Start one with 'adp flow start' and a description of "
                "what you want; the session id it prints is what --resume takes.",
                "session_not_found",
                1,
            )
        session_id = state["session_id"]
        if state.get("repository"):
            if args.repo and args.repo.casefold() != state["repository"].casefold():
                raise CliError("This session belongs to another repository. Resume without changing --repo.", "repository_changed", 1)
            args.repo = state["repository"]
        if state.get("requested_issue"):
            if args.issue and str(args.issue) != state["requested_issue"]:
                raise CliError("This session belongs to another issue. Resume without changing --issue.", "issue_changed", 1)
            args.issue = int(state["requested_issue"])
        detail.update(session_id=session_id, repo=args.repo, issue=args.issue)
        progress(f"Resumed planning session {session_id}.")
        if state.get("working"):
            # The agent still holds the turn. Waiting is right; sending would
            # interleave with the answer being written and the server refuses it.
            progress("The planning agent is still working on the last message...")
            state, stop = turn_result(api, session_id, after=0, prompt_for_answer=interactive)
            if stop:
                return common.envelope("pending", "flow start", detail, stop)
        elif state.get("last_response"):
            progress("\n" + state["last_response"].strip())
        if state.get("draft"):
            progress("\nThe plan so far:\n" + render_draft(state["draft"]))
    else:
        outcome = args.outcome or ""
        if not outcome.strip() and interactive:
            progress("What outcome would you like this flow to deliver?")
            try:
                outcome = input("> ").strip()
            except EOFError:
                outcome = ""
        if not outcome.strip():
            raise CliError(
                'Describe what you want, in your own words: adp flow start "Add per-tenant rate limiting to the '
                'public API". Add --repo OWNER/NAME to say where it lands, --issue NUMBER to start from an '
                "existing issue, or --resume to continue a conversation you already began.",
                "usage_error",
                1,
            )
        opening = outcome
        if args.repo:
            opening += f"\n\nThis should land in the repository {args.repo}."
        if args.issue:
            opening += f"\n\nStart from issue #{args.issue} in that repository."

        request_id = args.request_id or uuid.uuid4().hex
        detail["request_id"] = request_id
        progress(f"Opening planning request {request_id}. If delivery is interrupted, retry with --request-id {request_id}.")
        started = api.request(
            "POST",
            INTAKE,
            {"message": opening, "repository": args.repo, "issue": str(args.issue) if args.issue else None, "retry_token": request_id},
        )
        session_id = started["session_id"]
        detail["session_id"] = session_id
        # BEFORE the wait, deliberately. See the docstring.
        progress(f"Planning session {session_id} started. Resume it any time with: adp flow start --resume {session_id}")
        progress("\nThinking...")
        state, stop = turn_result(api, session_id, after=0, prompt_for_answer=interactive, task_id=started.get("task_id"))
        if stop:
            detail["session"] = state
            return common.envelope("pending", "flow start", detail, stop)

    if resuming and args.answer:
        request_id = args.request_id or uuid.uuid4().hex
        detail["request_id"] = request_id
        progress(f"Sending answer {request_id}. Retry this answer with --request-id {request_id} if delivery is interrupted.")
        sent = api.request("POST", f"{INTAKE}/{segment(session_id)}/turns", {"message": args.answer, "retry_token": request_id})
        state, stop = turn_result(
            api, session_id, after=int((state or {}).get("updated_at") or 0), prompt_for_answer=interactive, task_id=sent.get("task_id")
        )
        if stop:
            detail["session"] = state
            return common.envelope("pending", "flow start", detail, stop)
    state, stop = converse(api, session_id, interactive=interactive, first_state=state)
    if stop:
        detail["session"] = state
        return common.envelope("pending", "flow start", detail, stop)

    detail["draft"] = (state or {}).get("draft") or {}
    detail["issue_ref"] = (state or {}).get("issue_ref") or ""
    # Carried into the machine-readable envelope, not only printed to stderr: a
    # `--json` consumer reads stdout, and a caller that stored `detail["draft"]`
    # as the refined plan needs to know when it is empty because the store could
    # not be read rather than because nothing was captured.
    detail["draft_available"] = bool((state or {}).get("draft_available", True))

    if not args.plan:
        # The conversation alone. Still the default, because refining an intent and
        # authorizing autonomous work are different acts and the second one should
        # be asked for.
        return common.envelope("ok", "flow start", detail, _READY_TO_PLAN.format(session_id=session_id))

    # --- from here on: --plan was asked for ---------------------------------
    #
    # The document is derived by the SERVER from the draft this conversation
    # produced. Nothing about the plan is assembled here: see `INTAKE_PLAN`.
    progress("\nAsking ADP to derive a plan from this intent. This writes nothing.")
    derived = api.request(
        "POST",
        INTAKE_PLAN.format(session_id=segment(session_id)),
        # `--repo` goes to the server to be RESOLVED against the caller's real
        # installations, not concatenated into the opening message as prose for an
        # agent to interpret. A repository name decides where autonomous work
        # lands, so it is checked, and an unconnected one is refused here rather
        # than at dispatch on a plan a human already approved.
        {"repository": args.repo or None, "issue": str(args.issue) if args.issue else None, "flow_slug": None},
    )
    document = derived.get("proposal") or {}
    if derived.get("prerequisites"):
        return common.envelope(
            "pending",
            "flow start",
            {**detail, "proposal": document, "prerequisites": derived["prerequisites"], "accepted": False},
            "Planning prerequisites: " + " ".join(derived["prerequisites"]) + " Nothing was registered or approved.",
        )
    detail["derived"] = {
        "derived_from_outcomes": derived.get("derived_from_outcomes"),
        "repository": derived.get("repository"),
        "repository_verified_live": derived.get("repository_verified_live"),
        "issue_ref": derived.get("issue_ref"),
        # The server's own statement that deriving wrote nothing. Echoed rather
        # than assumed, so a deployment that changed that would show up here.
        "wrote_nothing": derived.get("wrote_nothing"),
    }
    if not document.get("nodes"):
        raise CliError(
            "ADP derived a plan with no work in it, so there is nothing to register or approve. Keep refining the "
            f"intent — say what observable results would mean this worked: adp flow start --resume {session_id}",
            "empty_plan",
            4,
        )

    if derived.get("repository") and derived.get("repository_verified_live") is False:
        # Weaker evidence, stated before consent rather than after. A stale
        # snapshot match means the repository was connected when ADP last looked,
        # which is not the same as now.
        progress(
            f"Note: {derived['repository']} was matched against ADP's stored list of your repositories, not a live "
            "check, so its access could have changed since."
        )

    if not document.get("proposed_execution_policy") and not document.get("execution_policy"):
        return common.envelope(
            "pending",
            "flow start",
            {**detail, "proposal": document, "accepted": False},
            f"Choose a connected --repo to propose a bounded execution policy, then resume session {session_id} with --plan. "
            "Nothing was registered or approved.",
        )
    accepted = register_preview_accept(api, document, verb="flow start", reason=args.reason, assume_yes=args.yes, expected_hash=args.expect_plan_hash)
    # Merge rather than replace: the conversation's own record — session id, draft,
    # what the derivation resolved — is what makes the resulting flow traceable back
    # to the intent a person actually described.
    accepted["detail"] = {**detail, **(accepted.get("detail") or {})}
    return accepted


# --- dispatch --------------------------------------------------------------


def parser():
    root = common.Parser(prog="adp flow", description="Follow and control AI-DLC delivery flows.")
    commands = root.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start", help="Describe an outcome and refine it into a plan with the planning agent")
    # Positional and optional: `--resume` needs no outcome, and requiring a
    # placeholder string to reattach to a conversation would be a papercut on the
    # command a dropped connection sends you back to.
    start.add_argument(
        "outcome",
        nargs="?",
        metavar="OUTCOME",
        help='What you want, in your own words: "Add per-tenant rate limiting to the public API"',
    )
    start.add_argument("--repo", metavar="OWNER/NAME", help="The repository the flow delivers into")
    start.add_argument("--issue", type=int, metavar="NUMBER", help="Use an existing issue in that repository as context")
    start.add_argument("--answer", help="Send an answer to a resumed planning session, including from a script")
    start.add_argument("--request-id", help="Stable idempotency token for retrying the same opening message or --answer")
    start.add_argument(
        "--resume",
        nargs="?",
        const="",
        metavar="SESSION_ID",
        help="Reattach to a planning conversation. With no id, resumes your most recent one",
    )
    start.add_argument(
        "--plan",
        action="store_true",
        help="After refining, have ADP derive a plan from this intent, preview its effective graph, and ask you to accept it",
    )
    start.add_argument("--refine-only", action="store_true", help="Stop after intent refinement instead of continuing to a plan preview")
    start.add_argument("--expect-plan-hash", metavar="HASH", help="Previously reviewed preview hash; required with --yes")
    start.add_argument("--reason", help="Why this plan is being approved (recorded on the decision, with --plan)")
    start.add_argument(
        "--yes",
        action="store_true",
        help="With --plan and --expect-plan-hash, accept the previously reviewed revision without a prompt",
    )
    start.add_argument("--json", action="store_true", help="Print machine-readable output")

    create = commands.add_parser("create", help="Register a plan document as an inert draft, preview it, then accept it")
    create.add_argument("--file", required=True, metavar="PATH", help="A plan document (JSON)")
    create.add_argument("--expect-plan-hash", metavar="HASH", help="Previously reviewed preview hash; required with --yes")
    create.add_argument("--reason", help="Why this plan is being approved (recorded on the decision)")
    create.add_argument(
        "--yes",
        action="store_true",
        help="With --expect-plan-hash, accept the previously reviewed revision without a prompt",
    )
    create.add_argument("--json", action="store_true", help="Print machine-readable output")

    draft = commands.add_parser("draft", help="Preview or save an existing unapproved draft without starting execution")
    draft_actions = draft.add_subparsers(dest="draft_action", required=True)
    for verb in ("preview", "save"):
        edit = draft_actions.add_parser(verb, help=f"{verb.title()} a revision of the same inert flow")
        edit.add_argument("flow_id", metavar="FLOW_ID")
        edit.add_argument("--file", required=True, metavar="PLAN_JSON", help="Authored proposal; omit the server-inserted acceptance gate")
        edit.add_argument("--expect-plan-version", required=True, type=int, metavar="N")
        edit.add_argument("--expect-plan-hash", required=True, metavar="HASH")
        if verb == "save":
            edit.add_argument("--expect-proposal-hash", required=True, metavar="HASH", help="Effective proposal hash returned by draft preview")
        edit.add_argument("--reason", help="Reason recorded with the draft revision")
        edit.add_argument("--json", action="store_true", help="Print machine-readable output")

    listing = commands.add_parser("list", help="List the flows you can see")
    listing.add_argument("--status", choices=FLOW_STATUSES, help="Only flows with this status")
    listing.add_argument("--needs-me", action="store_true", help="Only flows waiting on a decision from you")
    listing.add_argument("--limit", type=int, default=25, metavar="N", help="Page size (1-100)")
    listing.add_argument("--offset", type=int, default=0, metavar="N", help="Skip N flows")
    listing.add_argument("--json", action="store_true", help="Print machine-readable output")

    # Every flow-scoped read takes the same positional, so they are built in a
    # loop: a divergent spelling between `show` and `watch` is a papercut a user
    # hits on their second command.
    for verb, help_text in (
        ("show", "Show progress, blockers, outstanding gates and next eligible work"),
        ("watch", "Follow a flow; exiting detaches and leaves hosted work running"),
        ("plans", "Read plan versions and the current proposal"),
        ("decisions", "Read attributed approvals and transitions"),
        ("cost", "Show recorded cost, preserving unknown/none/known"),
    ):
        command = commands.add_parser(verb, help=help_text)
        command.add_argument("flow_id", metavar="FLOW_ID")
        command.add_argument("--json", action="store_true", help="Print machine-readable output")
        if verb == "watch":
            command.add_argument("--once", action="store_true", help="Read the current state once and exit")

    gate = commands.add_parser("gate", help="Answer a decision the engine is waiting on")
    gate_actions = gate.add_subparsers(dest="gate_action", required=True)
    for action, help_text in (("approve", "Approve a gate"), ("reject", "Reject a gate")):
        answer = gate_actions.add_parser(action, help=help_text)
        answer.add_argument("gate_id", metavar="GATE_ID")
        answer.add_argument("--reason", help="Recorded on the decision")
        answer.add_argument("--json", action="store_true", help="Print machine-readable output")
        # Offered on BOTH verbs. The server applies the precondition to a rejection
        # exactly as it does to an approval, on the ground that recording a decision
        # against a revision the operator did not read misattributes a rejection just
        # as badly as an approval. Exposing it only on `approve` left that server
        # behaviour unreachable from the CLI for half the decisions it governs.
        answer.add_argument(
            "--expect-plan-hash",
            metavar="HASH",
            help=(
                f"Refuse unless this is still the flow's current plan revision. Sent as a server-side "
                f"precondition, compared inside the same transaction that {'approves' if action == 'approve' else 'rejects'} the gate"
            ),
        )
        answer.add_argument("--yes", action="store_true", help="State approval without a prompt, for scripts")
    return root


HANDLERS = {
    "start": start_flow,
    "create": create_flow,
    "draft": revise_draft,
    "list": list_flows,
    "show": show_flow,
    "watch": watch_flow,
    "plans": list_plans,
    "decisions": list_decisions,
    "cost": flow_cost,
    "gate": answer_gate,
}


def run(args, api):
    return HANDLERS[args.command](args, api)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv
    command = "flow " + (argv[0] if argv and not argv[0].startswith("-") else "list")
    try:
        args = parser().parse_args(argv)
        if getattr(args, "answer", None) and args.resume is None:
            raise CliError("Use --answer with --resume SESSION_ID.", "usage_error", 1)
        if getattr(args, "repo", None) and (
            not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo) or any(part in {".", ".."} for part in args.repo.split("/"))
        ):
            raise CliError("Use --repo OWNER/NAME.", "usage_error", 1)
        if getattr(args, "issue", None) is not None and args.issue < 1:
            raise CliError("Use a positive --issue number.", "usage_error", 1)
        if getattr(args, "request_id", None) is not None and not 1 <= len(args.request_id.strip()) <= 128:
            raise CliError("Use a nonempty --request-id of at most 128 characters.", "usage_error", 1)
        # Validate the caller's OWN arguments before resolving a gateway, so a
        # malformed id is reported as the usage error it is. Building Api() first
        # makes a typo complain about gateway configuration and sends the user to
        # reinstall the CLI over something they can fix in the command they typed
        # — the same ordering adp-github.py uses for --repo.
        # Presence, not truthiness: an EMPTY flow id is precisely the input that
        # must be rejected, and `if args.flow_id:` skipped validation for it and
        # sent a request to the collection path instead.
        if hasattr(args, "flow_id"):
            args.flow_id = flow_id_argument(args.flow_id)
        if getattr(args, "limit", None) is not None and not 1 <= args.limit <= 100:
            raise CliError("Use --limit between 1 and 100.", "usage_error", 1)
        if getattr(args, "offset", None) is not None and args.offset < 0:
            raise CliError("Use --offset 0 or greater.", "usage_error", 1)
        return common.emit(run(args, LazyApi()), args.json)
    except (CliError, OSError, ValueError, KeyError, TypeError) as exc:
        return common.report_error(exc, command, as_json)
    except KeyboardInterrupt:
        # Interrupting is a DETACH, and saying so is the point. A user who
        # believes Ctrl-C cancelled hosted delivery will not go looking for the
        # work that is still running, and may approve or start it a second time.
        return common.report_error(CliError(DETACHED, "interrupted", 130), command, as_json)


if __name__ == "__main__":
    sys.exit(main())
