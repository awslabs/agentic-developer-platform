"""Verify an operations run against a reviewed target without requiring a PR."""

from dataclasses import asdict, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from zipfile import BadZipFile

from pydantic import ValidationError
from sqlalchemy import select

from src.agentauth.github_operation_service import require_current_claim

from .accepted_node import accepted_node_contract
from .delivery_contract import release_manifest, verify_delivery_evidence
from .deployment_controller import ADAPTERS, RECEIPT_LIFETIME
from .deployment_manifest import EntryStatus, load_packaged_manifest
from .deployment_release_provider import ReleaseProvider
from .deployment_runtime_contract import DeploymentReceipt, RuntimeComponent
from .deployment_runtime_reader import PilotRuntimeReader, require
from .deployment_target import DeploymentTargetResolver
from .dispatch import graph_address
from .environment_leases import foundation_release_scope, shared_release_held
from .execution_policy import Action, CredentialScope, ResourceRef, authorize_action
from .execution_state import ExecutionIdentity
from .models import OrchestrationAction, OrchestrationExecution, OrchestrationFlow, OrchestrationNode
from .operations_release_resolution import operational_execution_pins
from .operations_repair import repair_revisions
from .operations_workflow_evidence import CorrelatedWorkflowReceipt, context_receipt_ref, current_push_head
from .policy_admission import load_in_force_policy
from .product_case_selection import PRODUCT_CASE_BINDINGS
from .review_cycle_dispatch import ReviewCycleServices


