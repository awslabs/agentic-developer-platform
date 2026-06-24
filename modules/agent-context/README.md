# Agent Context Intelligence Platform

A unified code intelligence platform that gives AI coding agents deep understanding of codebases, web documentation, and infrastructure. Agents connect to one MCP endpoint and get semantic search, code search, architectural documentation, structural analysis, and persistent memory across all indexed content.

## What It Does

When an AI agent (Claude Code, Kiro, Cursor, or an ARC runner) needs to understand code it hasn't seen before, it calls the Context MCP Server. That single endpoint fans out to multiple specialized backends — semantic search, exact code search, wiki generation, and memory — then merges results into a unified response.

The platform indexes ~100 curated repositories, web documentation sites, and (optionally) live AWS infrastructure. Content stays fresh via a daily incremental refresh that only re-processes what changed.

> **Cost note:** This module is opt-in because the full stack (especially GraphRAG and wiki generation) costs ~$800/month idle. Deploy with `--agent-context-only` flag or set `AGENT_CONTEXT_ENABLED=true`.

### Deployment Cost Tiers

| Mode | Monthly Cost | What's Deployed |
|------|-------------|-----------------|
| **Personal-context-only** (`--personal-context-only`) | **~$280/mo** | OpenViking, LiteLLM proxy, Context MCP Server, synthesis CronJob |
| **Full stack** (default) | **~$800/mo** | Everything above + Sourcebot, DeepWiki, ingestion pipeline, KEDA workers |
| **Incremental** (adding personal context to existing full deploy) | **~$0–30/mo** | Synthesis CronJob only (OpenViking/LiteLLM already running) |

Use `--personal-context-only` when you only need per-user experiential memory (save/recall/synthesize) without the code-intelligence backends (code search, wiki generation, repository indexing).

## Architecture

```
                           AI Coding Agents
               (Claude Code, Kiro, Cursor, ARC Runners)
                                 │
                     ┌───────────▼───────────┐
                     │  Context MCP Server   │  Single endpoint for agents
                     │  :5100                │  5 tools: search, understand,
                     │                       │  impact, browse, remember
                     └───┬───────┬───────┬───┘
                         │       │       │
           ┌─────────────┘       │       └─────────────┐
           ▼                     ▼                     ▼
┌──────────────────────────────────────────────────────────────────────┐
│                    agent-context namespace (EKS)                      │
│                                                                      │
│  ┌───────────────┐  ┌───────────────┐  ┌───────────────┐            │
│  │  OpenViking   │  │  Sourcebot    │  │  DeepWiki     │            │
│  │  :1933        │  │  :3000        │  │  :8001        │            │
│  │               │  │               │  │               │            │
│  │ Semantic      │  │ Code search   │  │ Rich repo     │            │
│  │ search        │  │ (Zoekt)       │  │ wikis         │            │
│  │ L0/L1         │  │ Regex         │  │ Diagrams      │            │
│  │ summaries     │  │ Cross-repo    │  │ Arch docs     │            │
│  │ Memory (AGFS) │  │               │  │               │            │
│  └───────┬───────┘  └───────────────┘  └───────┬───────┘            │
│          │                                      │                    │
│          │           ┌──────────────────┐       │                    │
│          └──────────►│  LiteLLM Proxy   │◄──────┘                    │
│                      │  :4000           │                            │
│                      │                  │                            │
│                      │  Titan V2        │                            │
│                      │  (embeddings)    │                            │
│                      │  Claude Sonnet   │                            │
│                      │  (VLM + wiki)    │                            │
│                      │  Claude Opus     │                            │
│                      │  (wiki LLM)      │                            │
│                      └──────────────────┘                            │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │  Ingestion CronJob (daily 6am UTC)                           │    │
│  │  refresh-repos.py — SHA-based incremental refresh            │    │
│  │  ingest-url.py — web doc crawling                            │    │
│  │  discover-infra.py — AWS resource discovery                  │    │
│  └──────────────────────────────────────────────────────────────┘    │
│                                                                      │
│  Storage:                                                            │
│  ├── EBS PVC 200Gi (OpenViking data + AGFS)                         │
│  ├── S3 Files PVC via EFS (DeepWiki cache, code-indexes, state)     │
│  └── EBS PVCs for Sourcebot (100Gi indexes + 20Gi Postgres + Redis) │
└──────────────────────────────────────────────────────────────────────┘
                               │
                      AWS Bedrock (us-east-1)
```

