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
        inherited = re.findall(r'aws_iam_role_policy\.(\w+)\.policy', runtime)
        self.assertEqual(len(inherited), 8)
        sources = self.gateway + (INFRA / "chat-agent-infra.tf").read_text()
        for name in inherited:
            self.assertNotIn('bedrock:', resource(sources, "aws_iam_role_policy", name))
        deny = resource(self.chat, "aws_iam_role_policy", "chat_worker_deny_direct_bedrock")
        self.assertIn('"Deny"', deny)
        self.assertIn('"bedrock:InvokeModel"', deny)
        self.assertIn('"bedrock:InvokeModelWithResponseStream"', deny)
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


if __name__ == "__main__":
    unittest.main()
