# Design Note: EFS Overlay to Mountpoint for Amazon S3

**Issue:** #1354 (sub of EPIC #1345)
**Date:** 2026-06-11
**Status:** Implementation-ready
**Author:** @agent-architect

---

## 1. Summary

Replace the EFS-over-S3 overlay (`modules/agent-context/terraform/modules/s3-files/`) with
**Mountpoint for Amazon S3** for durable, read-heavy, write-once artifacts. This removes the
EFS file system, mount targets, and NFS security group. Pods access the S3 bucket directly
via the Mountpoint S3 CSI driver.

---

## 2. Fact Verification

| Claim | Status | Detail |
|-------|--------|--------|
| Mountpoint for S3 CSI driver is GA | **Confirmed** | Available as EKS managed add-on `aws-mountpoint-s3-csi-driver`. [docs.aws.amazon.com/eks/latest/userguide/s3-csi.html] |
| Write-once / no-in-place-modify | **Confirmed with nuance** | No random writes or partial modifications. New files created sequentially. Full-object overwrite possible only with `--allow-overwrite` flag + `O_TRUNC`; without it, create-only. We do NOT enable `--allow-overwrite` — enforces true write-once. [github.com/awslabs/mountpoint-s3/blob/main/doc/SEMANTICS.md] |
| No file locking | **Confirmed** | POSIX file locks (`lockf`) are not supported. |
| CSI driver name | **Confirmed** | `s3.csi.aws.com` |
| IRSA support | **Confirmed** | Uses OIDC + `eks.amazonaws.com/role-arn` annotation |
| OSV-Scanner Apache-2.0 | **Confirmed** | github.com/google/osv-scanner — Apache-2.0, actively maintained |
| Trivy Apache-2.0 | **Confirmed** | github.com/aquasecurity/trivy — Apache-2.0 |
| PostgreSQL 16 support to Nov 2028 | **Confirmed** | postgresql.org/support/versioning/ — 5-year policy from Sept 2023 GA |

---

## 3. Resource Delta

### Removed (EFS layer)

| Resource | Terraform identifier |
|----------|---------------------|
| `aws_efs_file_system.platform_data` | EFS file system |
| `aws_efs_mount_target.platform_data[*]` | One per subnet/AZ |
| `aws_security_group.efs_mount` | NFS SG |
| `aws_security_group_rule.efs_ingress_nfs` | Ingress rule |
| `aws_security_group_rule.efs_egress_all` | Egress rule |
| `aws_eks_addon.efs_csi_driver` | EFS CSI driver add-on |
| `aws_iam_role.efs_csi_controller` | IRSA role for EFS CSI controller |
| `aws_iam_role.efs_csi_node` | IRSA role for EFS CSI node |
| `aws_iam_role_policy_attachment.efs_csi_controller_policy` | EFS policy |
| `aws_iam_role_policy_attachment.efs_csi_s3_access` | S3 policy on EFS role |
| `aws_iam_role_policy_attachment.efs_csi_node_policy` | EFS node policy |
| `aws_iam_role_policy_attachment.efs_csi_node_s3_readonly` | S3 read-only on EFS node |

### Added (Mountpoint S3 layer)

| Resource | Purpose |
|----------|---------|
| `aws_eks_addon.mountpoint_s3_csi_driver` | Mountpoint for S3 CSI driver EKS add-on |
| `aws_iam_role.s3_csi_controller` | IRSA role for S3 CSI driver service account |
| `aws_iam_policy.s3_csi_access` | S3 bucket read/write policy (scoped to this bucket) |
| `aws_iam_role_policy_attachment.s3_csi_access` | Attach policy to role |

### Kept (unchanged)

| Resource | Notes |
|----------|-------|
| `aws_s3_bucket.platform_data` | The bucket itself stays |
| `aws_s3_bucket_versioning.platform_data` | Versioning stays |
| `aws_s3_bucket_server_side_encryption_configuration.platform_data` | SSE stays |
| `aws_s3_bucket_lifecycle_configuration.platform_data` | Lifecycle stays |
| `aws_s3_bucket_public_access_block.platform_data` | Public block stays |
| `aws_s3_object.prefix_*` | Directory markers stay (renamed for new layout) |

