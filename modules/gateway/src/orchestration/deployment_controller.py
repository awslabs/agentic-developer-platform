"""D3 runtime verification, bounded recovery and durable evaluation handoff."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta

import httpx
from botocore.exceptions import ClientError
from botocore.exceptions import ConnectionError as AWSConnectionError
from sqlalchemy import select

from .deployment_manifest import EntryStatus, PhysicalTarget, TargetEvidence
from .deployment_release_provider import ReleaseProvider
from .deployment_runtime_contract import DeploymentReceipt
from .deployment_runtime_reader import PilotRuntimeReader, require
from .deployment_workflows import HANDOFF_KIND, WorkflowReceipt, WorkflowServices, canonical, components_for_paths
from .dispatch import graph_address
from .environment_leases import LeaseHolder, ReleaseReason, release_lease
from .execution_policy import Action, CredentialScope, ResourceRef, authorize_action
from .execution_runner import DecisionKind, HandlerDecision, HandlerObservation, ObservationKind, OperationIdentity
from .execution_state import ActionIntent, BlockCode, ExecutionPhase, Observation, ObservedOutcome, OutcomeKind
from .execution_store import prepare_action, record_observation
from .models import OrchestrationAction
from .review_cycle import CycleBlockedError, block

DEPLOYMENT_KIND = "deployment_verification"
PHASES = (ExecutionPhase.AWAITING_RUNTIME_VERIFICATION,)
ADAPTERS = {
    "gateway-health-verification": frozenset({"gateway-backend", "gateway-frontend"}),
    "alembic-single-head-verification": frozenset({"gateway-migrations"}),
}
# No rollback adapter is registered. A manifest name alone cannot authorize a
# mutation; failed verification retains its lease and requires accepted repair.
RECEIPT_LIFETIME = timedelta(minutes=10)


def validate_identity(receipt, context, merge, binding):
    require(all(getattr(receipt, key) == value for key, value in asdict(context.identity).items()), "deployment_receipt_ownership_changed")
    require(
        (receipt.execution_id, receipt.flow_id, receipt.repo, receipt.source_revision, receipt.merge_operation_key)
        == (context.execution.id, context.execution.flow_id, binding.repo, merge.merge_sha, merge.operation_key),
        "deployment_receipt_merge_changed",
    )


@dataclass(frozen=True)
class RuntimeObservation(HandlerObservation):
    receipt: DeploymentReceipt | None = None
    workflow: WorkflowReceipt | None = None
    remaining: bool = False
    binding_revision: int | None = None
    manifest_hash: str | None = None


class DeploymentServices(WorkflowServices):
    def __init__(self, factory, *, runtime=None, **kwargs):
        kwargs.setdefault("provider", ReleaseProvider())
        super().__init__(factory, **kwargs)
        self.runtime = runtime or PilotRuntimeReader()

    async def current(self, context):
        async with self.factory() as session:
            node, binding, merge, run_id, flow = await self.state(session, context)
            rows = await self.actions(session, context)
            # Check the original bound before any credential/provider I/O. An
            # unavailable authority service must not reset the observation budget.
            for row in rows:
                if (row.detail or {}).get("runtime_verified") is not True:
                    require(
                        datetime.now(UTC) < datetime.fromisoformat(row.detail["observation_deadline"]),
                        "deployment_runtime_observation_deadline_reached",
                    )
            facts = await self.authority.authority_context(session, context, node, binding, run_id, Action.DEPLOY, delivery=True)
            handoffs = list(
                (
                    await session.scalars(
                        select(OrchestrationAction)
                        .where(
                            OrchestrationAction.org_id == context.identity.org_id,
                            OrchestrationAction.execution_id == context.execution.id,
                            OrchestrationAction.kind == HANDOFF_KIND,
                            OrchestrationAction.status == "succeeded",
                        )
                        .order_by(OrchestrationAction.created_at.desc())
                        .limit(2)
                    )
                ).all()
            )
        return node, binding, merge, flow, facts[2].policy, facts[3], facts[-1], rows, handoffs

    async def verify(self, context):
        node, binding, merge, flow, policy, principal, auth, rows, handoffs = await self.current(context)
        manifest = self.manifest_loader()
        manifest_hash = hashlib.sha256(canonical(asdict(manifest)).encode()).hexdigest()
        outstanding = [row for row in rows if (row.detail or {}).get("runtime_verified") is not True]
        require(len(outstanding) <= 1, "deployment_outstanding_workflows_ambiguous")
        now = datetime.now(UTC)
        if outstanding:
            require(now < datetime.fromisoformat(outstanding[0].detail["observation_deadline"]), "deployment_runtime_observation_deadline_reached")
        components = components_for_paths(await self.provider.changed_files(binding))
        if not outstanding:
            require(bool(handoffs), "deployment_handoff_missing")
            handoff = handoffs[0]
            data = handoff.detail
            reason = data.get("handoff_reason")
            require(
                data.get("source_revision") == merge.merge_sha and data.get("merge_operation_key") == merge.operation_key,
                "deployment_handoff_merge_changed",
            )
            require(
                data.get("operation_key") == OperationIdentity.from_context(context, HANDOFF_KIND, merge.merge_sha, reason).key,
                "deployment_handoff_ownership_changed",
            )
            require(data.get("components") == list(components), "deployment_handoff_components_changed")
            if reason == "documentation_only":
                entry = next((e for e in manifest.entries if e.entry_id == data.get("manifest_entry_id")), None)
                require(
                    components == ("documentation",)
                    and entry is not None
                    and entry.docs_only
                    and entry.status is EntryStatus.ENABLED
                    and entry.covers("documentation"),
                    "deployment_docs_classification_unverifiable",
                )
                receipt = self.receipt(
                    context,
                    binding,
                    merge,
                    actual=merge.merge_sha,
                    components=[],
                    targets=[],
                    workflows=[],
                    entries=[entry.entry_id],
                    deadline=policy.expires_at,
                    docs_only=True,
                    delivery_complete=True,
                )
            else:
                require(reason == "all_entries_verified" and bool(rows), "deployment_handoff_unrecognized")
                require(all(row.detail.get("runtime_manifest_hash") == manifest_hash for row in rows), "deployment_verified_manifest_changed")
                verified = [DeploymentReceipt.model_validate(row.detail["deployment_receipt"]) for row in rows]
                for item in verified:
                    validate_identity(item, context, merge, binding)
                    require(item.valid_until > now, "deployment_prior_runtime_evidence_expired")
                require(set(components) == {c.component for item in verified for c in item.components}, "deployment_component_evidence_incomplete")
                actuals = {item.actual_revision for item in verified}
                require(len(actuals) == 1, "deployment_component_revisions_inconsistent")
                receipt = self.receipt(
                    context,
                    binding,
                    merge,
                    actual=actuals.pop(),
                    components=[c for item in verified for c in item.components],
                    targets=[t for item in verified for t in item.targets],
                    workflows=[key for item in verified for key in item.workflow_operation_keys],
                    entries=[key for item in verified for key in item.manifest_entry_ids],
                    deadline=min(item.valid_until for item in verified),
                    delivery_complete=True,
                )
            return RuntimeObservation(ObservationKind.SUCCEEDED, receipt=receipt, binding_revision=binding.revision, manifest_hash=manifest_hash)

        return await self.verify_workflow(
            context,
            outstanding[0],
            node=node,
            binding=binding,
            merge=merge,
            flow=flow,
            policy=policy,
            principal=principal,
            auth=auth,
            components=components,
            manifest=manifest,
        )

    async def verify_workflow(
        self,
        context,
        row,
        *,
        node,
        binding,
        merge,
        flow,
        policy,
        principal,
        auth,
        components,
        manifest,
        action=Action.DEPLOY,
        resource_address=None,
        revalidate=False,
    ):
        """Read the authenticated release and actual runtime; never settle a lease.

        E2 reuses this after a test run. Only an already verified D2 entry may
        outlive its original observation deadline, and current policy, manifest,
        provider attempt, registered role and runtime checks still apply.
        """
        now = datetime.now(UTC)
        manifest_hash = hashlib.sha256(canonical(asdict(manifest)).encode()).hexdigest()
        data = row.detail
        workflow = WorkflowReceipt.model_validate(data["workflow_receipt"])
        validate_identity(workflow, context, merge, binding)
        require(
            workflow.operation_key == row.operation_key
            and workflow.lease_holder_action_id == row.id
            and workflow.provider_repository_id == binding.provider_repository_id,
            "deployment_workflow_receipt_changed",
        )
        if revalidate:
            require(data.get("runtime_verified") is True, "evaluation_deployment_not_verified")
        deadline = policy.expires_at if revalidate else min(datetime.fromisoformat(data["observation_deadline"]), policy.expires_at)
        require(now < deadline, "deployment_runtime_observation_deadline_reached")
        require(workflow.conclusion == "success", "deployment_workflow_failed_requires_accepted_repair")
        entry = next((e for e in manifest.entries if e.entry_id == workflow.manifest_entry_id), None)
        require(entry is not None and entry.status is EntryStatus.ENABLED and not entry.docs_only, "deployment_verification_manifest_unavailable")
        require(set(workflow.components) == {c for c in components if entry.covers(c)}, "deployment_verification_components_changed")
        require(set(workflow.components) <= ADAPTERS.get(entry.verification_adapter, frozenset()), "deployment_verification_adapter_unsupported")
        target = PhysicalTarget(**{**workflow.target, "evidence": TargetEvidence(**workflow.target["evidence"])})
        require(
            (entry.resource_kind, entry.resource_id, entry.workflow.path) == (target.resource_kind, target.resource_id, workflow.workflow_path),
            "deployment_verification_target_changed",
        )
        actual = entry.artifact_revision
        if actual == merge.merge_sha:
            require(
                hashlib.sha256(canonical(asdict(entry)).encode()).hexdigest() == workflow.manifest_digest, "deployment_verification_manifest_changed"
            )
            # Re-read the provider. A re-run must not inherit an earlier attempt's
            # successful context/artifacts or release the original hold early.
            from .deployment_workflow_provider import WorkflowDefinition

            definition = WorkflowDefinition(**data["definition"])
            run, _ = await self.provider.observe(
                binding,
                workflow=entry.workflow,
                definition=definition,
                target=target,
                source_revision=actual,
                inputs=data["inputs"],
                correlation=data["correlation"],
                run_id=workflow.run_id,
            )
            require(
                run is not None
                and (run.run_id, run.run_attempt, run.artifact_id, run.artifact_digest)
                == (workflow.run_id, workflow.run_attempt, workflow.artifact_id, workflow.artifact_digest),
                "deployment_workflow_attempt_changed",
            )
        else:
            # A newer manifest pin is explicit release approval; ancestry alone
            # cannot approve the revision, target, workflow or runtime artifacts.
            await self.provider.contains(binding, merge.merge_sha, actual)
            definition = await self.provider.definition(binding, entry.workflow, actual, for_dispatch=False)
            run, _ = await self.provider.observe(
                binding, workflow=entry.workflow, definition=definition, target=target, source_revision=actual, inputs=data["inputs"]
            )
        require(run is not None and run.status == "completed" and run.conclusion == "success", "deployment_release_workflow_not_successful")
        artifacts = [await self.provider.release(binding, run, component) for component in workflow.components]

        def inspect(scoped, description, namespace):
            return [
                self.runtime.inspect(
                    scoped,
                    description,
                    namespace,
                    artifact=artifact,
                    artifact_hash=digest,
                    evidence_ref=ref,
                    environment=run.context.inputs.get("environment"),
                )
                for artifact, digest, ref in artifacts
            ]

        def authorize_scope(credential_id, role_arn):
            decision = authorize_action(
                replace(auth, credential_scope=CredentialScope.USER_GRANTED),
                action,
                ResourceRef(
                    repository_id=binding.repo,
                    environment_connection_id=entry.connection_id,
                    node_address=resource_address or graph_address(node, flow_slug=flow.slug),
                    org_id=node.org_id,
                    user_credential_id=credential_id,
                    aws_role_arn=role_arn,
                ),
                context.identity.accepted_plan_version,
            )
            if not decision.permitted:
                raise CycleBlockedError("deployment_runtime_authority_denied:" + decision.reason.value, BlockCode.AUTHORITY_UNVERIFIABLE)

        async with self.factory() as session:
            resolved = await self.targets.resolve(
                session,
                entry=entry,
                policy=policy,
                principal_user_id=principal,
                execution_id=context.execution.id,
                inspect_target=inspect,
                authorize_scope=authorize_scope,
                action=action,
            )
        require(resolved.physical.canonical_key == target.canonical_key, "deployment_runtime_physical_target_changed")
        authorize_scope(resolved.credential_id, resolved.role_arn)
        verified = resolved.observation
        require(
            isinstance(verified, list) and len(verified) == len(workflow.components) and {c.component for c in verified} == set(workflow.components),
            "deployment_runtime_components_missing",
        )
        receipt = self.receipt(
            context,
            binding,
            merge,
            actual=actual,
            components=verified,
            targets=[asdict(resolved.physical)],
            workflows=[workflow.operation_key],
            entries=[entry.entry_id],
            deadline=deadline,
        )
        return RuntimeObservation(
            ObservationKind.SUCCEEDED,
            receipt=receipt,
            workflow=workflow,
            remaining=True,
            binding_revision=binding.revision,
            manifest_hash=manifest_hash,
        )

    def receipt(
        self, context, binding, merge, *, actual, components, targets, workflows, entries, deadline, docs_only=False, delivery_complete=False
    ):
        now = datetime.now(UTC)
        require(
            all(now - RECEIPT_LIFETIME <= c.observed_at <= now + timedelta(seconds=30) for c in components), "deployment_runtime_observation_stale"
        )
        # The oldest component controls the validity window; aggregation cannot
        # refresh old evidence just by copying it into a new receipt.
        observed = min((c.observed_at for c in components), default=now)
        return DeploymentReceipt(
            **asdict(context.identity),
            execution_id=context.execution.id,
            flow_id=context.execution.flow_id,
            operation_key=OperationIdentity.from_context(
                context, DEPLOYMENT_KIND, merge.merge_sha, actual, "delivery" if delivery_complete else "entry", *entries
            ).key,
            repo=binding.repo,
            source_revision=merge.merge_sha,
            actual_revision=actual,
            merge_operation_key=merge.operation_key,
            workflow_operation_keys=workflows,
            manifest_entry_ids=entries,
            targets=targets,
            components=components,
            docs_only=docs_only,
            delivery_complete=delivery_complete,
            observed_at=observed,
            valid_until=min(deadline, observed + RECEIPT_LIFETIME),
        )

    async def settle(self, session, context, observation):
        _, binding, merge, _, _ = await self.state(session, context)
        receipt = observation.receipt
        validate_identity(receipt, context, merge, binding)
        require(binding.revision == observation.binding_revision, "deployment_binding_changed_during_verification")
        require(
            hashlib.sha256(canonical(asdict(self.manifest_loader())).encode()).hexdigest() == observation.manifest_hash,
            "deployment_manifest_changed_during_verification",
        )
        require(receipt.valid_until > datetime.now(UTC), "deployment_runtime_evidence_expired")
        prepared = await prepare_action(
            session,
            identity=context.identity,
            intent=ActionIntent(receipt.operation_key, DEPLOYMENT_KIND, detail={"deployment_receipt": receipt.model_dump(mode="json")}),
        )
        require(prepared.kind is OutcomeKind.APPLIED, "deployment_verification_superseded")
        if observation.workflow:
            workflow = observation.workflow
            row = await session.scalar(
                select(OrchestrationAction)
                .where(
                    OrchestrationAction.org_id == context.identity.org_id,
                    OrchestrationAction.execution_id == context.execution.id,
                    OrchestrationAction.operation_key == workflow.operation_key,
                )
                .with_for_update()
            )
            require(
                row is not None and (row.detail or {}).get("workflow_receipt") == workflow.model_dump(mode="json"),
                "deployment_workflow_changed_during_verification",
            )
            target = PhysicalTarget(**{**workflow.target, "evidence": TargetEvidence(**workflow.target["evidence"])})
            released = await release_lease(
                session,
                canonical_target_key=target.canonical_key,
                holder=LeaseHolder(context.identity.org_id, workflow.lease_holder_action_id, context.identity.claim_generation),
                reason=ReleaseReason.COMPLETED,
                terminal_evidence=receipt.operation_key,
            )
            require(released.applied, "deployment_lease_ownership_changed")
            row.detail = {
                **row.detail,
                "runtime_verified": True,
                "runtime_manifest_hash": observation.manifest_hash,
                "deployment_receipt": receipt.model_dump(mode="json"),
            }
        recorded = await record_observation(
            session,
            identity=context.identity,
            observation=Observation(
                receipt.operation_key,
                ObservedOutcome.SUCCEEDED,
                receipt_ref=receipt.operation_key,
                detail="Runtime verified; evaluation remains required.",
            ),
        )
        require(recorded.kind is OutcomeKind.APPLIED, "deployment_verification_superseded")


class DeploymentController:
    def __init__(self, factory, services=None):
        self.services = services or DeploymentServices(factory)

    async def observe(self, context):
        try:
            return await self.services.verify(context)
        except CycleBlockedError as exc:
            return RuntimeObservation(ObservationKind.BLOCKED, block=block(exc.reason, exc.code))
        except (httpx.HTTPError, AWSConnectionError, TimeoutError) as exc:
            transient = not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code == 429 or exc.response.status_code >= 500
            if transient:
                return RuntimeObservation(ObservationKind.WAITING, detail="Runtime provider temporarily unavailable; retry within original bounds.")
            return RuntimeObservation(ObservationKind.BLOCKED, block=block("deployment_runtime_provider_refused", BlockCode.PROVIDER_UNAVAILABLE))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"ThrottlingException", "Throttling", "RequestLimitExceeded", "ServiceUnavailable"}:
                return RuntimeObservation(ObservationKind.WAITING, detail="Runtime provider throttled; retry within original bounds.")
            return RuntimeObservation(ObservationKind.BLOCKED, block=block("deployment_runtime_provider_refused", BlockCode.PROVIDER_UNAVAILABLE))
        except Exception:
            return RuntimeObservation(
                ObservationKind.BLOCKED, block=block("deployment_runtime_evidence_unverifiable", BlockCode.PROVIDER_UNAVAILABLE)
            )

    def decide(self, context, observation):
        if observation.kind is ObservationKind.BLOCKED:
            return HandlerDecision(DecisionKind.BLOCK, block=observation.block)
        if observation.kind is not ObservationKind.SUCCEEDED:
            return HandlerDecision(DecisionKind.WAIT, next_check_at=context.now + timedelta(seconds=60), progress_note=observation.detail)

        async def settle(session, current):
            await self.services.settle(session, current, observation)

        return HandlerDecision(
            DecisionKind.ADVANCE,
            phase=ExecutionPhase.DEPLOYMENT_PENDING if observation.remaining else ExecutionPhase.EVALUATION_PENDING,
            settlement=settle,
            progress_note="Deployed revision verified; evaluation barriers remain in force.",
        )


def handlers(factory):
    return dict.fromkeys(PHASES, DeploymentController(factory))


def bounded_deployment_summary(detail):
    try:
        receipt = DeploymentReceipt.model_validate((detail or {})["deployment_receipt"])
    except (KeyError, TypeError, ValueError):
        return None
    return receipt.model_dump(
        mode="json",
        include={"source_revision", "actual_revision", "manifest_entry_ids", "delivery_complete", "docs_only", "observed_at", "valid_until"},
    )
