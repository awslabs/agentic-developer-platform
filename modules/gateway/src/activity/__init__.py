"""Activity module for querying agent invocation history.

Keep the FastAPI router exports lazy. Narrow runtime consumers such as the
orchestration tick import :mod:`src.activity.liveness`; eagerly importing the
routes here also initializes web authentication, which correctly requires a
token secret that the tick Lambda does not have.
"""

from typing import Any

__all__ = [
    "activity_router",
]


def __getattr__(name: str) -> Any:
    if name in {"activity_router", "router"}:
        from .routes import router

        return router
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
