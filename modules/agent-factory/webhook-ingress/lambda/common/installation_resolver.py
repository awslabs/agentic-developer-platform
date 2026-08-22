"""Resolve GitHub App installation_id for a tenant (org_id).

Issue #2336: The EventBridge and agent-trigger handlers hardcoded
installation_id=0, causing workers to crash with a 404 when minting
tokens. This module provides a reverse-lookup from org_id to the
GitHub App installation_id stored in the identity-index DynamoDB table.

The identity-index stores forward rows:
  PK=github_installation_id, SK=<installation_id> -> org_id

This module queries the REVERSE row:
  PK=org_installation, SK=<org_id> -> installation_id

The reverse row is written by _auto_register_installation() in
github/handler.py whenever a webhook reveals a new installation.
"""

from __future__ import annotations

import logging
import os

import boto3

logger = logging.getLogger(__name__)

IDENTITY_INDEX_TABLE = os.environ.get("IDENTITY_INDEX_TABLE", "")
REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))

_dynamodb = None


def _get_table():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=REGION)
    return _dynamodb.Table(IDENTITY_INDEX_TABLE)


def resolve_installation_for_tenant(org_id: str) -> int | None:
    """Resolve the GitHub App installation_id for a given org/tenant.

    Performs a single GetItem on the identity-index table with key:
      identity_type = "org_installation"
      identity_value = <org_id>

    Args:
        org_id: The ADP org/tenant identifier (e.g. "aws-e").

    Returns:
        The installation_id (int) if found, or None if the reverse-lookup
        row does not exist or the table is not configured.
    """
    if not org_id:
        logger.warning("resolve_installation_for_tenant: empty org_id")
        return None

    if not IDENTITY_INDEX_TABLE:
        logger.error("resolve_installation_for_tenant: IDENTITY_INDEX_TABLE not set")
        return None

    try:
        table = _get_table()
        resp = table.get_item(
            Key={
                "identity_type": "org_installation",
                "identity_value": org_id,
            }
        )
        item = resp.get("Item")
        if not item:
            logger.warning(
                "resolve_installation_for_tenant: no reverse-lookup row "
                "for org_id=%r — attempting forward-scan fallback",
                org_id,
            )
            # Issue #3860: Forward-scan fallback — query forward rows
            # (github_installation_id) for matching org_id. If exactly one
            # match, write-through the reverse row and return it.
            fallback_id = _forward_scan_fallback(table, org_id)
            if fallback_id is None:
                _emit_resolution_failed_metric(org_id)
            return fallback_id

        installation_id = item.get("installation_id")
        if installation_id is None:
            logger.warning(
                "resolve_installation_for_tenant: row exists but "
                "installation_id is None for org_id=%r",
                org_id,
            )
            return None

        return int(installation_id)

    except Exception as e:
        logger.error(
            "resolve_installation_for_tenant: DDB error for org_id=%r: %s",
            org_id,
            e,
        )
        return None


def _forward_scan_fallback(table, org_id: str) -> int | None:
    """Forward-scan fallback: find the installation_id by scanning forward rows.

    Issue #3860: When the reverse row (org_installation) is missing (e.g.
    UI-installed tenants before the dual-write fix), scan the forward rows
    (github_installation_id) for any that map to this org_id. If exactly
    one match is found, write-through the reverse row and return the
    installation_id. If zero or multiple matches, refuse (ambiguous).

    This is a lazy self-heal: the first successful resolution fixes the gap
    permanently by writing the reverse row.
    """
    try:
        # Query forward rows where org_id matches. The identity-index table
        # has identity_type as PK and identity_value as SK. We need to scan
        # rows where identity_type="github_installation_id" and org_id=<org_id>.
        # Use Query with a filter expression on org_id.
        resp = table.query(
            KeyConditionExpression="identity_type = :it",
            FilterExpression="org_id = :org",
            ExpressionAttributeValues={
                ":it": "github_installation_id",
                ":org": org_id,
            },
        )
        items = resp.get("Items", [])

        if not items:
            logger.warning(
                "resolve_installation_for_tenant: forward-scan found no "
                "github_installation_id rows for org_id=%r",
                org_id,
            )
            return None

        if len(items) > 1:
            installation_ids = [i.get("identity_value") for i in items]
            logger.error(
                "resolve_installation_for_tenant: forward-scan found %d "
                "github_installation_id rows for org_id=%r — refusing ambiguous "
                "resolution (installation_ids=%r)",
                len(items),
                org_id,
                installation_ids,
            )
            return None

        # Exactly one match — resolve and write-through the reverse row
        matched_item = items[0]
        installation_id_str = matched_item.get("identity_value")
        if not installation_id_str:
            logger.error(
                "resolve_installation_for_tenant: forward-scan matched item "
                "has no identity_value for org_id=%r",
                org_id,
            )
            return None

        installation_id = int(installation_id_str)

        # Write-through: create the reverse row so future lookups are O(1)
        try:
            table.put_item(
                Item={
                    "identity_type": "org_installation",
                    "identity_value": org_id,
                    "installation_id": installation_id,
                    "updated_at": _now_iso(),
                    "auto_registered": True,
                },
                # Only write if no row exists (avoid race with concurrent writes)
                ConditionExpression="attribute_not_exists(identity_type)",
            )
            logger.info(
                "resolve_installation_for_tenant: forward-scan self-healed — "
                "wrote reverse row org_installation/%s → %d",
                org_id,
                installation_id,
            )
        except Exception as write_exc:  # noqa: BLE001
            # Write-through is best-effort; the resolution still succeeds
            logger.warning(
                "resolve_installation_for_tenant: forward-scan self-heal "
                "write-through failed for org_id=%r: %s (resolution still succeeds)",
                org_id,
                write_exc,
            )

        return installation_id

    except Exception as e:
        logger.error(
            "resolve_installation_for_tenant: forward-scan fallback error "
            "for org_id=%r: %s",
            org_id,
            e,
        )
        return None


def _now_iso() -> str:
    """Return current UTC time in ISO format."""
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _emit_resolution_failed_metric(org_id: str) -> None:
    """Emit CloudWatch metric when installation resolution fails (fail-soft)."""
    try:
        import boto3 as _boto3

        cw = _boto3.client("cloudwatch", region_name=REGION)
        cw.put_metric_data(
            Namespace="WebhookIngress",
            MetricData=[
                {
                    "MetricName": "InstallationResolutionFailed",
                    "Dimensions": [
                        {"Name": "org_id", "Value": org_id},
                    ],
                    "Value": 1,
                    "Unit": "Count",
                }
            ],
        )
    except Exception as e:
        logger.debug("Failed to emit InstallationResolutionFailed metric: %s", e)
