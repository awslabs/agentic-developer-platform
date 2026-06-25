"""Activity service — DynamoDB query logic for agent invocations.

Reads from the `webhook-events` table (owned by agent-factory) via its
`user-index` (PK=user_id, SK=arrived_at) and `tenant-index`
(PK=tenant_id, SK=arrived_at) GSIs.

Key design decisions:
- Table name resolved from env var WEBHOOK_EVENTS_TABLE (set via SSM in prod).
- Missing GSI/table → returns empty result with a warning log, never 500.
- Cursor is base64(json(LastEvaluatedKey)), opaque to client.

Phase 6 additions (issue #1461):
- Lineage enrichment: map parent_invocation_id, trigger_kind, root_human_id,
  is_human_rooted from DynamoDB item fields (written by webhook-ingress).
- Chain query: retrieve all invocations sharing a correlation_id, build a tree
  by parent_invocation_id, with depth cap and user/tenant scoping.
"""

import base64
import json
import logging
import os

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from src.activity.schemas import (
    ChainListResponse,
    ChainSummary,
    InvocationChainItem,
    InvocationChainResponse,
    InvocationItem,
    InvocationListResponse,
    TriggerKind,
)

logger = logging.getLogger("bedrockgateway.activity")

# Default table name; overridden via env or constructor arg for testability.
_DEFAULT_TABLE_NAME = "adp-dev-webhook-events"

# Maximum number of items to retrieve for a chain view (prevents unbounded reads).
_CHAIN_DEPTH_CAP = 50


def _get_table_name() -> str:
    return os.environ.get("WEBHOOK_EVENTS_TABLE", _DEFAULT_TABLE_NAME)


def _encode_cursor(last_evaluated_key: dict) -> str:
    """Encode DynamoDB LastEvaluatedKey as an opaque base64 cursor."""
    return base64.urlsafe_b64encode(json.dumps(last_evaluated_key).encode()).decode()