### Backend Services

| Service | Port | Role |
|---------|------|------|
| Context MCP Server | 5100 | The only endpoint agents need. Fans out to backends, merges results. |
| OpenViking | 1933 | Semantic vector search, L0/L1 summaries, AGFS filesystem, session memory |
| Sourcebot | 3000 | Fast exact code search via Zoekt. Regex, cross-repo grep. |
| DeepWiki | 8001 | Generates rich architectural wikis with diagrams during ingestion |
| LiteLLM Proxy | 4000 | Routes all LLM/embedding calls to Bedrock (Titan V2, Claude Sonnet, Claude Opus) |

## MCP Tools

Agents interact with 5 tools. Each tool handles backend fan-out internally.

### `search(query, scope, limit)`

Find relevant code, documentation, and past learnings.

```
search("how to implement retry with exponential backoff")
search("bedrock agent runtime API", scope="web")
```

Backends: OpenViking semantic search + Sourcebot Zoekt code search. Scope options: `all`, `code`, `docs`, `memory`, `web`, `infra`.

### `understand(target, depth)`

Get deep understanding of a repo, directory, or file.

```
understand("langchain-ai/langchain", depth="overview")
understand("aws-samples/bedrock-chat", depth="detailed")
```

Backends: OpenViking L0/L1 summaries + code-index.json structural data (classes, functions, imports, call graphs).

### `impact(target, cross_repo)`

Analyze blast radius of changing a symbol, file, or pattern.

```
impact("RetryHandler", cross_repo=true)
```

Backends: Code-index call graph data + Sourcebot cross-repo grep.

### `browse(action, uri, depth)`

Navigate the indexed content filesystem.

```
browse(action="tree", uri="langchain-ai/langchain", depth=2)
browse(action="read", uri="org/repo/src/main.py")
```

Backends: OpenViking filesystem API. Actions: `ls`, `tree`, `read`, `abstract`, `overview`.

### `remember(session_id, messages, outcome)`

Save session context and learnings to long-term memory.

```
remember(session_id="fix-retry-bug", messages=[...], outcome="Fixed with circuit breaker")
```

Backends: OpenViking session API. Extracts memories across 8 categories. Future agents find these via `search()`.

## Ingestion Pipeline

Content enters through three input files and a daily CronJob:

### Input Files

| File | Format | What It Feeds |
|------|--------|---------------|
| `index_content/repos.txt` | `org/repo` per line | ~100 curated repos |
| `index_content/urls.txt` | URL per line | Web documentation sites |
| `index_content/accounts.txt` | `account_id:role:regions` | AWS infrastructure discovery |

### Per-Repo Pipeline

Each repo goes through multi-step enrichment:

```
org/repo
  │
  1. POST to OpenViking → clone, AST extraction, VLM summaries, embeddings
  │
  2. cgc analyze → code-index.json (symbols, imports, call graphs)
  │                → write to S3 Files PVC + upload to OpenViking
  │
  3. DeepWiki API → wiki.md (architecture docs, diagrams)
  │                → upload to OpenViking
  │
  4. Cleanup
```

### Daily Refresh (CronJob, 6am UTC)

SHA-based incremental — only re-processes repos whose HEAD changed:

1. `git ls-remote` each repo → compare SHA with saved state
2. Changed repos: full re-ingest
3. DeepWiki backfill: generate wikis for repos missing them (15/run cap)
4. Web docs: HEAD request → compare ETag → re-crawl if changed
5. Save state to `/platform-data/repo-state.json`

### GitHub Actions Trigger

Pushing changes to `index_content/` on main auto-triggers ingestion via `ingest-content.yml`.

## Deployment

### Automated (recommended)

```bash
# Deploy agent-context only (opt-in due to cost)
./platform/scripts/deploy-all.sh --agent-context-only

# Or enable as part of full deploy
AGENT_CONTEXT_ENABLED=true ./platform/scripts/deploy-all.sh
```

### Manual

