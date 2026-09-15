"""Invocation status updater for worker pods.

Worker-only: updates the webhook-events row (written by the Lambda at trigger
time) with status transitions as the agent run progresses.

Phase 1 of Agent Activity (issue #1455). Mirrors the fail-soft pattern from
correlation_store.py — never blocks or fails the run.

## Two write paths (#5028 AC4)

When ``ADP_AGENT_AUTHORITY_ENABLED`` is true, every write in this module goes
through the gateway (:mod:`lib.status_gateway_client`) instead of DynamoDB.

The direct path below writes with the pod's IAM credentials, which belong to the
**shared platform worker role** — every agent worker assumes the same one — under a
grant that permits ``dynamodb:UpdateItem`` on the whole webhook-events table with no
key or attribute condition. Since the row key ``(event_id, arrived_at)`` is a
caller-supplied argument, any worker can write any other worker's row, including its
``control_address`` and ``control_token``. That redirects the other run's control
channel. IAM cannot express the missing restriction, because the row key is data and
the run identity that would have to be compared against it does not exist at the IAM
layer. The gateway has both, so the write moves there and the grant can be removed.

**There is no fallback from the gateway path to the direct path.** A fallback would
require keeping the unconditioned grant to serve it — the exact vulnerability being
removed — and would engage precisely when something is already wrong. When authority
is disabled (the default) the direct path below is used unchanged, so a rollback is
a flag flip.
"""

from __future__ import annotations

import logging
import os
import time

import boto3

from lib.status_gateway_client import (
    StatusGatewayError,
    authority_enabled,
    clear_control,
    record_status,
    register_control,
)

logger = logging.getLogger(__name__)

_table_name = os.environ.get("WEBHOOK_EVENTS_TABLE", "")
_ddb: "boto3.client" | None = None

# Truncation bound for error_message. A stack-trace-shaped string could
# otherwise push the item toward the 400KB DDB limit and fail the whole update,
# losing the status transition too — the exact failure mode we're fixing.
_MAX_ERROR_MESSAGE_CHARS = 1024

# The only status values this worker may write (#3964, ADR-7).
#
# `status` is not decoration: the gateway derives `completed_at` from it, decides
# whether a run is still controllable, and counts it on the dashboard. A typo or an
# invented value therefore does real damage in two directions — an unrecognised
# status is not terminal to any reader, so a finished run keeps its control
# authority and its budget headroom and reads as "in progress" forever; and a value
# that happens to collide with a terminal one retires a live run's controls.
# Neither failure is visible at the call site, which is why this is checked here
# rather than left to the readers to tolerate.
#
# Every value below is one this worker actually writes today, enumerated from the
# `update_status` call sites in `entrypoint.py` (bootstrap failure, idempotency
# skip, in_progress, session-id record, terminal complete/failed, post-agent
# failure) plus `budget_stopped` from #4187. `tests/test_status_vocabulary.py`
# asserts that parity in both directions, so adding a write with a new status fails
# there rather than silently degrading a dashboard.
#
# `aborted` is the status a CONFIRMED ADP abort finalization writes. It is listed
# here so the write path exists when S4 (#3963) delivers the mechanism that calls
# it — this story is vocabulary only and never initiates an abort.
#
# **This allowlist is provider-neutral by construction.** It is a fixed set of ADP
# outcome names, so a provider's native interrupt or error vocabulary
# (`interrupted`, `AbortError`, `ECONNRESET`, a signal name) is rejected rather than
# mapped. Normalization belongs in the harness adapter, which decides whether an
# interruption actually finalized an abort; a writer that guessed from a provider
# string would report a run as deliberately stopped when it merely lost its
# transport.
ALLOWED_WRITE_STATUSES = frozenset(
    {
        "in_progress",
        "complete",
        "failed",
        "skipped",
        "budget_stopped",
        "aborted",
    }
)

# The generation this process was assigned when it registered its listener, so
# teardown can name it (#5028 AC4). Process-local because it describes this pod's
# own attempt: a value carried across attempts is precisely what must not be used
# to clear a registration. None until a successful gateway registration.
_registered_generation: int | None = None


def _get_client():
    global _ddb
    if _ddb is None:
        _ddb = boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return _ddb


