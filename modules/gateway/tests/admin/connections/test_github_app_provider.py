"""Unit tests for GitHubAppCredsProvider (Issue #2594).

Tests cover:
- Provider returns creds from a mocked Secrets Manager secret
- invalidate() forces a re-read on next call
- TTL expiry triggers re-read
- Env-var fallback when secret is absent
- Missing everything yields empty strings (caller handles 503)
- SM error degrades to env-var fallback (not a crash)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from src.admin.connections.github_app_provider import (
    GitHubAppCredsProvider,
    _reset_provider_for_testing,
    get_github_app_provider,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sm_client(
    *,
    app_id: str = "12345",
    private_key: str = "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
    slug: str = "adp-agent-platform",
    missing_id: bool = False,
    missing_key: bool = False,
    missing_meta: bool = False,
    raise_on_id: Exception | None = None,
) -> MagicMock:
    """Build a mock boto3 SM client that returns configured secret values."""
    import json

    sm = MagicMock()

    def get_secret_value(SecretId: str) -> dict:  # noqa: N803
        if raise_on_id and "id" in SecretId:
            raise raise_on_id

        if "id" in SecretId:
            if missing_id:
                raise ClientError(
                    {"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}},
                    "GetSecretValue",
                )
            return {"SecretString": app_id}
        elif "key" in SecretId:
            if missing_key:
                raise ClientError(
                    {"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}},
                    "GetSecretValue",
                )
            return {"SecretString": private_key}
        elif "meta" in SecretId:
            if missing_meta:
                raise ClientError(
                    {"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}},
                    "GetSecretValue",
                )
            meta = json.dumps({"app_slug": slug, "app_id": app_id})
            return {"SecretString": meta}
        raise ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}},
            "GetSecretValue",
        )

    sm.get_secret_value = MagicMock(side_effect=get_secret_value)
    return sm


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGetCredentials:
    """Test GitHubAppCredsProvider.get_credentials()."""

    @patch("src.admin.connections.github_app_provider.boto3")
    def test_returns_creds_from_secrets_manager(self, mock_boto3):
        """Provider reads id + key secrets and returns them."""
        sm = _make_sm_client(app_id="99999", private_key="PEM_DATA")
        mock_boto3.client.return_value = sm

        provider = GitHubAppCredsProvider(ttl_seconds=60)
        app_id, key = provider.get_credentials()

        assert app_id == "99999"
        assert key == "PEM_DATA"

    @patch("src.admin.connections.github_app_provider.boto3")
    def test_caches_result_within_ttl(self, mock_boto3):
        """Second call within TTL does not hit SM again."""
        sm = _make_sm_client()
        mock_boto3.client.return_value = sm

        provider = GitHubAppCredsProvider(ttl_seconds=300)
        provider.get_credentials()
        provider.get_credentials()

        # boto3.client called once (SM client created once per fetch)
        assert mock_boto3.client.call_count == 1

    @patch("src.admin.connections.github_app_provider.boto3")
    def test_invalidate_forces_refetch(self, mock_boto3):
        """invalidate() clears cache so next call hits SM."""
        sm = _make_sm_client(app_id="111")
        mock_boto3.client.return_value = sm

        provider = GitHubAppCredsProvider(ttl_seconds=300)
        provider.get_credentials()
        assert mock_boto3.client.call_count == 1

        provider.invalidate()

        # Update the mock to return new value
        sm2 = _make_sm_client(app_id="222")
        mock_boto3.client.return_value = sm2
        app_id, _ = provider.get_credentials()

        assert mock_boto3.client.call_count == 2
        assert app_id == "222"

    @patch("src.admin.connections.github_app_provider.time")
    @patch("src.admin.connections.github_app_provider.boto3")
    def test_ttl_expiry_triggers_refetch(self, mock_boto3, mock_time):
        """After TTL expires, the next call re-reads from SM."""
        sm = _make_sm_client(app_id="aaa")
        mock_boto3.client.return_value = sm

        # First call at t=0
        mock_time.monotonic.return_value = 0.0
        provider = GitHubAppCredsProvider(ttl_seconds=60)
        provider.get_credentials()
        assert mock_boto3.client.call_count == 1

        # Second call at t=30 (within TTL) — should use cache
        mock_time.monotonic.return_value = 30.0
        provider.get_credentials()
        assert mock_boto3.client.call_count == 1

        # Third call at t=61 (TTL expired) — should refetch
        mock_time.monotonic.return_value = 61.0
        sm2 = _make_sm_client(app_id="bbb")
        mock_boto3.client.return_value = sm2
        app_id, _ = provider.get_credentials()
        assert mock_boto3.client.call_count == 2
        assert app_id == "bbb"

    @patch("src.admin.connections.github_app_provider.boto3")
    def test_fallback_to_env_vars_when_secret_absent(self, mock_boto3, monkeypatch):
        """When SM secrets don't exist, falls back to BG_ env vars."""
        sm = _make_sm_client(missing_id=True)
        mock_boto3.client.return_value = sm

        monkeypatch.setenv("BG_GITHUB_APP_ID", "env-id-123")
        monkeypatch.setenv("BG_GITHUB_APP_PRIVATE_KEY", "env-key-pem")

        provider = GitHubAppCredsProvider(ttl_seconds=60)
        app_id, key = provider.get_credentials()

        assert app_id == "env-id-123"
        assert key == "env-key-pem"

    @patch("src.admin.connections.github_app_provider.boto3")
    def test_returns_empty_when_nothing_configured(self, mock_boto3, monkeypatch):
        """When both SM and env vars are empty, returns empty strings."""
        sm = _make_sm_client(missing_id=True)
        mock_boto3.client.return_value = sm

        monkeypatch.setenv("BG_GITHUB_APP_ID", "")
        monkeypatch.setenv("BG_GITHUB_APP_PRIVATE_KEY", "")
        monkeypatch.setenv("BG_GITHUB_APP_SLUG", "")

        provider = GitHubAppCredsProvider(ttl_seconds=60)
        app_id, key = provider.get_credentials()

        assert app_id == ""
        assert key == ""

    @patch("src.admin.connections.github_app_provider.boto3")
    def test_sm_error_falls_back_to_env(self, mock_boto3, monkeypatch):
        """Non-404 SM errors degrade gracefully to env-var fallback."""
        error = ClientError(
            {"Error": {"Code": "InternalServiceError", "Message": "SM is down"}},
            "GetSecretValue",
        )
        sm = _make_sm_client(raise_on_id=error)
        mock_boto3.client.return_value = sm

        monkeypatch.setenv("BG_GITHUB_APP_ID", "fallback-id")
        monkeypatch.setenv("BG_GITHUB_APP_PRIVATE_KEY", "fallback-key")

        provider = GitHubAppCredsProvider(ttl_seconds=60)
        app_id, key = provider.get_credentials()

        assert app_id == "fallback-id"
        assert key == "fallback-key"


