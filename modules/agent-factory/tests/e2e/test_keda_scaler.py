"""
KEDA scaler health tests (live-only).

Tests 21-24 from the issue:
 21. kubectl get scaledjob shows the object
 22. Trigger config matches SQS queue URL and authMode
 23. Enqueuing a test message causes a pod to spawn within 30s
 24. Pod image pulls cleanly (not ImagePullBackOff)
"""

from __future__ import annotations

import json
import time

import pytest


pytestmark = [pytest.mark.live_only, pytest.mark.kubectl]

SCALEDJOB_NAME = "agent-gateway-worker"
NAMESPACE = "adp-gateway-agents"
SKIP_MSG = (
    f"ScaledJob '{SCALEDJOB_NAME}' not deployed in {NAMESPACE}. "
    "Deploy via deploy-all.sh Step 7/7 (build-and-push + apply keda-scaledjob.yaml)."
)


class TestScaledJobExists:
    """Test 21: ScaledJob object exists."""

    def test_scaledjob_present(self, kube_client):
        sj = kube_client.get_scaledjob(SCALEDJOB_NAME)
        if not sj:
            pytest.skip(SKIP_MSG)
        assert sj["metadata"]["name"] == SCALEDJOB_NAME


class TestTriggerConfig:
    """Test 22: Trigger config matches expected SQS queue and authMode."""

    def test_trigger_matches_sqs_queue(self, kube_client, test_env):
        sj = kube_client.get_scaledjob(SCALEDJOB_NAME)
        if not sj:
            pytest.skip(SKIP_MSG)

        triggers = sj.get("spec", {}).get("triggers", [])
        assert len(triggers) >= 1, "ScaledJob should have at least one trigger"

        sqs_trigger = triggers[0]
        assert sqs_trigger["type"] == "aws-sqs-queue"

        trigger_meta = sqs_trigger.get("metadata", {})
        assert "queueURL" in trigger_meta
        assert trigger_meta.get("identityOwner") == "operator"

        # In live mode, verify it matches the actual queue URL
        if test_env.is_live and test_env.live.tasks_queue_url:
            assert test_env.live.tasks_queue_url in trigger_meta["queueURL"]


class TestPodSpawnOnEnqueue:
    """Test 23: Enqueuing a message causes a pod to spawn.

    IMPORTANT: This test sends a real message to the tasks queue.
    Only runs in live mode with explicit opt-in.
    """

    def test_pod_spawns_on_message(self, kube_client, test_env):
        sj = kube_client.get_scaledjob(SCALEDJOB_NAME)
        if not sj:
            pytest.skip(SKIP_MSG)

        if not test_env.live.tasks_queue_url:
            pytest.skip("TASKS_QUEUE_URL not set")

        import boto3
        import subprocess

        sqs = boto3.client("sqs", region_name="us-east-1")

        # Send a test message
        test_task = {
            "task_id": f"keda-test-{int(time.time())}",
            "session_id": "keda-test-session",
            "thread_id": "",
            "connection_id": "",
            "channel": "test",
            "mode": "chat",
            "agent_type": "developer",
            "message": "KEDA test message - ignore",
            "platform_data": {},
            "enqueued_at": int(time.time()),
        }

        sqs.send_message(
            QueueUrl=test_env.live.tasks_queue_url,
            MessageBody=json.dumps(test_task),
        )

        # Wait up to 30s for a pod to appear
        for _ in range(6):
            time.sleep(5)
            pods = kube_client.list_pods()
            worker_pods = [
                p for p in pods
                if "agent-gateway-worker" in p.get("metadata", {}).get("name", "")
            ]
            if worker_pods:
                return  # Success — pod spawned

        pytest.fail(
            "No worker pod spawned within 30 seconds after enqueuing a message. "
            "Check: kubectl get pods -n adp-gateway-agents; kubectl get scaledjob -n adp-gateway-agents"
        )


class TestPodImagePull:
    """Test 24: Worker pod image pulls cleanly (not ImagePullBackOff)."""

    def test_no_image_pull_backoff(self, kube_client):
        sj = kube_client.get_scaledjob(SCALEDJOB_NAME)
        if not sj:
            pytest.skip(SKIP_MSG)

        pods = kube_client.list_pods()
        worker_pods = [
            p for p in pods
            if "agent-gateway-worker" in p.get("metadata", {}).get("name", "")
        ]

        if not worker_pods:
            pytest.skip("No worker pods currently running to check image pull status")

        for pod in worker_pods:
            statuses = pod.get("status", {}).get("containerStatuses", [])
            for cs in statuses:
                waiting = cs.get("state", {}).get("waiting", {})
                reason = waiting.get("reason", "")
                assert reason != "ImagePullBackOff", (
                    f"Pod {pod['metadata']['name']} has ImagePullBackOff. "
                    "Ensure the worker image exists in ECR: "
                    "aws ecr describe-images --repository-name adp-agent-gateway"
                )
                assert reason != "ErrImagePull", (
                    f"Pod {pod['metadata']['name']} has ErrImagePull."
                )
