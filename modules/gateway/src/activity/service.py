"""Activity service — DynamoDB query logic for agent invocations.

Reads from the `webhook-events` table (owned by agent-factory) via its GSIs:
- `user-index` (PK=user_id, SK=arrived_at)
- `tenant-index` (PK=tenant_id, SK=arrived_at)
- `root-human-index` (PK=root_human_id, SK=arrived_at) — sparse, chain-attributed
- `correlation-index` (PK=correlation_id, SK=arrived_at) — chain assembly

Also queries the base table directly (PK=event_id, SK=arrived_at) for O(1)
single-invocation lookups.

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
        """Query invocations for a specific user via user-index + root-human-index.

        Issue #3705: Queries BOTH user-index (direct runs) and root-human-index
        (chain runs attributed to this user via root_human_id) for list-parity
        with the stats dashboard. Merges with dedup on event_id. Falls back to
        user-index-only if root-human-index is missing.

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
        # Primary query: direct runs (user_id = caller)
        primary = self._execute_query(
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

        # Secondary query: chain runs attributed via root_human_id
        # Only fetch if we have no cursor (first page) or cursor belongs to user-index.
        # On paginated calls, root-human-index items are already merged from page 1;
        # we fetch them again to ensure completeness but dedup handles overlap.
        secondary = self._execute_query(
            index_name="root-human-index",
            partition_key_name="root_human_id",
            partition_key_value=user_id,
            page_size=page_size,
            last_key=None,  # root-human-index has its own key space
            status=status,
            channel=channel,
            persona=persona,
            since=since,
            until=until,
            include_non_triggering=include_non_triggering,
        )

        # If secondary returned nothing (GSI missing or no chain runs), return primary as-is
        if not secondary.items:
            return primary

        # Merge with dedup on invocation_id, sorted by invoked_at descending
        seen_ids: set[str] = set()
        merged: list[InvocationItem] = []

        for item in primary.items:
            if item.invocation_id not in seen_ids:
                seen_ids.add(item.invocation_id)
                merged.append(item)

        for item in secondary.items:
            if item.invocation_id not in seen_ids:
                seen_ids.add(item.invocation_id)
                merged.append(item)

        # Sort merged by invoked_at descending (newest first)
        merged.sort(key=lambda x: x.invoked_at or "", reverse=True)

        # Trim to page_size
        trimmed = merged[:page_size]

        # Preserve the primary cursor for pagination (user-index drives pagination)
        return InvocationListResponse(
            items=trimmed,
            count=len(trimmed),
            last_key=primary.last_key,
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
            # Issue #3069: S3 transcript key
            transcript_key=item.get("transcript_key"),
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
        include_non_triggering: bool = False,
    ) -> InvocationChainResponse:
        """Retrieve all invocations sharing a correlation_id and build a tree.

        Authorization (membership-based, Issue #3949):
        - For /me/ (user_id): authorize the CHAIN, not each row. The caller may
          read the chain if ANY member has user_id == caller or root_human_id ==
          caller. Once authorized, return ALL members unfiltered. This prevents
          mid-chain row drops from sparse root_human_id (pre-#2042 rows lack it)
          which would cause _build_chain_tree to promote orphans to roots.
        - For /admin/ (tenant_id): authorize by tenant_id on any member.
        - If both are None, returns empty (safety: never return unscoped data).

        Issue #3708: When include_non_triggering is False (default), excludes
        no_op and webhook_received statuses — the same convention as the flat
        list endpoints (Issue #1658). This prevents webhook echoes from
        appearing as phantom child runs in the chain view. The depth_cap is
        applied AFTER filtering, so cap budget is not wasted on echoes.

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
        # GSI (PK=correlation_id, SK=arrived_at). A Query is bounded and cheap.
        # Issue #3949: membership-based scoping — fetch ALL items, then authorize
        # the chain as a whole. No per-row user_id filter (root_human_id is sparse;
        # per-row filtering drops mid-chain members and restructures the tree).
        # Status filtering (non-triggering) is still applied at the DDB level.
        status_filter = None
        if not include_non_triggering:
            _non_triggering = ["no_op", "webhook_received"]
            status_filter = ~Attr("status").is_in(_non_triggering)

        # Tenant scoping is still applied as a FilterExpression (tenant_id is
        # always present on every row, so it's safe as a per-row filter).
        filter_expr = None
        if tenant_id:
            filter_expr = Attr("tenant_id").eq(tenant_id)
            if status_filter:
                filter_expr = filter_expr & status_filter
        elif status_filter:
            filter_expr = status_filter

        all_items: list[dict] = []
        depth_capped = False

        try:
            query_kwargs: dict = {
                "IndexName": "correlation-index",
                "KeyConditionExpression": Key("correlation_id").eq(correlation_id),
                "ScanIndexForward": True,  # ascending arrived_at = chain order
            }
            if filter_expr is not None:
                query_kwargs["FilterExpression"] = filter_expr

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

        # Issue #3949: Membership-based authorization for user-scoped chains.
        # Authorize the chain as a whole: the caller may read it if ANY member
        # has user_id == caller or root_human_id == caller. If none match,
        # return empty (existence-hiding 404 semantics preserved).
        if user_id:
            authorized = any(item.get("user_id") == user_id or item.get("root_human_id") == user_id for item in all_items)
            if not authorized:
                return InvocationChainResponse(
                    correlation_id=correlation_id,
                    items=[],
                    total_count=0,
                    depth_capped=False,
                )

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
        """Query chains for a specific user via user-index + root-human-index.

        Issue #1662: Chain-grouped board view.
        Issue #3723: Merged fetch — queries BOTH user-index (direct runs) and
        root-human-index (bot-attributed runs where root_human_id = caller) for
        parity with query_by_user and the stats dashboard. Merges with dedup on
        invocation_id. Falls back to user-index-only if root-human-index is
        missing.

        Steps:
        1. Query user-index + root-human-index, merge with dedup (same pattern
           as query_by_user).
        2. For each root with a correlation_id, fetch chain members via
           correlation-index GSI (exclude non-triggering statuses from
           descendants by default).
        3. Assemble ChainSummary objects (root + descendants).

        Cost enrichment is handled at the route layer (batched Postgres query).
        """
        # Step 1: Get the page of roots via merged fetch (user-index + root-human-index)
        primary = self._execute_query(
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

        # Issue #3723: Secondary query — bot-attributed roots via root-human-index
        secondary = self._execute_query(
            index_name="root-human-index",
            partition_key_name="root_human_id",
            partition_key_value=user_id,
            page_size=page_size,
            last_key=None,  # root-human-index has its own key space
            status=status,
            channel=channel,
            persona=persona,
            since=since,
            until=until,
            include_non_triggering=include_non_triggering,
        )

        # Merge with dedup on invocation_id (same pattern as query_by_user)
        if secondary.items:
            seen_ids: set[str] = set()
            merged: list[InvocationItem] = []
            for item in primary.items:
                if item.invocation_id not in seen_ids:
                    seen_ids.add(item.invocation_id)
                    merged.append(item)
            for item in secondary.items:
                if item.invocation_id not in seen_ids:
                    seen_ids.add(item.invocation_id)
                    merged.append(item)
            # Sort merged by invoked_at descending (newest first)
            merged.sort(key=lambda x: x.invoked_at or "", reverse=True)
            # Trim to page_size
            merged = merged[:page_size]
            flat_result = InvocationListResponse(
                items=merged,
                count=len(merged),
                last_key=primary.last_key,
            )
        else:
            flat_result = primary

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
        # Issue #3949: TWO-PASS emission. The page is ordered newest-first, so a
        # descendant is frequently visited BEFORE its own root. A single pass
        # would then fire a backfill query for a chain whose root is already on
        # the page, and anchor the row on the backfilled copy instead of the
        # on-page root — and if that backfill query degrades (TTL/IAM error), the
        # chain would be dropped entirely even though its root was right there.
        # Pass 1 emits every true root; pass 2 backfills only chains pass 1 did
        # not cover. This makes the cost bound (≤ page_size extra queries) and
        # the "root + child on one page → zero backfills" guarantee hold
        # independent of page order.
        chains: list[ChainSummary] = []
        _seen_correlations: set[str] = set()
        _pending_backfill: list[str] = []

        # ---- Pass 1: emit true roots (and singletons) in page order ----
        for root_item in flat_result.items:
            correlation_id = root_item.correlation_id

            # A run WITH a parent is not a chain root — defer it to pass 2, which
            # backfills the real root only if no root for this chain is on-page.
            if correlation_id and root_item.triggered_by_invocation_id:
                _pending_backfill.append(correlation_id)
                continue

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

            # One top-level row per chain: if we've already emitted this chain
            # (from its true root), don't add it again.
            if correlation_id in _seen_correlations:
                continue
            _seen_correlations.add(correlation_id)

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

        # ---- Pass 2: backfill roots for chains with no root on this page ----
        # Cost bound: at most one correlation-index root fetch per DISTINCT
        # un-emitted correlation_id, so ≤ page_size extra queries per page, and
        # zero in the common case where every chain's root is on-page.
        for correlation_id in _pending_backfill:
            if correlation_id in _seen_correlations:
                continue
            _seen_correlations.add(correlation_id)

            backfilled_root = self._backfill_chain_root(correlation_id)
            if backfilled_root is None:
                # Root fetch degraded (query error, or the whole chain expired) —
                # skip rather than emitting a rootless chain row.
                continue

            descendants = self._fetch_chain_descendants(
                correlation_id=correlation_id,
                root_invocation_id=backfilled_root.invocation_id,
                include_non_triggering=include_non_triggering,
            )
            chains.append(
                ChainSummary(
                    chain_id=correlation_id,
                    root=backfilled_root,
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

        Issue #3723: Filter non-triggering statuses INSIDE the accumulation loop
        so that depth_cap counts only REAL descendants. Previously, depth_cap was
        applied to raw items before filtering, so a noisy chain (many no_op
        echoes) would fill the cap with noise and silently truncate real
        descendants beyond item 50. The max-page cap (20) remains as a runaway-
        protection backstop against unbounded DDB reads.
        """
        # Non-triggering statuses to exclude from descendants
        _non_triggering_statuses = {"no_op", "webhook_received"}

        # Issue #3723: Accumulate filtered descendants directly, applying
        # depth_cap to the POST-FILTER count so noise doesn't consume budget.
        descendants: list[InvocationChainItem] = []
        max_pages = 20  # backstop: max raw DDB pages to read (runaway protection)
        pages = 0
        try:
            query_kwargs: dict = {
                "IndexName": "correlation-index",
                "KeyConditionExpression": Key("correlation_id").eq(correlation_id),
                "ScanIndexForward": True,  # ascending arrived_at = chain order
            }

            while True:
                pages += 1
                response = self._table.query(**query_kwargs)

                for item in response.get("Items", []):
                    # Issue #1756: fall back to event_id (the real DDB key)
                    inv_id = item.get("invocation_id") or item.get("pk") or item.get("event_id", "")
                    if inv_id == root_invocation_id:
                        continue  # Skip the root itself

                    # Filter non-triggering statuses inside the loop
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
                            transcript_key=item.get("transcript_key"),
                        )
                    )

                    # depth_cap counts REAL descendants only
                    if len(descendants) >= depth_cap:
                        return descendants

                if "LastEvaluatedKey" not in response or pages >= max_pages:
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

        return descendants

    def _backfill_chain_root(self, correlation_id: str) -> InvocationItem | None:
        """Fetch the chain root for a correlation_id via correlation-index.

        Issue #3949: When page 1 of query_chains_by_user contains only descendants
        (all have parent_invocation_id), the emission loop has no root to anchor
        the chain. This helper fetches the earliest item in the correlation (the
        root, which has no parent_invocation_id) and returns it as an InvocationItem.

        If the true root is TTL-expired or missing (all items have
        parent_invocation_id), falls back to the earliest member as the chain
        representative — a chain row anchored on a surviving descendant beats an
        empty view.

        Returns None if:
        - correlation-index query fails (graceful degradation)
        - no items found at all (chain fully expired)

        Cost: one correlation-index query (ScanIndexForward=True, first page only).
        """
        try:
            response = self._table.query(
                IndexName="correlation-index",
                KeyConditionExpression=Key("correlation_id").eq(correlation_id),
                ScanIndexForward=True,  # ascending arrived_at → root is first
            )
            items = response.get("Items", [])
            # Find the true root (no parent_invocation_id)
            for item in items:
                if not item.get("parent_invocation_id"):
                    return self._map_item(item)
            # TTL-expired root: fall back to the earliest member
            if items:
                return self._map_item(items[0])
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("ValidationException", "ResourceNotFoundException", "AccessDeniedException"):
                logger.warning(
                    "DynamoDB query failed (backfill chain root) — skipping",
                    extra={"correlation_id": correlation_id, "error_code": error_code},
                )
                return None
            raise

        return None

    def get_invocation(
        self,
        invocation_id: str,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> InvocationItem | None:
        """Fetch a single invocation by event_id, with authorize-after-fetch.

        Issue #3949: Uses a base-table Query on event_id (the DDB hash key) for
        an O(1) lookup — strictly cheaper than the previous GSI-based approach
        and eliminates false-404s for items beyond a pagination cap.

        Authorization (post-fetch):
        - user_id provided: allow if item.user_id == caller OR
          item.root_human_id == caller. This covers both direct runs and
          chain-attributed runs (bot user_id, human root_human_id).
        - tenant_id provided: allow if item.tenant_id == caller_tenant.
        - Neither provided: return None (safety).

        Returns None if not found or not authorized (existence-hiding 404).
        """
        if not user_id and not tenant_id:
            return None

        try:
            response = self._table.query(
                KeyConditionExpression=Key("event_id").eq(invocation_id),
                ScanIndexForward=False,
            )
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("ValidationException", "ResourceNotFoundException", "AccessDeniedException"):
                logger.warning(
                    "DynamoDB query failed (get_invocation) — returning None",
                    extra={
                        "invocation_id": invocation_id,
                        "error_code": error_code,
                    },
                )
                return None
            raise

        items = response.get("Items", [])
        if not items:
            return None

        row = items[0]

        # Authorize after fetch — existence-hiding 404 preserved
        if user_id:
            if row.get("user_id") != user_id and row.get("root_human_id") != user_id:
                return None
        elif tenant_id:
            if row.get("tenant_id") != tenant_id:
                return None

        return self._map_item(row)


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
            transcript_key=item.get("transcript_key"),
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
