"""FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import async_session_factory
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
from app.routers.quota import router as quota_router
from app.routers.users import router as users_router
from app.routers.workspaces import router as workspaces_router
from app.services.vault_sync import VaultSyncReconciler
from app.services.workspace_reconciler import WorkspaceReconciler

logger = logging.getLogger(__name__)

# VaultSyncReconciler — singleton instance for credential rotation propagation.
vault_sync_reconciler = VaultSyncReconciler(session_factory=async_session_factory)

# WorkspaceReconciler — singleton instance for failed bootstrap retry + drift detection.
workspace_reconciler = WorkspaceReconciler(session_factory=async_session_factory)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle — start/stop background reconcilers."""
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
)

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

# Routers
app.include_router(health.router, tags=["health"])
app.include_router(auth_router)
app.include_router(orgs_router)
app.include_router(workspaces_router)
app.include_router(proxy_router)
app.include_router(cost_router)
app.include_router(heartbeat_router)
app.include_router(research_router)
app.include_router(quota_router)
app.include_router(events_router)
app.include_router(accounts_router)
app.include_router(users_router)
app.include_router(internal_router)
