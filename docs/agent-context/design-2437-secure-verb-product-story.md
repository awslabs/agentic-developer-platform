# Product Story: `secure` MCP Verb — CVE Remediation Entry Point

**Status:** Product-enriched story (ready for architect breakdown)
**Issue:** #2437
**Last updated:** 2026-06-30
**Author:** @agent-product
**Companion docs:** `knowledge-layer-product-vision.md` (§4 "CVE-to-PR is autonomous"), `knowledge-layer-design.md` (§5 vuln loop)

---

## 1. Problem Statement

The Knowledge Layer already produces rich security data — CycloneDX SBOMs (415+ components/repo with purl+version), a vulnerability scanner generating `NormalizedVulnerability` records (CVE, package, affected/fixed version, severity, CVSS), a SCIP call graph in Neptune with 4-hop reachability, and Zoekt code search. **None of this is queryable by agents via MCP.**

Today, when CVE-2025-XXXX is published:
- Someone manually checks "are we affected?" repo-by-repo
- If affected, they figure out where the vulnerable package is used
- They have no reachability signal — no way to know if the vulnerability is exercised vs. merely present
- They manually plan the fix, change the files, hope they didn't miss a transitive dependency

The `secure` verb closes this gap: it turns a CVE identifier (or a repo, or a package name) into a **prioritized, reachability-scored remediation brief** that an autonomous agent can act on immediately — or that a human can use for instant triage.

---

## 2. Personas

### Persona A: The Autonomous Remediation Agent ("SecBot")

**Who:** An ADP developer-agent triggered by the vulnerability detection loop (test_vuln_loop.py triage gate). It receives a CVE + affected repo and must produce a tested, reviewable PR.

**Journey:**
1. Triage loop fires: "CVE-2025-1234 affects `org/payments-api` via lodash@4.17.0"
2. Agent calls `secure(cve="CVE-2025-1234", repo="org/payments-api", action="identify")`
3. Receives: severity, reachable usage sites, fix target version, files to change, breaking-change risk
4. Calls `understand(target="org/payments-api::deepMerge")` to learn how the vulnerable function is used locally
5. Plans the fix (bump version, update imports if API changed)
6. Makes the change, runs tests
7. Calls `secure(cve="CVE-2025-1234", repo="org/payments-api", action="verify")` to confirm the vuln is resolved
8. Opens a PR with the fix + verification evidence

**What they need from `secure`:** Enough structured context to skip triage entirely and jump to fixing — specific files, lines, symbols, and the target version. The "start here" pointer.

### Persona B: The Human Triager ("Alex the Security Engineer")

**Who:** A security engineer who sees a Dependabot alert or a new advisory and wants to know: "How bad is this for us? Where should I start?"

**Journey:**
1. Alex sees advisory for CVE-2025-1234 in their feed
2. Asks the agent (via chat or CLI): "Are we exposed to CVE-2025-1234?"
3. Agent calls `secure(cve="CVE-2025-1234")`
4. Receives: N repos affected, but only M have reachable paths — severity downgraded from CRITICAL (CVSS 9.8) to HIGH (reachable only via test code) for 2 repos, stays CRITICAL for 1 repo where it's in the hot path
5. Alex prioritizes the one truly-critical repo, delegates the rest to the autonomous agent

**What they need from `secure`:** A prioritized list — not just "you have it" but "here's where it actually matters, ranked." The reachability signal is the differentiator.

### Persona C: The Compliance Auditor ("Morgan")

**Who:** A compliance officer preparing for SOC2/FedRAMP audit who needs to prove exposure posture for specific advisories.

**Journey:**
1. Morgan asks: "Show me all repos affected by any CRITICAL CVE with a fix available that we haven't patched"
2. Agent calls `secure(action="identify")` with severity filter (future: batch/audit mode)
3. Receives: Complete exposure report — N findings, grouped by severity, with fix availability and reachability confidence
4. Exports to compliance tooling

**What they need from `secure`:** Exhaustive coverage (not just one CVE), structured output suitable for compliance reporting, and confidence levels they can defend in an audit.

---

## 3. The Identify → Locate → Fix → Verify Boundary

The `secure` verb is the **orchestration entry point**, not the full remediation engine. It provides the intelligence; other verbs and agent capabilities do the work.

