"""Static retirement boundary for the credentialed legacy chat worker."""

from pathlib import Path
import os
import subprocess


ROOT = Path(__file__).resolve().parents[2]
ROLE = (ROOT / "infra/chat-worker-iam.tf").read_text()
WORKER = (ROOT / "agent/src/complex-task-chat/complex-task-chat-agent.ts").read_text()


def policy(name):
    return ROLE.split(f'resource "aws_iam_role_policy" "{name}" {{', 1)[1].split('\nresource "', 1)[0]


def test_worker_has_no_model_owner_store_gateway_or_response_grants():
    old_grants = ROLE.split('resource "aws_iam_role" "chat_supervisor"', 1)[0]
    for reference in (
        "gateway_agent_execute_api.policy",
        "gateway_agent_chat_dynamodb.policy",
        "gateway_agent_chat_s3.policy",
        "gateway_agent_chat_sqs_fifo.policy",
    ):
        assert reference not in old_grants
    queue = policy("chat_worker_queue_depth")
    assert 'Action   = ["sqs:GetQueueAttributes"]' in queue
    assert "Resource = aws_sqs_queue.chat_agent_tasks_fifo.arn" in queue
    assert queue.count('Effect   = "Allow"') == 1
    denied = policy("chat_worker_deny_direct_authority")
    assert 'Effect = "Deny"' in denied
    assert 'Resource = "*"' in denied
    for action in (
        "bedrock:*", "dynamodb:*", "s3:*", "secretsmanager:*", "kms:*",
        "execute-api:Invoke", "sts:AssumeRole", "iam:PassRole",
        "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:SendMessage",
    ):
        assert f'"{action}"' in denied


def test_worker_entrypoint_refuses_before_queue_receipt_and_direct_store_fallback():
    entry = WORKER.split("export async function main(): Promise<void> {", 1)[1].split("\n}", 1)[0]
    assert "Credentialed chat worker retired" in entry
    assert "new SqsClient()" not in entry
    builder = WORKER.split("export async function buildChatStores(", 1)[1].split("export async function main", 1)[0]
    assert "runQuery" not in WORKER and "vaultToolsForTurn" not in WORKER
    assert "Credentialed chat worker cannot build direct owner stores" in builder
    assert "buildContextManager(env);" not in builder
    assert "buildMemoryProvider(env);" not in builder
    assert "buildArtifactStore(env);" not in builder


def test_retired_scaledjob_launcher_refuses_before_aws_or_kubernetes_access():
    script = ROOT / "agent/k8s/deploy-chat-scaledjob.sh"
    result = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, check=False,
        env={"PATH": os.environ["PATH"]},
    )
    assert result.returncode == 1
    assert "Credentialed chat ScaledJob retired" in result.stderr
    assert "ENVIRONMENT is required" not in result.stderr
    source = script.read_text()
    assert source.index("exit 1") < source.index("aws ssm get-parameter")
    assert source.index("exit 1") < source.index("kubectl apply")
