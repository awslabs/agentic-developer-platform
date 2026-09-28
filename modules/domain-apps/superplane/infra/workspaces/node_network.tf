# Version and system image registries are reviewed inputs, never latest lookups.
locals {
  node_network_policy = jsondecode(file("${path.module}/node-network-pins.json"))
}

resource "aws_eks_addon" "vpc_cni" {
  cluster_name                = aws_eks_cluster.workspace.name
  addon_name                  = "vpc-cni"
  addon_version               = local.node_network_policy.vpc_cni_version
  configuration_values        = jsonencode({ enableNetworkPolicy = "true" })
  service_account_role_arn    = aws_iam_role.vpc_cni.arn
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"
  depends_on                  = [aws_iam_role_policy_attachment.vpc_cni]
}
