# Agent Context E2E Test Suite

Reproducible test suite for the Agent Context Intelligence Platform.
Works in **unit mode** (fast, no AWS, no cluster) and **live mode** (deployed cluster).

## Quick Start

```bash
# Install dependencies
cd modules/agent-context
uv sync --all-extras  # or: pip install -e ".[all]"

# Unit mode (default - fast, no AWS, no cluster)
uv run pytest tests/ -v

# With shellcheck linting
pip install shellcheck-py
uv run pytest tests/ -v
```

## Live Mode

Runs against a deployed EKS cluster. Requires kubectl access and env vars:

```bash
TEST_ENV=dev \
NAMESPACE=agent-context \
KUBE_CONTEXT=arn:aws:eks:us-east-1:879318057152:cluster/adp-dev-eks-cluster \
OPENVIKING_URL=http://openviking.agent-context.svc.cluster.local:1933 \
MCP_URL=http://context-mcp.agent-context.svc.cluster.local:5100 \
OV_API_KEY=$(aws secretsmanager get-secret-value --secret-id adp/aws-e/openviking-root-key --query SecretString --output text) \
uv run pytest tests/ -v -m "live or not live_only"
```

## GraphRAG Tests

Only meaningful when Neptune + OpenSearch Serverless are enabled:

```bash
GRAPHRAG_ENABLED=true \
TEST_ENV=dev \
uv run pytest tests/ -v -m "graphrag"
```

## Pytest Markers

| Marker | Description |
|--------|-------------|
| `unit` | Uses fixtures/mocks (default) |
| `live` | Hits the deployed cluster; needs env vars |
| `live_only` | Only meaningful in live mode; skipped in unit |
| `graphrag` | Requires Neptune+OpenSearch; skipped unless `GRAPHRAG_ENABLED=true` |
| `workflow` | Triggers a real GitHub Actions workflow via `gh`; live only |
| `kubectl` | Shells out to kubectl; fails fast if not available |

Default: `pytest tests/` runs unit tests only. Add `-m "live or not live_only"` for live.

## Test Map

| File | Tests | Mode |
|------|-------|------|
| `e2e/test_platform_health.py` | Deployments, Services, PVCs, CronJobs, pod health, IRSA | Unit + Live |
| `e2e/test_mcp_endpoint.py` | /tools listing, search, remember, malformed JSON, auth | Unit + Live |
| `e2e/test_ingestion.py` | CronJob trigger, state persistence, SHA skip, new repo | Live only |
| `e2e/test_workflow_dispatch.py` | GitHub Actions workflow_dispatch | Live only |
| `e2e/test_graphrag.py` | Neptune, OpenSearch, GraphRAG search | GraphRAG only |
| `terraform/test_plan_clean.py` | Plan clean, outputs defined | Unit (outputs.tf) + Live (plan) |
| `test_scripts.py` | bash -n, shellcheck, validate.sh | Unit + Live |

## Config Discovery

Tests read configuration from a single source (priority order):
1. Environment variables (e.g., `MCP_URL`, `NAMESPACE`)
2. Terraform output (`terraform output -json` in `terraform/`)
3. Defaults

See `tests/config.py` for the full config model.

## Relationship to validate.sh

The existing `scripts/validate.sh` is **retired** in favor of this pytest suite.
`test_platform_health.py` covers all 10 of its checks with better structure,
parameterization, and CI integration. The script remains in-tree for backward
compatibility but is no longer the primary validation tool.
