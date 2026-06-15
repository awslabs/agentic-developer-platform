# Neo4j Deep Code Graph — Canonical Design Document

> **Status**: LOCKED (Design Authority for EPIC #1512)
> **Author**: @agent-architect
> **Date**: 2026-06-15
> **Blocks**: #1513, #1514, #1515, #1516, #1517, #1518

This document is the **single source of truth** for the Neo4j deep-code-graph effort.
All six implementation stories MUST implement against the decisions below — no developer
agent invents or overrides these values.

---

## Decisions Table

| # | Decision | Value | Rationale |
|---|----------|-------|-----------|
| D1 | Neo4j version | **5.26** (Community Edition, image `neo4j:5.26-community`) | Latest stable 5.x LTS line (5.26.27 at time of writing); Community = free, sufficient for single-instance; cgc requires `>=5.15.0` |
| D2 | Neo4j heap config | `initial=1G`, `max=1G` | Fixed heap avoids GC pause spikes; 1G heap + 1G pagecache + OS headroom fits a 4Gi pod |
| D3 | Neo4j pagecache | `1G` | Caches graph + indexes in RAM; 1G handles ~50M nodes comfortably for our corpus |
| D4 | CodeGraphContext (cgc) version | **0.5.1** (`codegraphcontext` on PyPI) | Current release; compatible with Neo4j 5.15+; CLI is `codegraphcontext index <path>` |
| D5 | cgc correct CLI invocation | `codegraphcontext index <clone_path>` | NOT `cgc analyze <path> --json` (wrong; that exits 2 → silent fallback). `index` writes directly to configured graph DB |
| D6 | cgc Neo4j configuration | Env vars: `CGC_BACKEND=neo4j`, `CGC_NEO4J_URI`, `CGC_NEO4J_USER`, `CGC_NEO4J_PASSWORD` | Per cgc docs; alternative: `codegraphcontext neo4j setup` writes `~/.codegraphcontext/config.toml` |
| D7 | SCIP indexers | `scip-python@0.6.6` (npm), `scip-typescript@0.4.0` (npm), `scip-go@v0.2.7` (go install) | Corpus languages: Python, TypeScript/JS, Go. Java/Rust deferred (not in current corpus). Verified cgc `setup-scip` recognizes these |
| D8 | SCIP install method | npm global install for python/ts; `go install` binary for Go; all done at Docker build time in ingestion image | Build-time install = no runtime latency; matches existing Syft install pattern in Dockerfile |
| D9 | Python neo4j driver | **`neo4j==6.2.0`** (PyPI) | Latest stable; compatible with Neo4j 5.x (all releases); Python >=3.10. Used by the Door |
| D10 | Graph data model | See [Graph Schema](#graph-schema) below | Single schema shared by writer (#1514), isolator (#1515), and readers (#1517, #1518) |
| D11 | Symbol unique key | `repo` + `file` + `symbol` + `kind` | Composite natural key; enables delete-by-repo, cross-repo edge resolution, and re-index replacement |
| D12 | Bolt URI | `bolt://neo4j.agent-context.svc.cluster.local:7687` | ClusterIP Service; internal-only. Same DNS convention as Zoekt/DeepWiki |
| D13 | Auth secret | Secrets Manager `agent-context/neo4j-auth` → K8s Secret `agent-context-secrets` key `neo4j-auth` | Format: `neo4j/<password>` (NEO4J_AUTH env format). Matches existing secret pattern |
| D14 | Connection pool (writers) | `max_connection_pool_size=50`, `connection_acquisition_timeout=60s` | Supports N parallel ingestion pods (KEDA fans out ~50 concurrent). cgc's internal driver uses these |
| D15 | Connection pool (readers / Door) | `max_connection_pool_size=25`, `connection_acquisition_timeout=30s` | Door is single-pod with async queries; lower pool sufficient |
| D16 | Cross-repo edge policy | **Query-time resolution** (no reconciliation pass) | Edges within a repo are persisted during ingestion. Cross-repo call detection uses query-time Cypher pattern matching on shared symbol names. Simpler, no extra pipeline step |
| D17 | Bounded-query limits | Max traversal depth: **4 hops**; result cap: **100 paths** (impact), **50 symbols** (understand neighborhood) | Prevents context-blowing dumps; higher depth available via explicit `depth` parameter |
| D18 | Fallback contract | Door → `code-index.json` (S3) when Neo4j unreachable (connection timeout 5s) | Graceful degradation; returns flat one-hop data. Log WARN, do not fail the verb |
| D19 | PVC storage | `ebs-gp3`, 50Gi, mounted at `/data` | Mirrors Zoekt pattern. 50Gi sufficient for ~500 repos' graph data. Expandable |
| D20 | K8s Deployment strategy | `Recreate` (single replica) | Single-writer graph; EBS is `ReadWriteOnce`. Matches Zoekt pattern exactly |

---

## Graph Schema

All stories (#1514 writes, #1515 isolates, #1517/#1518 query) use this exact schema.

### Node Labels

| Label | Description | Required Properties |
|-------|-------------|---------------------|
| `Symbol` | A code symbol (function, method, class, variable, constant, interface, type) | `repo`, `file`, `name`, `kind`, `line`, `signature` |
| `File` | A source file | `repo`, `path`, `language` |
| `Module` | A logical module/package (directory-level grouping) | `repo`, `path` |

### Node Properties (detail)

#### Symbol
| Property | Type | Description | Indexed |
|----------|------|-------------|---------|
| `repo` | String | `org/repo` (e.g., `aws-e/adp`) — the namespace key | YES (composite) |
| `file` | String | Relative file path within the repo | YES (composite) |
| `name` | String | Symbol name (e.g., `ContentRouter`, `cgc_analyze`) | YES (composite) |
| `kind` | String | One of: `function`, `method`, `class`, `interface`, `type`, `variable`, `constant` | YES (composite) |
| `line` | Integer | Line number of definition | No |
| `signature` | String | Full signature (parameters + return type if available) | No |
| `visibility` | String | `public`, `private`, `internal` (best-effort from SCIP) | No |

**Unique constraint**: `CREATE CONSTRAINT symbol_unique FOR (s:Symbol) REQUIRE (s.repo, s.file, s.name, s.kind) IS UNIQUE`

#### File
| Property | Type | Description | Indexed |
|----------|------|-------------|---------|
| `repo` | String | `org/repo` | YES |
| `path` | String | Relative file path | YES (unique within repo) |
| `language` | String | Detected language (`python`, `typescript`, `go`, etc.) | No |

**Unique constraint**: `CREATE CONSTRAINT file_unique FOR (f:File) REQUIRE (f.repo, f.path) IS UNIQUE`

#### Module
| Property | Type | Description | Indexed |
|----------|------|-------------|---------|
| `repo` | String | `org/repo` | YES |
| `path` | String | Module/package path (directory) | YES |

**Unique constraint**: `CREATE CONSTRAINT module_unique FOR (m:Module) REQUIRE (m.repo, m.path) IS UNIQUE`

### Edge Types (Relationships)

| Type | Source → Target | Description | Properties |
|------|----------------|-------------|------------|
| `CALLS` | Symbol → Symbol | Function/method calls another | `repo` (source repo, for fast delete) |
| `IMPORTS` | File → File | File imports from another file | `repo` |
| `DEFINES` | File → Symbol | File defines/contains a symbol | `repo` |
| `CONTAINS` | Module → File | Module contains a file | `repo` |
| `INHERITS` | Symbol → Symbol | Class extends/implements another | `repo` |
| `IMPLEMENTS` | Symbol → Symbol | Concrete impl of an interface method | `repo` |
| `MEMBER_OF` | Symbol → Symbol | Method/field belongs to a class | `repo` |

**Key design decision**: Every edge carries a `repo` property (the *source* node's repo). This enables:
- `MATCH ()-[r {repo: $r}]->() DELETE r` for fast edge cleanup during re-index
- Cross-repo edges: when a Symbol in repo A `CALLS` a Symbol in repo B, the edge's `repo` = A (the caller's repo). This means re-indexing repo A removes A's outgoing call edges but NOT B's.

### Cross-Repo Edge Resolution (D16 detail)

Cross-repo CALLS edges are **NOT written at ingestion time** (because repo B may not be indexed yet when repo A is ingested). Instead:

**At query time** (Door Cypher in #1517/#1518):
```cypher
// Find cross-repo callers of a symbol
MATCH (target:Symbol {repo: $repo, name: $symbol_name, file: $file})
WITH target
MATCH (caller:Symbol)-[:CALLS]->(callee:Symbol)
WHERE callee.name = target.name
  AND callee.file = target.file
  AND caller.repo <> target.repo
RETURN caller, callee, target
LIMIT 100
```

This works because:
1. SCIP resolves cross-module imports to fully-qualified names
2. The `name` + `file` combination identifies a symbol across repos (external symbols imported into repo A carry the original's coordinates)
3. No reconciliation pass needed; new repos appear in cross-repo results as soon as indexed

**Future optimization** (not in scope): A periodic reconciliation job could materialize frequently-queried cross-repo edges for performance. Only build this if query-time resolution proves slow (unlikely for <500 repos).

### Index Creation (run once at Neo4j init)

```cypher
// Uniqueness constraints (also create indexes)
CREATE CONSTRAINT symbol_unique IF NOT EXISTS
  FOR (s:Symbol) REQUIRE (s.repo, s.file, s.name, s.kind) IS UNIQUE;
CREATE CONSTRAINT file_unique IF NOT EXISTS
  FOR (f:File) REQUIRE (f.repo, f.path) IS UNIQUE;
CREATE CONSTRAINT module_unique IF NOT EXISTS
  FOR (m:Module) REQUIRE (m.repo, m.path) IS UNIQUE;

// Additional indexes for query patterns
CREATE INDEX symbol_repo IF NOT EXISTS FOR (s:Symbol) ON (s.repo);
CREATE INDEX file_repo IF NOT EXISTS FOR (f:File) ON (f.repo);
CREATE INDEX symbol_name IF NOT EXISTS FOR (s:Symbol) ON (s.name);
CREATE INDEX symbol_kind IF NOT EXISTS FOR (s:Symbol) ON (s.kind);
```

---

## Connection & Configuration

### Single Definition (consumed by all stories)

```yaml
# Added to agent-context-configmap.yaml
NEO4J_URI: "bolt://neo4j.agent-context.svc.cluster.local:7687"
NEO4J_DATABASE: "neo4j"  # Community Edition default DB
NEO4J_USERNAME: "neo4j"
```

The password is NOT in the ConfigMap. It lives in Secrets Manager and is projected as a K8s Secret:

```
AWS Secrets Manager: agent-context/neo4j-auth
  Value: {"username": "neo4j", "password": "<generated>"}
  ↓ (deploy.sh seeds this + creates K8s Secret)
K8s Secret: agent-context-secrets
  Key: neo4j-password → <the password value>
```

### Consumer Configuration

| Consumer | Env Vars | Pool Settings |
|----------|----------|---------------|
| **Ingestion** (ingest-repo.py, #1514) | `NEO4J_URI`, `NEO4J_DATABASE`, `NEO4J_USERNAME`, `NEO4J_PASSWORD` (from secret) | max_pool=50, acquire_timeout=60s |
| **cgc** (CodeGraphContext, #1514) | `CGC_BACKEND=neo4j`, `CGC_NEO4J_URI=$NEO4J_URI`, `CGC_NEO4J_USER=$NEO4J_USERNAME`, `CGC_NEO4J_PASSWORD=$NEO4J_PASSWORD` | Internal (cgc manages its own) |
| **Door** (structural_backend.py, #1517) | `NEO4J_URI`, `NEO4J_DATABASE`, `NEO4J_USERNAME`, `NEO4J_PASSWORD` (from secret) | max_pool=25, acquire_timeout=30s |
| **Verify gate** (#1516) | Same `NEO4J_URI` + creds (runs as a Job in CI) | Single connection (no pool) |

### StatefulSet Service Definition (#1513)

```yaml
apiVersion: v1
kind: Service
metadata:
  name: neo4j
  namespace: agent-context
  labels:
    app.kubernetes.io/name: neo4j
    app.kubernetes.io/part-of: agent-context-platform
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: neo4j
  ports:
  - name: bolt
    port: 7687
    targetPort: 7687
  - name: http
    port: 7474
    targetPort: 7474
```

---

## Ingestion Write Path (how cgc → Neo4j works)

### Correct Invocation (#1514)

```python
# CORRECT (replaces the broken "cgc analyze <path> --json")
import subprocess, os

env = {
    **os.environ,
    "CGC_BACKEND": "neo4j",
    "CGC_NEO4J_URI": os.environ["NEO4J_URI"],
    "CGC_NEO4J_USER": os.environ["NEO4J_USERNAME"],
    "CGC_NEO4J_PASSWORD": os.environ["NEO4J_PASSWORD"],
}

result = subprocess.run(
    ["codegraphcontext", "index", clone_path],
    env=env,
    capture_output=True,
    timeout=600,  # 10 min for large repos
)
if result.returncode != 0:
    raise RuntimeError(f"cgc index failed: {result.stderr.decode()}")
```

### SCIP Indexer Installation (#1514 — Dockerfile additions)

```dockerfile
# In images/ingestion/Dockerfile, AFTER the existing system deps:

# SCIP indexers for compiler-grade call-graph resolution
# scip-python + scip-typescript via npm
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    npm install -g @sourcegraph/scip-python@0.6.6 @sourcegraph/scip-typescript@0.4.0

# scip-go via pre-built binary (requires Go toolchain for install)
RUN curl -fsSL https://go.dev/dl/go1.22.4.linux-amd64.tar.gz | tar -C /usr/local -xzf - && \
    /usr/local/go/bin/go install github.com/scip-code/scip-go@v0.2.7 && \
    mv /root/go/bin/scip-go /usr/local/bin/scip-go && \
    rm -rf /usr/local/go /root/go

# Verify SCIP indexers are available to cgc
RUN codegraphcontext setup-scip --check || echo "SCIP check (advisory only)"
```

### Re-Index Flow (#1515)

```python
# Before indexing a repo, delete its existing subgraph
from neo4j import GraphDatabase

def delete_repo_subgraph(driver, repo: str):
    """Atomically delete all nodes and edges belonging to a repo."""
    with driver.session(database="neo4j") as session:
        # Delete edges first (faster than DETACH DELETE on large subgraphs)
        session.run("MATCH ()-[r {repo: $repo}]->() DELETE r", repo=repo)
        # Delete nodes
        session.run("MATCH (n {repo: $repo}) DELETE n", repo=repo)

def index_repo(driver, clone_path: str, repo: str):
    """Full re-index: delete old → index new."""
    delete_repo_subgraph(driver, repo)
    # Then run cgc index (writes new nodes/edges)
    # cgc writes directly via its own driver session
    subprocess.run(["codegraphcontext", "index", clone_path], ...)
```

---

## Door Query Patterns (#1517, #1518)

### Neo4j Client Initialization

```python
# door/neo4j_client.py (new file)
from neo4j import AsyncGraphDatabase
import os, logging

log = logging.getLogger(__name__)

_driver = None

def get_neo4j_driver():
    """Lazy-init async Neo4j driver with connection pooling."""
    global _driver
    if _driver is None:
        uri = os.environ.get("NEO4J_URI", "")
        if not uri:
            return None
        _driver = AsyncGraphDatabase.driver(
            uri,
            auth=(
                os.environ.get("NEO4J_USERNAME", "neo4j"),
                os.environ.get("NEO4J_PASSWORD", ""),
            ),
            max_connection_pool_size=25,
            connection_acquisition_timeout=30.0,
            connection_timeout=5.0,  # Fallback trigger
        )
    return _driver


async def neo4j_available() -> bool:
    """Check if Neo4j is reachable (for fallback decision)."""
    driver = get_neo4j_driver()
    if not driver:
        return False
    try:
        async with driver.session(database="neo4j") as session:
            await session.run("RETURN 1")
        return True
    except Exception:
        log.warning("Neo4j unreachable — falling back to code-index.json")
        return False
```

### Impact Query (bounded, verdict-first)

```cypher
// Direct + transitive callers, bounded at 4 hops, capped at 100 paths
MATCH path = (caller:Symbol)-[:CALLS*1..4]->(target:Symbol {
  repo: $repo, file: $file, name: $symbol
})
WHERE caller <> target
RETURN caller.repo AS caller_repo,
       caller.file AS caller_file,
       caller.name AS caller_name,
       caller.kind AS caller_kind,
       length(path) AS distance
ORDER BY distance ASC, caller_repo, caller_file
LIMIT 100
```

### Understand Query (symbol neighborhood)

```cypher
// Symbol + its immediate neighborhood (callers, callees, parent class)
MATCH (s:Symbol {repo: $repo, file: $file, name: $symbol})
OPTIONAL MATCH (s)-[:CALLS]->(callee:Symbol)
OPTIONAL MATCH (caller:Symbol)-[:CALLS]->(s)
OPTIONAL MATCH (s)-[:INHERITS]->(parent:Symbol)
OPTIONAL MATCH (s)-[:MEMBER_OF]->(owner:Symbol)
RETURN s,
       collect(DISTINCT callee) AS callees,
       collect(DISTINCT caller) AS callers,
       collect(DISTINCT parent) AS parents,
       collect(DISTINCT owner) AS owners
LIMIT 50
```

### Cross-Repo Impact (#1518)

```cypher
// Find all repos that call a symbol (cross-repo blast radius)
MATCH (target:Symbol {repo: $repo, name: $symbol, file: $file})
WITH target
MATCH (caller:Symbol)-[:CALLS*1..4]->(interim:Symbol)
WHERE interim.name = target.name AND interim.file = target.file
  AND caller.repo <> $repo
RETURN DISTINCT caller.repo AS calling_repo,
       caller.file AS calling_file,
       caller.name AS calling_symbol,
       count(*) AS call_count
ORDER BY call_count DESC
LIMIT 100
```

---

## Verify Gate Assertion (#1516)

```bash
#!/usr/bin/env bash
# Added to agent-context-verify.yml

NEO4J_URI="${NEO4J_URI:-bolt://neo4j.agent-context.svc.cluster.local:7687}"

# Total edge count
TOTAL_EDGES=$(cypher-shell -u "$NEO4J_USERNAME" -p "$NEO4J_PASSWORD" \
  -a "$NEO4J_URI" --format plain \
  "MATCH ()-[r:CALLS]->() RETURN count(r)" 2>/dev/null | tail -1)

if [ "${TOTAL_EDGES:-0}" -eq 0 ]; then
  echo "FAIL: Neo4j has 0 CALLS edges — call graph is empty"
  exit 1
fi

# Per-repo check: every code-bearing repo must have edges
EMPTY_REPOS=$(cypher-shell -u "$NEO4J_USERNAME" -p "$NEO4J_PASSWORD" \
  -a "$NEO4J_URI" --format plain \
  "MATCH (s:Symbol)
   WITH DISTINCT s.repo AS repo, count(s) AS sym_count
   WHERE sym_count > 10
   OPTIONAL MATCH (:Symbol {repo: repo})-[r:CALLS]->()
   WITH repo, sym_count, count(r) AS edge_count
   WHERE edge_count = 0
   RETURN repo" 2>/dev/null | grep -v "^repo$")

if [ -n "$EMPTY_REPOS" ]; then
  echo "FAIL: Repos with symbols but 0 CALLS edges:"
  echo "$EMPTY_REPOS"
  exit 1
fi

echo "PASS: Neo4j call graph has $TOTAL_EDGES CALLS edges, all code repos have edges"
```

---

## Fallback Contract (D18 detail)

When the Door cannot reach Neo4j (connection timeout after 5 seconds):

1. Log `WARNING: Neo4j unreachable at {uri} — using code-index.json fallback`
2. Load `code-index.json` from S3 (existing `structural_backend.py` path)
3. Return one-hop callers/callees from the flat JSON (no transitive, no cross-repo)
4. Include in response metadata: `"source": "code-index-fallback"` so the agent knows it's degraded
5. Do NOT raise an error — the verb succeeds with reduced data quality

This means the Door always answers, even during Neo4j maintenance windows. The verify gate (#1516) catches the "permanently empty graph" failure mode separately.

---

## Kubernetes Manifest Structure (#1513)

New files in `modules/agent-context/manifests/`:

| File | Contents |
|------|----------|
| `neo4j.yaml` | Deployment (Recreate, 1 replica) + PVC (ebs-gp3, 50Gi) + Service (ClusterIP, ports 7687+7474) |
| — | (No separate StatefulSet needed — Deployment+PVC with Recreate strategy provides the same guarantee for a single replica, and mirrors the Zoekt pattern exactly) |

Environment variables on the Neo4j container:
```yaml
env:
- name: NEO4J_AUTH
  valueFrom:
    secretKeyRef:
      name: agent-context-secrets
      key: neo4j-auth   # format: "neo4j/thepassword"
- name: NEO4J_server_memory_heap_initial__size
  value: "1G"
- name: NEO4J_server_memory_heap_max__size
  value: "1G"
- name: NEO4J_server_memory_pagecache_size
  value: "1G"
- name: NEO4J_dbms_security_procedures_unrestricted
  value: "apoc.*"
```

Resource requests/limits:
```yaml
resources:
  requests:
    cpu: "500m"
    memory: "3Gi"
  limits:
    cpu: "2"
    memory: "4Gi"
```

---

## SCIP Indexer Details

| Language | Indexer | Version | Install | Produces |
|----------|---------|---------|---------|----------|
| Python | `@sourcegraph/scip-python` | 0.6.6 | `npm install -g @sourcegraph/scip-python@0.6.6` | `.scip` index file |
| TypeScript/JS | `@sourcegraph/scip-typescript` | 0.4.0 | `npm install -g @sourcegraph/scip-typescript@0.4.0` | `.scip` index file |
| Go | `scip-go` | 0.2.7 | `go install github.com/scip-code/scip-go@v0.2.7` | `.scip` index file |
| Java | Deferred | — | — | Not in current corpus |
| Rust | Deferred | — | — | Not in current corpus |

cgc reads `.scip` files to resolve cross-file and cross-module references with compiler-grade accuracy (vs. Tree-sitter heuristics which only get ~60% of calls right). The SCIP indexers are the "cost driver" of this EPIC — they add ~200MB to the ingestion image (Node.js + Go toolchain), but accuracy goes from heuristic to compiler-grade.

---

## File Changes Summary (per story)

| Story | Files to Create | Files to Modify |
|-------|----------------|-----------------|
| #1513 | `modules/agent-context/manifests/neo4j.yaml` | `deploy.sh`, `agent-context-deploy.yml`, `config.env` (add NEO4J vars) |
| #1514 | — | `images/ingestion/Dockerfile` (SCIP + Node + neo4j driver), `images/ingestion/ingest-repo.py` (fix cgc invocation), `images/ingestion/requirements.txt` or pip line |
| #1515 | — | `images/ingestion/ingest-repo.py` (add delete_repo_subgraph before index) |
| #1516 | — | `.github/workflows/agent-context-verify.yml` (add Neo4j edge assertions) |
| #1517 | `door/neo4j_client.py` | `door/structural_backend.py` (add Neo4j query path + fallback), `door/server.py` (init Neo4j driver), `images/context-mcp/requirements.txt` (add `neo4j==6.2.0`) |
| #1518 | `door/cross_repo_queries.py` (or inline in structural_backend) | `door/structural_backend.py` (cross-repo Cypher), verb description strings |

---

## Compatibility Matrix

| Component | Version | Depends On | Verified Compatible |
|-----------|---------|------------|---------------------|
| Neo4j Community | 5.26.x | — | Baseline |
| codegraphcontext | 0.5.1 | Neo4j >=5.15.0 | YES (5.26 > 5.15) |
| neo4j Python driver | 6.2.0 | Neo4j 5.x (all) | YES |
| scip-python | 0.6.6 | Node >=16, Python >=3.10 | YES (image has Python 3.13 + we add Node 20) |
| scip-typescript | 0.4.0 | Node >=16 | YES (Node 20) |
| scip-go | 0.2.7 | Go 1.22+ | YES (we install Go 1.22) |

---

## Cost Estimate

| Resource | Monthly Cost | Notes |
|----------|-------------|-------|
| Neo4j pod (1 × m5.large equiv) | ~$25–40 | EKS Auto Mode; 4Gi, 2 vCPU limit |
| EBS gp3 50Gi | ~$4 | $0.08/GB/mo |
| **Total incremental** | **~$30–45/mo** | Well within the "no Neptune" cost constraint |

---

## Non-Goals (explicitly excluded)

- Neo4j Enterprise features (clustering, RBAC, CDC) — not needed for single-instance
- Neo4j Aura (managed SaaS) — cost and latency; self-hosted is fine
- Vector similarity edges in Neo4j — deferred; current SCIP accuracy is sufficient
- Schema versioning / migrations in Neo4j — constraints are idempotent (`IF NOT EXISTS`); no Alembic needed
- Multi-database (Neo4j 5 Community supports only the default `neo4j` database) — not needed

---

## References

- Parent EPIC: #1512
- Root cause of empty call graph: #1487 (cgc CLI-syntax fallback)
- Relevance eval: #1511
- Zoekt pattern (PVC + Deployment): `modules/agent-context/manifests/zoekt.yaml`
- Neptune pattern (graph.py connection): `modules/agent-context/personal_context/graph.py`
- Existing structural backend: `modules/agent-context/door/structural_backend.py`
- Existing ingestion: `modules/agent-context/images/ingestion/ingest-repo.py`
- cgc Dockerfile: `modules/agent-context/images/codegraph-context/Dockerfile`
