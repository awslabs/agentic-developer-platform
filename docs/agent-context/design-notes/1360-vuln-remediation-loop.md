# Autonomous Vulnerability Remediation Loop — Design Note

**Issue:** #1360 (sub of EPIC #1345)
**Date:** 2026-06-12
**Status:** Implementation-ready design
**Author:** @agent-architect
**Parent design:** `knowledge-layer-storage-design.md` (Section 7.4)
**Depends on:** #1359 (matching engine, merged), #1394 (SBOM generation + reverse-index, in progress)

---

## 1. Purpose

Close the loop between vulnerability detection and remediation: when a CVE is
matched against the dependency reverse-index, automatically triage it for
reachability, file a fix issue per affected repo, hand it to the existing
developer agent to patch and test, open a PR, and record the verified fix as
Experience. This turns ADP from a code-search tool into an autonomous
vulnerability-management-and-remediation platform.

The design **orchestrates existing pieces** (matching engine, developer agent
loop, Experience layer) — the net-new component is the triage + orchestration
layer.

---

## 2. External Fact Verification

### 2.1 Amazon S3 Vectors

| Claim in parent design | Verified status | Source |
|---|---|---|
| GA status | **Confirmed GA** — AWS docs describe it as production-ready with full API surface, pricing page live | docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors.html |
| ~2,500 writes/s/index drives sharding | **Confirmed exactly** — "Combined vectors inserted and deleted per second per vector index: Up to 2,500" | docs.aws.amazon.com/.../s3-vectors-limitations.html |
| Max vectors/index | 2 billion (was "10M" in earlier previews — design is safe) | Same source |
| Max dimensions | 4,096 | Same source |
| Requests/s/index | 1,000 (combined Put+Delete requests) | Same source |
| Filterable metadata | Up to 2 KB per vector, up to 50 metadata keys total | Same source |

**Adjustment needed:** None. The design's sharding assumption (shard at ~2,500 vectors/s) aligns exactly with the confirmed limit.

### 2.2 OSV-Scanner

| Claim | Verified status | Source |
|---|---|---|
| Apache-2.0 | **Confirmed** | github.com/google/osv-scanner (LICENSE) |
| Actively maintained | **Confirmed** — v2.3.8 (May 2025), 51 releases | github.com/google/osv-scanner/releases |
| Consumes CycloneDX SBOM | **ADJUSTMENT NEEDED** — OSV-Scanner V2 does NOT natively consume CycloneDX/SPDX SBOMs as input. It scans lockfiles directly (package-lock.json, requirements.txt, go.mod, etc.) | google.github.io/osv-scanner/supported-languages-and-lockfiles/ |

**Design impact:** The `vulnerability-matching-engine-design.md` already correctly documented this as using the `-L` flag: `osv-scanner scan source -L /path/to/repo.cdx.json`. OSV-Scanner V2 treats `.cdx.json` files as a lockfile type (auto-detected by extension). However, the official supported-formats page does NOT list CycloneDX. The existing `scanner.py` wrapper (`scan_sbom_osv()`) already handles this — it passes the SBOM path via `-L` and parses whatever output comes back. The pipeline should **fall back to scanning individual lockfiles** when CycloneDX input fails, which is the approach already coded in `scanner.py`.

**Recommendation:** Keep the dual approach: attempt SBOM scan first; if no results (CycloneDX not recognized), fall back to direct lockfile scanning. The `scanner.py` already handles both paths. No design change needed — just document the fallback explicitly.

### 2.3 Trivy

| Claim | Verified status | Source |
|---|---|---|
| Apache-2.0 | **Confirmed** | github.com/aquasecurity/trivy |
| Actively maintained | **Confirmed** — v0.71.0 (June 2026), 87 releases, 36.4k stars | Same |
| Covers OS/base-image layers | **Confirmed** — Alpine, Debian, Ubuntu, RHEL, Amazon Linux, etc. | Same |
| Consumes CycloneDX SBOM | **Confirmed** — `trivy sbom <file> --format json` supports CycloneDX | trivy docs + scanner.py already uses this |

### 2.4 PostgreSQL 16

