"""Offline supervisor-role boundary; installed IAM and admission require #6937."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ROLE = (ROOT / "infra/chat-worker-iam.tf").read_text()
OUTPUTS = (ROOT / "infra/chat-agent-infra.tf").read_text()


def block(kind: str, name: str) -> str:
    return ROLE.split(f'resource "{kind}" "{name}" {{', 1)[1].split('\nresource "', 1)[0]


def test_supervisor_can_only_assume_its_own_service_account():
    trust = block("aws_iam_role", "chat_supervisor")
    account = block("kubernetes_service_account", "chat_supervisor")
    assert "system:serviceaccount:${var.gateway_namespace}:adp-chat-supervisor" in trust
    assert '}:aud" = "sts.amazonaws.com"' in trust
    assert "data.aws_iam_role.keda_operator" not in trust
    assert 'adp-agent"' not in trust
    assert "permissions_boundary = var.automation_permissions_boundary_arn" in trust
    assert 'name      = "adp-chat-supervisor"' in account
    assert '"eks.amazonaws.com/role-arn" = aws_iam_role.chat_supervisor.arn' in account
    assert "value       = aws_iam_role.chat_supervisor.arn" in OUTPUTS


def test_supervisor_cannot_use_direct_owner_stores_or_dispatch_to_the_queue():
    policy = block("aws_iam_role_policy", "chat_supervisor")
    assert "role = aws_iam_role.chat_supervisor.id" in policy
    assert "Resource = aws_sqs_queue.chat_agent_tasks_fifo.arn" in policy
    for action in (
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:ChangeMessageVisibility",
        "sqs:GetQueueAttributes",
    ):
        assert f'"{action}"' in policy
    assert (
        '"arn:aws:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*/*/POST/agent/internal/v1/agent/chat/data/admit"'
        in policy
    )
    assert '"arn:aws:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*/*/POST/agent/internal/v1/agent/chat/data/exit"' in policy
    for action in (
        "bedrock:*",
        "dynamodb:*",
        "s3:*",
        "secretsmanager:*",
        "sqs:SendMessage",
        "sts:AssumeRole",
        "iam:PassRole",
    ):
        assert f'"{action}"' in policy.split('Sid      = "NoModelOrDirectOwnerStore"', 1)[1]
    assert policy.count('Effect   = "Allow"') + policy.count('Effect = "Allow"') == 2
    assert policy.count('Effect   = "Deny"') == 1
    assert "aws_iam_role_policy.gateway_agent_chat_dynamodb.policy" not in policy
    assert "aws_iam_role_policy.gateway_agent_chat_s3.policy" not in policy


def test_supervisor_pod_rbac_is_not_installed_before_enforcing_admission():
    account = block("kubernetes_service_account", "chat_supervisor")
    assert '"eks.amazonaws.com/role-arn" = aws_iam_role.chat_supervisor.arn' in account
    deploy_script = (ROOT / "agent/k8s/deploy-chat-scaledjob.sh").read_text()
    assert "chat-supervisor-rbac.yaml" not in deploy_script
    assert "chat-supervisor-pod-policy.yaml" not in deploy_script
    assert 'resource "kubernetes_role_binding" "chat_supervisor"' not in ROLE
    assert 'resource "kubernetes_cluster_role_binding" "chat_supervisor"' not in ROLE
