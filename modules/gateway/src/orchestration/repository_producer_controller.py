"""One correlated scan effect, recovered by the existing execution runner."""

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select

from .deployment_workflow_provider import WorkflowDefinition
from .evaluation_contract import multi_repository_models, verify_multi_repository_receipt
from .execution_policy import Action
from .execution_runner import DecisionKind, EffectOutcome, EffectRequest, EffectResult, HandlerDecision, OperationIdentity
from .execution_state import ActionIntent, BlockCode, ExecutionIdentity, ExecutionPhase, OutcomeKind
from .execution_store import load_execution
from .models import OrchestrationAction
from .repository_evaluation import _ProviderBudget, authorize, multi_repository_sources, multi_source_receipts, receipt_model, record, scoped_binding
from .repository_evaluation_contract import harness_digest
from .repository_evaluation_provider import require
from .repository_producer import (
    CLI_PRODUCER_KIND,
    MULTI_PROBE_KIND,
    PRODUCER_KIND,
    RepositoryScanProvider,
    digest,
    probe_manifest,
    producer_kind,
    producer_state,
    workflow_ref,
)
from .repository_producer_contract import DeployedProbeResult
from .review_cycle import CycleBlockedError, block
from .state import ActorKind, NodeState, transition
from .work_claims import Disposition, ReleaseReason, release_work


def run_data(run):
    document = asdict(run)
    if document.get("started_at") is None:
        document.pop("started_at", None)
    return {**document, "context": run.context.model_dump(mode="json")}


def probe_target(spec):
    return SimpleNamespace(
        account_id=spec.probe.account_id,
        region=spec.probe.region,
        resource_kind="integration_probe",
        resource_id=spec.environment.target_contract_id,
    )


