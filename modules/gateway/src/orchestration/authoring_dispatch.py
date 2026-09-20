"""Turn one verified human `replan:` into exactly one AI-DLC authoring job (#4529).

The missing link in the amendment loop. `engine_commands` records the request;
`pending_amendments` holds it; `draft_routes` receives what an author produces. This
module is what summons the author — the piece whose absence made the reply "the plan
is unchanged until someone authors an amendment" describe work nobody was doing.

--------------------------------------------------------------------------------
Commit-then-publish, and why the ordering is the whole design
--------------------------------------------------------------------------------

Two halves, mirroring `dispatch_pass`:

    build_authoring_assignment()   in the command pass, inside its transaction
    publish_authoring()            after the caller commits

The database write must land FIRST. An envelope published before its request row is
durable manufactures an authoring run the platform has no record of commissioning:
the run calls `POST /orchestration/flows/{flow}/amendments/drafts`, the server looks
for the assignment it is registering against, finds nothing, and refuses — so the
work is lost *and* the human was told their replan was accepted. Published second,
the worst case is a commissioned assignment whose message did not reach the queue,
which stays `QUEUED` and is re-published by a later pass. A retryable owed job is a
recoverable state; a run nobody authorized is not.

That is also why `assign_author_run` runs in the same transaction as the request
row rather than after the send: the run id written there is what
`resolve_authoring_request` and `validate_authoring_authority` both check, so if it
landed after publication there would be a window where a live authoring run could
reach the registration route while the server had no record of having commissioned
it.

--------------------------------------------------------------------------------
One human ask, one author
--------------------------------------------------------------------------------

Three independent mechanisms, because this path is reachable from a webhook and
webhooks deliver twice:

1. `record_replan_request` is idempotent on `(org_id, replan_decision_id)`, enforced
   by a unique index. A duplicated delivery reconciles onto the first row.
2. The run id is **derived** from the request id (:func:`authoring_run_id`), not
   generated. Two passes handling the same request compute the same run id, so the
   second cannot mint a second distinct authoring identity.
3. The FIFO deduplication id is derived from the request and its decision — never
   from a timestamp. A timestamp would change on every attempt and defeat dedup
   entirely, which is the bug `dispatch_pass.message_deduplication_id` documents.

So a duplicate delivery, a lost publish ack, and a tick that crashes between commit
and publish all converge on one authoring assignment rather than two authors racing
to answer one human.

Which is why `DISPATCHED` — not "a run is bound" — is what stops a re-publish. A
request can carry a bound run and still never have reached the queue, and those two
situations are indistinguishable from here; treating a bound run as "already handled"
would strand the assignment `QUEUED` forever while every retry told the human an
author had been assigned. Re-publishing is safe precisely because of the three
mechanisms above.

--------------------------------------------------------------------------------
What the author receives, and what it does not
--------------------------------------------------------------------------------

The envelope carries the assignment: tenant, flow, the request id, the base plan
version and hash the human asked against, the rooting decision, and the human's own
words as **data to consider**. Nothing in this platform executes `request_text`.

It does not carry, and the run cannot obtain, authority to accept the amendment it
proposes, to change the accepted plan, to bypass policy metering, or to publish
unrelated repository changes. Those are refused elsewhere and independently — the
grant minted for it holds `MONITOR` only (`engine.provision_authoring`), dispatch
refuses the kind outright (`agentauth/dispatch.py`), the worker credential boundary
refuses it (`runtime_policy`), work admission refuses it (`work_admission`), and
acceptance requires a human naming the draft (`pending_amendments.accept_amendment`).
This module deliberately adds no fifth mechanism of its own: it builds a bounded
assignment and publishes it, and the bounds are enforced where authority is read.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.shared.models.base import utcnow

from .models import AmendmentRequestState
from .pending_amendments import AmendmentRequest, assign_author_run, mark_request_dispatched, owed_authoring_requests

logger = logging.getLogger("bedrockgateway.orchestration.authoring_dispatch")

__all__ = [
    "AUTHORING_PERSONA",
    "RECOVERY_GRACE_SECONDS",
    "RECOVERY_LIMIT",
    "PendingAuthoring",
    "authoring_run_id",
    "build_authoring_assignment",
    "message_deduplication_id",
    "message_group_id",
    "publish_authoring",
    "recover_owed_authoring",
]

# How long a request must have been owed before recovery will rebuild it, in seconds.
# Not a tuning knob so much as a separation between the two paths: the pass that records
# a request publishes it moments later in its own post-commit flush, so anything younger
# than this is presumed to be that first attempt still in flight. Recovery is for
# genuinely stuck work.
RECOVERY_GRACE_SECONDS = 300

# The per-pass cap on rebuilt assignments. Bounds the tick: `QUEUED` does not expire, so
# a remainder is picked up next wake rather than lost, and one tenant's backlog cannot
# make the engine's own pass unbounded.
RECOVERY_LIMIT = 10

# The persona that authors AI-DLC plans. Fixed rather than configurable: the whole
# point of the assignment is that an AI-DLC author answers it, and a configurable
# persona here would let a deployment point amendment authoring at a persona whose
# authority model was never considered.
AUTHORING_PERSONA = "aidlc"

# Envelope schema version, matching `dispatch_pass._ENVELOPE_VERSION` so the worker
# parses this producer's messages with the same code path.
_ENVELOPE_VERSION = "1.0"

# SQS caps both FIFO key fields at 128 characters.
_MAX_SQS_KEY_LEN = 128

QUEUE_URL_ENV = "BG_ORCH_DISPATCH_QUEUE_URL"
REPO_ENV = "BG_ORCH_DISPATCH_REPO"


def authoring_run_id(request_id: str) -> str:
    """The stable authoring run id for one request. **Derived, never generated.**

    A UUID5 of the request id, so two passes handling the same request compute the
    same value. That is what makes the run binding idempotent: `assign_author_run` is
    conditional on the column still being NULL, so a re-pass writes nothing — but if
    the id were random, a re-pass that *did* win the write would re-point a live
    assignment at a second run, admitting two authors for one human ask.

    Prefixed `replan:` rather than `orch:` so an operator reading a queue message,
    an execution row or a credential can tell an authoring run from graph dispatch
    at a glance, without resolving anything.
    """
    return "replan:" + str(uuid5(NAMESPACE_URL, f"adp:orchestration:amendment-request:{request_id}"))


def message_group_id(*, org_id: str, request_id: str) -> str:
    """The FIFO group for one authoring assignment. **Per request.**

    Per request rather than per tenant or per flow, for the reason
    `dispatch_pass.message_group_id` gives at length: a group shared by several
    messages serialises them, so one stuck authoring job would head-of-line block
    every other tenant's replan. There is no ordering requirement between two
    requests' authoring runs to lose by separating them.
    """
    return f"{org_id}#{request_id}"[:_MAX_SQS_KEY_LEN]


def message_deduplication_id(*, request_id: str, replan_decision_id: str) -> str:
    """The FIFO dedup id for one authoring assignment.

    Derived from what makes the assignment unique — the request and the human
    decision that rooted it — and **deliberately not from a timestamp or the
    envelope's `arrived_at`**. A time-derived key changes on every attempt, so the
    5-minute dedup window would never match and a re-published assignment would
    enqueue a second author. That is the specific bug this issue has to prevent: a
    lost publish ack must reconcile to one authoring job, and the dedup key is what
    makes the retry safe.
    """
    return f"replan:{request_id}:{replan_decision_id}"[:_MAX_SQS_KEY_LEN]


@dataclass(frozen=True)
class PendingAuthoring:
    """One authoring assignment committed to the database and not yet published.

    The `dispatch_pass.PendingPublish` analogue: holding the intent as data between
    the commit and the send is what makes the ordering explicit and testable rather
    than an accident of where the `await` happens.

    Every field is server-resolved. Nothing here is read from a comment body except
    `request_text`, which is carried as data and never interpreted.
    """

    org_id: str
    request_id: str
    flow_id: str
    replan_decision_id: str
    requested_by: str
    author_run_id: str
    base_plan_version: int | None
    base_plan_hash: str | None
    envelope: dict[str, Any]
    group_id: str
    deduplication_id: str


def _build_envelope(
    *,
    org_id: str,
    request: AmendmentRequest,
    author_run_id: str,
    user_id: str,
    cognito_sub: str,
    repo: str,
    issue: int,
    installation_id: int,
    base_input: dict[str, Any],
) -> dict[str, Any]:
    """The authoring assignment as an envelope, built explicitly.

    Mirrors `dispatch_pass._build_envelope`'s shape because the envelope contract is
    what the worker consumes, and differs in exactly one place: the `orchestration`
    block names a *request* and a base revision rather than a node and an attempt.
    An authoring run has no graph node, so there is no `node_id` to carry and none
    is invented — `graph_address` is absent for the same reason, and that absence is
    what stops an authoring run's spend from being attributed to work it did not do.

    `correlation.root_human_id` is attribution, not a credential: the human who asked
    is recorded so an operator can answer "who wanted this?", and the acting principal
    stays the authoring run.
    """
    return {
        "version": _ENVELOPE_VERSION,
        "message_id": author_run_id,
        "actor": {"user_id": user_id, "org_id": org_id},
        "cognito_sub": cognito_sub,
        # The engine is its own channel, as in `dispatch_pass`: nothing here came
        # from a GitHub event, and labelling it "github" would make an authoring
        # assignment indistinguishable from a webhook trigger in every log.
        "channel": "orchestration",
        "tenant_id": org_id,
        "persona": AUTHORING_PERSONA,
        "source_ref": {
            "installation_id": installation_id,
            "repo": repo,
            "issue": issue,
        },
        "intent": {
            "trigger": "engine_replan",
            "label": None,
            "persona": AUTHORING_PERSONA,
        },
        "correlation": {
            "correlation_id": author_run_id,
            "root_human_id": user_id,
            "is_human_rooted": True,
            "chain_depth": 0,
        },
        "orchestration": {
            "flow_id": request.flow_id,
            # The assignment. `request_id` is what the author presents back to the
            # registration route, and `root_decision_id` is the committed
            # `REPLAN_REQUESTED` decision the whole job is rooted in.
            "request_id": request.id,
            "root_decision_id": request.replan_decision_id,
            # What was in force when the human asked. Carried so the author knows
            # which plan it is amending without a second read that could see a
            # different version.
            "base_plan_version": request.base_plan_version,
            "base_plan_hash": request.base_plan_hash,
        },
        # The human's words, as DATA for the author to consider. Nothing in this
        # platform executes this string; it is quoted into the authoring context the
        # same way an issue body is.
        "payload": {"replan_request": request.request_text, "requested_by": request.requested_by, "amendment_base": base_input},
        "arrived_at": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


async def build_authoring_assignment(
    session: AsyncSession,
    *,
    org_id: str,
    request: AmendmentRequest,
    repo: str,
    issue: int,
    installation_id: int,
) -> PendingAuthoring | None:
    """Bind an authoring run to a recorded request. **Commits nothing; sends nothing.**

    Call inside the same transaction that recorded the request, so the run binding and
    the request row land together or not at all.

    Returns None only when the request has already been **published** (`DISPATCHED`):
    one author is already answering, so nothing is owed and the caller reports the same
    success it reported the first time.

    A request that is still `QUEUED` yields an assignment even if a run is already
    bound, because `QUEUED` means the envelope did not provably reach the queue — a
    duplicated delivery and a lost publish ack look identical from here, and both are
    answered by re-publishing. That reconciles to *one* authoring job rather than two,
    because the run id is derived and the deduplication id is derived: SQS collapses the
    duplicate, and even past the dedup window the pod binding in
    `bootstrap.bind` admits only one pod per invocation. Returning None here instead
    would strand the assignment — the row would stay `QUEUED` forever while every
    retry told the human an author had been assigned, which is precisely the
    misleadingly-successful replan this must not produce.

    Args:
        session: Caller-owned. Not committed here.
        request: The snapshot `record_replan_request` returned.
        repo: The repository the authoring run works in, from dispatch configuration.
        issue: The issue the request arrived on, for the run's `source_ref`.
        installation_id: The tenant's resolved GitHub installation.
    """
    if request.state == AmendmentRequestState.DISPATCHED.value:
        # Published already. Not an error and not a second job: the caller's reply is
        # unchanged, because one author is already answering this human.
        logger.info(
            "authoring assignment already published request=%s run=%s org=%s",
            request.id,
            request.author_run_id,
            org_id,
        )
        return None

    # The stored id wins when one is already bound: it is what `resolve_authoring_request`
    # checks, so a re-publish has to address the run the server already commissioned.
    # Equal to the derived value in every reachable case — `assign_author_run` only ever
    # writes this function's derivation — but reading it rather than recomputing means a
    # re-publish can never disagree with the binding.
    run_id = request.author_run_id or authoring_run_id(request.id)

    from src.shared.identity.resolver import resolve_root_user_entity_id, resolve_user_entity_id

    # Both namespaces, resolved from the requesting human within this tenant, exactly
    # as `dispatch_pass` does: the worker/vault and root ledger use `users.id`, and
    # personal context uses the Cognito sub.
    #
    # Resolved BEFORE the run binding, deliberately. Either resolver raises on a human
    # this tenant cannot resolve, and a binding written first would leave the request
    # with an author id and no published envelope: `QUEUED` forever, with every later
    # pass reporting a success nobody is working on. Resolved first, a failure leaves
    # `author_run_id` NULL and the assignment genuinely retryable.
    user_id = await resolve_root_user_entity_id(session, org_id, request.requested_by)
    cognito_sub = await resolve_user_entity_id(session, org_id, user_id)

    from .authoring_input import AuthoringInputError, resolve_authoring_input

    base_input = await resolve_authoring_input(session, org_id=org_id, request=request, author_run_id=run_id)

    envelope = _build_envelope(
        org_id=org_id,
        request=request,
        author_run_id=run_id,
        user_id=user_id,
        cognito_sub=cognito_sub,
        repo=repo,
        issue=issue,
        installation_id=installation_id,
        base_input=base_input,
    )
    if len(json.dumps(envelope).encode("utf-8")) > 256 * 1024:
        raise AuthoringInputError("authoring_input_envelope_too_large")

    # Bind only after the complete input is resolvable and transportable. A refusal
    # leaves the durable request queued without commissioning an unusable run.
    await assign_author_run(session, org_id=org_id, request_id=request.id, author_run_id=run_id)

    logger.info(
        "authoring assignment built request=%s flow=%s run=%s base=v%s org=%s — envelope queued for publish",
        request.id,
        request.flow_id,
        run_id,
        request.base_plan_version,
        org_id,
    )
    return PendingAuthoring(
        org_id=org_id,
        request_id=request.id,
        flow_id=request.flow_id,
        replan_decision_id=request.replan_decision_id,
        requested_by=request.requested_by,
        author_run_id=run_id,
        base_plan_version=request.base_plan_version,
        base_plan_hash=request.base_plan_hash,
        envelope=envelope,
        group_id=message_group_id(org_id=org_id, request_id=request.id),
        deduplication_id=message_deduplication_id(request_id=request.id, replan_decision_id=request.replan_decision_id),
    )


async def recover_owed_authoring(
    session: AsyncSession,
    *,
    limit: int = RECOVERY_LIMIT,
    grace_seconds: int = RECOVERY_GRACE_SECONDS,
) -> list[PendingAuthoring]:
    """Rebuild the assignments for requests still owed an author. **Commits nothing.**

    The other half of commit-then-publish, and the reason this module's claim that "a
    later pass re-publishes it" is now true. `build_authoring_assignment` is driven by a
    human's comment; this is driven by the durable row, which is the only thing that
    survives a publish that did not land. Without it the marker was consumed, the reply
    said an author had been assigned, and nothing ever looked at the `QUEUED` row again.

    Call inside the engine pass's transaction, before the caller commits, and publish the
    returned assignments in the same post-commit flush as the pass's own — the ordering
    argument in the module docstring applies identically here.

    **This cannot produce a second author for one human ask.** The run id is read from
    the row when one is bound and derived from the request id otherwise, so a rebuild
    addresses the run the server already commissioned; the FIFO deduplication id is
    derived from the request and its decision, so the queue collapses a duplicate; and
    `mark_request_dispatched` is conditional on `QUEUED`, so the first successful publish
    takes the row out of this query's scope permanently.

    Per-request failures are contained and skipped rather than raised: a tenant whose
    installation cannot be resolved, or a human who has since been removed, must not stop
    every other tenant's owed request being recovered. A skipped request stays `QUEUED`
    and is tried again next wake, which is the honest outcome — it is still owed.

    Returns:
        The assignments to publish. Empty when nothing is owed, which is the normal case.
    """
    repo = (os.environ.get(REPO_ENV) or "").strip()
    if not repo:
        # Same fail-closed rule the command pass applies: with no configured repository
        # an authoring run cannot be addressed at all, so there is nothing to rebuild.
        # Logged at debug because on a deployment that never enabled the engine this
        # would otherwise be a warning on every tick.
        logger.debug("authoring recovery: %s is unset; no owed assignment can be addressed", REPO_ENV)
        return []

    from datetime import timedelta

    from .dispatch_pass import resolve_installation_id

    try:
        owed = await owed_authoring_requests(session, limit=limit, older_than=utcnow() - timedelta(seconds=grace_seconds))
    except Exception:
        # A read that fails leaves every row `QUEUED`, so the next wake retries. Counted
        # nowhere and raised nowhere: recovery is a repair pass, and its failure must not
        # cost the tick the durable work it did this invocation.
        logger.exception("authoring recovery: could not read owed authoring requests")
        return []

    if not owed:
        return []

    logger.info("authoring recovery: %d request(s) still owed an author", len(owed))

    rebuilt: list[PendingAuthoring] = []
    for org_id, request, intent_ref in owed:
        try:
            # Addressed from server state only. The issue comes from the flow's own
            # intent issue and the installation from the org record through the SAME
            # fail-closed resolver dispatch uses — never from anything a comment
            # supplied, which by now is long consumed anyway.
            issue = _issue_number(intent_ref)
            if issue is None:
                logger.warning(
                    "authoring recovery: flow %s has no usable intent issue; request %s stays queued",
                    request.flow_id,
                    request.id,
                )
                continue

            installation_id = await resolve_installation_id(session, org_id=org_id)
            if installation_id is None:
                logger.warning(
                    "authoring recovery: org %s has no unambiguous installation; request %s stays queued",
                    org_id,
                    request.id,
                )
                continue

            assignment = await build_authoring_assignment(
                session,
                org_id=org_id,
                request=request,
                repo=repo,
                issue=issue,
                installation_id=installation_id,
            )
        except Exception:
            # Contained per request, for the reason in the docstring: one tenant's
            # unresolvable identity must not strand every other tenant's owed work.
            logger.exception("authoring recovery: could not rebuild an assignment for request %s — it stays queued", request.id)
            continue

        if assignment is None:
            # Published between the read and here. Nothing is owed; not an error.
            continue

        logger.info(
            "authoring recovery: rebuilt the assignment for request %s flow=%s run=%s org=%s",
            request.id,
            request.flow_id,
            assignment.author_run_id,
            org_id,
        )
        rebuilt.append(assignment)

    return rebuilt


def _issue_number(intent_ref: str | None) -> int | None:
    """The issue number a flow's `intent_ref` names, or None.

    Both `"4527"` and `"#4527"` occur in real proposals — `engine_commands`,
    `dispatch_pass` and `diagnose` all cope with either spelling — so this strips the
    `#` rather than matching one form and silently failing on half of all flows.

    None for absent or non-numeric text, which is a skip rather than a guess: a rebuilt
    envelope addressed at an invented issue would post an authoring run's output
    somewhere nobody asked for.
    """
    text = (intent_ref or "").strip().lstrip("#")
    return int(text) if text.isdigit() else None


async def publish_authoring(
    pending: PendingAuthoring,
    *,
    session_factory: Any,
    client: Any | None = None,
    queue_url: str | None = None,
    region: str | None = None,
) -> bool:
    """Publish one committed authoring assignment. **Call after the caller commits.**

    Returns whether the assignment reached the queue. False leaves the request
    `QUEUED`, which is the retryable state by design — there is deliberately no
    `FAILED`, because the only honest meaning of "publish did not land" is "try
    again", and that is what `QUEUED` already says.

    A False return must reach the human as a retryable outcome, never as a
    successful replan. The caller is responsible for that wording; this function's
    job is to be truthful about what happened.

    Protected runs capture the requesting human's persona-model settings after
    authority provisioning and before SQS publication, while the execution is
    still pending. The snapshot lives on the protected record, so the sealed
    envelope is unchanged. Preparation failures are report-only evidence; the
    worker's runtime posture still determines whether model use is permitted.

    `mark_request_dispatched` runs in its own short transaction *after* a successful
    send, through `session_factory`, because the caller's transaction is already
    committed by the time this runs. If that write fails the message has still been
    sent and the row stays `QUEUED`: a later pass finds an assignment with an author
    already bound, publishes under the same deduplication id, and SQS collapses the
    duplicate. Re-publishing a message the queue already holds is the benign failure;
    marking a request dispatched when nothing was sent is not.
    """
    url = queue_url if queue_url is not None else (os.environ.get(QUEUE_URL_ENV) or "").strip()
    if not url:
        logger.error(
            "authoring assignment not published: %s is unset request=%s — request remains queued and retryable",
            QUEUE_URL_ENV,
            pending.request_id,
        )
        return False

    protected = os.environ.get("AGENT_AUTHORITY_ENABLED", "false").lower() == "true"
    envelope = pending.envelope
    if not protected:
        from src.admin.persona_models.dispatch_selection import apply_dispatch_selection, mapping_enabled

        if mapping_enabled():
            try:
                async with session_factory() as session:
                    envelope = await apply_dispatch_selection(session, envelope)
            except Exception:
                logger.exception("Authoring persona model selection unavailable request=%s; request remains queued", pending.request_id)
                return False
    if protected:
        try:
            from src.agentauth.engine import get_engine_authority_writer

            writer = await run_in_threadpool(get_engine_authority_writer)
            envelope = await run_in_threadpool(writer.provision_authoring, pending)
        except Exception:
            # Fail-closed and loudly: no authority row means no credential, so a
            # message sent now would produce a run that cannot bootstrap. The
            # request stays `QUEUED`.
            logger.exception(
                "authoring assignment not published: protected authority unavailable request=%s — request remains queued",
                pending.request_id,
            )
            return False

        from src.agentauth.model_policy import ensure_snapshot_report_only

        try:
            async with session_factory() as session:
                receipt = await ensure_snapshot_report_only(session, store=writer.store, invocation_id=pending.author_run_id)
        except Exception:
            # Session acquisition/cleanup can fail outside the report-only
            # helper. Like unavailable policy reads, this must not change queue
            # admission or be mistaken for a protected-authority failure.
            receipt = {"status": "unavailable", "reason": "snapshot_unavailable"}
        if receipt.get("status") != "available":
            logger.warning(
                "authoring model-policy evidence unavailable request=%s reason=%s; dispatch is unaffected",
                pending.request_id,
                receipt.get("reason"),
            )

    sqs = client if client is not None else _get_sqs_client(region or os.environ.get("AWS_REGION") or "us-east-1")
    try:
        response = sqs.send_message(
            QueueUrl=url,
            MessageBody=json.dumps(envelope, default=str),
            MessageGroupId=pending.group_id,
            MessageDeduplicationId=pending.deduplication_id,
        )
    except Exception:
        logger.exception(
            "authoring assignment publish failed request=%s run=%s — request remains queued and retryable",
            pending.request_id,
            pending.author_run_id,
        )
        return False

    logger.info(
        "authoring assignment published request=%s run=%s sqs_message_id=%s",
        pending.request_id,
        pending.author_run_id,
        (response or {}).get("MessageId", ""),
    )

    try:
        async with session_factory() as session:
            await mark_request_dispatched(session, org_id=pending.org_id, request_id=pending.request_id)
            await session.commit()
    except Exception:
        # The message IS on the queue. Counted as a warning rather than a failure
        # because the assignment exists and the author will run — see the docstring
        # on why a re-publish under the same dedup id is the benign outcome.
        logger.warning(
            "authoring assignment published but not marked dispatched request=%s — a later pass may re-publish under the same dedup id",
            pending.request_id,
            exc_info=True,
        )
    return True


_sqs_client: Any | None = None


def _get_sqs_client(region: str) -> Any:
    global _sqs_client
    if _sqs_client is None:
        import boto3

        _sqs_client = boto3.client("sqs", region_name=region)
    return _sqs_client
