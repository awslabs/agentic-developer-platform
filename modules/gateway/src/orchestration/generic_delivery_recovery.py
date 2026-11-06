"""Close failed dispatches and their target lease from the provider's result.

Failure does not require a deployment artifact or a still-running agent. A
workflow can fail before either exists. This is the same lifecycle operation
when observing a failure and before dispatching a repaired release.
"""

import re
from collections import defaultdict
from urllib.parse import quote

from sqlalchemy import select

from .delivery_contract import TERMINAL_WORKFLOW_FAILURES
from .deployment_manifest import PhysicalTarget, TargetEvidence
from .deployment_workflow_provider import MAX_LISTING_BYTES, RUNS_PAGE_SIZE
from .environment_leases import LeaseHolder, LeaseOutcomeKind, ReleaseReason, release_lease
from .models import OrchestrationAction, OrchestrationEnvironmentLease, OrchestrationExecution
from .review_cycle import CycleBlockedError


async def _terminal_run(provider, binding, saved, token, *, allow_success=False, reuse_terminal=False):
    """Prove one original dispatch finished; a partial listing proves nothing."""
    pinned = saved.get("dispatch_revision")
    workflow = saved.get("workflow", "")
    if (
        saved.get("repo") != binding.repo
        or saved.get("provider_repository_id") != binding.provider_repository_id
        or re.fullmatch(r"[0-9a-f]{40}", str(pinned or "")) is None
        or re.fullmatch(r"\.github/workflows/[A-Za-z0-9][A-Za-z0-9_.-]*\.ya?ml", workflow) is None
        or not saved.get("correlation")
    ):
        return None
    terminal = saved.get("terminal_run")
    if reuse_terminal and terminal is not None:
        if (
            not isinstance(terminal, dict)
            or type(terminal.get("run_id")) is not int
            or terminal["run_id"] <= 0
            or type(terminal.get("run_attempt")) is not int
            or terminal["run_attempt"] != (saved.get("run_attempt") or 0) + 1
            or terminal.get("conclusion") not in (TERMINAL_WORKFLOW_FAILURES | ({"success"} if allow_success else set()))
            or (saved.get("run_id") is not None and terminal["run_id"] != saved["run_id"])
        ):
            return None
        return terminal
    response = await provider.request(
        binding,
        "GET",
        f"/repos/{binding.repo}/actions/workflows/{quote(workflow.removeprefix('.github/workflows/'))}/runs",
        token=token,
        max_bytes=MAX_LISTING_BYTES,
        params={"event": "workflow_dispatch", "head_sha": pinned, "per_page": RUNS_PAGE_SIZE},
    )
    try:
        listing = response.json()
    except ValueError:
        raise CycleBlockedError("deployment_provider_response_invalid") from None
    runs = listing.get("workflow_runs") if isinstance(listing, dict) else None
    if not isinstance(runs, list) or type(listing.get("total_count")) is not int or listing["total_count"] != len(runs) or len(runs) > RUNS_PAGE_SIZE:
        return None
    matches = [
        run
        for run in runs
        if isinstance(run, dict)
        and run.get("display_title") == "ADP deployment " + saved["correlation"]
        and run.get("path") == workflow
        and run.get("head_sha") == pinned
        and run.get("head_branch") == saved.get("branch")
        and run.get("event") == "workflow_dispatch"
        and (run.get("repository") or {}).get("id") == binding.provider_repository_id
    ]
    if len(matches) != 1:
        return None
    run = matches[0]
    if (
        run.get("status") != "completed"
        or run.get("conclusion") not in (TERMINAL_WORKFLOW_FAILURES | ({"success"} if allow_success else set()))
        or type(run.get("id")) is not int
        or type(run.get("run_attempt")) is not int
        or run["run_attempt"] != (saved.get("run_attempt") or 0) + 1
        or (saved.get("run_id") is not None and run["id"] != saved["run_id"])
    ):
        return None
    return {"run_id": run["id"], "run_attempt": run["run_attempt"], "conclusion": run["conclusion"]}


