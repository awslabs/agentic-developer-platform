# Design: Repo-Grain ACL Mirrored from GitHub + Door Fail-Closed Filtering

**Issue:** #1356 (Sub of EPIC #1345)
**Status:** Implementation-ready design
**Author:** @agent-architect
**Date:** 2026-06-11
**Design of record:** `docs/knowledge-layer-storage-design.md` §8

---

## 1. Problem Statement

The Knowledge Layer indexes code from private and public repos into shared
search engines (Zoekt for exact search, S3 Vectors for semantic search). Neither
engine has document-level security — any query against the shared index returns
results from all indexed repos. Without enforcement at the query layer, a caller
could see code from repos they have no GitHub access to.

**The invariant:** every search result must be filtered so the caller only sees
repos they're permitted to access on GitHub. If the caller's identity cannot be
resolved, they see **nothing** (fail-closed).

---

## 2. Design Decisions

### 2.1 Why post-query filtering (not per-tenant indexes)

| Approach | Pros | Cons | Verdict |
|----------|------|------|---------|
| **Separate index per org/user** | True isolation — no filter needed | N × storage cost; cross-org search impossible; Zoekt doesn't support multi-index query natively | Rejected for code search |
| **Post-query filter at the Door** | Single index; simple; correctness depends on one code path; leverages Postgres ACL table | ~O(batch_size) Postgres lookups per query; over-fetching from engine | **Selected** — matches design of record §8 |
| **S3 Vectors filterable metadata** | Filter at query time via metadata predicate; no over-fetch | Only helps S3 Vectors (not Zoekt); metadata is limited to 2 KB; principal lists for large repos may exceed | **Complementary** — use where possible, fall back to post-query filter |

**Decision:** The Door (MCP query layer) applies a post-query filter to ALL
result sets. S3 Vectors queries additionally pass a `principal` metadata filter
as an optimization (reduces over-fetch for the common case), but correctness
never depends on it alone — the Door filter is the single enforcement point.

### 2.2 Principal format (GitHub identities, not Cognito subs — for now)

The design stores `allowed_principals` using **GitHub identities** (login
usernames and team slugs like `org/team-name`), not Cognito subs. This is
because:

1. Enterprise/code indexing is the first consumer — it indexes org repos where
   access is defined in GitHub terms.
2. The mapping from GitHub identity → Cognito sub (required for personal context)
   depends on #1319, which is not yet implemented.
3. The caller principal for code-search queries is derived from the GitHub App
   installation context (which repo is the agent working on? who triggered the
   run?), not from a Cognito JWT.

**When #1319 ships:** the Door can additionally resolve the caller's
`cognito_sub` → GitHub identity and check against `allowed_principals`. Until
then, personal-context gated code search (a user in the chat UI searching code)
is not supported; only agent-triggered code search (where the GitHub identity is
available from the webhook envelope) works.

### 2.3 Fail-closed semantics

The filter is the **security boundary** for multi-tenant code search. It MUST
fail closed:

| Condition | Behavior |
|-----------|----------|
| Caller principal is a valid GitHub identity | Filter results to repos where that identity is in `allowed_principals` |
| Caller principal is empty/None/unresolvable | Return **empty** result set (zero hits) |
| `allowed_principals` column is NULL or empty for a repo | That repo's results are **dropped for all callers** (deny-by-default) |
| `allowed_principals` contains the special value `*` (public) | That repo's results are visible to ALL callers, including anonymous |

The `*` sentinel is used for public/OSS repos that should be searchable by
everyone. This is set at ingest time based on GitHub repo visibility.

---

## 3. Schema

### 3.1 `repositories` table (in `agent_context` database)

This table is defined by the parent EPIC #1345 schema sub-issue. The column
relevant to this issue:

```sql
CREATE TABLE repositories (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_name       TEXT NOT NULL UNIQUE,   -- e.g. "aws-e/adp"
    git_url         TEXT NOT NULL,
    owner           TEXT NOT NULL,          -- GitHub org or user
    allowed_principals TEXT[] NOT NULL DEFAULT '{}',  -- GitHub logins + team slugs
    last_indexed_at TIMESTAMPTZ,
    last_sha        TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Index for the lookup in the filter path
CREATE INDEX idx_repositories_repo_name ON repositories (repo_name);
```

