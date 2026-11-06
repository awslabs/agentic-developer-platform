"""Reserve the accepted operations release group before its external dispatch."""

from dataclasses import asdict, replace
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import select

from .deployment_manifest import EntryStatus, load_packaged_manifest
from .deployment_target import DeploymentTargetResolver
from .dispatch import graph_address
from .dispatch_pass import attempt_run_id
from .environment_leases import LeaseHolder, acquire_lease, foundation_release_scope
from .execution_policy import Action, CredentialScope, ResourceRef, authorize_action
from .execution_state import ExecutionIdentity
from .execution_store import _live_authority_conflict
from .models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from .operations_continuation_authority import assignment_matches, require, transfer
from .operations_release_resolution import recorded_operations_release
from .operations_repair import RepairMergeRequiredError, repair_revisions


async def accepted_release_pins(session, *, node, execution, contract, report):
    if contract is None or getattr(contract.node, "operational_release_binding", None) is None:
        return None
    pins = await recorded_operations_release(
        session,
        node=node,
        execution=execution,
        contract=contract,
        metadata=(report.dispatch_metadata or {}).get("orchestration") or {},
    )
    if pins is None:
        return None
    # A merged repair moves these pins forward, and the Identity stage of
    # tools-platform-deploy qualifies the adapter against the gateway image
    # tagged with that same pinned source revision (#8723), so a repaired source
    # has to produce its own matching gateway release. Nothing here changes the
    # pins; the note records why an adapter repair touches gateway source too.
    try:
        return await repair_revisions(
            session,
            node_id=node.id,
            org_id=node.org_id,
            # Repair receipts belong to the original execution. A later scope
            # amendment can preserve that assignment without creating an
            # execution row at the new plan version.
            plan_version=execution.accepted_plan_version,
            cycle=execution.cycle,
            pins=pins,
            contract=contract,
            require_merged=True,
        )
    except RepairMergeRequiredError:
        return None