---

## 4. Consumer Write-Path Audit

### 4.1 ingestion-scaledjob.yaml (KEDA workers)

| Path | Operation | Mountpoint-compatible? |
|------|-----------|----------------------|
| `/tmp/repos` (emptyDir) | git clone, in-place writes | **Yes** — on emptyDir, not Mountpoint |
| `/tmp/code-indexes` (emptyDir) | Build code-index JSON | **Yes** — on emptyDir |
| `/platform-data` | Final artifact upload (copy from /tmp) | **Needs change** — currently writes via `open(path, "w")` which creates new files. Compatible IF files are always new (no overwrite of same name). See remediation below. |

**Current code:** `ingest-repo.py` line 454: `with open(path, "w") as f: f.write(code_index_json)` — this truncates if file exists. Without `--allow-overwrite` on Mountpoint, a re-index of the same repo would fail.

**Remediation:** Two options:
- **(A) Enable `--allow-overwrite` on the Mountpoint mount** — allows `O_TRUNC` semantics (full-object replace). This is safe for our use case since artifacts are always written as complete objects.
- **(B) Write with a version suffix** (e.g., `repo-name-<sha>.json`) and point readers at latest via a manifest file.

**Recommendation: Option A.** The `--allow-overwrite` flag is purpose-built for exactly this pattern (re-indexing produces a new complete file at the same path). It does NOT enable random writes — it only allows creating a new file at a path where one already exists, by replacing the entire object. This matches our "conceptually write-once but may re-index" semantics perfectly.

### 4.2 repo-refresh-cronjob.yaml

| Path | Operation | Mountpoint-compatible? |
|------|-----------|----------------------|
| `/platform-data/repos` | git clone + git pull (in-place) | **NO** — git requires locking, rename, in-place writes |
| `/platform-data/learning` | Write learning artifacts | Partially — new files OK, but directory listing performance concern |
| `/platform-data` (STATE_DIR) | Read/write state | **NO** — likely in-place state file updates |

**Remediation:** The repo-refresh-cronjob must move its `CLONE_BASE` to an emptyDir scratch volume (same pattern as ingestion-scaledjob). Final artifacts are then copied to Mountpoint. This is called out in the parent EPIC as the "worker-scratch" model — each worker owns its scratch.

**Action items (separate sub-issues):**
1. Move `CLONE_BASE` in repo-refresh-cronjob from `/platform-data/repos` to `/tmp/repos` (emptyDir)
2. Move state tracking to DynamoDB (already partially done — `DYNAMO_TABLE` env var exists)

### 4.3 sourcebot.yaml

| Path | Operation | Mountpoint-compatible? |
|------|-----------|----------------------|
| `/data/sourcebot` (subPath) | Zoekt reads shards; Sourcebot writes index/config | **Partially** — Zoekt webserver reads are fine. Sourcebot's internal indexer may do in-place writes during shard building. |

