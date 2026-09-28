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

from src.admin.access_control import AccessControl
from src.admin.audit_operation import AuditedAdminRoute, current_operation, mark_admin_effects
from src.admin.config import AdminRole, Permission
from src.admin.exceptions import InvalidScopeError
from src.admin.memberships import project_member_org_ids, upsert_tenant_membership
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.models.onboarding import TenantAccessRequest, TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.schemas.auth import TokenContext

from .approval import approve_request, attach_approved_member, deny_request
from .approval_decision import ApprovalDecision, derive_approval_decision, target_account_for_request
from .cli_contract import request_lock, submit_lock
from .cli_contract import router as cli_router
from .schemas import (
    RESERVED_TENANT_IDS,
    TENANT_ID_PATTERN,
    AccessRequestPayload,
    AccessRequestResponse,
    AccessStatusResponse,
    AdminAccessRequestItem,
    AdminAccessRequestList,
    AdminApprovalResponse,
    AdminDecisionPayload,
)
from .trusted_identity import (
    NO_LINKED_IDENTITY,
    TrustedGitHubIdentity,
    trusted_login_from_attributes,
)

logger = logging.getLogger(__name__)

router = APIRouter(route_class=AuditedAdminRoute)
router.include_router(cli_router)


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

    #5666 (A11): the login half is read through ``trusted_login_from_attributes``,
    the same single accessor the Cognito lookup uses, so the trusted attribute name
    has exactly one definition. A JWT claim cannot be forged (the token is signed),
    but reading it through one accessor is what stops a second, laxer source of the
    login reappearing here.
    """
    github_login = trusted_login_from_attributes(claims)
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
            Limit=2,
        )
        users = resp.get("Users", [])
        if len(users) != 1 or resp.get("PaginationToken"):
            return "", ""
        user = users[0]
        username = user.get("Username", "")
        attrs = {a["Name"]: a["Value"] for a in user.get("Attributes", [])}
        # #5666 (A11): the ``or attrs.get("name")`` fallback that used to sit here
        # is REMOVED, not relocated. ``name`` is self-writable by the SPA client, so
        # it let a user type another person's GitHub login and have the membership
        # matcher treat it as their own identity. See trusted_identity.py for the
        # full path from that attribute to a cross-tenant membership row.
        if attrs.get("sub") != cognito_sub:
            return "", ""
        github_login = trusted_login_from_attributes(attrs)
        match = re.fullmatch(r"github_([0-9]+)", username, re.IGNORECASE)
        github_id = match.group(1) if match else ""
        return github_login, github_id
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cognito AdminGetUser fallback failed for sub=%s: %s", cognito_sub, exc)
        return "", ""


def resolve_trusted_github_identity(claims: dict, cognito_sub: str) -> TrustedGitHubIdentity:
    """Resolve one complete Cognito broker identity for the authenticated subject.

    The separately decoded Authorization payload is not an authentication proof.
    Read both identity halves from the same provider record, validate its subject,
    and require the broker's immutable numeric username. Native/bootstrap users
    without that identity retain local access but cannot gain GitHub memberships.
    """
    login, numeric_id = _fetch_github_identity_from_cognito(cognito_sub)
    if not login or not re.fullmatch(r"[0-9]+", numeric_id):
        return NO_LINKED_IDENTITY
    return TrustedGitHubIdentity(login=login, numeric_id=numeric_id, linked=True)


def _extract_github_identity(claims: dict, cognito_sub: str) -> tuple[str, str]:
    """Return the complete broker identity bound to the authenticated subject.

    Raises HTTPException(400) when the authoritative record lacks either value.

    #5666 (A11): a thin adapter over :func:`resolve_trusted_github_identity` so the
    access-request route keeps its 400-on-absence contract while the trust decision
    lives in exactly one function.
    """
    identity = resolve_trusted_github_identity(claims, cognito_sub)
    github_login, github_id = identity.login, identity.numeric_id

    if not identity.complete:
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
    github_id: str,
) -> list[MatchedTenant]:
    """Find ALL existing ADP tenants this GitHub user is a verified member of.

    Issue #2953 (D5): Returns a list of MatchedTenant ordered by
    Organization.created_at (deterministic). Empty list if none match.

    Failure mode: any GitHub API error for a specific org -> log + skip that
    org (fail-closed per org, not per call).
    """
    from src.admin.connections.github_client import GitHubAppClient
    from src.admin.connections.service import _get_github_app_credentials

    if not re.fullmatch(r"[0-9]+", github_id):
        return []

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
                    token = await client.get_installation_token(int(install_id))
                    response = await client._http_client.get(
                        f"/orgs/{org.name}/memberships/{github_login}",
                        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
                    )
                    membership = response.json() if response.status_code == 200 else {}
                    is_member = membership.get("state") == "active" and str((membership.get("user") or {}).get("id", "")) == github_id
                    if is_member:
                        # Issue #2954: If this org is linked to a parent tenant,
                        # resolve to the parent (attach-forward-only rule 3).
                        resolved_org_id = org.parent_tenant_id or org.id
                        matched.append(
                            MatchedTenant(
                                org_id=resolved_org_id,
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

    # Issue #2954: Deduplicate by org_id — rule 3 can produce duplicates when a
    # user belongs to both the parent org and a linked child (both resolve to the
    # same parent_tenant_id). Without dedup, _create_memberships_for_matches
    # would hit a UniqueConstraint violation on (user_id, tenant_id).
    seen_ids: set[str] = set()
    deduped: list[MatchedTenant] = []
    for m in matched:
        if m.org_id not in seen_ids:
            seen_ids.add(m.org_id)
            deduped.append(m)
    return deduped


async def _determine_role_for_matched_user(
    github_login: str,
    org_login: str,
    installation_id: int,
    github_id: str,
) -> str:
    """Return 'org_admin' if the user is a GitHub org admin, else 'member'.

    Uses GET /orgs/{org}/memberships/{username} with installation token.
    """
    from src.admin.connections.github_client import GitHubAppClient
    from src.admin.connections.service import _get_github_app_credentials

    if not re.fullmatch(r"[0-9]+", github_id):
        return "member"
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
            membership = resp.json() if resp.status_code == 200 else {}
            if (
                membership.get("role") == "admin"
                and membership.get("state") == "active"
                and str((membership.get("user") or {}).get("id", "")) == github_id
            ):
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

    Issue #4018: the auto-approve tail (user + identities + membership) now lives
    in ``approval.attach_approved_member`` so the org-scoped admin approval route
    executes the identical write, with the *same* derived role. It is a shared
    executor rather than two copies precisely so the role can never diverge
    between the two paths.
    """
    org = await db.get(Organization, org_id)
    if org is None:
        # Should not happen — caller verified it exists. Fall through to new-tenant flow.
        return AccessRequestResponse(
            status="collision",
            reason="Matched org not found; please contact an administrator.",
        )

    # Determine role via GitHub API
    install_id = int(org.github_installation_ids[0]) if org.github_installation_ids else 0
    role = await _determine_role_for_matched_user(github_login, org.name, install_id, github_id)

    # Check approval policy
    auto_approve = org.member_approval_policy == "auto_approve_org_members"

    # Create the audit-trail row as pending; attach_approved_member is what flips
    # it to approved, so the two paths share one state transition.
    request = TenantAccessRequest(
        cognito_sub=cognito_sub,
        provider="github",
        provider_user_id=github_id,
        proposed_tenant_id=org_id,
        target_login=github_login,
        motivation=f"Auto-matched to org '{org.name}' via GitHub org membership",
        status="pending",
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

    await db.flush()
    try:
        await attach_approved_member(
            db,
            request,
            granted_role=role,
            decided_by="system:org-member-match",
            # This path is a login-time auto-match, not an admin decision, and
            # has never synced Cognito claims; keeping it off preserves that
            # behaviour (and keeps a Cognito round-trip off the sign-in path).
            sync_cognito_claims=False,
        )
    except ValueError:
        # No team in the org — can't attach. attach_approved_member validates
        # before adding any row, so the session still holds only the pending
        # request; commit it and let an admin decide.
        logger.warning("auto-approve attach failed for org=%s login=%s; leaving request pending", org_id, github_login)
        await db.commit()
        await db.refresh(request)
        return AccessRequestResponse(
            status="pending",
            request_id=request.id,
            eta_hours=24,
        )

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
    stmt = (
        select(TenantAccessRequest)
        .where(
            TenantAccessRequest.proposed_tenant_id == base_slug,
            TenantAccessRequest.status == "pending",
        )
        .order_by(TenantAccessRequest.created_at, TenantAccessRequest.id)
        .limit(1)
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
    github_id: str,
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
        target_org = await db.get(Organization, mt.org_id)
        if target_org is None or target_org.member_approval_policy != "auto_approve_org_members":
            continue

        # D4: determine role from GitHub org membership
        role = await _determine_role_for_matched_user(github_login, mt.org_name, mt.install_id, github_id)

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

    # Issue #3134's member_org_ids write-through used to live here. Issue #4849
    # moved it to each caller's post-commit point: this function only flushes, so
    # projecting here published memberships that a caller's later rollback erased.
    # Callers must call admin.memberships.project_member_org_ids after committing.


async def _proven_link_conflict(db: AsyncSession, cognito_sub: str, github_id: str) -> str | None:
    """Compare immutable provider identities across organization-local accounts.

    Missing historical rows do not substitute for proof: callers separately
    require a complete broker identity and matching provider membership response.
    A renamed/reused handle is not an account identifier.
    """
    from sqlalchemy import or_

    from src.shared.identity.verification import is_proven
    from src.shared.models.vault import UserIdentity

    if not re.fullmatch(r"[0-9]+", github_id):
        return "missing_immutable_identity"
    rows = (
        await db.execute(
            select(UserIdentity, User.cognito_sub)
            .join(User, User.id == UserIdentity.user_id)
            .where(
                UserIdentity.provider == "github",
                or_(
                    UserIdentity.provider_user_id == github_id,
                    User.cognito_sub == cognito_sub,
                ),
            )
        )
    ).all()
    for identity, linked_sub in rows:
        if not is_proven(identity.verification_method):
            continue
        if identity.provider_user_id == github_id and linked_sub not in (None, "", cognito_sub):
            return "provider_identity_proven_for_another_subject"
        if identity.provider_user_id == github_id and not linked_sub:
            placements = (
                await db.scalars(
                    select(UserIdentity.provider_user_id).where(
                        UserIdentity.user_id == identity.user_id,
                        UserIdentity.provider == "cognito",
                        UserIdentity.verification_method == "org_placement",
                    )
                )
            ).all()
            if any(subject != cognito_sub for subject in placements):
                return "provider_identity_placed_under_another_subject"
        if linked_sub == cognito_sub and identity.provider_user_id != github_id:
            return "subject_has_different_proven_provider_identity"
    return None


async def sync_memberships_on_login(
    db: AsyncSession,
    user: User,
    github_login: str,
    *,
    github_id: str,
    resolved_for_sub: str,
) -> None:
    """Add memberships only for the authenticated subject's immutable identity.

    Existing memberships remain untouched when proof is unavailable. The common
    conflict check supports compatible organization-local accounts; the matcher
    requires GitHub's active membership response to confirm the same numeric ID.
    """
    if user.cognito_sub != resolved_for_sub:
        logger.error(
            "membership_sync_refused reason=identity_user_mismatch user=%s resolved_for=%s",
            user.cognito_sub,
            resolved_for_sub,
        )
        return

    conflict = await _proven_link_conflict(db, resolved_for_sub, github_id)
    if conflict is not None:
        logger.error(
            "membership_sync_refused reason=%s user=%s login=%s",
            conflict,
            user.cognito_sub,
            github_login,
        )
        return

    matched_tenants = await _find_matching_tenants_for_user(db, github_login, github_id)
    if not matched_tenants:
        return

    await _create_memberships_for_matches(
        db=db,
        user_id=user.id,
        matched_tenants=matched_tenants,
        github_login=github_login,
        github_id=github_id,
    )
    await db.commit()
    # Issue #4849: project post-commit (see project_member_org_ids' docstring).
    await project_member_org_ids(db, user_id=user.id)


# ---------------------------------------------------------------------------
# Public routes (authenticated but no tenant required)
# ---------------------------------------------------------------------------


def _onboarding_result(result, *, provider_complete: bool = True, request_id: str | None = None):
    """Stage the durable result without logging motivation, tokens, or profiles."""
    operation = current_operation.get()
    if operation is not None:
        data = result if isinstance(result, dict) else result.model_dump()
        status = data.get("status", "")
        operation.target_org = data.get("tenant_id") or operation.target_org
        provider_complete = provider_complete and (operation.terminal or {}).get("outcome") != "reconciliation_required"
        operation.terminal = {
            "event_type": "admin_" + operation.action,
            "outcome": "reconciliation_required" if not provider_complete else ("denied" if status in {"unavailable", "collision"} else "success"),
            "target_type": "access_request",
            "target_id": request_id or data.get("request_id") or operation.route,
            "org_id": operation.target_org,
            "extra": {"request_status": status, "granted_role": data.get("granted_role")},
        }
    return result


@router.get("/access/status", response_model=AccessStatusResponse)
async def get_access_status(
    request_in: Request,
    current_user: TokenContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    target_tenant: str | None = None,
) -> AccessStatusResponse:
    """Check if the caller has a user row (registered) or needs to onboard.

    Issue #3017: For registered users, also syncs org-tenant memberships —
    creates TenantMembership rows for any org tenants the user belongs to
    (via GitHub org membership) that were created after their initial onboarding.
    """
    cognito_sub = current_user.user_id
    if target_tenant is not None:
        from src.shared.identity.workspaces import workspace_user

        account = await workspace_user(db, cognito_sub, target_tenant)
        membership = None
        if account is not None:
            membership = await db.scalar(
                select(TenantMembership).where(TenantMembership.user_id == account.id, TenantMembership.tenant_id == target_tenant)
            )
        if membership is not None and getattr(membership, "revoked_at", None) is None:
            return AccessStatusResponse(status="member", tenant_id=target_tenant, membership_role=membership.role)
        pending = await db.scalar(
            select(TenantAccessRequest)
            .where(
                TenantAccessRequest.cognito_sub == cognito_sub,
                TenantAccessRequest.proposed_tenant_id == target_tenant,
                TenantAccessRequest.status == "pending",
            )
            .order_by(TenantAccessRequest.created_at)
            .limit(1)
        )
        return AccessStatusResponse(
            status="pending" if pending else "no_membership", request_id=pending.id if pending else None, tenant_id=target_tenant
        )

    # Check if user already exists
    stmt = select(User).where(User.cognito_sub == cognito_sub).order_by(User.org_id, User.id).limit(1)
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
            # #5666 (A11): one resolver, no string fallback. This call site used to
            # inline its own two-step lookup, which is how it inherited the
            # self-writable ``name`` fallback; routing it through
            # resolve_trusted_github_identity means the trust decision cannot differ
            # between here and the access-request route.
            identity = resolve_trusted_github_identity(claims, cognito_sub)
            if identity.linked:
                await sync_memberships_on_login(db, user, identity.login, github_id=identity.numeric_id, resolved_for_sub=cognito_sub)
            else:
                # Issue #3031: greppable event for post-deploy smoke diagnostics.
                # #5666 (A11): an unlinked session is an explicit no-op that creates
                # no membership row in ANY organization, rather than a best-effort
                # match on whatever string was available.
                logger.info(
                    "membership_sync_skipped reason=no_verified_identity user=%s",
                    cognito_sub,
                )
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
    stmt = (
        select(TenantAccessRequest)
        .where(
            TenantAccessRequest.cognito_sub == cognito_sub,
            TenantAccessRequest.status == "pending",
        )
        .order_by(TenantAccessRequest.created_at, TenantAccessRequest.id)
        .limit(1)
    )
    result = await db.execute(stmt)
    pending = result.scalar_one_or_none()
    if pending is not None:
        return AccessStatusResponse(status="pending", request_id=pending.id)

    return AccessStatusResponse(status="new")


@router.post("/access/request", response_model=AccessRequestResponse, dependencies=[Depends(submit_lock)])
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
        return _onboarding_result(
            AccessRequestResponse(
                status="unavailable",
                reason="USER_IDENTITY_INDEX_V2_WRITE=false, run #537 migration steps first",
            )
        )

    cognito_sub = current_user.user_id
    if payload.target_tenant is not None:
        target = await db.get(Organization, payload.target_tenant)
        if target is None:
            raise HTTPException(404, "Target organization not found; use platform onboarding for a new organization")
        existing = await db.scalar(
            select(TenantAccessRequest)
            .where(
                TenantAccessRequest.cognito_sub == cognito_sub,
                TenantAccessRequest.proposed_tenant_id == target.id,
                TenantAccessRequest.status == "pending",
            )
            .order_by(TenantAccessRequest.created_at)
            .limit(1)
        )
        if existing:
            return _onboarding_result(AccessRequestResponse(status="pending", request_id=existing.id, tenant_id=target.id))
        claims = _decode_jwt_claims(request_in.headers.get("authorization"))
        login, provider_id = _extract_github_identity(claims, cognito_sub)
        if await _proven_link_conflict(db, cognito_sub, provider_id):
            raise HTTPException(409, "GitHub identity requires administrator review")
        mark_admin_effects()
        row = TenantAccessRequest(
            cognito_sub=cognito_sub,
            provider="github",
            provider_user_id=provider_id,
            proposed_tenant_id=target.id,
            target_login=login,
            motivation=payload.motivation,
            status="pending",
        )
        db.add(row)
        await db.flush()
        return _onboarding_result(AccessRequestResponse(status="pending", request_id=row.id, tenant_id=target.id))

    # Idempotency first — if this user already has a pending request, reuse it.
    stmt = (
        select(TenantAccessRequest)
        .where(
            TenantAccessRequest.cognito_sub == cognito_sub,
            TenantAccessRequest.status == "pending",
        )
        .order_by(TenantAccessRequest.created_at, TenantAccessRequest.id)
        .limit(1)
    )
    result = await db.execute(stmt)
    dup_request = result.scalar_one_or_none()
    if dup_request is not None:
        return _onboarding_result(
            AccessRequestResponse(
                status="pending",
                request_id=dup_request.id,
                eta_hours=24,
            )
        )

    # Derive GitHub identity — JWT claims first, Cognito AdminGetUser fallback
    claims = _decode_jwt_claims(request_in.headers.get("authorization"))
    github_login, github_id = _extract_github_identity(claims, cognito_sub)
    if await _proven_link_conflict(db, cognito_sub, github_id):
        raise HTTPException(status_code=409, detail="GitHub identity requires administrator review")

    # Issue #2953 (D5): Before slug derivation, check if this user belongs to
    # ANY existing ADP tenants (via verified GitHub org membership).
    matched_tenants = await _find_matching_tenants_for_user(db, github_login, github_id)
    if matched_tenants:
        mark_admin_effects()
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
            from src.shared.identity.workspaces import workspace_user

            user = await workspace_user(db, cognito_sub, first_match.org_id, username=f"github_{github_id}")
            if user is not None:
                await _create_memberships_for_matches(
                    db=db,
                    user_id=user.id,
                    matched_tenants=matched_tenants,
                    github_login=github_login,
                    github_id=github_id,
                )
                await db.commit()
                # Issue #4849: project post-commit.
                await project_member_org_ids(db, user_id=user.id)

        return _onboarding_result(response)

    # D6 fallback: No org matches — derive tenant ID from the GitHub login.
    # Reject on collision so an admin can decide whether this user belongs in
    # the existing tenant (invite flow) or needs different routing.
    base_slug = _slugify_tenant_id(github_login)
    tenant_id = await _pick_tenant_id(db, base_slug, cognito_sub)
    if tenant_id is None:
        return _onboarding_result(
            AccessRequestResponse(
                status="collision",
                reason=(
                    f"A workspace named '{base_slug}' already exists or is being "
                    f"requested by another user. Contact an administrator to "
                    f"join an existing workspace."
                ),
            )
        )

    # Create the request
    mark_admin_effects()
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
        stmt = select(User).where(User.cognito_sub == cognito_sub, User.org_id == approved_tenant_id)
        result = await db.execute(stmt)
        user = result.scalar_one_or_none()
        if user is not None:
            # Issue #4006: approve_request now writes this membership itself, so
            # this is normally a no-op; kept (as an upsert) so the username-slug
            # tenant still gets a row if the approval path ever stops writing one.
            # De-nested from the old "only if absent" branch so the #3134
            # write-through below always runs.
            await upsert_tenant_membership(
                db,
                user_id=user.id,
                tenant_id=approved_tenant_id,
                role=user.role or "member",
                joined_via="username_self",
            )
            await db.commit()

            # Issue #3134: Write-through member_org_ids after auto-approve.
            # Issue #4849: consolidated into admin/memberships.py.
            await project_member_org_ids(db, user_id=user.id, writer=writer)

        return _onboarding_result(
            AccessRequestResponse(
                status="approved",
                tenant_id=approved_tenant_id,
                redirect="/dashboard",
            )
        )

    return _onboarding_result(
        AccessRequestResponse(
            status="pending",
            request_id=request.id,
            eta_hours=24,
        )
    )


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------
#
# Issue #4018: these three routes used to be gated on ``require_admin``, i.e.
# PLATFORM admin only (``auth/dependencies.py`` deliberately excludes org_admin
# from ``is_admin``, per #3981). Since the first approved user of any org is
# created as org_admin, that org's own administrator could neither see nor drain
# their own pending queue — every approval funnelled through the seeded
# platform-admin account. They are now gated on ``Permission.USER_MANAGE`` with
# an org-scope check, which admits platform_admin (all orgs) and org_admin
# (their own org only) and still rejects dept_admin/member.
#
# TWO REQUEST CLASSES, opposite handling — the load-bearing distinction here:
#
#   (A) New-tenant requests (``submit_access_request``'s D6 fallback). By
#       construction ``_pick_tenant_id`` returns None when the slug already
#       exists in ``organizations``, so a class-A request's proposed_tenant_id
#       names an org that DOES NOT EXIST YET; ``approve_request`` is what creates
#       it. Nobody should be able to approve the creation of a tenant they don't
#       own, so these stay PLATFORM-ADMIN-ONLY and are hidden from an org admin's
#       list entirely.
#
#   (B) Join-existing-org requests (``_attach_user_to_existing_tenant`` under
#       ``member_approval_policy != "auto_approve_org_members"``). Here
#       proposed_tenant_id is a REAL, existing org, so it can be compared against
#       the caller's own tenant. This class — and only this class — is what an
#       org admin may decide.
#
# The class is derived server-side by looking the org up, never taken from the
# client. ``_is_existing_org`` below is that test.


async def _get_access_control(db: AsyncSession = Depends(get_db)) -> AccessControl:
    """Provide a per-request AccessControl instance.

    Mirrors ``admin/routes.py`` / ``admin/tenants/routes.py``. Per-request (not
    module-level) so the TTL role cache inside AccessControl cannot serve a role
    resolved for one caller to another.
    """
    return AccessControl(db)


async def _resolve_decider_scope(
    access: AccessControl,
    caller: TokenContext,
) -> tuple[AdminRole, str | None]:
    """Resolve the caller's role and the single org they may decide requests for.

    Authority comes from the caller's resolved ``tenant_memberships`` row via
    ``AccessControl.get_user_role`` (#3987/#3998) — never from a token claim
    (``custom:role`` reduces to ``is_admin`` only, per #3981).

    Returns ``(role, allowed_org_id)``; ``allowed_org_id`` is None for a platform
    admin, who is unscoped.

    Known limitation (#4018): a user who administers several orgs resolves to
    their single *active* tenant, so they see one queue at a time and must switch
    tenants to drain another. Tracked as a follow-up rather than widening the
    predicate, which would make the scope check multi-valued.
    """
    role, allowed_org_id, _ = await access.get_user_role(caller)
    if role == AdminRole.PLATFORM_ADMIN:
        return role, None
    return role, allowed_org_id


async def _is_existing_org(db: AsyncSession, tenant_id: str) -> bool:
    """True when ``tenant_id`` names an org that already exists (a class-B request).

    A class-A (new-tenant) request names an org that does not exist yet, so this
    is False for it — which is what keeps class A platform-admin-only.
    """
    return (await db.get(Organization, tenant_id)) is not None


@router.get("/admin/access-requests", response_model=AdminAccessRequestList)
async def list_access_requests(
    admin: TokenContext = Depends(get_current_user),
    access: AccessControl = Depends(_get_access_control),
    db: AsyncSession = Depends(get_db),
) -> AdminAccessRequestList:
    """List pending access requests the caller may decide.

    Platform admin sees every pending request (both classes). An org admin sees
    only class-B (join-existing-org) requests targeting their own org — the class
    they are actually able to approve, so the list never shows a row that would
    403 on click.
    """
    await access.check_permission(admin, Permission.USER_MANAGE)
    role, allowed_org_id = await _resolve_decider_scope(access, admin)

    stmt = select(TenantAccessRequest).where(TenantAccessRequest.status == "pending")
    if role != AdminRole.PLATFORM_ADMIN:
        # check_permission already rejected an empty scope for USER_MANAGE (it is
        # in _ORG_SCOPED_PERMISSIONS), so allowed_org_id is truthy here. The
        # belt-and-braces guard keeps a future change to that frozenset from
        # silently turning this into an unfiltered read.
        if not allowed_org_id:
            return AdminAccessRequestList(requests=[])
        stmt = stmt.where(TenantAccessRequest.proposed_tenant_id == allowed_org_id)

    result = await db.execute(stmt)
    requests = result.scalars().all()

    if role != AdminRole.PLATFORM_ADMIN:
        # Class filter: only requests whose target org already exists. For a
        # non-platform caller allowed_org_id IS an existing org, so this is
        # normally a no-op — kept so the list can never expose a class-A request
        # that happens to share a slug with the caller's org.
        requests = [r for r in requests if await _is_existing_org(db, r.proposed_tenant_id)]

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


async def _authorize_decision(
    access: AccessControl,
    db: AsyncSession,
    caller: TokenContext,
    request: TenantAccessRequest,
) -> tuple[AdminRole, str | None]:
    """Authorize a caller to approve/deny ``request``, or raise 403.

    Enforced server-side on BOTH decision routes; the client never supplies the
    scope. Raises ``AccessDeniedError`` / ``InvalidScopeError`` — both
    ``BedrockGatewayError`` 403s handled globally in ``app.py``, so callers need
    no try/except.

    Both decisions authorize the stored request's target tenant before changing
    its status. Denial never deletes a global Cognito login.
    """
    # target_org_id makes check_permission itself enforce the cross-tenant
    # boundary; USER_MANAGE is in _ORG_SCOPED_PERMISSIONS, so a caller with an
    # empty scope is rejected rather than short-circuiting the comparison (#3989).
    await access.check_permission(caller, Permission.USER_MANAGE, target_org_id=request.proposed_tenant_id)
    role, allowed_org_id = await _resolve_decider_scope(access, caller)

    if role != AdminRole.PLATFORM_ADMIN:
        # Class A (new-tenant) requests name an org that does not exist yet.
        # Creating a brand-new tenant is a platform-admin act, so refuse even
        # when the slug happens to match the caller's own org id.
        if not await _is_existing_org(db, request.proposed_tenant_id):
            raise InvalidScopeError(
                message="Only a platform administrator can approve a request for a new organization",
                allowed_scope=f"org:{allowed_org_id}",
                requested_scope=f"new-org:{request.proposed_tenant_id}",
            )

    return role, allowed_org_id


@router.post("/admin/access-requests/{request_id}/approve", response_model=AdminApprovalResponse, dependencies=[Depends(request_lock)])
async def approve_access_request(
    request_id: str,
    body: AdminDecisionPayload | None = None,
    admin: TokenContext = Depends(get_current_user),
    access: AccessControl = Depends(_get_access_control),
    db: AsyncSession = Depends(get_db),
) -> AdminApprovalResponse:
    """Approve a pending access request.

    Platform admin: unchanged — ``approve_request`` creates the new tenant for a
    class-A request (or heals an already-existing one idempotently).

    Org admin: class-B only, own org only, via ``attach_approved_member``.
    ``approve_request`` is NOT used here — its existing-org branch hardcodes
    ``role="org_admin"``, which would turn every member approval into a silent
    co-admin grant. The granted role is derived server-side from GitHub org
    membership instead (admin → org_admin, otherwise member).
    """
    request = await db.get(TenantAccessRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Request not found")

    operation = current_operation.get()
    if operation is not None:
        operation.target_org = request.proposed_tenant_id
    role, _ = await _authorize_decision(access, db, admin, request)

    # Idempotent: already approved. Report the role actually held in the target org
    # rather than re-deriving it — the grant already happened and this must not look
    # like a fresh decision.
    if request.status == "approved":
        return _onboarding_result(
            AdminApprovalResponse(
                status="approved",
                tenant_id=request.proposed_tenant_id,
                granted_role=await _granted_role_in_org(db, request),
            ),
            request_id=request_id,
        )

    # #5666 (A11): ONE derivation for BOTH approver branches. The platform-admin
    # branch below previously skipped role derivation entirely and inherited
    # approve_request's hardcoded "org_admin", so a platform admin approving a
    # request to JOIN an existing org minted a co-administrator of it. The guards
    # (require_assignable_role + require_modifiable_target) run inside this call, so
    # neither branch can reach a write without them.
    decision = await derive_approval_decision(
        db,
        access,
        admin,
        request,
        role_for_existing_org=_determine_role_for_matched_user,
    )

    if body and (
        (body.expected_role and body.expected_role != decision.granted_role)
        or (body.expected_scope and body.expected_scope != decision.request_class.value)
    ):
        raise HTTPException(409, "Proposed grant changed; review this request again")

    mark_admin_effects()
    if role != AdminRole.PLATFORM_ADMIN:
        tenant_id = await _approve_as_org_admin(db, admin, request, decision)
        _log_decision(admin, role, request, "approve")
        return _onboarding_result(
            AdminApprovalResponse(status="approved", tenant_id=tenant_id, granted_role=decision.granted_role), request_id=request_id
        )

    from src.admin.identity.identity_index_writer import IdentityIndexWriter

    writer = IdentityIndexWriter()
    try:
        if decision.creates_organization:
            tenant_id = await approve_request(
                db=db,
                request=request,
                admin_sub=admin.user_id,
                identity_writer=writer,
                granted_role=decision.granted_role,
            )
        elif await _has_user_in_org(db, request):
            # The org AND a users row for this sub already exist — a partially
            # completed earlier approval. approve_request's existing-org branch is
            # the heal path for exactly this: it upserts the membership and re-syncs
            # claims without minting a second users row. It now heals with the
            # DERIVED role instead of a hardcoded org_admin.
            tenant_id = await approve_request(
                db=db,
                request=request,
                admin_sub=admin.user_id,
                identity_writer=writer,
                granted_role=decision.granted_role,
            )
        else:
            # An existing org is the JOIN_EXISTING class whichever branch approves
            # it, so a platform admin now takes the same executor an org admin does.
            # approve_request's existing-org branch only heals a membership; it never
            # created the users/identity rows a first-time joiner needs, so a platform
            # admin approving a genuine join used to leave an authority-less principal
            # — or, worse, promote them to co-admin of the org they were joining.
            tenant_id = await attach_approved_member(
                db,
                request,
                granted_role=decision.granted_role,
                decided_by=admin.user_id,
            )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    _log_decision(admin, role, request, "approve")
    return _onboarding_result(
        AdminApprovalResponse(status="approved", tenant_id=tenant_id, granted_role=decision.granted_role), request_id=request_id
    )


async def _granted_role_in_org(db: AsyncSession, request: TenantAccessRequest) -> str:
    """The role this requester actually holds in the target org — #5666 (A11).

    Read from ``tenant_memberships``, the row ``access_control`` resolves org
    permissions from, so the reported role is the one that is really in force rather
    than a re-derivation that might now differ.
    """
    user = await target_account_for_request(db, request)
    if user is None:
        return ""
    role = await db.scalar(
        select(TenantMembership.role).where(
            TenantMembership.user_id == user.id,
            TenantMembership.tenant_id == request.proposed_tenant_id,
        )
    )
    return role or ""


async def _has_user_in_org(db: AsyncSession, request: TenantAccessRequest) -> bool:
    """True when a users row for this requester already exists in the target org.

    Scoped to the org (#5666 A11): an unscoped ``cognito_sub`` lookup would report a
    user row belonging to a DIFFERENT tenant and send a genuine first-time join down
    the heal path, which never creates the rows that joiner needs.
    """
    user = await target_account_for_request(db, request)
    return user is not None


async def _approve_as_org_admin(
    db: AsyncSession,
    caller: TokenContext,
    request: TenantAccessRequest,
    decision: ApprovalDecision,
) -> str:
    """Execute a class-B approval as an org admin, with the already-derived role.

    #5666 (A11): the derivation and both ceiling guards moved into
    ``derive_approval_decision``, which the route calls for BOTH approver branches.
    This function now only executes, so the org-admin and platform-admin paths cannot
    reach a different role for the same request.
    """
    try:
        return await attach_approved_member(
            db,
            request,
            granted_role=decision.granted_role,
            decided_by=caller.user_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


def _log_decision(caller: TokenContext, role: AdminRole, request: TenantAccessRequest, decision: str) -> None:
    """Emit a greppable audit line for an access-request decision.

    ``decided_by`` stores only the Cognito sub; with two classes of approver now
    able to act, "who decided this and under what authority" must be answerable
    from logs alone.
    """
    logger.info(
        "access_request_decided actor=%s actor_role=%s request_id=%s tenant=%s decision=%s",
        caller.user_id,
        role.value,
        request.id,
        request.proposed_tenant_id,
        decision,
    )


@router.post("/admin/access-requests/{request_id}/deny", dependencies=[Depends(request_lock)])
async def deny_access_request(
    request_id: str,
    body: AdminDecisionPayload | None = None,
    admin: TokenContext = Depends(get_current_user),
    access: AccessControl = Depends(_get_access_control),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Deny this tenant access request while preserving the global login."""
    request = await db.get(TenantAccessRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Request not found")

    # Authorization governs this request's tenant, never global account deletion.
    operation = current_operation.get()
    if operation is not None:
        operation.target_org = request.proposed_tenant_id
    role, _ = await _authorize_decision(access, db, admin, request)

    if body and (body.expected_role or body.expected_scope):
        decision = await derive_approval_decision(
            db,
            access,
            admin,
            request,
            role_for_existing_org=_determine_role_for_matched_user,
        )
        if (body.expected_role and body.expected_role != decision.granted_role) or (
            body.expected_scope and body.expected_scope != decision.request_class.value
        ):
            raise HTTPException(409, "Proposed grant changed; review this request again")

    try:
        mark_admin_effects()
        await deny_request(
            db=db,
            request=request,
            admin_sub=admin.user_id,
            decision_note=body.decision_note if body else None,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    _log_decision(admin, role, request, "deny")
    return _onboarding_result({"status": "denied", "request_id": request_id})