async def reserve_native_release(
    session, *, context, state, workflow, environment, initial=False, dispatch_revision=None, resolver=None, manifest_loader=load_packaged_manifest
):
    release = getattr(state.contract.node, "operational_release_binding", None)
    recorded = ((state.report.dispatch_metadata or {}).get("orchestration") or {}).get("release_binding") or {}
    entry = manifest_loader().entry(release.target) if release is not None else None
    pins = await accepted_release_pins(session, node=state.node, execution=state.row, contract=state.contract, report=state.report)
    require(
        pins is not None
        and pins == {key: state.execution.get(key, {}).get("S") for key in pins}
        and release is not None
        and entry is not None
        and entry.status is EntryStatus.ENABLED
        and not entry.docs_only
        and entry.release_group is not None
        and entry.entry_id == recorded.get("target")
        and entry.connection_id == recorded.get("connection_id")
        and bool(entry.mutation_role_arn)
        and entry.mutation_role_arn == recorded.get("mutation_role_arn")
        and entry.resource_kind == recorded.get("resource_kind")
        and entry.resource_id == recorded.get("resource_id")
        and entry.artifact_revision == recorded.get("manifest_artifact_revision")
        and entry.workflow is not None
        and entry.workflow.definition_revision == recorded.get("manifest_definition_revision")
        and entry.workflow.path == recorded.get("workflow") == f".github/workflows/{workflow}"
        and entry.covers(release.component)
        and release.repository == state.binding.repo
        and workflow in release.workflows
        and state.contract.node.evidence_expectations == [f"runtime_verified:{release.component}"]
        and entry.connection_id in state.contract.environment_connection_ids
        and environment in entry.workflow.allowed_inputs.get("environment", ())
        and (
            release.component != "tool-adapters"
            or (workflow == "tools-platform-deploy.yml" and "tool-adapters" in entry.workflow.allowed_inputs.get("stage", ()))
        ),
        "operations_shared_release_scope_changed",
    )
    source = state.execution["source_revision"]["S"]
    expected_node_state = state.node.state
    expected_attempt = state.node.attempts
    flow = await session.get(OrchestrationFlow, state.node.flow_id)
    require(flow is not None and flow.org_id == state.node.org_id, "operations_shared_release_owner_changed")
    expected_flow_slug = flow.slug
    policy, principal, auth = state.values[2].policy, state.values[3], state.values[-1]

    def authorize_scope(credential_id, role_arn):
        decision = authorize_action(
            replace(auth, credential_scope=CredentialScope.USER_GRANTED),
            Action.DEPLOY,
            ResourceRef(
                repository_id=state.binding.repo,
                environment_connection_id=entry.connection_id,
                node_address=graph_address(state.node, flow_slug=flow.slug),
                org_id=state.node.org_id,
                user_credential_id=credential_id,
                aws_role_arn=role_arn,
            ),
            state.contract.plan_version,
        )
        require(decision.permitted, "operations_shared_release_target_not_authorized")

    target = await (resolver or DeploymentTargetResolver()).resolve(
        session,
        entry=entry,
        policy=policy,
        principal_user_id=principal,
        execution_id=state.row.id,
        authorize_scope=authorize_scope,
    )
    require(
        (target.physical.resource_kind, target.physical.resource_id) == (entry.resource_kind, entry.resource_id),
        "operations_shared_release_target_changed",
    )
    require(
        entry.mutation_role_arn.split(":")[4] == target.physical.account_id
        and getattr(target, "role_arn", None) is not None
        and entry.mutation_role_arn != target.role_arn,
        "operations_shared_release_mutation_role_changed",
    )
    identity = ExecutionIdentity(
        state.node.org_id,
        state.node.id,
        expected_attempt,
        state.contract.plan_version,
        context.identity.claim_id,
        context.identity.claim_generation,
    )
    # Canonical lock order is node -> flow/claim/plan -> execution, as documented in
    # the result observer and the worker write path. Acquiring the node FOR UPDATE
    # before the authority rows keeps this reservation from inverting against that
    # observer; every binding below is still revalidated under the same locks.
    anchor = await session.get(OrchestrationNode, state.node.id, with_for_update=True, populate_existing=True)
    require(
        anchor is not None and anchor.org_id == identity.org_id and anchor.flow_id == flow.id,
        "operations_shared_release_owner_changed",
    )
    require(
        await _live_authority_conflict(session, identity, flow_id=flow.id, require_unarchived=True) is None,
        "operations_shared_release_owner_changed",
    )
    flow = await session.get(OrchestrationFlow, flow.id, populate_existing=True)
    require(
        flow is not None
        and flow.slug == expected_flow_slug
        and flow.state in {"pending", "running"}
        and not flow.execution_paused
        and flow.archived_at is None,
        "operations_shared_release_owner_changed",
    )
    plan = await session.scalar(
        select(OrchestrationAcceptedPlan.version).where(
            OrchestrationAcceptedPlan.org_id == state.node.org_id,
            OrchestrationAcceptedPlan.flow_id == flow.id,
            OrchestrationAcceptedPlan.superseded_at.is_(None),
        )
    )
    require(plan == state.contract.plan_version, "operations_shared_release_scope_changed")
    if initial:
        claim = await session.get(OrchestrationWorkClaim, state.row.claim_id, with_for_update=True, populate_existing=True)
        require(
            claim is not None
            and claim.org_id == state.node.org_id
            and claim.state == "held"
            and claim.generation == context.identity.claim_generation
            and claim.active_run_id == state.report.run_id
            and claim.claim_event_id == state.report.run_id
            and claim.lease_expires_at is not None
            and claim.lease_expires_at.tzinfo is not None
            and claim.lease_expires_at > datetime.now(UTC),
            "operations_shared_release_owner_changed",
        )
    row = await session.get(OrchestrationExecution, state.row.id, with_for_update=True, populate_existing=True)
    require(
        row is not None
        and row.org_id == state.node.org_id
        and row.node_id == state.node.id
        and row.flow_id == flow.id
        and row.cycle == identity.cycle
        and row.id == context.execution.id
        and row.claim_id == context.identity.claim_id
        and row.claim_generation == context.identity.claim_generation
        and row.revision == context.execution.revision
        and (row.pending_action_key is None if initial else row.pending_action_key is not None),
        "operations_shared_release_owner_changed",
    )
    node = await session.get(OrchestrationNode, row.node_id, with_for_update=True, populate_existing=True)
    require(
        node is not None
        and node.org_id == row.org_id
        and node.flow_id == flow.id
        and node.state == expected_node_state
        and node.attempts == expected_attempt,
        "operations_shared_release_owner_changed",
    )
    native_key = f"native-release:{node.id}:{node.attempts}:{sha256(workflow.encode()).hexdigest()}"
    key = native_key if initial else row.pending_action_key
    action = await session.scalar(
        select(OrchestrationAction)
        .where(
            OrchestrationAction.org_id == row.org_id,
            OrchestrationAction.execution_id == row.id,
            OrchestrationAction.operation_key == key,
        )
        .with_for_update()
    )
    if initial:
        require(
            action is None
            # The engine persists the admitted wait (phase ADMITTED, status
            # AWAITING_EXTERNAL) before the first native actions_dispatch, so the
            # initial reservation must accept that exact admitted wait as well as a
            # runnable row. Status is compared first so a row in any other status
            # never reads `phase`, and no other phase may reserve while waiting.
            and (row.status == "runnable" or (row.status == "awaiting_external" and row.phase == "admitted"))
            and row.handoff_receipt_ref is None
            and row.accepted_plan_version == state.contract.plan_version
            and node.state == "running"
            and node.attempts == row.cycle
            and state.report.run_id == attempt_run_id(node.id, node.attempts)
            and state.execution.get("invocation_id", {}).get("S") == state.report.run_id
            and state.report.repo == state.binding.repo
            and state.report.provider_repository_id == state.binding.provider_repository_id
            and type(state.binding.provider_repository_id) is int
            and state.binding.provider_repository_id > 0
            and isinstance(dispatch_revision, str)
            and len(dispatch_revision) == 40
            and all(character in "0123456789abcdef" for character in dispatch_revision)
            and isinstance(state.execution.get("default_branch", {}).get("S"), str)
            and 0 < len(state.execution["default_branch"]["S"]) <= 255
            and await assignment_matches(session, node=node, execution=row, report=state.report),
            "operations_shared_release_intent_changed",
        )
    else:
        # A lease refusal becomes UNKNOWN in the runner. Re-preparing the same
        # operation preserves that status, so allow it only before any native
        # dispatch reservation or publication retry has begun.
        require(
            action is not None
            and action.kind == "operations_continuation_deploy"
            and action.status in {"prepared", "unknown"}
            and (action.detail or {}).get("workflow") == workflow
            and "shared_release" not in (action.detail or {})
            and not (action.detail or {}).get("native_retry")
            and not (action.detail or {}).get("dispatch_started")
            and not (action.detail or {}).get("native_release_action_id"),
            "operations_shared_release_intent_changed",
        )
        require(await transfer(session, node=node, execution=row, report=state.report), "operations_shared_release_owner_changed")
        previous = await session.scalar(
            select(OrchestrationAction).where(
                OrchestrationAction.org_id == row.org_id,
                OrchestrationAction.execution_id == row.id,
                OrchestrationAction.operation_key == native_key,
            )
        )
        require(previous is None, "operations_shared_release_intent_changed")
        require(
            isinstance(dispatch_revision, str)
            and len(dispatch_revision) == 40
            and all(character in "0123456789abcdef" for character in dispatch_revision)
            and isinstance(state.execution.get("default_branch", {}).get("S"), str)
            and state.report.run_id == state.execution.get("invocation_id", {}).get("S"),
            "operations_shared_release_intent_changed",
        )
    shared, owner = foundation_release_scope(
        node=node,
        flow=flow,
        entry=entry,
        component=release.component,
        target=target.physical,
        environment=environment,
        source_revision=source,
    )
    result = await acquire_lease(
        session,
        target=shared,
        holder=LeaseHolder(row.org_id, owner, row.claim_generation),
        manifest_entry_id=entry.entry_id,
        release_ref=source,
    )
    require(result.applied, "operations_shared_release_held")
    native = OrchestrationAction(
        org_id=row.org_id,
        execution_id=row.id,
        operation_key=native_key,
        kind="operations_native_release_dispatch",
        status="prepared",
        attempt=1,
        detail={
            "workflow": workflow,
            "correlation": state.report.run_id,
            "repo": state.binding.repo,
            "provider_repository_id": state.binding.provider_repository_id,
            "source_revision": source,
            "definition_revision": pins["definition_revision"],
            "dispatch_revision": dispatch_revision,
            "branch": state.execution["default_branch"]["S"],
            "dispatch_started": True,
            **({"controller_dispatch": True} if not initial else {}),
        },
    )
    session.add(native)
    native.detail = {
        **native.detail,
        "shared_release": {
            "owner": owner,
            "target": asdict(target.physical),
            "mutation_role_arn": entry.mutation_role_arn,
            "target_key": shared.canonical_key,
            "manifest_entry_id": entry.entry_id,
            "source_revision": source,
            "environment": environment,
            "generation": row.claim_generation,
        },
    }
    await session.flush()
    if not initial:
        action.detail = {**action.detail, "native_release_action_id": native.id, "dispatch_started": True}
    return result
