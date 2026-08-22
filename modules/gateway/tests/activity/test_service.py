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

        # Issue #3705: query_by_user now makes two calls (user-index + root-human-index).
        # Check the FIRST call targets user-index with the correct partition key.
        first_call_kwargs = mock_dynamodb_table.query.call_args_list[0][1]
        assert first_call_kwargs["IndexName"] == "user-index"
        # Verify the key condition uses user_id as PK and the correct value
        key_expr = first_call_kwargs["KeyConditionExpression"]
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

    def test_invocation_id_from_event_id_real_schema(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Issue #1756 regression: real webhook-events rows key the invocation by
        `event_id` ONLY — no `invocation_id`/`pk` attribute. The old mapping
        defaulted to "" so invocation_id was BLANK on every item, breaking the
        cost-join (cost_map.get("")) and the chain tree. Assert event_id is used.

        (The original mapping test used a fixture with BOTH pk and invocation_id —
        which production rows never have — so it never caught this.)
        """
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    # NOTE: only event_id, exactly like a production row
                    "event_id": "fdeadead-ca8f-4768-9b41-0b77315e9070",
                    "arrived_at": "2026-06-24T11:27:00Z",
                    "channel": "github",
                    "status": "complete",
                    "persona": "developer",
                    "correlation_id": "corr-x",
                    "run_id": "agent-scaledjob-zrcdp-hhjqc",
                    "user_id": "user-1",
                    "tenant_id": "org-1",
                }
            ],
            "Count": 1,
        }
        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_by_user(user_id="user-1")
        item = result.items[0]
        # The key fix: invocation_id is populated from event_id, NOT blank.
        assert item.invocation_id == "fdeadead-ca8f-4768-9b41-0b77315e9070"
        assert item.invocation_id != ""

    def test_accumulates_across_pages_when_filter_thins_them(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Issue #1757: DynamoDB Limit applies BEFORE the FilterExpression, and
        the table is dominated by no_op rows. A single raw page can yield very
        few triggering items even though more exist on later pages. The query
        must keep fetching until it has page_size post-filter items.

        Here page 1 returns 1 triggering row + LEK, page 2 returns 2 more + LEK,
        page 3 returns 1 more and exhausts the index. With page_size=3 we expect
        the loop to fetch enough pages to collect >= 3 items (not stop at 1).
        """
        page = lambda inv, lek: {  # noqa: E731
            "Items": [
                {
                    "event_id": inv,
                    "arrived_at": "2026-06-24T10:00:00Z",
                    "status": "complete",
                    "persona": "developer",
                    "user_id": "user-1",
                }
            ],
            "Count": 1,
            **({"LastEvaluatedKey": {"pk": inv}} if lek else {}),
        }
        mock_dynamodb_table.query.side_effect = [
            page("inv-1", True),
            page("inv-2", True),
            page("inv-3", False),  # exhausted
            # Issue #3705: root-human-index query (returns empty — no chain runs)
            {"Items": [], "Count": 0},
        ]
        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_by_user(user_id="user-1", page_size=3)

        # Without the accumulation loop this would have returned just 1 item.
        assert result.count == 3
        assert {i.invocation_id for i in result.items} == {"inv-1", "inv-2", "inv-3"}

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

        # Issue #3705: First call is user-index with the cursor as ExclusiveStartKey
        first_call_kwargs = mock_dynamodb_table.query.call_args_list[0][1]
        assert first_call_kwargs["ExclusiveStartKey"] == start_key

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


class TestChainNonTriggeringFilter:
    """Issue #3708: Chain query filters non-triggering statuses by default.

    Fixtures mirror the real DDB shape from embark1: no_op rows carry a bot
    user_id, parent_invocation_id pointing at the root event_id, and the same
    correlation_id. Real child runs are bot-attributed but have triggering
    statuses (in_progress, complete) and MUST survive the filter.
    """

    # Real DDB shape: 2 real runs + 5 no_op webhook echoes (bot-attributed)
    _ROOT_USER = "650f093f-ecd9-4ce1-a5a9-368e02c449cf"
    _BOT_USER = "edc91ba7-bot-user-id"
    _CORRELATION = "7993bfd5-chain-correlation"

    @property
    def _fixture_items(self) -> list[dict]:
        """Fixture mirroring real DDB shape: root + real child + 5 no_op echoes."""
        return [
            {
                "invocation_id": "root-event-001",
                "arrived_at": "2026-07-11T09:18:00Z",
                "channel": "github",
                "status": "in_progress",
                "topic": "Orchestration root",
                "user_id": self._ROOT_USER,
                "tenant_id": "org-embark1",
                "correlation_id": self._CORRELATION,
                "is_human_rooted": True,
                "root_human_id": self._ROOT_USER,
            },
            {
                "invocation_id": "child-real-001",
                "arrived_at": "2026-07-11T09:18:02Z",
                "channel": "github",
                "status": "in_progress",
                "topic": "Real child agent",
                "user_id": self._BOT_USER,
                "tenant_id": "org-embark1",
                "correlation_id": self._CORRELATION,
                "parent_invocation_id": "root-event-001",
                "is_human_rooted": True,
                "root_human_id": self._ROOT_USER,
            },
            # no_op webhook echoes — bot user, parent points at root
            *[
                {
                    "invocation_id": f"noop-echo-{i:03d}",
                    "arrived_at": f"2026-07-11T09:18:{1 + i * 5:02d}Z",
                    "channel": "github",
                    "status": "no_op",
                    "topic": "status-comment edit echo",
                    "user_id": self._BOT_USER,
                    "tenant_id": "org-embark1",
                    "correlation_id": self._CORRELATION,
                    "parent_invocation_id": "root-event-001",
                    "is_human_rooted": True,
                    "root_human_id": self._ROOT_USER,
                }
                for i in range(5)
            ],
        ]

    def test_default_excludes_no_op_echoes(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Default chain (include_non_triggering=False) returns only real runs."""
        mock_dynamodb_table.query.return_value = {
            "Items": [item for item in self._fixture_items if item["status"] != "no_op"],
            "Count": 2,
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.get_chain(self._CORRELATION, user_id=self._ROOT_USER)

        # Verify FilterExpression was used (the mock returns pre-filtered items
        # to simulate DDB's FilterExpression behavior)
        call_kwargs = mock_dynamodb_table.query.call_args[1]
        assert "FilterExpression" in call_kwargs

        # Only 2 real runs returned
        assert result.total_count == 2
        # Tree: root has 1 real child
        assert len(result.items) == 1
        assert result.items[0].invocation_id == "root-event-001"
        assert len(result.items[0].children) == 1
        assert result.items[0].children[0].invocation_id == "child-real-001"

    def test_include_non_triggering_returns_all(self, mock_dynamodb_resource, mock_dynamodb_table):
        """include_non_triggering=True returns all items including no_op echoes."""
        mock_dynamodb_table.query.return_value = {
            "Items": self._fixture_items,
            "Count": 7,
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.get_chain(self._CORRELATION, user_id=self._ROOT_USER, include_non_triggering=True)

        # All 7 items returned (2 real + 5 no_op)
        assert result.total_count == 7

    def test_bot_attributed_real_child_survives_membership_scoping(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Issue #3949: Bot-attributed child survives because membership-based scoping
        authorizes the chain as a whole (any member has user_id or root_human_id =
        caller), then returns ALL members unfiltered. No per-row user_id filter.

        This test asserts on the query args: FilterExpression contains ONLY the
        status filter (no user_id condition), proving membership-based scoping.
        """
        # Chain: root (owned by human) + bot-owned child with root_human_id
        items_with_complete_child = [item for item in self._fixture_items if item["status"] != "no_op"] + [
            {
                "invocation_id": "child-complete-bot",
                "arrived_at": "2026-07-11T09:20:00Z",
                "channel": "github",
                "status": "complete",
                "topic": "Bot child completed",
                "user_id": self._BOT_USER,
                "tenant_id": "org-embark1",
                "correlation_id": self._CORRELATION,
                "parent_invocation_id": "root-event-001",
                "is_human_rooted": True,
                "root_human_id": self._ROOT_USER,
            },
        ]
        mock_dynamodb_table.query.return_value = {
            "Items": items_with_complete_child,
            "Count": 3,
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.get_chain(self._CORRELATION, user_id=self._ROOT_USER)

        # Assert: FilterExpression does NOT contain user_id scoping
        call_kwargs = mock_dynamodb_table.query.call_args[1]
        filter_expr = call_kwargs.get("FilterExpression")
        # The filter should only be the status exclusion (no user_id/root_human_id)
        if filter_expr is not None:
            expr_str = str(filter_expr.get_expression())
            assert "user_id" not in expr_str

        # All 3 real items returned (bot-attributed child survives membership auth)
        assert result.total_count == 3
        # Root has 2 children
        assert len(result.items) == 1
        children_ids = [c.invocation_id for c in result.items[0].children]
        assert "child-real-001" in children_ids
        assert "child-complete-bot" in children_ids

    def test_depth_cap_not_consumed_by_filtered_echoes(self, mock_dynamodb_resource, mock_dynamodb_table):
        """depth_cap applies after filtering — echoes don't waste cap budget.

        With depth_cap=3 and 2 real runs + 5 no_op echoes, the 2 real runs
        should all be returned (cap not reached) with depth_capped=False.
        """
        # Simulate DDB returning only real items (FilterExpression removed no_op)
        real_items = [item for item in self._fixture_items if item["status"] != "no_op"]
        mock_dynamodb_table.query.return_value = {
            "Items": real_items,
            "Count": 2,
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.get_chain(self._CORRELATION, user_id=self._ROOT_USER, depth_cap=3)

        # 2 real items fit within cap of 3 — not capped
        assert result.total_count == 2
        assert result.depth_capped is False

    def test_webhook_received_also_filtered(self, mock_dynamodb_resource, mock_dynamodb_table):
        """webhook_received status is also filtered out by default."""
        items_with_webhook = [item for item in self._fixture_items if item["status"] != "no_op"]
        # DDB already filtered — simulate it returning only real items
        mock_dynamodb_table.query.return_value = {
            "Items": items_with_webhook,
            "Count": 2,
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.get_chain(self._CORRELATION, user_id=self._ROOT_USER)

        # Verify the FilterExpression includes status filter
        call_kwargs = mock_dynamodb_table.query.call_args[1]
        filter_expr = call_kwargs.get("FilterExpression")
        assert filter_expr is not None
        # Only real items returned
        assert result.total_count == 2

    def test_tenant_scope_combined_with_status_filter(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Tenant-scoped chain also applies status filter (combined FilterExpression)."""
        real_items = [item for item in self._fixture_items if item["status"] != "no_op"]
        mock_dynamodb_table.query.return_value = {
            "Items": real_items,
            "Count": 2,
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.get_chain(self._CORRELATION, tenant_id="org-embark1")

        call_kwargs = mock_dynamodb_table.query.call_args[1]
        assert "FilterExpression" in call_kwargs
        assert result.total_count == 2


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
    """Tests for get_invocation — base-table Query + authorize-after-fetch (Issue #3949).

    The new implementation queries the base table by event_id (hash key) for
    O(1) lookup, then authorizes in code. No GSI, no pagination loop.
    """

    def test_returns_item_when_found_user_scope(self, mock_dynamodb_resource, mock_dynamodb_table):
        """get_invocation returns InvocationItem when event_id matches and user owns it."""
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "event_id": "inv-target",
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

    def test_base_table_query_no_index(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Issue #3949: get_invocation uses base-table Query (no IndexName), single call."""
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "event_id": "inv-123",
                    "arrived_at": "2026-07-29T10:00:00Z",
                    "status": "complete",
                    "user_id": "user-1",
                }
            ],
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        service.get_invocation("inv-123", user_id="user-1")

        # Single query, no IndexName (base table), KeyCondition on event_id
        assert mock_dynamodb_table.query.call_count == 1
        call_kwargs = mock_dynamodb_table.query.call_args[1]
        assert "IndexName" not in call_kwargs
        # Verify KeyConditionExpression uses event_id
        key_expr = call_kwargs["KeyConditionExpression"]
        expr_dict = key_expr.get_expression()
        key_obj = expr_dict["values"][0]
        assert key_obj.name == "event_id"
        assert expr_dict["values"][1] == "inv-123"

    def test_returns_none_when_not_found(self, mock_dynamodb_resource, mock_dynamodb_table):
        """get_invocation returns None when no item exists with that event_id."""
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

    def test_chain_attributed_authorized_via_root_human_id(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Issue #3949: chain-attributed row (user_id=bot, root_human_id=caller) is authorized."""
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "event_id": "inv-chain-bot",
                    "arrived_at": "2026-07-29T10:21:00Z",
                    "channel": "github",
                    "status": "complete",
                    "status_updated_at": "2026-07-29T10:25:00Z",
                    "user_id": "aws-e-adp-agent-dev[bot]",
                    "root_human_id": "user-human-1",
                    "transcript_key": "developer/org/repo/issue-42/transcript.md",
                    "correlation_id": "corr-abc",
                }
            ],
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        item = service.get_invocation("inv-chain-bot", user_id="user-human-1")

        assert item is not None
        assert item.invocation_id == "inv-chain-bot"
        assert item.transcript_key == "developer/org/repo/issue-42/transcript.md"
        assert item.root_human_id == "user-human-1"

    def test_cross_user_disclosure_blocked(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Issue #3949: row with user_id=bot, root_human_id=other-user → None for attacker.

        Asserts on the authorization path: the row IS returned by DDB, but the
        code rejects it because neither user_id nor root_human_id match the caller.
        """
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "event_id": "inv-victim-run",
                    "arrived_at": "2026-07-29T10:00:00Z",
                    "status": "complete",
                    "user_id": "aws-e-adp-agent-dev[bot]",
                    "root_human_id": "user-victim",
                    "tenant_id": "org-shared",
                }
            ],
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        item = service.get_invocation("inv-victim-run", user_id="user-attacker")

        # Row exists in DDB but authorization rejects it
        assert item is None
        # The query DID execute (it's the authorization that blocks, not a miss)
        assert mock_dynamodb_table.query.call_count == 1

    def test_tenant_scope_authorized(self, mock_dynamodb_resource, mock_dynamodb_table):
        """get_invocation with tenant_id authorizes by tenant_id match."""
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "event_id": "inv-123",
                    "arrived_at": "2026-07-29T10:00:00Z",
                    "status": "complete",
                    "user_id": "bot-user",
                    "tenant_id": "org-001",
                }
            ],
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        item = service.get_invocation("inv-123", tenant_id="org-001")

        assert item is not None
        assert item.invocation_id == "inv-123"

    def test_tenant_scope_rejected(self, mock_dynamodb_resource, mock_dynamodb_table):
        """get_invocation with wrong tenant_id returns None."""
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "event_id": "inv-123",
                    "arrived_at": "2026-07-29T10:00:00Z",
                    "status": "complete",
                    "user_id": "bot-user",
                    "tenant_id": "org-other",
                }
            ],
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        item = service.get_invocation("inv-123", tenant_id="org-attacker")

        assert item is None

    def test_graceful_degradation_on_error(self, mock_dynamodb_resource, mock_dynamodb_table):
        """get_invocation returns None on ValidationException (table error)."""
        mock_dynamodb_table.query.side_effect = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "Table not found"}},
            "Query",
        )

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        item = service.get_invocation("inv-123", user_id="user-1")

        assert item is None


# ---------------------------------------------------------------------------
# Issue #1658 tests: include_non_triggering filter
# ---------------------------------------------------------------------------


class TestIncludeNonTriggering:
    """Tests for the include_non_triggering filter (issue #1658).

    Default behavior (include_non_triggering=False): excludes no_op and
    webhook_received rows. When True, shows all statuses. An explicit
    status filter always takes precedence.
    """

    def test_default_excludes_non_triggering(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Default (include_non_triggering=False) adds a NOT-IN filter for no_op/webhook_received."""
        mock_dynamodb_table.query.return_value = {"Items": [], "Count": 0}

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        service.query_by_user(user_id="user-1")

        call_kwargs = mock_dynamodb_table.query.call_args[1]
        # Should have a FilterExpression excluding non-triggering statuses
        assert "FilterExpression" in call_kwargs

    def test_include_non_triggering_true_no_exclusion_filter(self, mock_dynamodb_resource, mock_dynamodb_table):
        """include_non_triggering=True does NOT add the exclusion filter."""
        mock_dynamodb_table.query.return_value = {"Items": [], "Count": 0}

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        service.query_by_user(user_id="user-1", include_non_triggering=True)

        call_kwargs = mock_dynamodb_table.query.call_args[1]
        # No filter expression when showing all and no other filters
        assert "FilterExpression" not in call_kwargs

    def test_explicit_status_overrides_exclusion(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Explicit status=no_op returns no_op even with include_non_triggering=False."""
        mock_dynamodb_table.query.return_value = {"Items": [], "Count": 0}

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        service.query_by_user(user_id="user-1", status="no_op", include_non_triggering=False)

        call_kwargs = mock_dynamodb_table.query.call_args[1]
        # Filter should be an equality check for no_op, not the exclusion
        assert "FilterExpression" in call_kwargs
        # The filter is Attr("status").eq("no_op") — it includes no_op, not excludes it

    def test_rejected_shown_by_default(self, mock_dynamodb_resource, mock_dynamodb_table):
        """rejected status is NOT excluded by the default filter (it's a run status)."""
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "invocation_id": "inv-rejected",
                    "arrived_at": "2026-06-20T10:00:00Z",
                    "channel": "github",
                    "status": "rejected",
                    "user_id": "user-1",
                }
            ],
            "Count": 1,
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_by_user(user_id="user-1")

        # rejected is not excluded — it should appear in results
        assert result.count == 1
        assert result.items[0].status == "rejected"

    def test_rate_limited_shown_by_default(self, mock_dynamodb_resource, mock_dynamodb_table):
        """rate_limited status is NOT excluded by the default filter."""
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "invocation_id": "inv-ratelimit",
                    "arrived_at": "2026-06-20T10:00:00Z",
                    "channel": "github",
                    "status": "rate_limited",
                    "user_id": "user-1",
                }
            ],
            "Count": 1,
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_by_user(user_id="user-1")

        assert result.count == 1
        assert result.items[0].status == "rate_limited"

    def test_tenant_query_default_excludes_non_triggering(self, mock_dynamodb_resource, mock_dynamodb_table):
        """query_by_tenant also excludes non-triggering by default."""
        mock_dynamodb_table.query.return_value = {"Items": [], "Count": 0}

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        service.query_by_tenant(tenant_id="org-001")

        call_kwargs = mock_dynamodb_table.query.call_args[1]
        assert "FilterExpression" in call_kwargs

    def test_tenant_query_include_non_triggering_true(self, mock_dynamodb_resource, mock_dynamodb_table):
        """query_by_tenant with include_non_triggering=True shows all."""
        mock_dynamodb_table.query.return_value = {"Items": [], "Count": 0}

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        service.query_by_tenant(tenant_id="org-001", include_non_triggering=True)

        call_kwargs = mock_dynamodb_table.query.call_args[1]
        assert "FilterExpression" not in call_kwargs

    def test_default_filter_combines_with_channel_filter(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Default exclusion filter AND a channel filter both apply together."""
        mock_dynamodb_table.query.return_value = {"Items": [], "Count": 0}

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        service.query_by_user(user_id="user-1", channel="github")

        call_kwargs = mock_dynamodb_table.query.call_args[1]
        # Should have both the non-triggering exclusion AND the channel filter
        assert "FilterExpression" in call_kwargs

    def test_pagination_with_exclusion_filter(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Short page with non-null last_key still works with the exclusion filter."""
        mock_dynamodb_table.query.return_value = {
            "Items": [],
            "Count": 0,
            "LastEvaluatedKey": {"pk": "inv-050", "arrived_at": "2026-06-01T00:00:00Z", "user_id": "u1"},
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_by_user(user_id="u1")

        # Respects the filtered-pagination contract: 0 items but cursor present
        assert result.items == []
        assert result.count == 0
        assert result.last_key is not None


# ---------------------------------------------------------------------------
# Issue #1662 tests: Chain-grouped board view (query_chains_by_user)
# ---------------------------------------------------------------------------


class TestQueryChainsByUser:
    """Tests for query_chains_by_user — the chain-grouped board view."""

    def test_singleton_chain_no_correlation_id(self, mock_dynamodb_resource, mock_dynamodb_table):
        """A root with no correlation_id is a singleton (no descendants, chain_id = invocation_id)."""
        # user-index query returns one item without correlation_id
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "invocation_id": "inv-solo",
                    "arrived_at": "2026-06-22T10:00:00Z",
                    "channel": "github",
                    "status": "complete",
                    "user_id": "user-1",
                }
            ],
            "Count": 1,
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_chains_by_user(user_id="user-1")

        assert result.count == 1
        chain = result.chains[0]
        assert chain.chain_id == "inv-solo"
        assert chain.root.invocation_id == "inv-solo"
        assert chain.descendant_count == 0
        assert chain.descendants == []

    def test_multi_run_chain_fetches_descendants(self, mock_dynamodb_resource, mock_dynamodb_table):
        """A root with correlation_id fetches descendants from correlation-index."""
        # First call: user-index query returns root
        # Second call: root-human-index (Issue #3723 merged fetch — empty here)
        # Third call: correlation-index query returns root + descendants
        mock_dynamodb_table.query.side_effect = [
            # user-index result (root)
            {
                "Items": [
                    {
                        "invocation_id": "inv-root",
                        "arrived_at": "2026-06-22T10:00:00Z",
                        "channel": "github",
                        "status": "complete",
                        "topic": "Deploy ADP",
                        "persona": "developer",
                        "source_url": "https://github.com/aws-e/adp/issues/1320",
                        "repo": "aws-e/adp",
                        "issue_number": 1320,
                        "user_id": "user-1",
                        "correlation_id": "corr-001",
                        "is_human_rooted": True,
                        "root_human_id": "user-1",
                    }
                ],
                "Count": 1,
            },
            # root-human-index result (Issue #3723 — no additional roots)
            {"Items": [], "Count": 0},
            # correlation-index result (root + 2 descendants)
            {
                "Items": [
                    {
                        "invocation_id": "inv-root",
                        "arrived_at": "2026-06-22T10:00:00Z",
                        "channel": "github",
                        "status": "complete",
                        "topic": "Deploy ADP",
                        "persona": "developer",
                        "user_id": "user-1",
                        "correlation_id": "corr-001",
                    },
                    {
                        "invocation_id": "inv-child-1",
                        "arrived_at": "2026-06-22T10:05:00Z",
                        "channel": "github",
                        "status": "complete",
                        "topic": "Code review",
                        "persona": "reviewer",
                        "user_id": "user-1",
                        "correlation_id": "corr-001",
                        "parent_invocation_id": "inv-root",
                    },
                    {
                        "invocation_id": "inv-child-2",
                        "arrived_at": "2026-06-22T10:10:00Z",
                        "channel": "github",
                        "status": "in_progress",
                        "topic": "Deploy follow-up",
                        "persona": "ops",
                        "user_id": "user-1",
                        "correlation_id": "corr-001",
                        "parent_invocation_id": "inv-root",
                    },
                ],
                "Count": 3,
            },
        ]

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_chains_by_user(user_id="user-1")

        assert result.count == 1
        chain = result.chains[0]
        assert chain.chain_id == "corr-001"
        assert chain.root.invocation_id == "inv-root"
        assert chain.root.topic == "Deploy ADP"
        assert chain.descendant_count == 2
        assert len(chain.descendants) == 2
        # Descendants are time-ordered
        assert chain.descendants[0].invocation_id == "inv-child-1"
        assert chain.descendants[0].topic == "Code review"
        assert chain.descendants[0].persona == "reviewer"
        assert chain.descendants[1].invocation_id == "inv-child-2"
        assert chain.descendants[1].topic == "Deploy follow-up"

    def test_excludes_non_triggering_descendants_by_default(self, mock_dynamodb_resource, mock_dynamodb_table):
        """no_op and webhook_received descendants are excluded by default."""
        mock_dynamodb_table.query.side_effect = [
            # user-index result
            {
                "Items": [
                    {
                        "invocation_id": "inv-root",
                        "arrived_at": "2026-06-22T10:00:00Z",
                        "status": "complete",
                        "user_id": "user-1",
                        "correlation_id": "corr-002",
                    }
                ],
                "Count": 1,
            },
            # root-human-index result (Issue #3723 — no additional roots)
            {"Items": [], "Count": 0},
            # correlation-index: root + no_op + webhook_received + real child
            {
                "Items": [
                    {
                        "invocation_id": "inv-root",
                        "arrived_at": "2026-06-22T10:00:00Z",
                        "status": "complete",
                        "user_id": "user-1",
                        "correlation_id": "corr-002",
                    },
                    {
                        "invocation_id": "inv-noop",
                        "arrived_at": "2026-06-22T10:01:00Z",
                        "status": "no_op",
                        "user_id": "user-1",
                        "correlation_id": "corr-002",
                    },
                    {
                        "invocation_id": "inv-webhook",
                        "arrived_at": "2026-06-22T10:02:00Z",
                        "status": "webhook_received",
                        "user_id": "user-1",
                        "correlation_id": "corr-002",
                    },
                    {
                        "invocation_id": "inv-real",
                        "arrived_at": "2026-06-22T10:03:00Z",
                        "status": "in_progress",
                        "topic": "Real task",
                        "user_id": "user-1",
                        "correlation_id": "corr-002",
                    },
                ],
                "Count": 4,
            },
        ]

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_chains_by_user(user_id="user-1")

        chain = result.chains[0]
        # Only the real child should appear (no_op and webhook_received excluded)
        assert chain.descendant_count == 1
        assert chain.descendants[0].invocation_id == "inv-real"

    def test_includes_non_triggering_when_flag_set(self, mock_dynamodb_resource, mock_dynamodb_table):
        """include_non_triggering=True includes no_op/webhook_received descendants."""
        mock_dynamodb_table.query.side_effect = [
            # user-index result (include_non_triggering=True doesn't exclude roots)
            {
                "Items": [
                    {
                        "invocation_id": "inv-root",
                        "arrived_at": "2026-06-22T10:00:00Z",
                        "status": "complete",
                        "user_id": "user-1",
                        "correlation_id": "corr-003",
                    }
                ],
                "Count": 1,
            },
            # root-human-index result (Issue #3723 — no additional roots)
            {"Items": [], "Count": 0},
            # correlation-index: root + no_op descendant
            {
                "Items": [
                    {
                        "invocation_id": "inv-root",
                        "arrived_at": "2026-06-22T10:00:00Z",
                        "status": "complete",
                        "user_id": "user-1",
                        "correlation_id": "corr-003",
                    },
                    {
                        "invocation_id": "inv-noop",
                        "arrived_at": "2026-06-22T10:01:00Z",
                        "status": "no_op",
                        "user_id": "user-1",
                        "correlation_id": "corr-003",
                    },
                ],
                "Count": 2,
            },
        ]

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_chains_by_user(user_id="user-1", include_non_triggering=True)

        chain = result.chains[0]
        assert chain.descendant_count == 1
        assert chain.descendants[0].invocation_id == "inv-noop"

    def test_pagination_cursor_preserved(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Chain pagination cursor = user-index LastEvaluatedKey.

        Issue #1757: _execute_query now accumulates across raw DDB pages until it
        has page_size POST-FILTER items (DynamoDB applies Limit BEFORE the
        FilterExpression, and the table is dominated by no_op rows). So this test
        must simulate real pagination with a side_effect SEQUENCE: page 1 returns
        a row + LastEvaluatedKey, page 2 returns nothing more and DROPS the
        LastEvaluatedKey (index exhausted). A static return_value would loop to
        the page cap. The final cursor reflects the last raw page's LEK.
        """
        mock_dynamodb_table.query.side_effect = [
            # user-index page 1 (has LEK)
            {
                "Items": [
                    {
                        "invocation_id": "inv-page1",
                        "arrived_at": "2026-06-22T10:00:00Z",
                        "status": "complete",
                        "user_id": "user-1",
                    }
                ],
                "Count": 1,
                "LastEvaluatedKey": {
                    "pk": "inv-page1",
                    "arrived_at": "2026-06-22T10:00:00Z",
                    "user_id": "user-1",
                },
            },
            # user-index page 2 (exhausted)
            {"Items": [], "Count": 0},
            # root-human-index result (Issue #3723 — no additional roots)
            {"Items": [], "Count": 0},
        ]

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_chains_by_user(user_id="user-1")

        # The triggering row is returned, and pagination terminates cleanly.
        assert result.count == 1

    def test_depth_cap_on_descendants(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Chain descendants are capped at the depth limit."""
        # Generate 60 items (exceeds default cap of 50)
        many_items = [
            {
                "invocation_id": f"inv-{i:03d}",
                "arrived_at": f"2026-06-22T{10 + (i // 60):02d}:{i % 60:02d}:00Z",
                "status": "complete",
                "user_id": "user-1",
                "correlation_id": "corr-big",
            }
            for i in range(60)
        ]

        mock_dynamodb_table.query.side_effect = [
            # user-index returns the root
            {
                "Items": [
                    {
                        "invocation_id": "inv-000",
                        "arrived_at": "2026-06-22T10:00:00Z",
                        "status": "complete",
                        "user_id": "user-1",
                        "correlation_id": "corr-big",
                    }
                ],
                "Count": 1,
            },
            # root-human-index result (Issue #3723 — no additional roots)
            {"Items": [], "Count": 0},
            # correlation-index returns all 60 items
            {"Items": many_items, "Count": 60},
        ]

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_chains_by_user(user_id="user-1")

        chain = result.chains[0]
        # Issue #3723: depth_cap now counts REAL descendants (post-filter).
        # With 60 items, root excluded = 59 descendants, cap = 50 → exactly 50.
        assert chain.descendant_count == 50

    def test_correlation_index_error_returns_empty_descendants(self, mock_dynamodb_resource, mock_dynamodb_table):
        """If correlation-index query fails, chain has zero descendants (graceful)."""
        mock_dynamodb_table.query.side_effect = [
            # user-index returns root
            {
                "Items": [
                    {
                        "invocation_id": "inv-root",
                        "arrived_at": "2026-06-22T10:00:00Z",
                        "status": "complete",
                        "user_id": "user-1",
                        "correlation_id": "corr-fail",
                    }
                ],
                "Count": 1,
            },
            # root-human-index result (Issue #3723 — no additional roots)
            {"Items": [], "Count": 0},
            # correlation-index fails
            ClientError(
                {"Error": {"Code": "ValidationException", "Message": "Index not found"}},
                "Query",
            ),
        ]

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_chains_by_user(user_id="user-1")

        assert result.count == 1
        chain = result.chains[0]
        assert chain.descendant_count == 0
        assert chain.descendants == []

    def test_multiple_chains_on_one_page(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Multiple roots on one page each get their own chain with descendants."""
        mock_dynamodb_table.query.side_effect = [
            # user-index returns 2 roots
            {
                "Items": [
                    {
                        "invocation_id": "inv-A",
                        "arrived_at": "2026-06-22T11:00:00Z",
                        "status": "complete",
                        "topic": "Chain A",
                        "user_id": "user-1",
                        "correlation_id": "corr-A",
                    },
                    {
                        "invocation_id": "inv-B",
                        "arrived_at": "2026-06-22T10:00:00Z",
                        "status": "in_progress",
                        "topic": "Chain B",
                        "user_id": "user-1",
                        "correlation_id": "corr-B",
                    },
                ],
                "Count": 2,
            },
            # root-human-index result (Issue #3723 — no additional roots)
            {"Items": [], "Count": 0},
            # correlation-index for corr-A (root + 1 descendant)
            {
                "Items": [
                    {
                        "invocation_id": "inv-A",
                        "arrived_at": "2026-06-22T11:00:00Z",
                        "status": "complete",
                        "user_id": "user-1",
                        "correlation_id": "corr-A",
                    },
                    {
                        "invocation_id": "inv-A-child",
                        "arrived_at": "2026-06-22T11:05:00Z",
                        "status": "complete",
                        "topic": "A child",
                        "persona": "reviewer",
                        "user_id": "user-1",
                        "correlation_id": "corr-A",
                    },
                ],
                "Count": 2,
            },
            # correlation-index for corr-B (root only = singleton)
            {
                "Items": [
                    {
                        "invocation_id": "inv-B",
                        "arrived_at": "2026-06-22T10:00:00Z",
                        "status": "in_progress",
                        "user_id": "user-1",
                        "correlation_id": "corr-B",
                    },
                ],
                "Count": 1,
            },
        ]

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_chains_by_user(user_id="user-1")

        assert result.count == 2
        # Chain A has 1 descendant
        chain_a = result.chains[0]
        assert chain_a.chain_id == "corr-A"
        assert chain_a.descendant_count == 1
        assert chain_a.descendants[0].invocation_id == "inv-A-child"
        # Chain B is singleton
        chain_b = result.chains[1]
        assert chain_b.chain_id == "corr-B"
        assert chain_b.descendant_count == 0

    def test_agent_descendants_on_user_page_dont_duplicate_chain(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Regression (#2058): after #2042, agent-spawned child runs are also
        attributed to the human's user_id, so the user-index page returns BOTH
        the human root AND its agent children — all sharing one correlation_id.
        The chain view must emit ONE top-level row (rooted at the human run),
        NOT one row per run. Children (those with a parent_invocation_id) must
        never appear as top-level chains, and a correlation_id must never be
        duplicated across top-level rows."""
        mock_dynamodb_table.query.side_effect = [
            # user-index now returns the human root AND two agent-spawned children,
            # all with the SAME correlation_id (the #2042 attribution change).
            {
                "Items": [
                    {
                        "invocation_id": "inv-human-root",
                        "arrived_at": "2026-06-22T10:00:00Z",
                        "status": "complete",
                        "topic": "Operations: orchestrate",
                        "persona": "operations",
                        "user_id": "user-1",
                        "correlation_id": "corr-dup",
                        "is_human_rooted": True,
                        "root_human_id": "user-1",
                    },
                    {
                        "invocation_id": "inv-agent-child-1",
                        "arrived_at": "2026-06-22T10:05:00Z",
                        "status": "complete",
                        "topic": "Developer: implement",
                        "persona": "developer",
                        "user_id": "user-1",
                        "correlation_id": "corr-dup",
                        "parent_invocation_id": "inv-human-root",
                        "is_human_rooted": True,
                        "root_human_id": "user-1",
                    },
                    {
                        "invocation_id": "inv-agent-child-2",
                        "arrived_at": "2026-06-22T10:10:00Z",
                        "status": "in_progress",
                        "topic": "Reviewer: review",
                        "persona": "reviewer",
                        "user_id": "user-1",
                        "correlation_id": "corr-dup",
                        "parent_invocation_id": "inv-agent-child-1",
                        "is_human_rooted": True,
                        "root_human_id": "user-1",
                    },
                ],
                "Count": 3,
            },
            # root-human-index result (Issue #3723 — no additional roots)
            {"Items": [], "Count": 0},
            # correlation-index for corr-dup (root + 2 descendants)
            {
                "Items": [
                    {
                        "invocation_id": "inv-human-root",
                        "arrived_at": "2026-06-22T10:00:00Z",
                        "status": "complete",
                        "user_id": "user-1",
                        "correlation_id": "corr-dup",
                    },
                    {
                        "invocation_id": "inv-agent-child-1",
                        "arrived_at": "2026-06-22T10:05:00Z",
                        "status": "complete",
                        "topic": "Developer: implement",
                        "persona": "developer",
                        "user_id": "user-1",
                        "correlation_id": "corr-dup",
                        "parent_invocation_id": "inv-human-root",
                    },
                    {
                        "invocation_id": "inv-agent-child-2",
                        "arrived_at": "2026-06-22T10:10:00Z",
                        "status": "in_progress",
                        "topic": "Reviewer: review",
                        "persona": "reviewer",
                        "user_id": "user-1",
                        "correlation_id": "corr-dup",
                        "parent_invocation_id": "inv-agent-child-1",
                    },
                ],
                "Count": 3,
            },
        ]

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_chains_by_user(user_id="user-1")

        # Exactly ONE top-level chain, rooted at the human run.
        assert result.count == 1
        chain = result.chains[0]
        assert chain.chain_id == "corr-dup"
        assert chain.root.invocation_id == "inv-human-root"
        assert chain.root.persona == "operations"
        # The agent children are nested as descendants, not top-level rows.
        assert chain.descendant_count == 2
        descendant_ids = {d.invocation_id for d in chain.descendants}
        assert descendant_ids == {"inv-agent-child-1", "inv-agent-child-2"}
        # correlation-index queried exactly once (not once per attributed run).
        correlation_queries = [c for c in mock_dynamodb_table.query.call_args_list if c.kwargs.get("IndexName") == "correlation-index"]
        assert len(correlation_queries) == 1


# ---------------------------------------------------------------------------
# Issue #3705 tests: root-human-index dual-query merge (list parity)
# ---------------------------------------------------------------------------


class TestQueryByUserRootHumanMerge:
    """Tests for Issue #3705: query_by_user dual-query with root-human-index.

    Validates:
    - Chain runs (root_human_id = caller) appear in the list
    - Dedup: item with user_id = root_human_id = caller is NOT double-counted
    - Scoping: other user's runs never appear
    - Missing GSI fallback: returns user-index-only results without error
    """

    def test_chain_runs_included_in_list(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Bot-attributed chain runs (root_human_id = caller) appear in list results."""

        def query_side_effect(**kwargs):
            index = kwargs.get("IndexName", "")
            if index == "user-index":
                return {
                    "Items": [
                        {
                            "event_id": "direct-1",
                            "arrived_at": "2026-07-10T10:00:00Z",
                            "status": "complete",
                            "user_id": "user-1",
                        }
                    ],
                    "Count": 1,
                }
            elif index == "root-human-index":
                return {
                    "Items": [
                        {
                            "event_id": "chain-1",
                            "arrived_at": "2026-07-10T10:05:00Z",
                            "status": "in_progress",
                            "user_id": "bot-svc-account",
                            "root_human_id": "user-1",
                        }
                    ],
                    "Count": 1,
                }
            return {"Items": [], "Count": 0}

        mock_dynamodb_table.query.side_effect = query_side_effect
        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_by_user(user_id="user-1")

        # Both runs should appear
        assert result.count == 2
        inv_ids = {i.invocation_id for i in result.items}
        assert "direct-1" in inv_ids
        assert "chain-1" in inv_ids

    def test_dedup_no_double_count(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Item with user_id = root_human_id = caller is NOT double-counted."""
        shared_item = {
            "event_id": "shared-1",
            "arrived_at": "2026-07-10T10:00:00Z",
            "status": "complete",
            "user_id": "user-1",
            "root_human_id": "user-1",
        }

        def query_side_effect(**kwargs):
            index = kwargs.get("IndexName", "")
            if index in ("user-index", "root-human-index"):
                return {"Items": [shared_item], "Count": 1}
            return {"Items": [], "Count": 0}

        mock_dynamodb_table.query.side_effect = query_side_effect
        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_by_user(user_id="user-1")

        # Only counted once
        assert result.count == 1
        assert result.items[0].invocation_id == "shared-1"

    def test_missing_gsi_fallback(self, mock_dynamodb_resource, mock_dynamodb_table):
        """If root-human-index is missing, falls back to user-index only."""

        def query_side_effect(**kwargs):
            index = kwargs.get("IndexName", "")
            if index == "user-index":
                return {
                    "Items": [
                        {
                            "event_id": "direct-1",
                            "arrived_at": "2026-07-10T10:00:00Z",
                            "status": "complete",
                            "user_id": "user-1",
                        }
                    ],
                    "Count": 1,
                }
            elif index == "root-human-index":
                raise ClientError(
                    {"Error": {"Code": "ValidationException", "Message": "Index not found"}},
                    "Query",
                )
            return {"Items": [], "Count": 0}

        mock_dynamodb_table.query.side_effect = query_side_effect
        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_by_user(user_id="user-1")

        # Should NOT raise; returns user-index-only results
        assert result.count == 1
        assert result.items[0].invocation_id == "direct-1"

    def test_sorted_by_arrived_at_descending(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Merged results are sorted newest-first (arrived_at descending)."""

        def query_side_effect(**kwargs):
            index = kwargs.get("IndexName", "")
            if index == "user-index":
                return {
                    "Items": [
                        {
                            "event_id": "old-direct",
                            "arrived_at": "2026-07-10T08:00:00Z",
                            "status": "complete",
                            "user_id": "user-1",
                        }
                    ],
                    "Count": 1,
                }
            elif index == "root-human-index":
                return {
                    "Items": [
                        {
                            "event_id": "new-chain",
                            "arrived_at": "2026-07-10T12:00:00Z",
                            "status": "in_progress",
                            "user_id": "bot-svc",
                            "root_human_id": "user-1",
                        }
                    ],
                    "Count": 1,
                }
            return {"Items": [], "Count": 0}

        mock_dynamodb_table.query.side_effect = query_side_effect
        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_by_user(user_id="user-1")

        # Newest first
        assert result.items[0].invocation_id == "new-chain"
        assert result.items[1].invocation_id == "old-direct"

    def test_page_size_respected_after_merge(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Merged results are trimmed to page_size."""

        def query_side_effect(**kwargs):
            index = kwargs.get("IndexName", "")
            if index == "user-index":
                items = [
                    {
                        "event_id": f"direct-{i}",
                        "arrived_at": f"2026-07-10T{10 + i:02d}:00:00Z",
                        "status": "complete",
                        "user_id": "user-1",
                    }
                    for i in range(3)
                ]
                return {"Items": items, "Count": 3}
            elif index == "root-human-index":
                items = [
                    {
                        "event_id": f"chain-{i}",
                        "arrived_at": f"2026-07-10T{13 + i:02d}:00:00Z",
                        "status": "complete",
                        "user_id": "bot-svc",
                        "root_human_id": "user-1",
                    }
                    for i in range(3)
                ]
                return {"Items": items, "Count": 3}
            return {"Items": [], "Count": 0}

        mock_dynamodb_table.query.side_effect = query_side_effect
        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_by_user(user_id="user-1", page_size=4)

        # 6 total items, but page_size=4 trims the result
        assert result.count == 4


# ---------------------------------------------------------------------------
# Issue #3723 tests: Chain view merged fetch + depth cap + filter logic
# ---------------------------------------------------------------------------


class TestQueryChainsByUserMergedFetch:
    """Issue #3723: query_chains_by_user merged fetch from root-human-index.

    Validates:
    - Bot-attributed roots (root_human_id = caller, user_id = bot) appear in
      the chain view (same merge as query_by_user).
    - Dedup: a run appearing in BOTH user-index and root-human-index does not
      produce duplicate chain rows.
    - The chain view returns the SAME run count as the flat list (consistency
      between dashboard tile and /activity?status=in_progress).
    """

    _ROOT_USER = "650f093f-ecd9-4ce1-a5a9-368e02c449cf"
    _BOT_USER = "edc91ba7-bot-user-id"

    def test_bot_attributed_roots_included(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Bot-attributed roots (root_human_id = caller) appear in chain view.

        This mirrors the live evidence: 1 direct run (developer) + 2 reviewer
        runs (bot-attributed, root_human_id = caller). The chain view must show
        all 3 as top-level chains, not just the 1 from user-index.
        """

        def query_side_effect(**kwargs):
            index = kwargs.get("IndexName", "")
            if index == "user-index":
                # Direct run only (developer — human user_id)
                return {
                    "Items": [
                        {
                            "invocation_id": "feae00a8-developer",
                            "arrived_at": "2026-07-11T09:18:00Z",
                            "channel": "github",
                            "status": "in_progress",
                            "topic": "Implement feature",
                            "persona": "developer",
                            "user_id": self._ROOT_USER,
                            "correlation_id": "corr-dev",
                            "is_human_rooted": True,
                            "root_human_id": self._ROOT_USER,
                        }
                    ],
                    "Count": 1,
                }
            elif index == "root-human-index":
                # Bot-attributed roots (reviewer runs)
                return {
                    "Items": [
                        {
                            "invocation_id": "a488466e-reviewer-1",
                            "arrived_at": "2026-07-11T09:20:00Z",
                            "channel": "github",
                            "status": "in_progress",
                            "topic": "Review PR #123",
                            "persona": "reviewer",
                            "user_id": self._BOT_USER,
                            "correlation_id": "corr-rev1",
                            "is_human_rooted": True,
                            "root_human_id": self._ROOT_USER,
                        },
                        {
                            "invocation_id": "42ed0a12-reviewer-2",
                            "arrived_at": "2026-07-11T09:22:00Z",
                            "channel": "github",
                            "status": "in_progress",
                            "topic": "Review PR #124",
                            "persona": "reviewer",
                            "user_id": self._BOT_USER,
                            "correlation_id": "corr-rev2",
                            "is_human_rooted": True,
                            "root_human_id": self._ROOT_USER,
                        },
                    ],
                    "Count": 2,
                }
            elif index == "correlation-index":
                # Each chain is a singleton (root only, no descendants)
                return {"Items": [], "Count": 0}
            return {"Items": [], "Count": 0}

        mock_dynamodb_table.query.side_effect = query_side_effect
        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_chains_by_user(user_id=self._ROOT_USER, status="in_progress")

        # Must see all 3 chains (1 direct + 2 bot-attributed)
        assert result.count == 3
        chain_ids = {c.root.invocation_id for c in result.chains}
        assert chain_ids == {"feae00a8-developer", "a488466e-reviewer-1", "42ed0a12-reviewer-2"}

    def test_dedup_on_invocation_id(self, mock_dynamodb_resource, mock_dynamodb_table):
        """A run in BOTH user-index AND root-human-index is not duplicated."""

        def query_side_effect(**kwargs):
            index = kwargs.get("IndexName", "")
            shared_item = {
                "invocation_id": "inv-both",
                "arrived_at": "2026-07-11T10:00:00Z",
                "channel": "github",
                "status": "in_progress",
                "user_id": self._ROOT_USER,
                "root_human_id": self._ROOT_USER,
                "correlation_id": "corr-both",
                "is_human_rooted": True,
            }
            if index == "user-index":
                return {"Items": [shared_item], "Count": 1}
            elif index == "root-human-index":
                return {"Items": [shared_item], "Count": 1}
            elif index == "correlation-index":
                return {"Items": [], "Count": 0}
            return {"Items": [], "Count": 0}

        mock_dynamodb_table.query.side_effect = query_side_effect
        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_chains_by_user(user_id=self._ROOT_USER)

        # Dedup: only 1 chain, not 2
        assert result.count == 1
        assert result.chains[0].root.invocation_id == "inv-both"

    def test_missing_root_human_index_falls_back(self, mock_dynamodb_resource, mock_dynamodb_table):
        """If root-human-index GSI is missing, gracefully falls back to user-index only."""

        def query_side_effect(**kwargs):
            index = kwargs.get("IndexName", "")
            if index == "user-index":
                return {
                    "Items": [
                        {
                            "invocation_id": "inv-direct",
                            "arrived_at": "2026-07-11T10:00:00Z",
                            "status": "in_progress",
                            "user_id": self._ROOT_USER,
                        }
                    ],
                    "Count": 1,
                }
            elif index == "root-human-index":
                # GSI missing — ValidationException caught by _execute_query
                raise ClientError(
                    {"Error": {"Code": "ValidationException", "Message": "Index root-human-index not found"}},
                    "Query",
                )
            return {"Items": [], "Count": 0}

        mock_dynamodb_table.query.side_effect = query_side_effect
        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_chains_by_user(user_id=self._ROOT_USER)

        # Falls back: user-index item still shown
        assert result.count == 1
        assert result.chains[0].root.invocation_id == "inv-direct"


class TestFetchChainDescendantsDepthCap:
    """Issue #3723: depth_cap counts REAL descendants (post-filter).

    Fixtures mirror the real 143-item chain shape from the live evidence:
    a chain with 2 real children + 141 no_op webhook echoes. The old code
    would fill the depth_cap (50) with raw items including echoes, then filter
    them out — real descendants past item 50 were silently truncated.
    """

    _ROOT_USER = "650f093f-ecd9-4ce1-a5a9-368e02c449cf"
    _BOT_USER = "edc91ba7-bot-user-id"
    _CORRELATION = "7993bfd5-chain-correlation"

    def _make_noisy_chain(self, real_child_count: int = 3, no_op_count: int = 140) -> list[dict]:
        """Build a fixture mirroring the real 143-item chain shape."""
        items = [
            # Root
            {
                "invocation_id": "root-event-001",
                "arrived_at": "2026-07-11T09:18:00Z",
                "channel": "github",
                "status": "in_progress",
                "topic": "Orchestration root",
                "user_id": self._ROOT_USER,
                "correlation_id": self._CORRELATION,
                "is_human_rooted": True,
                "root_human_id": self._ROOT_USER,
            },
        ]
        # Real children (in_progress/complete)
        for i in range(real_child_count):
            items.append(
                {
                    "invocation_id": f"real-child-{i:03d}",
                    "arrived_at": f"2026-07-11T09:19:{i:02d}Z",
                    "channel": "github",
                    "status": "in_progress" if i % 2 == 0 else "complete",
                    "topic": f"Real agent task {i}",
                    "persona": "developer",
                    "user_id": self._BOT_USER,
                    "correlation_id": self._CORRELATION,
                    "parent_invocation_id": "root-event-001",
                    "is_human_rooted": True,
                    "root_human_id": self._ROOT_USER,
                }
            )
        # no_op webhook echoes
        for i in range(no_op_count):
            items.append(
                {
                    "invocation_id": f"noop-echo-{i:03d}",
                    "arrived_at": f"2026-07-11T09:18:{(i % 60):02d}Z",
                    "channel": "github",
                    "status": "no_op",
                    "topic": "status-comment edit echo",
                    "user_id": self._BOT_USER,
                    "correlation_id": self._CORRELATION,
                    "parent_invocation_id": "root-event-001",
                    "is_human_rooted": True,
                    "root_human_id": self._ROOT_USER,
                }
            )
        return items

    def test_depth_cap_counts_real_descendants_not_noise(self, mock_dynamodb_resource, mock_dynamodb_table):
        """depth_cap=50 with 3 real + 140 no_op: all 3 real descendants survive.

        Before this fix, depth_cap was applied to raw items (143 total), so the
        first 50 raw items were kept (mix of echoes and real), then no_ops were
        filtered → fewer than 3 real descendants returned. Now the filter runs
        INSIDE the loop and depth_cap counts only post-filter items.
        """
        chain_items = self._make_noisy_chain(real_child_count=3, no_op_count=140)

        def query_side_effect(**kwargs):
            index = kwargs.get("IndexName", "")
            if index == "user-index":
                return {
                    "Items": [
                        {
                            "invocation_id": "root-event-001",
                            "arrived_at": "2026-07-11T09:18:00Z",
                            "status": "in_progress",
                            "user_id": self._ROOT_USER,
                            "correlation_id": self._CORRELATION,
                        }
                    ],
                    "Count": 1,
                }
            elif index == "root-human-index":
                return {"Items": [], "Count": 0}
            elif index == "correlation-index":
                return {"Items": chain_items, "Count": len(chain_items)}
            return {"Items": [], "Count": 0}

        mock_dynamodb_table.query.side_effect = query_side_effect
        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_chains_by_user(user_id=self._ROOT_USER)

        chain = result.chains[0]
        # All 3 real descendants found (not truncated by noise filling the cap)
        assert chain.descendant_count == 3
        assert all(d.invocation_id.startswith("real-child-") for d in chain.descendants)
        # No no_op items leaked through
        assert all(d.status != "no_op" for d in chain.descendants)

    def test_depth_cap_still_enforced_on_real_items(self, mock_dynamodb_resource, mock_dynamodb_table):
        """depth_cap=5 with 10 real descendants: only 5 returned."""
        chain_items = self._make_noisy_chain(real_child_count=10, no_op_count=0)

        def query_side_effect(**kwargs):
            index = kwargs.get("IndexName", "")
            if index == "user-index":
                return {
                    "Items": [
                        {
                            "invocation_id": "root-event-001",
                            "arrived_at": "2026-07-11T09:18:00Z",
                            "status": "in_progress",
                            "user_id": self._ROOT_USER,
                            "correlation_id": self._CORRELATION,
                        }
                    ],
                    "Count": 1,
                }
            elif index == "root-human-index":
                return {"Items": [], "Count": 0}
            elif index == "correlation-index":
                return {"Items": chain_items, "Count": len(chain_items)}
            return {"Items": [], "Count": 0}

        mock_dynamodb_table.query.side_effect = query_side_effect
        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)

        # Use the internal _fetch_chain_descendants directly for precise cap control
        descendants = service._fetch_chain_descendants(
            correlation_id=self._CORRELATION,
            root_invocation_id="root-event-001",
            depth_cap=5,
        )

        # Exactly 5 real descendants (cap enforced)
        assert len(descendants) == 5

    def test_include_non_triggering_includes_noise_under_cap(self, mock_dynamodb_resource, mock_dynamodb_table):
        """include_non_triggering=True returns no_op items, still respecting depth_cap."""
        chain_items = self._make_noisy_chain(real_child_count=2, no_op_count=10)

        def query_side_effect(**kwargs):
            index = kwargs.get("IndexName", "")
            if index == "user-index":
                return {
                    "Items": [
                        {
                            "invocation_id": "root-event-001",
                            "arrived_at": "2026-07-11T09:18:00Z",
                            "status": "in_progress",
                            "user_id": self._ROOT_USER,
                            "correlation_id": self._CORRELATION,
                        }
                    ],
                    "Count": 1,
                }
            elif index == "root-human-index":
                return {"Items": [], "Count": 0}
            elif index == "correlation-index":
                return {"Items": chain_items, "Count": len(chain_items)}
            return {"Items": [], "Count": 0}

        mock_dynamodb_table.query.side_effect = query_side_effect
        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)

        # With include_non_triggering=True, all items (real + no_op) counted
        descendants = service._fetch_chain_descendants(
            correlation_id=self._CORRELATION,
            root_invocation_id="root-event-001",
            include_non_triggering=True,
            depth_cap=50,
        )

        # 2 real + 10 no_op = 12 total descendants (all fit under cap of 50)
        assert len(descendants) == 12
        no_op_count = sum(1 for d in descendants if d.status == "no_op")
        assert no_op_count == 10


# ---------------------------------------------------------------------------
# Issue #3949 tests: get_invocation base-table Query + authorize-after-fetch
# ---------------------------------------------------------------------------


class TestGetInvocationBaseTableQuery:
    """Issue #3949: get_invocation uses base-table Query + authorize-after-fetch.

    Tests assert on query call args and the authorization path — not canned
    mock returns — to prove the scoping logic works correctly.
    """

    def test_direct_run_authorized_via_user_id(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Direct run (user_id = caller) is authorized. Single base-table query."""
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "event_id": "inv-direct",
                    "arrived_at": "2026-07-29T09:00:00Z",
                    "channel": "github",
                    "status": "complete",
                    "user_id": "user-1",
                }
            ],
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        item = service.get_invocation("inv-direct", user_id="user-1")

        assert item is not None
        assert item.invocation_id == "inv-direct"
        # Single query, no IndexName (base table)
        assert mock_dynamodb_table.query.call_count == 1
        call_kwargs = mock_dynamodb_table.query.call_args[1]
        assert "IndexName" not in call_kwargs

    def test_chain_attributed_authorized_via_root_human_id(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Chain-attributed row (user_id=bot, root_human_id=caller) → authorized."""
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "event_id": "inv-chain-bot",
                    "arrived_at": "2026-07-29T10:21:00Z",
                    "status": "complete",
                    "status_updated_at": "2026-07-29T10:25:00Z",
                    "user_id": "aws-e-adp-agent-dev[bot]",
                    "root_human_id": "user-human-1",
                    "transcript_key": "dev/org/repo/issue-42/transcript.md",
                }
            ],
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        item = service.get_invocation("inv-chain-bot", user_id="user-human-1")

        assert item is not None
        assert item.transcript_key == "dev/org/repo/issue-42/transcript.md"

    def test_cross_user_disclosure_blocked_assert_on_auth_path(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Row exists but neither user_id nor root_human_id match caller → None.

        Asserts on the authorization path: DDB returns the row, code rejects.
        """
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "event_id": "inv-victim",
                    "arrived_at": "2026-07-29T10:00:00Z",
                    "status": "complete",
                    "user_id": "aws-e-adp-agent-dev[bot]",
                    "root_human_id": "user-victim",
                }
            ],
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        item = service.get_invocation("inv-victim", user_id="user-attacker")

        # DDB returned the row (query executed), but authorization blocked it
        assert item is None
        assert mock_dynamodb_table.query.call_count == 1

    def test_graceful_degradation_validation_exception(self, mock_dynamodb_resource, mock_dynamodb_table):
        """ValidationException → None, no 500 (environments without table/key schema)."""
        mock_dynamodb_table.query.side_effect = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "Table error"}},
            "Query",
        )

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        item = service.get_invocation("inv-123", user_id="user-1")

        assert item is None

    def test_tenant_scope_unchanged(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Tenant-scoped lookup authorizes by tenant_id; no user_id/root_human_id check."""
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "event_id": "inv-admin",
                    "arrived_at": "2026-07-29T10:00:00Z",
                    "status": "complete",
                    "user_id": "bot-user",
                    "tenant_id": "org-001",
                }
            ],
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        item = service.get_invocation("inv-admin", tenant_id="org-001")

        assert item is not None
        assert mock_dynamodb_table.query.call_count == 1


# ---------------------------------------------------------------------------
# Issue #3949 tests: get_chain membership-based scoping
# ---------------------------------------------------------------------------


class TestGetChainMembershipScoping:
    """Issue #3949: get_chain uses membership-based authorization.

    Authorize the CHAIN (any member has user_id or root_human_id = caller),
    then return ALL members unfiltered. No per-row user_id filter.
    """

    def test_bot_owned_member_survives_membership_auth(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Chain with bot-owned members: all survive because root has root_human_id = caller."""
        chain_items = [
            {
                "event_id": "root-001",
                "arrived_at": "2026-07-29T10:00:00Z",
                "status": "complete",
                "user_id": "user-human",
                "root_human_id": "user-human",
                "correlation_id": "corr-test",
            },
            {
                "event_id": "child-bot-001",
                "arrived_at": "2026-07-29T10:01:00Z",
                "status": "complete",
                "user_id": "bot-user",  # bot-owned
                "root_human_id": "user-human",
                "correlation_id": "corr-test",
                "parent_invocation_id": "root-001",
            },
            {
                "event_id": "child-bot-002",
                "arrived_at": "2026-07-29T10:02:00Z",
                "status": "complete",
                "user_id": "bot-user",
                # NO root_human_id (sparse — pre-#2042 row)
                "correlation_id": "corr-test",
                "parent_invocation_id": "child-bot-001",
            },
        ]
        mock_dynamodb_table.query.return_value = {"Items": chain_items, "Count": 3}

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.get_chain("corr-test", user_id="user-human")

        # All 3 items returned (including sparse-root_human_id row)
        assert result.total_count == 3
        assert len(result.items) == 1  # one root
        assert result.items[0].invocation_id == "root-001"
        # Root has 1 direct child, which has 1 grandchild
        assert len(result.items[0].children) == 1
        assert result.items[0].children[0].invocation_id == "child-bot-001"

    def test_foreign_chain_returns_empty(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Chain where NO member has user_id or root_human_id = caller → empty."""
        chain_items = [
            {
                "event_id": "foreign-root",
                "arrived_at": "2026-07-29T10:00:00Z",
                "status": "complete",
                "user_id": "other-user",
                "root_human_id": "other-user",
                "correlation_id": "corr-foreign",
            },
        ]
        mock_dynamodb_table.query.return_value = {"Items": chain_items, "Count": 1}

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.get_chain("corr-foreign", user_id="user-attacker")

        # Authorization fails — empty response (existence-hiding)
        assert result.total_count == 0
        assert result.items == []

    def test_no_per_row_user_filter_in_query_args(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Assert FilterExpression does NOT contain user_id condition (membership-based)."""
        mock_dynamodb_table.query.return_value = {
            "Items": [
                {
                    "event_id": "r1",
                    "arrived_at": "2026-07-29T10:00:00Z",
                    "status": "complete",
                    "user_id": "user-1",
                    "correlation_id": "corr-x",
                }
            ],
            "Count": 1,
        }

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        service.get_chain("corr-x", user_id="user-1")

        call_kwargs = mock_dynamodb_table.query.call_args[1]
        # FilterExpression should NOT have user_id scoping
        filter_expr = call_kwargs.get("FilterExpression")
        if filter_expr is not None:
            expr_str = str(filter_expr.get_expression())
            assert "user_id" not in expr_str

    def test_sparse_root_human_id_no_orphan_promotion(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Pre-#2042 mid-chain row (no root_human_id) is NOT dropped — no tree restructure."""
        chain_items = [
            {
                "event_id": "root-001",
                "arrived_at": "2026-07-29T10:00:00Z",
                "status": "complete",
                "user_id": "user-human",
                "correlation_id": "corr-sparse",
            },
            {
                "event_id": "mid-001",
                "arrived_at": "2026-07-29T10:01:00Z",
                "status": "complete",
                "user_id": "bot-user",
                # No root_human_id — sparse
                "correlation_id": "corr-sparse",
                "parent_invocation_id": "root-001",
            },
            {
                "event_id": "leaf-001",
                "arrived_at": "2026-07-29T10:02:00Z",
                "status": "complete",
                "user_id": "bot-user",
                "root_human_id": "user-human",
                "correlation_id": "corr-sparse",
                "parent_invocation_id": "mid-001",
            },
        ]
        mock_dynamodb_table.query.return_value = {"Items": chain_items, "Count": 3}

        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.get_chain("corr-sparse", user_id="user-human")

        # All 3 items returned — mid-chain row NOT dropped
        assert result.total_count == 3
        # Tree structure preserved: root → mid → leaf (not: root + orphan mid + orphan leaf)
        assert len(result.items) == 1  # single root
        root = result.items[0]
        assert root.invocation_id == "root-001"
        assert len(root.children) == 1
        mid = root.children[0]
        assert mid.invocation_id == "mid-001"
        assert len(mid.children) == 1
        assert mid.children[0].invocation_id == "leaf-001"


# ---------------------------------------------------------------------------
# Issue #3949 tests: query_chains_by_user chain-root backfill
# ---------------------------------------------------------------------------


class TestQueryChainsByUserRootBackfill:
    """Issue #3949: query_chains_by_user backfills chain root when page 1 has only descendants.

    When all items on the merged page are descendants (have parent_invocation_id),
    the chain view would previously render 0 rows. Now it fetches the chain root
    via correlation-index and emits the chain.
    """

    _ROOT_USER = "650f093f-ecd9-4ce1-a5a9-368e02c449cf"
    _BOT_USER = "edc91ba7-bot-user-id"

    def test_descendants_only_page_emits_chain(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Page 1 with only descendants → chain root backfilled, chain rendered."""

        def query_side_effect(**kwargs):
            index = kwargs.get("IndexName", "")
            if index == "user-index":
                # Only a descendant item on page 1 (has parent_invocation_id)
                return {
                    "Items": [
                        {
                            "invocation_id": "inv-child-only",
                            "arrived_at": "2026-07-29T10:05:00Z",
                            "channel": "github",
                            "status": "complete",
                            "user_id": self._BOT_USER,
                            "root_human_id": self._ROOT_USER,
                            "correlation_id": "corr-orphan",
                            "parent_invocation_id": "inv-true-root",
                            "is_human_rooted": True,
                        }
                    ],
                    "Count": 1,
                }
            elif index == "root-human-index":
                return {"Items": [], "Count": 0}
            elif index == "correlation-index":
                return {
                    "Items": [
                        {
                            "invocation_id": "inv-true-root",
                            "arrived_at": "2026-07-29T10:00:00Z",
                            "channel": "github",
                            "status": "complete",
                            "topic": "The real root",
                            "user_id": self._ROOT_USER,
                            "root_human_id": self._ROOT_USER,
                            "correlation_id": "corr-orphan",
                            "is_human_rooted": True,
                        },
                        {
                            "invocation_id": "inv-child-only",
                            "arrived_at": "2026-07-29T10:05:00Z",
                            "channel": "github",
                            "status": "complete",
                            "user_id": self._BOT_USER,
                            "root_human_id": self._ROOT_USER,
                            "correlation_id": "corr-orphan",
                            "parent_invocation_id": "inv-true-root",
                            "is_human_rooted": True,
                        },
                    ],
                    "Count": 2,
                }
            return {"Items": [], "Count": 0}

        mock_dynamodb_table.query.side_effect = query_side_effect
        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_chains_by_user(user_id=self._ROOT_USER)

        # Chain is emitted (not empty view)
        assert result.count == 1
        chain = result.chains[0]
        assert chain.chain_id == "corr-orphan"
        assert chain.root.invocation_id == "inv-true-root"
        assert chain.root.topic == "The real root"
        assert chain.descendant_count == 1
        assert chain.descendants[0].invocation_id == "inv-child-only"

    def test_two_descendants_same_chain_one_backfill(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Two descendants of one chain trigger exactly one backfill query (dedupe)."""
        backfill_count = [0]

        def query_side_effect(**kwargs):
            index = kwargs.get("IndexName", "")
            if index == "user-index":
                return {
                    "Items": [
                        {
                            "invocation_id": "inv-child-1",
                            "arrived_at": "2026-07-29T10:05:00Z",
                            "status": "complete",
                            "user_id": self._BOT_USER,
                            "root_human_id": self._ROOT_USER,
                            "correlation_id": "corr-shared",
                            "parent_invocation_id": "inv-root",
                            "is_human_rooted": True,
                        },
                        {
                            "invocation_id": "inv-child-2",
                            "arrived_at": "2026-07-29T10:06:00Z",
                            "status": "complete",
                            "user_id": self._BOT_USER,
                            "root_human_id": self._ROOT_USER,
                            "correlation_id": "corr-shared",
                            "parent_invocation_id": "inv-root",
                            "is_human_rooted": True,
                        },
                    ],
                    "Count": 2,
                }
            elif index == "root-human-index":
                return {"Items": [], "Count": 0}
            elif index == "correlation-index":
                backfill_count[0] += 1
                return {
                    "Items": [
                        {
                            "invocation_id": "inv-root",
                            "arrived_at": "2026-07-29T10:00:00Z",
                            "status": "complete",
                            "user_id": self._ROOT_USER,
                            "correlation_id": "corr-shared",
                        },
                        {
                            "invocation_id": "inv-child-1",
                            "arrived_at": "2026-07-29T10:05:00Z",
                            "status": "complete",
                            "user_id": self._BOT_USER,
                            "correlation_id": "corr-shared",
                            "parent_invocation_id": "inv-root",
                        },
                        {
                            "invocation_id": "inv-child-2",
                            "arrived_at": "2026-07-29T10:06:00Z",
                            "status": "complete",
                            "user_id": self._BOT_USER,
                            "correlation_id": "corr-shared",
                            "parent_invocation_id": "inv-root",
                        },
                    ],
                    "Count": 3,
                }
            return {"Items": [], "Count": 0}

        mock_dynamodb_table.query.side_effect = query_side_effect
        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_chains_by_user(user_id=self._ROOT_USER)

        # One chain emitted
        assert result.count == 1
        # Backfill called only TWICE: once for _backfill_chain_root, once for
        # _fetch_chain_descendants — NOT once per descendant
        # (correlation-index queries = backfill + descendants fetch = 2)
        assert backfill_count[0] == 2

    def test_root_plus_child_on_page_zero_backfills(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Root + child on the same page → zero backfill queries (root emitted directly).

        Issue #3949: the page is deliberately ordered NEWEST-FIRST (child before
        its root), which is what production actually produces — `_execute_query`
        queries with `ScanIndexForward=False` and `query_chains_by_user` sorts the
        merged page by `invoked_at` descending. A single-pass emission loop visits
        the child first and fires a backfill for a chain whose root is right there
        on the page. The two-pass loop must emit the on-page root regardless of
        page order, so this fixture order is the actual regression guard.
        """
        correlation_queries = [0]

        def query_side_effect(**kwargs):
            index = kwargs.get("IndexName", "")
            if index == "user-index":
                # Both root AND child on page, newest-first (child precedes root)
                return {
                    "Items": [
                        {
                            "invocation_id": "inv-child",
                            "arrived_at": "2026-07-29T10:05:00Z",
                            "status": "complete",
                            "user_id": self._BOT_USER,
                            "root_human_id": self._ROOT_USER,
                            "correlation_id": "corr-direct",
                            "parent_invocation_id": "inv-root",
                            "is_human_rooted": True,
                        },
                        {
                            "invocation_id": "inv-root",
                            "arrived_at": "2026-07-29T10:00:00Z",
                            "status": "complete",
                            "user_id": self._ROOT_USER,
                            "correlation_id": "corr-direct",
                            "is_human_rooted": True,
                            "root_human_id": self._ROOT_USER,
                        },
                    ],
                    "Count": 2,
                }
            elif index == "root-human-index":
                return {"Items": [], "Count": 0}
            elif index == "correlation-index":
                correlation_queries[0] += 1
                return {
                    "Items": [
                        {
                            "invocation_id": "inv-root",
                            "arrived_at": "2026-07-29T10:00:00Z",
                            "status": "complete",
                            "user_id": self._ROOT_USER,
                            "correlation_id": "corr-direct",
                        },
                        {
                            "invocation_id": "inv-child",
                            "arrived_at": "2026-07-29T10:05:00Z",
                            "status": "complete",
                            "user_id": self._BOT_USER,
                            "correlation_id": "corr-direct",
                            "parent_invocation_id": "inv-root",
                        },
                    ],
                    "Count": 2,
                }
            return {"Items": [], "Count": 0}

        mock_dynamodb_table.query.side_effect = query_side_effect
        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_chains_by_user(user_id=self._ROOT_USER)

        assert result.count == 1
        # Only _fetch_chain_descendants queries correlation-index (1 call).
        # Pass 1 emits the on-page root and records corr-direct, so pass 2 finds
        # nothing to backfill — zero backfill queries, even though the child was
        # first in page order.
        assert correlation_queries[0] == 1
        # The chain is anchored on the ON-PAGE root, not a backfilled copy.
        assert result.chains[0].root.invocation_id == "inv-root"

    def test_root_on_page_survives_backfill_query_error(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Issue #3949: a chain whose root is ON the page must not depend on backfill.

        Regression guard for the two-pass ordering. With a newest-first page (child
        before root) and a correlation-index that errors, a single-pass loop fires a
        backfill for the child, gets None from the degraded query, and drops the
        chain — losing a chain whose root was already on the page. Under the
        two-pass loop the root is emitted in pass 1, so the chain survives with an
        empty descendant list.
        """

        def query_side_effect(**kwargs):
            index = kwargs.get("IndexName", "")
            if index == "user-index":
                return {
                    "Items": [
                        {
                            "invocation_id": "inv-child",
                            "arrived_at": "2026-07-29T10:05:00Z",
                            "status": "complete",
                            "user_id": self._BOT_USER,
                            "root_human_id": self._ROOT_USER,
                            "correlation_id": "corr-degraded",
                            "parent_invocation_id": "inv-root",
                            "is_human_rooted": True,
                        },
                        {
                            "invocation_id": "inv-root",
                            "arrived_at": "2026-07-29T10:00:00Z",
                            "status": "complete",
                            "topic": "On-page root",
                            "user_id": self._ROOT_USER,
                            "root_human_id": self._ROOT_USER,
                            "correlation_id": "corr-degraded",
                            "is_human_rooted": True,
                        },
                    ],
                    "Count": 2,
                }
            elif index == "root-human-index":
                return {"Items": [], "Count": 0}
            elif index == "correlation-index":
                raise ClientError(
                    {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
                    "Query",
                )
            return {"Items": [], "Count": 0}

        mock_dynamodb_table.query.side_effect = query_side_effect
        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_chains_by_user(user_id=self._ROOT_USER)

        # The chain survives, anchored on the on-page root
        assert result.count == 1
        assert result.chains[0].chain_id == "corr-degraded"
        assert result.chains[0].root.invocation_id == "inv-root"
        assert result.chains[0].root.topic == "On-page root"
        # Descendants degraded to empty (correlation-index unavailable), not a 500
        assert result.chains[0].descendant_count == 0

    def test_backfilled_root_carries_transcript_key(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Backfilled root preserves transcript_key (uses _map_item, not hand-construct)."""

        def query_side_effect(**kwargs):
            index = kwargs.get("IndexName", "")
            if index == "user-index":
                return {
                    "Items": [
                        {
                            "invocation_id": "inv-descendant",
                            "arrived_at": "2026-07-29T10:05:00Z",
                            "status": "complete",
                            "user_id": self._BOT_USER,
                            "root_human_id": self._ROOT_USER,
                            "correlation_id": "corr-transcript",
                            "parent_invocation_id": "inv-root-with-key",
                            "is_human_rooted": True,
                        }
                    ],
                    "Count": 1,
                }
            elif index == "root-human-index":
                return {"Items": [], "Count": 0}
            elif index == "correlation-index":
                return {
                    "Items": [
                        {
                            "invocation_id": "inv-root-with-key",
                            "arrived_at": "2026-07-29T10:00:00Z",
                            "status": "complete",
                            "user_id": self._ROOT_USER,
                            "correlation_id": "corr-transcript",
                            "transcript_key": "developer/org/repo/42/transcript.md",
                            "status_updated_at": "2026-07-29T10:03:00Z",
                        },
                        {
                            "invocation_id": "inv-descendant",
                            "arrived_at": "2026-07-29T10:05:00Z",
                            "status": "complete",
                            "user_id": self._BOT_USER,
                            "correlation_id": "corr-transcript",
                            "parent_invocation_id": "inv-root-with-key",
                        },
                    ],
                    "Count": 2,
                }
            return {"Items": [], "Count": 0}

        mock_dynamodb_table.query.side_effect = query_side_effect
        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_chains_by_user(user_id=self._ROOT_USER)

        assert result.count == 1
        root = result.chains[0].root
        assert root.transcript_key == "developer/org/repo/42/transcript.md"
        assert root.completed_at == "2026-07-29T10:03:00Z"

    def test_ttl_expired_root_falls_back_to_earliest_member(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Root TTL-expired (all items have parent_invocation_id) → earliest member used."""

        def query_side_effect(**kwargs):
            index = kwargs.get("IndexName", "")
            if index == "user-index":
                return {
                    "Items": [
                        {
                            "invocation_id": "inv-orphan-child",
                            "arrived_at": "2026-07-29T10:05:00Z",
                            "status": "complete",
                            "user_id": self._BOT_USER,
                            "root_human_id": self._ROOT_USER,
                            "correlation_id": "corr-expired",
                            "parent_invocation_id": "inv-expired-root",
                            "is_human_rooted": True,
                        }
                    ],
                    "Count": 1,
                }
            elif index == "root-human-index":
                return {"Items": [], "Count": 0}
            elif index == "correlation-index":
                # Root is gone (TTL-expired); only descendants remain
                return {
                    "Items": [
                        {
                            "invocation_id": "inv-orphan-child",
                            "arrived_at": "2026-07-29T10:05:00Z",
                            "status": "complete",
                            "user_id": self._BOT_USER,
                            "root_human_id": self._ROOT_USER,
                            "correlation_id": "corr-expired",
                            "parent_invocation_id": "inv-expired-root",
                        },
                    ],
                    "Count": 1,
                }
            return {"Items": [], "Count": 0}

        mock_dynamodb_table.query.side_effect = query_side_effect
        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_chains_by_user(user_id=self._ROOT_USER)

        # Chain emitted even though root is TTL-expired — earliest member used
        assert result.count == 1
        chain = result.chains[0]
        assert chain.root.invocation_id == "inv-orphan-child"

    def test_backfill_dedup_with_direct_root(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Same correlation_id from both a direct root and a descendant → only one chain row.

        Page order is newest-first (descendant before its root), matching production.
        """

        def query_side_effect(**kwargs):
            index = kwargs.get("IndexName", "")
            if index == "user-index":
                return {
                    "Items": [
                        {
                            "invocation_id": "inv-child",
                            "arrived_at": "2026-07-29T10:05:00Z",
                            "channel": "github",
                            "status": "complete",
                            "user_id": self._BOT_USER,
                            "root_human_id": self._ROOT_USER,
                            "correlation_id": "corr-dedup",
                            "parent_invocation_id": "inv-root",
                            "is_human_rooted": True,
                        },
                        {
                            "invocation_id": "inv-root",
                            "arrived_at": "2026-07-29T10:00:00Z",
                            "channel": "github",
                            "status": "complete",
                            "user_id": self._ROOT_USER,
                            "correlation_id": "corr-dedup",
                            "is_human_rooted": True,
                            "root_human_id": self._ROOT_USER,
                        },
                    ],
                    "Count": 2,
                }
            elif index == "root-human-index":
                return {"Items": [], "Count": 0}
            elif index == "correlation-index":
                return {
                    "Items": [
                        {
                            "invocation_id": "inv-root",
                            "arrived_at": "2026-07-29T10:00:00Z",
                            "status": "complete",
                            "user_id": self._ROOT_USER,
                            "correlation_id": "corr-dedup",
                        },
                        {
                            "invocation_id": "inv-child",
                            "arrived_at": "2026-07-29T10:05:00Z",
                            "status": "complete",
                            "user_id": self._BOT_USER,
                            "correlation_id": "corr-dedup",
                            "parent_invocation_id": "inv-root",
                        },
                    ],
                    "Count": 2,
                }
            return {"Items": [], "Count": 0}

        mock_dynamodb_table.query.side_effect = query_side_effect
        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_chains_by_user(user_id=self._ROOT_USER)

        # Only one chain row (not duplicated)
        assert result.count == 1
        assert result.chains[0].chain_id == "corr-dedup"
        assert result.chains[0].root.invocation_id == "inv-root"

    def test_backfill_correlation_index_error_degrades(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Error during root backfill → descendant skipped silently, no crash."""

        def query_side_effect(**kwargs):
            index = kwargs.get("IndexName", "")
            if index == "user-index":
                return {
                    "Items": [
                        {
                            "invocation_id": "inv-orphan",
                            "arrived_at": "2026-07-29T10:05:00Z",
                            "channel": "github",
                            "status": "complete",
                            "user_id": self._BOT_USER,
                            "root_human_id": self._ROOT_USER,
                            "correlation_id": "corr-broken",
                            "parent_invocation_id": "inv-missing-root",
                            "is_human_rooted": True,
                        }
                    ],
                    "Count": 1,
                }
            elif index == "root-human-index":
                return {"Items": [], "Count": 0}
            elif index == "correlation-index":
                raise ClientError(
                    {"Error": {"Code": "ValidationException", "Message": "Index not found"}},
                    "Query",
                )
            return {"Items": [], "Count": 0}

        mock_dynamodb_table.query.side_effect = query_side_effect
        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        result = service.query_chains_by_user(user_id=self._ROOT_USER)

        # No crash; chain not emitted (root backfill failed)
        assert result.count == 0

    def test_status_filter_backfilled_root_bypasses(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Backfilled chain root bypasses the status filter (intended semantic).

        With status=complete, a chain whose root is 'failed' but contains a
        matching 'complete' descendant IS emitted — the root is the chain anchor.
        """

        def query_side_effect(**kwargs):
            index = kwargs.get("IndexName", "")
            if index == "user-index":
                return {
                    "Items": [
                        {
                            "invocation_id": "inv-complete-child",
                            "arrived_at": "2026-07-29T10:05:00Z",
                            "status": "complete",
                            "user_id": self._BOT_USER,
                            "root_human_id": self._ROOT_USER,
                            "correlation_id": "corr-mixed-status",
                            "parent_invocation_id": "inv-failed-root",
                            "is_human_rooted": True,
                        }
                    ],
                    "Count": 1,
                }
            elif index == "root-human-index":
                return {"Items": [], "Count": 0}
            elif index == "correlation-index":
                return {
                    "Items": [
                        {
                            "invocation_id": "inv-failed-root",
                            "arrived_at": "2026-07-29T10:00:00Z",
                            "status": "failed",  # Root is failed, not complete
                            "user_id": self._ROOT_USER,
                            "correlation_id": "corr-mixed-status",
                        },
                        {
                            "invocation_id": "inv-complete-child",
                            "arrived_at": "2026-07-29T10:05:00Z",
                            "status": "complete",
                            "user_id": self._BOT_USER,
                            "correlation_id": "corr-mixed-status",
                            "parent_invocation_id": "inv-failed-root",
                        },
                    ],
                    "Count": 2,
                }
            return {"Items": [], "Count": 0}

        mock_dynamodb_table.query.side_effect = query_side_effect
        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        # Note: status filter is applied to the flat query (user-index), not backfill
        result = service.query_chains_by_user(user_id=self._ROOT_USER, status="complete")

        # Chain IS emitted — the backfilled root (status=failed) bypasses the filter
        assert result.count == 1
        assert result.chains[0].root.invocation_id == "inv-failed-root"
