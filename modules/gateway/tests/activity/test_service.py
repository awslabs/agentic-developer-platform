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
        mock_dynamodb_table.scan.return_value = {
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
        mock_dynamodb_table.scan.return_value = {
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
        mock_dynamodb_table.scan.return_value = {"Items": items, "Count": 10}

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.get_chain("chain-deep", user_id="user-1", depth_cap=5)

        assert result.total_count == 5
        assert result.depth_capped is True

    def test_chain_missing_table_returns_empty(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Chain query with missing table returns empty gracefully."""
        mock_dynamodb_table.scan.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "Table not found"}},
            "Scan",
        )

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.get_chain("chain-001", user_id="user-1")

        assert result.items == []
        assert result.total_count == 0

    def test_chain_flat_fallback_no_parent_edges(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Chain with no parent edges renders as flat list (all roots)."""
        mock_dynamodb_table.scan.return_value = {
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
