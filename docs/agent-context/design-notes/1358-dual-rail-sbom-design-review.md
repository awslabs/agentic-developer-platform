# Design Note: Dual-Rail SBOM Generation (#1358)

**Date:** 2026-06-11
**Author:** @agent-architect
**Status:** Implementation-ready (with phasing notes)
**Parent:** EPIC #1345 (Knowledge Layer)
**Depends on:** #1355 (PostgreSQL schema), #1353 (parallel indexing)
**Consumed by:** #1359 (vulnerability matching), #1360 (autonomous remediation)

---

## 1. Summary

Generate a software bill of materials for every indexed repo using two rails:
- **Rail 1 (source):** Run `syft dir:<clone>` inside the ingestion worker for every repo. Cheap, always-on.
- **Rail 2 (image):** For repos with Dockerfiles, build the container on CodeBuild and run `syft <image>`. Best-effort, bounded cost.

Both emit CycloneDX JSON to S3. The source SBOM's parsed dependencies are written to the PostgreSQL `dependencies` table (after #1355 ships).

---

## 2. Architecture

```
                          ┌─────────────────────────────┐
                          │   SQS Ingestion Queue       │
                          └─────────────┬───────────────┘
                                        │
                          ┌─────────────▼───────────────┐
                          │  KEDA ScaledJob Worker Pod   │
                          │  (ingest-repo.py)            │
                          │                             │
                          │  Step 5e: Source SBOM        │
                          │  syft dir:/tmp/repos/<org>/<repo> │
                          │  -o cyclonedx-json           │
                          │  → S3 (platform-data bucket) │
                          │  → Parse → Postgres deps     │
                          │    (after #1355)             │
                          │                             │
                          │  If Dockerfile found:        │
                          │  → Enqueue image-SBOM job    │
                          └─────────────┬───────────────┘
                                        │ (async trigger)
                          ┌─────────────▼───────────────┐
                          │  CodeBuild: sbom-image-scan  │
                          │  (bs-sbom-image-scan.yml)    │
                          │                             │
                          │  1. Clone repo               │
                          │  2. docker build             │
                          │  3. syft <image>             │
                          │  → S3 (security-scans bucket)│
                          │  4. Report result to DynamoDB│
                          └─────────────────────────────┘
```

---

## 3. Rail 1: Source SBOM (all repos)

### Where it runs
Inside the existing ingestion worker pod, as a new step in `ingest-repo.py` (after Step 3: clone, before/alongside Step 3: cgc analysis).

### Prerequisites
- Syft binary installed in the ingestion container image
- Clone already on disk at `CLONE_BASE/<org>/<repo>`

### Implementation

**Dockerfile change** (`modules/agent-context/images/ingestion/Dockerfile`):
```dockerfile
# Install Syft for source SBOM generation
ARG SYFT_VERSION=1.45.1
RUN curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh \
    | sh -s -- -b /usr/local/bin "v${SYFT_VERSION}"
```

**New function in `ingest-repo.py`:**
```python
def generate_source_sbom(clone_path: str, org_repo: str) -> dict[str, Any]:
    """Generate source SBOM using Syft, upload to S3."""
    safe_name = org_repo.replace("/", "-")
    sbom_path = f"/tmp/sbom-{safe_name}.source.cdx.json"

    result = subprocess.run(
        ["syft", f"dir:{clone_path}", "-o", f"cyclonedx-json={sbom_path}"],
        capture_output=True,
        timeout=300,  # 5 min max
    )
    if result.returncode != 0:
        return {"status": "failed", "error": result.stderr.decode()[:500]}

    # Upload to S3
    s3_key = f"sbom/repos/{safe_name}.source.cdx.json"
    s3.upload_file(sbom_path, PLATFORM_DATA_BUCKET, s3_key)

    # Parse dependencies (phase 2, after #1355)
    deps = parse_cyclonedx_dependencies(sbom_path)

    return {"status": "ok", "s3_key": s3_key, "dep_count": len(deps)}
```

### S3 path convention
```
s3://<platform-data-bucket>/sbom/repos/<org>-<repo>.source.cdx.json
```

### Cost
Negligible. Syft source scan reads manifest/lockfiles from the already-cloned repo. Takes 5-30 seconds per repo. No Docker, no network, no build.

---

