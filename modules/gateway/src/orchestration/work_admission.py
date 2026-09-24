"""Production work-claim integration for trusted dispatch and worker lifecycle.

The switch requires protected agent authority on every producer and worker.
An events-table row or a caller-supplied tenant is never an ownership proof.
This module changes only work claims; it cannot approve or advance a graph.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from src.agentauth.abort_reconciliation import TRANSIENT_REPAIR_FAILURE, AbortRepair, repair_aborted_terminal_status
from src.agentauth.composition import WEBHOOK_EVENTS_TABLE_ENV
from src.agentauth.grants import AUTHORITY_GATE_DECISION, AUTHORITY_GITHUB_EVENT, AUTHORITY_SERVICE_POLICY

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

# Which authority kind files a claim under which owner (#4529). A mapping rather than a
# conditional so an unlisted kind has no owner to fall back to — see the check site.
_CLAIM_OWNER_KINDS: dict[str, OwnerKind] = {
    AUTHORITY_GATE_DECISION: OwnerKind.ENGINE_FLOW,
    AUTHORITY_GITHUB_EVENT: OwnerKind.DIRECT_DISPATCH,
    AUTHORITY_SERVICE_POLICY: OwnerKind.DIRECT_DISPATCH,
}


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

    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or any(part in {".", ".."} for part in repo.split("/")):
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


async def admit_pending(store, invocation_id: str, *, session=None, allow_defer: bool = False) -> dict:
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

    async def with_model_policy(active_session, receipt: dict) -> dict:
        # PMM-06 is deliberately behaviour-neutral until PMM-09 flips the
        # posture. Snapshot failures are returned as evidence and never turn a
        # work-claim success into a producer refusal in report-only mode.
        from src.agentauth.model_policy import ensure_snapshot_report_only

        result = dict(receipt)
        result["model_policy_snapshot"] = await ensure_snapshot_report_only(
            active_session,
            store=store,
            invocation_id=invocation_id,
        )
        return result

    no_issue = issue == 0 and grant.authority.kind == AUTHORITY_SERVICE_POLICY
    # Recognized-authority handling (#4529). The owner line below used to read
    # `ENGINE_FLOW if kind == "gate_decision" else DIRECT_DISPATCH`, so every kind
    # this function had never considered silently claimed work as a direct dispatch.
    # That matters because `owner_kind` decides how a stuck claim is *reconciled*: an
    # engine-owned claim is resolved against the graph, a direct one only against the
    # ingress ledger. A misfiled claim is therefore one nothing can correctly clean up.
    #
    # `replan_request` is refused rather than mapped to an owner, and the reason is a
    # behavioural one, not bookkeeping: a work claim is an *exclusivity* lease on an
    # issue, and the flow's intent issue is where the flow's real work runs. An
    # authoring run taking that lease would block or displace the very plan it was
    # asked to propose a change to. It needs no lease — it reads a bounded request and
    # files a draft — so the honest answer is that it claims nothing.
    if grant.authority.kind not in _CLAIM_OWNER_KINDS:
        raise WorkClaimError("authority_kind_not_recognized", "This authority kind does not claim work.")
    owner = ClaimOwner(_CLAIM_OWNER_KINDS[grant.authority.kind], grant.flow_id)

    async def reserve(active_session):
        try:
            return await admit(active_session, org_id=org_id, repository_id=repository_id, issue=issue, owner=owner, invocation_id=invocation_id)
        except WorkClaimError as exc:
            if not allow_defer or exc.code not in {"held_by_other_owner", "held_lease_lapsed"}:
                raise
            held = await active_session.scalar(
                select(OrchestrationWorkClaim).where(
                    OrchestrationWorkClaim.org_id == org_id,
                    OrchestrationWorkClaim.provider_repository_id == repository_id,
                    OrchestrationWorkClaim.issue_number == issue,
                )
            )
            parent = execution.get("parent_principal", {}).get("S", "").rsplit("#", 1)[0]
            if not parent or held is None or held.owner_ref != owner.ref or held.owner_kind != owner.kind.value or held.active_run_id != parent:
                raise
            # Only an already-authorized direct child of THIS lane's active run
            # may wait. An independent webhook/engine launch is still refused.
            await run_in_threadpool(
                store.client.update_item,
                TableName=store.table,
                Key={k: execution[k] for k in ("pk", "sk")},
                UpdateExpression="SET work_claim_deferred_from = :parent",
                ConditionExpression="#st = :pending AND attribute_not_exists(workload_binding)",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":parent": {"S": parent}, ":pending": {"S": "pending"}},
            )
            return {"disposition": "waiting_for_owner", "invocation_id": invocation_id}

    if session is not None:
        receipt = {"enforced": True, "disposition": "no_issue"} if no_issue else await reserve(session)
        return await with_model_policy(session, receipt)
    from src.shared.database import get_session_factory

    async with get_session_factory()() as owned_session:
        # Resolve the repository for future issue-bearing child dispatches even
        # though the scheduled coordinator itself owns no assigned issue.
        receipt = {"enforced": True, "disposition": "no_issue"} if no_issue else await reserve(owned_session)
        receipt = await with_model_policy(owned_session, receipt)
        await owned_session.commit()
        return receipt


async def admit_deferred_bootstrap(store, invocation_id: str, digest: str) -> None:
    """Called after pod verification, before binding or returning any credential."""
    if not enabled():
        return
    from src.agentauth.bootstrap import BootstrapRefusedError

    pointer = await run_in_threadpool(store._read, f"INVOCATION#{invocation_id}", "DISPATCH")
    org_id = (pointer or {}).get("tenant_id", {}).get("S")
    if not org_id:
        raise BootstrapRefusedError("dispatch unavailable")
    raw = await run_in_threadpool(store._read, f"TENANT#{org_id}", f"EXEC#{invocation_id}")
    if not raw or raw.get("envelope_digest") != {"S": digest}:
        raise BootstrapRefusedError("dispatch unavailable")
    if not raw.get("work_claim_deferred_from") or raw.get("status") != {"S": "pending"}:
        return
    if await run_in_threadpool(cancel_unstarted_claim, store, raw, now=datetime.now(UTC)):
        await run_in_threadpool(
            store.authority.release_dispatch,
            tenant_id=org_id,
            grant_id=raw["parent_grant_id"]["S"],
            reservation_id=raw["dispatch_reservation_id"]["S"],
        )
        raise BootstrapRefusedError("startup deadline exceeded")
    try:
        await admit_pending(store, invocation_id)
    except WorkClaimError as exc:
        if exc.code in {"held_by_other_owner", "held_lease_lapsed", "claim_race_lost"}:
            raise WorkClaimError("work_waiting", "Authorized child is waiting for exclusive ownership.") from None
        raise


async def maintain_worker_claim(
    session, *, org_id: str, invocation_id: str, terminal: bool = False, reason: ReleaseReason = ReleaseReason.COMPLETED
) -> None:
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
        # Process status is advisory. The locked release primitive preserves a
        # current continuation without rejecting a valid terminal status report.
        await release_work(
            session,
            org_id=org_id,
            claim_id=row.id,
            generation=row.generation,
            reason=reason,
            terminal_evidence=f"protected terminal report:{invocation_id}",
        )
    else:
        await heartbeat(session, org_id=org_id, claim_id=row.id, generation=row.generation)


async def worker_checkpoint(
    *, org_id: str, invocation_id: str, terminal: bool = False, store=None, reason: ReleaseReason = ReleaseReason.COMPLETED
) -> None:
    from src.shared.database import get_session_factory

    if not enabled():
        return
    if store is not None:
        raw = await run_in_threadpool(store._read, f"TENANT#{org_id}", f"EXEC#{invocation_id}")
        if raw and raw.get("issue_number") == {"N": "0"}:
            grant = await run_in_threadpool(store._read, f"TENANT#{org_id}", f"GRANT#{invocation_id}#1")
            if grant and grant.get("authority_kind") == {"S": "service_policy"}:
                return
    async with get_session_factory()() as session:
        await maintain_worker_claim(session, org_id=org_id, invocation_id=invocation_id, terminal=terminal, reason=reason)
        await session.commit()


@dataclass(frozen=True)
class ClaimRecoveryReport:
    released: int
    next_id: str | None
    # Aborted runs whose terminal dashboard row this pass repaired (#3963 S4).
    # Counted separately from `released` because they mean something different to an
    # operator: a release is routine lifecycle cleanup, while a repair means a run
    # they deliberately stopped had been showing as live since it died.
    aborts_repaired: int = 0


def cancel_unstarted_claim(store, raw: dict, *, now: datetime) -> bool:
    """Fence a dispatch that never bootstrapped within the startup deadline.

    This is a conditional cancellation, not a liveness inference from a lease.
    A concurrently bootstrapping pod wins the same protected status transition;
    cancellation then fails and its claim remains held. A delayed queue delivery
    cannot bootstrap after cancellation and cannot receive action credentials.
    """
    if raw.get("workload_binding") or raw.get("status") != {"S": "pending"}:
        return False
    arrived = raw.get("arrived_at", {}).get("S", "")
    try:
        created = datetime.fromisoformat(arrived.replace("Z", "+00:00"))
        if created.tzinfo is None or (now - created).total_seconds() < 1800:
            return False
        store.client.update_item(
            TableName=store.table,
            Key={k: raw[k] for k in ("pk", "sk")},
            UpdateExpression="SET #st = :cancelled, work_claim_cancellation = :reason",
            ConditionExpression="#st = :pending AND attribute_not_exists(workload_binding) AND arrived_at = :arrived",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":pending": {"S": "pending"},
                ":cancelled": {"S": "cancelled"},
                ":arrived": {"S": arrived},
                ":reason": {"S": "startup_deadline_exceeded"},
            },
        )
    except (KeyError, ValueError, TypeError, ClientError, BotoCoreError):
        return False
    logger.warning("work claim cancelled before bootstrap tenant=%s invocation=%s", raw["tenant_id"]["S"], raw["invocation_id"]["S"])
    return True


async def recover_exited_claims(session, *, store, workloads, limit: int = 50, after_id: str | None = None) -> ClaimRecoveryReport:
    """Bounded recovery after a worker dies before its terminal callback.

    Multiple gateway processes may run this pass. Row locks serialize release;
    protected execution supplies the exact pod identity and Kubernetes supplies
    positive exit evidence. Neither an expired lease nor mutable events suffice.
    """
    rows = list(
        (
            await session.execute(
                select(
                    OrchestrationWorkClaim.id, OrchestrationWorkClaim.org_id, OrchestrationWorkClaim.generation, OrchestrationWorkClaim.active_run_id
                )
                .where(
                    OrchestrationWorkClaim.state == ClaimState.HELD.value,
                    OrchestrationWorkClaim.active_run_id.is_not(None),
                    OrchestrationWorkClaim.id > (after_id or ""),
                )
                .order_by(OrchestrationWorkClaim.id)
                .limit(limit)
            )
        ).all()
    )
    releasable = []
    repaired = 0
    for row in rows:
        raw = await run_in_threadpool(store._read, f"TENANT#{row.org_id}", f"EXEC#{row.active_run_id}")
        if not raw or raw.get("tenant_id") != {"S": row.org_id}:
            continue
        name, uid = raw.get("pod_name", {}).get("S"), raw.get("workload_binding", {}).get("S")
        reason = ReleaseReason.ABANDONED
        if raw.get("status") == {"S": "completed"}:
            evidence = f"protected terminal report:{row.active_run_id}"
            reason = ReleaseReason.COMPLETED if raw.get("terminal_outcome") == {"S": "completed"} else ReleaseReason.FAILED
        elif (raw.get("status") == {"S": "cancelled"} and not uid) or await run_in_threadpool(
            cancel_unstarted_claim, store, raw, now=datetime.now(UTC)
        ):
            evidence = f"protected cancellation before bootstrap:{row.active_run_id}"
            reason = ReleaseReason.FAILED
        elif name and uid and await run_in_threadpool(workloads.has_exited, name=name, uid=uid):
            evidence = f"kubernetes terminated pod:{uid} run:{row.active_run_id}"
            # The one branch that reaches here on positive container-exit evidence
            # rather than on a report the run wrote, which is precisely the case where
            # an aborted run's terminal row may be missing: the pod died without
            # managing to write it. Repaired here and nowhere else — the other branches
            # mean the run DID report, so there is nothing to repair, and a repair
            # before `has_exited` would assert a stop that has not happened yet
            # (`record_abort_intent` leaves the execution ACTIVE on purpose, so the
            # marker alone is acceptance, not quiescence).
            #
            # Gated on the marker here as well as inside the repair. The duplication is
            # deliberate: the overwhelmingly common case is an exited run that was never
            # aborted, and this keeps that case from reaching the events table at all.
            # The repair re-checks because it must not depend on a caller's filter for a
            # condition that decides whether a terminal status gets written.
            #
            # Repaired BEFORE the release, and the release is skipped while the repair is
            # still failing transiently. That ordering is load-bearing, not stylistic:
            # this query selects `state == HELD`, so releasing first would remove the row
            # from the only set that ever looks at it again and a failed repair would
            # never be retried — the permanently-stale row this reconciler exists to
            # prevent. Leaving the claim held is what makes the next 60s pass re-select
            # it, so the existing sweep IS the retry, with no attempt counter and no new
            # state. See `_repair_before_release` for what "transiently" is limited to.
            if "abort_command_id" in raw:
                repair = await _repair_before_release(store=store, invocation_id=row.active_run_id, tenant_id=row.org_id, raw=raw)
                repaired += int(repair.repaired)
                if repair.reason == TRANSIENT_REPAIR_FAILURE:
                    # Hold the claim and try again next pass. The cost is that this
                    # issue's lane stays blocked while the events table is down; the
                    # alternative is telling the operator their aborted run is still
                    # running, forever. Bounded to the transient reason precisely so
                    # anything a retry cannot fix — an unset table name or a tenant
                    # disagreement, both of which could persist for weeks — falls through
                    # and releases instead of wedging the lane indefinitely.
                    logger.warning(
                        "holding work claim to retry an aborted run's terminal repair invocation=%s", row.active_run_id
                    )
                    continue
        else:
            continue
        parent_grant = raw.get("parent_grant_id", {}).get("S")
        reservation = raw.get("dispatch_reservation_id", {}).get("S")
        continuation = bool(raw.get("orchestration_continuation_receipt"))
        if bool(parent_grant) != bool(reservation) and not (continuation and parent_grant and not reservation):
            logger.error("work claim recovery missing reservation binding invocation=%s", row.active_run_id)
            continue
        if parent_grant and reservation:
            await run_in_threadpool(store.authority.release_dispatch, tenant_id=row.org_id, grant_id=parent_grant, reservation_id=reservation)
        releasable.append((row, reason, evidence))
    released = 0
    for row, reason, evidence in releasable:
        # Never hold a claim lock across an external read. The captured generation
        # fences this release if ownership changed while Kubernetes was queried.
        receipt = await release_work(
            session, org_id=row.org_id, claim_id=row.id, generation=row.generation, reason=reason, terminal_evidence=evidence
        )
        released += int(receipt.admitted)
    return ClaimRecoveryReport(released, rows[-1].id if len(rows) == limit else None, repaired)


async def _repair_before_release(*, store, invocation_id: str, tenant_id: str, raw: dict) -> AbortRepair:
    """Report an exited aborted run's terminal status, off the event loop.

    Separated from the sweep body so the ordering constraint above has one place to
    point at, and so the threadpool hop (boto3 is blocking) is not buried mid-branch.

    Never raises. The caller's primary job is releasing work claims, and a repair that
    could raise would let an events-table outage abandon claim recovery for every
    tenant — strictly worse than a dashboard row that is another minute late.
    """
    return await run_in_threadpool(
        repair_aborted_terminal_status,
        authority_client=store.client,
        events_table=os.environ.get(WEBHOOK_EVENTS_TABLE_ENV, ""),
        execution=raw,
        invocation_id=invocation_id,
        tenant_id=tenant_id,
    )


async def maintain_work_claims() -> None:
    """Lifecycle cleanup in existing gateway processes; no new scheduler."""
    cursor = None
    retained_cursor = ""
    while True:
        # Recovery is independent of SQL claims and runs first so a database
        # outage cannot discard accepted-abort reporting work.
        try:
            if os.environ.get("AGENT_AUTHORITY_ENABLED", "false").lower() == "true":
                from src.agentauth.retained_abort_recovery import recover_retained_abort_pods
                from src.agentauth.routes import get_agent_runtime

                runtime = get_agent_runtime()
                _, retained_cursor = await run_in_threadpool(
                    recover_retained_abort_pods, store=runtime.store,
                    workloads=runtime.workloads,
                    events_table=os.environ.get(WEBHOOK_EVENTS_TABLE_ENV, ""),
                    cursor=retained_cursor,
                )
        except Exception:
            logger.exception("retained abort recovery failed; pod evidence remains")
            retained_cursor = ""
        try:
            if enabled():
                require_authority()
                from src.agentauth.routes import get_agent_runtime
                from src.shared.database import get_session_factory

                runtime = get_agent_runtime()
                async with get_session_factory()() as session:
                    report = await recover_exited_claims(session, store=runtime.store, workloads=runtime.workloads, after_id=cursor)
                    await session.commit()
                cursor = report.next_id
                if report.released:
                    logger.info("work_claim_recovery released=%s", report.released)
                if report.aborts_repaired:
                    # Its own line, at warning: this is not routine cleanup. Each one
                    # is a run an operator stopped that has been reporting itself as
                    # live ever since, so the count is the size of a reporting outage
                    # and not a throughput statistic.
                    logger.warning("abort_terminal_repaired count=%s", report.aborts_repaired)
        except Exception:
            logger.exception("work_claim_recovery failed; unresolved claims remain held")
        await asyncio.sleep(60)