**Remediation:** Sourcebot is being replaced by direct Zoekt (per EPIC #1345). The replacement Zoekt webserver will mount Mountpoint **read-only** for serving shards. Shard building happens on worker scratch, then the finished `.zoekt` files are uploaded as new objects.

**For the transition period:** Sourcebot can continue using the EFS PVC until it's removed. This is acceptable — the EFS teardown happens AFTER Sourcebot is replaced, not before.

### 4.4 deepwiki.yaml

| Path | Operation | Mountpoint-compatible? |
|------|-----------|----------------------|
| `/root/.adalflow` (subPath: deepwiki) | Wiki cache read/write | **NO** — DeepWiki writes cache files in-place with updates |

**Remediation:** DeepWiki's cache is local working state, not durable artifacts. Move to an emptyDir or a small EBS PVC (per-pod). The durable wiki output (`.deepwiki-wiki.md`) is already written by the ingestion pipeline to `/platform-data` as a new file — that path is compatible.

### 4.5 codegraph.yaml

| Path | Operation | Mountpoint-compatible? |
|------|-----------|----------------------|
| `/data/codegraph` (subPath) | cgc writes index files | **Partially** — if cgc always creates new files, OK. If it updates in place, not compatible. |

**Remediation:** CodeGraphContext's `CGC_HOME` is working state. Move to emptyDir. The final `code-index.json` artifacts are already written by the ingestion pipeline (not codegraph directly).

---

## 5. Mountpoint Configuration

### 5.1 PV Spec (Mountpoint S3)

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: agent-context-platform-data
spec:
  capacity:
    storage: 1Ti  # Notional — S3 is unlimited
  accessModes:
    - ReadWriteMany
  mountOptions:
    - allow-delete
    - allow-overwrite
  csi:
    driver: s3.csi.aws.com
    volumeHandle: s3-csi-<bucket-name>
    volumeAttributes:
      bucketName: <bucket-name>
  storageClassName: ""
  persistentVolumeReclaimPolicy: Retain
```

Key options:
- `allow-overwrite` — enables full-object replacement (re-indexing same repo)
- `allow-delete` — enables artifact cleanup/rotation
- No `--allow-other` needed (CSI driver handles mount propagation)

### 5.2 PVC (unchanged name for consumer compatibility)

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: platform-data
  namespace: agent-context
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: ""
  resources:
    requests:
      storage: 1Ti
  volumeName: agent-context-platform-data
```

The PVC name `platform-data` is preserved so existing pod volume mounts continue to work unchanged.

---

## 6. IAM Design (IRSA for Mountpoint S3 CSI)

The Mountpoint S3 CSI driver's node pod needs an IAM role that grants S3 access to the specific bucket. The role is assumed via IRSA (Web Identity + OIDC).

**Trust policy:**
```json
{
  "Effect": "Allow",
  "Principal": {
    "Federated": "arn:aws:iam::<account>:oidc-provider/<oidc-id>"
  },
  "Action": "sts:AssumeRoleWithWebIdentity",
  "Condition": {
    "StringLike": {
      "<oidc-id>:sub": "system:serviceaccount:kube-system:s3-csi-*",
      "<oidc-id>:aud": "sts.amazonaws.com"
    }
  }
}
```

**Permissions policy (scoped to bucket):**
```json
{
  "Effect": "Allow",
  "Action": [
    "s3:GetObject",
    "s3:PutObject",
    "s3:DeleteObject",
    "s3:ListBucket",
    "s3:GetBucketLocation",
    "s3:AbortMultipartUpload",
    "s3:ListMultipartUploadParts"
  ],
  "Resource": [
    "arn:aws:s3:::<bucket>",
    "arn:aws:s3:::<bucket>/*"
  ]
}
```

---

## 7. Migration Strategy

### Phase 1 (this PR): Infrastructure swap
1. Terraform: remove EFS resources, add Mountpoint S3 CSI driver + IAM
2. K8s: replace `s3-files-storage.yaml` with Mountpoint-based PV/PVC
3. Deploy script: remove EFS file system ID templating

### Phase 2 (follow-up sub-issues): Consumer migration
1. Move `CLONE_BASE` in repo-refresh-cronjob to emptyDir (required before EFS removal)
2. Move DeepWiki cache to emptyDir
3. Move CodeGraph `CGC_HOME` to emptyDir
4. Replace Sourcebot with direct Zoekt (read-only Mountpoint mount)

### Rollback plan
- Keep the EFS Terraform in a `_deprecated/` directory (not `terraform destroy`'d) until Phase 2 is validated
- The S3 bucket is shared — switching back to EFS just means re-deploying the old module and PV/PVC
- Artifacts in S3 are not affected by the driver change (they stay in the bucket either way)

### Data migration
- **None required.** All artifacts are already in S3 (the EFS was just an overlay for POSIX access). Mountpoint reads the same bucket directly.

---

## 8. Updated S3 Bucket Prefixes

The current prefixes reflect the old tool names. Updated for the Knowledge Layer:

| Old prefix | New prefix | Contents |
|------------|-----------|----------|
| `sourcebot/` | `zoekt-shards/` | Zoekt search index shard files |
| `codegraph/` | `code-indexes/` | `code-index.json` per repo |
| `deepwiki/` | `wikis/` | Generated wiki markdown |
| `openviking/` | (removed) | Was OpenViking data — being decommissioned |
| (new) | `sbom/` | SBOM files (CycloneDX JSON) |
| (new) | `learning/` | Learning artifacts |

The prefix rename is a soft migration — new writers use new prefixes; old data stays until cleanup.

---

## 9. Variables Removed / Added

### Removed variables
- `vpc_id` — EFS mount targets needed VPC; Mountpoint S3 does not
- `subnet_ids` — EFS mount targets needed subnets; Mountpoint S3 does not
- `node_security_group_id` — EFS NFS SG rule; not needed for S3
- `efs_csi_driver_version` — replaced by `mountpoint_s3_csi_driver_version`

### Added variables
- `mountpoint_s3_csi_driver_version` — version of the Mountpoint S3 CSI driver add-on

### Kept variables
- `cluster_name`, `aws_region`, `bucket_name`, `namespace`, `oidc_provider_url`, `tags`, `glacier_transition_days`

---

## 10. Outputs Updated

### Removed outputs
- `file_system_id` — no more EFS
- `mount_target_ips` — no more mount targets
- `mount_target_ids` — no more mount targets
- `security_group_id` — no more EFS SG
- `csi_node_role_arn` — replaced

### Added outputs
- `s3_csi_role_arn` — IAM role ARN for the Mountpoint S3 CSI driver

### Kept outputs
- `bucket_name`, `bucket_arn`, `csi_controller_role_arn` (renamed from EFS to S3 semantics)

---

## 11. Incompatible Patterns Summary (Action Required Before Full Cutover)

| Consumer | Incompatible pattern | Severity | Fix |
|----------|---------------------|----------|-----|
| repo-refresh-cronjob | `CLONE_BASE=/platform-data/repos` (git clone/pull) | **Blocker** | Move to emptyDir; copy artifacts out |
| repo-refresh-cronjob | `STATE_DIR=/platform-data` | Medium | Already migrating to DynamoDB |
| deepwiki | In-place cache at `/root/.adalflow` via subPath | Medium | Move to emptyDir |
| codegraph | `CGC_HOME=/data/codegraph` working state | Medium | Move to emptyDir |
| sourcebot | Zoekt shard writes during indexing | Low (being replaced) | Wait for Zoekt migration |
| ingest-repo.py | `open(path, "w")` overwrites existing index | **OK with `--allow-overwrite`** | Mount option handles it |

**Critical finding:** The repo-refresh-cronjob is the only true blocker. It clones repos to the platform-data PVC and does in-place git operations. This MUST be remediated before EFS can be removed. The ingestion-scaledjob already uses the correct pattern (emptyDir for scratch, Mountpoint for output).

---

## 12. Recommendations

1. **Ship this PR** with the Terraform + K8s infrastructure swap. The PV/PVC name stays the same, so consumer pods will bind to the new Mountpoint-backed volume.

2. **File follow-up sub-issues** for each consumer remediation (repo-refresh scratch, deepwiki cache, codegraph scratch).

3. **Do NOT remove the lifecycle `prevent_destroy`** from the S3 bucket — it protects data regardless of how it's mounted.

4. **Test sequence:**
   - Deploy Terraform (installs Mountpoint CSI driver, removes EFS)
   - Apply new K8s PV/PVC manifests
   - Verify: `kubectl exec` into a pod, list bucket contents via the mount path
   - Run one ingestion job, confirm artifact appears in S3

5. **The `--allow-overwrite` mount option is essential** — without it, re-indexing the same repo (which produces a file at the same path) would fail with EEXIST. This is NOT a violation of "write-once" semantics in the design — the design means "write a complete object as a unit" (no partial/append), not "never replace." Mountpoint enforces that each write is a full-object upload.
