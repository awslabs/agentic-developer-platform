"""``POST /v1/tasks`` — authenticated task submission (T2, issue #5795).

Reached only through the one lazy dispatch branch in ``github/handler.py``, so
nothing here runs for the GitHub, EventBridge or agent-trigger paths.

What this handler does, in order:

1. refuses everything malformed, oversize or platform-owned at the edge;
2. forwards the caller's own token and a request-bound producer proof to the
   gateway admission adapter;
3. returns the gateway's receipt as a 202.

What it deliberately does not do: decide who the caller is, decide what the
caller may do, or construct a task identifier. Those belong to the gateway,
which owns the acceptance transaction. Every identifier, timestamp and status
in a 202 comes from the component that actually committed the record, so a
202 cannot describe a task that was never stored.
"""

from __future__ import annotations

import json
import os
import uuid

from . import admit_client, contract, errors, validate


def _request_id(event: dict) -> str:
    """Prefer API Gateway's request ID so a caller's report is traceable."""
    context = event.get("requestContext")
    if isinstance(context, dict):
        value = context.get("requestId")
        if isinstance(value, str) and 1 <= len(value) <= 128:
            return value
    return str(uuid.uuid4())


def _admission_enabled() -> bool:
    """Task admission is off unless explicitly enabled.

    Default-off is what lets the route exist in infrastructure ahead of the
    worker and consumer compatibility window without accepting any task.
    """
    return os.environ.get(contract.ADMISSION_FLAG, "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "body": json.dumps(body),
        "headers": {"Content-Type": "application/json"},
    }


def handle_task_submit(event: dict, context) -> dict:
    """Handle one ``POST /v1/tasks`` API Gateway proxy event."""
    request_id = _request_id(event)
    try:
        if event.get("httpMethod") not in (None, contract.SUBMIT_METHOD):
            raise errors.not_found()
        if not _admission_enabled():
            # Off means no task is accepted and none is queued. It is
            # reported as an unavailable prerequisite, not as a 202 or a
            # caller error, because nothing about the request was wrong.
            raise errors.prerequisite_unavailable(
                "Task submission is not enabled in this environment."
            )

        headers = {
            str(k).lower(): v for k, v in (event.get("headers") or {}).items()
        }
        caller_token = validate.bearer_token(headers)
        idempotency_key = validate.idempotency_key(headers)
        raw = validate.request_bytes(event)
        body = validate.decode_body(raw)
        submit = validate.submit_request(body)

        receipt = admit_client.admit(
            submit=submit,
            idempotency_key=idempotency_key,
            caller_token=caller_token,
            request_body=raw,
        )
        return _response(202, validate.submit_response(receipt))
    except errors.TaskApiError as exc:
        return _response(exc.status, exc.body(request_id))
    except Exception:  # noqa: BLE001 - the last line of defence must be total
        # Deliberately broad, and deliberately the final branch. An unexpected
        # fault anywhere above means the task was not durably recorded, so the
        # only safe answer is an explicit failure. Letting an exception escape
        # would surface as an API Gateway 502 whose body this code does not
        # control, which is how a caller ends up unable to tell a refused
        # submission from a possibly-accepted one. The exception is not
        # formatted into the log, because it may carry the caller's token,
        # the producer proof or the request body.
        print("task_api: submission failed before acceptance")
        fallback = errors.prerequisite_unavailable()
        return _response(fallback.status, fallback.body(request_id))
