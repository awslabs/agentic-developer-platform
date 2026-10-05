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
from app.composition import Composition, compose
from app.config import require_database_url, resolve_cors_origins, settings
from app.database import async_session_factory
from app.domain_guard import enforce_domain_authorization
from app.management import enforce_management_surface, management_only
from app.middleware.audit import AuditMiddleware
from app.middleware.auth import require_jwt_secret_key
from app.middleware.quota import QuotaEnforcementMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
from app.routers import health
from app.routers.accounts import router as accounts_router
from app.routers.auth import router as auth_router
from app.routers.controller_management import router as controller_management_router
from app.routers.bootstrap_observation import router as bootstrap_observation_router
from app.routers.controller_recovery import router as controller_recovery_router
from app.routers.cost import router as cost_router
from app.routers.events import router as events_router
from app.routers.heartbeat import router as heartbeat_router
from app.routers.installation import router as installation_router
from app.routers.internal import router as internal_router
from app.routers.operation_approvals import router as operation_approvals_router
from app.routers.onboarding import router as onboarding_router
from app.routers.retirement import router as retirement_router
from app.routers.orgs import router as orgs_router
from app.routers.provider_connections import router as provider_connections_router
from app.routers.provider_handles import router as provider_handles_router
from app.routers.proxy import router as proxy_router
from app.routers.quota import router as quota_router
from app.routers.research import router as research_router
from app.routers.users import router as users_router
from app.routers.workspaces import router as workspaces_router
from app.routers.workspace_access import router as workspace_access_router
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


