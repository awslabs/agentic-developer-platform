# Agent-Context Database Design Note

**Date:** 2026-06-11
**Issue:** #1355 (sub of #1345)
**Status:** Implementation-ready
**Design of record:** `docs/knowledge-layer-storage-design.md` (this note validates and refines it)

---

## 1. Decision: Shared Instance, Separate Database

The `agent_context` schema lives in a **new database** on the existing gateway RDS
instance (PostgreSQL 16.14, `bedrockgw-dev-postgres`). It is NOT a set of tables in
the gateway's `bedrockgateway` database.

**Rationale:**
- One instance = no new infrastructure cost, no new security group rules, no new
  subnet group. Pods in EKS already have network access to this instance.
- Separate database = complete namespace isolation. A login scoped to
  `agent_context` cannot `SELECT` from `bedrockgateway` tables, even accidentally.
- Escape hatch: if indexing load competes with gateway traffic, `pg_dump` the
  `agent_context` database to a new instance. Because it's a self-contained database
  (no cross-DB foreign keys), this is a lift-and-shift, not a redesign.

**What shared means in practice:**
- CPU/memory/IO on the same `db.t4g.medium`.
- Backup snapshots cover both databases (one instance-level backup).
- Parameter group settings (e.g. `shared_preload_libraries`) apply to both.
- Enhanced monitoring and Performance Insights cover both.

---

## 2. Low-Privilege Login: `agent_context_svc`

A dedicated PostgreSQL role is created with:

```sql
CREATE USER agent_context_svc;
GRANT rds_iam TO agent_context_svc;
GRANT ALL PRIVILEGES ON DATABASE agent_context TO agent_context_svc;
-- After connecting to agent_context:
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO agent_context_svc;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO agent_context_svc;
```

