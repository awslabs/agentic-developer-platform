"""Transactional approval logic for onboarding access requests.

Issue #538: Creates org + tenant + dept + team + user + user_identities
in a single Postgres transaction, then writes to DDB (best-effort).
"""

from __future__ import annotations

import logging
import os

from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.identity.identity_index_writer import IdentityIndexWriter
from src.shared.models.base import new_uuid, utcnow
from src.shared.models.onboarding import Tenant, TenantAccessRequest
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import UserIdentity

logger = logging.getLogger(__name__)


def _v2_write_enabled() -> bool:
    """Check if USER_IDENTITY_INDEX_V2_WRITE is enabled."""
    return os.environ.get("USER_IDENTITY_INDEX_V2_WRITE", "false").lower() == "true"


def _emit_metric(namespace: str, metric_name: str, value: float = 1.0) -> None:
    """Best-effort CloudWatch metric emission."""
    try:
        import boto3

        client = boto3.client("cloudwatch", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        client.put_metric_data(
            Namespace=namespace,
            MetricData=[
                {
                    "MetricName": metric_name,
                    "Value": value,
                    "Unit": "Count",
                }
            ],
        )
    except Exception:
        logger.exception("Failed to emit CloudWatch metric %s/%s", namespace, metric_name)


async def approve_request(
    db: AsyncSession,
    request: TenantAccessRequest,
    admin_sub: str,
    identity_writer: IdentityIndexWriter | None = None,
) -> str:
    """Approve a tenant access request — atomic Postgres transaction.

    Creates: organization, tenant, default department, default team, user,
    2x user_identities (cognito + github). Then DDB write-through (best-effort).

    Returns the tenant_id on success.
    Raises ValueError if the request is not in 'pending' state.
    Raises RuntimeError if USER_IDENTITY_INDEX_V2_WRITE is off.
    """
    if not _v2_write_enabled():
        raise RuntimeError("USER_IDENTITY_INDEX_V2_WRITE=false; onboarding not enabled in this environment")

    if request.status != "pending":
        raise ValueError(f"Request {request.id} is not pending (status={request.status})")

    tenant_id = request.proposed_tenant_id
    now = utcnow()

    # Check if org already exists (idempotent re-approve)
    existing_org = await db.get(Organization, tenant_id)
    if existing_org is not None:
        # Already approved — idempotent
        request.status = "approved"
        request.decided_by = admin_sub
        request.decided_at = now
        await db.commit()
        return tenant_id

    # Create all rows in a single transaction
    dept_id = new_uuid()
    team_id = new_uuid()
    user_id = new_uuid()

    org = Organization(
        id=tenant_id,
        name=request.target_login,
        aws_accounts=[],
        role_mappings={},
        settings={},
        github_installation_ids=[],
        cognito_client_ids=[],
    )
    db.add(org)

    tenant = Tenant(
        id=tenant_id,
        display_name=request.target_login,
    )
    db.add(tenant)

    dept = Department(
        id=dept_id,
        org_id=tenant_id,
        name="Default",
    )
    db.add(dept)

    team = Team(
        id=team_id,
        org_id=tenant_id,
        department_id=dept_id,
        name="Default",
    )
    db.add(team)

    user = User(
        id=user_id,
        org_id=tenant_id,
        team_id=team_id,
        email=f"{request.target_login}@github.onboard",
        name=request.target_login,
        cognito_sub=request.cognito_sub,
        role="org_admin",
    )
    db.add(user)

    # User identities: cognito + github
    cognito_identity = UserIdentity(
        id=new_uuid(),
        user_id=user_id,
        org_id=tenant_id,
        team_id=team_id,
        provider="cognito",
        provider_user_id=request.cognito_sub,
        provider_username=request.target_login,
        verification_method="oauth",
        verified_at=now,
    )
    db.add(cognito_identity)

    github_identity = UserIdentity(
        id=new_uuid(),
        user_id=user_id,
        org_id=tenant_id,
        team_id=team_id,
        provider="github",
        provider_user_id=request.provider_user_id,
        provider_username=request.target_login,
        verification_method="oauth",
        verified_at=now,
    )
    db.add(github_identity)

    # Mark request as approved
    request.status = "approved"
    request.decided_by = admin_sub
    request.decided_at = now

    await db.commit()

    # Post-commit: DDB write-through (best-effort)
    if identity_writer:
        try:
            await identity_writer.put_user_identity(
                provider_user_id=request.cognito_sub,
                user_id=user_id,
                org_id=tenant_id,
                provider="cognito",
                provider_username=request.target_login,
            )
        except Exception:
            logger.exception("DDB write-through failed for cognito identity (onboarding approval)")
            _emit_metric("ADP/Onboarding", "OnboardingApproval.DdbWriteFailure")

        try:
            await identity_writer.put_user_identity(
                provider_user_id=request.provider_user_id,
                user_id=user_id,
                org_id=tenant_id,
                provider="github",
                provider_username=request.target_login,
            )
        except Exception:
            logger.exception("DDB write-through failed for github identity (onboarding approval)")
            _emit_metric("ADP/Onboarding", "OnboardingApproval.DdbWriteFailure")

    return tenant_id


async def deny_request(
    db: AsyncSession,
    request: TenantAccessRequest,
    admin_sub: str,
    decision_note: str | None = None,
    cognito_client=None,
    user_pool_id: str | None = None,
) -> None:
    """Deny a tenant access request and delete the Cognito user.

    Variant A1: AdminDeleteUser on the Cognito sub so user can re-sign-in fresh.
    """
    if request.status not in ("pending", "denied"):
        raise ValueError(f"Request {request.id} cannot be denied (status={request.status})")

    now = utcnow()
    request.status = "denied"
    request.decided_by = admin_sub
    request.decided_at = now
    request.decision_note = decision_note
    await db.commit()

    # Post-commit: Delete Cognito user (best-effort, idempotent)
    if cognito_client and user_pool_id:
        try:
            cognito_client.admin_delete_user(
                UserPoolId=user_pool_id,
                Username=request.cognito_sub,
            )
        except Exception as e:
            error_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            if error_code == "UserNotFoundException":
                # Already deleted — idempotent success
                logger.info("Cognito user %s already deleted (idempotent deny)", request.cognito_sub)
            else:
                logger.exception(
                    "Failed to delete Cognito user %s during deny",
                    request.cognito_sub,
                )
                _emit_metric("ADP/Onboarding", "OnboardingDeny.CognitoDeleteFailure")
