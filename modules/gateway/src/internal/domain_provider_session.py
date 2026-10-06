"""Short-lived provider sessions derived exclusively from a live paid operation."""

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select

from src.auth.aws_connection_authority import connection_material, require_assumed_identity, verified_connection_evidence
from src.auth.vault_delivery import DELIVERY_PERMISSION, OperationBinding, deliver_credential
from src.internal.domain_operation_runtime import authenticated
from src.internal.domain_operation_store import harness, operation_connect, operation_session
from src.internal.sts_assume_service import assume_role
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import User

REFUSED = "paid provider session refused"


async def current_human_identity(db, *, subject, adp_org_id):
    # The shared identity owner supplies this maintained Cognito + membership reader.
    # There is no raw-subject or local-row fallback when it is unavailable.
    from src.internal.domain_current_identity import current_human_identity as read

    return await read(db, subject=subject, adp_org_id=adp_org_id)


def sealed_target(operation, region):
    """Request fields are comparisons, never authority to select another target."""
    identity = harness("identity")
    credential = identity.admitted_credential_reference(operation["request_payload"], operation["plan_digest"])
    provider, account = identity.admitted_credential_target(operation["request_payload"], operation["plan_digest"])
    request = identity.decode_payload(operation["request_payload"])
    parameters = request.parameters
    lifecycle = json.loads(parameters["lifecycle_request"])
    if (
        provider != "aws"
        or credential[1] != "aws"
        or not re.fullmatch(r"[0-9]{12}", account)
        or lifecycle.get("mode") not in {"existing-account-managed", "bring-existing-cluster"}
        or lifecycle.get("target_account_id") != account
        or lifecycle.get("region") != region
        or not re.fullmatch(r"[a-z]{2}(?:-gov)?-[a-z]+-[0-9]", region)
        or parameters.get("shared_membership")
        or parameters.get("shared_membership_id")
    ):
        raise HTTPException(403, REFUSED)
    return credential, account


def session_deadline(grant, operation, lease, *, require_session=True):
    deadline = min(grant.expires_at, operation["approval_expires_at"], lease["runtime_deadline"])
    if datetime.now(UTC) + timedelta(seconds=900 if require_session else 0) >= deadline:
        raise HTTPException(403, REFUSED)
    return deadline


def verify_session(result, role_arn, deadline, *, issued_at):
    require_assumed_identity(result, role_arn)
    expiry = datetime.fromisoformat(result.expiration.replace("Z", "+00:00"))
    # STS may truncate fractional seconds; no fabricated extension is accepted.
    if expiry.tzinfo is None or not issued_at < expiry <= min(deadline, datetime.now(UTC) + timedelta(seconds=900)):
        raise HTTPException(403, REFUSED)
    role_id = getattr(result, "assumed_role_id", None)
    session_name = result.assumed_role_arn.rsplit("/", 1)[1]
    if not isinstance(role_id, str) or not re.fullmatch(r"AROA[A-Z0-9]{17}:" + re.escape(session_name), role_id):
        raise HTTPException(403, REFUSED)
    if any(not isinstance(value, str) or not value for value in (result.access_key_id, result.secret_access_key, result.session_token)):
        raise HTTPException(403, REFUSED)
    return expiry


async def current(request, operation_id):
    binding, _, caller, record, grant, original, operation = await authenticated(request, mode="execution")
    if original["operation_id"] != operation_id:
        raise HTTPException(403, REFUSED)
    async with operation_connect(binding) as connection:
        lease = await connection.fetchrow(
            "SELECT * FROM harness_operation_leases WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3 "
            "AND holder=$4 AND attempt_id=$4 AND closed_at IS NULL AND expires_at>clock_timestamp() "
            "AND runtime_deadline>clock_timestamp()",
            operation_id,
            binding.org_id,
            original["workspace_id"],
            caller.principal,
        )
    if lease is None:
        raise HTTPException(403, REFUSED)
    return binding, caller.principal, record, grant, operation, dict(lease)


def snapshot(state):
    binding, principal, record, grant, operation, lease = state
    return (
        binding,
        principal,
        record,
        grant,
        operation["requester"],
        operation["plan_digest"],
        operation["approval_id"],
        tuple(lease[key] for key in ("operation_id", "org_id", "workspace_id", "holder", "attempt_id", "fence_token")),
    )


