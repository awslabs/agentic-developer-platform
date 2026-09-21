"""Current, authorized evidence accepts evaluations; human gates remain human."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select

from .evaluation_contract import models
from .evaluation_evidence import EvaluationEvidenceError, EvaluationExpectation, evidence_summary, specification_hash
from .evaluation_plan import accepted_evaluation, current_deployment, managed_evaluation, predecessor_deployments, require
from .evaluation_provider import EvaluationProvider
from .evaluation_runtime import EvaluationRuntime
from .execution_runner import DecisionKind, HandlerDecision, HandlerObservation, ObservationKind, OperationIdentity, RunnerContext
from .execution_state import (
    ActionIntent,
    BlockCode,
    ExecutionIdentity,
    ExecutionPhase,
    ExecutionStatus,
    Observation,
    ObservedOutcome,
    OutcomeKind,
    PhaseAdvance,
)
from .execution_store import advance_execution, create_execution, load_execution, prepare_action, record_observation
from .models import OrchestrationAction, OrchestrationDecision, OrchestrationEdge, OrchestrationFlow, OrchestrationNode
from .review_cycle import CycleBlockedError, block
from .state import ActorKind, NodeState, transition

CONTEXT_KIND = "evaluation_context"
EVIDENCE_KIND = "evaluation_evidence"
ACTOR = "system:evaluation-controller"


@dataclass(frozen=True)
class EvaluationObservation(HandlerObservation):
    stage: str = "wait"
    snapshot: dict | None = None
    validated: object | None = None


def deployment_keys(deployments):
    return sorted((parent.id, anchor.execution.id, receipt.operation_key) for anchor, parent, receipt in deployments)


def anchor_for(deployments):
    runtime = [item for item in deployments if not item[2].docs_only]
    return max(runtime or deployments, key=lambda item: (item[2].observed_at, item[1].id))


def compatible_targets(deployments, spec):
    require(spec.target is not None, "evaluation_target_missing")
    runtime = [receipt for _, _, receipt in deployments if not receipt.docs_only]
    require(bool(runtime), "evaluation_runtime_deployment_incomplete")
    for receipt in runtime:
        require(
            any(
                all(target.get(key) == value for key, value in spec.target.model_dump().items())
                and target.get("evidence", {}).get("source") == "registered-aws-role:" + spec.environment_connection_id
                for target in receipt.targets
            ),
            "evaluation_predecessor_target_mismatch",
        )
    require(len({receipt.repo for receipt in runtime}) == 1, "evaluation_predecessor_repository_mismatch")


async def move(session, node, target, *, reason):
    decision = transition(node.state, target, actor_kind=ActorKind.SERVICE, reason=reason)
    require(decision.allowed, "evaluation_human_or_terminal_state")
    before = node.state
    node.state = target.value
    row = OrchestrationDecision(
        org_id=node.org_id,
        flow_id=node.flow_id,
        node_id=node.id,
        kind="gate_presented" if target is NodeState.AWAITING_GATE else "result_observed",
        actor_id=ACTOR,
        actor_role="engine",
        actor_kind="service",
        reason=reason,
        from_state=before,
        to_state=target.value,
    )
    session.add(row)
    await session.flush()
    return row.id


class EvaluationServices:
    def __init__(self, factory, *, runtime=None, provider=None, corrections=None):
        self.factory = factory
        self.runtime = runtime or EvaluationRuntime(factory)
        self.provider = provider or EvaluationProvider()
        self.corrections = corrections

    def correction_service(self):
        if self.corrections is None:
            from .evaluation_corrections import EvaluationCorrections

            self.corrections = EvaluationCorrections(self)
        return self.corrections

    async def node(self, session, context):
        loaded = await load_execution(session, identity=context.identity)
        require(loaded is not None and loaded.kind is OutcomeKind.APPLIED, "evaluation_execution_authority_changed")
        node = await session.scalar(
            select(OrchestrationNode)
            .where(
                OrchestrationNode.org_id == context.identity.org_id,
                OrchestrationNode.id == context.identity.node_id,
                OrchestrationNode.flow_id == context.execution.flow_id,
            )
            .execution_options(populate_existing=True)
        )
        flow = await session.get(OrchestrationFlow, context.execution.flow_id, populate_existing=True)
        require(node is not None and flow is not None and flow.org_id == node.org_id and flow.state == "running", "evaluation_flow_not_running")
        require(node.attempts == context.identity.cycle, "evaluation_cycle_changed")
        return node

    async def request(self, session, context):
        rows = list(
            (
                await session.scalars(
                    select(OrchestrationAction)
                    .where(
                        OrchestrationAction.org_id == context.identity.org_id,
                        OrchestrationAction.execution_id == context.execution.id,
                        OrchestrationAction.kind == CONTEXT_KIND,
                        OrchestrationAction.status == "succeeded",
                    )
                    .limit(2)
                )
            ).all()
        )
        require(len(rows) == 1, "evaluation_context_missing_or_ambiguous")
        return rows[0]

    async def state(self, session, context):
        node = await self.node(session, context)
        require(node.kind == "eval", "evaluation_node_kind_changed")
        accepted = await accepted_evaluation(session, node)
        require(accepted is not None, "evaluation_specification_removed")
        plan, spec, address = accepted
        require(plan.version == context.identity.accepted_plan_version, "evaluation_plan_changed")
        request = await self.request(session, context)
        require(request.detail["specification_hash"] == specification_hash(spec), "evaluation_specification_changed")
        deployments = await predecessor_deployments(session, node, plan.version, now=datetime.now(UTC))
        require(deployments is not None, "evaluation_predecessor_not_complete")
        require(deployment_keys(deployments) == [tuple(item) for item in request.detail["deployments"]], "evaluation_deployment_changed")
        if request.detail.get("correction_operation_key"):
            from .evaluation_correction_state import retest_deployment

            corrected = await retest_deployment(session, node, request, plan.version)
            deployments.append(corrected)
            anchor, _, deployment = corrected
        else:
            anchor, _, deployment = anchor_for(deployments)
        require(asdict(anchor.identity) == request.detail["anchor_identity"], "evaluation_anchor_changed")
        return node, plan, spec, address, request, deployments, anchor, deployment

    async def story(self, context):
        async with self.factory() as session:
            node = await self.node(session, context)
            require(node.state == "passed", "evaluation_code_not_complete")
            own = await current_deployment(session, node, context.identity.accepted_plan_version, now=context.now)
            require(own is not None and own[0].execution.id == context.execution.id, "evaluation_final_deployment_missing")
            from .evaluation_correction_state import correction_link

            correction = await correction_link(session, node)
            if correction is not None:
                parent = await session.get(OrchestrationNode, correction.detail["parent_node_id"], populate_existing=True)
                require(parent is not None and parent.org_id == node.org_id, "evaluation_correction_parent_missing")
                if parent.state == "passed" or parent.attempts > correction.detail["evaluation_cycle"] + 1:
                    return EvaluationObservation(ObservationKind.SUCCEEDED, stage="done")
                require(parent.state == "running", "evaluation_correction_parent_not_active")
                return EvaluationObservation(ObservationKind.WAITING, detail="Corrected deployment is retained for the parent's current evaluation.")
            successors = list(
                (
                    await session.scalars(
                        select(OrchestrationNode)
                        .join(
                            OrchestrationEdge,
                            OrchestrationEdge.to_node_id == OrchestrationNode.id,
                        )
                        .where(
                            OrchestrationEdge.org_id == node.org_id,
                            OrchestrationEdge.from_node_id == node.id,
                            OrchestrationNode.org_id == node.org_id,
                            OrchestrationNode.flow_id == node.flow_id,
                            OrchestrationNode.kind == "eval",
                        )
                        .order_by(OrchestrationNode.id)
                        .limit(129)
                    )
                ).all()
            )
            require(len(successors) <= 128, "evaluation_successor_limit")
            pending = False
            for evaluation in successors:
                if not await managed_evaluation(session, evaluation):
                    continue
                if evaluation.state == "passed":
                    continue
                require(evaluation.state not in {"rejected_at_gate", "failed", "halted", "superseded"}, "evaluation_requires_human_recovery")
                accepted = await accepted_evaluation(session, evaluation)
                require(accepted is not None, "evaluation_specification_missing")
                plan, spec, address = accepted
                if spec.evidence_schema == "repository-evaluation/v1":
                    # The native read-only observer consumes merged-code receipts
                    # independently; it must not inherit a deployment claim.
                    continue
                pending = True
                require(plan.version == context.identity.accepted_plan_version, "evaluation_plan_changed")
                deployments = await predecessor_deployments(session, evaluation, plan.version, now=datetime.now(UTC))
                if deployments is None or evaluation.state != "ready":
                    continue
                anchor, _, deployment = anchor_for(deployments)
                if anchor.identity.node_id != node.id:
                    continue
                if spec.acceptance_mode == "machine":
                    compatible_targets(deployments, spec)
                    require(deployment.valid_until > datetime.now(UTC), "evaluation_deployment_start_window_expired")
                    await self.runtime.authority(anchor, address)
                return EvaluationObservation(
                    ObservationKind.READY,
                    stage="admit",
                    snapshot=dict(
                        node_id=evaluation.id,
                        specification_hash=specification_hash(spec),
                        deployments=deployment_keys(deployments),
                        anchor_identity=asdict(anchor.identity),
                    ),
                )
            return EvaluationObservation(
                ObservationKind.WAITING if pending else ObservationKind.SUCCEEDED,
                stage="wait" if pending else "done",
                detail="Waiting for the dependent evaluation; existing human gates remain in force.",
            )

    async def admit(self, session, context, snapshot):
        # K2 holds the current flow/claim/version lock before this settlement.
        node = await session.scalar(
            select(OrchestrationNode)
            .where(
                OrchestrationNode.id == snapshot["node_id"],
                OrchestrationNode.org_id == context.identity.org_id,
                OrchestrationNode.flow_id == context.execution.flow_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        require(node is not None and node.state == "ready", "evaluation_admission_raced")
        accepted = await accepted_evaluation(session, node)
        require(accepted is not None, "evaluation_specification_removed")
        plan, spec, address = accepted
        require(
            plan.version == context.identity.accepted_plan_version and specification_hash(spec) == snapshot["specification_hash"],
            "evaluation_plan_changed",
        )
        deployments = await predecessor_deployments(session, node, plan.version, now=datetime.now(UTC))
        require(deployments is not None and deployment_keys(deployments) == snapshot["deployments"], "evaluation_admission_deployment_changed")
        anchor, _, deployment = anchor_for(deployments)
        require(asdict(anchor.identity) == snapshot["anchor_identity"], "evaluation_anchor_changed")
        if spec.acceptance_mode == "machine":
            compatible_targets(deployments, spec)
            await self.runtime.authority(anchor, address)
        await move(session, node, NodeState.RUNNING, reason="Accepted evaluation entered its evidence phase.")
        node.attempts += 1
        if spec.acceptance_mode == "human":
            await move(session, node, NodeState.AWAITING_GATE, reason="Human evaluation mode requires the existing approval control.")
            return
        identity = ExecutionIdentity(node.org_id, node.id, node.attempts, plan.version, anchor.identity.claim_id, anchor.identity.claim_generation)
        created = await create_execution(
            session,
            identity=identity,
            flow_id=node.flow_id,
            phase=ExecutionPhase.EVALUATION_PENDING,
            next_check_at=datetime.now(UTC),
            deadline_at=anchor.execution.deadline_at,
        )
        require(created.kind is OutcomeKind.APPLIED, "evaluation_admission_conflict")
        current = RunnerContext(identity, created.record, datetime.now(UTC))
        key = OperationIdentity.from_context(current, CONTEXT_KIND, spec.runner.harness_revision, snapshot["specification_hash"]).key
        prepared = await prepare_action(session, identity=identity, intent=ActionIntent(operation_key=key, kind=CONTEXT_KIND, detail=snapshot))
        require(prepared.kind is OutcomeKind.APPLIED, "evaluation_context_conflict")
        recorded = await record_observation(
            session,
            identity=identity,
            observation=Observation(
                key,
                ObservedOutcome.SUCCEEDED,
                receipt_ref="evaluation/context/" + created.record.id,
                detail="Bound to the accepted suite and predecessor deployments; no verdict supplied.",
            ),
        )
        require(recorded.kind is OutcomeKind.APPLIED, "evaluation_context_conflict")

    async def evaluate(self, context):
        async with self.factory() as session:
            node, plan, spec, address, request, deployments, anchor, deployment = await self.state(session, context)
            if node.state == "passed":
                return EvaluationObservation(ObservationKind.SUCCEEDED, stage="done")
            require(node.state == "running", "evaluation_human_or_terminal_state")
            failed = await session.scalar(
                select(OrchestrationAction)
                .where(
                    OrchestrationAction.org_id == node.org_id,
                    OrchestrationAction.execution_id == context.execution.id,
                    OrchestrationAction.kind == EVIDENCE_KIND,
                    OrchestrationAction.status == "succeeded",
                )
                .order_by(OrchestrationAction.created_at.desc())
                .limit(1)
            )
            if failed is not None and (failed.detail or {}).get("required_failures"):
                return await self.correction_service().observe(context)
            if spec.acceptance_mode != "machine":
                return EvaluationObservation(ObservationKind.READY, stage="human")
            expected, binding = await self.expectation(session, context)
            policy_hash = expected.policy_hash
        validated = await self.provider.find(binding, expected)
        if validated is None:
            return EvaluationObservation(
                ObservationKind.WAITING, detail="Awaiting a current authenticated evaluation receipt from the accepted harness."
            )
        return EvaluationObservation(
            ObservationKind.SUCCEEDED,
            stage="accept",
            validated=validated,
            snapshot=dict(
                specification_hash=specification_hash(spec),
                policy_hash=policy_hash,
                deployments=deployment_keys(deployments),
                anchor_identity=asdict(anchor.identity),
            ),
        )

    async def expectation(self, session, context):
        node, plan, spec, address, request, deployments, anchor, deployment = await self.state(session, context)
        require(node.state == "running" and spec.acceptance_mode == "machine", "evaluation_human_or_terminal_state")
        compatible_targets(deployments, spec)
        _, source_binding, _, _, policy, _, _ = await self.runtime.authority(anchor, address)
        require(policy.policy_hash == plan.plan_document["execution_policy"]["policy_hash"], "evaluation_policy_changed")
        for _, _, predecessor in deployments:
            if not predecessor.docs_only and predecessor.source_revision != deployment.actual_revision:
                await self.runtime.deployments.provider.contains(source_binding, predecessor.source_revision, deployment.actual_revision)
        require(
            spec.runner.repository in policy.repository_ids and source_binding.installation_id is not None,
            "evaluation_harness_repository_unavailable",
        )
        expected = EvaluationExpectation(
            context.identity, context.execution.id, node.flow_id, policy.policy_hash, spec.model_dump(mode="json"), deployment
        )
        binding = SimpleNamespace(
            org_id=node.org_id,
            repo=spec.runner.repository,
            provider_repository_id=spec.runner.repository_id,
            installation_id=source_binding.installation_id,
        )
        return expected, binding

    async def settle(self, session, context, observation):
        node, plan, spec, address, request, deployments, anchor, deployment = await self.state(session, context)
        require(node.state == "running" and spec.acceptance_mode == "machine", "evaluation_human_or_terminal_state")
        require(
            specification_hash(spec) == observation.snapshot["specification_hash"]
            and deployment_keys(deployments) == observation.snapshot["deployments"],
            "evaluation_acceptance_scope_changed",
        )
        _, _, _, _, policy, _, _ = await self.runtime.authority(anchor, address)
        require(policy.policy_hash == observation.snapshot["policy_hash"], "evaluation_policy_changed")
        receipt = observation.validated.receipt
        require(
            receipt.actual_revision == deployment.actual_revision
            and receipt.deployment_operation_key == deployment.operation_key
            and receipt.specification_hash == specification_hash(spec)
            and all(getattr(receipt, key) == value for key, value in asdict(context.identity).items()),
            "evaluation_acceptance_scope_changed",
        )
        now = datetime.now(UTC)
        require(receipt.expires_at > now and now - receipt.completed_at <= timedelta(seconds=spec.max_age_seconds), "evaluation_evidence_expired")
        # This bounded read is deliberately inside the acceptance transaction:
        # a current plan/claim is locked, and no earlier cached runtime read can
        # become the acceptance proof after the final settlement checkpoint.
        observed = await asyncio.wait_for(self.runtime.verify(anchor, deployment, address), timeout=25)
        require(-timedelta(seconds=30) <= datetime.now(UTC) - observed <= timedelta(seconds=30), "evaluation_runtime_observation_stale")
        # Authority can expire/revoke during target I/O. Refresh it at the commit
        # boundary instead of retaining the pre-I/O grant or wall clock.
        _, _, _, _, current_policy, _, _ = await self.runtime.authority(anchor, address)
        require(current_policy.policy_hash == policy.policy_hash, "evaluation_policy_changed")
        now = datetime.now(UTC)
        require(receipt.expires_at > now and now - receipt.completed_at <= timedelta(seconds=spec.max_age_seconds), "evaluation_evidence_expired")
        key = OperationIdentity.from_context(context, EVIDENCE_KIND, receipt.harness_revision, observation.validated.artifact_hash).key
        summary = evidence_summary(observation.validated)
        prepared = await prepare_action(
            session,
            identity=context.identity,
            intent=ActionIntent(
                operation_key=key,
                kind=EVIDENCE_KIND,
                artifact_ref=observation.validated.artifact_ref,
                detail=dict(
                    evaluation_receipt=receipt.model_dump(mode="json"),
                    specification=spec.model_dump(mode="json"),
                    evidence_summary=summary,
                    artifact_hash=observation.validated.artifact_hash,
                    required_failures=list(observation.validated.required_failures),
                ),
            ),
        )
        require(prepared.kind is OutcomeKind.APPLIED, "evaluation_evidence_conflict")
        if observation.validated.mandatory_passed:
            decision_id = await move(
                session,
                node,
                NodeState.PASSED,
                reason=json.dumps(
                    dict(
                        action="evaluation_accepted",
                        execution_id=context.execution.id,
                        cycle=context.identity.cycle,
                        accepted_plan_version=context.identity.accepted_plan_version,
                        operation_key=key,
                        evidence=summary,
                    ),
                    sort_keys=True,
                ),
            )
            from .tick import release_satisfied_successors

            await release_satisfied_successors(session, node)
        else:
            # E3 consumes the immutable failed evidence. The node stays active;
            # neither a predecessor resurrection nor a correction is inferred.
            decision = OrchestrationDecision(
                org_id=node.org_id,
                flow_id=node.flow_id,
                node_id=node.id,
                kind="result_observed",
                actor_id=ACTOR,
                actor_role="engine",
                actor_kind="service",
                reason=json.dumps(dict(action="evaluation_failed", operation_key=key, evidence=summary), sort_keys=True),
                from_state=node.state,
                to_state=node.state,
            )
            session.add(decision)
            await session.flush()
            decision_id = decision.id
        row = await session.scalar(
            select(OrchestrationAction).where(
                OrchestrationAction.org_id == node.org_id,
                OrchestrationAction.execution_id == context.execution.id,
                OrchestrationAction.operation_key == key,
            )
        )
        row.detail = {**row.detail, "decision_id": decision_id}
        recorded = await record_observation(
            session,
            identity=context.identity,
            observation=Observation(
                key,
                ObservedOutcome.SUCCEEDED,
                receipt_ref="evaluation/decision/" + decision_id,
                detail="Evaluation evidence recorded; required failures remain blocked."
                if not observation.validated.mandatory_passed
                else "Authorized evaluation accepted.",
            ),
        )
        require(recorded.kind is OutcomeKind.APPLIED, "evaluation_recording_conflict")


class EvaluationController:
    def __init__(self, factory, services=None):
        self.services = services or EvaluationServices(factory)
        from .repository_producer_controller import RepositoryProducerController

        self.repository_producer = RepositoryProducerController(factory)

    async def observe(self, context):
        try:
            async with self.services.factory() as session:
                node = await self.services.node(session, context)
                accepted = await accepted_evaluation(session, node) if node.kind == "eval" else None
            if accepted is not None and getattr(accepted[1], "producer", None) is not None:
                return await self.repository_producer.observe(context)
            return await (self.services.story(context) if node.kind == "story" else self.services.evaluate(context))
        except EvaluationEvidenceError as error:
            return EvaluationObservation(ObservationKind.BLOCKED, block=block(error.reason.value, BlockCode.HUMAN_INPUT_REQUIRED))
        except CycleBlockedError as error:
            return EvaluationObservation(ObservationKind.BLOCKED, block=block(error.reason, error.code))
        except Exception:
            return EvaluationObservation(ObservationKind.BLOCKED, block=block("evaluation_evidence_unverifiable", BlockCode.PROVIDER_UNAVAILABLE))

    async def perform(self, context, effect):
        from .evaluation_correction_state import CORRECTION_KIND
        from .repository_producer import PRODUCER_KIND

        if effect.intent.kind == PRODUCER_KIND:
            return await self.repository_producer.perform(context, effect)
        require(effect.intent.kind == CORRECTION_KIND, "evaluation_effect_unsupported")
        return await self.services.correction_service().perform(context, effect)

    def decide(self, context, observation):
        stage = getattr(observation, "stage", "wait")
        if stage.startswith("repository_"):
            return self.repository_producer.decide(context, observation)
        if stage.startswith("correction_"):
            return self.services.correction_service().decide(context, observation)
        if observation.kind is ObservationKind.BLOCKED:
            return HandlerDecision(DecisionKind.BLOCK, block=observation.block)
        if stage == "done":
            return HandlerDecision(
                DecisionKind.CONCLUDE, progress_note="Delivery/evaluation handoff complete; explicit human gates remain authoritative."
            )
        if stage == "admit":

            async def admit(session, current):
                await self.services.admit(session, current, observation.snapshot)

            return HandlerDecision(DecisionKind.WAIT, settlement=admit, next_check_at=context.now + timedelta(seconds=30))
        if stage == "accept":

            async def settle(session, current):
                try:
                    async with session.begin_nested():
                        await self.services.settle(session, current, observation)
                except (CycleBlockedError, ValueError, TimeoutError) as error:
                    reason = getattr(
                        error, "reason", "evaluation_runtime_timeout" if isinstance(error, TimeoutError) else "evaluation_runtime_unverifiable"
                    )
                    code = error.code if isinstance(error, CycleBlockedError) else BlockCode.HUMAN_INPUT_REQUIRED
                    await advance_execution(
                        session,
                        identity=current.identity,
                        advance=PhaseAdvance(
                            phase=ExecutionPhase.EVALUATION_PENDING,
                            status=ExecutionStatus.BLOCKED,
                            expected_revision=current.execution.revision,
                            next_check_at=datetime.now(UTC) + timedelta(seconds=30),
                        ),
                        block=block(str(reason), code),
                    )

            return HandlerDecision(
                DecisionKind.ADVANCE,
                phase=ExecutionPhase.EVALUATION_PENDING,
                settlement=settle,
                next_check_at=context.now + timedelta(seconds=30),
                progress_note="Evaluation evidence recorded.",
            )
        return HandlerDecision(DecisionKind.WAIT, next_check_at=context.now + timedelta(seconds=30), progress_note=observation.detail)


def handlers(factory):
    return {ExecutionPhase.EVALUATION_PENDING: EvaluationController(factory)}


def bounded_evaluation_summary(detail):
    """Project a recorded decision only; missing failure metadata is not a pass."""
    try:
        from .evaluation_contract import specification

        receipt = models().EvaluationReceipt.model_validate(detail["evaluation_receipt"])
        spec = specification(detail["specification"])
        failures = detail["required_failures"]
        if not detail.get("decision_id") or receipt.specification_hash != specification_hash(spec):
            return None
        outcomes = {item.criterion_id: item.outcome for item in receipt.criteria}
        required = [criterion.criterion_id for criterion in spec.criteria if criterion.required]
        if not required or any(outcomes.get(key) not in {"pass", "fail"} for key in required):
            return None
        if not isinstance(failures, list) or sorted(failures) != sorted(key for key in required if outcomes[key] == "fail"):
            return None
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    return dict(
        actual_revision=receipt.actual_revision,
        harness_revision=receipt.harness_revision,
        completed_at=receipt.completed_at.isoformat(),
        expires_at=receipt.expires_at.isoformat(),
        mandatory_passed=not failures,
        criteria=[dict(criterion_id=c.criterion_id, outcome=c.outcome) for c in receipt.criteria],
    )
