"""Canonical workspace preview shared by approval and admission."""

import json
import uuid
import hashlib
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from workspace_provisioning.lifecycle_policy import LifecyclePolicy, policy_digest
from sqlalchemy import or_, select

from app.adapters.operation_authority_source import (
    GrantBackedAuthority,
    acting_principal,
)
from app.config import settings
from app.database import async_session_factory
from app.models.cloud_account import CloudAccount
from app.models.organization import Organization
from app.schemas.workspace import CreateWorkspaceRequest
from app.services.provisioning import ProvisioningRefused, ProvisioningUnavailable

RESOURCE_NAMESPACE = uuid.UUID("0831ad7c-9dba-5dc3-b75d-eabf596c9975")


def normalized_request(body):
    if body.isolation_mode != "research":
        return body
    values = {}
    if body.budget_max_daily_usd is None:
        values["budget_max_daily_usd"] = Decimal("100.00")
    if body.budget_max_gpus is None:
        values["budget_max_gpus"] = 8
    return body.model_copy(update=values)


def policy_for(org_id):
    path = settings.superplane_lifecycle_config_file
    if not path:
        raise ProvisioningUnavailable(
            "workspace lifecycle target policy is not configured"
        )
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 65536:
            raise ValueError()
        value = json.loads(raw)
        if value["version"] != 1 or not isinstance(value["tenants"], dict):
            raise ValueError()
        entry = value["tenants"].get(str(org_id))
        if entry is None:
            raise ProvisioningRefused(
                "no workspace targets are authorized for this organization"
            )
        return LifecyclePolicy.model_validate(entry)
    except ProvisioningRefused:
        raise
    except Exception:
        raise ProvisioningUnavailable(
            "workspace lifecycle target policy is unreadable"
        ) from None


def workspace_id_for(org_id, request_id):
    return uuid.uuid5(RESOURCE_NAMESPACE, f"{org_id}/{request_id}")


def request_document(body: CreateWorkspaceRequest):
    # An approval reference is added after preview. It does not alter the work.
    return body.model_dump(
        mode="json", exclude={"operation_id", "approval_id", "plan_revision"}
    )


