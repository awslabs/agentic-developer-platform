"""Fresh owner consent can read one retained investigation, never execute it again."""

from datetime import UTC, datetime

from l5_services.devops.recovery import recovery_deadline
from l5_services.models.run import Run

from src.shared.models.vault import CredentialServiceDelegation

PURPOSE = "devops_report_recovery"


def is_report_recovery(grant):
    return isinstance(grant.execution_scope, dict) and grant.execution_scope.get("purpose") == PURPOSE


async def original_authority(db, run):
    from src.auth.l5_execution_scope import current_execution_scope
    from src.l5_devops_resources import retained_investigation_admission

    if (
        run.service_id != "devops"
        or run.origin != "adp_submitted"
        or run.input.get("capability") != "investigation"
        or run.input.get("sources")
        or not run.native_job_ref
        or not isinstance(run.admission, dict)
        or run.admission.get("source_snapshots")
    ):
        return None
    original = await db.get(CredentialServiceDelegation, run.admission.get("delegation_id"), populate_existing=True)
    if (
        original is None
        or is_report_recovery(original)
        or not original.parent_delegation_id
        or original.revoked_at is not None
        or original.revision != run.admission.get("delegation_version")
        or not isinstance(original.execution_scope, dict)
        or original.execution_scope.get("purpose") is not None
    ):
        return None
    parent = await db.get(CredentialServiceDelegation, original.parent_delegation_id, populate_existing=True)
    fields = (
        "tenant_id",
        "product_id",
        "project_id",
        "service_id",
        "credential_id",
        "created_by",
        "binding_generation",
        "visibility",
        "execution_scope",
    )
    if (
        parent is None
        or parent.revoked_at is not None
        or parent.parent_delegation_id is not None
        or any(getattr(original, field) != getattr(parent, field) for field in fields)
        or not set(original.operations).issubset(parent.operations)
        or not set(original.capabilities).issubset(parent.capabilities)
        or any(source not in parent.sources for source in original.sources)
        or await retained_investigation_admission(db, original) is None
        or not await current_execution_scope(db, original, check_binding=True, binding_id=run.binding_id, binding_generation=run.binding_generation)
    ):
        return None
    return original


def recovery_scope(run, original):
    return {
        **original.execution_scope,
        "purpose": PURPOSE,
        "run_id": run.run_id,
        "original_delegation_id": original.id,
        "original_delegation_revision": original.revision,
        "native_job_ref": run.native_job_ref,
    }


async def current_report_recovery(db, grant):
    now = datetime.now(UTC)
    if (
        not is_report_recovery(grant)
        or grant.parent_delegation_id is not None
        or grant.revoked_at is not None
        or grant.expires_at.replace(tzinfo=grant.expires_at.tzinfo or UTC) <= now
        or grant.service_id != "l5-devops-worker"
        or grant.operation_id != grant.execution_scope.get("run_id")
        or grant.operations != ["product:read", "credential:session"]
        or grant.capabilities != ["investigation"]
    ):
        return False
    run = await db.get(Run, (grant.tenant_id, grant.operation_id), populate_existing=True)
    if run is None:
        return False
    until = recovery_deadline(run, "poll", now)
    authority = ((run.monitoring or {}).get("devops_recovery") or {}).get("authorization")
    if (
        until is None
        or grant.expires_at.replace(tzinfo=grant.expires_at.tzinfo or UTC) > until
        or authority != {"delegation_id": grant.id, "delegation_version": grant.revision}
    ):
        return False
    original = await original_authority(db, run)
    fields = ("tenant_id", "product_id", "project_id", "service_id", "credential_id", "created_by", "binding_generation", "visibility", "sources")
    return (
        original is not None
        and grant.execution_scope == recovery_scope(run, original)
        and all(getattr(grant, field) == getattr(original, field) for field in fields)
    )
