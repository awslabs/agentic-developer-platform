# Design Note: Replace OpenViking with S3 Vectors + S3 (Mountpoint)

**Issue:** #1348 (sub of #1345)
**Author:** @agent-architect
**Date:** 2026-06-11
**Status:** Implementation-ready design
**Companion docs:** `docs/knowledge-layer-storage-design.md` (design of record), this note (implementation detail)

---

## 1. Executive Summary

This design note validates and refines the agreed strategy to replace OpenViking (AGPL-3.0) with Amazon S3 Vectors (vector/semantic search) and S3 via Mountpoint (document/content store). It confirms the design assumptions against current AWS documentation, fills implementation gaps, and provides a concrete migration plan.

**Verdict: The design in `knowledge-layer-storage-design.md` is sound.** One quota adjustment needed (see §2.2).

---

## 2. Fact-Check: AWS Service Validation

### 2.1 Amazon S3 Vectors — CONFIRMED GA

| Claim in design | Verified value | Source |
|---|---|---|
| GA status | **Confirmed.** API version `s3vectors-2025-07-15`. No "preview" or "beta" labels in documentation. Full IAM namespace (`s3vectors`). | AWS User Guide, `s3-vectors.html` |
| Max dimensions | **4,096** | AWS S3 Vectors Limitations page |
| Max vectors/index | **2 billion** | Same |
| Write throughput | **2,500 vectors/sec/index** (combined insert + delete), via up to 1,000 requests/sec (batch 500/call) | Same |
| Distance metrics | `euclidean`, `cosine` (NOT dot_product) | CreateIndex API reference |
| Data type | `float32` only | CreateIndex API reference |
| Filterable metadata | Up to **2 KB** per vector, up to 50 keys | Limitations page |
| Non-filterable metadata | Up to **40 KB** total per vector | Same |
| Top-K per query | Up to **100** | Same |
| Vectors per PutVectors call | Up to **500** | Same |
| Vector buckets per region | 10,000 | Same |
| Indexes per bucket | 10,000 | Same |

**Pricing (US East):**
- Storage: $0.06/GB/month (logical size = vector data + metadata + key)
- PUT writes: $0.20/GB uploaded
- Query API: $2.50/million queries
- Query data processing: $0.004/TB (first 100K vectors), $0.002/TB (above)

**Region availability:** Not confirmed from the documentation pages retrieved. **Action required:** verify S3 Vectors availability in the deploy region (`us-east-1`) before Terraform apply. If not available, fall back to `us-west-2`.

**Key implications for design:**
- ✅ 1024-dim Titan Embed v2 vectors fit well within 4,096 limit
- ✅ 2B vectors/index is far above our ceiling (~50M for 500 repos)
- ⚠️ **Write limit is actually 1,000 requests/sec per index** (each up to 500 vectors), yielding the 2,500 vectors/sec figure through batching. The design's assumption of ~2,500 vectors/s/index is **correct**.
- ⚠️ `cosine` distance is available (matches our embedding similarity approach)
- ⚠️ Top-K=100 per query is fine for code search (we return 5-20 results typically)

### 2.2 Write-Throughput Sharding — CONFIRMED NEEDED

**Math:** 50-100 parallel workers × ~50 vectors/batch × ~2 batches/sec = 5,000-10,000 vectors/sec peak. A single index caps at 2,500/sec → **need 2-4 shards minimum** for burst ingestion.

**Recommended sharding strategy:** Hash `org_id` to shard index (e.g., `code-vectors-{org_hash_mod_N}`). Start with N=4 indexes. At query time, scatter-gather across all shards and merge results by score.

**Alternative considered:** One index per org. Simpler isolation but more indexes to manage. For <10 orgs this is viable; for >100 orgs the scatter-gather cost at query time grows. **Recommendation:** Start with hash-sharded (N=4), migrate to per-org if tenant count stays small.

### 2.3 Mountpoint for Amazon S3 — CONFIRMED GA

