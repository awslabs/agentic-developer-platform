"""Service identity resolution for machine/root-triggered events.

Issue #2154: Resolves service accounts (EventBridge rules, CI pipelines, CloudWatch
alarms) to tenant + org via the identity-index DynamoDB table.

Service identity rows use:
  PK: identity_type = "service_account"
  SK: identity_value = "<service_identity>" (e.g. "eventbridge:adp-dev-high-error-rate")

Each row contains:
  - tenant_id: the ADP tenant this service is authorized for
  - org_id: the org this service belongs to
  - allowed_personas: optional list restricting which personas this service can spawn
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import boto3

logger = logging.getLogger(__name__)

IDENTITY_INDEX_TABLE = os.environ.get("IDENTITY_INDEX_TABLE", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")

_dynamodb = None


@dataclass
class ServiceIdentityResult:
    """Result of a service identity lookup."""

    tenant_id: str
    org_id: str
    service_identity: str
    allowed_personas: list[str] = field(default_factory=list)


def _get_table():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=REGION)
    return _dynamodb.Table(IDENTITY_INDEX_TABLE)


def resolve_service_identity(
    service_identity: str,
) -> tuple[ServiceIdentityResult | None, str]:
    """Resolve a service identity string to tenant + org.

    Args:
        service_identity: The service identity to look up (e.g.
            "eventbridge:adp-dev-high-error-rate", "ci:deploy-pipeline").

    Returns:
        Tuple of (ServiceIdentityResult or None, outcome_reason).
        outcome_reason is one of: "ok", "unknown_service_identity", "table_error".
    """
    if not IDENTITY_INDEX_TABLE:
        logger.error("IDENTITY_INDEX_TABLE not set — cannot resolve service identity")
        return None, "table_error"

    try:
        table = _get_table()
        resp = table.get_item(
            Key={
                "identity_type": "service_account",
                "identity_value": service_identity,
            }
        )
        item = resp.get("Item")
        if not item:
            logger.info(
                "Unknown service_identity=%r — no identity-index entry",
                service_identity,
            )
            return None, "unknown_service_identity"

        # allowed_personas is stored as a DynamoDB list (L) or string set (SS)
        raw_allowed = item.get("allowed_personas")
        if isinstance(raw_allowed, set):
            allowed_personas = list(raw_allowed)
        elif isinstance(raw_allowed, list):
            allowed_personas = raw_allowed
        else:
            allowed_personas = []

        return (
            ServiceIdentityResult(
                tenant_id=item["tenant_id"],
                org_id=item["org_id"],
                service_identity=service_identity,
                allowed_personas=allowed_personas,
            ),
            "ok",
        )

    except Exception as e:
        logger.error(
            "Service identity resolution failed for %r: %s",
            service_identity,
            e,
        )
        return None, "table_error"