async def provider_session(request, body, db, sm, *, preflight_only=False):
    initial = await current(request, body.operation_id)
    binding, principal, _, grant, operation, lease = initial
    (credential_id, service, label), account = sealed_target(operation, body.region)
    deadline = session_deadline(grant, operation, lease, require_session=not preflight_only)
    authority = snapshot(initial)
    from src.internal.domain_provider_cleanup import cleanup_policy

    entry_arn = getattr(body, "access_entry_arn", None)
    policy, entry_identity = await cleanup_policy(binding, operation, entry_arn)

    async def refresh():
        latest = await current(request, body.operation_id)
        if snapshot(latest) != authority:
            raise HTTPException(403, REFUSED)
        if await cleanup_policy(binding, operation, entry_arn) != (policy, entry_identity):
            raise HTTPException(403, REFUSED)
        # A shortened approval or runtime deadline must also constrain the answer.
        if session_deadline(latest[3], latest[4], latest[5], require_session=not preflight_only) < deadline:
            raise HTTPException(403, REFUSED)
        return frozenset({DELIVERY_PERMISSION})

    operation_binding = OperationBinding(
        body.operation_id, lease["attempt_id"], operation["job_id"], binding.org_id, operation["workspace_id"], "aws", account
    )
    async with operation_session(binding) as operations:

        async def deliver(*, preflight=False):
            return await deliver_credential(
                db,
                sm,
                binding=operation_binding,
                credential_id=credential_id,
                service=service,
                label=label,
                recipient=principal,
                authenticated_recipient=principal,
                granted_permissions=await refresh(),
                refresh_executor=refresh,
                operation_session=operations,
                vault_org_id=binding.adp_org_id,
                preflight_only=preflight,
            )

        async def owner(credential):
            identity = await current_human_identity(db, subject=operation["requester"], adp_org_id=binding.adp_org_id)
            if (
                identity.get("subject") != operation["requester"]
                or identity.get("adp_org_id") != binding.adp_org_id
                or identity.get("principal_type") != "human"
                or identity.get("active") is not True
                or identity.get("enabled") is not True
                or not identity.get("membership_id")
            ):
                raise HTTPException(403, REFUSED)
            user = await db.scalar(
                select(User)
                .join(TenantMembership, TenantMembership.user_id == User.id)
                .where(
                    TenantMembership.id == identity["membership_id"],
                    TenantMembership.tenant_id == binding.adp_org_id,
                    TenantMembership.revoked_at.is_(None),
                    User.org_id == binding.adp_org_id,
                )
                .execution_options(populate_existing=True)
            )
            if (
                user is None
                or user.org_id != binding.adp_org_id
                or user.user_kind != "human"
                or user.is_shadow
                or credential.user_id != user.id
                or credential.org_id != user.org_id
                or credential.credential_type != "aws_role"
            ):
                raise HTTPException(403, REFUSED)
            return user.id

        credential, secret = await deliver(preflight=preflight_only)
        user_id = await owner(credential)
        evidence = verified_connection_evidence(credential)
        if await asyncio.to_thread(sm.current_version_id, credential.secret_arn) != evidence[1]:
            raise HTTPException(403, REFUSED)
        if preflight_only:
            await refresh()
            return {"admits_work": True, "operation_id": body.operation_id, "authority_expires_at": deadline.isoformat()}
        role, external_id, selected_account = connection_material(json.loads(secret.reveal()), credential.scopes or {}, credential)
        if selected_account != account:
            raise HTTPException(403, REFUSED)
        issued_at = datetime.now(UTC)
        result = await asyncio.to_thread(
            assume_role,
            role_arn=role,
            external_id=external_id,
            session_duration_seconds=900,
            default_region=body.region,
            user_id=user_id,
            agent_id="superplane-operation",
            task_id=body.operation_id,
            label=label,
            aws_region=body.region,
            session_policy=policy,
        )
        expiry = verify_session(result, role, deadline, issued_at=issued_at)
        if entry_identity is not None:
            import boto3
            from botocore.config import Config

            session = boto3.Session(
                aws_access_key_id=result.access_key_id,
                aws_secret_access_key=result.secret_access_key,
                aws_session_token=result.session_token,
                region_name=body.region,
            )
            eks = session.client("eks", config=Config(connect_timeout=5, read_timeout=15, retries={"total_max_attempts": 1}))
            generation, cluster, principal_arn, owned = entry_identity
            actual = await asyncio.to_thread(eks.describe_access_entry, clusterName=cluster.rsplit("/", 1)[1], principalArn=principal_arn)
            entry = actual.get("accessEntry", {})
            if (
                entry.get("accessEntryArn") != entry_arn
                or entry.get("principalArn") != principal_arn
                or entry.get("clusterName") != cluster.rsplit("/", 1)[1]
                or entry.get("type") != "STANDARD"
                or entry.get("tags", {}).get("superplane-generation") != generation
                or sorted(entry.get("kubernetesGroups", [])) != owned.get("groups")
                or entry.get("username") != owned.get("username")
            ):
                raise HTTPException(403, REFUSED)
        latest, _ = await deliver(preflight=True)
        await db.refresh(latest)
        if await owner(latest) != user_id or verified_connection_evidence(latest) != evidence:
            raise HTTPException(403, REFUSED)
        await refresh()
        from src.internal.credential_routes import _write_audit

        await _write_audit(
            db,
            event_type="paid_provider_session_issued",
            org_id=binding.adp_org_id,
            actor_id=principal,
            details={"operation_id": body.operation_id, "credential_id": credential_id, "requester": user_id, "expires_at": expiry.isoformat()},
        )
        await db.commit()
        await refresh()
        latest, _ = await deliver(preflight=True)
        await db.refresh(latest)
        if await owner(latest) != user_id or verified_connection_evidence(latest) != evidence:
            raise HTTPException(403, REFUSED)
        return {
            "version": 1,
            "operation_id": body.operation_id,
            "credential_id": credential_id,
            "role_arn": role,
            "account_id": account,
            "region": body.region,
            "assumed_role_arn": result.assumed_role_arn,
            "assumed_role_id": result.assumed_role_id,
            "access_key_id": result.access_key_id,
            "secret_access_key": result.secret_access_key,
            "session_token": result.session_token,
            "expiration": expiry.isoformat(),
            "authority_expires_at": deadline.isoformat(),
            "access_entry_arn": entry_arn,
        }
