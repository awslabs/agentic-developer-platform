"""Register an authored loop proposal with the orchestration engine as a draft.

Issue #4528 (EPIC #4191, intent #4120), the worker half of the engine bridge. The
AIDLC persona finishes by committing its delivery-loop drafts to the branch; this
module takes the machine-readable proposal it emitted alongside them and POSTs it
to the gateway, which stores it as an inert draft plan a human can see in the
graph UI and accept with one comment.

--------------------------------------------------------------------------------
Fail-soft is the whole contract
--------------------------------------------------------------------------------

Registration is an *addition* to a run whose real output — the wave-map and the
issue drafts on the branch — is already committed by the time this runs. So a
registration that fails must cost the run nothing: `draft_registration_note`
never raises and never returns a non-zero anything. Every failure path
(no artifact, no tenant, HTTP error, unreachable gateway, unparseable response)
returns a *warning note* that the caller appends to the closing comment, and the
run completes exactly as it would have before this module existed.

That is the issue's third named bug class ("compile failure kills the AIDLC run"),
and it is prevented structurally rather than by care: the caller receives a string,
so there is no exception for it to mishandle and no exit code for it to propagate.

--------------------------------------------------------------------------------
Transport
--------------------------------------------------------------------------------

SigV4 through API Gateway's ``/agent/{proxy+}`` route, which is the machine path
into the gateway's **operator plane**: the route authorises with AWS_IAM, injects
``X-Caller-Identity``, and the gateway resolves that ARN in the agent registry to a
service principal with a registry-derived ``org_id``. Worker IAM already grants
``execute-api:Invoke`` on ``*/*/*/agent/*``, so no terraform accompanies this.

Deliberately NOT ``/internal/v1/*`` (the path `gateway_credential_client` uses):
agent pods may call any internal-plane route with any method, so a registration
endpoint there would need no permission at all. `docs/design-notes/
4303-engine-genesis-transport.md` rejects the internal plane for orchestration
explicitly. The ``/agent`` segment is therefore required here and forbidden there —
the two are different planes, not two spellings of one.

--------------------------------------------------------------------------------
Tenant
--------------------------------------------------------------------------------

``org_id`` on the document is overwritten from ``ADP_TENANT_ID`` — the tenant the
webhook resolved for this run and exported during bootstrap — and never read from
the artifact, which the agent authored and could therefore have named any tenant
in. The gateway compares the declared org against its own resolved one and rejects
a mismatch, so a forged value would be refused there too; overwriting it here means
the request the worker sends is *correct*, not merely caught.

Issue #4597 adds ``X-Agent-RunId``, and it is what makes that comparison *pass* for
a real tenant. The gateway resolves this pod as the shared ``scaledjob-worker``
registry entry, whose ``org_id`` is the literal ``__platform__`` — so before #4597
the gateway's "own resolved one" was never the run's tenant and every real-tenant
registration was refused with a 422. The header carries the run's envelope
``message_id``, which is the partition key of the ``webhook-events`` row
webhook-ingress wrote at ingress; the gateway reads the tenant off that row.

Note what this header is and is not. It is a **reference** to a row the server
wrote, not an assertion of identity or of tenant — the worker is not trusted to
name its own tenant, and the ``X-Agent-OrgId`` attribution header is deliberately
NOT what the gateway reads (it is caller-influenced, and the #4132 invariant
forbids it gating access). It is signed as part of the SigV4 request rather than
appended afterwards, so it cannot be rewritten in flight.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from lib.amendment_input import AMENDMENT_BASE_PATH_ENV

logger = logging.getLogger(__name__)

__all__ = [
    "AMENDMENT_ARTIFACT_TEMPLATE",
    "AMENDMENT_BASE_HASH_ENV",
    "AMENDMENT_BASE_PATH_ENV",
    "AMENDMENT_BASE_VERSION_ENV",
    "AMENDMENT_OUTPUT_PATH_ENV",
    "AMENDMENT_REQUEST_ENV",
    "AMENDMENT_REQUEST_TEXT_ENV",
    "DISABLED_ENV",
    "EngineRegistrationError",
    "FLOW_ID_ENV",
    "amendment_artifact_path",
    "amendment_registration_note",
    "authoring_assignment",
    "draft_registration_note",
    "proposal_artifact_path",
    "register_amendment_proposal",
    "register_loop_proposal",
    "registration_disabled",
]


# Kill switch. Set it and the run behaves exactly as it did before this story:
# artifacts committed, no HTTP call, nothing appended to the closing comment.
DISABLED_ENV = "ADP_ENGINE_REGISTRATION_DISABLED"

_TRUE_SPELLINGS = frozenset({"1", "true", "yes", "on"})

# Where the AIDLC skill emits the machine-readable proposal, relative to the repo
# root — the same directory as the wave-map and the issue drafts it accompanies.
# Produced by `modules/agent-factory/skills/aidlc-emit-issues/SKILL.md` Step 7e,
# which is the only writer of this path; if that step is ever removed or renamed,
# this whole module becomes unreachable dead code (as it was when first shipped —
# found in review, PR #4558) and the loop silently stays markdown-only.
_ARTIFACT_TEMPLATE = "aidlc/spaces/issue-{issue}/construction/loop-proposal/proposal.json"

# The operator-plane draft-registration route, behind API Gateway's /agent proxy.
_DRAFT_PATH = "/agent/orchestration/flows/drafts"

_TIMEOUT_SECONDS = 30

# ---------------------------------------------------------------------------------
# The amendment half (#4529)
# ---------------------------------------------------------------------------------
#
# An *amendment* is not a new flow. The route, the artifact and the authorization
# are all different, which is why they get their own names rather than a flag on the
# ones above:
#
#   new flow    POST /agent/orchestration/flows/drafts
#               authorized by holding PLAN_DRAFT; the author chooses the flow slug.
#
#   amendment   POST /agent/orchestration/flows/{flow_id}/amendments/drafts?request_id=...
#               authorized by the server having COMMISSIONED this run for this
#               request. `resolve_authoring_request` refuses unless the presented
#               `X-Agent-RunId` equals the `author_run_id` the server wrote onto the
#               assignment when a verified human commented `replan:`. Holding the
#               route's permission is not sufficient.
#
# So the two ids below are not decoration and are not caller-chosen: they are the
# server's own record of what this run was summoned to do, read from the dispatch
# envelope during bootstrap and never from the model's output. An authoring run
# cannot amend a plan nobody asked it to touch, because it cannot name an assignment
# it was not given — the request id is not secret, but the binding to this run is.

#: The assignment this run was commissioned for, exported by the entrypoint from the
#: envelope's server-written `orchestration.request_id`. Absent for every run that is
#: not an amendment-authoring run, which is what keeps this module inert by default.
AMENDMENT_REQUEST_ENV = "ADP_AMENDMENT_REQUEST_ID"

#: The flow being amended, from the envelope's `orchestration.flow_id`. Sent in the
#: path, where the server treats it as a CHECK against the assignment's own flow
#: rather than as the target — a mismatch is refused, not reconciled.
FLOW_ID_ENV = "ADP_FLOW_ID"

#: Where an authoring run writes the amended plan. Keyed on the request id, not the
#: issue: one issue can carry several `replan:` asks over a flow's life, and a path
#: keyed on the issue would make the second one overwrite the first — then register
#: whichever file happened to be on disk against whichever assignment was live.
AMENDMENT_ARTIFACT_TEMPLATE = "aidlc/spaces/amendments/{request_id}/proposal.json"

# --- The authoring brief (the producer half of the contract above) ----------------
#
# The four names below are why a commissioned author knows what to do. The two ids
# above identify the assignment; these describe the *job*, and without them a run
# that was correctly summoned, correctly authorized and correctly bound still has no
# idea what it was summoned for. Review of the first cut of #4529 found exactly that:
# this module read an artifact path no instruction anywhere told an author to write,
# so a real authoring run would have followed its ordinary planning instructions,
# opened a flow nobody asked for and filed nothing.
#
# All four are exported by the entrypoint from the *dispatch envelope* and nowhere
# else, alongside the two ids and under the same both-or-neither rule. The request
# text is the human's words carried as DATA for the author to consider; nothing in
# this platform executes it, and the server capped it at 2000 characters
# (`github_commands.REPLAN_TEXT_MAX_LEN`) before it was ever stored.
#
# They are deliberately NOT read by this module. `register_amendment_proposal` needs
# the assignment, not the brief — the server re-checks the base revision against its
# own record, so a run that lied about its base version would be refused rather than
# believed. They exist for the authoring *instructions* to consume, which is why they
# are defined here (the module that owns the amendment contract) rather than in the
# entrypoint that happens to export them: the instruction text and this file must
# agree on the names, and a contract test pins that they do.

#: The human's `replan:` words, verbatim and bounded. Data to consider, never executed.
AMENDMENT_REQUEST_TEXT_ENV = "ADP_AMENDMENT_REQUEST_TEXT"

#: The accepted plan version the amendment is authored against. Carried so the author
#: amends what the human was looking at rather than re-reading and possibly seeing a
#: different version.
AMENDMENT_BASE_VERSION_ENV = "ADP_AMENDMENT_BASE_VERSION"

#: The hash of that same accepted version. The server compares it on accept and
#: returns a conflict if the plan moved underneath the draft.
AMENDMENT_BASE_HASH_ENV = "ADP_AMENDMENT_BASE_HASH"

#: The absolute path this run must write its authored amendment to — the same path
#: `amendment_artifact_path` reads. Exported rather than left to the author to compose
#: so that the producer and the consumer cannot disagree about it: an author that
#: composes the path itself can get it subtly wrong (the issue-keyed shape is the
#: obvious wrong guess) and the failure is a silent "no artifact found".
AMENDMENT_OUTPUT_PATH_ENV = "ADP_AMENDMENT_OUTPUT_PATH"

#: Composed rather than a constant because the flow and the request are both
#: per-assignment. `{request_id}` goes in the query string because that is where the
#: route declares it (`Query(min_length=1)`), and it is required: there is no
#: "amend the flow" form without an assignment.
_AMENDMENT_PATH_TEMPLATE = "/agent/orchestration/flows/{flow_id}/amendments/drafts"

#: What the route reports for every successful registration, unconditionally. Pinned
#: here so the worker's own note cannot claim anything stronger than the server said.
_PENDING_HUMAN_ACCEPT = "pending_human_accept"


class EngineRegistrationError(RuntimeError):
    """Registration did not happen. Always caught before it reaches the run."""


def registration_disabled() -> bool:
    """Whether the kill switch is set.

    Only an explicit truthy spelling disables registration: the default has to be
    "register", because a typo'd flag value silently reverting the whole story to
    markdown-only would be indistinguishable from the feature not being deployed.
    """
    raw = os.environ.get(DISABLED_ENV, "")
    return raw.strip().lower() in _TRUE_SPELLINGS


def proposal_artifact_path(work_dir: Path, issue: int) -> Path:
    """The path the AIDLC skill emits its `proposal.json` to."""
    return work_dir / _ARTIFACT_TEMPLATE.format(issue=issue)


def amendment_artifact_path(work_dir: Path, request_id: str) -> Path:
    """Where an authoring run writes the amended plan for one assignment.

    Keyed on the request id for the reason given at `AMENDMENT_ARTIFACT_TEMPLATE`:
    two `replan:` asks on one issue must not share a file.
    """
    return work_dir / AMENDMENT_ARTIFACT_TEMPLATE.format(request_id=request_id)


def authoring_assignment() -> tuple[str, str] | None:
    """This run's `(flow_id, request_id)`, or None if it was not commissioned to amend.

    Both or neither. A half-present pair means the envelope carried an `orchestration`
    block the entrypoint exported incompletely, and the honest response is to treat
    the run as not-an-amendment rather than to guess the missing half: with no
    `request_id` there is no assignment to authorize against, and with no `flow_id`
    there is no path to send it to. Either way the request would be refused by the
    server — reporting "not an amendment run" here is the same outcome without a
    pointless round trip and without a warning that names the wrong cause.

    Returns None rather than raising because "this is not an amendment run" is the
    normal case for every other persona and every webhook trigger.
    """
    flow_id = os.environ.get(FLOW_ID_ENV, "").strip()
    request_id = os.environ.get(AMENDMENT_REQUEST_ENV, "").strip()
    if not flow_id or not request_id:
        return None
    return flow_id, request_id


def _sigv4_sign_request(method: str, url: str, headers: dict, data: bytes | None) -> dict:
    """Sign a request with SigV4 using the pod's IRSA credentials.

    Same shape as `lib/gateway_credential_client.py::_sigv4_sign_request` and
    `lib/provenance_client.py`'s copy. Not imported from either: both are private
    helpers of clients for a different plane, and importing one would couple this
    module's transport to a client whose base URL must NOT carry `/agent`.
    """
    import botocore.auth
    import botocore.awsrequest
    import botocore.session

    session = botocore.session.get_session()
    credentials = session.get_credentials()
    if credentials is None:
        raise EngineRegistrationError("no AWS credentials available for SigV4 signing")
    credentials = credentials.get_frozen_credentials()

    aws_request = botocore.awsrequest.AWSRequest(method=method, url=url, headers=headers, data=data)
    region = os.environ.get("AWS_REGION", "us-east-1")
    botocore.auth.SigV4Auth(credentials, "execute-api", region).add_auth(aws_request)

    return dict(aws_request.headers)


def _load_document(path: Path, *, tenant_id: str, intent_ref: str | None) -> dict[str, Any]:
    """Read the emitted artifact and apply the server-owned fields.

    `intent_ref=None` means "this path has no fallback to offer" (the amendment
    case), which is distinct from `""`: the field is `str | None` on the wire with no
    `min_length`, so writing an empty string would store a present-but-blank intent
    where the schema's own way of saying "unknown" is absence. An author-declared
    value is kept either way.

    Raises:
        EngineRegistrationError: The artifact is missing, unreadable, not JSON, or
            not a JSON object.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EngineRegistrationError(f"proposal artifact not readable at {path}: {exc}") from exc

    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EngineRegistrationError(f"proposal artifact at {path} is not valid JSON: {exc}") from exc

    if not isinstance(document, dict):
        raise EngineRegistrationError(f"proposal artifact at {path} must be a JSON object, got {type(document).__name__}")

    # Never the artifact's own value — see the module docstring.
    document["org_id"] = tenant_id
    # The originating intent issue, so the engine can post plan status back to the
    # conversation this plan came out of. An author who named it keeps their value;
    # the run's issue is the fallback, not an override, because a proposal may
    # legitimately be authored on one issue for an intent tracked on another.
    if intent_ref and not document.get("intent_ref"):
        document["intent_ref"] = intent_ref

    return document