def _decode_cursor(cursor: str) -> dict:
    """Decode an opaque base64 cursor back to DynamoDB ExclusiveStartKey.

    Raises ValueError on malformed input.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode())
        key = json.loads(raw)
        if not isinstance(key, dict):
            raise ValueError("Cursor must decode to a JSON object")
        return key
    except (json.JSONDecodeError, UnicodeDecodeError, Exception) as exc:
        raise ValueError(f"Invalid cursor: {exc}") from exc


class ActivityService:
    """Service for querying agent invocation records from DynamoDB."""

    def __init__(self, table_name: str | None = None, dynamodb_resource=None):
        """Initialize the activity service.

        Args:
            table_name: Override DynamoDB table name (for testing).
            dynamodb_resource: Override boto3 DynamoDB resource (for testing).
        """
        self._table_name = table_name or _get_table_name()
        self._dynamodb = dynamodb_resource or boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        self._table = self._dynamodb.Table(self._table_name)

    def query_by_user(
        self,
        user_id: str,
        *,
        page_size: int = 20,
        last_key: str | None = None,
        status: str | None = None,
        channel: str | None = None,
        persona: str | None = None,
        since: str | None = None,
        until: str | None = None,
        include_non_triggering: bool = False,
    ) -> InvocationListResponse:
        """Query invocations for a specific user via user-index GSI.

        Args:
            user_id: The user's ID (from token, never from request param).
            page_size: Max items to read from DDB before filtering.
            last_key: Opaque pagination cursor.
            status: Optional filter on invocation status.
            channel: Optional filter on channel.
            persona: Optional filter on persona.
            since: ISO 8601 lower-bound on arrived_at (inclusive).
            until: ISO 8601 upper-bound on arrived_at (inclusive).
            include_non_triggering: If False (default), exclude no_op and
                webhook_received rows. Ignored when an explicit status filter
                is provided.

        Returns:
            InvocationListResponse with items, count, and optional next cursor.
        """
        return self._execute_query(
            index_name="user-index",
            partition_key_name="user_id",
            partition_key_value=user_id,
            page_size=page_size,
            last_key=last_key,
            status=status,
            channel=channel,
            persona=persona,
            since=since,
            until=until,
            include_non_triggering=include_non_triggering,
        )

    def query_by_tenant(
        self,
        tenant_id: str,
        *,
        page_size: int = 20,
        last_key: str | None = None,
        status: str | None = None,
        channel: str | None = None,
        persona: str | None = None,
        since: str | None = None,
        until: str | None = None,
        user_id: str | None = None,
        include_non_triggering: bool = False,
    ) -> InvocationListResponse:
        """Query invocations for a tenant via tenant-index GSI.

        Args:
            tenant_id: The tenant/org ID (from token for org admins).
            page_size: Max items to read from DDB before filtering.
            last_key: Opaque pagination cursor.
            status: Optional filter on invocation status.
            channel: Optional filter on channel.
            persona: Optional filter on persona.
            since: ISO 8601 lower-bound on arrived_at (inclusive).
            until: ISO 8601 upper-bound on arrived_at (inclusive).
            user_id: Optional admin filter to a single user within tenant.
            include_non_triggering: If False (default), exclude no_op and
                webhook_received rows. Ignored when an explicit status filter
                is provided.

        Returns:
            InvocationListResponse with items, count, and optional next cursor.
        """
        return self._execute_query(
            index_name="tenant-index",
            partition_key_name="tenant_id",
            partition_key_value=tenant_id,
            page_size=page_size,
            last_key=last_key,
            status=status,
            channel=channel,
            persona=persona,
            since=since,
            until=until,
            extra_filter_user_id=user_id,
            include_non_triggering=include_non_triggering,
        )

    def _execute_query(
        self,
        *,
        index_name: str,
        partition_key_name: str,
        partition_key_value: str,
        page_size: int,
        last_key: str | None,
        status: str | None,
        channel: str | None,
        persona: str | None,
        since: str | None,
        until: str | None,
        extra_filter_user_id: str | None = None,
        include_non_triggering: bool = False,
    ) -> InvocationListResponse:
        """Execute a DynamoDB Query with shared logic for both endpoints.

        Handles:
        - KeyConditionExpression on PK + optional SK range (since/until)
        - FilterExpression for status, channel, persona, user_id
        - Pagination via ExclusiveStartKey / LastEvaluatedKey
        - Missing GSI/table → empty result (deploy-order-independent)
        """
        # Build KeyConditionExpression
        key_condition = Key(partition_key_name).eq(partition_key_value)

        # Add date range to sort key (arrived_at) if provided
        if since and until:
            key_condition = key_condition & Key("arrived_at").between(since, until)
        elif since:
            key_condition = key_condition & Key("arrived_at").gte(since)
        elif until:
            key_condition = key_condition & Key("arrived_at").lte(until)

        # Build FilterExpression
        filter_expression = None
        if status:
            filter_expression = Attr("status").eq(status)
        elif not include_non_triggering:
            # Default: exclude non-triggering statuses (no_op, webhook_received)
            # so the board shows only actual agent runs. An explicit status filter
            # takes precedence (the user chose to see that specific status).
            _non_triggering = ["no_op", "webhook_received"]
            cond = ~Attr("status").is_in(_non_triggering)
            filter_expression = cond
        if channel:
            cond = Attr("channel").eq(channel)
            filter_expression = (filter_expression & cond) if filter_expression else cond
        if persona:
            cond = Attr("persona").eq(persona)
            filter_expression = (filter_expression & cond) if filter_expression else cond
        if extra_filter_user_id:
            cond = Attr("user_id").eq(extra_filter_user_id)
            filter_expression = (filter_expression & cond) if filter_expression else cond

        # Build query kwargs
        query_kwargs: dict = {
            "IndexName": index_name,
            "KeyConditionExpression": key_condition,
            "ScanIndexForward": False,  # newest first
            "Limit": page_size,
        }
        if filter_expression:
            query_kwargs["FilterExpression"] = filter_expression
        if last_key:
            query_kwargs["ExclusiveStartKey"] = _decode_cursor(last_key)

        # Issue #1757: DynamoDB applies `Limit` to rows read BEFORE the
        # FilterExpression. The webhook-events table is dominated by no_op rows
        # (bot status-comment events), so a single page of `page_size` rows
        # typically yields only 1-2 triggering runs after filtering — even though
        # many more exist on later pages (LastEvaluatedKey set). The board then
        # showed "far fewer runs than expected". Fix: accumulate across pages
        # until we have page_size POST-FILTER items or the index is exhausted,
        # bounded by a max-page cap so a pathological all-no_op chain can't loop
        # unboundedly.
        items: list[InvocationItem] = []
        next_cursor: str | None = None
        max_pages = 20  # backstop: read at most max_pages * page_size raw rows
        pages = 0
        try:
            while True:
                pages += 1
                response = self._table.query(**query_kwargs)
                items.extend(self._map_item(it) for it in response.get("Items", []))
                lek = response.get("LastEvaluatedKey")
                # Stop when we have enough POST-FILTER items, the index is
                # exhausted, or we hit the page-read backstop. The next cursor is
                # DDB's own LastEvaluatedKey (no synthetic cursor — keeps resume
                # correct against the GSI key schema). We may return slightly more
                # than page_size filtered items from the final raw page; that's
                # harmless and avoids fragile cursor reconstruction.
                if len(items) >= page_size or lek is None or pages >= max_pages:
                    if lek is not None:
                        next_cursor = _encode_cursor(lek)
                    break
                query_kwargs["ExclusiveStartKey"] = lek
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("ValidationException", "ResourceNotFoundException"):
                # Missing GSI or table — deploy-order gap; return empty.
                logger.warning(
                    "DynamoDB query failed (likely missing GSI/table) — returning empty",
                    extra={
                        "index_name": index_name,
                        "error_code": error_code,
                        "error_message": str(exc),
                    },
                )
                return InvocationListResponse(items=[], count=0, last_key=None)
            raise

        return InvocationListResponse(items=items, count=len(items), last_key=next_cursor)

    @staticmethod
    def _map_item(item: dict) -> InvocationItem:
        """Map a raw DynamoDB item to the InvocationItem schema."""
        # Issue #1653: Derive completed_at from status_updated_at for terminal statuses
        status = item.get("status")
        terminal_statuses = {"complete", "failed", "rejected", "rate_limited", "no_op"}
        completed_at = item.get("status_updated_at") if status in terminal_statuses else None

        return InvocationItem(
            # Issue #1756: the DDB webhook-events row keys the invocation by
            # `event_id` (the message_id). There is NO `invocation_id`/`pk`
            # attribute, so the old `.get("invocation_id", .get("pk"))` always
            # fell through to "" — leaving invocation_id BLANK on every item,
            # which broke the cost-join (cost_map.get("")) so per-run cost never
            # rendered. Fall back to event_id.
            invocation_id=item.get("invocation_id") or item.get("pk") or item.get("event_id", ""),
            invoked_at=item.get("arrived_at", ""),
            channel=item.get("channel"),
            status=status,
            status_updated_at=item.get("status_updated_at"),
            topic=item.get("topic"),
            persona=item.get("persona"),
            summary=item.get("summary"),
            source_url=item.get("source_url"),
            repo=item.get("repo"),
            issue_number=_safe_int(item.get("issue_number")),
            correlation_id=item.get("correlation_id"),
            run_id=item.get("run_id"),
            # Issue #1653: error_message, completed_at, run_log_url
            error_message=item.get("error_message"),
            completed_at=completed_at,
            run_log_url=item.get("check_run_url"),
            # Phase 6 lineage fields (#1461)
            trigger_kind=_derive_trigger_kind(item),
            triggered_by_invocation_id=item.get("parent_invocation_id"),
            triggered_by_topic=item.get("parent_topic"),
            root_human_id=item.get("root_human_id"),
            is_human_rooted=item.get("is_human_rooted", True),
        )

    def get_chain(
        self,
        correlation_id: str,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
        depth_cap: int = _CHAIN_DEPTH_CAP,
    ) -> InvocationChainResponse:
        """Retrieve all invocations sharing a correlation_id and build a tree.

        Scoping: filters by user_id (for /me/) or tenant_id (for /admin/).
        If both are None, returns empty (safety: never return unscoped data).

        Depth cap: returns at most `depth_cap` items. If more exist, sets
        `depth_capped=True` in the response.

        Tree construction: builds by parent_invocation_id. Items without a
        parent are roots. Falls back to flat date-ordered list if no parent
        edges exist (pre-feature rows).
        """
        if not user_id and not tenant_id:
            return InvocationChainResponse(
                correlation_id=correlation_id,
                items=[],
                total_count=0,
                depth_capped=False,
            )

        # Query all items sharing this correlation_id via the correlation-index
        # GSI (PK=correlation_id, SK=arrived_at). A Query is bounded and cheap;
        # the previous full-table Scan was costly and required a dynamodb:Scan
        # grant the gateway role does not (and should not) have — which surfaced
        # as a 500 "failed to load chain" in the UI. Scoping (user_id/tenant_id)
        # is applied as a post-query FilterExpression; a single chain is small,
        # so post-filtering a Query page is fine.
        scope_filter = None
        if user_id:
            scope_filter = Attr("user_id").eq(user_id)
        elif tenant_id:
            scope_filter = Attr("tenant_id").eq(tenant_id)

        all_items: list[dict] = []
        depth_capped = False

        try:
            query_kwargs: dict = {
                "IndexName": "correlation-index",
                "KeyConditionExpression": Key("correlation_id").eq(correlation_id),
                "ScanIndexForward": True,  # ascending arrived_at = chain order
            }
            if scope_filter is not None:
                query_kwargs["FilterExpression"] = scope_filter

            while True:
                response = self._table.query(**query_kwargs)
                all_items.extend(response.get("Items", []))

                if len(all_items) >= depth_cap:
                    all_items = all_items[:depth_cap]
                    depth_capped = True
                    break

                if "LastEvaluatedKey" not in response:
                    break
                query_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            # Degrade gracefully on any access/availability error (GSI missing
            # before the migration applies, IAM gap, etc.) — never surface a 500
            # to the chain view; show an empty chain and log for operators.
            if error_code in (
                "ValidationException",
                "ResourceNotFoundException",
                "AccessDeniedException",
            ):
                logger.warning(
                    "DynamoDB query failed (chain) — returning empty",
                    extra={"correlation_id": correlation_id, "error_code": error_code},
                )
                return InvocationChainResponse(
                    correlation_id=correlation_id,
                    items=[],
                    total_count=0,
                    depth_capped=False,
                )
            raise

        # Sort by arrived_at ascending (chain order)
        all_items.sort(key=lambda x: x.get("arrived_at", ""))

        # Determine root_human_id and is_human_rooted from first item with those fields
        root_human_id = None
        is_human_rooted = True
        for item in all_items:
            if item.get("root_human_id"):
                root_human_id = item["root_human_id"]
                is_human_rooted = item.get("is_human_rooted", True)
                break

        # Build tree
        chain_items = _build_chain_tree(all_items)

        return InvocationChainResponse(
            correlation_id=correlation_id,
            root_human_id=root_human_id,
            is_human_rooted=is_human_rooted,
            items=chain_items,
            total_count=len(all_items),
            depth_capped=depth_capped,
        )

    def query_chains_by_user(
        self,
        user_id: str,
        *,
        page_size: int = 20,
        last_key: str | None = None,
        status: str | None = None,
        channel: str | None = None,
        persona: str | None = None,
        since: str | None = None,
        until: str | None = None,
        include_non_triggering: bool = False,
    ) -> ChainListResponse:
        """Query chains for a specific user via user-index GSI.

        Issue #1662: Chain-grouped board view. For /me, every user-index hit IS
        a chain root (humans always start a new correlation_id), so pagination
        is straightforward — same as flat view, but each root is enriched with
        its chain descendants.

        Steps:
        1. Query user-index (same as query_by_user) to get the page of roots.
        2. For each root with a correlation_id, fetch chain members via
           correlation-index GSI (exclude non-triggering statuses from
           descendants by default).
        3. Assemble ChainSummary objects (root + descendants).

        Cost enrichment is handled at the route layer (batched Postgres query).
        """
        # Step 1: Get the page of roots (same query as flat view)
        flat_result = self._execute_query(
            index_name="user-index",
            partition_key_name="user_id",
            partition_key_value=user_id,
            page_size=page_size,
            last_key=last_key,
            status=status,
            channel=channel,
            persona=persona,
            since=since,
            until=until,
            include_non_triggering=include_non_triggering,
        )

        # Step 2: For each TRUE root, fetch descendants from correlation-index.
        #
        # Issue #2058: the old code assumed "every user-index hit IS a chain root"
        # (humans always start a new correlation_id). That assumption broke with
        # #2042 — agent-spawned runs are now also attributed to the human's
        # user_id (so they appear in /me), but they are NOT roots: they have a
        # parent_invocation_id and share their correlation_id with the human run.
        # The old loop therefore emitted one top-level chain row PER RUN, so a
        # single chain (one correlation_id) showed up as many duplicate rows, and
        # agent-triggered child runs appeared as top-level "roots". Fix: emit one
        # row per correlation_id, rooted at the actual root run (no parent), with
        # everything else nested as descendants.
        chains: list[ChainSummary] = []
        _seen_correlations: set[str] = set()
        for root_item in flat_result.items:
            correlation_id = root_item.correlation_id

            # Skip descendants masquerading as roots: a run WITH a parent is not a
            # chain root — it belongs nested under its parent's chain row.
            if correlation_id and root_item.triggered_by_invocation_id:
                continue
            # One top-level row per chain: if we've already emitted this chain
            # (from its true root), don't add it again.
            if correlation_id and correlation_id in _seen_correlations:
                continue
            if correlation_id:
                _seen_correlations.add(correlation_id)

            if not correlation_id:
                # No correlation_id → singleton chain (no descendants possible)
                chains.append(
                    ChainSummary(
                        chain_id=root_item.invocation_id,
                        root=root_item,
                        descendant_count=0,
                        descendants=[],
                    )
                )
                continue

            # Fetch chain members via correlation-index
            descendants = self._fetch_chain_descendants(
                correlation_id=correlation_id,
                root_invocation_id=root_item.invocation_id,
                include_non_triggering=include_non_triggering,
            )

            chains.append(
                ChainSummary(
                    chain_id=correlation_id,
                    root=root_item,
                    descendant_count=len(descendants),
                    descendants=descendants,
                )
            )

        return ChainListResponse(
            chains=chains,
            count=len(chains),
            last_key=flat_result.last_key,
        )

    def _fetch_chain_descendants(
        self,
        correlation_id: str,
        root_invocation_id: str,
        *,
        include_non_triggering: bool = False,
        depth_cap: int = _CHAIN_DEPTH_CAP,
    ) -> list[InvocationChainItem]:
        """Fetch chain descendants (non-root members) for a correlation_id.

        Issue #1662: Uses correlation-index GSI. Excludes the root itself
        (already shown as the chain row). Filters out no_op/webhook_received
        descendants by default (consistent with #1658 behavior).
        """
        # Non-triggering statuses to exclude from descendants
        _non_triggering_statuses = {"no_op", "webhook_received"}

        all_items: list[dict] = []
        try:
            query_kwargs: dict = {
                "IndexName": "correlation-index",
                "KeyConditionExpression": Key("correlation_id").eq(correlation_id),
                "ScanIndexForward": True,  # ascending arrived_at = chain order
            }

            while True:
                response = self._table.query(**query_kwargs)
                all_items.extend(response.get("Items", []))

                if len(all_items) >= depth_cap:
                    all_items = all_items[:depth_cap]
                    break

                if "LastEvaluatedKey" not in response:
                    break
                query_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("ValidationException", "ResourceNotFoundException", "AccessDeniedException"):
                logger.warning(
                    "DynamoDB query failed (chain descendants) — returning empty",
                    extra={"correlation_id": correlation_id, "error_code": error_code},
                )
                return []
            raise

        # Filter: exclude the root item and optionally non-triggering statuses
        descendants: list[InvocationChainItem] = []
        for item in all_items:
            # Issue #1756: fall back to event_id (the real DDB key); the row has
            # no invocation_id/pk attribute, so the old default left this blank.
            inv_id = item.get("invocation_id") or item.get("pk") or item.get("event_id", "")
            if inv_id == root_invocation_id:
                continue  # Skip the root itself

            # Exclude non-triggering statuses from descendants by default
            item_status = item.get("status")
            if not include_non_triggering and item_status in _non_triggering_statuses:
                continue

            descendants.append(
                InvocationChainItem(
                    invocation_id=inv_id,
                    invoked_at=item.get("arrived_at", ""),
                    channel=item.get("channel"),
                    status=item_status,
                    topic=item.get("topic"),
                    persona=item.get("persona"),
                    parent_invocation_id=item.get("parent_invocation_id"),
                    children=[],
                )
            )

        return descendants

    def get_invocation(
        self,
        invocation_id: str,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> InvocationItem | None:
        """Fetch a single invocation by event_id, scoped to user or tenant.

        Uses a Query on the user-index or tenant-index GSI with a FilterExpression
        on event_id (the DDB PK). This is necessary because GetItem would require
        both event_id (PK) AND arrived_at (SK), but the detail endpoint only has
        the invocation_id.

        Returns None if not found (caller should return 404 — existence-hiding).

        Paginates internally (up to 5 pages / ~5 MB read) to find the item in the
        user's partition. This is acceptable for a user-initiated detail page load.
        """
        if not user_id and not tenant_id:
            return None

        if user_id:
            index_name = "user-index"
            pk_name = "user_id"
            pk_value = user_id
        else:
            index_name = "tenant-index"
            pk_name = "tenant_id"
            pk_value = tenant_id

        filter_expr = Attr("event_id").eq(invocation_id)

        query_kwargs: dict = {
            "IndexName": index_name,
            "KeyConditionExpression": Key(pk_name).eq(pk_value),
            "FilterExpression": filter_expr,
            "ScanIndexForward": False,
        }

        max_pages = 5
        try:
            for _ in range(max_pages):
                response = self._table.query(**query_kwargs)
                items = response.get("Items", [])
                if items:
                    return self._map_item(items[0])
                if "LastEvaluatedKey" not in response:
                    break
                query_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("ValidationException", "ResourceNotFoundException", "AccessDeniedException"):
                logger.warning(
                    "DynamoDB query failed (get_invocation) — returning None",
                    extra={"invocation_id": invocation_id, "error_code": error_code},
                )
                return None
            raise

        return None


def _derive_trigger_kind(item: dict) -> TriggerKind:
    """Derive the trigger kind from DynamoDB item fields.

    Logic:
    - Has parent_invocation_id → "agent" (spawned by another run)
    - No parent + is_human_rooted=False → "bot" (cron/automated)
    - Otherwise → "human" (user-initiated)
    """
    if item.get("parent_invocation_id"):
        return "agent"
    if not item.get("is_human_rooted", True):
        return "bot"
    return "human"


def _build_chain_tree(items: list[dict]) -> list[InvocationChainItem]:
    """Build a tree of InvocationChainItem from flat DynamoDB items.

    Items with parent_invocation_id are nested under their parent.
    Items without a parent (or whose parent isn't in the list) are roots.
    Falls back to flat list if no parent edges exist.
    """
    # Create nodes
    nodes: dict[str, InvocationChainItem] = {}
    for item in items:
        # Issue #1756: fall back to event_id (the real DDB key) — see _map_item.
        inv_id = item.get("invocation_id") or item.get("pk") or item.get("event_id", "")
        nodes[inv_id] = InvocationChainItem(
            invocation_id=inv_id,
            invoked_at=item.get("arrived_at", ""),
            channel=item.get("channel"),
            status=item.get("status"),
            topic=item.get("topic"),
            persona=item.get("persona"),
            parent_invocation_id=item.get("parent_invocation_id"),
            children=[],
        )

    # Build parent→children relationships
    roots: list[InvocationChainItem] = []
    for node in nodes.values():
        parent_id = node.parent_invocation_id
        if parent_id and parent_id in nodes:
            nodes[parent_id].children.append(node)
        else:
            roots.append(node)

    return roots


def _safe_int(value) -> int | None:
    """Safely convert a value to int, returning None on failure."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None
