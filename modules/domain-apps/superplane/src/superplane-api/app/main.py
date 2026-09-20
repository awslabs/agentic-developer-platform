"""FastAPI application entry point."""

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from superplane_contracts.emission import install_log_redaction, redact_spans, scrub

from app.auth import build_domain_policy
from app.config import settings
from app.database import async_session_factory
from app.domain_guard import enforce_domain_authorization
from app.middleware.audit import AuditMiddleware
from app.middleware.quota import QuotaEnforcementMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
from app.routers import health
from app.routers.auth import router as auth_router
from app.routers.orgs import router as orgs_router
from app.routers.cost import router as cost_router
from app.routers.heartbeat import router as heartbeat_router
from app.routers.proxy import router as proxy_router
from app.routers.research import router as research_router
from app.routers.events import router as events_router
from app.routers.accounts import router as accounts_router
from app.routers.internal import router as internal_router
from app.routers.installation import router as installation_router
from app.routers.provider_connections import router as provider_connections_router
from app.routers.provider_handles import router as provider_handles_router
from app.routers.quota import router as quota_router
from app.routers.users import router as users_router
from app.routers.workspaces import router as workspaces_router
from app.services.vault_sync import VaultSyncReconciler
from app.services.workspace_reconciler import WorkspaceReconciler


def configure_log_redaction() -> None:
    """Protect application and Uvicorn outputs after server logging configuration."""
    logging.basicConfig()
    install_log_redaction(logging.getLogger())
    # Uvicorn's default handlers do not propagate to root.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        install_log_redaction(logging.getLogger(name))


configure_log_redaction()
logger = logging.getLogger(__name__)

# VaultSyncReconciler — singleton instance for credential rotation propagation.
vault_sync_reconciler = VaultSyncReconciler(session_factory=async_session_factory)

# WorkspaceReconciler — singleton instance for failed bootstrap retry + drift detection.
workspace_reconciler = WorkspaceReconciler(session_factory=async_session_factory)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle — start/stop background reconcilers."""
    # Reapply if the server or an embedding host replaced handlers after import.
    configure_log_redaction()
    if os.environ.get("SUPERPLANE_INSTALLATION_REQUIRED") == "true":
        from app.installation import capabilities_async

        # `capabilities_async`, not `capabilities`: this runs inside the lifespan's
        # event loop, and each capability is now established by calling the adapter
        # and requiring it to refuse an unauthorized probe rather than by testing
        # that the name is bound. Same refusal, better evidenced.
        if not all((await capabilities_async()).values()):
            raise RuntimeError("Production Superplane trust adapters are not composed in this image")
    logger.info("Starting VaultSyncReconciler background task")
    await vault_sync_reconciler.start()
    logger.info("Starting WorkspaceReconciler background task")
    await workspace_reconciler.start()
    yield
    logger.info("Stopping WorkspaceReconciler background task")
    await workspace_reconciler.stop()
    logger.info("Stopping VaultSyncReconciler background task")
    await vault_sync_reconciler.stop()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
    # Domain authorization (issue #5055, U14 — R5/R6). ONE dependency for the
    # whole app rather than a `Depends` per handler: every request is classified
    # against app/endpoint_inventory.py and a route with no recorded decision is
    # refused, so the unsafe state is "route not inventoried" (which fails CI and
    # fails closed) instead of "handler missing its auth dependency" (which is
    # invisible and ships reachable). See app/domain_guard.py for why this cannot
    # be a Starlette middleware: middleware runs before routing, so it cannot
    # identify the route it is protecting.
    dependencies=[Depends(enforce_domain_authorization)],
)

# The token policy is built once, at import, and held on app.state. Building it
# is what enforces "an empty client allowlist is a startup failure, not a
# default" — with enforcement on and no allowlist or issuer configured, the
# policy's constructor raises and this process does not serve, rather than
# serving while admitting every app client in the user pool.
app.state.domain_policy = build_domain_policy()

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Audit logging middleware (logs mutating API calls to events table)
app.add_middleware(AuditMiddleware)

# Quota enforcement middleware (adds headers + logging for quota 429s)
app.add_middleware(QuotaEnforcementMiddleware)

# Rate limiting middleware (per-user, per-workspace)
app.add_middleware(
    RateLimitMiddleware,
    requests_per_minute=settings.rate_limit_per_minute,
    window_seconds=60,
)


@app.exception_handler(RequestValidationError)
async def _scrubbed_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return a 422 that cannot echo the rejected value. Issue #5053 (U7b).

    FastAPI's default handler puts each error's ``input`` in the response body
    verbatim. For the fields that exist to REFUSE credential material that inverts
    the control: submitting a secret ARN to `POST /vault/credentials` returned a 422
    containing the full ARN — AWS account id, region and secret name — which is
    precisely the disclosure the validator rejects the value to prevent. Verified
    before this handler existed; ``tests/test_accounts.py`` pins it.

    The fix scrubs rather than deletes, and it scrubs at two different strengths for
    two different kinds of text:

    * ``input`` is the caller's raw value, of unknown provenance — **dropped
      entirely**. Any part of it could be the secret.
    * ``msg`` is the validator's own prose, deliberately written not to echo the
      value. It goes through ``redact_spans``, which replaces secret-shaped *spans*
      and keeps the explanation. Running the whole message through ``scrub`` instead
      (the first version of this handler) collapsed it to ``[REDACTED]``, leaving a
      caller unable to tell a rejected ARN from a rejected empty string — a refusal
      nobody can act on, which is its own defect.

    Removing the errors wholesale would have made every ordinary 422 in the service
    undebuggable in order to fix a leak in a few fields.

    Registered application-wide, not on one router: any field anywhere may be handed
    a secret by mistake, and a per-route handler protects only the routes someone
    remembered to annotate.
    """
    scrubbed: list[dict[str, Any]] = []
    for error in exc.errors():
        entry = {k: v for k, v in error.items() if k != "input"}
        if "msg" in entry:
            entry["msg"] = redact_spans(str(entry["msg"]))
        if "loc" in entry:
            # Field names/indices, not values — but a key can itself be
            # secret-shaped, so they are checked as whole values.
            entry["loc"] = [scrub(part) for part in entry["loc"]]
        # `ctx` can carry the original exception object, whose rendering may contain
        # the value. Dropped rather than scrubbed: it holds no information a caller
        # needs that `msg` does not already state.
        entry.pop("ctx", None)
        scrubbed.append(entry)
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": scrubbed}
    )


# Routers
app.include_router(health.router, tags=["health"])
app.include_router(auth_router)
app.include_router(orgs_router)
app.include_router(workspaces_router)
app.include_router(provider_connections_router)
app.include_router(proxy_router)
app.include_router(cost_router)
app.include_router(heartbeat_router)
app.include_router(research_router)
app.include_router(quota_router)
app.include_router(events_router)
app.include_router(accounts_router)
app.include_router(users_router)
app.include_router(internal_router)
app.include_router(installation_router)
app.include_router(provider_handles_router)