```bash
cd modules/agent-context

# Configure
cp config.env config.local.env
# Edit: set CLUSTER_NAME, AWS_REGION, secrets paths

# Deploy everything (kubectl + Terraform + Helm)
./deploy.sh

# Deploy personal-context only (lean stack, ~$280/mo)
./deploy.sh --personal-context-only

# Validate (10 automated checks)
./scripts/validate.sh
```

#### `--personal-context-only` flag

Deploys the lean personal-context stack only:
- **Deployed**: OpenViking (vector + AGFS), LiteLLM proxy (embeddings + synthesis LLM), Context MCP Server (experience tool active), personal-context synthesis CronJob
- **Skipped**: Sourcebot, DeepWiki, OpenSearch Serverless, ingestion pipeline (repos/URLs/infra), KEDA workers
- **Conditional**: Neptune deployed only if `GRAPHRAG_ENABLED=true`

This reduces monthly cost from ~$800 to ~$280, suitable for operators who need personal context without code intelligence. Can also be set via environment variable: `PERSONAL_CONTEXT_ONLY=true ./deploy.sh`

`deploy.sh` performs:
1. Configure kubectl for EKS cluster
2. Deploy S3 Files storage (Terraform + EFS CSI driver + PV/PVC)
3. Create namespace, service account, RBAC
4. Deploy LiteLLM Proxy
5. Deploy OpenViking
6. Deploy Sourcebot + Postgres + Redis
7. Deploy DeepWiki
8. Deploy ingestion CronJob
9. Run validation

### Teardown

```bash
./teardown.sh                    # Remove deployments (preserves data)
./teardown.sh --delete-pvcs      # Remove everything including data
./teardown.sh --delete-namespace # Nuclear option
```

### Prerequisites

- EKS cluster with kubectl access
- IRSA service account with: S3, Secrets Manager, Bedrock InvokeModel, EFS CSI driver
- Bedrock model access: Titan V2 (embeddings), Claude Sonnet (VLM), Claude Opus (wiki)
- GitHub App credentials in Secrets Manager

## Storage Architecture

| Storage | Type | Size | Contents |
|---------|------|------|----------|
| OpenViking data | EBS PVC (gp3) | 200Gi | Vector index + AGFS (92K+ small files) |
| Platform data | S3 Files PVC (EFS) | Elastic | DeepWiki cache, code-indexes, ingestion state |
| Sourcebot indexes | EBS PVC | 100Gi | Zoekt code search indexes |
| Sourcebot Postgres | EBS PVC | 20Gi | Metadata database |
| Sourcebot Redis | EBS PVC | 10Gi | Job queue and cache |

## Content Management

```bash
# Add repos — edit index_content/repos.txt, push to main
# Add web docs — edit index_content/urls.txt, push to main
# Add AWS accounts — edit index_content/accounts.txt

# Manual re-index
gh workflow run ingest-content.yml -f full_reindex=true

# Manual CronJob trigger
kubectl create job --from=cronjob/ingestion-refresh manual-refresh -n agent-context
```

## Configuration (`config.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `CLUSTER_NAME` | `github-arc-runner-eks` | EKS cluster name |
| `NAMESPACE` | `agent-context` | Kubernetes namespace |
| `S3_FILES_BUCKET` | `agent-context-platform-data` | S3 bucket for platform data |
| `BEDROCK_EMBEDDING_MODEL` | `amazon.titan-embed-text-v2:0` | Embedding model |
| `BEDROCK_VLM_MODEL` | `global.anthropic.claude-sonnet-4-6` | VLM for summaries |
| `WIKI_LLM_MODEL` | `bedrock/global.anthropic.claude-opus-4-6-v1` | LLM for wiki generation |
| `DEEPWIKI_ENABLED` | `true` | Enable DeepWiki |
| `INGESTION_REFRESH_SCHEDULE` | `0 6 * * *` | CronJob schedule |

## Observability (Kill Switch & Troubleshooting)

The ingestion pipeline emits structured logs (JSON), distributed traces (OTel -> X-Ray),
and metrics. All telemetry follows a **fail-open discipline**: emission errors are
swallowed silently and never block ingestion.

### Kill switches

| Env var | Default | Effect |
|---------|---------|--------|
| `KNOWLEDGE_LAYER_TELEMETRY_ENABLED` | `true` | Master switch — disables ALL telemetry (logs revert to text, no traces, no metrics) |
| `KNOWLEDGE_LAYER_TRACES_ENABLED` | `true` | Disables trace export only (logs + metrics continue) |

