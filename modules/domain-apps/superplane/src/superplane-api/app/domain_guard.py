"""The single global guard that enforces the endpoint inventory.

Issue #5055 (U14) — the "every domain endpoint and transport" half of R5/R6.

WHY ONE GLOBAL DEPENDENCY, NOT A ``Depends`` PER HANDLER
--------------------------------------------------------
Adding a dependency to each of ~40 handlers satisfies "every endpoint" only for
as long as nobody forgets one — and a forgotten one is invisible, because a route
with no auth dependency looks exactly like a route that was considered and found
not to need any. That is not hypothetical here: twelve ``/api/v1/research/*``
routes and two ``/internal/*`` routes reached production with no authentication
at all, and nothing in the code distinguishes them from routes that are public on
purpose.

Registering one dependency on the ``FastAPI`` app inverts the default. Every
request is classified against ``app/endpoint_inventory.py`` first, and a route
with no recorded decision is REFUSED. So the unsafe state becomes "route missing
from the inventory", which fails closed at runtime and fails
``tests/test_auth.py`` in CI, rather than shipping reachable.

WHY NOT A STARLETTE MIDDLEWARE
------------------------------
Measured, not assumed: a ``BaseHTTPMiddleware`` runs BEFORE routing, so
``request.scope["route"]`` is ``None`` and ``path_params`` is unset for every
request. A middleware-based guard would therefore have been unable to identify
the route it was protecting and would have passed everything through — enforcement
that reads as present and matches nothing. A router-level dependency runs after
routing, so it sees the matched template and its path parameters, and it can take
an ``AsyncSession``, which the per-operation grant check requires.

WHAT IT ENFORCES, IN ORDER
--------------------------
1. Client-supplied identity headers are stripped and the sanitized mapping is
   published, before any decision is made.
2. The route is classified. Unrecorded means refused.
3. Public routes proceed. Internal routes are left to their machine
   authenticators: the shared-token check for the existing internal surface,
   or the observation receiver's workspace credential and HMAC verification.
4. Domain routes require a verified access token admitted by U9's policy, then a
   server-held grant re-read for THIS operation — workspace-scoped against the
   workspace resolved from the path, organization-scoped against the separate
   organization path that a workspace grant deliberately cannot satisfy.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth as domain_auth
from app.database import get_session
from app.endpoint_inventory import (
    WORKSPACE_PATH_PARAM,
    RouteClass,
    RouteNotInventoried,
    Scope,
    classify,
)
from superplane_auth.policy import Permission, strip_identity_headers

logger = logging.getLogger(__name__)


async def enforce_domain_authorization(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(
        domain_auth.domain_bearer
    ),
    db: AsyncSession = Depends(get_session),
) -> None:
    """Classify the matched route and enforce what it requires.

    Registered once on the app. Returns ``None`` on success; raises 401/403
    otherwise. Never returns a partially-authorized state: a caller that reaches
    a handler has been admitted and granted for that specific operation.
    """
    # Step 1 — strip client-supplied identity before anything reads it. Done for
    # every request, enforced or not, and published on request.state so handlers
    # take the sanitized mapping rather than re-reading request.headers.
    request.state.safe_headers = strip_identity_headers(request.headers)

    route = request.scope.get("route")
    template = getattr(route, "path", None)
    if template is None:
        # Unmatched path: Starlette will answer 404/405. There is no operation
        # to authorize, and inventing a 403 would turn every typo into an auth
        # error.
        return None

    method = request.method.upper()

    # Step 2 — classify. Unrecorded routes fail closed.
    try:
        route_class, requirement = classify(method, template)
    except RouteNotInventoried:
        logger.error(
            "refusing %s %s: no recorded authorization decision", method, template
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="endpoint has no recorded authorization decision",
        ) from None

    # Step 3 — classes that do not use the domain path.
    if route_class is RouteClass.PUBLIC:
        return None
    if route_class is RouteClass.INTERNAL:
        # Machine authentication stays with each internal endpoint family. The
        # shared-token check lives in app/routers/internal.py; observation
        # routes use their workspace-scoped submitter credential and HMAC
        # verification in app/routers/heartbeat.py. Classifying them here is
        # what stops machine-to-machine routes from being mistaken for
        # unclassified ones without weakening either authenticator.
        return None

    # Step 4 — domain routes.
    policy = getattr(request.app.state, "domain_policy", None)
    if policy is None:
        # Enforcement is off: the legacy org-scoped JWT path in
        # app/middleware/auth.py remains authoritative, and this guard has still
        # done its structural job (the route was classified). With enforcement ON
        # there is no path back to that validator — the flag chooses which path
        # decides, it does not soften the strict one. Retiring the legacy path is
        # U21's explicitly conditional story, not this one.
        return None

    scope, permission = requirement  # type: ignore[misc]
    caller = await domain_auth.require_verified_caller(request, credentials)
    from app.organization_binding import bind_caller

    caller = await bind_caller(db, caller)
    request.state.caller = caller

    if scope is Scope.WORKSPACE:
        workspace_id = _resolve_workspace_id(request, template)
        grant = await domain_auth.authorize_workspace_operation(
            db, caller, workspace_id, permission
        )
        request.state.grant = grant
        return None

    if scope is Scope.ORGANIZATION:
        await domain_auth.authorize_organization_operation(db, caller, permission)
        return None

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="endpoint scope is not recognized",
    )


def _resolve_workspace_id(request: Request, template: str) -> uuid.UUID:
    """Resolve the target workspace SERVER-SIDE, from the matched path.

    Taken from the routing layer's parsed path parameters, never from a body
    field or header. A workspace-scoped route whose template carries no
    workspace parameter is a denial rather than an unscoped read: an unscoped
    read of a workspace collection is precisely the cross-workspace exposure
    being closed.
    """
    params = request.scope.get("path_params") or {}
    raw = params.get(WORKSPACE_PATH_PARAM)
    if raw is None:
        logger.error(
            "workspace-scoped route %s has no %s path parameter",
            template,
            WORKSPACE_PATH_PARAM,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="workspace-scoped endpoint could not resolve a workspace",
        )
    try:
        return raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))
    except (ValueError, TypeError) as exc:
        # A malformed id is a denial here rather than a 422 from the handler,
        # because this guard runs first and must not leak whether a given
        # workspace exists.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="workspace identifier is not valid",
        ) from exc


__all__ = ["Permission", "enforce_domain_authorization"]