async def _verify_operations_runtime(
    factory,
    *,
    binding,
    issued,
    execution,
    raw_run,
    context_action_id,
    reauthorize,
    provider=None,
    targets=None,
    runtime=None,
    manifest_loader=load_packaged_manifest,
    controller_authority=None,
):
    provider = provider or ReleaseProvider()
    targets = targets or DeploymentTargetResolver()
    runtime = runtime or PilotRuntimeReader()
    invocation = execution["invocation_id"]["S"]
    async with factory() as session:
        node = await session.get(OrchestrationNode, issued.node_id)
        contract = await accepted_node_contract(session, node) if node is not None else None
        row = (
            await session.scalar(
                select(OrchestrationExecution).where(
                    OrchestrationExecution.org_id == binding.org_id,
                    OrchestrationExecution.node_id == issued.node_id,
                    OrchestrationExecution.cycle == node.attempts,
                    OrchestrationExecution.accepted_plan_version == issued.plan_version,
                )
            )
            if node is not None
            else None
        )
        context_action = await session.get(OrchestrationAction, context_action_id)
        accepted_pins = (
            await operational_execution_pins(session, node=node, execution=row, contract=contract, run_id=invocation)
            if contract is not None and row is not None
            else None
        )
        pins = (
            await repair_revisions(
                session,
                node_id=node.id,
                org_id=binding.org_id,
                plan_version=issued.plan_version,
                cycle=node.attempts,
                pins=accepted_pins,
                contract=contract,
            )
            if accepted_pins is not None
            else None
        )
        workflow = raw_run["path"].removeprefix(".github/workflows/")
        components = [item.removeprefix("runtime_verified:") for item in contract.node.evidence_expectations] if contract else []
        if (
            row is None
            or contract is None
            or contract.plan_version != issued.plan_version
            or node.org_id != binding.org_id
            or contract.node.repository_identity not in (None, binding.repo)
            or contract.node.executor is None
            or contract.node.executor.role != "operate"
            or contract.node.executor.persona != execution["persona"]["S"]
            or len(components) != 1
            or workflow not in issued.workflow_allowlist
            or pins is None
            or pins != {key: execution.get(key, {}).get("S") for key in pins}
            or row.status in {"concluded", "superseded"}
            or context_action is None
            or context_action.org_id != binding.org_id
            or context_action.execution_id != row.id
            or context_action.kind != "correlated_workflow_observation"
            or context_action.status != "succeeded"
            or context_action.operation_key != f"correlated-workflow:{raw_run['id']}:{raw_run['run_attempt']}"
        ):
            return None
        try:
            context_receipt = CorrelatedWorkflowReceipt.model_validate((context_action.detail or {}).get("correlated_workflow_receipt"))
        except ValidationError:
            return None
        if (
            context_receipt.valid_until <= datetime.now(UTC)
            or context_receipt.repo != binding.repo
            or context_receipt.component != components[0]
            or context_receipt.workflow != workflow
            or context_receipt.run_id != raw_run["id"]
            or context_receipt.run_attempt != raw_run["run_attempt"]
            or context_receipt.event != raw_run.get("event", "workflow_dispatch")
            or context_receipt.claim_id != row.claim_id
            or context_receipt.claim_generation != row.claim_generation
            or context_receipt.source_revision != pins["source_revision"]
            or context_receipt.definition_revision != pins["definition_revision"]
            or context_action.receipt_ref
            != context_receipt_ref(raw_run["id"], context_receipt.context_artifact_id, context_receipt.context_reference)
        ):
            return None
        identity = ExecutionIdentity(binding.org_id, node.id, node.attempts, issued.plan_version, row.claim_id, row.claim_generation)
        source = SimpleNamespace(identity=identity, execution=row)
        flow = await session.get(OrchestrationFlow, node.flow_id)
        if controller_authority is None:
            authority = await ReviewCycleServices(factory).authority_context(session, source, node, binding, invocation, Action.DEPLOY)
        else:
            authority = await controller_authority(session, source)
        _, _, admission, principal, _, auth = authority
        policy = admission.policy
        require(flow is not None and policy is not None and policy.permits(Action.DEPLOY), "operations_runtime_policy_missing")
        release = getattr(contract.node, "operational_release_binding", None)
        delivery = getattr(release, "delivery_contract", None)
        require(delivery == getattr(issued, "delivery_contract", None), "operations_runtime_contract_changed")
        product_cases = getattr(release, "product_cases", "none")
        require(
            product_cases == getattr(issued, "product_cases", "none")
            and (
                product_cases == "none"
                or (PRODUCT_CASE_BINDINGS.get(product_cases) == (components[0], workflow) and raw_run.get("event") == "workflow_dispatch")
            ),
            "operations_runtime_product_selection_changed",
        )
        proof_phase = getattr(release, "proof_phase", "none")
        require(
            proof_phase == getattr(issued, "proof_phase", "none")
            and (
                proof_phase == "none"
                or (
                    proof_phase == "stage.identity-proof"
                    and product_cases == "none"
                    and (components[0], workflow) == ("gateway-backend", "gateway-deploy.yml")
                    and raw_run.get("event") == "workflow_dispatch"
                )
            ),
            "operations_runtime_proof_selection_changed",
        )
        resolved = None
        if release is not None:
            from .run_reports import OrchestrationRunReport

            report = await session.get(OrchestrationRunReport, invocation)
            resolved = report.dispatch_metadata["orchestration"]["release_binding"]
        manifest = release_manifest(release, manifest_loader)
        matches = [
            entry
            for entry in manifest.entries
            if entry.status is EntryStatus.ENABLED
            and not entry.docs_only
            and entry.covers(components[0])
            and entry.workflow is not None
            and entry.workflow.path == raw_run["path"]
            and (
                (
                    entry.entry_id == resolved.get("target")
                    and entry.connection_id == resolved.get("connection_id")
                    and entry.resource_kind == resolved.get("resource_kind")
                    and entry.resource_id == resolved.get("resource_id")
                    and entry.workflow.definition_revision == resolved.get("manifest_definition_revision")
                    and entry.artifact_revision == resolved.get("manifest_artifact_revision")
                    and (
                        entry.release_group is None
                        or (bool(entry.mutation_role_arn) and entry.mutation_role_arn == resolved.get("mutation_role_arn"))
                    )
                )
                if resolved is not None
                else entry.release_group is None
                and entry.workflow.definition_revision == accepted_pins["definition_revision"]
                and entry.artifact_revision == accepted_pins["source_revision"]
            )
            and (delivery is not None or components[0] in ADAPTERS.get(entry.verification_adapter, frozenset()))
        ]
        require(len(matches) == 1, "operations_runtime_manifest_unavailable")
        approved_entry = matches[0]
        entry = approved_entry
        if resolved is not None or pins != accepted_pins:
            entry = replace(
                approved_entry,
                workflow=replace(approved_entry.workflow, definition_revision=pins["definition_revision"]),
                artifact_revision=pins["source_revision"],
            )
        if raw_run.get("event") == "push" and pins != accepted_pins:
            require(
                context_receipt.event == "push" and await current_push_head(provider, binding, pins["default_branch"]) == pins["source_revision"],
                "operations_runtime_push_revision_changed",
            )

        def authorize_scope(credential_id, role_arn):
            decision = authorize_action(
                replace(auth, credential_scope=CredentialScope.USER_GRANTED),
                Action.DEPLOY,
                ResourceRef(
                    repository_id=binding.repo,
                    environment_connection_id=entry.connection_id,
                    node_address=graph_address(node, flow_slug=flow.slug),
                    org_id=node.org_id,
                    user_credential_id=credential_id,
                    aws_role_arn=role_arn,
                ),
                issued.plan_version,
            )
            require(decision.permitted, "operations_runtime_target_not_authorized")

        first = await targets.resolve(
            session, entry=entry, policy=policy, principal_user_id=principal, execution_id=row.id, authorize_scope=authorize_scope
        )
        require(
            (first.physical.resource_kind, first.physical.resource_id) == (entry.resource_kind, entry.resource_id),
            "operations_runtime_target_changed",
        )
        definition = await provider.definition(
            binding, entry.workflow, pins["source_revision"], for_dispatch=False, **({"generic_delivery": True} if delivery is not None else {})
        )
        head = raw_run.get("head_sha")
        workflow_revision = pins["definition_revision"]
        if raw_run.get("event") == "workflow_dispatch" and isinstance(head, str):
            # Artifact and workflow revisions can differ during a rollback.
            # Bind observation to the actual approved workflow head, including
            # when it equals the definition or source pin. Other heads must
            # contain the source and preserve the approved workflow bytes.
            if head != workflow_revision:
                await provider.contains(binding, pins["source_revision"], head)
                require(
                    await provider.workflow_content_matches(binding, entry.workflow, workflow_revision, head),
                    "operations_runtime_definition_changed",
                )
            definition = replace(definition, dispatch_revision=head)
            workflow_revision = head
        run, _ = await provider.observe(
            binding,
            workflow=entry.workflow,
            definition=definition,
            target=first.physical,
            source_revision=pins["source_revision"],
            inputs={
                **(delivery.inputs if delivery is not None else {}),
                **({"product_cases": product_cases} if product_cases != "none" else {}),
                **({"proof_phase": proof_phase} if proof_phase != "none" else {}),
                **(
                    {"release_group": "atlassian-foundation"}
                    if approved_entry.release_group == "atlassian-foundation" and components == ["gateway-backend"]
                    else {}
                ),
                **({"stage": "tool-adapters"} if approved_entry.release_group == "atlassian-foundation" and components == ["tool-adapters"] else {}),
            },
            correlation=invocation if raw_run.get("event") == "workflow_dispatch" else None,
            run_id=raw_run["id"],
        )
        require(
            run is not None
            and run.status == "completed"
            and run.conclusion == "success"
            and run.run_attempt == raw_run["run_attempt"]
            and (run.artifact_id, getattr(run, "artifact_ref", None)) == (context_receipt.context_artifact_id, context_receipt.context_reference)
            and run.artifact_digest == context_receipt.context_digest
            and run.context.source_revision == pins["source_revision"]
            and (product_cases == "none" or run.context.inputs.get("product_cases") == product_cases)
            and (delivery is not None or run.context.inputs.get("proof_phase", "none") == proof_phase)
            and (
                delivery is not None
                or run.context.inputs.get("release_group", "none")
                == (
                    "atlassian-foundation" if approved_entry.release_group == "atlassian-foundation" and components == ["gateway-backend"] else "none"
                )
            )
            and (delivery is not None or components != ["tool-adapters"] or run.context.inputs.get("stage") == "tool-adapters")
            and (proof_phase == "none" or run.context.inputs.get("environment") == "dev")
            and run.context.workflow_revision == workflow_revision
            and (run.context.account_id, run.context.region, run.context.resource_kind, run.context.resource_id)
            == (first.physical.account_id, first.physical.region, first.physical.resource_kind, first.physical.resource_id),
            "operations_runtime_run_changed",
        )
        if delivery is not None:
            require(raw_run.get("event") == "workflow_dispatch", "delivery_correlated_run_required")
            artifact, digest, reference = await provider.release(binding, run, components[0], delivery_contract=delivery)
            component = verify_delivery_evidence(
                artifact,
                contract=delivery,
                context=run.context,
                component=components[0],
                digest=digest,
                reference=reference,
            )
            verified = first
        else:
            artifact, digest, reference = await provider.release(binding, run, components[0])

            def inspect(scoped, description, namespace):
                return runtime.inspect(
                    scoped,
                    description,
                    namespace,
                    artifact=artifact,
                    artifact_hash=digest,
                    evidence_ref=reference,
                    environment=run.context.inputs.get("environment"),
                )

            verified = await targets.resolve(
                session,
                entry=entry,
                policy=policy,
                principal_user_id=principal,
                execution_id=row.id,
                inspect_target=inspect,
                authorize_scope=authorize_scope,
            )
            require(verified.physical.canonical_key == first.physical.canonical_key, "operations_runtime_target_changed")
            component = RuntimeComponent.model_validate(verified.observation)
            require(
                component.component == components[0]
                and component.actual_revision == pins["source_revision"]
                and component.artifact_hash == digest
                and component.evidence_ref == reference,
                "operations_runtime_component_changed",
            )
    await reauthorize()
    async with factory() as session:
        locked = await session.scalar(select(OrchestrationExecution).where(OrchestrationExecution.id == row.id).with_for_update())
        current_node = await session.get(OrchestrationNode, node.id)
        current = await session.get(OrchestrationAction, context_action_id)
        if controller_authority is None:
            await require_current_claim(session, org_id=binding.org_id, invocation_id=invocation, claim_generation=identity.claim_generation)
        else:
            await controller_authority(session, source)
        current_policy = await load_in_force_policy(session, org_id=binding.org_id, flow_id=node.flow_id)
        current_contract = await accepted_node_contract(session, current_node) if current_node is not None else None
        current_base_pins = (
            await operational_execution_pins(session, node=current_node, execution=locked, contract=current_contract, run_id=invocation)
            if current_contract is not None and current_node is not None and locked is not None
            else None
        )
        current_pins = (
            await repair_revisions(
                session,
                node_id=node.id,
                org_id=binding.org_id,
                plan_version=issued.plan_version,
                cycle=node.attempts,
                pins=current_base_pins,
                contract=current_contract,
            )
            if current_base_pins is not None
            else None
        )
        require(
            current_policy.refusal is None
            and current_policy.plan_version == issued.plan_version
            and current_policy.policy == policy
            and current_contract == contract
            and current_pins == pins
            and current_node.attempts == identity.cycle
            and current_node.state == node.state
            and any(
                saved == approved_entry for saved in release_manifest(current_contract.node.operational_release_binding, manifest_loader).entries
            ),
            "operations_runtime_authority_changed",
        )
        if raw_run.get("event") == "push" and pins != accepted_pins:
            require(
                await current_push_head(provider, binding, pins["default_branch"]) == pins["source_revision"],
                "operations_runtime_push_revision_changed",
            )
        require(
            locked is not None
            and locked.status not in {"concluded", "superseded"}
            and locked.claim_id == identity.claim_id
            and locked.claim_generation == identity.claim_generation
            and current is not None
            and current.org_id == binding.org_id
            and current.execution_id == row.id
            and current.kind == "correlated_workflow_observation"
            and current.status == "succeeded"
            and current.receipt_ref == context_action.receipt_ref
            and current.detail == context_action.detail,
            "operations_runtime_authority_changed",
        )
        scope = (
            None
            if delivery is not None
            else foundation_release_scope(
                node=current_node,
                flow=flow,
                entry=approved_entry,
                component=components[0],
                target=verified.physical,
                environment=run.context.inputs["environment"],
                source_revision=pins["source_revision"],
            )
        )
        if scope is not None:
            shared_target, owner = scope
            started = raw_run.get("created_at")
            try:
                started_at = datetime.fromisoformat(started.replace("Z", "+00:00")) if isinstance(started, str) else None
            except ValueError:
                started_at = None
            require(
                raw_run.get("run_attempt") == 1
                and started_at is not None
                and await shared_release_held(
                    session,
                    target=shared_target,
                    org_id=binding.org_id,
                    action_id=owner,
                    release_ref=pins["source_revision"],
                    manifest_entry_id=approved_entry.entry_id,
                    generation=identity.claim_generation,
                    started_at=started_at,
                ),
                "operations_shared_release_lease_missing",
            )
            native = (
                await session.scalars(
                    select(OrchestrationAction)
                    .where(
                        OrchestrationAction.org_id == identity.org_id,
                        OrchestrationAction.execution_id == row.id,
                        OrchestrationAction.kind == "operations_native_release_dispatch",
                        OrchestrationAction.detail["correlation"].as_string() == invocation,
                    )
                    .limit(2)
                )
            ).all()
            require(len(native) == 1, "operations_shared_release_run_unpinned")
            saved = native[0].detail or {}
            release = saved.get("shared_release") or {}
            require(
                raw_run.get("event") == "workflow_dispatch"
                and native[0].status in {"prepared", "succeeded", "unknown"}
                and saved.get("dispatch_started") is True
                and saved.get("correlation") == invocation
                and saved.get("repo") == binding.repo
                and saved.get("provider_repository_id") == binding.provider_repository_id
                and saved.get("workflow") == approved_entry.workflow.path.removeprefix(".github/workflows/")
                and saved.get("source_revision") == pins["source_revision"]
                and saved.get("dispatch_revision") == raw_run.get("head_sha")
                and saved.get("release_job") == {"run_id": str(raw_run["id"]), "run_attempt": "1"}
                and release.get("owner") == owner
                and release.get("target_key") == shared_target.canonical_key
                and release.get("manifest_entry_id") == approved_entry.entry_id
                and release.get("generation") == identity.claim_generation
                and release.get("environment") == run.context.inputs["environment"],
                "operations_shared_release_run_unpinned",
            )
        workflow_key = f"operations-workflow:{raw_run['id']}:{raw_run['run_attempt']}"
        receipt_key = f"operations-runtime:{raw_run['id']}:{raw_run['run_attempt']}"
        receipt = DeploymentReceipt(
            schema_version=2 if delivery is not None else 1,
            org_id=identity.org_id,
            execution_id=row.id,
            node_id=node.id,
            flow_id=node.flow_id,
            cycle=identity.cycle,
            accepted_plan_version=identity.accepted_plan_version,
            claim_id=identity.claim_id,
            claim_generation=identity.claim_generation,
            operation_key=receipt_key,
            repo=binding.repo,
            source_revision=pins["source_revision"],
            actual_revision=pins["source_revision"],
            merge_operation_key=f"operations-source:{pins['source_revision']}",
            workflow_operation_keys=[workflow_key],
            manifest_entry_ids=[entry.entry_id],
            targets=[asdict(verified.physical)],
            components=[component],
            delivery_complete=True,
            observed_at=component.observed_at,
            valid_until=min(policy.expires_at, component.observed_at + RECEIPT_LIFETIME),
        )
        require(receipt.valid_until > datetime.now(UTC), "operations_runtime_observation_expired")
        previous = await session.scalar(
            select(OrchestrationAction).where(
                OrchestrationAction.org_id == identity.org_id,
                OrchestrationAction.execution_id == row.id,
                OrchestrationAction.operation_key == receipt_key,
            )
        )
        if previous is not None:
            prior = DeploymentReceipt.model_validate((previous.detail or {})["deployment_receipt"])
            require(
                previous.status == "succeeded"
                and previous.receipt_ref == receipt_key
                and prior.valid_until > datetime.now(UTC)
                and prior.claim_id == receipt.claim_id
                and prior.claim_generation == receipt.claim_generation
                and prior.source_revision == receipt.source_revision
                and prior.workflow_operation_keys == receipt.workflow_operation_keys
                and len(prior.targets) == len(receipt.targets) == 1
                and {key: value for key, value in prior.targets[0].items() if key != "evidence"}
                == {key: value for key, value in receipt.targets[0].items() if key != "evidence"}
                and len(prior.components) == 1
                and prior.components[0].model_dump(exclude={"observed_at"}) == component.model_dump(exclude={"observed_at"}),
                "operations_runtime_receipt_changed",
            )
            if delivery is not None:
                await session.commit()
            return previous.id
        if delivery is not None:
            from .environment_leases import LeaseHolder, LeaseOutcomeKind, ReleaseReason, release_lease

            released = await release_lease(
                session,
                canonical_target_key=verified.physical.canonical_key,
                holder=LeaseHolder(identity.org_id, f"delivery:{row.id}", identity.claim_generation),
                reason=ReleaseReason.COMPLETED,
                terminal_evidence=f"github/actions/runs/{raw_run['id']}/attempts/{raw_run['run_attempt']}",
            )
            require(released.kind is LeaseOutcomeKind.APPLIED, "delivery_target_lease_changed")
            dispatches = await session.scalars(
                select(OrchestrationAction).where(
                    OrchestrationAction.org_id == identity.org_id,
                    OrchestrationAction.execution_id == row.id,
                    OrchestrationAction.kind == "delivery_workflow_dispatch",
                )
            )
            for dispatched in dispatches:
                saved = dispatched.detail or {}
                if (
                    saved.get("correlation") == invocation
                    and saved.get("workflow") == raw_run["path"]
                    and saved.get("source_revision") == pins["source_revision"]
                    and saved.get("contract_digest") == delivery.digest
                ):
                    dispatched.status = "succeeded"
        session.add(
            OrchestrationAction(
                org_id=identity.org_id,
                execution_id=row.id,
                operation_key=workflow_key,
                kind="deployment_workflow",
                status="succeeded",
                attempt=1,
                detail={
                    "runtime_verified": True,
                    "context_action_id": context_action_id,
                    **({"controller_observation": True} if controller_authority is not None else {}),
                },
                receipt_ref=current.receipt_ref,
            )
        )
        recorded = OrchestrationAction(
            org_id=identity.org_id,
            execution_id=row.id,
            operation_key=receipt_key,
            kind="deployment_verification",
            status="succeeded",
            attempt=1,
            detail={"deployment_receipt": receipt.model_dump(mode="json")},
            receipt_ref=receipt_key,
        )
        session.add(recorded)
        await session.flush()
        result = recorded.id
        await session.commit()
        return result


async def verify_operations_runtime(factory, **kwargs):
    from .review_cycle import CycleBlockedError
    from .validation_diagnostics import record_runtime_validation

    try:
        result = await _verify_operations_runtime(factory, **kwargs)
    except CycleBlockedError as exc:
        await record_runtime_validation(
            factory, binding=kwargs["binding"], issued=kwargs["issued"], code=exc.reason, context_action_id=kwargs["context_action_id"]
        )
        raise
    except (ValidationError, BadZipFile) as exc:
        await record_runtime_validation(
            factory,
            binding=kwargs["binding"],
            issued=kwargs["issued"],
            code="delivery_evidence_invalid",
            context_action_id=kwargs["context_action_id"],
        )
        raise CycleBlockedError("delivery_evidence_invalid") from exc
    if result is not None:
        await record_runtime_validation(
            factory,
            binding=kwargs["binding"],
            issued=kwargs["issued"],
            code="runtime_verified",
            passed=True,
            context_action_id=kwargs["context_action_id"],
        )
    return result
