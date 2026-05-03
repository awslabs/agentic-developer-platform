"""Identity resolution: maps channel identifiers to tenant + user via identity-index.

Phase B.1 (Issue #402): Replaces tenant_resolver.py.
Reads from adp-dev-identity-index DynamoDB table (PK=identity_type, SK=identity_value).

Flow:
  1. Resolve tenant via identity_type="github_installation_id"
  2. Resolve sender via identity_type="github_user"
  3. Cross-check: sender's org_id must match installation's org_id
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import boto3

logger = logging.getLogger(__name__)

# Env var set by Terraform in lambdas.tf
IDENTITY_INDEX_TABLE = os.environ.get("IDENTITY_INDEX_TABLE", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")

_dynamodb = None


@dataclass
class ResolvedIdentity:
    """Result of a successful identity resolution."""

    tenant_id: str
    org_id: str
    user_id: str
    user_provisioning_mode: str  # "strict" | "auto_provision"


def _get_table():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=REGION)
    return _dynamodb.Table(IDENTITY_INDEX_TABLE)


def resolve(
    installation_id: int, sender_id: int
) -> tuple[ResolvedIdentity | None, str]:
    """Resolve installation + sender to a full identity.

    Args:
        installation_id: GitHub App installation ID from webhook payload.
        sender_id: GitHub sender.id from webhook payload.

    Returns:
        Tuple of (ResolvedIdentity or None, outcome_reason).
        outcome_reason is one of: "ok", "unknown_installation", "unknown_user",
        "cross_tenant_identity".
    """
    if not IDENTITY_INDEX_TABLE:
        logger.error("IDENTITY_INDEX_TABLE env var is not set")
        return None, "unknown_installation"

    try:
        table = _get_table()

        # Step 1: Resolve tenant from installation
        tenant_resp = table.get_item(
            Key={
                "identity_type": "github_installation_id",
                "identity_value": str(installation_id),
            }
        )
        tenant_item = tenant_resp.get("Item")
        if not tenant_item:
            logger.info(
                "Unknown installation_id=%d — no identity-index entry",
                installation_id,
            )
            return None, "unknown_installation"

        org_id = tenant_item["org_id"]
        user_provisioning_mode = tenant_item.get("user_provisioning_mode", "strict")

        # Step 2: Resolve sender
        user_resp = table.get_item(
            Key={
                "identity_type": "github_user",
                "identity_value": str(sender_id),
            }
        )
        user_item = user_resp.get("Item")
        if not user_item:
            logger.info(
                "Unknown sender_id=%d — no identity-index entry", sender_id
            )
            return None, "unknown_user"

        # Step 3: Cross-check org membership
        if user_item["org_id"] != org_id:
            logger.warning(
                "Cross-tenant identity: sender_id=%d belongs to org=%s but "
                "installation_id=%d belongs to org=%s",
                sender_id,
                user_item["org_id"],
                installation_id,
                org_id,
            )
            return None, "cross_tenant_identity"

        return (
            ResolvedIdentity(
                tenant_id=org_id,
                org_id=org_id,
                user_id=user_item["user_id"],
                user_provisioning_mode=user_provisioning_mode,
            ),
            "ok",
        )

    except Exception as e:
        logger.error(
            "Identity resolution failed for installation_id=%d sender_id=%d: %s",
            installation_id,
            sender_id,
            e,
        )
        return None, "unknown_installation"
