"""
Fleet-state diagnostics captured at live-assertion expiry.

The GitLab live-fleet assertions wait for a real agent-worker pod to consume a
queued job and post an acknowledgement. When that wait expires the interesting
question is never "did the assertion fail" but "what was the fleet doing" —
#5354 recorded workers arriving 9-39s *after* the deadline, which means the
message was consumed late, not lost. Reconstructing that required a manual hunt
through Lambda logs.

This module collects the three facts that distinguish "the path is broken" from
"the consumer was late", at the moment of expiry:

  * ``ApproximateNumberOfMessagesNotVisible`` — in flight: received by some
    consumer, not yet acknowledged.
  * ``ApproximateNumberOfMessagesVisible`` — queued and unclaimed. Zero visible
    with a high in-flight count is a saturated fleet, not an ingress fault.
  * worker pods that claimed a queue message since the enqueue, counted from
    the bootstrap log group. Every pod that successfully receives a message
    opens a bootstrap stream keyed by its correlation id
    (``entrypoint.py`` -> ``BootstrapLogger``); a pod that long-polls an empty
    queue exits before that point, so a stream created during the wait means a
    consumer actually claimed work.

    The bootstrap group is the right source specifically because it is
    provider-agnostic. The GitLab mention path returns from
    ``_handle_gitlab_mention`` long before it would exec the Node agent, so it
    never creates an ``agent-<persona>-issue-<n>`` stream in the *agent* log
    group. Counting that group would report 0 for every GitLab run and invite
    exactly the wrong conclusion — "nothing picked it up" — which is the
    misreading #5354 exists to prevent.

Collection is best-effort by construction: a diagnostics failure must never
mask or replace the assertion it is describing. Every collector degrades to an
``errors`` entry and the caller still reports its real failure.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

# Every worker pod that claims a queue message opens a stream here, whatever
# provider the message came from; see webhook-ingress/infra/cloudwatch.tf.
BOOTSTRAP_LOG_GROUP_TEMPLATE = "/adp/{env}/agent-factory/bootstrap"

_UNAVAILABLE = "unavailable"


def bootstrap_log_group(env: str | None = None) -> str:
    """Return the worker bootstrap log group for an environment."""
    resolved = env or os.environ.get("TEST_ENV") or "dev"
    return BOOTSTRAP_LOG_GROUP_TEMPLATE.format(env=resolved)


@dataclass
class FleetState:
    """Fleet/queue state sampled at a single moment."""

    in_flight: int | None = None
    visible: int | None = None
    worker_streams_since_enqueue: int | None = None
    recent_stream_names: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "submit_queue_in_flight": self.in_flight,
            "submit_queue_visible": self.visible,
            "worker_streams_since_enqueue": self.worker_streams_since_enqueue,
            "recent_stream_names": self.recent_stream_names,
            "diagnostic_errors": self.errors,
        }


def _fmt(value: int | None) -> str:
    return _UNAVAILABLE if value is None else str(value)


def collect_queue_depth(queue_url: str, state: FleetState, sqs_client: Any = None) -> None:
    """Sample in-flight and visible depth on the agent-submit queue."""
    if not queue_url:
        state.errors.append("queue depth not sampled: no queue URL available to the test")
        return
    try:
        client = sqs_client
        if client is None:
            import boto3

            client = boto3.client("sqs", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        attrs = client.get_queue_attributes(
            QueueUrl=queue_url,
            AttributeNames=[
                "ApproximateNumberOfMessagesNotVisible",
                "ApproximateNumberOfMessages",
            ],
        ).get("Attributes", {})
        state.in_flight = int(attrs["ApproximateNumberOfMessagesNotVisible"])
        state.visible = int(attrs["ApproximateNumberOfMessages"])
    except Exception as exc:  # noqa: BLE001 — diagnostics must never mask the assertion
        state.errors.append(f"queue depth not sampled: {type(exc).__name__}: {exc}")


def collect_worker_streams(
    enqueue_epoch_ms: int,
    state: FleetState,
    env: str | None = None,
    logs_client: Any = None,
    max_names: int = 8,
) -> None:
    """Count worker pods that claimed a queue message at or after the enqueue.

    Counts streams in the bootstrap log group, which every message-claiming pod
    opens regardless of provider. A pod that long-polls an empty queue exits
    before opening one, so these are consumers that actually took work.

    Streams are requested newest-first by last event. Bootstrap streams emit
    their first event immediately on open, so a pod that started during the wait
    sorts into this window rather than being stranded behind idle streams.
    """
    try:
        client = logs_client
        if client is None:
            import boto3

            client = boto3.client("logs", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        resp = client.describe_log_streams(
            logGroupName=bootstrap_log_group(env),
            orderBy="LastEventTime",
            descending=True,
            limit=50,
        )
        matched: list[str] = []
        for stream in resp.get("logStreams", []):
            created = stream.get("creationTime")
            if created is None or created < enqueue_epoch_ms:
                continue
            matched.append(stream.get("logStreamName", ""))
        state.worker_streams_since_enqueue = len(matched)
        state.recent_stream_names = matched[:max_names]
    except Exception as exc:  # noqa: BLE001 — diagnostics must never mask the assertion
        state.errors.append(f"worker streams not counted: {type(exc).__name__}: {exc}")


def collect_fleet_state(
    queue_url: str,
    enqueue_epoch_ms: int,
    env: str | None = None,
    sqs_client: Any = None,
    logs_client: Any = None,
) -> FleetState:
    """Sample queue depth and worker-start count. Never raises."""
    state = FleetState()
    collect_queue_depth(queue_url, state, sqs_client=sqs_client)
    collect_worker_streams(enqueue_epoch_ms, state, env=env, logs_client=logs_client)
    return state


def format_expiry_diagnostics(
    what: str,
    timeout: float,
    sqs_message_id: str | None,
    state: FleetState,
) -> str:
    """Build the assertion message for an expired live-fleet wait.

    Names *why* the wait expired, not merely that it did: queue depth, how many
    workers started during the wait, and the queue message ID that joins this
    run to the worker logs.
    """
    lines = [
        f"{what} within {timeout:.0f}s.",
        "",
        "Fleet state at expiry (see #5354 — a late consumer is not a broken path):",
        f"  submit queue in flight (received, unacked): {_fmt(state.in_flight)}",
        f"  submit queue visible (queued, unclaimed):   {_fmt(state.visible)}",
        (
            f"  workers that claimed a message during the wait: "
            f"{_fmt(state.worker_streams_since_enqueue)}"
        ),
        f"  webhook sqs_id (join key for worker logs):  {sqs_message_id or _UNAVAILABLE}",
    ]
    if state.recent_stream_names:
        lines.append("  worker bootstrap streams opened during the wait (by correlation id):")
        lines.extend(f"    - {name}" for name in state.recent_stream_names)
    if state.in_flight is not None and state.visible == 0 and state.in_flight > 0:
        lines.append(
            "  reading: nothing queued and unclaimed, work in flight elsewhere "
            "— consistent with a saturated fleet rather than an ingress fault."
        )
    if state.errors:
        lines.append("  diagnostics not fully collected:")
        lines.extend(f"    - {err}" for err in state.errors)
    return "\n".join(lines)