**`allowed_principals` semantics:**

- Array of text values, each a GitHub identity:
  - User login: `"octocat"` (lowercase)
  - Team slug: `"my-org/backend-team"` (org-prefixed)
  - Public sentinel: `"*"`
- Empty array `{}` means **no one can see this repo** (deny-by-default).
- Re-derived on every ingest run (tracks GitHub in near-real-time).

### 3.2 S3 Vectors filterable metadata

Each vector stored in S3 Vectors carries filterable metadata:

```json
{
  "repo": "aws-e/adp",
  "file": "src/main.py",
  "is_public": true
}
```

For S3 Vectors queries, when the caller's GitHub identity is known, the query
includes a metadata filter: `repo IN (list of allowed repos)` OR `is_public =
true`. This reduces over-fetch but is NOT the sole enforcement — the Door filter
always runs afterward.

**Why not put `allowed_principals` in S3 Vectors metadata?** The filterable
metadata limit is 2 KB per vector. A repo with 50+ collaborators/teams would
exceed this. The `is_public` boolean and `repo` name are small, predictable, and
sufficient for the optimization.

---

## 4. ACL Derivation at Ingest Time

### 4.1 Where in the pipeline

In `ingest-repo.py`, a new step inserts **between** clone (Step 2/3) and index
building (Step 5). This becomes Step 4 in the design-of-record:

```
Step 3: Clone repo
Step 4: Derive ACL (NEW)  ← GitHub API → allowed_principals → Postgres
Step 5: Build indexes (Zoekt, structure map, S3 Vectors, SBOM)
```

### 4.2 GitHub API calls

Using the GitHub App installation token (already available for private repo
clone), the worker calls:

1. **`GET /repos/{owner}/{repo}`** — check `visibility` field.
   - If `"public"` → `allowed_principals = ["*"]` (done, skip collaborator fetch).
   - If `"private"` or `"internal"` → proceed to collaborator/team enumeration.

2. **`GET /repos/{owner}/{repo}/collaborators`** (paginated, affiliation=direct)
   — yields user logins with `push` or `admin` permission.

3. **`GET /repos/{owner}/{repo}/teams`** (paginated)
   — yields team slugs (e.g. `"backend-team"`) with their permission level.
   We include teams with `push`, `maintain`, or `admin` access.

**Combined principal list:**

```python
principals = []
if repo_visibility == "public":
    principals = ["*"]
else:
    # Users with at least push access
    principals += [c["login"].lower() for c in collaborators if c["permissions"]["push"]]
    # Teams with at least push access
    principals += [f"{owner}/{t['slug']}" for t in teams if t["permission"] in ("push", "maintain", "admin")]
```

### 4.3 Required token scopes

The GitHub App must have:
- `Repository: Contents (read)` — already required for clone
- `Organization: Members (read)` — required for `/repos/{owner}/{repo}/teams`
- `Repository: Administration (read)` — required for `/repos/{owner}/{repo}/collaborators`

