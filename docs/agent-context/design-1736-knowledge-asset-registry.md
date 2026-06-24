# Design Note: Knowledge Asset Registry + Management UI

**Issue:** #1736 (Child EPIC #1345)
**Status:** Design spike — produced by @agent-architect
**Date:** 2026-06-23 (updated 2026-06-24: phased trigger model + extensible asset_type)
**Depends on:** #1721 (tenant isolation schema), #1728 (project scoping)
**Reconciles with:** E7 #1672 (self-serve indexing engine)
**Aligns with:** `docs/agent-context/knowledge-layer-design.md` (consolidated design of record)

---

## 1. Problem Statement

The Knowledge Layer's "what to index" is a set of platform-admin-owned flat files (`index_content/repos.txt`, `urls.txt`, `docs.txt`) checked into the repository. There is:

- **No user-facing way to register an asset** (repos, URLs, docs) for indexing.
- **No queryable source-of-truth** for what's registered, under whose scope.
- **No multi-tenant ownership model** for indexed assets — everything is global/shared.

The ingestion pipeline (`publish-ingestion.py` + `sqs-worker.py`) reads these flat files, checks DynamoDB state, and enqueues SQS messages. Scope (#1721) will be added to the SQS envelope, but the *trigger source* remains the flat files.

**This EPIC introduces:**
1. A `knowledge_assets` DB table as the **single queryable registry** (source of truth for "what to index + scope").
2. A **management UI** for users to add/remove assets (one at a time, personal scope) and tenant admins to **bulk-upload** a file (tenant scope).
3. An **API contract** connecting the registry to the ingestion engine (#1672).

---

## 2. Core Principle: Decouple "What to Index + Scope" from "Who Triggered It"

**Before (current state):**
```
flat file (repos.txt) → publish-ingestion.py → SQS → worker
```
- Scope must be inferred at trigger time or is absent entirely.
- #1319 (GitHub-sender → cognito_sub) sits on the ingestion critical path.

**After (target state):**
```
User/Admin → Gateway API → knowledge_assets row (scope stamped at write) → SQS → worker (reads scope off row)
```
- Scope (`tenant_id`, `owner_sub`, `project_id`) is written into the registry row **at registration time** from the authenticated gateway session (Cognito JWT → identity).
- Ingestion reads scope from the row — it never needs to resolve identity itself.
- **#1319 moves off the ingestion critical path** — it's needed only at query time for ACL enforcement.

This is the key architectural win of the registry approach.

---

## 3. Registry Schema

### 3.1 Table: `knowledge_assets`

**Location:** `agent_context` database (shared RDS instance, same as `repositories`, `index_runs`, etc.)

**Migration:** `004_knowledge_assets.py` in `modules/agent-context/alembic/versions/`

```sql
CREATE TABLE knowledge_assets (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- What kind of asset (OPEN VARCHAR — NOT an ENUM or CHECK constraint)
    -- v1 values: 'repo', 'url', 'doc'
    -- New types are added via ASSET_TYPE_REGISTRY config, not DDL changes
    asset_type      VARCHAR(32) NOT NULL,

    -- The reference (what to index)
    source_ref      VARCHAR(2048) NOT NULL, -- git URL / web URL / S3 doc path

    -- Display metadata
    display_name    VARCHAR(512),           -- Optional human-friendly name
    tags            JSONB DEFAULT '{}'::jsonb,  -- Arbitrary user tags

    -- Type-specific fields (extensibility — common fields stay as real columns)
    -- repos: {"default_branch": "main", "is_monorepo": true}
    -- urls: {"crawl_depth": 2, "selector": ".content"}
    -- docs: {"format": "pdf", "page_count": 42}
    -- Future types add their own JSONB shape here — zero schema migration
    metadata        JSONB DEFAULT '{}'::jsonb,

    -- Scope (who owns this registration)
    tenant_id       VARCHAR(256),           -- NULL = shared/platform corpus
    owner_sub       VARCHAR(128),           -- NULL = org-level (not personal)
    project_id      UUID,                   -- NULL = no project grouping (reserved for #1728)

    -- Lifecycle
    status          VARCHAR(32) NOT NULL DEFAULT 'registered',
                    -- registered → queued → indexing → indexed → failed → removed
    last_error      TEXT,
    retry_count     INTEGER NOT NULL DEFAULT 0,

    -- Provenance
    registered_by   VARCHAR(255) NOT NULL,  -- cognito_sub of the registrant
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

**Design decision: open `asset_type` + `metadata` JSONB (operator, 2026-06-24)**

Adding a new asset type must be a **config + handler change, not a schema migration**:
- `asset_type` is `VARCHAR(32)` — **NOT** a Postgres ENUM or `CHECK(... IN (...))`. Those force a migration per new type.
- Type-specific fields go in `metadata JSONB`, not new columns. The four-column spine (`id`, `asset_type`, `source_ref`, `status`) plus scope columns remain stable.
- Validation of `asset_type` values happens at the API layer against `ASSET_TYPE_REGISTRY` (§6.4), not via DDL constraints. Unknown types are rejected by the API (400), not the DB.
- v1 implements `repo`/`url`/`doc`. Future types (e.g. Confluence space, Slack channel, S3 bucket prefix) slot in by adding a registry entry + handler — zero schema changes.

### 3.2 Uniqueness / Dedup Rule

```sql
-- A given source_ref can only be registered once per scope.
-- Scope = (tenant_id, owner_sub). NULL-safe via COALESCE for unique index.
CREATE UNIQUE INDEX uq_knowledge_assets_source_scope
    ON knowledge_assets (
        source_ref,
        COALESCE(tenant_id, '__shared__'),
        COALESCE(owner_sub, '__org__')
    )
    WHERE status != 'removed';
```

**Semantics:**
- The same repo URL can be registered once as shared, once per tenant, and once per user — those are distinct scopes with distinct isolation.
- A `removed` asset does NOT block re-registration (the `WHERE` clause excludes it).
- Attempting to register a duplicate within the same scope returns HTTP 409 Conflict.

### 3.3 Indexes

```sql
CREATE INDEX ix_knowledge_assets_tenant ON knowledge_assets(tenant_id);
CREATE INDEX ix_knowledge_assets_owner ON knowledge_assets(owner_sub);
CREATE INDEX ix_knowledge_assets_status ON knowledge_assets(status);
CREATE INDEX ix_knowledge_assets_type_status ON knowledge_assets(asset_type, status);
CREATE INDEX ix_knowledge_assets_project ON knowledge_assets(project_id) WHERE project_id IS NOT NULL;
```

### 3.4 Relationship to `repositories` Table

The existing `repositories` table is the **indexed-content catalog** — it stores post-indexing metadata (SHA, per-stage status, ACL, Zoekt/vectors/structure/SBOM status). The `knowledge_assets` table is the **registration catalog** — it stores pre-indexing intent (what to index, under whose scope).

**Relationship:** one `knowledge_assets` row (registration intent) may produce one `repositories` row (indexed result for repos) or equivalent indexed artifacts (for URLs/docs). They are linked by `source_ref` matching `repositories.git_url` (for repos). A formal FK is NOT added because:
1. URLs and docs don't produce `repositories` rows.
2. The `repositories` table may be populated by other paths (e.g., flat-file manifests for the shared corpus).

**Status sync:** when ingestion completes for a registry row, the registry status updates to `indexed`. The detailed per-stage status lives on `index_run_stages` (not duplicated here).

### 3.5 Decision: Why a New Table (Not Extending `repositories`)

The `repositories` table is repo-specific (columns like `zoekt_status`, `vectors_status`, `last_indexed_sha` make no sense for a URL or doc). The registry must handle **three asset types** uniformly. A new table is cleaner than overloading `repositories` with nullable columns for URLs/docs.

The alternative (one table per type) was rejected because the management UI, quota enforcement, and bulk-upload all benefit from a single, uniform registry.

---

## 4. Seam with E7 #1672 (Self-Serve Indexing Engine)

### 4.1 Ownership Boundary

| Concern | Owner | Notes |
|---------|-------|-------|
| **Registry table** (`knowledge_assets`) | **This EPIC (#1736)** | Schema, CRUD, uniqueness, status lifecycle |
| **Management UI** (add/remove/list assets, bulk-upload) | **This EPIC (#1736)** | React page in gateway frontend |
| **Bulk-upload endpoint + parsing** | **This EPIC (#1736)** | `POST /api/agent-context/assets/bulk` |
| **Phase 1 inline SQS dispatch** (API → SQS) | **This EPIC (#1736)** | Inline publish in API handler; row-first invariant (§6.1) |
| **Type→steps registry** (`ASSET_TYPE_REGISTRY`) | **This EPIC (#1736)** | Config-driven step resolution; new types = config change only (§6.4) |
| **Registration-time quota soft-check** | **This EPIC (#1736)** | Rejects registration above per-scope limit (§4.3) |
| **Phase 2 sweeper-with-nudge** (registry → SQS with fairness) | **E7 #1672** | Replaces Phase 1 inline when load warrants (§6.1) |
| **Hard quota enforcement at trigger time** | **E7 #1672** | Re-validates before enqueue (guards concurrent registration race) |
| **Access verification** (can this GitHub App clone the repo?) | **E7 #1672** | Pre-flight check before enqueue (or at Phase 2 sweep time) |
| **ACL-write** (stamp `allowed_principals` on `repositories`) | **E7 #1672** | Post-indexing, consumes GitHub API |
| **Status callback** (ingestion → registry status update) | **E7 #1672** | Worker updates `knowledge_assets.status` on completion/failure |

### 4.2 Interface Contract (This EPIC → #1672)

**What #1672 reads from the registry:**
```sql
SELECT id, asset_type, source_ref, tenant_id, owner_sub, project_id, tags
FROM knowledge_assets
WHERE status = 'registered'
ORDER BY created_at ASC
LIMIT :batch_size;
```

**What #1672 writes back:**
```sql
UPDATE knowledge_assets SET status = 'queued', updated_at = NOW() WHERE id = :id;
-- After SQS enqueue succeeds

UPDATE knowledge_assets SET status = 'indexing', updated_at = NOW() WHERE id = :id;
-- On worker pickup

UPDATE knowledge_assets SET status = 'indexed', updated_at = NOW() WHERE id = :id;
-- On successful completion

UPDATE knowledge_assets SET status = 'failed', last_error = :err, retry_count = retry_count + 1, updated_at = NOW() WHERE id = :id;
-- On failure
```

### 4.3 Quota Enforcement (Owned by #1672, Checked by This EPIC at Registration)

**Soft-check at registration time** (this EPIC): the API rejects a registration if the scope's current count >= quota. This provides immediate user feedback.

```sql
-- At POST /api/agent-context/assets time:
SELECT COUNT(*) FROM knowledge_assets
WHERE tenant_id = :tid AND owner_sub = :sub AND status != 'removed';
-- If >= quota → 429 Too Many Requests
```

**Hard-check at trigger time** (#1672): the trigger engine re-validates before enqueueing (guards against race conditions with concurrent registrations).

**Default quotas (configurable via SSM/env):**
| Scope | Default Limit | Rationale |
|-------|--------------|-----------|
| Per-user (personal) | 20 repos, 50 URLs, 20 docs | Generous for individual use; bounded |
| Per-tenant | 200 repos, 500 URLs, 200 docs | Covers a mid-size engineering org |
| Shared (platform) | No limit (admin only) | Platform operator controls flat files |

---

## 5. Bulk-Upload Contract

### 5.1 File Format

**Plain text, one asset per line.** Supports the same extended format as `index_content/*.txt`:

```
# Lines starting with # are comments (ignored)
# Simple format: source_ref
https://github.com/org/repo1
https://github.com/org/repo2

# Extended format: source_ref | display_name | tag1:val1, tag2:val2
https://github.com/org/repo3 | Our Core Service | team:platform, priority:high
https://docs.aws.amazon.com/bedrock/ | Bedrock Docs | category:reference
s3://my-bucket/docs/design.pdf | Architecture Doc | category:design
```

**Asset type is inferred from `source_ref`:**
| Pattern | Inferred `asset_type` |
|---------|----------------------|
| `https://github.com/*` or `git@github.com:*` | `repo` |
| `s3://*` | `doc` |
| Any other `http(s)://` URL | `url` |

### 5.2 Endpoint (Two-Step: Preview + Commit)

**Step 1 — Preview (no DB writes):**
```
POST /api/agent-context/assets/bulk
Content-Type: multipart/form-data

Form fields:
  file: <the upload file>
  scope: "tenant" | "personal"   (default: "tenant" for admin, "personal" for user)
```

Response returns valid/rejected/duplicates/quota without writing. See §8.7 for full response shape.

**Step 2 — Commit (writes + dispatches):**
```
POST /api/agent-context/assets/bulk/commit
Content-Type: application/json

Body: { "items": [ ... validated items from preview ... ] }
```

Response returns created count + asset IDs. See §8.7 for full response shape.

### 5.3 Scope Assignment

- **Tenant admin** uploading → all rows get `tenant_id` from the admin's org membership, `owner_sub = NULL`.
- **Individual user** uploading → all rows get `owner_sub` from the user's Cognito sub, `tenant_id` from their org (for isolation), scope is personal.
- `registered_by` = the uploader's `cognito_sub` on every row.

### 5.4 Validation (v1 — at upload time)

| Check | Behavior |
|-------|----------|
| Line parsing | Invalid lines → `errors[]` in response, not created |
| URL format | Must be valid URL or S3 path |
| Duplicate detection | Existing (non-removed) row with same `source_ref` + scope → counted as `duplicates`, not created |
| Quota check | If (existing + new) > quota → reject entire upload with 429, listing the overage |
| File size limit | Max 1 MB / 500 lines per upload (configurable) |

### 5.5 Preview + Commit (v1 — Two-Step Pattern)

**Resolved (2026-06-23):** The two-step preview/commit pattern is **v1**, not a fast-follow. The preview endpoint (`POST .../assets/bulk`) parses + validates the file and returns what *would* be created — without writing to the DB. The commit endpoint (`POST .../assets/bulk/commit`) writes the validated items. See §8.3 + §8.7 for full API contracts.

---

## 6. Ingestion Read Path — Phased Trigger Model

### 6.1 Decision: Phased, Reversible (Operator Decision, 2026-06-24)

The trigger model is **phased** — Phase 1 ships with the simplest working approach; Phase 2 adds sophistication only when load warrants it. The registry row is the durable seam that makes the transition cheap.

#### Phase 1 (Now): API Publishes to SQS Inline

The gateway API publishes the SQS message **inline at registration time**. This is simpler to ship and gives immediate feedback.

**Sequence (the invariant that keeps Phase 2 cheap):**
```
1. INSERT knowledge_assets … status='registered'     ← durable row FIRST
2. Resolve steps via type→steps registry (§6.4)
3. Publish SQS message with scope envelope
4. UPDATE … status='queued'                          ← only after publish succeeds
```

A **failed SQS publish** leaves the row at `registered` — recoverable by a re-index action or by Phase 2's sweeper. The system never publishes without a durable `registered` row first (that would bake in API-as-only-publisher and make Phase 2 a refactor).

**The SQS message** carries the registry row's scope (per #1721's envelope):

```json
{
  "source": "org/repo-name",
  "content_type": "repo",
  "registry_asset_id": "uuid-of-the-knowledge-assets-row",
  "scope": {
    "tenant_id": "acme-corp",
    "owner_sub": null,
    "project_id": null,
    "visibility": "tenant"
  },
  "steps": ["s3_upload", "cgc", "deepwiki", "graphrag"],
  "triggered_by": "self_serve",
  "enqueued_at": "2026-06-24T12:00:00Z"
}
```

**The worker** (`sqs-worker.py`): processes the message, stamps scope on all artifacts per #1721, and on completion calls back to update `knowledge_assets.status` (→ `indexed` or `failed`).

**IAM implication:** The gateway API needs `sqs:SendMessage` permission on the ingestion queue in Phase 1. This is a deliberate phased choice (overrides the earlier "API shouldn't need SQS perms" reasoning), not a contradiction — the row-first invariant ensures Phase 2 is additive.

#### Phase 2 (Later, Load-Triggered): Sweeper-with-Nudge

When load justifies it, a **sweeper process** owned by #1672 takes over SQS publishing:
- Polls `status = 'registered'` rows
- Applies per-tenant fairness + backpressure (round-robin across tenants, rate-limited)
- Publishes to SQS
- The API stops publishing inline (removes SQS perms from gateway role)

Adding Phase 2 is **purely additive** because the `status` column is already the seam: the sweeper reads `registered` rows exactly like Phase 1's inline code does — same query, same SQS message format, same status transitions.

#### Migration Triggers for Phase 2

Move to Phase 2 when ANY of:
- SQS depth persistently high (>100 messages sustained)
- Workers pinned at the KEDA `maxReplicaCount` cap (50)
- One tenant's bulk upload starves others (fairness concern)
- `bulk/commit` endpoint becomes slow from synchronous fan-out (>5s response time at 200+ items)

### 6.2 Phase 1 Guardrails (Mandatory, Cheap)

Two guardrails prevent Phase 1's simpler model from causing resource exhaustion:

1. **KEDA `maxReplicaCount` cap** — already set to 50 in `manifests/ingestion-scaledjob.yaml`. This is the backstop: a burst of registrations deepens SQS instead of melting the cluster. Shared Bedrock/Zoekt/Neptune capacity is protected regardless of queue depth.

2. **Registration-time quota check** (§4.3) — limits how many rows can be created per scope per type. A tenant at their 200-repo quota cannot register more, so at most 200 SQS messages can be enqueued per tenant for repos (plus equivalent for urls/docs).

Together: quotas bound the input; `maxReplicaCount` bounds the throughput. Phase 1 is safe at current scale.

### 6.3 Why This Phasing (Rationale)

- **Phase 1 is simpler to ship** — no CronJob, no sweeper deployment, no separate IAM role for a trigger service. One less moving part.
- **The row-first invariant makes Phase 2 cheap** — the API's inline publish and Phase 2's sweeper are interchangeable readers of `status='registered'` rows. Swapping is a one-PR IAM + code change (remove `sqs:SendMessage` from gateway, deploy sweeper CronJob).
- **Consistency with existing pattern:** `publish-ingestion.py` already publishes to SQS inline after reading flat files. Phase 1 mirrors this — just from DB rows instead of flat files.
- **No lost messages:** A row left at `registered` (failed publish) is visible in the UI as "stuck at registered" and actionable via re-index. Phase 2's sweeper auto-recovers these.

### 6.4 Type→Steps Registry (Extensible Dispatch)

**Operator decision (2026-06-24):** Adding a new asset type must be a **config + handler change, not a schema migration + pipeline rewrite.**

The existing `STEPS_BY_TYPE` dict in `publish-ingestion.py` is the right shape — a simple type→steps mapping:

```python
# Current (hardcoded in publish-ingestion.py):
STEPS_BY_TYPE = {
    "repo": ["s3_upload", "cgc", "deepwiki", "graphrag"],
    "url": ["s3_upload", "graphrag"],
    "doc": ["s3_upload", "graphrag"],
    "infra": ["discovery", "graphrag"],
}
```

**Target (registry-driven, shared between API + worker):**

```python
# config.py or a dedicated type_registry.py:
ASSET_TYPE_REGISTRY = {
    "repo": {
        "steps": ["s3_upload", "cgc", "deepwiki", "graphrag"],
        "timeout": 900,
        "source_ref_pattern": r"^(https://github\.com/|git@github\.com:)",
        "requires_github_app": True,
    },
    "url": {
        "steps": ["s3_upload", "graphrag"],
        "timeout": 600,
        "source_ref_pattern": r"^https?://",
        "requires_github_app": False,
    },
    "doc": {
        "steps": ["s3_upload", "graphrag"],
        "timeout": 300,
        "source_ref_pattern": r"^s3://",
        "requires_github_app": False,
    },
    # Future: adding a new type is ONE entry here + its handler script
    # "confluence": {
    #     "steps": ["fetch_pages", "graphrag"],
    #     "timeout": 600,
    #     "source_ref_pattern": r"^https://.*\.atlassian\.net/wiki/",
    #     "requires_github_app": False,
    # },
}
```

**How this is consumed:**
1. **At registration** (API): validate `source_ref` against the type's `source_ref_pattern`. Reject unknown `asset_type` values (not in the registry).
2. **At SQS publish** (Phase 1 inline / Phase 2 sweeper): look up `steps` from the type registry, include in the SQS message.
3. **At worker dispatch** (`sqs-worker.py`): route to the handler based on `content_type` in the message. The worker's existing `if content_type == "repo"` dispatch is already effectively this — it just needs to support an `else: load_handler(content_type)` fallback for new types.

**Adding a new asset type requires:**
1. An entry in `ASSET_TYPE_REGISTRY` (steps, timeout, validation pattern)
2. A handler script (`ingest-<type>.py`) in `images/ingestion/`
3. **Zero schema migration** — `asset_type` is VARCHAR, `metadata` JSONB holds type-specific fields

**The stable contract every asset type must satisfy:**
- An owner/scope (`tenant_id`/`owner_sub`/`project_id`)
- A `source_ref` (how to reach the content)
- A `status` lifecycle (`registered → queued → indexing → indexed → failed → removed`)
- A set of ingestion steps producing indexed artifacts

### 6.5 Reconciliation with #1721 SQS Envelope

#1721 defines the target SQS envelope with a `scope` field. This EPIC's registry is the **source** of that scope data. The Phase 1 inline publish (or Phase 2 sweeper) reads `(tenant_id, owner_sub, project_id)` from the `knowledge_assets` row and populates the SQS `scope` field exactly as #1721 specifies.

---

## 7. Relationship to Flat Manifests (`repos.txt`/`urls.txt`/`docs.txt`)

### 7.1 Decision: Coexist (Not Replace)

**Flat files remain for the platform-curated shared corpus.** The registry is for self-serve/tenant/user registrations.

| Input Source | Scope | Owner | Mechanism | Registry Row? |
|--------------|-------|-------|-----------|---------------|
| `repos.txt` / `urls.txt` / `docs.txt` | Shared (global) | Platform operator | GitOps push → `publish-ingestion.py` → SQS | **No** (flat files are the source of truth for shared) |
| Gateway API (single add) | Personal or Tenant | User or Tenant Admin | API → registry row → trigger → SQS | **Yes** |
| Gateway API (bulk upload) | Tenant | Tenant Admin | API → registry rows → trigger → SQS | **Yes** |

### 7.2 Why Not Migrate Flat Files to the Registry?

1. **The shared corpus is operator-curated** — it changes infrequently, benefits from code review (PRs), and has no tenant scoping.
2. **The registry adds per-scope quotas, dedup, status tracking** — overhead not needed for a 14-repo eval corpus.
3. **Migration risk** — the CronJob + DynamoDB state-check flow for flat files is battle-tested. Migrating it introduces risk with no user benefit.

### 7.3 Future Option

If the platform later wants to unify all inputs into the registry (e.g., for a single "what's indexed" admin view), a **one-time backfill script** can read flat files and insert registry rows with `tenant_id = NULL, owner_sub = NULL` (shared scope). This is a non-breaking additive step.

---

## 8. API Contract (Gateway Backend)

### 8.1 Routing Architecture Decision

**Resolved question (from operator):** Where do the registry CRUD routes live — gateway `admin` router, or the agent-context `APIRouter` mounted by the gateway (the #1424 route-ownership pattern)?

**Decision: Agent-context `APIRouter` mounted by the gateway** (same as `indexing_router.py`).

Rationale:
- The registry is semantically part of the knowledge layer, not gateway admin CRUD.
- The #1424 pattern (`agent_context/api/indexing_router.py` → gateway mounts with DI override) is the established convention.
- The assets router needs a **different auth guard** than the admin router: it's user-facing (any authenticated user), not admin-only. The gateway mounts it with `Depends(get_current_user)` (not `require_admin`).

**Implementation pattern:**
```python
# modules/agent-context/agent_context/api/assets_router.py
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/agent-context/assets", tags=["knowledge-assets"])

async def get_assets_db() -> AsyncSession:
    """Placeholder — gateway overrides with its session factory."""
    raise HTTPException(status_code=503, detail="DB not configured")

# --- in modules/gateway/src/app.py (under AGENT_CONTEXT_ENABLED gate) ---
from agent_context.api.assets_router import router as assets_router, get_assets_db
app.dependency_overrides[get_assets_db] = get_db
app.include_router(assets_router, dependencies=[Depends(get_current_user)])
```

### 8.2 Routes — Asset Registry CRUD

All routes gated behind `AGENT_CONTEXT_ENABLED=true`. Auth: Cognito JWT via `get_current_user` (returns `TokenContext`). Scope derived from the authenticated session, never from request body values.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/api/agent-context/assets` | Any authenticated user | Register one asset |
| `GET` | `/api/agent-context/assets` | Any user (scoped) | List/filter assets for caller's scope |
| `GET` | `/api/agent-context/assets/{id}` | Any user (scoped) | Asset detail + index coverage |
| `DELETE` | `/api/agent-context/assets/{id}` | Owner or tenant admin | Soft-delete (`status = 'removed'`) |
| `POST` | `/api/agent-context/assets/{id}/reindex` | Owner or tenant admin | Re-queue: set `status = 'registered'` |

### 8.3 Routes — Bulk Upload (Two-Step: Preview + Commit)

**Resolved question (from operator):** The two-step preview/commit pattern is the recommended safe flow for large uploads and is **v1** (not fast-follow). This prevents a tenant admin from accidentally committing 500 rows.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/api/agent-context/assets/bulk` | Tenant admin | Parse + validate → return preview (no DB writes) |
| `POST` | `/api/agent-context/assets/bulk/commit` | Tenant admin | Commit previewed batch → rows + enqueue |

### 8.4 Routes — Repo Picker (Add-Asset Helper)

Powers the "pick a repo" UI (like NotebookLM's Drive picker). Uses the caller's tenant GitHub App installation to list accessible repos.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/agent-context/github/accessible-repos` | Any authenticated user | List repos the tenant's GitHub App can access |

### 8.5 Routes — Status / Coverage

Powers the per-asset index-status chips in the UI. Reuses the existing `index_runs`/`index_run_stages` data.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/agent-context/assets/{id}/status` | Any user (scoped) | Per-stage status for the asset's latest run |

### 8.6 Routes — Project Membership (Ties to #1728)

Powers the left-rail "include/exclude from project" toggle. Writes to the M:N `project_repositories` join table defined in the #1728 design.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/api/agent-context/projects/{pid}/assets` | Project owner | Add asset to project |
| `DELETE` | `/api/agent-context/projects/{pid}/assets/{asset_id}` | Project owner | Remove asset from project |

**Note:** These endpoints are implemented by the **#1728 project-scoping** EPIC, not this one. Listed here for completeness — this EPIC's UI will call them once #1728 ships.

### 8.7 Request/Response Contracts

**POST /api/agent-context/assets** (Register one asset)
```json
// Request
{
  "asset_type": "repo",
  "source_ref": "https://github.com/acme/my-service",
  "display_name": "My Service",
  "tags": {"team": "platform"},
  "metadata": {"default_branch": "main"},
  "scope": "personal"
}

// Response (201 Created)
{
  "id": "a1b2c3d4-...",
  "asset_type": "repo",
  "source_ref": "https://github.com/acme/my-service",
  "display_name": "My Service",
  "tags": {"team": "platform"},
  "metadata": {"default_branch": "main"},
  "tenant_id": "acme-corp",
  "owner_sub": "us-east-1:abc-123-def",
  "project_id": null,
  "status": "registered",
  "registered_by": "us-east-1:abc-123-def",
  "created_at": "2026-06-24T12:00:00Z"
}

// Error: 409 Conflict (duplicate source_ref in same scope)
{"detail": "Asset already registered under this scope", "existing_id": "..."}

// Error: 429 Too Many Requests (quota exceeded)
{"detail": "Quota exceeded", "quota": {"repos": {"used": 20, "limit": 20}}}
```

**GET /api/agent-context/assets** (List/filter)
```json
// Query params: ?scope=personal|tenant|shared&asset_type=repo&status=indexed
//               &project_id=<uuid>&page=1&page_size=20

// Response (200 OK)
{
  "items": [
    {
      "id": "a1b2c3d4-...",
      "asset_type": "repo",
      "source_ref": "https://github.com/acme/my-service",
      "display_name": "My Service",
      "tags": {"team": "platform"},
      "tenant_id": "acme-corp",
      "owner_sub": "us-east-1:abc-123-def",
      "project_id": null,
      "status": "indexed",
      "created_at": "2026-06-23T12:00:00Z",
      "index_summary": {
        "last_run_at": "2026-06-23T14:00:00Z",
        "stages_complete": 5,
        "stages_total": 6,
        "stages_failed": 1
      }
    }
  ],
  "total": 42,
  "page": 1,
  "page_size": 20,
  "has_more": true,
  "quota": {
    "repos": {"used": 15, "limit": 20},
    "urls": {"used": 3, "limit": 50},
    "docs": {"used": 0, "limit": 20}
  }
}
```

**GET /api/agent-context/assets/{id}** (Asset detail + index coverage)
```json
// Response (200 OK)
{
  "id": "a1b2c3d4-...",
  "asset_type": "repo",
  "source_ref": "https://github.com/acme/my-service",
  "display_name": "My Service",
  "tags": {"team": "platform"},
  "tenant_id": "acme-corp",
  "owner_sub": "us-east-1:abc-123-def",
  "project_id": null,
  "status": "indexed",
  "last_error": null,
  "retry_count": 0,
  "registered_by": "us-east-1:abc-123-def",
  "created_at": "2026-06-23T12:00:00Z",
  "updated_at": "2026-06-23T14:00:00Z",
  "index_coverage": {
    "last_run_id": "run-uuid",
    "last_run_at": "2026-06-23T14:00:00Z",
    "stages": [
      {"stage": "clone", "status": "verified", "completed_at": "..."},
      {"stage": "cgc_structural", "status": "verified", "completed_at": "..."},
      {"stage": "embed_vectors", "status": "verified", "completed_at": "..."},
      {"stage": "sbom_source", "status": "verified", "completed_at": "..."},
      {"stage": "deepwiki", "status": "failed", "error": "timeout after 300s"},
      {"stage": "zoekt_index", "status": "verified", "completed_at": "..."}
    ]
  },
  "projects": ["project-uuid-1", "project-uuid-2"],
  "summary": "Generated wiki summary from DeepWiki (if available)"
}
```

**POST /api/agent-context/assets/bulk** (Preview — no DB writes)
```json
// Request: multipart/form-data
//   file: <uploaded .txt or .csv>
//   scope: "tenant"  (or "personal" for non-admin)

// Response (200 OK — preview only, nothing committed)
{
  "total_lines": 150,
  "parsed": 136,
  "skipped_comments": 12,
  "valid": [
    {"line": 1, "source_ref": "https://github.com/acme/repo1", "asset_type": "repo", "display_name": null},
    {"line": 3, "source_ref": "https://docs.aws.amazon.com/bedrock/", "asset_type": "url", "display_name": "Bedrock Docs"}
  ],
  "rejected": [
    {"line": 42, "source_ref": "not-a-url", "reason": "Cannot infer asset_type from source_ref"},
    {"line": 99, "source_ref": "ftp://old-server/file", "reason": "Unsupported protocol"}
  ],
  "duplicates": [
    {"line": 5, "source_ref": "https://github.com/acme/repo2", "existing_id": "uuid"}
  ],
  "quota_ok": true,
  "quota_after": {"repos": {"used": 45, "limit": 200}, "urls": {"used": 12, "limit": 500}}
}

// Error: 413 Payload Too Large (file > 1 MB or > 500 lines)
// Error: 429 Too Many Requests (valid + existing > quota — preview shows the overage)
```

**POST /api/agent-context/assets/bulk/commit** (Commit previewed batch)
```json
// Request
{
  "items": [
    {"source_ref": "https://github.com/acme/repo1", "asset_type": "repo", "display_name": null, "tags": {}},
    {"source_ref": "https://docs.aws.amazon.com/bedrock/", "asset_type": "url", "display_name": "Bedrock Docs", "tags": {"category": "reference"}}
  ]
}

// Response (201 Created)
{
  "created": 120,
  "skipped_duplicates": 16,
  "assets": [{"id": "uuid", "source_ref": "...", "status": "registered"}, ...]
}
```

**GET /api/agent-context/github/accessible-repos** (Repo picker)
```json
// Query params: ?q=<search>&page=1&page_size=50

// Response (200 OK)
{
  "repos": [
    {"full_name": "acme/my-service", "private": true, "url": "https://github.com/acme/my-service"},
    {"full_name": "acme/docs", "private": false, "url": "https://github.com/acme/docs"}
  ],
  "total": 87,
  "page": 1,
  "has_more": true
}
```

### 8.8 Authorization Rules

| Action | Who Can | How Verified (from `TokenContext`) |
|--------|---------|-----|
| Register (personal scope) | Any authenticated user | `current_user.user_id` → `owner_sub`; `current_user.org_id` → `tenant_id` |
| Register (tenant scope) | Tenant admin (`is_admin=true`) | `current_user.org_id` → `tenant_id`; `owner_sub = NULL` |
| List personal assets | Any user | Filter: `owner_sub = current_user.user_id` |
| List tenant assets | Any user in that tenant | Filter: `tenant_id = current_user.org_id` (includes personal + tenant) |
| List shared assets | Any user | Filter: `tenant_id IS NULL` |
| Delete own personal asset | The `registered_by` user | Match `registered_by = current_user.user_id` |
| Delete any tenant asset | Tenant admin | Match `tenant_id = current_user.org_id` AND `current_user.is_admin` |
| Bulk upload (preview + commit) | Tenant admin only | `current_user.is_admin = true` |
| Repo picker | Any authenticated user | Uses tenant's GitHub App installation (`adp/<env>/tenants/<org_id>/github-app`) |
| Re-index | Owner or admin | Same rules as delete |

### 8.9 Ingestion Dispatch (Phased Trigger Model — Resolved 2026-06-24)

**Operator decision:** Phase 1 — the API publishes to SQS inline. The write→publish→update invariant ensures Phase 2 (sweeper) is additive. See §6.1 for the full phased model.

**Phase 1 Pattern (API publishes SQS inline):**
```python
from agent_context.ingestion.type_registry import ASSET_TYPE_REGISTRY

async def dispatch_ingestion(asset: KnowledgeAsset, db: AsyncSession):
    """Phase 1: inline SQS publish. Row MUST exist at status='registered' before calling."""
    type_config = ASSET_TYPE_REGISTRY[asset.asset_type]

    message = {
        "source": extract_source_identifier(asset.source_ref, asset.asset_type),
        "content_type": asset.asset_type,
        "registry_asset_id": str(asset.id),
        "scope": {
            "tenant_id": asset.tenant_id,
            "owner_sub": asset.owner_sub,
            "project_id": str(asset.project_id) if asset.project_id else None,
            "visibility": "personal" if asset.owner_sub else ("tenant" if asset.tenant_id else "shared"),
        },
        "steps": type_config["steps"],
        "triggered_by": "self_serve",
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }

    # Publish to SQS — if this fails, row stays at 'registered' (recoverable)
    sqs_client.send_message(
        QueueUrl=settings.INGESTION_QUEUE_URL,
        MessageBody=json.dumps(message),
        MessageAttributes={"content_type": {"DataType": "String", "StringValue": asset.asset_type}},
    )

    # Only update status AFTER successful publish
    asset.status = "queued"
    asset.updated_at = datetime.now(timezone.utc)
    await db.commit()
```

**In POST /api/agent-context/assets (single registration):**
```python
asset = await create_registry_row(...)       # status='registered' — durable row FIRST
await dispatch_ingestion(asset, db)          # SQS publish + status='queued'
return asset                                 # 201 Created (status may be 'registered' if publish failed)
```

**In POST /api/agent-context/assets/bulk/commit:**
```python
assets = await bulk_create_rows(items)       # N rows at status='registered'
for asset in assets:
    try:
        await dispatch_ingestion(asset, db)  # Best-effort inline publish
    except SQSPublishError:
        pass  # Row stays at 'registered' — Phase 2 sweeper or re-index action recovers
return BulkCommitResponse(created=len(assets), ...)
```

**Phase 2 transition:** When Phase 2 deploys (sweeper CronJob), the inline `dispatch_ingestion()` calls are removed from the API handlers. The sweeper reads `status='registered'` rows and publishes — same message format, same status transitions. The API loses `sqs:SendMessage` permission. No other changes needed.

**IAM requirement (Phase 1):** Gateway task role needs `sqs:SendMessage` on `arn:aws:sqs:<region>:<account>:adp-<env>-ingestion-queue`. Add via the gateway-infra Terraform module's IAM policy.

---

## 9. Frontend (Management UI) — NotebookLM-Inspired

### 9.1 Design Philosophy

**North star:** NotebookLM's interaction model, adapted for multi-tenant, ACL-scoped, repo-indexing context.

| NotebookLM Concept | ADP Equivalent |
|-------------------|----------------|
| Notebook | Project (#1728) |
| Source | Knowledge Asset (this EPIC) |
| Source type (Drive doc, URL, paste) | Asset type (repo, URL, doc) |
| Drive picker | Repo picker (tenant GitHub App) |
| Source status/summary | Index-status chips + DeepWiki summary |
| Source checkbox (include in notebook) | Project include/exclude toggle (M:N) |

### 9.2 Location Decision

**Resolved question (from operator):** Does the UI extend `IndexingStatus.tsx` or is it a new page?

**Decision: New page (`/knowledge`).** `IndexingStatus.tsx` is:
- Admin-only (gated behind `isPlatformAdmin()` in `Navigation.tsx`)
- Read-only (shows run-level status, no CRUD)
- Run-centric (grouped by `index_runs`), not asset-centric

The Knowledge Assets page is user-facing (all authenticated users), write-capable (add/remove/bulk-upload), and asset-centric. It is a fundamentally different surface.

The existing `IndexingStatus.tsx` remains as the platform-admin's "indexing pipeline health" view. The new page becomes the user's "what's in my Knowledge Layer" view.

### 9.3 Three-Zone Layout

```
+---------------------------+----------------------------------+-------------------+
|     LEFT RAIL             |         CENTER                   |  RIGHT (optional) |
|     Asset List            |    Asset Detail / Guide          |  Project Context  |
+---------------------------+----------------------------------+-------------------+
| [+ Add Asset]  [Bulk]    |                                  |                   |
| Scope: [Shared|Tenant|Me]|  <selected asset detail>         | Active Project:   |
|                           |                                  |  "client-A"       |
| search/filter             |  Index Coverage                  |  Assets: 12       |
|                           |    clone: ok                     |                   |
| repo  acme/svc    indexed |    structural: ok                | [Query project]   |
| url   docs.aws... indexed |    vectors: ok                   |                   |
| doc   design.pdf  failed  |    sbom: ok                      |                   |
| repo  acme/lib    queued  |    wiki: FAILED (timeout)        |                   |
|                           |    zoekt: ok                     |                   |
| ...                       |                                  |                   |
|                           |  Summary (DeepWiki)              |                   |
|                           |    "This repo contains..."       |                   |
|                           |                                  |                   |
|                           |  Scope: Personal                 |                   |
|                           |  Projects: client-A, internal    |                   |
|                           |  Registered: 2026-06-22          |                   |
|                           |  [Re-index] [Remove]             |                   |
+---------------------------+----------------------------------+-------------------+
```

### 9.4 Zone 1: Left Rail — Asset List (Core)

The primary surface. One row per registered asset, grouped/filterable.

**Top controls:**
- **"Add Asset" button** — opens `AddAssetDialog` (type picker: repo/link/doc)
- **"Bulk Upload" button** (tenant admins only) — opens `BulkUploadDialog`
- **Scope filter** — tabs or dropdown: `Shared` / `My Tenant` / `Personal` (default: Personal)
- **Asset type filter** — checkboxes: repo, url, doc
- **Status filter** — dropdown: all / indexed / failed / queued / registered
- **Search** — filters by `display_name` or `source_ref` substring

**Per-row display:**
- Type icon (repo: git-branch, url: globe, doc: file-text)
- Asset name (`display_name` or last path segment of `source_ref`)
- Index-status chip: **per-stage matrix** showing which tools ran (clone, structural, wiki, sbom, etc.) as mini colored dots or abbreviated labels — borrowed from IndexingStatus's `statusColor()` pattern
- Last-run timestamp (relative: "2h ago")
- Project toggle (checkbox/switch): include/exclude this asset from the active project (soft M:N per #1728). **Disabled until #1728 ships.**
- Row actions (overflow menu): Re-index, Remove

**Asset count + scope label** always visible at the top of the rail.

### 9.5 Zone 2: Center — Asset Detail / "Asset Guide"

Selecting an asset in the left rail reveals its full detail:

1. **Index coverage panel** — per-stage status chips (the 6 canonical stages from `IndexingStatus.tsx`: clone, cgc_structural, embed_vectors, sbom_source, deepwiki, zoekt_index). Uses the same `statusColor()` pattern. Failed stages show expandable error messages.

2. **Generated summary** — if DeepWiki output exists for this asset (repos only), display the generated wiki summary. This is the "source guide" equivalent from NotebookLM. For URLs/docs, show a snippet/title if available.

3. **Metadata panel:**
   - Scope: Personal / Tenant (org name)
   - Projects: list of projects this asset belongs to (links to #1728 project view)
   - Registered by: user email/name
   - Created / Last indexed timestamps
   - Tags (editable)

4. **Actions:** Re-index button, Remove button, Edit tags

### 9.6 Zone 3: Right — Project Context (Optional, #1728 Dependent)

Shows the currently-active project as a curated set of assets:
- Project name + asset count
- "Query within this project" affordance → scopes Door verbs to the project's assets
- Quick list of assets in the project

**This zone is stubbed/hidden until #1728 ships.** The frontend builds the layout with a collapsible right panel; it activates once the project API exists.

### 9.7 Add-Asset Flow (NotebookLM "Add Source" equivalent)

**Type picker dialog** (3 tabs or radio group):
1. **Repo** — shows the "accessible repos" picker (fetched from `GET /api/agent-context/github/accessible-repos`). Search + select. Like NotebookLM's Drive picker.
2. **URL / Link** — text input for a URL. Validates format on blur.
3. **Document** — file upload to S3 (or S3 path input). For v1: S3 path only (the worker already handles S3 docs). Upload → S3 → registry row is v1.1.

**Scope selector:** "Add to: My personal library / Tenant library" (tenant option visible only to admins).

**On submit:** `POST /api/agent-context/assets` → toast success → asset appears in left rail with `registered` status.

### 9.8 Bulk Upload Flow (Two-Step: Preview + Commit)

1. **File drop zone** — accepts `.txt` or `.csv`, max 1 MB / 500 lines.
2. **Preview step** — calls `POST /api/agent-context/assets/bulk`. Displays:
   - Valid items (green): ready to commit
   - Rejected items (red): with per-line error reason
   - Duplicates (yellow): already registered under this scope
   - Quota summary: "After commit: 45/200 repos used"
3. **Commit button** — calls `POST /api/agent-context/assets/bulk/commit` with the valid items. Shows progress (for large batches, a progress bar). On success: toast + refresh left rail.
4. **Cancel** — discards preview, no DB writes occurred.

### 9.9 Component Architecture

| File | Component | Purpose |
|------|-----------|---------|
| `pages/KnowledgeAssets.tsx` | `KnowledgeAssets` | Page container: three-zone layout, data fetching, selection state |
| `components/knowledge/AssetList.tsx` | `AssetList` | Left rail: filterable list, scope tabs, search |
| `components/knowledge/AssetRow.tsx` | `AssetRow` | Single row: icon, name, status chips, project toggle, actions |
| `components/knowledge/AssetDetail.tsx` | `AssetDetail` | Center zone: coverage, summary, metadata, actions |
| `components/knowledge/AssetStatusChips.tsx` | `AssetStatusChips` | Per-stage status dots/labels (reuses `statusColor()` pattern) |
| `components/knowledge/AddAssetDialog.tsx` | `AddAssetDialog` | Modal: type tabs (repo picker / URL input / doc upload), scope selector |
| `components/knowledge/RepoPicker.tsx` | `RepoPicker` | Searchable list of accessible repos from GitHub App |
| `components/knowledge/BulkUploadDialog.tsx` | `BulkUploadDialog` | Modal: file drop, preview table, commit/cancel |
| `components/knowledge/ProjectContext.tsx` | `ProjectContext` | Right zone: project summary + query affordance (stubbed until #1728) |
| `services/knowledge.ts` | service module | API calls: `getAssets()`, `createAsset()`, `deleteAsset()`, `bulkPreview()`, `bulkCommit()`, `getAccessibleRepos()`, `getAssetStatus()` |

### 9.10 Routing + Navigation

**Route:** `/knowledge` (in `App.tsx`, lazy-loaded)
```tsx
const KnowledgeAssets = lazy(() => import('./pages/KnowledgeAssets'));
// Inside protected routes:
<Route path="/knowledge" element={<KnowledgeAssets />} />
```

**Navigation entry** (in `Navigation.tsx`): visible to ALL authenticated users (not admin-gated):
```typescript
// After "Credentials" entry, before admin-only section
navItems.push({ to: '/knowledge', label: 'Knowledge', icon: '📚' });
```

### 9.11 Responsive Behavior

- **Desktop (>1024px):** Full three-zone layout
- **Tablet (768-1024px):** Left rail + center; project context collapses to a drawer
- **Mobile (<768px):** Single-zone; list view → detail on select (back button returns to list)

---

## 10. Access Verification at Registration

### 10.1 The Problem

A user registers `https://github.com/org/private-repo` — but does the platform's GitHub App have access to clone it? If not, ingestion will fail.

### 10.2 Decision: Deferred Verification (v1)

**v1:** Accept the registration, attempt verification asynchronously at trigger time (#1672). If access fails, set `status = 'failed'` with `last_error = 'GitHub App cannot access this repository'`.

**Rationale:**
- Synchronous verification at registration time requires a GitHub API call per asset (slow for bulk upload).
- The per-tenant GitHub App credentials are stored in Secrets Manager (`adp/<env>/tenants/<tenant>/github-app`) — the gateway API doesn't currently resolve them.
- The trigger service (#1672) already has GitHub App access for token minting.

**v1.1 (fast-follow):** Add an optional `?verify=true` query param on single-asset registration that performs a synchronous access check. Bulk upload always defers.

---

## 11. Security Considerations

### 11.1 Cross-Tenant Exposure (Critical)

**Risk:** A registry row gets `tenant_id = 'wrong-tenant'` → asset indexed under the wrong scope → visible to wrong users after #1721 enforces isolation.

**Mitigation:** Scope is derived from the authenticated session, NEVER from user input:
```python
# In the API handler:
asset.tenant_id = current_user.org_id    # From JWT, not request body
asset.owner_sub = current_user.cognito_sub if scope == "personal" else None
```

The `scope` field in the request body is an enum (`"personal"` | `"tenant"`) that controls whether `owner_sub` is populated — it does NOT contain the actual tenant_id/owner_sub values.

### 11.2 SSRF via Source Ref

**Risk:** A user registers `http://169.254.169.254/latest/meta-data/` as a URL asset → ingestion worker fetches instance metadata.

**Mitigation:** (Already exists in `ingest-url.py` — URL validation + blocklist for internal IPs/metadata endpoints.) The registry itself doesn't fetch; ingestion does. Ensure the existing protection covers this path.

### 11.3 Quota Exhaustion

**Risk:** A user registers 10,000 repos → unbounded compute.

**Mitigation:** Per-scope quotas (§4.3) enforced at registration time + trigger time.

---

## 12. Migration and Deployment

### 12.1 Migration Sequencing

```
#1721 migration 004 (tenant_id/owner_sub on repositories)
    ↓ (must land first — establishes the scope columns pattern)
#1736 migration 005 (knowledge_assets table)   ← THIS EPIC
    ↓
#1728 (projects table)  — references knowledge_assets.project_id
```

**Why #1721 first:** the isolation columns on `repositories` establish the scope pattern that this EPIC mirrors. The registry's `tenant_id`/`owner_sub` semantics MUST match #1721's (same VARCHAR lengths, same NULL semantics, same index strategy).

### 12.2 Deploy Sequence

1. **Migration** — Alembic migration runs via `run-gateway-migrations.yml` (gateway-infra CI)
2. **IAM update** — Add `sqs:SendMessage` on the ingestion queue to the gateway task role (Terraform in `modules/gateway/infra/`). Required for Phase 1 inline dispatch.
3. **Backend API** — New router in `agent_context/api/assets_router.py` + `type_registry.py`, mounted by gateway (same pattern as `indexing_router.py`). Includes Phase 1 inline SQS dispatch.
4. **Frontend** — New page built by `gateway-deploy.yml` frontend job
5. **Status callback** (#1672) — Worker updates `knowledge_assets.status` on completion/failure
6. **Phase 2 (future, load-triggered)** — Sweeper CronJob replaces inline dispatch; gateway loses `sqs:SendMessage` permission

### 12.3 Rollback

- **Migration rollback:** `DROP TABLE knowledge_assets;` — no FK dependencies from other tables.
- **API rollback:** Remove the router mount; existing IndexingStatus page unaffected.
- **Data rollback:** Registry rows are independent of `repositories`; dropping the table loses registrations but doesn't affect already-indexed content.

---

## 13. Child Issue Decomposition

### Issue A: Registry Schema Migration
**Scope:** Create `knowledge_assets` table (migration 005 in `modules/agent-context/alembic/versions/`)
**Depends on:** #1721 child issue A (migration 004 — tenant_id/owner_sub on repositories)
**Deliverables:** Alembic migration file (up + down), test verifying table creation + unique index
**Module:** `modules/agent-context/`
**Estimate:** S (half-day)

### Issue B: Registry CRUD API
**Scope:** FastAPI router (`assets_router.py`) with POST/GET/GET-detail/DELETE/reindex endpoints. Includes Pydantic request/response schemas (per §8.7), scope derivation from `TokenContext`, soft-check quota enforcement, and gateway mount.
**Depends on:** Issue A (table exists)
**Deliverables:** `agent_context/api/assets_router.py`, `agent_context/api/assets_schemas.py`, gateway mount in `app.py`, unit tests
**Module:** `modules/agent-context/` (router defined here), `modules/gateway/` (mount point)
**Estimate:** M (1-2 days)

### Issue C: Bulk Upload Endpoint (Two-Step Preview + Commit)
**Scope:** `POST /api/agent-context/assets/bulk` (preview: parse + validate, return preview response without DB writes) + `POST /api/agent-context/assets/bulk/commit` (commit valid items → rows + dispatch). File format parsing (§5.1), validation, duplicate detection, quota-check.
**Depends on:** Issue B (CRUD API + schemas exist)
**Deliverables:** Two new endpoints in `assets_router.py`, file parser utility, validation logic, tests (valid file, invalid lines, quota exceeded, duplicates)
**Module:** `modules/agent-context/`
**Estimate:** M (1-2 days)

### Issue D: Repo Picker API
**Scope:** `GET /api/agent-context/github/accessible-repos` — fetch repos accessible to the caller's tenant GitHub App install. Resolves App credentials from Secrets Manager (`adp/<env>/tenants/<org_id>/github-app`), lists installations repos, returns paginated response.
**Depends on:** Issue B (router exists), per-tenant GitHub App provisioning
**Deliverables:** Endpoint, GitHub App token minting utility (or reuse from #1672), tests
**Module:** `modules/agent-context/`
**Estimate:** M (1-2 days)

### Issue E: Management UI — Three-Zone Layout + Asset List + Add
**Scope:** New `/knowledge` page with three-zone NotebookLM-inspired layout (§9.3). Left rail: `AssetList` + `AssetRow` components with scope tabs, type/status filters, search. Center: `AssetDetail` with index coverage chips + metadata. Add-asset dialog with type picker (repo picker / URL input / doc path). Right zone: stubbed `ProjectContext` (activates with #1728). Service layer (`services/knowledge.ts`). Navigation entry. Route registration.
**Depends on:** Issue B (CRUD API exists), Issue D (repo picker for the add-repo flow)
**Deliverables:** All components in §9.9, route + nav registration, `services/knowledge.ts`, basic Playwright smoke test
**Module:** `modules/gateway/` (frontend)
**Estimate:** L (2-3 days)

### Issue F: Management UI — Bulk Upload Dialog
**Scope:** `BulkUploadDialog` component: file dropzone, calls preview endpoint, renders preview table (valid/rejected/duplicates/quota), commit button, error handling, progress display.
**Depends on:** Issue C (bulk endpoints exist), Issue E (page + dialog patterns exist)
**Deliverables:** `BulkUploadDialog.tsx` component, integration with `services/knowledge.ts`, tests
**Module:** `modules/gateway/` (frontend)
**Estimate:** M (1-2 days)

### Issue G: Asset Index Status Chips (Join to index_runs)
**Scope:** `GET /api/agent-context/assets/{id}/status` endpoint (joins `knowledge_assets.source_ref` to `repositories.git_url` → `index_run_stages`). Frontend `AssetStatusChips` component reusing `statusColor()` pattern from `IndexingStatus.tsx`. Inline per-stage display in asset list row + expanded coverage in detail view.
**Depends on:** Issue B (assets API), Issue E (UI shell)
**Deliverables:** Status endpoint, `AssetStatusChips.tsx`, integration
**Module:** `modules/agent-context/` (endpoint), `modules/gateway/` (frontend component)
**Estimate:** M (1-2 days)

### Issue H: Inline SQS Dispatch (Phase 1 — This EPIC) + Type Registry
**Scope:** Implement the Phase 1 inline SQS publish (§8.9): `dispatch_ingestion()` function that reads type→steps from `ASSET_TYPE_REGISTRY` (§6.4), builds the SQS message with scope envelope (per #1721), publishes inline, and updates `status='queued'`. Also: the `ASSET_TYPE_REGISTRY` config module (`type_registry.py`), validation of `asset_type` + `source_ref` patterns. IAM: add `sqs:SendMessage` to gateway task role in Terraform.
**Depends on:** Issue A (table exists), Issue B (router calls dispatch)
**Owner:** This EPIC (#1736)
**Note:** Phase 2 (sweeper-with-nudge, owned by #1672) replaces inline publish when load triggers justify it. The row-first invariant ensures Phase 2 is a pure additive swap.

### Issue I: Status Callback (Owned by #1672)
**Scope:** Worker updates `knowledge_assets.status` on ingestion completion/failure. On success: `status = 'indexed'`. On failure: `status = 'failed'`, `last_error = <message>`, `retry_count += 1`.
**Depends on:** Issue A (table exists), Issue H (dispatch works)
**Owner:** E7 #1672

### Issue J: Phase 2 Sweeper (Owned by #1672 — Future)
**Scope:** CronJob/sweeper that polls `status = 'registered'` rows, applies per-tenant fairness + backpressure, publishes to SQS, removes inline dispatch from the API. Migration trigger: SQS depth >100 sustained / KEDA at cap / tenant starvation.
**Depends on:** Issue H (Phase 1 working), load observation
**Owner:** E7 #1672
**Note:** Purely additive — same message format, same status transitions. API loses `sqs:SendMessage` permission when this ships.

---

### Dependency Graph

```
Issue A (migration)
  ├── Issue B (CRUD API)
  │     ├── Issue C (bulk upload endpoints)
  │     │     └── Issue F (bulk upload UI)
  │     ├── Issue D (repo picker API)
  │     │     └── Issue E (management UI — needs repo picker)
  │     ├── Issue E (management UI)
  │     │     └── Issue G (status chips)
  │     ├── Issue G (status endpoint)
  │     └── Issue H (Phase 1 inline dispatch + type registry)
  ├── Issue I (status callback — #1672, after H)
  ├── Issue J (Phase 2 sweeper — #1672, load-triggered, after H)
  └── (independent) #1728 project membership UI activates Zone 3
```

### Parallel Work Streams

Once Issue A (migration) lands, three streams can proceed in parallel:
1. **Backend stream:** B → C → D → H (API endpoints + inline dispatch)
2. **Frontend stream:** E → F → G (UI components, can mock API initially)
3. **#1672 stream:** I (status callback, needs H to be working) → J (Phase 2 sweeper, load-triggered)

---

## 14. Resolved + Open Questions

### Resolved (operator decisions, 2026-06-23 + 2026-06-24)

1. **UI: Extend IndexingStatus or new page?** → **New page (`/knowledge`).** IndexingStatus is admin-only, read-only, run-centric. The Knowledge Assets page is user-facing, write-capable, asset-centric. (§9.2)
2. **Route ownership: gateway admin router or agent-context APIRouter?** → **Agent-context `APIRouter` mounted by gateway** (the #1424 pattern). Consistent with `indexing_router.py`. Different auth guard: `get_current_user` not `require_admin`. (§8.1)
3. **Trigger model — PHASED, reversible (2026-06-24, supersedes earlier "stub until #1672" approach):** → **Phase 1: API publishes to SQS inline** (gateway gets `sqs:SendMessage`). Row written FIRST (`status='registered'`), then SQS publish, then `status='queued'`. Failed publish leaves row at `registered` (recoverable). **Phase 2 (later, load-triggered):** sweeper-with-nudge owned by #1672 reads `registered` rows, applies fairness/backpressure, publishes to SQS; API stops publishing inline. Guardrails: KEDA `maxReplicaCount: 50` cap + registration-time quotas. (§6.1-§6.3)
4. **Bulk upload: preview in v1 or fast-follow?** → **v1 (two-step preview + commit).** The operator specified this as the recommended safe pattern. (§8.3)
5. **UX pattern:** → **NotebookLM three-zone layout** (left rail = asset list, center = asset detail/guide, right = project context). (§9.1-§9.6)
6. **Extensible asset_type (2026-06-24):** → `asset_type` is **open VARCHAR(32)** — NO Postgres ENUM or CHECK constraint. Type-specific fields live in a **`metadata JSONB` column**, not new columns. New types require config + handler changes only: one entry in `ASSET_TYPE_REGISTRY` + one handler script. Zero schema migrations. The stable contract: owner/scope, source_ref, status lifecycle, set of ingestion steps. (§3.1, §6.4)
7. **Type→steps dispatch is registry-driven (2026-06-24):** → Ingestion steps resolved per type via `ASSET_TYPE_REGISTRY` config (not hardcoded `if asset_type=='repo'` branches). New type = register handler + step list; trigger/worker dispatch generically. (§6.4)

### Still Open (For Operator Decision)

1. **Quota values** — The defaults in §4.3 (20 repos / 50 URLs / 20 docs per user; 200/500/200 per tenant) are reasonable guesses. Confirm or adjust.
2. **Bulk upload file size limit** — 1 MB / 500 lines proposed. Is this sufficient for the largest anticipated tenant upload?
3. **Asset removal behavior** — Soft-delete (`status = 'removed'`) preserves audit trail but doesn't trigger un-indexing of already-indexed content. Should removal also schedule a cleanup job? (Recommend: v1 soft-delete only; v2 adds cleanup/un-index.)
4. **Document upload (asset_type=doc):** v1 accepts S3 paths only (the worker already handles S3 docs). Should v1 also support direct file upload (user uploads a PDF → gateway stores to S3 → registry row)? Or defer to v1.1?
5. **Shared corpus visibility in the UI:** Should normal users see the platform-curated shared corpus (from flat files) in the asset list? If yes, these would appear as read-only rows (no remove/re-index actions). If no, the `/knowledge` page shows only self-serve registrations.

---

## 15. Reuse Table

| Capability | Lives In | How This EPIC Uses It |
|-----------|----------|----------------------|
| Scope stamping (tenant_id/owner_sub) | #1721 | Registry mirrors the same column semantics (VARCHAR types, NULL meaning) |
| SQS scope envelope | #1721 design §4-5 | Phase 1 inline dispatch populates from registry row; Phase 2 sweeper (#1672) does the same |
| SQS queue (`adp-<env>-ingestion-queue`) | `modules/agent-context/terraform/` | Phase 1: gateway publishes inline (needs `sqs:SendMessage` IAM) |
| `STEPS_BY_TYPE` / ingestion step dispatch | `images/ingestion/publish-ingestion.py` | Evolves into `ASSET_TYPE_REGISTRY` (§6.4); same shape, config-driven |
| KEDA ScaledJob (`maxReplicaCount: 50`) | `manifests/ingestion-scaledjob.yaml` | Phase 1 guardrail — caps throughput regardless of SQS depth |
| Project grouping (project_id) | #1728 `project_repositories` join table | Registry's nullable `project_id`; Zone 3 project toggle calls #1728's API |
| Phase 2 sweeper + ACL-write + hard quotas | #1672 | Consumes registry; this EPIC defines the interface (§4.2 + §8.9) |
| Per-tenant GitHub App | `adp/<env>/tenants/<tenant>/github-app` (Secrets Manager) | Repo picker API (§8.4) + access verification at trigger time (#1672) |
| `index_runs` / `index_run_stages` | Existing (migration 001/003) | Status chips (§9.4), asset detail coverage panel (§9.5) |
| Conditional router mount (`AGENT_CONTEXT_ENABLED`) | `indexing_router.py` → `app.py:205-228` | Assets router uses identical gate + DI override pattern (§8.1) |
| `TokenContext` (Cognito JWT → identity) | `src/shared/schemas/auth.py`, `src/auth/dependencies.py` | Derives `tenant_id` (from `org_id`), `owner_sub` (from `user_id`), `is_admin` for scope + auth |
| `statusColor()` pattern | `pages/admin/IndexingStatus.tsx` | `AssetStatusChips` component reuses same green/red/blue/yellow chip colors |
| Modal component | `components/ui/Modal.tsx` | `AddAssetDialog`, `BulkUploadDialog` use existing portal-rendered modal |
| Tailwind component library | `components/ui/` (Button, Card, Badge, Table, Tabs, Toast) | All new components use existing UI primitives |
| Lazy route loading | `App.tsx` pattern | `/knowledge` route lazy-loaded like all other pages |
| `NavItem` permission pattern | `Navigation.tsx` | Knowledge entry added to all-authenticated section (no admin gate) |
| `BudgetFormModal` CRUD pattern | `components/budget/BudgetFormModal.tsx` | AddAssetDialog follows same state management + validation pattern |
| Service layer pattern | `services/admin.ts` (snake→camelCase, pagination) | `services/knowledge.ts` follows same API call + transform pattern |

---

## 16. Alignment with Existing Codebase

### 16.1 Confirmed Alignments

- **DB location:** `agent_context` database on the shared RDS instance — same as `repositories`, `index_runs`. Matches `knowledge-layer-design.md` §4.
- **Router pattern:** Defined in `agent_context/api/`, mounted by gateway with dependency override for DB session. Matches `indexing_router.py` (issue #1424).
- **SQS message format:** Extends existing format from `publish-ingestion.py` (adds `registry_asset_id` + `scope` fields). Backward-compatible — existing flat-file messages still work without these fields.
- **Scope columns:** `tenant_id VARCHAR(256)`, `owner_sub VARCHAR(128)` — matches #1721's migration 004 spec exactly (same types, same NULL semantics).
- **Status values:** Distinct from `repositories` status (which uses `pending/verified/failed` per-stage) and `index_runs.status` (which uses `running/completed/failed`). The registry has its own lifecycle: `registered → queued → indexing → indexed → failed → removed`.

### 16.2 No Conflicts Found

- No existing `knowledge_assets` table or migration.
- No existing `/api/agent-context/assets` route.
- No naming collision with the existing `repositories` catalog.
- The `asset_type` values (`repo`, `url`, `doc`) match the `content_type` field used in `publish-ingestion.py` and `sqs-worker.py` — same string values, validated at the API layer (not via DDL constraint).
- The `metadata JSONB` column does not conflict with `repositories.metadata` (which doesn't exist; `repositories` uses per-stage status columns instead).
- The `ASSET_TYPE_REGISTRY` config pattern mirrors `STEPS_BY_TYPE` in `publish-ingestion.py` — same shape (dict of type→config), enriched with validation patterns and timeouts.

---

## 17. Non-Goals (Explicitly Out of Scope)

- **Ingestion engine implementation** — owned by #1672.
- **Store partitioning** (per-tenant S3 prefixes, per-user Zoekt shards) — owned by #1721.
- **Project membership UI** — owned by #1728.
- **Un-indexing / cleanup on removal** — v2; v1 is soft-delete only.
- **Access verification at registration time** — v1.1; v1 defers to trigger time.
- **Migrating flat files to the registry** — future option, not v1.
- **Webhook-triggered registration** (GitHub push → auto-register) — possible future enhancement.
