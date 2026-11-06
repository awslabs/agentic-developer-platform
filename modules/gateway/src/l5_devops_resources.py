"""Authentic workload references retained by scoped resource verification."""

import re
from datetime import UTC, datetime

from fastapi import HTTPException
from l5_services.devops.access_policy import PolicyUnavailable, access_policy_document
from l5_services.devops.resource_scope import policy_digest
from l5_services.devops.setup import original_setup
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from sqlalchemy import select


def stored_workload_refs(settings):
    """Authorize retained metadata under its connection, without claiming readiness.

    Only saved versions use this path. Saves and execution continue to require
    fresh resource and policy verification.
    """
    from l5_services.devops_configuration import _references

    from src.products.configuration import RecordRef

    selected = settings.get("devops") or {}
    connection = selected.get("hosting_connection_ref") or {}
    try:
        references = [ref for ref in _references(selected) if ref["record_type"] in {"resource", "telemetry", "policy"}]
        for reference in references:
            RecordRef.model_validate(reference)
            if not re.fullmatch(r"[a-f0-9]{64}", reference["record_revision"]) or any(
                reference[field] != connection.get(field) for field in ("tenant_id", "project_id", "source_connection_id")
            ):
                raise ValueError("retained resource scope changed")
    except (ValueError, KeyError, TypeError):
        raise HTTPException(404, detail={"code": "not_found"}) from None
    return references


def workload_refs(document):
    observed = document["resource_observation"]
    resource = observed["resource_selection"]
    if resource["workload_kind"] != "lambda" or observed.get("observations", {}).get("functions") != 1:
        raise PolicyUnavailable("resource_readback_unverified")
    if observed.get("association_readback") != {
        "associationId": observed.get("association_id"),
        "agentSpaceId": document["native_space_id"],
        "serviceId": "aws",
        "status": "valid",
        "configuration": {"aws": {"accountId": resource["account_id"], "accountType": "monitor", "assumableRoleArn": resource["role_arn"]}},
    }:
        raise PolicyUnavailable("association_readback_unverified")
    revision = policy_digest(observed)
    base = {**resource["connection_ref"], "record_revision": revision}
    return {
        "resource_refs": [{**base, "record_type": "resource", "record_id": resource["resource_arn"]}],
        "telemetry_refs": [{**base, "record_type": "telemetry", "record_id": resource["telemetry_arn"]}],
    }


async def resource_options(db, *, tenant_id, project_id, owner_id, settings, binding_id=None, admitted_at=None):
    """Only exact readbacks under this owner's unchanged Product environment."""
    from src.auth.l5_devops_resource_scope import current_resource_environment

    if not await current_resource_environment(db, tenant_id=tenant_id, project_id=project_id, settings=settings):
        return []
    selected = settings.get("devops") or {}
    rows = (
        await db.execute(
            select(Binding, Configuration)
            .join(Configuration, (Configuration.tenant_id == Binding.tenant_id) & (Configuration.configuration_id == Binding.configuration_id))
            .where(
                Binding.tenant_id == tenant_id,
                Binding.project_id == project_id,
                Binding.service_id == "devops",
                Configuration.owner_id == owner_id,
                Binding.binding_id == binding_id if binding_id is not None else True,
            )
        )
    ).all()
    result = []
    for binding, config in rows:
        if (
            binding.connection_ref != selected.get("hosting_connection_ref")
            or (binding.provider_identity or {}).get("region") != selected.get("region")
            or config.handoff_state != "linked"
        ):
            continue
        try:
            receipt = await original_setup(
                db, binding, configuration=config, settings=settings, require_setup_consent=binding.ownership == "customer_linked"
            )
        except (ValueError, TypeError, KeyError):
            continue
        original = await db.get(ConfigurationVersion, (tenant_id, config.configuration_id, receipt.configuration_revision))
        if original is None:
            continue

        # Resources can be added after their readback; environment authority cannot.
        def environments(value):
            return [
                (account.get("account_id"), [env.get("environment_ref") for env in account.get("environments", [])])
                for account in (value.get("devops") or {}).get("accounts", [])
            ]

        if environments(settings) != environments(original.settings):
            continue
        try:
            policy = access_policy_document(binding, receipt, now=datetime.now(UTC), admitted_at=admitted_at)
            refs = workload_refs(policy["document"])
        except PolicyUnavailable:
            continue
        resource = policy["document"]["resource_observation"]["resource_selection"]
        if (
            resource["connection_ref"] != {**binding.connection_ref, "record_type": "credential"}
            or resource["setup_role_arn"] != binding.provider_identity.get("role_arn")
            or resource["account_id"] != binding.provider_identity.get("account_id")
        ):
            continue
        result.append({"binding_id": binding.binding_id, "native_readiness": policy["native_readiness"], **refs})
    return result


