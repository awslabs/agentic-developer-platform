"""Provenance client for worker pods.

Posts action provenance records to the gateway's /internal/v1/provenance endpoint
after successful outbound GitHub actions. Fail-soft: never crashes the worker.

Phase 2-d of EPIC #779.
"""

from __future__ import annotations

import json
import logging
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


def post_provenance(
    *,
    actor_user_id: str,
    triggered_by: str | None,
    root_human_id: str,
    is_human_rooted: bool,
    action_kind: str,
    source_event: str,
    correlation_id: str,
    org_id: str | None = None,
) -> str | None:
    """Post an action provenance record to the gateway. Fail-soft.

    Args:
        actor_user_id: User ID of the acting agent/user.
        triggered_by: ID of the provenance record that caused this action (nullable).
        root_human_id: The originating human's user ID.
        is_human_rooted: Whether the chain traces back to a human action.
        action_kind: Type of action (e.g. "pr_create", "comment_post").
        source_event: Source event description (e.g. "worker:entrypoint").
        correlation_id: Correlation ID for this action chain.
        org_id: Organization ID (optional).

    Returns:
        The provenance_id from the gateway response, or None on failure.
    """
    gateway_url = os.environ.get("VAULT_GATEWAY_URL", "").rstrip("/")
    api_key = os.environ.get("VAULT_INTERNAL_API_KEY", "")

    if not gateway_url or not api_key:
        logger.debug("Gateway URL or API key not configured; skipping provenance post")
        return None

    endpoint = f"{gateway_url}/internal/v1/provenance"
    payload = {
        "actor_user_id": actor_user_id,
        "triggered_by": triggered_by,
        "root_human_id": root_human_id,
        "is_human_rooted": is_human_rooted,
        "action_kind": action_kind,
        "source_event": source_event,
        "correlation_id": correlation_id,
        "org_id": org_id,
    }

    headers = {
        "X-Internal-Api-Key": api_key,
        "Content-Type": "application/json",
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = Request(endpoint, data=data, headers=headers, method="POST")
        with urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            provenance_id = result.get("provenance_id")
            logger.info("Posted provenance: id=%s corr=%s", provenance_id, correlation_id)
            return provenance_id
    except (HTTPError, URLError, Exception) as exc:
        logger.warning("Failed to post provenance (non-fatal): %s", exc)
        return None