async def reconcile_retired_target(session, *, binding, target_key, provider, token):
    """Reconcile a retired holder after the caller authorizes this physical target.

    A finished workflow can lack runtime acceptance. Its retired execution must
    not hold the target forever, including when the next authorized deployment
    belongs to another flow. Every original dispatch is independently observed;
    no node, acceptance record, budget or work claim is advanced here.
    """
    lease = await session.scalar(
        select(OrchestrationEnvironmentLease).where(
            OrchestrationEnvironmentLease.canonical_target_key == target_key,
            OrchestrationEnvironmentLease.owner_org_id == binding.org_id,
            OrchestrationEnvironmentLease.state == "held",
        )
    )
    if lease is None or not (lease.owner_action_id or "").startswith("delivery:"):
        return 0
    execution = await session.scalar(
        select(OrchestrationExecution)
        .where(
            OrchestrationExecution.id == lease.owner_action_id.removeprefix("delivery:"),
            OrchestrationExecution.org_id == binding.org_id,
            OrchestrationExecution.phase == "concluded",
            OrchestrationExecution.status.in_(("concluded", "superseded")),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if execution is None or execution.pending_action_key or execution.claim_generation != lease.owner_generation:
        return 0
    actions = list(
        await session.scalars(
            select(OrchestrationAction)
            .where(OrchestrationAction.execution_id == execution.id, OrchestrationAction.org_id == binding.org_id)
            .order_by(OrchestrationAction.id)
            .limit(101)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(actions) > 100 or any(a.kind != "delivery_workflow_dispatch" and a.status != "succeeded" for a in actions):
        return 0
    dispatches = [a for a in actions if a.kind == "delivery_workflow_dispatch"]
    if not dispatches:
        return 0
    terminal = []
    for action in dispatches:
        saved = action.detail or {}
        try:
            physical = saved["target"]
            target = PhysicalTarget(**{**physical, "evidence": TargetEvidence(**physical["evidence"])})
        except (KeyError, TypeError, ValueError):
            return 0
        if (
            target.canonical_key != target_key
            or saved.get("claim_id") != execution.claim_id
            or saved.get("claim_generation") != execution.claim_generation
        ):
            return 0
        run = await _terminal_run(provider, binding, saved, token, allow_success=True, reuse_terminal=action.status == "succeeded")
        if run is None:
            return 0
        job = saved.get("release_job")
        if job is not None and job != {"run_id": str(run["run_id"]), "run_attempt": str(run["run_attempt"])}:
            return 0
        terminal.append((action, run))
    evidence = ",".join(f"github/actions/runs/{run['run_id']}/attempts/{run['run_attempt']}" for _, run in terminal)
    released = await release_lease(
        session,
        canonical_target_key=target_key,
        holder=LeaseHolder(binding.org_id, f"delivery:{execution.id}", execution.claim_generation),
        reason=ReleaseReason.ABANDONED,
        terminal_evidence=f"Retired execution; workflows terminal; runtime acceptance unchanged: {evidence}",
    )
    if released.kind is not LeaseOutcomeKind.APPLIED:
        raise CycleBlockedError("delivery_target_lease_changed")
    for action, run in terminal:
        action.status = "succeeded"
        action.detail = {**action.detail, "terminal_run": run}
    return len(terminal)


async def reconcile_failed_dispatches(session, *, binding, node_id, provider, token, target_key=None):
    """Caller authorizes the node; stored dispatches identify the original work.

    Lock the dispatches, then close the records and lease in
    one transaction. Multiple reservations belonging to one holder must all be
    finished before releasing that holder's target. Canonical lease release
    retains the ownership, generation and unfinished-publication checks.
    """
    records = list(
        await session.execute(
            select(OrchestrationExecution, OrchestrationAction)
            .join(OrchestrationAction, OrchestrationAction.execution_id == OrchestrationExecution.id)
            .where(
                OrchestrationExecution.org_id == binding.org_id,
                OrchestrationExecution.node_id == node_id,
                OrchestrationAction.org_id == binding.org_id,
                OrchestrationAction.kind == "delivery_workflow_dispatch",
                OrchestrationAction.detail["terminal_run"].as_string().is_(None),
            )
            .order_by(OrchestrationExecution.id, OrchestrationAction.id)
            .limit(101)
            .with_for_update(of=OrchestrationAction)
            .execution_options(populate_existing=True)
        )
    )
    if len(records) > 100:
        raise CycleBlockedError("delivery_dispatch_reconciliation_required")
    groups = defaultdict(list)
    for execution, action in records:
        physical = (action.detail or {}).get("target", {})
        target = PhysicalTarget(**{**physical, "evidence": TargetEvidence(**physical["evidence"])})
        groups[(execution.id, target.canonical_key)].append((execution, action))
    reconciled = 0
    for (_, canonical_key), actions in groups.items():
        if target_key is not None and canonical_key != target_key:
            continue
        terminal = []
        for execution, action in actions:
            saved = action.detail or {}
            if saved.get("claim_id") != execution.claim_id or saved.get("claim_generation") != execution.claim_generation:
                break
            run = await _terminal_run(provider, binding, saved, token)
            if run is None:
                break
            terminal.append((action, run))
        if len(terminal) != len(actions):
            continue
        evidence = ",".join(f"github/actions/runs/{run['run_id']}/attempts/{run['run_attempt']}" for _, run in terminal)
        released = await release_lease(
            session,
            canonical_target_key=canonical_key,
            holder=LeaseHolder(binding.org_id, f"delivery:{execution.id}", execution.claim_generation),
            reason=ReleaseReason.FAILED,
            terminal_evidence=evidence,
        )
        if released.kind is not LeaseOutcomeKind.APPLIED:
            raise CycleBlockedError("delivery_target_lease_changed")
        for action, run in terminal:
            action.status = "succeeded"  # Dispatch confirmed; deployment still failed.
            action.detail = {**action.detail, "terminal_run": run}
            reconciled += 1
    return reconciled
