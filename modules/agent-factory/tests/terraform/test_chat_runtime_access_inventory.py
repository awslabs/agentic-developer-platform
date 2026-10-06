"""Check that the SEC01 source audit remains aligned with runtime inputs."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
INVENTORY = ROOT / "docs/architecture/assistant-runtime-access-inventory.md"
ROLE = ROOT / "modules/agent-factory/infra/chat-worker-iam.tf"
SERVICE_ACCOUNT = ROOT / "modules/agent-factory/infra/gateway-main.tf"
JOB = ROOT / "modules/agent-factory/agent/k8s/chat-scaledjob.yaml"
QUERY = ROOT / "modules/agent-factory/agent/src/complex-task-chat/run-query.ts"
ROUTING = ROOT / "modules/agent-factory/agent/src/complex-task-chat/bedrock-routing.ts"


def test_inventory_tracks_service_role_grants():
    role = ROLE.read_text()
    inventory = INVENTORY.read_text()
    runtime = role.split('resource "aws_iam_role" "chat_worker"', 1)[1].split('resource "aws_iam_role" "chat_supervisor"', 1)[0]
    assert not re.search(r"= aws_iam_role_policy\.\w+\.policy", runtime)
    assert 'Action   = ["sqs:GetQueueAttributes"]' in runtime
    assert "queue-depth only, with explicit model, owner-store and gateway denies" in re.sub(r"\s+", " ", inventory)
    assert "sts:AssumeRoleWithWebIdentity" in role
    assert "sts:AssumeRole" in role
    assert '"eks.amazonaws.com/role-arn" = aws_iam_role.chat_worker.arn' in SERVICE_ACCOUNT.read_text()


def test_inventory_tracks_pod_mounts_and_retired_tool_entrypoint():
    job = JOB.read_text()
    query = QUERY.read_text()
    routing = ROUTING.read_text()
    inventory = INVENTORY.read_text()
    assert "serviceAccountName: adp-agent" in job
    assert "audience: adp-agent-bootstrap" in job
    assert "automountServiceAccountToken: false" not in job
    assert 'ADP_CHAT_DATA_ENABLED: "false"' in job
    assert "kind: NetworkPolicy" not in job
    assert "'Bash', 'Read', 'Write'" in query
    assert "permissionMode: 'bypassPermissions'" in query
    assert "Credentialed chat model routing retired" in routing
    assert "spawn(" not in routing
    assert "runQuery" not in (ROOT / "modules/agent-factory/agent/src/complex-task-chat/complex-task-chat-agent.ts").read_text()
    for probe in ("`/proc`", "STS", "S3", "DynamoDB", "Secrets Manager",
                  "Kubernetes API", "node metadata", "background processes"):
        assert probe in inventory


def test_inventory_separates_source_findings_from_live_evidence():
    inventory = INVENTORY.read_text()
    assert "source audit" in inventory
    assert "not an observation of a running pod" in inventory
    assert "#6937" in inventory
