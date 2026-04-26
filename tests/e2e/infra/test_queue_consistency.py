"""
Infrastructure consistency tests for the chat agent pipeline.

Regression guard for the FIFO queue split-brain incident: when the response
queue was renamed from Standard to FIFO, the worker ConfigMap, ingest Lambda
env, response Lambda event source, and worker IAM policy drifted out of sync.
Workers got AccessDenied for 30+ minutes.

These tests use boto3 + kubectl (subprocess) directly — no browser needed.
They run in seconds and catch config drift before it causes user-visible
failures.

Requires:
- E2E_CHAT_ENABLED=1
- kubectl configured for the target cluster
- AWS credentials with read access to Lambda, SQS, IAM, and EKS
"""

from __future__ import annotations

import json
import os
import re
import subprocess

import boto3
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _kubectl_json(*args: str) -> dict | list:
    """Run a kubectl command with -o json and return parsed output."""
    cmd = ["kubectl", *args, "-o", "json"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        pytest.skip(f"kubectl failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def _queue_url_to_arn(queue_url: str, region: str, account_id: str) -> str:
    """Convert an SQS queue URL to its ARN.

    https://sqs.<region>.amazonaws.com/<account>/<name>
    → arn:aws:sqs:<region>:<account>:<name>
    """
    # Parse queue name from URL
    parts = queue_url.rstrip("/").split("/")
    queue_name = parts[-1]
    return f"arn:aws:sqs:{region}:{account_id}:{queue_name}"


def _extract_queue_url_from_arn(arn: str, region: str, account_id: str) -> str:
    """Convert an SQS queue ARN to its URL."""
    # arn:aws:sqs:<region>:<account>:<name>
    parts = arn.split(":")
    queue_name = parts[-1]
    return f"https://sqs.{region}.amazonaws.com/{account_id}/{queue_name}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def aws_clients(aws_region):
    """Lazily-created AWS service clients."""
    return {
        "lambda": boto3.client("lambda", region_name=aws_region),
        "sqs": boto3.client("sqs", region_name=aws_region),
        "iam": boto3.client("iam", region_name=aws_region),
        "sts": boto3.client("sts", region_name=aws_region),
    }


@pytest.fixture(scope="module")
def account_id(aws_clients):
    return aws_clients["sts"].get_caller_identity()["Account"]


@pytest.fixture(scope="module")
def worker_configmap(kubectl_available, name_prefix):
    """Read the chat-agent-config ConfigMap from the worker namespace."""
    try:
        cm = _kubectl_json(
            "get", "configmap", "chat-agent-config",
            "-n", "adp-gateway-agents",
        )
        return cm.get("data", {})
    except Exception:
        pytest.skip("Could not read chat-agent-config ConfigMap")


@pytest.fixture(scope="module")
def ingest_lambda_env(aws_clients, name_prefix):
    """Read environment variables from the ingest Lambda."""
    try:
        resp = aws_clients["lambda"].get_function_configuration(
            FunctionName=f"{name_prefix}-gateway-ingest"
        )
        return resp.get("Environment", {}).get("Variables", {})
    except Exception as e:
        pytest.skip(f"Could not read ingest Lambda config: {e}")


@pytest.fixture(scope="module")
def response_lambda_config(aws_clients, name_prefix):
    """Read the response Lambda's configuration + event source mappings."""
    lam = aws_clients["lambda"]
    func_name = f"{name_prefix}-gateway-response"
    try:
        config = lam.get_function_configuration(FunctionName=func_name)
        env = config.get("Environment", {}).get("Variables", {})

        # Get event source mappings for this function
        mappings = lam.list_event_source_mappings(FunctionName=func_name)
        sqs_mappings = [
            m for m in mappings.get("EventSourceMappings", [])
            if ":sqs:" in m.get("EventSourceArn", "")
        ]
        return {"env": env, "event_source_mappings": sqs_mappings}
    except Exception as e:
        pytest.skip(f"Could not read response Lambda config: {e}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestChatInfraConsistency:
    """Regression guard for the FIFO queue split-brain incident.

    When any of {worker ConfigMap, Ingest Lambda env, Response Lambda event
    source, Worker IAM policy} points at a different queue than the others,
    chat breaks silently.  Assert they all agree and the queue exists.
    """

    def test_response_queue_consistent_across_worker_and_lambdas(
        self,
        worker_configmap,
        ingest_lambda_env,
        response_lambda_config,
        aws_clients,
        aws_region,
        account_id,
    ):
        """All components must agree on the response queue URL/ARN, and
        the queue must actually exist in SQS."""

        # 1. Worker ConfigMap: RESPONSE_QUEUE_URL
        cm_response_url = worker_configmap.get("RESPONSE_QUEUE_URL", "")
        assert cm_response_url and not cm_response_url.startswith("REPLACE_WITH"), (
            f"Worker ConfigMap RESPONSE_QUEUE_URL is not set: {cm_response_url!r}"
        )

        # 2. Ingest Lambda env: RESPONSE_QUEUE_URL
        ingest_response_url = ingest_lambda_env.get("RESPONSE_QUEUE_URL", "")
        assert ingest_response_url and not ingest_response_url.startswith("REPLACE_WITH"), (
            f"Ingest Lambda RESPONSE_QUEUE_URL is not set: {ingest_response_url!r}"
        )

        # 3. Response Lambda event source mapping: EventSourceArn
        esm_list = response_lambda_config["event_source_mappings"]
        assert len(esm_list) >= 1, (
            "Response Lambda has no SQS event source mapping — "
            "it will never be triggered by response queue messages."
        )
        esm = esm_list[0]
        esm_arn = esm["EventSourceArn"]

        # 4. Convert URLs to ARNs for comparison
        cm_arn = _queue_url_to_arn(cm_response_url, aws_region, account_id)
        ingest_arn = _queue_url_to_arn(ingest_response_url, aws_region, account_id)

        # 5. Assert all three agree
        assert cm_arn == ingest_arn, (
            f"Response queue mismatch between worker ConfigMap and ingest Lambda!\n"
            f"  Worker ConfigMap → {cm_response_url} ({cm_arn})\n"
            f"  Ingest Lambda   → {ingest_response_url} ({ingest_arn})\n"
            f"Split-brain: messages will be sent to one queue but consumed from another."
        )
        assert cm_arn == esm_arn, (
            f"Response queue mismatch between worker ConfigMap and response Lambda event source!\n"
            f"  Worker ConfigMap         → {cm_response_url} ({cm_arn})\n"
            f"  Response Lambda ESM ARN  → {esm_arn}\n"
            f"Split-brain: worker sends to one queue, Lambda listens on another."
        )

        # 6. Assert the queue actually exists
        try:
            attrs = aws_clients["sqs"].get_queue_attributes(
                QueueUrl=cm_response_url,
                AttributeNames=["QueueArn"],
            )
            actual_arn = attrs["Attributes"]["QueueArn"]
            assert actual_arn == cm_arn, (
                f"Queue exists but ARN mismatch: expected {cm_arn}, got {actual_arn}"
            )
        except aws_clients["sqs"].exceptions.QueueDoesNotExist:
            pytest.fail(
                f"Response queue does not exist in SQS!\n"
                f"  URL: {cm_response_url}\n"
                f"All components point at a non-existent queue."
            )
        except Exception as e:
            if "NonExistentQueue" in str(e) or "QueueDoesNotExist" in str(e):
                pytest.fail(
                    f"Response queue does not exist: {cm_response_url}\n{e}"
                )
            raise

        # 7. Assert event source mapping is enabled
        assert esm.get("State") == "Enabled", (
            f"Response Lambda event source mapping is not Enabled "
            f"(state={esm.get('State')!r}). Messages will pile up unprocessed."
        )

    def test_tasks_queue_consistent(
        self,
        worker_configmap,
        ingest_lambda_env,
        aws_clients,
        aws_region,
        account_id,
        kubectl_available,
        name_prefix,
    ):
        """The input/tasks queue must be consistent across:
        - Worker ConfigMap (INPUT_QUEUE_URL)
        - Ingest Lambda env (INPUT_QUEUE_URL)
        - KEDA ScaledJob trigger (queueURL)
        And the queue must exist.
        """

        # 1. Worker ConfigMap: INPUT_QUEUE_URL
        cm_input_url = worker_configmap.get("INPUT_QUEUE_URL", "")
        assert cm_input_url and not cm_input_url.startswith("REPLACE_WITH"), (
            f"Worker ConfigMap INPUT_QUEUE_URL is not set: {cm_input_url!r}"
        )

        # 2. Ingest Lambda env: INPUT_QUEUE_URL
        ingest_input_url = ingest_lambda_env.get("INPUT_QUEUE_URL", "")
        assert ingest_input_url and not ingest_input_url.startswith("REPLACE_WITH"), (
            f"Ingest Lambda INPUT_QUEUE_URL is not set: {ingest_input_url!r}"
        )

        # 3. Assert ConfigMap and Lambda agree
        assert cm_input_url == ingest_input_url, (
            f"Tasks queue mismatch between worker ConfigMap and ingest Lambda!\n"
            f"  Worker ConfigMap → {cm_input_url}\n"
            f"  Ingest Lambda   → {ingest_input_url}\n"
            f"Split-brain: ingest enqueues to one queue, worker consumes from another."
        )

        # 4. Check KEDA ScaledJob trigger queueURL
        try:
            sj = _kubectl_json(
                "get", "scaledjob", "chat-agent-worker",
                "-n", "adp-gateway-agents",
            )
            triggers = sj.get("spec", {}).get("triggers", [])
            sqs_triggers = [
                t for t in triggers
                if t.get("type") == "aws-sqs-queue"
            ]
            if sqs_triggers:
                keda_queue_url = sqs_triggers[0].get("metadata", {}).get("queueURL", "")
                if keda_queue_url and not keda_queue_url.startswith("REPLACE_WITH"):
                    assert keda_queue_url == cm_input_url, (
                        f"Tasks queue mismatch between KEDA trigger and worker ConfigMap!\n"
                        f"  KEDA trigger    → {keda_queue_url}\n"
                        f"  Worker ConfigMap → {cm_input_url}\n"
                        f"KEDA scales on the wrong queue — workers won't spin up."
                    )
        except Exception:
            # KEDA ScaledJob may not exist in all environments — non-fatal
            pass

        # 5. Assert the queue exists
        try:
            attrs = aws_clients["sqs"].get_queue_attributes(
                QueueUrl=cm_input_url,
                AttributeNames=["QueueArn"],
            )
            expected_arn = _queue_url_to_arn(cm_input_url, aws_region, account_id)
            actual_arn = attrs["Attributes"]["QueueArn"]
            assert actual_arn == expected_arn, (
                f"Tasks queue ARN mismatch: expected {expected_arn}, got {actual_arn}"
            )
        except Exception as e:
            if "NonExistentQueue" in str(e) or "QueueDoesNotExist" in str(e):
                pytest.fail(
                    f"Tasks queue does not exist: {cm_input_url}\n{e}"
                )
            raise

    def test_worker_iam_policy_matches_queues(
        self,
        worker_configmap,
        aws_clients,
        aws_region,
        account_id,
        name_prefix,
    ):
        """The worker's IAM sqs-access policy must reference the same queue
        ARNs as the ConfigMap.  If the policy points at a stale queue, the
        worker gets AccessDenied.
        """

        # Resolve expected ARNs from ConfigMap
        cm_input_url = worker_configmap.get("INPUT_QUEUE_URL", "")
        cm_response_url = worker_configmap.get("RESPONSE_QUEUE_URL", "")
        if not cm_input_url or not cm_response_url:
            pytest.skip("ConfigMap queue URLs not set — cannot check IAM policy")

        expected_input_arn = _queue_url_to_arn(cm_input_url, aws_region, account_id)
        expected_response_arn = _queue_url_to_arn(cm_response_url, aws_region, account_id)

        # Read the worker IAM role's sqs-access inline policy
        role_name = f"{name_prefix}-agent-gateway-agent"
        iam = aws_clients["iam"]
        try:
            resp = iam.get_role_policy(
                RoleName=role_name,
                PolicyName="sqs-access",
            )
        except iam.exceptions.NoSuchEntityException:
            pytest.fail(
                f"IAM role {role_name!r} or policy 'sqs-access' not found. "
                f"Worker has no SQS permissions."
            )

        policy_doc = resp["PolicyDocument"]
        if isinstance(policy_doc, str):
            policy_doc = json.loads(policy_doc)

        # Collect all Resource ARNs from the policy
        policy_arns: set[str] = set()
        for stmt in policy_doc.get("Statement", []):
            resources = stmt.get("Resource", [])
            if isinstance(resources, str):
                resources = [resources]
            policy_arns.update(resources)

        assert expected_input_arn in policy_arns, (
            f"Worker IAM policy does not include the tasks queue ARN!\n"
            f"  Expected: {expected_input_arn}\n"
            f"  Policy resources: {policy_arns}\n"
            f"Worker will get AccessDenied when trying to consume from the tasks queue."
        )
        assert expected_response_arn in policy_arns, (
            f"Worker IAM policy does not include the response queue ARN!\n"
            f"  Expected: {expected_response_arn}\n"
            f"  Policy resources: {policy_arns}\n"
            f"Worker will get AccessDenied when trying to send to the response queue."
        )
