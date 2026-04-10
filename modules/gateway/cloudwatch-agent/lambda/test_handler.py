"""
Tests for CloudWatch Agent Lambda handler.

Tests cover the core functions for error processing, deduplication,
and GitHub issue creation.
"""

import base64
import gzip
import hashlib
import json
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# Test extract_error_type
class TestExtractErrorType:
    """Tests for extract_error_type function."""

    def test_extract_type_error(self) -> None:
        """Should extract TypeError from message."""
        from handler import extract_error_type

        result = extract_error_type("TypeError: cannot unpack non-iterable NoneType object")
        assert result == "TypeError"

    def test_extract_value_error(self) -> None:
        """Should extract ValueError from message."""
        from handler import extract_error_type

        result = extract_error_type("ValueError: invalid literal for int()")
        assert result == "ValueError"

    def test_extract_key_error(self) -> None:
        """Should extract KeyError from message."""
        from handler import extract_error_type

        result = extract_error_type("KeyError: 'missing_key'")
        assert result == "KeyError"

    def test_extract_attribute_error(self) -> None:
        """Should extract AttributeError from message."""
        from handler import extract_error_type

        result = extract_error_type("AttributeError: 'NoneType' object has no attribute 'foo'")
        assert result == "AttributeError"

    def test_extract_connection_error(self) -> None:
        """Should extract ConnectionError from message."""
        from handler import extract_error_type

        result = extract_error_type("ConnectionError: Failed to connect to database")
        assert result == "ConnectionError"

    def test_extract_fatal_error(self) -> None:
        """Should extract Fatal Error from FATAL message."""
        from handler import extract_error_type

        result = extract_error_type("FATAL: database crashed")
        assert result == "Fatal Error"

    def test_extract_critical_error(self) -> None:
        """Should extract Critical Error from CRITICAL message."""
        from handler import extract_error_type

        result = extract_error_type("CRITICAL: system failure")
        assert result == "Critical Error"

    def test_extract_generic_error(self) -> None:
        """Should extract Error from generic error message."""
        from handler import extract_error_type

        result = extract_error_type("Error: something went wrong")
        assert result == "Error"

    def test_extract_unknown_returns_truncated_message(self) -> None:
        """Should return truncated message when no pattern matches."""
        from handler import extract_error_type

        result = extract_error_type("Something completely different happened here")
        assert result == "Something completely different happened here"
        assert len(result) <= 50

    def test_extract_newlines_replaced(self) -> None:
        """Should replace newlines in truncated message."""
        from handler import extract_error_type

        result = extract_error_type("Line 1\nLine 2\nLine 3")
        assert "\n" not in result


# Test compute_error_hash
class TestComputeErrorHash:
    """Tests for compute_error_hash function."""

    def test_hash_returns_12_chars(self) -> None:
        """Should return a 12-character hash."""
        from handler import compute_error_hash

        result = compute_error_hash("Some error message")
        assert len(result) == 12

    def test_hash_is_deterministic(self) -> None:
        """Same input should produce same hash."""
        from handler import compute_error_hash

        msg = "TypeError: cannot unpack non-iterable"
        hash1 = compute_error_hash(msg)
        hash2 = compute_error_hash(msg)
        assert hash1 == hash2

    def test_hash_normalizes_dates(self) -> None:
        """Should normalize dates for consistent hashing."""
        from handler import compute_error_hash

        msg1 = "Error at 2024-01-15 in module"
        msg2 = "Error at 2024-06-20 in module"
        hash1 = compute_error_hash(msg1)
        hash2 = compute_error_hash(msg2)
        assert hash1 == hash2

    def test_hash_normalizes_times(self) -> None:
        """Should normalize times for consistent hashing."""
        from handler import compute_error_hash

        msg1 = "Error at 10:30:45"
        msg2 = "Error at 14:22:33"
        hash1 = compute_error_hash(msg1)
        hash2 = compute_error_hash(msg2)
        assert hash1 == hash2

    def test_hash_normalizes_memory_addresses(self) -> None:
        """Should normalize memory addresses for consistent hashing."""
        from handler import compute_error_hash

        msg1 = "Object at 0x7f8b1c2d3e4f"
        msg2 = "Object at 0xdeadbeef1234"
        hash1 = compute_error_hash(msg1)
        hash2 = compute_error_hash(msg2)
        assert hash1 == hash2

    def test_hash_normalizes_line_numbers(self) -> None:
        """Should normalize line numbers for consistent hashing."""
        from handler import compute_error_hash

        msg1 = "Error at line 42 in script.py"
        msg2 = "Error at line 123 in script.py"
        hash1 = compute_error_hash(msg1)
        hash2 = compute_error_hash(msg2)
        assert hash1 == hash2

    def test_hash_uses_sha256(self) -> None:
        """Should use SHA256 for hashing."""
        from handler import compute_error_hash

        msg = "Simple error"
        result = compute_error_hash(msg)
        # Verify it's a valid hex string (SHA256 output)
        assert all(c in "0123456789abcdef" for c in result)


