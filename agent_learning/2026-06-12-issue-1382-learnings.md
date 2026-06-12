# Learnings: Issue #1382 — DeepWiki Output → S3 + S3 Vectors

**Date:** 2026-06-12
**Agent:** @agent-architect
**Issue:** #1382 (Sub of EPIC #1345)
**Outcome:** Design review posted, design note written

---

## Key Technical Decisions

### 1. Phased delivery is critical for dependency ordering
- S3 Vectors terraform module (`modules/agent-context/terraform/modules/s3-vectors/`) does NOT exist yet
- Issue #1383 (OPEN) provisions this infrastructure
- **Decision:** Split into Phase A (S3 write, no blockers) and Phase B (embed, after #1383)
- This avoids the storage gap: wiki moves out of OpenViking immediately without waiting for vectors

### 2. Embedding dimension disambiguation
- DeepWiki's `embedder.json` uses **256-dim** Titan Embed v2 — this is its INTERNAL retriever for context during generation
- The pipeline's embedding client (`personal_context/embeddings.py`) uses **1024-dim** Titan Embed v2
- The S3 Vectors indexes (per `design-1348`) are dimensioned for **1024**
- **Gotcha:** Someone implementing this could easily reach for DeepWiki's embedder config and produce wrong-dimension vectors

### 3. S3 write mechanism: boto3, NOT Mountpoint filesystem
- Mountpoint supports write-once semantics (or overwrite with `--allow-overwrite`)
- Re-indexing a repo OVERWRITES the same wiki path → needs native S3 PutObject
- Read path correctly uses Mountpoint (strong read-after-write consistency)
- The TF module header confirms "Full-object overwrite: supported" but relying on mount options is fragile

### 4. Object naming convention
- Existing convention: `wikis/{org}-{repo}-wiki.md` (with `-wiki` suffix)
- Two consumers already read from `/platform-data/wikis/{safe_name}-wiki.md`:
  - `ingest-repo.py:1101` (GraphRAG extraction reads wiki for entity extraction)
  - `generate-learning-artifacts.py:76` (learning pipeline reads wiki)
- Breaking this convention silently breaks downstream consumers

### 5. ACL enforcement at Door, NOT in vector metadata
- `allowed_principals` is a JSON array → cannot be S3 Vectors filterable metadata (scalar only)
- S3 Vectors filterable metadata: max 2 KB, up to 50 keys, scalar types
- The correct pattern: filter vectors by `repo`, then look up `repositories.allowed_principals` post-query

---

## What Worked

- **Reading the design-1348 note first** — it has the authoritative S3 Vectors specs (GA status, dimension limits, write throughput, metadata constraints)
- **Checking existing consumers** before proposing naming changes — found two scripts that already read from the wiki path
- **Verifying the DynamoDB state table** — `deepwiki_status` is tracked there but NOT in Postgres `repositories` table (gap identified)
- **Cross-referencing issue dependency chain** via `gh issue list` — confirmed #1383 is the blocker for Phase B

## What Took Multiple Attempts

- Understanding the relationship between DeepWiki's `embedder.json` (internal) and the pipeline's `embeddings.py` (external) — required reading both the DeepWiki Dockerfile config AND how `ingest-repo.py` calls the DeepWiki API
- Finding the existing wiki read paths — they're scattered across `ingest-repo.py` and `generate-learning-artifacts.py`, not obvious from the issue body

## Gotchas for Future Agents

1. **The `repositories` table has NO `wiki_status` column** — only `zoekt_status`, `vectors_status`, `structure_status`, `sbom_status`. A new migration is needed.
2. **DynamoDB tracks `deepwiki_status`** (operational state) separately from Postgres (catalog state). Both need updating.
3. **The S3 bucket name** is `agent-context-platform-data-{account_id}` — not in `config.py` yet. Must be added as an env var.
4. **DeepWiki's streaming endpoint** returns full text (not SSE chunks) when `stream=False` — see `ingest-repo.py:579`
5. **Issue #1383** is the implementation issue for S3 Vectors infra. #1348 was the DESIGN issue (CLOSED). Don't confuse them.

## File Reference Map

| Purpose | Path |
|---|---|
| Main ingestion pipeline | `modules/agent-context/images/ingestion/ingest-repo.py` |
| Centralized config | `modules/agent-context/images/ingestion/config.py` |
| Embedding client (1024-dim) | `modules/agent-context/personal_context/embeddings.py` |
| DeepWiki internal embedder (256-dim) | `modules/agent-context/images/deepwiki/config/embedder.json` |
| DeepWiki model config | `modules/agent-context/images/deepwiki/config/generator.json` |
| S3 bucket + Mountpoint TF | `modules/agent-context/terraform/modules/s3-files/main.tf` |
| Catalog schema (migration 001) | `modules/agent-context/alembic/versions/001_knowledge_layer_schema.py` |
| Knowledge Layer design of record | `docs/knowledge-layer-storage-design.md` |
| S3 Vectors validated design | `docs/design-1348-replace-openviking.md` |
| DynamoDB state tracking | `modules/agent-context/terraform/modules/dynamodb-state/main.tf` |
| Wiki consumer: GraphRAG | `ingest-repo.py:1099-1107` |
| Wiki consumer: learning | `generate-learning-artifacts.py:76` |
