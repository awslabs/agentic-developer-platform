"""Budget module for hierarchical budget management and enforcement."""

from .config import budget_config
from .middleware import BudgetEnforcementMiddleware
from .service import BudgetService
from .utils import calculate_model_cost, get_period_start_end

__all__ = [
    "BudgetService",
    "BudgetEnforcementMiddleware",
    "budget_router",
    "budget_config",
    "calculate_model_cost",
    "get_period_start_end",
]


def __getattr__(name: str):
    # The scheduled engine reads budget configuration and reservations without
    # serving HTTP. Loading routes here would initialize web authentication and
    # require its JWT signing key before the engine can even read its meter.
    # Preserve the public router export for callers that actually serve it.
    if name == "budget_router":
        from .routes import router

        globals()[name] = router
        return router
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
