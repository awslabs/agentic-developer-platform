"""Unit tests for ActivityService — DynamoDB query logic.

Covers:
- Missing GSI (ValidationException) → empty response, not 500
- Missing table (ResourceNotFoundException) → empty response, not 500
- Cursor encode/decode round-trip; bad cursor → ValueError
- FilterExpression short-page: zero items + non-null last_key is valid
- user_id comes from argument (token), never from query params
- Phase 6 (#1461): trigger_kind derivation, chain query, depth cap
"""

import pytest
from botocore.exceptions import ClientError

from src.activity.service import (
    ActivityService,
    _build_chain_tree,
    _decode_cursor,
    _derive_trigger_kind,
    _encode_cursor,
)


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


# ---------------------------------------------------------------------------
# Phase 6 tests (#1461): Lineage enrichment + chain query
# ---------------------------------------------------------------------------


class TestTriggerKindDerivation:
    """Tests for _derive_trigger_kind — the logic that maps DDB fields to trigger_kind."""

    def test_human_triggered_no_parent(self):
        """No parent_invocation_id + is_human_rooted=True → 'human'."""
        item = {"is_human_rooted": True}
        assert _derive_trigger_kind(item) == "human"

    def test_human_triggered_default(self):
        """Missing fields default to 'human'."""
        item = {}
        assert _derive_trigger_kind(item) == "human"

    def test_agent_triggered_has_parent(self):
        """Has parent_invocation_id → 'agent' regardless of is_human_rooted."""
        item = {"parent_invocation_id": "inv-parent-001", "is_human_rooted": True}
        assert _derive_trigger_kind(item) == "agent"

    def test_agent_triggered_non_human_parent(self):
        """Has parent_invocation_id + is_human_rooted=False → still 'agent' (parent wins)."""
        item = {"parent_invocation_id": "inv-parent-002", "is_human_rooted": False}
        assert _derive_trigger_kind(item) == "agent"

    def test_bot_no_parent_not_human_rooted(self):
        """No parent + is_human_rooted=False → 'bot'."""
        item = {"is_human_rooted": False}
        assert _derive_trigger_kind(item) == "bot"

    def test_bot_explicit_false(self):
        """Explicit is_human_rooted=False without parent → 'bot'."""
        item = {"is_human_rooted": False, "parent_invocation_id": None}
        assert _derive_trigger_kind(item) == "bot"


class TestLineageFieldMapping:
    """Tests that lineage fields are correctly mapped from DDB items."""

    def test_human_triggered_item(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Human-triggered item has correct lineage fields."""
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "invocation_id": "inv-root",
                    "arrived_at": "2026-06-14T10:00:00Z",
                    "channel": "github",
                    "status": "complete",
                    "user_id": "user-1",
                    "is_human_rooted": True,
                    "root_human_id": "user-1",
                    "correlation_id": "chain-001",
                }
            ],
            "Count": 1,
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_by_user(user_id="user-1")

        item = result.items[0]
        assert item.trigger_kind == "human"
        assert item.triggered_by_invocation_id is None
        assert item.triggered_by_topic is None
        assert item.root_human_id == "user-1"
        assert item.is_human_rooted is True

    def test_agent_triggered_item(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Agent-triggered item shows parent link."""
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "invocation_id": "inv-child",
                    "arrived_at": "2026-06-14T10:05:00Z",
                    "channel": "github",
                    "status": "in_progress",
                    "user_id": "user-1",
                    "parent_invocation_id": "inv-root",
                    "parent_topic": "Deploy infrastructure",
                    "is_human_rooted": True,
                    "root_human_id": "user-1",
                    "correlation_id": "chain-001",
                }
            ],
            "Count": 1,
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_by_user(user_id="user-1")

        item = result.items[0]
        assert item.trigger_kind == "agent"
        assert item.triggered_by_invocation_id == "inv-root"
        assert item.triggered_by_topic == "Deploy infrastructure"
        assert item.root_human_id == "user-1"
        assert item.is_human_rooted is True

    def test_bot_triggered_item(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Bot-triggered item (not human-rooted, no parent)."""
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "invocation_id": "inv-cron",
                    "arrived_at": "2026-06-14T00:00:00Z",
                    "channel": "api",
                    "status": "complete",
                    "user_id": "user-bot",
                    "is_human_rooted": False,
                    "correlation_id": "chain-bot-001",
                }
            ],
            "Count": 1,
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_by_user(user_id="user-bot")

        item = result.items[0]
        assert item.trigger_kind == "bot"
        assert item.triggered_by_invocation_id is None
        assert item.root_human_id is None
        assert item.is_human_rooted is False

    def test_pre_feature_rows_default_human(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Pre-feature rows (no lineage fields) default to human trigger."""
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "invocation_id": "inv-old",
                    "arrived_at": "2026-05-01T10:00:00Z",
                    "channel": "github",
                    "status": "complete",
                    "user_id": "user-1",
                    # No lineage fields at all
                }
            ],
            "Count": 1,
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_by_user(user_id="user-1")

        item = result.items[0]
        assert item.trigger_kind == "human"
        assert item.triggered_by_invocation_id is None
        assert item.triggered_by_topic is None
        assert item.root_human_id is None
        assert item.is_human_rooted is True