# Test is_duplicate
class TestIsDuplicate:
    """Tests for is_duplicate function."""

    def test_returns_false_when_no_table_configured(self) -> None:
        """Should return False when DEDUP_TABLE is not set."""
        from handler import is_duplicate

        with patch("handler.DEDUP_TABLE", ""):
            result = is_duplicate("abc123", "test-repo")
            assert result is False

    @patch("handler.dynamodb")
    @patch("handler.DEDUP_TABLE", "test-table")
    def test_returns_false_for_new_error(self, mock_dynamodb: MagicMock) -> None:
        """Should return False for a new error hash."""
        from handler import is_duplicate

        mock_table = MagicMock()
        mock_table.get_item.return_value = {}  # No existing item
        mock_dynamodb.Table.return_value = mock_table

        result = is_duplicate("new_hash_123", "test-repo")
        assert result is False
        mock_table.put_item.assert_called_once()

    @patch("handler.dynamodb")
    @patch("handler.DEDUP_TABLE", "test-table")
    @patch("handler.COOLDOWN_SECONDS", 300)
    def test_returns_true_for_recent_duplicate(self, mock_dynamodb: MagicMock) -> None:
        """Should return True for recent duplicate within cooldown."""
        from handler import is_duplicate

        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            "Item": {"error_hash": "dup_hash", "repo": "test-repo", "timestamp": time.time() - 60}
        }
        mock_dynamodb.Table.return_value = mock_table

        result = is_duplicate("dup_hash", "test-repo")
        assert result is True

    @patch("handler.dynamodb")
    @patch("handler.DEDUP_TABLE", "test-table")
    @patch("handler.COOLDOWN_SECONDS", 300)
    def test_returns_false_for_old_duplicate(self, mock_dynamodb: MagicMock) -> None:
        """Should return False for duplicate outside cooldown period."""
        from handler import is_duplicate

        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            "Item": {
                "error_hash": "old_hash",
                "repo": "test-repo",
                "timestamp": time.time() - 600,  # 10 minutes ago
            }
        }
        mock_dynamodb.Table.return_value = mock_table

        result = is_duplicate("old_hash", "test-repo")
        assert result is False
        mock_table.put_item.assert_called_once()


# Test handler
class TestHandler:
    """Tests for the main Lambda handler function."""

    def _create_cloudwatch_event(
        self, log_group: str, log_stream: str, messages: list[str]
    ) -> dict[str, Any]:
        """Create a mock CloudWatch Logs subscription event."""
        log_data = {
            "logGroup": log_group,
            "logStream": log_stream,
            "logEvents": [
                {"id": str(i), "timestamp": 1700000000000 + i * 1000, "message": msg}
                for i, msg in enumerate(messages)
            ],
        }
        compressed = gzip.compress(json.dumps(log_data).encode("utf-8"))
        encoded = base64.b64encode(compressed).decode("utf-8")
        return {"awslogs": {"data": encoded}}

    @patch("handler.create_github_issue")
    @patch("handler.get_log_context")
    @patch("handler.is_duplicate")
    @patch("handler.get_repo_for_log_group")
    def test_handler_creates_issue_for_new_error(
        self,
        mock_get_repo: MagicMock,
        mock_is_dup: MagicMock,
        mock_get_context: MagicMock,
        mock_create_issue: MagicMock,
    ) -> None:
        """Should create a GitHub issue for new errors."""
        from handler import handler

        mock_get_repo.return_value = "my-repo"
        mock_is_dup.return_value = False
        mock_get_context.return_value = "Log context here"
        mock_create_issue.return_value = {"html_url": "https://github.com/org/repo/issues/1"}

        event = self._create_cloudwatch_event(
            "/aws/lambda/my-function", "stream-123", ["TypeError: something went wrong"]
        )

        result = handler(event, None)

        assert result["statusCode"] == 200
        mock_create_issue.assert_called_once()
        call_kwargs = mock_create_issue.call_args[1]
        assert call_kwargs["repo_name"] == "my-repo"
        assert "TypeError" in call_kwargs["title"]

    @patch("handler.is_duplicate")
    @patch("handler.get_repo_for_log_group")
    def test_handler_skips_duplicate_errors(
        self, mock_get_repo: MagicMock, mock_is_dup: MagicMock
    ) -> None:
        """Should skip creating issue for duplicate errors."""
        from handler import handler

        mock_get_repo.return_value = "my-repo"
        mock_is_dup.return_value = True

        event = self._create_cloudwatch_event(
            "/aws/lambda/my-function", "stream-123", ["TypeError: duplicate error"]
        )

        result = handler(event, None)

        assert result["statusCode"] == 200
        assert "Duplicate" in result["body"]

    @patch("handler.get_repo_for_log_group")
    def test_handler_returns_early_when_no_repo(self, mock_get_repo: MagicMock) -> None:
        """Should return early when no repo is configured."""
        from handler import handler

        mock_get_repo.return_value = ""

        event = self._create_cloudwatch_event(
            "/aws/lambda/unknown", "stream-123", ["Some error"]
        )

        result = handler(event, None)

        assert result["statusCode"] == 200
        assert "No repo configured" in result["body"]


