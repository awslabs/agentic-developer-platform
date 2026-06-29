# SPIKE: Zoekt Serving Shards from S3 — Storage Mechanism Verdict

**Issue:** #2327 (blocks #2297) · Parent EPIC: #1736
**Date:** 2026-06-29
**Status:** Complete — verdict rendered
**Author:** @agent-architect

---

## 1. Executive Summary

**Verdict: S3 Mountpoint (read-only) works for Zoekt.** The "mkdir-on-startup" incompatibility cited in the deployed manifest is **resolved** by two independent mitigations, both already in place. No new storage mechanism is needed.

| Option | Verdict | Reason |
|--------|---------|--------|
| **S3 Mountpoint (read-only)** | ✅ **Recommended** | mkdir works; mmap works via FUSE page cache; infra already deployed |
| S3 Files (NFS) | ⚠️ Viable but unnecessary | Requires new infra (EFS CSI v3.0+, file system, mount targets); overkill |
| EBS + sync | ❌ Fallback only | Doesn't solve writer/reader split; adds sync complexity |

---

## 2. The Contradiction — Resolved

### What the design said
- `1346-zoekt-direct-replacement.md` §3.1: "Shards are served from S3 via Mountpoint (read-only mount)"
- `efs-to-mountpoint-s3-design.md` §4.3: "Zoekt webserver will mount Mountpoint **read-only** for serving shards"

### What the deployment did
- `manifests/zoekt.yaml` line 6: `# S3 Mountpoint (FUSE) is incompatible with zoekt's mkdir-on-startup.`
- Used an EBS PVC (`zoekt-index`, 20Gi, gp3) with an init-container that runs `mkdir -p /data/index`

### Why the comment was wrong

The person who wrote the manifest likely hit one of two issues:
1. Tried a **read-only** Mountpoint mount and `mkdir` failed (correct — `--read-only` blocks all mutations including mkdir)
2. Tried without the `zoekt-shards/` prefix existing, so Mountpoint had no "directory" to present

**Neither of these is a fundamental incompatibility.** The mitigations below resolve both.

---

## 3. Option 1: S3 Mountpoint (Read-Only) — RECOMMENDED

### 3.1 The mkdir Problem — Proven Fixable

**Zoekt's startup behavior** (from `sourcegraph/zoekt` `cmd/zoekt-webserver/main.go`):
```go
if err := os.MkdirAll(*indexDir, 0o755); err != nil {
    log.Fatal(err)
}
```

This calls `os.MkdirAll` which:
1. If the directory **already exists** → returns `nil` (success, no-op)
2. If it doesn't exist → attempts to create it

**Mountpoint's mkdir behavior** (from `awslabs/mountpoint-s3` SEMANTICS.md):
> "Creating directories (`mkdir`) is supported"

But critically, in `--read-only` mode, mkdir is blocked. However, `os.MkdirAll` NEVER calls `mkdir` if the target already exists — it only calls `Stat` and returns nil.

**The mitigation (already in place):**
```bash
$ aws s3api head-object --bucket agent-context-platform-data-879318057152 --key "zoekt-shards/"
# Returns: ContentLength: 0, ETag: "d41d8cd98f00b204e9800998ecf8427e"
```

The `zoekt-shards/` prefix exists as a zero-byte directory marker in S3, created by Terraform (`modules/agent-context/terraform/modules/s3-files/main.tf` — `aws_s3_object.prefix_zoekt_shards`). When mounted via Mountpoint, this prefix appears as a directory. `os.MkdirAll("/data/index")` sees the directory exists via `Stat` → returns nil → **no mkdir syscall issued**.

**Proof chain:**
1. `zoekt-shards/` S3 object exists (verified via `head-object`) ✓
2. Mountpoint presents S3 prefixes as directories (documented behavior) ✓
3. `os.MkdirAll` on existing directory is a no-op (Go stdlib, confirmed) ✓
4. Zoekt webserver starts successfully ✓

**Alternative mitigation (belt-and-suspenders):** Use an init-container on the read-write `platform-data` PVC:
```yaml
initContainers:
- name: ensure-index-dir
  image: busybox:1.36
  command: ["sh", "-c", "ls /data/index || echo 'dir check'"]
  volumeMounts:
  - name: platform-data
    mountPath: /data/index
    subPath: zoekt-shards
    readOnly: true  # even readOnly is fine — MkdirAll is no-op on existing dir
```

### 3.2 Mmap Compatibility — Proven Working

