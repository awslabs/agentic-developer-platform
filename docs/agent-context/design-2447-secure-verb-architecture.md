# Technical Design: `secure` MCP Verb Implementation

**Issue:** #2447 (architect design, from product story #2437)
**Status:** Architecture design
**Author:** @agent-architect
**Last updated:** 2026-06-30
**Product story:** `docs/agent-context/design-2437-secure-verb-product-story.md`

---

## 1. Overview

The `secure` verb joins four existing data sources at query time to produce
reachability-scored vulnerability findings:

```
SBOM (S3 CycloneDX) ─┐
                      │
Dependencies (PG)  ───┼──▶  Data-Join Layer  ──▶  Prioritizer  ──▶  Response
                      │         (new)                (new)
Vulnerabilities (PG) ─┘
                      
Neptune (SCIP graph) ──▶  Reachability Scorer  ──┘
                              (new)
Zoekt (code search) ───▶  Import Locator  ─────┘
                              (new)
```

**No new tables, no new infrastructure.** The verb is a query-time composition
of existing indexed data. The only new code lives in `door/` (the MCP query
layer).

---

## 2. Data-Join Layer

### 2.1 Input Resolution

The first step resolves user input to a set of `(repo_name, package_purl, version, cve_id)` tuples.

```python
# door/secure_backend.py — new module

async def resolve_input(
    *,
    cve: str | None,
    repo: str | None,
    package: str | None,
    db_pool,
    caller: CallerPrincipal,
    acl_store: ACLStore | None,
) -> list[AffectedTuple]:
    """Resolve input params to affected (repo, package, version, cve) tuples.
    
    Resolution paths:
    1. cve given → query vulnerabilities table → get (package, affected_versions)
       → reverse-lookup dependencies table for repos with that package+version
    2. repo given → query dependencies for that repo → match against vulnerabilities
    3. package given → query dependencies for that package → match against vulnerabilities
    4. Multiple inputs → intersection of the above
    """
```

### 2.2 Core Join Queries

**Path 1: CVE → packages → repos**

```sql
-- Step 1: Find affected packages for a CVE
SELECT package, affected_versions, safe_version, severity, details
FROM vulnerabilities
WHERE cve_id = %(cve_id)s;

-- Step 2: Find repos using the affected package+version
-- The package_coordinate column is a purl (e.g., "pkg:pypi/requests@2.25.0")
-- We need to match on package name prefix and check version range
SELECT r.repo_name, d.package_coordinate, d.version, d.repo_id
FROM dependencies d
JOIN repositories r ON r.id = d.repo_id
WHERE d.package_coordinate LIKE %(purl_prefix)s
  AND d.version IS NOT NULL;
```

**Path 2: repo → packages → vulns**

```sql
-- Get all dependencies for a repo, then match against known vulns
SELECT d.package_coordinate, d.version, v.cve_id, v.severity, 
       v.affected_versions, v.safe_version, v.details
FROM dependencies d
JOIN repositories r ON r.id = d.repo_id
LEFT JOIN vulnerabilities v ON (
    -- Join condition: package name matches and version is in affected range
    -- NOTE: version range matching must be done in Python (semver/PEP440)
    -- because Postgres can't evaluate ">=2.0.0,<2.28.1" constraints
    v.package LIKE d.package_coordinate || '%'
    OR d.package_coordinate LIKE 'pkg:' || v.package || '@%'
)
WHERE r.repo_name = %(repo_name)s
  AND v.cve_id IS NOT NULL;
```

### 2.3 Version Range Matching

The `vulnerabilities.affected_versions` column stores range constraints like
`>=2.0.0,<2.28.1`. The Postgres JOIN above over-fetches (package name match
only); **version range evaluation happens in Python**:

