"""Tenant resolution: maps channel-specific identifiers to tenant_id.

For GitHub: installation_id → tenant_id via DynamoDB lookup.
"""

import logging
import os

import boto3

logger = logging.getLogger(__name__)

TENANTS_TABLE = os.environ.get("TENANTS_TABLE", "adp-tenants")
REGION = os.environ.get("AWS_REGION", "us-east-1")

_dynamodb = None


def _get_table():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=REGION)
    return _dynamodb.Table(TENANTS_TABLE)


def resolve_tenant(installation_id: int) -> dict | None:
    """Resolve a GitHub App installation_id to tenant metadata.

    Args:
        installation_id: The GitHub App installation ID from the webhook payload.

    Returns:
        Dict with at minimum {"tenant_id": str} or None if unknown installation.
    """
    try:
        table = _get_table()
        resp = table.get_item(Key={"PK": f"github#installation#{installation_id}"})
        item = resp.get("Item")
        if not item:
            logger.info(
                "Unknown installation_id=%d — no tenant mapping", installation_id
            )
            return None
        return {
            "tenant_id": item["tenant_id"],
            "org_name": item.get("org_name", ""),
            "plan": item.get("plan", "free"),
        }
    except Exception as e:
        logger.error(
            "Tenant resolution failed for installation_id=%d: %s", installation_id, e
        )
        return None