| Claim | Verified |
|---|---|
| GA status | **Confirmed.** AWS markets as "Generally Available and Ready for Production Workloads." |
| Write semantics | **Write-once only.** Sequential writes for new files. Cannot modify, overwrite, or append existing objects. |
| File locking | **Not supported.** |
| CSI driver | `awslabs/mountpoint-s3-csi-driver` v2.6.0, **Apache-2.0** license. EKS add-on available. |
| Read-heavy suitability | Excellent. Local caching supported (instance storage, memory, EBS). |
| Kubernetes support | v1.30+, x86-64 and arm64. |

**Design implication:** Mountpoint is a perfect fit for the document store (Zoekt indexes, structure maps, SBOMs, wikis). Workers write once to S3; serving pods mount read-only. The write-once constraint is not a limitation because index files are immutable once built — a re-index produces new objects.

### 2.4 Security Tooling Licenses — CONFIRMED

| Tool | License | Status |
|---|---|---|
| **OSV-Scanner** (Google) | Apache-2.0 | Active, maintained by Google. Consumes CycloneDX SBOM format. |
| **Trivy** (Aqua Security) | Apache-2.0 | Active, covers OS/base-image layers. |
| **Syft** (Anchore) | Apache-2.0 | Already in use in ADP. |

### 2.5 PostgreSQL 16 — CONFIRMED

PostgreSQL 16 has community support until **November 2028** (5-year window from release). The gateway RDS instance runs 16.6. The `agent_context` database inherits this version. 2.5 years of support runway is adequate for this phase.

---

## 3. Architecture: What Replaces What

