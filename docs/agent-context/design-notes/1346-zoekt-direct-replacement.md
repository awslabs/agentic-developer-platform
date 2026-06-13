# Design Note: Replace Sourcebot with Direct Zoekt

**Issue:** #1346 (sub of EPIC #1345)
**Date:** 2026-06-12
**Status:** Implementation-ready
**Author:** @agent-architect

---

## 1. Summary

Replace the Sourcebot container (`ghcr.io/sourcebot-dev/sourcebot`, FSL-1.1-ALv2) with a
direct deployment of **Zoekt** (`sourcegraph/zoekt`, Apache-2.0) for exact/regex code search.
Sourcebot is a web layer around Zoekt; we drop the web layer and run Zoekt's own webserver
directly, querying its JSON API from the Door's search backend.

This eliminates:
- The FSL-1.1 license dependency (distribution blocker for open-source release)
- Sourcebot's bundled PostgreSQL (unnecessary — our catalog lives in the shared RDS instance)
- Sourcebot's bundled Redis 8 (unnecessary cache layer; see sibling #1347)
- The token-refresh CronJob (Zoekt webserver is read-only; tokens are only needed at index time)
- ~130 Gi of PVC claims (100Gi index + 20Gi Postgres + 10Gi Redis)

---

## 2. Fact Verification

| Claim | Status | Evidence |
|-------|--------|----------|
| Zoekt (sourcegraph/zoekt) is Apache-2.0 | **Confirmed** | `knowledge-layer-storage-design.md` line 52; GitHub repo LICENSE file |
| `zoekt-git-index` writes self-contained `.zoekt` shard files | **Confirmed** | `efs-to-mountpoint-s3-design.md` line 113: "finished `.zoekt` files are uploaded as new objects" |
| `zoekt-webserver` exposes JSON `/api/search` endpoint | **Confirmed** | Port 6070 in existing `sourcebot.yaml`; Zoekt source `web/server.go` serves `/api/search` returning JSON with `FileMatches` |
| Shards are immutable once built (write-once, Mountpoint-compatible) | **Confirmed** | Design doc: "Shard building happens on worker scratch, then the finished `.zoekt` files are uploaded as new objects" |
| S3 prefix `zoekt-shards/` already provisioned | **Confirmed** | `terraform/modules/s3-files/main.tf` line 189 |

---

## 3. Architecture

### 3.1 Serving Path (this PR)

```
                                    S3 (zoekt-shards/)
                                         │
                                    Mountpoint CSI
                                    (read-only mount)
                                         │
┌──────────┐   JSON/HTTP    ┌────────────▼───────────┐
│   Door   │ ──────────────▶│   zoekt-webserver      │
│ (MCP)    │   :6070        │   /api/search          │
└──────────┘                │   reads .zoekt shards  │
      │                     └────────────────────────┘
      │ post-query
      ▼ ACL filter
  [filtered results]
```

- **zoekt-webserver** is a stateless read-only process
- Shards are served from S3 via Mountpoint (read-only mount, no `--allow-overwrite`)
- No database, no Redis, no token management at serve time
- Door applies ACL filter post-query (per `door/acl.py`)

### 3.2 Indexing Path (deferred to follow-up after #1387 merges)

```
┌──────────────────┐     ┌──────────────────────┐     ┌─────────────┐
│ ingestion worker │────▶│ zoekt-git-index       │────▶│ S3 upload   │
│ (sqs-worker.py)  │     │ (on scratch disk)     │     │ zoekt-shards/│
└──────────────────┘     └──────────────────────┘     └─────────────┘
```

The indexing hook will:
1. Run `zoekt-git-index` against the already-cloned repo on scratch disk
2. Upload the resulting `.zoekt` shard file(s) to `s3://<bucket>/zoekt-shards/<org>/<repo>/`
3. Update `repositories.zoekt_status` in Postgres

**Why deferred:** `sqs-worker.py` and `ingest-repo.py` are being rewritten by #1387
(OpenViking->S3 rewire). Editing them here would create a merge collision.

---

## 4. Zoekt API Contract

### Request: `GET /api/search?q=<query>&num=<limit>&repos=<filter>`

Query parameters:
- `q` (string, required): Search query. Supports literal text and regex (`regex:pattern`).
- `num` (int, optional): Max results. Default: 50.
- `repos` (string, optional): Regex filter for repository names. E.g., `^org/repo-a$`.

### Response (JSON):

