"""Admin module for organization management, access control, and monitoring."""

from .access_control import AccessControl
from .config import AdminConfig, AdminRole, Permission, get_admin_config, set_admin_config
from .health import HealthChecker
from .health import router as health_router
from .log_service import LogService
from .metrics import MetricsService, get_metrics_service, metrics_endpoint
from .middleware import RequestLoggingMiddleware, create_request_logging_middleware
from .routes import router as admin_router
from .service import AdminService

__all__ = [
    # Access Control
    "AccessControl",
    # Configuration
    "AdminConfig",
    "AdminRole",
    "Permission",
    "get_admin_config",
    "set_admin_config",
    # Services
    "AdminService",
    "LogService",
    "MetricsService",
    "get_metrics_service",
    "metrics_endpoint",
    # Health
    "HealthChecker",
    "health_router",
    # Middleware
    "RequestLoggingMiddleware",
    "create_request_logging_middleware",
    # Routes
    "admin_router",
]

# Export router for FastAPI auto-discovery
router = admin_router
