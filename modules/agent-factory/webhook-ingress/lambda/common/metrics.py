"""CloudWatch custom metrics for webhook ingress observability.

Emits metrics under the `WebhookIngress` namespace:
  - EventsReceived: count of webhooks (dimensions: channel, tenant_id)
  - EventsDispatched: count of webhooks dispatched to agent (dimension: persona)
  - RateLimited: count of rate-limited requests (dimension: tenant_id)

Metrics are batched and flushed to reduce CloudWatch API calls. Each Lambda
invocation handles one webhook, so we emit immediately (no batching needed).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import boto3

logger = logging.getLogger(__name__)

NAMESPACE = "WebhookIngress"


class WebhookMetrics:
    """Emit CloudWatch custom metrics for webhook ingress.

    Usage:
        metrics = WebhookMetrics()
        metrics.record_received(channel="github", tenant_id="acme-corp")
        metrics.record_dispatched(persona="developer")
        metrics.record_rate_limited(tenant_id="acme-corp")
        metrics.flush()
    """

    def __init__(self, region: str = "us-east-1", namespace: str = NAMESPACE):
        self._cloudwatch = boto3.client("cloudwatch", region_name=region)
        self._namespace = namespace
        self._metric_data: list[dict[str, Any]] = []

    def record_received(self, channel: str, tenant_id: str) -> None:
        """Record a webhook received event."""
        self._metric_data.append(
            {
                "MetricName": "EventsReceived",
                "Dimensions": [
                    {"Name": "Channel", "Value": channel},
                    {"Name": "TenantId", "Value": tenant_id},
                ],
                "Value": 1,
                "Unit": "Count",
                "Timestamp": time.time(),
            }
        )

    def record_dispatched(self, persona: str) -> None:
        """Record a successful agent dispatch."""
        self._metric_data.append(
            {
                "MetricName": "EventsDispatched",
                "Dimensions": [
                    {"Name": "Persona", "Value": persona},
                ],
                "Value": 1,
                "Unit": "Count",
                "Timestamp": time.time(),
            }
        )

    def record_rate_limited(self, tenant_id: str) -> None:
        """Record a rate-limited request."""
        self._metric_data.append(
            {
                "MetricName": "RateLimited",
                "Dimensions": [
                    {"Name": "TenantId", "Value": tenant_id},
                ],
                "Value": 1,
                "Unit": "Count",
                "Timestamp": time.time(),
            }
        )

    def record_sibling_app(self, repo: str, sibling_login: str) -> None:
        """Record detection of a sibling ADP App on the same repo (issue #2732).

        Emitted when a foreign ADP-family bot comment (carries the
        ``adp-correlation:`` marker but is not our own App's bot) is delivered
        to our webhook — a signal that a second ADP deployment's App is
        installed on the same repo and executing the same triggers.

        Advisory only: this method never blocks or rejects the event.

        Args:
            repo: The full repo name (e.g. "aws-innovate/adp").
            sibling_login: The foreign bot's login (e.g. "aws-e-adp-agent-dev[bot]").
        """
        self._metric_data.append(
            {
                "MetricName": "SiblingAppDetected",
                "Dimensions": [
                    {"Name": "Repo", "Value": repo},
                    {"Name": "SiblingLogin", "Value": sibling_login},
                ],
                "Value": 1,
                "Unit": "Count",
                "Timestamp": time.time(),
            }
        )

    def record_rejected(self, reason: str) -> None:
        """Record a rejected webhook with distinct RejectedReason dimension.

        Args:
            reason: One of "unknown_installation", "unknown_user",
                "cross_tenant_identity".
        """
        self._metric_data.append(
            {
                "MetricName": "Rejected",
                "Dimensions": [
                    {"Name": "RejectedReason", "Value": reason},
                ],
                "Value": 1,
                "Unit": "Count",
                "Timestamp": time.time(),
            }
        )

    def flush(self) -> None:
        """Flush accumulated metrics to CloudWatch.

        Best-effort: errors are logged but never block the Lambda response.
        CloudWatch PutMetricData accepts up to 1000 metric data points per call.
        """
        if not self._metric_data:
            return

        try:
            # CloudWatch expects timestamps as datetime objects or epoch floats.
            # boto3 handles float timestamps by converting to datetime internally.
            from datetime import datetime, timezone

            for md in self._metric_data:
                if isinstance(md.get("Timestamp"), (int, float)):
                    md["Timestamp"] = datetime.fromtimestamp(
                        md["Timestamp"], tz=timezone.utc
                    )

            self._cloudwatch.put_metric_data(
                Namespace=self._namespace,
                MetricData=self._metric_data,
            )
            logger.debug("Flushed %d metrics to CloudWatch", len(self._metric_data))
        except Exception as e:
            logger.error("Failed to flush metrics to CloudWatch: %s", e)
        finally:
            self._metric_data = []
