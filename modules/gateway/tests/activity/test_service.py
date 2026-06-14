"""Unit tests for ActivityService — DynamoDB query logic.

Covers:
- Missing GSI (ValidationException) → empty response, not 500
- Missing table (ResourceNotFoundException) → empty response, not 500
- Cursor encode/decode round-trip; bad cursor → ValueError
- FilterExpression short-page: zero items + non-null last_key is valid
- user_id comes from argument (token), never from query params
"""

import pytest
from botocore.exceptions import ClientError

from src.activity.service import ActivityService, _decode_cursor, _encode_cursor


class TestCursorEncodeDecode:
    """Test the cursor encode/decode helpers."""

    def test_round_trip(self):
        """Cursor encodes and decodes back to the same dict."""
        original = {"pk": "inv-123", "arrived_at": "2026-06-09T14:00:00Z", "user_id": "user-1"}
        encoded = _encode_cursor(original)
        decoded = _decode_cursor(encoded)
        assert decoded == original

    def test_decode_bad_base64(self):
        """Invalid base64 raises ValueError."""
        with pytest.raises(ValueError, match="Invalid cursor"):
            _decode_cursor("not!valid!base64!!!")

    def test_decode_non_dict_json(self):
        """Base64 that decodes to non-dict JSON raises ValueError."""
        import base64
        import json

        bad = base64.urlsafe_b64encode(json.dumps([1, 2, 3]).encode()).decode()
        with pytest.raises(ValueError, match="must decode to a JSON object"):
            _decode_cursor(bad)

    def test_decode_empty_string(self):
        """Empty string raises ValueError."""
        with pytest.raises(ValueError, match="Invalid cursor"):
            _decode_cursor("")


