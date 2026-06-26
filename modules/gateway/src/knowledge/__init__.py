"""Knowledge Registry module — asset registration, ingestion dispatch, repo picker.

Issue #2045: Relocated from agent-context into the gateway image so routes are
present (gated behind AGENT_CONTEXT_ENABLED) without requiring the agent-context
package to be installed.

Follows the activity module precedent (src/activity/).

Gating: individual route modules (routes.py, github_repos.py) conditionally
expose their `router` attribute only when AGENT_CONTEXT_ENABLED=true. The
UNIT_MODULES auto-discovery loop checks `hasattr(module, "router")` — when the
gate is off, the attribute is absent and routes are silently skipped.
"""
