"""Admin module for organization management, access control, and monitoring.

Exports are resolved LAZILY (PEP 562). This package used to import every
submodule eagerly, which meant `import src.admin.access_control` — or any
other submodule — executed `routes.py` → `src.auth.middleware`, whose
module-level ``AuthService()`` requires ``BG_TOKEN_SECRET_KEY``. Fine inside
a gateway pod; fatal in any component that only needs one class from here:
the orchestration-tick Lambda (#4527) crashed on import at every invocation
because it has no web-session secret — and should never be given one just to
satisfy an import side effect. Lazy resolution keeps `from src.admin import
AdminService` working for the app while letting narrow consumers import
exactly what they need without dragging in the auth stack.
"""

from typing import Any

# Maps each public name to the submodule that defines it. Resolution happens
# on first attribute access, not at package import.
_EXPORTS = {
    # Access Control
    "AccessControl": "access_control",
    # Configuration
    "AdminConfig": "config",
    "AdminRole": "config",
    "Permission": "config",
    "get_admin_config": "config",
    "set_admin_config": "config",
    # Services
    "AdminService": "service",
    "LogService": "log_service",
    "MetricsService": "metrics",
    "get_metrics_service": "metrics",
    "metrics_endpoint": "metrics",
    # Health
    "HealthChecker": "health",
    # Middleware
    "RequestLoggingMiddleware": "middleware",
    "create_request_logging_middleware": "middleware",
}

# Names whose submodule attribute differs from the exported name.
_ALIASED_EXPORTS = {
    "health_router": ("health", "router"),
    "admin_router": ("routes", "router"),
    # FastAPI auto-discovery uses `src.admin.router`.
    "router": ("routes", "router"),
}

__all__ = [*_EXPORTS, "health_router", "admin_router"]


def __getattr__(name: str) -> Any:
    import importlib

    if name in _EXPORTS:
        module = importlib.import_module(f".{_EXPORTS[name]}", __name__)
        return getattr(module, name)
    if name in _ALIASED_EXPORTS:
        submodule, attr = _ALIASED_EXPORTS[name]
        module = importlib.import_module(f".{submodule}", __name__)
        return getattr(module, attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
