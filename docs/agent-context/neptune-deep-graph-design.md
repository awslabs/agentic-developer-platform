# Neptune Deep Code Graph — Canonical Design Document

> **Status**: LOCKED (Design Authority for EPIC #1529)
> **Author**: @agent-architect
> **Date**: 2026-06-15
> **Supersedes**: `neo4j-deep-graph-design.md` (EPIC #1512, closed — GPLv3 violation)
> **Blocks**: #1531, #1527, #1532, #1533, #1534, #1535, #1536

This document is the **single source of truth** for the Neptune deep-code-graph effort.
All seven child stories MUST implement against the decisions below — no developer
agent invents or overrides these values.

---

## Decisions Table

| # | Decision | Value | Rationale |
|---|----------|-------|-----------|
| D1 | Neptune engine version | **1.4.7.0** (Serverless) | Latest active release (2026-03-03), EOL 2027-06-03. Supports openCypher, Bulk Loader, IAM auth. Terraform default `1.3.4.0` is outdated — override in tfvars |
| D2 | Neptune Serverless NCU range | min: **1.0**, max: **16.0** | 1.0 NCU idles at ~$0.12/h; 16 handles burst ingestion. ~$105/mo steady-state |
| D3 | Endpoints | Bolt: `bolt+s://<cluster-endpoint>:8182`; HTTPS: `https://<cluster-endpoint>:8182/openCypher` | Single port 8182 for both protocols. TLS mandatory (SG allows only 8182 from EKS nodes) |
| D4 | Authentication | **IAM database auth** (SigV4-signed). No username/password stored | IRSA role per-pod signs requests. Python neo4j driver `NeptuneAuthToken` class (see Connection section). No Secrets Manager credential needed for Neptune itself |
| D5 | CodeGraphContext (cgc) version | **0.5.1** (`codegraphcontext` on PyPI) | Current release. CLI: `codegraphcontext index <path>`. Default backend: FalkorDB Lite (embedded, in-process, throwaway) |
| D6 | cgc backend for this pipeline | **FalkorDB Lite (default)** — cgc writes to embedded in-pod DB, NOT to Neptune | We extract from FalkorDB via `GRAPH.QUERY`, transform to Neptune CSV, then bulk-load. No bolt-write to Neptune. FalkorDB is throwaway (discarded after extraction) |
| D7 | SCIP indexers | `@sourcegraph/scip-python@0.6.6` (npm), `@sourcegraph/scip-typescript@0.4.0` (npm), `scip-go@v0.2.7` (go install) | Latest stable versions for Python/TS/Go (verified 2026-06-15). Java/Rust deferred |
| D8 | SCIP install method | npm global install (python/ts) + go install binary (Go) at Docker build time | Same pattern as Syft install. Adds ~200MB to ingestion image (Node 20 + Go 1.22 toolchain) |
| D9 | Python neo4j driver (for reads) | **`neo4j==6.2.0`** (PyPI) | Latest stable. Compatible with Neptune bolt :8182. Used by the Door for openCypher reads. Python >=3.10 required (image has 3.13) |
| D10 | Graph data model | See [Graph Schema](#graph-schema) below | Single schema shared by writer (#1532), isolator (#1533), and readers (#1535, #1536) |
| D11 | Node ID encoding (`~id`) | `{repo}\|{file}\|{name}\|{kind}` for Symbol; `{repo}\|{path}` for File; `{repo}\|module\|{path}` for Module | Pipe-separated composite. Neptune has NO schema constraints — uniqueness is ONLY via `~id`. Re-loading same `~id` = upsert (updateSingleCardinalityProperties=TRUE) |
| D12 | Cross-repo edge resolution | **Stable SCIP `symbol_id` (moniker)**, NEVER name+file matching | SCIP monikers are globally unique qualified identifiers. name+file matching produces false edges (same-named functions in different repos). REJECTED. |
| D13 | `symbol_id` format | SCIP moniker string (e.g., `python pkg.module/ClassName#method_name.`) | Extracted from `.scip` index data by cgc. Stored as node property for cross-repo join |
| D14 | Connection pool (Door/readers) | `max_connection_pool_size=25`, `connection_acquisition_timeout=30s`, `connection_timeout=5s` | Door is single-pod with async queries. 5s timeout triggers fallback to code-index.json |
| D15 | Bounded-query limits | Max traversal depth: **4 hops**; result cap: **100 paths** (impact), **50 symbols** (understand) | Prevents context-blowing dumps; matches existing contract |
| D16 | Fallback contract | Door -> `code-index.json` (S3) when Neptune unreachable (5s timeout) | Graceful degradation; returns flat one-hop data. Log WARN, response includes `"source": "code-index-fallback"` |
| D17 | ACL enforcement | **Post-query filter against Postgres `repositories.allowed_principals`** (fail-closed) | Graph (Neptune) and ACL (Postgres) are separate stores. Every result filtered through existing `door/acl.py`. No principal -> no results. |
| D18 | Re-index isolation | Scoped delete (`MATCH (n) WHERE n.repo = $r DETACH DELETE n`) then bulk-load | Per-repo subgraph replaced atomically. Cross-repo edges use `symbol_id`; they're resolved at query-time, not materialized |
| D19 | Bulk Loader CSV format | **`opencypher`** format with `userProvidedEdgeIds=TRUE` | See [CSV Format](#neptune-bulk-loader-csv-format) below. Explicit IDs enable resume-on-error |
| D20 | S3 staging path | `s3://{bucket}/neptune-bulk-load/{repo_safe_name}/{timestamp}/` | Per-repo, timestamped. Bucket is the existing agent-context ingestion bucket |
| D21 | Bulk Loader IAM | Neptune cluster assumes a role with S3 read access (via `aws_neptune_cluster.iam_roles`) | Role must be attached to cluster. Separate from IRSA pod role |
| D22 | openCypher compatibility | No `shortestPath()` / `allShortestPaths()`. Use variable-length path `*1..N` instead. No APOC. No `FOREACH`. | Validated against Neptune docs. Our queries use `[:CALLS*1..4]` which IS supported |
| D23 | `neptune_enabled` flag | Already merged (PR #1528). Decoupled from OpenSearch (`graphrag_enabled`). Default: `false` | Operator flips to `true` + sets `CONFIRM_GRAPHRAG_COST=yes` for apply |

---

## Graph Schema

All stories (#1532 writes, #1533 isolates, #1535/#1536 query) use this exact schema.

### Node Labels

| Label | Description | Required Properties |
|-------|-------------|---------------------|
| `Symbol` | A code symbol (function, method, class, variable, constant, interface, type) | `repo`, `file`, `name`, `kind`, `line`, `signature`, `symbol_id` |
| `File` | A source file | `repo`, `path`, `language` |
| `Module` | A logical module/package (directory-level grouping) | `repo`, `path` |

### Node Properties (detail)

#### Symbol

| Property | Type | Description | CSV Column |
|----------|------|-------------|------------|
| `repo` | String | `org/repo` (e.g., `aws-e/adp`) | `repo:String` |
| `file` | String | Relative file path within the repo | `file:String` |
| `name` | String | Symbol name (e.g., `ContentRouter`, `cgc_analyze`) | `name:String` |
| `kind` | String | One of: `function`, `method`, `class`, `interface`, `type`, `variable`, `constant` | `kind:String` |
| `line` | Int | Line number of definition | `line:Int` |
| `signature` | String | Full signature (parameters + return type) | `signature:String` |
| `symbol_id` | String | Stable SCIP moniker (globally unique qualified identifier) | `symbol_id:String` |
| `visibility` | String | `public`, `private`, `internal` (best-effort from SCIP) | `visibility:String` |

**`~id` encoding**: `{repo}|{file}|{name}|{kind}` (pipe-separated)

Example: `aws-e/adp|modules/gateway/src/router.py|ContentRouter|class`

#### File

| Property | Type | Description | CSV Column |
|----------|------|-------------|------------|
| `repo` | String | `org/repo` | `repo:String` |
| `path` | String | Relative file path | `path:String` |
| `language` | String | Detected language (`python`, `typescript`, `go`, etc.) | `language:String` |

**`~id` encoding**: `{repo}|{path}`

#### Module

| Property | Type | Description | CSV Column |
|----------|------|-------------|------------|
| `repo` | String | `org/repo` | `repo:String` |
| `path` | String | Module/package path (directory) | `path:String` |

**`~id` encoding**: `{repo}|module|{path}`

### Edge Types (Relationships)

| Type | Source -> Target | Description | Properties |
|------|-----------------|-------------|------------|
| `CALLS` | Symbol -> Symbol | Function/method calls another | `repo` (source repo) |
| `IMPORTS` | File -> File | File imports from another file | `repo` |
| `DEFINES` | File -> Symbol | File defines/contains a symbol | `repo` |
| `CONTAINS` | Module -> File | Module contains a file | `repo` |
| `INHERITS` | Symbol -> Symbol | Class extends/implements another | `repo` |
| `IMPLEMENTS` | Symbol -> Symbol | Concrete impl of an interface method | `repo` |
| `MEMBER_OF` | Symbol -> Symbol | Method/field belongs to a class | `repo` |

**Key design decision**: Every edge carries a `repo` property (the *source* node's repo). This enables:
- `MATCH (n {repo: $r}) DETACH DELETE n` for fast subgraph cleanup during re-index
- Cross-repo edges: when Symbol in repo A `CALLS` Symbol in repo B, the edge's `repo` = A. Re-indexing repo A removes A's outgoing edges but NOT B's.

### Cross-Repo Edge Resolution (D12 detail)

Cross-repo CALLS edges use **stable SCIP `symbol_id`** for resolution. This is fundamentally different from the Neo4j design (which used name+file matching at query-time).

**At query time** (Door openCypher, #1535/#1536):
```cypher
// Find cross-repo callers of a symbol via stable symbol_id
MATCH (target:Symbol {repo: $repo, file: $file, name: $symbol_name})
WITH target, target.symbol_id AS target_sid
MATCH (caller:Symbol)-[:CALLS]->(callee:Symbol {symbol_id: target_sid})
WHERE caller.repo <> $repo
RETURN DISTINCT caller.repo AS calling_repo,
       caller.file AS calling_file,
       caller.name AS calling_symbol,
       caller.kind AS calling_kind
ORDER BY calling_repo, calling_file
LIMIT 100
```

**Why symbol_id, not name+file:**
1. SCIP monikers are compiler-resolved, globally unique identifiers
2. Same-named functions in different repos (e.g., `connect()` in `db.py`) will NOT produce false cross-repo edges
3. HARD REQUIREMENT: negative test — two repos with same-named function in same-named file that do NOT call each other MUST assert NO false cross-repo edge (#1536)

**Edge materialization policy**: Cross-repo edges are NOT materialized at ingestion time. Resolution is query-time via `symbol_id` join. This is correct because:
- Repo B may not be indexed when repo A is ingested
- No reconciliation pass needed
- New repos appear in cross-repo results as soon as indexed

---

## Neptune Bulk Loader CSV Format

### Vertex File: `nodes.csv`

```csv
:ID,repo:String,file:String,name:String,kind:String,line:Int,signature:String,symbol_id:String,visibility:String,path:String,language:String,:LABEL
aws-e/adp|modules/gateway/src/router.py|ContentRouter|class,aws-e/adp,modules/gateway/src/router.py,ContentRouter,class,42,"class ContentRouter(BaseRouter)",python pkg.gateway.router/ContentRouter#,public,,,Symbol
aws-e/adp|modules/gateway/src/router.py,aws-e/adp,,,,,,,,modules/gateway/src/router.py,python,File
aws-e/adp|module|modules/gateway/src,aws-e/adp,,,,,,,,modules/gateway/src,,Module
```

**Column rules:**
- `:ID` — mandatory, globally unique, uses the pipe-encoding defined in D11
- `:LABEL` — one of `Symbol`, `File`, `Module`
- Property columns use `name:Type` format (Neptune data types: `String`, `Int`)
- Empty values are permitted (properties not applicable to that label)

### Edge File: `edges.csv`

```csv
:ID,:START_ID,:END_ID,:TYPE,repo:String,target_symbol_id:String
e|aws-e/adp|router.py|ContentRouter|class|CALLS|db.py|connect|function,aws-e/adp|modules/gateway/src/router.py|ContentRouter|class,aws-e/adp|modules/gateway/src/db.py|connect|function,CALLS,aws-e/adp,python pkg.gateway.db/connect.
e|aws-e/adp|router.py|IMPORTS|db.py,aws-e/adp|modules/gateway/src/router.py,aws-e/adp|modules/gateway/src/db.py,IMPORTS,aws-e/adp,
e|aws-e/adp|router.py|DEFINES|ContentRouter|class,aws-e/adp|modules/gateway/src/router.py,aws-e/adp|modules/gateway/src/router.py|ContentRouter|class,DEFINES,aws-e/adp,
```

**Column rules:**
- `:ID` — mandatory (userProvidedEdgeIds=TRUE), unique per edge. Encoding: `e|{source_id}|{type}|{target_short}`
- `:START_ID` — references the source node's `:ID`
- `:END_ID` — references the target node's `:ID`
- `:TYPE` — one of `CALLS`, `IMPORTS`, `DEFINES`, `CONTAINS`, `INHERITS`, `IMPLEMENTS`, `MEMBER_OF`
- `repo:String` — the source node's repo (for scoped deletion)
- `target_symbol_id:String` — SCIP moniker of the target (for cross-repo resolution). Only populated on CALLS edges

### Bulk Loader Request

```json
{
  "source": "s3://adp-dev-agent-context-{account_id}/neptune-bulk-load/{repo_safe_name}/{timestamp}/",
  "format": "opencypher",
  "iamRoleArn": "arn:aws:iam::{account_id}:role/adp-dev-neptune-s3-loader",
  "region": "us-east-1",
  "failOnError": "FALSE",
  "parallelism": "MEDIUM",
  "updateSingleCardinalityProperties": "TRUE",
  "userProvidedEdgeIds": "TRUE",
  "queueRequest": "TRUE"
}
```

**Key parameters:**
- `format: "opencypher"` — NOT `"csv"` (that's Gremlin format)
- `userProvidedEdgeIds: "TRUE"` — required because we supply `:ID` on edges
- `updateSingleCardinalityProperties: "TRUE"` — re-index overwrites existing properties
- `parallelism: "MEDIUM"` — avoids deadlock risk documented in Neptune loader docs
- `queueRequest: "TRUE"` — allows concurrent loads for parallel repo ingestion

---

## Connection & Configuration

### Single Connection Definition (consumed by all stories)

All consumers use the same endpoint + IAM auth pattern. No username/password.

```python
# Shared Neptune connection helper
# Used by: Door (#1535), verify gate (#1534), ingestion bulk-load (#1532)

import json
import os
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from botocore.session import Session
from neo4j import GraphDatabase, Auth


def get_neptune_auth(endpoint: str, region: str) -> Auth:
    """Generate a SigV4-signed NeptuneAuthToken for bolt connections."""
    session = Session()
    credentials = session.get_credentials().get_frozen_credentials()

    request = AWSRequest(
        method="GET",
        url=f"https://{endpoint}:8182/opencypher",
    )
    request.headers.add_header("Host", f"{endpoint}:8182")

    sigv4 = SigV4Auth(credentials, "neptune-db", region)
    sigv4.add_auth(request)

    auth_obj = {
        "Authorization": request.headers["Authorization"],
        "HttpMethod": "GET",
        "X-Amz-Date": request.headers["X-Amz-Date"],
        "Host": request.headers["Host"],
        "X-Amz-Security-Token": request.headers.get("X-Amz-Security-Token", ""),
    }
    return Auth("basic", "username", json.dumps(auth_obj), "realm")


def create_neptune_driver(
    endpoint: str | None = None,
    region: str | None = None,
    max_pool: int = 25,
    acquire_timeout: float = 30.0,
    connection_timeout: float = 5.0,
):
    """Create a neo4j driver connected to Neptune via bolt+s.

    Uses IAM SigV4 auth (IRSA). Returns None if endpoint not configured.
    """
    endpoint = endpoint or os.environ.get("NEPTUNE_ENDPOINT", "")
    region = region or os.environ.get("AWS_REGION", "us-east-1")

    if not endpoint:
        return None

    uri = f"bolt+s://{endpoint}:8182"
    auth = get_neptune_auth(endpoint, region)

    return GraphDatabase.driver(
        uri,
        auth=auth,
        encrypted=True,
        max_connection_pool_size=max_pool,
        connection_acquisition_timeout=acquire_timeout,
        connection_timeout=connection_timeout,
    )
```

### Environment Variables

| Variable | Source | Consumer |
|----------|--------|----------|
| `NEPTUNE_ENDPOINT` | Terraform output -> SSM -> ConfigMap | Door, verify gate, ingestion |
| `NEPTUNE_PORT` | Fixed: `8182` | All |
| `AWS_REGION` | EKS pod metadata | All |
| `NEPTUNE_BULK_LOADER_ROLE_ARN` | Terraform output -> ConfigMap | Ingestion (for POST /loader) |
| `NEPTUNE_ENABLED` | Feature flag | Door (controls fallback path) |

### Consumer Configuration

| Consumer | Connection Pattern | Pool Settings |
|----------|-------------------|---------------|
| **Ingestion** (#1532) | HTTPS `POST /loader` (bulk load API, SigV4-signed via boto3 `neptunedata` client) | N/A (HTTP API call, not persistent connection) |
| **Door** (#1535) | Bolt `bolt+s://{endpoint}:8182` via `neo4j==6.2.0` | max_pool=25, acquire_timeout=30s, connection_timeout=5s |
| **Verify gate** (#1534) | HTTPS via `boto3 neptunedata execute_open_cypher_query` | Single request (no pool) |
| **Cross-repo** (#1536) | Same as Door | Same as Door |

### IAM Roles

| Role | Purpose | Key Permissions |
|------|---------|-----------------|
| `adp-dev-neptune-access` (IRSA) | Pod-level access for reads/writes | `neptune-db:connect`, `neptune-db:ReadDataViaQuery`, `neptune-db:WriteDataViaQuery`, `neptune-db:DeleteDataViaQuery` |
| `adp-dev-neptune-s3-loader` (NEW) | Attached to Neptune cluster for Bulk Loader | `s3:GetObject`, `s3:ListBucket` on the ingestion bucket |

---

## Ingestion Pipeline (Decoupled, Bolt-Free)

### Architecture

```
[Repo clone]
  -> SCIP indexers (scip-python / scip-typescript / scip-go)
  -> .scip index files
  -> cgc index (FalkorDB Lite, embedded, throwaway)
  -> Python extractor (GRAPH.QUERY on FalkorDB)
  -> Neptune CSV (nodes.csv + edges.csv)
  -> S3 upload
  -> Neptune Bulk Loader (POST /loader)
  -> Graph in Neptune
```

### Why Decoupled (No Bolt-Write to Neptune)

1. **Neptune has NO schema constraints** — no UNIQUE, no INDEX creation via openCypher. Uniqueness is ONLY via `~id` in bulk load.
2. **Neptune openCypher write performance** is poor for high-volume individual MERGE/CREATE operations (designed for bulk, not transactional writes)
3. **cgc expects Neo4j or FalkorDB** for its internal writes — it cannot write to Neptune natively
4. **Bulk Loader is the AWS-recommended path** for initial data loads and batch updates
5. **Idempotent**: re-running the same CSV = same result (updateSingleCardinalityProperties=TRUE)

### Correct cgc Invocation

```python
import subprocess
import os

# cgc uses its DEFAULT embedded FalkorDB Lite backend
# No CGC_BACKEND env var needed (default = falkordb-lite)
result = subprocess.run(
    ["codegraphcontext", "index", clone_path],
    capture_output=True,
    timeout=600,  # 10 min for large repos
)
if result.returncode != 0:
    raise RuntimeError(f"cgc index failed: {result.stderr.decode()}")
```

**NOT this** (broken, exits code 2):
```python
# WRONG — do not use
subprocess.run(["cgc", "analyze", clone_path, "--json"])
```

### SCIP Indexer Installation (Dockerfile additions)

```dockerfile
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

### FalkorDB Extraction (Python)

After `cgc index` completes, extract nodes/edges from the embedded FalkorDB:

```python
# Example extractor (simplified — full implementation in #1532)
import redis
from falkordb import FalkorDB

def extract_graph(repo: str) -> tuple[list[dict], list[dict]]:
    """Extract nodes and edges from cgc's embedded FalkorDB.

    Returns (nodes, edges) as lists of dicts ready for CSV serialization.
    """
    # cgc's embedded FalkorDB is accessible at localhost:6379 (in-pod)
    db = FalkorDB(host="localhost", port=6379)
    graph = db.select_graph("code_graph")  # cgc's default graph name

    # Extract symbols (nodes)
    nodes_result = graph.query(
        "MATCH (s:Symbol) RETURN s.repo, s.file, s.name, s.kind, "
        "s.line, s.signature, s.symbol_id, s.visibility"
    )

    # Extract edges
    edges_result = graph.query(
        "MATCH (a)-[r]->(b) "
        "RETURN a.repo, a.file, a.name, a.kind, "
        "type(r) AS rel_type, "
        "b.repo, b.file, b.name, b.kind, b.symbol_id"
    )

    # Transform to Neptune CSV format...
    return transform_to_neptune_csv(nodes_result, edges_result, repo)
```

---

## Door Query Patterns (#1535, #1536)

### Neptune Client Initialization (Door)

```python
# door/neptune_client.py (new file)
import logging
import os
from .neptune_auth import create_neptune_driver

log = logging.getLogger(__name__)

_driver = None


def get_neptune_driver():
    """Lazy-init Neptune driver with IAM auth and connection pooling."""
    global _driver
    if _driver is None:
        _driver = create_neptune_driver(
            max_pool=25,
            acquire_timeout=30.0,
            connection_timeout=5.0,
        )
    return _driver


async def neptune_available() -> bool:
    """Check if Neptune is reachable (for fallback decision)."""
    driver = get_neptune_driver()
    if not driver:
        return False
    try:
        with driver.session() as session:
            session.run("RETURN 1").consume()
        return True
    except Exception:
        log.warning("Neptune unreachable — falling back to code-index.json")
        return False
```

### Impact Query (bounded, verdict-first)

```cypher
// Direct + transitive callers, bounded at 4 hops, capped at 100 paths
MATCH (target:Symbol {repo: $repo, file: $file, name: $symbol_name})
WITH target
MATCH path = (caller:Symbol)-[:CALLS*1..4]->(target)
WHERE caller <> target
RETURN caller.repo AS caller_repo,
       caller.file AS caller_file,
       caller.name AS caller_name,
       caller.kind AS caller_kind,
       length(path) AS distance
ORDER BY distance ASC, caller_repo, caller_file
LIMIT 100
```

**Neptune openCypher note**: Variable-length path patterns `[:CALLS*1..4]` ARE supported.
`shortestPath()` is NOT supported but not needed — we use bounded variable-length with ORDER BY distance.

### Understand Query (symbol neighborhood)

```cypher
// Symbol + its immediate neighborhood (callers, callees, parent class)
MATCH (s:Symbol {repo: $repo, file: $file, name: $symbol_name})
OPTIONAL MATCH (s)-[:CALLS]->(callee:Symbol)
OPTIONAL MATCH (caller:Symbol)-[:CALLS]->(s)
OPTIONAL MATCH (s)-[:INHERITS]->(parent:Symbol)
OPTIONAL MATCH (s)-[:MEMBER_OF]->(owner:Symbol)
RETURN s.name AS symbol_name,
       s.kind AS symbol_kind,
       s.file AS symbol_file,
       s.signature AS signature,
       collect(DISTINCT {name: callee.name, file: callee.file, kind: callee.kind}) AS callees,
       collect(DISTINCT {name: caller.name, file: caller.file, kind: caller.kind}) AS callers,
       collect(DISTINCT {name: parent.name, file: parent.file}) AS parents,
       collect(DISTINCT {name: owner.name, file: owner.file}) AS owners
LIMIT 50
```

### Cross-Repo Impact (#1536)

```cypher
// Find all repos that call a symbol (cross-repo blast radius)
// Resolution via stable symbol_id — NEVER name+file
MATCH (target:Symbol {repo: $repo, name: $symbol_name, file: $file})
WITH target, target.symbol_id AS target_sid
WHERE target_sid IS NOT NULL
MATCH (caller:Symbol)-[:CALLS]->(callee:Symbol {symbol_id: target_sid})
WHERE caller.repo <> $repo
RETURN DISTINCT caller.repo AS calling_repo,
       caller.file AS calling_file,
       caller.name AS calling_symbol,
       caller.kind AS calling_kind
ORDER BY calling_repo, calling_file
LIMIT 100
```

**CRITICAL**: The cross-repo query joins on `symbol_id` (SCIP moniker). It does NOT match on `name` + `file`. This prevents false cross-repo edges.

### Repo-Level Understand (#1535 — repo/dir/file targets)

```cypher
// When target is a repo (not a specific symbol), return module topology
MATCH (m:Module {repo: $repo})
OPTIONAL MATCH (m)-[:CONTAINS]->(f:File)
OPTIONAL MATCH (f)-[:DEFINES]->(s:Symbol)
RETURN m.path AS module_path,
       collect(DISTINCT f.path) AS files,
       count(DISTINCT s) AS symbol_count
ORDER BY module_path
LIMIT 50
```

---

## ACL Enforcement (D17 detail)

Neptune (graph) and Postgres (ACL) are separate stores. The Door:

1. Executes the openCypher query against Neptune (unfiltered — Neptune has no row-level security)
2. Extracts the distinct `repo` values from the result set
3. Calls `PostgresACLStore.get_allowed_repos(caller)` (existing `door/acl.py`)
4. Filters: keeps only results where `result.repo` is in the allowed set
5. Returns filtered results to the caller

**Fail-closed guarantee**: If the caller has no principal (`X-GitHub-Login` / `X-GitHub-Teams` headers missing) OR if Postgres is unreachable, return empty results. This is already implemented in `door/acl.py:filter_results()`.

**SQL for ACL lookup** (existing, unchanged):
```sql
SELECT repo_name FROM repositories
WHERE '*' = ANY(allowed_principals)
   OR $login = ANY(allowed_principals)
   OR allowed_principals && $teams_array::text[]
```

**Performance**: The ACL query is simple (GIN-indexed array overlap), returns a set of repo names. The Neptune query returns max 100 results. Post-filtering is negligible.

---

## Verify Gate Assertion (#1534)

```bash
#!/usr/bin/env bash
# Added to agent-context-verify.yml

NEPTUNE_ENDPOINT="${NEPTUNE_ENDPOINT}"
AWS_REGION="${AWS_REGION:-us-east-1}"

# Use boto3 neptunedata client (IAM-authenticated)
TOTAL_EDGES=$(python3 -c "
import boto3
from botocore.config import Config
client = boto3.client('neptunedata',
    endpoint_url='https://${NEPTUNE_ENDPOINT}:8182',
    config=Config(read_timeout=60, retries={'total_max_attempts': 1}))
r = client.execute_open_cypher_query(openCypherQuery='MATCH ()-[r:CALLS]->() RETURN count(r) AS cnt')
print(r['results'][0]['cnt'])
")

if [ "${TOTAL_EDGES:-0}" -eq 0 ]; then
  echo "FAIL: Neptune has 0 CALLS edges — call graph is empty"
  exit 1
fi

echo "PASS: Neptune call graph has $TOTAL_EDGES CALLS edges"
```

---

## Fallback Contract (D16 detail)

When the Door cannot reach Neptune (connection timeout after 5 seconds):

1. Log `WARNING: Neptune unreachable at {endpoint} — using code-index.json fallback`
2. Load `code-index.json` from S3 (existing `structural_backend.py` path)
3. Return one-hop callers/callees from the flat JSON (no transitive, no cross-repo)
4. Include in response metadata: `"source": "code-index-fallback"` so the agent knows it's degraded
5. Do NOT raise an error — the verb succeeds with reduced data quality

---

## Compatibility Matrix

| Component | Version | Depends On | Verified Compatible |
|-----------|---------|------------|---------------------|
| Neptune Serverless | 1.4.7.0 | — | Baseline (latest active) |
| codegraphcontext | 0.5.1 | FalkorDB Lite (embedded) | YES (default backend) |
| neo4j Python driver | 6.2.0 | Neptune bolt :8182 | YES (AWS docs confirm) |
| scip-python | 0.6.6 | Node >=16, Python >=3.10 | YES (image has Python 3.13 + Node 20) |
| scip-typescript | 0.4.0 | Node >=16 | YES (Node 20) |
| scip-go | 0.2.7 | Go 1.22+ | YES (we install Go 1.22.4) |
| boto3 (neptunedata) | >=1.34 | — | YES (installed in image) |

---

## Cost Estimate

| Resource | Monthly Cost | Notes |
|----------|-------------|-------|
| Neptune Serverless (1-16 NCU) | ~$105 | $0.12/NCU-hour; 1 NCU idle ~$87/mo + burst |
| S3 bulk-load staging | ~$1 | CSV files, short-lived |
| **Total incremental** | **~$106/mo** | Gated by `neptune_enabled` flag |

---

## File Changes Summary (per story)

| Story | Files to Create | Files to Modify |
|-------|----------------|-----------------|
| #1531 | — | `environments/dev/modules/agent-context.tfvars` (neptune_enabled=true, engine_version override) |
| #1527 | spike scripts (temporary) | — |
| #1532 | `images/ingestion/neptune_extractor.py`, `images/ingestion/neptune_loader.py` | `images/ingestion/Dockerfile` (SCIP + Node + Go), `images/ingestion/ingest-repo.py` |
| #1533 | — | `images/ingestion/neptune_loader.py` (scoped delete before load) |
| #1534 | — | `.github/workflows/agent-context-verify.yml` (Neptune edge assertions) |
| #1535 | `door/neptune_client.py`, `door/neptune_auth.py` | `door/structural_backend.py` (Neptune query path + fallback), `door/server.py`, requirements |
| #1536 | — | `door/neptune_client.py` (cross-repo queries), tests (negative false-edge test) |

---

## openCypher Compatibility Notes (D22 detail)

Neptune's openCypher implementation differs from Neo4j Cypher. Key points for our queries:

| Feature | Neptune | Neo4j | Impact on Us |
|---------|---------|-------|-------------|
| Variable-length paths `*1..4` | YES | YES | Our impact query works |
| OPTIONAL MATCH | YES | YES | Our understand query works |
| collect(DISTINCT ...) | YES | YES | Aggregation works |
| shortestPath() | **NO** | YES | Not needed (we use bounded variable-length) |
| APOC procedures | **NO** | YES | Not needed |
| FOREACH | **NO** | YES | Not needed |
| Schema constraints | **NO** | YES | Uniqueness via `~id` only (Bulk Loader) |
| CREATE INDEX | **NO** (automatic) | YES | Neptune auto-indexes; no manual DDL |
| MERGE (upsert) | YES (limited) | YES | Not used (bulk load handles upserts) |
| Map literals in RETURN | YES | YES | Used in understand query |
| Pattern comprehension | YES (1.4+) | YES | Available if needed |

**Our queries validated**: All openCypher patterns used in impact/understand/cross-repo queries are within Neptune's supported subset.

---

## Non-Goals (explicitly excluded)

- Gremlin API for code graph (we use openCypher exclusively; personal_context/graph.py's Gremlin is a separate domain)
- Neptune Analytics / Neptune ML (not needed for structural queries)
- OpenSearch Serverless (decoupled via separate `graphrag_enabled` flag; stays OFF)
- Real-time bolt-write to Neptune (bulk load is the write path)
- Graph versioning / time-travel (re-index is full replacement per repo)
- Vector similarity edges in Neptune (SCIP accuracy is sufficient)
- Neo4j compatibility layer (clean break from the superseded design)

---

## References

- Parent EPIC: #1529
- Superseded design: `docs/agent-context/neo4j-deep-graph-design.md` (EPIC #1512)
- Merged infra PR: #1528 (neptune_enabled flag)
- Spike: #1527 (prove cgc->FalkorDB->CSV->Bulk Loader)
- Relevance eval: #1511
- Existing ACL implementation: `modules/agent-context/door/acl.py`
- Existing structural backend: `modules/agent-context/door/structural_backend.py`
- Existing Neptune module (Terraform): `modules/agent-context/terraform/modules/neptune-serverless/`
- Existing Neptune client (personal context): `modules/agent-context/personal_context/graph.py`
- Existing ingestion: `modules/agent-context/images/ingestion/ingest-repo.py`
- Neptune Bulk Loader docs: https://docs.aws.amazon.com/neptune/latest/userguide/bulk-load-tutorial-format-opencypher.html
- Neptune Bolt protocol: https://docs.aws.amazon.com/neptune/latest/userguide/access-graph-opencypher-bolt.html
- Neptune engine releases: https://docs.aws.amazon.com/neptune/latest/userguide/engine-releases.html