class TestChainQuery:
    """Tests for get_chain — the chain view query."""

    def test_returns_empty_without_scope(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Chain query without user_id or tenant_id returns empty (safety)."""
        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.get_chain("chain-001")

        assert result.items == []
        assert result.total_count == 0
        assert result.depth_capped is False

    def test_chain_with_user_scope(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Chain query with user_id returns items for that user."""
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "invocation_id": "inv-A",
                    "arrived_at": "2026-06-14T10:00:00Z",
                    "channel": "github",
                    "status": "complete",
                    "topic": "Root task",
                    "user_id": "user-1",
                    "correlation_id": "chain-001",
                    "is_human_rooted": True,
                    "root_human_id": "user-1",
                },
                {
                    "invocation_id": "inv-B",
                    "arrived_at": "2026-06-14T10:05:00Z",
                    "channel": "github",
                    "status": "in_progress",
                    "topic": "Child task",
                    "user_id": "user-1",
                    "correlation_id": "chain-001",
                    "parent_invocation_id": "inv-A",
                    "is_human_rooted": True,
                    "root_human_id": "user-1",
                },
            ],
            "Count": 2,
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.get_chain("chain-001", user_id="user-1")

        assert result.correlation_id == "chain-001"
        assert result.total_count == 2
        assert result.root_human_id == "user-1"
        assert result.is_human_rooted is True
        assert result.depth_capped is False
        # Tree: A is root, B is child
        assert len(result.items) == 1  # One root node
        assert result.items[0].invocation_id == "inv-A"
        assert len(result.items[0].children) == 1
        assert result.items[0].children[0].invocation_id == "inv-B"

    def test_chain_with_tenant_scope(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Chain query with tenant_id returns items for that tenant."""
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "invocation_id": "inv-T1",
                    "arrived_at": "2026-06-14T10:00:00Z",
                    "channel": "github",
                    "status": "complete",
                    "topic": "Tenant task",
                    "tenant_id": "org-001",
                    "correlation_id": "chain-002",
                    "is_human_rooted": True,
                    "root_human_id": "user-admin",
                },
            ],
            "Count": 1,
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.get_chain("chain-002", tenant_id="org-001")

        assert result.total_count == 1
        assert result.items[0].invocation_id == "inv-T1"

    def test_chain_depth_cap(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Chain query respects depth cap and sets depth_capped=True."""
        # Generate more items than the depth cap
        items = [
            {
                "invocation_id": f"inv-{i:03d}",
                "arrived_at": f"2026-06-14T{10 + i}:00:00Z",
                "channel": "github",
                "status": "complete",
                "user_id": "user-1",
                "correlation_id": "chain-deep",
            }
            for i in range(10)
        ]
        mock_dynamodb_table.query.return_value = {"Items": items, "Count": 10}

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.get_chain("chain-deep", user_id="user-1", depth_cap=5)

        assert result.total_count == 5
        assert result.depth_capped is True

    def test_chain_queries_correlation_index(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Chain query uses the correlation-index GSI (a Query, NOT a Scan)."""
        mock_dynamodb_table.query.return_value = {"Items": [], "Count": 0}

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        service.get_chain("chain-xyz", user_id="user-1")

        # Must be a Query on correlation-index — never a Scan (the bug: Scan
        # required a dynamodb:Scan grant the gateway role lacks → 500 in the UI).
        mock_dynamodb_table.query.assert_called()
        mock_dynamodb_table.scan.assert_not_called()
        call_kwargs = mock_dynamodb_table.query.call_args[1]
        assert call_kwargs["IndexName"] == "correlation-index"

    def test_chain_missing_table_returns_empty(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Chain query with missing table returns empty gracefully."""
        mock_dynamodb_table.query.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "Table not found"}},
            "Query",
        )

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.get_chain("chain-001", user_id="user-1")

        assert result.items == []
        assert result.total_count == 0

    def test_chain_access_denied_returns_empty(self, mock_dynamodb_resource, mock_dynamodb_table):
        """AccessDeniedException (e.g. missing IAM) degrades to empty, not a 500."""
        mock_dynamodb_table.query.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}},
            "Query",
        )

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.get_chain("chain-001", user_id="user-1")

        assert result.items == []
        assert result.total_count == 0

    def test_chain_flat_fallback_no_parent_edges(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Chain with no parent edges renders as flat list (all roots)."""
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "invocation_id": "inv-X",
                    "arrived_at": "2026-06-14T10:00:00Z",
                    "status": "complete",
                    "topic": "Task X",
                    "user_id": "user-1",
                    "correlation_id": "chain-flat",
                },
                {
                    "invocation_id": "inv-Y",
                    "arrived_at": "2026-06-14T10:05:00Z",
                    "status": "complete",
                    "topic": "Task Y",
                    "user_id": "user-1",
                    "correlation_id": "chain-flat",
                },
            ],
            "Count": 2,
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.get_chain("chain-flat", user_id="user-1")

        # Without parent edges, all items are roots (flat)
        assert len(result.items) == 2
        assert all(len(node.children) == 0 for node in result.items)


class TestBuildChainTree:
    """Tests for _build_chain_tree helper."""

    def test_linear_chain_abc(self):
        """A→B→C builds correctly nested tree."""
        items = [
            {"invocation_id": "A", "arrived_at": "2026-06-14T10:00:00Z", "status": "complete", "topic": "Root"},
            {"invocation_id": "B", "arrived_at": "2026-06-14T10:01:00Z", "status": "complete", "topic": "Child", "parent_invocation_id": "A"},
            {"invocation_id": "C", "arrived_at": "2026-06-14T10:02:00Z", "status": "complete", "topic": "Grandchild", "parent_invocation_id": "B"},
        ]

        tree = _build_chain_tree(items)

        assert len(tree) == 1  # One root (A)
        assert tree[0].invocation_id == "A"
        assert len(tree[0].children) == 1  # A has one child (B)
        assert tree[0].children[0].invocation_id == "B"
        assert len(tree[0].children[0].children) == 1  # B has one child (C)
        assert tree[0].children[0].children[0].invocation_id == "C"

    def test_branching_tree(self):
        """A→B, A→C builds tree with two children under A."""
        items = [
            {"invocation_id": "A", "arrived_at": "2026-06-14T10:00:00Z", "status": "complete", "topic": "Root"},
            {"invocation_id": "B", "arrived_at": "2026-06-14T10:01:00Z", "status": "complete", "topic": "Branch 1", "parent_invocation_id": "A"},
            {"invocation_id": "C", "arrived_at": "2026-06-14T10:02:00Z", "status": "complete", "topic": "Branch 2", "parent_invocation_id": "A"},
        ]

        tree = _build_chain_tree(items)

        assert len(tree) == 1
        assert tree[0].invocation_id == "A"
        assert len(tree[0].children) == 2
        child_ids = {c.invocation_id for c in tree[0].children}
        assert child_ids == {"B", "C"}

    def test_orphan_becomes_root(self):
        """Item whose parent is not in the list becomes a root."""
        items = [
            {
                "invocation_id": "B",
                "arrived_at": "2026-06-14T10:01:00Z",
                "status": "complete",
                "topic": "Orphan",
                "parent_invocation_id": "missing-parent",
            },
        ]

        tree = _build_chain_tree(items)

        assert len(tree) == 1
        assert tree[0].invocation_id == "B"

    def test_empty_items(self):
        """Empty input returns empty tree."""
        assert _build_chain_tree([]) == []


# ---------------------------------------------------------------------------
# Issue #1653 tests: error_message, completed_at, run_log_url, get_invocation
# ---------------------------------------------------------------------------


class TestErrorMessageMapping:
    """Tests that error_message is correctly mapped from DDB items."""

    def test_error_message_mapped(self, mock_dynamodb_resource, mock_dynamodb_table):
        """error_message in DDB item is mapped to InvocationItem."""
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "invocation_id": "inv-fail",
                    "arrived_at": "2026-06-20T10:00:00Z",
                    "channel": "github",
                    "status": "failed",
                    "status_updated_at": "2026-06-20T10:02:00Z",
                    "error_message": "RuntimeError: agent crashed",
                    "user_id": "user-1",
                }
            ],
            "Count": 1,
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_by_user(user_id="user-1")

        item = result.items[0]
        assert item.error_message == "RuntimeError: agent crashed"

    def test_error_message_none_when_absent(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Missing error_message in DDB item maps to None."""
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "invocation_id": "inv-ok",
                    "arrived_at": "2026-06-20T10:00:00Z",
                    "channel": "github",
                    "status": "complete",
                    "user_id": "user-1",
                }
            ],
            "Count": 1,
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_by_user(user_id="user-1")

        assert result.items[0].error_message is None