| Claim | Verified status | Source |
|---|---|---|
| Support through ~Nov 2028 | **Confirmed exactly** — EOL November 9, 2028 (5-year support) | postgresql.org/support/versioning/ |
| Current minor | 16.14 | Same |

### 2.5 Mountpoint for Amazon S3

| Claim | Verified status | Source |
|---|---|---|
| GA | **Confirmed** — "Generally Available and Ready for Production Workloads" | aws.amazon.com/s3/features/mountpoint/ |
| Write-once/no-locking | **Confirmed** — sequential writes only for new files, no locking, no overwrite | Same |
| CSI driver exists | **Confirmed** — Mountpoint CSI driver for Kubernetes exists (separate project, aws/mountpoint-s3-csi-driver) | GitHub |

---

## 3. Architecture Overview

```
                    ┌─────────────────────────────────────────────────┐
                    │         VULNERABILITY REMEDIATION LOOP           │
                    └─────────────────────────────────────────────────┘

   ┌──────────┐    ┌──────────────┐    ┌───────────┐    ┌──────────────┐
   │ DETECT   │───▶│ LOCATE +     │───▶│   FIX     │───▶│  REMEMBER    │
   │          │    │ TRIAGE       │    │           │    │              │
   │ OSV/Trivy│    │ Reverse-idx  │    │ File issue│    │ Experience   │
   │ matching │    │ + call-graph │    │ + dev agent│    │ event        │
   └──────────┘    │ reachability │    │ patches   │    └──────────────┘
                   └──────────────┘    │ + tests   │
                          │            │ + PR      │
                          │            └──────────────┘
                    TRIAGE GATE              │
                    (suppress false          │
                     positives)        ┌──────────────┐
                                       │   VERIFY     │
                                       │ Tests pass → │
                                       │ PR opened    │
                                       │ Tests fail → │
                                       │ Agent retries│
                                       └──────────────┘
```

### Sequence Diagram

```
Scheduler/Watchman          Orchestrator         DB        GitHub     Agent-Factory    Experience
       │                        │                │           │             │              │
       │──trigger-scan─────────▶│                │           │             │              │
       │                        │──get-vulns────▶│           │             │              │
       │                        │◀──vuln-list────│           │             │              │
       │                        │                │           │             │              │
       │                        │  for each (cve, affected_repos):        │              │
       │                        │──check-reach──▶│           │             │              │
       │                        │◀──reachable?───│           │             │              │
       │                        │                │           │             │              │
       │                        │  [if unreachable: skip]    │             │              │
       │                        │  [if duplicate: skip]      │             │              │
       │                        │                │           │             │              │
       │                        │──file-issue───────────────▶│             │              │
       │                        │  (5-section body,          │             │              │
       │                        │   label: "developer")      │             │              │
       │                        │──record-filed─▶│           │             │              │
       │                        │                │           │             │              │
       │                        │                │    webhook │             │              │
       │                        │                │◀──labeled──│             │              │
       │                        │                │    intent  │             │              │
       │                        │                │───SQS─────▶│             │              │
       │                        │                │           │──KEDA-pod──▶│              │
       │                        │                │           │  dev-agent  │              │
       │                        │                │           │  patches +  │              │
       │                        │                │           │  runs tests │              │
       │                        │                │           │             │              │
       │                        │                │           │◀──PR───────│              │
       │                        │                │           │             │              │
       │                        │                │           │             │──save-exp───▶│
       │                        │                │           │             │  "fixed CVE" │
       │                        │                │           │             │              │
```

---

## 4. The Triage Gate (Critical Design Decision)

The triage gate is the single most important quality control: it prevents mass
false-positive issue churn. Without it, a CVE affecting `lodash` would file
issues against every repo that lists lodash in `package.json` — even repos that
never call the vulnerable function.

### 4.1 Reachability Check Algorithm

```python
def is_reachable(repo_id: str, package: str, vuln_symbols: list[str] | None) -> bool:
    """Determine if a vulnerable package's code is actually reached.

    Strategy (ordered by precision, most → least):
    1. If vuln_symbols provided (CVE advisory names the vulnerable function):
       → check if any vuln_symbol appears in the repo's call_graph as a callee
    2. If no vuln_symbols (CVE is package-wide):
       → check if ANY import from the package is reachable from entry points
    3. If no call_graph available for repo (structural index not yet run):
       → default TRUE (fail-safe: assume reachable, don't suppress)

    Returns True if reachable (file issue), False if unreachable (suppress).
    """
```

