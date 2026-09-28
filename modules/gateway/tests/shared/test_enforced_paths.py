"""Tests for the shared ENFORCED_PATHS registry (Issue #2792).

The budget, rate-limit and approval enforcement middlewares must gate the same
set of proxy paths. Duplicated lists were the root cause of the mantle
passthrough slipping enforcement, so the middlewares now import a single source.
These tests assert the single source is what they actually use, and that the
mantle path is registered.

Issue #4144 added the approval (org-assignment) middleware to that set: a new
spend route must be registered for approval enforcement in the same one place it
is registered for budget and rate limits.
"""

from src.auth import approval_middleware as approval_mw
from src.budget import enforcement_middleware as budget_mw
from src.ratelimit import enforcement_middleware as ratelimit_mw
from src.shared.enforced_paths import ENFORCED_PATHS


def test_mantle_path_registered():
    assert "/openai/v1/responses" in ENFORCED_PATHS


def test_claude_paths_still_registered():
    # Regression: the pre-existing enforced routes must remain.
    for path in ("/v1/messages", "/v1/chat/completions", "/bedrock/invoke", "/model/"):
        assert path in ENFORCED_PATHS


def test_all_middlewares_use_the_shared_registry():
    # The single-source guarantee: every middleware references the same object.
    assert budget_mw.ENFORCED_PATHS is ENFORCED_PATHS
    assert ratelimit_mw.ENFORCED_PATHS is ENFORCED_PATHS
    # Issue #4144: approval enforcement joins the same registry.
    assert approval_mw.ENFORCED_PATHS is ENFORCED_PATHS


def test_middleware_should_enforce_matches_mantle_path():
    # The startswith gate must return True for the mantle path via all middlewares.
    budget_instance = budget_mw.BudgetEnforcementMiddleware(app=None)
    ratelimit_instance = ratelimit_mw.RateLimitEnforcementMiddleware(app=None)
    approval_instance = approval_mw.ApprovalEnforcementMiddleware(app=None)
    assert budget_instance._should_enforce("/openai/v1/responses") is True
    assert ratelimit_instance._should_enforce("/openai/v1/responses") is True
    assert approval_instance._should_enforce("/openai/v1/responses") is True


def test_approval_middleware_gates_every_enforced_path():
    """Issue #4144: no entry in the registry may escape the approval gate.

    This is the #2792 / #2809 class of hole restated for approval: a spend route
    that the middleware's startswith check misses is a route an un-approved user
    can still bill Bedrock through.
    """
    approval_instance = approval_mw.ApprovalEnforcementMiddleware(app=None)
    for path in ENFORCED_PATHS:
        request_path = f"{path}some-model/invoke" if path.endswith("/") else path
        assert approval_instance._should_enforce(request_path) is True, f"{request_path} must be gated for approval"


def test_non_spend_paths_are_not_enforced():
    """Routes an un-approved user must keep reaching (Issue #4144).

    /access/status and /access/request are how a pending user checks state and
    asks for approval — gating them would make approval unreachable. /v1/models
    and /v1/health make no Bedrock call.
    """
    approval_instance = approval_mw.ApprovalEnforcementMiddleware(app=None)
    for path in ("/access/status", "/access/request", "/v1/models", "/v1/health", "/auth/login", "/admin/users"):
        assert approval_instance._should_enforce(path) is False, f"{path} must not be gated"