class TestGetSlug:
    """Test GitHubAppCredsProvider.get_slug()."""

    @patch("src.admin.connections.github_app_provider.boto3")
    def test_returns_slug_from_meta_secret(self, mock_boto3):
        """Slug is read from the meta JSON secret."""
        sm = _make_sm_client(slug="my-custom-app")
        mock_boto3.client.return_value = sm

        provider = GitHubAppCredsProvider(ttl_seconds=60)
        slug = provider.get_slug()

        assert slug == "my-custom-app"

    @patch("src.admin.connections.github_app_provider.boto3")
    def test_slug_fallback_to_env_when_meta_absent(self, mock_boto3, monkeypatch):
        """When meta secret is missing, slug comes from BG_GITHUB_APP_SLUG."""
        sm = _make_sm_client(missing_meta=True)
        mock_boto3.client.return_value = sm
        monkeypatch.setenv("BG_GITHUB_APP_SLUG", "env-slug-app")

        provider = GitHubAppCredsProvider(ttl_seconds=60)
        slug = provider.get_slug()

        assert slug == "env-slug-app"

    @patch("src.admin.connections.github_app_provider.boto3")
    def test_slug_empty_when_nothing_configured(self, mock_boto3, monkeypatch):
        """When both SM and env are empty, slug is empty."""
        sm = _make_sm_client(missing_id=True)
        mock_boto3.client.return_value = sm
        monkeypatch.setenv("BG_GITHUB_APP_SLUG", "")
        monkeypatch.setenv("BG_GITHUB_APP_ID", "")
        monkeypatch.setenv("BG_GITHUB_APP_PRIVATE_KEY", "")

        provider = GitHubAppCredsProvider(ttl_seconds=60)
        slug = provider.get_slug()

        assert slug == ""


