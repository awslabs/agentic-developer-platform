# Knowledge Layer — Consolidated Design

**Status:** Design of record (consolidated). Supersedes the previous multi-file design set.
**Last consolidated:** 2026-06-23
**Scope:** The *code* Knowledge Layer — how ADP indexes source code so agents can search it, understand it, reason about its structure, and find/fix its vulnerabilities. EPIC #1345. This document covers both **what we build now** (§1–§12, §14) and **where it's heading** (§16, the north-star architecture).
**Audience:** Humans and the next agent. Plain language first; precise detail in the later sections.
**Companion doc:** `knowledge-layer-product-vision.md` — the *why* and *for whom* (sits above this).

> **What this replaces.** This single document consolidates and reconciles the prior set:
> `knowledge-layer-storage-design.md`, `database-design.md`, `design-1348-replace-openviking.md`,
> `design-notes/1346-zoekt-direct-replacement.md`, `design-notes/1357-structural-index-design.md`,
> `design-notes/1358-dual-rail-sbom-*.md`, `design-notes/1360-vuln-remediation-loop.md`,
> `design-notes/1382-deepwiki-*.md`, `neptune-deep-graph-design.md`, and the
> `vision-federated-code-intelligence.md` north-star brief (now §16). Where those docs
> disagreed, the resolved decision is stated here and called out in **§14 Reconciliations**.
> `neo4j-deep-graph-design.md` is **superseded** (GPLv3) and archived. The two *future-pillar*
> notes (IaC graph #1647, live-AWS resource graph #1546) remain separate — see §13.

---

## 1. The whole thing in one paragraph

The platform indexes each repository so agents can ask questions about code. We clone a repo **once** onto a scratch disk, then build up to **four** indexes from that single clone — exact text search, a structural map (what calls what), an optional meaning/semantic index, and a software bill-of-materials (SBOM) of its dependencies — plus a generated wiki. The durable results live in **plain AWS services** (S3, S3 Vectors, PostgreSQL, and an Amazon Neptune graph for cross-repo call structure); nothing important lives on a disk that can disappear. Work is fanned out across many machines (SQS + KEDA) so hundreds of repos index quickly. Agents ask questions through **one MCP server**, and every answer is permission-filtered against GitHub-mirrored ACLs (fail-closed: unknown caller sees nothing). The standout capability is the SBOM reverse-index: ADP can detect a new vulnerability, instantly find every affected repo, hand the fix to its existing developer agents, verify with tests, and remember the outcome — turning code search into autonomous vulnerability management.

---

## 2. The picture that matters

```
   A REPO ON GITHUB
        │  clone once  ──────────────────────────────────────────────┐
        ▼                                                             │
   WORKER (KEDA-spawned, one per repo, scratch disk)                  │
        │                                                             │
        ├─ 1. Exact search   → Zoekt shards          → S3 (durable) ──┤
        ├─ 2. Structure map   → code-index.json        → S3           │
        │                      + call graph            → Neptune      │
        ├─ 3. Meaning search* → embeddings             → S3 Vectors   │  *gated — see §6
        ├─ 4. SBOM            → source + image deps     → S3 + Postgres│
        └─ 5. Wiki            → DeepWiki prose          → S3 + Vectors │
        │                                                             │
        └─ catalog + ACL + run log                      → Postgres ───┘
                                                  │
                                                  ▼
   AGENTS ASK ONE MCP SERVER  →  permission-checked on every answer
   (you only ever see repos your GitHub identity is allowed to see)
```

---

## 3. What we removed, and why it's simpler now

| Removed | What it did | Why it went | Replaced by |
|---|---|---|---|
| **Sourcebot** (FSL-1.1) | Exact/keyword code search | License forbids commercial competition | **Zoekt** directly (Apache-2.0) — the engine that was *inside* Sourcebot |
| **OpenViking** (AGPL-3.0) | Semantic search + file browser + memory, all on one pod disk | Copyleft license + fragile single-disk storage | **S3 Vectors** + **S3** + a few hundred lines of our own code |
| **Redis 8** (AGPL-3.0) | Cache | Copyleft | Removed; not needed in the new design |
| **Neo4j** (GPLv3) | Deep call-graph store | Copyleft — violates the permissive-license mandate | **Amazon Neptune Serverless** (managed, no copyleft) |
| **Grype** | Vuln matching | License cleanliness | **OSV-Scanner** + **Trivy** (both Apache-2.0) |

Net effect: **one** standing third-party indexing service remains (Zoekt); everything else is plain AWS plus our own code. Every tool in the security chain is Apache-2.0/MIT, so the platform stays cleanly distributable.

---

## 4. Where things live (the stores)

| Store | What it is | What we keep here |
|---|---|---|
| **S3** (via Mountpoint) | Durable, write-once object storage mounted as files | Zoekt shard *artifacts*, `code-index.json`, wikis, SBOMs (CycloneDX) |
| **S3 Vectors** | AWS service storing "meaning fingerprints" and finding similar ones | The semantic index (code* and wiki prose) — sharded |
| **PostgreSQL** (shared gateway RDS, own `agent_context` DB) | Relational database | Catalog, ACLs, dependency reverse-index, vulnerabilities, run tracking |
| **Amazon Neptune Serverless** | Managed graph DB (openCypher) | Cross-repo call graph for `understand`/`impact` |
| **EBS** (Zoekt serving volume) | Real block disk on the Zoekt pod | The **live, queryable** Zoekt index the server serves from |
| **Worker scratch disk** | Ephemeral local disk per job | The cloned repo + half-built indexes, discarded on completion |

**Two storage notes that bite if ignored:**

- **Zoekt has two storage roles — don't conflate them.** The durable shard *artifact* is a write-once S3 object (fine for Mountpoint). The **serving index** the Zoekt server answers from must live on a **real POSIX block disk (EBS)** — a live search server does random reads, `mmap`, and a startup `mkdir`, which **S3 Mountpoint cannot do** (it crash-loops on `mkdir /data/index: file exists`). So shards are *produced* to S3 (durable, portable, rebuildable) but *served* from EBS. This is the one place "everything durable is in S3" doesn't hold, and it's the concrete trigger for the Probe-per-query evaluation in §12.

- **Postgres: shared instance, separate database.** We reuse the existing gateway RDS instance (PostgreSQL 16.x) but give agent-context its **own** `agent_context` database with a **low-privilege IAM-auth login** (`agent_context_svc`, `rds_iam`, DB-scoped grants only — never the master user). This reuses the instance, security group, IAM plumbing, and migration tooling we already trust, with no new infrastructure. If heavy indexing ever competes with gateway traffic, the escape hatch is a dump-and-restore onto its own instance — not a redesign.

---

## 5. Indexing (the write path)

1. **Decide what needs work.** A scheduler compares each repo's last-indexed SHA (Postgres) to its current SHA (GitHub). Unchanged → skip. Changed → enqueue one SQS message.
2. **Fan out.** KEDA spawns one worker per repo (up to ~50–100 concurrent), each handling exactly one repo then shutting down.
3. **Clone once.** The worker clones to scratch (GitHub App token for private repos). **This is the only download** — every index below reads this one copy.
4. **Mirror permissions.** Ask GitHub who can access the repo (teams + users); store that list in Postgres `repositories.allowed_principals`. We mirror GitHub exactly — we don't invent rules.
5. **Build the indexes** from the one clone:
   - **5a. Exact search (Zoekt):** build shards, upload to `s3://…/zoekt-shards/` (write-once). The serving pod loads them onto EBS.
   - **5b. Structure map:** run cgc/tree-sitter → `code-index.json` to S3; load call-graph nodes/edges into Neptune (cross-repo edges keyed on **SCIP `symbol_id` monikers**, never name+file).
   - **5c. Meaning search (gated — see §6):** chunk per-symbol (cgc boundaries, fixed-window fallback), embed via Bedrock Titan v2 (1024-dim, cosine), write to S3 Vectors tagged with repo + `source_type`.
   - **5d. SBOM:** see §7.
   - **5e. Wiki:** DeepWiki generates prose → S3 (human browse) + S3 Vectors (the embedding target, because **wiki prose embeds better than raw code**).
6. **Update the catalog.** Record new SHA, per-stage success/failure, ACL list, and a run-log row in `index_runs`.
7. **Clean up.** Discard scratch. Everything valuable is now in S3 / S3 Vectors / Postgres / Neptune.

---

## 6. Decision: semantic embeddings for code are GATED OFF by default

This is the one strategic call the prior docs disagreed on. **Resolution: do not run semantic embeddings for *code* in v1. Reserve the semantic tier for *wiki/doc prose and natural-language queries* — the genuine vocabulary-mismatch cases.**

**Why:**
- The first deploy is direct evidence: exact search scored **63%** and browse **93%** while the S3 Vectors code index came up **empty/broken — and the system still worked**. Semantic-for-code was not load-bearing.
- A capable agent already translates intent into precise boolean/structural queries (the Probe argument), so embeddings solve a vocabulary-mismatch problem the agent largely handles itself — for code.
- Wiki prose and NL questions are the real mismatch cases, so the semantic tier **stays on for the wiki sink (§5e)**.

**What this means concretely:** keep the S3 Vectors plumbing and the wiki embedding path; **do not** invest in repairing/expanding code embeddings until there's evidence they beat Zoekt + structural on a real eval. The chunker/symbol-span work (cgc boundaries) is retained because it also feeds the structural tier. Revisit if a future eval shows a gap that only semantic-for-code closes.

> **Priority correction that follows from this:** the **structural/AST tier is our strongest, least-built bet** and should be built next (the MCP server + `understand`/`impact` backends), rather than repairing the semantic-for-code index. See §9.

---

## 7. SBOM + autonomous vulnerability remediation

### 7.1 Dual-rail SBOM generation
Two rails, because there are two layers of "ingredients":

| Rail | Tool | Sees | Misses | When |
|---|---|---|---|---|
| **Source** | `syft dir:` in the ingestion worker | App's own declared dependencies, every repo | OS packages in a built container | **Every** indexed repo (cheap, non-blocking) |
| **Image** | `docker build` + `syft <image>` on CodeBuild | OS packages + base image + what's actually installed | Repos with no Dockerfile / failing builds | Best-effort for repos **with a Dockerfile**, SHA-gated |

SBOMs are written as **CycloneDX** to S3 (the canonical record); dependency rows in Postgres are a derived index. Image builds that fail are recorded as honest **coverage gaps**, not silently skipped. Guardrails: CodeBuild concurrency capped, per-build timeout, only build on change.

### 7.2 Vulnerability matching — correct tool/format pairing
| Job | Tool | License | Format reality |
|---|---|---|---|
| Generate SBOM | **Syft** | Apache-2.0 | emits CycloneDX |
| Match OS/base-image layer | **Trivy** | Apache-2.0 | **consumes the CycloneDX SBOM** |
| Match ecosystem packages (npm/PyPI/Go/Cargo…) | **OSV-Scanner** | Apache-2.0 | **scans lockfiles/manifests directly — it does NOT consume CycloneDX** |

> This pairing corrects a factual error in the older design-of-record (§7.4 there claimed "OSV-Scanner matches our SBOMs"). OSV-Scanner reads lockfiles; Trivy is the one that ingests the CycloneDX SBOM. Both are Apache-2.0, so the chain stays commercially clean.

### 7.3 The reverse lookup (why this is a product)
A normal SBOM answers "what does repo X use?" The valuable question is the reverse — "**which repos use dependency Y?**" — answered instantly via a Postgres reverse-index on the package coordinate (purl):

```
Ask: "which repos use requests 2.28?"
SQL: SELECT repo FROM dependencies WHERE package = 'pkg:pypi/requests@2.28'
Get: [repo-A, repo-B, … 14 repos]  (one indexed query)
```

A global advisory ("lodash 4.17.20 is vulnerable") becomes one join `vulnerabilities → dependencies → repositories` returning every affected repo and its owner.

### 7.4 The loop
```
1. DETECT   OSV-Scanner (packages) + Trivy (OS/image) → match advisories.
            Reverse-index → exactly which repos + files are affected.
2. TRIAGE   Reachability check via the Neptune call graph: is the vulnerable
            code actually reached? Suppress false positives.
            Fail-safe: if no graph data, file the issue anyway.
3. FIX      File one fix issue per affected repo → existing developer agents
            pick it up, fix, and run tests. (Reused, already exists.)
4. VERIFY   Tests pass → open PR. Tests fail → agent retries. NEVER auto-merge.
5. REMEMBER Record "fixed CVE-X across N repos, tests passed" as a verified
            Experience-layer lesson.
```
Wave-based rollout (e.g. 5 repos/wave, spaced intervals); severity gate HIGH+CRITICAL; idempotency on `(cve_id, repo_id)` in a `remediation_runs` table. Steps 3–5 already exist in agent-factory; the SBOM is the missing piece that points them at the right work.

---

## 8. Permissions (ACL)

The storage engines (Zoekt, S3 Vectors, Neptune) have **no** built-in per-user rules. We enforce at the **MCP server**, fail-closed:

1. **Index time:** mirror each repo's GitHub access list into Postgres `allowed_principals`.
2. **Query time:** every result names its repo; after search, drop any result from a repo the caller can't see.
3. **Unknown caller → return nothing.** Fail safe, never fail open.

Identity is **header-based and pluggable**: the verbs trust `X-GitHub-Login` / `X-GitHub-Teams` (GitHub realm) and `X-Owner-Sub` / `X-Tenant-Id` (Cognito realm) set by *a* dispatch layer. Do **not** hard-wire ADP Cognito into the verbs — this is a portability seam (§12). For future personal/private context, S3 Vectors supports a **per-user index** so one user's data physically cannot appear in another's results.

---

## 9. The MCP server and its verbs

One server (`context-mcp`, port 5100) is the single query surface, registered in-process into agent runtimes as `AgentTool[]` (in-process `KnowledgeLayerPort`, not an external MCP transport hop). It is also reachable over REST (`GET /tools`, `POST /call {name, arguments}`) and MCP Streamable HTTP (`/mcp/`). Build it **stateless** (request/response) and **serverless from day one** (Lambda + API Gateway, or Fargate) — nothing to migrate later.

**Six verbs are deployed** — four retrieval verbs and two memory/Experience-layer verbs. This reflects the live `context-mcp` surface (as deployed), which differs from the originally-planned five `knowledge_*` retrieval verbs in two ways: there is **no standalone `search_semantic`** (semantic is folded into `search`, consistent with the §6 decision to gate semantic-for-code), and the **`remember` / `experience` write verbs** are exposed (the Experience layer — see the product-vision moat).

### Retrieval verbs

| Verb | Signature | Backed by | Notes |
|---|---|---|---|
| `search` | `search(query*, scope, limit)` | Zoekt (+ docs/learnings) | Find relevant code, documentation, and past learnings. Semantic-for-code stays gated (§6); semantic applies to wiki/doc prose only. |
| `understand` | `understand(target*, depth)` | `code-index.json` + Neptune | Deep understanding (structure summary) of a repo, directory, or file. |
| `impact` | `impact(target*, cross_repo)` | Neptune (call graph) | Complete caller set for a symbol before edit/delete. Verdict-first, ranked, bounded ≤100, grouped by repo; cross-repo via `symbol_id`. Prefer over grep for blast-radius. |
| `browse` | `browse(action*, uri*, depth)` | catalog + S3 | Navigate the indexed content filesystem; carries coverage metadata so agents discover what's indexed (no separate discovery tool). |

### Memory / Experience verbs (write side)

| Verb | Signature | Notes |
|---|---|---|
| `remember` | `remember(session_id*, messages*, outcome)` | Save session context, decisions, and learnings to long-term memory. |
| `experience` | `experience(action*, persona*, content, learning_type, context, query, visibility, limit, cross_persona)` | Save or recall experiential knowledge — per-user, persona-scoped, synthesized. This is the Experience layer (the product-vision moat: outcome-verified experience on top of retrieval). |

Neptune query constraints are baked in: no `shortestPath()`/APOC/`FOREACH`; never put inline map literals in `collect()` (Bug #1611 — use `collect(node)` + project in Python); fallback to `code-index.json` with `"source": "code-index-fallback"`; never return a silent `[]` on error — raise `NeptuneQueryError`.

**ACL is enforced here, fail-closed (§8):** verified on the live Door — a `search` with no `X-GitHub-Login` identity header returns `{"results": [], "total": 0}`.

**Build order priority (from §6):** structural tier (`understand`/`impact`) first; it is the strongest, least-built tier (the live Door currently returns exact `search`/`browse` results but empty `understand` — the structural index/Neptune graph is the gap). Semantic-for-code stays gated.

---

## 10. Database schema (`agent_context`)

Own database on the shared RDS instance; own Alembic env (`AC_RDS_*` prefix, migrations from 001); idempotent Terraform-managed bootstrap.

| Table | Purpose | Key columns |
|---|---|---|
| `repositories` | Catalog + ACL + per-stage status | `id, repo_name, git_url, owner, allowed_principals (JSONB), last_sha, {zoekt,structural,semantic,sbom,wiki}_status` |
| `dependencies` | SBOM reverse-index | `id, repo_id, package_coordinate (purl), version, is_transitive, source ('code'|'image')` |
| `vulnerabilities` | Advisory cache | `id, cve_id, package, affected_versions, safe_version, details` |
| `index_runs` | Observability log | `id, repo_id, stage, started_at, duration, status, message` |
| `remediation_runs` | Vuln-loop idempotency + audit | `(cve_id, repo_id)` unique, status, PR link |

S3 is canonical for SBOMs/wikis/indexes; Postgres rows are derived and rebuildable.

---

## 11. What we reuse vs. build new

| Piece | Reuse / New |
|---|---|
| SQS + KEDA parallel pipeline | **Reuse** |
| Clone repos to disk | **Reuse** |
| Structure-map (cgc/tree-sitter) | **Reuse** (storage path fixed: S3-backed PVC, not ephemeral `/tmp`) |
| Bedrock for embeddings | **Reuse** |
| Syft (SBOM) | **Reuse** |
| Developer agents that fix code | **Reuse** (whole agent-factory) |
| Postgres catalog/deps/vuln tables | **New tables, reused DB** |
| Zoekt (vs Sourcebot) | **New** — smaller, simpler deploy |
| S3 Vectors (vs OpenViking) | **New** |
| Neptune call graph (vs Neo4j) | **New** — managed, no copyleft |
| OSV-Scanner + Trivy (vs Grype) | **New** — Apache-2.0 |
| Reverse dependency lookup | **New** |
| ACL filter at MCP server | **New** — the security gate |

---

## 12. Compute platform: serverless target + portability

**Storage/data plane is already serverless** (S3, S3 Vectors, SQS, Bedrock, Neptune Serverless; RDS is the one provisioned store, serverless-capable). Ingestion is KEDA scale-to-zero. The non-serverless residue is standing compute: MCP server, litellm-proxy, deepwiki, and **Zoekt**.

| Component | Target form |
|---|---|
| MCP server | Build serverless from the start (Lambda/API GW or Fargate) |
| litellm-proxy | Fargate, or drop and call Bedrock directly |
| ingestion | Fargate tasks (not Lambda — big repos exceed 15-min limit) |
| deepwiki | Fargate, scale-to-zero |
| **Zoekt** | The one real misfit — see below |

**The Zoekt decision = the §6 decision.** Zoekt-as-a-daemon (standing pod + EBS volume + the only break in "durable = S3" + a recurring deploy crash-loop) is the single obstacle to fully-serverless. The alternative is **Probe's model — no index server, a binary invoked per-query (ripgrep + AST over the cloned tree)** — which is inherently serverless and is the same structural-over-semantic move as §6. We keep EBS-backed Zoekt **now** (it serves a live index and unblocks the deploy), but the EBS requirement is the trigger to **schedule the Probe evaluation** — no longer a "someday maybe."

**Sequencing:** fix the logic on the current stack first (it hasn't deployed cleanly even once); *then* migrate compute, since the data plane won't change. Preserve the portability seams: header-based identity (don't hard-wire Cognito), AWS-services-not-platform runtime, Terraform remote-state lookups acceptable as direct inputs, and the `agent_context` DB toggleable to its own instance.

---

## 13. Out of scope here (separate documents)

These stay as their own docs — they are not the core code Knowledge Layer being built now (the north-star architecture that *was* a separate vision brief is now folded in as §16):

- **IaC dependency graph (#1647)** — a *separate graph domain* in the same Neptune cluster (`:InfraResource` labels, `iac:` id prefix). **Design locked, implementation decision-gated** behind the code arm proving agent usage.
- **Live AWS resource graph (#1546)** — **research only** (recommends AWS Config → Neptune); pre-design, gated behind the loader role (#1532) and IaC schema (#1545).
- **Personal/private context (#1287)** — separate EPIC; blocked on GitHub-sender → cognito_sub mapping (#1319). Enterprise/shared code indexing ships first.

---

## 14. Reconciliations (where the old docs disagreed)

| Topic | Old conflict | **Resolved here** |
|---|---|---|
| Semantic embeddings for code | design-1348 built them; storage §12.1 argued de-scope | **Gated OFF for code; semantic tier reserved for wiki/NL** (§6) |
| S3 Vectors write throughput | 2,500/s/index (design-1348) vs 1,000/s/index (#1357) | **1,000/s/index** → shard into **3–5** indexes, not 4 |
| OSV-Scanner input format | "consumes CycloneDX SBOM" (storage §7.4, design-1348) | **False — OSV scans lockfiles; Trivy consumes CycloneDX** (§7.2) |
| Code search engine | Zoekt (#1346) vs lingering Sourcebot refs (#1357) | **Zoekt** at :6070, all Sourcebot references retired (§3, §9) |
| Deep graph store | Neo4j (#1512) vs Neptune (#1529) | **Neptune Serverless**; Neo4j superseded/archived (GPLv3) |
| Image-SBOM bucket | platform-data vs security-scans | **security-scans bucket** for image SBOMs (newer #1358 review) |
| Syft pin | v1.11.1 vs v1.45.1 | **v1.45.1** (newer) |
| Image build timeout | 10 vs 15 min | **15 min** per build, 30 min overall |

---

## 15. Open questions (genuinely undecided)

1. **Confirm S3 Vectors GA + region** before committing the wiki semantic path.
2. **Probe-vs-Zoekt evaluation** (§12) — schedule it; the EBS requirement is the trigger.
3. **`browse`/file-tree** — keep rebuilt-from-catalog (recommended, low priority) or drop for v1?
4. **Consolidated SBOM scope** — one corpus-wide bill, and does it mix our repos with study repos or keep them separate? (Reverse index works either way.)
5. **Image-build cost** — tune concurrency caps and timeouts once we have real numbers.

---

## 16. North-star architecture (where this is heading)

> **Read §1–§15 as the system we build now; read §16 as the destination.** This section is the
> former `vision-federated-code-intelligence.md` brief, folded in. It is a **roadmap, not a
> description of shipped capability** — the §16.1 table is the honest map of vision-vs-today.
> The target: unify fast text retrieval, compiler-level semantics, framework routing, structural
> analytics, semantic vectors, wiki data, and IaC config into a single **developer property graph**.

### 16.1 Current state vs. this vision

| Capability | Today | Reference |
|---|---|---|
| Zoekt trigram string search | ✅ Live (`search`, ~72% eval) | #1529 |
| SCIP compiler-verified code graph | ✅ Live (Neptune; `understand`/`impact`) | #1529, #1611, #1635 |
| Tree-Sitter fallback when SCIP fails | ⚠️ Partial — SCIP-native today; Tree-Sitter base not built | §16.3 Tier 1 |
| Implicit framework route resolution | ❌ Not built | §16.3 Tier 2 / E2 #1666 |
| IaC / infrastructure topology graph | 🔵 Designed, not built | #1647, #1545 |
| Live AWS account asset graph | 🔵 Researched only | #1546 |
| Wiki/doc semantic blending (DeepWiki) | ⚠️ Partial — present, not graph-fused | #1382 |
| Local vector semantic search (for code) | ⚠️ Gated off by design (§6) | #1529 |
| Native MCP Door (single endpoint) | ✅ Live (Streamable HTTP `/mcp`) | #1602 |
| Agents wired to the Door | ✅ Built, feature-flagged off | #1592 |
| `calculate_blast_radius` | ✅ Live as the `impact` verb | #1635 |
| `get_architecture_clusters` (Louvain) | ❌ Not built | §16.4 / E4 #1667 |
| `detect_system_dead_code` | ❌ Not built | §16.4 / E4 #1667 |
| Hybrid local-daemon / cloud topology | ❌ Cloud-backend only; no edge daemon | §16.2 / E5 #1668 |

**Single biggest divergence:** the platform today is **cloud-backend only** (Zoekt + Neptune + MCP server on EKS). The "local edge daemon" half of the hybrid topology does not exist — it is the largest unbuilt piece of the vision.

### 16.2 Hybrid federated topology (the target)

An asymmetric execution model pairing a local edge with the cloud hub:

- **The Edge — local workspace daemon.** A lightweight compiled C++/Go binary running as an MCP sidecar on the developer's machine: local SQLite + FTS5 index, OS file-watch for uncommitted edits, and on-device embeddings (e.g. `nomic-embed-code`) — sub-millisecond local tasks, no cloud roundtrip. **Not built; decision-gated (E5 #1668).**
- **The Hub — enterprise cloud backend.** What exists today: Zoekt for high-throughput regex/literal search + AWS Neptune as the master property graph for cross-repo links. Handles compute-heavy ingest sweeps and complex cross-repo inquiries.

### 16.3 Four-tier ingestion (the target pipeline)

1. **Tier 1 — SCIP + Tree-Sitter hybrid.** Tree-Sitter (158-language, no build step) builds an error-tolerant structural base layer; SCIP asynchronously enriches it with compiler-verified types. If SCIP fails on broken code, the Tree-Sitter layer remains — the graph never goes dark. *(Today: SCIP-native; the Tree-Sitter base is the gap.)*
2. **Tier 2 — framework route resolvers.** Custom extractors for 17+ web frameworks (FastAPI, Django, Spring Boot, Express, NestJS…) turn route declarations (`@app.route('/v1/users')`) into explicit endpoint→handler edges. Adopt CodeGraph's *rules* (re-implement, don't vendor — see §16.6). *(E2 #1666.)*
3. **Tier 3 — IaC / infra topology.** Parse Terraform/OpenTofu/Pulumi (source HCL via `python-hcl2`, and/or `tfstate`) into infrastructure nodes linking app logic to the resources hosting it. *(Designed: #1647; see §13.)*
4. **Tier 4 — wiki semantic blending.** Scrape Markdown/Confluence/Notion, entity-match doc concepts against code symbols, and attach text abstracts as graph node metadata so docs and code stop living in separate silos. *(Partial via DeepWiki #1382.)*

### 16.4 Target property-graph schema + analytical verbs

**Converge toward 5 node sets** (the §7-pruned target, not the full superset): INFRASTRUCTURE (`AWS_RESOURCE`), ROUTING/PIPELINE (`INFRA_ROUTE`, `CI_JOB`), CODE LOGIC (`FILE`/`FUNCTION`/`CLASS`), CONTEXT (`WIKI_CONCEPT`), MEANING-MATCHES (embedding/structural-duplicate links). Edges: `CONTAINS`, `CALLS`, `HANDLED_BY`, `HOSTS_SERVICE`, `READS_FROM`/`WRITES_TO`, `DOCUMENTS`, `SEMANTICALLY_RELATED`, `SIMILAR_TO`.

> **Reconciliation with today's graph:** Neptune currently uses `:Symbol` nodes (SCIP monikers) with `CALLS`/`REFERENCES`; #1647 adds `:InfraResource`/`:InfraModule`/`:InfraProvider`. The richer taxonomy above is the *target to converge toward*, not today's shape.

**Target analytical verbs** (beyond the five in §9): `calculate_blast_radius(git_diff)` (the diff-interception evolution of today's target-based `impact`), `get_architecture_clusters(repo_id)` (Louvain community detection over Neptune), `detect_system_dead_code(repo_id)` (functions with zero incoming call edges → refactor tasks). *(E4 #1667.)*

### 16.5 Operational/runtime intelligence is a SEPARATE pillar

A `diagnose_pod_failure(pod_id)` verb — runtime error → log → CI job → offending commit line — is **arguably the highest-value autonomous capability and the least started**. It is deliberately carved out, **not** part of the static core, because:
- **Different data nature** — everything else is *parsed static artifacts* (parse-once → property graph); operational data is *time-series telemetry* (streaming, TTL, alert correlation), a different ingestion model entirely.
- It overlaps the parked live-AWS-asset-graph research (#1546).

**Decision:** the static-code platform proceeds first; operational/runtime intelligence is its own future EPIC (E6 #1669), sequenced *after* the static graph is real and used daily.

### 16.6 License posture for vision components

Every source project the vision names is permissive (audited 2026-06-21 at the LICENSE-file level — this EPIC was twice burned by assumed licenses: Neo4j-GPLv3 and a cgc-docs error): `codegraph` MIT, `codebase-memory-mcp` MIT, `nomic-embed-code` Apache-2.0, `tree-sitter` MIT, Zoekt Apache-2.0. **Caveats:** top-level MIT ≠ transitive MIT (a full dependency SBOM is required before vendoring); default to **adopt-technique, re-implement — don't vendor**; the on-device inference stack (tokenizer + serving runtime) is a separate Phase-3 check; re-verify the exact commit/tag at adoption time.

### 16.7 Roadmap phases (vision sequencing)

Phase 1 (M1–2): hybrid semantic core + ingestion framework (edge daemon scaffold, Neptune base schema, Tree-Sitter fallback). Phase 2 (M3–4): framework route + IaC edge mapping. Phase 3 (M5–6): analytical intelligence (on-device embeddings, blast-radius traversal, Louvain clustering). Phase 4 (M7+): production rollout — distribute the edge daemon as an MCP package for Claude Code/Cursor, measure the >50% token-reduction target. These map onto the E1–E7 child EPICs (see `knowledge-layer-product-vision.md` §8).
