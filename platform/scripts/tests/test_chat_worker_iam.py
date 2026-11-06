"""Regression checks for chat/Python worker identity separation (no AWS calls)."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[3]
INFRA = ROOT / "modules/agent-factory/infra"


def resource(text, kind, name):
    start = text.index(f'resource "{kind}" "{name}" {{')
    tail = text[start:]
    return tail.split('\nresource ', 1)[0]


class ChatWorkerIamTests(unittest.TestCase):
    def setUp(self):
        self.gateway = (INFRA / "gateway-main.tf").read_text()
        self.chat = (INFRA / "chat-worker-iam.tf").read_text()

    def test_chat_cannot_assume_python_bedrock_role(self):
        trust = resource(self.gateway, "aws_iam_role", "gateway_agent")
        self.assertIn(':adp-gateway-worker"', trust)
        self.assertNotIn(':adp-agent"', trust)
        chat_trust = resource(self.chat, "aws_iam_role", "chat_worker")
        self.assertIn(':adp-agent"', chat_trust)
        self.assertNotIn(':adp-gateway-worker"', chat_trust)

    def test_chat_has_no_bedrock_allow_and_explicit_deny(self):
        runtime = resource(self.chat, "aws_iam_role_policy", "chat_worker_runtime")
        self.assertNotRegex(runtime, r'aws_iam_role_policy\.(\w+)\.policy')
        self.assertIn('var.chat_legacy_upgrade_policies', runtime)
        self.assertRegex(self.chat, r'variable "chat_legacy_upgrade_policies"[^}]+default\s*=\s*\{\}')
        deny = resource(self.chat, "aws_iam_role_policy", "chat_worker_deny_direct_authority")
        for action in ('"bedrock:*"', '"sqs:ReceiveMessage"', '"dynamodb:*"'):
            self.assertIn(action, deny)
        self.assertIn('"Deny"', deny)
        self.assertIn('aws_iam_role.chat_worker.id', deny)

    def test_workloads_use_separate_roles_and_keda_can_poll_both(self):
        sa = resource(self.gateway, "kubernetes_service_account", "gateway_agent")
        self.assertIn('aws_iam_role.chat_worker.arn', sa)
        self.assertIn('name      = "adp-agent"', sa)
        python_sa = resource(self.chat, "kubernetes_service_account", "python_gateway_worker")
        self.assertIn('aws_iam_role.gateway_agent.arn', python_sa)
        self.assertIn('name      = "adp-gateway-worker"', python_sa)
        for path, name in [
            ("agent/k8s/chat-scaledjob.yaml", "adp-agent"),
            ("gateway/k8s/keda-scaledjob.yaml", "adp-gateway-worker"),
        ]:
            text = (ROOT / "modules/agent-factory" / path).read_text()
            self.assertIn(f"serviceAccountName: {name}\n", text)
        self.assertIn('Resource = [aws_iam_role.gateway_agent.arn, aws_iam_role.chat_worker.arn]', self.gateway)
        self.assertIn('"bedrock:InvokeModel"', resource(self.gateway, "aws_iam_role_policy", "gateway_agent_bedrock"))

    def test_chat_sandbox_service_account_has_no_aws_role_or_default_token(self):
        sandbox = resource(self.chat, "kubernetes_service_account", "chat_sandbox")
        self.assertIn('name      = "adp-chat-sandbox"', sandbox)
        self.assertIn('automount_service_account_token = false', sandbox)
        self.assertNotIn('eks.amazonaws.com/role-arn', sandbox)
        self.assertNotIn('aws_iam_role.', sandbox)
        self.assertNotIn('adp-chat-sandbox', resource(self.chat, "aws_iam_role", "chat_worker"))
        self.assertNotIn('adp-chat-sandbox', resource(self.gateway, "aws_iam_role", "gateway_agent"))
        self.assertIn('serviceAccountName: adp-agent', (ROOT / "modules/agent-factory/agent/k8s/chat-scaledjob.yaml").read_text())

    def test_supervisor_scaler_uses_existing_queue_depth_only_role(self):
        outputs = (INFRA / "chat-agent-infra.tf").read_text()
        self.assertRegex(outputs, r'output "chat_scaler_role_arn" \{[^}]+value\s*=\s*aws_iam_role.chat_worker.arn')
        polling = resource(self.chat, "aws_iam_role_policy", "chat_worker_queue_depth")
        self.assertIn('Action   = ["sqs:GetQueueAttributes"]', polling)
        self.assertIn('Resource = aws_sqs_queue.chat_agent_tasks_fifo.arn', polling)
        self.assertIn('role = aws_iam_role.chat_worker.id', polling)
        trust = resource(self.chat, "aws_iam_role", "chat_worker")
        self.assertIn('Principal = { AWS = data.aws_iam_role.keda_operator.arn }', trust)
        self.assertIn('Action    = "sts:AssumeRole"', trust)
        operator = resource(self.gateway, "aws_iam_role_policy", "keda_operator_gateway_sqs")
        self.assertIn('aws_iam_role.chat_worker.arn', operator)
        self.assertNotIn('aws_iam_role.chat_supervisor.arn', operator)

    def test_supervisor_may_invoke_exactly_the_internal_chat_routes_it_calls_through_the_api_stage(self):
        policy = resource(self.chat, "aws_iam_role_policy", "chat_supervisor")
        supervisor = (ROOT / "modules/agent-factory/agent/src/complex-task-chat/sandbox-supervisor-admission.ts").read_text()
        called = set(re.findall(r"'(/internal/v1/agent/chat/[a-z/-]+)'", supervisor))
        self.assertIn("/internal/v1/agent/chat/data/admit", called)
        self.assertIn("/internal/v1/agent/chat/crew-run/ready", called)
        granted = re.findall(r'"arn:aws:execute-api:\$\{var\.aws_region\}:\$\{data\.aws_caller_identity\.current\.account_id\}:\*/\*/(\w+)(/[^"]+)"', policy)
        self.assertEqual({method for method, _ in granted}, {"POST"})
        # The API's /internal/{proxy+} method preserves the path; /agent/{proxy+} strips
        # its prefix, so a grant on /agent/internal/... never matches a signed request.
        self.assertEqual({path for _, path in granted}, called)
        self.assertNotIn("/POST/agent/internal/", policy)
        self.assertNotIn('"arn:aws:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*/*/*', policy)


if __name__ == "__main__":
    unittest.main()