class TestSingleton:
    """Test get_github_app_provider() singleton behavior."""

    def test_returns_same_instance(self):
        """Repeated calls return the same provider."""
        _reset_provider_for_testing(None)
        try:
            p1 = get_github_app_provider()
            p2 = get_github_app_provider()
            assert p1 is p2
        finally:
            _reset_provider_for_testing(None)

    def test_reset_replaces_singleton(self):
        """_reset_provider_for_testing replaces the instance."""
        _reset_provider_for_testing(None)
        try:
            p1 = get_github_app_provider()
            custom = GitHubAppCredsProvider(ttl_seconds=1)
            _reset_provider_for_testing(custom)
            p2 = get_github_app_provider()
            assert p2 is custom
            assert p2 is not p1
        finally:
            _reset_provider_for_testing(None)


class TestInvalidateIntegration:
    """Test that invalidate() integrates correctly with credential resolution."""

    @patch("src.admin.connections.github_app_provider.boto3")
    def test_invalidate_then_read_gets_fresh_value(self, mock_boto3):
        """Simulates the register-callback → invalidate → next-request flow."""
        # Initial state: old creds
        sm_old = _make_sm_client(app_id="old-id", private_key="old-key", slug="old-slug")
        mock_boto3.client.return_value = sm_old

        provider = GitHubAppCredsProvider(ttl_seconds=300)
        app_id, key = provider.get_credentials()
        assert app_id == "old-id"

        # Simulate register-callback writing new creds + calling invalidate()
        provider.invalidate()

        # Next read sees new creds
        sm_new = _make_sm_client(app_id="new-id", private_key="new-key", slug="new-slug")
        mock_boto3.client.return_value = sm_new

        app_id, key = provider.get_credentials()
        assert app_id == "new-id"
        assert key == "new-key"

        slug = provider.get_slug()
        assert slug == "new-slug"


class TestPlaceholderSentinel:
    """Issue #2659: Placeholder secret values must not be treated as real credentials."""

    @patch("src.admin.connections.github_app_provider.boto3")
    def test_placeholder_app_id_falls_back_to_env(self, mock_boto3, monkeypatch):
        """When SM returns the placeholder for app_id, provider treats it as absent."""
        sm = _make_sm_client(app_id="PLACEHOLDER_SET_BY_REGISTER_SCRIPT")
        mock_boto3.client.return_value = sm

        monkeypatch.setenv("BG_GITHUB_APP_ID", "env-fallback-id")
        monkeypatch.setenv("BG_GITHUB_APP_PRIVATE_KEY", "env-fallback-key")
        monkeypatch.setenv("BG_GITHUB_APP_SLUG", "env-fallback-slug")

        provider = GitHubAppCredsProvider(ttl_seconds=60)
        app_id, key = provider.get_credentials()

        assert app_id == "env-fallback-id"
        assert key == "env-fallback-key"

    @patch("src.admin.connections.github_app_provider.boto3")
    def test_placeholder_private_key_falls_back_to_env(self, mock_boto3, monkeypatch):
        """When SM returns the placeholder for private_key, provider treats it as absent."""
        sm = _make_sm_client(
            app_id="12345",
            private_key="PLACEHOLDER_SET_BY_REGISTER_SCRIPT",
        )
        mock_boto3.client.return_value = sm

        monkeypatch.setenv("BG_GITHUB_APP_ID", "env-fallback-id")
        monkeypatch.setenv("BG_GITHUB_APP_PRIVATE_KEY", "env-fallback-key")
        monkeypatch.setenv("BG_GITHUB_APP_SLUG", "env-fallback-slug")

        provider = GitHubAppCredsProvider(ttl_seconds=60)
        app_id, key = provider.get_credentials()

        assert app_id == "env-fallback-id"
        assert key == "env-fallback-key"

    @patch("src.admin.connections.github_app_provider.boto3")
    def test_placeholder_returns_empty_when_no_env_fallback(self, mock_boto3, monkeypatch):
        """Placeholder in SM + no env vars → empty strings (caller handles unconfigured)."""
        sm = _make_sm_client(app_id="PLACEHOLDER_SET_BY_REGISTER_SCRIPT")
        mock_boto3.client.return_value = sm

        monkeypatch.setenv("BG_GITHUB_APP_ID", "")
        monkeypatch.setenv("BG_GITHUB_APP_PRIVATE_KEY", "")
        monkeypatch.setenv("BG_GITHUB_APP_SLUG", "")

        provider = GitHubAppCredsProvider(ttl_seconds=60)
        app_id, key = provider.get_credentials()

        assert app_id == ""
        assert key == ""