```
┌──────────────────────────────────────────────────────────────────────────┐
│                        BEFORE (OpenViking)                                │
├──────────────────────────────────────────────────────────────────────────┤
│  Single pod (ghcr.io/volcengine/openviking:main)                         │
│  ├── AGFS filesystem (HTTP API): put/get/delete/list_prefix              │
│  │   ├── /personal/{owner}/learnings/*.json  (personal context)          │
│  │   ├── viking://resources/repos/*          (code-intel)                │
│  │   └── viking://resources/docs/*           (documents/wikis)           │
│  ├── Vector search (embedded engine)                                     │
│  └── 16 GB RAM, PVC-backed                                              │
└──────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼  REPLACED BY
┌──────────────────────────────────────────────────────────────────────────┐
│                        AFTER (AWS-native)                                 │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  ┌─────────────────────┐   ┌──────────────────────────────────────┐     │
│  │  S3 Vectors         │   │  S3 Bucket (via Mountpoint CSI)      │     │
│  │  (semantic search)  │   │  (document/content store)            │     │
│  │                     │   │                                      │     │
│  │  • Code embeddings  │   │  • Zoekt index shards                │     │
│  │  • Org-sharded      │   │  • Structure maps (.json)            │     │
│  │  • Metadata filter  │   │  • SBOM files                        │     │
│  │  • 1024-dim cosine  │   │  • Wiki/doc content                  │     │
│  └─────────────────────┘   │  • Personal context entries (.json)  │     │
│                             └──────────────────────────────────────┘     │
│                                                                          │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │  PostgreSQL (agent_context DB on existing gateway RDS)           │    │
│  │  • Catalog (indexed repos, versions, timestamps)                │    │
│  │  • Permissions (repo → allowed_principals)                      │    │
│  │  • Dependencies (reverse index for SBOM)                        │    │
│  │  • Vulnerabilities (CVE tracking)                               │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Design Decisions and Recommendations

### 4.1 Code Chunking Strategy: Per-Symbol (Recommended)

**Options considered:**

| Strategy | Pros | Cons |
|---|---|---|
| **Per-symbol** (function/class) | Semantically meaningful; results point to exact definitions; matches code structure; `cgc` already computes boundaries | Requires language-aware parser; very short functions may be too small to embed well |
| Fixed-window (e.g., 512 tokens) | Simple; language-agnostic | Splits functions mid-body; results harder to attribute; worse recall for "find the function that does X" |
| Hybrid (per-symbol + fallback fixed-window for non-parseable) | Best coverage | More complex |

**Recommendation: Hybrid with per-symbol primary.** Use the existing `cgc` (code-graph-context) symbol boundaries as the primary chunking unit. For files where the parser fails or produces no symbols (config files, markdown, etc.), fall back to fixed-window (512 tokens, 128-token overlap). Each chunk gets:
- `repo` metadata (filterable, for ACL lookup)
- `org_id` metadata (filterable, determines shard)
- `file_path` metadata (non-filterable, for result display)
- `symbol_name` metadata (non-filterable, for result display)
- `line_start`/`line_end` metadata (non-filterable, for code navigation)

**Metadata budget per vector:**
- Filterable: `repo` (~60 bytes) + `org_id` (36 bytes UUID) + `language` (~10 bytes) ≈ **~120 bytes** (well under 2 KB limit)
- Non-filterable: `file_path` + `symbol_name` + `line_start` + `line_end` + `chunk_text_preview` ≈ **~500 bytes** (well under 40 KB limit)

### 4.2 Personal Context Embeddings: S3 Vectors (Recommended)

The current implementation stores embeddings in `self._embeddings: dict[str, list[float]] = {}` — an in-memory dict that is **lost on pod restart**. This is the #1 gap cited in EPIC #1287.

**Options:**

| Option | Pros | Cons |
|---|---|---|
| **S3 Vectors (per-user index)** | Hard isolation (separate index per user); native ANN search; no custom similarity code | One index per active user; potential management overhead at scale (but 10,000 indexes/bucket is generous) |
| pgvector on existing Postgres | Single instance; simpler ops; hard isolation via row-level security | Puts embedding load on the gateway DB; not purpose-built for ANN; scaling concern at high user counts |
| S3 Vectors (shared personal index with metadata filter) | Fewer indexes to manage | Metadata filter is NOT a security boundary — only the Door filter provides that |

**Recommendation: S3 Vectors with one index per user for personal context.** Rationale:
1. Hard physical isolation — user A's vectors cannot appear in user B's query results even if the Door filter has a bug (defense in depth).
2. S3 Vectors supports 10,000 indexes per bucket — more than adequate for user counts.
3. Write volume per user is low (a few entries/day) — no sharding needed.
4. Eliminates the in-memory dict gap without adding load to the shared Postgres instance.

The design doc notes this is the personal-context team's call (#1287). This recommendation is informational — the code-indexing path uses sharded shared indexes regardless.

### 4.3 AGFS Backend Replacement: S3-Native Backend

The `PersonalContextStore` uses the `AGFSBackend` protocol (4 methods: `put`, `get`, `delete`, `list_prefix`). The existing `OpenVikingAGFSBackend` calls OpenViking's HTTP API. The replacement implements the same protocol against S3:

```python
class S3AGFSBackend:
    """AGFS-compatible backend using S3 (via boto3).

    Entries are stored as JSON objects at paths like:
      s3://{bucket}/personal-context/{path}

    list_prefix uses S3 list-objects-v2 with the given prefix.
    """

    def put(self, path: str, data: dict) -> None:
        # s3.put_object(Bucket=bucket, Key=f"personal-context{path}",
        #               Body=json.dumps(data))

    def get(self, path: str) -> dict | None:
        # s3.get_object(...) or return None on NoSuchKey

    def delete(self, path: str) -> None:
        # s3.delete_object(...)

    def list_prefix(self, prefix: str) -> list[dict]:
        # s3.list_objects_v2(Prefix=f"personal-context{prefix}")
        # For each key: s3.get_object and parse JSON
```

**Key design notes:**
- The S3 bucket already exists (`agent-context-platform-data-{account_id}`) — reuse it.
- Personal context entries are small (<10 KB each); S3 GET latency (~50-100ms) is acceptable for the `list_entries` path since personal context queries return 5-20 entries.
- Write path doesn't use Mountpoint (Mountpoint can't overwrite). Direct S3 API via boto3 from the pod.
- Read path CAN use Mountpoint for bulk reads, but individual entry CRUD is better via API (avoids cache staleness).

### 4.4 Code-Intel Document Store (AGFS → S3 with Mountpoint)

The ingestion pipeline currently uploads to OpenViking via `viking://resources/` URIs. These become S3 object keys:

| Current URI | New S3 key |
|---|---|
| `viking://resources/repos/{repo}/code-index.json` | `content/repos/{repo}/code-index.json` |
| `viking://resources/repos/{repo}/wiki/{page}.md` | `content/repos/{repo}/wiki/{page}.md` |
| `viking://resources/docs/{doc_id}` | `content/docs/{doc_id}` |
| `viking://resources/infrastructure/{item}` | `content/infrastructure/{item}` |

