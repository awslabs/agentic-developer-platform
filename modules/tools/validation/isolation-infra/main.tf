terraform {
  required_version = ">= 1.7, < 2.0"
  required_providers {
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 2.0" }
  }
}

variable "node_role_name" { type = string }
variable "subnet_ids" { type = list(string) }
variable "security_group_ids" { type = list(string) }

# Standalone owner for deployments that keep webhook validation provisioning off.
# No qualification label is assigned here. Each actual node must pass probes.
resource "kubernetes_manifest" "nodeclass" {
  manifest = {
    apiVersion = "eks.amazonaws.com/v1"
    kind       = "NodeClass"
    metadata   = { name = "adp-codex-validation" }
    spec = {
      role                       = var.node_role_name
      subnetSelectorTerms        = [for id in var.subnet_ids : { id = id }]
      securityGroupSelectorTerms = [for id in var.security_group_ids : { id = id }]
      networkPolicy              = "DefaultDeny"
      networkPolicyEventLogs     = "Enabled"
      advancedCompute            = { kubelet = { podPidsLimit = 128 } }
      ephemeralStorage           = { size = "80Gi" }
    }
  }
}

resource "kubernetes_manifest" "nodepool" {
  manifest = {
    apiVersion = "karpenter.sh/v1"
    kind       = "NodePool"
    metadata   = { name = "adp-codex-validation" }
    spec = {
      limits = { cpu = "4" }
      template = {
        metadata = { labels = { "adp.dev/validation-candidate" = "v1" } }
        spec = {
          nodeClassRef = { group = "eks.amazonaws.com", kind = "NodeClass", name = kubernetes_manifest.nodeclass.manifest.metadata.name }
          taints       = [{ key = "adp.dev/validation", value = "only", effect = "NoSchedule" }]
          expireAfter  = "24h"
          requirements = [
            { key = "karpenter.sh/capacity-type", operator = "In", values = ["on-demand"] },
            { key = "kubernetes.io/arch", operator = "In", values = ["amd64"] },
            { key = "node.kubernetes.io/instance-type", operator = "In", values = ["m6a.xlarge"] }
          ]
        }
      }
      disruption = { consolidationPolicy = "WhenEmpty", consolidateAfter = "60s" }
    }
  }
}

resource "kubernetes_namespace_v1" "validation" {
  metadata {
    name = "adp-codex-validation"
    labels = {
      "adp.dev/validation"                         = "true"
      "pod-security.kubernetes.io/enforce"         = "restricted"
      "pod-security.kubernetes.io/enforce-version" = "latest"
    }
  }
}

resource "kubernetes_network_policy_v1" "deny" {
  metadata {
    name      = "deny-all"
    namespace = kubernetes_namespace_v1.validation.metadata[0].name
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress", "Egress"]
  }
}

resource "kubernetes_resource_quota_v1" "validation" {
  metadata {
    name      = "validation-bound"
    namespace = kubernetes_namespace_v1.validation.metadata[0].name
  }
  spec {
    hard = {
      pods         = "8", configmaps = "1024", "requests.cpu" = "4", "requests.memory" = "8Gi",
      "limits.cpu" = "4", "limits.memory" = "8Gi"
    }
  }
}

resource "kubernetes_role_v1" "validation" {
  metadata {
    name      = "validation-host"
    namespace = kubernetes_namespace_v1.validation.metadata[0].name
  }
  rule {
    api_groups = [""]
    resources  = ["pods", "configmaps"]
    verbs      = ["create", "get", "list", "delete", "patch"]
  }
  rule {
    api_groups = [""]
    resources  = ["pods/log"]
    verbs      = ["get"]
  }
  rule {
    api_groups = ["networking.k8s.io"]
    resources  = ["networkpolicies"]
    verbs      = ["get", "list"]
  }
}

resource "kubernetes_cluster_role_v1" "namespace_read" {
  metadata { name = "adp-validation-namespace-read" }
  rule {
    api_groups     = [""]
    resources      = ["namespaces"]
    resource_names = [kubernetes_namespace_v1.validation.metadata[0].name]
    verbs          = ["get"]
  }
}
