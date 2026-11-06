"""Issue a work-scoped L5 grant from the owner's current Product and vault rights."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException
from l5_services.models.event import EventReceipt, semantic_key
from l5_services.models.preparation import AdmissionPreparation
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from src.auth.l5_execution_scope import require_execution_scope
from src.auth.l5_transform_branch_scope import OCCURRENCE, current_occurrence
from src.auth.vault_service import _visible_credential_filter
from src.l5_service_identity import worker_id
from src.products.models import Product
from src.products.source_routes import service_visible_bindings, source_ref_authorized
from src.shared.identity.product_scope import owner_scope, product_member
from src.shared.models.vault import CredentialServiceDelegation, UserCredential


def required_l5_work_operations(service_id, sources, capability=None):
    if not sources and (service_id == "finops" or (service_id == "devops" and capability in {"investigation", "release_verification"})):
        return ["product:read", "credential:session"]
    return ["product:read", "source:pin", "source:read", "credential:session"]


async def issue_l5_work_delegation(
    db,
    actor,
    template,
    credential,
    binding,
    connection,
    sources,
    capability,
    deadline_seconds,
    operation,
    idempotency_key,
    previous_id=None,
    *,
    source_occurrence=None,
):
    from src.auth.l5_devops_report_scope import is_report_recovery

    denied = HTTPException(403, detail={"code": "delegation_denied"})
    if is_report_recovery(template):
        raise denied
    if template.execution_scope is not None:
        await require_execution_scope(db, template, check_binding=True, binding_id=binding.binding_id, binding_generation=binding.generation)
        if binding.configuration_id != template.execution_scope["configuration_id"]:
            raise denied
    member = await product_member(db, user_id=actor.user_id, org_id=actor.org_id)
    visible = _visible_credential_filter(actor)
    if (
        member is None
        or template.created_by != member.id
        or not await db.scalar(select(Product.id).where(Product.id == template.product_id, owner_scope(member, template.tenant_id)))
        or not visible
        or not await db.scalar(
            select(UserCredential.id).where(UserCredential.id == credential.id, UserCredential.org_id == actor.org_id, or_(*visible))
        )
        or template.service_id != worker_id(binding.service_id)
        or template.parent_delegation_id is not None
        or template.revoked_at is not None
        or template.credential_id != credential.id
        or template.binding_generation != binding.generation
        or template.tenant_id != actor.org_id
        or template.project_id != binding.project_id
        or not isinstance(deadline_seconds, int)
        or not 60 <= deadline_seconds <= 86400
        or capability not in template.capabilities
        or operation not in {"submit", "retry"}
        or not idempotency_key
        or not set(required_l5_work_operations(binding.service_id, sources, capability)).issubset(template.operations)
    ):
        raise denied
    session_ref = {**connection, "record_type": "credential"}
    if session_ref not in template.sources:
        raise denied
    execution_scope = deepcopy(template.execution_scope)
    source_authority = template
    if source_occurrence is not None:
        if not isinstance(execution_scope, dict) or OCCURRENCE in execution_scope:
            raise denied
        execution_scope[OCCURRENCE] = deepcopy(source_occurrence)
        source_authority = SimpleNamespace(
            **{
                field: getattr(template, field)
                for field in (
                    "tenant_id",
                    "product_id",
                    "project_id",
                    "service_id",
                    "credential_id",
                    "created_by",
                    "binding_generation",
                    "visibility",
                    "operations",
                    "capabilities",
                    "installation_id",
                )
            },
            parent_delegation_id=template.id,
            execution_scope=execution_scope,
            sources=[session_ref, *sources],
        )
        if not await current_occurrence(db, source_authority) or source_occurrence.get("idempotency_key") != idempotency_key:
            raise denied
    visible = await service_visible_bindings(db, source_authority)
    if any(not any(source_ref_authorized(source_authority, item, source) for item in visible) for source in sources):
        raise denied
    expiry = template.expires_at.replace(tzinfo=UTC) if template.expires_at.tzinfo is None else template.expires_at
    identity = (template.tenant_id, template.product_id, template.project_id, actor.user_id, operation, idempotency_key)
    grant_id = previous_id or str(uuid5(NAMESPACE_URL, ":".join(identity if binding.service_id == "security" else (*identity, template.service_id))))
    existing = await db.get(CredentialServiceDelegation, grant_id, populate_existing=True)
    required_until = datetime.now(UTC) + timedelta(seconds=deadline_seconds)
    if existing is not None and binding.service_id == "security" and capability == "code_review":
        preparation = await db.get(
            AdmissionPreparation,
            (
                template.tenant_id,
                template.product_id,
                template.project_id,
                "security",
                actor.user_id,
                operation,
                idempotency_key,
            ),
            populate_existing=True,
        )
        if preparation is not None:
            frozen = preparation.verified
            deadline = preparation.deadline_at.replace(tzinfo=preparation.deadline_at.tzinfo or UTC)
            if (
                frozen.get("delegation_id") != existing.id
                or frozen.get("delegation_version") != existing.revision
                or frozen.get("binding_generation") != binding.generation
                or frozen.get("limits", {}).get("deadline_seconds") != deadline_seconds
                or frozen.get("trigger", {}).get("source_refs") != sources
                or deadline <= datetime.now(UTC)
                or deadline > existing.expires_at.replace(tzinfo=existing.expires_at.tzinfo or UTC)
            ):
                raise denied
            required_until = deadline
    if expiry <= required_until:
        raise denied
    if existing is not None:
        if (
            existing.parent_delegation_id != template.id
            or existing.execution_scope != execution_scope
            or existing.service_id != template.service_id
            or existing.revoked_at is not None
            or existing.credential_id != credential.id
            or existing.sources != [session_ref, *sources]
            or existing.capabilities != [capability]
            or existing.binding_generation != binding.generation
            or _expired(existing.expires_at)
        ):
            raise denied
        return existing
    if previous_id is not None:
        raise denied
    grant = CredentialServiceDelegation(
        id=grant_id,
        tenant_id=actor.org_id,
        credential_id=credential.id,
        service_id=template.service_id,
        product_id=template.product_id,
        project_id=template.project_id,
        created_by=member.id,
        operations=required_l5_work_operations(binding.service_id, sources, capability),
        sources=[session_ref, *sources],
        capabilities=[capability],
        visibility=template.visibility,
        revision=1,
        binding_generation=binding.generation,
        parent_delegation_id=template.id,
        execution_scope=execution_scope,
        expires_at=min(expiry, datetime.now(UTC) + timedelta(days=1)),
    )
    try:
        async with db.begin_nested():
            db.add(grant)
            await db.flush()
    except IntegrityError:
        existing = await db.get(CredentialServiceDelegation, grant_id, populate_existing=True)
        if (
            existing is None
            or existing.parent_delegation_id != template.id
            or existing.execution_scope != execution_scope
            or existing.sources != [session_ref, *sources]
            or existing.capabilities != [capability]
            or existing.revoked_at is not None
            or _expired(existing.expires_at)
        ):
            raise denied from None
        return existing
    return grant


def _expired(value):
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value) <= datetime.now(UTC)


async def issue_l5_incident_delegation(db, receipt, template, credential, binding, connection, deadline_seconds):
    """Derive work from existing owner consent for a durable signed incident.

    The principal below is a server-resolved authorization subject, not a login
    token or a claim that the human submitted this event. The event source stays
    on the run; source-supplied identity never supplies execution authority.
    """
    stored = await db.get(
        EventReceipt,
        (receipt.tenant_id, receipt.product_id, receipt.project_id, receipt.service_id, receipt.trigger_id, receipt.event_id),
        populate_existing=True,
    )
    if (
        stored is None
        or template is None
        or binding is None
        or credential is None
        or stored.state not in {"pending", "admitted"}
        or stored.event_kind != "native_event"
        or stored.service_id != "devops"
        or stored.configuration_id != binding.configuration_id
        or (stored.run_input or {}).get("binding_id") != binding.binding_id
        or (stored.run_input or {}).get("capability") != "investigation"
        or (stored.tenant_id, stored.product_id, stored.project_id) != (template.tenant_id, template.product_id, template.project_id)
        or template.service_id != worker_id("devops")
    ):
        raise HTTPException(403, detail={"code": "delegation_denied"})
    owner_subject = SimpleNamespace(org_id=template.tenant_id, user_id=template.created_by, team_id="", is_admin=False)
    return await issue_l5_work_delegation(
        db,
        owner_subject,
        template,
        credential,
        binding,
        connection,
        [],
        "investigation",
        deadline_seconds,
        "submit",
        "devops-incident:" + semantic_key(stored),
    )
