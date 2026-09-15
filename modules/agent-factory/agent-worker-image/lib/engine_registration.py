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
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

__all__ = [
    "DISABLED_ENV",
    "EngineRegistrationError",
    "draft_registration_note",
    "proposal_artifact_path",
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


def _load_document(path: Path, *, tenant_id: str, intent_ref: str) -> dict[str, Any]:
    """Read the emitted artifact and apply the two server-owned fields.

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
    if not document.get("intent_ref"):
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
    endpoint_base = os.environ.get("ADP_GATEWAY_ENDPOINT", "").rstrip("/")
    if not endpoint_base:
        raise EngineRegistrationError("ADP_GATEWAY_ENDPOINT is not set; no gateway to register with")

    tenant_id = os.environ.get("ADP_TENANT_ID", "").strip()
    if not tenant_id:
        # Better to report than to send a blank org the gateway must reject: a 422
        # from the engine would name the document, not the missing envelope field.
        raise EngineRegistrationError("no ADP_TENANT_ID in env; refusing to register a plan with no tenant")

    # Issue #4597: the run this registration is on behalf of. `ADP_MESSAGE_ID` is the
    # envelope `message_id` (exported during bootstrap, long before this runs), which
    # is the `event_id` partition key of the run's own `webhook-events` row — the row
    # the gateway reads the owning tenant off. NOT the SQS MessageId and not the KEDA
    # pod name, both of which are also called some spelling of "run id" and neither of
    # which the gateway can resolve.
    run_id = os.environ.get("ADP_MESSAGE_ID", "").strip()
    if not run_id:
        # Reported here rather than sent blank, for the same reason as the tenant
        # above: the gateway's refusal would name the header, and an operator reading
        # the closing comment needs to know the *pod* had nothing to send.
        raise EngineRegistrationError("no ADP_MESSAGE_ID in env; refusing to register a plan with no run to bind it to")

    document = _load_document(proposal_artifact_path(work_dir, issue), tenant_id=tenant_id, intent_ref=str(issue))

    url = f"{endpoint_base}{_DRAFT_PATH}"
    data = json.dumps(document).encode("utf-8")
    # Inside the signed header set, deliberately — see the module docstring.
    headers = _sigv4_sign_request("POST", url, {"Content-Type": "application/json", "X-Agent-RunId": run_id}, data)

    logger.info(
        "Registering draft plan: flow=%s nodes=%s edges=%s intent=%s",
        document.get("flow_slug"),
        len(document.get("nodes") or []),
        len(document.get("edges") or []),
        document.get("intent_ref"),
    )

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