class TestCompletedAtDerivation:
    """Tests that completed_at is derived from status_updated_at for terminal statuses."""

    def test_terminal_status_has_completed_at(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Terminal status (complete) derives completed_at from status_updated_at."""
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "invocation_id": "inv-done",
                    "arrived_at": "2026-06-20T10:00:00Z",
                    "status": "complete",
                    "status_updated_at": "2026-06-20T10:02:14Z",
                    "user_id": "user-1",
                }
            ],
            "Count": 1,
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_by_user(user_id="user-1")

        assert result.items[0].completed_at == "2026-06-20T10:02:14Z"

    def test_failed_status_has_completed_at(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Failed status also derives completed_at (it's terminal)."""
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "invocation_id": "inv-fail",
                    "arrived_at": "2026-06-20T10:00:00Z",
                    "status": "failed",
                    "status_updated_at": "2026-06-20T10:01:30Z",
                    "user_id": "user-1",
                }
            ],
            "Count": 1,
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_by_user(user_id="user-1")

        assert result.items[0].completed_at == "2026-06-20T10:01:30Z"

    def test_in_progress_has_no_completed_at(self, mock_dynamodb_resource, mock_dynamodb_table):
        """In-progress status does NOT have completed_at (non-terminal)."""
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "invocation_id": "inv-running",
                    "arrived_at": "2026-06-20T10:00:00Z",
                    "status": "in_progress",
                    "status_updated_at": "2026-06-20T10:00:05Z",
                    "user_id": "user-1",
                }
            ],
            "Count": 1,
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_by_user(user_id="user-1")

        assert result.items[0].completed_at is None

    def test_webhook_received_has_no_completed_at(self, mock_dynamodb_resource, mock_dynamodb_table):
        """webhook_received status does NOT have completed_at."""
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "invocation_id": "inv-queued",
                    "arrived_at": "2026-06-20T10:00:00Z",
                    "status": "webhook_received",
                    "status_updated_at": "2026-06-20T10:00:00Z",
                    "user_id": "user-1",
                }
            ],
            "Count": 1,
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_by_user(user_id="user-1")

        assert result.items[0].completed_at is None