def update_status(
    event_id: str,
    arrived_at: str,
    status: str,
    *,
    run_id: str | None = None,
    summary: str | None = None,
    transcript_key: str | None = None,
    session_id: str | None = None,
    token_mode: str | None = None,
    error_message: str | None = None,
    skip_reason: str | None = None,
    stop_reason: str | None = None,
) -> None:
    """Update the invocation row's status. Fail-soft: logs and returns on error.

    Args:
        event_id: The envelope message_id (PK of the webhook-events row).
        arrived_at: The envelope arrived_at timestamp (SK of the row).
        status: New status value (in_progress, complete, failed).
        run_id: KEDA job/pod name (set at in_progress).
        summary: Outcome summary (set at terminal status).
        transcript_key: S3 object key for the full run transcript (set at terminal status).
        session_id: Issue #4186 (Phase 1) — the Claude Agent SDK session id for
            this run, captured by the Node worker and handed over via
            /tmp/adp-result-metadata.json. Recorded for observability only:
            nothing reads it to resume a run yet. It is the identifier a future
            replacement pod would need to locate the persisted conversation
            (Phase 3), and on its own it already lets an operator correlate a
            run to its session.
        token_mode: Issue #3385 (C5) — "app" or "pat" provenance (set at in_progress).
        error_message: Issue #4030 — concrete failure cause, surfaced in the
            Agent Activity detail view. Previously only the ingress Lambda wrote
            this field (on its initial PutItem), so a worker that died during
            bootstrap left the row at ``webhook_received`` with no reason — and
            that status is filtered out of Activity entirely, which is why such
            failures looked like total silence rather than a failed run.
        skip_reason: Issue #4020 — static enum explaining a deliberate,
            non-failure skip (e.g. ``idempotency_merged_pr``). Distinct from
            ``error_message``: nothing went wrong, so the UI renders it neutrally
            rather than as an error. Truncated on the same bound for symmetry.
        stop_reason: Issue #4187 — why a spend cap stopped this run, paired with
            the ``budget_stopped`` status. Its own field rather than
            ``error_message`` for the same reason ``skip_reason`` is: hitting a
            configured cap is the control working, not a fault, and rendering it
            as an error sends operators debugging a run that behaved correctly.

    Raises nothing. An unrecognised ``status`` is refused here — before either
    write path — and logged; see :data:`ALLOWED_WRITE_STATUSES`.
    """
    # Validated FIRST, ahead of both the gateway path and the DynamoDB path
    # (#3964 AC-A12). Ordering is the requirement, not an optimization: a check
    # placed inside either branch would leave the other able to persist a status no
    # reader understands, and the gateway branch is the one that runs in production
    # with delegated authority enabled. Rejecting before any client is constructed
    # also means the unknown value never reaches the network.
    #
    # Fail-soft in the established style of this module: log and return rather than
    # raise. The caller is usually mid-teardown and a status write must never abort
    # the run it is describing. The cost of the refusal is a stale dashboard row;
    # the cost of writing the value would be a finished run that no reader can tell
    # is finished.
    if status not in ALLOWED_WRITE_STATUSES:
        logger.warning(
            "Refusing to write unrecognised invocation status %r (allowed: %s); "
            "a status no reader understands would leave this run looking active",
            status,
            sorted(ALLOWED_WRITE_STATUSES),
        )
        return

    if authority_enabled():
        # The gateway derives the row key from the protected execution record, so
        # event_id/arrived_at are deliberately not forwarded: this call cannot
        # address another run's row. Fail-soft is preserved — a lost status
        # transition degrades a dashboard and must never abort the run.
        try:
            record_status(
                status,
                {
                    "run_id": run_id or "",
                    "summary": summary or "",
                    "transcript_key": transcript_key or "",
                    "session_id": session_id or "",
                    "token_mode": token_mode or "",
                    "error_message": (error_message or "")[:_MAX_ERROR_MESSAGE_CHARS],
                    "skip_reason": (skip_reason or "")[:_MAX_ERROR_MESSAGE_CHARS],
                    "stop_reason": (stop_reason or "")[:_MAX_ERROR_MESSAGE_CHARS],
                },
            )
            logger.info("Updated invocation status via gateway: status=%s", status)
        except StatusGatewayError as exc:
            # No DynamoDB fallback: see the module docstring.
            logger.warning("Failed to update invocation status via gateway (non-fatal): %s", exc)
        except Exception:
            logger.warning("Failed to update invocation status via gateway (non-fatal)")
        return

    table = _table_name or os.environ.get("WEBHOOK_EVENTS_TABLE", "")
    if not table:
        logger.debug("WEBHOOK_EVENTS_TABLE not set; skipping status update")
        return

    if not event_id or not arrived_at:
        logger.debug("Missing event_id or arrived_at; skipping status update")
        return

    try:
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Build update expression
        expr_parts = ["#st = :status", "#su = :status_updated_at"]
        expr_names = {"#st": "status", "#su": "status_updated_at"}
        expr_values = {
            ":status": {"S": status},
            ":status_updated_at": {"S": now_iso},
        }

        if run_id:
            expr_parts.append("#rid = :run_id")
            expr_names["#rid"] = "run_id"
            expr_values[":run_id"] = {"S": run_id}

        if summary:
            expr_parts.append("#sm = :summary")
            expr_names["#sm"] = "summary"
            expr_values[":summary"] = {"S": summary}

        if transcript_key:
            expr_parts.append("#tk = :transcript_key")
            expr_names["#tk"] = "transcript_key"
            expr_values[":transcript_key"] = {"S": transcript_key}

        if session_id:
            expr_parts.append("#sid = :session_id")
            expr_names["#sid"] = "session_id"
            expr_values[":session_id"] = {"S": session_id}

        if token_mode:
            expr_parts.append("#tm = :token_mode")
            expr_names["#tm"] = "token_mode"
            expr_values[":token_mode"] = {"S": token_mode}

        if error_message:
            expr_parts.append("#em = :error_message")
            expr_names["#em"] = "error_message"
            expr_values[":error_message"] = {"S": error_message[:_MAX_ERROR_MESSAGE_CHARS]}

        if skip_reason:
            expr_parts.append("#sr = :skip_reason")
            expr_names["#sr"] = "skip_reason"
            expr_values[":skip_reason"] = {"S": skip_reason[:_MAX_ERROR_MESSAGE_CHARS]}

        if stop_reason:
            expr_parts.append("#stpr = :stop_reason")
            expr_names["#stpr"] = "stop_reason"
            expr_values[":stop_reason"] = {"S": stop_reason[:_MAX_ERROR_MESSAGE_CHARS]}

        update_expr = "SET " + ", ".join(expr_parts)

        # Retry once on ConditionalCheckFailedException for the "in_progress"
        # transition. The Lambda now writes DDB before SQS publish (handler.py
        # reorder, #1463), so this should rarely fire. The retry is defense-in-
        # depth for transient DDB eventual-consistency windows.
        max_attempts = 2 if status == "in_progress" else 1
        for attempt in range(max_attempts):
            try:
                _get_client().update_item(
                    TableName=table,
                    Key={
                        "event_id": {"S": event_id},
                        "arrived_at": {"S": arrived_at},
                    },
                    UpdateExpression=update_expr,
                    ExpressionAttributeNames=expr_names,
                    ExpressionAttributeValues=expr_values,
                    # Only update if row exists — prevents orphan creates
                    ConditionExpression="attribute_exists(event_id)",
                )
                logger.info("Updated invocation status: event_id=%s status=%s", event_id, status)
                return
            except _get_client().exceptions.ConditionalCheckFailedException:
                if attempt < max_attempts - 1:
                    logger.info(
                        "Row not yet visible for event_id=%s, retrying in 2s (attempt %d/%d)",
                        event_id,
                        attempt + 1,
                        max_attempts,
                    )
                    time.sleep(2)  # nosemgrep: arbitrary-sleep
                else:
                    # Row doesn't exist after retries — capture write failed or was skipped
                    logger.warning(
                        "Invocation row not found for status update (event_id=%s) after %d attempts — skipping",
                        event_id,
                        max_attempts,
                    )
    except Exception as exc:
        logger.warning("Failed to update invocation status (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Live control registration — Issue #3960
# ---------------------------------------------------------------------------

# Schema version for the control record. Written explicitly so a gateway reading
# a record it does not understand can refuse rather than guess: a future field
# rename would otherwise make an old pod look like it has no control endpoint,
# which is indistinguishable from a pod that never started a listener.
CONTROL_RECORD_VERSION = 1

# The control fields, named once. `clear_control_endpoint` removes exactly this
# set, so a field added here cannot be left behind at teardown — a stale token
# and address surviving into a terminal row is precisely how a dead pod's IP
# stays addressable after the pod is gone (and IPs get reused).
_CONTROL_ATTRIBUTES = (
    "control_version",
    "control_address",
    "control_port",
    "control_token",
    "control_token_expires_at",
    "control_generation",
    "control_registered_at",
)


def register_control_endpoint(
    event_id: str,
    arrived_at: str,
    *,
    address: str,
    port: int,
    token: str,
    token_expires_at: str,
) -> int | None:
    """Record where this pod's control listener is reachable, and with what token.

    Issue #3960. This is the gateway's only source for the address, port and
    bearer token of a live run's control channel; there is no service discovery
    and the gateway holds no Kubernetes client.

    **The generation is assigned here, by the row, not supplied by the caller.**
    It is an atomic DynamoDB ``ADD`` on ``control_generation``, so each successful
    registration for a given key returns a strictly higher number than the last.
    That matters because a generation is only useful if it actually changes
    between attempts: the pod has no per-attempt identity of its own to derive
    one from (a Job retry pod inherits an identical env, and Kubernetes exposes no
    "which attempt am I" field), so a value read from configuration would be the
    same constant on every attempt and the listener's generation check would
    compare it against itself forever. The row is the one thing that outlives the
    pod, which makes it the only honest source. The gateway reads the same
    attribute it was incremented on, so the two sides cannot disagree.

    Unlike :func:`update_status`, this reports failure instead of swallowing it.
    The reason is that ``update_status`` is fail-soft by design — a lost status
    transition degrades a dashboard — whereas a lost control registration
    produces a run that looks controllable in the UI and is not, with no signal
    anywhere that registration failed. The caller is expected to log and meter a
    ``None`` (FR-1.12, NFR-10). Exceptions are still contained: control is an
    add-on capability and must never abort the run it observes.

    The token is a credential. It is written to DynamoDB because the gateway must
    present it, but it is never logged here, never returned, and is removed by
    :func:`clear_control_endpoint` at terminal teardown.

    Returns:
        The generation assigned to this registration, or None on any failure —
        including a missing table, missing key, invalid arguments, an absent row,
        or a response that does not carry the new generation back. None is
        returned rather than a guessed generation because the caller hands the
        value to the in-pod listener while the gateway reads it from the row: a
        guess that disagreed with the stored value would make the listener reject
        every command the gateway sent, which is indistinguishable from an attack.
    """
    if authority_enabled():
        global _registered_generation

        # ``address`` and ``port`` are accepted for signature compatibility but not
        # forwarded: the gateway uses the IP of the pod it verified through
        # TokenReview. A caller-supplied address is the control-channel redirect
        # this path exists to remove, so passing one through would defeat it.
        if not token:
            logger.warning("Cannot register control endpoint: no control token")
            return None
        try:
            generation = register_control(token=token, token_expires_at=token_expires_at)
            _registered_generation = generation
            # Address and generation, never the token.
            logger.info("Registered control endpoint via gateway: generation=%d", generation)
            return generation
        except StatusGatewayError as exc:
            # None, not an exception: the caller declines to start the listener,
            # which is the correct fail-closed outcome. An unregistered listener is
            # an open port nothing can reach through the policy.
            logger.warning(
                "Failed to register control endpoint via gateway (control unavailable): %s", exc
            )
            return None
        except Exception:
            logger.warning("Failed to register control endpoint via gateway (control unavailable)")
            return None

    table = _table_name or os.environ.get("WEBHOOK_EVENTS_TABLE", "")
    if not table:
        logger.warning("Cannot register control endpoint: WEBHOOK_EVENTS_TABLE not set")
        return None

    if not event_id or not arrived_at:
        logger.warning(
            "Cannot register control endpoint: missing key (event_id=%r arrived_at=%r)",
            event_id,
            arrived_at,
        )
        return None

    # Validated rather than trusted. A blank address or a zero port would produce
    # a row the gateway treats as registered but cannot connect to, turning a
    # config bug into an unexplained 409 at click time.
    if not address or not token or not isinstance(port, int) or port <= 0:
        logger.warning(
            "Cannot register control endpoint: invalid parameters (address_set=%s port=%r token_set=%s)",
            bool(address),
            port,
            bool(token),
        )
        return None

    try:
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        response = _get_client().update_item(
            TableName=table,
            Key={
                "event_id": {"S": event_id},
                "arrived_at": {"S": arrived_at},
            },
            # ADD, not SET, for control_generation: ADD is applied atomically by
            # DynamoDB and treats an absent attribute as 0, so the first attempt
            # lands on 1 and each retry lands one higher without the pod needing
            # to read the current value first (a read-then-write would race two
            # attempts into the same generation, which is the bug this replaces).
            UpdateExpression=(
                "SET control_version = :v, control_address = :a, control_port = :p, "
                "control_token = :t, control_token_expires_at = :e, "
                "control_registered_at = :r "
                "ADD control_generation :one"
            ),
            ExpressionAttributeValues={
                ":v": {"N": str(CONTROL_RECORD_VERSION)},
                ":a": {"S": address},
                ":p": {"N": str(port)},
                ":t": {"S": token},
                ":e": {"S": token_expires_at},
                ":r": {"S": now_iso},
                ":one": {"N": "1"},
            },
            ConditionExpression="attribute_exists(event_id)",
            # The assigned generation has to come back from the same call that
            # assigned it. Re-reading the row afterwards would return whatever a
            # concurrent attempt had incremented it to since.
            ReturnValues="UPDATED_NEW",
        )
        raw_generation = (
            (response or {}).get("Attributes", {}).get("control_generation", {}).get("N")
        )
        if raw_generation is None:
            logger.warning(
                "Control endpoint write succeeded but returned no generation — "
                "treating registration as failed (control unavailable)"
            )
            return None
        generation = int(raw_generation)

        # Deliberately logs the address, port and generation but not the token.
        logger.info(
            "Registered control endpoint: event_id=%s port=%d generation=%d",
            event_id,
            port,
            generation,
        )
        return generation
    except Exception as exc:
        # Contained but loud. The caller decides what to do with None; what must
        # not happen is a silent pass that leaves the UI claiming control exists.
        logger.warning("Failed to register control endpoint (control unavailable): %s", exc)
        return None


def clear_control_endpoint(event_id: str, arrived_at: str) -> bool:
    """Remove the control endpoint and token at terminal teardown.

    Issue #3960. Two reasons this is not optional:

    1. **The token is a credential.** Leaving it in a terminal row extends its
       lifetime indefinitely beyond the process it authenticated.
    2. **Pod IPs are reused.** An address left behind on a finished run points at
       whatever pod holds that IP next. A gateway that trusted a stale address
       would be sending one tenant's control commands at another tenant's pod —
       which is why the terminal status alone is not considered sufficient
       protection, and the fields are actually removed.

    Best-effort by nature: the pod may be killed before this runs, which is why
    the gateway independently refuses control for a terminal run and independently
    checks token expiry rather than relying on this cleanup having happened.

    Returns:
        True if the fields were removed, False otherwise.
    """
    if authority_enabled():
        # The gateway needs the generation to refuse a late teardown from a
        # superseded attempt — clearing a *newer* attempt's registration would
        # silently remove control from a run that is still going. The caller's
        # signature carries only the row key, so the generation comes from the
        # registration this same process performed. If this process never
        # registered, there is nothing of its own to clear and it must not guess:
        # a guessed generation is either a no-op or someone else's teardown.
        generation = _registered_generation
        if generation is None:
            logger.debug("Skipping control endpoint clear (this process registered no listener)")
            return False
        try:
            clear_control(generation)
            logger.info("Cleared control endpoint via gateway: generation=%d", generation)
            return True
        except StatusGatewayError as exc:
            logger.warning("Failed to clear control endpoint via gateway (non-fatal): %s", exc)
            return False
        except Exception:
            logger.warning("Failed to clear control endpoint via gateway (non-fatal)")
            return False

    table = _table_name or os.environ.get("WEBHOOK_EVENTS_TABLE", "")
    if not table or not event_id or not arrived_at:
        logger.debug("Skipping control endpoint clear (table or key missing)")
        return False

    try:
        _get_client().update_item(
            TableName=table,
            Key={
                "event_id": {"S": event_id},
                "arrived_at": {"S": arrived_at},
            },
            UpdateExpression="REMOVE " + ", ".join(_CONTROL_ATTRIBUTES),
            ConditionExpression="attribute_exists(event_id)",
        )
        logger.info("Cleared control endpoint: event_id=%s", event_id)
        return True
    except Exception as exc:
        logger.warning("Failed to clear control endpoint (non-fatal): %s", exc)
        return False
