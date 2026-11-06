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
        '"arn:aws:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*/*/POST/internal/v1/agent/chat/data/admit"'
        in policy
    )
    assert (
        '"arn:aws:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*/*/POST/internal/v1/agent/chat/data/exit"'
        in policy
    )
    for path in ("reserve", "resume", "teardown", "finalize", "complete", "session/commit"):
        assert (
            '"arn:aws:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*/*/POST/internal/v1/agent/chat/data/'
            + path
            + '"'
        ) in policy
    assert "/chat/data/*" not in policy
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
    rbac = deploy_script.index('kubectl apply -f "${SCRIPT_DIR}/chat-supervisor-rbac.yaml"')
    for policy in (
        "chat-supervisor-pod-policy.yaml",
        "chat-sandbox-admission.yaml",
        "chat-crew-pod-admission.yaml",
    ):
        assert deploy_script.index(f'kubectl apply -f "${{SCRIPT_DIR}}/{policy}"') < rbac
    assert 'resource "kubernetes_role_binding" "chat_supervisor"' not in ROLE
    assert 'resource "kubernetes_cluster_role_binding" "chat_supervisor"' not in ROLE


def test_gateway_workload_verifier_rbac_is_applied_with_the_supervisor_rbac():
    """The gateway admits sandbox/Crew pods only after reading them from the API server
    (src/agentauth/workload.py). chat-model-rbac.yaml grants that read to the gateway
    service account; it existed in the repository but nothing applied it (dev 2026-10-17)."""
    deploy_script = (ROOT / "agent/k8s/deploy-chat-scaledjob.sh").read_text()
    supervisor = deploy_script.index('kubectl apply -f "${SCRIPT_DIR}/chat-supervisor-rbac.yaml"')
    verifier = deploy_script.index('kubectl apply -f "${SCRIPT_DIR}/chat-model-rbac.yaml"')
    assert supervisor < verifier
    import yaml

    docs = list(yaml.safe_load_all((ROOT / "agent/k8s/chat-model-rbac.yaml").read_text()))
    role = next(d for d in docs if d["kind"] == "Role")
    binding = next(d for d in docs if d["kind"] == "RoleBinding")
    assert role["metadata"]["namespace"] == "adp-gateway-agents"
    assert {r["resources"][0] for r in role["rules"]} == {"pods", "jobs"}
    assert all(r["verbs"] == ["get"] for r in role["rules"])
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "gateway-service", "namespace": "adp-gateway"}
    ]
