"""Tests for common/installation_resolver.py — Issue #2336.

Tests the reverse-lookup from org_id to GitHub App installation_id
used by EventBridge and agent-trigger handlers.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add lambda root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

os.environ.setdefault("IDENTITY_INDEX_TABLE", "adp-dev-identity-index")
os.environ.setdefault("AWS_REGION", "us-east-1")


@pytest.fixture(autouse=True)
def _canonical_active(monkeypatch):
    import importlib

    def canonical(iid):
        tenant = "acme-hackathon" if str(iid) == "146123525" else "aws-e"
        return {"state": "resolved", "tenant_id": tenant, "revocation_checked": True}

    monkeypatch.setattr(
        importlib.import_module("common.gateway_client"),
        "resolve_installation_by_id",
        canonical,
    )


def _guarded_transaction_table(table):
    current_get = table.get_item
    if hasattr(current_get, "return_value") and current_get.side_effect is None:
        current_get.side_effect = lambda **kwargs: (
            {}
            if kwargs["Key"].get("identity_type") == "github_installation_revoked"
            else current_get.return_value
        )

    def transact(*, TransactItems):  # noqa: N803
        check = TransactItems[0]["ConditionCheck"]
        assert check["Key"]["identity_type"] == "github_installation_revoked"
        assert check["ConditionExpression"] == "attribute_not_exists(identity_type)"
        operation = dict(TransactItems[1]["Put"])
        operation.pop("TableName")
        return table.put_item(**operation)

    table.meta.client.transact_write_items.side_effect = transact
    return table


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

        _guarded_transaction_table(mock_table.return_value)
        result = resolve_installation_for_tenant("aws-e")

        assert result == 124731131
        mock_table.return_value.get_item.assert_any_call(
            Key={
                "identity_type": "org_installation",
                "identity_value": "aws-e",
            }
        )

    @patch("common.installation_resolver._get_table")
    def test_unknown_org_returns_none(self, mock_table):
        """Unknown org returns None (no reverse row AND no forward rows)."""
        from common.installation_resolver import resolve_installation_for_tenant

        mock_table.return_value.get_item.return_value = {}
        # Issue #3860: forward-scan fallback also finds nothing
        mock_table.return_value.query.return_value = {"Items": []}

        _guarded_transaction_table(mock_table.return_value)
        result = resolve_installation_for_tenant("unknown-org")

        assert result is None

    @patch("common.installation_resolver._get_table")
    def test_empty_org_id_returns_none(self, mock_table):
        """Empty org_id returns None without querying DDB."""
        from common.installation_resolver import resolve_installation_for_tenant

        _guarded_transaction_table(mock_table.return_value)
        result = resolve_installation_for_tenant("")

        assert result is None
        mock_table.return_value.get_item.assert_not_called()

    @patch("common.installation_resolver._get_table")
    def test_none_org_id_returns_none(self, mock_table):
        """None org_id returns None without querying DDB."""
        from common.installation_resolver import resolve_installation_for_tenant

        _guarded_transaction_table(mock_table.return_value)
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

        _guarded_transaction_table(mock_table.return_value)
        result = resolve_installation_for_tenant("aws-e")

        assert result is None

    @patch("common.installation_resolver._get_table")
    def test_dynamodb_error_returns_none(self, mock_table):
        """DynamoDB exception returns None (fail-soft)."""
        from common.installation_resolver import resolve_installation_for_tenant

        mock_table.return_value.get_item.side_effect = Exception("DDB timeout")

        _guarded_transaction_table(mock_table.return_value)
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

        _guarded_transaction_table(mock_table.return_value)
        result = resolve_installation_for_tenant("aws-e")

        assert result == 124731274
        assert isinstance(result, int)


class TestForwardScanFallback:
    """Issue #3860: When the reverse row is missing, the resolver should
    attempt a forward-scan fallback to find the installation_id from
    forward rows (github_installation_id → org_id)."""

    @patch("common.installation_resolver._get_table")
    def test_single_match_returns_installation_id(self, mock_table):
        """Forward-scan with exactly one match returns installation_id."""
        from common.installation_resolver import resolve_installation_for_tenant

        table = mock_table.return_value
        # Reverse row miss
        table.get_item.return_value = {}
        # Forward-scan returns exactly one match
        table.query.return_value = {
            "Items": [
                {
                    "identity_type": "github_installation_id",
                    "identity_value": "146123525",
                    "org_id": "acme-hackathon",
                    "updated_at": "2026-07-12T00:00:00Z",
                }
            ]
        }

        _guarded_transaction_table(mock_table.return_value)
        result = resolve_installation_for_tenant("acme-hackathon")

        assert result == 146123525
        # Verify write-through was attempted
        table.put_item.assert_called_once()
        put_kwargs = table.put_item.call_args[1]
        assert put_kwargs["Item"]["identity_type"] == "org_installation"
        assert put_kwargs["Item"]["identity_value"] == "acme-hackathon"
        assert put_kwargs["Item"]["installation_id"] == 146123525
        assert put_kwargs["Item"]["auto_registered"] is True

    @patch("common.installation_resolver._get_table")
    def test_multiple_matches_refuses_ambiguous(self, mock_table):
        """Forward-scan with multiple matches refuses (returns None)."""
        from common.installation_resolver import resolve_installation_for_tenant

        table = mock_table.return_value
        # Reverse row miss
        table.get_item.return_value = {}
        # Forward-scan returns multiple matches → ambiguous
        table.query.return_value = {
            "Items": [
                {
                    "identity_type": "github_installation_id",
                    "identity_value": "111111111",
                    "org_id": "ambiguous-org",
                },
                {
                    "identity_type": "github_installation_id",
                    "identity_value": "222222222",
                    "org_id": "ambiguous-org",
                },
            ]
        }

        _guarded_transaction_table(mock_table.return_value)
        result = resolve_installation_for_tenant("ambiguous-org")

        assert result is None
        # No write-through on ambiguous
        table.put_item.assert_not_called()

    @patch("common.installation_resolver._get_table")
    def test_no_forward_matches_returns_none(self, mock_table):
        """Forward-scan with zero matches returns None."""
        from common.installation_resolver import resolve_installation_for_tenant

        table = mock_table.return_value
        # Reverse row miss
        table.get_item.return_value = {}
        # Forward-scan returns nothing
        table.query.return_value = {"Items": []}

        _guarded_transaction_table(mock_table.return_value)
        result = resolve_installation_for_tenant("ghost-org")

        assert result is None
        table.put_item.assert_not_called()

    @patch("common.installation_resolver._get_table")
    def test_guarded_heal_failure_denies_resolution(self, mock_table):
        from common.installation_resolver import resolve_installation_for_tenant

        table = mock_table.return_value
        # Reverse row miss
        table.get_item.return_value = {}
        # Forward-scan returns one match
        table.query.return_value = {
            "Items": [
                {
                    "identity_type": "github_installation_id",
                    "identity_value": "146123525",
                    "org_id": "acme-hackathon",
                }
            ]
        }
        # Write-through fails
        table.put_item.side_effect = Exception("DDB write error")

        _guarded_transaction_table(mock_table.return_value)
        result = resolve_installation_for_tenant("acme-hackathon")

        # Resolution still succeeds even though write-through failed
        assert result is None

    @patch("common.installation_resolver._get_table")
    def test_forward_scan_query_error_returns_none(self, mock_table):
        """If the forward-scan query itself fails, returns None."""
        from common.installation_resolver import resolve_installation_for_tenant

        table = mock_table.return_value
        # Reverse row miss
        table.get_item.return_value = {}
        # Forward-scan query errors
        table.query.side_effect = Exception("DDB query timeout")

        _guarded_transaction_table(mock_table.return_value)
        result = resolve_installation_for_tenant("error-org")

        assert result is None
