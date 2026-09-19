"""
Enqueue -> acknowledgement latency recording for the GitLab live-fleet checks.

#5354: a single 60s boolean cannot distinguish "the webhook->agent path is
broken" from "the fleet was busy and the worker arrived at 69s". The useful
measurement is the latency itself, tracked per run, so saturation is a visible
operational number instead of an intermittently red checkbox.

Two sinks, deliberately:

  * the existing per-test JSON harness (``tests/e2e/latency.py``), uploaded as a
    build artifact — human-readable evidence attached to the exact run;
  * a CloudWatch metric, so the series is queryable across recent runs without
    downloading artifacts one by one.

Both are best-effort. A metric sink that is unreachable must not fail a test
that otherwise passed.
"""

from __future__ import annotations

import os
from typing import Any

METRIC_NAMESPACE = "ADP/AgentFactory"
METRIC_NAME = "GitLabEnqueueToAckSeconds"


def publish_ack_latency(
    seconds: float,
    persona: str,
    env: str | None = None,
    workers_claimed: int | None = None,
    cloudwatch_client: Any = None,
) -> bool:
    """Publish one enqueue->ack latency datapoint. Returns True if published.

    Never raises: the caller is a test that has already established its result.
    """
    try:
        client = cloudwatch_client
        if client is None:
            import boto3

            client = boto3.client(
                "cloudwatch", region_name=os.environ.get("AWS_REGION", "us-east-1")
            )
        dimensions = [
            {"Name": "Environment", "Value": env or os.environ.get("TEST_ENV") or "dev"},
            {"Name": "Persona", "Value": persona},
        ]
        if workers_claimed is not None:
            dimensions.append({"Name": "WorkersClaimed", "Value": str(workers_claimed)})
        client.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[
                {
                    "MetricName": METRIC_NAME,
                    "Value": float(seconds),
                    "Unit": "Seconds",
                    "Dimensions": dimensions,
                }
            ],
        )
        return True
    except Exception:  # noqa: BLE001 — a metric sink outage is not a test failure
        return False
