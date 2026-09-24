"""Task dispatch publication onto the existing shared agent-submit queue.

Deliberately separate from `sqs_publisher`, which owns the legacy webhook
envelope. The two share a queue but nothing else: a different body shape, a
different grouping rule and a different deduplication key. Threading a task
branch through the legacy publisher would put the regression risk on the path
that carries all current traffic, so the task attributes are computed here and
the legacy function is left untouched.

Two rules in here are load-bearing for correctness rather than tidiness.

`MessageGroupId` is a hash of tenant + task, so one task's messages stay ordered
relative to each other while unrelated tasks progress in parallel. The legacy
path groups per (tenant, repo, issue) for the same head-of-line reason; a task
has no repo or issue, and grouping by tenant alone would let one stuck task
block the tenant.

`MessageDeduplicationId` is the stable dispatch UUID and NOT the invocation ID.
Recovery republishes the same invocation under a NEW dispatch ID on purpose.
Keying deduplication on the invocation would let SQS's five-minute window
silently swallow a legitimate recovery republish: no message, no error, and a
task that stalls until its deadline. The SQS deduplication window is not the
task's idempotency window -- the idempotency key owns that, durably.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os

import boto3

logger = logging.getLogger(__name__)

REGION = os.environ.get("AWS_REGION", "us-east-1")

# The envelope references committed input rather than embedding it, so a task
# body is small. This bound exists to fail loudly rather than to be approached.
MAX_TASK_MESSAGE_BYTES = 64 * 1024

TASK_ENVELOPE_KIND = "adp.task"
TASK_SCHEMA_VERSION = "1.0"

# Fields the envelope must never carry. Transport identifiers (a receipt handle,
# the SQS message ID) are the important ones: a redelivered message has a new
# transport ID and the SAME run, so letting a transport ID act as a run handle
# is exactly how one run is reported as two. The rest are caller-selected
# authority and credentials, which the assignment reference replaces.
FORBIDDEN_ENVELOPE_FIELDS = (
    "receipt_handle",
    "sqs_message_id",
    "run_credential",
    "token",
    "tenant_id",
    "repository",
    "installation_id",
    "issue_number",
)

REQUIRED_ENVELOPE_FIELDS = (
    "kind",
    "schema_version",
    "task_id",
    "invocation_id",
    "message_id",
    "persona",
    "dispatch_id",
    "request_digest",
    "input_ref",
    "assignment_ref",
)

_sqs = None


def _get_sqs():
    global _sqs
    if _sqs is None:
        _sqs = boto3.client("sqs", region_name=REGION)
    return _sqs


class TaskPublicationError(Exception):
    """The envelope could not be published under the required guarantees.

    `outcome` is what the gateway is told. "unknown" is a real outcome, not a
    softened failure: if the send may have reached SQS we must not assert that
    it did not, because that assertion is what would produce a second run.
    """

    def __init__(self, code: str, *, outcome: str = "failed"):
        self.code = code
        self.outcome = outcome
        super().__init__(code)


def digest_components(*components: str) -> str:
    """SHA-256 over versioned length-delimited components.

    Ambiguous concatenation is the defect being avoided: joining "ab" + "c" and
    "a" + "bc" with a separator-free scheme yields the same digest, so a tenant
    and task pair could collide with a different pair. Each component is
    prefixed with its byte length, and the scheme itself is versioned so a later
    change cannot silently reinterpret an existing digest.
    """
    parts = [b"v1"]
    for component in components:
        raw = component.encode("utf-8")
        parts.append(str(len(raw)).encode("ascii"))
        parts.append(raw)
    return hashlib.sha256(b"\x1f".join(parts)).hexdigest()


def message_group_id(tenant_id: str, task_id: str) -> str:
    """Hash of tenant + task: per-task ordering without per-tenant blocking."""
    if not tenant_id or not task_id:
        raise TaskPublicationError("invalid_group_scope")
    return digest_components(tenant_id, task_id)


def publish_attributes(envelope: dict, *, tenant_id: str) -> dict:
    """The exact FIFO attributes for a task envelope.

    Returned as a dict rather than applied inline so a test can assert the
    attributes without a queue, and so the dedup-key rule has one definition.
    """
    return {
        "MessageGroupId": message_group_id(tenant_id, envelope["task_id"]),
        "MessageDeduplicationId": envelope["dispatch_id"],
    }


def validate_envelope(envelope: dict) -> None:
    """Refuse an envelope the contract forbids, before it reaches the queue.

    This is a producer-side guard, not the security boundary: the worker
    revalidates the committed digest at bootstrap. It exists so a malformed
    envelope fails at the publisher with a clear reason instead of becoming a
    message no consumer can act on.
    """
    if not isinstance(envelope, dict):
        raise TaskPublicationError("invalid_envelope")
    missing = [field for field in REQUIRED_ENVELOPE_FIELDS if field not in envelope]
    if missing:
        raise TaskPublicationError("invalid_envelope")
    present_forbidden = [f for f in FORBIDDEN_ENVELOPE_FIELDS if f in envelope]
    if present_forbidden:
        raise TaskPublicationError("forbidden_envelope_field")
    if envelope["kind"] != TASK_ENVELOPE_KIND:
        raise TaskPublicationError("invalid_envelope")
    if envelope["schema_version"] != TASK_SCHEMA_VERSION:
        raise TaskPublicationError("invalid_envelope")
    # message_id is pinned to the invocation ID so there is exactly one run
    # handle. Two divergent identifiers is how the same run gets reported twice
    # under different names.
    if envelope["message_id"] != envelope["invocation_id"]:
        raise TaskPublicationError("message_id_not_invocation")


def serialize_envelope(envelope: dict) -> str:
    """The exact bytes published, matching the digest the gateway committed.

    Sorted keys and fixed separators, because the gateway persisted a digest of
    this body BEFORE publication and the worker compares against it. Any
    formatting drift here turns every bootstrap into a digest mismatch.
    """
    body = json.dumps(
        envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    if len(body.encode("utf-8")) > MAX_TASK_MESSAGE_BYTES:
        raise TaskPublicationError("envelope_too_large")
    return body


def publish_task_envelope(envelope: dict, *, tenant_id: str, queue_url: str) -> dict:
    """Publish one committed task envelope and report what actually happened.

    Returns the publication outcome the gateway settles against:
      confirmed -- SQS returned a message ID; the message is on the queue.
      unknown   -- the send may or may not have landed. Never retried blindly
                   here; recovery re-sends the SAME dispatch envelope and ID, so
                   FIFO deduplication collapses a duplicate, and the conditional
                   bootstrap prevents concurrent execution even after that
                   deduplication window expires.
      failed    -- refused before any send could reach the queue.

    The distinction between unknown and failed is the whole point. Collapsing
    unknown into failed loses tasks that actually were delivered; collapsing it
    into confirmed strands tasks that were not.
    """
    if not queue_url:
        raise TaskPublicationError("queue_unavailable", outcome="failed")
    validate_envelope(envelope)
    attributes = publish_attributes(envelope, tenant_id=tenant_id)
    body = serialize_envelope(envelope)

    send_kwargs = {"QueueUrl": queue_url, "MessageBody": body}
    if queue_url.endswith(".fifo"):
        send_kwargs.update(attributes)

    try:
        response = _get_sqs().send_message(**send_kwargs)
    except Exception as error:  # noqa: BLE001 - see below
        # Any send failure is ambiguous from here. A connection reset, a read
        # timeout and a throttle look alike to the caller, and at least one of
        # them can leave the message delivered. Reporting "unknown" keeps the
        # task recoverable; reporting "failed" would authorize a fresh dispatch
        # that could become a second execution.
        logger.warning(
            "task publication outcome unknown task_id=%s dispatch_id=%s error=%s",
            envelope.get("task_id"),
            envelope.get("dispatch_id"),
            type(error).__name__,
        )
        raise TaskPublicationError("send_ambiguous", outcome="unknown") from None

    sqs_message_id = response.get("MessageId") or ""
    if not sqs_message_id:
        # A 200 without a message ID is not proof of publication.
        raise TaskPublicationError("send_unconfirmed", outcome="unknown")
    logger.info(
        "task published task_id=%s dispatch_id=%s sqs_message_id=%s",
        envelope["task_id"],
        envelope["dispatch_id"],
        sqs_message_id,
    )
    # sqs_message_id is reported for settlement/diagnostics only. It never
    # enters the envelope and never identifies the run.
    return {"publication_outcome": "confirmed", "sqs_message_id": sqs_message_id}
