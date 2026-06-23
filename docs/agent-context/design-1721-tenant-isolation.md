# Design Note: Tenant & Individual Isolation for Knowledge Layer Indexing

**Issue:** #1721 (Child EPIC #1345)
**Status:** Design spike — produced by @agent-architect
**Date:** 2026-06-23
**Depends on:** #1319 (GitHub-sender to cognito_sub mapping), E7 #1672 (self-serve indexing UI/flow)
**Aligns with:** `docs/agent-context/knowledge-layer-design.md` section 8 (Permissions/ACL)

---

## 1. Problem Statement

The Knowledge Layer indexing pipeline has **no concept of a tenant**. Every indexed repo lands in one global, shared corpus. The Door enforces *identity* (fail-closed on a missing caller) but does **not** enforce *tenancy* (which tenant/user may see which indexed content).

The platform is multi-tenant at the gateway (`org_id` via `TenantMixin` threads through ~25 tables; 15 organizations / 12 tenants / 17 users on the live system), but the Knowledge Layer is single-tenant/global. As soon as a tenant indexes a **private** repo, the absence of isolation is a data-exposure risk.

**Current state of the 15 indexed repos:**
- 14 are public (eval corpus) with `allowed_principals = ["*"]`
- 1 is private (`aws-e/adp`) with collaborator/team ACLs
- No `org_id` or `tenant_id` column exists on `repositories`
- No tenant binding exists in the ingestion pipeline

---

## 2. Three Scoping Levels

