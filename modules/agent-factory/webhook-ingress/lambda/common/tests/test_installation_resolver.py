"""Tests for common/installation_resolver.py — Issue #2336.

Tests the reverse-lookup from org_id to GitHub App installation_id
used by EventBridge and agent-trigger handlers.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

# Add lambda root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

os.environ.setdefault("IDENTITY_INDEX_TABLE", "adp-dev-identity-index")
os.environ.setdefault("AWS_REGION", "us-east-1")


class TestResolveInstallationForTenant:
    """Tests for resolve_installation_for_tenant()."""

    @patch("common.installation_resolver._get_table")
    def test_known_org_returns_installation_id(self, mock_table):
        """Known org resolves to a valid installation_id."""
        from common.installation_resolver import resolve_installation_for_tenant

        mock_table.return_value.get_item.return_value = {
            "Item": {
                "identity_type": "org_installation",
                "identity_value": "aws-e",
                "installation_id": 124731131,
                "updated_at": "2026-06-29T10:00:00Z",
                "auto_registered": True,
            }
        }

        result = resolve_installation_for_tenant("aws-e")

        assert result == 124731131
        mock_table.return_value.get_item.assert_called_once_with(
            Key={
                "identity_type": "org_installation",
                "identity_value": "aws-e",
            }
        )

    @patch("common.installation_resolver._get_table")
    def test_unknown_org_returns_none(self, mock_table):
        """Unknown org returns None."""
        from common.installation_resolver import resolve_installation_for_tenant

        mock_table.return_value.get_item.return_value = {}

        result = resolve_installation_for_tenant("unknown-org")

        assert result is None

    @patch("common.installation_resolver._get_table")
    def test_empty_org_id_returns_none(self, mock_table):
        """Empty org_id returns None without querying DDB."""
        from common.installation_resolver import resolve_installation_for_tenant

        result = resolve_installation_for_tenant("")

        assert result is None
        mock_table.return_value.get_item.assert_not_called()

    @patch("common.installation_resolver._get_table")
    def test_none_org_id_returns_none(self, mock_table):
        """None org_id returns None without querying DDB."""
        from common.installation_resolver import resolve_installation_for_tenant

        result = resolve_installation_for_tenant(None)

        assert result is None
        mock_table.return_value.get_item.assert_not_called()

    @patch("common.installation_resolver._get_table")
    def test_row_missing_installation_id_returns_none(self, mock_table):
        """Row exists but installation_id attribute is missing → None."""
        from common.installation_resolver import resolve_installation_for_tenant

        mock_table.return_value.get_item.return_value = {
            "Item": {
                "identity_type": "org_installation",
                "identity_value": "aws-e",
                "updated_at": "2026-06-29T10:00:00Z",
            }
        }

        result = resolve_installation_for_tenant("aws-e")

        assert result is None

    @patch("common.installation_resolver._get_table")
    def test_dynamodb_error_returns_none(self, mock_table):
        """DynamoDB exception returns None (fail-soft)."""
        from common.installation_resolver import resolve_installation_for_tenant

        mock_table.return_value.get_item.side_effect = Exception("DDB timeout")

        result = resolve_installation_for_tenant("aws-e")

        assert result is None

    @patch("common.installation_resolver.IDENTITY_INDEX_TABLE", "")
    def test_missing_table_env_returns_none(self):
        """Missing IDENTITY_INDEX_TABLE env var returns None."""
        from common.installation_resolver import resolve_installation_for_tenant

        result = resolve_installation_for_tenant("aws-e")

        assert result is None

    @patch("common.installation_resolver._get_table")
    def test_string_installation_id_converted_to_int(self, mock_table):
        """installation_id stored as string (DDB Number) is converted to int."""
        from common.installation_resolver import resolve_installation_for_tenant

        mock_table.return_value.get_item.return_value = {
            "Item": {
                "identity_type": "org_installation",
                "identity_value": "aws-e",
                "installation_id": "124731274",
                "updated_at": "2026-06-29T10:00:00Z",
            }
        }

        result = resolve_installation_for_tenant("aws-e")

        assert result == 124731274
        assert isinstance(result, int)
