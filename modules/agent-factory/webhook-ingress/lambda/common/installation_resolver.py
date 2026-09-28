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
        logger.error(
            "resolve_installation_for_tenant: IDENTITY_INDEX_TABLE not set"
        )
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
                "resolve_installation_for_tenant: no reverse-lookup row for org_id=%r",
                org_id,
            )
            _emit_resolution_failed_metric(org_id)
            return None

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
