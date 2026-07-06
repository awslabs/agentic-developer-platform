# Issue #2374: gVisor Node Group + RuntimeClass + RBAC — Learnings

## Date: 2026-06-30
## Agent: @agent-operations
## PR: #2430

## Summary
Added gVisor infrastructure (EKS managed node group, RuntimeClass, RBAC) to the platform layer. This is the "infra, no flip" story — resources exist but no workloads use them yet.

## Key Technical Decisions

### 1. EKS Managed Node Group (not EC2NodeClass/Karpenter NodePool)
The cluster uses EKS Auto Mode which runs AWS-managed Karpenter. The `karpenter.k8s.aws/v1` EC2NodeClass CRD is NOT installed — only `eks.amazonaws.com/v1` NodeClass exists. You cannot create custom EC2NodeClass resources without deploying the full open-source Karpenter controller alongside Auto Mode. Hence: `aws_eks_node_group` Terraform resource.

### 2. AL2023 without custom AMI (not Bottlerocket)
- Bottlerocket does NOT support `enable-gvisor` in its container-runtime config (Phase 0 finding)
- We do NOT specify `image_id` in the launch template. EKS manages the AMI for `ami_type = "AL2023_x86_64_STANDARD"` and auto-injects NodeConfig bootstrap
- User-data is a MIME multipart script that installs runsc pre-bootstrap
- If you DO specify a custom AMI, EKS treats it as fully custom and does NOT inject bootstrap — you'd need to provide the full NodeConfig YAML yourself (including cluster endpoint, CA cert, service CIDR)

### 3. Containerd runtime registration
The gVisor install script appends to `/etc/containerd/config.toml`:
```toml
[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc]
  runtime_type = "io.containerd.runsc.v1"
```
Then restarts containerd. This must happen BEFORE kubelet starts (which is guaranteed by MIME multipart user-data ordering in EKS managed node groups).

### 4. RuntimeClass applied via null_resource + kubectl
Same pattern as `nodepool-packer.tf`. The `kubernetes_manifest` provider causes read-back drift because the API server normalizes empty `scheduling` sub-fields. The null_resource includes `--dry-run=server` validation before the actual apply.

## Gotchas

### Terraform apply from agent pod blocked by access entry conflict
When running `terraform apply` from the agent pod (`adp-dev-agent-scaledjob-role`), Terraform's `deployer_role_arn` logic in main.tf adds the current caller to `cluster_admin_principal_arns`. If that role already has an EKS access entry created outside Terraform state (which it does), the apply fails with `ResourceInUseException`. 

**Workaround**: Apply from the CI runner role (canonical deployer), or `terraform import` the existing access entry first:
```
terraform import 'module.eks.aws_eks_access_entry.admins["arn:aws:iam::879318057152:role/adp-dev-agent-scaledjob-role"]' adp-dev-eks-cluster:arn:aws:iam::879318057152:role/adp-dev-agent-scaledjob-role
```

### Terraform state backend
- Bucket: `adp-terraform-state-879318057152`
- State key: `dev/platform/terraform.tfstate` (NOT `platform/terraform.tfstate`)
- DynamoDB lock table: `adp-terraform-locks` (NOT `adp-terraform-state-lock`)

### vpc_security_group_ids in launch template
When using EKS managed node groups with a launch template, if you specify `vpc_security_group_ids`, EKS will NOT attach the managed cluster security group automatically. You need to include `module.eks.cluster_security_group_id` explicitly.

### Scaling min=0 behavior
EKS managed node groups with `min_size=0, desired_size=0` will NOT scale up automatically via Cluster Autoscaler unless pods with matching tolerations are pending. The Cluster Autoscaler expands node groups only when pods cannot be scheduled. This is the desired behavior for cost control.

## What Worked Well
- The `nodepool-packer.tf` pattern (null_resource + kubectl + sha256 trigger) is well-established and worked perfectly for RuntimeClass
- The `warm-pool.tf` RBAC pattern (ClusterRole with name-restricted mutating verbs) is a clean least-privilege model
- Server-side dry-run (`kubectl apply --dry-run=server`) caught potential issues before live apply
- The Kubernetes provider RBAC resources (ClusterRole/ClusterRoleBinding) apply cleanly via Terraform even when other resources are blocked

## Cluster Details (for future agents)
- Cluster: `adp-dev-eks-cluster`
- Region: `us-east-1`
- Account: `879318057152`
- EKS Version: 1.35
- Mode: EKS Auto Mode (compute_config.enabled = true)
- Agent namespace: `adp-agents`
- Agent SA: `agent-scaledjob-sa`
- CI Runner SA: `github-runner-sa` (namespace: `arc-runners`)

## Next Steps
- #2376 (the "flip" story): Add `runtimeClassName: gvisor` to agent pod specs
- The managed node group Terraform resources need a CI-driven `terraform apply` to create the actual node infrastructure
- Phase 0 test node group `gvisor-al2023-test` is in DELETING state; will be cleaned up automatically
