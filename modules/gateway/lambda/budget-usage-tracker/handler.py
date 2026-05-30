"""
Budget Usage Tracker Lambda Handler.

Triggered by S3 PutObject events on the chat logs bucket. Extracts actual
token counts from Bedrock responses, calculates cost, and upserts usage
into the budget_usage table in RDS.

Issue #234: Budget Usage Tracking Lambda

Environment Variables:
    DB_HOST: RDS hostname
    DB_PORT: RDS port (default: 5432)
    DB_NAME: Database name (default: bedrockgateway)
    DB_USERNAME: Database username (default: bgadmin)
    AWS_REGION: AWS region (default: us-east-1)
"""

import json
import logging
import os
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import boto3

# Add shared module to path for local Lambda deployment
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))

from db import get_db_connection
from pricing_fallback import MODEL_PRICING, calculate_cost, resolve_model_id

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# S3 client
s3_client = boto3.client("s3")

# In-memory pricing cache with TTL
_pricing_cache: dict[str, dict[str, Any]] = {}
_pricing_cache_time: float = 0
_PRICING_CACHE_TTL = 3600  # 1 hour


def get_period_starts(timestamp: datetime) -> dict[str, datetime]:
    """
    Calculate period start dates for daily, weekly, and monthly periods.

    Args:
        timestamp: The timestamp of the chat log

    Returns:
        Dict with period_type -> period_start mapping
    """
    # Ensure timezone-aware
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)

    date = timestamp.date()

    # Daily: start of the day
    daily_start = datetime(date.year, date.month, date.day, tzinfo=UTC)

    # Weekly: Monday of the week (weekday() returns 0 for Monday)
    days_since_monday = date.weekday()
    monday = date - timedelta(days=days_since_monday)
    weekly_start = datetime(monday.year, monday.month, monday.day, tzinfo=UTC)

    # Monthly: first of the month
    monthly_start = datetime(date.year, date.month, 1, tzinfo=UTC)

    return {
        "daily": daily_start,
        "weekly": weekly_start,
        "monthly": monthly_start,
    }


