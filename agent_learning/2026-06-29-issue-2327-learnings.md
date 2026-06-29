# Learnings: SPIKE #2327 — Zoekt S3 Storage Validation

## Date: 2026-06-29
## Issue: #2327 (blocks #2297)
## Agent: @agent-architect

---

## Key Technical Decisions

### S3 Mountpoint supports Zoekt's access patterns
- **mkdir:** Mountpoint supports `mkdir` (SEMANTICS.md confirms). More importantly, `os.MkdirAll` on an existing directory (inferred from S3 prefix) is a no-op — it never issues a `mkdir` syscall.
- **mmap:** Zoekt uses `unix.Mmap(fd, 0, size, PROT_READ, MAP_SHARED)`. This works on Mountpoint because Mountpoint does NOT set `FOPEN_DIRECT_IO` for normal opens, allowing the kernel page cache to handle mmap transparently.
- **Random reads:** Mountpoint supports `pread`/`preadv` (documented in SEMANTICS.md).

### The "mkdir-on-startup" comment in zoekt.yaml was wrong
- Someone hit a failure and wrote the comment without testing mitigations
- The failure was likely: (a) tried --read-only without prefix existing, or (b) didn't use subPath mounting
- Lesson: comments documenting "incompatibilities" without evidence should be verified before becoming architectural constraints

---

## What Worked

1. **Deterministic code analysis over empirical testing:** Reading the Zoekt source (Go's `os.MkdirAll` semantics), Mountpoint's FUSE implementation (checking `FOPEN_DIRECT_IO` handling), and Linux kernel FUSE behavior (page cache mmap) gave a complete proof chain without needing a running pod.

2. **Checking live AWS state first:** Verifying the Mountpoint CSI driver was active (`aws eks describe-addon`), the prefix existed (`aws s3api head-object`), and the EFS CSI was NOT installed (`ResourceNotFoundException`) immediately scoped the feasible options.

3. **WebFetch for primary sources:** The Mountpoint SEMANTICS.md and Zoekt source code were authoritative. The S3 Files blog post provided enough detail to assess the NFS-based option.

---

## What Didn't Work / Gotchas

1. **kubectl access:** The `adp-dev-agent-scaledjob-role` (which the agent pod runs as) is NOT in the EKS cluster's access entries. Only `ADP-Agent-adp-embark`, `Admin`, `agent-runner-role`, `codebuild-role`, and `node-group-role` have access. Future spikes needing pod operations should run as `ADP-Agent-adp-embark` or request access entry addition.

2. **No .zoekt shards in S3:** The 15 existing shards are ONLY on the EBS PVC (not accessible from the agent). The `zoekt-shards/` prefix in S3 contains only the 0-byte directory marker. #2297's first step must be copying these shards to S3.

3. **S3 Files documentation gaps:** The EKS-specific docs for S3 Files returned 404 or empty content. The blog post was the best source. Key finding: S3 Files requires the EFS CSI driver v3.0+ (NOT the Mountpoint CSI driver), which isn't installed.

---

## Critical Facts for Future Agents

### AWS Environment (account 879318057152, region us-east-1)
- **EKS cluster name:** `adp-dev-eks-cluster`
- **S3 bucket:** `agent-context-platform-data-879318057152`
- **Mountpoint CSI:** v1.15.0-eksbuild.1, ACTIVE, role `adp-dev-eks-cluster-s3-csi-controller`
- **EFS CSI:** NOT installed (would need it for S3 Files)
- **PV/PVC:** `agent-context-platform-data` / `platform-data` (ReadWriteMany, Mountpoint-backed)

### Zoekt Technical Facts
- **Image:** `ghcr.io/sourcegraph/zoekt:${ZOEKT_IMAGE_TAG}`
- **Shard reading:** `unix.Mmap(fd, 0, size, PROT_READ, MAP_SHARED)` — read-only mmap
- **Startup:** `os.MkdirAll(*indexDir, 0o755)` then `NewDirectorySearcherFast(indexDir)`
- **No flag to skip mkdir:** The directory creation is unconditional in startup
- **Shard sizes:** 50-234MB (15 shards, ~1-3GB total on current EBS)

### Mountpoint FUSE Key Behaviors
- mkdir: Supported (local-only until file committed beneath it)
- mmap: Works via kernel page cache (no FOPEN_DIRECT_IO for normal opens)
- Random reads: Supported via pread/preadv
- Prefetch: Up to 2GiB window per file handle, 8MB read-part-size
- --read-only flag: Blocks all mutations (including mkdir on non-existent dirs)
- Metadata refresh: "Minimal" by default (revalidates on access)

### S3 Files Key Facts (for future reference)
- Uses NFS v4.2, NOT FUSE
- Requires EFS CSI driver v3.0+ for EKS integration
- Available in all commercial AWS regions
- High-perf storage: $0.30/GB-month
- Large reads (>=1MB) stream directly from S3 even when cached
- Default cache threshold: files < 128KB
- Mount helper: `amazon-efs-utils` package (same as EFS)

---

## Recommendations for Future Work

1. **For #2297 implementation:** Just change the PVC reference in zoekt.yaml from `zoekt-index` (EBS) to `platform-data` (Mountpoint) with `subPath: zoekt-shards` and `readOnly: true`. Remove the EBS PVC definition.

2. **For cold-start performance:** Consider adding `--cache /tmp/s3-cache` to mount options for local disk caching of shard data. Monitor P99 search latency after migration.

3. **For the agent role:** If future spikes need kubectl access, add `adp-dev-agent-scaledjob-role` to EKS access entries, or run as `ADP-Agent-adp-embark`.

4. **For S3 Files evaluation:** Revisit if a future workload needs full POSIX writes on S3-backed data (e.g., running git directly against shared storage). Not worth the infra cost for read-only Zoekt.
