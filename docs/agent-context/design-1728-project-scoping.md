# Design Note: Project-Scoped Knowledge Layer

**Status:** Design of record
**Issue:** #1728 (Child of EPIC #1345)
**Author:** @agent-architect
**Date:** 2026-06-23
**Depends on:** #1721 (Tenant & individual isolation), #1319 (GitHub-sender → cognito_sub mapping)

---

## 1. Summary

This design adds **project** as an optional, query-time organizational view within the Knowledge Layer. A project is a named grouping of repositories owned by a user, enabling scoped retrieval (search/understand/impact/browse return results only from the project's repos). Project is NOT a confidentiality boundary — it is a soft filter layered on top of #1721's `(tenant_id, owner_sub)` isolation model.

**Key properties:**
- Soft view — can only narrow, never widen, what isolation already permits
- M:N membership — a repo is indexed once and can belong to many projects
- No physical partitioning — no new S3 prefixes, vector indexes, Zoekt dirs, or Neptune properties
- Optional — `project_id = NULL` / unspecified means "all my visible content" (today's behaviour)

---

## 2. Schema

### 2.1 New table: `projects`

```sql
CREATE TABLE projects (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_sub   VARCHAR(128) NOT NULL,     -- Cognito sub of the owning user (UUID)
    name        VARCHAR(256) NOT NULL,     -- Human-readable project name
    tenant_id   VARCHAR(128),             -- NULLABLE: reserved for future team-owned projects
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_projects_owner_name UNIQUE (owner_sub, name)
);

CREATE INDEX ix_projects_owner_sub ON projects (owner_sub);
CREATE INDEX ix_projects_tenant_id ON projects (tenant_id) WHERE tenant_id IS NOT NULL;
```

**Design decisions:**

| Column | Rationale |
|--------|-----------|
| `owner_sub` | The Cognito UUID of the user who owns this project. Ties to the identity plumbing in #1319. NOT NULL because v1 projects are always user-owned. |
| `name` | User-facing label (e.g. "client-A", "internal-tooling"). Unique per owner (same user can't have two projects with the same name). |
| `tenant_id` NULLABLE | **Future-proofing for team-owned projects.** When NULL, the project is personal (scoped to `owner_sub`). When set, the project is team/tenant-owned and visible to all members of that tenant. This nullable column avoids a schema migration when team projects ship later (a future EPIC just starts writing non-NULL values). The partial index ensures we can efficiently query team projects without scanning personal ones. |

### 2.2 New table: `project_repositories`

```sql
CREATE TABLE project_repositories (
    project_id  UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    repo_id     UUID NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (project_id, repo_id)
);

CREATE INDEX ix_project_repositories_repo_id ON project_repositories (repo_id);
```

**Design decisions:**

| Choice | Rationale |
|--------|-----------|
| Composite PK `(project_id, repo_id)` | Natural M:N representation. No surrogate ID needed — the pair is the identity. |
| `ON DELETE CASCADE` on both FKs | Deleting a project removes its memberships; deleting a repo removes it from all projects. Both are safe and expected. |
| `added_at` | Audit trail for when a repo was added to a project. Useful for UI display ("recently added") and debugging. |
| Index on `repo_id` | Enables the reverse lookup: "which projects contain this repo?" Useful for UI and for cascade-delete performance. |

### 2.3 `repositories` table: UNCHANGED

The `repositories` table receives **no modifications**. A repo is not "in one project" — it can belong to zero, one, or many projects via the join table. This is the explicit benefit of the M:N design: no column addition, no data migration, no disruption to existing indexing or ACL logic.

---

## 3. Door Query-Wrapper Extension

### 3.1 Architecture: where project filtering lives

Project filtering is a **single additional step** in the Door's central filter pipeline, positioned AFTER #1721's isolation filter and BEFORE result return:

```
Request arrives (with identity headers + optional project_id)
    │
    ├─ Step 1: Extract identity (X-GitHub-Login/Teams + X-Owner-Sub/Tenant-Id)
    │           → CallerPrincipal + CallerIdentity
    │
    ├─ Step 2: #1721 isolation filter (fail-closed)
    │           → Restrict to caller's permitted repo set
    │
    ├─ Step 3: Project filter (this EPIC)    ← NEW
    │           → If project_id specified:
    │              a. Resolve project_id → set(repo_id) [one Postgres lookup]
    │              b. Verify project.owner_sub matches caller's owner_sub
    │              c. Intersect project's repo set with isolation-permitted set
    │           → If project_id NOT specified: pass through (no narrowing)
    │
    └─ Step 4: Backend query (Zoekt/Neptune/S3/browse) using the narrowed repo set
```

**Critical invariant:** Project can only INTERSECT (narrow) the isolation-permitted set. It can never add repos the caller isn't entitled to see. This is enforced by the ordering: isolation runs first, project intersects the result.

### 3.2 Implementation: `resolve_project_repos()`

New function in the Door (proposed location: `door/project_filter.py`):

```python
def resolve_project_repos(
    project_id: str,
    caller_owner_sub: str,
    db_pool: Any,
) -> set[str] | None:
    """Resolve a project to its repo_id set with ownership verification.

    Returns:
        set of repo_id UUIDs if the project exists and is owned by the caller.
        None if project_id is invalid, not found, or not owned by caller.

    Ownership check prevents one user from querying under another user's project.
    """
    query = """
        SELECT pr.repo_id
        FROM project_repositories pr
        JOIN projects p ON p.id = pr.project_id
        WHERE p.id = $1
          AND (p.owner_sub = $2 OR p.tenant_id IS NOT NULL)
    """
    # Execute and return set of repo_ids (as strings)
```

**Ownership semantics:**
- Personal project (`tenant_id IS NULL`): only the owner (`owner_sub` match) can use it as a filter
- Team project (`tenant_id IS NOT NULL`): any member of that tenant can use it (v2 — for now, all projects are personal)

### 3.3 Per-store application

Project changes NOTHING physical — it narrows the repo set used in every store query:

| Store | How project applies | Change to existing code |
|-------|--------------------|-----------------------|
| **Postgres** (browse catalog) | `JOIN project_repositories WHERE project_id = $p` added to catalog queries | One additional `WHERE repo_id IN (...)` clause |
| **Zoekt** (search) | Door restricts search to shards for the project's repos, then ACL-filters results by the narrowed set | Pass `repo_names` filter to Zoekt query (already supports `repo:` filter syntax) |
| **S3** (code-index, browse content) | Read artifacts only for the project's repo set (resolved from PG) | Filter `repo_id` candidates before S3 key construction |
| **S3 Vectors** (semantic/wiki) | Restrict candidate repo set before the vector query — NOT per-project indexes | Add `repo_id IN (...)` metadata filter to vector search request |
| **Neptune** (understand/impact) | Add `WHERE n.repo IN $projectRepos` to openCypher queries — NOT a node property | One additional `AND` clause in Cypher; symbol's repo membership is dynamic per-query |

**Important: Neptune symbols are NOT project-tagged.** A symbol's repo can appear in many projects. The project filter is applied as a query-time `WHERE` clause, not as a stored property on the graph node. This is the direct consequence of M:N: a function in repo-A is "in" both project-X and project-Y simultaneously.

---

## 4. Default / Ungrouped Behaviour

**Rule: project is OPTIONAL. No project = today's behaviour.**

| Scenario | Behaviour |
|----------|-----------|
| `project_id` not specified in request | Skip Step 3 entirely. Query proceeds against the full isolation-permitted set. Identical to current behaviour. |
| `project_id` specified but project is empty (no repos) | Return empty results (the intersection of any set with ∅ is ∅). |
| `project_id` specified but project doesn't exist | Treated as "no project" (fail-open on invalid project = safe default; the isolation filter still runs). Alternative: return error. **Decision: return an error** (`{"error": "project_not_found"}`) so the caller knows their filter isn't working, rather than silently returning unscoped results. |
| Repo not in any project | Visible in unscoped queries. Not visible in any project-scoped query. This is expected — project membership is opt-in. |

**No "default project" concept in v1.** A user explicitly passes a project filter or doesn't. There is no system-assigned grouping. This avoids the complexity of auto-assignment and keeps the model clean.

---

## 5. Verb Contract: Project as an Optional Parameter

### 5.1 MCP tool parameter addition

All four retrieval verbs gain an optional `project` parameter:

```python
# Addition to TOOLS in door/server.py
{
    "name": "search",
    "parameters": {
        "query": {"type": "string", "required": True},
        "scope": {"type": "string", "required": False},
        "limit": {"type": "integer", "required": False},
        "project": {"type": "string", "required": False},  # NEW: project ID or name
    },
},
{
    "name": "understand",
    "parameters": {
        "target": {"type": "string", "required": True},
        "depth": {"type": "string", "required": False},
        "project": {"type": "string", "required": False},  # NEW
    },
},
{
    "name": "impact",
    "parameters": {
        "target": {"type": "string", "required": True},
        "cross_repo": {"type": "boolean", "required": False},
        "project": {"type": "string", "required": False},  # NEW
    },
},
{
    "name": "browse",
    "parameters": {
        "action": {"type": "string", "required": True},
        "uri": {"type": "string", "required": True},
        "depth": {"type": "integer", "required": False},
        "project": {"type": "string", "required": False},  # NEW
    },
},
```

**Write verbs (`remember`, `experience`) do NOT get a project parameter** — they are personal context operations scoped by `owner_sub`, not by repository membership.

### 5.2 Resolution: name vs. ID

The `project` parameter accepts **either** a project UUID or a project name:

```
project: "550e8400-e29b-41d4-a716-446655440000"  → UUID lookup
project: "client-A"                               → name lookup (scoped to caller's owner_sub)
```

Resolution logic:
1. If the value is a valid UUID → look up by `projects.id`
2. Otherwise → look up by `projects.name` WHERE `owner_sub = caller.owner_sub`

Name-based lookup is scoped to the caller to prevent cross-user project name enumeration.

### 5.3 Session default (future, not v1)

v1 delivers per-request `project` parameter only. A "session default project" (set once, applies to all subsequent calls) is a natural extension but introduces statefulness that conflicts with the Door's `stateless_http=True` design. Options for later:

- **Header-based:** `X-Project-Id` header set by the dispatch layer (mirrors identity headers)
- **Client-side:** the MCP client always includes `project` in every call (client memory)
- **Context endpoint:** `POST /project/set` stores in a short-lived cache (breaks statelessness)

**Decision for v1:** per-request `project` parameter in each tool call. No session state. The dispatch layer MAY set an `X-Project-Id` header as a convenience (the Door reads it as a default when the tool argument is absent), but the tool argument always takes precedence.

---

## 6. Project Selection at Query Time

### 6.1 Two entry points

| Entry point | How project is specified | Who sets it |
|-------------|--------------------------|-------------|
| **MCP tool argument** | `project` field in the tool call's `arguments` dict | The calling agent (MCP client) |
| **HTTP header** (optional default) | `X-Project-Id` header on the HTTP request | The dispatch layer (webhook Lambda / agent worker) |

**Precedence:** tool argument > header > no project (unscoped).

### 6.2 Reconciliation with #1319 identity plumbing

The identity flow from #1319 already propagates `X-Owner-Sub` and `X-Tenant-Id` headers through the SQS envelope → agent worker → MCP call chain (`personal-context-headers.ts`). Project follows the same pattern:

```
SQS envelope (task payload):
  cognito_sub: "..."      → X-Owner-Sub
  tenant_id: "..."        → X-Tenant-Id
  project_id: "..."       → X-Project-Id       ← NEW (optional)
```

The agent worker's `buildPersonalContextIdentity()` function (in `personal-context-headers.ts`) gains an optional `project_id` field that maps to the `X-Project-Id` header. This is backward-compatible (field is optional; omitting it = no project filter).

For the **MCP tool argument path** (preferred): the agent simply includes `"project": "..."` in its tool call arguments. No header needed — the Door reads it from the arguments dict.

---

## 7. Cross-Owner References

**Question:** Can a user's project include a shared-corpus (public) repo via the join table?

**Answer: Yes.**

A user may add any repo they can see (per #1721's ACL) to their project. This includes:
- Repos they own
- Repos their team has access to
- Public repos (`allowed_principals = ["*"]`)

The `project_repositories` join table simply records `(project_id, repo_id)`. There is no ownership check at membership-add time beyond "does the repo exist in the `repositories` table?" The ACL check at query time (Step 2 in §3.1) ensures the user can only SEE repos they're permitted to access — project membership doesn't grant new access.

**Safety argument:** Adding a public repo to your project is like bookmarking it. You could always see it (it's public); the project just includes it in your scoped view. If the repo later becomes private and you lose access, the ACL filter (Step 2) will exclude it from your results — the project membership row persists but has no effect (it's a dangling reference, not a security leak).

---

## 8. Alignment with Existing Architecture

### 8.1 Reuse table

| What | Lives in | How this EPIC uses it |
|------|----------|----------------------|
| Isolation model (tenant/user filter) | #1721, `door/acl.py` | This EPIC extends the same central filter pipeline with one more step (project intersection). Does NOT fork the ACL store or filter_results(). |
| Tenant model (`org_id`/`tenants`/`users`) | Gateway RDS | The nullable `tenant_id` on `projects` references the same tenant identifiers. No FK to gateway tables (cross-DB); referential integrity is logical. |
| Identity extraction | `personal_context/identity.py` | Project filter uses `caller.owner_sub` for ownership verification. Same header extraction, same fail-closed semantics. |
| PostgresACLStore.get_allowed_repos() | `door/acl.py` | Project filter resolves `project_id → set(repo_id)`, then intersects with the ACL-permitted set. Composition, not replacement. |
| Zoekt search filter | `door/search_backend.py` | Zoekt already supports `repo:` filter syntax. Project filter passes the narrowed repo list through the same mechanism. |
| Neptune query patterns | `door/structural_backend.py` | Neptune queries already use `WHERE n.repo = $repo` patterns. Project adds `WHERE n.repo IN $repos` — same shape, broader predicate. |

### 8.2 What this EPIC does NOT change

- `repositories` table schema (no new columns)
- `allowed_principals` semantics or derivation logic
- S3 key structure or bucket layout
- S3 Vectors index configuration
- Zoekt shard organization
- Neptune node/edge schema
- `remember` / `experience` verb semantics
- ACL derivation at ingest time (`derive_acl_from_github`)

---

## 9. Migration Strategy

### 9.1 Alembic migration (004)

New migration `004_project_scoping.py`:
- Creates `projects` table
- Creates `project_repositories` join table
- Creates indexes
- No data migration (additive only — new empty tables)
- Down migration: drop both tables + indexes

### 9.2 Rollout

1. **Migration runs first** (new tables, no impact on existing queries)
2. **Door code deploys** with project filter logic (inactive until called with a `project` param)
3. **MCP tool definitions update** (add optional `project` parameter to retrieval verbs)
4. **Agent worker update** (pass `X-Project-Id` header if project context is available)

Each step is independently deployable and backward-compatible. Step 1 without Step 2 = new tables exist but are unused. Step 2 without Step 3 = filter logic exists but no caller passes the parameter. Step 3 without Step 4 = MCP clients can specify project manually but the dispatch layer doesn't auto-set it.

---

## 10. API Surface: Project Management

Beyond the query-time filter, users need CRUD operations to manage projects. These are **separate from the MCP verbs** (the verbs are retrieval; management is admin):

### 10.1 Proposed REST endpoints (on the Door server)

```
POST   /projects                      → Create a new project
GET    /projects                      → List caller's projects
GET    /projects/:id                  → Get project details + repo list
PATCH  /projects/:id                  → Update project name
DELETE /projects/:id                  → Delete project (cascades memberships)

POST   /projects/:id/repos            → Add repo(s) to project
DELETE /projects/:id/repos/:repo_id   → Remove repo from project
```

All endpoints require `X-Owner-Sub` + `X-Tenant-Id` headers. Ownership is enforced: a user can only manage their own projects.

### 10.2 Alternative: MCP verb for management

A `project` MCP tool (like `experience`) could handle CRUD:
```
project(action="create", name="client-A")
project(action="list")
project(action="add_repo", project="client-A", repo="org/repo-name")
project(action="remove_repo", project="client-A", repo="org/repo-name")
```

**Decision: REST endpoints (§10.1) for v1.** Rationale: project management is a UI/admin operation, not something agents typically do mid-task. REST is simpler, cacheable, and doesn't pollute the MCP tool surface that agents see.

---

## 11. Child Issue Decomposition

The following implementable issues derive from this design. Each has the #1721 dependency explicit:

### Issue A: Database migration — `projects` + `project_repositories` tables
- **Scope:** Alembic migration 004, creates both tables + indexes
- **Depends on:** #1721 landed (so the isolation model is in place), but technically independent (additive migration)
- **Effort:** Small (1 file, ~80 lines)
- **Validation:** `alembic upgrade head` succeeds; tables exist; down migration drops cleanly

### Issue B: Door project filter — `resolve_project_repos()` + pipeline integration
- **Scope:** New `door/project_filter.py` module; integration into the central filter in `_apply_acl()` / `_dispatch_tool()`
- **Depends on:** Issue A (tables must exist), #1721 (isolation filter must be in place as the prior step)
- **Effort:** Medium (new module + integration into 4 verb handlers)
- **Validation:** Unit tests: project filter narrows correctly; invalid project returns error; unspecified project = passthrough; ownership check blocks cross-user access

### Issue C: MCP verb parameter addition — `project` on retrieval verbs
- **Scope:** Update TOOLS constant in `server.py`; update MCP tool signatures in `mcp_app.py`; pass `project` through `_dispatch_tool()` to the filter
- **Depends on:** Issue B (filter logic must exist to consume the parameter)
- **Effort:** Small-Medium (signature changes across 4 tools, both REST and MCP paths)
- **Validation:** MCP `tools/list` response includes `project` parameter; e2e test passes project and receives scoped results

### Issue D: Project management REST API
- **Scope:** New `door/project_api.py` router; CRUD endpoints per §10.1; mount on the Door's FastAPI app
- **Depends on:** Issue A (tables), #1319 (identity headers for ownership enforcement)
- **Effort:** Medium (new router, ~6 endpoints, auth integration)
- **Validation:** CRUD operations work; ownership enforced; cascading delete works

### Issue E: Identity plumbing — `X-Project-Id` header propagation
- **Scope:** Optional `project_id` field in SQS envelope; `buildPersonalContextIdentity()` update; Door reads `X-Project-Id` as default
- **Depends on:** #1319 (identity mapping must be in place), Issue C (Door must accept the parameter)
- **Effort:** Small (3 files: TS envelope type, header builder, Door header reader)
- **Validation:** Agent worker propagates project ID from envelope to MCP call; Door uses header as default when tool arg absent

### Issue F: Tests — isolation invariant + M:N correctness
- **Scope:** Unit + integration tests covering the key invariants
- **Depends on:** Issues A–C (all layers must be testable)
- **Effort:** Medium (test matrix covering project filter, M:N, ownership, cross-owner, ACL interaction)
- **Validation:** All tests pass; test coverage for `project_filter.py` ≥ 90%

### Ordering

```
#1721 (isolation) ─┬─→ Issue A (migration) ─→ Issue B (filter) ─→ Issue C (verb params)
                   │                                              ↓
#1319 (identity) ──┴─→ Issue D (REST API) ──────────────────────→ Issue E (header plumbing)
                                                                  ↓
                                                             Issue F (tests)
```

---

## 12. Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|-----------|
| Project filter adds latency (extra PG query per request) | ~1-5ms per query (single indexed lookup) | Cache project→repo_id mapping for the duration of a single request (not cross-request — membership can change). If hot, add a short TTL cache (30s). |
| Large project (1000+ repos) creates large IN clause | Slow Zoekt/Neptune queries | Postgres resolves to repo_ids; pass as a set to each backend. Zoekt handles `repo:` filters natively. Neptune uses parameterized lists. If still slow, paginate or cap project size (design note: recommend max 500 repos per project in v1). |
| Dangling memberships (repo deleted but membership row remains) | Stale data in join table | `ON DELETE CASCADE` on `project_repositories.repo_id` FK — Postgres handles cleanup automatically. |
| Project ownership bypass (user guesses another user's project UUID) | Sees results scoped to wrong project (but still within their own ACL-permitted repos) | Ownership check in `resolve_project_repos()` — project must be owned by caller OR be a team project in caller's tenant. |

---

## 13. Non-Goals (Explicitly Out of Scope)

- **Project as a security boundary** — Project never widens visibility. This is an organizational view, not an ACL. (#1721 owns security.)
- **Auto-assignment of repos to projects** — Users manually manage membership. No inference, no "smart grouping."
- **Project-level permissions** — No "share this project with teammate" in v1. (Reserved for team-owned projects in a future EPIC.)
- **Per-project indexes or storage** — No new S3 prefixes, vector indexes, Zoekt dirs, or Neptune properties. The whole point of the soft + M:N decision.
- **Project UI** — The REST API enables a future UI; the UI itself is out of scope for this EPIC.
- **Session-stateful project context** — v1 is per-request only. No server-side session memory of "current project."
