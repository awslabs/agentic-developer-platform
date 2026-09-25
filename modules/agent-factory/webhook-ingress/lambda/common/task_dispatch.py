"""Direct and scheduled publication of durable task work."""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime

logger = logging.getLogger(__name__)
RECOVERY_ALIAS = "task-recovery"
RECOVERY_FLAG = "ADP_TASK_API_RECOVERY_ENABLED"
SHARD_COUNT = 16
SHARD_VERSION = "v1"
MAX_WORK_RECORDS_PER_INVOCATION = 100
MAX_INVOCATION_SECONDS = 30
PAGE_LIMIT = 25
PROOF_HEADER = "X-Adp-Producer-Proof"
INVOCATION_HEADER = "x-adp-work-invocation"
STS_BODY = "Action=GetCallerIdentity&Version=2011-06-15"


class RecoveryRefusedError(Exception):
    pass


def invoked_alias(context) -> str | None:
    arn = getattr(context, "invoked_function_arn", "") or ""
    parts = arn.split(":")
    return parts[7] if len(parts) >= 8 and parts[7] else None


def require_recovery_invocation(context) -> None:
    if invoked_alias(context) != RECOVERY_ALIAS:
        raise RecoveryRefusedError("not invoked through the recovery alias")


def is_recovery_event(event: dict) -> bool:
    return (
        isinstance(event, dict)
        and event.get("source") == "aws.events"
        and event.get("detail-type") == "Scheduled Event"
        and bool(event.get("resources"))
    )


def handle_recovery_event(
    event: dict, context, *, env=None, clock=time.monotonic
) -> dict:
    """Route only an alias-authenticated scheduled invocation to the sweep."""
    require_recovery_invocation(context)
    if not is_recovery_event(event):
        raise RecoveryRefusedError("invalid recovery event")
    return sweep(context, env=env, clock=clock)


def recovery_enabled(env=None) -> bool:
    source = os.environ if env is None else env
    return str(source.get(RECOVERY_FLAG, "")).strip().lower() == "true"


def shards() -> list[str]:
    return [f"{SHARD_VERSION}#{index:02d}" for index in range(SHARD_COUNT)]


def _producer_proof(identity: str) -> str:
    import botocore.auth
    import botocore.awsrequest
    import botocore.session

    credentials = botocore.session.get_session().get_credentials()
    if credentials is None:
        raise RecoveryRefusedError("no credentials to prove identity with")
    region = os.environ.get("AWS_REGION", "us-east-1")
    proof = botocore.awsrequest.AWSRequest(
        method="POST",
        url=f"https://sts.{region}.amazonaws.com/",
        data=STS_BODY,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            INVOCATION_HEADER: identity,
        },
    )
    botocore.auth.SigV4Auth(
        credentials.get_frozen_credentials(), "sts", region
    ).add_auth(proof)
    return base64.b64encode(
        json.dumps(
            {key.lower(): value for key, value in proof.headers.items()}
        ).encode()
    ).decode()


def _gateway_base() -> str:
    endpoint = os.environ.get("ADP_TASK_GATEWAY_ENDPOINT", "").rstrip("/")
    if not endpoint:
        endpoint = os.environ.get("ADP_AGENT_CONTROL_ENDPOINT", "").rstrip("/")
        suffix = "/internal/v1/agent"
        if endpoint.endswith(suffix):
            endpoint = endpoint[: -len(suffix)]
    return endpoint


def _call_gateway(path: str, body: dict, *, identity: str) -> dict | None:
    """Call one exact internal adapter; failures remain recoverable unknowns."""
    import botocore.auth
    import botocore.awsrequest
    import botocore.session

    endpoint = _gateway_base()
    parsed = urllib.parse.urlparse(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        logger.warning("task gateway endpoint is not a plain https URL")
        return None
    payload = {**body, "producer_proof": _producer_proof(identity)}
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    url = endpoint + path
    region = os.environ.get("AWS_REGION", "us-east-1")
    try:
        credentials = botocore.session.get_session().get_credentials()
        if credentials is None:
            return None
        signed = botocore.awsrequest.AWSRequest(
            method="POST",
            url=url,
            data=data,
            headers={
                "Content-Type": "application/json",
                PROOF_HEADER: payload["producer_proof"],
            },
        )
        botocore.auth.SigV4Auth(
            credentials.get_frozen_credentials(), "execute-api", region
        ).add_auth(signed)
        request = urllib.request.Request(
            url, data=data, headers=dict(signed.headers), method="POST"
        )

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )
        with opener.open(request, timeout=10) as response:
            if response.status != 200:
                return None
            result = json.loads(response.read(1_048_577))
            return result if isinstance(result, dict) else None
    except urllib.error.HTTPError as error:
        logger.info("task adapter refused path=%s status=%s", path, error.code)
        return None
    except Exception as error:  # noqa: BLE001
        logger.warning(
            "task adapter call failed path=%s error=%s", path, type(error).__name__
        )
        return None


