"""Activity module for querying agent invocation history."""

from .routes import router as activity_router

__all__ = [
    "activity_router",
]

# Export router for FastAPI auto-discovery
router = activity_router
