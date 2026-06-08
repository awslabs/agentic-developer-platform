"""Onboarding API handler — access status, request, admin approve/deny.

Issue #538: Self-serve onboarding flow routes.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user, require_admin
from src.shared.database import get_db
from src.shared.models.onboarding import TenantAccessRequest
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


async def _find_matching_tenant_for_user(
    db: AsyncSession,
    github_login: str,
) -> str | None:
    """Find an existing ADP tenant that this GitHub user is a verified member of.

    Iterates orgs with non-empty github_installation_ids. For each, calls
    GitHub's org membership API using the App's installation token. Returns
    the org_id of the first matched tenant, or None.

    Failure mode: any GitHub API error -> log + skip that org (fail-closed,
    do not auto-attach when verification fails).
    """
    from src.admin.connections.github_client import GitHubAppClient
    from src.admin.connections.service import _get_github_app_credentials

    # Fetch all orgs and filter in Python — small N, no perf concern
    stmt = select(Organization)
    candidates = (await db.execute(stmt)).scalars().all()
    candidates = [o for o in candidates if o.github_installation_ids]
    if not candidates:
        return None

    app_id, private_key = _get_github_app_credentials()
    if not app_id or not private_key:
        logger.warning("GitHub App credentials not configured; cannot verify org membership")
        return None

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
                        return org.id
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
    return None


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


# ---------------------------------------------------------------------------
# Public routes (authenticated but no tenant required)
# ---------------------------------------------------------------------------


@router.get("/access/status", response_model=AccessStatusResponse)
async def get_access_status(
    current_user: TokenContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AccessStatusResponse:
    """Check if the caller has a user row (registered) or needs to onboard."""
    cognito_sub = current_user.user_id

    # Check if user already exists
    stmt = select(User).where(User.cognito_sub == cognito_sub)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if user is not None:
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

    # Issue #719: Before slug derivation, check if this user belongs to an
    # existing ADP tenant (via verified GitHub org membership).
    matched_org_id = await _find_matching_tenant_for_user(db, github_login)
    if matched_org_id is not None:
        return await _attach_user_to_existing_tenant(
            db=db,
            org_id=matched_org_id,
            cognito_sub=cognito_sub,
            github_login=github_login,
            github_id=github_id,
        )

    # Derive tenant ID from the GitHub login; reject on collision so an
    # admin can decide whether this user belongs in the existing tenant
    # (invite flow, not this flow) or needs different routing.
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