def register_loop_proposal(*, work_dir: Path, issue: int, timeout: int = _TIMEOUT_SECONDS) -> dict[str, Any]:
    """POST the emitted proposal to the gateway and return the parsed response.

    Args:
        work_dir: The cloned repo root.
        issue: The run's issue number — locates the artifact and supplies the
            fallback `intent_ref`.
        timeout: Per-request timeout in seconds.

    Returns:
        The gateway's `DraftRegisteredResponse` as a dict.

    Raises:
        EngineRegistrationError: On any failure. Callers use
            `draft_registration_note`, which turns this into a comment.
    """
    endpoint_base, tenant_id, run_id = _run_context()
    document = _load_document(proposal_artifact_path(work_dir, issue), tenant_id=tenant_id, intent_ref=str(issue))

    logger.info(
        "Registering draft plan: flow=%s nodes=%s edges=%s intent=%s",
        document.get("flow_slug"),
        len(document.get("nodes") or []),
        len(document.get("edges") or []),
        document.get("intent_ref"),
    )
    return _post_document(f"{endpoint_base}{_DRAFT_PATH}", document, run_id=run_id, endpoint_base=endpoint_base, timeout=timeout)


def _run_context() -> tuple[str, str, str]:
    """The three server-owned values every registration needs: endpoint, tenant, run.

    Extracted so the amendment path cannot drift from the new-flow path on any of
    them. Each is reported as a missing *envelope/deployment* field rather than sent
    blank, because the gateway's refusal would name the document or the header and an
    operator reading the closing comment needs to know the pod had nothing to send.
    """
    endpoint_base = os.environ.get("ADP_GATEWAY_ENDPOINT", "").rstrip("/")
    if not endpoint_base:
        raise EngineRegistrationError("ADP_GATEWAY_ENDPOINT is not set; no gateway to register with")

    tenant_id = os.environ.get("ADP_TENANT_ID", "").strip()
    if not tenant_id:
        raise EngineRegistrationError("no ADP_TENANT_ID in env; refusing to register a plan with no tenant")

    # Issue #4597: the run this registration is on behalf of. `ADP_MESSAGE_ID` is the
    # envelope `message_id` (exported during bootstrap, long before this runs), which
    # is the `event_id` partition key of the run's own `webhook-events` row — the row
    # the gateway reads the owning tenant off. NOT the SQS MessageId and not the KEDA
    # pod name, both of which are also called some spelling of "run id" and neither of
    # which the gateway can resolve.
    #
    # For an amendment (#4529) it carries a second, heavier meaning: it is the
    # `author_run_id` the server wrote onto the assignment, and the route refuses
    # unless the two are equal. So this is the *binding*, not just attribution.
    run_id = os.environ.get("ADP_MESSAGE_ID", "").strip()
    if not run_id:
        raise EngineRegistrationError("no ADP_MESSAGE_ID in env; refusing to register a plan with no run to bind it to")

    return endpoint_base, tenant_id, run_id


