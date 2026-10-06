from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]


def test_pending_handoff_uses_only_exact_chat_fifo_and_trusted_gateway_role():
    policy = (ROOT / "modules/agent-factory/infra/gateway-chat-pending-access.tf").read_text()
    assert 'Action   = ["sqs:SendMessage"]' in policy
    assert "Resource = [aws_sqs_queue.chat_agent_tasks_fifo.arn]" in policy
    assert 'role       = "adp-${var.environment}-role-gateway-service"' in policy
    assert "*" not in policy and "ReceiveMessage" not in policy and "DeleteMessage" not in policy
    assert 'name        = "/adp/${var.environment}/agent-gateway/chat-input-queue-url"' in policy
    assert "value       = aws_sqs_queue.chat_agent_tasks_fifo.url" in policy
    config = yaml.safe_load((ROOT / "modules/gateway/k8s/configmap.yaml").read_text())
    assert config["data"]["ADP_CHAT_INPUT_QUEUE_URL"] == "__ADP_CHAT_INPUT_QUEUE_URL__"
    workflow = (ROOT / ".github/workflows/gateway-deploy.yml").read_text()
    assert 'ADP_CHAT_INPUT_QUEUE_URL=$(get_ssm "/adp/${ENVIRONMENT}/agent-gateway/chat-input-queue-url" "")' in workflow
    assert "s|__ADP_CHAT_INPUT_QUEUE_URL__|${ADP_CHAT_INPUT_QUEUE_URL}|g" in workflow
    for path in (ROOT / "modules/agent-factory/agent/k8s").glob("chat-sandbox*.yaml"):
        assert "ADP_CHAT_INPUT_QUEUE_URL" not in path.read_text()


def test_response_fifo_honors_partial_batch_failures():
    source = (ROOT / "modules/agent-factory/infra/modules/lambda-gateway/main.tf").read_text()
    mapping = source.split('resource "aws_lambda_event_source_mapping" "response_sqs" {', 1)[1].split("\nresource ", 1)[0]
    assert 'function_response_types = ["ReportBatchItemFailures"]' in mapping


def test_completion_write_is_limited_to_trusted_gateway_and_exact_owner_table():
    policy = (ROOT / "modules/agent-factory/infra/gateway-chat-completion-access.tf").read_text()
    assert 'Action   = ["dynamodb:UpdateItem", "dynamodb:ConditionCheckItem"]' in policy
    assert "Resource = [module.gateway_sessions.table_arn]" in policy
    assert 'role       = "adp-${var.environment}-role-gateway-service"' in policy
    assert "policy_arn = aws_iam_policy.gateway_chat_completion.arn" in policy
    assert "*" not in policy and "sqs:" not in policy
    assert "supervisor" not in policy and "sandbox" not in policy


def test_response_relay_configuration_matches_exact_queue_parameter():
    config = yaml.safe_load((ROOT / "modules/gateway/k8s/configmap.yaml").read_text())
    assert config["data"]["ADP_CHAT_RESPONSE_QUEUE_URL"] == "__ADP_CHAT_RESPONSE_QUEUE_URL__"
    workflow = (ROOT / ".github/workflows/gateway-deploy.yml").read_text()
    assert 'ADP_CHAT_RESPONSE_QUEUE_URL=$(get_ssm "/adp/${ENVIRONMENT}/agent-gateway/response-queue-url" "")' in workflow
    assert "s|__ADP_CHAT_RESPONSE_QUEUE_URL__|${ADP_CHAT_RESPONSE_QUEUE_URL}|g" in workflow
    policy = (ROOT / "modules/agent-factory/infra/gateway-chat-response-access.tf").read_text()
    assert 'name        = "/adp/${var.environment}/agent-gateway/response-queue-url"' in policy
    assert "value       = module.gateway_sqs.response_queue_url" in policy


def test_only_trusted_gateway_can_publish_to_exact_response_queue():
    policy = (ROOT / "modules/agent-factory/infra/gateway-chat-response-access.tf").read_text()
    assert 'Action   = ["sqs:SendMessage"]' in policy
    assert "Resource = [module.gateway_sqs.response_queue_arn]" in policy
    assert 'role       = "adp-${var.environment}-role-gateway-service"' in policy
    assert "policy_arn = aws_iam_policy.gateway_chat_response_publish.arn" in policy
    assert "*" not in policy
    assert "input_queue" not in policy
    assert "supervisor" not in policy
    sandbox = ROOT / "modules/agent-factory/agent"
    for path in [*sandbox.glob("k8s/chat-sandbox*.yaml"), sandbox / "src/complex-task-chat/sandbox-entrypoint.ts"]:
        assert "ADP_CHAT_RESPONSE_QUEUE_URL" not in path.read_text()
    inventory = (ROOT / "docs/architecture/assistant-runtime-access-inventory.md").read_text()
    assert "gateway-chat-response-access.tf" in inventory
    assert "authenticated owner cancellation are wired; final completion remains unfinished" in inventory
    assert "fences both pod binding and lease admission against cancellation races" in inventory
    assert "immutable `result_candidate`" in inventory
    assert "receipt explicitly remains nonterminal" in inventory