Serving pods mount the bucket read-only via Mountpoint CSI. Workers write directly via S3 API (PutObject). This is safe because:
- Each worker writes to a unique key path (no conflicts)
- Serving pods only need read access
- S3 provides strong read-after-write consistency

---

## 5. Terraform Changes

### 5.1 Resources to ADD

```hcl
# New module: modules/agent-context/terraform/modules/s3-vectors/
module "s3_vectors" {
  source = "./modules/s3-vectors"

  environment    = var.environment
  name_prefix    = local.name_prefix
  aws_region     = var.aws_region
  shard_count    = var.s3_vectors_shard_count  # default: 4
  dimension      = 1024                         # Titan Embed v2
  distance_metric = "cosine"
  irsa_role_name = module.iam.role_name
  tags           = var.tags
}
```

The module creates:
- 1 vector bucket: `adp-{env}-code-vectors-{account_id}`
- N vector indexes: `code-vectors-shard-{0..N-1}` (for code indexing)
- IAM policy attachment: `s3vectors:*` on the bucket ARN

### 5.2 Resources to ADD — Mountpoint CSI volume

The existing `s3-files` module already creates the S3 bucket and EFS. Add a Mountpoint PV/PVC alongside (or replace EFS with Mountpoint for read-only mounts):

```hcl
# In modules/s3-files or a new s3-mountpoint module:
# - PersistentVolume using the Mountpoint S3 CSI driver
# - PersistentVolumeClaim for read-only pod mounting
```

### 5.3 Resources to REMOVE

| Resource | File | Reason |
|---|---|---|
| OpenViking PVC | `kubernetes/pvcs.yaml` (openviking-data) | No longer needed |
| OpenViking ConfigMap | `kubernetes/openviking-configmap.yaml` | No longer needed |
| OpenViking Deployment + Service | `kubernetes/openviking-deployment.yaml` | No longer needed |
| OpenViking CronJob | `manifests/openviking-refresh-cronjob.yaml` | Replaced by ingestion pipeline |
| OpenViking config patch | `manifests/openviking-config-patch.yaml` | No longer needed |
| OpenViking deploy script | `scripts/deploy-openviking.sh` | No longer needed |
| S3 prefix for OpenViking data | `terraform/modules/s3-files/main.tf` (lines 310-314) | Migrate to new prefix |
| `openviking-root-key` secret | `agent-context-secrets` | No longer needed |

### 5.4 IAM Policy Updates

Add to the IRSA role policy:
```json
{
  "Effect": "Allow",
  "Action": [
    "s3vectors:CreateVectorBucket",
    "s3vectors:CreateIndex",
    "s3vectors:PutVectors",
    "s3vectors:QueryVectors",
    "s3vectors:GetVectors",
    "s3vectors:DeleteVectors",
    "s3vectors:ListVectors",
    "s3vectors:GetIndex",
    "s3vectors:ListIndexes"
  ],
  "Resource": "arn:aws:s3vectors:${region}:${account}:vector-bucket/adp-*"
}
```

---

## 6. Code Changes — File-Level Plan

### 6.1 New Files

| Path | Purpose |
|---|---|
| `modules/agent-context/personal_context/backends/__init__.py` | Package for backend implementations |
| `modules/agent-context/personal_context/backends/s3_backend.py` | S3AGFSBackend (replaces OpenVikingAGFSBackend) |
| `modules/agent-context/personal_context/backends/s3_vectors_backend.py` | S3 Vectors client for embedding persistence |
| `modules/agent-context/terraform/modules/s3-vectors/main.tf` | S3 Vectors bucket + indexes Terraform |
| `modules/agent-context/terraform/modules/s3-vectors/variables.tf` | Variables for shard count, dimension, etc. |
| `modules/agent-context/terraform/modules/s3-vectors/outputs.tf` | Bucket name, index ARNs |

### 6.2 Modified Files