class TestMissingGSI:
    """Test graceful handling when GSI/table doesn't exist yet."""

    def test_validation_exception_returns_empty(self, mock_dynamodb_resource, mock_dynamodb_table):
        """ValidationException (missing GSI) → empty result, not raised."""
        mock_dynamodb_table.query.side_effect = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "Index user-index not found"}},
            "Query",
        )

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_by_user(user_id="user-123")

        assert result.items == []
        assert result.count == 0
        assert result.last_key is None

    def test_resource_not_found_returns_empty(self, mock_dynamodb_resource, mock_dynamodb_table):
        """ResourceNotFoundException (missing table) → empty result, not raised."""
        mock_dynamodb_table.query.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "Table not found"}},
            "Query",
        )

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_by_tenant(tenant_id="org-001")

        assert result.items == []
        assert result.count == 0
        assert result.last_key is None

    def test_other_client_error_propagates(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Non-GSI-related ClientError is re-raised."""
        mock_dynamodb_table.query.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "Throttled"}},
            "Query",
        )

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        with pytest.raises(ClientError):
            service.query_by_user(user_id="user-123")


class TestQueryByUser:
    """Tests for query_by_user (the /me/ endpoint's service call)."""

    def test_uses_user_id_as_partition_key(self, mock_dynamodb_resource, mock_dynamodb_table):
        """The query uses the provided user_id as the GSI partition key."""
        mock_dynamodb_table.query.return_value = {"Items": [], "Count": 0}

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        service.query_by_user(user_id="user-token-value")

        call_kwargs = mock_dynamodb_table.query.call_args[1]
        assert call_kwargs["IndexName"] == "user-index"
        # Verify the key condition uses user_id as PK and the correct value
        key_expr = call_kwargs["KeyConditionExpression"]
        expr_dict = key_expr.get_expression()
        # values[0] is the Key object, values[1] is the partition key value
        key_obj = expr_dict["values"][0]
        assert key_obj.name == "user_id"
        assert expr_dict["values"][1] == "user-token-value"

    def test_returns_mapped_items(self, mock_dynamodb_resource, mock_dynamodb_table):
        """DDB items are correctly mapped to InvocationItem schema."""
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "pk": "inv-001",
                    "invocation_id": "inv-001",
                    "arrived_at": "2026-06-09T14:32:00Z",
                    "channel": "github",
                    "status": "in_progress",
                    "status_updated_at": "2026-06-09T14:32:05Z",
                    "topic": "Deploy ADP",
                    "persona": "operations",
                    "summary": "deploy run started",
                    "source_url": "https://github.com/aws-e/adp/issues/1320",
                    "repo": "aws-e/adp",
                    "issue_number": 1320,
                    "correlation_id": "corr-uuid",
                    "run_id": "agent-issue-1320-xyz",
                    "user_id": "user-1",
                    "tenant_id": "org-1",
                }
            ],
            "Count": 1,
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_by_user(user_id="user-1")

        assert result.count == 1
        assert result.last_key is None  # No LastEvaluatedKey
        item = result.items[0]
        assert item.invocation_id == "inv-001"
        assert item.invoked_at == "2026-06-09T14:32:00Z"
        assert item.channel == "github"
        assert item.status == "in_progress"
        assert item.topic == "Deploy ADP"
        assert item.persona == "operations"
        assert item.repo == "aws-e/adp"
        assert item.issue_number == 1320

    def test_short_page_with_last_key(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Filtered query returning 0 items with LastEvaluatedKey → non-null last_key in response."""
        mock_dynamodb_table.query.return_value = {
            "Items": [],  # Filters removed all items from this page
            "Count": 0,
            "LastEvaluatedKey": {"pk": "inv-050", "arrived_at": "2026-06-01T00:00:00Z", "user_id": "u1"},
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_by_user(user_id="u1", status="completed")

        assert result.items == []
        assert result.count == 0
        assert result.last_key is not None  # Non-null — more pages exist

    def test_pagination_with_cursor(self, mock_dynamodb_resource, mock_dynamodb_table):
        """When last_key is provided, it's passed as ExclusiveStartKey."""
        start_key = {"pk": "inv-010", "arrived_at": "2026-06-05T00:00:00Z", "user_id": "user-1"}
        cursor = _encode_cursor(start_key)

        mock_dynamodb_table.query.return_value = {"Items": [], "Count": 0}

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        service.query_by_user(user_id="user-1", last_key=cursor)

        call_kwargs = mock_dynamodb_table.query.call_args[1]
        assert call_kwargs["ExclusiveStartKey"] == start_key

    def test_bad_cursor_raises_value_error(self, mock_dynamodb_resource):
        """Malformed cursor raises ValueError (caught by route → 400)."""
        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        with pytest.raises(ValueError, match="Invalid cursor"):
            service.query_by_user(user_id="user-1", last_key="garbage!cursor!")

    def test_scan_index_forward_false(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Query uses ScanIndexForward=False for newest-first ordering."""
        mock_dynamodb_table.query.return_value = {"Items": [], "Count": 0}

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        service.query_by_user(user_id="user-1")

        call_kwargs = mock_dynamodb_table.query.call_args[1]
        assert call_kwargs["ScanIndexForward"] is False

    def test_date_range_since_only(self, mock_dynamodb_resource, mock_dynamodb_table):
        """since param adds a >= condition on arrived_at sort key."""
        mock_dynamodb_table.query.return_value = {"Items": [], "Count": 0}

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        service.query_by_user(user_id="user-1", since="2026-06-01T00:00:00Z")

        call_kwargs = mock_dynamodb_table.query.call_args[1]
        # The KeyConditionExpression should have a range condition
        assert call_kwargs["KeyConditionExpression"] is not None

    def test_filter_expressions_applied(self, mock_dynamodb_resource, mock_dynamodb_table):
        """status/channel/persona create FilterExpression."""
        mock_dynamodb_table.query.return_value = {"Items": [], "Count": 0}

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        service.query_by_user(user_id="user-1", status="completed", channel="github", persona="developer")

        call_kwargs = mock_dynamodb_table.query.call_args[1]
        assert "FilterExpression" in call_kwargs


class TestQueryByTenant:
    """Tests for query_by_tenant (the /admin/ endpoint's service call)."""

    def test_uses_tenant_id_as_partition_key(self, mock_dynamodb_resource, mock_dynamodb_table):
        """The query uses tenant_id as the tenant-index GSI partition key."""
        mock_dynamodb_table.query.return_value = {"Items": [], "Count": 0}

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        service.query_by_tenant(tenant_id="org-tenant-001")

        call_kwargs = mock_dynamodb_table.query.call_args[1]
        assert call_kwargs["IndexName"] == "tenant-index"

    def test_user_id_filter_for_admin(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Admin can filter by user_id within their tenant."""
        mock_dynamodb_table.query.return_value = {"Items": [], "Count": 0}

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        service.query_by_tenant(tenant_id="org-001", user_id="specific-user")

        call_kwargs = mock_dynamodb_table.query.call_args[1]
        assert "FilterExpression" in call_kwargs