If the token lacks `read:org`, the teams call will 403 → the worker logs a
warning and proceeds with only direct-collaborator ACL (safe degradation: some
legitimate team-member access won't be recognized, which means deny — fail-closed).

### 4.4 Storage

The derived `allowed_principals` list is written to the `repositories` table in
Postgres via an upsert:

```sql
INSERT INTO repositories (repo_name, git_url, owner, allowed_principals, last_indexed_at, last_sha)
VALUES ($1, $2, $3, $4, now(), $5)
ON CONFLICT (repo_name) DO UPDATE SET
    allowed_principals = EXCLUDED.allowed_principals,
    last_indexed_at = EXCLUDED.last_indexed_at,
    last_sha = EXCLUDED.last_sha,
    updated_at = now();
```

Re-derived every ingest so it tracks GitHub permission changes.

### 4.5 Rate limiting / cost

- One API call to check visibility: negligible.
- Collaborator + team pagination: for repos with <100 collaborators + <20 teams,
  this is 2-3 API calls total.
- For 500 repos indexed in parallel (50-100 workers): ~1,500 GitHub API calls
  spread across the ingest window. Well within GitHub App rate limits
  (5,000/hour/installation).

---

## 5. Door Filter (Query-Time Enforcement)

### 5.1 Where it lives

The MCP server (port 5100) currently implements 6 tools: `search`, `understand`,
`impact`, `browse`, `remember`, `experience`. The filter applies to:

- `search` — always (filters hits by repo)
- `understand` — when returning cross-repo references
- `impact` — when returning affected repos
- `browse` — when target is a private repo

### 5.2 Caller principal resolution

The caller's GitHub identity is propagated via request headers set by the
dispatch layer (same trust model as `X-Owner-Sub` / `X-Tenant-Id`):

```
X-GitHub-Login: octocat
X-GitHub-Teams: my-org/backend-team,my-org/platform-team
```

These headers are set by the trusted dispatch layer (webhook Lambda → SQS →
agent worker → MCP request). For agent-triggered queries, the GitHub login comes
from the webhook sender; the teams come from the installation context.

**If these headers are absent or empty:** the filter returns empty results
(fail-closed). This is the critical invariant.

### 5.3 Filter algorithm (pseudocode)

```python
def filter_results(results: list[SearchHit], caller: CallerPrincipal | None) -> list[SearchHit]:
    """Post-query filter. Fail-closed when caller is None."""
    if caller is None:
        return []  # FAIL-CLOSED: unresolved principal → nothing

    if not caller.github_login and not caller.github_teams:
        return []  # FAIL-CLOSED: empty principal → nothing

    # Build the set of repos this caller can see
    # (cacheable per-request or per-session, TTL ~60s)
    allowed_repos = get_allowed_repos(caller)

    filtered = []
    for hit in results:
        repo = hit.repo_name
        if repo in allowed_repos:
            filtered.append(hit)
        # else: silently drop — the caller never learns the hit existed

    return filtered


def get_allowed_repos(caller: CallerPrincipal) -> set[str]:
    """Look up which repos this caller is allowed to see.

    Query Postgres: repos WHERE '*' = ANY(allowed_principals)
    OR caller.github_login = ANY(allowed_principals)
    OR ANY(caller.github_teams) && allowed_principals
    """
    query = """
        SELECT repo_name FROM repositories
        WHERE '*' = ANY(allowed_principals)
           OR %s = ANY(allowed_principals)
           OR allowed_principals && %s
    """
    rows = db.execute(query, [caller.github_login, caller.github_teams])
    return {row.repo_name for row in rows}
```

### 5.4 Caching strategy

The `get_allowed_repos` result is cacheable for short durations:

- **Per-request cache:** Within a single MCP tool call, multiple sub-queries
  (e.g., Zoekt + S3 Vectors for `search`) share one ACL lookup.
- **Short TTL cache (60s):** Across rapid sequential tool calls from the same
  agent session. Bounded by the agent's GitHub identity, invalidated on session end.
- **No long-term cache:** Permissions can change at any time on GitHub; the
  60s window is acceptable (same-run consistency) without staleness risk.

### 5.5 Performance

- Postgres query with GIN index on `allowed_principals`:
  ```sql
  CREATE INDEX idx_repositories_principals ON repositories USING GIN (allowed_principals);
  ```
- For 500 indexed repos, this is a single indexed query returning ~50-200 repos
  (typical developer access). Sub-millisecond on Postgres.
- The filter itself is an O(n) scan of results (typically 10-50 hits). Negligible.

---

## 6. S3 Vectors Metadata Optimization

At ingest time (Step 5c in the pipeline), when writing vectors:

```python
put_vectors(
    vectors=[{
        "key": f"{repo}:{file}:{function_name}",
        "data": embedding_vector,
        "metadata": {
            "repo": repo_name,           # filterable
            "file": file_path,           # filterable
            "is_public": is_public,      # filterable (bool)
            "function": function_name,   # non-filterable (display)
            "lines": f"{start}-{end}",   # non-filterable (display)
        }
    }]
)
```

At query time, the S3 Vectors query includes:

```python
filter = {
    "$or": [
        {"is_public": {"$eq": True}},
        {"repo": {"$in": list(allowed_repos)}}  # from get_allowed_repos()
    ]
}
```

This reduces the number of results the Door must post-filter, but the Door
filter still runs on the response (defense in depth).

---

## 7. Identity Gap: #1319 Dependency

| Caller Path | GitHub Identity Available? | Code Search Works? |
|-------------|---------------------------|--------------------|
| Agent triggered by webhook (GitHub) | ✅ sender login + installation teams | ✅ Full ACL filtering |
| Agent in GitHub Actions (ARC runner) | ✅ `GITHUB_ACTOR` env var + org context | ✅ Full ACL filtering |
| User in chat UI (Cognito JWT) | ❌ only `cognito_sub` available | ❌ Fail-closed (returns empty) |
| Service account / scheduled job | Configurable: set `X-GitHub-Login` in dispatch | ✅ if configured |

**When #1319 ships:** The chat UI path can resolve `cognito_sub` → GitHub login
via the gateway's `/internal/v1/resolve-user` endpoint (which already returns
linked GitHub identities). The Door would then call this resolution before
filtering. Until #1319 ships, chat-UI users cannot search private code (they
see only `*` / public repos). This is acceptable-degraded, not a leak.

