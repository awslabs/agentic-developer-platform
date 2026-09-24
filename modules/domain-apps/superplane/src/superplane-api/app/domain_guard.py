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
5. The acting principal is published for the harness trust ports, from the same
   verified caller the grant check just used.

WHY THIS GUARD ALSO BINDS THE ACTING PRINCIPAL (#5535, W6)
----------------------------------------------------------
``harness_jobs``'s ``PrincipalResolver`` receives its ``org_id`` / ``workspace_id``
arguments as *an assertion to check, not a source of authority*
(``harness_jobs/facade.py:206``), so the composed resolver must derive the tenant
from a caller this process authenticated. Nothing did. Measured against real
PostgreSQL before this step existed, building the real composed facade and calling
it produced ``ProvisioningRefused: the acting principal could not be resolved from
the authenticated context`` — so every operation admitted through the
``operation_facade`` port was refused 100% of the time regardless of grants.

This is the right place for it, and deliberately not the adapter's own job:

* It is the only point that has a caller which has been BOTH admitted by the
  strict token policy and bound to a domain organization. Binding earlier would
  publish an unbound ADP org id, and the grant tables key on the domain one.
* It runs after ``bind_caller`` and after the grant check, so a principal is only
  ever published for a request that was already authorized for this operation.
  Publishing before the check would make the facade's view of "who is acting"
  reachable for callers the guard is about to refuse.
* A ``yield`` dependency rather than a plain one, so the contextvar is reset in a
  ``finally``. Stated precisely, because an earlier version of this comment claimed
  more than the measurement supports: the reset is **defense in depth, not the
  boundary that separates two tenants**. What separates them today is that
  ``BaseHTTPMiddleware`` runs the app below it in a child anyio task, which copies
  the context — so a write here cannot propagate back out to a later request at all.
  Bisected: with zero such middlewares an unreset write escapes; with one or more it
  does not, and ``app/main.py`` installs three. The reset is still worth keeping. It
  holds for in-process callers that are not behind that stack (the harness
  composition resolves principals outside any request), and it does not depend on a
  middleware arrangement that a future change could flatten without anyone
  connecting the two. It must simply not be *relied on* as the isolation guarantee,
  and tests must not assert it at a point where the middleware already makes a leak
  unobservable — see ``_GuardRun`` in ``tests/test_auth.py``, which drives this
  dependency directly for exactly that reason.

NOTHING IS BOUND WHEN ENFORCEMENT IS OFF. With ``domain_policy`` unset this
function returns at step 4 before reaching the binding, and
``require_verified_caller`` independently refuses that configuration. So there is
no path that manufactures an acting principal from the legacy JWT path's weaker
claims: an unbound context resolves to ``None``, which the harness converts to a
refusal, and a refusal naming the missing thing is the honest answer.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth as domain_auth
from app.adapters.operation_authority_source import (
    ActingPrincipal,
    reset_acting_principal,
    set_acting_principal,
)
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
) -> AsyncIterator[None]:
    """Classify the matched route, enforce what it requires, publish the actor.

    Registered once on the app. Yields once on success; raises 401/403 otherwise.
    Never yields a partially-authorized state: a caller that reaches a handler has
    been admitted and granted for that specific operation.

    A generator dependency because the acting principal it publishes for the
    harness trust ports must be unpublished when the request ends — see the module
    docstring.

    EVERY SUCCESSFUL PATH MUST ``yield`` EXACTLY ONCE, including the paths that
    authorize nothing. MEASURED: FastAPI drives a generator dependency as an async
    context manager, so a bare ``return`` before the ``yield`` raises
    ``RuntimeError: generator didn't yield`` and the request becomes a 500 — which
    on this function would mean every public and internal route breaking, since
    those are precisely the early exits. That is why the structure below is a
    single ``try``/``finally`` around one ``yield`` with the decisions expressed as
    a helper that returns, rather than the chain of early ``return``s this function
    used while it was a coroutine.
    """
    acting = await _authorize(request, credentials, db)
    if acting is None:
        # Authorized, but with no acting principal to publish: a public route, an
        # internal route, an unmatched path, or enforcement off. Yield anyway.
        yield
        return

    token = set_acting_principal(acting)
    try:
        yield
    finally:
        # Reset on every exit, including a handler that raised. Defense in depth
        # rather than the tenant boundary itself — the module docstring records what
        # was measured about which layer actually isolates the context.
        reset_acting_principal(token)


async def _authorize(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    db: AsyncSession,
) -> ActingPrincipal | None:
    """Enforce the inventory; return the principal to publish, or ``None``.

    Split out so the enforcement logic keeps its early ``return``s — which are far
    clearer than nested conditionals — while the generator above keeps its single
    ``yield``. ``None`` means "authorized, nothing to publish" and is NOT a refusal;
    refusals are raised as ``HTTPException`` exactly as before.
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
    # Signature/policy verification established the actor even if tenant binding
    # or the later grant check refuses this request. Never publish an unbound tenant.
    request.state.audit_principal = caller.principal.subject
    from app.organization_binding import bind_caller

    caller = await bind_caller(db, caller)
    request.state.caller = caller

    if scope is Scope.WORKSPACE:
        workspace_id = _resolve_workspace_id(request, template)
        grant = await domain_auth.authorize_workspace_operation(
            db, caller, workspace_id, permission
        )
        request.state.grant = grant
        return _acting_for(caller, str(workspace_id))

    if scope is Scope.ORGANIZATION:
        await domain_auth.authorize_organization_operation(db, caller, permission)
        # No workspace in the path, and none invented. An organization-scoped route
        # is exactly the zero-workspace case: `POST /workspaces` has no workspace
        # id yet because it is creating one. The handler supplies the workspace it
        # is about to create as the resolver's asserted `workspace_id`, and the
        # resolver accepts an empty one from the context rather than treating `""`
        # as a workspace named "".
        return _acting_for(caller, "")

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="endpoint scope is not recognized",
    )


def _acting_for(caller: domain_auth.VerifiedCaller, workspace_id: str):
    """The acting principal for the harness ports, from verified claims only.

    Every field comes off ``caller.principal``, which ``DomainPrincipal`` documents
    as resolved entirely from validated claims — nothing here can be influenced by
    a request body or a client header. ``org_id`` is the DOMAIN organization,
    already exchanged for the ADP claim by ``bind_caller``, because that is what
    the grant tables key on; ``caller.source_org_id`` retains the original and is
    deliberately not used for authority.
    """
    return ActingPrincipal(
        subject=caller.principal.subject,
        org_id=caller.principal.org_id,
        workspace_id=workspace_id,
        account_type=caller.principal.account_type,
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