def load_pricing_from_db(conn) -> dict[str, dict[str, Decimal]]:
    """
    Load pricing from the model_pricing database table.

    Args:
        conn: Database connection

    Returns:
        Dict mapping model_id to pricing info
    """
    pricing = {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT model_id, input_price_per_1k_tokens, output_price_per_1k_tokens
                FROM model_pricing
                """
            )
            for row in cur.fetchall():
                model_id, input_price, output_price = row
                pricing[model_id] = {
                    "input": Decimal(str(input_price)),
                    "output": Decimal(str(output_price)),
                }
        logger.info(f"Loaded {len(pricing)} models from model_pricing table")
    except Exception as e:
        logger.warning(f"Failed to load pricing from database: {e}")

    return pricing


def get_pricing_table(conn) -> dict[str, dict[str, Any]]:
    """
    Get pricing table with caching.

    Tries to load from database first, falls back to hardcoded pricing.

    Args:
        conn: Database connection

    Returns:
        Dict mapping model_id to pricing info
    """
    global _pricing_cache, _pricing_cache_time

    now = time.monotonic()

    # Check cache TTL
    if _pricing_cache and (now - _pricing_cache_time) < _PRICING_CACHE_TTL:
        return _pricing_cache

    # Try to load from database
    db_pricing = load_pricing_from_db(conn)

    if db_pricing:
        _pricing_cache = db_pricing
        _pricing_cache_time = now
        return db_pricing

    # Fall back to hardcoded pricing
    logger.info("Using fallback hardcoded pricing table")
    _pricing_cache = MODEL_PRICING
    _pricing_cache_time = now
    return MODEL_PRICING


def parse_chat_log(chat_log: dict[str, Any]) -> dict[str, Any] | None:
    """
    Parse and validate chat log JSON.

    Issue #249: Added support for agent usage tracking via account_type and
    agent_id fields in chat logs.

    Args:
        chat_log: Parsed chat log dictionary

    Returns:
        Parsed data dict or None if invalid
    """
    required_fields = ["org_id", "user_id", "model"]

    for field in required_fields:
        if field not in chat_log:
            logger.warning(f"Missing required field: {field}")
            return None

    # Extract usage from response
    response = chat_log.get("response", {})
    usage = response.get("usage", {})

    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")

    if input_tokens is None or output_tokens is None:
        logger.warning("Missing token counts in response.usage")
        return None

    # Parse timestamp
    timestamp_str = chat_log.get("timestamp")
    if timestamp_str:
        try:
            # Handle ISO format with Z suffix
            if timestamp_str.endswith("Z"):
                timestamp_str = timestamp_str[:-1] + "+00:00"
            timestamp = datetime.fromisoformat(timestamp_str)
        except ValueError:
            logger.warning(f"Invalid timestamp format: {timestamp_str}")
            timestamp = datetime.now(UTC)
    else:
        timestamp = datetime.now(UTC)

    return {
        "org_id": chat_log["org_id"],
        "user_id": chat_log["user_id"],
        "team_id": chat_log.get("team_id"),
        "model": chat_log["model"],
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "timestamp": timestamp,
        # Issue #1016: request_id for bridging cost back to usage_logs
        "request_id": chat_log.get("request_id"),
        # Issue #249: Agent-specific fields
        "account_type": chat_log.get("account_type"),  # "service" for agents
        "agent_id": chat_log.get("agent_id"),  # Agent UUID if IAM-authenticated
        "budget_config_id": chat_log.get("budget_config_id"),  # Agent's budget config ID
    }


def upsert_budget_usage(
    conn,
    org_id: str,
    entity_type: str,
    entity_id: str,
    period_start: datetime,
    period_type: str,
    cost: Decimal,
    tokens: int,
):
    """
    Upsert budget usage record.

    Uses PostgreSQL ON CONFLICT to atomically increment usage.

    Args:
        conn: Database connection
        org_id: Organization ID
        entity_type: Entity type (user/team/organization)
        entity_id: Entity ID
        period_start: Start of the period
        period_type: Period type (daily/weekly/monthly)
        cost: Cost in USD
        tokens: Total tokens (input + output)
    """
    usage_id = str(uuid.uuid4())

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO budget_usage (
                id, org_id, entity_type, entity_id, period_start, period_type,
                total_cost_usd, total_tokens, request_count
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1)
            ON CONFLICT (org_id, entity_type, entity_id, period_start, period_type)
            DO UPDATE SET
                total_cost_usd = budget_usage.total_cost_usd + EXCLUDED.total_cost_usd,
                total_tokens = budget_usage.total_tokens + EXCLUDED.total_tokens,
                request_count = budget_usage.request_count + 1
            """,
            (
                usage_id,
                org_id,
                entity_type,
                entity_id,
                period_start.date(),
                period_type,
                float(cost),
                tokens,
            ),
        )


def bridge_cost_to_usage_logs(conn, request_id: str, cost: Decimal) -> bool:
    """
    Bridge calculated cost back to the usage_logs table.

    Issue #1074: Updates usage_logs.cost_usd for the matching request_id so
    the admin dashboard cost tile (which reads SUM(usage_logs.cost_usd)) shows
    real numbers.

    Args:
        conn: Database connection
        request_id: The request ID linking the chat log to usage_logs
        cost: Calculated cost in USD

    Returns:
        True if a row was updated, False otherwise
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE usage_logs
                SET cost_usd = %s
                WHERE request_id = %s AND cost_usd = 0
                """,
                (float(cost), request_id),
            )
            updated = cur.rowcount > 0
            if updated:
                logger.info(f"Bridged cost_usd=${cost} to usage_logs for request_id={request_id}")
            else:
                logger.debug(f"No usage_logs row found for request_id={request_id} (or already populated)")
            return updated
    except Exception as e:
        logger.warning(f"Failed to bridge cost to usage_logs: {e}")
        return False


