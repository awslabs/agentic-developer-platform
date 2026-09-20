"""K2 deployment workflow adapter: workflow completion still requires D3 verification."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .deployment_authority import load_delivery_merge
from .deployment_manifest import EntryStatus, PhysicalTarget, TargetEvidence, WorkflowRef, load_packaged_manifest, resolve_manifest_entry
from .deployment_target import DeploymentTargetResolver
from .deployment_workflow_provider import WorkflowDefinition, WorkflowProvider
from .dispatch import graph_address
from .environment_leases import LeaseHolder, acquire_lease
from .execution_policy import Action, CredentialScope, ResourceRef, authorize_action
from .execution_runner import (
    DecisionKind,
    EffectOutcome,
    EffectRequest,
    EffectResult,
    HandlerDecision,
    HandlerObservation,
    ObservationKind,
    OperationIdentity,
)
from .execution_state import ActionIntent, BlockCode, ExecutionPhase, Observation, ObservedOutcome, OutcomeKind
from .execution_store import load_execution, prepare_action, record_observation
from .models import OrchestrationAction, OrchestrationFlow, OrchestrationNode, OrchestrationWorkClaim
from .review_cycle import CycleBlockedError, block
from .review_cycle_dispatch import ReviewCycleServices

WORKFLOW_KIND = "deployment_workflow"
HANDOFF_KIND = "deployment_handoff"
PHASES = (ExecutionPhase.DEPLOYMENT_PENDING,)


class WorkflowReceipt(BaseModel):
    """D2 evidence for D3; this is never a runtime deployment acceptance receipt."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    org_id: str = Field(min_length=1, max_length=512)
    execution_id: str = Field(min_length=1, max_length=512)
    node_id: str = Field(min_length=1, max_length=512)
    flow_id: str = Field(min_length=1, max_length=512)
    cycle: int = Field(ge=1)
    accepted_plan_version: int = Field(ge=1)
    claim_id: str = Field(min_length=1, max_length=512)
    claim_generation: int = Field(ge=1)
    repo: str = Field(min_length=1, max_length=512)
    provider_repository_id: int = Field(gt=0)
    merge_operation_key: str = Field(min_length=1, max_length=512)
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    operation_key: str = Field(min_length=1, max_length=512)
    manifest_entry_id: str = Field(min_length=1, max_length=512)
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    components: list[str] = Field(min_length=1, max_length=32)
    remaining_entry_ids: list[str] = Field(max_length=32)
    workflow_path: str = Field(min_length=1, max_length=512)
    approved_definition_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    definition_blob_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    run_id: int = Field(gt=0)
    run_attempt: int = Field(gt=0)
    conclusion: str = Field(min_length=1, max_length=64)
    run_url: str = Field(min_length=1, max_length=1024)
    artifact_id: int = Field(gt=0)
    artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    target: dict
    inputs: dict[str, str] = Field(max_length=20)
    lease_id: str = Field(min_length=1, max_length=512)
    lease_revision: int = Field(ge=1)
    lease_holder_action_id: str = Field(min_length=1, max_length=512)
    observed_at: datetime


def canonical(value):
    def convert(item):
        if isinstance(item, set | frozenset):
            return sorted(item)
        raise TypeError(type(item).__name__)

    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=convert)


def components_for_paths(paths):
    """Code-owned component mapping; issue prose never selects a workflow."""
    components = set()
    for path in paths:
        if path.startswith("modules/gateway/frontend/"):
            components.add("gateway-frontend")
        elif path.startswith("modules/gateway/alembic/") or path == ".github/workflows/run-gateway-migrations.yml":
            components.add("gateway-migrations")
        elif path.startswith("modules/gateway/tests/") or path.startswith(("tests/", "docs/", "aidlc/")) or path.endswith(".md"):
            continue
        elif path.startswith("modules/gateway/") or path == ".github/workflows/gateway-deploy.yml":
            components.add("gateway-backend")
        elif path.startswith("modules/agent-factory/agent-worker-image/") or path.startswith("modules/agent-factory/agent/"):
            components.add("agent-worker")
        elif path.startswith("modules/agent-factory/webhook-ingress/"):
            components.add("agent-webhook")
        else:
            raise CycleBlockedError("deployment_component_unmapped", BlockCode.HUMAN_INPUT_REQUIRED)
    return tuple(sorted(components or {"documentation"}))


