# Design Note: Parallel Indexing Scale-Up + Single Authenticated Fetcher

**Issue**: #1353 (sub of EPIC #1345)
**Date**: 2026-06-11
**Status**: Implementation-ready design
**Author**: @agent-architect
**Design of record**: `docs/knowledge-layer-storage-design.md` (§5 indexing flow)

---

## 1. Summary

This design scales the agent-context ingestion pipeline from 10 to 50 concurrent
workers, and consolidates the current anonymous git clone into a **single
authenticated clone** per repo that all downstream indexers share. These two
changes are prerequisites for the Knowledge Layer: private repos cannot be
indexed without auth, and 500-repo indexing is impractical at 10 workers.

---

## 2. Current State (What Exists Today)

| Component | File | Behavior |
|-----------|------|----------|
| Clone | `images/ingestion/ingest-repo.py` L253-270 | Anonymous `git clone --depth=1 https://github.com/{org}/{repo}` — public repos only |
| Worker | `images/ingestion/sqs-worker.py` | One pod per SQS message, routes to `ingest-repo.py` subprocess |
| ScaledJob | `manifests/ingestion-scaledjob.yaml` | `maxReplicaCount: 10`, `queueLength: "1"` |
| Scratch | ScaledJob volumes | `emptyDir` (10Gi) at `/tmp`; `platform-data` PVC mounted at `/platform-data` |
| Token script | `scripts/github-app-token.py` | Generates GitHub App installation tokens from Secrets Manager; currently invoked by Sourcebot init container only |
| Publisher | `images/ingestion/publish-ingestion.py` | SHA-checks via `git ls-remote` + DynamoDB state; enqueues only changed repos |

