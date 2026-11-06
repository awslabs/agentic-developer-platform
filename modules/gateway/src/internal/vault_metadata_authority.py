"""Current caller authority for internal credential metadata operations."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException, Request
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.auth.agent_registry import get_agent_registry_service, parse_assumed_role_arn
from src.internal.auth_deps import INTERNAL_PLANE_SCOPES, VerifiedInternalIdentity
from src.internal.service_authorization import require_service_operation, service_tenant

logger = logging.getLogger(__name__)

CAPABILITIES = {
    "evidence": "credential:evidence:read",
    "revocation": "credential:revocation:read",
}


@dataclass(frozen=True)
class VaultMetadataAuthority:
    tenant_id: str
    principal: str
    acting_principal: str
    workspace_id: str
    domain_org_id: str | None = None
    owner_principal: str | None = None
    operation_binding: object | None = None
    authority_kind: Literal["tenant_service", "domain_producer", "agent_run"] = "tenant_service"


def _refuse(action: str, reason: str) -> None:
    logger.info("Vault metadata denied: action=%s reason=%s", action, reason)
    raise HTTPException(403, "vault metadata authority refused")


async def authorize_vault_metadata(
    request: Request,
    db: AsyncSession,
    *,
    action: Literal["evidence", "revocation"],
    requested_tenant_id: str,
    requested_workspace_id: str,
    requested_principal: str | None = None,
    operation_selector: object | None = None,
) -> VaultMetadataAuthority:
    """Reject identity assertions before a credential lookup can observe existence."""
    identity = getattr(request.state, "internal_identity", None)
    if not isinstance(identity, VerifiedInternalIdentity) or identity.kind == "tools_assertion":
        _refuse(action, "no_verified_caller")
    if not identity.tenant_id or not identity.service_id or not requested_workspace_id:
        _refuse(action, "unbound_caller")
    context = getattr(request.state, "token_context", None)
    role = parse_assumed_role_arn(request.headers.get("X-Caller-Identity", ""))
    if not role or getattr(context, "agent_registry_id", None) != identity.service_id or getattr(context, "org_id", None) != identity.tenant_id:
        _refuse(action, "registry_identity_mismatch")

    try:
        current = await run_in_threadpool(get_agent_registry_service().get_current_agent, identity.service_id, role)
    except (ClientError, BotoCoreError):
        raise HTTPException(503, "vault metadata authority unavailable") from None
    if current is None or current.get("scope") not in INTERNAL_PLANE_SCOPES or current.get("org_id") != identity.tenant_id:
        _refuse(action, "registry_grant_removed")
    scopes = set(current.get("credential_scopes") or ())

    if identity.kind in {"agent_run", "domain_executor"}:
        return await _authorize_operation_metadata(
            request,
            db,
            identity,
            current,
            scopes,
            action,
            requested_tenant_id,
            requested_workspace_id,
            operation_selector,
            requested_principal,
        )

    if action == "evidence" and scopes == {"domain:operation-producer"}:
        from src.internal.domain_current_identity import current_human_identity
        from src.internal.domain_operation_runtime import PRODUCER_SCOPE, current_registry
        from src.internal.domain_operation_store import bindings, domain_session
        from src.shared.identity.workspaces import workspace_user

        if not requested_principal or current_registry(request, PRODUCER_SCOPE) != identity.service_id:
            _refuse(action, "producer_not_current")
        matches = [
            binding
            for binding in bindings()
            if binding.domain == "superplane"
            and binding.org_id == requested_tenant_id
            and binding.adp_org_id == identity.tenant_id
            and binding.producer_registry_id == identity.service_id
        ]
        if len(matches) != 1 or current.get("domain_org_id") != requested_tenant_id:
            _refuse(action, "domain_binding_missing")
        binding = matches[0]
        human = await current_human_identity(db, subject=requested_principal, adp_org_id=binding.adp_org_id)
        if (
            not isinstance(human, dict)
            or human.get("subject") != requested_principal
            or human.get("adp_org_id") != binding.adp_org_id
            or human.get("principal_type") != "human"
            or human.get("enabled") is not True
            or human.get("active") is not True
            or not human.get("membership_id")
        ):
            _refuse(action, "human_identity_mismatch")
        try:
            async with domain_session(binding) as session:
                owned = await session.scalar(
                    text("SELECT 1 FROM workspaces WHERE id::text=:workspace AND org_id::text=:org"),
                    {"workspace": requested_workspace_id, "org": binding.org_id},
                )
                grants = await session.execute(
                    text(
                        "SELECT permissions, revoked_at FROM workspace_grants "
                        "WHERE workspace_id::text=:workspace AND org_id::text=:org "
                        "AND principal=:principal AND principal_type='human'"
                    ),
                    {"workspace": requested_workspace_id, "org": binding.org_id, "principal": requested_principal},
                )
                rows = grants.all()
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "vault metadata authority unavailable") from None
        if not owned or len(rows) != 1 or rows[0].revoked_at is not None or "workspace:renew_credential" not in rows[0].permissions.split():
            _refuse(action, "human_workspace_grant_missing")
        try:
            user = await workspace_user(db, requested_principal, binding.adp_org_id)
            if user is not None:
                from src.shared.models.onboarding import TenantMembership

                membership = await db.scalar(
                    select(TenantMembership)
                    .where(
                        TenantMembership.id == human["membership_id"],
                        TenantMembership.user_id == user.id,
                        TenantMembership.tenant_id == binding.adp_org_id,
                        TenantMembership.revoked_at.is_(None),
                    )
                    .execution_options(populate_existing=True)
                )
            else:
                membership = None
        except ValueError:
            _refuse(action, "human_owner_missing")
        except SQLAlchemyError:
            raise HTTPException(503, "vault metadata authority unavailable") from None
        if user is None or membership is None or user.is_shadow or user.user_kind != "human":
            _refuse(action, "human_owner_missing")
        return VaultMetadataAuthority(
            binding.adp_org_id,
            f"service:{identity.service_id}",
            requested_principal,
            requested_workspace_id,
            binding.org_id,
            f"user:{user.id}",
            authority_kind="domain_producer",
        )

    if requested_tenant_id != identity.tenant_id:
        # A cross-tenant scope alone cannot select another principal or vault tenant.
        if (
            not requested_tenant_id
            or requested_tenant_id.startswith("__")
            or "internal:cross-tenant" not in scopes
            or CAPABILITIES[action] not in scopes
        ):
            _refuse(action, "tenant_mismatch")
        require_service_operation(request, "internal:cross-tenant")
        if service_tenant(request, requested_tenant_id, CAPABILITIES[action]) != requested_tenant_id:
            _refuse(action, "tenant_mismatch")
        principal = f"service:{identity.service_id}"
        if action == "evidence" and requested_principal != principal:
            _refuse(action, "acting_principal_mismatch")
        return await _authorize_operation_metadata(
            request,
            db,
            identity,
            current,
            scopes,
            action,
            requested_tenant_id,
            requested_workspace_id,
            operation_selector,
            requested_principal,
            cross_tenant=True,
        )
    if (
        CAPABILITIES[action] not in scopes
        or action == "revocation"
        or operation_selector is not None
        or f"credential:evidence:workspace:{requested_workspace_id}" not in scopes
    ):
        _refuse(action, "operation_capability_missing")
    require_service_operation(request, CAPABILITIES[action])
    service_tenant(request, requested_tenant_id, CAPABILITIES[action])
    principal = f"service:{identity.service_id}"
    if requested_principal != principal:
        _refuse(action, "acting_principal_mismatch")
    return VaultMetadataAuthority(identity.tenant_id, principal, principal, requested_workspace_id)


async def _authorize_operation_metadata(
    request,
    db,
    identity,
    current,
    scopes,
    action,
    requested_tenant_id,
    workspace_id,
    selector,
    requested_principal,
    *,
    cross_tenant=False,
):
    from src.auth.operation_contract import identity_contract
    from src.auth.vault_delivery import OperationBinding
    from src.internal.domain_operation_store import operation_session

    if not isinstance(selector, tuple) or len(selector) not in {5, 7} or any(not value for value in selector[:5]):
        _refuse(action, "operation_selector_missing")
    operation_id, job_id, attempt_id, credential_id, principal = selector[:5]
    if requested_principal is not None and requested_principal != principal:
        _refuse(action, "acting_principal_mismatch")
    domain_binding = getattr(request.state, "domain_operation_binding", None)
    if cross_tenant:
        if domain_binding is not None or principal != f"service:{identity.service_id}":
            _refuse(action, "cross_tenant_actor_mismatch")
    elif identity.kind == "domain_executor":
        if (
            domain_binding is None
            or domain_binding.adp_org_id != identity.tenant_id
            or domain_binding.org_id != requested_tenant_id
            or current.get("domain_org_id") != requested_tenant_id
            or domain_binding.worker_registry_id != identity.service_id
            or "domain:operation-executor" not in scopes
        ):
            _refuse(action, "domain_operation_mismatch")
    elif requested_tenant_id != identity.tenant_id or CAPABILITIES[action] not in scopes:
        _refuse(action, "operation_capability_missing")

    # The verified run or service holder must match a live server-held lease;
    # body operation names never grant a workspace or credential by themselves.
    statement = text("""
        SELECT o.operation_id, o.job_id, o.org_id, o.workspace_id, l.attempt_id,
               o.request_payload, o.plan_digest
          FROM harness_operations o
          JOIN harness_operation_leases l ON l.operation_id=o.operation_id
          JOIN harness_approval_consumption a ON a.operation_id=o.operation_id
         WHERE o.operation_id=:operation AND o.job_id=:job
           AND o.org_id=:org AND o.workspace_id=:workspace
           AND l.org_id=o.org_id AND l.workspace_id=o.workspace_id
           AND l.attempt_id=:attempt AND l.holder=:holder
           AND l.closed_at IS NULL AND l.expires_at>clock_timestamp()
           AND l.runtime_deadline>clock_timestamp()
           AND o.state IN ('pending','running')
           AND o.cancel_requested_at IS NULL AND NOT o.cleanup_required
           AND a.reservation_state='confirmed' AND a.org_id=o.org_id
           AND a.workspace_id=o.workspace_id AND a.plan_digest=o.plan_digest
    """)
    parameters = {
        "operation": operation_id,
        "job": job_id,
        "org": requested_tenant_id,
        "workspace": workspace_id,
        "attempt": attempt_id,
        "holder": principal,
    }
    try:
        if domain_binding is not None:
            async with operation_session(domain_binding) as session:
                row = (await session.execute(statement, parameters)).mappings().one_or_none()
        else:
            row = (await db.execute(statement, parameters)).mappings().one_or_none()
    except SQLAlchemyError:
        raise HTTPException(503, "vault operation authority unavailable") from None
    if row is None:
        _refuse(action, "operation_binding_missing")
    try:
        reference = identity_contract().admitted_credential_reference(row["request_payload"], row["plan_digest"])
    except RuntimeError:
        raise HTTPException(503, "vault operation authority unavailable") from None
    except (ValueError, TypeError, KeyError):
        _refuse(action, "credential_binding_missing")
    if (
        reference[0] != credential_id
        or (len(selector) == 7 and selector[5] is not None and reference[1] != selector[5])
        or (len(selector) == 7 and selector[6] is not None and reference[2] != selector[6])
    ):
        _refuse(action, "credential_binding_mismatch")
    binding = OperationBinding(
        operation_id=row["operation_id"],
        attempt_id=row["attempt_id"],
        job_id=row["job_id"],
        org_id=requested_tenant_id if cross_tenant else identity.tenant_id,
        workspace_id=row["workspace_id"],
    )
    return VaultMetadataAuthority(
        binding.org_id,
        principal,
        principal,
        binding.workspace_id,
        domain_org_id=domain_binding.org_id if domain_binding else None,
        operation_binding=binding,
        authority_kind="tenant_service" if cross_tenant else "agent_run",
    )
