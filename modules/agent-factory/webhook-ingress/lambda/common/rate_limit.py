"""Tenant-level rate limiting via DynamoDB atomic counters.

Simple sliding-window rate limiter. Each tenant gets N requests per minute.
"""

import logging
import os
import time

import boto3

logger = logging.getLogger(__name__)

RATE_LIMIT_TABLE = os.environ.get("RATE_LIMIT_TABLE", "adp-rate-limits")
REGION = os.environ.get("AWS_REGION", "us-east-1")
# Default: 60 webhook events per minute per tenant
DEFAULT_RATE_LIMIT = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "60"))

_dynamodb = None


def _get_table():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=REGION)
    return _dynamodb.Table(RATE_LIMIT_TABLE)


def check_rate_limit(tenant_id: str) -> tuple[bool, int]:
    """Check if a tenant is within their rate limit.

    Args:
        tenant_id: The tenant identifier.

    Returns:
        Tuple of (is_allowed: bool, retry_after_seconds: int).
        If allowed, retry_after is 0. If limited, retry_after is seconds to wait.
    """
    window_key = f"{tenant_id}#{int(time.time()) // 60}"

    try:
        table = _get_table()
        resp = table.update_item(
            Key={"PK": window_key},
            UpdateExpression="SET #cnt = if_not_exists(#cnt, :zero) + :one, #ttl = :ttl",
            ExpressionAttributeNames={"#cnt": "count", "#ttl": "expires_at"},
            ExpressionAttributeValues={
                ":zero": 0,
                ":one": 1,
                ":ttl": int(time.time()) + 120,
            },
            ReturnValues="UPDATED_NEW",
        )
        count = int(resp["Attributes"]["count"])
        if count > DEFAULT_RATE_LIMIT:
            seconds_remaining = 60 - (int(time.time()) % 60)
            logger.warning("Rate limited tenant=%s count=%d", tenant_id, count)
            return False, seconds_remaining
        return True, 0
    except Exception as e:
        # Fail open — don't block customers on rate-limit infra issues
        logger.error("Rate limit check failed for tenant=%s: %s (allowing)", tenant_id, e)
        return True, 0
