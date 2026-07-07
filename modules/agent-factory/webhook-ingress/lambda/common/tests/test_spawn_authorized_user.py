"""Tests for authorized_user_id computation at spawn — Issue #3174.

Covers the credential-authorization binding chain policy (design §Q3):
  - Human-initiated (direct) → authorized_user_id = cognito_sub
  - Human-rooted chain under depth limit → root_human_id
  - Human-rooted chain at/over max_credential_chain_depth → ""
  - Bot-rooted (not human-rooted) → ""
  - max_credential_chain_depth read from tenant-registry (default 3)
  - Existing tests unaffected (new kwarg defaults to "")
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add lambda root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from common.spawn_persona import (
    DEFAULT_MAX_CREDENTIAL_CHAIN_DEPTH,
    _compute_authorized_user_id,
    _get_max_credential_chain_depth,
)
from common.webhook_events import WebhookEventLogger

# =============================================================================
# _compute_authorized_user_id — Unit Tests
# =============================================================================


class TestComputeAuthorizedUserId:
    """Core policy table tests for _compute_authorized_user_id."""

    def test_human_initiated_returns_cognito_sub(self):
        """Human-initiated (depth 0, cognito_sub set) → cognito_sub."""
        ctx = {
            "is_human_rooted": True,
            "root_human_id": "user-alice",
            "chain_depth": 0,
        }
        result = _compute_authorized_user_id(
            correlation_ctx=ctx,
            cognito_sub="user-alice",
            max_credential_chain_depth=3,
        )
        assert result == "user-alice"

    def test_human_rooted_chain_under_depth_returns_root_human_id(self):
        """Human-rooted chain, depth < max → root_human_id."""
        ctx = {
            "is_human_rooted": True,
            "root_human_id": "user-human-root",
            "chain_depth": 2,
        }
        result = _compute_authorized_user_id(
            correlation_ctx=ctx,
            cognito_sub="",  # bot sender, no cognito_sub
            max_credential_chain_depth=3,
        )
        assert result == "user-human-root"

    def test_human_rooted_chain_at_depth_returns_empty(self):
        """Human-rooted chain, depth == max → "" (no vault)."""
        ctx = {
            "is_human_rooted": True,
            "root_human_id": "user-human-root",
            "chain_depth": 3,
        }
        result = _compute_authorized_user_id(
            correlation_ctx=ctx,
            cognito_sub="",
            max_credential_chain_depth=3,
        )
        assert result == ""

    def test_human_rooted_chain_over_depth_returns_empty(self):
        """Human-rooted chain, depth > max → "" (no vault)."""
        ctx = {
            "is_human_rooted": True,
            "root_human_id": "user-human-root",
            "chain_depth": 5,
        }
        result = _compute_authorized_user_id(
            correlation_ctx=ctx,
            cognito_sub="",
            max_credential_chain_depth=3,
        )
        assert result == ""

    def test_bot_rooted_returns_empty(self):
        """Bot-rooted (is_human_rooted=False) → "" regardless of depth."""
        ctx = {
            "is_human_rooted": False,
            "root_human_id": "",
            "chain_depth": 0,
        }
        result = _compute_authorized_user_id(
            correlation_ctx=ctx,
            cognito_sub="",
            max_credential_chain_depth=3,
        )
        assert result == ""

    def test_bot_rooted_with_root_human_id_still_empty(self):
        """Bot-rooted with leftover root_human_id → still ""."""
        ctx = {
            "is_human_rooted": False,
            "root_human_id": "some-user",
            "chain_depth": 1,
        }
        result = _compute_authorized_user_id(
            correlation_ctx=ctx,
            cognito_sub="",
            max_credential_chain_depth=3,
        )
        assert result == ""

    def test_human_rooted_depth_1_under_limit_returns_root(self):
        """Depth 1 with limit 3 → inherits root_human_id."""
        ctx = {
            "is_human_rooted": True,
            "root_human_id": "user-orig",
            "chain_depth": 1,
        }
        result = _compute_authorized_user_id(
            correlation_ctx=ctx,
            cognito_sub="",
            max_credential_chain_depth=3,
        )
        assert result == "user-orig"

    def test_cognito_sub_takes_precedence_when_set(self):
        """When cognito_sub is set (human sender), use it over root_human_id."""
        ctx = {
            "is_human_rooted": True,
            "root_human_id": "root-user-different",
            "chain_depth": 0,
        }
        result = _compute_authorized_user_id(
            correlation_ctx=ctx,
            cognito_sub="direct-human-user",
            max_credential_chain_depth=3,
        )
        assert result == "direct-human-user"

    def test_custom_max_depth_1(self):
        """Tenant with max_credential_chain_depth=1: only root allowed."""
        ctx = {
            "is_human_rooted": True,
            "root_human_id": "user-root",
            "chain_depth": 1,
        }
        result = _compute_authorized_user_id(
            correlation_ctx=ctx,
            cognito_sub="",
            max_credential_chain_depth=1,
        )
        assert result == ""

    def test_custom_max_depth_1_root_allowed(self):
        """Tenant with max_credential_chain_depth=1: depth 0 (root) allowed."""
        ctx = {
            "is_human_rooted": True,
            "root_human_id": "user-root",
            "chain_depth": 0,
        }
        result = _compute_authorized_user_id(
            correlation_ctx=ctx,
            cognito_sub="user-root",
            max_credential_chain_depth=1,
        )
        assert result == "user-root"

    def test_missing_is_human_rooted_defaults_to_false(self):
        """Missing is_human_rooted in ctx defaults to False (fail-closed)."""
        ctx = {
            "root_human_id": "user-x",
            "chain_depth": 0,
        }
        result = _compute_authorized_user_id(
            correlation_ctx=ctx,
            cognito_sub="",
            max_credential_chain_depth=3,
        )
        assert result == ""


# =============================================================================
# _get_max_credential_chain_depth — DDB Lookup
# =============================================================================


class TestGetMaxCredentialChainDepth:
    """Tenant-registry lookup for max_credential_chain_depth."""

    def test_default_value_is_3(self):
        """DEFAULT_MAX_CREDENTIAL_CHAIN_DEPTH is 3."""
        assert DEFAULT_MAX_CREDENTIAL_CHAIN_DEPTH == 3

    def test_missing_env_var_returns_default(self):
        """No TENANT_REGISTRY_TABLE env → returns default."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TENANT_REGISTRY_TABLE", None)
            result = _get_max_credential_chain_depth(12345)
        assert result == DEFAULT_MAX_CREDENTIAL_CHAIN_DEPTH

    @patch("boto3.resource")
    def test_reads_from_ddb(self, mock_boto_resource):
        """Reads max_credential_chain_depth attribute from tenant-registry."""
        os.environ["TENANT_REGISTRY_TABLE"] = "adp-dev-tenant-registry"

        mock_table = MagicMock()
        mock_boto_resource.return_value.Table.return_value = mock_table
        mock_table.get_item.return_value = {
            "Item": {"installation_id": "12345", "max_credential_chain_depth": 5}
        }

        result = _get_max_credential_chain_depth(12345)

        assert result == 5
        mock_table.get_item.assert_called_once_with(
            Key={"installation_id": "12345"},
            ProjectionExpression="max_credential_chain_depth",
        )
        os.environ.pop("TENANT_REGISTRY_TABLE", None)

    @patch("boto3.resource")
    def test_missing_attribute_returns_default(self, mock_boto_resource):
        """Row exists but attribute absent → returns default."""
        os.environ["TENANT_REGISTRY_TABLE"] = "adp-dev-tenant-registry"

        mock_table = MagicMock()
        mock_boto_resource.return_value.Table.return_value = mock_table
        mock_table.get_item.return_value = {
            "Item": {"installation_id": "12345", "tenant_id": "org-x"}
        }

        result = _get_max_credential_chain_depth(12345)

        assert result == DEFAULT_MAX_CREDENTIAL_CHAIN_DEPTH
        os.environ.pop("TENANT_REGISTRY_TABLE", None)

    @patch("boto3.resource")
    def test_no_item_returns_default(self, mock_boto_resource):
        """No row for installation_id → returns default."""
        os.environ["TENANT_REGISTRY_TABLE"] = "adp-dev-tenant-registry"

        mock_table = MagicMock()
        mock_boto_resource.return_value.Table.return_value = mock_table
        mock_table.get_item.return_value = {}

        result = _get_max_credential_chain_depth(12345)

        assert result == DEFAULT_MAX_CREDENTIAL_CHAIN_DEPTH
        os.environ.pop("TENANT_REGISTRY_TABLE", None)

    @patch("boto3.resource")
    def test_ddb_error_returns_default(self, mock_boto_resource):
        """DDB exception → returns default (fail-soft)."""
        os.environ["TENANT_REGISTRY_TABLE"] = "adp-dev-tenant-registry"

        mock_table = MagicMock()
        mock_boto_resource.return_value.Table.return_value = mock_table
        mock_table.get_item.side_effect = Exception("DDB timeout")

        result = _get_max_credential_chain_depth(12345)

        assert result == DEFAULT_MAX_CREDENTIAL_CHAIN_DEPTH
        os.environ.pop("TENANT_REGISTRY_TABLE", None)