def _valid_claim(claimed: dict, dispatch_id: str) -> bool:
    from common import task_publisher

    if set(claimed) != {
        "schema_version",
        "envelope",
        "lease_token",
        "lease_expires_at",
    }:
        return False
    envelope = claimed.get("envelope")
    if not isinstance(envelope, dict):
        return False
    if (
        claimed.get("schema_version") != "1.0"
        or not claimed.get("lease_token")
        or not claimed.get("lease_expires_at")
    ):
        return False
    try:
        task_publisher.validate_envelope(envelope)
    except task_publisher.TaskPublicationError:
        return False
    return envelope.get("dispatch_id") == dispatch_id


def publish_dispatch(dispatch_id: str) -> dict:
    """Claim, publish, and settle one stable dispatch ID."""
    from common import task_publisher

    claimed = _call_gateway(
        "/internal/v1/tasks/dispatch/claim",
        {"schema_version": "1.0", "dispatch_id": dispatch_id},
        identity=dispatch_id,
    )
    if claimed is None:
        return {"publication_outcome": "claim_refused", "settled": False}
    if not _valid_claim(claimed, dispatch_id):
        return {"publication_outcome": "invalid_claim", "settled": False}
    try:
        result = task_publisher.publish_task_envelope(
            claimed["envelope"], queue_url=os.environ.get("SUBMIT_QUEUE_URL", "")
        )
    except task_publisher.TaskPublicationError as error:
        result = {"publication_outcome": error.outcome, "sqs_message_id": None}
    settled = _call_gateway(
        "/internal/v1/tasks/dispatch/settle",
        {
            "schema_version": "1.0",
            "dispatch_id": dispatch_id,
            "lease_token": claimed["lease_token"],
            "publication_outcome": result["publication_outcome"],
            "sqs_message_id": result.get("sqs_message_id"),
        },
        identity=dispatch_id,
    )
    return {**result, "settled": settled is not None, "settlement": settled}


def _settle_recovery(work: dict, *, observed: bool) -> bool:
    kind = work.get("kind")
    if not isinstance(kind, str):
        return False
    evidence_kind = {
        "dispatch": "publication",
        "execution": "workload_termination",
        "queue_ack": "queue_ack",
        "cleanup": "retention",
    }.get(kind)
    if evidence_kind is None:
        return False
    result = _call_gateway(
        "/internal/v1/tasks/recovery/settle",
        {
            "schema_version": "1.0",
            "work_id": work.get("work_id", ""),
            "lease_token": work.get("lease_token", ""),
            "evidence": {
                "kind": evidence_kind,
                "observed": observed,
                "observed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        },
        identity=work.get("work_id", ""),
    )
    return result is not None


def _recover_one(work: dict) -> str:
    work_id, kind = work.get("work_id", ""), work.get("kind")
    if not work_id or not work.get("lease_token"):
        return "invalid_work"
    if kind != "dispatch":
        _settle_recovery(work, observed=False)
        return f"pending_{kind or 'unknown'}"
    result = publish_dispatch(work_id)
    confirmed = (
        result.get("publication_outcome") == "confirmed"
        and result.get("settled") is True
    )
    # A refused publication claim may mean a competing publisher already
    # committed evidence. Presenting observed=true cannot fabricate success:
    # the gateway confirms only against its own durable publication record.
    settlement_observation = (
        confirmed or result.get("publication_outcome") == "claim_refused"
    )
    recovery_settled = _settle_recovery(work, observed=settlement_observation)
    if not recovery_settled:
        return "recovery_settle_unconfirmed"
    return result["publication_outcome"]


def sweep(context, *, env=None, clock=time.monotonic) -> dict:
    require_recovery_invocation(context)
    if not recovery_enabled(env):
        return {"status": "disabled", "processed": 0}
    deadline = clock() + MAX_INVOCATION_SECONDS
    processed = 0
    outcomes: dict[str, int] = {}
    truncated = False
    for shard in shards():
        cursor = None
        while True:
            if processed >= MAX_WORK_RECORDS_PER_INVOCATION or clock() >= deadline:
                truncated = True
                break
            page = _call_gateway(
                "/internal/v1/tasks/recovery/claim",
                {
                    "schema_version": "1.0",
                    "shard": shard,
                    "cursor": cursor,
                    "limit": PAGE_LIMIT,
                },
                identity=shard,
            )
            if page is None:
                break
            for work in page.get("work", []):
                if processed >= MAX_WORK_RECORDS_PER_INVOCATION or clock() >= deadline:
                    truncated = True
                    break
                outcome = _recover_one(work)
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
                processed += 1
            cursor = page.get("next_cursor")
            if not cursor:
                break
        if truncated:
            break
    logger.info(
        "task recovery sweep processed=%s truncated=%s outcomes=%s",
        processed,
        truncated,
        outcomes,
    )
    return {
        "status": "ok",
        "processed": processed,
        "truncated": truncated,
        "outcomes": outcomes,
    }
