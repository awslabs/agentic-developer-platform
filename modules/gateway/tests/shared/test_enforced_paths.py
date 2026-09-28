"""Tests for the shared ENFORCED_PATHS registry (Issue #2792).

Both the budget and rate-limit enforcement middlewares must gate the same set of
proxy paths. Duplicated lists were the root cause of the mantle passthrough
slipping enforcement, so the two middlewares now import a single source. These
tests assert the single source is what both middlewares actually use, and that
the mantle path is registered.
"""

from src.budget import enforcement_middleware as budget_mw
from src.ratelimit import enforcement_middleware as ratelimit_mw
from src.shared.enforced_paths import ENFORCED_PATHS


def test_mantle_path_registered():
    assert "/openai/v1/responses" in ENFORCED_PATHS


def test_claude_paths_still_registered():
    # Regression: the pre-existing enforced routes must remain.
    for path in ("/v1/messages", "/v1/chat/completions", "/bedrock/invoke", "/model/"):
        assert path in ENFORCED_PATHS


def test_both_middlewares_use_the_shared_registry():
    # The single-source guarantee: both middlewares reference the same object.
    assert budget_mw.ENFORCED_PATHS is ENFORCED_PATHS
    assert ratelimit_mw.ENFORCED_PATHS is ENFORCED_PATHS


def test_middleware_should_enforce_matches_mantle_path():
    # The startswith gate must return True for the mantle path via both middlewares.
    budget_instance = budget_mw.BudgetEnforcementMiddleware(app=None)
    ratelimit_instance = ratelimit_mw.RateLimitEnforcementMiddleware(app=None)
    assert budget_instance._should_enforce("/openai/v1/responses") is True
    assert ratelimit_instance._should_enforce("/openai/v1/responses") is True