# =============================================================================
# WebhookEventLogger.log_event — authorized_user_id persistence
# =============================================================================


def _logger_with_mock_table():
    with patch("common.webhook_events.boto3"):
        event_logger = WebhookEventLogger("adp-dev-webhook-events")
    mock_table = MagicMock()
    event_logger._table = mock_table
    return event_logger, mock_table


class TestAuthorizedUserIdPersisted:
    """authorized_user_id is written to the DDB row."""

    def test_authorized_user_id_written_when_set(self):
        """Non-empty authorized_user_id is persisted to the item."""
        event_logger, table = _logger_with_mock_table()
        event_logger.log_event(
            event_id="evt-001",
            arrived_at="2026-07-07T10:00:00Z",
            tenant_id="t",
            channel="github",
            event_type="issue_comment",
            action="created",
            status="webhook_received",
            authorized_user_id="user-human-42",
        )
        item = table.put_item.call_args[1]["Item"]
        assert item["authorized_user_id"] == "user-human-42"

    def test_authorized_user_id_absent_when_empty(self):
        """Empty authorized_user_id (default) is NOT written to the item."""
        event_logger, table = _logger_with_mock_table()
        event_logger.log_event(
            event_id="evt-002",
            arrived_at="2026-07-07T10:00:00Z",
            tenant_id="t",
            channel="github",
            event_type="issues",
            action="labeled",
            status="webhook_received",
            authorized_user_id="",
        )
        item = table.put_item.call_args[1]["Item"]
        assert "authorized_user_id" not in item

    def test_authorized_user_id_absent_when_default(self):
        """Not passing authorized_user_id at all → field omitted."""
        event_logger, table = _logger_with_mock_table()
        event_logger.log_event(
            event_id="evt-003",
            arrived_at="2026-07-07T10:00:00Z",
            tenant_id="t",
            channel="github",
            event_type="issues",
            action="opened",
            status="webhook_received",
        )
        item = table.put_item.call_args[1]["Item"]
        assert "authorized_user_id" not in item