**Scoping:**
- `agent_context_svc` is granted `rds_iam` so it authenticates via IAM tokens
  (same mechanism as the gateway's `bgadmin`).
- It has full DDL/DML on the `agent_context` database only.
- It has **no grants** on the `bedrockgateway` database.
- It is NOT a superuser.

**IRSA mapping:** The agent-context worker pods and the Door service use a
Kubernetes service account annotated with an IAM role that has
`rds-db:connect` permission for user `agent_context_svc` on the RDS resource.

---

## 3. Schema

Four tables in the `public` schema of the `agent_context` database:

### 3.1 `repositories`

The catalog of indexed repos, with ACL and indexing state.

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| id | UUID | PK, DEFAULT gen_random_uuid() | |
| repo_name | VARCHAR(512) | NOT NULL, UNIQUE | `org/repo` format |
| git_url | VARCHAR(1024) | NOT NULL | Clone URL |
| owner | VARCHAR(256) | NOT NULL | GitHub org or user |
| allowed_principals | JSONB | NOT NULL DEFAULT '[]' | ACL: list of GitHub logins/teams |
| last_indexed_sha | VARCHAR(40) | | Latest commit SHA indexed |
| indexed_at | TIMESTAMPTZ | | When last full index completed |
| zoekt_status | VARCHAR(32) | NOT NULL DEFAULT 'pending' | One of: pending, running, complete, failed |
| vectors_status | VARCHAR(32) | NOT NULL DEFAULT 'pending' | |
| structure_status | VARCHAR(32) | NOT NULL DEFAULT 'pending' | |
| sbom_status | VARCHAR(32) | NOT NULL DEFAULT 'pending' | |
| created_at | TIMESTAMPTZ | NOT NULL DEFAULT NOW() | |
| updated_at | TIMESTAMPTZ | NOT NULL DEFAULT NOW() | |

**Indexes:**
- PK on `id`
- UNIQUE on `repo_name`
- B-tree on `owner` (filter repos by org)

### 3.2 `dependencies`

The reverse-lookup index: given a package coordinate, find all repos that use it.

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| id | UUID | PK, DEFAULT gen_random_uuid() | |
| repo_id | UUID | NOT NULL, FK -> repositories(id) ON DELETE CASCADE | |
| package_coordinate | VARCHAR(512) | NOT NULL | purl format: `pkg:pypi/requests@2.28` |
| version | VARCHAR(128) | | Resolved version |
| is_transitive | BOOLEAN | NOT NULL DEFAULT FALSE | Direct vs transitive dep |
| source | VARCHAR(16) | NOT NULL DEFAULT 'code' | 'code' or 'image' |
| base_image | VARCHAR(512) | | Only for source='image' |
| created_at | TIMESTAMPTZ | NOT NULL DEFAULT NOW() | |

**Indexes:**
- PK on `id`
- **B-tree on `package_coordinate`** (the critical reverse-lookup index)
- B-tree on `repo_id` (FK index for cascade deletes)
- UNIQUE on `(repo_id, package_coordinate, source)` to prevent duplicates

### 3.3 `vulnerabilities`

Known advisories matched against indexed packages.

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| id | UUID | PK, DEFAULT gen_random_uuid() | |
| cve_id | VARCHAR(64) | NOT NULL, UNIQUE | e.g. CVE-2024-1234 or GHSA-xxxx |
| package | VARCHAR(512) | NOT NULL | Affected package coordinate prefix |
| affected_versions | VARCHAR(512) | NOT NULL | Version range expression |
| safe_version | VARCHAR(128) | | First safe version, if known |
| severity | VARCHAR(16) | NOT NULL DEFAULT 'unknown' | critical, high, medium, low, unknown |
| details | JSONB | | Raw advisory data from OSV/Trivy |
| discovered_at | TIMESTAMPTZ | NOT NULL DEFAULT NOW() | |

**Indexes:**
- PK on `id`
- UNIQUE on `cve_id`
- B-tree on `package` (join to dependencies.package_coordinate)
- B-tree on `severity` (filter by severity)

### 3.4 `index_runs`

Append-only observability log for indexing operations.

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| id | UUID | PK, DEFAULT gen_random_uuid() | |
| repo_id | UUID | NOT NULL, FK -> repositories(id) ON DELETE CASCADE | |
| started_at | TIMESTAMPTZ | NOT NULL DEFAULT NOW() | |
| completed_at | TIMESTAMPTZ | | |
| duration_ms | INTEGER | | Computed on completion |
| status | VARCHAR(32) | NOT NULL DEFAULT 'running' | running, complete, failed |
| error | TEXT | | Error message if failed |
| steps_completed | JSONB | DEFAULT '{}' | {"zoekt": true, "vectors": false, ...} |

**Indexes:**
- PK on `id`
- B-tree on `repo_id` (find runs for a repo)
- B-tree on `started_at` (recent-first ordering)

---

## 4. Key Queries

The schema is optimized for these queries:

```sql
-- Reverse lookup: which repos use package X?
SELECT r.repo_name, d.version
FROM dependencies d
JOIN repositories r ON r.id = d.repo_id
WHERE d.package_coordinate = 'pkg:pypi/requests@2.28';

-- Global advisory: which repos are affected by CVE-X?
SELECT r.repo_name, r.owner, d.version
FROM vulnerabilities v
JOIN dependencies d ON d.package_coordinate LIKE v.package || '%'
WHERE v.cve_id = 'CVE-2024-1234';

-- ACL check: can principal X see repo Y?
SELECT 1 FROM repositories
WHERE repo_name = 'org/repo'
  AND allowed_principals @> '"username"';

-- Observability: recent failed runs
SELECT r.repo_name, ir.started_at, ir.error
FROM index_runs ir
JOIN repositories r ON r.id = ir.repo_id
WHERE ir.status = 'failed'
ORDER BY ir.started_at DESC
LIMIT 20;
```

---

## 5. Migration Strategy

Agent-context gets its **own Alembic environment**, completely independent of the
gateway's. This prevents any possibility of running agent-context migrations against
the gateway database.

**Key differences from gateway Alembic:**
- Env var prefix: `AC_RDS_*` (not `BG_RDS_*`)
- Default database: `agent_context` (not `bedrockgateway`)
- Default username: `agent_context_svc` (not `bgadmin`)
- No shared model imports — agent-context models are self-contained
- Same IAM auth mechanism, same SSL/TLS, same RDS CA bundle

**Migration numbering:** starts at `001` (independent revision chain).

---

## 6. Bootstrap Sequence

The bootstrap job (Terraform-managed Kubernetes Job, following the gateway pattern):

1. Connects to the RDS instance as the master user (reads creds from Secrets Manager)
2. Creates the `agent_context` database if not exists
3. Creates the `agent_context_svc` role if not exists
4. Grants `rds_iam` to `agent_context_svc`
5. Grants ownership of `agent_context` to `agent_context_svc`

This runs once on fresh deploy. It is idempotent (all operations are IF NOT EXISTS /
already-granted checks).

---

## 7. Fact Verification Notes

| Claim in design | Verification | Status |
|-----------------|-------------|--------|
| PostgreSQL 16 support until Nov 2028 | PostgreSQL versioning policy: 5 years from release (16 released Sep 2023 -> Nov 2028) | Confirmed |
| Existing RDS is PostgreSQL 16.14 | `modules/gateway/infra/modules/rds/main.tf` line 64: `engine_version = "16.14"` | Confirmed |
| IAM auth already enabled | `iam_database_authentication_enabled = true` in rds/main.tf line 78 | Confirmed |
| rds-bootstrap job pattern exists | `modules/gateway/infra/modules/rds-bootstrap/main.tf` | Confirmed |
| EKS -> RDS security group rule exists | Platform infra provides `rds_security_group_id` with EKS ingress | Confirmed |

---

## 8. Risk Mitigation

| Risk | Mitigation |
|------|-----------|
| Migration run against wrong DB | Separate Alembic env with `AC_RDS_*` prefix; default DB is `agent_context` |
| agent_context_svc over-privileged | Database-scoped grants only; no superuser; no grants on `bedrockgateway` |
| Heavy indexing saturates shared instance | Monitor via Performance Insights; escape hatch = dump/restore to dedicated instance |
| Package coordinate index bloat | B-tree on VARCHAR(512) is efficient for equality lookups; VACUUM handles dead tuples |
| Bootstrap job collides with gateway bootstrap | Different job name, different service account, different namespace (`agent-context` vs `bedrockgw`) |
