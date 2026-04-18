# Agent Context Intelligence Platform

A unified Code Intelligence Platform that gives AI coding agents deep understanding of codebases, web documentation, and infrastructure. Agents interact with one MCP endpoint and get semantic search, code search, architectural documentation, structural analysis, infrastructure discovery, and persistent memory across all indexed repositories and web docs.

## Architecture

```
                           AI Coding Agents
               (Claude Code, Kiro, Cursor, ARC Runners)
                                 |
                     +-----------v-----------+
                     |  Context MCP Server   |  Single endpoint for agents
                     |  :5100                |  5 tools: search, understand,
                     |                       |  impact, browse, remember
                     +---+-------+-------+---+
                         |       |       |
           +-------------+       |       +-------------+
           v                     v                     v
+--------------------------------------------------------------------------+
|                    agent-context namespace (EKS)                          |
|                                                                          |
|  +---------------+  +---------------+  +---------------+                 |
|  |  OpenViking   |  |  Sourcebot    |  |  DeepWiki     |                 |
|  |  :1933        |  |  :3000        |  |  :8001        |                 |
|  |               |  |               |  |               |                 |
|  | - Semantic    |  | - Code search |  | - Rich repo   |                 |
|  |   search      |  |   (Zoekt)     |  |   wikis       |                 |
|  | - L0/L1       |  | - Regex       |  | - Diagrams    |                 |
|  |   summaries   |  | - Cross-repo  |  | - Arch docs   |                 |
|  | - Memory      |  |               |  |               |                 |
|  | - AGFS        |  | + Postgres    |  |               |                 |
|  |               |  | + Redis       |  |               |                 |
|  +-------+-------+  +---------------+  +-------+-------+                 |
|          |                                      |                        |
|          |           +------------------+       |                        |
|          +---------->|  LiteLLM Proxy   |<------+                        |
|                      |  :4000           |                                |
|                      |  Bedrock gateway:|                                |
|                      |  - Titan V2      |                                |
|                      |    (embeddings)  |                                |
|                      |  - Claude Sonnet |                                |
|                      |    (VLM + wiki)  |                                |
|                      |  - Claude Opus   |                                |
|                      |    (wiki LLM)    |                                |
|                      +--------+---------+                                |
|                               |                                          |
|  +---------------+            |         +-----------------------------+  |
|  | Gemma4 Router |            |         |  Ingestion CronJob          |  |
|  | :11434 (CPU)  |            |         |  (daily 6am UTC)            |  |
|  | Task          |            |         |  - refresh-repos.py         |  |
|  | complexity    |            |         |  - ingest-url.py            |  |
|  | classifier    |            |         |  - discover-infra.py        |  |
|  +---------------+            |         +-----------------------------+  |
|                               |                                          |
|  +-----------+  +-------------------------------+                        |
|  | EBS PVC   |  | S3 Files PVC (platform-data)  |                        |
|  | 200Gi     |  | Shared NFS via EFS CSI driver  |                        |
|  | OpenViking|  | - DeepWiki cache               |                        |
|  | data+AGFS |  | - code-indexes (JSON)          |                        |
|  +-----------+  | - ingestion state              |                        |
|                 +-------------------------------+                        |
+--------------------------------------------------------------------------+
                               |
                      AWS Bedrock (us-east-1)
                      AWS Secrets Manager
                      S3 (agent-context-platform-data)
```

**Key change from earlier architecture:** CodeGraphContext is no longer a running pod. The `cgc` tool now runs inline during ingestion (`ingest-repo.py`) to produce `code-index.json` files stored on the S3 Files PVC and uploaded to OpenViking.

## Ingestion Pipeline

Content enters the platform through three input files and a daily CronJob:

### Input files

| File | Format | What it feeds |
|------|--------|---------------|
| `index_content/repos.txt` | `org/repo` per line | 100 curated repos (aws-samples, awslabs, strands-agents, OSS agentic AI) |
| `index_content/urls.txt` | URL per line | Web documentation (Bedrock docs, Strands docs, MCP spec) |
| `index_content/accounts.txt` | `account_id:role:regions` per line | AWS accounts for infrastructure discovery |

### Per-repo pipeline (`ingest-repo.py`)

Each repo goes through a multi-step enrichment pipeline:

```
org/repo
  |
  1. POST to OpenViking  -->  clone, AST extraction, VLM summaries, embeddings
  |
  2. git clone --depth=1 to /tmp (for enrichment)
  |
  3. cgc analyze  -->  code-index.json  -->  write to S3 Files PVC + upload to OpenViking
  |                                          (symbols, imports, call graphs, language stats)
  4. DeepWiki API  -->  wiki.md  -->  upload to OpenViking
  |                                   (architecture docs, diagrams, component analysis)
  5. Cleanup temp clone
```

Usage:
```bash
python ingest-repo.py --repo org/repo --ov-url http://openviking:1933 --ov-key ROOT_KEY
python ingest-repo.py --repo org/repo --ov-url http://openviking:1933 --ov-key ROOT_KEY --skip-ov  # enrichment only
python ingest-repo.py --repo org/repo --ov-url http://openviking:1933 --ov-key ROOT_KEY --skip-cgc --skip-deepwiki  # OpenViking only
```

### Per-URL pipeline (`ingest-url.py`)

Web documentation is crawled and indexed into OpenViking:

```
https://docs.example.com/
  |
  1. Discover pages via /sitemap.xml (capped at --max-pages)
  |
  2. crawl4ai (or requests fallback)  -->  clean markdown
  |
  3. Upload to OpenViking at viking://resources/web/{domain}/{path}.md
```

Usage:
```bash
python ingest-url.py --url https://docs.aws.amazon.com/bedrock/latest/userguide/ --ov-url http://openviking:1933 --ov-key KEY
```

### Infrastructure discovery (`discover-infra.py`)

Discovers live AWS resources and IaC declarations:

```
accounts.txt
  |
  1. AWS Resource Explorer  -->  resource inventory per account
  |
  2. IaC parsing (Terraform, CloudFormation, CDK)  -->  infra-map.json per repo
  |
  3. CI/CD workflow parsing (.github/workflows)  -->  deploy-map.json per repo
  |
  4. Upload all to OpenViking at viking://resources/infra/{account}/
```

### Daily CronJob (`refresh-repos.py`)

Runs daily at 6am UTC. SHA-based incremental refresh that only re-processes what changed:

```
Daily at 06:00 UTC  (CronJob: ingestion-refresh)
  |
  1. For each repo in repos.txt:
  |    git ls-remote  -->  compare HEAD SHA with saved state
  |    If changed: re-run ingest-repo.py
  |    If repo has existing wiki: LLM-based incremental wiki update
  |    Tag repo with LLM-discovered topics
  |
  2. DeepWiki backfill: generate wikis for repos missing them
  |    (capped at 15 per run to stay within rate limits)
  |
  3. For each URL in urls.txt:
  |    HEAD request  -->  compare ETag/Last-Modified
  |    If changed: re-crawl via ingest-url.py
  |
  4. Save state to /platform-data/repo-state.json, url-state.json
```

Manual trigger:
```bash
kubectl create job --from=cronjob/ingestion-refresh manual-refresh -n agent-context
```

### GitHub Actions trigger (`ingest-content.yml`)

Pushing changes to `index_content/` on main auto-triggers ingestion:

```yaml
on:
  push:
    branches: [main]
    paths:
      - 'context_management/agent-context/index_content/**'
  workflow_dispatch:
    inputs:
      full_reindex: { type: boolean, default: false }
      ingest_urls: { type: boolean, default: false }
```

Runner: `arc-runner-org` (org-level ARC runner on EKS with IRSA for AWS access).

## MCP Tools Reference

The Context MCP Server exposes 5 tools. Each tool fans out to one or more backend services, merges results, and returns a unified response. Agents only need to know the tool names.

### `search(query, scope, limit)`

Find relevant code, documentation, and past learnings across all indexed repos and web docs.

| Parameter | Type | Description |
|-----------|------|-------------|
| `query` | string (required) | Natural language search query |
| `scope` | string (optional) | Filter: `all` (default), `code`, `docs`, `memory`, `web`, `infra` |
| `limit` | integer (optional) | Max results to return |

**Backends:** OpenViking `find()` (semantic) + Sourcebot `search()` (exact code matches via Zoekt)

**Example:**
```
search("how to implement retry with exponential backoff")
```
Returns: retry.py files, DeepWiki wiki sections on retry patterns, web docs on Bedrock retry configuration, and past agent learnings about retry fixes.

```
search("bedrock agent runtime API", scope="web")
```
Returns: crawled Bedrock documentation pages matching the query.

### `understand(target, depth)`

Get deep understanding of a specific repo, directory, or file.