# =============================================================================
# Integration: spawn_persona → _capture_invocation_event → log_event
# =============================================================================


@dataclass
class MockResolvedIdentity:
    """Minimal mock of ResolvedIdentity for test purposes."""

    tenant_id: str = "test-org"
    org_id: str = "test-org"
    user_id: str = "user-123"
    user_provisioning_mode: str = "strict"
    user_kind: str = "human"
    bot_kind: str = ""


class TestSpawnPersonaAuthorizedUserIntegration:
    """spawn_persona writes authorized_user_id via _capture_invocation_event."""

    @patch("common.spawn_persona._get_max_credential_chain_depth", return_value=3)
    @patch("common.spawn_persona._emit_metric")
    @patch("common.spawn_persona._write_pointer_and_provenance")
    @patch("common.sqs_publisher.publish_envelope", return_value="msg-test")
    @patch("common.webhook_events.boto3")
    def test_human_spawn_writes_cognito_sub(
        self, mock_boto, mock_sqs, mock_write, mock_metric, mock_depth
    ):
        """Human-initiated spawn writes cognito_sub as authorized_user_id."""
        from common.spawn_persona import spawn_persona

        os.environ["EVENTS_TABLE"] = "test-events"
        mock_table = MagicMock()
        mock_boto.resource.return_value.Table.return_value = mock_table

        ctx = {
            "correlation_id": "corr-001",
            "root_human_id": "user-alice",
            "triggered_by": None,
            "is_human_rooted": True,
            "is_new_chain": True,
            "parent_invocation_id": None,
            "chain_depth": 0,
            "last_triggered_persona": None,
            "recent_triggered_personas": set(),
            "recent_trigger_count": 0,
        }

        result = spawn_persona(
            persona="developer",
            correlation_ctx=ctx,
            channel_key="github:repo=org/repo,issue=1",
            resolved_identity=MockResolvedIdentity(
                user_kind="human", user_id="user-alice"
            ),
            tenant_id="test-org",
            actor_user_id="user-alice",
            actor_org_id="test-org",
            sender={"login": "alice", "id": 100, "type": "User"},
            event_type="issue_comment",
            action="created",
            installation_id=123,
            repo="org/repo",
            payload={
                "action": "created",
                "issue": {"number": 1, "title": "Test", "html_url": "http://x"},
                "sender": {"login": "alice", "id": 100, "type": "User"},
                "installation": {"id": 123},
            },
            intent_trigger="mentioned",
        )

        assert result.success is True
        # Verify authorized_user_id was written
        put_call = mock_table.put_item.call_args
        item = put_call[1]["Item"]
        assert item["authorized_user_id"] == "user-alice"

        os.environ.pop("EVENTS_TABLE", None)

    @patch("common.spawn_persona._get_max_credential_chain_depth", return_value=3)
    @patch("common.spawn_persona._emit_metric")
    @patch("common.spawn_persona._write_pointer_and_provenance")
    @patch("common.sqs_publisher.publish_envelope", return_value="msg-chain")
    @patch("common.webhook_events.boto3")
    def test_bot_chain_under_depth_writes_root_human(
        self, mock_boto, mock_sqs, mock_write, mock_metric, mock_depth
    ):
        """Bot-sender chain under depth writes root_human_id."""
        from common.spawn_persona import spawn_persona

        os.environ["EVENTS_TABLE"] = "test-events"
        mock_table = MagicMock()
        mock_boto.resource.return_value.Table.return_value = mock_table

        ctx = {
            "correlation_id": "corr-002",
            "root_human_id": "user-human-root",
            "triggered_by": "bot-x",
            "is_human_rooted": True,
            "is_new_chain": False,
            "parent_invocation_id": "parent-evt",
            "chain_depth": 1,
            "last_triggered_persona": "operations",
            "recent_triggered_personas": set(),
            "recent_trigger_count": 0,
        }

        result = spawn_persona(
            persona="developer",
            correlation_ctx=ctx,
            channel_key="github:repo=org/repo,issue=2",
            resolved_identity=MockResolvedIdentity(
                user_kind="bot", bot_kind="operations", user_id="bot-ops"
            ),
            tenant_id="test-org",
            actor_user_id="bot-ops",
            actor_org_id="test-org",
            sender={"login": "adp-bot[bot]", "id": 900, "type": "Bot"},
            event_type="issue_comment",
            action="created",
            installation_id=123,
            repo="org/repo",
            payload={
                "action": "created",
                "issue": {"number": 2, "title": "Chain", "html_url": "http://y"},
                "sender": {"login": "adp-bot[bot]", "id": 900, "type": "Bot"},
                "installation": {"id": 123},
            },
            intent_trigger="mentioned",
        )

        assert result.success is True
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["authorized_user_id"] == "user-human-root"

        os.environ.pop("EVENTS_TABLE", None)

    @patch("common.spawn_persona._get_max_credential_chain_depth", return_value=3)
    @patch("common.spawn_persona._emit_metric")
    @patch("common.spawn_persona._write_pointer_and_provenance")
    @patch("common.sqs_publisher.publish_envelope", return_value="msg-deep")
    @patch("common.webhook_events.boto3")
    def test_bot_chain_at_depth_writes_empty(
        self, mock_boto, mock_sqs, mock_write, mock_metric, mock_depth
    ):
        """Bot chain at max_credential_chain_depth writes ""."""
        from common.spawn_persona import spawn_persona

        os.environ["EVENTS_TABLE"] = "test-events"
        mock_table = MagicMock()
        mock_boto.resource.return_value.Table.return_value = mock_table

        ctx = {
            "correlation_id": "corr-003",
            "root_human_id": "user-human-root",
            "triggered_by": "bot-x",
            "is_human_rooted": True,
            "is_new_chain": False,
            "parent_invocation_id": "parent-evt",
            "chain_depth": 3,  # == max_credential_chain_depth
            "last_triggered_persona": "operations",
            "recent_triggered_personas": set(),
            "recent_trigger_count": 0,
        }

        result = spawn_persona(
            persona="developer",
            correlation_ctx=ctx,
            channel_key="github:repo=org/repo,issue=3",
            resolved_identity=MockResolvedIdentity(
                user_kind="bot", bot_kind="operations", user_id="bot-ops"
            ),
            tenant_id="test-org",
            actor_user_id="bot-ops",
            actor_org_id="test-org",
            sender={"login": "adp-bot[bot]", "id": 900, "type": "Bot"},
            event_type="issue_comment",
            action="created",
            installation_id=123,
            repo="org/repo",
            payload={
                "action": "created",
                "issue": {"number": 3, "title": "Deep", "html_url": "http://z"},
                "sender": {"login": "adp-bot[bot]", "id": 900, "type": "Bot"},
                "installation": {"id": 123},
            },
            intent_trigger="mentioned",
        )

        assert result.success is True
        item = mock_table.put_item.call_args[1]["Item"]
        # At depth == max → no vault access
        assert "authorized_user_id" not in item

        os.environ.pop("EVENTS_TABLE", None)