### Emergency procedure

If telemetry is consuming excessive resources or causing issues:

1. **Immediate (no redeploy):** Set `KNOWLEDGE_LAYER_TELEMETRY_ENABLED=false` in the ConfigMap:
   ```bash
   kubectl -n agent-context edit configmap agent-context-config
   # Add: KNOWLEDGE_LAYER_TELEMETRY_ENABLED: "false"
   kubectl -n agent-context rollout restart deployment/context-mcp
   # ScaledJob pods pick up ConfigMap on next spawn (no restart needed)
   ```

2. **Verify disabled:** Check logs are plain text (not JSON):
   ```bash
   kubectl -n agent-context logs -l app=ingestion-worker --tail=5
   # Should show "2026-... [INFO] [sqs-worker] Processing: ..." (text format)
   ```

3. **Investigate root cause:** Check ADOT collector health:
   ```bash
   kubectl -n adp-agents logs -l app.kubernetes.io/name=adot-collector --tail=20
   ```

### Dashboard

CloudWatch dashboard: `adp-<env>-knowledge-layer`

### Correlation query

Find everything about one asset in CloudWatch Logs Insights:
```
SOURCE '/adp/dev/knowledge-layer/ingestion'
| filter asset_id = '<org/repo>'
| sort @timestamp asc
```

### Fail-open guarantee

The `test_telemetry_failopen.py` regression test validates that even with ALL
telemetry subsystems throwing exceptions (traces, logger formatter, correlation
context), the ingestion pipeline completes with correct artifacts. This test
must always pass — it is the safety net for the entire observability layer.

## Testing

```bash
cd modules/agent-context

# Unit tests (no AWS, no cluster)
uv run pytest tests/ -v

# Live E2E tests (against deployed cluster)
TEST_ENV=dev uv run pytest tests/ -v -m "live or not live_only"
```

## Observability (Kill Switch & Troubleshooting)

The ingestion pipeline emits structured logs (JSON), distributed traces (OTel/X-Ray), and metrics. All telemetry follows a **fail-open discipline**: emission errors are silently swallowed and never block ingestion.

### Kill switches

| Env var | Default | Effect |
|---------|---------|--------|
| `KNOWLEDGE_LAYER_TELEMETRY_ENABLED` | `true` | Master switch — disables ALL telemetry (logs revert to text, no traces, no metrics) |
| `KNOWLEDGE_LAYER_TRACES_ENABLED` | `true` | Disables trace export only (logs + metrics continue) |

### Emergency procedure

If telemetry is consuming excessive resources or causing issues:

1. **Immediate (no redeploy):** Set `KNOWLEDGE_LAYER_TELEMETRY_ENABLED=false` in the ConfigMap:
   ```bash
   kubectl -n agent-context edit configmap agent-context-config
   # Add: KNOWLEDGE_LAYER_TELEMETRY_ENABLED: "false"
   kubectl -n agent-context rollout restart deployment/context-mcp
   # ScaledJob pods pick up ConfigMap on next spawn (no restart needed)
   ```

2. **Verify disabled:** Check logs are plain text (not JSON):
   ```bash
   kubectl -n agent-context logs -l app=ingestion-worker --tail=5
   # Should show "2026-... [INFO] [sqs-worker] Processing: ..." (text format)
   ```

3. **Investigate root cause:** Check ADOT collector health:
   ```bash
   kubectl -n adp-agents logs -l app.kubernetes.io/name=adot-collector --tail=20
   ```

### Correlation query

Find everything about one asset in CloudWatch Logs Insights:
```
SOURCE '/adp/dev/knowledge-layer/ingestion'
| filter asset_id = '<org/repo>'
| sort @timestamp asc
```

### Fail-open guarantee

The `safe_emit()` utility in `telemetry.py` wraps every telemetry call so exceptions are caught and discarded. The regression test suite (`tests/unit/test_telemetry_failopen.py`) patches all telemetry subsystems to throw on every call, then verifies a full ingestion cycle completes with correct artifacts. This test runs in CI on every PR.

## Further Reading

- [tests/README.md](tests/README.md) — Full test suite documentation
- [index_content/README.md](index_content/README.md) — Content management guide
- Platform integration: `ARCHITECTURE.md` (repo root)
