"""The authenticated route a delivering run uses to bind its pull request (#5301).

Mounted under the same ``/internal`` proxy as :mod:`src.agentauth.routes` and
reusing its transport dependency, its ``AgentRuntime`` and its workload verifier,
so there is one credential system behind every agent-facing write.

## Why this route is authenticated by the run credential, not a header

An earlier design for this route read the target story from an ``X-Agent-RunId``
header. That is wrong, and the reason is written down in
:mod:`src.budget.run_binding` and in ``lib/engine_registration.py``: the header is
*a reference to a row the server wrote, never an identity*. Every hosted worker
assumes the same platform role and resolves to the single shared
``scaledjob-worker`` registry entry, so SigV4 plus a self-declared run id reduces
to "whatever the caller typed" — any worker could bind any PR to any story whose
run id it could guess or read.

So this route requires the same two proofs as
:mod:`src.agentauth.registration_routes`:

- ``X-Adp-Run-Credential`` — the HMAC-verified run credential, which says *which
  invocation and attempt* is calling.
- ``X-Adp-Workload-Token`` — a projected Kubernetes token verified through
  TokenReview against live pod facts, which says *which pod* is presenting that
  credential.

The registration target is then derived from the **authenticated** execution:
``RunCredential.invocation_id`` is, for an engine-dispatched run, exactly the
``run_id`` the ``NODE_DISPATCHED`` decision recorded (both are
``dispatch_pass.attempt_run_id(node_id, attempt)`` — see ``_build_envelope``,
which sets the envelope ``message_id`` to that value). So the story is resolved
from a cryptographically verified identity rather than an assertion, and the
request body carries no story, node, flow or tenant field for a caller to point
somewhere else.

## What the body may contain

Only the pull request's own identity assertions. GitHub verifies them using the
repository and installation from protected execution state; no immutable ID or
head is accepted on the caller's assertion alone. There is deliberately no ``node_id``,
``flow_id``, ``org_id``, ``attempt`` or ``run_id`` field: ``extra="forbid"`` makes
an attempt to supply one a 422 rather than a silently ignored field, and the
values that matter are read from the protected execution record instead. A caller
may additionally *downgrade* its own registration to a reviewer artifact; it can
never upgrade one (see ``pr_bindings._resolve_role``).

## Refusals

Every authorization failure returns the same 404 — bad credential, superseded
attempt, cancelled run, wrong pod, a run that is not an engine story dispatch.
A caller able to tell those apart learns whether a run it named exists. The one
exception is a genuine *binding* refusal (``AMBIGUOUS_CANDIDATE``,
``ALREADY_BOUND_ELSEWHERE``, ``REPOSITORY_MISMATCH``, ...), which returns 409 with
its stable :class:`~src.orchestration.pr_bindings.BindingRefusal` code. That is
safe because it is only reachable after the caller has authenticated as itself,
and the code is the whole diagnostic value: it is what the worker echoes into its
closing comment and what the story API surfaces as the hold reason.

## No admin permission is involved

Note what is absent: no ``Permission`` check. This route's authority is the run
credential, which is strictly narrower than any admin permission — it authorises
exactly one invocation to bind exactly one PR to the one story it was dispatched
for. Adding an admin permission here would mean granting it to the least-privilege
``MEMBER`` role that a registry-resolved agent principal maps to, which is the
widening ``config.Permission.PLAN_DRAFT`` documents at length and which reaches
every ordinary human user in every tenant. The credential path avoids that
entirely. The operator-plane recovery path for *historical* unbound work is a
different surface and does carry a permission, because a human is asserting an
association their own run did not deliver.
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

# ``/self`` for the same reason `registration_routes` uses it: everything here
# acts on the caller's own run, identified from its credential, never on a run
# named in the request. The trailing segment is distinct from that module's
# routes so the two authorization models cannot be one typo apart.
router = APIRouter(
    prefix="/internal/v1/agent/self",
    tags=["agent-authority"],
    dependencies=[Depends(require_agent_transport)],
)

# A GitHub commit SHA. Bound to hex so a ref name or a URL cannot arrive here:
# `head_sha` is what makes review/check evidence falsifiable, and comparing a
# provider SHA against a caller-supplied branch name would always differ.
_SHA_PATTERN = r"^[0-9a-f]{7,64}$"


class BindPullRequestRequest(BaseModel):
    """The pull request this run is registering as its delivery.

    Every field is the *pull request's* own identity. Nothing here names the story,
    because the story comes from the authenticated execution.

    ``provider_repository_id`` and ``provider_pr_node_id`` are GitHub's immutable
    ids and are required rather than optional: a binding keyed on ``owner/name`` and
    a number is silently re-pointed by a repository rename or transfer, and a
    caller that cannot supply the immutable pair is refused
    (``INCOMPLETE_IDENTITY``) rather than bound on a name.
    """

    model_config = ConfigDict(extra="forbid")

    provider_repository_id: int = Field(ge=1)
    provider_pr_node_id: str = Field(min_length=1, max_length=255)
    repo: str = Field(min_length=3, max_length=255, pattern=r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
    pr_number: int = Field(ge=1, le=100_000_000)
    head_sha: str = Field(pattern=_SHA_PATTERN)
    # A caller may declare itself a reviewer artifact, which only ever *reduces*
    # what the binding can do (`pr_bindings._resolve_role`). Declaring
    # "implementation" adds nothing, since a resolved story dispatch already means
    # that, so the field is a single opt-in flag rather than a role string a caller
    # could spell in a way that reads as an upgrade.
    reviewer_artifact: bool = False


@router.post("/pull-request")
async def bind_pull_request(
    body: BindPullRequestRequest,
    request: Request,
    runtime: AgentRuntime = Depends(get_agent_runtime),
) -> JSONResponse:
    """Bind the caller's pull request to the story its own run was dispatched for.

    Idempotent: re-registering the same PR returns the existing binding rather than
    inserting a second one, and a head change on that PR repairs the existing
    binding. So a duplicated event, a request retry and a re-run all converge.
    """
    from src.orchestration.models import BindingRole
    from src.orchestration.pr_bindings import (
        BindingError,
        BindingRefusal,
        register_binding,
        resolve_registration_target,
    )
    from src.orchestration.pr_identity import PrIdentityError, resolve_pr_identity
    from src.orchestration.state import ActorKind
    from src.shared.database import get_session_factory

    credential = request.headers.get(CREDENTIAL_HEADER, "")
    workload = request.headers.get(WORKLOAD_HEADER, "")
    if not credential:
        raise HTTPException(404, "not found")

    try:
        # Both proofs, verified together and fresh per request. `authenticate`
        # returns (pod, caller, record, grant); the caller credential is what
        # identifies the run, and the pod is what proves the credential did not
        # leak out of it.
        context = await run_in_threadpool(runtime.authenticate, credential, workload)
        _, caller, record, grant = context
        await runtime.validate_flow(record, grant)
        execution = await run_in_threadpool(runtime.store._read, f"TENANT#{record.tenant_id}", f"EXEC#{record.invocation_id}")
        # These fields are written by protected dispatch, not the worker. In
        # particular, story dispatch supports reviewers as well as developers.
        repository_id = int(execution["provider_repository_id"]["N"])
        installation_id = int(execution["installation_id"]["N"])
        persona = execution["persona"]["S"]
        if (
            repository_id < 1
            or installation_id < 1
            or persona not in {"developer", "reviewer"}
            or grant.authority.kind != "gate_decision"
            or not record.repo
            or execution["repo"]["S"] != record.repo
            or record.repo not in grant.repo_scope
        ):
            raise BootstrapRefusedError("unresolved story execution")
    except (WorkloadRefusedError, BootstrapRefusedError, CredentialError, ExecutionStateError):
        raise HTTPException(404, "not found") from None
    except (KeyError, TypeError, ValueError):
        raise HTTPException(404, "not found") from None
    except AuthorityStoreError:
        raise HTTPException(503, "agent authority unavailable") from None

    try:
        async with get_session_factory()() as session:
            # The authenticated invocation IS the engine run id for a dispatched
            # story attempt, so this resolves the target from verified identity.
            # `expected_org_id` is defence in depth: the tenant a binding is filed
            # under always comes from the resolved node row, and this asserts the
            # credential's tenant agrees rather than silently resolving another
            # tenant's story.
            target = await resolve_registration_target(
                session,
                run_id=caller.invocation_id,
                expected_org_id=caller.tenant_id,
            )
            if (
                target.flow_id != record.flow_id
                or target.flow_id != grant.flow_id
                or target.node_id != execution["orchestration_node_id"]["S"]
                or target.attempt != int(execution["orchestration_node_attempt"]["N"])
                or target.repo.lower() != record.repo.lower()
                or target.installation_id != installation_id
            ):
                raise HTTPException(404, "not found")
            if body.repo.lower() != record.repo.lower():
                raise BindingError(BindingRefusal.REPOSITORY_MISMATCH, "Pull request is outside this execution's repository.")
            pr = await resolve_pr_identity(
                org_id=record.tenant_id,
                installation_id=installation_id,
                repo=record.repo,
                pr_number=body.pr_number,
            )
            if pr.provider_repository_id != repository_id or body.provider_repository_id != repository_id:
                raise BindingError(BindingRefusal.REPOSITORY_MISMATCH, "Pull request does not belong to the dispatched repository.")
            if body.provider_pr_node_id != pr.provider_pr_node_id:
                raise BindingError(BindingRefusal.INCOMPLETE_IDENTITY, "Pull-request identity does not match the provider.")
            binding, created = await register_binding(
                session,
                target=target,
                pr=pr,
                actor_id=caller.principal,
                actor_kind=ActorKind.SERVICE,
                declared_role=BindingRole.REVIEWER_ARTIFACT if persona == "reviewer" or body.reviewer_artifact else None,
            )
            await session.commit()
    except PrIdentityError:
        raise HTTPException(503, "pull-request identity unavailable") from None
    except (KeyError, TypeError, ValueError):
        raise HTTPException(404, "not found") from None
    except BindingError as exc:
        # A run that is not an engine-dispatched story cannot be distinguished from
        # a run that does not exist, so those collapse to 404 with the rest of the
        # authorization failures. A binding conflict is a 409 carrying its code: it
        # is reachable only post-authentication and the code is what the worker
        # reports and the story API surfaces.
        if exc.code in {
            BindingRefusal.MISSING_RUN_ID,
            BindingRefusal.UNKNOWN_RUN,
            BindingRefusal.STALE_RUN,
            BindingRefusal.NOT_A_STORY,
            BindingRefusal.TENANT_MISMATCH,
        }:
            logger.info("pull-request binding refused invocation=%s reason=%s", caller.invocation_id, exc.code.value)
            raise HTTPException(404, "not found") from None
        logger.info("pull-request binding conflict invocation=%s reason=%s", caller.invocation_id, exc.code.value)
        raise HTTPException(409, exc.code.value) from None

    return JSONResponse(
        {
            "bound": True,
            "created": created,
            "node_id": binding.node_id,
            "pr_number": binding.pr_number,
            "head_sha": binding.head_sha,
            "role": binding.role,
            "state": binding.state,
        },
        status_code=201 if created else 200,
        headers={"Cache-Control": "no-store"},
    )
