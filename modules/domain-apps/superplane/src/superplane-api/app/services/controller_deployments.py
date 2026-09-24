"""Governed workload preview/admission composition for the deployment owner.

The router owns namespace/quota reservation and the original deployment intent.
This service owns the paid request and registers it in that same domain
transaction. It has no Kubernetes mutation or credential-delivery fallback.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from harness_jobs.identity import OperationRefused, decode_payload, payload_digest
from sqlalchemy import select
from superplane_executor.deployment_plan import (
    build_deployment_preview,
    teardown_request,
    validate_request,
)
from superplane_executor.deployment_registry import register_deployment_operation

from app.adapters.operation_authority_source import GrantBackedAuthority
from app.database import async_session_factory
from app.models.cloud_account import CloudAccount
from app.models.cluster import Cluster
from app.models.organization import Organization
from app.models.workspace import Workspace
from app.services.provisioning import (
    ProvisioningRefused,
    ProvisioningUnavailable,
    get_operation_facade,
)


async def require_current_approval(*, org_id, workspace_id, request, approval_id):
    """Validate the shared approval policy before reserving model quota."""
    from harness_jobs.approval import evaluate_approval

    authority = GrantBackedAuthority(async_session_factory)
    principal = await authority.resolve(
        org_id=str(org_id),
        workspace_id=str(workspace_id),
        permission="workspace:provision",
    )
    if principal is None:
        raise ProvisioningRefused("controller deployment authority refused")
    approval = await authority.approval_for(principal=principal, request=request)
    if approval.record is None or approval.record.approval_id != str(approval_id):
        raise ProvisioningRefused("approval does not match this reviewed deployment")
    decision = evaluate_approval(
        approval.record,
        principal=principal,
        request=request,
        requested_envelope=approval.requested_envelope,
        approver_statuses=approval.approver_statuses,
        now=datetime.now(UTC),
    )
    if not decision.permitted:
        raise ProvisioningRefused(decision.reason)


def profile_for(path, org_id, workspace_id, profile_id):
    if not path:
        raise ProvisioningUnavailable(
            "controller deployment profiles are not configured"
        )
    try:
        with Path(path).open("rb") as source:
            raw = source.read(262145)
        if len(raw) > 262144:
            raise ValueError("oversize policy")
        document = json.loads(raw)
        if set(document) != {"version", "tenants"} or document["version"] != 1:
            raise ValueError("unsupported policy")
        tenant = document["tenants"].get(str(org_id))
        if tenant is None:
            raise ProvisioningRefused(
                "no controller profiles are authorized for this organization"
            )
        if set(tenant) != {"adp_org_id", "workspaces"}:
            raise ValueError("unsupported organization policy")
        profiles = tenant["workspaces"].get(str(workspace_id), {})
        if profile_id not in profiles:
            raise ProvisioningRefused(
                "controller deployment profile is not authorized for this workspace"
            )
        return tenant["adp_org_id"], profiles[profile_id]
    except ProvisioningRefused:
        raise
    except (OSError, KeyError, TypeError, ValueError, AttributeError):
        raise ProvisioningUnavailable(
            "controller deployment policy is unreadable"
        ) from None


async def preview_controller_deployment(
    db,
    *,
    policy_path,
    org_id,
    workspace_id,
    request_id,
    profile_id,
    name,
    model_options,
):
    principal = await GrantBackedAuthority(async_session_factory).resolve(
        org_id=str(org_id),
        workspace_id=str(workspace_id),
        permission="workspace:provision",
    )
    if principal is None:
        raise ProvisioningRefused("controller deployment authority refused")
    row = (
        await db.execute(
            select(Workspace, Cluster, CloudAccount, Organization)
            .join(
                Cluster,
                (Cluster.id == Workspace.cluster_id)
                & (Cluster.org_id == Workspace.org_id),
            )
            .join(
                CloudAccount,
                (CloudAccount.id == Workspace.aws_account_id)
                & (CloudAccount.org_id == Workspace.org_id),
            )
            .join(Organization, Organization.id == Workspace.org_id)
            .where(Workspace.id == workspace_id, Workspace.org_id == org_id)
        )
    ).one_or_none()
    if row is None:
        raise ProvisioningRefused(
            "registered controller deployment destination is unavailable"
        )
    workspace, cluster, account, organization = row
    if (
        workspace.status not in {"Ready", "active"}
        or cluster.status not in {"Ready", "Active"}
        or account.provider != "aws"
        or account.status != "Active"
    ):
        raise ProvisioningRefused("controller deployment destination is not active")
    adp_org_id, profile = profile_for(policy_path, org_id, workspace_id, profile_id)
    if organization.adp_org_id != adp_org_id:
        raise ProvisioningRefused("controller profile organization binding changed")
    reference = profile.get("credential_reference", {})
    if reference.get("credential_id") not in json.loads(
        account.adp_credential_ids_json or "[]"
    ):
        raise ProvisioningRefused(
            "controller credential is not registered for this account"
        )
    target = {
        "cluster_id": str(cluster.id),
        "cluster_arn": cluster.eks_cluster_arn,
        "endpoint": cluster.endpoint,
        "namespace": workspace.namespace_name,
        "provider_account_id": account.account_identifier,
    }
    try:
        return build_deployment_preview(
            org_id=str(org_id),
            workspace_id=str(workspace_id),
            request_id=str(request_id),
            profile_id=profile_id,
            profile=profile,
            target=target,
            name=name,
            model_options=model_options,
        )
    except OperationRefused as error:
        raise ProvisioningRefused(str(error)) from None


async def admit_controller_deployment(
    composition,
    db,
    *,
    org_id,
    workspace_id,
    preview,
    approval_id,
    revision,
):
    """Called with current preview and locked, quota-reserved deployment intent.

    No commit is performed here: immutable registration is committed atomically
    with the router's domain intent/tombstone. Shared admission is separately
    durable; replay recovers that exact paid identity after a lost domain commit.
    """
    request = preview.request
    if payload_digest(request) != revision:
        raise ProvisioningRefused("controller deployment preview revision changed")
    facade = get_operation_facade()
    if facade is None or composition.dispatcher is None:
        raise ProvisioningUnavailable(
            "governed controller deployment transport is unavailable"
        )
    principal = await GrantBackedAuthority(async_session_factory).resolve(
        org_id=str(org_id),
        workspace_id=str(workspace_id),
        permission="workspace:provision",
    )
    if principal is None:
        raise ProvisioningRefused("controller deployment authority refused")
    await db.flush()
    domain = await db.connection()
    raw = (await domain.get_raw_connection()).driver_connection
    locked = await raw.fetchrow(
        "SELECT w.namespace_name FROM workspaces w WHERE w.id::text=$1 AND w.org_id::text=$2 FOR UPDATE",
        str(workspace_id),
        str(org_id),
    )
    intent = await raw.fetchrow(
        "SELECT * FROM deployments WHERE id::text=$1 AND workspace_id::text=$2 AND org_id::text=$3 FOR UPDATE",
        preview.deployment_id,
        str(workspace_id),
        str(org_id),
    )
    if (
        locked is None
        or intent is None
        or intent["namespace"] != locked["namespace_name"]
        or intent["operation_request_json"]
        != json.dumps(preview.deployment_request, sort_keys=True, separators=(",", ":"))
        or intent["operation_target_json"]
        != json.dumps(preview.deployment_target, sort_keys=True, separators=(",", ":"))
    ):
        raise ProvisioningRefused(
            "controller deployment intent changed before admission"
        )
    # The workspace lock covers both registrations and independently committed
    # admissions. A competing stop UUID must not buy an orphan second operation
    # merely because the first caller lost its registration transaction.
    prior = await raw.fetchrow(
        "SELECT o.idempotency_key FROM harness_operations o "
        "WHERE o.org_id=$1 AND o.workspace_id=$2 AND o.action=$3 "
        "AND o.request_payload::jsonb->'parameters'->>'controller_deployment_id'=$4 "
        "AND o.idempotency_key<>$5 LIMIT 1",
        str(org_id),
        str(workspace_id),
        request.action,
        preview.deployment_id,
        request.idempotency_key,
    )
    if prior is not None:
        raise ProvisioningRefused(
            "this deployment action already has a paid request; recover its original operation ID"
        )
    try:
        validate_request(
            request,
            preview.deployment_target,
            org_id=str(org_id),
            workspace_id=str(workspace_id),
        )
        if request.action == "provision":
            if decode_payload(
                intent["controller_request_payload"]
            ) != request or intent["controller_approval_id"] != str(approval_id):
                raise ProvisioningRefused("original reviewed deployment intent changed")
        else:
            source = await raw.fetchrow(
                "SELECT o.request_payload FROM controller_deployment_operations r "
                "JOIN harness_operations o ON o.operation_id=r.operation_id AND o.plan_digest=r.plan_digest "
                "AND o.org_id=r.org_id AND o.workspace_id=r.workspace_id "
                "JOIN harness_operation_leases l ON l.operation_id=o.operation_id AND l.closed_at IS NOT NULL "
                "WHERE r.operation_id=$1 AND r.org_id=$2 AND r.workspace_id=$3 AND r.deployment_id=$4 "
                "AND r.action='provision' AND o.state IN ('succeeded','failed','cancelled')",
                request.parameters["controller_source_operation_id"],
                str(org_id),
                str(workspace_id),
                preview.deployment_id,
            )
            if (
                source is None
                or source["request_payload"] != intent["controller_request_payload"]
            ):
                raise ProvisioningRefused(
                    "teardown requires the original settled deployment source"
                )
            expected = teardown_request(
                decode_payload(source["request_payload"]),
                org_id=str(org_id),
                workspace_id=str(workspace_id),
                request_id=request.idempotency_key,
                source_operation_id=request.parameters[
                    "controller_source_operation_id"
                ],
            )
            if expected != request:
                raise ProvisioningRefused(
                    "teardown changed its original allocation or exact delete plan"
                )
    except (OperationRefused, KeyError, TypeError, ValueError) as error:
        raise ProvisioningRefused(
            "controller deployment plan or source is invalid"
        ) from error
    async with composition.operation_connect() as connection:
        existing = await connection.fetchrow(
            "SELECT o.*,a.approval_id FROM harness_operations o "
            "JOIN harness_approval_consumption a ON a.operation_id=o.operation_id "
            "AND a.org_id=o.org_id AND a.workspace_id=o.workspace_id AND a.plan_digest=o.plan_digest "
            "WHERE o.org_id=$1 AND o.workspace_id=$2 AND o.idempotency_key=$3",
            str(org_id),
            str(workspace_id),
            request.idempotency_key,
        )
    if existing is not None:
        if (
            existing["approval_id"] != str(approval_id)
            or existing["plan_digest"] != revision
            or decode_payload(existing["request_payload"]) != request
        ):
            raise ProvisioningRefused(
                "request identity already names another paid deployment"
            )
        operation_id, state = existing["operation_id"], existing["state"]
    else:
        approval = await GrantBackedAuthority(async_session_factory).approval_for(
            principal=principal,
            request=request,
        )
        if approval.record is None or approval.record.approval_id != str(approval_id):
            raise ProvisioningRefused(
                "approval does not match the exact controller deployment"
            )
        if not await composition.dispatcher.ready(str(org_id)):
            raise ProvisioningUnavailable(
                "governed controller deployment transport is not ready"
            )
        progress = await facade.open_operation(
            action=request.action,
            workspace_id=str(workspace_id),
            org_id=str(org_id),
            permission="workspace:provision",
            parameters={
                **request.parameters,
                "idempotency_key": request.idempotency_key,
            },
        )
        operation_id, state = progress.operation_id, progress.state
    await db.flush()
    connection = await db.connection()
    raw = await connection.get_raw_connection()
    try:
        await register_deployment_operation(
            raw.driver_connection,
            operation_id=operation_id,
            org_id=str(org_id),
            workspace_id=str(workspace_id),
            deployment_id=preview.deployment_id,
        )
    except OperationRefused as error:
        raise ProvisioningRefused(str(error)) from None
    return {
        "operation_id": operation_id,
        "state": state,
        "deployment_id": preview.deployment_id,
        "request_id": request.idempotency_key,
    }
