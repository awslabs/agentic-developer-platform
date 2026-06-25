"""Agent-context API routers.

These routers are authored in agent-context but mounted by the gateway
via conditional include_router behind AGENT_CONTEXT_ENABLED.
"""

from agent_context.api.assets_router import router as assets_router
from agent_context.api.indexing_router import router as indexing_router

__all__ = ["assets_router", "indexing_router"]