### Boundary Definition

| Phase | What `secure` does | What other verbs/capabilities do |
|-------|-------------------|----------------------------------|
| **Identify** | Joins SBOM + vuln DB + reachability → returns structured findings with severity, affected repos, fix target | — |
| **Locate** | Returns exact usage sites (file:line:symbol) with reachability flag and blast-radius summary | `search(scope=code)` for exhaustive usage enumeration; `impact(target=symbol)` for full caller tree |
| **Fix** | Returns remediation guidance: target version, files to change, breaking-change risk, "start here" pointer | Agent's own code-editing capability; `understand(target=file)` for context |
| **Verify** | Re-runs the identify+locate check post-fix; confirms vuln no longer detected or no longer reachable | Agent runs tests; `secure(action=verify)` confirms clean state |

### Key Design Decision: `secure` Plans, Agents Execute

The `secure` verb does NOT:
- Open PRs
- Edit code
- Run tests
- Install packages

It returns a **remediation brief** — a structured data object that tells an agent (or human) everything it needs to execute the fix. The verb is read-only and idempotent. This keeps it composable: the same output works for an autonomous agent, a human triager, or a compliance dashboard.

### Sub-actions (within `secure`, not separate verbs)

`action` is a parameter of `secure`, not a separate verb. Rationale: all three sub-actions operate on the same data (SBOM + vuln + callgraph join); separating them would duplicate the join logic and fragment the tool surface.

| Action | Purpose | When to use |
|--------|---------|-------------|
| `identify` (default) | Full assessment: what's wrong, where, how bad, what to fix | Starting point for any security query |
| `plan` | Remediation steps: specific commands, file changes, verification instructions | After identify confirms action is needed; used by agents to structure their work |
| `verify` | Post-fix re-check: re-scans, confirms vuln resolved or unreachable | After agent applies fix, before opening PR |

---

## 4. Reachability Semantics

Reachability is the core differentiator of `secure` over a plain scanner. It answers: "yes the package is present, but does your code *actually use* the vulnerable function?"

### Confidence Levels

| Level | Label | Definition | How determined | Example |
|-------|-------|-----------|----------------|---------|
| 0 | `present` | Package appears in dependency tree (SBOM lists it) | SBOM + dependencies table has the purl | lodash is in package.json |
| 1 | `imported` | Package is imported/required in at least one source file | Zoekt finds `import`/`require` statement for the package | `import { merge } from 'lodash'` in merge.ts |
| 2 | `called` | A symbol from the vulnerable package is called | Neptune: Symbol from package has inbound CALLS edge from application code | `deepMerge(...)` call site in utils/merge.ts:42 |
| 3 | `reachable` | A call path exists from an application entry point to the vulnerable symbol (bounded at 4 hops) | Neptune: bounded variable-length path `(entry)-[:CALLS*1..4]->(vuln_symbol)` exists | `api/handler.ts:88 → utils/merge.ts:42 → lodash.merge` |

### Confidence Rules

- **Level 0** (present only): lowest priority. Package is there but never used — likely a transitive dep or unused lockfile entry.
- **Level 1** (imported): medium-low. Import exists but symbol may be unused (tree-shaking candidate) or only used in test/dev code.
- **Level 2** (called): medium-high. Code calls the symbol but we can't prove it's on a live path (may be dead code).
- **Level 3** (reachable): highest confidence. A connected path exists from application entry points. **This is what makes remediation urgent.**

### Degraded Mode

When Neptune is unavailable (5-second timeout, fallback to S3 code-index):
- Levels 2-3 cannot be determined (no call graph available)
- Response includes `"reachability_confidence": "degraded"` and `"reachability_source": "code-index-fallback"`
- Level 1 can still be determined via Zoekt import search
- The finding is NOT suppressed — it's reported at Level 1 with a degraded confidence flag

### Design Constraint: Fail-Safe, Not Fail-Silent

If reachability cannot be determined (Neptune down, symbol not indexed, new language without SCIP support):
- Default to `reachable: true` (assume worst case)
- Include `"reachability_confidence": "assumed"` in the response
- Rationale: a false positive is an unnecessary fix (low cost); a false negative is a missed vulnerability (high cost)

---

## 5. Prioritization Model

