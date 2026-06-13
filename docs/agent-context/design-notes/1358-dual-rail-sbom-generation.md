# Design Note: Dual-Rail SBOM Generation

**Issue:** #1358 (sub of EPIC #1345)
**Author:** @agent-architect
**Date:** 2026-06-11
**Status:** Implementation-ready
**Parent design:** `docs/knowledge-layer-storage-design.md` (sections 7.1-7.4)

---

## 1. Summary

Generate a software bill of materials (SBOM) for every indexed repository via two
complementary rails:

- **Rail 1 (source):** Run Syft against the cloned source directory during the
  existing ingestion fan-out. Cheap, works for every repo.
- **Rail 2 (image, best-effort):** On CodeBuild (which has Docker), build each
  Dockerfile, scan the resulting image with Syft. Skip + record on failure.

Both emit **CycloneDX JSON** to S3 and parse dependency rows into a new
`dependencies` table in the `agent_context` PostgreSQL database. This powers the
reverse-index ("which repos use package X?") and the vulnerability-management loop.

---

## 2. Fact-Verification Against Current Sources

The review brief requested verification of several design assumptions. Results:

### 2.1 Amazon S3 Vectors

| Claim in design | Verified status |
|---|---|
| GA + available in deploy region | **Partial.** S3 Vectors has full documentation and API (CreateVectorBucket, PutVectors, QueryVectors) published. AWS docs do not explicitly state "preview" anywhere and present it as a production service. Region availability list is not published on a dedicated page; **verify `us-east-1` availability before first use** (likely available given it's a launch region). |
| ~2,500 writes/s/index drives sharding | **Confirmed.** Quota: "Combined vectors inserted and deleted per second per vector index: Up to 2,500." With 50-100 parallel workers each writing ~50 vectors/s, saturation is possible. Sharding strategy in design is justified. |
| Max dimensions | **Confirmed: 1-4,096.** Sufficient for common embedding models (1536 for text-embedding-3-small, 3072 for text-embedding-3-large). |
| Max vectors/index | **Confirmed: 2 billion.** No concern for code-search use case. |

**Recommendation:** No design change needed for SBOM sub-issue (S3 Vectors is for meaning-search, not SBOM storage). Note for sibling issues: confirm region availability via `aws s3vectors list-vector-buckets` before provisioning.

### 2.2 Mountpoint for Amazon S3

| Claim | Status |
|---|---|
| GA | **Confirmed.** Blog: "Generally Available and Ready for Production Workloads." |
| Write-once/no-locking semantics | **Confirmed.** "Supports sequential write operations for creating new files... doesn't try to emulate shared file system features such as locking." |
| CSI driver for EKS | **Confirmed.** Official EKS add-on: "Mountpoint for Amazon S3 Container Storage Interface (CSI) driver... presents an Amazon S3 bucket as a volume that can be accessed by containers in Amazon EKS." Already in use (`s3-files` Terraform module). |

**Recommendation:** No change. The platform-data PVC is already backed by this.

### 2.3 OSV-Scanner

| Claim | Status |
|---|---|
| Apache-2.0 | **Confirmed.** |
| Actively maintained | **Confirmed.** v2.3.8 (May 2026), 1,933 commits, 51 releases. |
| Consumes CycloneDX SBOM as input | **NOT CONFIRMED.** OSV-Scanner does NOT support CycloneDX or SPDX SBOM ingestion. It scans lockfiles, manifests, and container images directly via its own extractors. |

**Critical design adjustment:** The parent design (section 7.4) says "OSV-Scanner matches our SBOMs against known vulnerabilities." This is **incorrect as stated**. OSV-Scanner cannot consume a `.cdx.json` file.

**Two viable integration patterns:**
1. **Feed lockfiles directly to OSV-Scanner** (it already knows how to parse them) -- simpler, but then the SBOM is only for audit/compliance, not the vuln-match input.
2. **Use `grype` or `trivy` to scan the CycloneDX SBOM** -- both Trivy (`trivy sbom <file>`) and Grype (`grype sbom:<file>`) accept CycloneDX as input. Trivy is Apache-2.0 (clean); Grype is also Apache-2.0 (Anchore relicensed in 2024).

**Recommendation for the vuln-matching sub-issue (NOT this issue):**
- Use **Trivy** as the unified vulnerability scanner for both source SBOMs (CycloneDX input) and image layers. Trivy is Apache-2.0, actively maintained (v0.71.0, June 2026), and accepts `trivy sbom <cyclonedx.json>`.
- Keep OSV-Scanner as an optional second-opinion for lockfile-native scanning (it has higher precision for some ecosystems via commit-level matching).
- This issue (#1358) only produces the SBOMs; it does NOT run vulnerability matching. That's a separate sub-issue under #1345.

### 2.4 Trivy

| Claim | Status |
|---|---|
| Apache-2.0 | **Confirmed.** |
| Actively maintained | **Confirmed.** v0.71.0 (June 2026), 4,106 commits, 87 releases. |
| Covers OS/base-image layers | **Confirmed.** Container image scanning is a primary target; scans OS packages, application dependencies, and IaC. |

### 2.5 PostgreSQL 16 Support Window

| Claim | Status |
|---|---|
| Support through ~Nov 2028 | **Confirmed.** PostgreSQL 16 EOL: November 9, 2028 (5-year window from Sep 2023 initial release). Current minor: 16.14. |

---

## 3. Detailed Design

### 3.1 Rail 1: Source SBOM (all repos, in-worker)

**Where it runs:** Inside the existing KEDA ingestion worker pod, as a new step
in `ingest-repo.py`, after the clone and before cleanup.

**Trigger:** Every `content_type=repo` message processed by `sqs-worker.py`.

**Flow:**

```
ingest-repo.py (existing)
  Step 2: clone/update repo → /platform-data/repos/{org}/{repo}
  ...
  Step 5b (NEW): Source SBOM generation
    ├── syft dir:/platform-data/repos/{org}/{repo} -o cyclonedx-json=/tmp/sbom-source-{slug}.cdx.json
    ├── Upload to S3: s3://{SBOM_BUCKET}/sbom/repos/{org}/{repo}/source.cdx.json
    │     metadata: commit={sha}, generated_at={iso}, syft_version={ver}
    ├── Parse CycloneDX JSON → extract dependency rows
    └── INSERT into agent_context.dependencies (upsert on repo_id + package_coordinate)
```

**Key decisions:**
- Syft is **installed in the ingestion Docker image** (add to Dockerfile, ~30MB binary).
- Source SBOM runs against the **already-cloned directory** -- zero additional network cost.
- The SBOM file is the **canonical record** (S3); Postgres rows are a **derived index** for fast lookup.
- Each dependency row records `resolution_source`: `lockfile` or `manifest` (Syft metadata distinguishes these via the `foundBy` field in CycloneDX components).

**S3 path structure:**
```
s3://{PLATFORM_DATA_BUCKET}/sbom/repos/{org}/{repo}/source.cdx.json
```

Note: We use the **agent-context platform-data bucket** (`agent-context-platform-data-{account_id}`), NOT the security-scans bucket. Rationale: the security-scans bucket is for the ADP platform's own scans; the SBOM corpus is for indexed customer/study repos and belongs to the knowledge layer.

**Failure handling:**
- Syft failure is **non-blocking** (like DeepWiki/GraphRAG today). Log error, set `sbom_source_status: failed` in DynamoDB state, continue pipeline.
- Record result in the `ingest_repo()` return dict: `"sbom_source": "complete"` or `"sbom_source": "failed"`.

### 3.2 Rail 2: Image SBOM (Dockerfile repos, best-effort, CodeBuild)

**Where it runs:** CodeBuild (has Docker daemon), extending the existing `bs-syft-scan.yml` pattern.

**Trigger:** A new CodeBuild project `agent-context-image-sbom` (or extend the existing `bs-syft-scan` project with a source parameter). Invoked after ingestion completes for repos that have Dockerfiles.

**Flow:**

```
bs-image-sbom-scan.yml (new buildspec, based on bs-syft-scan.yml)
  1. Receive: repo URL, commit SHA, list of Dockerfiles (from ingestion metadata)
  2. For each Dockerfile:
     ├── docker build --pull --no-cache -t sbom-target:{slug} {context_dir}
     │   Timeout: 10 minutes per build
     ├── On build failure: record marker, continue to next Dockerfile
     ├── syft {image} -o cyclonedx-json=/tmp/sbom-image-{slug}.cdx.json
     ├── Upload: s3://{PLATFORM_DATA_BUCKET}/sbom/images/{org}/{repo}/{slug}.image.cdx.json
     │   metadata: commit={sha}, dockerfile={path}, base_image={from_line}
     ├── Parse → INSERT into agent_context.dependencies (source='image', base_image=...)
     └── docker rmi {image} (free disk)
  3. Record coverage: {built}/{total} Dockerfiles succeeded
```

**Guardrails (cost control):**
- **SHA-gated rebuild:** Only trigger image SBOM when the repo's commit SHA changed since last image scan (tracked in DynamoDB: `last_image_sbom_sha`).
- **Concurrency cap:** CodeBuild project limited to `max_concurrent_builds = 5`.
- **Per-build timeout:** 10 minutes per individual `docker build`. Overall CodeBuild timeout: 30 minutes.
- **Selective trigger:** Only repos with at least one Dockerfile (detected during source ingestion, stored as `has_dockerfile: true` in DynamoDB).
- **Best-effort, never blocks:** Build failure records a marker row in Postgres (`sbom_status = 'build_failed'`) and a DynamoDB attribute (`image_sbom: build_failed`). The pipeline continues.

**S3 path structure:**
```
s3://{PLATFORM_DATA_BUCKET}/sbom/images/{org}/{repo}/{dockerfile-slug}.image.cdx.json
```

### 3.3 Coverage Reporting

The system honestly reports what it scanned and what it couldn't:

```sql
-- Coverage query
SELECT
  COUNT(*) AS total_repos,
  COUNT(*) FILTER (WHERE source_sbom_status = 'complete') AS source_scanned,
  COUNT(*) FILTER (WHERE has_dockerfile = true) AS dockerfile_repos,
  COUNT(*) FILTER (WHERE image_sbom_status = 'complete') AS image_scanned,
  COUNT(*) FILTER (WHERE image_sbom_status = 'build_failed') AS image_unbuildable
FROM repo_sbom_coverage;
```

Output example: "Source: 487/500 repos scanned. Image (OS-layer): 58/120 Dockerfile repos built successfully; 62 unbuildable (recorded)."

---

## 4. Database Schema

### 4.1 Location

New tables in the **`agent_context` database** on the shared RDS instance (separate from the gateway database). Managed via a lightweight migration tool (Python script with versioned SQL files, matching the pattern established for agent-context DynamoDB but now for Postgres).

### 4.2 Tables

```sql
-- Migration: 001_dependencies_schema.sql
-- Purpose: Reverse dependency index for SBOM-powered vulnerability lookups

CREATE TABLE IF NOT EXISTS repositories (
    id              BIGSERIAL PRIMARY KEY,
    org_name        TEXT NOT NULL,
    repo_name       TEXT NOT NULL,
    git_url         TEXT NOT NULL,
    default_branch  TEXT DEFAULT 'main',
    last_indexed_sha TEXT,
    last_source_sbom_sha TEXT,
    last_image_sbom_sha  TEXT,
    source_sbom_status   TEXT DEFAULT 'pending',  -- pending|complete|failed
    image_sbom_status    TEXT DEFAULT 'pending',  -- pending|complete|build_failed|no_dockerfile
    has_dockerfile       BOOLEAN DEFAULT FALSE,
    indexed_at           TIMESTAMPTZ,
    created_at           TIMESTAMPTZ DEFAULT NOW(),
    updated_at           TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (org_name, repo_name)
);

CREATE TABLE IF NOT EXISTS dependencies (
    id                  BIGSERIAL PRIMARY KEY,
    repo_id             BIGINT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    package_url         TEXT NOT NULL,       -- Package URL (purl) e.g. pkg:pypi/requests@2.28.1
    package_name        TEXT NOT NULL,       -- e.g. "requests"
    package_version     TEXT,                -- e.g. "2.28.1" (NULL if unresolved)
    package_ecosystem   TEXT NOT NULL,       -- e.g. "pypi", "npm", "golang", "deb"
    source              TEXT NOT NULL,       -- 'code' (from source SBOM) or 'image' (from image SBOM)
    resolution_source   TEXT,               -- 'lockfile' | 'manifest' | 'binary' | 'os-package'
    is_transitive       BOOLEAN DEFAULT FALSE,
    dockerfile_path     TEXT,               -- only for source='image': which Dockerfile
    base_image          TEXT,               -- only for source='image': e.g. "python:3.13-slim"
    sbom_component_type TEXT,               -- 'library' | 'framework' | 'application' | 'operating-system'
    discovered_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (repo_id, package_url, source, COALESCE(dockerfile_path, ''))
);

-- The reverse-index: "which repos use package X?"
CREATE INDEX idx_dependencies_package_url ON dependencies (package_url);
CREATE INDEX idx_dependencies_package_name ON dependencies (package_name);
CREATE INDEX idx_dependencies_ecosystem ON dependencies (package_ecosystem);
CREATE INDEX idx_dependencies_repo_id ON dependencies (repo_id);

-- Coverage stats
CREATE INDEX idx_repositories_sbom_status ON repositories (source_sbom_status, image_sbom_status);

CREATE TABLE IF NOT EXISTS vulnerabilities (
    id                  BIGSERIAL PRIMARY KEY,
    cve_id              TEXT NOT NULL UNIQUE,
    package_ecosystem   TEXT NOT NULL,
    package_name        TEXT NOT NULL,
    affected_versions   TEXT NOT NULL,       -- version constraint string
    fixed_version       TEXT,                -- NULL if no fix available
    severity            TEXT,                -- critical|high|medium|low|unknown
    summary             TEXT,
    source_db           TEXT DEFAULT 'osv',  -- 'osv' | 'nvd' | 'ghsa'
    published_at        TIMESTAMPTZ,
    discovered_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_vulnerabilities_package ON vulnerabilities (package_ecosystem, package_name);
CREATE INDEX idx_vulnerabilities_cve ON vulnerabilities (cve_id);
```

### 4.3 The Reverse-Index Query

```sql
-- "Which repos use requests 2.28.x?" (the key question for vuln response)
SELECT r.org_name, r.repo_name, d.package_version, d.source, d.resolution_source
FROM dependencies d
JOIN repositories r ON r.id = d.repo_id
WHERE d.package_name = 'requests'
  AND d.package_ecosystem = 'pypi'
  AND d.package_version LIKE '2.28%';
```

### 4.4 Database Access

- **Connection:** IAM auth via the existing IRSA role (`agent-context-platform-irsa`).
- **New IAM policy needed:** `rds-connect` action for the agent-context role to the `agent_context` database user.
- **Separate database on shared instance:** `CREATE DATABASE agent_context;` + `CREATE USER agent_context_rw WITH LOGIN;` + `GRANT ALL ON DATABASE agent_context TO agent_context_rw;`
- **Migration runner:** Python script executed as a Kubernetes Job (pattern: gateway's `rds-bootstrap` job).

---

## 5. File-Level Changes

### 5.1 Files to Modify

| File | Change |
|---|---|
| `modules/agent-context/images/ingestion/ingest-repo.py` | Add Step 5b: source SBOM generation (Syft call, S3 upload, Postgres insert) |
| `modules/agent-context/images/ingestion/sqs-worker.py` | Add `sbom_source_status` to DynamoDB state tracking |
| `modules/agent-context/images/ingestion/Dockerfile` | Install Syft binary + `psycopg2-binary` for Postgres access |
| `modules/agent-context/terraform/modules/iam/main.tf` | Add `rds-connect` IAM policy for agent-context DB access; add S3 PutObject for SBOM prefix |
| `codebuild/bs-syft-scan.yml` | Extend to support multi-repo mode (receive repo URL as env var) OR create new buildspec |

### 5.2 Files to Create

| File | Purpose |
|---|---|
| `modules/agent-context/images/ingestion/sbom_parser.py` | Parse CycloneDX JSON into dependency rows; shared by both rails |
| `modules/agent-context/images/ingestion/db.py` | Postgres connection helper (IAM auth) + upsert functions for dependencies table |
| `modules/agent-context/db/migrations/001_dependencies_schema.sql` | SQL migration (schema above) |
| `modules/agent-context/db/migrate.py` | Migration runner (applies numbered .sql files in order) |
| `codebuild/bs-image-sbom-scan.yml` | New buildspec for multi-repo image SBOM (extends bs-syft-scan.yml pattern) |
| `modules/agent-context/terraform/modules/rds-access/main.tf` | IAM policy for RDS IAM auth to agent_context database |
| `modules/agent-context/tests/unit/test_sbom_parser.py` | Unit tests for CycloneDX parsing |
| `modules/agent-context/tests/unit/test_db.py` | Unit tests for DB upsert logic |

---

## 6. Integration Points

| Produces | Consumed by |
|---|---|
| `dependencies` table rows (Postgres) | Reverse-index sub-issue (#1345 child); vuln-matching sub-issue |
| S3 SBOM files (CycloneDX JSON) | Trivy vuln scanning (future); audit/compliance export |
| DynamoDB `has_dockerfile` + `last_image_sbom_sha` | Image SBOM trigger logic (skip unchanged repos) |
| Coverage stats (Postgres query) | Platform dashboard; watchman alerts |

---

## 7. Sequence Diagrams

### 7.1 Rail 1 (Source SBOM — in ingestion worker)

```
SQS Message (repo changed)
    │
    ▼
┌─────────────────────────────────────────────┐
│ KEDA Worker Pod (ingest-repo.py)            │
│                                              │
│  1. Clone/update repo                        │
│  2. OpenViking ingest                        │
│  3. Code-index generation                    │
│  4. DeepWiki wiki                            │
│  5. GraphRAG extraction                      │
│  6. ──► SOURCE SBOM (NEW) ◄──                │
│     │   syft dir:{clone_path}                │
│     │   → CycloneDX JSON                     │
│     │   → S3 upload                          │
│     │   → Postgres dependencies INSERT       │
│  7. Update DynamoDB state                    │
└─────────────────────────────────────────────┘
```

### 7.2 Rail 2 (Image SBOM — CodeBuild, triggered post-ingestion)

```
Ingestion completes with has_dockerfile=true AND sha != last_image_sbom_sha
    │
    ▼
┌─────────────────────────────────────────────────┐
│ Trigger: Lambda or CronJob checks DynamoDB      │
│   → starts CodeBuild with repo_url + sha        │
└────────────────────────┬────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────┐
│ CodeBuild (bs-image-sbom-scan.yml)              │
│                                                  │
│  For each Dockerfile:                            │
│    1. docker build (10 min timeout)              │
│    2. On fail → record build_failed, continue    │
│    3. syft {image} -o cyclonedx-json             │
│    4. Upload to S3                               │
│    5. Parse → Postgres INSERT (source='image')   │
│    6. docker rmi                                 │
│                                                  │
│  Record coverage: N/M built                      │
└─────────────────────────────────────────────────┘
```

---

## 8. Cost Guardrails

| Guardrail | Mechanism | Bound |
|---|---|---|
| Only scan changed repos | SHA comparison (DynamoDB `last_sha` vs current) | 0 cost for unchanged repos |
| Source SBOM cost | Runs on existing worker pod, reads existing clone — near-zero marginal cost | ~2s CPU per repo |
| Image build concurrency | CodeBuild `max_concurrent_builds` | 5 concurrent |
| Per-build timeout | `docker build` wrapped with `timeout 600` | 10 min max |
| Overall CodeBuild timeout | buildspec `timeoutInMinutes` | 30 min |
| Disk exhaustion | `docker rmi` after each scan; `docker system prune -f` on failure | Bounded |
| Only build when changed | `last_image_sbom_sha` gate | Skip if SHA matches |
| Honest failure tracking | `build_failed` marker; never retry unbuildable repos until they change | No infinite loops |

---

## 9. CycloneDX Parsing Strategy

Syft's CycloneDX JSON output structure (simplified):

```json
{
  "bomFormat": "CycloneDX",
  "specVersion": "1.6",
  "components": [
    {
      "type": "library",
      "name": "requests",
      "version": "2.31.0",
      "purl": "pkg:pypi/requests@2.31.0",
      "properties": [
        { "name": "syft:package:foundBy", "value": "python-pip-requirements-lock-cataloger" }
      ]
    }
  ]
}
```

**Parsing rules for `sbom_parser.py`:**
1. Extract `purl` (Package URL) as the canonical coordinate — it encodes ecosystem, name, and version.
2. Determine `resolution_source` from `syft:package:foundBy`:
   - `*-lock-*` or `*-lockfile-*` → `lockfile`
   - `*-manifest-*` or `*-requirements-*` (without lock) → `manifest`
   - `dpkg-*`, `apk-*`, `rpm-*` → `os-package`
   - `binary-*` → `binary`
3. Extract `is_transitive` — Syft doesn't directly flag this, but lockfile presence implies resolved (potentially transitive). Default `false`; can be refined later.
4. For image SBOMs, extract `base_image` from the Dockerfile's `FROM` line (pass as metadata).

---

## 10. Open Questions / Decisions for Implementation

| # | Question | Recommended answer |
|---|---|---|
| 1 | Where to store source SBOMs: security-scans bucket or platform-data bucket? | **Platform-data bucket.** Security-scans is for ADP's own scans; SBOM corpus is knowledge-layer data for customer repos. |
| 2 | How to trigger Rail 2 (image SBOM)? | **CronJob** that queries DynamoDB for `has_dockerfile=true AND sha != last_image_sbom_sha`, batches into CodeBuild invocations. Simpler than a Lambda trigger; reuses existing K8s CronJob pattern (`refresh-repos.py`). |
| 3 | Should source SBOM block on Postgres availability? | **No.** S3 upload is the durable record. Postgres insert is best-effort on first pass; a backfill job can replay from S3 if DB was down. |
| 4 | Consolidated corpus-wide SBOM? | **Defer.** The reverse index serves the same purpose. A rolled-up export can be a future reporting feature. |
| 5 | Syft version to pin? | **v1.11.1** (matches existing `bs-syft-scan.yml`). Pin in Dockerfile and buildspec. |

---

## 11. Validation Plan

| Test | Type | What it proves |
|---|---|---|
| Parse a known CycloneDX JSON → correct purl, resolution_source, ecosystem | Unit | `sbom_parser.py` logic is correct |
| Syft failure → pipeline continues, DynamoDB records `sbom_source: failed` | Unit | Non-blocking failure handling |
| Image build failure → `build_failed` marker in DB, no crash | Unit | Best-effort semantics |
| Index a repo with `requirements.txt` + `package-lock.json` → deps in Postgres | Integration | End-to-end source SBOM |
| Index a repo with a simple Dockerfile → image-layer packages tagged `source='image'` | Integration | End-to-end image SBOM |
| Reverse-index query returns correct repos for a known package | Integration | Query pattern works |
| Existing ADP-repo container scan (`bs-syft-scan.yml`) still runs unchanged | Regression | No breakage of existing security workflow |
| Coverage stats query returns accurate counts | Integration | Honest reporting |

---

## 12. Deployment Sequence

1. **Merge schema migration** → run `migrate.py` Job against RDS (creates `agent_context` DB + tables)
2. **Update ingestion image** → add Syft + `psycopg2-binary` + new scripts → push to ECR
3. **Roll KEDA ScaledJob** → new image picks up source SBOM step automatically
4. **Create CodeBuild project** for image SBOM (or add to existing `images-build` module)
5. **Deploy CronJob** for image-SBOM trigger (queries DynamoDB, invokes CodeBuild)
6. **Run ingestion** (`publish-ingestion.py --force`) to populate SBOMs for all repos
7. **Verify:** check S3 for SBOM files, query Postgres for dependency rows, confirm coverage stats

**Rollback:** Remove the SBOM step from `ingest-repo.py` (feature flag: `SBOM_ENABLED` env var, default `true`). Existing indexes/search unaffected.

---

## 13. Relationship to Sibling Sub-Issues

| Sub-issue | Dependency on this issue |
|---|---|
| Dependencies table schema | **This issue creates it.** |
| Reverse-index lookup | Consumes `dependencies` table created here |
| Vulnerability matching | Consumes SBOM files from S3 (uses Trivy, not OSV-Scanner, for CycloneDX input) |
| Watchman CVE triage | Queries reverse-index to find affected repos |
| Coverage dashboard | Queries `repositories` table for sbom_status counts |
