"""Production work-claim integration for trusted dispatch and worker lifecycle.

The switch requires protected agent authority on every producer and worker.
An events-table row or a caller-supplied tenant is never an ownership proof.
This module changes only work claims; it cannot approve or advance a graph.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from .models import ClaimState, OrchestrationWorkClaim
from .work_claims import (
    ClaimBinding,
    ClaimOwner,
    Disposition,
    OwnerKind,
    ReleaseReason,
    WorkClaimError,
    bind_run,
    claim_work,
    heartbeat,
    release_work,
)

logger = logging.getLogger(__name__)
ENABLED_ENV = "ADP_WORK_CLAIMS_ENABLED"


def enabled() -> bool:
    return os.environ.get(ENABLED_ENV, "false").lower() == "true"


def require_authority() -> None:
    if os.environ.get("AGENT_AUTHORITY_ENABLED", "false").lower() != "true":
        raise WorkClaimError("authority_required", "Work claims require protected producer and worker identity.")


async def resolve_repository_id(*, org_id: str, installation_id: int, repo: str) -> int:
    """Resolve the provider's immutable ID, never hash or key on a repo name.

    A rename resolves to the same numeric ID. An unavailable lookup blocks
    dispatch; no default repository or platform-wide token substitutes for it.
    """
    from src.knowledge.github_app_service import mint_installation_token_with_expiry, resolve_tenant_app_credentials

    if len(repo.split("/")) != 2 or any(part in {"", ".", ".."} for part in repo.split("/")):
        raise WorkClaimError("invalid_repository", "Repository must name an owner and repository.")
    app_id, key = await resolve_tenant_app_credentials(org_id)
    token, _ = await mint_installation_token_with_expiry(
        app_id, key, installation_id, repositories=[repo.split("/")[1]], permissions={"metadata": "read"}
    )
    async with httpx.AsyncClient(base_url="https://api.github.com", timeout=10, trust_env=False, follow_redirects=False) as client:
        response = await client.get(f"/repos/{repo}", headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
        response.raise_for_status()
        repository_id = response.json().get("id")
    if type(repository_id) is not int or repository_id <= 0:
        raise WorkClaimError("repository_unresolved", "GitHub did not return an immutable repository ID.")
    return repository_id


async def admit(session, *, org_id: str, repository_id: int, issue: int, owner: ClaimOwner, invocation_id: str) -> dict:
    """Reserve the lane and expected invocation in the producer's transaction."""
    receipt = await claim_work(session, binding=ClaimBinding(org_id, repository_id, issue), owner=owner, event_id=invocation_id)
    if receipt.disposition == Disposition.DUPLICATE:
        # An acknowledgement may be lost after a committed admission. Only the
        # SAME held generation and invocation can retry its pending publication.
        row = await session.scalar(
            select(OrchestrationWorkClaim).where(OrchestrationWorkClaim.id == receipt.claim_id, OrchestrationWorkClaim.org_id == org_id)
        )
        if (
            row is None
            or row.state != ClaimState.HELD.value
            or row.active_run_id != invocation_id
            or row.owner_ref != owner.ref
            or row.owner_kind != owner.kind.value
        ):
            raise WorkClaimError("event_already_completed", "This admission is no longer pending.")
    elif not receipt.admitted:
        logger.info("work_admission refused org=%s invocation=%s reason=%s", org_id, invocation_id, receipt.reason)
        raise WorkClaimError(receipt.reason or receipt.disposition.value, "Work is already owned or ownership is unresolved.")
    else:
        bound = await bind_run(session, org_id=org_id, claim_id=receipt.claim_id, generation=receipt.generation, run_id=invocation_id)
        if not bound.admitted:
            raise WorkClaimError(bound.reason or "bind_refused", "Work claim could not bind the admitted invocation.")
    return {"claim_id": receipt.claim_id, "generation": receipt.generation, "invocation_id": invocation_id, "disposition": receipt.disposition.value}