When multiple findings exist (e.g., `secure(repo="org/repo")`), results are ranked by a composite score:

```
priority_score = severity_weight × reachability_weight × fix_availability_weight
```

| Factor | Values | Weight |
|--------|--------|--------|
| **Severity** | CRITICAL=1.0, HIGH=0.7, MEDIUM=0.4, LOW=0.1 | Multiplicative |
| **Reachability** | reachable=1.0, called=0.8, imported=0.5, present=0.2 | Multiplicative |
| **Fix available** | yes (fixed_version known)=1.0, no=0.3 | Multiplicative |

### Ranking Examples

| CVE | Severity | Reachability | Fix Available | Score | Priority |
|-----|----------|-------------|---------------|-------|----------|
| CVE-A | CRITICAL (1.0) | reachable (1.0) | yes (1.0) | 1.00 | P0 — fix immediately |
| CVE-B | HIGH (0.7) | called (0.8) | yes (1.0) | 0.56 | P1 — fix soon |
| CVE-C | CRITICAL (1.0) | present (0.2) | yes (1.0) | 0.20 | P2 — low urgency (not reachable) |
| CVE-D | MEDIUM (0.4) | imported (0.5) | no (0.3) | 0.06 | P3 — monitor only |

### Priority Buckets

| Bucket | Score Range | Recommended Action |
|--------|-------------|-------------------|
| P0 | ≥ 0.7 | Immediate autonomous fix |
| P1 | 0.4 – 0.69 | Schedule fix within sprint |
| P2 | 0.1 – 0.39 | Backlog; monitor for reachability change |
| P3 | < 0.1 | Informational only; no action needed |

---

## 6. I/O Contract (Final)

### Input Schema

```json
{
  "name": "secure",
  "description": "Identify, locate, and plan remediation for vulnerabilities. Given a CVE, repo, or package, returns reachability-scored findings with remediation guidance. The security entry point — use before fixing a vulnerability.",
  "parameters": {
    "cve": {"type": "string", "required": false, "description": "CVE identifier (e.g., 'CVE-2025-1234'). Query: 'where do I fix this?'"},
    "repo": {"type": "string", "required": false, "description": "Repository identifier (e.g., 'org/repo-name'). Query: 'what vulns exist here?'"},
    "package": {"type": "string", "required": false, "description": "Package name (e.g., 'lodash'). Query: 'where is this used and what are its vulns?'"},
    "action": {"type": "string", "enum": ["identify", "plan", "verify"], "required": false, "description": "identify (default): assess + locate. plan: remediation steps. verify: post-fix re-check."},
    "severity_min": {"type": "string", "enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW"], "required": false, "description": "Filter findings to this severity or above. Default: all."},
    "reachable_only": {"type": "boolean", "required": false, "description": "If true, only return findings with reachability level >= 'called'. Default: false."},
    "project": {"type": "string", "required": false, "description": "Project scope filter (consistent with other verbs)."}
  }
}
```

**Validation rules:**
- At least one of `cve`, `repo`, or `package` MUST be provided (error if none)
- All three may be combined (intersection: "is this CVE present in this repo via this package?")
- `action=verify` requires `cve` (you verify a specific CVE was resolved)
- `action=plan` requires `cve` + `repo` (you plan a fix for a specific CVE in a specific repo)

### Output Schema — `action=identify` (default)

