# Knowledge Layer — Functional Testing Strategy

> **Owns the seams between components.** Each sibling issue (#1346–#1360) implements
> one component; this document defines how we prove they compose correctly —
> especially the fail-closed permission filter and the vulnerability→fix loop.

## 1. Test Layers

| Layer | Runs In | Proves | Marker |
|-------|---------|--------|--------|
| **Unit** | CI, every PR | Pure logic: chunkers, purl parsing, ACL filter decision, clone-URL builder (no token in logs), SBOM→rows mapping, reverse-index SQL generation | `@pytest.mark.unit` (default) |
| **Component / Integration** | CI, every PR (seeded fixtures, mocked AWS) | A single component end-to-end against a fixture: Zoekt index+query, S3 Vectors write+query (mock adapter), reverse-lookup SQL, OSV-Scanner on fixture SBOM | (default — no marker needed) |
| **Live / E2E** | dev environment, gated by `@pytest.mark.live` | Real stores wired together: ingest a fixture repo → query via the Door; planted CVE → fix issue filed | `@pytest.mark.live` or `@pytest.mark.live_only` |
| **Smoke** | Operator, post-deploy | One command proving the module is up and serving queries | Manual (see §7) |

### Why this layering

- **Unit** catches logic regressions instantly — no infra needed, < 5 s total.
- **Component** catches integration contract drift (e.g. Zoekt response shape
  changed) without needing a deployed cluster — runs with fixtures + mocks.
- **Live** catches the seams that mocks can't: real IAM, real S3 Vectors latency,
  real Bedrock embedding dimensions, real Postgres concurrent writes.
- **Smoke** is the operator's confidence check after deploy — not a test suite.

## 2. Store Testability Assessment

| Store | Local/Mock Strategy | Live Strategy |
|-------|--------------------:|---------------|
| **Zoekt (exact search)** | Spawn ephemeral Zoekt container in CI (docker-compose); index a 3-file fixture repo; query via HTTP API. Deterministic, fast. | Query deployed Zoekt via `openviking` service. |
| **S3 Vectors (semantic)** | **No local emulator exists** (LocalStack does not support `s3vectors`). Use a thin `VectorStoreAdapter` interface with an in-memory fake (`dict[str, list[float]]`) for unit/component tests. The adapter MUST match the real client's `put_vectors`/`query_vectors` call shapes. | Hit real S3 Vectors in dev (gated by `@pytest.mark.live`). Verify recall survives pod restart. |
| **Structural (S3 JSON)** | Use `tmp_path` fixture as the S3 mount point; read/write JSON. Moto or localstack for S3 API if needed. | Hit real S3-backed PVC (`/platform-data/code-indexes/`). |
| **SBOM (S3 + Postgres)** | Fixtures: a pre-generated CycloneDX JSON. Postgres: use `testcontainers` or an in-memory SQLite (schema-compatible subset) for the `dependencies` table. | Real RDS in dev. |

### S3 Vectors mock design

```python
class FakeVectorStore:
    """In-memory vector store for unit/component tests."""

    def __init__(self):
        self._vectors: dict[str, dict] = {}  # key → {embedding, metadata}

    def put_vectors(self, index_name: str, vectors: list[dict]) -> None: ...
    def query_vectors(self, index_name: str, query_vector: list[float], top_k: int) -> list[dict]: ...
    def delete_vectors(self, index_name: str, keys: list[str]) -> None: ...
```

Component tests use this fake; live tests use the real `boto3` `s3vectors` client.
The production code accepts a `VectorStoreProtocol` (duck-typed or Protocol class).

## 3. Vulnerability Scanner Testability

| Tool | Deterministic CI Strategy |
|------|---------------------------|
| **OSV-Scanner** | `--offline` mode with a pinned local database snapshot. Scan a fixture lockfile (`tests/fixtures/vulnerable-requirements.txt`) containing a known-bad version. Verify it reports exactly the planted CVE. |
| **Trivy** | `trivy sbom --skip-db-update ./tests/fixtures/planted-vuln.cdx.json` with a cached/bundled DB. Alternatively, pin Trivy version + DB date in CI for reproducibility. Verify it reports the planted OS-layer CVE. |

**Important**: both tools query online databases by default. For deterministic CI:
- Pin tool versions in Dockerfile / CI step.
- Use `--offline` (OSV-Scanner) or `--skip-db-update` + cached DB (Trivy).
- Fixture SBOMs contain known-bad versions that will always be flagged.

## 4. Permission Testing — Fail-Closed Guarantee

This is the **highest-priority test surface**. A miss here means cross-tenant data exposure.

### Unit tests (pure logic, no I/O)

| # | Test Case | Proves |
|---|-----------|--------|
| P1 | Two principals, two private repos: filter returns only caller's repos | Positive isolation |
| P2 | Unknown/missing principal → empty result set (never fail-open) | Fail-closed default |
| P3 | Public/OSS repos remain visible to all principals | Public visibility preserved |
| P4 | Permission change on re-ingest: repo removed from principal's ACL → next query excludes it | ACL freshness |
| P5 | Malformed identity header → rejection (not silent pass-through) | Input validation |
| P6 | Empty ACL for a repo (no one granted) → repo invisible to all | Edge case: orphan repo |

### Live tests (real Postgres + real GitHub App)

| # | Test Case | Proves |
|---|-----------|--------|
| P7 | Ingest private repo → query as authorized user → results returned | Happy path |
| P8 | Ingest private repo → query as unauthorized user → zero results | Live isolation |
| P9 | Change repo collaborator on GitHub → re-ingest → ACL reflects change | End-to-end freshness |

## 5. Vulnerability Loop Testing — Planted-CVE → Fix

The flagship E2E test proves the full autonomous remediation loop:

### Setup (fixture)
- A seeded fixture repo containing:
  - `requirements.txt` with `requests==2.25.0` (CVE-2023-32681 — SSRF via redirect)
  - A Python file that `import requests` and calls `requests.get(user_input)` (reachable usage)
  - A passing test suite (`pytest` green)
  - A `Dockerfile` with a known-vulnerable base image (for Trivy rail)

### Test steps (live, gated)

| Step | Asserts |
|------|---------|
| 1. Ingest fixture repo | SBOM generated; `dependencies` table has `pkg:pypi/requests@2.25.0` |
| 2. Run OSV-Scanner on SBOM | Detects CVE-2023-32681; row inserted into `vulnerabilities` table |
| 3. Reverse lookup | `SELECT repo_id FROM dependencies WHERE package = 'pkg:pypi/requests' AND version = '2.25.0'` returns exactly the fixture repo |
| 4. Reachability check | Structural index confirms `requests.get` is called from application code (not dead import) |
| 5. Triage gate | Reachable → issue filed. (Contrariwise: a dead-import fixture → NO issue filed) |
| 6. Fix verification | Developer agent opens PR bumping `requests>=2.31.0`; PR tests pass; PR is NOT auto-merged |

### Unit tests (mocked, CI)

| # | Test Case | Proves |
|---|-----------|--------|
| V1 | Reverse lookup SQL: seeded deps → correct repo list | Query correctness |
| V2 | OSV-Scanner fixture scan → expected CVE ID returned | Scanner integration |
| V3 | Trivy fixture scan → expected OS-layer CVE returned | Scanner integration |
| V4 | Triage: unreachable symbol → no issue filed | False-positive suppression |
| V5 | Triage: reachable symbol → exactly one issue per affected repo | Correct fan-out |
| V6 | Duplicate CVE (same pkg+ver already reported) → no duplicate issue | Idempotency |

## 6. Write Path & Index Testing

### Write path (indexing pipeline)

| # | Test Case | Proves | Layer |
|---|-----------|--------|-------|
| W1 | Unchanged SHA → no enqueue | Skip logic | Unit |
| W2 | Changed SHA → exactly one SQS message | Enqueue logic | Unit |
| W3 | Clone uses App token for private repos; anonymous for public | Auth path | Unit |
| W4 | GitHub token never appears in any log line | Security | Unit |
| W5 | N changed repos → N parallel workers → all complete | Fan-out | Live |
| W6 | Worker crash → message returns to queue (not lost) | Retry semantics | Live |

### Four indexes

| # | Test Case | Proves | Layer |
|---|-----------|--------|-------|
| I1 | Zoekt: unique token in fixture → correct file+line returned | Exact search correctness | Component |
| I2 | Zoekt: result shape matches MCP `search` contract | Contract compliance | Component |
| I3 | S3 Vectors: concept query → expected function returned | Semantic recall | Live |
| I4 | S3 Vectors: recall survives pod restart | Durability (no in-process-dict gap) | Live |
| I5 | Structural: `understand` returns known function location | Structure correctness | Component |
| I6 | Structural: `impact` returns in-repo callers | Call-graph correctness | Component |
| I7 | Structural: cgc failure → falls back to tree-sitter | Graceful degradation | Component |
| I8 | SBOM source: fixture lockfile → expected deps with correct purls | Source SBOM | Unit |
| I9 | SBOM image: buildable Dockerfile → OS packages tagged `image` | Image SBOM | Live |
| I10 | SBOM image: unbuildable repo → `build_failed` marker (not crash) | Error resilience | Unit |

## 7. Smoke Test (post-deploy)

After deploying the Knowledge Layer to dev, run:

```bash
# 1. MCP endpoint is alive
curl -sf "http://context-mcp.agent-context.svc:5100/tools" | jq '.[] | .name'
# Expected: search, understand, impact, browse, remember, experience

# 2. Search returns results for an indexed repo
curl -sf -X POST "http://context-mcp.agent-context.svc:5100/call" \
  -H 'Content-Type: application/json' \
  -d '{"tool": "search", "arguments": {"query": "main", "scope": "code", "limit": 3}}' | jq '.results | length'
# Expected: > 0

# 3. Ingestion CronJob exists and ran successfully
kubectl get cronjob ingestion-refresh -n agent-context -o jsonpath='{.status.lastSuccessfulTime}'
# Expected: timestamp within last 24h
```

## 8. Regression Guardrails

| Existing test file | Must continue to pass | Why |
|--------------------|----------------------|-----|
| `personal_context/tests/test_isolation.py` | All tests green | Personal-context isolation must not regress |
| `personal_context/tests/test_experience_tool.py` | All tests green | Experience tool API unchanged |
| `tests/e2e/test_mcp_endpoint.py` | All tests green | MCP contract unchanged |
| `tests/e2e/test_platform_health.py` | All tests green | Platform resources still healthy |

## 9. Coverage Bar

- **Target**: >= 80% line coverage for new Knowledge Layer code paths
- **Non-negotiable**: 100% branch coverage for the permission filter (`acl_filter.py` or equivalent)
- **Flaky test policy**: no test may use `pytest-rerunfailures` or raise the retry threshold to pass. Flaky tests are bugs.

## 10. CI Integration

```yaml
# In agent-context CI workflow (PR gate):
- name: Unit + Component tests
  run: |
    cd modules/agent-context
    pip install -e ".[all]"
    pytest tests/ -m "not live_only and not graphrag and not workflow" --cov --cov-branch --cov-fail-under=80

# In dev environment (post-merge or scheduled):
- name: Live/E2E tests
  env:
    TEST_ENV: dev
  run: |
    cd modules/agent-context
    pip install -e ".[all,live]"
    pytest tests/ -m "live or live_only" --timeout=300
```

## 11. Sibling Issue → Test Case Mapping

| Sibling Issue | Test Cases | Notes |
|---------------|-----------|-------|
| #1346 (Scheduler / enqueue) | W1, W2 | Changed-SHA detection |
| #1347 (Single-fetch clone) | W3, W4 | Token handling |
| #1348 (Parallel fan-out) | W5, W6 | KEDA + SQS retry |
| #1353 (Zoekt exact search) | I1, I2 | Index + MCP contract |
| #1354 (S3 Vectors semantic) | I3, I4 | Recall + durability |
| #1355 (Structural index) | I5, I6, I7 | understand/impact + fallback |
| #1356 (Permission filter) | P1–P9 | Fail-closed guarantee |
| #1357 (Structural → S3) | I5, I6 | Storage durability |
| #1358 (Dual-rail SBOM) | I8, I9, I10 | Source + image SBOMs |
| #1359 (Reverse lookup + vuln) | V1, V2, V3 | Detection + lookup |
| #1360 (Triage + fix loop) | V4, V5, V6, E2E | Reachability gate + flagship |

## 12. Test Fixture Inventory

| Fixture | Location | Contents |
|---------|----------|----------|
| `fixture-repo-private/` | `tests/fixtures/` | 3 Python files, `requirements.txt` with planted vuln, passing tests, Dockerfile |
| `fixture-repo-public/` | `tests/fixtures/` | Small OSS repo (no access restriction) |
| `planted-vuln.cdx.json` | `tests/fixtures/` | CycloneDX SBOM with `requests==2.25.0` (CVE-2023-32681) |
| `vulnerable-requirements.txt` | `tests/fixtures/` | Lockfile with known-bad version |
| `code-index-fixture.json` | `tests/fixtures/` | Pre-computed structural index for unit tests |
| `acl-fixture.json` | `tests/fixtures/` | Permission snapshot (2 principals, 2 repos) |

---

## References

- Design of record: `docs/knowledge-layer-storage-design.md`
- Parent EPIC: #1345
- Existing test harness: `modules/agent-context/tests/conftest.py`
- Personal-context isolation pattern: `personal_context/tests/test_isolation.py`
