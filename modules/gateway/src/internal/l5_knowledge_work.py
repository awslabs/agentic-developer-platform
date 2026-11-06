"""Source-only permits for exact captured knowledge under current SetupWork."""

from datetime import UTC, datetime

from fastapi import HTTPException
from l5_services.devops.knowledge_work import current_context
from l5_services.models.setup import SetupIntent, SetupWork
from l5_services.workers.bindings import SetupClaim
from l5_services.workers.execution import AuthorityRevoked

from src.auth.l5_devops_knowledge_scope import current_knowledge_scope
from src.products.source_routes import service_visible_bindings, source_ref_authorized


async def verify_knowledge_source_work(db, grant, body):
    from src.internal.l5_credential_routes import L5WorkPermit

    denied = HTTPException(409, detail={"code": "knowledge_work_authority_changed"})
    if grant.id != body.delegation_id or grant.revision != body.delegation_revision or not await current_knowledge_scope(db, grant, admitted=True):
        raise denied
    intent = await db.get(SetupIntent, (grant.tenant_id, body.operation_id), populate_existing=True)
    work = await db.get(SetupWork, (grant.tenant_id, body.attempt_id), populate_existing=True)
    if (
        intent is None
        or work is None
        or work.lease_until is None
        or intent.authorization.get("delegation_id") != grant.id
        or intent.authorization.get("delegation_revision") != grant.revision
        or not body.lease_owner
        or type(body.lease_fence) is not int
    ):
        raise denied
    claim = SetupClaim(grant.tenant_id, body.operation_id, body.attempt_id, work.generation, body.lease_owner, body.lease_fence, work.lease_until)
    try:
        context = await current_context(db, intent, claim)
    except AuthorityRevoked as error:
        raise denied from error
    selected = body.connection_ref.model_dump()
    snapshots = context.admission["source_snapshots"]
    if selected not in [snapshot["source_ref"] for snapshot in snapshots]:
        # This permit cannot authorize a credential, arbitrary source or repin.
        raise denied
    bindings = await service_visible_bindings(db, grant)
    if any(not any(source_ref_authorized(grant, binding, snapshot["source_ref"]) for binding in bindings) for snapshot in snapshots):
        raise HTTPException(403, detail={"code": "knowledge_source_access_revoked"})
    return L5WorkPermit(
        grant.tenant_id,
        grant.product_id,
        grant.project_id,
        selected["source_connection_id"],
        body.operation_id,
        body.attempt_id,
        work.fence,
        work.generation,
        min(
            grant.expires_at.replace(tzinfo=grant.expires_at.tzinfo or UTC),
            work.lease_until.replace(tzinfo=work.lease_until.tzinfo or UTC),
            datetime.fromisoformat(context.admission["retain_until"]),
        ),
        frozenset(snapshot["snapshot_id"] for snapshot in snapshots),
        lease_owner=work.lease_owner,
    )


async def verify_knowledge_native_work(db, grant, body):
    import json

    from src.auth.l5_knowledge_native_consent import NATIVE_ACTIONS, current_native_grant
    from src.internal.l5_credential_routes import L5WorkPermit
    from src.internal.l5_devops_policy import space_arn

    intent = await db.get(SetupIntent, (grant.tenant_id, body.operation_id), populate_existing=True)
    source = await current_native_grant(db, grant, intent)
    if body.delegation_id != grant.id or body.delegation_revision != grant.revision or body.connection_ref.model_dump() != grant.sources[0]:
        raise HTTPException(403, detail={"code": "knowledge_native_consent_required"})
    snapshot = intent.authorization["knowledge_admission"]["source_snapshots"][0]
    from src.internal.l5_credential_routes import ConnectionRef

    source_body = body.model_copy(
        update={
            "delegation_id": source.id,
            "delegation_revision": source.revision,
            "connection_ref": ConnectionRef.model_validate(snapshot["source_ref"]),
        }
    )
    source_permit = await verify_knowledge_source_work(db, source, source_body)
    from l5_services.models.binding import Binding

    binding = await db.get(Binding, (grant.tenant_id, intent.request["binding_id"]), populate_existing=True)
    deadline = min(source_permit.expires_at, grant.expires_at.replace(tzinfo=grant.expires_at.tzinfo or UTC))
    policy = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["aidevops:" + name for name in NATIVE_ACTIONS],
                    "Resource": space_arn(binding),
                    "Condition": {
                        "DateLessThan": {"aws:CurrentTime": deadline.isoformat()},
                        "StringEquals": {"aws:RequestedRegion": binding.provider_identity["region"]},
                    },
                }
            ],
        },
        separators=(",", ":"),
    )
    verified = {
        "knowledge_native": True,
        "scope": grant.execution_scope,
        "intent_hash": intent.intent_hash,
        "binding_id": binding.binding_id,
        "binding_generation": binding.generation,
        "native_space_ref": binding.native_resource_ref,
        "provider_identity": binding.provider_identity,
        "source_delegation_id": source.id,
        "source_delegation_revision": source.revision,
    }
    return L5WorkPermit(
        grant.tenant_id,
        grant.product_id,
        grant.project_id,
        grant.credential_id,
        body.operation_id,
        body.attempt_id,
        source_permit.lease_fence,
        source_permit.binding_generation,
        deadline,
        session_policy=policy,
        lease_owner=source_permit.lease_owner,
        setup_verified=verified,
        provider_identity=binding.provider_identity,
    )


def native_active_grant(grant, body, permit, *, minimum_remaining=None):
    from src.auth.l5_knowledge_native_consent import NATIVE_PURPOSE

    now = datetime.now(UTC)
    verified = permit.setup_verified or {}
    if (
        verified.get("knowledge_native") is not True
        or (grant.execution_scope or {}).get("purpose") != NATIVE_PURPOSE
        or grant.id != body.delegation_id
        or grant.revision != body.delegation_revision
        or grant.parent_delegation_id != verified.get("source_delegation_id")
        or grant.operation_id != body.operation_id
        or grant.attempt_id is not None
        or grant.lease_fence is not None
        or grant.revoked_at is not None
        or grant.binding_generation != permit.binding_generation
        or not body.lease_owner
        or body.lease_owner != permit.lease_owner
        or body.lease_fence != permit.lease_fence
        or (permit.tenant_id, permit.product_id, permit.project_id, permit.source_connection_id, permit.operation_id, permit.attempt_id)
        != (grant.tenant_id, grant.product_id, grant.project_id, grant.credential_id, body.operation_id, body.attempt_id)
    ):
        raise HTTPException(409, detail={"code": "knowledge_work_authority_changed"})
    deadline = min(grant.expires_at.replace(tzinfo=grant.expires_at.tzinfo or UTC), permit.expires_at)
    if deadline <= now:
        raise HTTPException(403, detail={"code": "knowledge_native_consent_expired"})
    return deadline
