"""Fresh original operation identity and shared policy validation for lifecycle effects."""

import json
from pathlib import Path

from account_factory.modes import (
    OwnershipMode,
    ValidationAuthorization,
    ensure_valid,
    from_mapping,
)

from .artifacts import digest
from .lifecycle_policy import policy_document, policy_digest
from .preview import preview_workspace
from .runtime_config import LifecycleRefused, validate_runtime_config


async def current_operation(operation, context):
    current = await context.authority.resolve(operation.grant.lease.operation_id)
    fields = (
        "operation_id",
        "org_id",
        "workspace_id",
        "holder",
        "attempt_id",
        "fence_token",
    )
    if (
        any(
            getattr(current.grant.lease, key) != getattr(operation.grant.lease, key)
            for key in fields
        )
        or current.job_id != operation.job_id
        or current.plan_digest != operation.plan_digest
        or current.reservation_state != "confirmed"
    ):
        raise LifecycleRefused(
            "lifecycle execution identity changed or authority was withdrawn"
        )
    if (
        current.request.action == "provision"
        and "runtime_config_sha256" in current.request.parameters
    ):
        validated_request(current, context)
    if getattr(context, "policy_fixture", None) is not True:
        await context.authority.preflight(current)
    return current


def load_policy(context, org_id):
    path = getattr(context, "policy_file", None)
    if path:
        path = Path(path)
        if not path.is_absolute():
            raise LifecycleRefused(
                "lifecycle policy path must be deployment-owned and absolute"
            )
        with path.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise LifecycleRefused("lifecycle policy exceeds its bound")
        document = json.loads(raw)
        if document.get("version") != 1 or not isinstance(
            document.get("tenants"), dict
        ):
            raise LifecycleRefused("lifecycle policy document is invalid")
        policy = document["tenants"].get(org_id)
    elif getattr(context, "policy_fixture", None) is True:
        policy = context.policy
    else:
        raise LifecycleRefused("fresh deployment-owned lifecycle policy is required")
    if not isinstance(policy, dict):
        raise LifecycleRefused(
            "no lifecycle policy is configured for this organization"
        )
    return policy_document(policy)


def validated_request(operation, context):
    parameters = operation.request.parameters
    from .shared_membership import approved_membership

    approved_membership(
        parameters,
        org_id=operation.grant.lease.org_id,
        workspace_id=operation.grant.lease.workspace_id,
    )
    policy = load_policy(context, operation.grant.lease.org_id)
    if parameters["lifecycle_policy_sha256"] != policy_digest(policy):
        raise LifecycleRefused("approved lifecycle policy changed")
    config = validate_runtime_config(policy["runtime"])
    if digest(config) != parameters["runtime_config_sha256"]:
        raise LifecycleRefused("approved lifecycle runtime configuration changed")
    request = from_mapping(json.loads(parameters["lifecycle_request"]))
    public_request = json.loads(parameters["lifecycle_inputs"])
    modes = {
        "managed": OwnershipMode.EXISTING_ACCOUNT_MANAGED,
        "adopt": OwnershipMode.BRING_EXISTING_CLUSTER,
        "new-account-managed": OwnershipMode.NEW_ACCOUNT_MANAGED,
    }
    authorization = ValidationAuthorization(
        organization_id=policy["aws_organization_id"],
        operation_org_id=operation.grant.lease.org_id,
        management_account_id=policy["management_account_id"],
        management_cluster=policy["management_cluster"],
        permitted_modes=frozenset(modes[value] for value in policy["permitted_modes"]),
        workspace_id=operation.grant.lease.workspace_id,
        permitted_target_accounts=frozenset(policy["permitted_target_accounts"]),
        permitted_organizational_units=frozenset(
            policy["permitted_organizational_units"]
        ),
    )
    if ensure_valid(request, authorization):
        raise LifecycleRefused("lifecycle authorization is incomplete")
    if (
        request.region not in policy["permitted_regions"]
        or public_request["isolation_mode"] not in policy["isolation_modes"]
    ):
        raise LifecycleRefused("lifecycle region or isolation policy changed")
    preview = preview_workspace(
        request,
        authorization=authorization,
        requested_capacity={
            "max_resource_units": int(
                parameters["lifecycle_allocation_max_resource_units"]
            ),
            "max_runtime_seconds": int(
                parameters["lifecycle_allocation_max_runtime_seconds"]
            ),
            "max_cost_micros": int(parameters["lifecycle_allocation_max_cost_micros"]),
            "request": public_request,
            "allocation_id": parameters["allocation_id"],
            "policy_revision": policy_digest(policy),
        },
        cost_estimate=None,
        approval_required=True,
    )
    if preview.revision != parameters["plan_revision"]:
        raise LifecycleRefused("approved workspace preview revision changed")
    return config, request, authorization
