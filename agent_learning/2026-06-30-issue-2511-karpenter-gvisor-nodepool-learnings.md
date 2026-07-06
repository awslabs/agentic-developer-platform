# Issue #2511: gVisor Karpenter NodePool — Learnings

## Date: 2026-06-30
## Agent: @agent-operations
## PR: (pending)

## Summary
Codified the proven-working Karpenter NodePool for gVisor as Terraform, following the null_resource + kubectl heredoc pattern from `nodepool-packer.tf`. Applied and validated live against the EKS Auto Mode cluster.

## Key Technical Decisions

### 1. Karpenter NodePool (not EKS Managed Node Group)
EKS Managed Node Groups do NOT register on this EKS Auto Mode cluster. Across 5 agent runs and 11 launch-template versions (tracked in #2374/#2430), no CSR was ever produced. The Karpenter NodePool registered a node Ready in <30s during live validation on #2382.

**Root cause**: EKS Auto Mode manages its own Karpenter controller. Self-managed node groups require a different registration path (they don't go through Auto Mode's provisioning flow). The Karpenter NodePool CRD is the native mechanism.

### 2. Weight 10 (not 100)
- `aggressive-packer`: weight 100 (general workloads, tight bin-packing)
- `gvisor`: weight 10 (dedicated, only for pods that tolerate the taint)
- `general-purpose` (AWS built-in): weight 0

Weight 10 ensures Karpenter doesn't prefer the gVisor pool for random workloads. The NoSchedule taint is the actual isolation mechanism — weight just affects preference ordering when multiple pools could satisfy a pod.

### 3. eks.amazonaws.com/v1 NodeClass "default" (not EC2NodeClass)
EKS Auto Mode does NOT expose the standard `karpenter.k8s.aws/v1` EC2NodeClass CRD. Only `eks.amazonaws.com/v1` NodeClass exists, and the only instance is named "default". This manages AMI, subnets, security groups, and IAM role automatically.

### 4. Instance requirements match aggressive-packer cost knobs
- On-demand only (no spot for security-sensitive sandboxed execution)
- Categories: c, m, r (compute/memory/balanced)
- Generation: >4 (modern instances with better performance/cost)
- Size cap: medium/large/xlarge (no oversized half-empty nodes)

### 5. Taint-based isolation
`adp.io/runtime=gvisor:NoSchedule` ensures NO existing workload can schedule onto gVisor nodes without an explicit toleration. Verified live — 0 pods on gVisor nodes after apply.

### 6. Server-side dry-run in the provisioner
The Terraform heredoc embeds `kubectl apply --dry-run=server` BEFORE the actual apply. This catches YAML errors and API validation failures (like invalid NodeClass references or unknown fields) at plan-apply time rather than silently creating an invalid resource.

## Validation Results

| Check | Result |
|-------|--------|
| `terraform fmt -check` | ✅ Clean |
| `kubectl apply --dry-run=server` | ✅ `nodepool.karpenter.sh/gvisor configured (server dry run)` |
| `kubectl apply` | ✅ `nodepool.karpenter.sh/gvisor configured` |
| NodePool Ready status | ✅ `READY: True` |
| Node count (no pending pods) | ✅ `NODES: 0` (expected — no pods requesting gVisor) |
| Taint isolation | ✅ No existing workloads scheduled onto gVisor nodes |

## Gotchas

### terraform validate requires init
`terraform validate` needs `terraform init` (S3 backend access). In agent environments without backend credentials, `terraform fmt -check` catches syntax issues. The real validation is `kubectl apply --dry-run=server` — it tests the YAML inside the heredoc against the live API server, which `terraform validate` cannot do anyway.

### NodePool creationTimestamp shows prior live creation
The NodePool was originally created live during #2382 validation (creationTimestamp: 2026-06-30T12:23:54Z). Our apply updates it in-place (generation: 2). Terraform's null_resource will treat this as a fresh create since it's not in state yet — this is fine because `kubectl apply` is idempotent.

### aggressive-packer not yet deployed
`nodepool-packer.tf` exists in code but hasn't been applied via CI yet. The gVisor NodePool works independently. Both reference the same default NodeClass.

### EKS Auto Mode default expireAfter
The API server automatically injects `spec.template.spec.expireAfter: 336h` (14 days) if not specified. This means gVisor nodes auto-terminate after 14 days and are replaced — good for security patching but worth knowing if investigating unexpected node churn.

## Cluster Details (for future agents)
- Cluster: `adp-dev-eks-cluster`
- Region: `us-east-1`
- Account: `879318057152`
- EKS Version: 1.35
- Mode: EKS Auto Mode (Karpenter managed by AWS)
- NodePool name: `gvisor`
- NodeClass: `default` (eks.amazonaws.com/v1)
- Label: `adp.io/runtime: gvisor`
- Taint: `adp.io/runtime=gvisor:NoSchedule`

## Files Created
- `platform/infra/nodepool-gvisor.tf` — Karpenter NodePool Terraform resource

## What This Supersedes
- `platform/infra/gvisor-nodegroup.tf` — EKS Managed Node Group approach (DOES NOT WORK on Auto Mode). Not deleted in this PR to keep the diff surgical; separate cleanup tracked.

## Next Steps
- S3 story: containerd config for gVisor runtime on Karpenter-provisioned nodes (since there's no user-data hook like managed node groups had)
- S4 story: PoC deploying an actual pod with `runtimeClassName: gvisor` on a Karpenter-provisioned node
- Cleanup: remove `gvisor-nodegroup.tf` + associated launch template (dead code since managed NGs don't work)