| Parameter | Type | Description |
|-----------|------|-------------|
| `target` | string (required) | Repo path (`org/repo`), file path, or URI |
| `depth` | string (optional) | `overview` (default), `detailed`, or `full` |

**Backends:** OpenViking `overview()`/`abstract()` for L0/L1 summaries + code-index.json from S3 Files PVC for structural info (classes, functions, imports, dependencies)

**Example:**
```
understand("langchain-ai/langchain", depth="overview")
```
Returns: architecture overview, key components, how the retrieval pipeline works, language stats.

```
understand("aws-samples/bedrock-chat", depth="detailed")
```
Returns: L1 summary + structural info (classes, methods, imports, call graph) from code-index.

**Graceful degradation:** If code-index is not available for a repo, returns OpenViking data only. If L1 is not generated yet, returns L0 abstract or raw file list.

### `impact(target, cross_repo)`

Analyse what would be affected by changing a symbol, file, or pattern.

| Parameter | Type | Description |
|-----------|------|-------------|
| `target` | string (required) | Symbol name, file path, or pattern |
| `cross_repo` | boolean (optional) | Search across all repos (default: false) |

**Backends:** Code-index data for direct callers/dependents + Sourcebot `search()` for cross-repo grep

**Example:**
```
impact("RetryHandler", cross_repo=true)
```
Returns: "RetryHandler is called by 3 functions in payment-service, imported in 2 other repos, total blast radius: 5 files across 3 repos"

**Graceful degradation:** Falls back to Sourcebot grep if code-index is unavailable, or code-index only if Sourcebot is down. Returns partial results with a warning.

### `browse(action, uri, depth)`

Navigate the indexed content filesystem.

| Parameter | Type | Description |
|-----------|------|-------------|
| `action` | string (required) | `ls`, `tree`, `read`, `abstract`, `overview` |
| `uri` | string (required) | OpenViking URI or `org/repo/path` shorthand |
| `depth` | integer (optional) | Tree depth (for `tree` action) |

**Backends:** OpenViking filesystem API (`ls`, `tree`, `read`, `abstract`, `overview`)

**URI formats:** Accepts both `viking://resources/org/repo/path` and shorthand `org/repo/path` — the MCP server normalizes automatically.

**Example:**
```
browse(action="tree", uri="langchain-ai/langchain", depth=2)
```
Returns: top-level directory structure of the LangChain repo.

```
browse(action="overview", uri="aws-samples/bedrock-chat")
```
Returns: L1 structured summary (~2K tokens).

### `remember(session_id, messages, outcome)`

Save session context, decisions, and learnings to long-term memory.

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | string (required) | Unique session identifier |
| `messages` | array (required) | Conversation history |
| `outcome` | string (optional) | What was accomplished |

**Backends:** OpenViking session API (`add_message`, `used`, `commit`)

**Example:**
```
remember(
  session_id="fix-retry-bug",
  messages=[...conversation...],
  outcome="Fixed by adding circuit breaker pattern"
)
```
OpenViking extracts memories across 8 categories (profile, preferences, entities, events, cases, patterns, tools, skills). Future agents find these automatically via `search()`.

### Backend Service Matrix

| Tool | OpenViking | Sourcebot | Code-Index (S3 Files) | DeepWiki |
|------|:----------:|:---------:|:---------------------:|:--------:|
| search | Semantic search | Code search (Zoekt) | -- | Via indexed wikis in OV |
| understand | L0/L1 summaries | -- | Structural info | Via indexed wikis in OV |
| impact | -- | Cross-repo grep | Call graph, deps | -- |
| browse | Filesystem API | -- | -- | -- |
| remember | Session + memory | -- | -- | -- |

## Content Management

### Adding repos

1. Edit `index_content/repos.txt` — add `org/repo` (one per line, `#` for comments)
2. Push to main
3. `ingest-content.yml` auto-triggers: OpenViking ingest + cgc code-index + DeepWiki wiki
4. Daily CronJob will keep it updated via SHA-based incremental refresh

### Adding web documentation

1. Edit `index_content/urls.txt` — add the URL (sitemap discovery is automatic)
2. Push to main
3. `ingest-content.yml` auto-triggers crawling and upload to OpenViking

### Adding AWS accounts for infra discovery

1. Edit `index_content/accounts.txt` — add `account_id:role_name:regions`
2. The IAM role must exist in the target account and trust the platform's IRSA role
3. Required permissions: `resource-explorer-2:Search`, `tag:GetResources`, `cloudformation:ListStacks`

### Manual triggers

