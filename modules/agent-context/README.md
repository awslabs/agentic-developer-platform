# Agent Context — the Knowledge Layer

A unified, multi-tenant code intelligence platform that gives AI coding agents deep understanding of codebases, web documentation, and infrastructure. Agents connect to **one MCP endpoint** (the "Door") and get semantic search, exact code search, structural analysis, architectural wikis, and persistent per-user memory across all indexed content — scoped to the caller's tenant, projects, and identity.

This module is the implementation of the **Knowledge Layer** (EPIC #1345). Design-of-record lives in [`docs/agent-context/knowledge-layer-design.md`](../../docs/agent-context/knowledge-layer-design.md); product vision in [`docs/agent-context/knowledge-layer-product-vision.md`](../../docs/agent-context/knowledge-layer-product-vision.md).

## Vision

> **Every agent and engineer working in our codebase should be able to ask any question about it — "what does this do?", "what breaks if I change this?", "which repos are exposed to this CVE?" — and get a correct, permission-safe answer in seconds, across every repository, without reading the code first. And beyond answering: the platform should _act_ on what it knows — finding vulnerabilities across the whole corpus and fixing them with its own agents, then remembering what worked.**

The Knowledge Layer turns a pile of repositories into a **queryable, reasoning substrate** that humans and ADP's autonomous agents share — the difference between agents that grep blindly and agents that *understand* the code they're changing. It exists because as a codebase grows past what one person can hold in their head, three things break: **comprehension doesn't scale** (answering "how does auth work here?" means reading code across repos), **change is blind** ("what breaks if I touch this?" gets discovered in production, not before the PR), and **security is reactive** (a new CVE means a frantic manual repo-by-repo audit). The common root cause is that the knowledge is latent in the code but not *queryable*.

**ADP is not a code-search tool — it is an autonomous code-intelligence and vulnerability-management platform.** Code search is table stakes; the product is what becomes possible once the codebase is queryable:

- **Agents that understand before they act** — fewer wrong edits, less wasted reasoning, higher first-pass PR quality.
- **Blast-radius before the PR, not after the incident.**
- **A vulnerability that fixes itself** — detect a CVE → reverse-index finds every affected repo/file → reachability triage drops false positives → a fix issue is filed → ADP's developer agents fix it and run the tests → a PR is opened (never auto-merged) → the verified outcome is remembered. Across the whole corpus, autonomously.

**The moat is outcome-verified experience on top of a competent retriever.** Retrieval quality is becoming a commodity; our defensible position is orthogonal — an **Experience layer** that remembers what actually *worked*, backed by substrate proof (tests passed, deploy succeeded, CVE closed), and matures proven procedures into reusable workflows. Retrieval tells an agent *where to look*; verified experience tells it *what has worked here before*. It compounds with every green deploy, is proprietary by construction (built from our outcomes on our corpus), and is the bridge from "answers questions" to "does work."

**Design principles** (the constraints that shape everything below):
1. **Permission-safe or it doesn't ship** — every answer is ACL-filtered at the single query surface; an unknown caller sees nothing (fail-closed).
2. **Permissively licensed, end to end** — Apache-2.0 / MIT only (the reason Sourcebot, OpenViking, Redis, Neo4j, and Grype were all replaced); no AGPL/GPL/BSL/SSPL anywhere.
3. **Portable, not welded to ADP** — header-based pluggable identity, talks to AWS services directly; runnable as "a few Lambdas + Fargate + S3 via one Terraform module."
4. **Structural over semantic for code** — bet on AST/structural understanding (whole functions, not broken chunks); reserve embeddings for genuine vocabulary-mismatch cases (wiki/doc prose, NL questions).
5. **Act, don't just answer** — retrieval that stops at a result list is half a product.

See [`docs/agent-context/knowledge-layer-product-vision.md`](../../docs/agent-context/knowledge-layer-product-vision.md) for the full vision (personas, success measures, roadmap, non-goals).

## What It Does

When an AI agent (Claude Code, Kiro, Cursor, or an ARC runner) needs to understand code it hasn't seen before, it calls the **Context MCP Server** (a.k.a. the Door) on `:5100`. That single endpoint:

1. **Extracts the caller's identity** from request headers (GitHub login/teams + tenant/owner).
2. **Resolves what the caller is allowed to see** (tenant isolation + optional project scoping).
3. **Fans out** to the specialized backends (vector search, Zoekt code search, structural code-index, Neptune graph, wikis, memory).
4. **Merges and filters** results through the ACL before returning.

The platform indexes curated repositories, web documentation sites, and (optionally) live AWS infrastructure. Content stays fresh via an incremental refresh that only re-processes what changed, and operators/users can self-register content through the **Knowledge Asset Registry** REST API + management UI.

> **Cost note:** This module is opt-in. Idle infrastructure cost is dominated by Neptune Serverless (min-capacity floor) and the EKS compute for the backend pods; the dominant *variable* cost is Bedrock usage during ingestion (wiki generation, embeddings, GraphRAG extraction, vuln scanning), which scales with corpus size and re-index frequency. OpenSearch Serverless (`graphrag_enabled`) is disabled by default and is by far the most expensive component if turned on. Deploy with `--agent-context-only`, or the lean `--personal-context-only` tier. See [Deployment Cost Tiers](#deployment-cost-tiers).

## Architecture

```
                           AI Coding Agents
               (Claude Code, Kiro, Cursor, ARC Runners)
                                 │  identity headers:
                                 │  X-GitHub-Login / X-GitHub-Teams
                                 │  X-Tenant-Id / X-Owner-Sub  (+ optional project)
                     ┌───────────▼────────────┐
                     │  Context MCP Server     │  The "Door" — single endpoint
                     │  (context-mcp) :5100    │  6 tools: search, understand,
                     │  identity → ACL filter  │  impact, browse, remember,
                     │  → fan-out → merge      │  experience
                     └──┬────┬────┬────┬────┬──┘
          ┌─────────────┘    │    │    │    └──────────────┐
          ▼                  ▼    ▼    ▼                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                       agent-context namespace (EKS)                        │
│                                                                            │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐   │
│  │  Zoekt       │  │  DeepWiki    │  │  LiteLLM     │  │  codegraph   │   │
│  │  :6070       │  │  :8001       │  │  Proxy :4000 │  │  (CGC)       │   │
│  │              │  │              │  │              │  │              │   │
│  │ Exact/regex  │  │ Architectural│  │ Bedrock      │  │ Structural   │   │
│  │ code search  │  │ wikis +      │  │ embeddings + │  │ code-index   │   │
│  │ cross-repo   │  │ diagrams     │  │ LLM routing  │  │ symbols/     │   │
│  │              │  │              │  │              │  │ call graphs  │   │
│  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────┘   │
│                                                                            │
│  ┌──────────────────────────┐   ┌──────────────────────────────────────┐  │
│  │  Semantic search          │   │  Ingestion (KEDA ScaledJob, SQS-fed) │  │
│  │  S3 Vectors (per-tenant   │   │  + daily refresh CronJob             │  │
│  │  index, embeddings)       │   │  + personal-context synthesis CronJob│  │
│  └──────────────────────────┘   └──────────────────────────────────────┘  │
│                                                                            │
│  State / storage:                                                          │
│  ├── Postgres (agent_context DB): repositories, projects,                  │
│  │     project_repositories, knowledge_assets, index_runs   ── ACL source  │
│  ├── S3 (content) + S3 Vectors (embeddings, per-tenant index)              │
│  ├── DynamoDB (adp-context-service-state): ingestion run state             │
│  └── Neptune Serverless (optional, GRAPHRAG_ENABLED): code/IaC graph       │
└──────────────────────────────────────────────────────────────────────────┘
                                 │
              AWS Bedrock (us-east-1) · CloudWatch (OTel logs/traces/metrics)
```

> **Note on names:** OpenViking (the former semantic-search + AGFS memory backend) was **removed in #1387** — semantic search now runs on **S3 + S3 Vectors**, and memory/experience are Postgres + S3 backed. "Sourcebot" was replaced by a plain **Zoekt** webserver. The deployed Kubernetes objects are named `context-mcp`, `zoekt`, `deepwiki`, `litellm-proxy`, `codegraph` (see `manifests/`).

### Backend Services

| Service (k8s name) | Port | Role |
|---|---|---|
| Context MCP Server (`context-mcp`) | 5100 | The "Door" — the only endpoint agents need. Identity extraction, ACL/tenant/project filtering, backend fan-out, result merge. |
| Zoekt (`zoekt`) | 6070 | Fast exact/regex code search, cross-repo. |
| DeepWiki (`deepwiki`) | 8001 | Generates architectural wikis with diagrams during ingestion. |
| LiteLLM Proxy (`litellm-proxy`) | 4000 | Routes all embedding/LLM calls to Bedrock (Titan V2 embeddings; Claude Sonnet/Haiku/Opus per task tier). |
| codegraph (`codegraph` / CGC) | — | Structural code-index extraction (symbols, imports, call graphs) consumed by `understand`/`impact`. |
| Semantic search | — | S3 Vectors per-tenant index (no standalone pod; called by the Door). |

## Multi-Tenancy, Identity & Scoping

The Knowledge Layer is multi-tenant. Every request is scoped by the caller's identity; the Door **fails closed** — missing or partial identity returns empty results, never a leak.

### Identity headers

The Door reads these request headers (set by the trusted ingress / sidecar; a NetworkPolicy prevents external injection):

| Header | Meaning |
|---|---|
| `X-GitHub-Login` | Caller's GitHub login |
| `X-GitHub-Teams` | Comma-separated team slugs |
| `X-Tenant-Id` | Caller's tenant (org) |
| `X-Owner-Sub` | Caller's individual subject id (per-user scope) |

### Visibility model (E8 #1721)

The `repositories` table carries nullable `tenant_id` and `owner_sub` columns. The ACL filter (`door/acl.py`) resolves which repos a caller may see:

1. **Shared** (`tenant_id IS NULL`) — visible to any caller whose principals match the repo ACL (the common corpus).
2. **Per-tenant** (`tenant_id = caller's tenant`) — visible only within the tenant, principals must match.
3. **Per-individual** (`owner_sub = caller's sub`) — visible unconditionally to that user.
4. **Cross-tenant** — excluded (fail-closed).

> **Kill switch:** tenant scoping is gated by `TENANT_SCOPE_ENABLED` (default `false`). When off, the Door runs a legacy principal-only query with **no tenant isolation**. It must be set to `"true"` in the deployment's ConfigMap for isolation to be enforced. The cross-tenant gate is validated by `tests/integration/test_cross_tenant_isolation.py` (E8 security gate, #1777).

### Project scoping (E9 #1728)

Projects are a **soft, M:N organizational view** over repositories (`projects` + `project_repositories` tables). Callers can pass a `project` argument to the retrieval verbs (or an `X-Project-Id`-style scope) to narrow results to a project's repos. Gated by `PROJECT_FILTER_ENABLED` (default `false`); filter logic in `door/project_filter.py` (`resolve_project_repos`).

## MCP Tools

Agents interact with **6 tools**. Each handles identity resolution, ACL/project filtering, and backend fan-out internally. (`GET /tools` returns the live schema; tools are invoked via `POST /call` with `{"name": ..., "arguments": {...}}`.)

### `search(query, scope, limit)`
Find relevant code, documentation, and past learnings.
```
search("how to implement retry with exponential backoff")
search("bedrock agent runtime API", scope="web")
```
Backends: S3 Vectors semantic search (when `SEMANTIC_SEARCH_ENABLED`) + Zoekt code search. Scope options: `all`, `code`, `docs`, `memory`, `web`, `infra`.

### `understand(target, depth)`
Get deep understanding of a repo, directory, or file.
```
understand("langchain-ai/langchain", depth="overview")
understand("aws-samples/bedrock-chat", depth="detailed")
```
Backends: code-index structural data (classes, functions, imports, call graphs) + Neptune graph (when enabled) + wikis.

### `impact(target, cross_repo)`
Analyze the blast radius of changing a symbol, file, or pattern. Returns a verdict-first, ranked caller set (bounded at 100), grouped by repo.
```
impact("RetryHandler", cross_repo=true)
```
Backends: code-index call-graph data + Neptune graph + Zoekt cross-repo grep.

### `browse(action, uri, depth)`
Navigate the indexed content filesystem.
```
browse(action="tree", uri="langchain-ai/langchain", depth=2)
browse(action="read", uri="org/repo/src/main.py")
```
Actions: `ls`, `tree`, `read`, `abstract`, `overview`.

### `remember(session_id, messages, outcome)`
Save session context, decisions, and learnings to long-term memory.
```
remember(session_id="fix-retry-bug", messages=[...], outcome="Fixed with circuit breaker")
```
Memories are extracted and made findable by future `search()` calls.

### `experience(action, persona, ...)`
Save or recall **per-user, persona-scoped, synthesized** experiential knowledge. Requires identity headers (fails closed).
```
experience(action="save", persona="developer", content="...", learning_type="gotcha", visibility="private")
experience(action="recall", persona="operations", query="how do we drain the SQS queue safely?")
experience(action="list_syntheses", persona="architect")
```
- `action`: `save` · `recall` · `list_syntheses`
- `persona`: `operations` · `developer` · `architect` · `reviewer`
- `visibility`: `private` (default) · `shared`
- Storage is keyed by `(owner_sub, tenant_id, persona)` — strictly isolated cross-tenant/cross-user. A synthesis CronJob periodically consolidates raw entries into durable syntheses.

## Knowledge Asset Registry (E10 #1736)

The registry is the **single source of truth for "what to index + at what scope"**, backing a NotebookLM-style management UI in the gateway frontend. It's a REST API mounted into the gateway app under `/api/agent-context/assets` (auth-guarded; `assets_router.py`).

An **asset** is a unit of indexable content: `asset_type` ∈ `{repo, url, doc, ...}`, with a `scope` of `personal` or `tenant`. Per-type quotas are enforced.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/agent-context/assets` | Register a single asset |
| `GET` | `/api/agent-context/assets` | List assets (+ quota usage) |
| `GET` | `/api/agent-context/assets/{id}` | Asset detail |
| `DELETE` | `/api/agent-context/assets/{id}` | Remove an asset |
| `POST` | `/api/agent-context/assets/{id}/reindex` | Re-trigger indexing |
| `POST` | `/api/agent-context/assets/bulk` | Bulk-upload preview (validate + dedup + quota check) |
| `POST` | `/api/agent-context/assets/bulk/commit` | Bulk-upload commit |

Registering an asset dispatches an ingestion job (Phase-1 inline SQS dispatch). The management UI (repo picker, three-zone layout, status chips, bulk-upload dialog) lives in `modules/gateway/frontend`.

## Ingestion Pipeline

Ingestion is where raw content becomes the queryable substrate the verbs serve. It is **not a single step** — each asset goes through several independent enrichment stages, each producing a different artifact for a different backend (code search, structural graph, semantic vectors, wikis, SBOM). This section covers how content enters and exactly what processing runs before anything is queryable.

### How content enters

Content is dispatched onto an **SQS** queue and processed by a **KEDA ScaledJob** worker (`images/ingestion/sqs-worker.py`); per-run state is tracked in **DynamoDB** (`adp-context-service-state`). Work arrives from four sources:

| Source | How |
|---|---|
| **Asset Registry** (E10) | Registering an asset via the API dispatches an ingestion job inline |
| **Input files** | `index_content/{repos,urls,accounts}.txt` (see below), published on change |
| **Daily refresh CronJob** | SHA/ETag-based incremental re-ingest of existing content |
| **Manual** | `gh workflow run agent-context-ingest.yml` or a one-off `kubectl create job` |

Each SQS message carries a `content_type` (`repo` · `url` · `doc` · `infra`) plus scope tags (`tenant_id` / `owner_sub` / `project`), and the worker routes to the matching pipeline below. The whole run is wrapped in an OTel root span with a child span per stage.

#### Input Files

| File | Format | What It Feeds |
|------|--------|---------------|
| `index_content/repos.txt` | `org/repo` per line | Curated repos |
| `index_content/urls.txt` | URL per line | Web documentation sites |
| `index_content/accounts.txt` | `account_id:role:regions` | AWS infrastructure discovery |

### Processing stages by content type

#### `repo` — the full enrichment pipeline (`ingest-repo.py`)

A repository is the richest asset. It runs through up to six stages, each writing to a different store. Every stage is **independent and best-effort**: one failing (e.g. DeepWiki) does not block the others, and each follows a **verify-after-write contract** (write row → produce artifact → read-back verify → only then record the stage as verified), so an interrupted run resumes cleanly.

```
org/repo
  │
  1. Clone            git clone --depth=1 → kept on the S3 Files mount
  │                   (shared by all downstream stages; not deleted after)
  │
  2. Structural       cgc analyze (CodeGraphContext) → code-index
  │   code-index      (symbols, imports, call graphs); tree-sitter fallback
  │                   → JSON to filesystem + Markdown summary to S3
  │
  3. Wiki             DeepWiki API → wiki.md (architecture docs + diagrams)
  │                   → S3, and embedded into S3 Vectors via wiki_store
  │                   (this is the main semantic-embedding step — wiki/NL prose)
  │
  4. GraphRAG         LLM entity/relationship extraction → Neptune
  │   (optional)      (only when GRAPHRAG_ENABLED)
  │
  5a. SCIP graph      Detect languages → run scip-<lang> indexer → decode .scip
  │   (deep graph)    → build call/reference graph → Neptune CSV → S3 → bulk-load
  │                   Neptune.  Languages: Python, TS/JS, Go, Java/Kotlin/Scala,
  │                   Ruby, C# (scip-python/-typescript/-go/-java/-ruby/-dotnet)
  │
  5b. Source SBOM     syft dir:<clone> -o cyclonedx-json → S3
  │                   → parse + upsert components to Postgres (feeds the
  │                   reverse-dependency / vuln-management loop)
  │
  6. Persist          Scope-stamp DB rows (tenant_id / owner_sub), keep the
                      clone on the S3 Files mount for incremental refresh
```

Why each artifact exists: **code-index** powers `understand`/`impact` structurally (whole functions, not chunks); **SCIP → Neptune** is the cross-repo deep call graph; **wiki + S3 Vectors** serve `search` for NL/vocabulary-mismatch queries; **SBOM → Postgres** is the reverse-dependency index behind "which repos are exposed to CVE X?". Exact code search (Zoekt) indexes the persisted clone separately.

#### `url` — web documentation (`ingest-url.py`)

1. **Discover** pages — look for a sitemap; otherwise crawl the URL directly (bounded by `--max-pages`).
2. **Crawl** each page with `crawl4ai` (headless browser) → clean **Markdown**; falls back to a `requests` + HTML-to-Markdown path if `crawl4ai` is unavailable.
3. **Store** Markdown to S3, embedded into S3 Vectors for semantic `search(scope="web")`.

#### `doc` — uploaded documents (`ingest-doc.py`)

1. **Fetch** the document (URL download or S3 copy).
2. **Convert** to Markdown with `markitdown` (handles PDF, PPTX, DOCX, HTML, images).
3. **Store** to S3 at `docs/{slug}.md`.
4. **GraphRAG** entity extraction (when enabled).

#### `infra` — AWS infrastructure (`discover-infra.py`)

1. **Discover** live resources via AWS Resource Explorer (assuming a read-only role per account).
2. **Parse IaC** from indexed repos — Terraform, CloudFormation, CDK.
3. **Store** the resource inventory to S3 (and, with GraphRAG, an IaC graph to Neptune) for `search(scope="infra")`.

### Daily Refresh (CronJob)

SHA-based incremental — only re-processes what changed (`refresh-repos.py`):
1. `git ls-remote` each repo → compare HEAD SHA with saved state (DynamoDB).
2. Changed repos → full re-ingest (dispatched via SQS); stages already verified at the current SHA are **skipped**, not redone.
3. DeepWiki backfill for repos missing wikis (capped per run).
4. Web docs: HEAD/ETag compare → re-crawl if changed.

### GitHub Actions Trigger

Pushing changes to `index_content/` on `main` auto-triggers ingestion via `agent-context-ingest.yml`. The image-build and service-deploy workflows are `agent-context-images-build.yml` and `agent-context-deploy.yml`.

## Database Schema

Postgres database `agent_context`, managed by Alembic (`alembic/versions/`):

| Migration | Adds |
|---|---|
| `001_knowledge_layer_schema` | `repositories` (catalog + per-step status + ACL), `dependencies`, `vulnerabilities`, `index_runs` |
| `002_add_wiki_columns` | Wiki status/columns on `repositories` |
| `003_index_run_stages` | Per-stage tracking on `index_runs` |
| `004_add_tenant_isolation_columns` | `tenant_id`, `owner_sub` on `repositories` (E8 #1770) |
| `005_backfill_tenant_scope` | Backfill scope columns for existing rows |
| `005_project_scoping` | `projects`, `project_repositories` (E9 #1784) |
| `006_merge_005_heads` | Merge the two 005 branch heads into one linear chain |
| `007_knowledge_assets` | `knowledge_assets` registry table (E10 #1790) |

## Deployment

### Deployment Cost Tiers

| Mode | Relative Cost | What's Deployed |
|------|--------------|-----------------|
| **Personal-context-only** (`--personal-context-only`) | Lowest — no standing backends beyond the Door + proxy | LiteLLM proxy, Context MCP (Door), S3 Vectors, personal-context synthesis CronJob |
| **Full stack** (default) | Adds backend pod compute + Neptune min-capacity floor + ingestion Bedrock spend | Everything above + Zoekt, DeepWiki, ingestion pipeline (KEDA workers), codegraph, Neptune Serverless |
| **Incremental** (adding personal context to an existing full deploy) | Negligible | Synthesis CronJob only |

Actual spend depends on corpus size, re-index frequency, and Neptune scaling (min/max NCUs in `terraform/variables.tf`) — check Cost Explorer in the target account rather than relying on estimates here. `graphrag_enabled` (OpenSearch Serverless) is the one switch with a large standing cost; it stays off unless explicitly enabled and is guarded by the CI cost check in `agent-context-infra-apply.yml`.

`--personal-context-only` (or `PERSONAL_CONTEXT_ONLY=true`) deploys the lean per-user experiential-memory stack without the code-intelligence backends. The Context MCP Server (Door) deploys in **both** modes — it is the serving endpoint. Neptune deploys only if `neptune_enabled=true` (decoupled from `GRAPHRAG_ENABLED`).

### Automated (recommended)

```bash
# Deploy agent-context only (opt-in due to cost)
./platform/scripts/deploy-all.sh --agent-context-only

# Or enable as part of a full deploy
AGENT_CONTEXT_ENABLED=true ./platform/scripts/deploy-all.sh
```

### Manual

```bash
cd modules/agent-context

# Configure
cp config.env config.local.env
# Edit: set CLUSTER_NAME, AWS_REGION, secrets paths

# Deploy everything (kubectl + Terraform)
./deploy.sh

# Lean personal-context stack
./deploy.sh --personal-context-only

# Validate
./scripts/validate.sh
```

`deploy.sh` performs (roughly): configure kubectl → deploy S3 Files storage + namespace/SA/RBAC → LiteLLM proxy → semantic/S3-Vectors wiring → Zoekt → DeepWiki → codegraph → run DB migrations (`migration-job.yaml`) → ingestion ScaledJob + refresh CronJob → validation.

> **Enabling isolation:** a fresh deploy ships with `TENANT_SCOPE_ENABLED`/`PROJECT_FILTER_ENABLED` **off** unless set in `manifests/agent-context-configmap.yaml`. Set them to `"true"` and `rollout restart deploy/context-mcp` for the change to take effect (envFrom ConfigMap changes do not auto-restart pods).

### Teardown

```bash
./teardown.sh                    # Remove deployments (preserves data)
./teardown.sh --delete-pvcs      # Remove everything including data
./teardown.sh --delete-namespace # Nuclear option
```

There are also Terraform-managed infra workflows: `agent-context-infra-apply.yml` / `-plan.yml` / `-destroy.yml`.

### Prerequisites

- EKS cluster with kubectl access
- IRSA service account (`agent-context-sa`) with: S3, S3 Vectors, Secrets Manager, Bedrock InvokeModel, DynamoDB, (Neptune if GraphRAG)
- Bedrock model access: Titan V2 (embeddings), Claude Sonnet/Haiku/Opus (VLM, wiki, tagging tiers)
- GitHub App credentials in Secrets Manager (`adp/aws-e/gh-app-ops-*`)

## Storage Architecture

| Storage | Type | Contents |
|---|---|---|
| `agent_context` DB | Postgres | repositories, projects, project_repositories, knowledge_assets, index_runs — **the ACL + registry source of truth** |
| Content store | S3 | Cloned content, code-indexes, wikis |
| Semantic index | S3 Vectors | Per-tenant embedding index (`adp-<env>-code-vectors-<account_id>`, sharded) |
| Ingestion state | DynamoDB | `adp-context-service-state` — per-repo SHA / run tracking |
| Zoekt indexes | EBS PVC | Code search shards |
| Graph (optional) | Neptune Serverless | Code/IaC graph when `GRAPHRAG_ENABLED=true` |

## Content Management

```bash
# Add repos — edit index_content/repos.txt, push to main (or use the Asset Registry UI/API)
# Add web docs — edit index_content/urls.txt, push to main
# Add AWS accounts — edit index_content/accounts.txt

# Manual re-index
gh workflow run agent-context-ingest.yml -f full_reindex=true

# Manual ingestion trigger
kubectl create job --from=cronjob/ingestion-refresh manual-refresh -n agent-context
```

## Configuration (`config.env`)

Key variables (see `config.env` for the full set):

| Variable | Default | Description |
|---|---|---|
| `CLUSTER_NAME` | `adp-dev-eks-cluster` | EKS cluster name |
| `NAMESPACE` | `agent-context` | Kubernetes namespace |
| `S3_FILES_BUCKET` | `agent-context-platform-data-<acct>` | S3 bucket for content/state |
| `S3_VECTORS_BUCKET_NAME` | `adp-dev-code-vectors-<acct>` | S3 Vectors semantic index bucket |
| `BEDROCK_EMBEDDING_MODEL` | `amazon.titan-embed-text-v2:0` | Embedding model |
| `BEDROCK_VLM_MODEL` | `global.anthropic.claude-sonnet-4-6` | VLM for summaries |
| `WIKI_LLM_MODEL` | `bedrock/global.anthropic.claude-sonnet-4-6` | LLM for wiki generation |
| `DEEPWIKI_ENABLED` | `true` | Enable DeepWiki |
| `GRAPHRAG_ENABLED` | `false` | Deploy Neptune + load code/IaC graph |
| `INGESTION_REFRESH_SCHEDULE` | `0 6 * * *` | Refresh CronJob schedule |
| `DYNAMO_TABLE` | `adp-context-service-state` | Ingestion state table |

**Door feature flags** (`door/config.py`, read from the ConfigMap):

| Flag | Default | Effect |
|---|---|---|
| `TENANT_SCOPE_ENABLED` | `false` | Enforce tenant/owner isolation (off ⇒ legacy principal-only, **no isolation**) |
| `PROJECT_FILTER_ENABLED` | `false` | Enable project-scoped retrieval |
| `SEMANTIC_SEARCH_ENABLED` | `false` | Enable S3 Vectors semantic search in `search` |
| `NEPTUNE_ENABLED` | `false` | Use Neptune graph for `understand`/`impact` |

## Observability (Kill Switch & Troubleshooting)

The ingestion pipeline and Door emit structured logs (JSON), distributed traces (OTel → X-Ray), and metrics, correlated by `asset_id` / `owner_sub` / `tenant_id` / `project_id` / `run_id` (E11 #1746). All telemetry follows a **fail-open discipline**: emission errors are swallowed and never block ingestion or serving.

### Kill switches

| Env var | Default | Effect |
|---|---|---|
| `KNOWLEDGE_LAYER_TELEMETRY_ENABLED` | `true` | Master switch — disables ALL telemetry (logs revert to text, no traces, no metrics) |
| `KNOWLEDGE_LAYER_TRACES_ENABLED` | `true` | Disables trace export only (logs + metrics continue) |

### Emergency procedure

1. **Immediate (no redeploy):** disable telemetry via the ConfigMap:
   ```bash
   kubectl -n agent-context edit configmap agent-context-config
   # set KNOWLEDGE_LAYER_TELEMETRY_ENABLED: "false"
   kubectl -n agent-context rollout restart deployment/context-mcp
   # ScaledJob pods pick up the ConfigMap on next spawn (no restart needed)
   ```
2. **Verify disabled:** logs should be plain text, not JSON:
   ```bash
   kubectl -n agent-context logs -l app=ingestion-worker --tail=5
   ```
3. **Investigate:** check ADOT collector health:
   ```bash
   kubectl -n adp-agents logs -l app.kubernetes.io/name=adot-collector --tail=20
   ```

### Dashboard & correlation

CloudWatch dashboard: `adp-<env>-knowledge-layer`. Find everything about one asset:
```
SOURCE '/adp/dev/knowledge-layer/ingestion'
| filter asset_id = '<org/repo>'
| sort @timestamp asc
```

### Fail-open guarantee

The `safe_emit()` utility wraps every telemetry call so exceptions are caught and discarded. The regression test `tests/unit/test_telemetry_failopen.py` patches all telemetry subsystems to throw on every call, then verifies a full ingestion cycle completes with correct artifacts. It runs in CI on every PR — it is the safety net for the entire observability layer.

## Testing

```bash
cd modules/agent-context

# Unit tests (no AWS, no cluster)
uv run pytest tests/ -v

# Cross-tenant isolation gate (E8 #1777)
uv run pytest tests/integration/test_cross_tenant_isolation.py -v

# Live E2E tests (against a deployed cluster)
TEST_ENV=dev uv run pytest tests/ -v -m "live or not live_only"
```

## Further Reading

- [`docs/agent-context/knowledge-layer-design.md`](../../docs/agent-context/knowledge-layer-design.md) — consolidated design-of-record
- Design notes: `design-1721-tenant-isolation.md`, `design-1728-project-scoping.md`, `design-1736-knowledge-asset-registry.md`, `design-1746-observability.md` (under `docs/agent-context/`)
- [tests/README.md](tests/README.md) — test suite documentation
- [index_content/README.md](index_content/README.md) — content management guide
- Platform integration: `ARCHITECTURE.md` (repo root)