| Path | Change |
|---|---|
| `personal_context/synthesis.py` | Replace `_create_agfs_backend()` → use `S3AGFSBackend` |
| `personal_context/experience_tool.py` | Replace in-memory `_embeddings` dict → S3 Vectors client |
| `images/ingestion/ingest-repo.py` | Replace `upload_to_openviking()` → S3 PutObject |
| `images/ingestion/ingest-url.py` | Same |
| `images/ingestion/ingest-doc.py` | Same |
| `images/ingestion/refresh-repos.py` | Replace OpenViking read/write → S3 |
| `images/ingestion/lint-wiki.py` | Replace `list_from_openviking`, `read_from_openviking`, `upload_to_openviking` → S3 |
| `images/ingestion/generate-indexes.py` | Replace `search_openviking()` → S3 Vectors QueryVectors |
| `images/ingestion/correlate.py` | Replace OpenViking read/write → S3 |
| `images/ingestion/discover-infra.py` | Replace OpenViking upload → S3 |
| `images/ingestion/publish-ingestion.py` | Remove "openviking" target; add S3 Vectors target |
| `images/ingestion/sqs-worker.py` | Update status tracking (no OpenViking) |
| `terraform/main.tf` | Add `module "s3_vectors"` block |
| `terraform/modules/iam/main.tf` | Add S3 Vectors IAM permissions |
| `config.env` | Remove `OPENVIKING_ROOT_KEY_SECRET_ID`, add S3 Vectors config |
| `deploy.sh` | Remove OpenViking deployment steps |

### 6.3 Files to DELETE

| Path | Reason |
|---|---|
| `kubernetes/openviking-deployment.yaml` | Replaced |
| `kubernetes/openviking-configmap.yaml` | Replaced |
| `manifests/openviking-config-patch.yaml` | Replaced |
| `manifests/openviking-refresh-cronjob.yaml` | Replaced by ingestion pipeline |
| `scripts/deploy-openviking.sh` | Replaced |
| `scripts/add-repo.sh` | OpenViking-specific; functionality moves to ingestion pipeline |
| `scripts/check-index-status.sh` | OpenViking health checks no longer needed |

---

## 7. Migration Strategy

### 7.1 Phase 1: Add New Backend (Non-Breaking)

1. Implement `S3AGFSBackend` and `S3VectorsEmbeddingStore`
2. Add Terraform module for S3 Vectors (create bucket + indexes)
3. Add Mountpoint CSI PV/PVC for read-only serving
4. Wire new backends behind a feature flag (`STORAGE_BACKEND=openviking|s3`)
5. Tests pass with both backends

### 7.2 Phase 2: Dual-Write (Transition)

1. Ingestion pipeline writes to BOTH OpenViking and S3/S3 Vectors
2. Personal context writes to both
3. Reads switch to S3/S3 Vectors (with OpenViking as fallback)
4. Validate: all queries return equivalent results

### 7.3 Phase 3: Cut Over

1. Stop writing to OpenViking
2. Remove OpenViking deployment manifests
3. Remove OpenViking Terraform/k8s resources
4. Run full re-ingestion against S3 Vectors to ensure completeness
5. Delete OpenViking PVC data

### 7.4 Phase 4: Cleanup

1. Remove dual-write code paths
2. Remove feature flag
3. Remove `OV_URL`, `OPENVIKING_ROOT_KEY` env vars
4. Update all documentation
5. SBOM license gate confirms no AGPL dependencies

---

## 8. S3 Vectors Index Sharding — Detailed Design

### 8.1 Index Naming

```
Vector bucket: adp-{env}-code-vectors-{account_id}
Indexes:
  code-shard-0   (org_id hash % 4 == 0)
  code-shard-1   (org_id hash % 4 == 1)
  code-shard-2   (org_id hash % 4 == 2)
  code-shard-3   (org_id hash % 4 == 3)
```

### 8.2 Write Path (Ingestion Worker)