class RepositoryProducerController:
    def __init__(self, factory, *, provider=None):
        self.factory = factory
        self.provider = provider or RepositoryScanProvider()

    async def snapshot(self, context, *, authorize_effect=False):
        async with self.factory() as session:
            state = await producer_state(session, context, allow_multi_source_refresh=True)
            node, plan, spec, accepted, admission, sources, binding = state
            if spec.evidence_schema == "multi-repository-evaluation/v1":
                if spec.mode == "repository":
                    require(not authorize_effect, "producer_effect_changed")
                    key = OperationIdentity.from_context(context, "multi_repository_context", accepted[0].id).key
                    return state, {**admission, "operation_key": key}
                require(spec.probe is not None, "probe_specification_missing")
                if authorize_effect:
                    current, bindings, _ = await multi_repository_sources(session, node, plan, spec)
                    require(current == sources, "producer_sources_changed")
                    authority = await authorize(
                        session,
                        node,
                        plan,
                        accepted[1],
                        binding,
                        self.provider.evidence,
                        exclude_current_evaluation=True,
                        source_repositories=tuple(item.full_name for item in bindings.values()),
                    )
                    require(authority == (accepted[0].id, admission["evaluation_policy_hash"]), "producer_authority_changed")
                key = OperationIdentity.from_context(context, MULTI_PROBE_KIND, accepted[0].id, admission["source_snapshot_hash"]).key
                row = await session.scalar(
                    select(OrchestrationAction).where(
                        OrchestrationAction.execution_id == context.execution.id,
                        OrchestrationAction.org_id == node.org_id,
                        OrchestrationAction.operation_key == key,
                    )
                )
                saved = dict(row.detail) if row is not None else None
                if saved is None:
                    prepared = await self.provider.probe_preflight(binding, spec, admission["source_snapshot_hash"], sources)
                    prepared["workflow"]["allowed_inputs"] = {name: sorted(values) for name, values in prepared["workflow"]["allowed_inputs"].items()}
                    saved = dict(**admission, **prepared, correlation=digest(key), operation_key=key, producer_kind=MULTI_PROBE_KIND)
                require(
                    saved["acceptance_decision_id"] == accepted[0].id
                    and saved["source_snapshot_hash"] == admission["source_snapshot_hash"]
                    and saved["specification_hash"] == admission["specification_hash"],
                    "producer_intent_changed",
                )
                return state, saved
            if authorize_effect:
                require(spec.runner.harness_sha256 == harness_digest(), "producer_effect_harness_changed")
                authority = await authorize(session, node, plan, spec, binding, self.provider.evidence, exclude_current_evaluation=True)
                require(authority == (accepted[0].id, admission["evaluation_policy_hash"]), "producer_authority_changed")
            key = OperationIdentity.from_context(context, producer_kind(spec), accepted[0].id, admission["source_snapshot_hash"]).key
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
            saved = dict(**admission, **prepared, correlation=digest(key), operation_key=key, producer_kind=producer_kind(spec))
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
            target=probe_target(spec) if spec.evidence_schema == "multi-repository-evaluation/v1" else spec.producer.target,
            source_revision=data["source_revision"],
            inputs=data["inputs"] if spec.evidence_schema == "multi-repository-evaluation/v1" else spec.producer.inputs,
            correlation=data["correlation"],
        )

    async def observe(self, context):
        from .evaluation_controller import EvaluationObservation
        from .execution_runner import ObservationKind

        try:
            state, data = await self.snapshot(context)
            if state[2].evidence_schema == "multi-repository-evaluation/v1" and state[2].mode == "deployed":
                return await self.observe_deployed(context, state, data)
            if state[2].evidence_schema == "multi-repository-evaluation/v1":
                return await self.observe_multi(context, state, data)
            run, incomplete = await self.observed_run(state, data)
            if run is None:
                if incomplete or data.get("dispatch_started"):
                    return EvaluationObservation(
                        ObservationKind.UNCERTAIN,
                        stage="repository_wait",
                        detail="Evaluation workflow dispatch is unresolved; no duplicate will be sent.",
                    )
                return EvaluationObservation(ObservationKind.READY, stage="repository_dispatch", snapshot=data)
            if run.status != "completed":
                return EvaluationObservation(
                    ObservationKind.WAITING, stage="repository_wait", detail="The correlated evaluation workflow is still running."
                )
            if run.conclusion != "success":
                return EvaluationObservation(
                    ObservationKind.BLOCKED,
                    stage="repository_wait",
                    block=block("repository_scan_failed_cleanup_unverified:" + str(run.run_id), BlockCode.PROVIDER_UNAVAILABLE),
                )
            node, plan, spec, accepted, admission, sources, binding = state
            pulls, revisions = await self.provider.evidence.verify_sources(binding, spec, sources)
            workflow = await self.provider.evidence.workflow(
                binding,
                spec.workflows[0],
                revisions=revisions,
                max_age_seconds=spec.max_age_seconds,
                bound_run=run,
                producer=spec.producer,
                qualification=getattr(spec, "qualification", None),
            )
            # Revalidate correlation and context after the artifact read as well.
            fresh, _ = await self.observed_run(state, data)
            require(
                fresh is not None
                and {key: value for key, value in run_data(fresh).items() if key != "observed_at"}
                == {key: value for key, value in run_data(run).items() if key != "observed_at"},
                "producer_run_changed_during_evidence",
            )
            receipt = receipt_model(spec)(
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
                collector_harness_sha256=harness_digest(),
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

    async def observe_deployed(self, context, state, data):
        from .evaluation_controller import EvaluationObservation
        from .execution_runner import ObservationKind

        node, plan, spec, accepted, admission, sources, binding = state
        run, incomplete = await self.observed_run(state, data)
        if run is None:
            if incomplete or data.get("dispatch_started"):
                return EvaluationObservation(
                    ObservationKind.UNCERTAIN, stage="repository_wait", detail="Awaiting the existing integration probe run."
                )
            return EvaluationObservation(ObservationKind.READY, stage="repository_dispatch", snapshot=data)
        if run.status != "completed":
            return EvaluationObservation(ObservationKind.WAITING, stage="repository_wait", detail="The integration probe is running.")
        require(run.conclusion in {"success", "failure"}, "probe_run_not_concluded")
        async with self.factory() as session:
            current, bindings, harness = await multi_repository_sources(session, node, plan, spec)
            require(current == sources, "producer_sources_changed")
            authority = await authorize(
                session,
                node,
                plan,
                accepted[1],
                binding,
                self.provider.evidence,
                exclude_current_evaluation=True,
                source_repositories=tuple(item.full_name for item in bindings.values()),
            )
            require(authority == (accepted[0].id, admission["evaluation_policy_hash"]), "producer_authority_changed")
            observations = []
            async with _ProviderBudget():
                for source, declared in zip(sources, spec.predecessors):
                    receipts, checks = await self.provider.evidence.verified_multi_source(
                        scoped_binding(node.org_id, bindings[declared.address]), source, declared
                    )
                    require(
                        len(checks) == len(receipts) and all(check["repository_id"] == declared.repository_id for check in checks),
                        "producer_source_check_identity_changed",
                    )
                    observations.append({"receipts": receipts, "checks": checks})
                document = await self.provider.evidence.multi_probe_receipt(scoped_binding(node.org_id, harness), run, spec.probe)
            resolved = await multi_source_receipts(session, node, plan, spec, sources, observations)
        fresh, incomplete = await self.observed_run(state, data)
        require(
            not incomplete
            and fresh is not None
            and {key: value for key, value in run_data(fresh).items() if key != "observed_at"}
            == {key: value for key, value in run_data(run).items() if key != "observed_at"},
            "probe_run_changed_during_evidence",
        )
        contract = multi_repository_models()
        try:
            result = DeployedProbeResult.model_validate(document)
            manifest = probe_manifest(sources, spec.environment)
            now = datetime.now(UTC)
            started_at = datetime.fromisoformat(run.started_at.replace("Z", "+00:00")) if run.started_at else None
            require(started_at is not None and started_at.tzinfo is not None and started_at <= now, "probe_run_start_unverifiable")
            required = {criterion.criterion_id: criterion.required for criterion in spec.criteria}
            require(
                result.correlation == data["correlation"] == run.context.correlation
                and result.source_snapshot_hash == admission["source_snapshot_hash"]
                and result.source_manifest_digest == hashlib.sha256(manifest.encode()).hexdigest()
                and data["inputs"]["source_manifest"] == manifest
                and result.specification_digest == contract.specification_digest(spec)
                and result.target_contract_hash == spec.environment.target_contract_hash
                and (result.run_id, result.run_attempt) == (run.run_id, run.run_attempt)
                and {item.criterion_id for item in result.criteria} == set(required)
                and len(result.runtime_readbacks) == len(sources)
                and [
                    {
                        "address": item.address,
                        "repository_id": item.repository_id,
                        "revision": item.revision,
                        "components": sorted(item.components),
                        "deployment_receipt_key": item.deployment_receipt_key,
                        "targets": item.targets,
                        "runtime_artifacts": sorted(
                            (artifact.model_dump(mode="json") for artifact in item.runtime_artifacts),
                            key=lambda artifact: artifact["component"],
                        ),
                    }
                    for item in sorted(result.runtime_readbacks, key=lambda item: item.address)
                    if started_at <= item.observed_at <= now
                ]
                == json.loads(manifest)["sources"]
                and max(started_at, now - timedelta(seconds=spec.max_age_seconds)) <= result.observed_at <= now
                and (run.conclusion == "success") == all(item.outcome == "pass" for item in result.criteria if required[item.criterion_id]),
                "probe_producer_or_run_mismatch",
            )
            receipt = contract.EvaluationReceipt(
                mode="deployed",
                org_id=node.org_id,
                flow_id=node.flow_id,
                node_id=node.id,
                execution_id=context.execution.id,
                attempt=node.attempts,
                claim_generation=context.identity.claim_generation,
                specification_digest=contract.specification_digest(spec),
                input_set_digest=contract.input_set_digest(spec, resolved),
                harness=spec.harness,
                environment=spec.environment,
                sources=resolved,
                criteria=[contract.CriterionOutcome.model_validate(item.model_dump()) for item in result.criteria],
                producer=contract.Producer(
                    adapter=spec.harness.adapter,
                    provider_identity="github:" + str(binding.provider_repository_id),
                    run_id=str(run.run_id),
                    run_attempt=run.run_attempt,
                ),
                observed_at=result.observed_at,
                expires_at=result.observed_at + timedelta(seconds=spec.max_age_seconds),
                verdict="pass" if run.conclusion == "success" else "fail",
            )
            verify_multi_repository_receipt(spec.model_dump(mode="json"), receipt.model_dump(mode="json"), resolved, now=now)
        except (ValueError, KeyError) as error:
            raise CycleBlockedError("probe_receipt_unverifiable") from error
        return EvaluationObservation(
            ObservationKind.SUCCEEDED,
            stage="repository_settle",
            receipt_ref=run.url,
            snapshot={
                "receipt": receipt.model_dump(mode="json"),
                "source_observations": observations,
                "operation_key": OperationIdentity.from_context(context, "multi_repository_context", accepted[0].id).key,
                "probe_operation_key": data["operation_key"],
                "run": run_data(run),
            },
        )

    async def observe_multi(self, context, state, data):
        from .evaluation_controller import EvaluationObservation
        from .execution_runner import ObservationKind

        node, plan, spec, accepted, admission, sources, harness_binding = state
        async with self.factory() as session:
            current, bindings, harness = await multi_repository_sources(session, node, plan, spec)
            require(current == sources, "producer_sources_changed")
            source_repositories = tuple(binding.full_name for binding in bindings.values())
            authority = await authorize(
                session,
                node,
                plan,
                accepted[1],
                harness_binding,
                self.provider.evidence,
                exclude_current_evaluation=True,
                source_repositories=source_repositories,
            )
            require(authority == (accepted[0].id, admission["evaluation_policy_hash"]), "producer_authority_changed")
            observations = []
            reading = None
            try:
                async with _ProviderBudget():
                    for revision in {spec.harness.source_revision, spec.harness.definition_revision} - {None}:
                        await self.provider.evidence.deployed_revision(
                            scoped_binding(node.org_id, harness),
                            {"head_sha": revision, "merge_sha": revision, "required_checks": []},
                        )
                    for source, declared in zip(sources, spec.predecessors):
                        reading = declared
                        receipts, checks = await self.provider.evidence.verified_multi_source(
                            scoped_binding(node.org_id, bindings[declared.address]),
                            source,
                            declared,
                        )
                        require(
                            len(checks) == len(receipts)
                            and all(
                                check["repository_id"] == declared.repository_id and check["pr_node_id"] == source.get("provider_pr_node_id")
                                for check in checks
                            ),
                            "producer_source_check_identity_changed",
                        )
                        observations.append({"receipts": receipts, "checks": checks})
                        reading = None
            except CycleBlockedError as error:
                if reading is None:
                    raise
                raise CycleBlockedError(f"{error.reason}:{reading.address}:{reading.repository_id}", error.code) from None
            except Exception:
                if reading is None:
                    raise
                raise CycleBlockedError(
                    f"repository_evaluation_source_unverifiable:{reading.address}:{reading.repository_id}", BlockCode.PROVIDER_UNAVAILABLE
                ) from None
            resolved = await multi_source_receipts(session, node, plan, spec, sources, observations)
        contract = multi_repository_models()
        now = datetime.now(UTC)
        expires = min(now + timedelta(seconds=spec.max_age_seconds), context.execution.deadline_at)
        require(now < expires, "producer_receipt_expired")
        input_digest = contract.input_set_digest(spec, resolved)
        receipt = contract.EvaluationReceipt(
            org_id=node.org_id,
            flow_id=node.flow_id,
            node_id=node.id,
            execution_id=context.execution.id,
            attempt=node.attempts,
            claim_generation=context.identity.claim_generation,
            mode="repository",
            specification_digest=contract.specification_digest(spec),
            input_set_digest=input_digest,
            harness=spec.harness,
            sources=resolved,
            criteria=[dict(criterion_id=criterion.criterion_id, outcome="pass", evidence_references=[input_digest]) for criterion in spec.criteria],
            producer=dict(
                adapter=spec.harness.adapter, provider_identity=spec.harness.repository, run_id=context.execution.id, run_attempt=node.attempts
            ),
            observed_at=now,
            expires_at=expires,
            verdict="pass",
        )
        verify_multi_repository_receipt(spec.model_dump(mode="json"), receipt.model_dump(mode="json"), resolved, now=now)
        return EvaluationObservation(
            ObservationKind.SUCCEEDED,
            stage="repository_settle",
            receipt_ref="evaluation/receipt/" + context.execution.id,
            snapshot={"receipt": receipt.model_dump(mode="json"), "source_observations": observations, "operation_key": data["operation_key"]},
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
            require(
                effect.intent.kind in {PRODUCER_KIND, CLI_PRODUCER_KIND, MULTI_PROBE_KIND} and effect.action is Action.EVALUATE,
                "producer_effect_changed",
            )
            state, data = await self.snapshot(context, authorize_effect=True)
            require(data["operation_key"] == effect.intent.operation_key and effect.intent.kind == producer_kind(state[2]), "producer_effect_changed")
            run, incomplete = await self.observed_run(state, data)
            if run is not None or incomplete or data.get("dispatch_started"):
                return EffectResult(EffectOutcome.UNCERTAIN, detail="Reconcile the existing evaluation workflow; no additional dispatch.")
            fresh = (
                await self.provider.probe_preflight(state[-1], state[2], data["source_snapshot_hash"], state[5])
                if effect.intent.kind == MULTI_PROBE_KIND
                else await self.provider.preflight(state[-1], state[2], state[-2])
            )
            require(
                fresh["definition"] == data["definition"]
                and fresh["source_revision"] == data["source_revision"]
                and (effect.intent.kind != MULTI_PROBE_KIND or fresh["inputs"] == data.get("inputs")),
                "producer_definition_changed",
            )

            async def reauthorize():
                nonlocal started
                await self.mark_dispatch_started(context, data)
                started = True

            await self.provider.dispatch(
                state[-1],
                workflow=workflow_ref(data),
                definition=WorkflowDefinition(**data["definition"]),
                source_revision=data["source_revision"],
                inputs=data["inputs"] if effect.intent.kind == MULTI_PROBE_KIND else state[2].producer.inputs,
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
        state = await producer_state(session, context, allow_multi_source_refresh=True)
        node, plan, spec, accepted, admission, _, _ = state
        # This records an already observed terminal outcome and cleanup proof.
        # Identity, accepted scope, source and claim fences still apply, while a
        # later expiry or exhausted spend allowance cannot prevent truthful
        # settlement. Effect authorization belongs only at the dispatch boundary.
        if spec.evidence_schema == "multi-repository-evaluation/v1":
            await self.settle_multi(session, context, state, snapshot)
            return
        receipt = receipt_model(spec).model_validate(snapshot["receipt"]) if snapshot.get("receipt") else None
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
                and receipt.collector_harness_sha256 == harness_digest()
                and receipt.source_snapshot_hash == admission["source_snapshot_hash"],
                "producer_receipt_changed",
            )
        passed = receipt is not None and receipt.mandatory_passed
        target = (NodeState.AWAITING_GATE if spec.acceptance_mode == "human" else NodeState.PASSED) if passed else NodeState.FAILED
        require(
            transition(node.state, target, actor_kind=ActorKind.SERVICE, reason="Authenticated evaluation workflow outcome").allowed,
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
        if passed and spec.acceptance_mode == "machine":
            from .tick import release_satisfied_successors

            await release_satisfied_successors(session, node)

    async def settle_multi(self, session, context, state, snapshot):
        node, plan, spec, accepted, admission, sources, _ = state
        contract = multi_repository_models()
        receipt = contract.EvaluationReceipt.model_validate(snapshot["receipt"])
        require(
            receipt.org_id == node.org_id
            and receipt.flow_id == node.flow_id
            and receipt.node_id == node.id
            and receipt.execution_id == context.execution.id
            and receipt.attempt == node.attempts
            and receipt.claim_generation == context.identity.claim_generation
            and snapshot["operation_key"] == OperationIdentity.from_context(context, "multi_repository_context", accepted[0].id).key
            and admission["specification_hash"] == digest(accepted[1].model_dump(mode="json"))
            and admission["source_snapshot_hash"] == digest(sources)
            and admission["evaluation_policy_hash"] == accepted[2].policy_hash,
            "producer_receipt_changed",
        )
        fresh_sources, _, _ = await multi_repository_sources(session, node, plan, spec, lock=True)
        require(fresh_sources == sources, "producer_sources_changed")
        observed = (
            snapshot["source_observations"]
            if spec.mode == "deployed"
            else [{"receipts": [check.model_dump(mode="json") for check in source.checks]} for source in receipt.sources]
        )
        if spec.mode == "deployed":
            require(
                snapshot.get("probe_operation_key")
                == OperationIdentity.from_context(context, MULTI_PROBE_KIND, accepted[0].id, admission["source_snapshot_hash"]).key
                and snapshot["run"]["run_id"] == int(receipt.producer.run_id)
                and snapshot["run"]["run_attempt"] == receipt.producer.run_attempt,
                "probe_operation_or_run_changed",
            )
        resolved = await multi_source_receipts(session, node, plan, spec, fresh_sources, observed)
        try:
            verify_multi_repository_receipt(spec.model_dump(mode="json"), snapshot["receipt"], resolved, now=datetime.now(UTC))
        except ValueError as error:
            raise CycleBlockedError(str(error)) from None
        require(receipt.verdict in {"pass", "fail"}, "producer_verdict_not_concluded")
        context_action = await session.scalar(
            select(OrchestrationAction)
            .where(
                OrchestrationAction.org_id == node.org_id,
                OrchestrationAction.execution_id == context.execution.id,
                OrchestrationAction.kind == "multi_repository_context",
                OrchestrationAction.status == "succeeded",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        require(context_action is not None and context_action.detail["acceptance_decision_id"] == accepted[0].id, "producer_context_changed")
        if receipt.verdict == "fail":
            require(spec.mode == "deployed", "producer_failure_mode_unavailable")
            require(context_action.detail["source_snapshot_hash"] == admission["source_snapshot_hash"], "producer_sources_changed")
            require(
                any(
                    item.outcome == "fail"
                    for item in receipt.criteria
                    if next(criterion.required for criterion in spec.criteria if criterion.criterion_id == item.criterion_id)
                ),
                "producer_required_failure_missing",
            )
            context_action.detail = {**context_action.detail, "failed_probe": snapshot}
            await record(
                session, node, dict(action="multi_repository_failed", producer_execution_id=context.execution.id, **snapshot), before="running"
            )
            return
        previous_hash = context_action.detail["source_snapshot_hash"]
        if previous_hash != admission["source_snapshot_hash"]:
            _, bindings, harness = await multi_repository_sources(session, node, plan, spec, lock=True)
            authority = await authorize(
                session,
                node,
                plan,
                accepted[1],
                scoped_binding(node.org_id, harness),
                self.provider.evidence,
                exclude_current_evaluation=True,
                source_repositories=tuple(binding.full_name for binding in bindings.values()),
            )
            require(authority == (accepted[0].id, admission["evaluation_policy_hash"]), "producer_authority_changed")
            context_action.detail = {**context_action.detail, "settled_source_snapshot_hash": admission["source_snapshot_hash"]}
            await record(
                session,
                node,
                dict(
                    action="multi_repository_sources_refreshed",
                    execution_id=context.execution.id,
                    previous_source_snapshot_hash=previous_hash,
                    source_snapshot_hash=admission["source_snapshot_hash"],
                ),
                before="running",
            )
        target = NodeState.AWAITING_GATE if spec.acceptance_mode == "human" else NodeState.PASSED
        require(
            transition(node.state, target, actor_kind=ActorKind.SERVICE, reason="Verified repository inputs").allowed, "producer_transition_refused"
        )
        node.state = target.value
        await record(
            session, node, dict(action="multi_repository_completed", producer_execution_id=context.execution.id, **snapshot), before="running"
        )
        released = await release_work(
            session,
            org_id=node.org_id,
            claim_id=context.identity.claim_id,
            generation=context.identity.claim_generation,
            reason=ReleaseReason.COMPLETED,
            terminal_evidence="evaluation/receipt/" + context.execution.id,
        )
        require(released.disposition in {Disposition.ADMITTED, Disposition.DUPLICATE}, "producer_claim_release_refused")
        if spec.acceptance_mode == "machine":
            from .tick import release_satisfied_successors

            await release_satisfied_successors(session, node)

    def decide(self, context, observation):
        if observation.block is not None:
            return HandlerDecision(DecisionKind.BLOCK, block=observation.block)
        if observation.stage == "repository_dispatch":
            data = observation.snapshot
            return HandlerDecision(
                DecisionKind.EFFECT,
                effect=EffectRequest(ActionIntent(data["operation_key"], data.get("producer_kind", PRODUCER_KIND), detail=data), Action.EVALUATE),
            )
        if observation.stage == "repository_settle":

            async def settle(session, current):
                await self.settle(session, current, observation.snapshot)

            failed = observation.snapshot.get("receipt", {}).get("verdict") == "fail"
            return HandlerDecision(
                DecisionKind.ADVANCE if failed else DecisionKind.CONCLUDE,
                phase=ExecutionPhase.EVALUATION_PENDING if failed else None,
                settlement=settle,
                next_check_at=context.now + timedelta(seconds=30) if failed else None,
                progress_note="Failed integration probe retained for source-bound correction."
                if failed
                else "Evaluation workflow finished; recorded evidence determines acceptance.",
            )
        return HandlerDecision(DecisionKind.WAIT, next_check_at=context.now + timedelta(seconds=30), progress_note=observation.detail)


async def producer_effect_policy(session, record, effect):
    """The runner may use only this exact accepted extension for a scan effect."""
    require(
        effect.intent.kind in {PRODUCER_KIND, CLI_PRODUCER_KIND, MULTI_PROBE_KIND} and effect.action is Action.EVALUATE,
        "producer_effect_changed",
    )
    from .execution_runner import RunnerContext

    identity = ExecutionIdentity(record.org_id, record.node_id, record.cycle, record.accepted_plan_version, record.claim_id, record.claim_generation)
    state = await producer_state(session, RunnerContext(identity, record, datetime.now(UTC)))
    require(
        (state[2].harness.harness_digest if effect.intent.kind == MULTI_PROBE_KIND else state[2].runner.harness_sha256) == harness_digest(),
        "producer_effect_harness_changed",
    )
    require(
        effect.intent.kind == producer_kind(state[2])
        and effect.intent.detail["acceptance_decision_id"] == state[3][0].id
        and effect.intent.detail["evaluation_policy_hash"] == state[3][2].policy_hash,
        "producer_effect_authority_changed",
    )
    return state[3][2]
