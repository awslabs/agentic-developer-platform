"""Provenance client for worker pods.

Posts action provenance records to the gateway's /internal/v1/provenance endpoint
after successful outbound GitHub actions. Fail-soft: never crashes the worker.

Phase 2-d of EPIC #779.

Issue #575 / #1103: Supports two transport modes based on environment:
  - SigV4 via API Gateway (when ADP_GATEWAY_ENDPOINT is set) — IRSA-based, no shared secret
  - Shared-secret via direct URL (when VAULT_GATEWAY_URL + VAULT_INTERNAL_API_KEY are set) — legacy
"""

from __future__ import annotations

import json
import logging
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


def _sigv4_sign_request(method: str, url: str, headers: dict, data: bytes | None) -> dict:
    """Sign a request with SigV4 using pod IRSA credentials. Returns signed headers."""
    import botocore.auth
    import botocore.awsrequest
    import botocore.session

    session = botocore.session.get_session()
    credentials = session.get_credentials()
    if credentials is None:
        raise RuntimeError("No AWS credentials available for SigV4 signing")
    credentials = credentials.get_frozen_credentials()

    aws_request = botocore.awsrequest.AWSRequest(
        method=method,
        url=url,
        headers=headers,
        data=data,
    )

    region = os.environ.get("AWS_REGION", "us-east-1")
    signer = botocore.auth.SigV4Auth(credentials, "execute-api", region)
    signer.add_auth(aws_request)

    return dict(aws_request.headers)


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
    parent_invocation_id: str | None = None,
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
        parent_invocation_id: The upstream run's message_id (nullable).

    Returns:
        The provenance_id from the gateway response, or None on failure.
    """
    gateway_endpoint = os.environ.get("ADP_GATEWAY_ENDPOINT", "").rstrip("/")
    gateway_url = os.environ.get("VAULT_GATEWAY_URL", "").rstrip("/")
    api_key = os.environ.get("VAULT_INTERNAL_API_KEY", "")

    # Determine mode: SigV4 (preferred) or legacy shared-secret
    use_sigv4 = bool(gateway_endpoint)

    if use_sigv4:
        base_url = gateway_endpoint + "/agent"
    elif gateway_url and api_key:
        base_url = gateway_url
    else:
        logger.debug(
            "Neither ADP_GATEWAY_ENDPOINT nor VAULT_GATEWAY_URL+API_KEY configured; "
            "skipping provenance post"
        )
        return None

    endpoint = f"{base_url}/internal/v1/provenance"
    payload = {
        "actor_user_id": actor_user_id,
        "triggered_by": triggered_by,
        "root_human_id": root_human_id,
        "is_human_rooted": is_human_rooted,
        "action_kind": action_kind,
        "source_event": source_event,
        "correlation_id": correlation_id,
        "org_id": org_id,
        "parent_invocation_id": parent_invocation_id,
    }

    headers = {"Content-Type": "application/json"}
    data = json.dumps(payload).encode("utf-8")

    try:
        if use_sigv4:
            headers = _sigv4_sign_request("POST", endpoint, headers, data)
        else:
            headers["X-Internal-Api-Key"] = api_key

        req = Request(endpoint, data=data, headers=headers, method="POST")
        with urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            provenance_id = result.get("provenance_id")
            logger.info("Posted provenance: id=%s corr=%s", provenance_id, correlation_id)
            return provenance_id
    except (HTTPError, URLError, Exception) as exc:
        logger.warning("Failed to post provenance (non-fatal): %s", exc)
        return None
