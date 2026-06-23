# Solution Brief: World-Class Federated Code Intelligence Platform

> **Status:** Vision / north-star brief. This describes the target blueprint for the
> Knowledge Layer. For what is *built and live today* versus what is aspirational,
> see [§0 Current State vs. This Vision](#0-current-state-vs-this-vision) below.

**Target Blueprint:** Top-1% Code Intelligence Architecture

**Core Objective:** Unify fast text retrieval, strict compiler-level language semantics,
framework-level dynamic routing, structural dependency analytics, local vector semantic
data, enterprise wiki data, and Infrastructure-as-Code (IaC) configuration parameters into
a single, cohesive developer property graph.

---

## 0. Current State vs. This Vision

This brief is the destination. The Knowledge Layer today implements a meaningful subset of
it. This section is the honest map so the brief is read as a roadmap, not a description of
shipped capability.

| Capability in this brief | Today | Reference |
|---|---|---|
| Zoekt trigram string search | ✅ Live (`search_exact`, ~72% eval) | EPIC #1529 |
| SCIP compiler-verified code graph | ✅ Live (Neptune; `understand`/`impact`) | #1529, #1611, #1635 |
| Graceful Tree-Sitter fallback when SCIP fails | ⚠️ Partial — SCIP-native today; Tree-Sitter base layer not yet built | Tier 1 below |
| Implicit framework route resolution (`@app.route` → handler) | ❌ Not built | Tier 2 below |
| IaC / infrastructure topology graph | 🔵 Designed, not built | EPIC #1647, research #1545 |
| Live AWS account asset graph | 🔵 Researched only | #1546 |
| Wiki / doc semantic blending (DeepWiki) | ⚠️ Partial — DeepWiki present, not graph-fused | #1382 |
| Local vector semantic search | ⚠️ Partial / gated (`search_semantic` weak) | #1529 |
| Native MCP Door (single endpoint, all verbs) | ✅ Live (Streamable HTTP `/mcp`) | #1602 |
| Agents wired to the Door | ✅ Built, feature-flagged off | #1592 |
| `calculate_blast_radius` for agents | ✅ Live as the `impact` verb (Neptune) | #1635 |
| `get_architecture_clusters` (Louvain) | ❌ Not built | Tool 2 below |
| `detect_system_dead_code` | ❌ Not built | Tool 3 below |
| Hybrid local-daemon / cloud-backend topology | ❌ Cloud-backend only today; no local edge daemon | §2 below |

**Single biggest divergence from this brief:** the platform today is **cloud-backend only**
(Zoekt + Neptune + Door on EKS). The "local edge daemon" half of the hybrid topology — the
on-device C++/Go MCP server with SQLite/FTS5 and on-device embeddings — does not exist. The
brief's federated, sub-millisecond-local model is the aspiration; the current architecture is
the Hub from §2 without the Edge.

---

## 0.5. License Audit (source projects)

Realizing this vision means adopting techniques (and possibly code) from several external
projects. Under the [[#1345]] permissive-license mandate, **every candidate was license-verified
at the LICENSE-file level** (not just README badges — this EPIC has twice been burned by assumed
licenses: Neo4j-GPLv3 and the cgc-docs-were-wrong episode). Audit date: 2026-06-21.

| Project | Vision tier | License | Verified via | Permissive? |
|---|---|---|---|---|
| `colbymchenry/codegraph` | Tier 2 — framework route resolution | **MIT** | LICENSE file ("Copyright (c) 2026 Colby Mchenry") | ✅ |
| `DeusData/codebase-memory-mcp` | Tier 1 — Tree-Sitter error-tolerant base | **MIT** | LICENSE file ("Copyright (c) 2025 DeusData") | ✅ |
| `nomic-embed-code` (model weights) | On-device embeddings (edge daemon) | **Apache-2.0** | HuggingFace model card; permits commercial use | ✅ |
| `tree-sitter` | Tier 1 foundation | **MIT** | repository | ✅ |
| Zoekt (`google/zoekt`) | Hub string search (already in use) | **Apache-2.0** | already deployed | ✅ |

**Verdict:** all source projects the vision names are permissively licensed and compatible with
the #1345 mandate. No GPL / AGPL / BSL / SSPL encumbrances — the full vision is buildable on
permissive foundations.

**Caveats (must hold before any code is vendored):**

1. **Top-level MIT ≠ transitive MIT.** `codegraph` and `codebase-memory-mcp` each pull their own
   dependency trees; the repo's MIT license does not cover everything it imports. **If we vendor
   their code, a full dependency-license scan (SBOM) is still required.** If we instead **adopt
   their techniques/rules and re-implement** (the recommended path — e.g. re-build codegraph's
   route-extractor patterns ourselves), this risk disappears and MIT explicitly permits the
   adaptation. Default to *adopt-technique, not vendor*.
2. **`nomic-embed-code` weights are Apache-2.0, but the inference stack is a separate check.** When
   the edge-daemon phase arrives, confirm the tokenizer + any GGUF/llama.cpp serving runtime are
   also permissive — Apache weights do not guarantee an Apache serving toolchain. This is a
   Phase-3 gate, not a blocker now.
3. **Re-verify before each adoption.** Licenses change across versions/forks; pin and re-check the
   exact commit/tag we build against, don't trust this snapshot indefinitely.

---

## 1. Executive Summary

Traditional code intelligence solutions suffer from a fragmentation paradox: global indexing
clusters are too high-latency and costly for fast local workflows, while local developer
tools lack the massive multi-repository enterprise visibility. High-performance platforms
like Sourcegraph provide compiler-verified search via SCIP and Zoekt, but fail to resolve
implicit web routes, declarative infrastructure definitions, and adjacent documentation
context.

This solution brief outlines a top-1% platform design. By leveraging a hybrid federated
topology, it pairs localized desktop/IDE processing daemons with a scalable cloud backend. It
adopts the error-tolerant multi-language parsing rules of codebase-memory-mcp, the implicit
framework routing maps of `colbymchenry/codegraph`, and advanced infrastructure state-file
tracing. The result is a unified knowledge engine that slashes AI token consumption by over
50%, eliminates context hallucinations, and handles code semantics from the UI layer to
deployed infrastructure.

---

## 2. Hybrid Federated System Architecture

To deliver sub-millisecond local completions while handling enterprise-wide repositories, the
platform is divided into an asymmetric execution model:

```
                  ┌────────────────────────────────────────────────────────┐
                  │                 DEVELOPER WORKSPACE                      │
                  │   [IDE Agent Client / Claude Code / Cursor Sandbox]      │
                  └───────────────────────────┬────────────────────────────┘
                                              │
                    ┌─────────────────────────┴─────────────────────────┐
                    ▼ (Sub-Millisecond Workspace Tasks)                  ▼ (Cross-Repo / Complex Inquiries)
       ┌─────────────────────────────────┐                 ┌─────────────────────────────────┐
       │     LOCAL DAEMON (THE EDGE)     │                 │    ENTERPRISE CLOUD BACKEND     │
       │  • Compiled C++/Go Static Engine│                 │  • Zoekt (Trigram String Engine)│
       │  • Local SQLite + FTS5 Index    │                 │  • AWS Neptune Graph Cluster    │
       │  • On-Device Vector Embeddings  │                 │  • Deep Vector Wiki/Doc Vault   │
       └─────────────────────────────────┘                 └─────────────────────────────────┘
```

### The Edge: Local Workspace Daemon

* **Technology:** A single, lightweight, compiled C++/Go binary deployed as a sidecar process
  on the developer's local machine, acting as an MCP (Model Context Protocol) server.
* **Storage & Logic:** A local SQLite database utilizing an FTS5 full-text module. It monitors
  native OS file system watch events to process uncommitted local edits instantly.
* **On-Device Embedding Engine:** Uses an embedded local model (such as `nomic-embed-code`)
  running directly on the local CPU or GPU, completely avoiding cloud network roundtrips for
  file edits.

### The Hub: Enterprise Cloud Backend

* **Technology:** Distributed cluster infrastructure executing compute-heavy ingest sweeps
  across entire version control systems (GitHub, GitLab).
* **Storage & Logic:** [Zoekt](https://github.com/google/zoekt) handles high-throughput regex
  and literal string discovery. [AWS Neptune](https://aws.amazon.com/neptune/) acts as the
  centralized master property graph, housing cross-repository links, infrastructure states,
  and historical data.

---

## 3. High-Fidelity Data Ingestion & Enrichment Pipeline

When a repository is updated, the ingestion system executes an asynchronous, four-tier
multi-stage parsing routine to build a comprehensive system map:

```
                           [ REPOSITORY INGESTION WORKER ]
                                           │
         ┌───────────────────┬─────────────┴─────────────┬───────────────────┐
         ▼                   ▼                           ▼                   ▼
  [ SCIP Compiler ]   [ Tree-Sitter ]            [ Framework Rules ]   [ TFState Parser ]
   Precision Type      158-Language Base          Implicit Route        Infrastructure
   Verification        AST Structure Map          String Resolver       Topology Engine
         │                   │                           │                   │
         └───────────────────┼───────────────────────────┴───────────────────┘
                             ▼
                 [ AWS Neptune Master Graph ]
```

### Tier 1: The Robust SCIP & Tree-Sitter Hybrid

* **Problem Solved:** SCIP maps precise semantic declarations but crashes if code fails to
  compile, if versions mismatch, or if dependencies are missing.
* **Mechanism:** The worker immediately executes a high-speed Tree-Sitter parsing pass using
  pre-compiled grammar models for 158 programming languages. This builds a structural base
  layer (classes, function names, scopes, brackets) without requiring a build step.
  Asynchronously, a separate SCIP container runs to resolve compiler-verified types. If SCIP
  finishes cleanly, it enriches the tree. If it fails due to broken code, the system gracefully
  retains the error-tolerant Tree-Sitter layout. The graph never goes dark.

### Tier 2: Implicit Framework Route Resolvers

* **Problem Solved:** Standard language syntax parsers see web endpoint paths
  (`@app.route('/v1/users')`) as raw string literals. They cannot link network requests to the
  handler functions executing them.
* **Mechanism:** Adopt CodeGraph's framework parsing rules. The AST pipeline scans codebases
  using custom extractors tailored to 17+ web frameworks (FastAPI, Django, Spring Boot,
  Express, NestJS, etc.). It reads route declaration structures and builds explicit logical
  routing maps.

### Tier 3: IaC & Cloud Infrastructure Topology Mapping

* **Problem Solved:** Code logic depends entirely on underlying infrastructure constraints
  (e.g., identity roles, database engines, security groups) that are declared in configuration
  scripts, not language files.
* **Mechanism:** The pipeline reads the project's `terraform.tfstate`, OpenTofu, or Pulumi JSON
  export layers. It converts declarative cloud state mappings into distinct infrastructure
  components within the graph database, connecting your application logic directly to the live
  resources hosting it.
* **Status note:** Designed in EPIC #1647 (IAC-0 design authority complete), grounded in the
  #1545 tool research. Source-IaC parsing (Terraform via `python-hcl2`) is the chosen first
  step; this brief's `tfstate` angle and the #1647 HCL-source angle are complementary inputs.

### Tier 4: Unified Wiki Semantic Blending

* **Problem Solved:** Team documentation and codebase code bases exist in isolated storage
  silos, forcing AI models to query separate vector databases and manually merge context.
* **Mechanism:** An asynchronous documentation scraping loop reads Confluence, Markdown, or
  Notion wikis. It extracts system nouns, matches them against code structures via entity
  extraction, and saves text abstracts directly as node metadata inside your master graph
  database.

---

## 4. Master Property Graph Schema

To combine these layers cleanly, implement a highly relational graph structure inside AWS
Neptune (replicated on-device within the local micro-SQLite engine):

```
    [ INFRASTRUCTURE LAYER ]          (AWS_VPC) ───► (AWS_SECURITY_GROUP) ───► [AWS_EC2_INSTANCE]
                                                                                       │
                                                                                       ▼ (HOSTS_SERVICE)
    [ FRAMEWORK/ROUTE LAYER ]                                                   (INFRA_ROUTE)
                                                                                       │
                                                                                       ▼ (HANDLED_BY)
    [ CODE/SEMANTIC LAYER ]               (CLASS) ───► (FUNCTION) ◄────────────────────┘
                                                            │
                                             (CALLS)        ▼ (READS_FROM)
                                         (FUNCTION) ───► (DATABASE_TABLE)
                                              ▲
                                              │ (DOCUMENTS)
    [ SEMANTIC/CONTEXT LAYER ]          (WIKI_CONCEPT) ◄─► (FUNCTION [Semantically Related])
```

### Core Node Schema

* **FILE:** Represents physical code files. Properties: `path: string`, `language: string`,
  `git_hash: string`.
* **CLASS / FUNCTION:** Functional code symbols. Properties: `name: string`,
  `signature: string`, `start_line: int`, `end_line: int`.
* **INFRA_ROUTE:** Web endpoints. Properties: `method: string` (GET/POST),
  `path_pattern: string`, `framework: string`.
* **AWS_RESOURCE:** Deployed cloud units. Properties: `arn: string`, `type: string`
  (EC2, S3, RDS), `provider: string`.
* **DATABASE_TABLE:** Persisted data entities. Properties: `table_name: string`,
  `schema_definition: string`.
* **WIKI_CONCEPT:** Context documentation blocks. Properties: `title: string`,
  `text_payload: string`.

> **Reconciliation with the current graph:** today's Neptune graph uses `:Symbol` nodes (SCIP
> monikers) with `CALLS`/`REFERENCES` edges for code, and the #1647 design adds
> `:InfraResource`/`:InfraModule`/`:InfraProvider` with `DEPENDS_ON`/`DECLARED_IN`/
> `USES_PROVIDER`. This brief's richer node taxonomy (FILE/CLASS/FUNCTION/INFRA_ROUTE/
> AWS_RESOURCE/DATABASE_TABLE/WIKI_CONCEPT) is the target schema to converge toward.

### Edge Relationship Schema

* **CONTAINS:** Connects a FILE node directly to its internal code symbols (CLASS/FUNCTION).
* **CALLS:** Connects a FUNCTION node to an execution target FUNCTION node.
* **HANDLED_BY:** Links an INFRA_ROUTE node directly to the backend FUNCTION handling the web
  traffic.
* **HOSTS_SERVICE:** Connects an AWS_RESOURCE node (e.g., an EC2 instance or Lambda function)
  to the service root execution file.
* **READS_FROM / WRITES_TO:** Tracks variables or data connections going into specific database
  tables or memory structures.
* **DOCUMENTS:** Links a WIKI_CONCEPT node directly to the corresponding CLASS or AWS_RESOURCE
  node it documents.
* **SEMANTICALLY_RELATED:** Connects functions sharing similar local text embeddings to resolve
  vocabulary gaps (e.g., `emit()` and `dispatch()`).
* **SIMILAR_TO:** Uses MinHash + LSH and Jaccard metrics to link duplicated or copy-pasted block
  fragments across repositories.

---

## 5. Elite AI-Agent Analytical Tools

A top-1% platform cannot simply act as a passive database; it must provide high-utility
analytical tools that AI coding models can query directly via the Model Context Protocol (MCP):

### Tool 1: `calculate_blast_radius(git_diff)`

* **The Logic:** When an agent proposes a code modification, the platform intercepts the
  uncommitted diff. It looks up the targeted function node and traverses upstream recursively
  via incoming CALLS edges.
* **The Actionable AI Response:** It returns a safety payload to the agent: *"Modifying
  `BillingService.process()` will impact 14 upstream methods across 3 separate repositories.
  Here is the strict verification execution chain."*
* **Status note:** Live today as the Door's `impact` verb (Neptune-backed, transitive callers).
  The `git_diff` interception framing is the next evolution — today it takes a target symbol,
  not a raw diff.

### Tool 2: `get_architecture_clusters(repo_id)`

* **The Logic:** Large legacy systems overload AI context windows. The platform periodically
  runs the Louvain community detection algorithm directly over Neptune's topology.
* **The Actionable AI Response:** It simplifies system complexity down into high-level
  structural domains: *"This platform consists of 4 distinct functional blocks: Authentication
  Core, Payment Operations, Audit Logging, and UI Rendering."*

### Tool 3: `detect_system_dead_code(repo_id)`

* **The Logic:** Scans target repositories by checking for function entities that have exactly
  zero incoming execution or invocation edges.
* **The Actionable AI Response:** Automatically queues structural refactoring tasks for the AI
  agent to remove dead code paths cleanly.

---

## 6. Implementation & Engineering Roadmap

```
Phase 1: Hybrid Core (M1-2) ──► Phase 2: Route & Infra (M3-4) ──► Phase 3: AI Analytics (M5-6) ──► Phase 4: Production (M7+)
```

### Phase 1: Hybrid Semantic Core & Ingestion Framework (Months 1–2)

* **Goal:** Build an error-tolerant foundation that operates natively on-device and inside your
  enterprise cloud network.
* **Deliverables:**
  * Compile the local workspace client daemon using a lightweight Go/C stack to serve as an
    on-device indexer with zero runtime dependencies.
  * Initialize the Neptune master property graph schema using the foundational code entity
    structures (FILE, CLASS, FUNCTION).
  * Integrate Tree-Sitter extraction scripts alongside your existing cloud SCIP worker to handle
    language verification fallbacks automatically if code fails to compile.

### Phase 2: Route & Infrastructure Edge Mapping (Months 3–4)

* **Goal:** Bridge the gaps separating static code structures, dynamic framework routing
  strings, and cloud deployment states.
* **Deliverables:**
  * Build your custom AST regex parsing engine to extract route patterns across your core
    development frameworks (FastAPI, Django, Express, Spring Boot).
  * Write a pipeline worker that ingests `terraform.tfstate` files, converting cloud
    infrastructure layers into distinct graph entries.
  * Connect endpoints directly to their corresponding code definitions via HANDLED_BY and
    HOSTS_SERVICE edge paths.

### Phase 3: Analytical Intelligence & Context Fusion (Months 5–6)

* **Goal:** Equip your AI agent with deep context tracking capabilities and on-device machine
  learning tools.
* **Deliverables:**
  * Integrate an on-device embedding engine (like `nomic-embed-code`) directly into the local
    workspace daemon to compute local file affinities.
  * Implement background graph traversal queries across your Neptune database to compute Git
    update blast radius statistics.
  * Run regular Louvain clustering loops across massive codebases to automatically divide complex
    repositories into high-level modular blocks.

### Phase 4: Production Rollout & Validation (Month 7+)

* **Goal:** Distribute your edge client daemon as an official MCP server package for development
  tools like Claude Code and Cursor.
* **Deliverables:**
  * Configure your system's query router to handle lightweight file changes inside the local
    SQLite database while directing deep cross-repository searches to your central Zoekt and
    Neptune clusters.
  * Measure end-to-end performance and token utilization to verify that AI context windows
    consume over 50% fewer tokens during complex architectural tasks.

---

## 7. Simplified Convergence Model (schema + verb refinement)

A tighter restatement of §2–§5 emerged as a scope-discipline pass. It is **adopted as the
convergence target** for the static-code platform — fewer node types, a fixed six-verb surface —
because it isolates data flows cleanly and resists scope creep. Treat the numbers here (5 node
sets, 6 verbs) as the canonical target schema; the richer taxonomy in §4 is the superset to
prune toward.

### 7.1 Four input categories
1. **Code repositories** — source, framework config, multi-language modules.
2. **Infrastructure files** — `.tf`, `terraform.tfstate`, Kubernetes YAML.
3. **Context documentation** — Markdown, Confluence/Notion, architecture descriptions.
4. **Operational streams** — *see §7.4; this is a distinct future pillar, NOT part of the
   simplified static core.*

### 7.2 Five node sets (the target schema)
- **INFRASTRUCTURE** — `AWS_RESOURCE` (EC2, task definitions, DB instances)
- **ROUTING / PIPELINE** — `INFRA_ROUTE` (endpoints) ◄──► `CI_JOB` / `PIPELINE`
- **CODE LOGIC** — `FILE` / `FUNCTION` / `CLASS` (AST symbols, variable types)
- **CONTEXT** — `WIKI_CONCEPT` (doc vectors, structural abstracts)
- **MEANING MATCHES** — `FUNCTION` linked by on-device embeddings / structural duplicates

```
  [ INFRASTRUCTURE ] ───►  AWS_RESOURCE (EC2, Task Definitions, DB Instances)
                                │
                                ▼ (HOSTS_SERVICE)
  [ ROUTING/PIPELINE ] ──►  INFRA_ROUTE (Endpoints)  ◄──►  CI_JOB / PIPELINE (Logs, States)
                                │
                                ▼ (HANDLED_BY)
  [ CODE LOGIC ]       ──►  FILE / FUNCTION / CLASS (AST Symbols, Variable Types)
                                ▲
                                │ (DOCUMENTS)
  [ CONTEXT ]          ──►  WIKI_CONCEPT (Documentation Vectors, Structural Abstracts)
                                ▲
                                │ (SEMANTICALLY_RELATED)
  [ MEANING MATCHES ]  ──►  FUNCTION (Local On-Device Embeddings, Structural Duplicates)
```

### 7.3 Six MCP verbs (the fixed surface)
| Verb | Purpose | Status today |
|---|---|---|
| `search_codebase(regex)` | Fast text/file discovery via Zoekt | ✅ live (`search`) |
| `get_code_dependencies(symbol)` | Traverse AST refs / variable pathways | ✅ live (`understand`) |
| `resolve_web_route(url_path)` | Endpoint → backend handler | ❌ needs Tier-2 framework routing |
| `calculate_blast_radius(git_diff)` | Upstream impact of a change | ✅ live as `impact` (target-based; diff-based is the next step) |
| `get_architecture_clusters(repo_id)` | Louvain modular decomposition | ❌ not built |
| `diagnose_pod_failure(pod_id)` | Runtime error → log → CI job → commit line | ❌ **see §7.4 — separate pillar** |

### 7.4 ⚠️ Scope boundary: Operational / Runtime Intelligence is a SEPARATE pillar
The simplified brief folds **live operational data** — CI/CD logs, K8s pod statuses,
OpenTelemetry/APM exception streams — into the same model, surfaced via
`diagnose_pod_failure(pod_id)`. **This is a genuine scope expansion, not a simplification**, and
is deliberately carved out here so it does not silently inflate the "core":

- **Different data nature.** Everything else in this vision is *parsed static artifacts*
  (source, IaC, docs) → parse-once → property graph. Operational streams are *time-series
  telemetry* (streaming, TTL, alert correlation) — a fundamentally different ingestion model that
  the static parse→graph→query pipeline is not built for.
- **Highest value, furthest from reality.** "Agent diagnoses a prod failure down to the offending
  commit line" is arguably the most valuable autonomous capability in the whole vision — and the
  least started (no telemetry ingestion exists in any form today).
- **Overlaps existing parked work.** The live-state angle relates to the live-AWS-asset-graph
  research (#1546), which is also a runtime/inventory data source distinct from IaC-source.

**Decision:** the static-code platform (§7.1–§7.3 minus operational streams) is the convergence
target and proceeds first. **Operational/Runtime Intelligence (`diagnose_pod_failure` + CI/APM/pod
ingestion) is its own future EPIC**, sequenced *after* the static graph is real and agents use it
daily — not blended into the simplified core. Filing it as a distinct pillar keeps the
scope-discipline the simplified brief rightly asks for.

---

## Open Questions / Next Deep-Dives

* Exact JSON schemas for passing infrastructure mappings into the ingestion worker.
* Implementation of the Louvain clustering script inside AWS Neptune (openCypher — note the
  collect-of-node + Python-projection pattern required; inline-map aggregation is unsupported,
  see #1611).
* The local edge daemon: language/runtime choice, MCP packaging, and the local↔cloud query
  router contract — the single largest unbuilt piece of this brief.

---

## References

* Code graph EPIC: **#1529** (Neptune SCIP graph) · query-layer fixes **#1595**, **#1611**, **#1635**
* Native MCP Door: **#1602** · agent wiring **#1592**
* IaC infra arm: EPIC **#1647** · research **#1545** · live-asset research **#1546**
* Wiki/docs: DeepWiki **#1382** · OpenViking replacement **#1348**
* Knowledge Layer parent EPIC: **#1345** (permissive-license mandate)
* Design notes: `docs/agent-context/design-notes/`