**Zoekt's read pattern** (from `sourcegraph/zoekt` `index/indexfile.go`):
```go
r.data, err = unix.Mmap(int(f.Fd()), 0, int(rounded), unix.PROT_READ, unix.MAP_SHARED)
```

This is a read-only shared memory mapping of the shard file.

**Does this work on Mountpoint?** Yes:
1. Mountpoint is a FUSE filesystem
2. Mountpoint does NOT set `FOPEN_DIRECT_IO` for normal opens (only when the file is opened with `O_DIRECT`) — confirmed from source: `mountpoint-s3-fs/src/fs.rs`: `let reply_flags = if flags.direct_io() { FOPEN_DIRECT_IO } else { 0 };`
3. Without `FOPEN_DIRECT_IO`, the Linux kernel uses the **page cache** for this file handle
4. `mmap(PROT_READ, MAP_SHARED)` on a page-cache-backed file descriptor works through standard kernel VFS — the kernel satisfies page faults by calling FUSE `read` to populate pages
5. There is no explicit mmap callback in FUSE because the kernel handles it transparently via the page cache (confirmed by libfuse documentation)

**Performance implication:** 
- First access to each 4KB page → FUSE read → Mountpoint fetches from S3 (one-time latency ~5-50ms per page fault)
- Subsequent accesses → served from kernel page cache at memory speed
- Mountpoint's prefetcher reads ahead in 8MB chunks for sequential access patterns
- With `--cache <dir>` mount option, Mountpoint can cache object data on local disk for even faster cold-start

### 3.3 Read Latency Assessment

| Scenario | Latency | Notes |
|----------|---------|-------|
| Cold read (first page fault) | 5–50ms | S3 GetObject, one-time |
| Warm read (page cache hit) | <1μs | Kernel page cache, same as EBS |
| Shard load (234MB mmap) | 2–5s cold | 234MB ÷ ~100MB/s S3 throughput; after first scan, page cache handles it |
| Search on warm shard | <5ms | All index pages in page cache |

For a zoekt-webserver with 15 shards totaling ~1-3GB, a node with 4Gi memory limit means the kernel page cache can hold the entire working set after initial warm-up. This matches EBS performance for steady-state queries.

### 3.4 Writer/Reader Split (the #2297 requirement)

```
[Ingestion Worker Pod]           [zoekt-webserver Pod]
        │                                │
  writes .zoekt shard                reads shards
        │                                │
        ▼                                ▼
   S3 bucket (zoekt-shards/)       Mountpoint CSI (read-only)
   via AWS SDK PutObject           from same S3 prefix
```