```python
# door/secure_backend.py

from packaging.version import Version  # for PyPI
# Additional: semver for npm, Go module version comparison

def version_in_range(version: str, range_spec: str, ecosystem: str) -> bool:
    """Check if a version satisfies an affected-versions range.
    
    Ecosystem-specific parsing:
    - pypi: PEP 440 (packaging.version.Version)
    - npm: node-semver ranges
    - go: Go module pseudo-versions
    - fallback: string equality (conservative — matches everything)
    """
```

**Design decision:** Version matching in Python (not SQL) because:
1. Range specs are ecosystem-specific (semver vs PEP440 vs Go)
2. The vuln table stores human-readable ranges, not database-queryable constraints
3. Over-fetch + Python filter is acceptable at the expected scale (hundreds of deps per repo, not millions)

### 2.4 ACL Integration

All repo-scoped results pass through the existing ACL filter:

```python
# After resolving affected tuples, filter by caller's allowed repos
allowed_repos = acl_store.get_allowed_repos(caller)
filtered_tuples = [t for t in affected_tuples if t.repo_name in allowed_repos]
```

This reuses `door/acl.py`'s `PostgresACLStore.get_allowed_repos()` — same
fail-closed semantics as all other verbs. Repos the caller can't see simply
don't appear in results (no error, no leak).

---

## 3. Reachability Scoring

### 3.1 Four-Level Queries

For each affected `(repo, package)` pair, determine reachability level:

| Level | Label | How Determined |
|-------|-------|----------------|
| 0 | `present` | Package exists in dependencies table (baseline — always true by construction) |
| 1 | `imported` | Zoekt finds `import`/`require` statement for the package in the repo |
| 2 | `called` | Neptune: a Symbol from the package has an inbound CALLS edge from application code |
| 3 | `reachable` | Neptune: bounded path (1..4 hops) from an entry-point Symbol to the package's Symbol |

### 3.2 Level 1: Import Detection (Zoekt)

```python
# door/secure_backend.py

async def check_import_level(
    package_name: str,
    repo_name: str,
    ecosystem: str,
    zoekt_backend: SearchBackend,
) -> list[UsageSite]:
    """Find import/require statements for a package in a repo.
    
    Zoekt query patterns by ecosystem:
    - npm:  "import.*from.*'{package}'" or "require('{package}')"
    - pypi: "import {package}" or "from {package}"
    - go:   '"{module_path}"'
    """
    # Build ecosystem-specific import regex
    query = _build_import_query(package_name, ecosystem)
    hits = await zoekt_backend.search(query, repo_ids=[repo_name], limit=50)
    return [UsageSite(file=h.data["file"], line=h.data["line"], 
                      symbol=package_name, reachability_level="imported")
            for h in hits]
```

### 3.3 Level 2–3: Neptune Reachability

```python
# door/secure_backend.py

def query_package_reachability(
    repo: str,
    package_symbols: list[str],  # Symbol names from the package
    *,
    tenant_id: str | None = None,
) -> dict[str, ReachabilityResult]:
    """Query Neptune for symbol-level reachability.
    
    Level 2 (called): Any inbound CALLS edge to the package's symbol
    Level 3 (reachable): Bounded path from entry point to package symbol
    """
```

**Level 2 — "called" query:**

```cypher
-- Find application symbols that CALL a symbol from the vulnerable package
MATCH (caller:Symbol {repo: $repo})-[:CALLS]->(target:Symbol)
WHERE target.module STARTS WITH $package_module
  AND caller.module <> target.module  -- exclude internal package calls
RETURN caller.name AS caller_name, caller.file AS caller_file,
       target.name AS target_name, target.file AS target_file
LIMIT 50
```

**Level 3 — "reachable" query:**

