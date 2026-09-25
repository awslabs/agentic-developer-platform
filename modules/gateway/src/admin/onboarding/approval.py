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
from src.admin.config import PLATFORM_LEVEL_ROLES
from src.admin.identity.identity_index_writer import IdentityIndexWriter
from src.admin.memberships import project_member_org_ids, upsert_tenant_membership
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

from .approval_decision import target_account_for_request

logger = logging.getLogger(__name__)


def _sync_approval_claims(**kwargs):
    """Keep committed approval, but expose incomplete provider synchronization."""
    complete = sync_cognito_role_claims(**kwargs)
    if complete is False:
        from src.admin.audit_operation import current_operation

        operation = current_operation.get()
        if operation is not None:
            operation.terminal = {"outcome": "reconciliation_required"}
    return complete


def _v2_write_enabled() -> bool:
    """Check if USER_IDENTITY_INDEX_V2_WRITE is enabled."""
    return os.environ.get("USER_IDENTITY_INDEX_V2_WRITE", "false").lower() == "true"


async def approve_request(
    db: AsyncSession,
    request: TenantAccessRequest,
    admin_sub: str,
    identity_writer: IdentityIndexWriter | None = None,
    *,
    granted_role: str = "org_admin",
) -> str:
    """Approve a tenant access request — atomic Postgres transaction.

    Creates: organization, tenant, default department, default team, user,
    2x user_identities (cognito + github). Then DDB write-through (best-effort).

    Args:
        granted_role: The role to write onto the user, the membership row and the
            Cognito claims. #5666 (A11): this was hardcoded ``"org_admin"`` at five
            points in this function, including the ``existing_org is not None``
            branch — which meant a platform admin approving a request to JOIN an
            existing org silently minted a co-administrator of it. It is now a
            parameter derived once by
            ``approval_decision.derive_approval_decision``, so the role reported,
            persisted and synced is necessarily the same value.

            The ``"org_admin"`` default covers the genuine new-organization case,
            where the requester IS the org's owner, and keeps ``bootstrap_admin``
            (which creates the first platform admin's own org, then overwrites
            ``users.role`` itself) working unchanged.

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
        # #5666 (A11): scoped to the org being approved. This lookup was global
        # (`cognito_sub` alone), so with more than one users row for a sub it could
        # heal a membership for a DIFFERENT tenant than the one under approval, and
        # in Postgres the unscoped read is ambiguous where SQLite is forgiving.
        existing_user = await target_account_for_request(db, request)
        if existing_user is not None:
            # Organization approval must not revoke account-wide platform authority.
            if existing_user.org_id == tenant_id and (existing_user.role or "").strip().lower() not in PLATFORM_LEVEL_ROLES:
                existing_user.role = granted_role
            await upsert_tenant_membership(
                db,
                user_id=existing_user.id,
                tenant_id=tenant_id,
                # #5666 (A11): was hardcoded "org_admin". On an existing org this is
                # the JOIN_EXISTING class, so a hardcoded owner role promoted every
                # approved joiner to co-admin.
                role=granted_role,
                joined_via="onboarding_approval",
            )
        await db.commit()
        # Issue #4849: this branch upserts a membership but returns before the
        # post-commit projection block below, so re-approves — the branch that
        # *heals* the pre-#4006 no-row cohort — never refreshed member_org_ids.
        if existing_user is not None:
            await project_member_org_ids(db, user_id=existing_user.id, writer=identity_writer)
        # Re-sync Cognito claims in case a prior approval predated this step or
        # the attributes were cleared — cheap + idempotent. (team_id is omitted
        # on this path; role + org_id are what gate the SPA nav/dashboard.)
        canonical_role = await db.scalar(select(User.role).where(User.cognito_sub == request.cognito_sub))
        claim_role = canonical_role if (canonical_role or "").strip().lower() in PLATFORM_LEVEL_ROLES else granted_role
        if existing_user is not None and (existing_user.role or "").strip().lower() in PLATFORM_LEVEL_ROLES:
            claim_role = existing_user.role
        _sync_approval_claims(
            cognito_sub=request.cognito_sub,
            org_id=tenant_id,
            # #5666 (A11): the claims must carry the SAME role as the membership row
            # written above. Hardcoding here was the second half of the defect: even
            # had the membership been correct, the claims still asserted org_admin.
            role=claim_role,
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

    canonical = await db.scalar(select(User).where(User.cognito_sub == request.cognito_sub))
    user = User(
        id=user_id,
        org_id=tenant_id,
        team_id=team_id,
        email=f"{request.target_login}@github.onboard",
        name=request.target_login,
        cognito_sub=None if canonical is not None else request.cognito_sub,
        # #5666 (A11): the derived role. Reaching this point normally means the
        # CREATE_NEW class, where org_admin is correct because the requester owns the
        # org being created — but it is the DERIVATION that says so now, not this line.
        role=granted_role,
    )
    db.add(user)
    await db.flush()
    if canonical is not None:
        from src.shared.identity.workspaces import link_login_to_workspace

        await link_login_to_workspace(db, canonical, user)

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
    if canonical is None:
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
        role=granted_role,
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
                # #5664 (A10): mirror the provenance of the Postgres row created
                # above — an approved onboarding completed a provider sign-in.
                verification_method=cognito_identity.verification_method,
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
                verification_method=github_identity.verification_method,
            )
        except Exception:
            logger.exception("DDB write-through failed for github identity (onboarding approval)")
            emit_metric("ADP/Onboarding", "OnboardingApproval.DdbWriteFailure")

        # Issue #4849: the two writes above seed the identity rows from the
        # *request* (they must — the rows may not exist yet, and only this path
        # projects the cognito-provider row). This re-projects from committed
        # Postgres truth via the shared helper, which also covers any additional
        # github identity rows the user holds in other orgs — user_identities is
        # uniquely indexed per (provider, provider_user_id, org_id), so a
        # multi-org user has more than one and request.provider_user_id names
        # only the one being approved.
        await project_member_org_ids(db, user_id=user_id, writer=identity_writer)

    # Post-commit: sync role/org/team onto the Cognito user so the next token
    # the user mints carries them (the pre-token Lambda reads Cognito attrs, not
    # Postgres). Without this the approved user logs in with an empty role/org →
    # broken SPA nav + dashboard. Best-effort; never rolls back the approval.
    _sync_approval_claims(
        cognito_sub=request.cognito_sub,
        org_id=tenant_id,
        role=canonical.role if canonical is not None and (canonical.role or "").strip().lower() in PLATFORM_LEVEL_ROLES else granted_role,
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

    from src.shared.identity.workspaces import link_login_to_workspace

    now = utcnow()
    canonical = await db.scalar(select(User).where(User.cognito_sub == request.cognito_sub))
    user = await target_account_for_request(db, request)
    if user is None:
        user = User(
            id=new_uuid(),
            org_id=tenant_id,
            team_id=team.id,
            email=f"{request.target_login}@github.onboard",
            name=request.target_login,
            cognito_sub=None if canonical is not None else request.cognito_sub,
            role=granted_role,
        )
        db.add(user)
        await db.flush()
    elif user.org_id == tenant_id and (user.role or "").strip().lower() not in PLATFORM_LEVEL_ROLES:
        user.role = granted_role
    user_id = user.id
    if canonical is not None and canonical.id != user_id:
        await link_login_to_workspace(db, canonical, user)

    for provider, provider_user_id in (
        ("cognito", request.cognito_sub),
        ("github", request.provider_user_id),
    ):
        existing_identity = await db.scalar(
            select(UserIdentity).where(
                UserIdentity.user_id == user_id,
                UserIdentity.provider == provider,
                UserIdentity.provider_user_id == provider_user_id,
            )
        )
        if existing_identity is not None:
            continue
        db.add(
            UserIdentity(
                id=new_uuid(),
                user_id=user_id,
                org_id=user.org_id,
                team_id=user.team_id,
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

    # Issue #4849: this path (the #4018 org-admin approval class, and the
    # login auto-match attach) created a membership but never projected it, so
    # member_org_ids stayed stale for every member attached to an existing org.
    await project_member_org_ids(db, user_id=user_id)

    # Post-commit, best-effort: the pre-token-generation Lambda reads Cognito
    # attributes rather than Postgres, so without this the approved member logs
    # in with an empty role/org and the SPA nav + dashboard break.
    if sync_cognito_claims:
        canonical_role = canonical.role if canonical is not None else user.role
        claim_role = canonical_role if (canonical_role or "").strip().lower() in PLATFORM_LEVEL_ROLES else granted_role
        if (user.role or "").strip().lower() in PLATFORM_LEVEL_ROLES:
            claim_role = user.role
        _sync_approval_claims(
            cognito_sub=request.cognito_sub,
            org_id=tenant_id,
            role=claim_role,
            team_id=team.id,
            department_id=team.department_id or "",
        )

    return tenant_id


async def deny_request(
    db: AsyncSession,
    request: TenantAccessRequest,
    admin_sub: str,
    decision_note: str | None = None,
) -> None:
    """Deny only this access request; preserve the requester's global account.

    A tenant decision does not authorize deleting a Cognito login shared by other
    workspaces. Global deletion belongs to the separate user-administration flow.
    """
    if request.status not in ("pending", "denied"):
        raise ValueError(f"Request {request.id} cannot be denied (status={request.status})")

    request.status = "denied"
    request.decided_by = admin_sub
    request.decided_at = utcnow()
    request.decision_note = decision_note
    await db.commit()
