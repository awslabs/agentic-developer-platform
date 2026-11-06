variable "isolated_node_pool" {
  description = "Optional operator-owned isolated capacity. Candidate mode consolidates empty nodes; warm mode retains idle nodes until normal expiry. Neither mode qualifies a node or enables runtime work."
  type = object({
    cluster_name       = string
    node_role_name     = string
    subnet_ids         = list(string)
    security_group_ids = list(string)
    capacity_mode      = optional(string, "candidate")
  })
  default  = null
  nullable = true
  validation {
    condition = var.isolated_node_pool == null ? true : (
      length(var.isolated_node_pool.subnet_ids) > 0 &&
      length(var.isolated_node_pool.security_group_ids) > 0 &&
      contains(["candidate", "warm"], var.isolated_node_pool.capacity_mode) &&
      (var.executor == null ? true : var.executor.cluster_name == var.isolated_node_pool.cluster_name)
    )
    error_message = "Select the same EKS cluster as the executor, explicit private subnets and node security groups, and candidate or warm capacity mode."
  }
}

locals {
  isolated_pool_enabled = var.isolated_node_pool == null ? {} : { selected = var.isolated_node_pool }
  isolated_pool_name    = "adp-${var.environment}-l5-transform"
}

# Separate from the existing Codex validation pool. No qualification label may
# be inherited by replacement nodes. Promotion belongs to independent probes.
resource "kubernetes_manifest" "isolated_node_class" {
  for_each = local.isolated_pool_enabled
  manifest = {
    apiVersion = "eks.amazonaws.com/v1"
    kind       = "NodeClass"
    metadata   = { name = local.isolated_pool_name }
    spec = {
      role                = each.value.node_role_name
      subnetSelectorTerms = [for id in each.value.subnet_ids : { id = id }]
      securityGroupSelectorTerms = [for id in concat(each.value.security_group_ids,
      var.provider_endpoint == null ? [] : [aws_security_group.isolated_node_endpoint["selected"].id]) : { id = id }]
      networkPolicy          = "DefaultDeny"
      networkPolicyEventLogs = "Enabled"
      advancedCompute        = { kubelet = { podPidsLimit = 128 } }
      ephemeralStorage       = { size = "80Gi" }
    }
  }
}

resource "kubernetes_manifest" "isolated_node_pool" {
  for_each = local.isolated_pool_enabled
  manifest = {
    apiVersion = "karpenter.sh/v1"
    kind       = "NodePool"
    metadata   = { name = local.isolated_pool_name }
    spec = {
      limits = { cpu = "4" }
      template = {
        metadata = { labels = { "adp.dev/transform-candidate" = "v1" } }
        spec = {
          nodeClassRef = { group = "eks.amazonaws.com", kind = "NodeClass", name = local.isolated_pool_name }
          taints       = [{ key = "adp.dev/transform", value = "only", effect = "NoSchedule" }]
          expireAfter  = "24h"
          requirements = [
            { key = "karpenter.sh/capacity-type", operator = "In", values = ["on-demand"] },
            { key = "kubernetes.io/arch", operator = "In", values = ["amd64"] },
            { key = "kubernetes.io/os", operator = "In", values = ["linux"] },
            { key = "node.kubernetes.io/instance-type", operator = "In", values = ["m6a.xlarge"] }
          ]
        }
      }
      disruption = {
        consolidationPolicy = "WhenEmpty"
        consolidateAfter    = each.value.capacity_mode == "warm" ? "Never" : "60s"
      }
    }
  }
  depends_on = [kubernetes_manifest.isolated_node_class]
}

output "isolated_node_pool_name" {
  value = var.isolated_node_pool == null ? null : local.isolated_pool_name
}
