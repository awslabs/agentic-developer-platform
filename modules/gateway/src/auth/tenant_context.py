"""Human tenant leases layered over unchanged, independently validated Cognito JWTs.

The envelope is not a login token: both the Cognito token and signed tenant lease
must validate, and current membership/team scope is re-read for every request.
No shared workspace selection or Cognito claim is changed.
"""

from datetime import UTC, datetime, timedelta

import jwt
from fastapi import HTTPException

from src.shared.config import get_settings
from src.shared.database import get_session_factory
from src.shared.identity.workspaces import memberships_for_login, primary_team_for_workspace

PREFIX = "adpctx1~"
ISSUER = "adp-tenant-context-v1"
AUDIENCE = "adp-human-tenant-context"
MAX_ENVELOPE = 32768


def split_token(token):
    if not token.startswith(PREFIX):
        return token, None
    parts = token.split("~")
    if len(parts) != 3 or len(token) > MAX_ENVELOPE or not parts[1] or not parts[2]:
        raise HTTPException(401, {"error": "invalid_tenant_context"})
    return parts[2], parts[1]


async def membership(db, context, tenant, expected_membership=None):
    if context.account_type != "human" or context.auth_source != "jwt":
        raise HTTPException(403, {"error": "tenant_context_unavailable"})
    try:
        _, memberships = await memberships_for_login(db, context.user_id, username=context.cognito_username)
        pair = memberships.get(tenant)
        if not pair:
            raise HTTPException(403, {"error": "tenant_not_visible"})
        user, row = pair
        membership_id = row.id if row else "legacy:" + user.id
        if expected_membership and expected_membership != membership_id:
            raise HTTPException(403, {"error": "tenant_membership_changed"})
        team = await primary_team_for_workspace(db, user, tenant)
    except ValueError:
        raise HTTPException(409, {"error": "tenant_identity_ambiguous"}) from None
    return user, membership_id, team


async def issue_context(db, context, tenant, expected_membership=None):
    settings = get_settings()
    if not settings.token_secret_key or not settings.cognito_user_pool_id:
        raise HTTPException(503, {"error": "tenant_context_unavailable"})
    user, membership_id, _ = await membership(db, context, tenant, expected_membership)
    now = datetime.now(UTC)
    expiry = min(context.expires_at, now + timedelta(minutes=15))
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "pool": settings.cognito_user_pool_id,
        "sub": context.user_id,
        "tenant": tenant,
        "user_id": user.id,
        "membership_id": membership_id,
        "iat": now,
        "exp": expiry,
    }
    return {
        "tenant_id": tenant,
        "identity": context.user_id,
        "canonical_user_id": user.id,
        "membership_id": membership_id,
        "context_token": jwt.encode(claims, settings.token_secret_key, algorithm="HS256"),
        "expires_at": expiry,
    }


async def apply_context(context, lease, db=None):
    if lease is None:
        return context
    settings = get_settings()
    try:
        claims = jwt.decode(
            lease,
            settings.token_secret_key,
            algorithms=["HS256"],
            issuer=ISSUER,
            audience=AUDIENCE,
            options={"require": ["sub", "pool", "tenant", "user_id", "membership_id", "exp", "iat"]},
        )
        if claims["sub"] != context.user_id or claims["pool"] != settings.cognito_user_pool_id:
            raise ValueError
        if not all(isinstance(claims[k], str) and claims[k] for k in ("tenant", "user_id", "membership_id")):
            raise ValueError
    except (jwt.InvalidTokenError, ValueError, TypeError):
        raise HTTPException(401, {"error": "invalid_tenant_context"}) from None
    if db is None:
        async with get_session_factory()() as session:
            return await _apply(session, context, claims)
    return await _apply(db, context, claims)


async def _apply(db, context, claims):
    user, _, team = await membership(db, context, claims["tenant"], claims["membership_id"])
    if user.id != claims["user_id"]:
        raise HTTPException(403, {"error": "tenant_membership_changed"})
    return context.model_copy(
        update={
            "org_id": claims["tenant"],
            "attributed_org_id": claims["tenant"],
            "team_id": team.id if team else "",
            "department_id": team.department_id if team else "",
            "expires_at": min(context.expires_at, datetime.fromtimestamp(claims["exp"], UTC)),
        }
    )
