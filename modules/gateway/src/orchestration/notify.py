"""Deliver an orchestration alert to a human. Issue #4211 (EPIC #4191, intent #4120).

R-Q9d in one sentence: **detection that nobody hears about is indistinguishable
from no detection at all.** This module is the delivery half of the stall/halt
story, and it exists because nothing in `modules/gateway/src/` could reach a human
before it.

That absence was verified, not assumed. Across the whole gateway backend there is
no `boto3.client("sns")`, no Slack webhook, no SES send, no GitHub-issue posting
and no `notifications` table — the only egress to a human is CloudWatch, and the
repo's established shape for that is metric -> alarm -> `alarm_actions` -> an SNS
topic supplied as a Terraform variable (`modules/gateway/infra/modules/budget-alarms`,
`.../redis`). Both existing instances of that shape are **unset in every tfvars**,
so they currently page nobody. Reusing an unwired precedent would satisfy the
letter of "reuse what exists" while delivering exactly the log line the issue
forbids, so per the issue's own instruction this story ships the smallest real
delivery path instead: an SNS topic the tick publishes to directly.

Why SNS publish from the engine rather than a CloudWatch alarm:

- A stall is a **per-node event with an identity** (which node, which org, which
  flow, how long). An alarm fires on an aggregate crossing a threshold and cannot
  name the node, so the operator would be told "something stalled somewhere" and
  have to go find it — which is the diagnosis problem this EPIC exists to end.
- Alarms are per-environment and dimension-limited; notifications here must be
  **org-scoped** (a stall in one org must never notify another), and the org id
  belongs in the message body, not in an alarm dimension whose cardinality is
  unbounded by tenant count.
- "Emitted once per event, not per tick" is a property of the event, not of a
  threshold. An alarm re-notifies on every breach period by design.

Three properties are load-bearing rather than stylistic:

**The target is configuration, never a hard-coded address.** `NotifyConfig` reads
`BG_ORCH_NOTIFY_TOPIC_ARN` from the environment, stamped in by Terraform. Nothing
in this module knows an email address, a channel name or an account id.

**Failure is loud (R-NF3).** `notify()` raises `NotificationError` on any delivery
failure. It does not return False, it does not log-and-continue, and it emphatically
does not swallow. A silently failing notification path is one of the listed bug
classes for this story, so the "helpful" fail-soft guard is the anti-pattern being
avoided here. The *caller* decides what a delivery failure means for its own
report — see `stall.py`, which records it as an error and forces non-success.

**Delivery being disabled is a distinct, visible outcome.** With no topic
configured, `notify()` raises `NotificationsDisabledError` rather than returning
quietly. An unconfigured environment must not read as a delivered notification;
the caller surfaces it as a real error precisely so "we never wired the topic" is
discovered by the first stall instead of the first postmortem.

Tenant isolation: `Notification` carries `org_id` and it is rendered into both the
subject and the body. There is one topic per environment, subscribed by that
environment's operators — the same audience boundary every existing alarm in this
repo uses. Per-tenant delivery targets are a later story; what this one guarantees
is that no notification about org A is ever *attributed* to org B.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from functools import lru_cache

logger = logging.getLogger("bedrockgateway.orchestration.notify")

# Env var Terraform stamps the topic ARN into. `BG_` prefix matches
# `src/shared/config.py`'s Settings (`env_prefix = "BG_"`) and the vars already on
# the tick Lambda.
TOPIC_ARN_ENV = "BG_ORCH_NOTIFY_TOPIC_ARN"

# SNS truncates subjects at 100 characters and rejects longer ones outright, so a
# long flow title must not be allowed to fail the publish that carries it.
_MAX_SUBJECT = 100


class NotificationError(RuntimeError):
    """Delivery was attempted and failed.

    Raised rather than returned. A caller that wants to continue past a delivery
    failure has to catch this explicitly and record it, which is the point: there
    is no code path where a failed notification costs nothing to ignore.
    """


class NotificationsDisabledError(NotificationError):
    """No delivery target is configured, so nothing could be sent.

    A subclass of `NotificationError` so every caller that handles delivery
    failure also handles this one. Distinct from its parent so an operator reading
    the error can tell "misconfigured environment" apart from "SNS rejected the
    publish" without reading a traceback.
    """


@dataclass(frozen=True)
class NotifyConfig:
    """Where alerts go. Read from the environment, never hard-coded.

    Frozen so a caller cannot repoint delivery halfway through a tick.
    """

    topic_arn: str | None = None
    aws_region: str | None = None

    @classmethod
    def from_env(cls) -> NotifyConfig:
        """Build from the process environment.

        An empty or whitespace-only value is normalised to None so that an
        unset-but-present Terraform variable behaves identically to an absent one
        — both are "not configured", and both raise on send rather than passing an
        empty ARN to SNS and getting an opaque client error.
        """
        raw = (os.environ.get(TOPIC_ARN_ENV) or "").strip()
        return cls(
            topic_arn=raw or None,
            aws_region=os.environ.get("AWS_REGION") or os.environ.get("BG_AWS_REGION") or "us-east-1",
        )

    @property
    def enabled(self) -> bool:
        """True when a delivery target is configured."""
        return bool(self.topic_arn)


@dataclass(frozen=True)
class Notification:
    """One thing a human needs to know about, addressed to one org.

    `event` is the machine-readable discriminator (`node_stalled` / `node_halted`)
    and is what a subscriber filters on. `dedupe_key` is carried so that a
    downstream consumer can recognise a repeat; it is NOT what makes delivery
    once-only here — that guarantee comes from the caller only notifying on a
    conditional UPDATE that matched a row (see `stall.py`).
    """

    org_id: str
    flow_id: str
    node_id: str
    event: str
    summary: str
    # Rendered into the body verbatim. Small, flat, and JSON-serialisable — this
    # is an operator-facing message, not a metrics payload.
    detail: dict[str, str | int | float | None]

    @property
    def dedupe_key(self) -> str:
        """Stable identity of the event this notification is about."""
        return f"{self.org_id}:{self.node_id}:{self.event}"

    def subject(self) -> str:
        """SNS subject line, truncated to the 100-char limit SNS enforces."""
        subject = f"[ADP orchestration] {self.event} — org {self.org_id}"
        if len(subject) <= _MAX_SUBJECT:
            return subject
        return subject[: _MAX_SUBJECT - 1] + "…"

    def body(self) -> str:
        """JSON body. Machine-parseable and human-readable in an email client."""
        return json.dumps(
            {
                "event": self.event,
                "org_id": self.org_id,
                "flow_id": self.flow_id,
                "node_id": self.node_id,
                "summary": self.summary,
                "dedupe_key": self.dedupe_key,
                "detail": self.detail,
            },
            sort_keys=True,
            indent=2,
            default=str,
        )


@lru_cache(maxsize=1)
def _sns_client(region: str):
    """Cached SNS client.

    Cached because a tick may notify about several nodes and client construction
    is the expensive part. Keyed on region so a test that repoints the region gets
    a different client; `_reset_client_cache` clears it between tests.
    """
    import boto3

    return boto3.client("sns", region_name=region)


def _reset_client_cache() -> None:
    """Clear the cached SNS client. For tests and for a re-used Lambda container."""
    _sns_client.cache_clear()


def notify(notification: Notification, config: NotifyConfig | None = None) -> str:
    """Deliver one notification. Returns the SNS message id.

    Args:
        notification: What happened, to which node, in which org.
        config: Where to deliver. Read from the environment when omitted.

    Returns:
        The SNS `MessageId` — evidence the publish was accepted, which is what
        makes "was this actually delivered?" answerable rather than assumed.

    Raises:
        NotificationsDisabledError: No topic is configured. Raised, not returned:
            an environment with no delivery target must not look like one that
            delivered.
        NotificationError: The publish was attempted and failed. Never swallowed
            (R-NF3).
    """
    config = config or NotifyConfig.from_env()

    if not config.enabled:
        raise NotificationsDisabledError(
            f"no orchestration notification target configured ({TOPIC_ARN_ENV} is unset); "
            f"cannot deliver {notification.event} for node {notification.node_id}"
        )

    try:
        response = _sns_client(config.aws_region or "us-east-1").publish(
            TopicArn=config.topic_arn,
            Subject=notification.subject(),
            Message=notification.body(),
            MessageAttributes={
                # Attributes, not just body fields, so a subscriber can filter
                # server-side — an org's operators can subscribe to their own org
                # without receiving another org's events.
                "event": {"DataType": "String", "StringValue": notification.event},
                "org_id": {"DataType": "String", "StringValue": notification.org_id},
            },
        )
    except Exception as exc:
        # Wrapped rather than re-raised bare so callers can catch one type, and
        # chained so the underlying botocore error survives for diagnosis.
        raise NotificationError(f"failed to publish {notification.event} for node {notification.node_id}: {exc}") from exc

    message_id = str(response.get("MessageId") or "")
    logger.info(
        "orchestration notify: delivered %s for node %s (org %s) as message %s",
        notification.event,
        notification.node_id,
        notification.org_id,
        message_id or "<no id>",
    )
    return message_id
