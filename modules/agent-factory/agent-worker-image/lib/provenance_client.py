"""Provenance client for worker pods.

Posts action provenance records to the gateway's /internal/v1/provenance endpoint
after successful outbound GitHub actions. Fail-soft: never crashes the worker.

Phase 2-d of EPIC #779.

Issue #575 / #1103: Supports two transport modes based on environment:
  - SigV4 via API Gateway (when ADP_GATEWAY_ENDPOINT is set) — IRSA-based, no shared secret
  - Shared-secret via direct URL (when VAULT_GATEWAY_URL + VAULT_INTERNAL_API_KEY are set) — legacy

Issue #4029: every write from this client used to 422. The payload disagreed with
the gateway's CreateProvenanceRequest on two independent fields (source_event was
sent as a str where a dict is required; org_id was omitted where a non-null str is
required) and the success path read a response key the gateway never returns
('provenance_id' vs 'id'). The payload shape is now pinned by a golden fixture
shared with the gateway and TypeScript suites — see
contracts/provenance/v1/create-provenance-request.golden.json. Do not change the
payload here without updating that fixture; the gateway test validates against it.
"""

from __future__ import annotations

import json
import logging
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request

from lib.authenticated_http import open_authenticated as urlopen

logger = logging.getLogger(__name__)

# Issue #4029: a 100% provenance-write failure rate hid behind logger.warning for
# months. Every fail-soft exit path now also emits this metric so the failure is
# visible in CloudWatch without anyone reading pod logs.
METRIC_NAMESPACE = "ADP/Provenance"
METRIC_WRITE_FAILED = "ProvenanceWriteFailed"
METRIC_WRITE_OK = "ProvenanceWriteSucceeded"


def _sigv4_sign_request(method: str, url: str, headers: dict, data: bytes | None) -> dict:
    """Sign a request with SigV4 using pod IRSA credentials. Returns signed headers."""
    import botocore.auth
    import botocore.awsrequest
    import botocore.session

    session = botocore.session.get_session()
    from adp_trigger.transport_identity import gateway_signing_region, worker_credentials

    credentials = worker_credentials(session)
    if credentials is None:
        raise RuntimeError("No AWS credentials available for SigV4 signing")
    credentials = credentials.get_frozen_credentials()

    aws_request = botocore.awsrequest.AWSRequest(
        method=method,
        url=url,
        headers=headers,
        data=data,
    )

    region = gateway_signing_region(url)
    signer = botocore.auth.SigV4Auth(credentials, "execute-api", region)
    signer.add_auth(aws_request)

    return dict(aws_request.headers)