## 4. Rail 2: Image SBOM (Dockerfile repos, best-effort)

### Where it runs
AWS CodeBuild (already has Docker daemon, privileged mode).

### Key design decision: Per-repo invocation, not the ADP-only pattern

The existing `bs-syft-scan.yml` scans the ADP repo's own Dockerfiles from the source zip. For indexed external repos, we need a **new buildspec** parameterized per repo:

**New file: `codebuild/bs-sbom-image-scan.yml`**
```yaml
version: 0.2
env:
  variables:
    SYFT_VERSION: "1.45.1"
    REPO_URL: ""        # Override at start-build time
    REPO_NAME: ""       # org-repo (slug)
    DOCKERFILE_PATH: "" # Relative path (e.g., "Dockerfile" or "services/api/Dockerfile")
phases:
  install:
    commands:
      - curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh | sh -s -- -b /usr/local/bin "v${SYFT_VERSION}"
  pre_build:
    commands:
      - git clone --depth=1 "${REPO_URL}" /src
      - DATE=$(date -u +%Y/%m/%d)
      - RUN_ID="${CODEBUILD_BUILD_ID##*:}"
  build:
    commands:
      - |
        BUILD_DIR=$(dirname "/src/${DOCKERFILE_PATH}")
        IMAGE_TAG="sbom-scan:${REPO_NAME}"
        KEY="sbom/images/${DATE}/${RUN_ID}/${REPO_NAME}.image.cdx.json"

        if ! docker build --no-cache --pull -t "${IMAGE_TAG}" "${BUILD_DIR}" 2>&1 | tail -20; then
          echo "BUILD_FAILED"
          # Record failure (non-fatal)
          echo '{"status":"build_failed","repo":"'"${REPO_NAME}"'"}' > /tmp/result.json
          exit 0
        fi

        syft "${IMAGE_TAG}" -o cyclonedx-json --file "/tmp/sbom.cdx.json"
        aws s3 cp "/tmp/sbom.cdx.json" "s3://${SECURITY_SCANS_BUCKET}/${KEY}" \
          --content-type application/json \
          --metadata "repo=${REPO_NAME},dockerfile=${DOCKERFILE_PATH}"
        echo '{"status":"ok","repo":"'"${REPO_NAME}"'","s3_key":"'"${KEY}"'"}' > /tmp/result.json

        docker rmi "${IMAGE_TAG}" 2>/dev/null || true
```

### Triggering the image scan

The ingestion worker (after generating the source SBOM) checks for Dockerfiles:
```python
def check_and_trigger_image_sbom(clone_path: str, org_repo: str):
    """If repo has Dockerfile(s), trigger CodeBuild image scan."""
    dockerfiles = list(Path(clone_path).rglob("Dockerfile"))
    # Exclude vendor, node_modules, test fixtures
    dockerfiles = [d for d in dockerfiles if not any(
        p in d.parts for p in ("node_modules", "vendor", ".git", "test", "fixtures")
    )]

    if not dockerfiles:
        return {"image_sbom": "no_dockerfile"}

    # SHA-gate: skip if unchanged since last scan
    current_sha = get_repo_sha(clone_path)
    last_scanned_sha = get_last_image_sbom_sha(org_repo)  # from DynamoDB state
    if current_sha == last_scanned_sha:
        return {"image_sbom": "unchanged"}

    # Trigger CodeBuild for the primary Dockerfile only (root preferred)
    primary_df = select_primary_dockerfile(dockerfiles, clone_path)
    trigger_codebuild_image_scan(org_repo, primary_df)
    return {"image_sbom": "triggered"}
```

### Concurrency guardrails

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Max concurrent image builds | 5 | Reserve CodeBuild capacity for other projects (10 defined) |
| Per-build timeout | 15 minutes | Most successful builds complete in <5min; 15min catches edge cases |
| Retry policy | None | Best-effort; failure recorded, not retried |
| SHA-gate | Only build if repo HEAD changed since last image SBOM | Avoids redundant builds |

### S3 path convention
```
s3://<security-scans-bucket>/sbom/images/YYYY/MM/DD/<run-id>/<org>-<repo>.image.cdx.json
```

---

## 5. Postgres integration (phased — after #1355)

