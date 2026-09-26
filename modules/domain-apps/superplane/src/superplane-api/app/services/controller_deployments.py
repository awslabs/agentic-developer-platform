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
    BATCH_FIELDS,
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


def profiles_for(path, org_id, workspace_id):
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
        if not isinstance(profiles, dict) or len(profiles) > 128:
            raise ValueError("invalid or oversized workspace profile catalog")
        return tenant["adp_org_id"], profiles
    except ProvisioningRefused:
        raise
    except (OSError, KeyError, TypeError, ValueError, AttributeError):
        raise ProvisioningUnavailable(
            "controller deployment policy is unreadable"
        ) from None


def profile_for(path, org_id, workspace_id, profile_id):
    adp_org_id, profiles = profiles_for(path, org_id, workspace_id)
    if profile_id not in profiles:
        raise ProvisioningRefused(
            "controller deployment profile is not authorized for this workspace"
        )
    return adp_org_id, profiles[profile_id]


async def serving_catalog(
    request, db, org_id, workspace_id, *, workload_kind="serving"
):
    """Expose only profiles accepted by the maintained producer for this caller.

    Preview validates canonical workspace/account/credential and installed profile
    bindings without opening an operation, delivering credentials or contacting a
    provider. Discovery is configuration eligibility, not live workload health.
    """
    import uuid

    from app.config import settings
    from app.services.deployment_operations import composition
    from app.services.proxy import get_workspace_cluster
    from app.services.workspace_namespace import resolve_workspace_namespace

    workspace, _ = await get_workspace_cluster(workspace_id, org_id, db)
    resolve_workspace_namespace(workspace)
    result = {
        "workspace_id": str(workspace_id),
        "profiles": [],
        "can_submit": False,
        "can_review_teardown": False,
        "can_cancel": False,
        "can_observe": False,
        "can_read_accounting": False,
        "can_read_results": False,
    }
    try:
        read_principal = await GrantBackedAuthority(async_session_factory).resolve(
            org_id=str(org_id),
            workspace_id=str(workspace_id),
            permission="workspace:read",
        )
        result["can_observe"] = bool(
            read_principal
            and settings.controller_status_url
            and settings.controller_registry_credential
        )
        owner = composition(request)
        result["can_read_accounting"] = bool(read_principal)
        result["can_read_results"] = bool(read_principal and workload_kind == "batch")
        principal = await GrantBackedAuthority(async_session_factory).resolve(
            org_id=str(org_id),
            workspace_id=str(workspace_id),
            permission="workspace:provision",
        )
        if principal is None:
            return {**result, "reason": "not-permitted"}
        result["can_cancel"] = getattr(owner, "ledger", None) is not None
        if "workspace:spend" not in principal.permissions:
            return {**result, "reason": "not-permitted"}
        ready = owner.dispatcher is not None and await owner.dispatcher.ready(
            str(org_id)
        )
        result["can_review_teardown"] = bool(ready)
        _, profiles = profiles_for(
            settings.superplane_controller_profiles_file, org_id, workspace_id
        )
        for profile_id, profile in sorted(profiles.items()):
            if (
                not isinstance(profile, dict)
                or not isinstance(profile.get("model_options"), dict)
                or not isinstance(profile.get("workload"), dict)
            ):
                raise ProvisioningUnavailable(
                    "controller deployment policy is unreadable"
                )
            if profile.get("workload", {}).get("kind") != workload_kind:
                continue
            batch_options = (
                {key: profile["workload"].get(key) for key in BATCH_FIELDS}
                if workload_kind == "batch"
                else None
            )
            # This identity belongs only to a transient preview; no request is
            # registered and it can never be submitted as the user's operation.
            preview = await preview_controller_deployment(
                db,
                policy_path=settings.superplane_controller_profiles_file,
                org_id=org_id,
                workspace_id=workspace_id,
                request_id=uuid.uuid4(),
                profile_id=profile_id,
                name="profile-review",
                model_options=profile["model_options"],
                workload_kind=workload_kind,
                batch_options=batch_options,
            )
            parameters = preview.request.parameters
            plan = json.loads(parameters["controller_plan"])
            result["profiles"].append(
                {
                    "profile_id": profile_id,
                    **(
                        {"batch_options": batch_options}
                        if workload_kind == "batch"
                        else {"model_options": dict(profile["model_options"])}
                    ),
                    "image": plan["workload"]["image"],
                    "max_resource_units": int(parameters["max_resource_units"]),
                    "max_runtime_seconds": int(parameters["max_runtime_seconds"]),
                    "max_cost_micros": int(parameters["max_cost_micros"]),
                }
            )
        return {
            **result,
            "can_submit": bool(ready and result["profiles"]),
            "reason": None if ready and result["profiles"] else "unavailable",
        }
    except ProvisioningRefused:
        return {
            **result,
            "profiles": [],
            "can_submit": False,
            "reason": "not-permitted",
        }
    except ProvisioningUnavailable:
        return {
            **result,
            "profiles": [],
            "can_submit": False,
            "reason": "unavailable",
        }


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
    workload_kind="serving",
    batch_options=None,
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
            workload_kind=workload_kind,
            batch_options=batch_options,
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
    from app.operation_activation import require_admission_enabled

    require_admission_enabled()
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
            from superplane_executor.cleanup_binding import source_for, validate

            try:
                source = await source_for(
                    raw,
                    org_id=str(org_id),
                    workspace_id=str(workspace_id),
                    deployment_id=preview.deployment_id,
                    source_id=request.parameters["controller_source_operation_id"],
                )
            except OperationRefused:
                raise ProvisioningRefused(
                    "teardown requires the original settled deployment source or fenced cancelled source"
                ) from None
            if source["request_payload"] != intent["controller_request_payload"]:
                raise ProvisioningRefused(
                    "teardown changed its original deployment source"
                )
            await validate(raw, source, request, approval_id=approval_id)
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
        if request.action == "teardown":
            # Persist ownership independently of the caller's domain transaction.
            # Lost ledger replies/registration commits recover this same request.
            from superplane_executor.cleanup_binding import bind, source_for

            async with composition.operation_connect() as binding_connection:
                async with binding_connection.transaction():
                    fresh_source = await source_for(
                        binding_connection,
                        org_id=str(org_id),
                        workspace_id=str(workspace_id),
                        deployment_id=preview.deployment_id,
                        source_id=request.parameters["controller_source_operation_id"],
                    )
                    await bind(
                        binding_connection,
                        fresh_source,
                        request,
                        approval_id=approval_id,
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
