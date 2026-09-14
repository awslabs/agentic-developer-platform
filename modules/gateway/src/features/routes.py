"""Feature-flags endpoint — Issue #3566.

Returns deployment-level feature gates. Core flags default to enabled
(fail-open). Knowledge and indexing flags inherit from AGENT_CONTEXT_ENABLED
when their own FEATURE_* env var is unset.

Optional add-on flags (e.g. gitlab) use fail-closed semantics: they default
to False and must be explicitly enabled with "true" (Issue #3773).

The endpoint requires authentication (inherits gateway JWT middleware)
but is not tenant-scoped — flags are deployment-wide.
"""

import os

from fastapi import APIRouter, Depends

from src.auth.dependencies import get_current_user

router = APIRouter(prefix="/features", tags=["features"])


def _is_enabled_strict(env_var: str) -> bool:
    """Read a fail-closed feature flag from the environment.

    Returns True ONLY if the env var is explicitly set to "true" (case-insensitive).
    Any other value or absence → False. Use for optional add-on features where
    the safe default is disabled (Issue #3773).
    """
    value = os.environ.get(env_var)
    if value is not None:
        return value.lower() == "true"
    return False


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
            # Fail-closed: optional add-on, hidden by default (Issue #3773)
            "gitlab": _is_enabled_strict("FEATURE_GITLAB_ENABLED"),
            # Fail-closed: the orchestration engine + graph UI are a per-flow
            # opt-in add-on (Issue #4209). Strict, not `_is_enabled`, for two
            # reasons: legacy mode (AIDLC emits orchestrator + evaluation issues,
            # the operations persona drives the loop) remains the default and
            # fully supported path indefinitely (ruling D-R20), so the new path
            # must be invisible unless somebody asks for it; and a lookup error
            # must resolve to *off*, because failing open here would silently
            # enable an opt-in engine path in production.
            "orchestration_engine": _is_enabled_strict("FEATURE_ORCHESTRATION_ENGINE_ENABLED"),
            # Fail-closed: the Budget & Spend screen (Issue #4402) ships behind a flag
            # whose documented rollback is "flip it off — screen and nav vanish, no
            # redeploy". That only holds if absence resolves to *off*: with
            # `_is_enabled` the screen would be live in every environment the moment
            # the SPA deployed, and unsetting the var would not turn it off again.
            "budget_spend": _is_enabled_strict("FEATURE_BUDGET_SPEND_ENABLED"),
            # Fail-closed: the live run-control channel (Issue #3960). Strict for a
            # stronger reason than the flags above — this one gates a channel that
            # reaches into a running pod, and its rollout invariant is that ordinary
            # workloads stay off until the abort writer and its readers are deployed.
            # A fail-open default would enable it in every environment the moment the
            # gateway shipped, which is precisely the state the invariant forbids.
            #
            # Note this flag is read independently by the worker, which runs its own
            # strict reader. Answering true here cannot start a listener in a pod: a
            # gateway-side flag that could activate worker capabilities would make one
            # config change enable a listener the ingress policy may not yet cover.
            "agent_control": _is_enabled_strict("FEATURE_AGENT_CONTROL_ENABLED"),
            # Fail-closed: the opt-in /next UI shell (Issue #5079, EPIC #5078). The
            # current UI is the default and stays so; /next is an additional
            # experience users enter voluntarily. Its documented rollback is "flip
            # this flag off — the entry link and the /next routes disappear, the
            # current UI keeps working, and no configured data or model-routing rule
            # changes". That only holds if absence resolves to *off*: with
            # `_is_enabled` the new shell would be live in every environment the
            # moment the gateway shipped, and unsetting the var would not turn it
            # off again. This flag is a rollout control, NOT a security boundary —
            # every page reachable under /next enforces the same server-side
            # authorization as its current-UI counterpart.
            "new_ui": _is_enabled_strict("FEATURE_NEW_UI_ENABLED"),
        }
    }
