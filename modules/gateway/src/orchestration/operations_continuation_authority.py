"""Service authority for finishing a settled operations repair, without a worker retry.

The original assignment and terminal report remain immutable. A service decision
transfers its released claim to a deterministic controller identity. The transfer
licenses only merge, deployment and observation of the already published repair.
"""

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.github_operation_service import AuthorizedOperation, OperationsGrant, build_assignment
from src.agentauth.github_operations import GitHubOperation
from src.agentauth.grants import AUTHORITY_GATE_DECISION, AgentAction

from .accepted_node import accepted_node_contract
from .continuation import digest
from .developer_personas import OPERATIONS_PERSONAS, operations_executor_enabled
from .dispatch import graph_address
from .dispatch_pass import attempt_run_id
from .execution_policy import Action, CredentialScope, ResourceRef, authorize_action
from .execution_runner import RunnerContext
from .execution_state import BlockCode, ExecutionIdentity, ExecutionPhase, ExecutionStatus, OutcomeKind, PhaseAdvance
from .execution_store import advance_execution, load_execution
from .genesis import resolve_engine_genesis
from .models import OrchestrationAction, OrchestrationDecision, OrchestrationExecution, OrchestrationFlow, OrchestrationNode, OrchestrationWorkClaim
from .operations_release_resolution import operational_execution_pins
from .operations_repair import repair_revisions
from .plan_lineage import approval_authorizes_node
from .review_cycle import CycleBlockedError
from .review_cycle_dispatch import ReviewCycleServices
from .run_reports import OrchestrationRunReport
from .stage_attempts import stage_attempts

KIND = "operations_repair_continuation"
ACTOR = "system:operations-continuation"
PREFIX = "operations-continuation:"


def require(condition, reason):
    if not condition:
        raise CycleBlockedError(reason, BlockCode.AUTHORITY_UNVERIFIABLE)


def report_digest(report):
    return digest(
        dict(
            run_id=report.run_id,
            org_id=report.org_id,
            flow_id=report.flow_id,
            node_id=report.node_id,
            attempt=report.attempt,
            persona=report.persona,
            repo=report.repo,
            installation_id=report.installation_id,
            repository_id=report.provider_repository_id,
            dispatch=report.dispatch_metadata,
            terminal=report.terminal_receipt,
        )
    )


def controller_run(decision_id):
    return PREFIX + decision_id


def repair_identity(action):
    return dict(
        id=action.id,
        key=action.operation_key,
        receipt=action.receipt_ref,
        pull_number=(action.detail or {}).get("pull_number"),
        head=(action.detail or {}).get("head"),
    )


async def transfer(session, *, node, execution, report):
    """Validate a durable service transfer; its mere presence is never authority."""
    ref = execution.handoff_receipt_ref or ""
    if not ref.startswith(PREFIX):
        return None
    decision = await session.get(OrchestrationDecision, ref.removeprefix(PREFIX))
    if decision is None:
        return None
    try:
        data = json.loads(decision.reason)
    except (TypeError, ValueError):
        return None
    fences = (report.dispatch_metadata or {}).get("handoff_expect") or {}
    claim = await session.get(OrchestrationWorkClaim, execution.claim_id)
    if not (
        decision.kind == KIND
        and decision.actor_id == ACTOR
        and decision.actor_kind == "service"
        and decision.org_id == node.org_id == execution.org_id == report.org_id
        and decision.flow_id == node.flow_id == execution.flow_id == report.flow_id
        and decision.node_id == node.id == execution.node_id == report.node_id
        and execution.cycle == node.attempts == report.attempt == data.get("attempt")
        and report.run_id == attempt_run_id(node.id, node.attempts) == data.get("source_run_id")
        and report_digest(report) == data.get("source_report_digest")
        and data.get("execution_id") == execution.id == fences.get("execution_id")
        and data.get("accepted_plan_version") == execution.accepted_plan_version == fences.get("accepted_plan_version")
        and data.get("claim_id") == execution.claim_id == fences.get("claim_id")
        and data.get("prior_claim_generation") == fences.get("claim_generation")
        and type(data.get("prior_claim_generation")) is int
        and data.get("claim_generation") == data["prior_claim_generation"] + 1 == execution.claim_generation
        and claim is not None
        and claim.org_id == node.org_id
        and claim.state == "held"
        and claim.generation == execution.claim_generation
        and claim.owner_kind == "engine_flow"
        and claim.owner_ref == node.flow_id
        and claim.active_run_id == ref == claim.claim_event_id
        and claim.provider_repository_id == report.provider_repository_id
        and claim.issue_number == int(str(node.issue_ref).lstrip("#"))
    ):
        return None
    repairs = list(
        await session.scalars(
            select(OrchestrationAction)
            .where(
                OrchestrationAction.org_id == node.org_id,
                OrchestrationAction.execution_id == execution.id,
                OrchestrationAction.kind == "operations_repair_pr",
                OrchestrationAction.status == "succeeded",
            )
            .order_by(OrchestrationAction.id)
            .limit(101)
        )
    )
    if not repairs or len(repairs) > 100 or [repair_identity(action) for action in repairs] != data.get("repairs"):
        return None
    return data


