"""One correlated scan effect, recovered by the existing execution runner."""

from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from .deployment_workflow_provider import WorkflowDefinition
from .execution_policy import Action
from .execution_runner import DecisionKind, EffectOutcome, EffectRequest, EffectResult, HandlerDecision, OperationIdentity
from .execution_state import ActionIntent, BlockCode, ExecutionIdentity, OutcomeKind
from .execution_store import load_execution
from .models import OrchestrationAction
from .repository_evaluation import RepositoryEvaluationReceipt, authorize, record
from .repository_evaluation_provider import require
from .repository_producer import PRODUCER_KIND, RepositoryScanProvider, digest, producer_state, workflow_ref
from .review_cycle import CycleBlockedError, block
from .state import ActorKind, NodeState, transition
from .work_claims import Disposition, ReleaseReason, release_work


def run_data(run):
    return {**asdict(run), "context": run.context.model_dump(mode="json")}


class RepositoryProducerController:
    def __init__(self, factory, *, provider=None):
        self.factory = factory
        self.provider = provider or RepositoryScanProvider()

    async def snapshot(self, context, *, authorize_effect=False):
        async with self.factory() as session:
            state = await producer_state(session, context)
            node, plan, spec, accepted, admission, sources, binding = state
            if authorize_effect:
                authority = await authorize(session, node, plan, spec, binding, self.provider.evidence, exclude_current_evaluation=True)
                require(authority == (accepted[0].id, admission["evaluation_policy_hash"]), "producer_authority_changed")
            key = OperationIdentity.from_context(context, PRODUCER_KIND, accepted[0].id, admission["source_snapshot_hash"]).key
            row = await session.scalar(
                select(OrchestrationAction).where(
                    OrchestrationAction.execution_id == context.execution.id,
                    OrchestrationAction.org_id == node.org_id,
                    OrchestrationAction.operation_key == key,
                )
            )
            saved = dict(row.detail) if row is not None else None
        if saved is None:
            prepared = await self.provider.preflight(binding, spec, sources)
            # WorkflowRef holds frozen allowlists; persist ordinary JSON, not Python sets.
            prepared["workflow"]["allowed_inputs"] = {key: sorted(values) for key, values in prepared["workflow"]["allowed_inputs"].items()}
            saved = dict(**admission, **prepared, correlation=digest(key), operation_key=key)
        require(
            saved["acceptance_decision_id"] == accepted[0].id and saved["source_snapshot_hash"] == admission["source_snapshot_hash"],
            "producer_intent_changed",
        )
        return state, saved

    async def observed_run(self, state, data):
        spec, binding = state[2], state[-1]
        return await self.provider.observe(
            binding,
            workflow=workflow_ref(data),
            definition=WorkflowDefinition(**data["definition"]),
            target=spec.producer.target,
            source_revision=data["source_revision"],
            inputs=spec.producer.inputs,
            correlation=data["correlation"],
        )

    async def observe(self, context):
        from .evaluation_controller import EvaluationObservation
        from .execution_runner import ObservationKind

        try:
            state, data = await self.snapshot(context)
            run, incomplete = await self.observed_run(state, data)
            if run is None:
                if incomplete or data.get("dispatch_started"):
                    return EvaluationObservation(
                        ObservationKind.UNCERTAIN, stage="repository_wait", detail="One-off scan dispatch is unresolved; no duplicate will be sent."
                    )
                return EvaluationObservation(ObservationKind.READY, stage="repository_dispatch", snapshot=data)
            if run.status != "completed":
                return EvaluationObservation(ObservationKind.WAITING, stage="repository_wait", detail="The correlated one-off scan is still running.")
            if run.conclusion != "success":
                return EvaluationObservation(
                    ObservationKind.BLOCKED,
                    stage="repository_wait",
                    block=block("repository_scan_failed_cleanup_unverified:" + str(run.run_id), BlockCode.PROVIDER_UNAVAILABLE),
                )
            node, plan, spec, accepted, admission, sources, binding = state
            pulls, revisions = await self.provider.evidence.verify_sources(binding, spec, sources)
            workflow = await self.provider.evidence.workflow(
                binding, spec.workflows[0], revisions=revisions, max_age_seconds=spec.max_age_seconds, bound_run=run, producer=spec.producer
            )
            # Revalidate correlation and context after the artifact read as well.
            fresh, _ = await self.observed_run(state, data)
            require(
                fresh is not None
                and {key: value for key, value in run_data(fresh).items() if key != "observed_at"}
                == {key: value for key, value in run_data(run).items() if key != "observed_at"},
                "producer_run_changed_during_evidence",
            )
            receipt = RepositoryEvaluationReceipt(
                org_id=node.org_id,
                flow_id=node.flow_id,
                node_id=node.id,
                cycle=node.attempts,
                accepted_plan_version=plan.version,
                accepted_plan_hash=plan.plan_hash,
                acceptance_decision_id=accepted[0].id,
                evaluation_policy_hash=admission["evaluation_policy_hash"],
                specification_hash=admission["specification_hash"],
                harness_sha256=spec.runner.harness_sha256,
                source_snapshot_hash=admission["source_snapshot_hash"],
                observed_at=datetime.now(UTC),
                pull_requests=pulls,
                workflows=[workflow],
                mandatory_passed=all(item["passed"] for item in workflow["criteria"]),
            )
            return EvaluationObservation(
                ObservationKind.SUCCEEDED,
                stage="repository_settle",
                receipt_ref=run.url,
                snapshot={"receipt": receipt.model_dump(mode="json"), "run": run_data(run), "operation_key": data["operation_key"]},
            )
        except CycleBlockedError as error:
            return EvaluationObservation(ObservationKind.BLOCKED, stage="repository_wait", block=block(error.reason, error.code))
        except Exception:
            return EvaluationObservation(
                ObservationKind.BLOCKED, stage="repository_wait", block=block("repository_scan_evidence_unverifiable", BlockCode.PROVIDER_UNAVAILABLE)
            )

    async def mark_dispatch_started(self, context, data):
        # K2 has already committed the effect intent. This second write prevents
        # retries after uncertain network delivery from ever repeating the POST.
        # Resolve credentials and meter before taking SQL ledger locks.
        _, authorized = await self.snapshot(context, authorize_effect=True)
        require(authorized == data, "producer_authority_changed")
        async with self.factory() as session:
            loaded = await load_execution(session, identity=context.identity, for_update=True)
            require(
                loaded is not None and loaded.kind is OutcomeKind.APPLIED and loaded.record.pending_action_key == data["operation_key"],
                "producer_pending_effect_changed",
            )
            state = await producer_state(session, context)
            node, _, _, accepted, _, _, _ = state
            require(
                (accepted[0].id, accepted[2].policy_hash) == (data["acceptance_decision_id"], data["evaluation_policy_hash"]),
                "producer_authority_changed",
            )
            require(datetime.now(UTC) < min(accepted[2].expires_at, context.execution.deadline_at), "producer_deadline_expired")
            row = await session.scalar(
                select(OrchestrationAction)
                .where(
                    OrchestrationAction.execution_id == context.execution.id,
                    OrchestrationAction.org_id == node.org_id,
                    OrchestrationAction.operation_key == data["operation_key"],
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            require(
                row is not None and row.status in {"prepared", "dispatched", "unknown"} and not row.detail.get("dispatch_started"),
                "producer_dispatch_already_attempted",
            )
            require({key: row.detail.get(key) for key in data} == data, "producer_intent_changed")
            row.detail = {**row.detail, "dispatch_started": True}
            await session.commit()

    async def perform(self, context, effect):
        started = False
        try:
            require(effect.intent.kind == PRODUCER_KIND and effect.action is Action.EVALUATE, "producer_effect_changed")
            state, data = await self.snapshot(context, authorize_effect=True)
            require(data["operation_key"] == effect.intent.operation_key, "producer_effect_changed")
            run, incomplete = await self.observed_run(state, data)
            if run is not None or incomplete or data.get("dispatch_started"):
                return EffectResult(EffectOutcome.UNCERTAIN, detail="Reconcile the existing scan; no additional dispatch.")
            fresh = await self.provider.preflight(state[-1], state[2], state[-2])
            require(fresh["definition"] == data["definition"] and fresh["source_revision"] == data["source_revision"], "producer_definition_changed")

            async def reauthorize():
                nonlocal started
                await self.mark_dispatch_started(context, data)
                started = True

            await self.provider.dispatch(
                state[-1],
                workflow=workflow_ref(data),
                definition=WorkflowDefinition(**data["definition"]),
                source_revision=data["source_revision"],
                inputs=state[2].producer.inputs,
                correlation=data["correlation"],
                reauthorize=reauthorize,
            )
            return EffectResult(EffectOutcome.UNCERTAIN, detail="One-off dispatch sent; awaiting authenticated run/context evidence.")
        except Exception as error:
            return EffectResult(
                EffectOutcome.UNCERTAIN if started else EffectOutcome.FAILED,
                detail=error.reason if isinstance(error, CycleBlockedError) else "repository_scan_dispatch_unverifiable",
            )

    async def settle(self, session, context, snapshot):
        state = await producer_state(session, context)
        node, plan, spec, accepted, admission, _, _ = state
        # This records an already observed terminal outcome and cleanup proof.
        # Identity, accepted scope, source and claim fences still apply, while a
        # later expiry or exhausted spend allowance cannot prevent truthful
        # settlement. Effect authorization belongs only at the dispatch boundary.
        receipt = RepositoryEvaluationReceipt.model_validate(snapshot["receipt"]) if snapshot.get("receipt") else None
        if receipt is not None:
            require(
                receipt.org_id == node.org_id
                and receipt.flow_id == node.flow_id
                and receipt.node_id == node.id
                and receipt.cycle == node.attempts
                and receipt.accepted_plan_version == plan.version
                and receipt.accepted_plan_hash == plan.plan_hash
                and receipt.acceptance_decision_id == accepted[0].id
                and receipt.evaluation_policy_hash == admission["evaluation_policy_hash"]
                and receipt.specification_hash == admission["specification_hash"]
                and receipt.harness_sha256 == spec.runner.harness_sha256
                and receipt.source_snapshot_hash == admission["source_snapshot_hash"],
                "producer_receipt_changed",
            )
        passed = receipt is not None and receipt.mandatory_passed
        target = NodeState.PASSED if passed else NodeState.FAILED
        require(
            transition(node.state, target, actor_kind=ActorKind.SERVICE, reason="Authenticated one-off scan outcome").allowed,
            "producer_transition_refused",
        )
        node.state = target.value
        await record(
            session, node, dict(action="repository_scan_completed", producer_execution_id=context.execution.id, **snapshot), before="running"
        )
        released = await release_work(
            session,
            org_id=node.org_id,
            claim_id=context.identity.claim_id,
            generation=context.identity.claim_generation,
            reason=ReleaseReason.COMPLETED if passed else ReleaseReason.FAILED,
            terminal_evidence=snapshot["run"]["url"],
        )
        require(released.disposition in {Disposition.ADMITTED, Disposition.DUPLICATE}, "producer_claim_release_refused")
        if passed:
            from .tick import release_satisfied_successors

            await release_satisfied_successors(session, node)

    def decide(self, context, observation):
        if observation.block is not None:
            return HandlerDecision(DecisionKind.BLOCK, block=observation.block)
        if observation.stage == "repository_dispatch":
            data = observation.snapshot
            return HandlerDecision(
                DecisionKind.EFFECT, effect=EffectRequest(ActionIntent(data["operation_key"], PRODUCER_KIND, detail=data), Action.EVALUATE)
            )
        if observation.stage == "repository_settle":

            async def settle(session, current):
                await self.settle(session, current, observation.snapshot)

            return HandlerDecision(
                DecisionKind.CONCLUDE, settlement=settle, progress_note="One-off scan finished; recorded evidence determines acceptance."
            )
        return HandlerDecision(DecisionKind.WAIT, next_check_at=context.now + timedelta(seconds=30), progress_note=observation.detail)


async def producer_effect_policy(session, record, effect):
    """The runner may use only this exact accepted extension for a scan effect."""
    require(effect.intent.kind == PRODUCER_KIND and effect.action is Action.EVALUATE, "producer_effect_changed")
    from .execution_runner import RunnerContext

    identity = ExecutionIdentity(record.org_id, record.node_id, record.cycle, record.accepted_plan_version, record.claim_id, record.claim_generation)
    state = await producer_state(session, RunnerContext(identity, record, datetime.now(UTC)))
    require(
        effect.intent.detail["acceptance_decision_id"] == state[3][0].id
        and effect.intent.detail["evaluation_policy_hash"] == state[3][2].policy_hash,
        "producer_effect_authority_changed",
    )
    return state[3][2]
