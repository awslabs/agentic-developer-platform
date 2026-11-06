"""Owner sessions: the login-equivalent context a verified Dev Box host run acts with (#9640).

Public routes authenticate Cognito logins. An owner session is not a Cognito token: it is
an HS256 gateway token in the revocable token table whose ``delegation`` claim names the
Dev Box host run acting for the owner (``dev-box:<box>:<host_run>``). Only the mint in
``src.crew.orchestration_read_routes`` issues one, after proving the box's signed instance
identity, its current host lease and the model token of that very run.

This module is the trusted path that lets such a token authenticate on the same public
routes a laptop login uses, with the same guarantees a login has:

* signature, expiry and database revocation (``TokenManager.validate_token``);
* canonical owner identity: the subject is the owner's login subject and must still be an
  unrevoked member of the token's workspace, resolved from the database on every request;
* current platform-admin authority: the mint recorded the owner's standing in the claim
  and every request re-decides it exactly as a login does (``apply_platform_admin_authority``:
  database revocations plus current IdP groups). The claim alone grants nothing;
* delegation is attribution, not permission: it is carried into the context so request
  logs and audit rows name the run acting for the owner, and nothing consults it to decide
  access.

Ordinary legacy exchange tokens carry no ``dev-box:`` delegation and never take this path;
``AuthService.validate_token`` keeps its restrictions for them. The model-invocation routes
authenticate through ``AuthService`` and treat an owner session as a legacy token, which is
why the box CLI never uses the session for model calls.
"""

from __future__ import annotations

import logging

import jwt
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.config import get_settings
from src.shared.schemas.auth import TokenContext

from .exceptions import TokenValidationError
from .token_manager import TokenManager

logger = logging.getLogger(__name__)

OWNER_SESSION_DELEGATION_PREFIX = "dev-box:"


def owner_session_delegation(token: str) -> str | None:
    """Return the delegation an owner-session candidate claims, without trusting the token.

    Cognito tokens are RS256 and carry no ``delegation``; a gateway HS256 token with a
    ``dev-box:`` delegation is the only thing that answers here. The caller still has to
    validate it with :func:`validate_owner_session`.
    """
    try:
        if jwt.get_unverified_header(token).get("alg") != "HS256":
            return None
        delegation = jwt.decode(token, options={"verify_signature": False}).get("delegation")
    except Exception:
        return None
    if isinstance(delegation, str) and delegation.startswith(OWNER_SESSION_DELEGATION_PREFIX):
        return delegation
    return None


def _denied(error: str, message: str, status: int = 401) -> HTTPException:
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return HTTPException(status_code=status, detail={"error": error, "message": message}, headers=headers)


async def validate_owner_session(token: str, db: AsyncSession | None = None) -> TokenContext:
    """Authenticate an owner session for a public route.

    Raises ``HTTPException`` 401 for a forged, expired or revoked session, 403 when the
    owner is no longer a member of the session's workspace, 503 when the gateway has no
    token secret.
    """
    if db is None:
        from src.shared.database import get_session_factory

        async with get_session_factory()() as session:
            return await validate_owner_session(token, session)

    from src.admin.membership_revocation import require_not_revoked_context
    from src.admin.platform_authority import apply_platform_admin_authority
    from src.shared.identity.workspaces import workspace_user

    try:
        manager = TokenManager(get_settings().token_secret_key)
    except ValueError:
        raise HTTPException(status_code=503, detail={"error": "auth_not_configured", "message": "Token signing is not configured"})
    try:
        context = await manager.validate_token(token, db)
    except TokenValidationError:
        raise _denied("owner_session_invalid", "Owner session is invalid, revoked, or expired")
    if not context.delegation.startswith(OWNER_SESSION_DELEGATION_PREFIX) or context.account_type != "human":
        raise _denied("owner_session_invalid", "Owner session is invalid, revoked, or expired")

    # Resolved like every login's membership: the subject's canonical row and its
    # verified placement links, so linked workspace accounts resolve to their member row.
    member = await workspace_user(db, context.user_id, context.org_id)
    if member is None:
        raise _denied("workspace_membership_denied", "The owner is no longer a member of this workspace", status=403)

    context = context.model_copy(
        update={
            # The subject stays the verified login subject the mint signed; the
            # workspace record supplies the current username and team, as a login's
            # membership resolution does. Authenticated as a login.
            "cognito_username": member.cognito_username or "",
            "team_id": member.team_id,
            "auth_source": "jwt",
        }
    )
    context._authenticated_subject_id = context.user_id
    # Admin standing is a current decision, never the claim: the same database
    # revocation + IdP group check every Cognito login goes through on every request.
    context = await apply_platform_admin_authority(context, db)
    await require_not_revoked_context(context, db)
    logger.debug("owner session authenticated user=%s delegation=%s is_admin=%s", context.user_id, context.delegation, context.is_admin)
    return context
