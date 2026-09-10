"""Transactional approval logic for onboarding access requests.

Issue #538: Creates org + tenant + dept + team + user + user_identities
in a single Postgres transaction, then writes to DDB (best-effort).
"""

from __future__ import annotations

import logging
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.cognito_claims import emit_metric, sync_cognito_role_claims
from src.admin.identity.identity_index_writer import IdentityIndexWriter
from src.admin.memberships import upsert_tenant_membership
from src.shared.models.base import new_uuid, utcnow
from src.shared.models.onboarding import Tenant, TenantAccessRequest
from src.shared.models.organization import (
    CREATED_VIA_OPERATOR,
    Department,
    Organization,
    Team,
    User,
)
from src.shared.models.vault import UserIdentity

logger = logging.getLogger(__name__)


def _v2_write_enabled() -> bool:
    """Check if USER_IDENTITY_INDEX_V2_WRITE is enabled."""
    return os.environ.get("USER_IDENTITY_INDEX_V2_WRITE", "false").lower() == "true"


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
        # Issue #4006: upsert the org-admin membership on re-approve too. Approvals
        # that predate this fix left the admin with no membership row at all, so
        # this branch is what heals them; the upsert is a no-op once the row is
        # correct. Same transaction as the request-status write.
        existing_user = await db.scalar(select(User).where(User.cognito_sub == request.cognito_sub))
        if existing_user is not None:
            await upsert_tenant_membership(
                db,
                user_id=existing_user.id,
                tenant_id=tenant_id,
                role="org_admin",
                joined_via="onboarding_approval",
            )
        await db.commit()
        # Re-sync Cognito claims in case a prior approval predated this step or
        # the attributes were cleared — cheap + idempotent. (team_id is omitted
        # on this path; role + org_id are what gate the SPA nav/dashboard.)
        sync_cognito_role_claims(
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
        # Issue #4842 (R6=a): stamped, not inherited. An access request only
        # reaches this function after a platform admin approved it, so the row is
        # operator-onboarded — the same standing as a directly provisioned
        # tenant. Recorded explicitly because the alternative is trusting a
        # column default, and this path had no test proving which value it got.
        created_via=CREATED_VIA_OPERATOR,
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

    # Issue #4006: the org creator's tenant_memberships row — in the SAME
    # transaction as the User row. `tenant_memberships.role` is the authority the
    # read side resolves org role from (#3987/#3998); without this row the admin
    # we just created only holds authority via the legacy ORG_ADMIN fallback, and
    # loses it entirely once that fallback flips to least-privilege.
    await upsert_tenant_membership(
        db,
        user_id=user_id,
        tenant_id=tenant_id,
        role="org_admin",
        joined_via="onboarding_approval",
    )

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
            # Belt-and-braces: since #4006 the membership above is written in the
            # same transaction, so the current tenant is always in this list. Kept
            # as a cheap guard so a future refactor of the write order can't
            # silently shrink member_org_ids.
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
            emit_metric("ADP/Onboarding", "OnboardingApproval.DdbWriteFailure")

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
            emit_metric("ADP/Onboarding", "OnboardingApproval.DdbWriteFailure")

    # Post-commit: sync role/org/team onto the Cognito user so the next token
    # the user mints carries them (the pre-token Lambda reads Cognito attrs, not
    # Postgres). Without this the approved user logs in with an empty role/org →
    # broken SPA nav + dashboard. Best-effort; never rolls back the approval.
    sync_cognito_role_claims(
        cognito_sub=request.cognito_sub,
        org_id=tenant_id,
        role="org_admin",
        team_id=team_id,
        department_id=dept_id,
    )

    return tenant_id


async def attach_approved_member(
    db: AsyncSession,
    request: TenantAccessRequest,
    *,
    granted_role: str,
    decided_by: str,
    sync_cognito_claims: bool = True,
) -> str:
    """Approve a *join-existing-org* access request, granting ``granted_role``.

    Issue #4018: this is the executor for the request class an org admin may
    decide — one whose ``proposed_tenant_id`` names an org that ALREADY exists.
    ``approve_request`` is deliberately NOT reused for it: that function's
    ``existing_org is not None`` branch hardcodes ``role="org_admin"``, so
    routing a member-join approval through it silently promotes the requester to
    co-admin of the org (and syncs ``custom:role=org_admin`` onto their Cognito
    user). The role is a *parameter* here precisely so the caller — which knows
    whether the requester is a GitHub org admin — decides it, and so the
    hardcoded grant cannot be reintroduced by accident.

    Creates, in ONE transaction (mirroring the auto-approve tail of
    ``_attach_user_to_existing_tenant``, which now delegates here): the ``users``
    row, both ``user_identities`` rows, the ``tenant_memberships`` row carrying
    ``granted_role``, and the request's approved status.

    Args:
        db: Session owning the transaction.
        request: The pending request; ``proposed_tenant_id`` must be an existing org.
        granted_role: Role to write onto the user + membership. Normalized for
            storage by ``upsert_tenant_membership``.
        decided_by: Audit value for ``tenant_access_requests.decided_by``.
        sync_cognito_claims: Write role/org/team onto the Cognito user after the
            commit. True for admin decisions (the approved member must be able to
            log in with a correct token). The login-time auto-match path passes
            False — it has never synced claims and keeping a Cognito round-trip
            off the sign-in path preserves that.

    Returns:
        The tenant_id the user was attached to.

    Raises:
        ValueError: The request is not pending, the org does not exist, or the
            org has no team to attach the user to. Raised before any row is
            added, so a failure leaves nothing partially written.
    """
    if request.status != "pending":
        raise ValueError(f"Request {request.id} is not pending (status={request.status})")

    tenant_id = request.proposed_tenant_id
    org = await db.get(Organization, tenant_id)
    if org is None:
        # Caller is expected to have scope-checked against an existing org, so
        # this is a race or a mis-routed new-tenant request, not a normal path.
        raise ValueError(f"Organization {tenant_id} does not exist; use approve_request to create a new tenant")

    team = (await db.execute(select(Team).where(Team.org_id == tenant_id))).scalars().first()
    if team is None:
        raise ValueError(f"Organization {tenant_id} has no team to attach the user to")

    now = utcnow()
    user_id = new_uuid()
    db.add(
        User(
            id=user_id,
            org_id=tenant_id,
            team_id=team.id,
            email=f"{request.target_login}@github.onboard",
            name=request.target_login,
            cognito_sub=request.cognito_sub,
            role=granted_role,
        )
    )

    for provider, provider_user_id in (
        ("cognito", request.cognito_sub),
        ("github", request.provider_user_id),
    ):
        db.add(
            UserIdentity(
                id=new_uuid(),
                user_id=user_id,
                org_id=tenant_id,
                team_id=team.id,
                provider=provider,
                provider_user_id=provider_user_id,
                provider_username=request.target_login,
                verification_method="oauth",
                verified_at=now,
            )
        )

    # Issue #4006: the membership lands in the SAME transaction as the users row —
    # tenant_memberships.role is the authority the read side resolves org role
    # from (#3987/#3998), so a users row without it is an authority-less principal.
    await upsert_tenant_membership(
        db,
        user_id=user_id,
        tenant_id=tenant_id,
        role=granted_role,
        joined_via="org_membership",
        github_org_id=org.name,
    )

    request.status = "approved"
    request.decided_by = decided_by
    request.decided_at = now

    await db.commit()

    # Post-commit, best-effort: the pre-token-generation Lambda reads Cognito
    # attributes rather than Postgres, so without this the approved member logs
    # in with an empty role/org and the SPA nav + dashboard break.
    if sync_cognito_claims:
        sync_cognito_role_claims(
            cognito_sub=request.cognito_sub,
            org_id=tenant_id,
            role=granted_role,
            team_id=team.id,
            department_id=team.department_id or "",
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
                emit_metric("ADP/Onboarding", "OnboardingDeny.CognitoDeleteFailure")