class TestRunLogUrlMapping:
    """Tests that run_log_url is mapped from check_run_url DDB field."""

    def test_check_run_url_mapped_to_run_log_url(self, mock_dynamodb_resource, mock_dynamodb_table):
        """check_run_url in DDB item maps to run_log_url on InvocationItem."""
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "invocation_id": "inv-with-log",
                    "arrived_at": "2026-06-20T10:00:00Z",
                    "status": "complete",
                    "check_run_url": "https://github.com/org/repo/runs/12345",
                    "user_id": "user-1",
                }
            ],
            "Count": 1,
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_by_user(user_id="user-1")

        assert result.items[0].run_log_url == "https://github.com/org/repo/runs/12345"

    def test_missing_check_run_url_maps_to_none(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Missing check_run_url → run_log_url is None (Tier 2 not yet deployed)."""
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "invocation_id": "inv-no-log",
                    "arrived_at": "2026-06-20T10:00:00Z",
                    "status": "complete",
                    "user_id": "user-1",
                }
            ],
            "Count": 1,
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_by_user(user_id="user-1")

        assert result.items[0].run_log_url is None


class TestGetInvocation:
    """Tests for get_invocation — single-item lookup by event_id."""

    def test_returns_item_when_found(self, mock_dynamodb_resource, mock_dynamodb_table):
        """get_invocation returns InvocationItem when event_id matches."""
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "event_id": "inv-target",
                    "invocation_id": "inv-target",
                    "arrived_at": "2026-06-20T10:00:00Z",
                    "channel": "github",
                    "status": "complete",
                    "status_updated_at": "2026-06-20T10:02:00Z",
                    "user_id": "user-1",
                    "topic": "Deploy service",
                }
            ],
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        item = service.get_invocation("inv-target", user_id="user-1")

        assert item is not None
        assert item.invocation_id == "inv-target"
        assert item.topic == "Deploy service"
        assert item.completed_at == "2026-06-20T10:02:00Z"

    def test_returns_none_when_not_found(self, mock_dynamodb_resource, mock_dynamodb_table):
        """get_invocation returns None when no item matches (user doesn't own it)."""
        mock_dynamodb_table.query.return_value = {"Items": []}

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        item = service.get_invocation("inv-nonexistent", user_id="user-1")

        assert item is None

    def test_returns_none_without_scope(self, mock_dynamodb_resource, mock_dynamodb_table):
        """get_invocation returns None if neither user_id nor tenant_id provided (safety)."""
        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        item = service.get_invocation("inv-123")

        assert item is None
        mock_dynamodb_table.query.assert_not_called()

    def test_uses_user_index_for_user_scope(self, mock_dynamodb_resource, mock_dynamodb_table):
        """get_invocation queries user-index when user_id is provided."""
        mock_dynamodb_table.query.return_value = {"Items": []}

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        service.get_invocation("inv-123", user_id="user-abc")

        call_kwargs = mock_dynamodb_table.query.call_args[1]
        assert call_kwargs["IndexName"] == "user-index"

    def test_uses_tenant_index_for_admin_scope(self, mock_dynamodb_resource, mock_dynamodb_table):
        """get_invocation queries tenant-index when tenant_id is provided."""
        mock_dynamodb_table.query.return_value = {"Items": []}

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        service.get_invocation("inv-123", tenant_id="org-001")

        call_kwargs = mock_dynamodb_table.query.call_args[1]
        assert call_kwargs["IndexName"] == "tenant-index"

    def test_paginates_when_not_found_on_first_page(self, mock_dynamodb_resource, mock_dynamodb_table):
        """get_invocation paginates internally (up to 5 pages) to find the item."""
        # First page: no match, has more pages
        # Second page: match found
        mock_dynamodb_table.query.side_effect = [
            {"Items": [], "LastEvaluatedKey": {"pk": "x", "arrived_at": "y", "user_id": "z"}},
            {
                "Items": [
                    {
                        "event_id": "inv-deep",
                        "invocation_id": "inv-deep",
                        "arrived_at": "2026-06-01T00:00:00Z",
                        "status": "complete",
                        "status_updated_at": "2026-06-01T00:05:00Z",
                        "user_id": "user-1",
                    }
                ],
            },
        ]

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        item = service.get_invocation("inv-deep", user_id="user-1")

        assert item is not None
        assert item.invocation_id == "inv-deep"
        assert mock_dynamodb_table.query.call_count == 2

    def test_graceful_degradation_on_error(self, mock_dynamodb_resource, mock_dynamodb_table):
        """get_invocation returns None on ValidationException (missing GSI)."""
        mock_dynamodb_table.query.side_effect = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "Index not found"}},
            "Query",
        )

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        item = service.get_invocation("inv-123", user_id="user-1")

        assert item is None
