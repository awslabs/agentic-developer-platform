"""Read-only CI evidence for the currently assigned reviewer, using merge policy."""

from __future__ import annotations

import copy
from dataclasses import asdict, replace
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from src.shared.database import get_session_factory

from .execution_policy import Action, CredentialScope, ResourceRef, authorize_action
from .merge_evidence import EligibilityReason, EvidenceUnavailableError, GitHubMergeObserver, evaluate_observation, evaluate_required_checks
from .merge_provider import MergeProvider
from .merge_review import load_merge_review
from .models import OrchestrationNode
from .pr_bindings import active_binding_for_node, binding_scope_matches
from .review_cycle import CycleBlockedError
from .run_reports import RunReportError, authenticate_run_report
from .shared_cycle import validate_current_report_assignment
from .shared_policy import authorize_shared_action


class ReviewChecksRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    head_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    for_merge: bool = False


async def observe_reviewer_checks(session, *, credential=None, head_sha, for_merge=False, provider=None, storage=None, protected=None):
    async def assigned():
        if protected is None:
            row = await authenticate_run_report(session, credential, lock=False)
            execution, identity = await validate_current_report_assignment(session, row)
        else:
            row, execution, identity = await protected_assignment(session, *protected)
        if for_merge and execution.deadline_at and execution.deadline_at <= datetime.now(UTC):
            raise RunReportError("reviewer_deadline_exceeded", retryable=False)
        cycle = row.dispatch_metadata.get("review_cycle_input") or {}
        expected = row.dispatch_metadata.get("review_expect") or {}
        if (
            row.persona != "agent-codex-reviewer"
            or cycle.get("reviewer_owned_delivery") is not True
            or not expected
            or expected.get("author_run_id") == row.run_id
            or row.terminal_receipt
        ):
            raise RunReportError("review_checks_not_assigned", retryable=False)
        node = await session.get(OrchestrationNode, row.node_id, populate_existing=True)
        binding = await active_binding_for_node(session, org_id=row.org_id, node_id=row.node_id, attempt=row.attempt)
        if (
            binding is None
            or not binding_scope_matches(binding, node)
            or binding.repo != row.repo
            or binding.provider_repository_id != row.provider_repository_id
            or binding.installation_id != row.installation_id
            or binding.pr_number != expected.get("pr_number")
            or binding.provider_pr_node_id != expected.get("provider_pr_node_id")
        ):
            raise RunReportError("review_checks_binding_changed", retryable=False)
        context = SimpleNamespace(execution=execution, identity=identity)
        if protected is None:
            await authorize_shared_action(
                session, context, node, binding, row.run_id, Action.MERGE if for_merge else Action.REVIEW, reserve=False, observation=True
            )
        else:
            from .review_cycle_dispatch import ReviewCycleServices

            authority = ReviewCycleServices(get_session_factory())
            if for_merge:
                from .dispatch import graph_address
                from .models import OrchestrationFlow
                from .runtime_policy import policy_github_permissions

                facts = await authority.authority_context(session, context, node, binding, row.run_id, Action.MERGE)
                # The reviewer already holds the repository-scoped repair token.
                # MERGE is checked independently against current policy; it does
                # not mint or delegate a broader worker capability.
                scope = (
                    CredentialScope.SCOPED
                    if (cycle.get("allow_story_repairs") is True and policy_github_permissions(facts[2].policy, Action.REPAIR))
                    else CredentialScope.UNSCOPABLE
                )
                flow = await session.get(OrchestrationFlow, node.flow_id)
                decision = authorize_action(
                    replace(facts[-1], credential_scope=scope),
                    Action.MERGE,
                    ResourceRef(repository_id=binding.repo, node_address=graph_address(node, flow_slug=flow.slug), org_id=node.org_id),
                    facts[2].plan_version,
                )
                if not decision.permitted:
                    raise CycleBlockedError(decision.reason.value)
            else:
                await authority.authorize(session, context, node, binding, row.run_id, Action.REVIEW)
        if for_merge:
            # Read-only authorization of an accepted review. The worker performs
            # the merge; this endpoint neither mutates GitHub nor schedules work.
            await load_merge_review(
                session,
                context=SimpleNamespace(execution=execution, identity=identity),
                node=node,
                binding=binding,
                reviewer_run_id=row.run_id,
                raw_execution={"current_attempt": {"N": "1"}},
                head_sha=head_sha,
                storage=storage,
            )
        return row, binding

    row, binding = await assigned()
    assignment = (row.run_id, binding.id, binding.revision, copy.deepcopy(row.dispatch_metadata))
    provider = provider or MergeProvider()
    token = await provider.token(binding, evidence=True)
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
        observer = GitHubMergeObserver(client, token, lambda: datetime.now(UTC))
        observation = await observer.observe(binding)
        if observation.head_sha != head_sha:
            raise RunReportError("review_checks_head_changed", retryable=False)
        reasons, checks = evaluate_required_checks(observation)
        # Bounded provider output supplies concrete failure context to the same
        # repair thread. The model does not receive the observation credential.
        failures = []
        for check in checks:
            if check.state in {"success", "skipped", "neutral", "pending"}:
                continue
            item = asdict(check)
            if check.source == "check_run" and len(failures) < 4:
                raw = await observer.get(f"/repos/{binding.repo}/check-runs/{check.provider_id}", kind="failed_check")
                if raw.get("head_sha") != head_sha or raw.get("id") != check.provider_id:
                    raise RunReportError("review_checks_head_changed", retryable=False)
                output = raw.get("output") or {}
                item["details"] = "\n".join(str(output.get(key) or "") for key in ("title", "summary", "text"))[:4000]
            failures.append(item)
        session.expire_all()
        current, current_binding = await assigned()
        if (current.run_id, current_binding.id, current_binding.revision, current.dispatch_metadata) != assignment:
            raise RunReportError("review_checks_assignment_changed", retryable=False)
    state = "failed" if EligibilityReason.REQUIRED_CHECK_FAILED in reasons else "pending" if reasons else "passed"
    result = {
        "contract_version": 1,
        "run_id": row.run_id,
        "attempt": row.attempt,
        "head_sha": observation.head_sha,
        "base_sha": observation.base_sha,
        "open": observation.open,
        "merged": observation.merged,
        "base_repair_required": observation.mergeable is False
        or observation.mergeable_state == "dirty"
        or (observation.requirements.strict_checks and observation.mergeable_state == "behind"),
        "state": state,
        "reasons": [r.value for r in reasons],
        "checks": [asdict(check) for check in checks][:100],
        "failures": failures[:4],
    }
    if for_merge:
        eligibility = evaluate_observation(observation)
        methods = observation.requirements.allowed_merge_methods
        result.update(
            merge_state=eligibility.state.value,
            merge_reasons=[reason.value for reason in eligibility.reasons],
            merge_method="queue"
            if observation.requirements.queue_required
            else next((m for m in ("squash", "merge", "rebase") if m in methods), None),
            pr_node_id=observation.pr_node_id,
        )
    return result


