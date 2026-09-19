"""The authenticated route a delivering run uses to commit its handoff (#5144).

Mounted under the same ``/internal`` proxy as :mod:`src.agentauth.routes` and
reusing its transport dependency, its ``AgentRuntime`` and its workload verifier, so
there is exactly one credential system behind every agent-facing write. Structured
deliberately like :mod:`src.agentauth.pr_binding_routes`, because the authorization
question is the same one and two near-identical models one typo apart is how a gap
gets introduced.

## Two proofs, and why one is not enough

Every hosted worker assumes the same platform role and resolves to the single shared
``scaledjob-worker`` registry entry, so SigV4 plus a self-declared run id reduces to
"whatever the caller typed". This route therefore requires both:

- ``X-Adp-Run-Credential`` — the HMAC-verified run credential, which says *which
  invocation and attempt* is calling.
- ``X-Adp-Workload-Token`` — a projected Kubernetes token verified through
  TokenReview against live pod facts, which says *which pod* is presenting it.

## The body names no work, and there is no field for it

``extra="forbid"`` with only an optional free-text ``summary``. No tenant, node,
cycle, flow, plan version, claim generation, execution id or action id — supplying
one is a 422 rather than a silently ignored field. Every authority fence is resolved
from the ``NODE_DISPATCHED`` decision and the protected execution record, so a
caller cannot commit a handoff against work it was not dispatched for.

Note in particular that ``summary`` grants nothing: it is operator diagnostics, and
no authority is derived from it.

## Refusals

Authorization failures collapse to the same 404 — bad credential, superseded
attempt, cancelled run, wrong pod, a run that is not an engine dispatch. A caller
able to tell those apart learns whether a run it named exists and how many attempts
it has had.

A **handoff** refusal is different and is answered 200 with its outcome, not an
error status. That is deliberate: ``superseded``/``stale``/``refused`` are
information the worker must act on (it may not report an accepted handoff), and they
are only reachable after the caller authenticated as itself. Collapsing them to 404
would make "another attempt owns this now" indistinguishable from "your credential
is bad", and the worker would have no way to report the former accurately.

## No admin permission is involved

As with the PR-binding route, this route's authority is the run credential, which is
strictly narrower than any admin permission: it authorises exactly one invocation to
commit exactly one handoff for the one execution it was dispatched for. Adding a
permission here would mean granting it to the least-privilege ``MEMBER`` role every
registry-resolved agent principal maps to, which reaches every ordinary human user
in every tenant.

No new database privilege is granted and no public mutation endpoint is added.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from src.agentauth.adapter import CREDENTIAL_HEADER
from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.execution import ExecutionStateError
from src.agentauth.routes import AgentRuntime, get_agent_runtime, require_agent_transport
from src.agentauth.run_credential import CredentialError
from src.agentauth.store import AuthorityStoreError
from src.agentauth.workload import WORKLOAD_HEADER, WorkloadRefusedError

logger = logging.getLogger(__name__)

# ``/self`` for the same reason the sibling modules use it: everything here acts on
# the caller's own run, identified from its credential, never on a run named in the
# request. The trailing segment is distinct from theirs so the authorization models
# cannot be confused for one another.
router = APIRouter(
    prefix="/internal/v1/agent/self",
    tags=["agent-authority"],
    dependencies=[Depends(require_agent_transport)],
)


class HandoffRequest(BaseModel):
    """A delivery handoff for the caller's own run.

    Note what is absent: no ``org_id``, ``node_id``, ``cycle``,
    ``accepted_plan_version``, ``claim_id``, ``claim_generation``, ``execution_id``,
    ``action_id`` or ``run_id``. ``extra="forbid"`` makes an attempt to supply one a
    422 rather than a silently ignored field, and the values that matter are read
    from protected state instead.
    """

    model_config = ConfigDict(extra="forbid")

    # Operator diagnostics only. No authority is derived from it, which is why it is
    # the single permitted field.
    summary: str | None = Field(default=None, max_length=4096)


@router.post("/handoff")
async def commit_handoff_route(
    body: HandoffRequest,
    request: Request,
    runtime: AgentRuntime = Depends(get_agent_runtime),
) -> JSONResponse:
    """Commit an idempotent handoff receipt plus a due continuation for this run.

    Idempotent: a repeated report, or a retry after a lost response, returns **the
    same** receipt rather than minting a second one or advancing a counter.

    A committed handoff is never terminal — it records that another party still owes
    the next step, so the execution stays due. This route cannot mark a lane
    complete.
    """
    from datetime import UTC, datetime

    from src.orchestration.handoff import HandoffOutcome, commit_handoff, current_identity
    from src.orchestration.pr_bindings import BindingError, resolve_registration_target
    from src.shared.database import get_session_factory

    credential = request.headers.get(CREDENTIAL_HEADER, "")
    workload = request.headers.get(WORKLOAD_HEADER, "")
    if not credential:
        raise HTTPException(404, "not found")

    try:
        # Both proofs, verified together and fresh per request.
        context = await run_in_threadpool(runtime.authenticate, credential, workload)
        _, caller, record, grant = context
        await runtime.validate_flow(record, grant)
    except (WorkloadRefusedError, BootstrapRefusedError, CredentialError, ExecutionStateError):
        raise HTTPException(404, "not found") from None
    except AuthorityStoreError:
        raise HTTPException(503, "agent authority unavailable") from None

    try:
        async with get_session_factory()() as session:
            # The authenticated invocation IS the engine run id for a dispatched
            # attempt, so the target resolves from verified identity. `expected_org_id`
            # asserts the credential's tenant agrees with the resolved node's tenant
            # rather than silently resolving another tenant's work.
            target = await resolve_registration_target(
                session,
                run_id=caller.invocation_id,
                expected_org_id=caller.tenant_id,
            )
            if target.flow_id != record.flow_id or target.flow_id != grant.flow_id:
                raise HTTPException(404, "not found")

            # Authority fences come from the live execution row, never the caller, and
            # through the SAME reader the engine's reconciliation uses — two resolvers
            # that agree today is how the evidence path and the write path drift apart.
            # Read here and re-verified under the row lock inside the store, which is
            # what keeps a value read before slow work from being trusted after it.
            identity = await current_identity(session, org_id=target.org_id, node_id=target.node_id)
            if identity is None:
                # No execution row: a handoff for work the engine has no execution for
                # is not something to create here.
                raise HTTPException(404, "not found")

            result = await commit_handoff(
                session,
                identity=identity,
                now=datetime.now(UTC),
                progress_note=(body.summary or None),
            )
            if result.accepted:
                # Committed only on acceptance. A refusal must leave nothing written,
                # so the transaction is rolled back rather than partially kept.
                await session.commit()
            else:
                await session.rollback()
    except HTTPException:
        raise
    except BindingError:
        # A run that is not an engine dispatch cannot be distinguished from one that
        # does not exist, so this joins the authorization 404s.
        raise HTTPException(404, "not found") from None
    except (KeyError, TypeError, ValueError):
        raise HTTPException(404, "not found") from None

    logger.info(
        "handoff route: invocation=%s outcome=%s accepted=%s",
        caller.invocation_id,
        result.outcome.value,
        result.accepted,
    )
    return JSONResponse(
        {
            "outcome": result.outcome.value,
            # Present only when genuinely durable. A refusal carries no receipt, so a
            # worker cannot mistake a reason string for one.
            "receipt_ref": result.receipt_ref if result.accepted else None,
            "reason": result.reason,
            "accepted": result.accepted,
        },
        status_code=201 if result.outcome is HandoffOutcome.COMMITTED else 200,
        headers={"Cache-Control": "no-store"},
    )