@dataclass(frozen=True)
class WorkflowObservation(HandlerObservation):
    snapshot: dict | None = None
    run: object | None = None
    target: object | None = None


class WorkflowServices:
    def __init__(self, factory, *, authority=None, provider=None, targets=None, manifest_loader=load_packaged_manifest):
        self.factory = factory
        self.authority = authority or ReviewCycleServices(factory)
        self.provider = provider or WorkflowProvider()
        self.targets = targets or DeploymentTargetResolver()
        self.manifest_loader = manifest_loader

    async def state(self, session, context):
        node = await session.scalar(
            select(OrchestrationNode)
            .where(OrchestrationNode.org_id == context.identity.org_id, OrchestrationNode.id == context.identity.node_id)
            .execution_options(populate_existing=True)
        )
        if node is None:
            raise CycleBlockedError("deployment_node_missing", BlockCode.OWNERSHIP_LOST)
        record, binding, merge = await load_delivery_merge(session, identity=context.identity, node=node)
        flow = await session.get(OrchestrationFlow, node.flow_id, populate_existing=True)
        claim = await session.get(OrchestrationWorkClaim, context.identity.claim_id, populate_existing=True)
        if flow is None or flow.state != "running" or claim is None or not claim.active_run_id:
            raise CycleBlockedError("deployment_flow_or_claim_unavailable", BlockCode.AUTHORITY_UNVERIFIABLE)
        return node, binding, merge, claim.active_run_id, flow

    async def actions(self, session, context):
        rows = list(
            (
                await session.scalars(
                    select(OrchestrationAction)
                    .where(
                        OrchestrationAction.org_id == context.identity.org_id,
                        OrchestrationAction.execution_id == context.execution.id,
                        OrchestrationAction.kind == WORKFLOW_KIND,
                    )
                    .order_by(OrchestrationAction.created_at)
                    .limit(101)
                )
            ).all()
        )
        if len(rows) > 100:
            raise CycleBlockedError("deployment_history_limit", BlockCode.ATTEMPTS_EXHAUSTED)
        return rows

    async def snapshot(self, context, *, reserved=False, reconcile=False):
        async with self.factory() as session:
            node, binding, merge, run_id, flow = await self.state(session, context)
            rows = await self.actions(session, context)
            # Reconciliation consumes the durable approval, even after a newer
            # branch, manifest or allowance would refuse a new deployment.
            outstanding = next((row for row in reversed(rows) if not (row.detail or {}).get("runtime_verified")), None)
            if reconcile and outstanding is not None:
                data = outstanding.detail
                if (data["source_revision"], data["binding_id"], data["binding_revision"], data["accepted_scope"]) != (
                    merge.merge_sha,
                    binding.id,
                    binding.revision,
                    binding.accepted_scope,
                ):
                    raise CycleBlockedError("deployment_recorded_scope_changed", BlockCode.OWNERSHIP_LOST)
                workflow = WorkflowRef(
                    **{**data["workflow"], "allowed_inputs": {k: frozenset(v) for k, v in data["workflow"]["allowed_inputs"].items()}}
                )
                physical = PhysicalTarget(**{**data["target"], "evidence": TargetEvidence(**data["target"]["evidence"])})
                return data, binding, workflow, WorkflowDefinition(**data["definition"]), physical, outstanding
            effective = (
                replace(context, execution=replace(context.execution, attempts=max(0, context.execution.attempts - 1))) if reserved else context
            )
            facts = await self.authority.authority_context(session, effective, node, binding, run_id, Action.DEPLOY, delivery=True)
            policy, principal, auth = facts[2].policy, facts[3], facts[-1]
            manifest = self.manifest_loader()
            paths = await self.provider.changed_files(binding)
            components = components_for_paths(paths)
            entries = {
                entry.entry_id: entry
                for component in components
                for entry in manifest.entries
                if entry.covers(component) and (entry.connection_id in policy.environment_connection_ids or entry.connection_id is None)
            }
            if len(entries) > 32:
                raise CycleBlockedError("deployment_manifest_selection_limit", BlockCode.HUMAN_INPUT_REQUIRED)
            if any(not any(entry.covers(component) for entry in entries.values()) for component in components):
                raise CycleBlockedError("deployment_component_has_no_approved_manifest", BlockCode.HUMAN_INPUT_REQUIRED)
            completed = {(row.detail or {}).get("manifest_entry_id") for row in rows if (row.detail or {}).get("runtime_verified") is True}
            remaining = [entry for key, entry in sorted(entries.items()) if key not in completed]
            if not remaining:
                return self.handoff(context, merge, components, "all_entries_verified"), binding, None, None, None, None
            entry = remaining[0]
            if entry.status is not EntryStatus.ENABLED:
                raise CycleBlockedError("deployment_target_unresolved", BlockCode.HUMAN_INPUT_REQUIRED)
            if entry.docs_only:
                if components != ("documentation",):
                    raise CycleBlockedError("deployment_docs_entry_cannot_cover_runtime", BlockCode.HUMAN_INPUT_REQUIRED)
                return self.handoff(context, merge, components, "documentation_only", entry.entry_id), binding, None, None, None, None
            if entry.artifact_revision != merge.merge_sha:
                raise CycleBlockedError("deployment_artifact_revision_not_approved", BlockCode.HUMAN_INPUT_REQUIRED)
            definition = await self.provider.definition(binding, entry.workflow, merge.merge_sha, for_dispatch=not reconcile)
            resolved = await self.targets.resolve(session, entry=entry, policy=policy, principal_user_id=principal, execution_id=context.execution.id)
            inputs = {}
            for name, values in entry.workflow.allowed_inputs.items():
                if len(values) == 1:
                    inputs[name] = next(iter(values))
                elif values and definition.defaults.get(name) not in values:
                    raise CycleBlockedError("deployment_inputs_require_manifest_choice", BlockCode.HUMAN_INPUT_REQUIRED)
            # Manual dispatch must use the registered user connection, not a
            # workflow's platform-account fallback. The manifest must approve it.
            connection_inputs = {
                "customer_account_id": resolved.physical.account_id,
                "customer_user_id": principal,
                "customer_aws_label": resolved.credential_label,
            }
            for name, value in connection_inputs.items():
                if name not in entry.workflow.allowed_inputs or value not in entry.workflow.allowed_inputs[name]:
                    raise CycleBlockedError("deployment_workflow_connection_not_approved", BlockCode.HUMAN_INPUT_REQUIRED)
                inputs[name] = value
            resolution = resolve_manifest_entry(
                manifest,
                entry_id=entry.entry_id,
                component=next(c for c in components if entry.covers(c)),
                policy_connection_ids=frozenset(policy.environment_connection_ids),
                target_lookup=lambda _: resolved.physical,
                requested_inputs=inputs,
                resolved_workflow_revision=definition.approved_revision,
            )
            if not resolution.resolved:
                raise CycleBlockedError("deployment_manifest_refused:" + resolution.block.code.value, BlockCode.HUMAN_INPUT_REQUIRED)
            decision = authorize_action(
                replace(auth, credential_scope=CredentialScope.USER_GRANTED),
                Action.DEPLOY,
                ResourceRef(
                    repository_id=binding.repo,
                    environment_connection_id=entry.connection_id,
                    node_address=graph_address(node, flow_slug=flow.slug),
                    org_id=node.org_id,
                    user_credential_id=resolved.credential_id,
                    aws_role_arn=resolved.role_arn,
                ),
                context.identity.accepted_plan_version,
            )
            if not decision.permitted:
                raise CycleBlockedError("deployment_authority_denied:" + decision.reason.value, BlockCode.AUTHORITY_UNVERIFIABLE)
            snapshot = dict(
                manifest_entry_id=entry.entry_id,
                manifest_digest=hashlib.sha256(canonical(asdict(entry)).encode()).hexdigest(),
                binding_id=binding.id,
                binding_revision=binding.revision,
                accepted_scope=binding.accepted_scope,
                source_revision=merge.merge_sha,
                merge_operation_key=merge.operation_key,
                repo=binding.repo,
                provider_repository_id=binding.provider_repository_id,
                components=[c for c in components if entry.covers(c)],
                remaining_entry_ids=[other.entry_id for other in remaining[1:]],
                policy_hash=policy.policy_hash,
                target=asdict(resolved.physical),
                inputs=inputs,
                definition=asdict(definition),
                workflow_path=entry.workflow.path,
                workflow=json.loads(canonical(asdict(entry.workflow))),
            )
            snapshot["target_key"] = resolved.physical.canonical_key
            key = OperationIdentity.from_context(
                context, WORKFLOW_KIND, entry.entry_id, snapshot["manifest_digest"], merge.merge_sha, snapshot["target_key"]
            ).key
            snapshot["operation_key"] = key
            snapshot["correlation"] = hashlib.sha256(key.encode()).hexdigest()
            latest = next((row for row in reversed(rows) if row.operation_key == key), None)
            return snapshot, binding, entry.workflow, definition, resolved.physical, latest

    def handoff(self, context, merge, components, reason, entry_id=None):
        return {
            "handoff_reason": reason,
            "operation_key": OperationIdentity.from_context(context, HANDOFF_KIND, merge.merge_sha, reason).key,
            "source_revision": merge.merge_sha,
            "merge_operation_key": merge.operation_key,
            "components": list(components),
            "manifest_entry_id": entry_id,
        }

    async def persist(self, context, snapshot, target, *, run=None, before_dispatch=False, require_pending=False):
        async with self.factory() as session:
            loaded = await load_execution(session, identity=context.identity, for_update=True)
            if (
                loaded is None
                or loaded.kind is not OutcomeKind.APPLIED
                or loaded.record is None
                or loaded.record.revision != context.execution.revision
            ):
                raise CycleBlockedError("deployment_intent_superseded", BlockCode.OWNERSHIP_LOST)
            if require_pending and loaded.record.pending_action_key != snapshot["operation_key"]:
                raise CycleBlockedError("deployment_pending_action_changed", BlockCode.OWNERSHIP_LOST)
            _, binding, _, _, _ = await self.state(session, context)
            if (binding.id, binding.revision, binding.accepted_scope) != (
                snapshot["binding_id"],
                snapshot["binding_revision"],
                snapshot["accepted_scope"],
            ):
                raise CycleBlockedError("deployment_scope_changed")
            action = await session.scalar(
                select(OrchestrationAction)
                .where(
                    OrchestrationAction.org_id == context.identity.org_id,
                    OrchestrationAction.execution_id == context.execution.id,
                    OrchestrationAction.operation_key == snapshot["operation_key"],
                )
                .with_for_update()
            )
            if action is None:
                raise CycleBlockedError("deployment_intent_missing")
            lease = await acquire_lease(
                session,
                target=target,
                holder=LeaseHolder(context.identity.org_id, action.id, context.identity.claim_generation),
                manifest_entry_id=snapshot["manifest_entry_id"],
                release_ref=snapshot["source_revision"],
            )
            if not lease.applied:
                raise CycleBlockedError("deployment_target_lease:" + (lease.reason or "held"), BlockCode.DEPENDENCY_UNSATISFIED)
            detail = {
                **(action.detail or {}),
                **snapshot,
                "lease_id": lease.lease.id,
                "lease_revision": lease.lease.revision,
                "lease_holder_action_id": action.id,
            }
            if before_dispatch:
                if detail.get("dispatch_started"):
                    raise CycleBlockedError("deployment_dispatch_already_attempted")
                detail["dispatch_started"] = True
            if run is not None:
                detail["workflow_run"] = {**asdict(run), "context": run.context.model_dump(mode="json")}
            action.detail = detail
            await session.commit()
            return detail

    async def perform(self, context, effect):
        expected = effect.intent.detail
        dispatch_started = False
        try:
            snapshot, binding, workflow, definition, target, latest = await self.snapshot(context, reserved=True)
            definition_keys = ("approved_revision", "source_revision", "blob_sha", "defaults", "caller_path", "caller_blob_sha")
            if any(snapshot[key] != expected[key] for key in ("operation_key", "inputs", "accepted_scope", "policy_hash")) or any(
                snapshot["definition"].get(key) != expected["definition"].get(key) for key in definition_keys
            ):
                raise CycleBlockedError("deployment_preconditions_changed")
            await self.persist(context, snapshot, target, require_pending=True)
            run, incomplete = await self.provider.observe(
                binding,
                workflow=workflow,
                definition=definition,
                target=target,
                source_revision=snapshot["source_revision"],
                inputs=snapshot["inputs"],
                correlation=snapshot["correlation"],
            )
            if run is not None:
                await self.persist(context, snapshot, target, run=run, require_pending=True)
                return EffectResult(
                    EffectOutcome.SUCCEEDED,
                    receipt_ref=f"github/actions/runs/{run.run_id}",
                    detail="Observed matching workflow; runtime remains unverified.",
                )
            if incomplete or (latest and (latest.detail or {}).get("dispatch_started")):
                return EffectResult(EffectOutcome.UNCERTAIN, detail="Workflow correlation remains unverified; retain target lease.")

            async def reauthorize():
                fresh, _, _, _, fresh_target, _ = await self.snapshot(context, reserved=True)
                if any(fresh[key] != snapshot[key] for key in ("operation_key", "inputs", "definition", "accepted_scope", "policy_hash")):
                    raise CycleBlockedError("deployment_authority_changed_before_dispatch")
                await self.persist(context, fresh, fresh_target, before_dispatch=True, require_pending=True)

            dispatch_started = True
            await self.provider.dispatch(
                binding,
                workflow=workflow,
                definition=definition,
                source_revision=snapshot["source_revision"],
                inputs=snapshot["inputs"],
                correlation=snapshot["correlation"],
                reauthorize=reauthorize,
            )
            return EffectResult(
                EffectOutcome.SUCCEEDED,
                receipt_ref="workflow-dispatch/" + snapshot["correlation"],
                detail="Dispatch accepted; correlated run remains to be observed.",
            )
        except CycleBlockedError as exc:
            return EffectResult(EffectOutcome.FAILED, detail=exc.reason)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {400, 401, 403, 404, 422}:
                return EffectResult(EffectOutcome.FAILED, detail="deployment_provider_refused")
            return EffectResult(EffectOutcome.UNCERTAIN, detail="Deployment provider outcome unknown; retain target lease.")
        except Exception:
            return EffectResult(EffectOutcome.UNCERTAIN if dispatch_started else EffectOutcome.FAILED, detail="deployment_provider_unavailable")


