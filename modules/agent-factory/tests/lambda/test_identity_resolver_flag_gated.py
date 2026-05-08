"""Tests for identity_resolver feature-flag-gated read paths.

Issue #537: Identity projection redesign — resolver reads from new table
when USER_IDENTITY_INDEX_V2_READ=true, falls back to old table.
"""

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Add the webhook-ingress lambda directory to sys.path
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "webhook-ingress",
    "lambda",
    "github",
)
COMMON_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "webhook-ingress",
    "lambda",
)


@pytest.fixture(autouse=True)
def _patch_sys_path():
    """Add lambda dirs to sys.path so identity_resolver can be imported."""
    original = sys.path.copy()
    sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
    sys.path.insert(0, os.path.abspath(COMMON_DIR))
    yield
    sys.path = original


@pytest.fixture(autouse=True)
def mock_env(monkeypatch):
    """Set required env vars for identity_resolver module."""
    monkeypatch.setenv("IDENTITY_INDEX_TABLE", "adp-dev-identity-index")
    monkeypatch.setenv("USER_IDENTITY_INDEX_TABLE", "adp-dev-user-identity-index")
    monkeypatch.setenv("AWS_REGION", "us-east-1")


class TestResolverFlagOff:
    """Tests when USER_IDENTITY_INDEX_V2_READ=false (default)."""

    def test_reads_from_old_table_only(self, monkeypatch):
        """With flag off, resolver reads user from old table only."""
        monkeypatch.setenv("USER_IDENTITY_INDEX_V2_READ", "false")

        with patch("boto3.resource") as mock_resource, patch("boto3.client"):
            mock_table = MagicMock()
            mock_resource.return_value.Table.return_value = mock_table

            import common.identity_resolver as resolver

            resolver._dynamodb = None
            resolver._cloudwatch = None
            importlib.reload(resolver)

            # Installation lookup + user lookup from OLD table
            mock_table.get_item.side_effect = [
                {"Item": {"org_id": "org-001", "user_provisioning_mode": "strict"}},
                {"Item": {"org_id": "org-001", "user_id": "user-001"}},
            ]

            result, reason = resolver.resolve(installation_id=111, sender_id=222)

            assert reason == "ok"
            assert result is not None
            assert result.org_id == "org-001"
            assert result.user_id == "user-001"
            assert mock_table.get_item.call_count == 2

    def test_unknown_user_returns_none(self, monkeypatch):
        """Unknown user in old table returns 'unknown_user'."""
        monkeypatch.setenv("USER_IDENTITY_INDEX_V2_READ", "false")

        with patch("boto3.resource") as mock_resource, patch("boto3.client"):
            mock_table = MagicMock()
            mock_resource.return_value.Table.return_value = mock_table

            import common.identity_resolver as resolver

            resolver._dynamodb = None
            resolver._cloudwatch = None
            importlib.reload(resolver)

            mock_table.get_item.side_effect = [
                {"Item": {"org_id": "org-001", "user_provisioning_mode": "strict"}},
                {},  # user not found
            ]

            result, reason = resolver.resolve(installation_id=111, sender_id=222)
            assert result is None
            assert reason == "unknown_user"


class TestResolverFlagOn:
    """Tests when USER_IDENTITY_INDEX_V2_READ=true."""

    def test_reads_new_table_first(self, monkeypatch):
        """With flag on, resolver reads from new table first."""
        monkeypatch.setenv("USER_IDENTITY_INDEX_V2_READ", "true")

        with patch("boto3.resource") as mock_resource, patch("boto3.client"):
            mock_old_table = MagicMock()
            mock_new_table = MagicMock()

            def table_factory(name):
                if "user-identity-index" in name:
                    return mock_new_table
                return mock_old_table

            mock_resource.return_value.Table.side_effect = table_factory

            import common.identity_resolver as resolver

            resolver._dynamodb = None
            resolver._cloudwatch = None
            importlib.reload(resolver)

            # Installation in old table
            mock_old_table.get_item.return_value = {
                "Item": {"org_id": "org-001", "user_provisioning_mode": "strict"}
            }
            # User in new table
            mock_new_table.get_item.return_value = {
                "Item": {"org_id": "org-001", "user_id": "user-001"}
            }

            result, reason = resolver.resolve(installation_id=111, sender_id=222)
            assert reason == "ok"
            assert result.user_id == "user-001"
            mock_new_table.get_item.assert_called_once()

    def test_falls_back_to_old_table_when_new_empty(self, monkeypatch):
        """With flag on, falls back to old table if new table has no item."""
        monkeypatch.setenv("USER_IDENTITY_INDEX_V2_READ", "true")

        with patch("boto3.resource") as mock_resource, patch("boto3.client"):
            mock_old_table = MagicMock()
            mock_new_table = MagicMock()

            def table_factory(name):
                if "user-identity-index" in name:
                    return mock_new_table
                return mock_old_table

            mock_resource.return_value.Table.side_effect = table_factory

            import common.identity_resolver as resolver

            resolver._dynamodb = None
            resolver._cloudwatch = None
            importlib.reload(resolver)

            # Installation in old table + user fallback in old table
            mock_old_table.get_item.side_effect = [
                {"Item": {"org_id": "org-001", "user_provisioning_mode": "strict"}},
                {"Item": {"org_id": "org-001", "user_id": "user-001"}},
            ]
            # New table returns empty
            mock_new_table.get_item.return_value = {}

            result, reason = resolver.resolve(installation_id=111, sender_id=222)
            assert reason == "ok"
            assert result.user_id == "user-001"


class TestCrossTenantMetric:
    """Tests for cross-tenant mismatch CloudWatch metric emission."""

    def test_emits_metric_on_cross_tenant_mismatch(self, monkeypatch):
        """Cross-tenant mismatch emits CloudWatch metric."""
        monkeypatch.setenv("USER_IDENTITY_INDEX_V2_READ", "false")

        with patch("boto3.resource") as mock_resource, patch("boto3.client") as mock_client:
            mock_cw = MagicMock()
            mock_client.return_value = mock_cw

            mock_table = MagicMock()
            mock_resource.return_value.Table.return_value = mock_table

            import common.identity_resolver as resolver

            resolver._dynamodb = None
            resolver._cloudwatch = None
            importlib.reload(resolver)

            mock_table.get_item.side_effect = [
                {"Item": {"org_id": "org-001", "user_provisioning_mode": "strict"}},
                {"Item": {"org_id": "org-OTHER", "user_id": "user-001"}},  # different org!
            ]

            result, reason = resolver.resolve(installation_id=111, sender_id=222)
            assert result is None
            assert reason == "cross_tenant_identity"

            # CloudWatch metric was emitted
            mock_cw.put_metric_data.assert_called_once()
            call_args = mock_cw.put_metric_data.call_args[1]
            assert call_args["Namespace"] == "ADP/IdentityResolver"
            assert call_args["MetricData"][0]["MetricName"] == "CrossTenantMismatch"