```json
{
  "summary": "2 vulnerabilities in org/payments-api: 1 CRITICAL (reachable), 1 HIGH (present only)",
  "query": {"cve": null, "repo": "org/payments-api", "package": null, "action": "identify"},
  "findings_count": 2,
  "findings": [
    {
      "cve_id": "CVE-2025-1234",
      "severity": "CRITICAL",
      "cvss": 9.8,
      "package": "lodash",
      "ecosystem": "npm",
      "affected_version": "4.17.0",
      "fixed_version": "4.17.21",
      "purl": "pkg:npm/lodash@4.17.0",
      "priority": "P0",
      "priority_score": 1.0,
      "repos_affected": ["org/payments-api"],
      "reachability": {
        "level": "reachable",
        "confidence": "high",
        "source": "neptune"
      },
      "usage_sites": [
        {
          "file": "src/utils/merge.ts",
          "line": 42,
          "symbol": "deepMerge",
          "reachability_level": "reachable",
          "callers": [
            {"file": "src/api/handler.ts", "line": 88, "symbol": "processRequest", "distance": 1}
          ]
        },
        {
          "file": "src/lib/transform.ts",
          "line": 15,
          "symbol": "transformConfig",
          "reachability_level": "imported",
          "callers": []
        }
      ],
      "remediation": {
        "fix_type": "version_bump",
        "target_version": "4.17.21",
        "files_to_change": ["package.json", "package-lock.json"],
        "start_here": "src/utils/merge.ts:42",
        "breaking_change_risk": "low",
        "notes": "No API changes between 4.17.0 and 4.17.21"
      }
    }
  ],
  "metadata": {
    "sbom_age_hours": 2.5,
    "vuln_db_last_sync": "2026-06-30T04:00:00Z",
    "reachability_source": "neptune",
    "repos_indexed": 1,
    "repos_requested": 1
  }
}
```

### Output Schema — `action=plan`

Same as `identify` but the `remediation` object is expanded:

```json
{
  "remediation": {
    "fix_type": "version_bump",
    "target_version": "4.17.21",
    "steps": [
      {"order": 1, "action": "update_dependency", "file": "package.json", "change": "lodash: ^4.17.0 → ^4.17.21"},
      {"order": 2, "action": "regenerate_lockfile", "command": "npm install"},
      {"order": 3, "action": "verify_import_compatibility", "target": "src/utils/merge.ts:42", "check": "deepMerge signature unchanged in 4.17.21"},
      {"order": 4, "action": "run_tests", "scope": "tests touching src/utils/merge.ts"},
      {"order": 5, "action": "verify_fix", "command": "secure(cve='CVE-2025-1234', repo='org/payments-api', action='verify')"}
    ],
    "breaking_change_risk": "low",
    "breaking_change_details": null,
    "estimated_complexity": "trivial",
    "files_to_change": ["package.json", "package-lock.json"],
    "files_to_verify": ["src/utils/merge.ts", "src/api/handler.ts"],
    "start_here": "src/utils/merge.ts:42"
  }
}
```

### Output Schema — `action=verify`

```json
{
  "summary": "CVE-2025-1234 resolved in org/payments-api",
  "query": {"cve": "CVE-2025-1234", "repo": "org/payments-api", "action": "verify"},
  "status": "resolved",
  "details": {
    "previous_version": "4.17.0",
    "current_version": "4.17.21",
    "fixed_version": "4.17.21",
    "vulnerability_still_present": false,
    "reachability_check": "not_applicable"
  },
  "metadata": {
    "sbom_age_hours": 0.1,
    "verified_at": "2026-06-30T14:30:00Z"
  }
}
```

**Verify status values:** `resolved` | `still_vulnerable` | `mitigated` (reachable paths removed but version unchanged) | `unknown` (SBOM stale or repo not indexed)

### Error Cases