```python
import hashlib

def get_shard_index(org_id: str, shard_count: int = 4) -> int:
    """Deterministic shard assignment by org_id hash."""
    h = hashlib.sha256(org_id.encode()).digest()
    return int.from_bytes(h[:4], "big") % shard_count

def write_vectors_to_s3v(vectors: list[dict], org_id: str):
    shard = get_shard_index(org_id)
    index_name = f"code-shard-{shard}"
    # Batch into groups of 500 (S3 Vectors PutVectors limit)
    for batch in chunked(vectors, 500):
        s3vectors_client.put_vectors(
            vectorBucketName=BUCKET_NAME,
            indexName=index_name,
            vectors=[{
                "key": f"{repo}:{file_path}:{symbol}:{line_start}",
                "data": {"float32": v["embedding"]},
                "metadata": {
                    "repo": v["repo"],
                    "org_id": org_id,
                    "language": v["language"],
                    # Non-filterable (stored but not indexed):
                    "file_path": v["file_path"],
                    "symbol_name": v["symbol_name"],
                    "line_start": str(v["line_start"]),
                    "line_end": str(v["line_end"]),
                }
            } for v in batch]
        )
```

### 8.3 Query Path (Context MCP Server)

```python
def semantic_search(query_text: str, caller_identity, limit: int = 20):
    # 1. Embed the query
    query_vector = embedding_client.embed(query_text)

    # 2. Scatter: query ALL shards
    all_results = []
    for shard_idx in range(SHARD_COUNT):
        index_name = f"code-shard-{shard_idx}"
        response = s3vectors_client.query_vectors(
            vectorBucketName=BUCKET_NAME,
            indexName=index_name,
            queryVector={"float32": query_vector},
            topK=min(limit * 2, 100),  # Over-fetch for post-filter
            filter={"org_id": {"$eq": caller_identity.org_id}},
        )
        all_results.extend(response["vectors"])

    # 3. Merge: sort by distance score
    all_results.sort(key=lambda r: r["distance"])

    # 4. Permission filter at the Door
    allowed_results = door_filter(all_results, caller_identity)

    # 5. Return top-K
    return allowed_results[:limit]
```

### 8.4 Scaling Path

- **Current:** 4 shards → 10,000 vectors/sec aggregate write capacity
- **Future (if needed):** Increase to 8 or 16 shards by creating new indexes and re-sharding. The hash function is deterministic, so re-indexing distributes data correctly.
- **Per-org indexes:** If a single org dominates write load, split it to its own dedicated index. The query scatter-gather handles mixed topologies.

---

## 9. Personal Context Embedding Persistence

### 9.1 Current State (Broken)

```python
# experience_tool.py line 60
self._embeddings: dict[str, list[float]] = {}  # LOST ON POD RESTART
```

### 9.2 Replacement: S3 Vectors Per-User Index

```python
class S3VectorsEmbeddingStore:
    """Persistent embedding storage using one S3 Vectors index per user."""

    def __init__(self, bucket_name: str):
        self.bucket_name = bucket_name

    def _index_name(self, owner_sub: str) -> str:
        """One index per user for hard isolation."""
        return f"personal-{owner_sub}"

    def ensure_index(self, owner_sub: str) -> None:
        """Create user's index if it doesn't exist (idempotent)."""
        # CreateIndex with dimension=1024, distanceMetric=cosine

    def store(self, owner_sub: str, entry_id: str, embedding: list[float]) -> None:
        """Store an embedding for an entry."""
        # PutVectors with key=entry_id, vector=embedding

    def recall(self, owner_sub: str, query_embedding: list[float],
               top_k: int = 20) -> list[tuple[str, float]]:
        """Find similar entries. Returns (entry_id, distance) pairs."""
        # QueryVectors against the user's index

    def delete(self, owner_sub: str, entry_id: str) -> None:
        """Remove an embedding when an entry is deleted."""
        # DeleteVectors with key=entry_id
```

**Integration in ExperienceTool:**
- `_save`: calls `self.embedding_store.store(identity.owner_sub, entry_id, embedding)`
- `_recall`: calls `self.embedding_store.recall(identity.owner_sub, query_embedding, limit*2)` then joins with entry data from S3 for decay scoring
- Removes `self._embeddings` dict entirely

### 9.3 Lazy Index Creation

Indexes are created on first `save` for a user (idempotent `CreateIndex` call). This avoids pre-creating indexes for users who never use the experience tool. Error handling: if `CreateIndex` returns "already exists", treat as success.

---

