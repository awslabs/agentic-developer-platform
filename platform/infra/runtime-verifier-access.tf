# The orchestration engine verifies deployments at runtime as the environment's
# connection role (the owner-created ADP-Agent-<nickname> role from the gateway's
# CloudFormation "Add AWS account" template, e.g. ADP-Agent-dev-platform). It reads
# Deployments, ReplicaSets, Pods and Services and probes service proxies in each
# deployment target namespace, nothing more. This access entry maps that role to a
# Kubernetes group; the namespaced Roles bound to the group live with each target:
#   - adp-gateway:   modules/agent-factory/infra/runtime-verifier-rbac.tf
#   - agent-context: modules/agent-context/manifests/ingestion-rbac.yaml
#
# The role only exists after the owner connects the account in the UI, so the ARN is
# account-specific and supplied per environment (TF_VAR_runtime_verifier_principal_arn
# or a private tfvars overlay), never committed. It lives in the platform stack so the
# self-managed deploy (deploy-all.sh) applies it; no GitHub-environment workflow is
# involved. Created live on dev 2026-10-15 (#7870). Adopt the live entry with:
#   terraform import 'aws_eks_access_entry.runtime_verifier[0]' '<cluster_name>:<principal_arn>'

variable "runtime_verifier_principal_arn" {
  description = "ARN of the environment connection role the orchestration engine verifies deployments with; empty disables the access entry."
  type        = string
  default     = ""
}

resource "aws_eks_access_entry" "runtime_verifier" {
  count             = var.runtime_verifier_principal_arn == "" ? 0 : 1
  cluster_name      = module.eks.cluster_name
  principal_arn     = var.runtime_verifier_principal_arn
  type              = "STANDARD"
  kubernetes_groups = ["adp:runtime-verifier-${var.environment}"]
}