async def review_checks(body: ReviewChecksRequest, request: Request):
    try:
        async with get_session_factory()() as session:
            return await observe_reviewer_checks(
                session, credential=request.headers.get("X-Adp-Report-Credential", ""), head_sha=body.head_sha, for_merge=body.for_merge
            )
    except RunReportError as error:
        raise HTTPException(503 if error.retryable else 409, error.code) from None
    except CycleBlockedError as error:
        raise HTTPException(409, error.reason) from None
    except (EvidenceUnavailableError, httpx.HTTPError):
        raise HTTPException(503, "review_checks_unavailable") from None


async def protected_assignment(session, request, runtime):
    """Use the existing workload-bound credential and committed dispatcher receipt."""
    from starlette.concurrency import run_in_threadpool

    from src.agentauth.run_services import live_context

    from .execution_state import OutcomeKind
    from .execution_store import load_execution
    from .handoff import identity_for_attempt
    from .review_assignment import review_assignment

    _, _, record, _ = await live_context(request, runtime)
    raw = await run_in_threadpool(runtime.store._read, f"TENANT#{record.tenant_id}", f"EXEC#{record.invocation_id}")
    node_id = (raw or {}).get("orchestration_node_id", {}).get("S")
    attempt = int((raw or {}).get("orchestration_node_attempt", {}).get("N", "0"))
    if not node_id or attempt < 1:
        raise RunReportError("review_checks_not_assigned")
    identity = await identity_for_attempt(session, org_id=record.tenant_id, node_id=node_id, attempt=attempt)
    loaded = await load_execution(session, identity=identity) if identity else None
    metadata = await review_assignment(session, org_id=record.tenant_id, node_id=node_id, run_id=record.invocation_id)
    if loaded is None or loaded.kind is not OutcomeKind.APPLIED or not metadata:
        raise RunReportError("execution_assignment_unverifiable")
    source = metadata["source_ref"]
    row = SimpleNamespace(
        run_id=record.invocation_id,
        org_id=record.tenant_id,
        node_id=node_id,
        attempt=attempt,
        persona=metadata["persona"],
        repo=source["repo"],
        installation_id=int(source["installation_id"]),
        provider_repository_id=source["provider_repository_id"],
        terminal_receipt=None,
        dispatch_metadata=metadata,
    )
    return row, loaded.record, identity


async def protected_review_checks(body, request, runtime):
    try:
        async with get_session_factory()() as session:
            return await observe_reviewer_checks(session, head_sha=body.head_sha, for_merge=body.for_merge, protected=(request, runtime))
    except RunReportError as error:
        raise HTTPException(503 if error.retryable else 409, error.code) from None
    except CycleBlockedError as error:
        raise HTTPException(409, error.reason) from None
    except (EvidenceUnavailableError, httpx.HTTPError):
        raise HTTPException(503, "review_checks_unavailable") from None
