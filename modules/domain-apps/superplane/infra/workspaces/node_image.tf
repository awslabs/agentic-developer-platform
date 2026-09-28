# Reviewed regional EKS-optimized AL2023 image releases. Updating this file changes
# the saved plan; an unchanged plan never resolves the current recommended image.
locals {
  node_image_policy  = jsondecode(file("${path.module}/node-image-pins.json"))
  node_image_release = try(local.node_image_policy.releases[var.aws_region][var.cluster_version], null)
}

output "node_image_pin" {
  value = {
    ami_type           = local.node_image_policy.ami_type
    release_version    = local.node_image_release
    region             = var.aws_region
    kubernetes_version = var.cluster_version
    reviewed_on        = local.node_image_policy.reviewed_on
  }
}