```bash
# Re-ingest everything
gh workflow run ingest-content.yml -f full_reindex=true

# Re-crawl all URLs
gh workflow run ingest-content.yml -f ingest_urls=true

# Trigger CronJob manually
kubectl create job --from=cronjob/ingestion-refresh manual-refresh -n agent-context
```

## Deployment

### Prerequisites

- EKS cluster with kubectl access
- IRSA service account with: S3, Secrets Manager, Bedrock InvokeModel, EFS CSI driver
- Bedrock model access: Titan V2 (embeddings) + Claude Sonnet 4.6 (VLM) + Claude Opus (wiki LLM)
- GitHub App credentials in Secrets Manager (`adp/gh-app-ops-id`, `adp/gh-app-ops-key`)

### Quick start

```bash
cd context_management/agent-context

# 1. Configure
cp config.env config.local.env
vim config.local.env   # Set cluster name, region, secrets

# 2. Deploy everything
./deploy.sh

# 3. Add repos to index
vim index_content/repos.txt
git add . && git commit -m "add repos" && git push
# GitHub Actions auto-triggers ingestion

# 4. Validate
./scripts/validate.sh
```

`deploy.sh` performs the following steps:
1. Configure kubectl for the EKS cluster
2. Deploy S3 Files storage infrastructure (Terraform + EFS CSI driver + PV/PVC)
3. Create service account, namespace, RBAC for ingestion pipeline
4. Deploy LiteLLM Proxy (Bedrock gateway)
5. Deploy OpenViking (semantic search + memory)
6. Deploy Sourcebot + Postgres + Redis (code search)
7. Deploy DeepWiki (wiki generation)
8. Deploy ingestion refresh CronJob (daily at 6am UTC)
9. Run `validate.sh` (10 checks)

### Teardown

```bash
# Remove deployments/services/cronjobs (preserves data)
./teardown.sh

# Remove everything including PVCs (data loss)
./teardown.sh --delete-pvcs

# Nuclear: delete entire namespace
./teardown.sh --delete-namespace
```

### Validation (`validate.sh`)

Runs 10 automated checks:

| Check | What it validates |
|-------|-------------------|
| 1. Pod Status | All expected deployments have ready replicas |
| 2. LiteLLM Proxy | Health endpoint, embedding endpoint, VLM endpoint, model list |
| 3. OpenViking | Health endpoint, embedding config, VLM config |
| 4. Ingestion CronJob | CronJob exists, schedule correct, last successful run |
| 5. Sourcebot | Health endpoint, Postgres accepting connections, Redis PONG |
| 6. DeepWiki | Deployment ready, health endpoint, LiteLLM proxy connectivity |
| 7. Cross-namespace DNS | Service DNS resolves from runner namespace |
| 8. Repo Ingestion | Ingestion jobs exist, latest job status |
| 9. S3 Files Storage | PVC bound, StorageClass exists, mounts accessible |
| 10. Ingestion RBAC | Role/RoleBinding exist, runner SA permissions verified |

## Configuration (`config.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `CLUSTER_NAME` | `github-arc-runner-eks` | EKS cluster name |
| `AWS_REGION` | `us-east-1` | AWS region |
| `NAMESPACE` | `agent-context` | K8s namespace |
| `SERVICE_ACCOUNT` | `agent-context-sa` | Service account (IRSA) |
| `STORAGE_CLASS` | `ebs-gp3` | Storage class for EBS PVCs |
| `S3_FILES_ENABLED` | `true` | Enable S3-backed file systems (EFS CSI) |
| `S3_FILES_BUCKET` | `agent-context-platform-data` | S3 bucket for platform data |
| `BEDROCK_EMBEDDING_MODEL` | `amazon.titan-embed-text-v2:0` | Embedding model (1024 dimensions) |
| `BEDROCK_VLM_MODEL` | `global.anthropic.claude-sonnet-4-6` | VLM model for summaries |
| `WIKI_LLM_MODEL` | `bedrock/global.anthropic.claude-opus-4-6-v1` | LLM for incremental wiki updates |
| `DEEPWIKI_ENABLED` | `true` | Enable/disable DeepWiki |
| `INGESTION_REFRESH_SCHEDULE` | `0 6 * * *` | CronJob schedule (daily 6am UTC) |
| `INGESTION_REFRESH_ENABLED` | `true` | Enable daily refresh CronJob |
| `RUNNER_NAMESPACE` | `arc-runners-org` | Namespace where ARC runners live |
| `RUNNER_LABEL` | `arc-runner-org` | GitHub Actions runner label |
| `GITHUB_APP_ID_SECRET` | `adp/gh-app-ops-id` | Secrets Manager: GitHub App ID |
| `GITHUB_APP_KEY_SECRET` | `adp/gh-app-ops-key` | Secrets Manager: GitHub App private key |