async def assignment_matches(session, *, node, execution, report):
    fences = (report.dispatch_metadata or {}).get("handoff_expect") or {}
    from .operations_agent_recovery import PREFIX as RETURN_PREFIX
    from .operations_agent_recovery import closed_assignment

    if (execution.handoff_receipt_ref or "").startswith(RETURN_PREFIX):
        return await closed_assignment(session, node=node, execution=execution, report=report) is not None
    if execution.handoff_receipt_ref and execution.handoff_receipt_ref.startswith(PREFIX):
        return await transfer(session, node=node, execution=execution, report=report) is not None
    return (
        execution.id == fences.get("execution_id")
        and execution.accepted_plan_version == fences.get("accepted_plan_version")
        and execution.claim_id == fences.get("claim_id")
        and execution.claim_generation == fences.get("claim_generation")
    )


class OperationsContinuationServices:
    def __init__(self, factory, *, authority=None):
        self.factory = factory
        self.authority = authority or ReviewCycleServices(factory)

    async def source(self, session, node, report):
        require(operations_executor_enabled(), "operations_executor_disabled")
        require(os.environ.get("AGENT_AUTHORITY_ENABLED", "false").lower() == "true", "protected_authority_required")
        run_id = attempt_run_id(node.id, node.attempts)
        raw = await self.authority.protected(node.org_id, run_id)
        terminal = report.terminal_receipt or {}
        require(
            report.run_id == run_id
            and report.org_id == node.org_id
            and report.flow_id == node.flow_id
            and report.node_id == node.id
            and report.attempt == node.attempts
            and report.persona in OPERATIONS_PERSONAS
            and terminal.get("outcome") in {"failed", "complete"}
            and terminal.get("run_id") == run_id
            and terminal.get("attempt") == node.attempts
            and raw
            and raw.get("status") == {"S": "completed"}
            and raw.get("terminal_outcome") == {"S": terminal["outcome"]}
            and raw.get("tenant_id") == {"S": node.org_id}
            and raw.get("flow_id") == {"S": node.flow_id}
            and raw.get("orchestration_node_id") == {"S": node.id}
            and raw.get("orchestration_node_attempt") == {"N": str(node.attempts)}
            and raw.get("invocation_id") == {"S": run_id}
            and raw.get("persona") == {"S": report.persona}
            and raw.get("repo") == {"S": report.repo}
            and raw.get("installation_id") == {"N": str(report.installation_id)}
            and raw.get("provider_repository_id") == {"N": str(report.provider_repository_id)}
            and raw.get("issue_number") == {"N": str(int(str(node.issue_ref).lstrip("#")))}
            and raw.get("workload_binding", {}).get("S")
            and not raw.get("orchestration_continuation_receipt"),
            "operations_continuation_source_unverified",
        )
        try:
            grant = await asyncio.to_thread(
                self.authority.writer.store.live_grant,
                invocation_id=run_id,
                tenant_id=node.org_id,
                attempt=int(raw["current_attempt"]["N"]),
                now=datetime.now(UTC),
            )
            genesis = await resolve_engine_genesis(session, org_id=node.org_id, decision_id=grant.authority.reference_id)
        except (BootstrapRefusedError, KeyError, TypeError, ValueError):
            raise CycleBlockedError("operations_continuation_source_authority_unverified") from None
        require(
            grant.flow_id == node.flow_id
            and grant.authority.kind == AUTHORITY_GATE_DECISION
            and report.repo in grant.repo_scope
            and AgentAction.MONITOR in grant.delegable_actions
            and genesis.flow_id == node.flow_id
            and genesis.root_human_id == grant.authority.human_id
            and await approval_authorizes_node(session, org_id=node.org_id, flow_id=node.flow_id, node_id=node.id, decision_id=genesis.decision_id),
            "operations_continuation_source_authority_changed",
        )
        return raw, grant

    async def authorize(self, session, context, action, *, recovering=False, settling=False):
        require(action in {Action.OPERATE, Action.MERGE, Action.DEPLOY}, "operations_continuation_action_refused")
        node = await session.get(OrchestrationNode, context.identity.node_id, populate_existing=True)
        row = await session.get(OrchestrationExecution, context.execution.id, populate_existing=True)
        require(
            node is not None
            and row is not None
            and node.org_id == context.identity.org_id
            and node.attempts == context.identity.cycle
            and row.claim_generation == context.identity.claim_generation,
            "operations_continuation_assignment_changed",
        )
        report = await session.get(OrchestrationRunReport, attempt_run_id(node.id, node.attempts), populate_existing=True)
        require(report is not None, "operations_continuation_source_missing")
        contract = await accepted_node_contract(session, node)
        require(
            contract is not None
            and contract.plan_version == row.accepted_plan_version
            and contract.node.executor is not None
            and contract.node.executor.role == "operate"
            and contract.node.executor.persona == report.persona
            and contract.node.repository_identity == report.repo,
            "operations_continuation_contract_changed",
        )
        data = None if recovering else await transfer(session, node=node, execution=row, report=report)
        require(
            recovering
            or data is not None
            and node.state == "running"
            and (row.status not in {"concluded", "superseded"} or settling and row.status == "concluded"),
            "operations_continuation_transfer_changed",
        )
        flow = await session.get(OrchestrationFlow, node.flow_id, populate_existing=True)
        require(
            flow is not None
            and flow.org_id == node.org_id
            and flow.state in {"pending", "running"}
            and not flow.execution_paused
            and flow.archived_at is None,
            "flow_not_active",
        )
        raw, grant = await self.source(session, node, report)
        binding = SimpleNamespace(
            org_id=node.org_id, repo=report.repo, installation_id=report.installation_id, provider_repository_id=report.provider_repository_id
        )
        run_id = report.run_id if recovering else row.handoff_receipt_ref
        values = await self.authority.policy_context(session, context, node, binding, run_id, action, raw, grant)
        policy, auth = values[2].policy, values[-1]
        require(all(policy.permits(stage) for stage in (Action.OPERATE, Action.MERGE, Action.DEPLOY)), "operations_continuation_not_accepted")
        require(recovering or data.get("policy_hash") == policy.policy_hash, "operations_continuation_policy_changed")
        used = await stage_attempts(session, org_id=node.org_id, node_id=node.id, action=action, exclude_operation_key=row.pending_action_key)
        # Provider credentials stay inside the controller and are scoped to this
        # exact operation, just as in the merge controller.
        # This authorizes observation/reconciliation of an admitted stage. The
        # runner separately checks the full count before reserving each effect.
        auth = replace(auth, credential_scope=CredentialScope.SCOPED, observed_attempts=max(0, used - 1))
        if not recovering:
            connections = tuple(contract.environment_connection_ids) if action is Action.DEPLOY else ()
            require(action is not Action.DEPLOY or len(connections) == 1, "operations_continuation_target_ambiguous")
            decision = authorize_action(
                auth,
                action,
                ResourceRef(
                    repository_id=binding.repo,
                    node_address=graph_address(node, flow_slug=flow.slug),
                    org_id=node.org_id,
                    environment_connection_id=connections[0] if connections else None,
                ),
                contract.plan_version,
            )
            require(decision.permitted, f"operations_continuation_{getattr(decision.reason, 'value', 'refused')}")
        pins = await operational_execution_pins(session, node=node, execution=row, contract=contract, run_id=report.run_id)
        require(pins is not None, "operations_continuation_release_changed")
        effective = await repair_revisions(
            session,
            node_id=node.id,
            org_id=node.org_id,
            plan_version=contract.plan_version,
            cycle=node.attempts,
            pins=pins,
            contract=contract,
            require_merged=action is Action.DEPLOY,
        )
        require(effective is not None, "operations_continuation_repair_changed")
        execution = {**raw, **{key: {"S": value} for key, value in effective.items()}}
        workflows = frozenset(
            item.source.removeprefix("deployment_workflow:")
            for item in contract.node.required_inputs
            if item.source.startswith("deployment_workflow:")
        )
        require(len(workflows) == 1 and len(contract.node.evidence_expectations) == 1, "operations_continuation_scope_unsupported")
        issued = OperationsGrant(
            "operations",
            binding.repo,
            workflows,
            {"actions": "write", "metadata": "read"},
            node.id,
            contract.plan_version,
            getattr(contract.node.operational_release_binding, "product_cases", "none"),
            getattr(contract.node.operational_release_binding, "proof_phase", "none"),
            getattr(contract.node.operational_release_binding, "delivery_contract", None),
        )
        assignment = build_assignment(
            execution=execution,
            decision=SimpleNamespace(
                provider_repository_id=binding.provider_repository_id,
                plan_version=contract.plan_version,
                not_after=min(policy.expires_at, grant.expires_at),
            ),
            workload_binding=ACTOR,
            claim_generation=row.claim_generation,
            now=datetime.now(UTC),
        )
        authorized = AuthorizedOperation(assignment, GitHubOperation.MERGE_PULL_REQUEST, policy, contract.plan_version, True, workflows)
        return SimpleNamespace(
            node=node,
            row=row,
            report=report,
            contract=contract,
            raw=raw,
            grant=grant,
            binding=binding,
            issued=issued,
            execution=execution,
            authorized=authorized,
            values=(*values[:-1], auth),
        )

    async def recover(self, session, node):
        """Only a settled, service-observed failure may transfer to the controller."""
        require(node.kind == "story" and node.state == "failed" and node.attempts > 0, "operations_story_not_failed")
        event = await session.scalar(
            select(OrchestrationDecision)
            .where(OrchestrationDecision.org_id == node.org_id, OrchestrationDecision.node_id == node.id, OrchestrationDecision.to_state == "failed")
            .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
            .limit(1)
        )
        require(
            event is not None
            and event.kind == "result_observed"
            and event.actor_kind == "service"
            and event.actor_id == "system:orchestration-results",
            "operations_failure_not_engine_observed",
        )
        failure = json.loads(event.reason or "{}")
        row = await session.get(OrchestrationExecution, failure.get("failed_execution_id"))
        require(
            row is not None
            and row.org_id == node.org_id
            and row.node_id == node.id
            and row.flow_id == node.flow_id
            and row.cycle == node.attempts
            and row.phase == "concluded"
            and row.status == "concluded"
            and row.pending_action_key is None
            and row.handoff_receipt_ref is None
            and failure.get("attempt") == node.attempts
            and failure.get("run_id") == attempt_run_id(node.id, node.attempts),
            "operations_failure_not_settled",
        )
        identity = ExecutionIdentity(node.org_id, node.id, node.attempts, row.accepted_plan_version, row.claim_id, row.claim_generation)
        loaded = await load_execution(session, identity=identity, released_failure_run_id=failure["run_id"])
        require(loaded is not None and loaded.kind is OutcomeKind.APPLIED, "operations_released_claim_changed")
        context = RunnerContext(identity, loaded.record, datetime.now(UTC))
        state = await self.authorize(session, context, Action.OPERATE, recovering=True)
        claim = await session.get(OrchestrationWorkClaim, row.claim_id)
        require(
            claim is not None
            and claim.state == "released"
            and claim.release_reason in {"failed", "completed"}
            and claim.generation == row.claim_generation
            and claim.active_run_id is None
            and claim.claim_event_id == failure["run_id"]
            and claim.owner_kind == "engine_flow"
            and claim.owner_ref == node.flow_id
            and claim.provider_repository_id == state.binding.provider_repository_id
            and claim.issue_number == int(str(node.issue_ref).lstrip("#")),
            "operations_released_claim_changed",
        )
        actions = list(
            await session.scalars(
                select(OrchestrationAction).where(OrchestrationAction.org_id == node.org_id, OrchestrationAction.execution_id == row.id).limit(101)
            )
        )
        require(
            len(actions) <= 100 and all(a.status in {"succeeded", "failed"} and a.kind != "review_cycle_dispatch" for a in actions),
            "operations_pending_effect_requires_reconciliation",
        )
        repairs = [a for a in actions if a.kind == "operations_repair_pr" and a.status == "succeeded"]
        require(repairs, "operations_retained_repair_missing")
        # The transfer contains a closed set of already-published PR identities.
        # It cannot be used to adopt a later, unrelated repair or another branch.
        for repair in repairs:
            detail = repair.detail or {}
            require(
                type(detail.get("pull_number")) is int
                and detail["pull_number"] > 0
                and isinstance(detail.get("head"), str)
                and len(detail["head"]) == 40
                and all(c in "0123456789abcdef" for c in detail["head"])
                and repair.receipt_ref == f"github/{state.binding.repo}/pull/{detail['pull_number']}",
                "operations_retained_repair_identity_invalid",
            )
        decision_id = str(uuid5(NAMESPACE_URL, f"{KIND}:{row.id}:{row.claim_generation}"))
        require(await session.get(OrchestrationDecision, decision_id) is None, "operations_continuation_already_started")
        from .work_claims import ClaimBinding, ClaimOwner, Disposition, OwnerKind, bind_run, claim_work

        receipt = await claim_work(
            session,
            binding=ClaimBinding(node.org_id, state.binding.provider_repository_id, int(str(node.issue_ref).lstrip("#"))),
            owner=ClaimOwner(OwnerKind.ENGINE_FLOW, node.flow_id),
            event_id=controller_run(decision_id),
        )
        require(
            receipt.disposition is Disposition.ADMITTED and receipt.claim_id == row.claim_id and receipt.generation == row.claim_generation + 1,
            "operations_continuation_claim_changed",
        )
        bound = await bind_run(
            session, org_id=node.org_id, claim_id=receipt.claim_id, generation=receipt.generation, run_id=controller_run(decision_id)
        )
        require(bound.disposition is Disposition.ADMITTED, "operations_continuation_claim_changed")
        data = dict(
            source_run_id=state.report.run_id,
            source_report_digest=report_digest(state.report),
            failure_decision_id=event.id,
            execution_id=row.id,
            attempt=row.cycle,
            accepted_plan_version=row.accepted_plan_version,
            claim_id=row.claim_id,
            prior_claim_generation=row.claim_generation,
            claim_generation=receipt.generation,
            policy_hash=state.authorized.policy.policy_hash,
            repairs=[repair_identity(a) for a in sorted(repairs, key=lambda a: a.id)],
        )
        session.add(
            OrchestrationDecision(
                id=decision_id,
                org_id=node.org_id,
                flow_id=node.flow_id,
                node_id=node.id,
                kind=KIND,
                actor_id=ACTOR,
                actor_kind="service",
                actor_role="engine",
                from_state="failed",
                to_state="running",
                reason=json.dumps(data, sort_keys=True),
            )
        )
        node.state = "running"
        updated = await advance_execution(
            session,
            identity=replace(identity, claim_generation=receipt.generation),
            advance=PhaseAdvance(
                ExecutionPhase.AWAITING_REVIEW,
                ExecutionStatus.RUNNABLE,
                expected_revision=loaded.record.revision,
                next_check_at=datetime.now(UTC),
                progress_note="Continuing retained operations repair; prior worker failure and attempts preserved.",
            ),
            handoff_receipt_ref=controller_run(decision_id),
        )
        require(updated.kind is OutcomeKind.APPLIED, "operations_continuation_assignment_changed")
        await self.authorize(
            session, RunnerContext(replace(identity, claim_generation=receipt.generation), updated.record, datetime.now(UTC)), Action.OPERATE
        )
        return decision_id
