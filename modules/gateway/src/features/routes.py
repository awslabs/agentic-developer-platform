"""Feature-flags endpoint — Issue #3566.

Returns deployment-level feature gates. All flags default to enabled
(fail-open). Knowledge and indexing flags inherit from AGENT_CONTEXT_ENABLED
when their own FEATURE_* env var is unset.

The endpoint requires authentication (inherits gateway JWT middleware)
but is not tenant-scoped — flags are deployment-wide.
"""

import os

from fastapi import APIRouter, Depends

from src.auth.dependencies import get_current_user

router = APIRouter(prefix="/features", tags=["features"])


def _is_enabled(env_var: str, fallback_var: str | None = None) -> bool:
    """Read a feature flag from the environment.

    Returns True (enabled) unless the env var is explicitly set to "false".
    When the primary env var is absent and a fallback_var is provided,
    inherits from the fallback (also defaults to True if fallback is absent).
    """
    value = os.environ.get(env_var)
    if value is not None:
        return value.lower() != "false"
    # Primary not set — check fallback
    if fallback_var:
        fallback = os.environ.get(fallback_var)
        if fallback is not None:
            return fallback.lower() != "false"
    # Neither set — default enabled
    return True


@router.get("")
async def get_features(_current_user=Depends(get_current_user)):
    """Return feature flags for the current deployment.

    All flags default to True (enabled). A flag is disabled only when
    its corresponding env var is explicitly set to "false".
    """
    return {
        "features": {
            "chat": _is_enabled("FEATURE_CHAT_ENABLED"),
            "knowledge": _is_enabled("FEATURE_KNOWLEDGE_ENABLED", "AGENT_CONTEXT_ENABLED"),
            "indexing": _is_enabled("FEATURE_INDEXING_ENABLED", "AGENT_CONTEXT_ENABLED"),
            "connections": _is_enabled("FEATURE_CONNECTIONS_ENABLED"),
            "credentials": _is_enabled("FEATURE_CREDENTIALS_ENABLED"),
            "system_dashboard": _is_enabled("FEATURE_SYSTEM_DASHBOARD_ENABLED"),
            "logs": _is_enabled("FEATURE_LOGS_ENABLED"),
        }
    }
