"""Fixed Product acceptance inventories admitted by maintained deployments."""

from typing import Literal

ProductCases = Literal[
    "none",
    "api_product_lifecycle,api_denial_oracle,api_recovery_cleanup",
    "live_browser_create,live_browser_conflict_denial,live_browser_evidence",
    "live_scope_isolation,live_scope_denials,live_scope_history_cleanup",
]

PRODUCT_CASE_BINDINGS = {
    "api_product_lifecycle,api_denial_oracle,api_recovery_cleanup": ("gateway-backend", "gateway-deploy.yml"),
    "live_browser_create,live_browser_conflict_denial,live_browser_evidence": ("gateway-frontend", "gateway-frontend-deploy.yml"),
    "live_scope_isolation,live_scope_denials,live_scope_history_cleanup": ("gateway-frontend", "gateway-frontend-deploy.yml"),
}