### Phase 1 (this issue): Generate + store in S3
- Source SBOM → `s3://<platform-data>/sbom/repos/<slug>.source.cdx.json`
- Image SBOM → `s3://<security-scans>/sbom/images/...`

### Phase 2 (after #1355): Parse + write to Postgres

Parse the CycloneDX JSON and write rows to the `dependencies` table:

```python
def parse_cyclonedx_dependencies(sbom_path: str) -> list[dict]:
    """Parse CycloneDX JSON into dependency records."""
    with open(sbom_path) as f:
        sbom = json.load(f)

    deps = []
    for component in sbom.get("components", []):
        purl = component.get("purl", "")
        if not purl:
            continue
        deps.append({
            "package_coordinate": purl,
            "version": component.get("version", ""),
            "is_transitive": component.get("scope") == "optional",
            "source": "source",  # or "image" for Rail 2
            "resolution_source": _infer_resolution_source(component),
        })
    return deps
```

The `resolution_source` is inferred from CycloneDX's `evidence.identity` or the presence of a lockfile in the `properties` array.

---

## 6. Coverage reporting

After each indexing pass, update a coverage summary:

```python
coverage = {
    "total_repos": 100,
    "source_sbom_success": 98,      # Syft found at least one dependency
    "source_sbom_empty": 2,          # Syft ran but found nothing (expected for empty repos)
    "image_sbom_success": 35,        # Docker build + scan succeeded
    "image_sbom_build_failed": 25,   # Docker build failed (normal for study repos)
    "image_sbom_no_dockerfile": 40,  # No Dockerfile in repo
    "last_updated": "2026-06-12T..."
}
```

Stored in DynamoDB state table (key: `coverage#sbom`, record_type: `STATE`).

---

## 7. File-level changes

| File | Action | Description |
|------|--------|-------------|
| `modules/agent-context/images/ingestion/Dockerfile` | Modify | Add Syft install |
| `modules/agent-context/images/ingestion/ingest-repo.py` | Modify | Add `generate_source_sbom()` step |
| `codebuild/bs-sbom-image-scan.yml` | Create | Per-repo image SBOM buildspec |
| `platform/infra/modules/codebuild/main.tf` | Modify | Add `sbom-image-scan` project with 15min timeout |
| `modules/agent-context/terraform/modules/s3-files/main.tf` | Modify | Add `sbom/` prefix |
| `modules/agent-context/manifests/ingestion-scaledjob.yaml` | Modify | Add `PLATFORM_DATA_BUCKET` env var |
| `modules/agent-context/images/ingestion/sbom_utils.py` | Create | Shared SBOM parsing + CodeBuild trigger utilities |

---

## 8. IAM changes

| Role | New grant | Scope |
|------|-----------|-------|
| `agent-context-irsa` | `codebuild:StartBuild` | `arn:aws:codebuild:<region>:<account>:project/<prefix>-sbom-image-scan` |
| `agent-context-irsa` | `rds-db:connect` | (Phase 2, after #1355) `arn:aws:rds-db:<region>:<account>:dbuser:*/agent_context_writer` |
| CodeBuild shared role | Already sufficient | `s3:PutObject` on `sbom/*` already granted |

---

## 9. Open decisions for implementer

1. **Syft version alignment:** Current buildspec uses 1.11.1; latest is 1.45.1. Recommend upgrading both to 1.45.1.
2. **Primary Dockerfile selection:** When a repo has multiple Dockerfiles, scan only the root one (or the shortest path). Document the heuristic.
3. **Multi-stage builds that require secrets:** These will fail. Record as `build_failed` — never pass credentials to indexed repos' builds.
4. **Git auth for private repos in CodeBuild:** Use the same GitHub App token that the ingestion worker uses. Pass as `REPO_URL=https://x-access-token:<token>@github.com/<org>/<repo>`.

---

## 10. Sequencing

```
#1353 (parallel indexing + single fetcher) ← prerequisite for scale
    │
    ▼
#1358 Phase 1: Source SBOM in worker + Image SBOM on CodeBuild (S3 only)
    │
    ▼
#1355 (PostgreSQL schema + connectivity)
    │
    ▼
#1358 Phase 2: Parse CycloneDX → write dependencies rows
    │
    ▼
#1359 (Vulnerability matching) reads from dependencies table + SBOMs
```

Phase 1 is independently shippable. Phase 2 is a follow-up PR after #1355 lands.