| Condition | HTTP Status | Error Response |
|-----------|-------------|----------------|
| No input provided (no cve/repo/package) | 400 | `{"error": "validation_error", "message": "At least one of cve, repo, or package is required"}` |
| CVE not in vuln database | 200 | `{"findings_count": 0, "findings": [], "metadata": {"cve_known": false, "suggestion": "CVE not in vulnerability database. DB last synced: <timestamp>"}}` |
| Repo not indexed | 200 | `{"findings_count": 0, "findings": [], "metadata": {"repo_indexed": false, "suggestion": "Repository not indexed. Request indexing via browse(action='info', uri='/org/repo')"}}` |
| SBOM stale (>24h) | 200 | Normal response + `"metadata": {"sbom_age_hours": 48, "sbom_stale_warning": true}` |
| Neptune unavailable | 200 | Normal response with degraded reachability: `"reachability": {"confidence": "degraded", "source": "code-index-fallback"}` |
| ACL denied (caller can't see repo) | 200 | `{"findings_count": 0, "findings": []}` (fail-closed, same as other verbs) |
| `action=plan` without cve+repo | 400 | `{"error": "validation_error", "message": "action=plan requires both cve and repo parameters"}` |
| `action=verify` without cve | 400 | `{"error": "validation_error", "message": "action=verify requires cve parameter"}` |

---

## 7. Integration with Existing Verbs

The `secure` verb is designed to hand off cleanly to existing verbs for deeper investigation:

| After `secure` returns... | Agent calls... | To get... |
|--------------------------|----------------|-----------|
| `usage_sites[].symbol` | `understand(target="org/repo::symbol")` | Full structural context of how the vulnerable code is used |
| `usage_sites[].callers` | `impact(target="org/repo::symbol", cross_repo=true)` | Complete blast radius beyond the 4-hop preview |
| Package name | `search(query="import lodash", scope="code")` | Every import/require site (broader than SBOM tells) |
| SBOM path | `browse(action="read", uri="/org/repo/sbom/source.cdx.json")` | Raw SBOM detail for manual inspection |

### Verb Orchestration Pattern (for autonomous agent)

```
1. secure(cve="CVE-2025-1234", action="identify")
   → findings[0].priority == "P0", start_here = "src/utils/merge.ts:42"

2. understand(target="org/repo/src/utils/merge.ts::deepMerge")
   → callers, callees, how the function works

3. secure(cve="CVE-2025-1234", repo="org/repo", action="plan")
   → step-by-step remediation instructions

4. [Agent edits code, runs tests]

5. secure(cve="CVE-2025-1234", repo="org/repo", action="verify")
   → status: "resolved" ✓
```

---

## 8. Scope & Non-Goals

### In Scope (v1)

- Query by CVE, repo, or package (any combination)
- Join SBOM (S3 CycloneDX) + vulnerabilities table + Neptune call graph + Zoekt
- Reachability scoring at all 4 levels
- Prioritization model (severity × reachability × fix-availability)
- `identify`, `plan`, and `verify` sub-actions
- ACL-scoped (same fail-closed semantics as other verbs)
- Degraded mode when Neptune is unavailable
- Source SBOM only (code dependencies)
- Direct dependencies + one level of transitive (what's in the lockfile)

### Non-Goals (explicit exclusions)

| Non-goal | Rationale |
|----------|-----------|
| **Opening PRs** | `secure` is a read-only intelligence verb. PR creation is the agent's job via existing GitHub tooling. |
| **Editing code** | Same principle — the verb returns the plan, agents execute. |
| **Image/container SBOM** | v1 focuses on source SBOMs (already generated by ingestion pipeline). Image SBOMs are a future extension once container scanning is wired into the ingestion pipeline. |
| **Deep transitive dependency analysis** | v1 uses what's in the lockfile (Syft extracts transitives into the CycloneDX). Deep graph resolution (npm audit-style) is future work. |
| **Real-time CVE feed ingestion** | v1 queries the existing `vulnerabilities` table (populated by the scanner pipeline). Real-time webhook from OSV/NVD is a separate infrastructure concern. |
| **Auto-remediation trigger** | v1 is pull-based (agent calls `secure`). Push-based triggering (vuln loop calls agent on new CVE) is the triage loop's job — `secure` is the tool the triage loop uses, not the trigger. |
| **Multi-language API change detection** | v1's breaking-change risk assessment is based on version diff metadata (changelog, semver). Actual API-surface diff analysis is future work. |
| **Compliance report export** | v1 returns JSON. Formatted compliance reports (SARIF, CSV, PDF) are a presentation layer concern, not the verb's job. |

### Future Extensions (v2+)

- Image SBOM support (container scanning)
- Batch/audit mode: "all CRITICAL vulns across all repos"
- SARIF output format for IDE integration
- Push-based alerting (webhook from vuln DB update → triage → `secure`)
- Breaking-change detection via API surface analysis
- License compliance (reuse SBOM for license scanning)
- Time-based queries: "what vulns were introduced this week?"

---

## 9. Data Flow Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         secure() verb                                 │
├─────────────────────────────────────────────────────────────────────┤
│                                                                       │
│  ┌──────────┐    ┌────────────────┐    ┌───────────┐    ┌────────┐ │
│  │   Input   │───▶│  Data Join Layer │───▶│ Prioritize │───▶│ Output │ │
│  │  (params) │    │                  │    │  & Rank    │    │ (JSON) │ │
│  └──────────┘    └────────────────┘    └───────────┘    └────────┘ │
│                           │                                           │
└───────────────────────────┼───────────────────────────────────────────┘
                            │
            ┌───────────────┼───────────────┐
            │               │               │
            ▼               ▼               ▼
   ┌────────────┐  ┌──────────────┐  ┌──────────────┐
   │   SBOM +    │  │   Vuln DB     │  │  Call Graph   │
   │Dependencies │  │  (Postgres)   │  │  (Neptune)    │
   │  (S3 + PG)  │  │               │  │              │
   └────────────┘  └──────────────┘  └──────────────┘
                                            │
                                            ▼
                                    ┌──────────────┐
                                    │ Code Search   │
                                    │   (Zoekt)     │
                                    └──────────────┘
```

### Join Logic (the core query)

```sql
-- Pseudocode for the data join (not literal SQL — spans S3 + PG + Neptune)

1. Resolve input to a set of (package, version, repo) tuples:
   - If cve: query vulnerabilities table → package + affected_versions
   - If repo: query dependencies table for that repo → all packages
   - If package: use directly

2. For each (package, repo) pair:
   - Match dependencies.package_coordinate LIKE 'pkg:{ecosystem}/{package}@%'
   - Check if version falls in affected_versions range
   - → produces: list of (repo, package, version, cve_id) tuples

3. For each affected (repo, package):
   - Zoekt: find import/usage sites of the package
   - Neptune: for each usage site symbol, query reachability (bounded 4-hop)
   - → produces: usage_sites[] with reachability_level

4. Score and rank by priority model

5. Format response
```

---

## 10. Acceptance Criteria

### AC-1: Basic CVE Query
**Given** a CVE that affects a package present in an indexed repo's SBOM
**When** an agent calls `secure(cve="CVE-2025-1234")`
**Then** the response includes the affected repo, the specific package+version, severity, fix version, and at least one usage site with reachability level

### AC-2: Reachability Scoring
**Given** a vulnerable package imported in 3 files, but only reachable from entry points in 1 file
**When** an agent calls `secure(cve="...", repo="...")`
**Then** the response shows 3 usage_sites, with `reachability_level: "reachable"` for 1 and `"imported"` or `"called"` for the others

### AC-3: Prioritization
**Given** multiple CVEs affecting the same repo
**When** an agent calls `secure(repo="org/repo")`
**Then** findings are sorted by `priority_score` descending, and the P0 bucket (CRITICAL + reachable + fix available) appears first

### AC-4: Plan Action
**Given** a valid (cve, repo) pair
**When** an agent calls `secure(cve="...", repo="...", action="plan")`
**Then** the response includes a `remediation.steps[]` array with ordered, actionable instructions

### AC-5: Verify Action
**Given** a CVE that was previously detected, after the fix has been applied
**When** an agent calls `secure(cve="...", repo="...", action="verify")`
**Then** the response shows `"status": "resolved"` if the current SBOM no longer contains the vulnerable version

### AC-6: Degraded Mode (Neptune unavailable)
**Given** Neptune is unreachable (5-second timeout)
**When** an agent calls `secure(cve="...", repo="...")`
**Then** findings are still returned with `"reachability.confidence": "degraded"` and `"reachability.source": "code-index-fallback"`, defaulting unreachable symbols to `reachable: true`

### AC-7: ACL Enforcement
**Given** a caller whose ACL does not grant access to repo X
**When** they call `secure(repo="X")`
**Then** the response is empty findings (fail-closed), same as other verbs

### AC-8: Unknown CVE
**Given** a CVE not in the vulnerabilities table
**When** an agent calls `secure(cve="CVE-9999-9999")`
**Then** the response has `findings_count: 0` with metadata indicating `cve_known: false`

### AC-9: Stale SBOM Warning
**Given** a repo whose SBOM is older than 24 hours
**When** an agent calls `secure(repo="org/stale-repo")`
**Then** the response includes `metadata.sbom_stale_warning: true` with the SBOM age

### AC-10: Integration Handoff
**Given** a `secure` response with usage_sites
**When** an agent calls `understand(target="org/repo::symbolName")` using a symbol from the response
**Then** it receives valid structural context (proving the handoff from secure → understand works)

---

## 11. Proposed Child Issue Breakdown

After architect review, this EPIC should decompose into:

| # | Child Issue | Agent | Dependency |
|---|-------------|-------|------------|
| 1 | **Data-join layer**: SBOM↔vuln↔dependency reverse lookup (the core query) | architect → developer | None — foundational |
| 2 | **Reachability scorer**: Neptune query for package-level reachability (extends `impact` pattern) | architect → developer | #1 (needs affected packages to query) |
| 3 | **`secure` verb handler**: `_handle_secure()` in server.py + MCP registration in mcp_app.py | developer | #1, #2 |
| 4 | **Prioritization engine**: scoring model + ranking | developer | #1, #2 |
| 5 | **`action=plan` logic**: remediation step generation from vuln metadata | developer | #3 |
| 6 | **`action=verify` logic**: post-fix re-check (re-scan SBOM, compare versions) | developer | #3 |
| 7 | **Tests**: unit (mock backends), integration (fixture data), e2e (live query) | developer | #3-6 |
| 8 | **SBOM freshness metadata**: track SBOM generation timestamp, expose staleness warning | developer | #1 |

---

## 12. Open Questions (for architect/stakeholder review)

1. **SBOM refresh on verify**: When `action=verify` is called, should it trigger a fresh SBOM generation (run Syft again), or only check the last-indexed SBOM? Fresh generation is accurate but slow (30-60s). Recommendation: check last-indexed, include `sbom_age_hours` in metadata, let caller decide if stale.

2. **Cross-repo CVE query**: If `secure(cve="CVE-X")` without a repo — should it search ALL indexed repos (potentially hundreds)? Recommendation: yes, but cap at 50 repos in response, paginate if needed, always respect ACL.

3. **Transitive dependency depth**: The SBOM from Syft includes transitives in the lockfile. But should `secure` report vulns in deps-of-deps that aren't directly imported? Recommendation: yes (they're in the SBOM), but reachability level stays at `present` (Level 0) unless call graph proves otherwise.

4. **Breaking-change intelligence**: Where does "breaking change risk" come from? Options: (a) semver analysis (major bump = high risk), (b) changelog parsing, (c) just "unknown" for v1. Recommendation: semver analysis for v1, with `"breaking_change_risk": "unknown"` when version metadata is insufficient.

---

## 13. Reuse Table (DO NOT REBUILD)

| Capability | Existing Location | How `secure` Uses It |
|-----------|------------------|---------------------|
| SBOM data | S3: `sbom/repos/{org}/{repo}/source.cdx.json` | Read CycloneDX to find component versions |
| Dependency records | Postgres: `dependencies` table (purl+version) | Reverse lookup: which repos have this package |
| Vulnerability data | Postgres: `vulnerabilities` table (cve_id, package, affected/fixed_version, severity, cvss) | Look up CVE details, match against dependencies |
| Call graph reachability | Neptune via `door/neptune_client.py` (`query_impact`) | Determine if vulnerable symbol is reachable |
| Code search (usage sites) | Zoekt via `door/search_backend.py` | Find import/require statements for the package |
| ACL filtering | `door/acl.py` (`_apply_acl`) | Same fail-closed repo-level filtering |
| Project scoping | `door/project_filter.py` | Same project-scope narrowing |
| MCP registration | `door/mcp_app.py` pattern | Follow existing `@mcp_server.tool` decorator pattern |
| Verb dispatch | `door/server.py` (`_dispatch_tool`) | Add `elif name == "secure"` route |
| Normalization | `pipeline/vuln_scanner/normalize.py` | `NormalizedVulnerability` is the canonical vuln model |

---

## 14. Success Metrics

| Metric | Target | How Measured |
|--------|--------|-------------|
| CVE triage time (human) | < 30 seconds from question to prioritized answer | Time from `secure()` call to response |
| Autonomous fix rate | > 80% of P0 findings result in a PR within 1 hour | Triage loop logs: finding → PR opened |
| False positive suppression | > 60% of present-but-unreachable findings correctly deprioritized | Compare Level 0 findings vs. Level 3 findings in same repo |
| Reachability accuracy | > 90% of Level 3 findings are genuinely reachable (validated by test coverage) | Sample audit of reachability claims vs. actual execution |
| Response latency | p95 < 5s for single-CVE single-repo query | Verb handler instrumentation |