**Problems:**
1. Clone is anonymous → private repos fail silently
2. `maxReplicaCount: 10` → 500-repo batch takes hours
3. Token script is wired only to Sourcebot (being removed per EPIC #1345)
4. Clone URL is logged in error messages (line 266: `e.stderr.decode()[:300]`)

---

## 3. Design

### 3.1 Single Authenticated Clone

**Goal**: One `git clone` per worker, authenticated via GitHub App token, used by
all downstream steps.

#### Token Acquisition

The existing `scripts/github-app-token.py` already handles:
- Reading App ID + private key from Secrets Manager (`adp/gh-app-ops-id`, `adp/gh-app-ops-key`)
- JWT generation + installation token exchange
- Writing token to a file (`--output-file`)

**Change**: Invoke this script at the START of `sqs-worker.py` (before calling
`ingest-repo.py`), writing the token to `/tmp/github-token`. Then pass it to
`ingest-repo.py` via environment variable `GIT_ASKPASS` pointing to a minimal
credential helper.

#### Credential Helper (Safe Token Injection)

Instead of embedding the token in the clone URL (which would leak it in logs,
error messages, and `.git/config`), use git's credential helper protocol:

```bash
#!/bin/sh
# /app/git-credential-helper.sh
# Called by git via GIT_ASKPASS — prints the token for the password prompt.
# The token file is written with 0600 permissions by github-app-token.py.
cat /tmp/github-token
```

Configure git to use this via environment variables (no global config mutation):
```
GIT_ASKPASS=/app/git-credential-helper.sh
GIT_TERMINAL_PROMPT=0
```

The clone URL remains `https://github.com/{org}/{repo}` (no embedded token).

#### Security Properties

| Property | How enforced |
|----------|--------------|
| Token never in clone URL | URL is always `https://github.com/{org}/{repo}` |
| Token never logged | `GIT_ASKPASS` is not expanded in git error messages; git stderr shows `fatal: Authentication failed for 'https://github.com/...'` without the credential |
| Token never in `.git/config` | `GIT_ASKPASS` is an env var, not stored in config |
| Token short-lived | Installation tokens expire in 1 hour (GitHub default) |
| Token file permissions | Written via `os.open(..., 0o600)` (already implemented in `github-app-token.py` L222) |
| Token not shared between pods | Each pod runs its own token-mint; emptyDir is per-pod |

#### Graceful Degradation

If token acquisition fails (Secrets Manager unreachable, App not installed):
1. Log a WARNING (not the token itself)
2. Attempt anonymous clone anyway (works for public repos)
3. If anonymous clone also fails, mark the repo as `clone_failed` in DynamoDB

This ensures public-repo indexing doesn't regress if the GitHub App isn't wired yet.

### 3.2 Per-Worker Scratch Isolation

**Current**: ScaledJob already uses `emptyDir` at `/tmp` (10Gi) per pod.
`CLONE_BASE=/tmp/repos` and `CODE_INDEX_DIR=/tmp/code-indexes` are set in the
manifest (lines 64-67).

**Change**: No structural change needed — the existing per-pod emptyDir already
provides isolation. Each KEDA-spawned Job gets its own emptyDir that is
inaccessible to other pods and destroyed on Job completion.

**Validation**: Confirm that `CLONE_BASE` is set to `/tmp/repos` (per-pod
ephemeral) and NOT `/platform-data/repos` (shared PVC). The current manifest
already does this correctly (L64-65).

**Note on the shared `platform-data` PVC**: The PVC is still mounted (for
writing finished code-indexes to the shared filesystem). This is fine — workers
write unique files (`{org}-{repo}.json`) with no cross-pod contention. However,
for the Knowledge Layer migration (EPIC #1345), this PVC will be replaced by S3
(Mountpoint or direct upload) — that's a separate sub-issue.

### 3.3 KEDA Scale-Up

#### Scale Ceiling Analysis

The constraining factor for parallel workers is **downstream write throughput**:

| Downstream | Current quota | Per-worker usage | At 50 workers |
|-----------|---------------|------------------|---------------|
| S3 Vectors (future) | 2,500 vectors/s/index | ~50-100 vectors/s (one repo's functions) | 2,500-5,000 vectors/s → **needs 2 index shards** |
| OpenViking API | Internal, not rate-limited | 2-3 requests/repo | 100-150 req/s → fine |
| DynamoDB (state) | On-demand, auto-scales | 2-3 writes/repo | 100-150 WCU → fine |
| Neptune (GraphRAG) | HTTP API, not yet enabled | 50-200 writes/repo | 2,500-10,000 req/s → **needs batching** |
| SQS | Effectively unlimited | 1 read + 1 delete/pod | Fine |
| GitHub API (clone) | 5,000 req/h per installation | 1 clone/pod | 50 concurrent = fine (not rate-limited per-clone) |
| GitHub API (`ls-remote` in publisher) | 5,000 req/h | 1 per repo at publish time | Sequential, fine |

**Conclusion**: `maxReplicaCount: 50` is safe today. The S3 Vectors sharding
concern applies to the *future* semantic indexing sub-issue (not this PR). Neptune
batching is gated behind `GRAPHRAG_ENABLED=false` (current default).

#### Recommended ScaledJob Changes

```yaml
# manifests/ingestion-scaledjob.yaml
spec:
  # ...
  pollingInterval: 10          # unchanged (10s)
  maxReplicaCount: 50          # was: 10
  successfulJobsHistoryLimit: 3 # was: 5 (reduce clutter at higher scale)
  failedJobsHistoryLimit: 10   # was: 5 (more failed history for debugging)
```

Keep `queueLength: "1"` (one pod per message). This is the correct setting for
a ScaledJob — KEDA divides queue depth by queueLength to determine desired
replicas. With queueLength=1, 50 messages = 50 pods (capped at max).

#### Cost Bounding

- Workers run only while processing (ScaledJob scales to 0 when queue is empty)
- Each worker uses ephemeral scratch (no persistent PVC per worker)
- EKS Auto Mode provisions nodes on-demand; pods are bursty, not sustained
- SQS visibility timeout (900s = 15min) provides a natural per-message ceiling

### 3.4 Integration with Downstream Indexers

Per the design of record (§5), the single clone on scratch disk feeds four
downstream producers:

```
                    ┌→ Zoekt index builder (exact search)
Worker pod:         │
  /tmp/repos/org/repo ─┼→ Structure-map analyzer (code-index.json)
                    │
                    ├→ Embedding chunker (semantic / S3 Vectors)
                    │
                    └→ SBOM generator (Syft → S3 + Postgres)
```

**This issue** establishes the clone and auth. The four producers are separate
sub-issues under EPIC #1345. The contract between this issue and those is:

- **Clone path**: `$CLONE_BASE/{org}/{repo}` (env var, defaults to `/tmp/repos`)
- **Repo identity**: Passed as `--repo org/repo` argument
- **Token available at**: `/tmp/github-token` (or env var `GITHUB_TOKEN`)
- **Producer responsibility**: Read-only access to the clone tree; write outputs
  to their respective stores (S3, S3 Vectors, Postgres)

---

## 4. File-Level Changes

| File | Change |
|------|--------|
| `images/ingestion/ingest-repo.py` | Add `GITHUB_TOKEN` env var support to `git_clone()`; use credential helper instead of embedding token in URL; suppress token from error logging |
| `images/ingestion/sqs-worker.py` | Add token-mint step before dispatching to ingestion scripts; write token to `/tmp/github-token`; set `GIT_ASKPASS` env |
| `images/ingestion/git-credential-helper.sh` | **New file** — minimal shell script that prints the token file contents |
| `manifests/ingestion-scaledjob.yaml` | Raise `maxReplicaCount` to 50; add env vars for GitHub App secret names; adjust history limits |
| `images/ingestion/Dockerfile` | Copy `git-credential-helper.sh` into image; ensure it's executable |

### Files NOT Changed (intentional non-changes)

| File | Why not |
|------|---------|
| `scripts/github-app-token.py` | Already correct — no modifications needed |
| `publish-ingestion.py` | Already does SHA-check via `git ls-remote`; no auth needed for `ls-remote` on public repos; private-repo `ls-remote` auth is a follow-up |
| `refresh-repos.py` | Daily refresh CronJob delegates to publisher → SQS; no direct changes |

---

## 5. External Dependency Verification

Research conducted 2026-06-11 against live AWS docs and GitHub repos.

### 5.1 Amazon S3 Vectors

| Claim in design doc | Verified status |
|---------------------|-----------------|
| GA and usable | **Confirmed** — product page live, docs published, "Get started" links to console |
| Region availability | **Confirmed** us-east-1 (our deploy region) is supported |
| ~2,500 writes/s/index | **Confirmed exactly** — docs state "Combined vectors inserted and deleted per second per vector index: Up to 2,500" |
| Drives index sharding | **Confirmed** — at 50 workers × ~50-100 vectors/s each = 2,500-5,000/s → need 2+ shards |

Additional quotas discovered:
- Max dimensions: **4,096** (sufficient for common embedding models: Titan=1,024, Cohere=1,024, text-embedding-3-large=3,072)
- Max vectors per index: **2 billion** (no concern for code indexing)
- PutVectors batch: up to **500 vectors per call** (design should batch to this)
- Filterable metadata: up to **2 KB per vector**, 50 keys max (sufficient for repo/file/permissions tags)
- Top-K per query: max **100** (adequate for code search)

**No corrections needed** — the design's assumption about the 2,500 writes/s limit is precisely correct.

### 5.2 Mountpoint for Amazon S3

| Claim | Verified status |
|-------|-----------------|
| GA | **Confirmed** — blog "Generally Available and Ready for Production Workloads" |
| Write-once/no-locking | **Confirmed** — "sequential write operations for creating new files"; no overwrite; no locking |
| CSI driver exists | **Confirmed** — `awslabs/mountpoint-s3-csi-driver` v2.6.0, Apache-2.0, EKS Add-on |

**Critical note for this issue**: Mountpoint is **not suitable for the git
scratch disk** (git requires file locking and random writes). The design of record
correctly calls for a "temporary local disk" (emptyDir/EBS) for scratch. Mountpoint
is only for finished outputs (Zoekt index shards, structure maps). This distinction
is already correctly reflected in the ScaledJob manifest (emptyDir for `/tmp`,
PVC for `/platform-data`).

### 5.3 OSV-Scanner

| Claim in design doc | Verified status |
|---------------------|-----------------|
| Apache-2.0 | **Confirmed** |
| Actively maintained | **Confirmed** — v2.3.8 (May 2026), 1,933 commits |
| Consumes CycloneDX SBOM | **INCORRECT** — OSV-Scanner does NOT accept SBOM files as input; it scans lockfiles/manifests directly |

**Correction needed in design doc**: §7.2 implies we generate an SBOM (Syft) and
feed it to OSV-Scanner. In practice, OSV-Scanner scans repos directly from their
lockfiles. The correct flow is:
- **Syft** → generates SBOM (CycloneDX) → stored in S3 as the official record
- **OSV-Scanner** → scans the repo clone directly (reads lockfiles in-tree) → produces vulnerability findings
- **Trivy** → scans container images (for the image SBOM path)

This doesn't change the architecture — Syft still produces the SBOM for the
reverse-lookup table, and OSV-Scanner still does the vulnerability matching —
but they don't chain through a file handoff. Both read the same clone tree.

### 5.4 Trivy

| Claim | Verified status |
|-------|-----------------|
| Apache-2.0 | **Confirmed** |
| Covers OS/base-image layers | **Confirmed** — scans container images, Kubernetes, repos |
| Actively maintained | **Confirmed** — 2025 copyright, active development |

### 5.5 PostgreSQL 16

| Claim | Verified status |
|-------|-----------------|
| Support through ~Nov 2028 | **Confirmed** — PostgreSQL 16 has 5-year support (released Sept 2023 → EOL Nov 2028) |

---

## 6. Detailed Implementation Spec

### 6.1 `sqs-worker.py` — Token Mint Integration

Add at the top of `main()`, before message processing:

```python
def _mint_github_token() -> bool:
    """Mint a GitHub App token and write to /tmp/github-token.
    
    Returns True if token was successfully obtained, False otherwise.
    Failure is non-fatal — anonymous clones still work for public repos.
    """
    app_id_secret = os.getenv("GITHUB_APP_ID_SECRET", "")
    app_key_secret = os.getenv("GITHUB_APP_KEY_SECRET", "")
    
    if not app_id_secret or not app_key_secret:
        log.info("No GitHub App secrets configured — using anonymous clones")
        return False
    
    try:
        result = subprocess.run(
            [
                sys.executable, "/app/../scripts/github-app-token.py",
                "--app-id-secret", app_id_secret,
                "--app-key-secret", app_key_secret,
                "--region", AWS_REGION,
                "--output-file", "/tmp/github-token",
            ],
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0:
            log.info("GitHub App token minted successfully")
            # Set GIT_ASKPASS for child processes
            os.environ["GIT_ASKPASS"] = "/app/git-credential-helper.sh"
            os.environ["GIT_TERMINAL_PROMPT"] = "0"
            return True
        else:
            stderr = result.stderr.decode()[:200]
            log.warning("GitHub App token mint failed: %s", stderr)
            return False
    except subprocess.TimeoutExpired:
        log.warning("GitHub App token mint timed out")
        return False
    except Exception as e:
        log.warning("GitHub App token mint error: %s", e)
        return False
```

**Call site**: Invoke `_mint_github_token()` once at the start of `main()`,
after validating `SQS_QUEUE_URL` but before `receive_sqs_message()`. The token
is then available to all child processes (`ingest-repo.py`) via the inherited
`GIT_ASKPASS` environment variable.

### 6.2 `ingest-repo.py` — Secure Clone

Replace the `git_clone()` function to ensure:
1. Never embed token in URL
2. Never log the token or auth-bearing URL
3. Sanitize stderr from git commands before logging

```python
def git_clone(repo_url: str, dest: str) -> bool:
    """Clone a repo using GIT_ASKPASS for auth. Returns True on success."""
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    
    env = os.environ.copy()
    # GIT_ASKPASS is already set by sqs-worker if token is available
    # Ensure terminal prompt is disabled regardless
    env["GIT_TERMINAL_PROMPT"] = "0"
    
    try:
        subprocess.run(
            ["git", "clone", "--depth=1", repo_url, dest],
            check=True,
            capture_output=True,
            timeout=300,
            env=env,
        )
        log.info("Cloned %s -> %s", repo_url, dest)
        return True
    except subprocess.CalledProcessError as e:
        # Sanitize stderr: never log anything after '@' in URLs
        stderr = _sanitize_git_output(e.stderr.decode()[:500])
        log.error("git clone failed for %s: %s", repo_url, stderr)
        return False
    except subprocess.TimeoutExpired:
        log.error("git clone timed out for %s", repo_url)
        return False


def _sanitize_git_output(text: str) -> str:
    """Remove any credentials from git output.
    
    Git with GIT_ASKPASS shouldn't leak credentials in stderr,
    but defense-in-depth: redact anything that looks like a token.
    """
    import re
    # Redact x-access-token:xxx@ patterns (shouldn't appear, but just in case)
    text = re.sub(r"x-access-token:[^@]+@", "x-access-token:***@", text)
    # Redact ghp_/gho_/ghu_ tokens
    text = re.sub(r"(ghp_|gho_|ghu_|github_pat_)[A-Za-z0-9_]+", r"\1***", text)
    return text
```

### 6.3 `git-credential-helper.sh` — New File

```bash
#!/bin/sh
# Git credential helper for GitHub App authentication.
# Invoked via GIT_ASKPASS — git calls this to get the password.
# Token is written by github-app-token.py with 0600 permissions.
#
# Security: This script only prints the token content to stdout.
# It does NOT log, does NOT echo to stderr, does NOT embed in URLs.
TOKEN_FILE="/tmp/github-token"
if [ -f "$TOKEN_FILE" ]; then
    cat "$TOKEN_FILE"
else
    exit 1
fi
```

### 6.4 `manifests/ingestion-scaledjob.yaml` — Scale Changes

```yaml
# Key changes:
spec:
  jobTargetRef:
    template:
      spec:
        containers:
          - name: worker
            env:
              # ... existing env vars ...
              # NEW: GitHub App secret references for token minting
              - name: GITHUB_APP_ID_SECRET
                value: "${GITHUB_APP_ID_SECRET}"
              - name: GITHUB_APP_KEY_SECRET
                value: "${GITHUB_APP_KEY_SECRET}"
  pollingInterval: 10
  maxReplicaCount: 50          # CHANGED from 10
  successfulJobsHistoryLimit: 3 # CHANGED from 5
  failedJobsHistoryLimit: 10    # CHANGED from 5
```

### 6.5 `Dockerfile` — Add Credential Helper

```dockerfile
# Add after the COPY of Python scripts:
COPY git-credential-helper.sh /app/git-credential-helper.sh
RUN chmod +x /app/git-credential-helper.sh
```

---

## 7. KEDA Scale Ceiling Deep-Dive

### 7.1 Why 50 (not 100)

The design of record says "50-100." We recommend starting at **50** because:

1. **EKS Auto Mode node provisioning**: Spinning up 100 nodes simultaneously can
   hit EC2 RunInstances limits on new accounts. 50 is well within default quotas.
2. **GitHub App rate limits**: 5,000 API requests/hour/installation. At 50
   concurrent clones (1 API call each), we're fine. At 100 + ls-remote checks +
   any retries, we approach the limit during burst.
3. **SQS visibility timeout math**: If a repo takes the full 15 min timeout and
   we have 500 repos, 50 workers finish in ~150 min (10 waves). Acceptable.
4. **Observability**: Easier to debug 50 concurrent workers than 100 during
   initial rollout. Raise to 100 after one stable batch.

### 7.2 Backpressure Signals

Workers should NOT blindly retry on downstream throttling. The existing
architecture handles this correctly:

- **SQS retry**: If a worker fails (exit 1), the message returns to the queue
  after visibility timeout. After 3 failures → DLQ. No retry storm.
- **ingest-repo.py**: Steps are independent — if DeepWiki fails, other steps
  still complete. Only OpenViking failure is fatal (exit 1).
- **Future S3 Vectors writes**: Should implement exponential backoff within the
  embedding producer (separate sub-issue). At 50 workers writing 50 vectors/s
  each = 2,500/s → exactly at the limit → the producer should batch to 500/put
  and back off on 429s.

### 7.3 Resource Sizing at 50 Workers

Current per-worker: 500m CPU request / 2 CPU limit, 1Gi RAM request / 4Gi limit.
At 50 workers: 25 CPU request, 50Gi RAM request. EKS Auto Mode handles this
(provisions m5.4xlarge or equivalent nodes on demand). No changes needed.

---

## 8. Migration Path

### Phase 1 (This Issue)
1. Add `git-credential-helper.sh` to the image
2. Modify `sqs-worker.py` to mint token at startup
3. Modify `ingest-repo.py` to use credential helper, sanitize output
4. Raise `maxReplicaCount` to 50
5. Add `GITHUB_APP_ID_SECRET` / `GITHUB_APP_KEY_SECRET` env vars to manifest

### Phase 2 (Follow-up: Private Repo Publisher)
- `publish-ingestion.py`'s `git ls-remote` for change detection also needs auth
  for private repos. This can use the same `GIT_ASKPASS` pattern in the CronJob
  that runs the publisher. Not in scope here.

### Rollback Plan
- Revert `maxReplicaCount` to 10
- Remove the `GITHUB_APP_ID_SECRET`/`GITHUB_APP_KEY_SECRET` env vars
- Workers fall back to anonymous clone (existing behavior)
- No data loss — DynamoDB state, SQS messages are unaffected

---

## 9. Acceptance Criteria (Verification Plan)

| # | Criterion | How to verify |
|---|-----------|---------------|
| 1 | Token never appears in worker logs | `kubectl logs -n agent-context -l app=ingestion-worker \| grep -i "gho_\|ghp_\|x-access-token"` returns empty |
| 2 | Private repo clones succeed | Enqueue a known private repo; confirm DynamoDB STATE shows `clone: ok` |
| 3 | Public repo clones still work without token | Unset `GITHUB_APP_ID_SECRET`; enqueue a public repo; confirm clone succeeds |
| 4 | 50 concurrent workers run without throttling | Enqueue 50 repos; confirm 50 pods appear (`kubectl get pods -l app=ingestion-worker --no-headers \| wc -l`) |
| 5 | Workers use per-pod scratch (no cross-contamination) | In two concurrent workers, confirm `$CLONE_BASE` paths are distinct emptyDirs |
| 6 | Token file has 0600 permissions | `kubectl exec` into a running worker; `stat /tmp/github-token` shows `-rw-------` |
| 7 | Graceful degradation on token failure | Simulate by pointing to nonexistent secret; confirm WARNING logged and public clone attempted |

---

## 10. Open Questions / Decisions Deferred

1. **Publisher auth for private repos** — `git ls-remote` in `publish-ingestion.py`
   needs auth for private repos. Deferred to a follow-up because it runs in the
   CronJob context, not the ScaledJob worker.

2. **Token refresh for long-running workers** — GitHub installation tokens expire
   in 1 hour. Current repos take 15 min max per worker. No risk today. If future
   producers (DeepWiki can take 15 min alone) chain to exceed 1 hour, the token
   must be refreshed. Recommend: mint fresh token per repo processing, not per
   pod lifetime. Cost: one extra Secrets Manager call per repo (~$0.00004/call).

3. **Scaling to 100** — After one successful batch at 50, raise to 100. Gate on:
   no EC2 limit hits, no GitHub rate-limit 429s, no OOM kills.

4. **OSV-Scanner input model correction** — The design of record (§7.2) implies
   Syft SBOM → OSV-Scanner. Verified: OSV-Scanner scans lockfiles directly, not
   SBOM files. The SBOM (Syft) is for the reverse-lookup table in Postgres; vuln
   scanning (OSV-Scanner) reads the clone tree. Update the design doc when the
   SBOM sub-issue is implemented.