---

## 8. Failure Modes and Mitigations

| Failure | Behavior | Mitigation |
|---------|----------|------------|
| Postgres unreachable at query time | `get_allowed_repos` throws → filter returns empty (fail-closed) | Wrap in try/except, log error, return `[]` |
| GitHub API unreachable at ingest time | ACL derivation fails → `allowed_principals` stays as previous value (or `{}` for new repos) | Log warning; retry on next ingest cycle; never set `["*"]` on failure |
| GitHub App token missing `read:org` | Teams call returns 403 → teams not included in principals | Log warning; proceed with user-only ACL (safe: some legitimate access denied) |
| S3 Vectors metadata filter wrong | Over- or under-fetch from S3 Vectors | Door post-filter catches any leak (defense in depth) |
| `allowed_principals` column is `{}` (empty) | Repo invisible to all callers | Correct behavior for uninitialized repos; resolved on next successful ingest |
| Principal header spoofed | Attacker sets `X-GitHub-Login` | Headers set by trusted dispatch layer (in-cluster NetworkPolicy); same trust model as `X-Owner-Sub` |

---

## 9. Migration Path

### Phase 1 (this issue): Foundation

1. Add `allowed_principals` column to `repositories` table (schema migration)
2. Add ACL derivation step to `ingest-repo.py`
3. Implement Door filter in MCP server query path
4. Unit tests for fail-closed invariant
5. Re-run ingestion to populate `allowed_principals` for all indexed repos

### Phase 2 (post #1319): Chat UI access

1. Resolve `cognito_sub` → GitHub identity at query time
2. Chat-UI users gain access to private code search (filtered by their GitHub permissions)

### Phase 3 (future): Fine-grained per-branch/per-path ACL

Not in scope. GitHub's permission model is repo-grain; we mirror that. Branch
protection and CODEOWNERS are enforcement at push time, not read time.

---

## 10. File-Level Changes

### New files

| File | Purpose |
|------|---------|
| `modules/agent-context/door/acl.py` | Door ACL filter: `CallerPrincipal`, `filter_results()`, `get_allowed_repos()` |
| `modules/agent-context/door/__init__.py` | Package init |
| `modules/agent-context/tests/unit/test_acl_filter.py` | Unit tests for fail-closed + filter correctness |

### Modified files

| File | Change |
|------|--------|
| `modules/agent-context/images/ingestion/ingest-repo.py` | Add Step 4: `derive_acl()` function + Postgres upsert |
| MCP server query handler (location TBD — depends on where the Door server lives when built) | Wire `filter_results()` into search/understand/impact response paths |

### Schema (separate sub-issue, referenced)

| File | Change |
|------|--------|
| `migrations/001_repositories.sql` (or Alembic in agent-context) | `CREATE TABLE repositories` with `allowed_principals TEXT[] NOT NULL DEFAULT '{}'` + GIN index |

