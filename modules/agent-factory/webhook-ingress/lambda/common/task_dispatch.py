"""The scheduled recovery sweep, and the alias that authenticates it.

## What this is for

A task can be accepted and durably recorded but not yet published: the process
died between the two, or the SQS send returned ambiguously. Nothing in the
request path will ever notice, because that request is over. This sweep is what
notices. It runs every 60 seconds, asks the gateway for outstanding work in each
shard, publishes what needs publishing, and reports back what it observed.

The client never retries. That is the entire point of T3-AC01: a `202` is a
promise that the task will be dispatched or will fail visibly, and this is the
component that keeps it.

## Why the alias is the authentication and the body is not

This module lives in a Lambda that also serves public HTTP traffic. If "am I a
recovery run?" were answered by reading the event body, then anyone who can
reach the public route could claim to be recovery by sending
`{"adp_task_recovery": true}` -- and recovery can enumerate and lease other
tenants' outstanding work.

So the question is answered from the *invocation context* instead:
``context.invoked_function_arn`` carries the alias the caller actually invoked,
and only the schedule can invoke the ``task-recovery`` alias (its resource policy
names the rule as the sole source; the public API Gateway integration points at a
different qualifier entirely). The body is never consulted. An unqualified ARN --
a direct ``$LATEST`` invoke -- is refused too, because it proves nothing about
who called.

This is defence in depth rather than belt-and-braces: the gateway independently
checks that the caller's IAM role is on the recovery allowlist. Two mechanisms,
two failure modes, neither sufficient alone.

## Why the bounds are hard limits and not tuning

Each invocation handles at most 100 work records and stops after 30 seconds,
and a dispatch gets at most 5 publication tries per 10-minute window. Without
those, one permanently-failing task would consume every invocation forever and
starve every other task in its shard -- a single poison record becoming a
platform-wide outage. With them, a stuck task produces a visible exhausted state
and the sweep moves on.

A sweep that runs out of time is not an error and does not roll anything back: it
leaves the remaining work due, and the next invocation 60 seconds later continues
from there. Progress is incremental and idempotent by construction.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

# Must match the alias name created in infra/task-recovery.tf. A mismatch fails
# closed (every invocation refused), which is the safe direction.
RECOVERY_ALIAS = "task-recovery"

RECOVERY_FLAG = "ADP_TASK_API_RECOVERY_ENABLED"
SHARD_COUNT = 16
SHARD_VERSION = "v1"

# Fixed bounds from contracts/v1/limits.json.
MAX_WORK_RECORDS_PER_INVOCATION = 100
MAX_INVOCATION_SECONDS = 30
PAGE_LIMIT = 25

PROOF_HEADER = "X-Adp-Producer-Proof"
INVOCATION_HEADER = "x-adp-work-invocation"
STS_BODY = "Action=GetCallerIdentity&Version=2011-06-15"


class RecoveryRefusedError(Exception):
    """This invocation is not an authenticated recovery run."""


def invoked_alias(context) -> str | None:
    """The alias actually invoked, from the context. Never from the event.

    ``invoked_function_arn`` is set by the Lambda service from the caller's
    request, not by the payload, so it cannot be spoofed by request content.
    A function ARN has 7 colon-separated parts; a qualified one has 8, and that
    8th part is the alias or version.
    """
    arn = getattr(context, "invoked_function_arn", "") or ""
    parts = arn.split(":")
    if len(parts) < 8:
        # Unqualified ($LATEST): proves nothing about the caller.
        return None
    qualifier = parts[7]
    return qualifier or None


def require_recovery_invocation(context) -> None:
    """Refuse anything that is not an invocation through the recovery alias.

    Deliberately takes no event parameter. A function that cannot see the body
    cannot be tricked by it, and that is easier to verify by reading than any
    amount of careful body validation.
    """
    if invoked_alias(context) != RECOVERY_ALIAS:
        raise RecoveryRefusedError("not invoked through the recovery alias")


def is_recovery_event(event: dict) -> bool:
    """Shape check used only for ROUTING, never for authorization.

    Separate from ``require_recovery_invocation`` on purpose, and named so the
    difference is impossible to miss at a call site: this says "this looks like
    the scheduled event", not "this caller is allowed to run recovery". The
    handler must call both, and the alias check must be the one that decides.
    """
    if not isinstance(event, dict):
        return False
    return event.get("detail-type") == "Scheduled Event" and bool(
        event.get("resources")
    )


def recovery_enabled(env=None) -> bool:
    """Default-off. Anything but an explicit "true" leaves the sweep closed."""
    source = os.environ if env is None else env
    return str(source.get(RECOVERY_FLAG, "")).strip().lower() == "true"


def shards() -> list[str]:
    return [f"{SHARD_VERSION}#{index:02d}" for index in range(SHARD_COUNT)]


def _producer_proof(identity: str) -> str:
    """A SigV4-signed GetCallerIdentity request, bound to `identity`.

    The gateway replays this to STS to learn our role. The identity is inside the
    signed headers, so a captured proof cannot be retargeted at other work.
    """
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
        json.dumps({k.lower(): v for k, v in proof.headers.items()}).encode()
    ).decode()


def _call_gateway(path: str, body: dict, *, identity: str) -> dict | None:
    """One SigV4 call to an internal task-dispatch adapter.

    Returns None on any failure. A caller must treat that as "unknown", never as
    "nothing to do": the difference is whether a task stays discoverable.
    """
    import botocore.auth
    import botocore.awsrequest
    import botocore.session

    endpoint = os.environ.get("ADP_AGENT_CONTROL_ENDPOINT", "").rstrip("/")
    parsed = urllib.parse.urlparse(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        logger.warning("task recovery endpoint is not a plain https URL")
        return None

    payload = dict(body)
    payload["producer_proof"] = _producer_proof(identity)
    data = json.dumps(payload).encode()
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
            """Signed credentials must not follow a redirect to another host."""

            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )
        with opener.open(request, timeout=10) as response:
            if response.status != 200:
                return None
            return json.loads(response.read(1_048_577))
    except urllib.error.HTTPError as error:
        # 409/429 are normal refusals: someone else owns this work, or its try
        # budget is spent. Not alarms, and not retried within this invocation.
        logger.info("task recovery refused path=%s status=%s", path, error.code)
        return None
    except Exception as error:  # noqa: BLE001
        logger.warning(
            "task recovery call failed path=%s error=%s", path, type(error).__name__
        )
        return None


def _publish_claimed(work: dict, claimed: dict) -> dict:
    """Publish one claimed dispatch and settle whatever actually happened.

    The envelope is whatever the gateway committed at acceptance. This function
    does not build, amend or re-sign it -- a courier, not an author.
    """
    from common import task_publisher

    envelope = claimed.get("envelope")
    tenant_id = claimed.get("tenant_id", "")
    queue_url = os.environ.get("SUBMIT_QUEUE_URL", "")
    if not envelope or not tenant_id:
        # Authority preparation failed. Report it rather than guessing at a
        # replacement, so the task gets a discoverable outcome (T3-AC05).
        return {"publication_outcome": "failed", "sqs_message_id": None}
    try:
        return task_publisher.publish_task_envelope(
            envelope, tenant_id=tenant_id, queue_url=queue_url
        )
    except task_publisher.TaskPublicationError as error:
        return {"publication_outcome": error.outcome, "sqs_message_id": None}


def sweep(context, *, env=None, clock=time.monotonic) -> dict:
    """One bounded recovery pass across all shards.

    Authentication first, before any work is read: an unauthenticated invocation
    must not learn that outstanding work exists, let alone which tasks.
    """
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
                # Out of budget, not out of work. The remaining records stay due
                # and the next invocation continues; nothing is rolled back.
                truncated = True
                break
            page = _call_gateway(
                "/task-dispatch/recovery/claim",
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

    # Logged rather than silent: "we stopped early" must be visible, or a
    # permanently-truncated sweep looks exactly like a healthy idle one.
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


def _recover_one(work: dict) -> str:
    """Claim, publish and settle a single work record.

    Every exit is an explicit outcome. There is no path that drops a record
    without recording why, because that is precisely how a task disappears.
    """
    kind = work.get("kind")
    task_id = work.get("task_id", "")
    dispatch_id = work.get("dispatch_id") or ""
    if kind != "dispatch" or not task_id or not dispatch_id:
        # Other work kinds (execution, queue_ack, cleanup) are owned by later
        # stories. Reporting them as skipped keeps them due rather than
        # pretending this sweep handled them.
        return f"skipped_{kind or 'unknown'}"

    claimed = _call_gateway(
        "/task-dispatch/claim",
        {"schema_version": "1.0", "task_id": task_id, "dispatch_id": dispatch_id},
        identity=dispatch_id,
    )
    if claimed is None:
        return "claim_refused"

    result = _publish_claimed(work, claimed)
    settled = _call_gateway(
        "/task-dispatch/settle",
        {
            "schema_version": "1.0",
            "task_id": task_id,
            "dispatch_id": dispatch_id,
            "lease_token": claimed.get("lease_token", ""),
            "publication_outcome": result["publication_outcome"],
            "sqs_message_id": result.get("sqs_message_id"),
        },
        identity=dispatch_id,
    )
    if settled is None:
        # Published (or maybe published) but the settlement did not land. The
        # record stays leased until its lease expires, then becomes due again.
        # Re-publishing under the same dispatch ID is deduplicated by FIFO, so
        # this is safe rather than a second execution.
        return "settle_unconfirmed"
    return result["publication_outcome"]
