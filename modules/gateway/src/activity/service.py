"""Activity service — DynamoDB query logic for agent invocations.

Reads from the `webhook-events` table (owned by agent-factory) via its
`user-index` (PK=user_id, SK=arrived_at) and `tenant-index`
(PK=tenant_id, SK=arrived_at) GSIs.

Key design decisions:
- Table name resolved from env var WEBHOOK_EVENTS_TABLE (set via SSM in prod).
- Missing GSI/table → returns empty result with a warning log, never 500.
- Cursor is base64(json(LastEvaluatedKey)), opaque to client.
"""

import base64
import json
import logging
import os

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from src.activity.schemas import InvocationItem, InvocationListResponse

logger = logging.getLogger("bedrockgateway.activity")

# Default table name; overridden via env or constructor arg for testability.
_DEFAULT_TABLE_NAME = "adp-dev-webhook-events"


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

        try:
            response = self._table.query(**query_kwargs)
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

        # Map DDB items to response schema
        items = [self._map_item(item) for item in response.get("Items", [])]

        # Encode next cursor if DDB says there are more pages
        next_cursor = None
        if "LastEvaluatedKey" in response:
            next_cursor = _encode_cursor(response["LastEvaluatedKey"])

        return InvocationListResponse(items=items, count=len(items), last_key=next_cursor)

    @staticmethod
    def _map_item(item: dict) -> InvocationItem:
        """Map a raw DynamoDB item to the InvocationItem schema."""
        return InvocationItem(
            invocation_id=item.get("invocation_id", item.get("pk", "")),
            invoked_at=item.get("arrived_at", ""),
            channel=item.get("channel"),
            status=item.get("status"),
            status_updated_at=item.get("status_updated_at"),
            topic=item.get("topic"),
            persona=item.get("persona"),
            summary=item.get("summary"),
            source_url=item.get("source_url"),
            repo=item.get("repo"),
            issue_number=_safe_int(item.get("issue_number")),
            correlation_id=item.get("correlation_id"),
            run_id=item.get("run_id"),
            error_message=item.get("error_message"),
        )


def _safe_int(value) -> int | None:
    """Safely convert a value to int, returning None on failure."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None