---

## 11. Test Specifications

### Unit tests (`test_acl_filter.py`)

| Test | Asserts |
|------|---------|
| `test_none_principal_returns_empty` | `filter_results(hits, None) == []` |
| `test_empty_login_and_teams_returns_empty` | `filter_results(hits, CallerPrincipal("", [])) == []` |
| `test_hit_from_allowed_repo_passes` | Hit with `repo=X` passes when `X` is in allowed set |
| `test_hit_from_disallowed_repo_dropped` | Hit with `repo=Y` dropped when `Y` not in allowed set |
| `test_public_repo_visible_to_all` | Hit from repo with `["*"]` in principals passes for any caller |
| `test_empty_principals_denies_all` | Repo with `allowed_principals=[]` → hits dropped for every caller |
| `test_team_membership_grants_access` | Caller in team `org/team-x` can see repo where `org/team-x` is a principal |
| `test_mixed_public_and_private_results` | Public hits pass, private hits without access are dropped |
| `test_postgres_failure_returns_empty` | When DB raises, filter returns `[]` (fail-closed, not fail-open) |
| `test_acl_derivation_public_repo` | Public repo → `["*"]` |
| `test_acl_derivation_private_repo` | Private repo → list of user logins + team slugs |
| `test_acl_derivation_api_failure_preserves_previous` | API failure → previous principals kept (not overwritten with empty) |

### Integration tests

| Test | Asserts |
|------|---------|
| `test_two_principals_cross_isolation` | Principal A searches, sees only repos A can access; Principal B searches same query, sees only B's repos |
| `test_unauthorized_principal_zero_results` | Token known to exist in restricted repo → zero results for unauthorized caller |
| `test_authorized_principal_finds_token` | Same token → hit returned for authorized caller |
| `test_public_repos_always_searchable` | Public repos visible regardless of caller identity |

---

## 12. Corrections to Design of Record

### OSV-Scanner does NOT consume CycloneDX SBOMs as input

The design document (§7.2.5) implies OSV-Scanner is chained after Syft:
"Match SBOM → known vulnerabilities (packages)". In reality:

- **Syft** generates the SBOM (CycloneDX format) — used as the bill-of-materials
  record in S3 and for the reverse-dependency index in Postgres.
- **OSV-Scanner** scans lockfiles directly (package-lock.json, go.mod,
  Cargo.lock, requirements.txt, etc.) — it does NOT accept a CycloneDX or SPDX
  file as input. It queries the OSV database using the lockfile contents.

**Impact on this issue:** None (this issue is about ACL, not SBOM). But the
ingest pipeline design should note that Syft and OSV-Scanner are parallel
consumers of the cloned repo, not a serial chain.

**Recommendation:** Update §7.2.5 to say:
> "OSV-Scanner scans the repo's lockfiles directly against the OSV database
> (it does not consume the Syft-generated SBOM). Both run on the same clone."

---

## 13. Fact Verification Summary

| Fact from design | Verified | Source |
|------------------|----------|--------|
| S3 Vectors GA | ✅ | docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors.html (2026-06-11) |
| S3 Vectors 2,500 writes/s/index limit | ✅ Exact match | docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-limitations.html |
| S3 Vectors max dimensions: 4,096 | ✅ | Same source |
| S3 Vectors filterable metadata: 2 KB | ✅ | Same source |
| Mountpoint for S3 GA + write-once semantics | ✅ | github.com/awslabs/mountpoint-s3 |
| Mountpoint CSI driver exists | ✅ v2.6.0 | github.com/awslabs/mountpoint-s3-csi-driver |
| OSV-Scanner Apache-2.0 | ✅ | github.com/google/osv-scanner |
| OSV-Scanner consumes CycloneDX SBOM | ❌ INCORRECT | google.github.io/osv-scanner/supported-languages-and-lockfiles/ — reads lockfiles, not SBOMs |
| Trivy Apache-2.0 + OS/base-image | ✅ | github.com/aquasecurity/trivy |
| PostgreSQL 16 EOL Nov 2028 | ✅ | postgresql.org/support/versioning/ — EOL 2028-11-09 |
