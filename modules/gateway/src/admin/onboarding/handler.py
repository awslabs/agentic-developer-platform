"""Onboarding API handler — access status, request, admin approve/deny.

Issue #538: Self-serve onboarding flow routes.
Issue #2953: D5 multi-tenant — join ALL matching org tenants on login.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user, require_admin
from src.shared.database import get_db
from src.shared.models.onboarding import TenantAccessRequest, TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.schemas.auth import TokenContext

from .approval import approve_request, deny_request
from .schemas import (
    RESERVED_TENANT_IDS,
    TENANT_ID_PATTERN,
    AccessRequestPayload,
    AccessRequestResponse,
    AccessStatusResponse,
    AdminAccessRequestItem,
    AdminAccessRequestList,
    AdminDecisionPayload,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@dataclass(frozen=True)
class MatchedTenant:
    """A tenant matched for a user via GitHub org membership verification."""

    org_id: str
    org_name: str
    install_id: int


def _cognito_user_pool_id() -> str:
    """Resolve the Cognito user pool id from either env-var spelling.

    The configmap sets ``BG_COGNITO_USER_POOL_ID`` (the BG_-prefixed name that
    pydantic Settings reads); some deployments also export the bare
    ``COGNITO_USER_POOL_ID``. Onboarding's GitHub-identity lookup historically
    read ONLY the bare name, so a deployment that set just the BG_ form left it
    empty → _fetch_github_identity_from_cognito returned ("","") → the access
    request 400'd with "Onboarding currently requires signing in via GitHub"
    even for a valid GitHub session. Check both, matching cognito_service.py /
    admin/routes.py.
    """
    return os.environ.get("BG_COGNITO_USER_POOL_ID") or os.environ.get("COGNITO_USER_POOL_ID", "")


def _get_auto_approve_orgs() -> list[dict]:
    """Parse ONBOARDING_AUTO_APPROVE_ORGS from environment (JSON list)."""
    raw = os.environ.get("ONBOARDING_AUTO_APPROVE_ORGS", "[]")
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []


def _is_auto_approve(target_login: str) -> bool:
    """Check if the target_login is in the auto-approve list."""
    orgs = _get_auto_approve_orgs()
    for entry in orgs:
        if entry.get("login") == target_login:
            return True
    return False


def _v2_write_enabled() -> bool:
    return os.environ.get("USER_IDENTITY_INDEX_V2_WRITE", "false").lower() == "true"


def _decode_jwt_claims(authorization: str | None) -> dict:
    """Decode (without validation) the claims of the Authorization Bearer JWT.

    The token has already been validated upstream by get_current_user; we just
    need the raw claims for fields TokenContext doesn't carry
    (custom:github_username, cognito:username, etc).
    """
    if not authorization:
        return {}
    if authorization.lower().startswith("bearer "):
        token = authorization[7:]
    else:
        token = authorization
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _slugify_tenant_id(login: str) -> str:
    """Slugify a GitHub login into a safe tenant ID.

    lowercase, alphanumeric + hyphens, no leading/trailing hyphen, trim to 64.
    """
    s = login.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    if len(s) > 64:
        s = s[:64].rstrip("-")
    return s


def _extract_from_claims(claims: dict) -> tuple[str, str]:
    """Best-effort (github_login, github_numeric_id) from JWT claims alone.

    Returns empty strings for anything missing — caller decides whether to
    fall back to an AdminGetUser lookup.
    """
    github_login = claims.get("custom:github_username") or ""
    cognito_username = claims.get("cognito:username") or claims.get("username") or ""
    github_id = ""
    if cognito_username.startswith("github_"):
        github_id = cognito_username[len("github_") :]
    # Cognito-native federation fallback (not used by our broker flow today,
    # kept for forward compat if we ever re-add native GitHub IdP)
    if not github_id and claims.get("identities"):
        try:
            ids = claims["identities"]
            if isinstance(ids, str):
                ids = json.loads(ids)
            for ident in ids or []:
                if ident.get("providerName", "").lower() in {"github", "loginwithgithub"}:
                    github_id = str(ident.get("userId") or "")
                    break
        except (ValueError, json.JSONDecodeError):
            pass
    return github_login, github_id


def _fetch_github_identity_from_cognito(cognito_sub: str) -> tuple[str, str]:
    """Look up the Cognito user by sub and extract GitHub identity from attrs.

    Needed because Cognito access tokens don't include `custom:*` claims by
    default (unless the pre-token-gen Lambda injects them). ID tokens do, but
    the SPA sends the access token as Bearer. Rather than couple onboarding
    to the pre-token-gen flow, we just do one admin API call here.

    Username convention (set by the broker on AdminCreateUser):
      github_<numeric_github_id>  →  we parse the id back out of the username
    """
    user_pool_id = _cognito_user_pool_id()
    if not user_pool_id:
        return "", ""
    try:
        import boto3

        client = boto3.client("cognito-idp")
        # sub is a UUID; we need to list by sub attribute since AdminGetUser
        # takes Username (not sub). Cognito supports a Filter for this.
        resp = client.list_users(
            UserPoolId=user_pool_id,
            Filter=f'sub = "{cognito_sub}"',
            Limit=1,
        )
        users = resp.get("Users", [])
        if not users:
            return "", ""
        user = users[0]
        username = user.get("Username", "")
        attrs = {a["Name"]: a["Value"] for a in user.get("Attributes", [])}
        github_login = attrs.get("custom:github_username") or attrs.get("name") or ""
        github_id = username[len("github_") :] if username.startswith("github_") else ""
        return github_login, github_id
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cognito AdminGetUser fallback failed for sub=%s: %s", cognito_sub, exc)
        return "", ""


def _extract_github_identity(claims: dict, cognito_sub: str) -> tuple[str, str]:
    """Return (github_login, github_numeric_id) — claims first, Cognito lookup as fallback.

    Raises HTTPException(400) only when neither source yields both values.
    """
    github_login, github_id = _extract_from_claims(claims)
    if github_login and github_id:
        return github_login, github_id

    # Fallback: look the user up in Cognito directly (sub is always in JWT)
    fallback_login, fallback_id = _fetch_github_identity_from_cognito(cognito_sub)
    github_login = github_login or fallback_login
    github_id = github_id or fallback_id

    if not github_login or not github_id:
        raise HTTPException(
            status_code=400,
            detail={
                "reason": "not_a_github_session",
                "hint": "Onboarding currently requires signing in via GitHub.",
            },
        )
    return github_login, github_id


async def _find_matching_tenants_for_user(
    db: AsyncSession,
    github_login: str,
) -> list[MatchedTenant]:
    """Find ALL existing ADP tenants this GitHub user is a verified member of.

    Issue #2953 (D5): Returns a list of MatchedTenant ordered by
    Organization.created_at (deterministic). Empty list if none match.

    Failure mode: any GitHub API error for a specific org -> log + skip that
    org (fail-closed per org, not per call).
    """
    from src.admin.connections.github_client import GitHubAppClient
    from src.admin.connections.service import _get_github_app_credentials

    # Fetch all orgs ordered by created_at for deterministic ordering
    stmt = select(Organization).order_by(Organization.created_at)
    candidates = (await db.execute(stmt)).scalars().all()
    candidates = [o for o in candidates if o.github_installation_ids]
    if not candidates:
        return []

    app_id, private_key = _get_github_app_credentials()
    if not app_id or not private_key:
        logger.warning("GitHub App credentials not configured; cannot verify org membership")
        return []

    matched: list[MatchedTenant] = []
    client = GitHubAppClient(app_id=app_id, private_key_pem=private_key)
    try:
        for org in candidates:
            for install_id in org.github_installation_ids:
                try:
                    is_member = await client.check_org_membership(
                        installation_id=int(install_id),
                        org_login=org.name,
                        username=github_login,
                    )
                    if is_member:
                        matched.append(
                            MatchedTenant(
                                org_id=org.id,
                                org_name=org.name,
                                install_id=int(install_id),
                            )
                        )
                        break  # found for this org, move to next org
                except Exception as exc:
                    logger.warning(
                        "org membership check failed org=%s install=%s user=%s: %s",
                        org.name,
                        install_id,
                        github_login,
                        exc,
                    )
                    continue  # try next install / next org
    finally:
        await client.aclose()
    return matched


async def _determine_role_for_matched_user(
    github_login: str,
    org_login: str,
    installation_id: int,
) -> str:
    """Return 'org_admin' if the user is a GitHub org admin, else 'member'.

    Uses GET /orgs/{org}/memberships/{username} with installation token.
    """
    from src.admin.connections.github_client import GitHubAppClient
    from src.admin.connections.service import _get_github_app_credentials

    try:
        app_id, private_key = _get_github_app_credentials()
        if not app_id or not private_key:
            return "member"
        client = GitHubAppClient(app_id=app_id, private_key_pem=private_key)
        try:
            token = await client.get_installation_token(installation_id)
            resp = await client._http_client.get(
                f"/orgs/{org_login}/memberships/{github_login}",
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            if resp.status_code == 200 and resp.json().get("role") == "admin":
                return "org_admin"
        finally:
            await client.aclose()
    except Exception:
        logger.warning("role check failed for %s in %s; defaulting to member", github_login, org_login)
    return "member"


async def _attach_user_to_existing_tenant(
    db: AsyncSession,
    org_id: str,
    cognito_sub: str,
    github_login: str,
    github_id: str,
) -> AccessRequestResponse:
    """Attach a user to an existing tenant as a member (or org_admin if GitHub org admin).

    Creates a TenantAccessRequest row (for audit trail) + User row + UserIdentity rows.
    Respects the org's member_approval_policy.
    """
    from src.shared.models.base import new_uuid, utcnow
    from src.shared.models.vault import UserIdentity

    org = await db.get(Organization, org_id)
    if org is None:
        # Should not happen — caller verified it exists. Fall through to new-tenant flow.
        return AccessRequestResponse(
            status="collision",
            reason="Matched org not found; please contact an administrator.",
        )

    # Determine role via GitHub API
    install_id = int(org.github_installation_ids[0]) if org.github_installation_ids else 0
    role = await _determine_role_for_matched_user(github_login, org.name, install_id)

    # Check approval policy
    auto_approve = org.member_approval_policy == "auto_approve_org_members"

    # Create audit trail row
    now = utcnow()
    request = TenantAccessRequest(
        cognito_sub=cognito_sub,
        provider="github",
        provider_user_id=github_id,
        proposed_tenant_id=org_id,
        target_login=github_login,
        motivation=f"Auto-matched to org '{org.name}' via GitHub org membership",
        status="approved" if auto_approve else "pending",
        decided_by="system:org-member-match" if auto_approve else None,
        decided_at=now if auto_approve else None,
    )
    db.add(request)

    if not auto_approve:
        await db.commit()
        await db.refresh(request)
        return AccessRequestResponse(
            status="pending",
            request_id=request.id,
            eta_hours=24,
        )

    # Auto-approve: create user + identities in the existing tenant
    # Find the default team in this org
    from src.shared.models.organization import Team

    stmt = select(Team).where(Team.org_id == org_id)
    result = await db.execute(stmt)
    team = result.scalars().first()
    if team is None:
        # No team — can't attach. Fall through to pending.
        request.status = "pending"
        request.decided_by = None
        request.decided_at = None
        await db.commit()
        await db.refresh(request)
        return AccessRequestResponse(
            status="pending",
            request_id=request.id,
            eta_hours=24,
        )

    user_id = new_uuid()
    user = User(
        id=user_id,
        org_id=org_id,
        team_id=team.id,
        email=f"{github_login}@github.onboard",
        name=github_login,
        cognito_sub=cognito_sub,
        role=role,
    )
    db.add(user)

    # User identities: cognito + github
    cognito_identity = UserIdentity(
        id=new_uuid(),
        user_id=user_id,
        org_id=org_id,
        team_id=team.id,
        provider="cognito",
        provider_user_id=cognito_sub,
        provider_username=github_login,
        verification_method="oauth",
        verified_at=now,
    )
    db.add(cognito_identity)

    github_identity = UserIdentity(
        id=new_uuid(),
        user_id=user_id,
        org_id=org_id,
        team_id=team.id,
        provider="github",
        provider_user_id=github_id,
        provider_username=github_login,
        verification_method="oauth",
        verified_at=now,
    )
    db.add(github_identity)

    await db.commit()

    return AccessRequestResponse(
        status="approved",
        tenant_id=org_id,
        redirect="/dashboard",
    )


async def _pick_tenant_id(db: AsyncSession, base_slug: str, cognito_sub: str) -> str | None:
    """Pick a tenant ID derived from the GitHub login slug.

    - Validates the slug against TENANT_ID_PATTERN + RESERVED_TENANT_IDS.
    - If the slug is already taken in organizations or by a *different* user's
      pending request, return None — the caller returns a collision response
      asking an admin to resolve (we do NOT auto-append suffixes; admins should
      decide whether this is a legitimate same-org sign-up or a different
      user wanting their own tenant).
    - If the caller already has a pending request, the caller reuses it
      (idempotency) — this function is only reached when there's no prior
      request, so the only reason to return None is a genuine collision.
    """
    if base_slug in RESERVED_TENANT_IDS or not TENANT_ID_PATTERN.match(base_slug):
        # Very unlikely for real GitHub logins, but defend against weird inputs
        return None
    # Collision check 1: organizations
    existing_org = await db.get(Organization, base_slug)
    if existing_org is not None:
        return None
    # Collision check 2: someone else's pending request for the same tenant
    stmt = select(TenantAccessRequest).where(
        TenantAccessRequest.proposed_tenant_id == base_slug,
        TenantAccessRequest.status == "pending",
    )
    result = await db.execute(stmt)
    other = result.scalar_one_or_none()
    if other is not None and other.cognito_sub != cognito_sub:
        return None
    return base_slug


async def _create_memberships_for_matches(
    db: AsyncSession,
    user_id: str,
    matched_tenants: list[MatchedTenant],
    github_login: str,
) -> None:
    """Create TenantMembership rows for each matched tenant (D5 multi-membership).

    Issue #2953: For each matched org, determine the user's role (D4) and create
    a TenantMembership row. Handles D7 (re-login): skips orgs the user already
    has a membership for. Sets is_active=true only on the first membership if
    the user has no existing active membership.
    """
    # Fetch existing memberships for this user (D7: don't duplicate)
    stmt = select(TenantMembership).where(TenantMembership.user_id == user_id)
    result = await db.execute(stmt)
    existing = result.scalars().all()
    existing_tenant_ids = {m.tenant_id for m in existing}
    has_active = any(m.is_active for m in existing)

    first_new = True
    for mt in matched_tenants:
        if mt.org_id in existing_tenant_ids:
            continue  # D7: already a member, skip

        # D4: determine role from GitHub org membership
        role = await _determine_role_for_matched_user(github_login, mt.org_name, mt.install_id)

        # Set is_active on the first new membership only if user has no active one
        is_active = first_new and not has_active

        membership = TenantMembership(
            user_id=user_id,
            tenant_id=mt.org_id,
            role=role,
            is_active=is_active,
            joined_via="org_membership",
            github_org_id=mt.org_name,
        )
        db.add(membership)
        first_new = False

    # Flush to catch constraint violations within the transaction
    await db.flush()


async def sync_memberships_on_login(
    db: AsyncSession,
    user: User,
    github_login: str,
) -> None:
    """Sync org-tenant memberships for an existing user on login.

    Issue #3017: Second call site for the #2953 matcher. Called from
    get_access_status when user is already registered and has a GitHub
    identity. Finds any org tenants the user is verified to belong to
    (via GitHub API org-membership check) and creates TenantMembership
    rows for ones they don't already have (D7 idempotent — skips existing).

    Does NOT create org tenants, does NOT change active-tenant selection.
    """
    matched_tenants = await _find_matching_tenants_for_user(db, github_login)
    if not matched_tenants:
        return

    await _create_memberships_for_matches(
        db=db,
        user_id=user.id,
        matched_tenants=matched_tenants,
        github_login=github_login,
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Public routes (authenticated but no tenant required)
# ---------------------------------------------------------------------------


@router.get("/access/status", response_model=AccessStatusResponse)
async def get_access_status(
    request_in: Request,
    current_user: TokenContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AccessStatusResponse:
    """Check if the caller has a user row (registered) or needs to onboard.

    Issue #3017: For registered users, also syncs org-tenant memberships —
    creates TenantMembership rows for any org tenants the user belongs to
    (via GitHub org membership) that were created after their initial onboarding.
    """
    cognito_sub = current_user.user_id

    # Check if user already exists
    stmt = select(User).where(User.cognito_sub == cognito_sub)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if user is not None:
        # Issue #3017: Sync memberships for org tenants created post-onboarding.
        # Extract GitHub login from JWT claims first; fall back to Cognito lookup.
        # Access tokens don't carry custom:github_username (pre-token-gen Lambda
        # only injects org/team/dept/role/account_type). Fall back to Cognito
        # lookup by sub — same fallback _extract_github_identity uses for
        # submit_access_request. Issue #3027.
        try:
            claims = _decode_jwt_claims(request_in.headers.get("authorization"))
            github_login, _ = _extract_from_claims(claims)
            if not github_login:
                github_login, _ = _fetch_github_identity_from_cognito(cognito_sub)
            if github_login:
                await sync_memberships_on_login(db, user, github_login)
        except Exception:
            # Best-effort: identity resolution or membership sync failure
            # must not break login. Non-GitHub sessions (email/password admin)
            # will simply skip the sync.
            logger.warning(
                "membership sync failed for user=%s",
                cognito_sub,
                exc_info=True,
            )
        return AccessStatusResponse(status="registered")

    # Check if there's a pending request
    stmt = select(TenantAccessRequest).where(
        TenantAccessRequest.cognito_sub == cognito_sub,
        TenantAccessRequest.status == "pending",
    )
    result = await db.execute(stmt)
    pending = result.scalar_one_or_none()
    if pending is not None:
        return AccessStatusResponse(status="pending", request_id=pending.id)

    return AccessStatusResponse(status="new")


@router.post("/access/request", response_model=AccessRequestResponse)
async def submit_access_request(
    request_in: Request,
    payload: AccessRequestPayload,
    current_user: TokenContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AccessRequestResponse:
    """Submit an onboarding access request.

    The only field the user supplies is `motivation`. Tenant ID, provider,
    and provider_user_id are derived from the authenticated JWT — the user
    already proved who they are via GitHub + Cognito, so re-asking them
    is pure friction. Slug defaults to the GitHub login; collisions return
    a collision response so an admin can route the user into an existing
    tenant (invite flow) rather than silently suffixing.
    """
    # Preflight: check feature flag
    if not _v2_write_enabled():
        return AccessRequestResponse(
            status="unavailable",
            reason="USER_IDENTITY_INDEX_V2_WRITE=false, run #537 migration steps first",
        )

    cognito_sub = current_user.user_id

    # Idempotency first — if this user already has a pending request, reuse it.
    stmt = select(TenantAccessRequest).where(
        TenantAccessRequest.cognito_sub == cognito_sub,
        TenantAccessRequest.status == "pending",
    )
    result = await db.execute(stmt)
    dup_request = result.scalar_one_or_none()
    if dup_request is not None:
        return AccessRequestResponse(
            status="pending",
            request_id=dup_request.id,
            eta_hours=24,
        )

    # Derive GitHub identity — JWT claims first, Cognito AdminGetUser fallback
    claims = _decode_jwt_claims(request_in.headers.get("authorization"))
    github_login, github_id = _extract_github_identity(claims, cognito_sub)

    # Issue #2953 (D5): Before slug derivation, check if this user belongs to
    # ANY existing ADP tenants (via verified GitHub org membership).
    matched_tenants = await _find_matching_tenants_for_user(db, github_login)
    if matched_tenants:
        # Attach user to the FIRST matched tenant (home tenant — creates User row)
        first_match = matched_tenants[0]
        response = await _attach_user_to_existing_tenant(
            db=db,
            org_id=first_match.org_id,
            cognito_sub=cognito_sub,
            github_login=github_login,
            github_id=github_id,
        )

        # If the user was approved (User row created), create memberships for
        # ALL matched tenants (including the first one). D7: skips existing.
        if response.status == "approved":
            # Look up the just-created User row to get its ID
            stmt = select(User).where(User.cognito_sub == cognito_sub)
            result = await db.execute(stmt)
            user = result.scalar_one_or_none()
            if user is not None:
                await _create_memberships_for_matches(
                    db=db,
                    user_id=user.id,
                    matched_tenants=matched_tenants,
                    github_login=github_login,
                )
                await db.commit()

        return response

    # D6 fallback: No org matches — derive tenant ID from the GitHub login.
    # Reject on collision so an admin can decide whether this user belongs in
    # the existing tenant (invite flow) or needs different routing.
    base_slug = _slugify_tenant_id(github_login)
    tenant_id = await _pick_tenant_id(db, base_slug, cognito_sub)
    if tenant_id is None:
        return AccessRequestResponse(
            status="collision",
            reason=(
                f"A workspace named '{base_slug}' already exists or is being "
                f"requested by another user. Contact an administrator to "
                f"join an existing workspace."
            ),
        )

    # Create the request
    request = TenantAccessRequest(
        cognito_sub=cognito_sub,
        provider="github",
        provider_user_id=github_id,
        proposed_tenant_id=tenant_id,
        target_login=github_login,
        motivation=payload.motivation,
    )
    db.add(request)
    await db.commit()
    await db.refresh(request)

    # Auto-approve check (no-op today — TF var is empty — but kept for when
    # a future DB-backed allowlist ships).
    if _is_auto_approve(github_login):
        from src.admin.identity.identity_index_writer import IdentityIndexWriter

        writer = IdentityIndexWriter()
        approved_tenant_id = await approve_request(
            db=db,
            request=request,
            admin_sub="system:auto-approve",
            identity_writer=writer,
        )
        # D6: Create username-self membership for username-slug tenant
        stmt = select(User).where(User.cognito_sub == cognito_sub)
        result = await db.execute(stmt)
        user = result.scalar_one_or_none()
        if user is not None:
            existing_stmt = select(TenantMembership).where(
                TenantMembership.user_id == user.id,
                TenantMembership.tenant_id == approved_tenant_id,
            )
            existing_result = await db.execute(existing_stmt)
            if existing_result.scalar_one_or_none() is None:
                membership = TenantMembership(
                    user_id=user.id,
                    tenant_id=approved_tenant_id,
                    role=user.role or "member",
                    is_active=True,
                    joined_via="username_self",
                )
                db.add(membership)
                await db.commit()

        return AccessRequestResponse(
            status="approved",
            tenant_id=approved_tenant_id,
            redirect="/dashboard",
        )

    return AccessRequestResponse(
        status="pending",
        request_id=request.id,
        eta_hours=24,
    )


# ---------------------------------------------------------------------------
# Admin routes (platform_admin role required)
# ---------------------------------------------------------------------------


@router.get("/admin/access-requests", response_model=AdminAccessRequestList)
async def list_access_requests(
    admin: TokenContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminAccessRequestList:
    """List pending access requests for admin review."""
    stmt = select(TenantAccessRequest).where(TenantAccessRequest.status == "pending")
    result = await db.execute(stmt)
    requests = result.scalars().all()

    items = [
        AdminAccessRequestItem(
            id=r.id,
            cognito_sub=r.cognito_sub,
            provider=r.provider,
            provider_user_id=r.provider_user_id,
            proposed_tenant_id=r.proposed_tenant_id,
            target_login=r.target_login,
            motivation=r.motivation,
            status=r.status,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in requests
    ]
    return AdminAccessRequestList(requests=items)


@router.post("/admin/access-requests/{request_id}/approve")
async def approve_access_request(
    request_id: str,
    body: AdminDecisionPayload | None = None,
    admin: TokenContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Approve a pending access request (admin only)."""
    request = await db.get(TenantAccessRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Request not found")

    # Idempotent: already approved
    if request.status == "approved":
        return {"status": "approved", "tenant_id": request.proposed_tenant_id}

    from src.admin.identity.identity_index_writer import IdentityIndexWriter

    writer = IdentityIndexWriter()
    try:
        tenant_id = await approve_request(
            db=db,
            request=request,
            admin_sub=admin.user_id,
            identity_writer=writer,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return {"status": "approved", "tenant_id": tenant_id}


@router.post("/admin/access-requests/{request_id}/deny")
async def deny_access_request(
    request_id: str,
    body: AdminDecisionPayload | None = None,
    admin: TokenContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Deny a pending access request and delete the user's Cognito account."""
    request = await db.get(TenantAccessRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Request not found")

    # Get Cognito client for AdminDeleteUser
    cognito_client = None
    user_pool_id = _cognito_user_pool_id()
    if user_pool_id:
        try:
            import boto3

            cognito_client = boto3.client(
                "cognito-idp",
                region_name=os.environ.get("AWS_REGION", "us-east-1"),
            )
        except Exception:
            logger.warning("Could not create Cognito client for deny action")

    try:
        await deny_request(
            db=db,
            request=request,
            admin_sub=admin.user_id,
            decision_note=body.decision_note if body else None,
            cognito_client=cognito_client,
            user_pool_id=user_pool_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return {"status": "denied", "request_id": request_id}
