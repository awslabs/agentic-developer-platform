"""Review immutable bootstrap ownership while cleanup access is unavailable."""

import json
from dataclasses import asdict

from harness_jobs.identity import decode_payload, payload_digest
from harness_jobs.store import OperationStore
from sqlalchemy import select, text

from app.adapters.operation_authority_source import GrantBackedAuthority
from app.database import async_session_factory
from app.models.workspace import Workspace
from app.services.onboarding import policy_for
from app.services.provisioning import (
    ProvisioningRefused,
    ProvisioningUnavailable,
)


class _ReviewStore:
    def __init__(self, session):
        self.session = session

    def execute(self, statement, parameters):
        result = self.session.execute(text(statement), parameters)
        return result.mappings().all() if result.returns_rows else []

    def transaction(self):
        return self.session.begin_nested()


async def _workspace(db, org_id, workspace_id):
    workspace = await db.scalar(
        select(Workspace).where(
            Workspace.id == workspace_id, Workspace.org_id == org_id
        )
    )
    principal = await GrantBackedAuthority(async_session_factory).resolve(
        org_id=str(org_id),
        workspace_id=str(workspace_id),
        permission="workspace:provision",
    )
    if workspace is None or principal is None or workspace.is_default:
        raise ProvisioningRefused("workspace retirement authority refused")
    return workspace, principal


async def retirement_facts(composition, db, org_id, workspace_id):
    from superplane_bootstrap.registry import SqlRegistrationStore
    from workspace_provisioning.artifacts import read_artifact
    from workspace_provisioning.retirement_inventory import (
        load_bootstrap_retirement_review,
    )
    from workspace_provisioning.retirement_plan import compose_retirement_plan
    from workspace_provisioning.runtime_config import validate_runtime_config

    workspace, principal = await _workspace(db, org_id, workspace_id)
    if workspace.status not in {"Active", "active", "Teardown", "retired"}:
        raise ProvisioningRefused("only a registered workspace can be retired")
    async with composition.operation_connect() as connection:
        source = await OperationStore().get(
            connection, principal, workspace.provisioning_operation_id
        )
    if source is None or source.state != "succeeded":
        raise ProvisioningRefused("original workspace bootstrap has not completed")
    original = decode_payload(source.request_payload)
    if (
        payload_digest(original) != source.plan_digest
        or original.action != "provision"
        or original.parameters.get("lifecycle_phase") != "bootstrap-workspace"
    ):
        raise ProvisioningRefused("workspace lacks an immutable bootstrap operation")
    artifact = await read_artifact(
        composition.operation_connect,
        artifact_id=original.parameters.get("lifecycle_artifact_id"),
        org_id=str(org_id),
        workspace_id=str(workspace_id),
        require_fresh=False,
    )
    async with composition.operation_connect() as connection:
        producer = await OperationStore().get(
            connection, principal, artifact["source_operation_id"]
        )
    if (
        producer is None
        or producer.state != "succeeded"
        or original.parameters.get("lifecycle_source_operation_id")
        != producer.operation_id
        or producer.job_id != artifact["source_job_id"]
        or producer.attempt_id != artifact["source_attempt_id"]
        or producer.plan_digest != artifact["source_payload_digest"]
        or producer.request_payload != artifact["source_request_payload"]
    ):
        raise ProvisioningRefused(
            "bootstrap source artifact no longer matches its original admission"
        )
    inventory = await db.run_sync(
        lambda session: load_bootstrap_retirement_review(
            registration_store=SqlRegistrationStore(store=_ReviewStore(session)),
            workspace_id=str(workspace_id),
            org_id=str(org_id),
        )
    )
    plan = compose_retirement_plan(inventory)
    if (
        json.loads(original.parameters["lifecycle_request"]).get("mode")
        != "bring-existing-cluster"
        and inventory.cluster_ownership == "adopted"
    ):
        raise ProvisioningRefused(
            "canonical ownership differs from the original approved workspace mode"
        )
    if plan.cluster_rbac_remaining:
        raise ProvisioningUnavailable(
            "retirement requires independently provisioned exact-name cleanup authority for owned cluster RBAC"
        )
    if not plan.completes_teardown:
        raise ProvisioningUnavailable(
            "retirement needs complete ownership and a reviewed destroy plan for every managed resource; no deletion was submitted"
        )
    policy = policy_for(org_id)
    runtime = validate_runtime_config(policy.runtime)
    account = inventory.cluster_arn.split(":")[4]
    region = inventory.cluster_arn.split(":")[3]
    if (
        artifact["account_id"] != account
        or original.parameters.get("aws_account_id") != account
    ):
        raise ProvisioningRefused(
            "bootstrap ownership differs from its admitted provider account"
        )
    if (
        account not in policy.permitted_target_accounts
        or region not in policy.permitted_regions
        or "adopt" not in policy.permitted_modes
    ):
        raise ProvisioningRefused("retirement target is outside current policy")
    reference = policy.credential_references.get(account)
    if reference is None:
        raise ProvisioningUnavailable(
            "retirement provider credential reference is unavailable"
        )
    return workspace, principal, source, artifact, inventory, plan, policy, runtime


async def preview_retirement(composition, db, org_id, workspace_id, request_id):
    from workspace_provisioning.artifacts import digest
    from workspace_provisioning.lifecycle_policy import policy_digest

    (
        workspace,
        principal,
        source,
        artifact,
        inventory,
        plan,
        policy,
        runtime,
    ) = await retirement_facts(composition, db, org_id, workspace_id)
    account = inventory.cluster_arn.split(":")[4]
    region = inventory.cluster_arn.split(":")[3]
    # These are review facts, not an executable OperationRequest. The separate
    # cleanup-access recipe must exist before an approvable request can be built.
    # Bind runtime configuration by digest, never copy its full JSON into a
    # Harness parameter (which has a 2 KiB bound).
    review = {
        "request_id": str(request_id),
        "workspace_id": str(workspace_id),
        "source_operation_id": source.operation_id,
        "source_payload_digest": source.plan_digest,
        "lifecycle_artifact_id": artifact["artifact_id"],
        "account_id": account,
        "region": region,
        "inventory_sha256": digest(asdict(inventory)),
        "lifecycle_policy_sha256": policy_digest(policy.model_dump(mode="json")),
        "runtime_config_sha256": digest(runtime),
        "steps": [asdict(step) for step in plan.steps],
        "preserved": list(plan.preserved),
        "admission_available": False,
        "blocked_reason": "staged_cleanup_access_required",
        "approval_request": None,
    }
    review["revision"] = digest(review)
    return workspace, principal, None, review


async def admit_retirement(
    composition, db, org_id, workspace_id, request_id, revision, approval_id
):
    """Refuse until a separately governed cleanup-access artifact is implemented.

    Bootstrap access was revoked, and the original resource allocation may be
    sealed. A retirement cannot silently create new access in that allocation.
    """
    await _workspace(db, org_id, workspace_id)
    raise ProvisioningUnavailable(
        "retirement requires a separately approved cleanup-access operation and immutable grant artifact"
    )