```json
{
  "Result": {
    "FileMatches": [
      {
        "FileName": "src/handler.py",
        "Repository": "org/my-repo",
        "LineMatches": [
          {
            "LineNumber": 42,
            "Line": "def process_request(data):",
            "LineFragments": [...]
          }
        ]
      }
    ],
    "RepoURLs": {"org/my-repo": "https://github.com/org/my-repo"},
    "Stats": {"MatchCount": 3, "FileCount": 2, "Duration": 15000000}
  }
}
```

### Mapping to MCP Contract

| Zoekt field | MCP result field | Mapping |
|-------------|------------------|---------|
| `FileMatch.Repository` | `repo_id` | Direct (already `org/repo` format) |
| `FileMatch.FileName` | `file` | Direct |
| `LineMatch.LineNumber` | `line` | Direct (1-indexed) |
| `LineMatch.Line` | `content` | Direct (full line text) |

---

## 5. K8s Deployment Design

### Deployment: `zoekt-webserver`

- **Image:** `ghcr.io/sourcegraph/zoekt:latest` (multi-arch, includes both zoekt-webserver and zoekt-git-index)
- **Command:** `zoekt-webserver -index /data/index -listen :6070`
- **Mount:** Mountpoint S3 PVC (`platform-data`) at `/data/index` subPath `zoekt-shards/`, **read-only**
- **Resources:** 500m CPU / 2Gi RAM request; 1 CPU / 4Gi RAM limit (lighter than Sourcebot's 1CPU/4Gi + Postgres + Redis)
- **Health:** HTTP GET `/` returns 200 when ready; `/api/search?q=healthcheck` for liveness
- **Replicas:** 1 (horizontally scalable if needed — stateless readers)

### Service: `zoekt`

- **Port:** 6070 (maintains continuity with existing Sourcebot zoekt port)
- **Type:** ClusterIP (internal only; Door is the public surface)

---

## 6. Search Backend Design

`door/search_backend.py` provides:

```python
class ZoektSearchBackend:
    """Queries the zoekt-webserver /api/search endpoint."""

    async def search(self, query: str, *, repo_ids: list[str] | None = None,
                     limit: int = 50) -> list[SearchHit]:
        ...
```

- Returns `list[SearchHit]` (from `door/acl.py`) so the Door ACL filter works unchanged
- Timeout: configurable via `settings.zoekt_timeout` (default 10s)
- Error handling: returns empty list on timeout/5xx (fail-safe for search; callers log)
- Repo scoping: converts `repo_ids` list to a Zoekt `repos` regex filter

---

## 7. Migration Path

### Phase 1 (this PR): Deploy Zoekt webserver + search backend
- New K8s manifest for zoekt-webserver (read-only mount)
- New Door search backend querying Zoekt API
- Config keys added
- Unit tests passing

### Phase 2 (follow-up, after #1387): Indexing hook
- Add `zoekt-git-index` step to ingestion pipeline
- Upload shards to S3
- Update `repositories.zoekt_status`
- Integration test: ingest -> search roundtrip

### Phase 3 (after Phase 2 validated): Remove Sourcebot
- Delete `manifests/sourcebot.yaml`, `sourcebot-config.yaml`, `sourcebot-token-cronjob.yaml`
- Delete `kubernetes/sourcebot-deployment.yaml`, `kubernetes/sourcebot-configmap.yaml`
- Remove Sourcebot PVC claims (100Gi + 20Gi + 10Gi)
- Remove `sourcebot-postgres-password`, `sourcebot-auth-secret`, `sourcebot-encryption-key` from secrets

---

## 8. Cost Impact

| Component | Before (Sourcebot) | After (Zoekt direct) |
|-----------|--------------------|--------------------|
| Pods | 3 (Sourcebot + Postgres + Redis) | 1 (zoekt-webserver) |
| PVC storage | 130 Gi (100 + 20 + 10) | 0 (reads from S3 Mountpoint) |
| CPU request | 1.75 cores (1 + 0.5 + 0.25) | 0.5 cores |
| Memory request | 5.5 Gi (4 + 1 + 0.5) | 2 Gi |
| CronJobs | 1 (token refresh every 50m) | 0 |

Net saving: ~70% reduction in dedicated compute for code search serving.

---

## 9. Risks and Mitigations

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Zoekt webserver image changes API shape | Low (stable since 2020) | Pin image tag; contract test in CI |
| Empty shards directory on first deploy | Certain | Health check tolerates empty index; search returns empty (not error) |
| S3 Mountpoint read latency for cold shards | Low (Mountpoint caches locally) | Monitor P99 latency; enable instance-storage cache if needed |
| Index format incompatibility across versions | Low | Pin `zoekt-git-index` and `zoekt-webserver` to same image tag |