## 10. Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| S3 Vectors not available in deploy region | Low (service is GA) | Blocks entire feature | Verify in target region before Phase 1. Fallback: use `us-east-1` or `us-west-2`. |
| Write throughput exceeded during bulk re-index | Medium | Slow ingestion, not data loss | 4-shard design provides 10K vectors/sec headroom. Add exponential backoff + retry. Monitor via CloudWatch. |
| Scatter-gather query latency across 4 shards | Low | Slightly slower semantic search | S3 Vectors queries are fast (~10-50ms). Parallel async queries. Total < 100ms even with 4 shards. |
| Personal context index-per-user creates management overhead | Low | More indexes to track | 10,000 indexes/bucket limit is generous. Cleanup on user deletion. |
| AGFS protocol semantics not fully preserved | Medium | Broken personal context CRUD | The S3 backend implements the same 4-method protocol. Existing tests (`test_isolation.py`, `test_experience_tool.py`) use `FakeAGFSBackend` — run them against the real S3 backend in integration tests. |
| Mountpoint cache staleness for recently-written content | Low | Slight delay in content availability | Mountpoint has configurable cache TTL. For the code-intel read path, data is written minutes before query. TTL of 60s is safe. |

---

## 11. Cost Comparison

| Component | Before (OpenViking) | After (S3 Vectors + S3) |
|---|---|---|
| Compute | 1 pod: 2-4 CPU, 8-16 GB RAM (~$150-250/mo on EKS) | $0 (serverless) |
| Storage | EBS PVC (~$50/mo for 500 GB gp3) | S3 Vectors: ~$5-15/mo for 50M vectors; S3: ~$10/mo for index files |
| Queries | Included in pod cost | S3 Vectors: ~$2.50/M queries; ~$5/mo for typical usage |
| **Total** | **~$200-300/mo** | **~$20-30/mo** |

**Net savings: ~$200/mo**, plus elimination of single-point-of-failure risk and AGPL licensing burden.

---

## 12. Acceptance Criteria (for implementing PRs)

1. **OpenViking pod is not deployed** — `kubectl get deployment openviking-server -n agent-context` returns "not found"
2. **S3 Vectors indexes exist** — `aws s3vectors list-indexes --vector-bucket-name adp-dev-code-vectors-*` returns 4 code shards
3. **Personal context persists across pod restart** — save an entry, restart pod, recall succeeds
4. **Semantic search returns results** — query for a known concept in an indexed repo returns relevant code
5. **Existing tests pass unchanged** — `test_isolation.py`, `test_experience_tool.py`, `test_synthesis.py`
6. **SBOM license gate clean** — no AGPL dependencies in the bill of materials
7. **No OpenViking references in deployed manifests** — `grep -r openviking k8s/ manifests/` returns nothing

---

## 13. Open Questions (Deferred to Implementation)

1. **S3 Vectors region confirmation** — must verify before Terraform apply (tracked as pre-req in deployment section of #1348).
2. **Non-filterable metadata key limit** — S3 Vectors allows max 10 non-filterable metadata keys per index. Our design uses 4-5 (file_path, symbol_name, line_start, line_end, optionally chunk_preview). Fits comfortably.
3. **`browse` feature rebuild** — low priority; can be implemented as S3 list-objects + presigned URL reads. Defer to follow-up issue.
4. **Synthesis CronJob backend swap** — straightforward once S3AGFSBackend exists; synthesis.py line 576 (`_create_agfs_backend`) is the only callsite.

---

## 14. Implementation Order (Recommended for @agent-developer)

```
1. Terraform: s3-vectors module (bucket + 4 code indexes)        [~2h]
2. S3AGFSBackend implementation + unit tests                      [~3h]
3. S3VectorsEmbeddingStore implementation + unit tests            [~3h]
4. Wire new backends in experience_tool.py + synthesis.py         [~2h]
5. Ingestion pipeline: replace upload_to_openviking → S3          [~4h]
6. Remove OpenViking manifests/scripts/config                     [~1h]
7. IAM policy updates + Mountpoint CSI PV                         [~1h]
8. Integration tests (semantic search E2E)                        [~2h]
9. Re-index and validate                                          [~1h]
```

Total estimated effort: ~19h (split across 2-3 PRs for reviewability).