def _post_document(url: str, document: dict[str, Any], *, run_id: str, endpoint_base: str, timeout: int) -> dict[str, Any]:
    """Sign, POST, and parse. The one transport both registration paths share."""
    data = json.dumps(document).encode("utf-8")
    # Inside the signed header set, deliberately — see the module docstring.
    headers = _sigv4_sign_request("POST", url, {"Content-Type": "application/json", "X-Agent-RunId": run_id}, data)

    request = Request(url, data=data, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8") if exc.fp else ""
        raise EngineRegistrationError(f"gateway returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise EngineRegistrationError(f"cannot reach gateway at {endpoint_base}: {exc.reason}") from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise EngineRegistrationError(f"gateway response was not JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise EngineRegistrationError(f"gateway response must be a JSON object, got {type(parsed).__name__}")

    return parsed


def register_amendment_proposal(*, work_dir: Path, flow_id: str, request_id: str, timeout: int = _TIMEOUT_SECONDS) -> dict[str, Any]:
    """POST the authored amendment as a pending draft and return the parsed response.

    The amendment counterpart of `register_loop_proposal`. Three things differ, and
    all three are the authorization rather than the payload: the flow is in the path,
    the assignment is in the query string, and the run id header must equal the
    `author_run_id` the server bound to that assignment.

    `intent_ref` is deliberately NOT supplied here. On the new-flow path it tells the
    engine which conversation to report into; an amendment already belongs to a flow
    that has one, and the request row records the human who asked. Inventing one from
    this run's issue would attribute the amendment to whatever issue the authoring
    run happened to execute on.

    Raises:
        EngineRegistrationError: On any failure, including a missing artifact.
            Callers use `amendment_registration_note`.
    """
    endpoint_base, tenant_id, run_id = _run_context()
    # `None`, not `""` — see `_load_document`. An author-declared intent survives; an
    # absent one stays absent rather than becoming a blank string.
    document = _load_document(amendment_artifact_path(work_dir, request_id), tenant_id=tenant_id, intent_ref=None)

    path = _AMENDMENT_PATH_TEMPLATE.format(flow_id=quote(flow_id, safe=""))
    url = f"{endpoint_base}{path}?request_id={quote(request_id, safe='')}"

    logger.info(
        "Registering amended plan: flow=%s request=%s nodes=%s edges=%s gates=%s",
        flow_id,
        request_id,
        len(document.get("nodes") or []),
        len(document.get("edges") or []),
        sum(1 for node in (document.get("nodes") or []) if isinstance(node, dict) and node.get("kind") == "gate"),
    )
    return _post_document(url, document, run_id=run_id, endpoint_base=endpoint_base, timeout=timeout)


def _success_note(result: dict[str, Any]) -> str:
    """The closing-comment section for a registered draft.

    The accept instruction is quoted from the gateway's own `accept_command`
    rather than spelled here. The human reading this comment types that string
    back, and a copy of it in the worker could drift from the parser in the
    gateway — leaving a human following a working instruction that does nothing.

    **The command goes in a fenced block, not inline backticks (#4599).** This note
    is posted on every successful registration, and an inline `@agent-engine accept`
    used to be read by the tick as a live command: marked pending, parsed, refused
    (this comment's author is a bot with no `PLAN_APPROVE`), and answered with
    "this command cannot be applied by this account". Every registration produced
    that reply — the feature's own success message triggering the feature. A fence
    is ignored by the parser's code-awareness rule while staying copy-pasteable,
    which is the property the human actually needs from this line.

    **The label is "Flow", and the id is a link when the gateway gives us one
    (#4885).** It said "Plan" over a `flow_id`, directly above a line promising the
    thing was "visible in the graph UI" — with no address for that UI anywhere in
    the comment. The one artifact the reader needed, they had to already know how to
    find, and the label pointed at the wrong noun while they looked. `flow_url` is
    composed by the gateway (`draft_routes._flow_url`) because only it knows the
    user-facing origin: this worker's `ADP_GATEWAY_ENDPOINT` is the API Gateway
    invoke URL, and pasting that would hand an operator a link to the machine plane.
    When the gateway sends no URL the id still prints bare — a missing link is a
    degraded comment, never a missing plan.
    """
    accept_command = result.get("accept_command") or "@agent-engine accept"
    already = result.get("already_registered")

    flow_id = result.get("flow_id")
    flow_url = result.get("flow_url")
    # A Markdown link only for an absolute URL. A relative one resolves against
    # github.com and 404s, which reads as a broken feature rather than an absent link.
    flow_ref = f"[`{flow_id}`]({flow_url})" if flow_url else f"`{flow_id}`"

    lines = [
        "### Delivery loop registered with the orchestration engine",
        "",
        f"**Flow**: {flow_ref} (v{result.get('plan_version')}) — "
        f"{result.get('nodes_created')} nodes, {result.get('edges_created')} edges",
        "**State**: `draft` — the plan is visible in the graph UI and executes nothing.",
        f"**Acceptance gate**: `{result.get('acceptance_gate_address')}`",
        "",
        "Reply with the following to start execution:",
        "",
        "```",
        accept_command,
        "```",
    ]
    if already:
        lines.extend(["", "_This document was already registered; the existing plan is unchanged._"])
    return "\n".join(lines)


def _warning_note(reason: str) -> str:
    """The closing-comment section for a registration that did not happen.

    A warning, not a failure, and it says so — the committed artifacts are the
    source of truth either way, and an operator who reads "failed" where nothing
    was lost will go looking for damage that does not exist.
    """
    return "\n".join(
        [
            "### ⚠️ Delivery loop not registered with the orchestration engine",
            "",
            f"Registration was skipped: {reason}",
            "",
            "The wave-map and issue drafts committed on this branch are unaffected and remain "
            "the source of truth. Nothing was lost, and nothing is executing.",
        ]
    )


def _gate_diff_line(gate_diff: Any) -> str:
    """One line describing what the amendment does to the plan's gates.

    The gate diff is the whole point of the amendment for the human reading this:
    `replan:` asks are overwhelmingly "put a gate before the deploy wave", and
    `changes_gating` is the server's own answer to "does accepting this change where
    humans get asked". It is read rather than recomputed — the worker has the
    proposal, but the *diff* is against the in-force plan, which only the server has.

    Degrades to a plain statement when the server sends no diff, rather than
    asserting "no gate changes": absent data must not read as a measured zero.
    """
    if not isinstance(gate_diff, dict):
        return "**Gates**: not reported by the engine."

    added = gate_diff.get("added") or []
    removed = gate_diff.get("removed") or []
    unchanged = gate_diff.get("unchanged") or []
    parts = [f"+{len(added)} added", f"-{len(removed)} removed", f"{len(unchanged)} unchanged"]
    line = f"**Gates**: {', '.join(parts)}"

    if gate_diff.get("changes_gating"):
        line += " — **this changes where humans are asked to approve**"
    else:
        line += " — no change to where humans are asked to approve"

    listed = [str(address) for address in added][:5]
    if listed:
        line += "\n" + "\n".join(f"  - added: `{address}`" for address in listed)
        if len(added) > len(listed):
            line += f"\n  - …and {len(added) - len(listed)} more"
    return line


def _amendment_success_note(result: dict[str, Any]) -> str:
    """The closing-comment section for a registered amendment.

    Three properties this note must have, all of them about not overstating:

    1. **It never says the amendment is applied.** `status` is printed from the
       server's own field and compared against the one value the route can return;
       anything else prints verbatim with no reassuring gloss. The worker holds no
       `PLAN_APPROVE` and the route writes no node, edge or plan version, so a
       comment implying the plan changed would send an operator to look at a graph
       that still shows the old gates.
    2. **It reports the base revision.** An amendment is authored against one plan
       version; if the plan moved while the author worked, accepting this draft is a
       decision about a stale base and the human needs to see which one it was.
    3. **The accept command is quoted from the server** (`accept_command`), fenced
       for the #4599 reason — an inline `@agent-engine accept amendment <id>` in a
       bot comment is parsed by the tick as a live command and refused, making every
       success message trigger a failure reply. The fallback spells the draft id in
       because a bare `@agent-engine accept` would answer the *acceptance gate*
       instead, which is a different and much larger action.
    """
    draft_id = result.get("draft_id")
    status = result.get("status")
    flow_id = result.get("flow_id")
    flow_url = result.get("flow_url")
    flow_ref = f"[`{flow_id}`]({flow_url})" if flow_url else f"`{flow_id}`"

    accept_command = result.get("accept_command") or f"@agent-engine accept amendment {draft_id}"

    if status == _PENDING_HUMAN_ACCEPT:
        state_line = "**State**: `pending_human_accept` — nothing has been applied. The in-force plan is unchanged and still executing."
    else:
        # Not normalised to the expected value: if the server ever reports something
        # else, the operator must see what it actually said.
        state_line = f"**State**: `{status}` — reported by the engine. Nothing in this run applied it."

    lines = [
        "### Plan amendment proposed for your approval",
        "",
        f"**Flow**: {flow_ref}",
        f"**Draft**: `{draft_id}`",
        f"**Authored against**: plan v{result.get('base_plan_version')}",
        state_line,
        _gate_diff_line(result.get("gate_diff")),
        "",
        "Reply with the following to apply it:",
        "",
        "```",
        accept_command,
        "```",
    ]
    if result.get("already_registered"):
        lines.extend(["", "_This amendment was already on file; the existing draft is unchanged._"])
    return "\n".join(lines)


def _amendment_warning_note(reason: str) -> str:
    """The closing-comment section for an amendment that was not filed.

    Says what the operator has to do next, which is the difference that matters
    between this and `_warning_note`: an unregistered *new* plan leaves committed
    artifacts that are themselves the deliverable, but an unfiled amendment leaves a
    human's `replan:` ask unanswered in the graph. Nothing is damaged — the in-force
    plan is untouched and still executing — but somebody is waiting.
    """
    return "\n".join(
        [
            "### ⚠️ Plan amendment not filed with the orchestration engine",
            "",
            f"Registration was skipped: {reason}",
            "",
            "The in-force plan is unchanged and still executing — nothing was applied and nothing was lost. "
            "The replan request that commissioned this run is still open, so it can be retried.",
        ]
    )


def amendment_registration_note(*, work_dir: Path) -> str:
    """File this run's authored amendment and return a closing-comment section.

    The amendment counterpart of `draft_registration_note`, and **it never raises**
    for the same reason: by the time this runs the authored artifacts are already
    committed to the branch, so a failure here must cost the run nothing.

    Takes no `flow_id`/`request_id` arguments — it reads them from the env via
    `authoring_assignment()`, deliberately. The caller is the entrypoint's finish
    path, which knows what persona ran but has no business deciding which assignment
    a registration is authorized against; sourcing them from the server-written
    envelope export in one place means there is no parameter for a caller to pass a
    different flow into.

    Returns:
        Markdown to append to the closing comment, or `""` when amendment
        registration does not apply: the kill switch is set, this run was not
        commissioned to amend anything, or it was but emitted no amended plan.
    """
    if registration_disabled():
        logger.info("%s is set — skipping amendment registration", DISABLED_ENV)
        return ""

    assignment = authoring_assignment()
    if assignment is None:
        # The normal case for every run that is not an amendment-authoring run.
        return ""
    flow_id, request_id = assignment

    artifact = amendment_artifact_path(work_dir, request_id)
    if not artifact.exists():
        # A commissioned run that produced no amended plan is a real, reportable
        # outcome — unlike the new-flow path, where silence is correct. A human asked
        # for a replan and is waiting on an answer, so "the author concluded without
        # proposing anything" must reach them rather than being logged and dropped.
        logger.warning("Commissioned to amend %s but no artifact at %s", flow_id, artifact)
        return _amendment_warning_note(f"no amended plan was emitted at `{AMENDMENT_ARTIFACT_TEMPLATE.format(request_id=request_id)}`")

    try:
        result = register_amendment_proposal(work_dir=work_dir, flow_id=flow_id, request_id=request_id)
    except EngineRegistrationError as exc:
        logger.warning("Amendment registration failed (non-fatal): %s", exc)
        return _amendment_warning_note(str(exc))
    except Exception as exc:  # noqa: BLE001 - see the module docstring: the run must survive anything
        logger.warning("Amendment registration failed unexpectedly (non-fatal): %s", exc)
        return _amendment_warning_note(f"unexpected error: {exc}")

    logger.info(
        "Amendment draft filed: draft=%s flow=%s base=v%s status=%s already=%s",
        result.get("draft_id"),
        result.get("flow_id"),
        result.get("base_plan_version"),
        result.get("status"),
        result.get("already_registered"),
    )
    return _amendment_success_note(result)


def draft_registration_note(*, work_dir: Path, issue: int) -> str:
    """Register the emitted proposal and return a closing-comment section.

    The single entry point for the entrypoint's finish path, and the reason that
    path needs no error handling of its own: **this function never raises**.

    Returns:
        Markdown to append to the run's closing comment: a success section, a
        warning section, or `""` when registration does not apply to this run
        (kill switch set, or the run emitted no proposal artifact).
    """
    if registration_disabled():
        logger.info("%s is set — skipping draft registration", DISABLED_ENV)
        return ""

    artifact = proposal_artifact_path(work_dir, issue)
    if not artifact.exists():
        # Not every run of an authoring persona composes a loop proposal (Run B
        # materialises issues from already-gated drafts, and earlier stages emit
        # inception artifacts only). Silence is correct here; a warning on every
        # such run would train operators to ignore the warning that matters.
        logger.info("No proposal artifact at %s — nothing to register", artifact)
        return ""

    try:
        result = register_loop_proposal(work_dir=work_dir, issue=issue)
    except EngineRegistrationError as exc:
        logger.warning("Draft registration failed (non-fatal): %s", exc)
        return _warning_note(str(exc))
    except Exception as exc:  # noqa: BLE001 - see the module docstring: the run must survive anything
        logger.warning("Draft registration failed unexpectedly (non-fatal): %s", exc)
        return _warning_note(f"unexpected error: {exc}")

    logger.info(
        "Draft plan registered: flow=%s v%s already=%s",
        result.get("flow_id"),
        result.get("plan_version"),
        result.get("already_registered"),
    )
    return _success_note(result)