def process_chat_log(conn, chat_log: dict[str, Any], pricing_table: dict[str, Any]):
    """
    Process a single chat log and record usage.

    Issue #249: Added agent-level usage tracking. When a chat log has
    account_type="service" and agent_id is present, usage is also recorded
    to the agent entity (entity_type="agent").

    Issue #1074: Bridges calculated cost back to usage_logs.cost_usd so
    the admin dashboard cost tile shows real spend.

    Args:
        conn: Database connection
        chat_log: Parsed chat log dictionary
        pricing_table: Model pricing table
    """
    parsed = parse_chat_log(chat_log)
    if not parsed:
        logger.warning("Skipping invalid chat log")
        return

    org_id = parsed["org_id"]
    user_id = parsed["user_id"]
    team_id = parsed["team_id"]
    model_id = parsed["model"]
    input_tokens = parsed["input_tokens"]
    output_tokens = parsed["output_tokens"]
    timestamp = parsed["timestamp"]
    request_id = parsed.get("request_id")

    # Issue #249: Agent-specific fields
    account_type = parsed.get("account_type")
    agent_id = parsed.get("agent_id")

    # Resolve cross-region model ID
    resolved_model_id = resolve_model_id(model_id)

    # Calculate cost
    cost = calculate_cost(resolved_model_id, input_tokens, output_tokens, pricing_table)
    total_tokens = input_tokens + output_tokens

    logger.info(f"Processing: model={model_id}, resolved={resolved_model_id}, input={input_tokens}, output={output_tokens}, cost=${cost}")

    # Issue #1074: Bridge cost to usage_logs for dashboard visibility
    if request_id and cost > 0:
        bridge_cost_to_usage_logs(conn, request_id, cost)

    # Get period starts
    periods = get_period_starts(timestamp)

    # Record usage for each entity level and period type
    entities = [
        ("user", user_id),
        ("organization", org_id),
    ]

    # Add team if present
    if team_id:
        entities.append(("team", team_id))

    # Issue #249: Add agent entity if this is an IAM-authenticated agent request
    if account_type == "service" and agent_id:
        entities.append(("agent", agent_id))
        logger.info(f"Including agent entity: {agent_id}")

    for entity_type, entity_id in entities:
        for period_type, period_start in periods.items():
            upsert_budget_usage(
                conn,
                org_id,
                entity_type,
                entity_id,
                period_start,
                period_type,
                cost,
                total_tokens,
            )

    logger.info(f"Recorded usage for {len(entities)} entities x {len(periods)} periods")


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Lambda handler for S3 PutObject events.

    Processes chat log JSON files and records budget usage.

    Args:
        event: S3 event notification
        context: Lambda context

    Returns:
        Response dict with status
    """
    records = event.get("Records", [])
    logger.info(f"Received {len(records)} S3 event records")

    processed_count = 0
    error_count = 0

    try:
        with get_db_connection() as conn:
            # Load pricing table (cached for 1 hour)
            pricing_table = get_pricing_table(conn)

            # Process each S3 record
            for record in event.get("Records", []):
                try:
                    # Extract S3 bucket and key
                    s3_info = record.get("s3", {})
                    bucket = s3_info.get("bucket", {}).get("name")
                    key = s3_info.get("object", {}).get("key")

                    if not bucket or not key:
                        logger.warning(f"Missing bucket or key in record: {record}")
                        error_count += 1
                        continue

                    # Skip non-JSON files
                    if not key.endswith(".json"):
                        logger.info(f"Skipping non-JSON file: {key}")
                        continue

                    logger.info(f"Processing s3://{bucket}/{key}")

                    # Read chat log from S3
                    response = s3_client.get_object(Bucket=bucket, Key=key)
                    body = response["Body"].read().decode("utf-8")
                    chat_log = json.loads(body)

                    # Process the chat log
                    process_chat_log(conn, chat_log, pricing_table)
                    processed_count += 1

                except json.JSONDecodeError as e:
                    logger.error(f"Invalid JSON in {key}: {e}")
                    error_count += 1
                except Exception as e:
                    logger.error(f"Error processing record: {e}", exc_info=True)
                    error_count += 1

    except Exception as e:
        logger.error(f"Database connection error: {e}", exc_info=True)
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)}),
        }

    result = {
        "statusCode": 200,
        "body": json.dumps(
            {
                "processed": processed_count,
                "errors": error_count,
            }
        ),
    }

    logger.info(f"Completed: {result['body']}")
    return result