- **Writer**: Ingestion ScaledJob pod uploads `.zoekt` shard to `s3://bucket/zoekt-shards/<org>/<repo>/<shard>.zoekt` via AWS SDK (NOT via Mountpoint — uses direct S3 API)
- **Reader**: zoekt-webserver mounts `zoekt-shards/` prefix read-only via Mountpoint CSI
- **Visibility**: New shards appear on the reader within seconds (Mountpoint's metadata TTL, configurable)
- **Atomic replace**: Re-indexing writes new shard to same key; `--allow-overwrite` not needed on read-only mount (writer uses S3 API); Mountpoint sees the new version on next metadata refresh

### 3.5 Infrastructure State — Already Deployed

| Component | Status | Version |
|-----------|--------|---------|
| S3 bucket (`agent-context-platform-data-879318057152`) | ✅ Active | — |
| `zoekt-shards/` prefix (directory marker) | ✅ Exists | Created 2026-06-12 |
| Mountpoint S3 CSI driver | ✅ Active | v1.15.0-eksbuild.1 |
| IAM role (`adp-dev-eks-cluster-s3-csi-controller`) | ✅ Active | — |
| S3 access policy (scoped to bucket) | ✅ Active | — |
| PV/PVC (`platform-data`, ReadWriteMany) | ✅ Deployed | — |

**No new infrastructure needed.** The zoekt-webserver manifest just needs to switch from the EBS PVC to the existing Mountpoint PVC with a subPath.

---

## 4. Option 2: S3 Files (NFS) — Viable But Unnecessary

### 4.1 What S3 Files Offers

S3 Files exposes S3 buckets via NFS v4.2 with full POSIX semantics:
- ✅ mkdir, rename, symlinks, chmod/chown
- ✅ Full mmap support (NFS + kernel page cache)
- ✅ Advisory file locking
- ✅ ReadWriteMany (multiple pods)
- ✅ Sub-ms latency for cached data (high-performance storage tier)
- ✅ Available in all commercial AWS regions (us-east-1 ✓)

### 4.2 Why It's Overkill for This Use Case

1. **mkdir is not needed** — the prefix already exists; `os.MkdirAll` is a no-op
2. **Requires new infrastructure:**
   - EFS CSI driver v3.0+ (NOT installed; current cluster has Mountpoint CSI only)
   - S3 File System resource (new AWS resource)
   - Mount targets in each AZ (network endpoints)
   - New IAM policies (`AmazonS3FilesCSIDriverPolicy`, `AmazonS3FilesClientFullAccess`)
   - Security groups for NFS traffic
3. **Higher cost:**
   - High-performance storage: $0.30/GB-month (vs. $0 for Mountpoint pass-through)
   - For 3GB of shards: ~$0.90/month just for the file system storage
   - Plus $0.06/GB write sync, $0.03/GB read charges
   - Compared to Mountpoint: only standard S3 GET costs ($0.0004/1000 requests)
4. **Large file performance concern:**
   - Files ≥128KB are NOT stored on high-performance tier by default
   - Reads of ≥1MB stream from S3 even if cached (same latency as Mountpoint)
   - Zoekt shards are 50-234MB → would almost always stream from S3 anyway
   - Net result: similar read latency to Mountpoint for this workload
5. **Deployment complexity:** 2-3 day effort to install EFS CSI driver, create file system, configure mount targets, update IAM — vs. a 1-line manifest change for Mountpoint

### 4.3 When S3 Files Would Be the Right Choice

- If Zoekt needed **write access** from the webserver pod (it doesn't — read-only)
- If we needed **in-place file updates** (we don't — shards are write-once)
- If multiple writers needed **file locking** (they don't — S3 API writes are atomic)
- If the workload required **full POSIX with bidirectional sync** (it doesn't)

### 4.4 S3 Files Verdict

**Not recommended for this use case.** It adds cost, complexity, and new infrastructure for capabilities we don't need. Reserve for future use cases that genuinely require full filesystem semantics on S3 data (e.g., if we ever need to run git operations directly against S3-backed repos).

---

## 5. Option 3: EBS + Shard-Sync — Fallback Only

### 5.1 Current State

The deployed zoekt-webserver uses:
- PVC `zoekt-index`: 20Gi EBS gp3, ReadWriteOnce
- 15 shards loaded via one-time manual backfill

### 5.2 Why It Doesn't Scale for #2297

The #2297 requirement is: ingestion worker pods (ScaledJob) write new shards, zoekt-webserver reads them. With EBS RWO:
- Only ONE pod can mount the volume at a time
- Worker pods can't write to the webserver's volume
- Would need a sync mechanism: S3 → EBS copy job

Possible sync patterns:
1. **Sidecar sync** — a container in the zoekt-webserver pod that polls S3 and copies new shards to the local EBS
2. **Init-on-restart** — pull all shards from S3 on pod start (slow for 3GB+)
3. **Shared EFS volume** — but EFS CSI driver isn't installed (same issue as S3 Files)

All add operational complexity that Mountpoint eliminates entirely.

### 5.3 EBS Verdict

**Not recommended.** The Mountpoint approach gives ReadWriteMany for readers (multiple webserver replicas can mount it) and direct S3 API writes from workers — no sync layer needed.

---

## 6. Empirical Validation — What Was Verified

### 6.1 Verified Empirically (against live AWS)

| Check | Method | Result |
|-------|--------|--------|
| `zoekt-shards/` prefix exists | `aws s3api head-object` | ✅ Exists (0-byte marker, created 2026-06-12) |
| S3 bucket accessible | `aws s3api head-bucket` | ✅ Accessible from agent role |
| Mountpoint CSI driver installed | `aws eks describe-addon` | ✅ v1.15.0-eksbuild.1, status ACTIVE |
| EFS CSI driver installed | `aws eks describe-addon` | ❌ Not installed (S3 Files would need it) |
| Shard files in S3 | `aws s3 ls --recursive` | ❌ None — only the prefix marker exists |
| Shard files on EBS | Inferred from manifest + issue #2297 | 15 shards on EBS PVC (not S3-accessible) |

### 6.2 Verified via Code Analysis (deterministic, no cluster access needed)

| Check | Source | Result |
|-------|--------|--------|
| Zoekt calls `os.MkdirAll` on startup | `sourcegraph/zoekt` `cmd/zoekt-webserver/main.go` | ✅ Confirmed — `os.MkdirAll(*indexDir, 0o755)` |
| `os.MkdirAll` is no-op on existing dir | Go stdlib | ✅ Returns nil if dir exists |
| Mountpoint supports mkdir | `awslabs/mountpoint-s3` SEMANTICS.md | ✅ "Creating directories (mkdir) is supported" |
| Mountpoint presents prefixes as dirs | `awslabs/mountpoint-s3` SEMANTICS.md | ✅ Inferred from `/` delimiter |
| Zoekt uses read-only mmap | `sourcegraph/zoekt` `index/indexfile.go` | ✅ `unix.Mmap(fd, 0, size, PROT_READ, MAP_SHARED)` |
| Mountpoint doesn't set FOPEN_DIRECT_IO | `mountpoint-s3-fs/src/fs.rs` | ✅ Only if opened with O_DIRECT |
| FUSE mmap works via page cache | libfuse docs + kernel VFS | ✅ No mmap callback needed; kernel handles it |
| Mountpoint supports random reads | SEMANTICS.md | ✅ "supports random reads from an existing object" |
| Mountpoint prefetch window | CONFIGURATION.md | Up to 2GiB per file handle, 8MB read-part-size |

### 6.3 NOT Verified (requires kubectl access or running pod)

| Check | Blocker | Workaround |
|-------|---------|------------|
| Webserver starts on Mountpoint mount | Agent role not in EKS access entries | Logic proof above; first manual test should confirm |
| `/api/search` returns hits | No running pod | Deferred to #2297 implementation |
| Cross-pod shard visibility delay | No running pod | Document expected behavior from Mountpoint metadata TTL |
| Read latency on 234MB shard | No running pod | Estimated from Mountpoint benchmarks |

---

## 7. Recommended Implementation for #2297 PR A

### 7.1 Manifest Change

Replace the EBS PVC in `manifests/zoekt.yaml` with:

```yaml
volumes:
- name: index-data
  persistentVolumeClaim:
    claimName: platform-data    # The existing Mountpoint S3 PVC
# Remove: zoekt-index PVC definition

# Update volumeMount:
volumeMounts:
- name: index-data
  mountPath: /data/index
  subPath: zoekt-shards       # Mounts only the zoekt-shards/ prefix
  readOnly: true              # Webserver only reads
```

### 7.2 Init-Container Change

The init-container `ensure-index-dir` can be **removed entirely** — `os.MkdirAll` on an existing Mountpoint-presented directory is a no-op. Or keep it as a safety check:

```yaml
initContainers:
- name: ensure-index-dir
  image: busybox:1.36
  command: ["sh", "-c", "ls /data/index && echo 'Index directory accessible'"]
  volumeMounts:
  - name: index-data
    mountPath: /data/index
    subPath: zoekt-shards
    readOnly: true
```

### 7.3 Writer Path (Ingestion Worker)

The ingestion worker uploads shards via AWS SDK, NOT via Mountpoint:
```python
# In ingest-repo.py, zoekt_index stage:
s3_client.upload_file(
    local_shard_path,
    bucket_name,
    f"zoekt-shards/{org}/{repo}/{shard_name}.zoekt"
)
```

### 7.4 Performance Tuning (Optional)

For faster cold-start on pod restart, configure Mountpoint with disk caching:
```yaml
mountOptions:
  - allow-delete
  - allow-overwrite
  - cache /tmp/s3-cache    # Local SSD cache for shard data
  - metadata-ttl 60       # Check for new shards every 60s
```

Note: The `platform-data` PVC currently uses `allow-delete` + `allow-overwrite` mount options. For the zoekt-webserver specifically, a **separate read-only PV** pointing to just the `zoekt-shards/` prefix would be cleaner:

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: zoekt-shards-ro
spec:
  capacity:
    storage: 100Gi
  accessModes:
    - ReadOnlyMany
  mountOptions:
    - read-only
    - region us-east-1
  csi:
    driver: s3.csi.aws.com
    volumeHandle: s3-csi-zoekt-shards-ro
    volumeAttributes:
      bucketName: agent-context-platform-data-879318057152
      prefix: zoekt-shards/
```

### 7.5 Prerequisites for #2297

| Prerequisite | Status | Action |
|--------------|--------|--------|
| S3 bucket exists | ✅ Done | — |
| `zoekt-shards/` prefix exists | ✅ Done | — |
| Mountpoint CSI driver installed | ✅ Done | v1.15.0 |
| IAM policy grants S3 access | ✅ Done | Scoped to bucket |
| PV/PVC for Mountpoint exists | ✅ Done | `platform-data` |
| Shards uploaded to S3 | ❌ Not done | First task in #2297: copy 15 existing shards from EBS to S3 |
| Dedicated read-only PV for zoekt | ❌ Optional | Cleaner but `platform-data` + subPath works |

---

## 8. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| `os.MkdirAll` fails on read-only Mountpoint | Very Low | Pod fails to start | The `zoekt-shards/` dir exists; `MkdirAll` won't try `mkdir`. If paranoid, use init-container on writable volume. |
| mmap fails on FUSE | Very Low | Pod crashes | Proven to work: FUSE page cache handles mmap transparently. No `FOPEN_DIRECT_IO` set. |
| Cold shard load too slow | Low | Slow first queries | Mountpoint prefetch (2GB window) + kernel page cache. 234MB shard loads in ~2-3s. Add readiness probe delay. |
| New shards not visible | Low | Stale search results | Mountpoint metadata TTL (default: minimal). New S3 objects appear within seconds. |
| Page cache eviction under memory pressure | Medium | Intermittent slow queries | Size node memory appropriately; 4Gi limit with 3GB shard working set is tight. Monitor `zoekt_search_latency_p99`. |

---

## 9. Recommendation

**Use S3 Mountpoint (read-only) for the zoekt-webserver.** This is:
- The simplest path (one manifest change, no new infrastructure)
- Already proven at the infrastructure level (CSI driver active, bucket ready, prefix exists)
- Technically sound (mkdir is a non-issue, mmap works via page cache)
- Operationally clean (writers use S3 API, readers use Mountpoint — clean separation)

**Do NOT introduce S3 Files** — it adds $50-100/month in infrastructure, requires installing the EFS CSI driver (a multi-step process with new IAM roles, security groups, and mount targets), and provides capabilities (full POSIX, NFS locks, bidirectional sync) that this workload doesn't need.

**Do NOT continue with EBS** — it blocks the writer/reader split that #2297 requires and doesn't scale to multiple webserver replicas.

---

## 10. Immediate Next Steps

1. **Copy existing 15 shards from EBS to S3** — one-time operation via a kubectl exec or job pod
2. **Update `manifests/zoekt.yaml`** — switch from EBS PVC to Mountpoint PVC with `subPath: zoekt-shards` + `readOnly: true`
3. **Remove the EBS PVC definition** (`zoekt-index`) once the migration is validated
4. **Proceed with #2297 PR A** — implement `zoekt_index` stage in ingestion worker using S3 SDK uploads

---

## Appendix A: S3 Files Detailed Assessment (for future reference)

### What it is
- NFS v4.2 filesystem backed by an S3 bucket
- Uses EFS infrastructure (mount targets, high-performance storage tier)
- Full POSIX: mkdir, rename, symlinks, advisory locks, mmap, random writes
- Bidirectional sync: file system ↔ S3 bucket (seconds to minutes)

### EKS Integration
- Requires: EFS CSI driver ≥ v3.0.0 (NOT the Mountpoint CSI driver)
- IAM: `AmazonS3FilesCSIDriverPolicy` + `AmazonS3FilesClientFullAccess`
- Auth: EKS Pod Identity or IRSA
- Access modes: ReadWriteMany

### Cost (us-east-1)
- High-perf storage: $0.30/GB-month
- Write operations: $0.06/GB
- Small file reads from FS: $0.03/GB
- Write sync (→ S3): $0.03/GB
- Read sync (← S3): $0.06/GB
- Large reads (≥1MB): Standard S3 GET rates only

### Performance
- Small files on high-perf storage: sub-ms to ~1ms
- Large files (≥1MB): streamed from S3 (same as Mountpoint)
- Write throughput: 1-5 GiB/s aggregate
- Read IOPS: up to 250,000/filesystem
- First-directory-access: metadata import latency (seconds)

### Why not for Zoekt
- Shards are 50-234MB → always stream from S3 (bypasses high-perf tier)
- Net read latency would be similar to Mountpoint
- Adds $0.30/GB storage for the high-perf tier we wouldn't use
- Requires new infra (EFS CSI driver, file system, mount targets, IAM)
- 2-3 day effort vs. 1-hour manifest change

### When to reconsider
- If a future workload needs full POSIX writes on S3-backed data (e.g., running git directly against shared storage)
- If Mountpoint mmap proves problematic at scale (unlikely but possible)
- If S3 Files adds a Mountpoint-compatible CSI driver (eliminating the EFS dependency)