```cypher
-- Find a path from an entry point to the vulnerable symbol (bounded 4 hops)
-- Entry points: symbols with kind IN ['function', 'method'] AND 
--   (file matches entry-point patterns OR has zero inbound CALLS edges)
MATCH (entry:Symbol {repo: $repo})
WHERE entry.kind IN ['function', 'method']
  AND NOT exists((other:Symbol)-[:CALLS]->(entry))  -- no callers = entry point
WITH entry
MATCH path = (entry)-[:CALLS*1..4]->(target:Symbol)
WHERE target.module STARTS WITH $package_module
RETURN entry.name AS entry_name, entry.file AS entry_file,
       target.name AS target_name, target.file AS target_file,
       length(path) AS distance
ORDER BY distance ASC
LIMIT 20
```

### 3.4 Entry-Point Definition

For level-3 (reachable), "entry point" means:

1. **Primary heuristic:** Symbols with **zero inbound CALLS edges** in the
   call graph (they're never called by other code in the repo — so they must
   be called externally, i.e., they're API handlers, main functions, etc.)
2. **Secondary heuristic (fallback):** Symbols in files matching known
   entry-point patterns:
   - `**/main.{py,ts,go,java}`
   - `**/app.{py,ts}`
   - `**/handler.{py,ts}`
   - `**/api/**`, `**/routes/**`, `**/endpoints/**`
   - `**/cmd/**` (Go)
   - `**/__main__.py`

### 3.5 Fail-Safe: Default to Reachable

When Neptune is unavailable (5-second timeout) or the symbol isn't in the graph:

```python
NEPTUNE_TIMEOUT_SECONDS = 5.0

async def determine_reachability(...) -> ReachabilityResult:
    try:
        result = await asyncio.wait_for(
            _query_neptune_reachability(...),
            timeout=NEPTUNE_TIMEOUT_SECONDS,
        )
        return result
    except (asyncio.TimeoutError, NeptuneQueryError):
        # Fail-safe: assume reachable (false positive > false negative)
        return ReachabilityResult(
            level="reachable",
            confidence="assumed",
            source="timeout-fallback",
        )
```

### 3.6 Degraded Mode (Neptune Unavailable)

If `neptune_available()` returns False at query start:
- Skip levels 2–3 entirely
- Use Zoekt import search for level 1
- Tag all findings with `"reachability_confidence": "degraded"` and
  `"reachability_source": "code-index-fallback"`
- Default undetermined symbols to `reachable: true` (fail-safe)

---

## 4. Verb Handler Registration

### 4.1 Tool Definition (TOOLS constant in server.py)

```python
# Added to TOOLS list in door/server.py
{
    "name": "secure",
    "description": (
        "Identify, locate, and plan remediation for vulnerabilities. "
        "Given a CVE, repo, or package, returns reachability-scored findings "
        "with remediation guidance. The security entry point."
    ),
    "parameters": {
        "cve": {"type": "string", "required": False},
        "repo": {"type": "string", "required": False},
        "package": {"type": "string", "required": False},
        "action": {
            "type": "string",
            "enum": ["identify", "plan", "verify"],
            "required": False,
        },
        "severity_min": {
            "type": "string",
            "enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW"],
            "required": False,
        },
        "reachable_only": {"type": "boolean", "required": False},
        "project": {"type": "string", "required": False},
    },
}
```

### 4.2 Dispatch Integration (server.py)

```python
# In _dispatch_tool():
elif name == "secure":
    return await _handle_secure(arguments, caller, project_scope, headers=headers)
```

### 4.3 MCP Shim (mcp_app.py)

```python
@mcp_server.tool(name="secure")
async def mcp_secure(
    cve: str = "",
    repo: str = "",
    package: str = "",
    action: str = "identify",
    severity_min: str = "",
    reachable_only: bool = False,
    project: str = "",
    ctx: Context = None,
) -> str:
    """Identify, locate, and plan remediation for vulnerabilities."""
    headers = _get_headers_from_context(ctx)
    caller = extract_caller_principal(headers)
    arguments = {k: v for k, v in {
        "cve": cve, "repo": repo, "package": package,
        "action": action, "severity_min": severity_min,
        "reachable_only": reachable_only, "project": project,
    }.items() if v}
    dispatch = _get_dispatch_tool()
    result = await dispatch("secure", arguments, headers, caller)
    return json.dumps(result, default=str)
```

### 4.4 ACL Gotcha (psycopg2 `%s` + jsonb operators)

The existing `PostgresACLStore._get_allowed_repos_scoped()` uses `?` and `?|`
jsonb operators that conflict with psycopg2's `%s` parameter syntax. This was
recently fixed (referenced in #2447 issue body). The `secure` verb handler
**reuses the same ACL store** — it calls `acl_store.get_allowed_repos(caller)`
and filters in Python, avoiding any new SQL that could hit this gotcha.

New SQL queries in `secure_backend.py` use only `%s` parameters (no jsonb
operators) because they query `dependencies` and `vulnerabilities` tables
which use standard column types, not jsonb arrays.

---

## 5. Handler Architecture

### 5.1 Module Layout

```
door/
  secure_backend.py     # NEW — core data-join + reachability logic
  server.py             # MODIFIED — add _handle_secure, add to TOOLS
  mcp_app.py            # MODIFIED — add mcp_secure shim
  config.py             # MODIFIED — add NEPTUNE_REACHABILITY_TIMEOUT
```

### 5.2 `_handle_secure` Handler (server.py)

```python
async def _handle_secure(
    arguments: dict[str, Any],
    caller: CallerPrincipal | None,
    project_scope: ProjectScope | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Handle the secure verb: vulnerability identification + reachability scoring.
    
    Sub-actions:
    - identify (default): Full assessment — what's wrong, where, how bad
    - plan: Remediation steps for a specific (cve, repo) pair
    - verify: Post-fix re-check — is the vuln still present?
    """
    action = arguments.get("action", "identify")
    cve = arguments.get("cve", "")
    repo = arguments.get("repo", "")
    package = arguments.get("package", "")
    
    # Input validation
    if not cve and not repo and not package:
        return {"error": "validation_error",
                "message": "At least one of cve, repo, or package is required"}
    if action == "plan" and (not cve or not repo):
        return {"error": "validation_error",
                "message": "action=plan requires both cve and repo parameters"}
    if action == "verify" and not cve:
        return {"error": "validation_error",
                "message": "action=verify requires cve parameter"}
    
    # Dispatch to sub-action handler
    from .secure_backend import (
        handle_identify, handle_plan, handle_verify,
    )
    
    if action == "identify":
        return await handle_identify(cve=cve, repo=repo, package=package,
                                     arguments=arguments, caller=caller,
                                     project_scope=project_scope)
    elif action == "plan":
        return await handle_plan(cve=cve, repo=repo, caller=caller,
                                 project_scope=project_scope)
    elif action == "verify":
        return await handle_verify(cve=cve, repo=repo, caller=caller,
                                   project_scope=project_scope)
    else:
        return {"error": "validation_error",
                "message": f"Unknown action: {action}"}
```

### 5.3 Backend Module Structure (`secure_backend.py`)

```python
"""Secure verb backend — data-join, reachability, prioritization.

Joins SBOM (S3) + dependencies (PG) + vulnerabilities (PG) + Neptune graph
+ Zoekt code search to produce reachability-scored vulnerability findings.

No new tables or infrastructure. All data already exists from the ingestion
pipeline; this module is a query-time composition layer.
"""

# --- Data types ---

@dataclass
class AffectedTuple:
    repo_name: str
    repo_id: str
    package_purl: str
    package_name: str
    package_version: str
    package_ecosystem: str
    cve_id: str
    severity: str
    cvss_score: float | None
    fixed_version: str | None
    affected_versions: str

@dataclass
class UsageSite:
    file: str
    line: int
    symbol: str
    reachability_level: str  # present | imported | called | reachable
    callers: list[dict]  # [{file, line, symbol, distance}]

@dataclass
class ReachabilityResult:
    level: str  # present | imported | called | reachable
    confidence: str  # high | degraded | assumed
    source: str  # neptune | code-index-fallback | timeout-fallback

@dataclass
class Finding:
    cve_id: str
    severity: str
    cvss_score: float | None
    package: str
    ecosystem: str
    affected_version: str
    fixed_version: str | None
    purl: str
    priority: str  # P0 | P1 | P2 | P3
    priority_score: float
    repos_affected: list[str]
    reachability: ReachabilityResult
    usage_sites: list[UsageSite]
    remediation: dict

# --- Public API ---

async def handle_identify(...) -> dict[str, Any]: ...
async def handle_plan(...) -> dict[str, Any]: ...
async def handle_verify(...) -> dict[str, Any]: ...

# --- Internal ---

async def resolve_input(...) -> list[AffectedTuple]: ...
async def determine_reachability(...) -> ReachabilityResult: ...
async def find_usage_sites(...) -> list[UsageSite]: ...
def compute_priority_score(...) -> tuple[float, str]: ...
def check_version_in_range(...) -> bool: ...
```

---

## 6. Prioritization Engine

### 6.1 Scoring Model

```python
def compute_priority_score(
    severity: str,
    reachability_level: str,
    fix_available: bool,
) -> tuple[float, str]:
    """Compute priority score and bucket.
    
    score = severity_weight * reachability_weight * fix_weight
    
    Returns (score, bucket) where bucket is P0/P1/P2/P3.
    """
    SEVERITY_WEIGHTS = {
        "CRITICAL": 1.0, "HIGH": 0.7, "MEDIUM": 0.4, "LOW": 0.1
    }
    REACHABILITY_WEIGHTS = {
        "reachable": 1.0, "called": 0.8, "imported": 0.5, "present": 0.2
    }
    FIX_WEIGHTS = {True: 1.0, False: 0.3}
    
    score = (
        SEVERITY_WEIGHTS.get(severity, 0.1) *
        REACHABILITY_WEIGHTS.get(reachability_level, 1.0) *  # default=1.0 (fail-safe)
        FIX_WEIGHTS.get(fix_available, 0.3)
    )
    
    if score >= 0.7:
        bucket = "P0"
    elif score >= 0.4:
        bucket = "P1"
    elif score >= 0.1:
        bucket = "P2"
    else:
        bucket = "P3"
    
    return (score, bucket)
```

### 6.2 Result Ordering

Findings are sorted by `priority_score` descending. Within the same score,
secondary sort is by `severity` (CRITICAL > HIGH > MEDIUM > LOW), then by
`cve_id` alphabetically for determinism.

---

## 7. SBOM Freshness

### 7.1 How SBOM Age Is Determined

The SBOM lives at `s3://{bucket}/sbom/repos/{org}/{repo}/source.cdx.json`.
The S3 object's `LastModified` metadata gives the generation timestamp.

```python
async def get_sbom_age_hours(repo_name: str, s3_client) -> float | None:
    """Get SBOM age in hours from S3 object metadata.
    
    Returns None if SBOM doesn't exist for this repo.
    """
    key = f"sbom/repos/{repo_name}/source.cdx.json"
    try:
        response = s3_client.head_object(Bucket=config.s3_bucket, Key=key)
        last_modified = response["LastModified"]
        age = datetime.now(timezone.utc) - last_modified
        return age.total_seconds() / 3600
    except s3_client.exceptions.NoSuchKey:
        return None
```

### 7.2 Staleness Warning

If `sbom_age_hours > 24`, the response includes:
```json
{"metadata": {"sbom_age_hours": 48.5, "sbom_stale_warning": true}}
```

### 7.3 Vuln Scanner Freshness

The vulnerability scanner (`pipeline/vuln_scanner/`) currently runs as part of
the ingestion pipeline (`ingest-repo.py` stage 5b). It runs when:
- A repo is first indexed
- The daily refresh CronJob detects a changed SHA
- A manual re-index is triggered

**Current gap:** There is no independent scheduled vuln-DB refresh. The scanner
uses OSV-Scanner's local DB which updates on each run, but stale repos (no code
change) won't get re-scanned against new advisories. This is acceptable for v1
(the `metadata.vuln_db_last_sync` field surfaces this to callers), but a future
enhancement should add a periodic vuln-only re-scan.

---

## 8. Dependencies and Blockers

### 8.1 Neptune Wiring (#2433) — HARD dependency for levels 2–3

The SCIP→Neptune loading pipeline exists (`scip_neptune_loader.py`) and is
proven (SPIKE-3: 521 nodes + 596 edges loaded). However, it requires:
1. `GRAPHRAG_ENABLED=true` in the deployment
2. Neptune Serverless instance provisioned
3. At least one repo indexed with SCIP data loaded into Neptune

**Without #2433 resolved**, the `secure` verb will operate in **degraded mode**
for all queries — reachability defaults to level 0/1 (present/imported via Zoekt)
with `"confidence": "degraded"`. This is functional (all acceptance criteria
except AC-2 and AC-6's positive path work without Neptune), but the key
differentiator (reachability scoring) won't be live.

**Mitigation:** The verb ships independently of #2433 and gracefully degrades.
Tests cover both Neptune-available and Neptune-unavailable paths.

### 8.2 Existing Infrastructure — No New Dependencies

| Component | Status | Used By `secure` |
|-----------|--------|------------------|
| `dependencies` table (PG) | Live, populated by ingestion | Reverse lookup: which repos have package X |
| `vulnerabilities` table (PG) | Live, populated by vuln scanner | CVE details, affected versions, fix versions |
| SBOM (S3) | Live, generated per-repo | Age metadata, direct CycloneDX parsing for verify |
| Zoekt | Live | Import detection (level 1 reachability) |
| Neptune | Requires #2433 | Level 2–3 reachability (graceful degradation) |
| ACL store (PG) | Live | Same repo-level filtering as other verbs |

---

## 9. Test Strategy

### 9.1 Fixture-Based Tests (NOT SQL-Skipping Mocks)

Per lessons from #2272/#2281, tests must exercise the actual SQL queries
against a real (or realistically-shaped) database, not mock the DB layer away.

**Approach:** Use pytest fixtures that seed a SQLite database (same schema as
`test_migrations.py` uses) with known dependency + vulnerability rows, then
run the actual Python functions against them.

### 9.2 Test Categories

| Category | What | Where |
|----------|------|-------|
| Unit — prioritization | Score computation, version range matching | `tests/unit/test_secure_priority.py` |
| Unit — input resolution | CVE→packages, repo→vulns, package→repos | `tests/unit/test_secure_resolve.py` |
| Unit — reachability | Neptune query construction, degraded mode | `tests/unit/test_secure_reachability.py` |
| Integration — handler | Full `_handle_secure` with fixture DB + fake Zoekt | `tests/integration/test_secure_handler.py` |
| Live — e2e | Query context-mcp on a deployed cluster | `tests/e2e/test_secure_e2e.py` |

### 9.3 Fixture Data Requirements

```python
# Minimum fixture set for integration tests:
# - 3 repos (org/service-a, org/service-b, org/lib-internal)
# - 5 dependencies (requests@2.25.0, lodash@4.17.0, flask@3.0.0, etc.)
# - 2 vulnerabilities (CVE-2023-32681 for requests, CVE-2025-1234 for lodash)
# - 1 repo with stale SBOM (>24h)
# - ACL: caller can see service-a and service-b, NOT lib-internal
```

---

## 10. Response Latency Budget

Target: p95 < 5s for single-CVE single-repo query.

| Step | Budget | Rationale |
|------|--------|-----------|
| Input resolution (PG) | 50ms | 2 indexed queries on small tables |
| Version range matching (Python) | 10ms | In-memory filter over ~100 deps |
| Zoekt import search | 200ms | Single regex query |
| Neptune reachability | 2000ms | Variable-length path query (bounded 4 hops) |
| Prioritization + formatting | 10ms | In-memory computation |
| **Total** | **~2300ms** | Well within 5s budget |

Neptune timeout: 5s hard cutoff. If Neptune exceeds this, degraded mode
kicks in and total response time stays under 5.5s.

---

## 11. Child Issue Breakdown

See filed issues. Dependency order:

```
#1: Data-join layer (foundational — no deps)
    ↓
#2: Reachability scorer (needs #1 for affected packages to query)
    ↓
#3: secure verb handler + MCP registration (needs #1, #2)
    ↓                          ↓
#4: Prioritization engine    #5: action=plan logic
    (needs #1, #2)              (needs #3)
                               ↓
                             #6: action=verify logic
                               (needs #3)
    ↓
#7: Integration tests (needs #3–#6)
    ↓
#8: SBOM freshness metadata (needs #1)
```

---

## 12. Open Decisions (Resolved)

| Question | Decision | Rationale |
|----------|----------|-----------|
| SBOM refresh on verify | Check last-indexed only | Fresh generation is 30-60s; too slow for verb response. Surface `sbom_age_hours` so caller can decide. |
| Cross-repo CVE query limit | 50 repos max | Prevents unbounded queries; ACL already limits visible repos. Add pagination later. |
| Transitive dep reporting | Yes (if in SBOM) | Syft includes transitives; reachability stays at "present" unless graph proves otherwise. |
| Breaking-change intelligence | Semver analysis for v1 | Major bump = high risk, minor = low, patch = none. "unknown" when metadata insufficient. |
| Version range library | `packaging` (PyPI) + custom semver | Avoid heavy npm-semver port; handle top ecosystems, fallback to string equality. |

---

## 13. Reuse Verification

Every integration point below already exists and is tested:

| Capability | Location | Verified By |
|-----------|----------|-------------|
| ACL filtering | `door/acl.py` → `PostgresACLStore` | `tests/test_acl.py`, `tests/integration/test_cross_tenant_isolation.py` |
| Zoekt search | `door/search_backend.py` → `ZoektSearchBackend` | `tests/test_search_backend.py` |
| Neptune queries | `door/neptune_client.py` → `query_impact()` | `tests/test_neptune_client.py` |
| Dependencies table | `alembic/versions/001_*` | `tests/test_migrations.py` |
| Vulnerabilities table | `alembic/versions/001_*` | `tests/test_migrations.py` |
| SBOM parsing | `images/ingestion/sbom_parser.py` | `tests/unit/test_sbom_parser.py` |
| Vuln normalization | `pipeline/vuln_scanner/normalize.py` | `tests/test_vuln_normalize.py` |
| Verb dispatch pattern | `door/server.py` → `_dispatch_tool()` | `tests/test_server.py` |
| MCP shim pattern | `door/mcp_app.py` → `@mcp_server.tool` | `tests/test_mcp_app.py` |
| Project filter | `door/project_filter.py` | `tests/test_project_filter.py` |

---

## 14. Security Considerations

1. **ACL enforcement** — Same fail-closed pattern as all other verbs. No
   vulnerability data is returned for repos the caller can't see.
2. **No CVE enumeration** — An unknown CVE returns `{findings_count: 0}` with
   `cve_known: false`, NOT an error. This prevents using the verb to enumerate
   which CVEs are in the vuln DB.
3. **Rate limiting** — The `secure` verb should respect the same per-tenant
   rate limits as other verbs (tracked in `adp-dev-rate-limits` DynamoDB).
4. **SQL injection** — All queries use parameterized `%s` placeholders.
   No string interpolation of user input into SQL.
5. **Neptune injection** — All Cypher queries use `$param` parameters, never
   string interpolation. Same pattern as existing `query_impact()`.
