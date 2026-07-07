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


def _cognito_user_pool_id() -> str:
    """Resolve the Cognito user pool id from either env-var spelling.

    Matches admin/cognito_service.py + onboarding/handler.py: the configmap sets
    BG_COGNITO_USER_POOL_ID; some deployments also export the bare name.
    """
    return os.environ.get("BG_COGNITO_USER_POOL_ID") or os.environ.get("COGNITO_USER_POOL_ID", "")


def _sync_cognito_role_claims(*, cognito_sub: str, org_id: str, role: str, team_id: str, department_id: str = "") -> None:
    """Write role/org/team onto the Cognito user's custom: attributes.

    The pre-token-generation Lambda copies custom:role / custom:org_id /
    custom:team_id / custom:department_id from the Cognito USER ATTRIBUTES into
    the access token — it does NOT read Postgres. So approving a request (which
    only writes the DB rows) leaves the user's token with an empty role/org →
    the SPA hides nav items and the dashboard errors ("undefined reading map").
    Setting the attributes here makes a fresh login mint a correct token.

    Best-effort + idempotent: logs + emits a metric on failure rather than
    rolling back the (already-committed) approval.

    Username handling: AdminUpdateUserAttributes takes a *Username*, which is
    only equal to the user's `sub` for email-signup users (Cognito assigns them
    a UUID username that happens to match). GitHub-broker-provisioned users get
    username `GitHub_<github_id>` (see lambda/github-auth-broker/
    cognito_provisioner.py), so an update keyed on the sub throws
    UserNotFoundException. We therefore try the sub first (correct + zero extra
    calls for email-signup users) and, on UserNotFoundException, resolve the
    real username by an exact `sub` filter (Cognito enforces sub uniqueness, so
    this cannot alias another user) and retry once.
    """
    pool_id = _cognito_user_pool_id()
    if not pool_id:
        logger.warning("Cannot sync Cognito role claims: no BG_COGNITO_USER_POOL_ID / COGNITO_USER_POOL_ID set")
        _emit_metric("ADP/Onboarding", "OnboardingApproval.CognitoClaimSyncSkipped")
        return
    attrs = [
        {"Name": "custom:role", "Value": role},
        {"Name": "custom:org_id", "Value": org_id},
        {"Name": "custom:team_id", "Value": team_id},
    ]
    if department_id:
        attrs.append({"Name": "custom:department_id", "Value": department_id})
    try:
        import boto3

        client = boto3.client("cognito-idp", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        try:
            client.admin_update_user_attributes(
                UserPoolId=pool_id,
                Username=cognito_sub,
                UserAttributes=attrs,
            )
            logger.info("Synced Cognito role claims for sub=%s (role=%s org=%s)", cognito_sub, role, org_id)
            return
        except Exception as e:
            error_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            if error_code != "UserNotFoundException":
                raise
            # sub != username (e.g. GitHub-federated user `GitHub_<id>`): resolve
            # the real username by an exact sub filter and retry once.
            username = _resolve_username_by_sub(client, pool_id, cognito_sub)
            if not username:
                logger.error(
                    "Failed to sync Cognito role claims: no Cognito user found for sub=%s "
                    "(AdminUpdateUserAttributes by sub raised UserNotFoundException and "
                    "ListUsers sub-filter returned no match)",
                    cognito_sub,
                )
                _emit_metric("ADP/Onboarding", "OnboardingApproval.CognitoClaimSyncFailure")
                return
            client.admin_update_user_attributes(
                UserPoolId=pool_id,
                Username=username,
                UserAttributes=attrs,
            )
            logger.info(
                "Synced Cognito role claims for sub=%s via resolved username=%s (role=%s org=%s)",
                cognito_sub,
                username,
                role,
                org_id,
            )
    except Exception:
        logger.exception("Failed to sync Cognito role claims for sub=%s", cognito_sub)
        _emit_metric("ADP/Onboarding", "OnboardingApproval.CognitoClaimSyncFailure")


def _resolve_username_by_sub(client, pool_id: str, cognito_sub: str) -> str:
    """Resolve a Cognito user's username from its `sub` via an exact-match filter.

    Returns the username, or "" if no user matches. Cognito enforces sub
    uniqueness, so the exact `sub = "<sub>"` filter cannot alias another user.
    """
    resp = client.list_users(
        UserPoolId=pool_id,
        Filter=f'sub = "{cognito_sub}"',
        Limit=1,
    )
    users = resp.get("Users", [])
    if not users:
        return ""
    return users[0].get("Username", "")


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
        # Re-sync Cognito claims in case a prior approval predated this step or
        # the attributes were cleared — cheap + idempotent. (team_id is omitted
        # on this path; role + org_id are what gate the SPA nav/dashboard.)
        _sync_cognito_role_claims(
            cognito_sub=request.cognito_sub,
            org_id=tenant_id,
            role="org_admin",
            team_id="",
        )
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
    # Issue #3134: Seed member_org_ids with the FULL list of tenant memberships
    # so the webhook Lambda can enforce trigger_policy immediately.
    # Issue #3134 fix: Query all memberships (not just [tenant_id]) to avoid
    # shrinking a multi-org user's membership list on re-approval.
    if identity_writer:
        try:
            from sqlalchemy import select as sa_select

            from src.shared.models.onboarding import TenantMembership

            all_memberships_stmt = sa_select(TenantMembership.tenant_id).where(
                TenantMembership.user_id == user_id,
            )
            all_org_ids = list((await db.execute(all_memberships_stmt)).scalars().all())
            # Ensure the current tenant is included (may not have a TenantMembership yet
            # if approval creates the user but membership is created separately)
            if tenant_id not in all_org_ids:
                all_org_ids.append(tenant_id)
        except Exception:
            logger.exception("Failed to query memberships for approval DDB write (falling back to [tenant_id])")
            all_org_ids = [tenant_id]

        try:
            await identity_writer.put_user_identity(
                provider_user_id=request.cognito_sub,
                user_id=user_id,
                org_id=tenant_id,
                provider="cognito",
                provider_username=request.target_login,
                member_org_ids=all_org_ids,
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
                member_org_ids=all_org_ids,
            )
        except Exception:
            logger.exception("DDB write-through failed for github identity (onboarding approval)")
            _emit_metric("ADP/Onboarding", "OnboardingApproval.DdbWriteFailure")

    # Post-commit: sync role/org/team onto the Cognito user so the next token
    # the user mints carries them (the pre-token Lambda reads Cognito attrs, not
    # Postgres). Without this the approved user logs in with an empty role/org →
    # broken SPA nav + dashboard. Best-effort; never rolls back the approval.
    _sync_cognito_role_claims(
        cognito_sub=request.cognito_sub,
        org_id=tenant_id,
        role="org_admin",
        team_id=team_id,
        department_id=dept_id,
    )

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
