"""Perform one authorized GitHub operation on a worker's behalf (#5223).

Policy-bearing develop/repair work is currently refused outright. The reason is a
property of the provider, not a gap in our checks: a GitHub installation token
carrying `contents: write` also authorizes `PUT /repos/{o}/{r}/pulls/{n}/merge`,
so no token can express "may push this branch, may not merge it". `contents:
write` also cannot be narrowed to a single branch, and an installation token's
lifetime floor is one hour, which routinely exceeds the grant it would serve.
`runtime_policy.policy_github_permissions` therefore returns None, and the work
does not happen.

The resolution here is to stop issuing a credential for these operations. The
worker asks for a *typed operation*; the gateway holds the installation token,
re-authorizes, performs that one operation, and returns the result. The worker
holds nothing between requests — no credential and no reference. Each call is
authorized from scratch on the authenticated transport, so its run, pod, attempt
and claim generation are read from protected records at the moment of the effect
rather than attested by something issued earlier. There is nothing to replay.

Two rules give this module its shape:

**Authority is derived, never asserted.** Every field that decides what happens —
tenant, installation, immutable repository ID, working branch, accepted plan
version, deadline — comes from protected records via
`authorize_worker_credential`. Request fields are assertions to be checked
against that, and a mismatch is a refusal rather than an override. The worker
cannot name a repository, a branch, an installation or a longer expiry.

**Authorization happens immediately before every provider mutation.** Not once
per request: once per call, including each retry and again after a bounded upload
completes. The window between "authorized" and "effect" is where a revoked grant,
a released claim, a superseded plan or a withdrawn acceptance would otherwise
still land a write. Revocation blocks *subsequent* effects; this module makes no
promise to undo a provider action that already completed, because it cannot.

Merge is deliberately absent from what a develop/repair assignment can reach. It
is a separate typed operation requiring `Action.MERGE` to be currently autonomous
(see `github_operations.operation_action`), so mediating commits does not quietly
become mediating merges. This module performs mediation only; it does not
schedule or decide merges — that is #5130's controller, which consumes the
contract published in `docs/security/mediated-github-operations.md`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from src.agentauth.github_operations import (
    MEDIATED_GITHUB_OPERATION_PATH,
    MUTATING_OPERATIONS,
    GitHubOperation,
    OperationAssignment,
    OperationRefusedError,
    assigned_branch,
    operation_action,
    operation_permitted_for_action,
)

if TYPE_CHECKING:  # Import only for typing: at runtime this would be a cycle,
    # because `runtime_policy` imports the operation contract.
    from src.orchestration.execution_policy import ExecutionPolicy

logger = logging.getLogger("bedrockgateway.agentauth.github_operation_service")


@dataclass(frozen=True)
class AuthorizedOperation:
    """The proven authority for one operation, plus the policy that proved it.

    `assignment` carries only derived fields. `policy` is retained so content
    checks (`escalating_paths`) consult the accepted document rather than a
    summary a caller could have shaped.
    """

    assignment: OperationAssignment
    operation: GitHubOperation
    policy: ExecutionPolicy
    plan_version: int | None


async def authorize_operation(
    session,
    *,
    execution: dict,
    grant,
    operation: GitHubOperation,
    workload_binding: str,
    claim_generation: int,
    asserted_repository: str | None = None,
    asserted_branch: str | None = None,
    now: datetime | None = None,
) -> AuthorizedOperation:
    """Re-authorize this assignment for this operation, or refuse.

    Called before the FIRST provider call and again before EVERY subsequent one.
    It is intentionally cheap to repeat and carries no memo: a cached decision is
    exactly the stale authority this function exists to re-check.

    Args:
        session: Live DB session; the accepted policy and current assignment are
            read through it.
        execution: The protected execution record for the authenticated run. Read,
            never written, and never merged with request input.
        grant: The live `DelegatedGrant` for this run and attempt.
        operation: The typed operation requested.
        workload_binding: Pod UID from the verified workload token.
        claim_generation: The generation the caller currently holds.
        asserted_repository: What the request *claimed*, checked against the
            protected record. `None` skips only the cross-check, never the
            derivation.
        asserted_branch: Likewise for the branch. The authorized branch is always
            derived from the protected issue number.

    Raises:
        OperationRefusedError: Any check failed. One exception type with a
            constant message per class of refusal, so a caller cannot use the
            response to learn which condition it tripped and probe from there.
    """
    from src.orchestration.runtime_policy import WorkerCredentialDecision, authorize_worker_credential

    now = now or datetime.now(UTC)

    # 1. The assignment itself must still be authorized, under the policy in
    #    force, for the action it was admitted for. This is the existing check:
    #    membership, role, plan version, wall clock, spend, grant revocation and
    #    current human acceptance all resolve inside it.
    decision = await authorize_worker_credential(session, execution=execution, grant=grant, broker_path=MEDIATED_GITHUB_OPERATION_PATH)
    if not decision.permitted or not isinstance(decision, WorkerCredentialDecision):
        # A bare permitted Decision means no policy was in force. Mediation is a
        # policy-bearing path; it does not run on legacy semantics, because there
        # would be no accepted document to check merge or content against.
        raise OperationRefusedError("operation is not currently authorized")

    # 2. The operation's own requirement, separate from the assignment's action.
    #    Only merge names one. A develop/repair assignment reaching for merge
    #    fails here even though its assignment is perfectly valid — which is the
    #    human merge gate surviving mediation.
    required = operation_action(operation)
    policy = decision.policy
    if policy is None:
        # Mediation evaluates content and gates against the accepted document. No
        # document means nothing to evaluate against, and "cannot check" is a
        # refusal rather than a pass.
        raise OperationRefusedError("no accepted policy is in force for this operation")
    if required is not None and not policy.permits(required):
        raise OperationRefusedError("operation is not currently authorized")

    # 3. Step 1 already required a claim held by this flow and run, so no
    #    operation reaches here without current ownership. What this adds for a
    #    mutation is the GENERATION: the claim can be re-admitted to a new run
    #    under the same flow, and that new generation must not leave the previous
    #    holder still able to write. A read is left at step 1's guarantee.
    if operation in MUTATING_OPERATIONS:
        await require_current_claim(
            session,
            org_id=grant.tenant_id,
            invocation_id=execution.get("invocation_id", {}).get("S", ""),
            claim_generation=claim_generation,
        )

    assignment = build_assignment(
        execution=execution,
        decision=decision,
        workload_binding=workload_binding,
        claim_generation=claim_generation,
        now=now,
    )

    # 4. Assertions are compared, never adopted. A mismatch means the worker's
    #    view of its own assignment diverged from the protected record; that is a
    #    refusal, not a signal to use the worker's version.
    if asserted_repository is not None and asserted_repository != assignment.repository:
        raise OperationRefusedError("operation is not currently authorized")
    if asserted_branch is not None and asserted_branch != assignment.branch:
        raise OperationRefusedError("operation is not currently authorized")

    # 5. The assignment's own action decides which operations it may reach. An
    #    allowlist, not a denylist of the one action we thought of: a `review`
    #    assignment publishing a commit is the same class of defect as an
    #    `evaluate` one doing it, and only an allowlist refuses an action added
    #    later that nobody mapped here.
    if not operation_permitted_for_action(decision.action, operation):
        raise OperationRefusedError("operation is not currently authorized")

    return AuthorizedOperation(
        assignment=assignment,
        operation=operation,
        policy=policy,
        plan_version=decision.plan_version,
    )


def build_assignment(
    *,
    execution: dict,
    decision,
    workload_binding: str,
    claim_generation: int,
    now: datetime,
) -> OperationAssignment:
    """Project the protected records into the authority an operation may use.

    Every field is read from the protected execution or from what
    `authorize_worker_credential` proved. There is no parameter here a request
    body reaches, which is what makes the branch and repository unforgeable
    rather than merely validated.
    """
    try:
        tenant_id = execution["tenant_id"]["S"]
        invocation_id = execution["invocation_id"]["S"]
        attempt = int(execution["orchestration_node_attempt"]["N"])
        installation_id = int(execution["installation_id"]["N"])
        repository = execution["repo"]["S"]
        issue_number = int(execution["issue_number"]["N"])
        node_id = execution["orchestration_node_id"]["S"]
    except (KeyError, TypeError, ValueError):
        raise OperationRefusedError("protected assignment is incomplete") from None

    repository_id = decision.provider_repository_id
    if not isinstance(repository_id, int) or isinstance(repository_id, bool) or repository_id <= 0:
        # The immutable numeric ID is the authorization target. Without it we
        # would be authorizing an `owner/name` string, which a rename frees for
        # someone else to claim.
        raise OperationRefusedError("protected assignment has no immutable repository identity")

    default_branch = execution.get("default_branch", {}).get("S") or "main"
    not_after = decision.not_after
    if not_after is None or not_after <= now:
        raise OperationRefusedError("authorization window is closed")

    return OperationAssignment(
        tenant_id=tenant_id,
        invocation_id=invocation_id,
        attempt=attempt,
        workload_binding=workload_binding,
        claim_generation=claim_generation,
        installation_id=installation_id,
        repository_id=repository_id,
        repository=repository,
        # Derived from the protected issue number: there is no request field by
        # which a caller can influence which branch gets written.
        branch=assigned_branch(issue_number=issue_number, default_branch=default_branch),
        default_branch=default_branch,
        node_id=node_id,
        accepted_plan_version=decision.plan_version,
        not_after=not_after,
    )


async def require_current_claim(session, *, org_id: str, invocation_id: str, claim_generation: int) -> None:
    """Refuse unless this run still holds the work claim at this generation.

    A lapsed lease is not evidence on its own, so this checks held state, active
    run and generation together — the same triple `work_admission` fences on.
    When work claims are disabled the generation is unenforceable, and this
    refuses rather than treating "cannot check" as "check passed".
    """
    from sqlalchemy import select

    from src.orchestration.models import ClaimState, OrchestrationWorkClaim
    from src.orchestration.work_admission import enabled

    if not enabled():
        raise OperationRefusedError("work ownership cannot be established")
    row = await session.scalar(
        select(OrchestrationWorkClaim).where(
            OrchestrationWorkClaim.org_id == org_id,
            OrchestrationWorkClaim.claim_event_id == invocation_id,
        )
    )
    if row is None or row.state != ClaimState.HELD.value or row.active_run_id != invocation_id or int(row.generation) != int(claim_generation):
        raise OperationRefusedError("this run no longer owns the work")


async def current_claim_generation(session, *, org_id: str, invocation_id: str) -> int:
    """The generation this run's work claim is CURRENTLY at, from the protected row.

    Read here rather than accepted from the request, and that is the whole point:
    a reference carries the generation it was minted at, so comparing it against
    this value is what makes a reference stop working once the work has changed
    hands. If the caller supplied the generation instead, it would be asserting the
    very fact the comparison is supposed to establish.
    """
    from sqlalchemy import select

    from src.orchestration.models import ClaimState, OrchestrationWorkClaim
    from src.orchestration.work_admission import enabled

    if not enabled():
        raise OperationRefusedError("work ownership cannot be established")
    row = await session.scalar(
        select(OrchestrationWorkClaim).where(
            OrchestrationWorkClaim.org_id == org_id,
            OrchestrationWorkClaim.claim_event_id == invocation_id,
        )
    )
    if row is None or row.state != ClaimState.HELD.value or row.active_run_id != invocation_id:
        raise OperationRefusedError("this run no longer owns the work")
    return int(row.generation)


async def checkpoint_ownership(*, org_id: str, invocation_id: str, store=None) -> None:
    """Heartbeat the claim around a long provider call, without releasing it.

    A bounded upload plus a tree/commit/ref sequence can outlast a lease. This
    keeps ownership current mid-operation; it is never called with `terminal`,
    because finishing an operation is not finishing the work.
    """
    from src.orchestration.work_admission import worker_checkpoint
    from src.orchestration.work_claims import WorkClaimError

    try:
        await worker_checkpoint(org_id=org_id, invocation_id=invocation_id, terminal=False, store=store)
    except WorkClaimError:
        raise OperationRefusedError("this run no longer owns the work") from None


async def installation_token(*, org_id: str, installation_id: int, repository: str, permissions: dict[str, str]) -> str:
    """Mint a provider token for ONE mediated call. It never leaves this process.

    `repositories` and `permissions` are always passed: omitting either makes
    GitHub fall back to the App's full installation scope across every repository
    it can see, which is the broad authority this whole design exists to avoid.
    """
    from src.knowledge.github_app_service import mint_installation_token_with_expiry, resolve_tenant_app_credentials

    app_id, private_key = await resolve_tenant_app_credentials(org_id)
    token, _expires_at = await mint_installation_token_with_expiry(
        app_id,
        private_key,
        installation_id,
        repositories=[repository.split("/")[1]],
        permissions=permissions,
    )
    return token


def operation_permissions(operation: GitHubOperation) -> dict[str, str]:
    """The narrowest provider permissions that can perform `operation`.

    Per-operation rather than per-assignment. A commit gets `contents: write` for
    the duration of one mediated call inside the gateway; the worker never holds
    it, so the merge capability that rides along with `contents: write` is never
    exposed to something that could use it.
    """
    read = {"contents": "read", "pull_requests": "read", "issues": "read", "checks": "read", "metadata": "read"}
    if operation is GitHubOperation.READ_REPOSITORY:
        return read
    if operation is GitHubOperation.FETCH_REPOSITORY_ARCHIVE:
        # Read-only, and deliberately the SAME set as a metadata read: transferring
        # the repository's content requires no more provider authority than looking
        # at it. In particular no `contents: write`, so the credential minted to
        # materialize a work tree cannot push or merge even inside the gateway.
        return read
    if operation is GitHubOperation.PUBLISH_COMMIT:
        return {**read, "contents": "write"}
    if operation is GitHubOperation.UPSERT_PULL_REQUEST:
        return {**read, "pull_requests": "write"}
    if operation is GitHubOperation.PUBLISH_REVIEW:
        return {**read, "pull_requests": "write", "issues": "write"}
    if operation is GitHubOperation.MERGE_PULL_REQUEST:
        return {**read, "contents": "write", "pull_requests": "write"}
    # Unreachable for enum members; a new member must state its permissions here
    # rather than inherit a permissive default.
    raise OperationRefusedError("operation has no defined provider permissions")


__all__ = [
    "AuthorizedOperation",
    "authorize_operation",
    "build_assignment",
    "checkpoint_ownership",
    "current_claim_generation",
    "installation_token",
    "operation_permissions",
    "require_current_claim",
]
