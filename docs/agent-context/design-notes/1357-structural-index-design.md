# Design Note: Structural Index (code-index.json) to S3 + understand/impact Backends

**Issue:** #1357 (sub of EPIC #1345)
**Author:** @agent-architect
**Date:** 2026-06-11
**Status:** Implementation-ready

---

## 1. Summary

This note specifies how `code-index.json` (the per-repo structural index produced by
CodeGraphContext / tree-sitter fallback) is stored durably, and how the MCP `understand`
and `impact` verbs consume it. It covers:

- Storage path correction (critical bug fix)
- `understand(target, depth)` backend algorithm
- `impact(target, cross_repo)` backend algorithm
- cgc robustness and fallback strategy
- Symbol span reuse contract for the semantic chunker (#1348)
- v1 scope boundaries (per-repo vs cross-repo)
- Fact-check corrections against the design of record

---

## 2. Current State (what exists today)

| Component | Status | Location |
|-----------|--------|----------|
| `cgc_analyze()` | Working | `images/ingestion/ingest-repo.py:278` |
| `_build_basic_code_index()` (tree-sitter fallback) | Working | `images/ingestion/ingest-repo.py:307` |
| `_write_code_index_to_filesystem()` | Working | `images/ingestion/ingest-repo.py:443` |
| CODE_INDEX_DIR env in ScaledJob | **BUG: points to /tmp** | `manifests/ingestion-scaledjob.yaml:66` |
| S3 prefix `code-indexes/` | **Missing** | `terraform/modules/s3-files/main.tf` |
| `understand` MCP tool | Schema only (mocked) | `tests/conftest.py:278` |
| `impact` MCP tool | Schema only (mocked) | `tests/conftest.py:285` |
| GraphRAG call-graph (Neptune) | Working (optional) | `images/ingestion/ingest-repo.py:589` |

---

## 3. Storage Design

### 3.1 The Bug (Critical)

The `ingestion-scaledjob.yaml` sets:

```yaml
- name: CODE_INDEX_DIR
  value: /tmp/code-indexes
```

The `/tmp` volume is an `emptyDir` (ephemeral). When the ingestion pod completes,
all code-index JSON files are **destroyed**. The MCP server cannot read them.

Meanwhile, `ingest-repo.py` defaults to `/platform-data/code-indexes` (the persistent
S3-backed PVC), which is correct. The manifest override breaks it.

### 3.2 Fix

Change `CODE_INDEX_DIR` in the ScaledJob to `/platform-data/code-indexes`. This path
is on the `platform-data` PVC (backed by S3 via EFS CSI driver), which is:

- **ReadWriteMany** (all pods can write concurrently)
- **Persistent** (survives pod termination)
- **S3-durable** (data lives in the `agent-context-platform-data-{account_id}` bucket)

### 3.3 S3 Object Path Convention

```
s3://agent-context-platform-data-{account_id}/code-indexes/{org}--{repo}.json
```

The filesystem (Mountpoint/EFS) presents this as:
```
/platform-data/code-indexes/{org}-{repo}.json
```

Note: `ingest-repo.py` uses `org_repo.replace("/", "-")` for the filename, producing
e.g. `aws-samples-bedrock-chat.json`. This is the established convention.

### 3.4 Terraform Prefix

Add `code-indexes/` to the S3 directory markers (alongside `deepwiki/`, `codegraph/`,
etc.) so the prefix exists before the first write.

### 3.5 Write Semantics

- **Write-once per ingestion run:** Each ingestion overwrites the repo's index atomically
  (Mountpoint semantics: create new file, close — no append/modify).
- **No locking needed:** One pod per repo (KEDA ScaledJob parallelism=1 per message).
- **Staleness mitigated:** Re-ingestion on SHA change (tracked via DynamoDB `last_sha`).

---

## 4. `understand(target, depth)` Backend

### 4.1 Contract

**Input:**
```json
{
  "target": "org/repo" | "org/repo/path/to/dir" | "org/repo/path/to/file.py",
  "depth": "overview" | "detailed"  // optional, default "overview"
}
```

**Output:**
```json
{
  "target": "<echoed>",
  "summary": "<structured markdown>",
  "structure": {
    "languages": {"python": 42, "typescript": 18},
    "symbols": [{"name": "...", "type": "function|class", "file": "...", "line": 0}],
    "imports": {"file.py": ["import os", "from foo import bar"]},
    "dependencies_external": ["requests", "boto3"],
    "call_graph_excerpt": {"main": ["setup", "run"]}
  }
}
```

### 4.2 Algorithm

```
understand(target, depth):
  1. Parse target → (repo, optional_path)
  2. Load code-index from /platform-data/code-indexes/{safe_name}.json
     - If not found → return {"summary": "No structural index available", ...}
  3. If target is repo-level:
     - depth="overview": return language_stats + top-20 classes + external deps
     - depth="detailed": return full symbol table + imports + call graph excerpt
  4. If target includes a path:
     - Filter symbols/imports to those whose `file` starts with the path
     - If single file: return all symbols in that file + its imports + who calls them
  5. Compose markdown summary from filtered data
```

### 4.3 Performance

- Code-index files are small JSON (typically 50KB-500KB per repo)
- Single filesystem read from the S3-backed mount
- No network calls beyond the FS mount
- Target response time: <200ms for overview, <500ms for detailed

---

## 5. `impact(target, cross_repo)` Backend

### 5.1 Contract

**Input:**
```json
{
  "target": "SymbolName" | "org/repo/path/to/file.py" | "org/repo/path/to/file.py:FuncName",
  "cross_repo": false  // optional, default false
}
```

**Output:**
```json
{
  "target": "<echoed>",
  "affected": [
    {
      "symbol": "caller_func",
      "file": "src/handler.py",
      "line": 42,
      "repo": "org/repo",
      "relationship": "calls"
    }
  ],
  "blast_radius": 3,
  "scope": "single_repo" | "cross_repo"
}
```

### 5.2 Algorithm (v1 — per-repo graph + cross-repo grep)

```
impact(target, cross_repo):
  1. Parse target → (repo, file, symbol_name)
  2. Load code-index for the repo
  3. INTRA-REPO (always):
     a. Find target symbol in symbols list (match by name + optional file)
     b. Traverse call_graph REVERSE: find all symbols that call target
        - Build reverse adjacency from call_graph: {callee → [callers]}
        - BFS/DFS from target, max depth=3
     c. Find files that import the target's module (from imports dict)
     d. Collect affected = direct callers + transitive callers (depth-limited)
  4. CROSS-REPO (if cross_repo=true):
     a. For each other repo's code-index on the filesystem:
        - Check imports for references to target's module/package
        - Check call_graph for references to target symbol name
     b. Fallback: grep via Zoekt/Sourcebot for symbol name across repos
        (POST to sourcebot search API with symbol name as literal query)
     c. Merge results, deduplicate
  5. Return affected list sorted by relationship depth, with blast_radius count
```

### 5.3 v1 Scope Boundaries

| Capability | v1 (this issue) | Future |
|------------|-----------------|--------|
| Intra-repo call graph traversal | Yes | Yes |
| Intra-repo import analysis | Yes | Yes |
| Cross-repo: grep for symbol | Yes (via Zoekt) | Yes |
| Cross-repo: true graph join | No | Neptune flag-gated |
| Transitive impact depth | Max 3 hops | Configurable |
| Language coverage | Python, TS/JS, Go | + Rust, Java |

**Why per-repo graph + cross-repo grep for v1:**
- True cross-repo graph requires Neptune (optional, flag-gated via `GRAPHRAG_ENABLED`)
- Grep catches most real cross-repo usages (import statements, direct calls)
- Neptune path already exists in the GraphRAG pipeline; the `impact` backend can
  optionally query it when enabled, but does NOT require it

### 5.4 Neptune Integration (flag-gated, optional)

When `GRAPHRAG_ENABLED=true` and Neptune is reachable, `impact` can additionally:

```gremlin
g.V().has('entity_id', '{repo}:{file}:{symbol}')
  .in('calls').path()
```

This gives true transitive callers across repos (entities with different `repo` properties).
The flag gate ensures `impact` degrades gracefully without Neptune.

---

## 6. cgc Robustness and Fallback Strategy

### 6.1 Current Fallback Chain (already implemented)

```python
def cgc_analyze(clone_path, org_repo):
    try:
        # 1. Try cgc CLI (full AST analysis, call graph)
        result = subprocess.run(["cgc", "analyze", ...])
        if result.returncode == 0:
            return json.loads(result.stdout)
    except (ImportError, FileNotFoundError, TimeoutExpired, JSONDecodeError):
        pass

    # 2. Fallback: basic file-level analysis (regex-based)
    return _build_basic_code_index(clone_path, org_repo)
```

### 6.2 Robustness Requirements for This Issue

The fallback produces a **reduced** index:
- **Has:** symbols (functions, classes), imports, external dependencies, language stats
- **Missing:** `call_graph` (always `{}`)

This means:
- `understand()` works fully with the fallback (symbols + imports + deps are present)
- `impact()` degrades gracefully:
  - Intra-repo import analysis still works (imports dict is populated)
  - Call graph traversal returns empty (no `call_graph` data)
  - Cross-repo grep still works (independent of call graph)
  - Response should include a `"note": "call graph unavailable; showing import-based impact only"`

### 6.3 No Changes Needed to cgc

The existing fallback chain is correctly structured. The only fix is the **storage path**
(ensuring the output is persisted, not lost to /tmp).

---

## 7. Symbol Span Reuse for Semantic Chunker (#1348)

### 7.1 Contract

The semantic chunking sub-issue (#1348) needs function/class boundaries to produce
per-function embeddings instead of fixed-size chunks. The code-index provides this:

```json
{
  "symbols": [
    {"name": "retry_handler", "type": "function", "file": "src/retry.py", "line": 42},
    {"name": "RetryConfig", "type": "class", "file": "src/retry.py", "line": 10}
  ]
}
```

### 7.2 What #1348 Consumes

The semantic chunker reads `/platform-data/code-indexes/{repo}.json` and uses the
`symbols` array to determine chunk boundaries:

1. Sort symbols by (file, line)
2. For each file: symbols define natural chunk boundaries
3. Text between symbol start lines becomes the chunk content
4. Each chunk is tagged with `{repo, file, symbol_name, symbol_type, line_start}`
5. If no symbols exist for a file → fall back to fixed-size chunking (500 tokens)

### 7.3 End-line Enhancement (recommended for #1348 implementation)

The current schema stores `line` (start) but not the end line. The semantic chunker
should treat the next symbol's start as the previous symbol's end. For the last symbol
in a file, use EOF.

**Optional enhancement** (out of scope for this issue, recommended for cgc upstream):
Add `end_line` to symbol entries. Until then, the start-of-next-symbol heuristic works.

---

## 8. Fact-Check Corrections

### 8.1 S3 Vectors Write Speed (affects #1348, not this issue)

The design of record (§10.6) assumes "~2,500 writes/s/index" drives sharding. Based on
current AWS documentation, the limit appears to be **1,000 writes/s/index** (default quota).

**Impact:** The sharding calculation in the sibling semantic issue (#1348) should use 1,000
not 2,500 as the per-index ceiling. With 50-100 workers writing concurrently, this means:
- At 50 workers × ~50 chunks/repo = ~2,500 writes total → 3 shards minimum
- At 100 workers × ~50 chunks/repo = ~5,000 writes total → 5 shards minimum

**Action:** Note this in #1348's design; does not affect this issue (code-index is plain S3/filesystem, not S3 Vectors).

### 8.2 OSV-Scanner Does NOT Consume CycloneDX SBOM (affects #1350)

The design of record (§7.2.5) states OSV-Scanner consumes a CycloneDX SBOM. This is
**incorrect**. OSV-Scanner scans lockfiles natively (19+ formats) and container images
directly. It does NOT accept SBOM files (CycloneDX or SPDX) as input.

**Impact:** The SBOM sub-issue (#1350) should:
- Use Syft to generate the SBOM as the audit record (CycloneDX output for compliance)
- Feed OSV-Scanner the **source directory** or lockfiles directly (not the SBOM)
- Trivy CAN consume SBOMs — so Trivy handles image-layer CVE matching from the SBOM

### 8.3 Mountpoint Write-Once Semantics: Confirmed

Mountpoint for Amazon S3:
- GA, production-ready (announced 2023, current CSI driver v2.6.0)
- Supports: create new files, read existing files, list files
- Does NOT support: modify existing files, delete directories, file locking, symlinks
- CSI driver for EKS: GA, supports K8s v1.30+, static provisioning

This matches the design's assumption. The system already uses EFS CSI driver with
S3 Files integration (volumeHandle format `<efs-id>::<bucket>`), which provides the
same write-once semantics at the POSIX layer.

### 8.4 Trivy: Confirmed

Apache-2.0 license, actively maintained (v0.71.0, June 2026), covers OS packages +
base-image layers + container scanning. Confirmed suitable.

### 8.5 PostgreSQL 16: Confirmed

Community support through November 2028 (~2.5 years). No action needed.

---

## 9. File-Level Changes

### Modified Files

| File | Change |
|------|--------|
| `modules/agent-context/manifests/ingestion-scaledjob.yaml` | `CODE_INDEX_DIR=/tmp/code-indexes` → `/platform-data/code-indexes` |
| `modules/agent-context/terraform/modules/s3-files/main.tf` | Add `code-indexes/` S3 prefix marker |

### New Files (for developer agent implementing backends)

| File | Purpose |
|------|---------|
| `modules/agent-context/mcp/backends/understand.py` | `understand()` backend (reads code-index, returns structure) |
| `modules/agent-context/mcp/backends/impact.py` | `impact()` backend (traverses call graph, optional cross-repo grep) |
| `modules/agent-context/mcp/backends/__init__.py` | Package init |
| `modules/agent-context/tests/unit/test_understand.py` | Unit tests for understand backend |
| `modules/agent-context/tests/unit/test_impact.py` | Unit tests for impact backend |

---

## 10. Deployment Notes

- **Storage fix (scaledjob):** Requires re-applying the K8s manifest (`kubectl apply -f manifests/ingestion-scaledjob.yaml`). Next ingestion run will persist indexes correctly.
- **Terraform prefix:** Requires `terraform apply` on the s3-files module (idempotent — just creates an empty S3 object).
- **Re-ingestion:** After deploying the fix, trigger re-ingestion for all repos to populate `/platform-data/code-indexes/`. The scheduler already detects SHA changes; a forced re-index can be triggered by clearing `last_sha` in DynamoDB.
- **MCP backends:** Once implemented, require an image rebuild and rollout of the MCP server pod.

---

## 11. Testing Strategy

| Layer | Test | What It Proves |
|-------|------|----------------|
| Unit | `understand("org/repo")` with seeded index → returns language_stats + symbols | Backend reads and parses code-index correctly |
| Unit | `understand("org/repo/src/main.py")` → returns only symbols in that file | Path filtering works |
| Unit | `impact("my_func")` with seeded call_graph → returns callers | Reverse graph traversal works |
| Unit | `impact("my_func")` with empty call_graph → returns import-based results + note | Graceful degradation when cgc fallback was used |
| Integration | Index a repo with known call chain → `impact` on leaf → lists callers | End-to-end from ingestion to query |
| Smoke | `understand` a known module via MCP endpoint → returns structure | Full MCP path works |
| Regression | Delete `cgc` binary → run ingestion → verify index still written (fallback) | Fallback produces usable output |

---

## 12. Open Questions (for implementer)

1. **MCP server location:** The MCP server codebase doesn't exist yet in this repo (only test schemas). The developer implementing backends needs to decide: new FastAPI service? Or extend an existing service? Recommendation: new lightweight FastAPI app at `modules/agent-context/mcp/` since the test infra already expects port 5100.

2. **Cross-repo grep endpoint:** The `impact` backend needs a Zoekt/Sourcebot search endpoint. Current Sourcebot is at `http://sourcebot.agent-context.svc.cluster.local:3000`. The API contract for symbol search needs confirmation from the Sourcebot deployment config.

3. **Concurrent read safety:** Multiple MCP server replicas reading the same code-index files from the S3-backed mount is safe (read-only after write). No mutex needed.