# Test get_repo_for_log_group
class TestGetRepoForLogGroup:
    """Tests for get_repo_for_log_group function."""

    @patch("handler.LOG_GROUP_REPO_MAP", {"/aws/lambda/my-app": "my-repo"})
    @patch("handler.logs_client")
    @patch("handler.get_account_id")
    def test_returns_repo_from_env_mapping(
        self, mock_account_id: MagicMock, mock_logs: MagicMock
    ) -> None:
        """Should return repo from LOG_GROUP_REPO_MAP environment variable."""
        from handler import get_repo_for_log_group

        mock_account_id.return_value = "123456789012"
        mock_logs.list_tags_for_resource.return_value = {"tags": {}}

        result = get_repo_for_log_group("/aws/lambda/my-app")
        assert result == "my-repo"

    @patch("handler.LOG_GROUP_REPO_MAP", {"my-app": "my-repo"})
    @patch("handler.logs_client")
    @patch("handler.get_account_id")
    def test_returns_repo_from_pattern_match(
        self, mock_account_id: MagicMock, mock_logs: MagicMock
    ) -> None:
        """Should return repo when log group contains pattern."""
        from handler import get_repo_for_log_group

        mock_account_id.return_value = "123456789012"
        mock_logs.list_tags_for_resource.return_value = {"tags": {}}

        result = get_repo_for_log_group("/aws/lambda/my-app-prod")
        assert result == "my-repo"

    @patch("handler.DEFAULT_REPO", "default-repo")
    @patch("handler.LOG_GROUP_REPO_MAP", {})
    @patch("handler.logs_client")
    @patch("handler.get_account_id")
    def test_returns_default_repo_when_no_match(
        self, mock_account_id: MagicMock, mock_logs: MagicMock
    ) -> None:
        """Should return DEFAULT_REPO when no mapping exists."""
        from handler import get_repo_for_log_group

        mock_account_id.return_value = "123456789012"
        mock_logs.list_tags_for_resource.return_value = {"tags": {}}

        result = get_repo_for_log_group("/aws/lambda/unknown")
        assert result == "default-repo"

    @patch("handler.LOG_GROUP_REPO_MAP", {})
    @patch("handler.logs_client")
    @patch("handler.get_account_id")
    def test_returns_repo_from_tags(
        self, mock_account_id: MagicMock, mock_logs: MagicMock
    ) -> None:
        """Should return repo from log group tags."""
        from handler import get_repo_for_log_group

        mock_account_id.return_value = "123456789012"
        mock_logs.list_tags_for_resource.return_value = {"tags": {"agent:repo": "tagged-repo"}}

        result = get_repo_for_log_group("/aws/lambda/my-app")
        assert result == "tagged-repo"


# Test get_log_context
class TestGetLogContext:
    """Tests for get_log_context function."""

    @patch("handler.logs_client")
    def test_returns_log_messages(self, mock_logs: MagicMock) -> None:
        """Should return joined log messages."""
        from handler import get_log_context

        mock_logs.get_log_events.return_value = {
            "events": [{"message": "Line 1"}, {"message": "Line 2"}, {"message": "Line 3"}]
        }

        result = get_log_context("/aws/lambda/my-app", "stream-123", 1700000000000)
        assert result == "Line 1\nLine 2\nLine 3"

    @patch("handler.logs_client")
    def test_returns_fallback_on_error(self, mock_logs: MagicMock) -> None:
        """Should return fallback message on ClientError."""
        from botocore.exceptions import ClientError

        from handler import get_log_context

        mock_logs.get_log_events.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "Not found"}},
            "GetLogEvents",
        )

        result = get_log_context("/aws/lambda/my-app", "stream-123", 1700000000000)
        assert "Could not fetch" in result