| Level | Visibility | Identity Anchor | Use Case |
|-------|-----------|-----------------|----------|
| **Shared (global)** | All authorized callers | `allowed_principals = ["*"]` | Public OSS, eval corpus, curated reference repos |
| **Per-tenant (org)** | Only callers whose `X-Tenant-Id` matches the repo's `tenant_id` | `tenant_id` on `repositories` row | An org's private/internal repos |
| **Per-individual (user)** | Only the `owner_sub` who indexed it | `owner_sub` on `repositories` row | Personal/private index (EPIC #1287) |

**Scoping hierarchy (resolution order at query time):**
1. If `allowed_principals = ["*"]` AND `tenant_id IS NULL` → **shared** (all authorized callers see it)
2. If `tenant_id IS NOT NULL` AND `owner_sub IS NULL` → **per-tenant** (only callers with matching `X-Tenant-Id`)
3. If `owner_sub IS NOT NULL` → **per-individual** (only the caller whose `X-Owner-Sub` matches)

A repo may transition from shared to per-tenant (e.g., a public repo goes private), but never from per-individual to shared without explicit re-indexing.

---

## 3. Soft vs Hard Isolation Decision

### 3.1 Decision: Hybrid Model

**Confirmed:** soft isolation for the shared corpus + **hard isolation for private per-tenant and per-user data**.

Justification: a single filter-bug on a shared store exposes all private data across tenants. The cost of hard isolation is acceptable (per-tenant S3 prefixes are free; per-tenant/user S3 Vectors indexes are pay-per-use and bounded). The shared corpus remains soft-isolated because its `allowed_principals = ["*"]` means there is nothing to expose.

### 3.2 Per-Store Isolation Model

| Store | Shared Corpus | Per-Tenant (org) | Per-Individual (user) | Rationale |
|-------|--------------|-------------------|----------------------|-----------|
| **Postgres** (`repositories`, `dependencies`) | Existing rows with `tenant_id = NULL` | Add `tenant_id` column, filter in query | Add `owner_sub` column, filter in query | Relational; filter is cheap with indexed columns. Physical separation adds no value for row-level data. |
| **S3** (zoekt-shards, code-indexes, wikis, SBOMs) | Existing prefixes: `zoekt-shards/`, `code-indexes/`, etc. | **Hard:** `tenants/{tenant_id}/zoekt-shards/`, `tenants/{tenant_id}/code-indexes/`, etc. | **Hard:** `users/{owner_sub}/zoekt-shards/`, etc. | Path-based isolation is free (no new buckets). IAM policies can scope access by prefix if needed in future. Prevents accidental cross-tenant reads even if Door has a bug. |
| **S3 Vectors** (semantic search) | Shared code-shard indexes (`code-shard-0..N`) | **Hard:** per-tenant index: `tenant-{tenant_id}` | **Hard:** per-user index: `personal-{owner_sub}` (already exists for Experience layer) | The existing `S3VectorsCodeStore` already shards by org hash. Per-tenant indexes add physical isolation. Per-user indexes already exist and are used for personal context. |
| **Neptune** (call graph) | Shared graph (`:Symbol` nodes without `tenant_id` property) | **Soft:** add `tenant_id` property on all tenant-scoped nodes; Cypher filter `WHERE n.tenant_id = $tenant_id OR n.tenant_id IS NULL` | **Soft:** add `owner_sub` property; same filter pattern | Neptune has no native namespace/partition; adding a property + filter is the only viable approach without deploying separate clusters. The fail-closed filter makes this acceptable — Neptune data is structural (not the content itself) and requires the Door to reach. |
| **Zoekt** (code search shards) | Shared EBS volume at `/data/index/` | **Hard:** per-tenant shard directory: `/data/index/tenants/{tenant_id}/` | **Hard:** per-user shard directory: `/data/index/users/{owner_sub}/` | Zoekt's shard loading is directory-based. Scoped directories mean the Door can serve queries against only the relevant shard set. Prevents a tenant from accidentally querying another's code at the engine level. |

### 3.3 Why Neptune Gets Soft Isolation (Not Hard)

Neptune Serverless is billed per-cluster, and deploying per-tenant clusters is cost-prohibitive and operationally impractical. The graph stores cross-repo call relationships (**`:Symbol` → `:Symbol` CALLS edges**) — this is structural metadata (function A calls function B), not source code content. A cross-tenant leak of "function X exists and calls function Y" is significantly lower-severity than leaking the actual code or documentation.

The Door already filters all Neptune results through the repo-level ACL check; adding a `tenant_id` property filter is defense-in-depth on top of that.

---

## 4. Identity to Scope Binding

### 4.1 GitHub App Installation to Tenant

**Binding:** A GitHub App installation on an org maps to an ADP `org_id` (and therefore a `tenant_id`).

The gateway already stores this mapping:
- `organizations.github_installation_ids` (JSONB with GIN index, migration 005)
- The webhook-ingress Lambda resolves `installation.id` from payloads

**Ingestion binding flow:**
1. Ingestion trigger carries the `installation_id` (from GitHub webhook or API call)
2. Resolve `installation_id` → `org_id` via the gateway's `organizations` table (already exists)
3. Stamp `tenant_id = org_id` on the `repositories` row at index time
4. Stamp the same `tenant_id` on all downstream artifacts (S3 prefix, S3 Vectors index, Neptune properties)

**For repos under a personal GitHub account** (user, not org): map to the user's `owner_sub`. This makes all user-owned repos per-individual by default.

### 4.2 GitHub Sender to User

**Dependency:** This requires #1319 (GitHub-sender to cognito_sub mapping).

Current state: the webhook-ingress Lambda extracts `actor.github_login` from the payload but maps it to a platform `user_id` (UUID), not a `cognito_sub`. The #1319 design resolves this by extending the gateway's `/internal/v1/resolve-user` endpoint to return `cognito_sub`.

**For tenant isolation, the user binding is needed for:**
1. Per-individual scope — the `owner_sub` on a repo row
2. Audit trail — who triggered the indexing

**Until #1319 ships:** per-individual indexing is blocked. Per-tenant indexing can proceed using only the `installation_id` → `org_id` mapping (which does NOT require user resolution).

### 4.3 Identity Headers at Query Time

The Door already receives and processes:
- `X-GitHub-Login` / `X-GitHub-Teams` → `CallerPrincipal` (for repo-level ACL filter)
- `X-Owner-Sub` / `X-Tenant-Id` → `CallerIdentity` (for personal context)

**Extension for tenant isolation:** the ACL filter (`filter_results` in `acl.py`) must be extended to also check `tenant_id` scoping:

```
Current: caller can see repo IF repo.allowed_principals includes caller's login/teams OR "*"
Extended: caller can see repo IF:
  (a) repo is shared (tenant_id IS NULL, allowed_principals match), OR
  (b) repo is per-tenant (repo.tenant_id == caller's X-Tenant-Id) AND allowed_principals match, OR
  (c) repo is per-individual (repo.owner_sub == caller's X-Owner-Sub)
```

The `X-Tenant-Id` header is already propagated by the dispatch chain (design #1586). No new header channel needed.

---

## 5. Ingestion Trigger Model

### 5.1 Current State

A single global `repos.txt` (14 public + 1 private repo) is the input manifest. Pushing to `index_content/` on main auto-runs `ingest-content.yml`. The daily CronJob also re-indexes based on SHA comparison.

### 5.2 Target State

**Three trigger surfaces (reconciling with E7 #1672):**

| Trigger | Scope | Owner | Mechanism |
|---------|-------|-------|-----------|
| **Global manifest** (`repos.txt`) | Shared | Platform operator | Push to `index_content/` on main (existing) |
| **Tenant repo list** | Per-tenant | Tenant admin (via gateway API) | New: `POST /api/agent-context/repos` → gateway → SQS |
| **Personal repo** | Per-individual | Individual user (via self-serve UI) | New: `POST /api/agent-context/repos` with `visibility: personal` |

**Scope delineation with E7 #1672:**
- **This EPIC (#1721) owns:** the isolation model — how `tenant_id`/`owner_sub` are stamped, how stores are partitioned, how the Door filters.
- **E7 #1672 owns:** the self-serve UI/flow — the API endpoints, the admin panel, the onboarding experience, the per-team sub-scoping within a tenant.

**Interface between #1721 and #1672:** This EPIC defines the **contract** that the self-serve flow must satisfy:
1. Every ingestion request must carry `tenant_id` (required) and `owner_sub` (optional, for personal scope)
2. The SQS message envelope must include `{"scope": {"tenant_id": "...", "owner_sub": "..." | null}}`
3. The ingestion worker reads `scope` from the message and stamps all artifacts accordingly

### 5.3 Postgres Schema for Trigger State

The per-tenant/per-user repo lists are stored in the existing `repositories` table with the new `tenant_id`/`owner_sub` columns — no separate "tenant repo list" table needed. A repo row with `tenant_id = "acme"` IS the acme tenant's repo list entry. Discovery: `SELECT repo_name FROM repositories WHERE tenant_id = $1`.

---

## 6. Migration of Existing Data

### 6.1 The 15 Indexed Repos

| Category | Repos | Current State | Target State | Action |
|----------|-------|--------------|--------------|--------|
| Public eval corpus (14) | `addyosmani/agent-skills`, etc. | `allowed_principals = ["*"]`, no `tenant_id` | `tenant_id = NULL` (shared) | **No action** — `NULL` tenant_id means shared. The new filter logic treats these as "visible to all authorized callers" which matches their current `["*"]` ACL. |
| Private (`aws-e/adp`) | 1 repo | `allowed_principals = [collaborators/teams]`, no `tenant_id` | `tenant_id = "aws-e"` (per-tenant) | **Backfill migration** (see below) |

### 6.2 Migration Steps

**Phase 1 — Schema migration (non-breaking):**
```sql
-- Migration 004: Add tenant isolation columns
ALTER TABLE repositories
  ADD COLUMN tenant_id VARCHAR(256),
  ADD COLUMN owner_sub UUID;

CREATE INDEX ix_repositories_tenant_id ON repositories(tenant_id);
CREATE INDEX ix_repositories_owner_sub ON repositories(owner_sub);
```

This is additive-only. Existing code continues to work — `NULL` columns don't break existing queries.

**Phase 2 — Backfill private repos:**
```sql
-- Classify repos that have non-"*" allowed_principals as tenant-scoped
-- For aws-e/adp: owner="aws-e" → tenant_id="aws-e"
UPDATE repositories
SET tenant_id = owner
WHERE allowed_principals != '["*"]'::jsonb
  AND tenant_id IS NULL;
```

This is a one-time backfill, not a re-index. It stamps the `tenant_id` on existing rows using the already-stored `owner` field (which contains the GitHub org/user, e.g. "aws-e").

**Phase 3 — S3 prefix migration (deferred, optional):**
The existing S3 objects for `aws-e/adp` remain at their current paths (`zoekt-shards/aws-e/adp/...`). New indexing runs for tenant-scoped repos will write to the new prefix structure. The old paths can be left in place (read fallback) or migrated by a background copy job. **No re-index required.**

**Phase 4 — Neptune property backfill:**
```cypher
// Tag existing nodes from aws-e/adp with tenant_id
MATCH (s:Symbol) WHERE s.repo_name STARTS WITH 'aws-e/'
SET s.tenant_id = 'aws-e'
```

This is a graph property update, not a structural change. Runs idempotently.

### 6.3 Why Full Re-index Is Not Required

- **Postgres:** Adding columns with NULL default doesn't require re-reading repo data.
- **S3:** Objects don't move; new writes go to scoped prefixes; reads fall back to the old path for already-indexed content.
- **S3 Vectors:** The shared code-shard indexes continue serving shared-corpus vectors. New tenant indexes get vectors on next re-index of tenant-scoped repos.
- **Neptune:** Property update (not node/edge restructuring).
- **Zoekt:** Existing shards on EBS continue serving. New shards for tenant-scoped repos go to tenant-scoped directories. The Door handles routing.

---

## 7. Fail-Closed Default: Extending Identity to Tenancy

### 7.1 Current Fail-Closed Posture

- **Identity:** `acl.py:filter_results()` — `caller is None` → empty results
- **Personal context:** `identity.py:extract_identity()` — partial headers → `IdentityError` (403)
- **Door:** `server.py` — no `X-GitHub-Login` → results = `[]`

### 7.2 Extension to Tenancy

The fail-closed contract extends as:

**Rule 1:** Un-attributed content (`tenant_id IS NULL`) is only visible if `allowed_principals` matches (shared corpus — existing behavior preserved).

**Rule 2:** Tenant-scoped content (`tenant_id IS NOT NULL`) is **invisible** unless the caller's `X-Tenant-Id` header matches the repo's `tenant_id`.

**Rule 3:** User-scoped content (`owner_sub IS NOT NULL`) is **invisible** unless the caller's `X-Owner-Sub` header matches the repo's `owner_sub`.

**Rule 4:** If `X-Tenant-Id` is absent and the repo is tenant-scoped → **invisible** (fail-closed, not "visible to all tenants").

**Rule 5:** If `X-Owner-Sub` is absent and the repo is user-scoped → **invisible**.

### 7.3 Updated ACL Store Query

The `PostgresACLStore.get_allowed_repos()` query extends from:

```sql
-- Current
SELECT repo_name FROM repositories
WHERE '*' = ANY(allowed_principals)
   OR $login = ANY(allowed_principals)
   OR allowed_principals && $teams_array
```

To:

```sql
-- Extended with tenant/user scope filtering
SELECT repo_name FROM repositories
WHERE (
    -- Shared repos: tenant_id is NULL, ACL matches
    (tenant_id IS NULL AND (
        '*' = ANY(allowed_principals)
        OR $login = ANY(allowed_principals)
        OR allowed_principals && $teams_array
    ))
    -- Tenant-scoped repos: tenant matches, ACL matches
    OR (tenant_id = $tenant_id AND (
        '*' = ANY(allowed_principals)
        OR $login = ANY(allowed_principals)
        OR allowed_principals && $teams_array
    ))
    -- User-scoped repos: owner matches (no further ACL check needed)
    OR (owner_sub = $owner_sub AND owner_sub IS NOT NULL)
)
```

The `CallerPrincipal` dataclass extends to carry `tenant_id` and `owner_sub`:

```python
@dataclass(frozen=True)
class CallerPrincipal:
    github_login: str = ""
    github_teams: list[str] = field(default_factory=list)
    tenant_id: str = ""       # NEW: from X-Tenant-Id header
    owner_sub: str = ""       # NEW: from X-Owner-Sub header
```

### 7.4 The Door's `extract_caller_principal()` Extension

Merge the two extraction functions (currently separate: `extract_caller_principal` for GitHub realm, `extract_identity` for Cognito realm) into a **unified extraction** that populates all four fields when available:

```python
def extract_caller_principal(headers: dict[str, str]) -> CallerPrincipal | None:
    normalized = {k.lower(): v for k, v in headers.items()}
    login = normalized.get("x-github-login", "").strip().lower()
    teams_raw = normalized.get("x-github-teams", "").strip()
    tenant_id = normalized.get("x-tenant-id", "").strip()
    owner_sub = normalized.get("x-owner-sub", "").strip().lower()
    
    teams = [t.strip().lower() for t in teams_raw.split(",") if t.strip()] if teams_raw else []
    
    # Fail-closed: no identity at all → None → empty results
    if not login and not teams and not tenant_id and not owner_sub:
        return None
    
    return CallerPrincipal(
        github_login=login,
        github_teams=teams,
        tenant_id=tenant_id,
        owner_sub=owner_sub,
    )
```

This is backward-compatible: callers that only send `X-GitHub-Login`/`X-GitHub-Teams` still get the shared corpus (existing behavior). Callers that also send `X-Tenant-Id` additionally see their tenant's repos.

---

## 8. Per-Store Implementation Details

### 8.1 Postgres

**New columns on `repositories`:**
- `tenant_id VARCHAR(256)` — nullable; NULL = shared
- `owner_sub UUID` — nullable; NULL = org-scoped or shared

**New indexes:**
- `ix_repositories_tenant_id` on `tenant_id`
- `ix_repositories_owner_sub` on `owner_sub`

**Constraint:** `CHECK (NOT (tenant_id IS NULL AND owner_sub IS NOT NULL))` — a user-scoped repo must also have a tenant context (the user belongs to a tenant).

**Dependencies table:** inherits scope via FK to `repositories.id`. No additional columns needed — the join resolves scope.

### 8.2 S3 (Platform Data Bucket)

**New prefix structure:**

```
s3://agent-context-platform-data-{account_id}/
├── zoekt-shards/                    ← shared corpus (existing)
├── code-indexes/                    ← shared corpus (existing)
├── wikis/                           ← shared corpus (existing)
├── sbom/                            ← shared corpus (existing)
├── tenants/
│   └── {tenant_id}/
│       ├── zoekt-shards/
│       ├── code-indexes/
│       ├── wikis/
│       └── sbom/
├── users/
│   └── {owner_sub}/
│       ├── zoekt-shards/
│       ├── code-indexes/
│       ├── wikis/
│       └── sbom/
└── learning/                        ← personal context (existing, separate)
```

**IAM considerations:** The existing IRSA role (`adp-dev-agent-context-*`) has `s3:*` on the bucket ARN. No IAM change needed for prefix-based isolation — the isolation is enforced at the application level (Door) and by construction (ingestion workers stamp the correct prefix). If customer-managed keys per-tenant are needed later, a per-prefix KMS key policy is additive.

### 8.3 S3 Vectors

**Index naming:**

| Scope | Index Name Pattern | Notes |
|-------|-------------------|-------|
| Shared | `code-shard-{0..N}` | Existing; scatter-gather across shards |
| Per-tenant | `tenant-{tenant_id}` | One index per tenant; lazy-created on first ingestion |
| Per-individual | `personal-{owner_sub}` | **Already exists** (used by Experience layer) |

The existing `S3VectorsCodeStore.put_vectors()` method accepts `org_id` for shard selection. For tenant-scoped repos, the ingestion worker writes to a dedicated `tenant-{tenant_id}` index instead of a shared shard. The Door queries the appropriate index(es) based on the caller's scope:
- Shared callers → scatter-gather `code-shard-*`
- Tenant callers → scatter-gather `code-shard-*` UNION query `tenant-{tenant_id}`
- Individual callers → above UNION query `personal-{owner_sub}`

### 8.4 Neptune

**Properties added to `:Symbol` nodes:**
- `tenant_id` (string, nullable) — NULL means shared
- `owner_sub` (string, nullable) — NULL means not user-scoped

**Query extension in `neptune_client.py`:**

```cypher
// Current
MATCH (s:Symbol {repo_name: $repo})-[:CALLS]->(t:Symbol)
RETURN t

// Extended
MATCH (s:Symbol {repo_name: $repo})-[:CALLS]->(t:Symbol)
WHERE t.tenant_id IS NULL
   OR t.tenant_id = $tenant_id
   OR t.owner_sub = $owner_sub
RETURN t
```

All Neptune queries in `neptune_client.py` (resolve_repo_name, resolve_symbol, callers/callees) receive the scope parameters and apply the filter.

### 8.5 Zoekt

**Shard directory structure on EBS:**

```
/data/index/
├── shared/                    ← public repos (existing shards moved here)
├── tenants/
│   └── {tenant_id}/          ← per-tenant shards
└── users/
    └── {owner_sub}/          ← per-user shards
```

**Query routing in the Door:**
- The Door constructs a Zoekt query with a repo filter regex scoped to the repos the caller can see (existing behavior in `search_backend.py`)
- **Enhancement:** additionally scope the Zoekt search to the appropriate shard directories. The Zoekt web server supports multiple index directories via configuration.

**Alternative (simpler, recommended for v1):** Keep a single Zoekt shard directory and rely on the existing repo-filter at query time + the post-query ACL filter. The S3 prefix isolation ensures shards are produced correctly; the single Zoekt server loads all shards but the Door filters results. This avoids Zoekt reconfiguration complexity.

**Decision:** Start with the simpler approach (single Zoekt, filtered). If performance degrades with many tenant shards loaded into one Zoekt instance, migrate to per-scope shard directories (the S3 prefix structure already supports this).

---

## 9. Ingestion Worker Changes

### 9.1 SQS Message Envelope Extension

The ingestion SQS message currently carries:
```json
{"repo_name": "org/repo", "git_url": "...", "sha": "..."}
```

**Extension:**
```json
{
  "repo_name": "org/repo",
  "git_url": "...",
  "sha": "...",
  "scope": {
    "tenant_id": "aws-e",
    "owner_sub": null,
    "visibility": "tenant"
  }
}
```

`scope.visibility` is one of: `"shared"`, `"tenant"`, `"personal"`.

### 9.2 Worker Routing Logic

The ingestion worker reads `scope` and:
1. **S3 prefix:** selects `tenants/{tenant_id}/...` or `users/{owner_sub}/...` or root (shared)
2. **S3 Vectors index:** writes to `tenant-{tenant_id}` or `personal-{owner_sub}` or `code-shard-N`
3. **Neptune properties:** stamps `tenant_id`/`owner_sub` on all created/updated nodes
4. **Postgres:** stamps `tenant_id`/`owner_sub` on the `repositories` row
5. **ACL derivation:** proceeds as before (GitHub API → `allowed_principals`)

### 9.3 Backward Compatibility

Messages without a `scope` field default to `{"visibility": "shared", "tenant_id": null, "owner_sub": null}`. This maintains backward compatibility with existing ingestion flows (CronJob, global manifest push).

---

## 10. Cost & Quota Analysis

| Resource | Change | Cost Impact |
|----------|--------|-------------|
| S3 | Per-tenant prefixes | Negligible (same bucket, no per-prefix cost) |
| S3 Vectors indexes | 1 per active tenant + 1 per active user | Pay-per-use (no fixed cost per index); bounded by tenant/user count |
| Neptune | Property additions | No cost impact (same node count, marginally larger storage per node) |
| Zoekt EBS | All shards on one volume (v1) | No change |
| Postgres | Two new columns + indexes | Negligible |

**Bounded by:** number of tenants/users that opt into private indexing. Current live state: 12 tenants, 17 users. At scale, 100 tenants × 1 S3 Vectors index = 100 indexes (well within S3 Vectors limits).

**Metering recommendation:** track per-tenant indexed-repo count and per-tenant vector count in the `index_runs` observability log. Alert if any single tenant exceeds a configurable threshold (e.g., 50 repos, 1M vectors).

---

## 11. Decomposition into Child Issues

### Child Issue 1: Schema Migration — Add Tenant Isolation Columns
**Scope:** Alembic migration 004: add `tenant_id`, `owner_sub` columns to `repositories`; add indexes; add CHECK constraint.
**Effort:** Small (1-2 hours)
**Blocks:** All subsequent children

### Child Issue 2: Backfill Existing Private Repos
**Scope:** One-time migration script to set `tenant_id = owner` for repos with non-"*" `allowed_principals`. Neptune property backfill.
**Effort:** Small (1-2 hours)
**Depends on:** Child 1

### Child Issue 3: Extend ACL Filter for Tenant Scoping
**Scope:** Extend `CallerPrincipal` with `tenant_id`/`owner_sub`; update `extract_caller_principal` to read all four headers; update `PostgresACLStore.get_allowed_repos()` with the extended query; add unit tests for all scope combinations.
**Effort:** Medium (4-6 hours)
**Depends on:** Child 1
**Critical path:** This is the security-critical change; requires thorough test coverage.

### Child Issue 4: S3 Prefix Routing in Ingestion Worker
**Scope:** Modify the ingestion worker to read `scope` from SQS message; route artifact writes to the appropriate S3 prefix (`tenants/{tenant_id}/...` or `users/{owner_sub}/...`).
**Effort:** Medium (3-4 hours)
**Depends on:** Child 1

### Child Issue 5: S3 Vectors Tenant Index Support
**Scope:** Extend `S3VectorsCodeStore` to write tenant-scoped vectors to `tenant-{tenant_id}` indexes; update the Door's query path to union shared + tenant + personal indexes.
**Effort:** Medium (4-6 hours)
**Depends on:** Child 3

### Child Issue 6: Neptune Tenant Property Filter
**Scope:** Add `tenant_id`/`owner_sub` properties to Neptune write path; extend all `neptune_client.py` queries with scope filter; backfill existing nodes.
**Effort:** Medium (3-4 hours)
**Depends on:** Child 1

### Child Issue 7: SQS Envelope Extension
**Scope:** Add `scope` field to ingestion SQS message schema; update producer (CronJob, self-serve API, manifest push) to populate it; update consumer to read with backward-compatible default.
**Effort:** Small (2-3 hours)
**Depends on:** Child 4

### Child Issue 8: Cross-Tenant Isolation Integration Tests
**Scope:** End-to-end test: index a repo as tenant A → query as tenant B → assert empty; query as tenant A → assert results. Test all five verbs (search, understand, impact, browse, experience). Test the fail-closed cases.
**Effort:** Medium (4-6 hours)
**Depends on:** Children 3, 4, 5, 6

### Child Issue 9: Per-Individual Scope Integration (blocked on #1319)
**Scope:** Wire `owner_sub` through the full path (SQS → worker → stores → Door filter). Requires #1319 to ship first.
**Effort:** Medium (4-6 hours)
**Depends on:** #1319, Children 3, 4, 5

---

## 12. Phasing & Ordering

```
Phase A (no external dependency):
  Child 1 → Child 2 → Child 3 → Child 4/6/7 (parallel) → Child 5 → Child 8

Phase B (blocked on #1319):
  Child 9
```

Phase A delivers per-tenant isolation end-to-end. Phase B adds per-individual scope once the identity mapping is resolved.

---

## 13. Reuse Table

| Component | Lives In | Reuse Strategy |
|-----------|----------|----------------|
| Tenant model (`org_id`, `organizations`, `users`) | Gateway RDS (`modules/gateway/src/shared/models/organization.py`) | Read-only lookup via gateway's `/internal/v1/resolve-user` endpoint or direct SQL from agent-context (same RDS instance) |
| `github_installation_ids` → `org_id` mapping | Gateway `organizations` table (migration 005) | Join or API call to resolve installation → tenant |
| Identity headers (`X-GitHub-Login`, `X-GitHub-Teams`, `X-Owner-Sub`, `X-Tenant-Id`) | Dispatch chain (webhook Lambda → SQS → agent worker) | Already flow to the Door; no new transport |
| ACL filter (`acl.py`, `CallerPrincipal`, `filter_results`) | `modules/agent-context/door/acl.py` | **Extend** (add fields + query), don't fork |
| Personal-context storage namespacing | `modules/agent-context/personal_context/storage.py` | Reuse the `personal-{owner_sub}` S3 Vectors index pattern for per-user code vectors |
| `S3VectorsEmbeddingStore` (per-user index) | `modules/agent-context/personal_context/backends/s3_vectors_backend.py` | Reuse pattern for per-tenant indexes; share the same bucket |

---

## 14. Open Questions (for operator/stakeholder decision)

1. **Should per-tenant repos also enforce `allowed_principals` within the tenant?** (Recommended: yes — a tenant admin might restrict a repo to specific teams within the org. This matches GitHub's model where org members don't automatically have access to all org repos.)

2. **Cross-tenant shared content.** Can a tenant explicitly share a repo with another tenant? (Recommended: not in v1. Keep the model simple: shared = everyone, tenant = one tenant, personal = one user. Multi-tenant sharing is a future extension.)

3. **Zoekt multi-instance.** If tenant count exceeds ~50 with significant index sizes, the single Zoekt server may hit memory limits. At what point do we invest in per-tenant Zoekt instances? (Decision deferred to operational monitoring.)

---

## 15. Alignment with Existing Architecture

This design:
- **Extends** the Door's ACL filter (does not fork or replace it)
- **Reuses** the gateway's tenant model (does not create a new tenant registry)
- **Reuses** the S3 Vectors per-user index pattern (already proven for personal context)
- **Reuses** the identity header channel (no new transport)
- **Mirrors** the gateway's `TenantMixin` pattern (adding `tenant_id` as an indexed column)
- **Preserves** the fail-closed security posture (extending it from identity to tenancy)
- **Does not** require a full re-index of existing data
- **Does not** require new AWS infrastructure (no new buckets, clusters, or services)
- **Does not** change the existing shared-corpus behavior for current users

---

## 16. Security Invariants (Test Coverage)

Every child issue's test plan must prove these invariants:

| # | Invariant | Test Strategy |
|---|-----------|---------------|
| 1 | Tenant A cannot see tenant B's private repos through any verb | Cross-tenant isolation test (all 5 verbs) |
| 2 | User X cannot see user Y's personal repos (same tenant) | Cross-user isolation test |
| 3 | Unknown caller (no headers) sees nothing | Existing test, re-verify |
| 4 | Missing `X-Tenant-Id` with tenant-scoped repo → invisible | New unit test on extended ACL query |
| 5 | Shared corpus remains visible to all authorized callers | Regression test |
| 6 | Public repos (`allowed_principals = ["*"]`) with NULL tenant → visible | Regression test |
| 7 | Partial headers on personal-context ops → 403 | Existing test, re-verify |
| 8 | Neptune cross-repo call graph doesn't leak caller names across tenants | New integration test |
| 9 | S3 prefix isolation: tenant A's artifacts don't appear under tenant B's prefix | Ingestion worker unit test |
| 10 | Backfill correctly classifies existing repos (no silent exposure) | Migration verification test |