class DeploymentWorkflows:
    def __init__(self, factory, services=None):
        self.factory, self.services = factory, services or WorkflowServices(factory)

    async def observe(self, context):
        try:
            snapshot, binding, workflow, definition, target, latest = await self.services.snapshot(context, reconcile=True)
            if snapshot.get("handoff_reason"):
                return WorkflowObservation(ObservationKind.SUCCEEDED, snapshot=snapshot)
            if latest is not None and latest.status == "failed":
                raise CycleBlockedError(
                    "deployment_action_failed:" + str((latest.detail or {}).get("observation", "provider_refused")), BlockCode.HUMAN_INPUT_REQUIRED
                )
            run, incomplete = await self.services.provider.observe(
                binding,
                workflow=workflow,
                definition=definition,
                target=target,
                source_revision=snapshot["source_revision"],
                inputs=snapshot["inputs"],
                correlation=snapshot["correlation"],
                run_id=((latest.detail or {}).get("workflow_run") or {}).get("run_id") if latest else None,
            )
            if latest is None:
                if incomplete:
                    return WorkflowObservation(ObservationKind.WAITING, detail="Matching automatic workflow has not published target evidence yet.")
                return WorkflowObservation(ObservationKind.READY, snapshot=snapshot, run=run, target=target)
            snapshot = {**(latest.detail or {}), **snapshot}
            if run is None:
                if snapshot.get("dispatch_started") or latest.status in {"unknown", "succeeded"}:
                    return WorkflowObservation(
                        ObservationKind.UNCERTAIN, snapshot=snapshot, target=target, detail="Workflow outcome unknown; retaining target lease."
                    )
                return WorkflowObservation(ObservationKind.READY, snapshot=snapshot, target=target)
            return WorkflowObservation(
                ObservationKind.SUCCEEDED if run.status == "completed" else ObservationKind.WAITING,
                snapshot=snapshot,
                target=target,
                run=run,
                detail="Workflow observed; runtime verification remains separate.",
            )
        except CycleBlockedError as exc:
            return WorkflowObservation(ObservationKind.BLOCKED, block=block(exc.reason, exc.code))
        except Exception:
            return WorkflowObservation(ObservationKind.BLOCKED, block=block("deployment_evidence_unavailable", BlockCode.PROVIDER_UNAVAILABLE))

    def decide(self, context, observation):
        if observation.kind is ObservationKind.BLOCKED:
            return HandlerDecision(DecisionKind.BLOCK, block=observation.block)
        if observation.snapshot and observation.snapshot.get("handoff_reason"):

            async def handoff(session, current):
                data = observation.snapshot
                prepared = await prepare_action(
                    session, identity=current.identity, intent=ActionIntent(data["operation_key"], HANDOFF_KIND, detail=data)
                )
                if prepared.kind is not OutcomeKind.APPLIED:
                    raise CycleBlockedError("deployment_handoff_superseded")
                recorded = await record_observation(
                    session,
                    identity=current.identity,
                    observation=Observation(data["operation_key"], ObservedOutcome.SUCCEEDED, detail=data["handoff_reason"]),
                )
                if recorded.kind is not OutcomeKind.APPLIED:
                    raise CycleBlockedError("deployment_handoff_superseded")

            return HandlerDecision(
                DecisionKind.ADVANCE,
                phase=ExecutionPhase.AWAITING_RUNTIME_VERIFICATION,
                settlement=handoff,
                progress_note="Deployment selection complete; delivery acceptance remains separate.",
            )
        if observation.kind is ObservationKind.READY:
            data = observation.snapshot
            return HandlerDecision(
                DecisionKind.EFFECT,
                effect=EffectRequest(ActionIntent(data["operation_key"], WORKFLOW_KIND, detail=data), Action.DEPLOY),
                progress_note="Observe or dispatch the approved deployment workflow.",
            )
        if observation.run is not None:

            async def settle(session, current):
                # This callback owns only SQL state; no provider or credential I/O.
                data, run = observation.snapshot, observation.run
                action = await session.scalar(
                    select(OrchestrationAction).where(
                        OrchestrationAction.org_id == current.identity.org_id,
                        OrchestrationAction.execution_id == current.execution.id,
                        OrchestrationAction.operation_key == data["operation_key"],
                    )
                )
                if action is None:
                    raise CycleBlockedError("deployment_intent_missing")
                leased = await acquire_lease(
                    session,
                    target=observation.target,
                    holder=LeaseHolder(current.identity.org_id, action.id, current.identity.claim_generation),
                    manifest_entry_id=data["manifest_entry_id"],
                    release_ref=data["source_revision"],
                )
                if not leased.applied:
                    raise CycleBlockedError("deployment_target_lease_changed")
                detail = {**(action.detail or {}), "workflow_run": {**asdict(run), "context": run.context.model_dump(mode="json")}}
                if run.status == "completed":
                    receipt = WorkflowReceipt(
                        **asdict(current.identity),
                        execution_id=current.execution.id,
                        flow_id=current.execution.flow_id,
                        repo=data["repo"],
                        provider_repository_id=data["provider_repository_id"],
                        merge_operation_key=data["merge_operation_key"],
                        source_revision=data["source_revision"],
                        operation_key=data["operation_key"],
                        manifest_entry_id=data["manifest_entry_id"],
                        manifest_digest=data["manifest_digest"],
                        components=data["components"],
                        remaining_entry_ids=data["remaining_entry_ids"],
                        workflow_path=data["workflow_path"],
                        approved_definition_revision=data["definition"]["approved_revision"],
                        definition_blob_sha=data["definition"]["blob_sha"],
                        run_id=run.run_id,
                        run_attempt=run.run_attempt,
                        conclusion=run.conclusion or "unknown",
                        run_url=run.url,
                        artifact_id=run.artifact_id,
                        artifact_digest=run.artifact_digest,
                        target=data["target"],
                        inputs=data["inputs"],
                        lease_id=leased.lease.id,
                        lease_revision=leased.lease.revision,
                        lease_holder_action_id=action.id,
                        observed_at=datetime.fromisoformat(run.observed_at),
                    )
                    detail["workflow_receipt"] = receipt.model_dump(mode="json")
                action.detail = detail

            return HandlerDecision(
                DecisionKind.ADVANCE if observation.run.status == "completed" else DecisionKind.WAIT,
                phase=ExecutionPhase.AWAITING_RUNTIME_VERIFICATION if observation.run.status == "completed" else ExecutionPhase.DEPLOYMENT_PENDING,
                settlement=settle,
                next_check_at=context.now + timedelta(seconds=60),
                progress_note=observation.detail,
            )
        return HandlerDecision(DecisionKind.WAIT, next_check_at=context.now + timedelta(seconds=60), progress_note=observation.detail)

    async def perform(self, context, effect):
        return await self.services.perform(context, effect)


def handlers(factory):
    return dict.fromkeys(PHASES, DeploymentWorkflows(factory))


def bounded_workflow_summary(detail):
    try:
        receipt = WorkflowReceipt.model_validate((detail or {})["workflow_receipt"])
    except (KeyError, TypeError, ValueError):
        return None
    return receipt.model_dump(
        mode="json",
        include={"source_revision", "manifest_entry_id", "workflow_path", "run_id", "run_attempt", "conclusion", "run_url", "observed_at"},
    )
