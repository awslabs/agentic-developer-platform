# Agent Factory Test Suite

E2E test suite for the agent-gateway async delivery pipeline (WebSocket + SQS + KEDA worker).

## Quick Start

```bash
# Install dependencies
cd modules/agent-factory
uv sync --all-extras    # or: pip install -e ".[all]"

# Unit mode (default -- fast, no AWS, no cluster)
uv run pytest tests/ -v

# Just Lambda unit tests
uv run pytest tests/lambda/ -v

# Just worker unit tests
uv run pytest tests/worker/ -v

# Shell script checks
uv run pytest tests/test_scripts.py -v
```

## Live Mode

Requires a deployed environment. Set environment variables or ensure `terraform output -json` works in `modules/agent-factory/infra/`.

```bash
TEST_ENV=dev \
WS_URL=wss://8ea7pg40b7.execute-api.us-east-1.amazonaws.com/v1 \
COGNITO_USER_POOL_ID=us-east-1_JEhv9xSGG \
COGNITO_CLIENT_ID=6cg7ba3hb4v41vbhm0cg8pl17j \
COGNITO_AGENT_CLIENT_ID=378cm2jdj3rjt2os4cthub7267 \
TEST_USER_EMAIL=adp-test@example.com \
TEST_USER_PASSWORD=... \
KUBE_CONTEXT=arn:aws:eks:us-east-1:879318057152:cluster/adp-dev-eks-cluster \
TASKS_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/879318057152/adp-dev-agent-gateway-tasks \
RESPONSES_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/879318057152/adp-dev-agent-gateway-responses \
SESSIONS_TABLE=adp-dev-agent-gateway-sessions \
uv run pytest tests/ -v -m "live or not live_only"
```

## Workflow Tests

These create real GitHub issues and trigger ARC runners. Only run when explicitly testing the label-trigger path:

```bash
uv run pytest tests/ -v -m workflow
```

## Test Organization

| File | Tests | Mode | What it covers |
|------|-------|------|----------------|
| `tests/lambda/test_ingest_handler.py` | 5-11 | Unit | Ingest Lambda: $connect, direct_response, long_running, PR#9 regression, malformed payloads, classifier failure, $disconnect |
| `tests/lambda/test_response_handler.py` | 12-14 | Unit | Response Lambda: WS routing, stale connections, malformed messages |
| `tests/worker/test_sqs_consumer.py` | 15-17 | Unit | SQS consumer: message processing, persona loading, Bedrock failure handling |
| `tests/e2e/test_ws_auth.py` | 1-4 | Live | WebSocket auth: no token, expired JWT, valid user/agent JWTs |
| `tests/e2e/test_ws_roundtrip.py` | 18-20 | Live | Full round-trip: direct, long-running, concurrent connections |
| `tests/e2e/test_keda_scaler.py` | 21-24 | Live | KEDA: ScaledJob exists, trigger config, pod spawn, image pull |
| `tests/e2e/test_label_trigger.py` | 25 | Live+Workflow | GitHub Actions label dispatch |
| `tests/terraform/test_plan_clean.py` | 26-27 | Live | Terraform plan clean, expected outputs |
| `tests/test_scripts.py` | 28-29 | Unit | bash -n + shellcheck on all .sh files |

## Pytest Markers

| Marker | Description |
|--------|-------------|
| `unit` | Uses fixtures/mocks (default, no AWS) |
| `live` | Hits the deployed cluster; needs env vars |
| `live_only` | Only meaningful in live mode; skipped in unit |
| `workflow` | Triggers a real GitHub Actions workflow via `gh` |
| `kubectl` | Shells out to kubectl |

Default `pytest tests/` runs unit-only tests.

## Config Discovery

`tests/config.py` reads from:
1. `terraform output -json` in `modules/agent-factory/infra/` (preferred)
2. Environment variables (fallback)

Fails fast if required live config is missing.

## Dependencies

- `pytest`, `moto` (unit tests)
- `websockets` (live WS tests)
- `shellcheck-py` (script linting)
- `boto3` (AWS interactions)

## Notes

- Live tests 19, 21-24 require the **long-running worker image in ECR** and the **KEDA ScaledJob deployed**. These skip gracefully when absent.
- `scripts/verify-e2e.sh` is a legacy MCP Agent Mail script (not agent-gateway). The pytest suite here covers the agent-gateway pipeline.
