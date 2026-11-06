"""Retired shared-IAM trigger and lineage lookups reused by GitHub ingress.

Agent dispatch must use the gateway's authenticated per-run control endpoint.
A shared IAM role and observable lineage identifiers cannot prove that a caller
owns a run. Keep the old URL as an unconditional tombstone, including when
protected authority is disabled, so configuration changes cannot reopen it.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)
_GSI_RETRY_DELAY_S = 0.2
_GSI_MAX_ATTEMPTS = 2


def handle_agent_trigger(event: dict, context) -> dict:
    """Reject legacy requests before reading lineage, credentials, or queues."""
    return _response(
        410,
        {
            "error": "legacy_agent_trigger_retired",
            "detail": "Use the gateway authenticated per-run dispatch endpoint. Shared-IAM agent dispatch is no longer supported.",
        },
    )


def _query_chain(correlation_id: str, limit: int) -> list[dict[str, Any]]:
    """Query the webhook-events correlation-index GSI for a chain's rows.

    Issue #4128: factored out of :func:`_resolve_chain` so the parent-invocation
    check reuses the SAME query — the reuse table in the issue is explicit that
    there must not be a second GSI query implementation to drift from this one.

    Uses mandatory single retry with 200ms backoff to handle GSI eventual
    consistency (the parent may have just written the row).

    Returns the most recent rows first, or an empty list when the chain is
    unknown/expired or the table is unconfigured.
    """
    import boto3
    from boto3.dynamodb.conditions import Key

    table_name = os.environ.get("EVENTS_TABLE", "")
    if not table_name:
        logger.error("agent_trigger: EVENTS_TABLE not configured")
        return []

    region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    dynamodb = boto3.resource("dynamodb", region_name=region)
    table = dynamodb.Table(table_name)

    for attempt in range(_GSI_MAX_ATTEMPTS):
        try:
            resp = table.query(
                IndexName="correlation-index",
                KeyConditionExpression=Key("correlation_id").eq(correlation_id),
                ScanIndexForward=False,  # Most recent first
                Limit=limit,
            )
            items = resp.get("Items", [])
            if items:
                return items
        except Exception as e:
            logger.warning(
                "agent_trigger: GSI query failed attempt=%d error=%s",
                attempt + 1,
                e,
            )

        # Retry with backoff (only if not last attempt)
        if attempt < _GSI_MAX_ATTEMPTS - 1:
            time.sleep(_GSI_RETRY_DELAY_S)  # nosemgrep: arbitrary-sleep

    return []


def _resolve_chain(correlation_id: str) -> dict[str, Any] | None:
    """Return the most recent event row for this correlation_id, or None.

    None means the chain is unknown/expired — the caller MUST NOT mint a new
    root from it.
    """
    items = _query_chain(correlation_id, limit=1)
    return items[0] if items else None


def _repo_in_tenant(
    target_repo: str,
    chain_tenant_id: str,
    chain_record: dict[str, Any],
) -> bool:
    """Whether ``target_repo`` belongs to ``chain_tenant_id`` (Issue #4128).

    Accepts on the first of three signals to match, cheapest first:

      1. The target repo IS the chain's own repo (same-repo dispatch, the
         overwhelmingly common case — no lookup needed).
      2. The repo owner equals the chain's tenant id. Tenants are keyed by org
         login throughout the identity index, so this is the normal match for a
         sibling repo in the same org.
      3. ``resolve_installation_for_tenant(owner)`` — reused from
         ``common/installation_resolver.py`` per the issue's reuse table —
         resolves the owner to the SAME installation as the chain's tenant. This
         covers a tenant id that is not literally the org login.

    Fail-closed: an owner we cannot tie to the chain's tenant is rejected.
    """
    if not target_repo or "/" not in target_repo:
        return False

    chain_repo = chain_record.get("repo")
    if chain_repo and target_repo == chain_repo:
        return True

    owner = target_repo.split("/", 1)[0]
    if owner == chain_tenant_id:
        return True

    try:
        from common.installation_resolver import resolve_installation_for_tenant

        owner_installation = resolve_installation_for_tenant(owner)
        if owner_installation is None:
            return False
        tenant_installation = resolve_installation_for_tenant(chain_tenant_id)
        return tenant_installation is not None and owner_installation == tenant_installation
    except Exception as e:  # noqa: BLE001 — an unresolvable owner is a rejection
        logger.warning(
            "agent_trigger: repo-tenant resolution failed for repo=%s tenant=%s: "
            "%s — failing closed",
            target_repo,
            chain_tenant_id,
            e,
        )
        return False


def _response(status_code: int, body: dict) -> dict:
    """Build API Gateway response."""
    return {
        "statusCode": status_code,
        "body": json.dumps(body),
        "headers": {"Content-Type": "application/json"},
    }