### 4.2 Data Source: `code-index.json` Call Graph

The structural index (issue #1357, merged) produces per-repo `code-index.json`:

```json
{
  "call_graph": {
    "src/db.py::connect_db": ["src/api.py::handle_request", "src/worker.py::process_job"],
    "src/api.py::handle_request": [],
    "src/worker.py::process_job": []
  }
}
```

**Reachability query:** Given a package symbol (e.g., `requests.get`), traverse
the call_graph to check if any caller chain reaches an entry point (a function
with no callers = root of the graph).

**Implementation:** The call_graph is stored in Neptune (GraphRAG extraction in
`ingest-repo.py` lines 636-668 creates "calls" edges). The reachability checker
queries Neptune: "is there a path from any entry point to a node matching
`{package}.*`?"

### 4.3 Fail-Safe Behavior

| Scenario | Decision | Rationale |
|---|---|---|
| Call graph available, symbol reachable | File issue | High confidence |
| Call graph available, symbol NOT reachable | **Suppress** | False positive prevention |
| Call graph NOT available for repo | **File issue** (fail-safe) | Don't miss real vulns; structural index may not have run yet |
| Package imported but zero calls in graph | Suppress | Dead import |
| CVE advisory lacks specific symbol info | File issue if any import exists | Can't prove unreachable |

### 4.4 Idempotency

Before filing, check `remediation_runs` table:
- Has an issue already been filed for this `(repo_id, cve_id)` pair?
- If yes: skip (duplicate prevention per V6 test)
- Per-repo scoping: same CVE in different repos → separate issues (V6b test)

---

## 5. Orchestrator Component

### 5.1 Module Location

```
modules/agent-context/pipeline/vuln_remediation/
├── __init__.py
├── orchestrator.py       # Main loop: detect → triage → file
├── reachability.py       # Call-graph reachability checker
├── issue_generator.py    # 5-section issue body builder
└── models.py             # RemediationRun, TriageDecision dataclasses
```

**Rationale:** Lives in `agent-context/pipeline/` alongside the existing
`vuln_scanner/` module. Does NOT touch the ingestion worker (shared spine,
per dependency note).

### 5.2 Trigger Modes

| Mode | Mechanism | Use case |
|---|---|---|
| Scheduled scan | EventBridge rule → Lambda → orchestrator | Weekly full re-scan |
| On-ingest | Post-SBOM pipeline step calls orchestrator for new findings | Real-time for freshly indexed repos |
| Manual | CLI/API call to orchestrator with specific CVE ID | Operator-initiated targeted remediation |

### 5.3 Orchestrator Flow (Pseudocode)

```python
class RemediationOrchestrator:
    def __init__(self, db, github_client, reachability_checker, config):
        self.db = db
        self.github = github_client
        self.reachability = reachability_checker
        self.config = config  # severity_threshold, max_concurrent, batch_size

    def run(self, cve_ids: list[str] | None = None):
        """Execute one remediation cycle.

        Args:
            cve_ids: Specific CVEs to process. If None, process all
                     unresolved vulns above severity threshold.
        """
        vulns = self._get_actionable_vulns(cve_ids)

        for vuln in vulns:
            affected_repos = self._reverse_lookup(vuln)

            for repo in affected_repos:
                decision = self._triage(repo, vuln)

                if decision.should_file_issue:
                    self._file_fix_issue(repo, vuln)
                else:
                    self._record_suppression(repo, vuln, decision.reason)

    def _triage(self, repo, vuln) -> TriageDecision:
        """Three-gate triage: idempotency → reachability → severity."""
        # Gate 1: Already filed?
        if self.db.issue_already_filed(repo.id, vuln.cve_id):
            return TriageDecision(False, "duplicate")

        # Gate 2: Reachable?
        if not self.reachability.is_reachable(repo.id, vuln.package_name):
            return TriageDecision(False, "unreachable")

        # Gate 3: Severity threshold
        if not self._meets_severity_threshold(vuln):
            return TriageDecision(False, "below_threshold")

        return TriageDecision(True, "reachable_and_actionable")

    def _file_fix_issue(self, repo, vuln):
        """File a 5-section issue and label it for the developer agent."""
        body = self.issue_generator.build(repo, vuln)
        issue = self.github.create_issue(
            repo=repo.repo_name,
            title=f"fix(deps): bump {vuln.package_name} to fix {vuln.cve_id}",
            body=body,
            labels=["developer", "security", "auto-remediation"],
        )
        self.db.record_issue_filed(repo.id, vuln.cve_id, issue.number)
```

---

## 6. Issue Body Template (5-Section Convention)

The orchestrator generates issues following the CLAUDE.md mandatory convention:

```markdown
## Description

Bump `{package_name}` from {installed_version} to {fixed_version} to fix
{cve_id} ({severity}). The vulnerable code path is reachable in this repo
(confirmed via structural call-graph analysis).

## Impact analysis

- **Who benefits** — security posture of {repo_name}; reduces attack surface
- **Who's impacted** — this repo's dependency tree; downstream consumers
- **What breaks if this ships with a bug:**
  | Bug class | Blast radius |
  |---|---|
  | Version bump breaks API compat | Build fails (caught by tests) |
  | Wrong package bumped | No security improvement (caught by re-scan) |
- **Cost / quota footprint** — one developer-agent run (~5 min, ~$0.50)

## Design

**Change:** Update `{lockfile_path}` to use {package_name}>={fixed_version}.
If breaking changes exist, update call sites as needed.

- **File-level changes:** `{lockfile_path}` (version bump), possibly source
  files if API changed
- **Integration points:** existing test suite validates compatibility
- **Reuse:** standard dependency bump pattern

## Deployment

- **Automatic on merge** — CI runs tests; no infra changes
- **Rollback** — revert the version bump PR

## Validation

- [ ] All existing tests pass with the bumped version
- [ ] No new deprecation warnings introduced
- [ ] `{package_name}` version in lockfile >= {fixed_version}
```

---

## 7. Staged Rollout Controls

### 7.1 Never Auto-Merge (Hard Constraint)

**Design decision:** The remediation loop NEVER auto-merges PRs. All PRs go
through normal branch protection and human review.

**Enforcement:**
1. The developer agent opens PRs but does NOT have merge permission
2. PRs are labeled `auto-remediation` for easy filtering
3. Branch protection rules (required reviews, status checks) remain enforced
4. The `auto-merge` GitHub API is NOT called by any component

**Rationale:** A bad auto-merged fix across 50 repos simultaneously would be
catastrophic. Human review is the final safety net.

### 7.2 Concurrency Controls

| Control | Value | Rationale |
|---|---|---|
| `max_issues_per_cve_per_wave` | 5 | Don't file 50 issues simultaneously |
| `wave_interval_minutes` | 30 | Space out waves to allow early feedback |
| `max_concurrent_agent_runs` | 10 | Stay within KEDA pod budget (50 max) |
| `severity_threshold` | "HIGH" | Only auto-remediate HIGH + CRITICAL initially |
| `cooldown_after_failure_minutes` | 60 | If 3+ repos fail for same CVE, pause and alert |

### 7.3 Staged Rollout Strategy

```
Wave 1: File issues for up to 5 repos (highest-traffic repos first)
         Wait 30 minutes
         Check: did the agent succeed? Tests green? PR opened?

Wave 2: If Wave 1 succeeded for >= 80%, file next 5 repos
         Wait 30 minutes

Wave 3+: Continue until all affected repos have issues filed

Abort: If any wave has >= 50% failure rate, halt and alert operator
```

**Implementation:** The orchestrator maintains a `remediation_runs` table
tracking wave state. Each run records: CVE, repos in wave, status per repo,
wave number, started_at, completed_at.

### 7.4 Severity Gating

| Severity | Auto-file? | Rationale |
|---|---|---|
| CRITICAL | Yes | Immediate threat, no delay |
| HIGH | Yes | Significant risk, automated fix worthwhile |
| MEDIUM | No (manual trigger only) | Low urgency; may cause noise |
| LOW | No | Not worth agent compute |

Configurable via `VULN_REMEDIATION_SEVERITY_THRESHOLD` env var.

---

## 8. Experience Layer Integration

### 8.1 How Fixes Become Experience

The existing `experience-save-hook.ts` (in agent-factory) already runs post-task:

1. Developer agent completes fix → tests pass → PR opened
2. `experience-save-hook.ts` extracts `### Learnings` from agent output
3. Calls Context MCP Server with `action: "save"`:
   ```json
   {
     "name": "experience",
     "arguments": {
       "action": "save",
       "persona": "developer",
       "content": "Fixed CVE-2023-32681 in org/service-a by bumping requests to 2.31.0. Breaking change: requests.get() no longer accepts `verify=False` without warning.",
       "learning_type": "vuln_fix",
       "context": {
         "issue_number": 1500,
         "pr": 1501,
         "cve_id": "CVE-2023-32681",
         "package": "requests",
         "from_version": "2.25.0",
         "to_version": "2.31.0"
       },
       "visibility": "shared"
     }
   }
   ```

### 8.2 What's New vs. What's Reused

| Component | Status | Notes |
|---|---|---|
| experience-save-hook.ts | **Reuse as-is** | Already runs post-task for all developer agent work |
| `learning_type: "vuln_fix"` | **New convention** | Allows querying fix-specific learnings |
| `visibility: "shared"` | **New default for vuln fixes** | Org-wide value (not private to one user) |
| Context metadata (cve_id, package, versions) | **New fields in context dict** | Enables "recall all fixes for CVE-X" |

### 8.3 Golden Path Emergence

Over time, repeated fixes for the same vulnerability class (e.g., "bump
requests to fix SSRF") accumulate as shared Experience. The synthesis job
(future #3.1) can aggregate these into a Golden Path: "when CVE class X
appears, the fix pattern is Y." This feeds back to improve future agent
performance on similar CVEs.

---

## 9. Database Schema Additions

### 9.1 `remediation_runs` Table

Tracks the orchestrator's state across waves. Provides idempotency, audit trail,
and staged-rollout coordination.

```sql
CREATE TABLE remediation_runs (
    id              BIGSERIAL PRIMARY KEY,
    cve_id          TEXT NOT NULL,
    repo_id         BIGINT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    wave_number     INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'pending',
        -- pending | issue_filed | agent_running | pr_opened | merged | failed | suppressed
    suppression_reason TEXT,          -- "unreachable" | "duplicate" | "below_threshold"
    issue_number    INTEGER,          -- GitHub issue number (once filed)
    pr_number       INTEGER,          -- GitHub PR number (once opened)
    filed_at        TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Idempotency: one run per (cve, repo)
    CONSTRAINT uq_remediation_cve_repo UNIQUE (cve_id, repo_id)
);

-- For "all runs for this CVE" (wave coordination)
CREATE INDEX idx_remediation_cve ON remediation_runs (cve_id, wave_number);

-- For "all runs for this repo" (per-repo audit)
CREATE INDEX idx_remediation_repo ON remediation_runs (repo_id, status);

-- For "find stale runs" (agent didn't complete)
CREATE INDEX idx_remediation_status ON remediation_runs (status, created_at);
```

### 9.2 Migration

Alembic migration: `002_remediation_runs.py` (next in sequence after
`001_knowledge_layer_schema.py`).

---

## 10. Reuse Table

| Capability | Existing component | How we use it |
|---|---|---|
| Vulnerability detection | `pipeline/vuln_scanner/scanner.py` + `normalize.py` | Input: normalized findings |
| Reverse lookup | `dependencies` table + `idx_dep_package_name` index | Query: which repos use pkg X? |
| Structural call graph | `code-index.json` → Neptune "calls" edges | Reachability traversal |
| Issue filing + label routing | `intent_parser.py` LABEL_TO_PERSONA | Label `developer` → dev agent |
| Developer agent loop | webhook-ingress → SQS FIFO → KEDA → agent pod | Patches, tests, opens PR |
| Experience recording | `experience-save-hook.ts` → Context MCP `experience` tool | Post-fix learning persistence |
| Rate limiting | `rate_limit.py` (50/5min, 500/hr per tenant) | Protects against flood |
| Idempotency pattern | `FakeDependencyStore.issue_already_filed()` (tested V6) | Duplicate prevention |
| Concurrency control | KEDA ScaledJob max 50 replicas | Natural ceiling on parallel fixes |

**Net-new components:**
1. `vuln_remediation/orchestrator.py` — the coordination loop
2. `vuln_remediation/reachability.py` — Neptune call-graph traversal
3. `vuln_remediation/issue_generator.py` — 5-section issue body builder
4. `remediation_runs` DB table — state tracking + idempotency
5. EventBridge rule — scheduled trigger (or manual invocation)

---

## 11. Integration Points

### 11.1 Input: Matching Engine Output

The orchestrator reads from the `vulnerabilities` table (populated by the
matching engine, issue #1359):

```sql
SELECT v.cve_id, v.package_name, v.package_ecosystem,
       v.affected_versions, v.fixed_version, v.severity
FROM vulnerabilities v
WHERE v.severity IN ('CRITICAL', 'HIGH')
  AND NOT EXISTS (
    SELECT 1 FROM remediation_runs r
    WHERE r.cve_id = v.cve_id AND r.status NOT IN ('failed', 'suppressed')
  );
```

### 11.2 Output: GitHub Issues

Filed via GitHub API (`gh` CLI or PyGithub) in the affected repo:
- Title: `fix(deps): bump {package} to fix {cve_id}`
- Labels: `["developer", "security", "auto-remediation"]`
- Body: 5-section template (Section 6 above)

The `developer` label triggers the webhook-ingress flow:
`issues.labeled` → intent_parser → SQS → KEDA → developer agent pod

### 11.3 Output: Experience Layer

The developer agent's `experience-save-hook.ts` fires automatically on task
completion. No additional wiring needed — the hook already runs for every
developer agent task.

**Enhancement for this issue:** The issue body template includes structured
context (CVE ID, package, versions) that the agent can extract into the
Experience `context` field, enabling future queries like "recall all fixes
for CVE-2023-32681."

---

## 12. Error Handling & Graceful Degradation

| Failure | Behavior | Recovery |
|---|---|---|
| Neptune unreachable (reachability check) | Fail-safe: assume reachable, file issue | Log warning; reachability is advisory |
| GitHub API rate limit | Exponential backoff (3 retries) then pause wave | Resume on next scheduled run |
| GitHub issue creation fails | Record `status=failed` in remediation_runs | Retry on next cycle |
| Developer agent times out | KEDA pod terminates after deadline; DLQ | Operator alert; manual re-trigger |
| Agent fix breaks tests | Agent retries (existing loop); if still fails: PR not opened | Record `status=failed`; no noise |
| Same CVE re-detected on next scan | Idempotency check → skip (already filed) | No duplicate issues |

---

## 13. Security Considerations

1. **No elevated permissions for the orchestrator.** It uses the same GitHub App
   credentials as existing webhook-ingress. PRs go through normal review.

2. **No auto-merge.** Branch protection remains enforced. The developer agent
   cannot bypass required reviewers.

3. **Tenant isolation.** The orchestrator runs per-tenant (scoped by
   `repositories.owner`). One tenant's vulns never trigger issues in another
   tenant's repos.

4. **Rate limiting inherited.** The 50/5min per-tenant rate limit in
   webhook-ingress naturally caps how fast issues can be processed. The
   orchestrator's wave system adds an additional layer.

5. **Audit trail.** Every triage decision (file or suppress) is recorded in
   `remediation_runs` with timestamp and reason. Full observability.

---

## 14. Configuration

```python
# Environment variables (centralized via config.py per #1378)
VULN_REMEDIATION_ENABLED = True          # Kill switch
VULN_REMEDIATION_SEVERITY_THRESHOLD = "HIGH"  # Minimum severity to auto-file
VULN_REMEDIATION_MAX_PER_WAVE = 5        # Issues filed per wave
VULN_REMEDIATION_WAVE_INTERVAL_MIN = 30  # Minutes between waves
VULN_REMEDIATION_MAX_CONCURRENT = 10     # Max parallel agent runs
VULN_REMEDIATION_COOLDOWN_ON_FAILURE = 60  # Minutes to pause after failures
VULN_REMEDIATION_DRY_RUN = False         # Log decisions without filing
```

---

## 15. Testing Strategy

### 15.1 Unit Tests (already partially exist)

The V4-V6 tests in `test_vuln_loop.py` already validate the triage logic using
`FakeReachabilityChecker` and `FakeDependencyStore`. Additional tests needed:

| Test | What it proves |
|---|---|
| Orchestrator respects severity threshold | MEDIUM vuln → no issue filed |
| Wave batching limits issues per cycle | 10 affected repos, max_per_wave=5 → only 5 filed |
| Cooldown triggers on failure rate | 3/5 failures → orchestrator pauses |
| Issue body follows 5-section convention | Generated body has all 5 headers |
| Dry-run mode logs but doesn't file | No GitHub API calls in dry-run |

### 15.2 Integration Tests

| Test | Scope |
|---|---|
| Reachability checker queries Neptune | Seeded graph → correct reachable/unreachable answers |
| Issue filing + label triggers intent_parser | Filed issue → webhook → intent extracted |
| Full loop (planted CVE → PR) | E2E with fixture repo |

### 15.3 E2E Acceptance (Smoke Test)

```bash
# Plant a vulnerable dep in fixture repo, trigger orchestrator
# Expected: issue filed → developer agent picks up → PR opened → Experience saved
python -m pytest modules/agent-context/tests/e2e/test_vuln_e2e.py -k "test_full_loop" --live
```

---

## 16. File-Level Implementation Plan

| Step | File | Action |
|---|---|---|
| 1 | `modules/agent-context/pipeline/vuln_remediation/__init__.py` | Create module |
| 2 | `modules/agent-context/pipeline/vuln_remediation/models.py` | TriageDecision, RemediationRun dataclasses |
| 3 | `modules/agent-context/pipeline/vuln_remediation/reachability.py` | Neptune call-graph traversal |
| 4 | `modules/agent-context/pipeline/vuln_remediation/issue_generator.py` | 5-section issue body builder |
| 5 | `modules/agent-context/pipeline/vuln_remediation/orchestrator.py` | Main loop |
| 6 | `modules/agent-context/alembic/versions/002_remediation_runs.py` | DB migration |
| 7 | `modules/agent-context/tests/unit/test_orchestrator.py` | Unit tests for orchestrator |
| 8 | `modules/agent-context/tests/unit/test_reachability.py` | Unit tests for reachability |
| 9 | `modules/agent-context/tests/unit/test_issue_generator.py` | Unit tests for issue body |

---

## 17. Open Questions (For Follow-Up Issues)

1. **Neptune vs. PostgreSQL for reachability:** If Neptune is not yet deployed for
   a tenant, should we fall back to loading `code-index.json` from S3 directly?
   (Recommendation: yes, with S3 as fallback.)

2. **PR status tracking:** Should the orchestrator poll for PR merge status to
   update `remediation_runs.status`? Or rely on a webhook callback?
   (Recommendation: webhook callback via existing PR event handling.)

3. **Multi-tenant orchestrator scheduling:** One shared orchestrator per
   environment, or per-tenant? (Recommendation: shared, with tenant iteration
   and per-tenant rate limiting.)

---

## 18. Acceptance Criteria

1. Orchestrator triages vulnerabilities using call-graph reachability (suppress unreachable)
2. Issues filed follow the 5-section convention and include the `developer` label
3. Idempotency: same (cve, repo) never generates duplicate issues
4. Wave-based rollout: max N issues per wave with configurable interval
5. Never auto-merge: PRs require human review (branch protection enforced)
6. Experience recorded post-fix via existing experience-save-hook.ts
7. Dry-run mode available for testing without filing real issues
8. Kill switch (`VULN_REMEDIATION_ENABLED=false`) halts all activity immediately
9. Severity threshold configurable (default: HIGH+CRITICAL only)
10. Full audit trail in `remediation_runs` table