def compose_vault_client() -> Composition:
    """Compose this process's trust adapters, through the one shared factory.

    Delegates to `app.composition.compose`, which is also what the packaged image's
    `python -m app.installation capabilities` and `readiness` commands call. That
    sharing is the point of #5535 rather than a tidy-up: this function used to hold
    the composition itself, and because `app/installation.py` does not import
    `app.main`, the packaged preflight never ran it — so the preflight reported
    every port uncomposed regardless of configuration, and the installer's
    all-four-true requirement could not be met by any deployment.

    Retained as a named function, with its call site unmoved, for two ordering rules
    that are asserted against this lifespan's source:

    * **Not at import.** A module-level install would give every deployment and every
      test process a vault dependency it never configured, and the ports'
      single-install guards would then be unusable for substitution.
    * **Before the installation gate below.** The gate probes whatever is installed.
      Composing after it would mean the gate always saw an uncomposed port, so a real
      adapter could never satisfy it and the gate would be permanently unsatisfiable
      rather than passed.

    A pre-existing adapter is left alone, and an unconfigured deployment composes
    nothing rather than a stub. See `app/composition.py` for why both.
    """
    return compose(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle — start/stop background reconcilers."""
    # Reapply if the server or an embedding host replaced handlers after import.
    configure_log_redaction()

    require_database_url()

    # Refuse to start without a token signing key (issue #5683, A04).
    #
    # WHY AT STARTUP RATHER THAN AT FIRST USE. `require_jwt_secret_key()` already
    # guards every sign and verify call, so the key can never be silently invented.
    # But relying on that alone means a deployment missing the key starts, passes
    # its health probe, serves traffic, and then fails every single login — which
    # reads as an outage of unknown cause. Failing here names the fault once, in
    # the logs, at the moment it is cheapest to fix.
    #
    # WHY UNCONDITIONAL. `auth_router` is always registered, so POST /auth/login is
    # always reachable and always signs a token with this key. There is no
    # supported configuration of this app where the key is unnecessary, so making
    # the check conditional would only create a way to opt back into the defect.
    #
    # WHY BEFORE THE `management_only()` BRANCH BELOW, WHICH RETURNS EARLY. That
    # mode is not an exception to this requirement: it still registers
    # `auth_router`, and its permitted surface (`/orgs/current`, `/workspaces`,
    # `/users`, `/events`) resolves the caller's org through `get_current_org`,
    # which verifies a token with this key. Placing the check after that branch
    # would leave exactly one supported configuration that starts without a key and
    # then rejects every authenticated request — the failure mode this check exists
    # to remove, reintroduced by ordering alone.
    #
    # The exception's message names the variable and never the value.
    require_jwt_secret_key()

    # Before the installation gate: the gate probes installed adapters (see the
    # docstring above).
    composition = compose_vault_client()
    app.state.trust_composition = composition

    # Connect what composition built but deliberately did not open (issue #5535).
    # `compose()` is synchronous and must run where there is no event loop and no
    # network — the packaged capability preflight runs it under `--network=none` —
    # so the harness operation store's pool is opened here, inside the lifespan.
    #
    # Before the capability gate below, and on the management branch as well: the
    # harness-backed ports are composed in both modes, and a pool left unopened
    # would make every operation answer 503 while the readout reported the ports
    # composed. `aopen()` never raises — an unreachable store is logged and the
    # ports answer their unavailable outcome — so this cannot turn a database
    # outage into a process that will not start.
    await composition.aopen()
    if management_only():
        from pathlib import Path

        from alembic.config import Config
        from alembic.script import ScriptDirectory

        from app.installation import database_check

        if getattr(app.state, "domain_policy", None) is None:
            raise RuntimeError(
                "Management service requires strict domain authorization"
            )
        try:
            observed = await database_check(verify_role_default=True)
        except Exception:
            raise RuntimeError("Management database boundary check failed") from None
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        config.set_main_option(
            "script_location", str(Path(__file__).resolve().parents[1] / "alembic")
        )
        if [observed["revision"]] != ScriptDirectory.from_config(config).get_heads():
            raise RuntimeError("Management database schema does not match the image")
        logger.info(
            "Starting authenticated management service; workspace execution unavailable"
        )
        # `finally`, not a straight-line close, and on this branch too: management
        # mode still composed whatever the deployment configured, so its transports
        # are still this lifespan's to release. An early `return` that skipped the
        # close would leak a connection pool per restart in exactly the mode a
        # control-plane-first installation runs in.
        try:
            composition.start_dispatcher()
            yield
        finally:
            await composition.aclose()
        return
    try:
        if os.environ.get("SUPERPLANE_INSTALLATION_REQUIRED") == "true":
            from app.installation import capabilities_async

            # `capabilities_async`, not `capabilities`: this runs inside the lifespan's
            # event loop, and each capability is now established by calling the adapter
            # and requiring it to refuse an unauthorized probe rather than by testing
            # that the name is bound. Same refusal, better evidenced.
            if not all((await capabilities_async()).values()):
                raise RuntimeError(
                    "Production Superplane trust adapters are not composed in this image"
                )
        logger.info("Starting VaultSyncReconciler background task")
        composition.start_dispatcher()
        await vault_sync_reconciler.start()
        logger.info("Starting WorkspaceReconciler background task")
        await workspace_reconciler.start()
        yield
        logger.info("Stopping WorkspaceReconciler background task")
        await workspace_reconciler.stop()
        logger.info("Stopping VaultSyncReconciler background task")
        await vault_sync_reconciler.stop()
    finally:
        # Reached when the gate above raises, too. A refused boot must not leave the
        # transports composition opened dangling in a process that is about to be
        # restarted by the orchestrator.
        await composition.aclose()


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
    dependencies=[
        Depends(enforce_domain_authorization),
        Depends(enforce_management_surface),
    ],
)

# The token policy is built once, at import, and held on app.state. Building it
# is what enforces "an empty client allowlist is a startup failure, not a
# default" — with enforcement on and no allowlist or issuer configured, the
# policy's constructor raises and this process does not serve, rather than
# serving while admitting every app client in the user pool.
app.state.domain_policy = build_domain_policy()
app.include_router(controller_management_router)
app.include_router(bootstrap_observation_router)
app.include_router(controller_recovery_router)

# Validate the browser origin allowlist at import, before serving requests.
# Preserve the existing credentialed CORS contract for explicitly reviewed origins;
# bearer-token verification remains the authentication boundary.
app.add_middleware(
    CORSMiddleware,
    allow_origins=resolve_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Quota enforcement middleware (adds headers + logging for quota 429s)
app.add_middleware(QuotaEnforcementMiddleware)

# Rate limiting middleware (per-user, per-workspace)
app.add_middleware(
    RateLimitMiddleware,
    requests_per_minute=settings.rate_limit_per_minute,
    window_seconds=60,
)

# Audit logging middleware. Issue #5673 (A17).
#
# ADDED LAST ON PURPOSE, AND THE ORDER IS THE FIX. `add_middleware` PREPENDS, so the
# middleware added last is the OUTERMOST one and wraps every middleware added before it.
#
# This block used to sit above the two below, which made the rate limiter outermost and
# the audit middleware inner. The rate limiter answers a 429 by returning a response
# WITHOUT calling the rest of the stack, so those rejections never reached the audit layer
# at all: a caller could stay entirely out of the audit trail by tripping the rate limit,
# which is precisely the traffic pattern most worth recording. Outermost means a
# short-circuit rejection from any inner middleware is still recorded.
#
# Verified by `tests/test_audit_middleware.py::TestMiddlewareOrdering`, which asserts the
# position structurally so a future edit that moves this call fails a test rather than
# silently reopening the hole.
app.add_middleware(AuditMiddleware)


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
app.include_router(operation_approvals_router)
app.include_router(onboarding_router)
app.include_router(retirement_router)
app.include_router(health.router, tags=["health"])
app.include_router(auth_router)
app.include_router(orgs_router)
app.include_router(workspaces_router)
app.include_router(workspace_access_router)
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