async def admit_pending(store, invocation_id: str, *, session=None) -> dict:
    """Resolve every authority field from protected dispatch, not HTTP input.

    The producer endpoint accepts only an invocation ID. It cannot manufacture
    a pending execution, select a tenant/issue/owner, release someone else's
    claim or request a handover. Protected dispatch must already exist.
    """
    if not enabled():
        return {"enforced": False}
    require_authority()
    pointer = await run_in_threadpool(store._read, f"INVOCATION#{invocation_id}", "DISPATCH")
    org_id = (pointer or {}).get("tenant_id", {}).get("S")
    if not org_id:
        raise WorkClaimError("dispatch_unresolved", "Protected dispatch was not found.")
    execution = await run_in_threadpool(store._read, f"TENANT#{org_id}", f"EXEC#{invocation_id}")
    if not execution or execution.get("status") != {"S": "pending"}:
        raise WorkClaimError("dispatch_not_pending", "Only a pending protected dispatch can acquire work.")
    grant = await run_in_threadpool(store.live_grant, invocation_id=invocation_id, tenant_id=org_id, attempt=1, now=datetime.now(UTC))
    try:
        repository_id = int(execution.get("provider_repository_id", {}).get("N", "0"))
        issue = int(execution["issue_number"]["N"])
        repo = execution["repo"]["S"]
    except (KeyError, TypeError, ValueError):
        raise WorkClaimError("dispatch_binding_missing", "Protected dispatch lacks immutable work identity.") from None
    if not grant.flow_id or repo not in grant.repo_scope or grant.tenant_id != org_id:
        raise WorkClaimError("dispatch_scope_invalid", "Protected work scope is unresolved.")
    if issue == 0 and grant.authority.kind == "service_policy":
        # A scheduled coordinator with no assigned issue owns no issue work.
        # Every issue-bearing child still passes this admission independently.
        return {"enforced": True, "disposition": "no_issue"}
    if repository_id <= 0:
        repository_id = await resolve_repository_id(org_id=org_id, installation_id=int(execution["installation_id"]["N"]), repo=repo)
        await run_in_threadpool(
            store.client.update_item,
            TableName=store.table,
            Key={"pk": {"S": f"TENANT#{org_id}"}, "sk": {"S": f"EXEC#{invocation_id}"}},
            UpdateExpression="SET provider_repository_id = :repo",
            ConditionExpression=(
                "attribute_exists(pk) AND #status = :pending AND (attribute_not_exists(provider_repository_id) OR provider_repository_id = :repo)"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":repo": {"N": str(repository_id)}, ":pending": {"S": "pending"}},
        )
    owner = ClaimOwner(OwnerKind.ENGINE_FLOW if grant.authority.kind == "gate_decision" else OwnerKind.DIRECT_DISPATCH, grant.flow_id)

    async def reserve(active_session):
        return await admit(active_session, org_id=org_id, repository_id=repository_id, issue=issue, owner=owner, invocation_id=invocation_id)

    if session is not None:
        return await reserve(session)
    from src.shared.database import get_session_factory

    async with get_session_factory()() as owned_session:
        receipt = await reserve(owned_session)
        await owned_session.commit()
        return receipt


async def maintain_worker_claim(session, *, org_id: str, invocation_id: str, terminal: bool = False) -> None:
    """Called after protected run/workload verification, never with body IDs."""
    row = await session.scalar(
        select(OrchestrationWorkClaim)
        .where(
            OrchestrationWorkClaim.org_id == org_id,
            OrchestrationWorkClaim.claim_event_id == invocation_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        if enabled():
            raise WorkClaimError("claim_missing", "This worker has no admitted work claim.")
        return
    if terminal and row.state == ClaimState.RELEASED.value:
        return
    if row.active_run_id != invocation_id or row.state != ClaimState.HELD.value:
        raise WorkClaimError("claim_not_owned", "This invocation no longer owns the work.")
    if terminal:
        await release_work(
            session,
            org_id=org_id,
            claim_id=row.id,
            generation=row.generation,
            reason=ReleaseReason.COMPLETED,
            terminal_evidence=f"protected terminal report:{invocation_id}",
        )
    else:
        await heartbeat(session, org_id=org_id, claim_id=row.id, generation=row.generation)


async def worker_checkpoint(*, org_id: str, invocation_id: str, terminal: bool = False) -> None:
    from src.shared.database import get_session_factory

    if not enabled():
        return
    async with get_session_factory()() as session:
        await maintain_worker_claim(session, org_id=org_id, invocation_id=invocation_id, terminal=terminal)
        await session.commit()


async def recover_exited_claims(session, *, store, workloads, limit: int = 50) -> int:
    """Bounded recovery after a worker dies before its terminal callback.

    Multiple gateway processes may run this pass. Row locks serialize release;
    protected execution supplies the exact pod identity and Kubernetes supplies
    positive exit evidence. Neither an expired lease nor mutable events suffice.
    """
    rows = list(
        (
            await session.scalars(
                select(OrchestrationWorkClaim)
                .where(
                    OrchestrationWorkClaim.state == ClaimState.HELD.value,
                    OrchestrationWorkClaim.active_run_id.is_not(None),
                )
                .order_by(OrchestrationWorkClaim.heartbeat_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    released = 0
    for row in rows:
        raw = await run_in_threadpool(store._read, f"TENANT#{row.org_id}", f"EXEC#{row.active_run_id}")
        if not raw or raw.get("tenant_id") != {"S": row.org_id}:
            continue
        name, uid = raw.get("pod_name", {}).get("S"), raw.get("workload_binding", {}).get("S")
        if raw.get("status") == {"S": "completed"}:
            evidence = f"protected terminal report:{row.active_run_id}"
        elif name and uid and await run_in_threadpool(workloads.has_exited, name=name, uid=uid):
            evidence = f"kubernetes terminated pod:{uid} run:{row.active_run_id}"
        else:
            continue
        await release_work(
            session, org_id=row.org_id, claim_id=row.id, generation=row.generation, reason=ReleaseReason.ABANDONED, terminal_evidence=evidence
        )
        released += 1
    return released


async def maintain_work_claims() -> None:
    """Lifecycle cleanup in existing gateway processes; no new scheduler."""
    while True:
        try:
            if enabled():
                require_authority()
                from src.agentauth.routes import get_agent_runtime
                from src.shared.database import get_session_factory

                runtime = get_agent_runtime()
                async with get_session_factory()() as session:
                    count = await recover_exited_claims(session, store=runtime.store, workloads=runtime.workloads)
                    await session.commit()
                if count:
                    logger.info("work_claim_recovery released=%s", count)
        except Exception:
            logger.exception("work_claim_recovery failed; unresolved claims remain held")
        await asyncio.sleep(60)
