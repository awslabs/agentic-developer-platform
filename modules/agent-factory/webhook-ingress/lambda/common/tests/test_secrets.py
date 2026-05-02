"""Tests for secrets.py — Secrets Manager helper."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add common/ to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


class TestGetSecret:
    def setup_method(self):
        """Clear cache before each test."""
        from common import secrets

        secrets.clear_cache()
        secrets._client = None

    @patch("common.secrets._get_client")
    def test_fetches_secret_from_secretsmanager(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {
            "SecretString": "my-webhook-secret",
        }
        mock_get_client.return_value = mock_client

        from common.secrets import get_secret

        result = get_secret("arn:aws:secretsmanager:us-east-1:123456789:secret:test")

        assert result == "my-webhook-secret"
        mock_client.get_secret_value.assert_called_once_with(
            SecretId="arn:aws:secretsmanager:us-east-1:123456789:secret:test"
        )

    @patch("common.secrets._get_client")
    def test_caches_secret_after_first_fetch(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {"SecretString": "cached-secret"}
        mock_get_client.return_value = mock_client

        from common.secrets import get_secret

        arn = "arn:aws:secretsmanager:us-east-1:123456789:secret:cached"
        result1 = get_secret(arn)
        result2 = get_secret(arn)

        assert result1 == "cached-secret"
        assert result2 == "cached-secret"
        # Only one API call despite two get_secret() calls
        assert mock_client.get_secret_value.call_count == 1

    def test_raises_on_empty_arn(self):
        from common.secrets import get_secret

        with pytest.raises(RuntimeError, match="secret_arn is empty"):
            get_secret("")

    @patch("common.secrets._get_client")
    def test_raises_on_api_failure(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_secret_value.side_effect = Exception("AccessDenied")
        mock_get_client.return_value = mock_client

        from common.secrets import get_secret

        with pytest.raises(RuntimeError, match="Failed to fetch secret"):
            get_secret("arn:aws:secretsmanager:us-east-1:123456789:secret:fail")

    @patch("common.secrets._get_client")
    def test_clear_cache_allows_refetch(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {"SecretString": "v1"}
        mock_get_client.return_value = mock_client

        from common.secrets import clear_cache, get_secret

        arn = "arn:aws:secretsmanager:us-east-1:123456789:secret:versioned"
        get_secret(arn)

        # Change the return value and clear cache
        mock_client.get_secret_value.return_value = {"SecretString": "v2"}
        clear_cache()

        result = get_secret(arn)
        assert result == "v2"
        assert mock_client.get_secret_value.call_count == 2
