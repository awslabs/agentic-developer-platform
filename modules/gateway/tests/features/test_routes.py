"""Tests for GET /features — Issue #3566.

Verifies fail-open defaults, individual flag override, and
AGENT_CONTEXT_ENABLED fallback for knowledge/indexing flags.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.features.routes import get_current_user, router


@pytest.fixture
def app():
    """Create a test FastAPI app with features router."""
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def client(app):
    """Create a test client with auth bypassed."""

    async def override_current_user():
        return {"user_id": "test-user"}

    app.dependency_overrides[get_current_user] = override_current_user
    return TestClient(app)


class TestFeaturesDefaults:
    """All flags default to True when no env vars are set (fail-open).

    Exceptions: gitlab (Issue #3773), orchestration_engine (Issue #4209) and
    budget_spend (Issue #4402) default to False — optional add-ons are fail-closed.
    """

    def test_all_enabled_by_default(self, client, monkeypatch):
        """With no FEATURE_* env vars, core flags are True; add-ons are False."""
        # Clear any existing feature flags
        for var in [
            "FEATURE_CHAT_ENABLED",
            "FEATURE_KNOWLEDGE_ENABLED",
            "FEATURE_INDEXING_ENABLED",
            "FEATURE_CONNECTIONS_ENABLED",
            "FEATURE_CREDENTIALS_ENABLED",
            "FEATURE_SYSTEM_DASHBOARD_ENABLED",
            "FEATURE_LOGS_ENABLED",
            "FEATURE_GITLAB_ENABLED",
            "FEATURE_ORCHESTRATION_ENGINE_ENABLED",
            "FEATURE_BUDGET_SPEND_ENABLED",
            "FEATURE_AGENT_CONTROL_ENABLED",
            "AGENT_CONTEXT_ENABLED",
        ]:
            monkeypatch.delenv(var, raising=False)

        response = client.get("/features")
        assert response.status_code == 200
        data = response.json()
        assert data == {
            "features": {
                "chat": True,
                "knowledge": True,
                "indexing": True,
                "connections": True,
                "credentials": True,
                "system_dashboard": True,
                "logs": True,
                "gitlab": False,
                "orchestration_engine": False,
                # Issue #4402: fail-closed, so the documented rollback ("flip the flag
                # off — screen and nav vanish") holds when the var is absent entirely.
                "budget_spend": False,
                # Issue #3960: fail-closed, and the strictest case in this dict. The
                # flag gates a channel into a running pod, and the rollout invariant
                # is that ordinary workloads stay off — so "absent" must mean off,
                # not "off until someone sets it to something unparseable".
                "agent_control": False,
            }
        }


class TestIndividualFlags:
    """Individual FEATURE_*_ENABLED=false disables the corresponding flag."""

    def test_chat_disabled(self, client, monkeypatch):
        monkeypatch.setenv("FEATURE_CHAT_ENABLED", "false")
        response = client.get("/features")
        data = response.json()["features"]
        assert data["chat"] is False
        # Other flags unaffected
        assert data["connections"] is True
        assert data["credentials"] is True

    def test_knowledge_disabled(self, client, monkeypatch):
        monkeypatch.setenv("FEATURE_KNOWLEDGE_ENABLED", "false")
        response = client.get("/features")
        data = response.json()["features"]
        assert data["knowledge"] is False

    def test_indexing_disabled(self, client, monkeypatch):
        monkeypatch.setenv("FEATURE_INDEXING_ENABLED", "false")
        response = client.get("/features")
        data = response.json()["features"]
        assert data["indexing"] is False

    def test_connections_disabled(self, client, monkeypatch):
        monkeypatch.setenv("FEATURE_CONNECTIONS_ENABLED", "false")
        response = client.get("/features")
        data = response.json()["features"]
        assert data["connections"] is False

    def test_credentials_disabled(self, client, monkeypatch):
        monkeypatch.setenv("FEATURE_CREDENTIALS_ENABLED", "false")
        response = client.get("/features")
        data = response.json()["features"]
        assert data["credentials"] is False

    def test_logs_disabled(self, client, monkeypatch):
        monkeypatch.setenv("FEATURE_LOGS_ENABLED", "false")
        response = client.get("/features")
        data = response.json()["features"]
        assert data["logs"] is False
        assert data["chat"] is True

    def test_case_insensitive(self, client, monkeypatch):
        """Flag values are case-insensitive."""
        monkeypatch.setenv("FEATURE_CHAT_ENABLED", "False")
        response = client.get("/features")
        assert response.json()["features"]["chat"] is False

    def test_any_non_false_value_is_enabled(self, client, monkeypatch):
        """Any value other than 'false' (case-insensitive) leaves the flag enabled."""
        monkeypatch.setenv("FEATURE_CHAT_ENABLED", "true")
        response = client.get("/features")
        assert response.json()["features"]["chat"] is True

        monkeypatch.setenv("FEATURE_CHAT_ENABLED", "yes")
        response = client.get("/features")
        assert response.json()["features"]["chat"] is True


class TestAgentContextFallback:
    """Knowledge and indexing inherit AGENT_CONTEXT_ENABLED when their own flag is unset."""

    def test_knowledge_inherits_agent_context_false(self, client, monkeypatch):
        """When FEATURE_KNOWLEDGE_ENABLED is unset, falls back to AGENT_CONTEXT_ENABLED."""
        monkeypatch.delenv("FEATURE_KNOWLEDGE_ENABLED", raising=False)
        monkeypatch.setenv("AGENT_CONTEXT_ENABLED", "false")
        response = client.get("/features")
        assert response.json()["features"]["knowledge"] is False

    def test_indexing_inherits_agent_context_false(self, client, monkeypatch):
        """When FEATURE_INDEXING_ENABLED is unset, falls back to AGENT_CONTEXT_ENABLED."""
        monkeypatch.delenv("FEATURE_INDEXING_ENABLED", raising=False)
        monkeypatch.setenv("AGENT_CONTEXT_ENABLED", "false")
        response = client.get("/features")
        assert response.json()["features"]["indexing"] is False

    def test_knowledge_inherits_agent_context_true(self, client, monkeypatch):
        monkeypatch.delenv("FEATURE_KNOWLEDGE_ENABLED", raising=False)
        monkeypatch.setenv("AGENT_CONTEXT_ENABLED", "true")
        response = client.get("/features")
        assert response.json()["features"]["knowledge"] is True

    def test_own_flag_overrides_agent_context(self, client, monkeypatch):
        """When FEATURE_KNOWLEDGE_ENABLED is set, it takes precedence over AGENT_CONTEXT_ENABLED."""
        monkeypatch.setenv("FEATURE_KNOWLEDGE_ENABLED", "true")
        monkeypatch.setenv("AGENT_CONTEXT_ENABLED", "false")
        response = client.get("/features")
        assert response.json()["features"]["knowledge"] is True

    def test_own_flag_false_overrides_agent_context_true(self, client, monkeypatch):
        """Own flag set to false wins over AGENT_CONTEXT_ENABLED=true."""
        monkeypatch.setenv("FEATURE_KNOWLEDGE_ENABLED", "false")
        monkeypatch.setenv("AGENT_CONTEXT_ENABLED", "true")
        response = client.get("/features")
        assert response.json()["features"]["knowledge"] is False


class TestGitlabFailClosed:
    """GitLab flag uses fail-closed semantics (Issue #3773).

    Unlike core flags which default to True (fail-open), gitlab defaults to
    False and only enables when FEATURE_GITLAB_ENABLED is explicitly "true".
    """

    def test_gitlab_false_by_default(self, client, monkeypatch):
        """With no FEATURE_GITLAB_ENABLED env var, gitlab is False."""
        monkeypatch.delenv("FEATURE_GITLAB_ENABLED", raising=False)
        response = client.get("/features")
        assert response.json()["features"]["gitlab"] is False

    def test_gitlab_true_when_explicitly_enabled(self, client, monkeypatch):
        """FEATURE_GITLAB_ENABLED=true enables the flag."""
        monkeypatch.setenv("FEATURE_GITLAB_ENABLED", "true")
        response = client.get("/features")
        assert response.json()["features"]["gitlab"] is True

    def test_gitlab_true_case_insensitive(self, client, monkeypatch):
        """Flag is case-insensitive for 'true'."""
        monkeypatch.setenv("FEATURE_GITLAB_ENABLED", "True")
        response = client.get("/features")
        assert response.json()["features"]["gitlab"] is True

        monkeypatch.setenv("FEATURE_GITLAB_ENABLED", "TRUE")
        response = client.get("/features")
        assert response.json()["features"]["gitlab"] is True

    def test_gitlab_false_for_non_true_values(self, client, monkeypatch):
        """Any value other than 'true' (case-insensitive) keeps flag disabled."""
        monkeypatch.setenv("FEATURE_GITLAB_ENABLED", "false")
        response = client.get("/features")
        assert response.json()["features"]["gitlab"] is False

        monkeypatch.setenv("FEATURE_GITLAB_ENABLED", "yes")
        response = client.get("/features")
        assert response.json()["features"]["gitlab"] is False

        monkeypatch.setenv("FEATURE_GITLAB_ENABLED", "1")
        response = client.get("/features")
        assert response.json()["features"]["gitlab"] is False

    def test_gitlab_does_not_affect_other_flags(self, client, monkeypatch):
        """Enabling/disabling gitlab leaves core flags unchanged."""
        monkeypatch.delenv("FEATURE_GITLAB_ENABLED", raising=False)
        response = client.get("/features")
        data = response.json()["features"]
        assert data["gitlab"] is False
        assert data["chat"] is True
        assert data["connections"] is True


class TestBudgetSpendFailClosed:
    """Budget & Spend flag uses fail-closed semantics (Issue #4402).

    The screen's documented rollback is "flip the flag off — screen and nav vanish, no
    redeploy". That only holds if *absence* of the var resolves to off: with fail-open
    semantics the screen would be live in every environment the moment the SPA deployed,
    and unsetting the var would not turn it off again.
    """

    def test_budget_spend_false_by_default(self, client, monkeypatch):
        """With no FEATURE_BUDGET_SPEND_ENABLED env var, budget_spend is False."""
        monkeypatch.delenv("FEATURE_BUDGET_SPEND_ENABLED", raising=False)
        response = client.get("/features")
        assert response.json()["features"]["budget_spend"] is False

    def test_budget_spend_true_when_explicitly_enabled(self, client, monkeypatch):
        """FEATURE_BUDGET_SPEND_ENABLED=true enables the flag."""
        monkeypatch.setenv("FEATURE_BUDGET_SPEND_ENABLED", "true")
        response = client.get("/features")
        assert response.json()["features"]["budget_spend"] is True

    def test_budget_spend_false_for_non_true_values(self, client, monkeypatch):
        """Any value other than 'true' keeps the flag disabled."""
        for value in ("false", "yes", "1", ""):
            monkeypatch.setenv("FEATURE_BUDGET_SPEND_ENABLED", value)
            response = client.get("/features")
            assert response.json()["features"]["budget_spend"] is False, value

    def test_budget_spend_does_not_affect_other_flags(self, client, monkeypatch):
        """Enabling budget_spend leaves the other flags unchanged."""
        monkeypatch.setenv("FEATURE_BUDGET_SPEND_ENABLED", "true")
        response = client.get("/features")
        data = response.json()["features"]
        assert data["budget_spend"] is True
        assert data["chat"] is True
        assert data["gitlab"] is False
        assert data["orchestration_engine"] is False


class TestRouterPrefix:
    """Guard against router prefix regression (Issue #3569).

    CloudFront strips /api before forwarding to the ALB, so the pod must
    register at /features (not /api/features). The frontend already calls
    apiClient.get('/features') which resolves to /api/features at the browser.
    """

    def test_router_prefix_is_features(self):
        """Router prefix must be /features — CloudFront strips /api."""
        from src.features.routes import router

        assert router.prefix == "/features"