async def preview(db, org_id, body: CreateWorkspaceRequest):
    from account_factory.modes import (
        OwnershipMode,
        ValidationAuthorization,
        from_mapping,
    )
    from workspace_provisioning.preview import preview_workspace
    from workspace_provisioning.artifacts import initial_execution_steps
    from workspace_provisioning.runtime_config import (
        supported_runtime_modes,
        validate_runtime_config,
    )

    body = normalized_request(body)
    if body.cluster_placement == "shared":
        # Issue #6048: the schema, `cluster_sharing.py`'s eligibility resolver and
        # the canonical bootstrap registration path all support shared placement,
        # but the execution-step wiring that would carry a resolved shared target
        # through account-factory's lifecycle request and into
        # `initial_execution_steps` does not exist yet. Refusing explicitly here
        # is the fail-closed choice: silently falling through to the dedicated
        # resolution below would accept a shared-placement request and hand back
        # a plan for a dedicated cluster the caller never asked for.
        raise ProvisioningUnavailable(
            "shared cluster placement is not yet executable through workspace "
            "creation; the eligibility and bootstrap paths exist, but preview's "
            "execution-step generation does not resolve a shared target yet"
        )
    caller = acting_principal()
    if caller is None or caller.org_id != str(org_id):
        raise ProvisioningRefused("verified operation principal is required")
    workspace_id = workspace_id_for(org_id, body.operation_id)
    principal = await GrantBackedAuthority(async_session_factory).resolve(
        org_id=str(org_id),
        workspace_id=str(workspace_id),
        permission="workspace:provision",
    )
    if principal is None:
        raise ProvisioningRefused("workspace provisioning authority refused")
    policy = policy_for(org_id)
    runtime = validate_runtime_config(policy.runtime)
    if body.mode not in supported_runtime_modes(runtime):
        raise ProvisioningUnavailable(
            "this workspace mode is not executable with the current runtime configuration"
        )
    organization = await db.scalar(
        select(Organization).where(Organization.id == org_id)
    )
    if organization is None or organization.adp_org_id != policy.adp_org_id:
        raise ProvisioningUnavailable(
            "lifecycle organization binding disagrees with installed state"
        )
    if (
        body.mode not in policy.permitted_modes
        or body.isolation_mode not in policy.isolation_modes
    ):
        raise ProvisioningRefused("workspace mode is outside current target policy")
    if not body.region or body.region not in policy.permitted_regions:
        raise ProvisioningRefused("an authorized target region is required")
    mode = {
        "managed": OwnershipMode.EXISTING_ACCOUNT_MANAGED,
        "adopt": OwnershipMode.BRING_EXISTING_CLUSTER,
        "new-account-managed": OwnershipMode.NEW_ACCOUNT_MANAGED,
    }[body.mode]
    account = None
    account_record = None
    if mode is not OwnershipMode.NEW_ACCOUNT_MANAGED:
        matches = (
            await db.scalars(
                select(CloudAccount).where(
                    CloudAccount.org_id == org_id,
                    CloudAccount.provider == "aws",
                    CloudAccount.status == "Active",
                    or_(
                        CloudAccount.account_identifier == body.account,
                        CloudAccount.friendly_name == body.account,
                    ),
                )
            )
        ).all()
        if len(matches) != 1:
            raise ProvisioningRefused(
                "select one registered AWS account by exact name or account ID"
            )
        account_record = matches[0]
        account = account_record.account_identifier
        if account not in policy.permitted_target_accounts:
            raise ProvisioningRefused(
                "account is outside current lifecycle target policy"
            )
    elif body.account:
        raise ProvisioningRefused("new-account mode cannot name an existing account")
    fields = dict(policy.workspace_defaults) if mode.creates_cluster else {}
    fields.update(
        mode=mode.value,
        organization_id=policy.aws_organization_id,
        management_account_id=policy.management_account_id,
        management_cluster=policy.management_cluster,
        workspace_id=str(workspace_id),
        region=body.region,
        target_account_id=account,
        account_email=body.account_email,
        organizational_unit_id=body.organizational_unit_id,
        existing_cluster_name=body.cluster_reference,
    )
    lifecycle_request = from_mapping(fields)
    authorization = ValidationAuthorization(
        operation_org_id=str(org_id),
        organization_id=policy.aws_organization_id,
        management_account_id=policy.management_account_id,
        management_cluster=policy.management_cluster,
        workspace_id=principal.workspace_id,
        permitted_modes=frozenset(
            {
                {
                    "managed": OwnershipMode.EXISTING_ACCOUNT_MANAGED,
                    "adopt": OwnershipMode.BRING_EXISTING_CLUSTER,
                    "new-account-managed": OwnershipMode.NEW_ACCOUNT_MANAGED,
                }[value]
                for value in policy.permitted_modes
            }
        ),
        permitted_target_accounts=policy.permitted_target_accounts,
        permitted_organizational_units=policy.permitted_organizational_units,
    )
    document = request_document(body)
    capacity = {
        "max_resource_units": body.budget_max_gpus or 0,
        "max_runtime_seconds": policy.operation_max_runtime_seconds,
        "max_cost_micros": int(Decimal(body.budget_max_daily_usd or 0) * 1_000_000),
        "request": document,
        "allocation_id": str(
            uuid.uuid5(RESOURCE_NAMESPACE, f"allocation/{org_id}/{workspace_id}")
        ),
    }
    credential_account = account or policy.management_account_id
    reference = policy.credential_references.get(credential_account)
    if reference is None:
        raise ProvisioningUnavailable(
            "target lifecycle credential reference is not configured"
        )
    if account_record is not None and reference.credential_id not in json.loads(
        account_record.adp_credential_ids_json or "[]"
    ):
        raise ProvisioningRefused(
            "lifecycle credential is not registered for the selected account"
        )
    # Policy and runtime configuration changes invalidate the reviewed revision.
    capacity["policy_revision"] = policy_digest(policy)
    plan = preview_workspace(
        lifecycle_request,
        authorization=authorization,
        requested_capacity=capacity,
        cost_estimate=None,
        approval_required=True,
    )
    parameters = {
        "workspace_name": body.name,
        "isolation_mode": body.isolation_mode,
        "plan_revision": plan.revision,
        "lifecycle_policy_sha256": capacity["policy_revision"],
        "lifecycle_request": json.dumps(
            asdict(lifecycle_request), sort_keys=True, separators=(",", ":")
        ),
        "lifecycle_inputs": json.dumps(document, sort_keys=True, separators=(",", ":")),
        "runtime_config_sha256": hashlib.sha256(
            json.dumps(
                runtime, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest(),
        **reference.model_dump(),
        "provider": "aws",
        "provider_account_id": credential_account,
        "aws_account_id": credential_account,
        "allocation_id": capacity["allocation_id"],
        **{
            key: str(capacity[key])
            for key in ("max_resource_units", "max_runtime_seconds", "max_cost_micros")
        },
    }
    for name in ("max_resource_units", "max_runtime_seconds", "max_cost_micros"):
        parameters["lifecycle_allocation_" + name] = parameters[name]
    # Initial phases prepare or bootstrap control objects; only a separately
    # approved apply phase receives the requested infrastructure spend envelope.
    parameters["max_resource_units"] = "0"
    parameters["max_cost_micros"] = "0"
    parameters["execution_steps"] = initial_execution_steps(parameters)
    return {
        **plan.as_dict(),
        "mode": body.mode,
        "ownership_mode": mode.value,
        "request_id": str(body.operation_id),
        "workspace_id": str(workspace_id),
        "cloud_account_id": str(account_record.id)
        if account_record is not None
        else None,
        "approval_request": {
            "workspace_id": str(workspace_id),
            "action": "provision",
            "idempotency_key": str(body.operation_id),
            "parameters": parameters,
        },
    }
