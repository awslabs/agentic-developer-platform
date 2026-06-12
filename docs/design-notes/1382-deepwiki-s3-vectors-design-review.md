# Design Note: DeepWiki Output → S3 (Browse) + S3 Vectors (Semantic)

**Issue:** #1382 (sub of #1345)
**Author:** @agent-architect
**Date:** 2026-06-12
**Status:** Design review complete — ready with caveats
**Companion docs:** `docs/knowledge-layer-storage-design.md` (§5c), `docs/design-1348-replace-openviking.md` (§4.1, §4.4)

---

## 1. Executive Summary

This note validates and refines the design for re-homing DeepWiki wiki output (and code-index markdown) from OpenViking to S3 (human-browsable artifact) and S3 Vectors (semantic embeddings). The dual-sink design is sound. Seven refinements are recommended for implementation.

**Core insight confirmed:** Wiki prose is materially better semantic-search input than raw code. A natural-language wiki sentence ("this module implements token-bucket throttling") matches concept queries far better than the raw class definition. The wiki should be the primary embedding target.

---

## 2. Architecture Decision: Phased Delivery

The S3 Vectors infrastructure does not yet exist (terraform module `modules/agent-context/terraform/modules/s3-vectors/` is planned in #1383 but unbuilt). To avoid a storage gap when #1348 removes OpenViking:

| Phase | What | Blocked on | Can land |
|---|---|---|---|
| **A** | Write wiki + code-index MD to S3; update catalog | Nothing (S3 bucket + `wikis/` prefix exist via #1354) | Immediately |
| **B** | Chunk wiki by section; embed via LiteLLM→Titan (1024-dim); write to S3 Vectors | #1383 (S3 Vectors infra) | After #1383 |

Phase A eliminates the OpenViking dependency. Phase B adds semantic search over wiki text.

---

## 3. S3 Write Path (Phase A)

### 3.1 Object Key Convention

Use the existing pattern already consumed by two downstream scripts:

```
s3://{bucket}/wikis/{org}-{repo}-wiki.md
s3://{bucket}/code-indexes/{org}-{repo}-code-index.md
```

Where `{org}-{repo}` = `org_repo.replace("/", "-")` (the `safe_name` pattern at `ingest-repo.py:1071`).

Evidence of existing consumers:
- `ingest-repo.py:1101` reads from `/platform-data/wikis/{safe}-wiki.md`
- `generate-learning-artifacts.py:76` reads from the same path

### 3.2 Write Mechanism

**Write via boto3 `s3.put_object()`**, NOT via Mountpoint filesystem write.

Rationale:
- Re-indexing a repo produces a new wiki for the same path → requires overwrite
- Mountpoint semantics for overwrite require `--allow-overwrite` mount option which may not be configured on all pod mounts
- S3 PutObject natively supports overwrite with strong read-after-write consistency
- Read path via Mountpoint remains correct (pods mount the bucket read-only)

```python
import boto3

s3 = boto3.client("s3", region_name=settings.aws_region)

def write_wiki_to_s3(wiki_text: str, org_repo: str, bucket: str) -> str:
    """Write wiki markdown to S3. Returns the object key."""
    safe_name = org_repo.replace("/", "-")
    key = f"wikis/{safe_name}-wiki.md"
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=wiki_text.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )
    return key
```

### 3.3 Catalog Update

The `repositories` table (migration 001) lacks a `wiki_status` column. Add via migration 002:

```sql
ALTER TABLE repositories
    ADD COLUMN wiki_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    ADD COLUMN wiki_s3_key VARCHAR(512);
```

After successful write:
```sql
UPDATE repositories
SET wiki_status = 'complete',
    wiki_s3_key = 'wikis/{org}-{repo}-wiki.md',
    updated_at = NOW()
WHERE repo_name = '{org}/{repo}';
```

### 3.4 SHA Gating

Before regenerating a wiki, compare the current `HEAD` SHA against `repositories.last_indexed_sha`. If unchanged, skip both wiki generation and embedding. This prevents redundant Bedrock calls (~$0.03/wiki generation + ~$0.001/embedding pass).

---

## 4. S3 Vectors Embedding Path (Phase B)

### 4.1 Embedding Dimension: 1024 (NOT 256)

| Source | Dimension | Purpose |
|---|---|---|
| `images/deepwiki/config/embedder.json` | 256 | DeepWiki's INTERNAL retriever (for context during generation) |
| `personal_context/embeddings.py` | 1024 | Pipeline's LiteLLM→Titan embed client |
| `design-1348` §4.1 metadata budget | 1024 | S3 Vectors index dimension |

The ingestion pipeline MUST use its own 1024-dim embedding path (`LiteLLMEmbeddingClient`), not DeepWiki's internal 256-dim embedder. They serve different purposes.

### 4.2 Wiki Chunking Strategy: Section-Aware

DeepWiki generates wiki content with consistent H2 structure:
```markdown
## Overview
...
## Architecture
...
## Key Components
...
## Code Organization
...
```

Chunking rules:
1. **Split on `## ` (H2) boundaries** — each H2 section becomes one chunk
2. **Context prefix:** Prepend `{org}/{repo} Architecture Wiki — {heading}` to each chunk (aids retrieval when the chunk is decontextualized)
3. **Minimum size:** If a section < 100 words, merge with the next section
4. **Maximum size:** If a section > 2000 tokens, split further on `### ` (H3) boundaries; if still too large, split on paragraph (`\n\n`) boundaries
5. **Include the `## Overview` section in ALL chunks** as a prefix (it provides document-level context that improves embedding quality)

### 4.3 Vector Metadata Schema

Per S3 Vectors constraints (2 KB filterable, 40 KB non-filterable):

**Filterable metadata** (used in query filters):
```json
{
  "repo": "org/repo-name",
  "org_id": "org-login",
  "source_type": "wiki"
}
```

**Non-filterable metadata** (returned with results, used for display):
```json
{
  "section_heading": "## Key Components",
  "chunk_index": 3,
  "total_chunks": 8
}
```

**NOT in metadata:**
- `allowed_principals` — this is a JSON array (variable-length), incompatible with S3 Vectors' scalar filterable metadata. ACL enforcement happens POST-query via the Door filter, which looks up `repositories.allowed_principals` using the `repo` from the vector result.

### 4.4 Query-Time ACL Flow

```
Agent query: "where is rate limiting implemented?"
    ↓
S3 Vectors QueryVectors(filter: {source_type: "wiki"})
    ↓ returns [{repo: "org/api-gateway", section: "## Architecture", score: 0.87}, ...]
    ↓
Door filter: SELECT allowed_principals FROM repositories WHERE repo_name = result.repo
    ↓ if caller NOT in allowed_principals → drop result
    ↓
Return filtered results to agent
```

---

## 5. Dependency Ordering

```
#1354 (Mountpoint/S3 bucket) — CLOSED ✅
    ↓
#1355 (Catalog schema) — CLOSED ✅
    ↓
#1382 Phase A (wiki → S3 + catalog) — THIS ISSUE
    ↓
#1383 (S3 Vectors infra) — OPEN, in progress
    ↓
#1382 Phase B (wiki → S3 Vectors embeddings)
    ↓
#1348 removal of OpenViking (safe — wiki has a home)
    ↓
#1356 (ACL filter at Door) — OPEN
```

Phase A must land WITH OR BEFORE #1348's OpenViking removal so the wiki never has a storage gap. Phase B can follow at any time after #1383.

---

## 6. DeepWiki JSON Second-Source-of-Truth Resolution

**Decision: Document as intentional DeepWiki-container config.**

| File | Controls | Changed by |
|---|---|---|
| `config.py:model_wiki` | Model passed in the API request body to DeepWiki | ConfigMap env var |
| `images/deepwiki/config/generator.json` | DeepWiki's internal model routing (may override API request) | Container image rebuild |
| `images/deepwiki/config/embedder.json` | DeepWiki's internal retriever (NOT the pipeline's embedder) | Container image rebuild |

These JSON files are analogous to a Docker CMD — they're build-time configuration for a third-party container. Attempting to template them from `config.py` at deploy time adds fragile coupling (volume mounts, init containers, race conditions). The simpler approach:

1. Add a comment in `config.py` above `model_wiki`:
   ```python
   # Note: DeepWiki's internal model routing is configured separately in
   # images/deepwiki/config/generator.json (container build-time config).
   # This variable controls the model passed via API request; DeepWiki
   # may use its own config for internal processing.
   ```

2. Add a comment in `generator.json`:
   ```json
   // This is DeepWiki's internal config. The ingestion pipeline also
   // passes a model via API request (see config.py:model_wiki).
   // Keep these in sync manually when changing models.
   ```

---

## 7. Code-Index Markdown: Same Treatment

`ingest-repo.py:1040–1046` uploads `code_index_md` to `viking://resources/{org_repo}/.code-index.md`. This needs the same S3 treatment:

```python
def write_code_index_md_to_s3(md_text: str, org_repo: str, bucket: str) -> str:
    safe_name = org_repo.replace("/", "-")
    key = f"code-indexes/{safe_name}-code-index.md"
    s3.put_object(Bucket=bucket, Key=key, Body=md_text.encode("utf-8"),
                  ContentType="text/markdown; charset=utf-8")
    return key
```

This replaces the `upload_to_openviking()` call for code-index markdown. The structured JSON (`_write_code_index_to_filesystem`) already writes to the correct filesystem path.

---

## 8. Implementation Checklist

### Phase A (no blockers — land now)
- [ ] Add `s3_bucket_name` to `config.py` (env var, default from terraform output)
- [ ] Implement `write_wiki_to_s3()` in `ingest-repo.py`
- [ ] Implement `write_code_index_md_to_s3()` in `ingest-repo.py`
- [ ] Replace `upload_to_openviking()` calls for wiki + code-index MD
- [ ] Add migration 002: `wiki_status` + `wiki_s3_key` columns
- [ ] Update `update_dynamo_state()` to report new status
- [ ] Add SHA-gating check before wiki regeneration
- [ ] Update DynamoDB `deepwiki_status` to reflect S3 write success
- [ ] Remove `viking://` URI construction for these two artifacts
- [ ] Document DeepWiki JSON as intentional container config

### Phase B (after #1383)
- [ ] Implement wiki chunker (section-aware, H2 boundaries)
- [ ] Wire chunker → `LiteLLMEmbeddingClient` (1024-dim)
- [ ] Write vectors to S3 Vectors with metadata schema above
- [ ] Update `vectors_status` in catalog after wiki embedding
- [ ] Add `source_type: wiki` filter support to semantic search query path

---

## References

- Parent EPIC: #1345
- Design of record: `docs/knowledge-layer-storage-design.md` (§5c, §7, §8)
- S3 Vectors validated specs: `docs/design-1348-replace-openviking.md` (§2.1)
- Mountpoint semantics: `modules/agent-context/terraform/modules/s3-files/main.tf` (header comments)
- Existing wiki consumers: `ingest-repo.py:1099–1107`, `generate-learning-artifacts.py:76`
- Dependency: #1383 (S3 Vectors infra), #1354 (Mountpoint), #1355 (catalog)
