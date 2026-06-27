"""Tests for common/service_identity.py — Issue #2154.

Tests service identity resolution for machine/root-triggered events.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

# Add lambda root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

os.environ.setdefault("IDENTITY_INDEX_TABLE", "adp-dev-identity-index")
os.environ.setdefault("AWS_REGION", "us-east-1")


class TestResolveServiceIdentity:
    """Tests for resolve_service_identity()."""

    @patch("common.service_identity._get_table")
    def test_known_service_identity_returns_result(self, mock_table):
        """Known service identity resolves to tenant + org."""
        from common.service_identity import resolve_service_identity

        mock_table.return_value.get_item.return_value = {
            "Item": {
                "identity_type": "service_account",
                "identity_value": "eventbridge:adp-dev-high-error-rate",
                "tenant_id": "acme-corp",
                "org_id": "acme-corp",
                "allowed_personas": ["operations", "developer"],
            }
        }

        result, outcome = resolve_service_identity(
            "eventbridge:adp-dev-high-error-rate"
        )

        assert outcome == "ok"
        assert result is not None
        assert result.tenant_id == "acme-corp"
        assert result.org_id == "acme-corp"
        assert result.service_identity == "eventbridge:adp-dev-high-error-rate"
        assert result.allowed_personas == ["operations", "developer"]

    @patch("common.service_identity._get_table")
    def test_unknown_service_identity_returns_none(self, mock_table):
        """Unknown service identity returns None with appropriate reason."""
        from common.service_identity import resolve_service_identity

        mock_table.return_value.get_item.return_value = {}

        result, outcome = resolve_service_identity("eventbridge:nonexistent-rule")

        assert result is None
        assert outcome == "unknown_service_identity"

    @patch("common.service_identity._get_table")
    def test_empty_allowed_personas_means_unrestricted(self, mock_table):
        """No allowed_personas field means any persona is permitted."""
        from common.service_identity import resolve_service_identity

        mock_table.return_value.get_item.return_value = {
            "Item": {
                "identity_type": "service_account",
                "identity_value": "ci:deploy-pipeline",
                "tenant_id": "acme-corp",
                "org_id": "acme-corp",
            }
        }

        result, outcome = resolve_service_identity("ci:deploy-pipeline")

        assert outcome == "ok"
        assert result is not None
        assert result.allowed_personas == []

    @patch("common.service_identity._get_table")
    def test_dynamodb_set_type_for_allowed_personas(self, mock_table):
        """DynamoDB SS (string set) type is handled correctly."""
        from common.service_identity import resolve_service_identity

        mock_table.return_value.get_item.return_value = {
            "Item": {
                "identity_type": "service_account",
                "identity_value": "eventbridge:adp-dev-ci-failure",
                "tenant_id": "acme-corp",
                "org_id": "acme-corp",
                "allowed_personas": {"operations", "developer"},
            }
        }

        result, outcome = resolve_service_identity("eventbridge:adp-dev-ci-failure")

        assert outcome == "ok"
        assert result is not None
        assert set(result.allowed_personas) == {"operations", "developer"}

    @patch("common.service_identity._get_table")
    def test_dynamodb_error_returns_table_error(self, mock_table):
        """DynamoDB exception returns table_error outcome."""
        from common.service_identity import resolve_service_identity

        mock_table.return_value.get_item.side_effect = Exception("DDB timeout")

        result, outcome = resolve_service_identity("eventbridge:some-rule")

        assert result is None
        assert outcome == "table_error"

    @patch("common.service_identity.IDENTITY_INDEX_TABLE", "")
    def test_missing_table_env_returns_table_error(self):
        """Missing IDENTITY_INDEX_TABLE env var returns table_error."""
        from common.service_identity import resolve_service_identity

        result, outcome = resolve_service_identity("eventbridge:some-rule")

        assert result is None
        assert outcome == "table_error"