def _emit_metric(metric_name: str, reason: str) -> None:
    """Emit a CloudWatch metric for a provenance write outcome. Never raises.

    Issue #4029: the observability control, not a nicety. The payload bug was
    survivable for months precisely because the only signal was a log line nobody
    alarmed on. Dimensioned by reason so a schema regression (http_422) is
    distinguishable from an outage (urlerror) on the graph.
    """
    try:
        import boto3

        cw = boto3.client("cloudwatch", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        cw.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[
                {
                    "MetricName": metric_name,
                    "Dimensions": [
                        {"Name": "Producer", "Value": "worker"},
                        {"Name": "Reason", "Value": reason},
                    ],
                    "Value": 1,
                    "Unit": "Count",
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must never break the worker
        logger.debug("Failed to emit %s metric: %s", metric_name, exc)


def build_provenance_payload(
    *,
    actor_user_id: str,
    triggered_by: str | None,
    root_human_id: str,
    is_human_rooted: bool,
    action_kind: str,
    source_event: dict,
    correlation_id: str,
    org_id: str,
    parent_invocation_id: str | None = None,
) -> dict:
    """Build the request body for POST /internal/v1/provenance.

    Issue #4029: split out as a pure function so the wire contract is testable
    without mocking HTTP. This is the artifact the shared golden fixture pins
    (contracts/provenance/v1/create-provenance-request.golden.json) and the
    gateway's CreateProvenanceRequest validates — the two sides can no longer
    drift without a test failing.

    Keys and types must match src/internal/provenance_routes.py:CreateProvenanceRequest
    exactly: source_event is a dict (JSONB column), org_id is a non-null str
    (VARCHAR(255) NOT NULL column).
    """
    return {
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


def post_provenance(
    *,
    actor_user_id: str,
    triggered_by: str | None,
    root_human_id: str,
    is_human_rooted: bool,
    action_kind: str,
    source_event: dict,
    correlation_id: str,
    org_id: str,
    parent_invocation_id: str | None = None,
) -> str | None:
    """Post an action provenance record to the gateway. Fail-soft.

    Args:
        actor_user_id: User ID of the acting agent/user.
        triggered_by: ID of the provenance record that caused this action (nullable).
        root_human_id: The originating human's user ID.
        is_human_rooted: Whether the chain traces back to a human action.
        action_kind: Type of action (e.g. "pr_create", "comment_post").
        source_event: Structured source-event object, e.g.
            {"source": "worker:entrypoint", "event_type": ..., "repo": ...}.
            Must be a dict — the column is JSONB and the gateway rejects a str.
        correlation_id: Correlation ID for this action chain.
        org_id: Tenant the action is attributed to. Required and non-null (the
            column is NOT NULL). Must come from the run's server-resolved tenant
            (ADP_TENANT_ID), never from caller-supplied or guessed values.
        parent_invocation_id: The upstream run's message_id (nullable).

    Returns:
        The new provenance row's id from the gateway response, or None on failure.
    """
    gateway_endpoint = os.environ.get("ADP_GATEWAY_ENDPOINT", "").rstrip("/")
    gateway_url = os.environ.get("VAULT_GATEWAY_URL", "").rstrip("/")
    api_key = os.environ.get("VAULT_INTERNAL_API_KEY", "")

    # Determine mode: SigV4 (preferred) or legacy shared-secret
    use_sigv4 = bool(gateway_endpoint)

    if use_sigv4:
        # Internal APIs use /internal/{proxy+}. Adding /agent routes them to
        # the edge ALB, which deliberately denies /internal/* (#5136, #4010).
        base_url = gateway_endpoint
    elif gateway_url and api_key:
        base_url = gateway_url
    else:
        logger.debug(
            "Neither ADP_GATEWAY_ENDPOINT nor VAULT_GATEWAY_URL+API_KEY configured; "
            "skipping provenance post"
        )
        return None

    endpoint = f"{base_url}/internal/v1/provenance"
    payload = build_provenance_payload(
        actor_user_id=actor_user_id,
        triggered_by=triggered_by,
        root_human_id=root_human_id,
        is_human_rooted=is_human_rooted,
        action_kind=action_kind,
        source_event=source_event,
        correlation_id=correlation_id,
        org_id=org_id,
        parent_invocation_id=parent_invocation_id,
    )

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
            # The gateway returns CreateProvenanceResponse{id, created_at} — NOT
            # "provenance_id" (issue #4029: the old key silently yielded None on
            # every success, so callers could not tell success from failure).
            provenance_id = result.get("id")
            if not provenance_id:
                logger.warning("Provenance response missing 'id' (non-fatal): keys=%s", sorted(result))
                _emit_metric(METRIC_WRITE_FAILED, "missing_id_in_response")
                return None
            logger.info("Posted provenance: id=%s corr=%s", provenance_id, correlation_id)
            _emit_metric(METRIC_WRITE_OK, "ok")
            return provenance_id
    except HTTPError as exc:
        # Dimension on the status code: a 4xx here means the two sides disagree
        # about the contract (the #4029 class), a 5xx means the gateway is unwell.
        logger.warning("Failed to post provenance (non-fatal): HTTP %s %s", exc.code, exc.reason)
        _emit_metric(METRIC_WRITE_FAILED, f"http_{exc.code}")
        return None
    except URLError as exc:
        logger.warning("Failed to post provenance (non-fatal): %s", exc)
        _emit_metric(METRIC_WRITE_FAILED, "urlerror")
        return None
    except Exception as exc:  # noqa: BLE001 — fail-soft by design: telemetry must not kill a run
        logger.warning("Failed to post provenance (non-fatal): %s", exc)
        _emit_metric(METRIC_WRITE_FAILED, "exception")
        return None
