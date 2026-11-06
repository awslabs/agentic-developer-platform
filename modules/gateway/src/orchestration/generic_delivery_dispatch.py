"""Reserve a physical target before an agent dispatches its accepted workflow."""

from dataclasses import asdict, replace
from types import SimpleNamespace

from sqlalchemy import select

from .deployment_target import DeploymentTargetResolver
from .dispatch import graph_address
from .environment_leases import LeaseHolder, LeaseOutcomeKind, acquire_lease
from .execution_policy import Action, CredentialScope, ResourceRef, authorize_action
from .execution_state import ExecutionIdentity
from .models import OrchestrationAction, OrchestrationExecution
from .review_cycle import CycleBlockedError
from .review_cycle_dispatch import ReviewCycleServices


async def reserve_dispatch(
    session,
    *,
    factory,
    node,
    flow,
    contract,
    entry,
    binding,
    execution,
    run_id=None,
    run_attempt=None,
    dispatch_revision=None,
    provider=None,
    token=None,
):
    row = await session.scalar(
        select(OrchestrationExecution)
        .where(
            OrchestrationExecution.org_id == node.org_id,
            OrchestrationExecution.node_id == node.id,
            OrchestrationExecution.cycle == node.attempts,
            OrchestrationExecution.accepted_plan_version == contract.plan_version,
        )
        .with_for_update()
    )
    if row is None or row.status in {"concluded", "superseded"}:
        raise CycleBlockedError("delivery_execution_changed")
    identity = ExecutionIdentity(node.org_id, node.id, row.cycle, contract.plan_version, row.claim_id, row.claim_generation)
    context = SimpleNamespace(identity=identity, execution=row)
    invocation = execution["invocation_id"]["S"]
    _, _, admission, principal, _, auth = await ReviewCycleServices(factory).authority_context(
        session, context, node, binding, invocation, Action.DEPLOY
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
            contract.plan_version,
        )
        if not decision.permitted:
            raise CycleBlockedError("operations_runtime_target_not_authorized")

    suffix = "initial" if run_id is None else f"rerun:{run_id}:{run_attempt}"
    key = f"delivery-dispatch:{execution['source_revision']['S']}:{suffix}"
    previous = await session.scalar(
        select(OrchestrationAction).where(
            OrchestrationAction.org_id == node.org_id, OrchestrationAction.execution_id == row.id, OrchestrationAction.operation_key == key
        )
    )
    if previous is not None:
        if previous.status != "succeeded":
            raise CycleBlockedError("delivery_dispatch_reconciliation_required")
        return {"id": previous.id, "already_dispatched": True}

    if run_id is not None:
        dispatches = await session.scalars(
            select(OrchestrationAction).where(
                OrchestrationAction.org_id == node.org_id,
                OrchestrationAction.execution_id == row.id,
                OrchestrationAction.kind == "delivery_workflow_dispatch",
                OrchestrationAction.status == "succeeded",
            )
        )
        # A rerun reuses GitHub's original inputs. Its authenticated failed
        # context must have confirmed this contract before we repeat it.
        if not any(
            (saved := action.detail or {}).get("correlation") == invocation
            and saved.get("contract_digest") == contract.node.operational_release_binding.delivery_contract.digest
            and saved.get("workflow") == entry.workflow.path
            and saved.get("source_revision") == execution["source_revision"]["S"]
            and (terminal := saved.get("terminal_run", {})).get("run_id") == run_id
            and terminal.get("run_attempt") == run_attempt
            for action in dispatches
        ):
            raise CycleBlockedError("delivery_dispatch_reconciliation_required")

    target = await DeploymentTargetResolver().resolve(
        session, entry=entry, policy=admission.policy, principal_user_id=principal, execution_id=row.id, authorize_scope=authorize_scope
    )
    delivery = contract.node.operational_release_binding.delivery_contract
    if delivery.job_mutation_role_arn is not None:
        import re

        if (
            not isinstance(dispatch_revision, str)
            or re.fullmatch(r"[0-9a-f]{40}", dispatch_revision) is None
            or not getattr(target, "role_arn", None)
            or delivery.job_mutation_role_arn.split(":")[4] != target.physical.account_id
        ):
            raise CycleBlockedError("delivery_job_authority_unbound")
    if provider is not None:
        from .generic_delivery_recovery import reconcile_failed_dispatches, reconcile_retired_target

        await reconcile_failed_dispatches(
            session, binding=binding, node_id=node.id, provider=provider, token=token, target_key=target.physical.canonical_key
        )
        await reconcile_retired_target(session, binding=binding, target_key=target.physical.canonical_key, provider=provider, token=token)
    outcome = await acquire_lease(
        session,
        target=target.physical,
        holder=LeaseHolder(node.org_id, f"delivery:{row.id}", row.claim_generation),
        manifest_entry_id=entry.entry_id,
        release_ref=execution["source_revision"]["S"],
    )
    if outcome.kind is not LeaseOutcomeKind.APPLIED:
        raise CycleBlockedError("delivery_target_busy")
    action = OrchestrationAction(
        org_id=node.org_id,
        execution_id=row.id,
        operation_key=key,
        kind="delivery_workflow_dispatch",
        status="prepared",
        attempt=1,
        detail={
            "target": asdict(target.physical),
            "correlation": invocation,
            "contract_digest": contract.node.operational_release_binding.delivery_contract.digest,
            "workflow": entry.workflow.path,
            "source_revision": execution["source_revision"]["S"],
            "run_id": run_id,
            "run_attempt": run_attempt,
            "repo": binding.repo,
            "provider_repository_id": binding.provider_repository_id,
            "dispatch_revision": dispatch_revision,
            "definition_revision": execution["definition_revision"]["S"],
            "branch": execution["default_branch"]["S"],
            "role_arn": getattr(target, "role_arn", None),
            "claim_id": row.claim_id,
            "claim_generation": row.claim_generation,
        },
    )
    session.add(action)
    await session.flush()
    return {"id": action.id, "already_dispatched": False}


async def settle_dispatch(factory, reservation, *, org_id, succeeded):
    async with factory() as session:
        action = await session.scalar(
            select(OrchestrationAction).where(OrchestrationAction.id == reservation["id"], OrchestrationAction.org_id == org_id).with_for_update()
        )
        if action is not None and action.status == "prepared":
            action.status = "succeeded" if succeeded else "unknown"
            await session.commit()