async def require_workload_refs(db, *, tenant_id, project_id, owner_id, settings, binding_id=None, admitted_at=None):
    selected = settings.get("devops") or {}
    from l5_services.devops_configuration import _references

    requested = [ref for ref in _references(selected) if ref.get("record_type") in {"resource", "telemetry"}]
    if not requested:
        return []
    options = await resource_options(
        db, tenant_id=tenant_id, project_id=project_id, owner_id=owner_id, settings=settings, binding_id=binding_id, admitted_at=admitted_at
    )
    allowed = [ref for option in options for ref in option["resource_refs"] + option["telemetry_refs"]]
    if any(ref not in allowed for ref in requested):
        raise HTTPException(
            409,
            detail={
                "code": "resource_readback_changed",
                "message": "Verify the selected Lambda and log group again, then select the references returned by this binding.",
            },
        )
    return allowed


async def retained_investigation_admission(db, grant):
    """Only a bound child for an accepted, unchanged native job retains its pins.

    New work and owner templates still require fresh resource verification.
    Broker leases/deadlines and current consent remain independently enforced.
    """
    from l5_services.models.attempt import WorkerAttempt
    from l5_services.models.run import Run

    if not grant.parent_delegation_id or not grant.operation_id:
        return None
    run = await db.get(Run, (grant.tenant_id, grant.operation_id), populate_existing=True)
    scope = grant.execution_scope or {}
    if (
        run is None
        or run.service_id != "devops"
        or run.origin != "adp_submitted"
        or run.input.get("capability") != "investigation"
        or not run.native_job_ref
        or run.configuration_id != scope.get("configuration_id")
        or run.configuration_revision != scope.get("configuration_revision")
        or (run.product_id, run.project_id, run.binding_generation) != (grant.product_id, grant.project_id, grant.binding_generation)
        or run.admission.get("delegation_id") != grant.id
        or run.admission.get("delegation_version") != grant.revision
        or run.admission.get("actor_id") != grant.created_by
        or (scope.get("binding_id") is not None and scope["binding_id"] != run.binding_id)
    ):
        return None
    binding = await db.get(Binding, (grant.tenant_id, run.binding_id), populate_existing=True)
    if (
        binding is None
        or binding.state != "ready"
        or binding.gaps
        or binding.generation != run.binding_generation
        or binding.scope != run.admission.get("scope")
        or binding.native_resource_ref != run.admission.get("native_space_ref")
        or binding.provider_identity != run.admission.get("provider_identity")
        or binding.connection_ref != run.admission.get("connection_ref")
    ):
        return None
    attempt = await db.scalar(
        select(WorkerAttempt)
        .where(WorkerAttempt.tenant_id == grant.tenant_id, WorkerAttempt.run_id == run.run_id, WorkerAttempt.operation == "submit")
        .execution_options(populate_existing=True)
    )
    if attempt is None or attempt.status != "accepted" or attempt.native_job_ref != run.native_job_ref:
        return None
    try:
        admitted = datetime.fromisoformat(run.admission["admitted_at"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return admitted if admitted.tzinfo is not None and admitted <= datetime.now(UTC) else None
