output "namespace" {
  value = kubernetes_namespace_v1.adapter.metadata[0].name
}

output "runtime_role_arn" {
  description = "IRSA trust role to pass as ADP_TOOLS_ADAPTER_ROLE_ARN; not the workflow mutation role."
  value       = aws_iam_role.runtime.arn
}

output "runtime_verifier_group" {
  value = var.runtime_verifier_group
}

output "verified_cluster_arn" {
  value = data.aws_eks_cluster.target.arn
}
