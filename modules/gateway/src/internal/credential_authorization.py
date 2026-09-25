"""Registry-based credential capability gate for sensitive delivery endpoints.

Issue #6050 (S12 continuation): replaces the header-based _check_agent_scope
helper with a single server-side check against the verified credential_scopes
on request.state.token_context.  That list is resolved from the agent_registry
DynamoDB entry at authentication time (auth_deps.py / agent_registry.py) and
cannot be influenced by any request header, body field or agent_id claim.

Missing identity, empty scopes and shared-key-only callers fail closed.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)


def require_credential_capability(request: Request, capability: str) -> None:
    """Raise 403 unless *capability* is in the caller's registry-granted scopes.

    Parameters
    ----------
    request:
        The current FastAPI request.  ``request.state.token_context`` is set by
        the IRSA authentication path; shared-secret callers have no context.
    capability:
        The exact scope string required, e.g. ``"credential:raw-read"`` or
        ``"credential:materialize"``.

    Raises
    ------
    HTTPException(403)
        When the caller has no verified identity, no credential_scopes, or the
        required capability is absent.  The response body includes the endpoint
        path and the missing capability but never secret values or raw bodies.
    """
    identity = getattr(request.state, "token_context", None)
    if identity is None:
        logger.info(
            "Credential capability denied: no verified identity on %s",
            request.url.path,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error": "insufficient_scope",
                "message": (f"Registry credential capability {capability!r} is required. No verified identity present."),
            },
        )

    granted: list[str] = getattr(identity, "credential_scopes", None) or []
    if capability not in granted:
        logger.info(
            "Credential capability denied: %s missing %r (granted=%s) on %s",
            identity.user_id,
            capability,
            granted,
            request.url.path,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error": "insufficient_scope",
                "message": (f"Registry credential capability {capability!r} is required for this operation."),
            },
        )
