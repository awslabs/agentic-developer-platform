"""Durable delivery completion for legacy worker paths (#5165).

A merged PR identifies neither a completed AIDLC workflow nor a completed queue
message. Keep a receipt on the exact ingress row instead. Its 30-day retention
exceeds SQS's maximum 14-day retention, and ordinary status updates never remove
the receipt. It records terminal message handling, including reported failures;
an explicitly retryable exit must not write it.

Use conditional writes under the existing UpdateItem permission without
returning row contents. No table-wide GetItem grant (which would expose control
credentials) is needed. Conditional scope checks prevent creating a row or
modifying another tenant/repository/persona's row. This is delivery bookkeeping,
not authority:
authority-enabled workers use protected dispatch and must never use this path.

SQS FIFO and its existing visibility heartbeat still coordinate active workers.
The receipt prevents re-execution after completion, not exactly-once execution
of GitHub side effects across a crash before completion was durably recorded.
"""

from __future__ import annotations

import os

from botocore.exceptions import BotoCoreError, ClientError

from lib.invocation_status import _get_client
from lib.status_gateway_client import authority_enabled


#: A run an operator deliberately stopped is finished, and finished differently
#: from `complete` — Issue #3963 (S4).
#:
#: It is read here as terminal for the same reason `complete` is: redelivering it
#: would launch the work again. The abort path's acknowledgement is bounded and can
#: legitimately end unconfirmed (an SQS delete that never succeeded), which is
#: precisely when this row is redelivered — so this is the guard that makes the
#: honest "unconfirmed" outcome safe rather than a way to resurrect an aborted run.
#:
#: Distinct from `complete` in the condition rather than folded in with it, because
#: the two are different facts about the run and a future reader of this expression
#: must be able to see that an abort was handled deliberately.
#:
#: Must match `ABORTED_STATUS` in the gateway's `src/activity/liveness.py` and the
#: `aborted` member of `ALLOWED_WRITE_STATUSES` in `lib/invocation_status.py`.
ABORTED_STATUS = "aborted"


class InvocationCompletionError(Exception):
    """Completion could not be established; preserve the message for retry."""


def _request(envelope: dict) -> dict:
    if authority_enabled():
        raise InvocationCompletionError(
            "protected dispatch must not use direct completion receipts"
        )
    table = os.environ.get("WEBHOOK_EVENTS_TABLE", "")
    event_id = envelope.get("message_id")
    arrived_at = envelope.get("arrived_at")
    tenant = envelope.get("tenant_id")
    persona = envelope.get("persona")
    repo = (envelope.get("source_ref") or {}).get("repo")
    if not all(
        isinstance(value, str) and value
        for value in (table, event_id, arrived_at, tenant, persona, repo)
    ):
        raise InvocationCompletionError(
            "completion receipt requires a table and complete invocation identity"
        )
    return {
        "TableName": table,
        "Key": {"event_id": {"S": event_id}, "arrived_at": {"S": arrived_at}},
        "ConditionExpression": (
            "attribute_exists(event_id) AND attribute_exists(arrived_at) "
            "AND #tenant = :tenant AND #repo = :repo AND #persona = :persona"
        ),
        "ExpressionAttributeNames": {
            "#tenant": "tenant_id",
            "#repo": "repo",
            "#persona": "persona",
            "#done": "delivery_completed",
        },
        "ExpressionAttributeValues": {
            ":tenant": {"S": tenant},
            ":repo": {"S": repo},
            ":persona": {"S": persona},
        },
    }


def is_delivery_completed(envelope: dict) -> bool:
    """Confirm completion, or confirm that the exact row is still unfinished.

    The first conditional write also promotes an older worker's complete status
    to a durable receipt. The second only succeeds on unfinished rows. If another
    worker completes between the two writes, the second fails and the caller
    retries the check on redelivery instead of launching duplicate work.
    Neither write reads row contents or changes its dashboard status.

    A row whose status is ``aborted`` counts as completed (#3963). An operator
    stopped that run on purpose, so redelivering it would restart exactly the work
    the abort existed to prevent — and this is the path that actually gets
    exercised, because the abort finalizer's queue acknowledgement is bounded and
    may legitimately end unconfirmed. The abort's own terminal write lands before
    its acknowledgement is attempted, so by the time a redelivery can happen the
    status is already there for this guard to read.
    """
    request = _request(envelope)
    scope = request["ConditionExpression"]
    request["ExpressionAttributeNames"]["#legacy_done"] = "aidlc_delivery_completed"
    request["ExpressionAttributeNames"]["#status"] = "status"
    request["ExpressionAttributeValues"].update(
        {
            ":true": {"BOOL": True},
            ":complete": {"S": "complete"},
            ":aborted": {"S": ABORTED_STATUS},
        }
    )
    request["UpdateExpression"] = "SET #done = :true"
    request["ConditionExpression"] = (
        scope
        + " AND (#done = :true OR #legacy_done = :true OR #status = :complete"
        " OR #status = :aborted)"
    )
    try:
        _get_client().update_item(**request)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise InvocationCompletionError(
                "cannot verify the invocation's completion receipt"
            ) from None
    except BotoCoreError:
        raise InvocationCompletionError(
            "cannot verify the invocation's completion receipt"
        ) from None

    del request["ExpressionAttributeValues"][":true"]
    request["ExpressionAttributeValues"][":false"] = {"BOOL": False}
    request["UpdateExpression"] = "SET #done = :false"
    request["ConditionExpression"] = (
        scope + " AND (attribute_not_exists(#done) OR #done = :false)"
        " AND (attribute_not_exists(#legacy_done) OR #legacy_done = :false)"
        " AND (attribute_not_exists(#status) OR #status <> :complete)"
        " AND (attribute_not_exists(#status) OR #status <> :aborted)"
    )
    try:
        _get_client().update_item(**request)
    except (BotoCoreError, ClientError):
        # Missing/mismatched rows, races and unavailable storage all preserve the
        # queue message. Do not expose SDK response bodies or row contents.
        raise InvocationCompletionError(
            "cannot confirm that this invocation is unfinished"
        ) from None
    return False


def record_delivery_completed(envelope: dict) -> None:
    """Persist terminal consumption before deleting the queue message.

    Idempotent and independent of the dashboard status. An acknowledgement retry
    cannot turn complete into skipped, erase the outcome, or launch another run.
    """
    request = _request(envelope)
    request["ExpressionAttributeValues"][":true"] = {"BOOL": True}
    request["UpdateExpression"] = "SET #done = :true"
    try:
        _get_client().update_item(**request)
    except (BotoCoreError, ClientError):
        raise InvocationCompletionError(
            "cannot persist the invocation's completion receipt"
        ) from None
