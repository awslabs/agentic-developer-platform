workspace "ADP Knowledge Layer" "Code intelligence: index repos, serve verbs to agents, drive autonomous vuln remediation." {

  !identifiers hierarchical

  model {
    # ---- people / external actors ----
    agent      = person "ADP Agent" "A developer/ops agent that needs code context."
    admin      = person "Platform Admin" "Operates the platform; watches indexing status."

    # ---- external software systems ----
    github     = softwareSystem "GitHub" "Source repositories (cloned) + permissions." "External"
    bedrock    = softwareSystem "Amazon Bedrock" "Embeddings (Titan) + LLMs (Claude)." "External"
    agentFactory = softwareSystem "Agent Factory" "Hosted developer agents that fix code (vuln remediation)." "External"
    gateway    = softwareSystem "ADP Gateway" "Bedrock proxy + Cognito identity; hosts the admin UI shell." "External"

    # ---- the system under design ----
    kl = softwareSystem "Knowledge Layer" "Indexes code repositories and answers questions about them (search, understand, impact, browse); drives vuln remediation." {

      # ----- containers -----
      ingest = container "Ingestion Worker" "Per-repo indexing orchestrator; owns the run_id; runs the stages." "Python · KEDA ScaledJob (ephemeral)" "Ephemeral" {
        # ----- components (inside the worker) -----
        cClone     = component "Clone" "Single authenticated git clone to scratch." "git + App token"
        cStruct    = component "Structural Indexer" "cgc → code-index.json (symbols, call graph)." "CodeGraphContext"
        cVectors   = component "Vector Embedder" "Chunk + embed code/wiki → S3 Vectors." "LiteLLM → Titan"
        cSbom      = component "SBOM Generator" "syft dir: → CycloneDX; deps → Postgres." "Syft"
        cWiki      = component "Wiki Producer" "Calls DeepWiki; stores wiki to S3 + S3 Vectors." "DeepWiki client"
        cZoekt     = component "Zoekt Indexer" "zoekt-index → shard artifact." "zoekt-git-index"
        cTracker   = component "Stage Tracker" "run_id header + per-stage verify-after-write." "stage_tracker.py"
        cConfig    = component "Config" "Single typed settings; per-task model tiering." "config.py (pydantic)"
      }

      mcp        = container "Context MCP Server" "Serves the verbs (search/understand/impact/browse/remember) to agents; applies the ACL filter." "FastAPI :5100 (always-on)" "AlwaysOn" {
        cAcl     = component "ACL Filter" "Fail-closed permission filter (GitHub-mirrored)." "door/acl.py"
        cSearch  = component "Search Backend" "Queries Zoekt + S3 Vectors." "door/search_backend.py"
      }

      zoekt      = container "Zoekt" "Exact/keyword code search server." "Apache-2.0 · serves from EBS (always-on)" "AlwaysOn"
      deepwiki   = container "DeepWiki" "Generates per-repo architecture wikis." "MIT upstream image (always-on*)" "AlwaysOn"
      litellm    = container "LiteLLM Proxy" "Routes embedding/LLM calls to Bedrock." "OpenAI-compat proxy (always-on*)" "AlwaysOn"
      crons      = container "CronJobs" "Scheduled repo-refresh + learning synthesis." "Kubernetes CronJob (ephemeral)" "Ephemeral"

      s3         = container "S3 (Mountpoint)" "Durable write-once artifacts: code-index.json, wikis, SBOMs, zoekt shards." "Amazon S3" "Managed,Datastore"
      s3vectors  = container "S3 Vectors" "Meaning fingerprints for semantic search." "Amazon S3 Vectors" "Managed,Datastore"
      pg         = container "PostgreSQL (agent_context)" "Catalog, permissions, dependency reverse-index, index_runs / index_run_stages." "RDS PostgreSQL 16.6" "Managed,Datastore"
      sqs        = container "SQS + KEDA" "Work queue that drives the per-repo worker fan-out." "Amazon SQS" "Managed"

      indexUI    = container "Indexing Status UI" "Runs table → per-stage detail (keyed on run_id). Routes defined here, mounted by the gateway, feature-flagged." "React + APIRouter (agent-context-owned)"
    }

    # ===== relationships: context level =====
    agent -> kl "Asks for code context (verbs)"
    admin -> kl "Views indexing status"
    kl -> github "Clones repos; reads permissions"
    kl -> bedrock "Embeddings + LLM calls"
    kl -> agentFactory "Files vuln-fix issues; agents patch & test"
    kl -> gateway "Reuses Cognito identity for the admin UI"

    # ===== relationships: container level =====
    agent -> kl.mcp "Calls verbs (HTTP :5100)"
    kl.sqs -> kl.ingest "Triggers one worker per repo"
    kl.ingest -> github "git clone (App token)"
    kl.ingest -> kl.litellm "Embeddings during indexing"
    kl.litellm -> bedrock "invoke (Titan/Claude)"
    kl.ingest -> kl.deepwiki "Generate wiki"
    kl.deepwiki -> kl.litellm "LLM calls"
    kl.ingest -> kl.s3 "Writes code-index, wikis, SBOMs, shards"
    kl.ingest -> kl.s3vectors "Writes embeddings"
    kl.ingest -> kl.pg "Writes deps, run/stage status"
    kl.ingest -> kl.zoekt "Loads shard / index"
    kl.mcp -> kl.zoekt "Exact search"
    kl.mcp -> kl.s3vectors "Semantic search"
    kl.mcp -> kl.s3 "Reads structure maps / wikis"
    kl.mcp -> kl.pg "Reads catalog + ACL"
    kl.crons -> kl.ingest "Scheduled refresh"
    admin -> kl.indexUI "Views runs + stages"
    kl.indexUI -> kl.pg "Reads index_runs / index_run_stages"
    kl.indexUI -> gateway "Mounted in gateway app; uses Cognito"

    # ===== relationships: component level (inside the worker) =====
    kl.sqs -> kl.ingest.cClone "Delivers repo task"
    kl.ingest.cClone -> kl.ingest.cStruct "clone tree"
    kl.ingest.cStruct -> kl.ingest.cVectors "symbols/spans"
    kl.ingest.cVectors -> kl.litellm "embed"
    kl.ingest.cStruct -> kl.s3 "code-index.json"
    kl.ingest.cSbom -> kl.s3 "SBOM"
    kl.ingest.cSbom -> kl.pg "dependency rows"
    kl.ingest.cWiki -> kl.deepwiki "generate"
    kl.ingest.cWiki -> kl.s3 "wiki.md"
    kl.ingest.cVectors -> kl.s3vectors "vectors"
    kl.ingest.cZoekt -> kl.s3 "shard"
    kl.ingest.cTracker -> kl.pg "run_id + per-stage verify-after-write"
    kl.ingest.cClone -> kl.ingest.cTracker "stage status"
    kl.ingest.cStruct -> kl.ingest.cTracker "stage status"
    kl.mcp.cSearch -> kl.zoekt "exact"
    kl.mcp.cSearch -> kl.s3vectors "semantic"
    kl.mcp.cAcl -> kl.pg "allowed_principals"
  }

  views {
    systemContext kl "Context" "Level 1 — who uses the Knowledge Layer and what it talks to." {
      include *
      autolayout lr
    }

    container kl "Containers" "Level 2 — the runnable/deployable units." {
      include *
      autolayout lr
    }

    component kl.ingest "IngestionComponents" "Level 3 — inside the Ingestion Worker." {
      include *
      autolayout lr
    }

    component kl.mcp "McpComponents" "Level 3 — inside the Context MCP Server." {
      include *
      autolayout lr
    }

    styles {
      element "Person"      { shape Person background #5b8cff color #ffffff }
      element "External"    { background #8a94a8 }
      element "Software System" { background #2a3040 color #ffffff }
      element "Container"   { background #1c2030 color #e9ecf3 }
      element "Component"   { background #222838 color #e9ecf3 }
      element "Datastore"   { shape Cylinder }
      element "Ephemeral"   { background #ffc24a color #1a1a1a }
      element "AlwaysOn"    { background #5b8cff color #ffffff }
      element "Managed"     { background #37c794 color #08231a }
    }
  }
}