## Storage Architecture

| Storage | Type | Size | What it holds | Why this type |
|---------|------|------|---------------|---------------|
| OpenViking data | EBS PVC (`openviking-data`) | 200Gi gp3 | Vector index + AGFS (92K+ small files) | Block storage for random I/O — NFS/S3 not suitable for AGFS workload |
| Platform data | S3 Files PVC (`platform-data`) | Elastic (EFS/NFS) | DeepWiki cache, code-indexes (JSON), ingestion state | Shared ReadWriteMany, persistent across pod restarts, 292 MB/s write |
| Sourcebot data | EBS PVC | 100Gi | Zoekt code search indexes | Block storage for search performance |
| Sourcebot Postgres | EBS PVC | 20Gi | Sourcebot metadata database | Block storage for database I/O |
| Sourcebot Redis | EBS PVC | 10Gi | Sourcebot job queue and cache | Block storage for Redis persistence |

The S3 Files storage uses a Terraform module (in `terraform/`) that provisions:
- S3 bucket with versioning, KMS encryption, and Glacier lifecycle
- EFS file system with mount targets in 2 AZs
- NFS security group
- IRSA roles for the EFS CSI driver
- EFS CSI driver v3.0.0 as an EKS add-on

## Services

| Service | Port | What it does | Role in platform |
|---------|------|--------------|------------------|
| Context MCP Server | 5100 | Unified agent interface (5 tools) | The only endpoint agents need |
| OpenViking | 1933 | Semantic search, L0/L1 summaries, memory, AGFS | Core search + memory backend |
| Sourcebot | 3000 | Fast code search via Zoekt | Exact code matches, regex, cross-repo grep |
| DeepWiki | 8001 | Rich repo documentation with diagrams | Generates wikis during ingestion (indexed into OpenViking) |
| LiteLLM Proxy | 4000 | Bedrock gateway (Titan embeddings + Claude Sonnet/Opus) | All LLM calls route through here |
| Gemma4 Router | 11434 | Task complexity classifier (CPU) | Smart model routing for gateway |

## CI/CD Workflows

| Workflow | Trigger | What it does | Runner |
|----------|---------|--------------|--------|
| `build-deploy-context-platform.yml` | Push to main (platform files) | Build Docker images (Kaniko), deploy to EKS | `arc-runner-org` |
| `ingest-content.yml` | Push to main (index_content/), manual dispatch | Content ingestion: repos, URLs, infra | `arc-runner-org` |
| `test-context-mcp.yml` | PR / manual | MCP server integration tests | `arc-runner-org` |

## Quality Metrics

| Metric | Value |
|--------|-------|
| Repos indexed (OpenViking) | 95/98 (after repos.txt update to 100) |
| DeepWiki wikis generated | 10+ (growing daily via CronJob backfill, 15/run cap) |
| MCP tool score | 4.87/5 (61 queries across all 5 tools) |
| Daily incremental refresh | Operational (6am UTC, SHA-based) |
| Web docs indexed | 6 documentation sites (Bedrock, AgentCore, Strands, MCP spec) |
| Platform data persistence | Verified (S3 Files survives pod restarts) |

## Roadmap

- **#112: Compounding knowledge** — incremental wiki updates (LLM-based diff), discovery persistence, lint, topic indexes
- **#116: GraphRAG with Neptune Serverless** — knowledge graph, learning artifacts, `learn` tool

## Related Issues

| Issue | Description |
|-------|-------------|
| #42 | Initial platform deployment |
| #46 | Context MCP Server |
| #55 | Platform go-live |
| #76 | GitOps ingestion workflow |
| #78 | Platform fixes (Sourcebot auth, MCP rebuild) |
| #80 | L0/L1 generation fix |
| #81 | DeepWiki integration |
| #93 | DeepWiki on EKS |
| #96 | S3 Files storage migration (Terraform + EFS) |
| #98 | Deep platform test (107+ MCP queries) |
| #99 | Top 100 repos curation |
| #105 | CodeGraphContext pod removal (cgc runs during ingestion) |
| #112 | Compounding knowledge |
| #116 | GraphRAG with Neptune Serverless |
