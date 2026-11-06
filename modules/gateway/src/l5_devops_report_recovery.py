"""An explicit owner action grants fifteen minutes of readback for one existing task."""

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException
from l5_services.admission import reconcile_and_dispatch
from l5_services.devops.recovery import RECOVERY_SECONDS

from src.auth.aws_connection_authority import owned_aws_connection, verified_connection_evidence
from src.auth.l5_devops_report_scope import original_authority, recovery_scope
from src.auth.l5_owner_delegation import _owner
from src.products.l5_devops_recovery import require_actor
from src.shared.models.audit import AuditLog
from src.shared.models.vault import CredentialServiceDelegation


async def recover_report(backend, product_id, project_id, run_id, request, key):
    require_actor(backend.actor)
    configuration, _ = await _owner(backend, product_id, project_id, "devops")
    store = await backend.run_store(product_id, project_id, "devops")
    run = await store.owned(run_id)
    payload = {"run_id": run_id, **request}
    digest = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    previous = await store.replay("report_recovery", key, digest)
    if previous is not None:
        return previous
    if (
        run.configuration_id != configuration.configuration_id
        or run.configuration_revision != configuration.current_revision
        or run.native_job_ref != request["native_job_ref"]
        or run.revision != request["expected_revision"]
    ):
        raise HTTPException(412, detail={"code": "report_recovery_changed", "message": "Refresh the original run before collecting its report."})
    now = datetime.now(UTC)
    if (
        run.state not in {"expired", "outcome_unknown", "running", "collecting"}
        or run.deadline_at.replace(tzinfo=run.deadline_at.tzinfo or UTC) > now
    ):
        raise HTTPException(
            409, detail={"code": "report_recovery_not_due", "message": "Use the existing run's normal progress while its execution window is active."}
        )
    original = await original_authority(backend.db, run)
    if original is None or original.created_by != backend.actor.user_id:
        raise HTTPException(
            403,
            detail={
                "code": "report_recovery_scope_changed",
                "message": "The original owner, connection, configuration or accepted task changed. "
                "Review this run's retained setup before collecting its report.",
            },
        )
    credential = await owned_aws_connection(backend.db, original.credential_id, backend.actor.user_id, backend.actor.org_id, lock=True)
    verified_connection_evidence(credential)
    grant_id = str(uuid5(NAMESPACE_URL, f"adp:devops-report:{run.tenant_id}:{run.run_id}:{backend.actor.user_id}:{key}"))
    expires = min(now + timedelta(seconds=RECOVERY_SECONDS), backend.actor.expires_at)
    if expires <= now + timedelta(minutes=1):
        raise HTTPException(403, detail={"code": "session_expiring", "message": "Refresh your ADP session, then collect this existing report."})
    fields = ("tenant_id", "product_id", "project_id", "service_id", "credential_id", "created_by", "binding_generation", "visibility", "sources")
    grant = CredentialServiceDelegation(
        id=grant_id,
        **{field: getattr(original, field) for field in fields},
        execution_scope=recovery_scope(run, original),
        operation_id=run.run_id,
        operations=["product:read", "credential:session"],
        capabilities=["investigation"],
        expires_at=expires,
        revision=1,
    )
    backend.db.add(grant)
    backend.db.add(
        AuditLog(
            org_id=run.tenant_id,
            actor_id=backend.actor.user_id,
            event_type="l5_report_recovery_authorized",
            details={
                "run_id": run.run_id,
                "delegation_id": grant.id,
                "original_delegation_id": original.id,
                "native_job_ref": run.native_job_ref,
                "expires_at": expires.isoformat(),
                "request_hash": digest,
            },
        )
    )
    return await reconcile_and_dispatch(
        store,
        backend.sessions,
        backend.publish,
        run.run_id,
        request["reason"],
        key,
        now=now,
        recovery_authorization={"delegation_id": grant.id, "delegation_version": grant.revision},
        recovery_request=request,
    )
