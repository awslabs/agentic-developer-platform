# Off until the cluster's network-policy and kubelet isolation are qualified.
# The SDK never receives this token or a Kubernetes tool. Only the trusted Task
# host can create source-only Pods in this separate namespace.
variable "codex_kubernetes_validation_enabled" {
  type        = bool
  default     = false
  description = "Provision isolated validation resources and a dedicated host identity. Does not wire the shared worker fleet or enable personas."
}

variable "codex_validation_api_cidrs" {
  type        = list(string)
  default     = []
  description = "Verified EKS API destination host CIDRs used by validation hosts; no subnet-wide access."
  validation {
    condition = alltrue([for value in var.codex_validation_api_cidrs :
      can(cidrhost(value, 0)) && (endswith(value, "/32") || endswith(value, "/128"))
    ])
    error_message = "Use exact API host /32 or /128 CIDRs."
  }
}

locals {
  codex_validation_namespace      = "adp-codex-validation"
  codex_validation_host_namespace = "adp-codex-validation-hosts"
  codex_validation_host_sa        = "validation-host"

}

resource "kubernetes_namespace" "codex_validation_hosts" {
  count = var.codex_kubernetes_validation_enabled ? 1 : 0
  metadata {
    name = local.codex_validation_host_namespace
    labels = {
      "pod-security.kubernetes.io/enforce" = "restricted"
    }
  }
}

resource "kubernetes_service_account" "codex_validation_host" {
  count = var.codex_kubernetes_validation_enabled ? 1 : 0
  metadata {
    name      = local.codex_validation_host_sa
    namespace = kubernetes_namespace.codex_validation_hosts[0].metadata[0].name
  }
}

resource "kubernetes_namespace" "codex_validation" {
  count = var.codex_kubernetes_validation_enabled ? 1 : 0
  metadata {
    name = local.codex_validation_namespace
    labels = {
      "adp.dev/validation"                         = "true"
      "pod-security.kubernetes.io/enforce"         = "restricted"
      "pod-security.kubernetes.io/enforce-version" = "latest"
    }
  }
  lifecycle {
    precondition {
      condition     = var.task_api_worker_enabled && length(var.codex_validation_api_cidrs) > 0
      error_message = "Kubernetes validation requires Task API support and verified API host CIDRs."
    }
  }
}

resource "kubernetes_network_policy" "codex_validation_deny" {
  count = var.codex_kubernetes_validation_enabled ? 1 : 0
  metadata {
    name      = "deny-all"
    namespace = kubernetes_namespace.codex_validation[0].metadata[0].name
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress", "Egress"]
  }
}

resource "kubernetes_resource_quota" "codex_validation" {
  count = var.codex_kubernetes_validation_enabled ? 1 : 0
  metadata {
    name      = "validation-bound"
    namespace = kubernetes_namespace.codex_validation[0].metadata[0].name
  }
  spec {
    hard = {
      pods              = "8"
      configmaps        = "1024"
      "requests.cpu"    = "16"
      "requests.memory" = "16Gi"
      "limits.cpu"      = "16"
      "limits.memory"   = "16Gi"
    }
  }
}

resource "kubernetes_role" "codex_validation" {
  count = var.codex_kubernetes_validation_enabled ? 1 : 0
  metadata {
    name      = "validation-host"
    namespace = kubernetes_namespace.codex_validation[0].metadata[0].name
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

resource "kubernetes_role_binding" "codex_validation" {
  count = var.codex_kubernetes_validation_enabled ? 1 : 0
  metadata {
    name      = "validation-host"
    namespace = kubernetes_namespace.codex_validation[0].metadata[0].name
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role.codex_validation[0].metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account.codex_validation_host[0].metadata[0].name
    namespace = kubernetes_namespace.codex_validation_hosts[0].metadata[0].name
  }
}

resource "kubernetes_cluster_role" "codex_validation_namespace" {
  count = var.codex_kubernetes_validation_enabled ? 1 : 0
  metadata {
    name = "adp-validation-namespace-read"
  }
  rule {
    api_groups     = [""]
    resources      = ["namespaces"]
    resource_names = [local.codex_validation_namespace]
    verbs          = ["get"]
  }
}

resource "kubernetes_cluster_role_binding" "codex_validation_namespace" {
  count = var.codex_kubernetes_validation_enabled ? 1 : 0
  metadata {
    name = "adp-validation-namespace-read"
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role.codex_validation_namespace[0].metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account.codex_validation_host[0].metadata[0].name
    namespace = kubernetes_namespace.codex_validation_hosts[0].metadata[0].name
  }
}

resource "kubernetes_config_map" "codex_validation" {
  count = var.codex_kubernetes_validation_enabled ? 1 : 0
  metadata {
    name      = "codex-validation-host"
    namespace = kubernetes_namespace.codex_validation_hosts[0].metadata[0].name
  }
  data = {
    "config.json" = jsonencode({
      endpoint   = data.aws_eks_cluster.main.endpoint
      namespace  = local.codex_validation_namespace
      token_file = "/var/run/secrets/kubernetes.io/serviceaccount/token"
      ca_file    = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    })
  }
}

resource "kubernetes_network_policy" "codex_validation_host_api" {
  count = var.codex_kubernetes_validation_enabled ? 1 : 0
  metadata {
    name      = "codex-validation-api"
    namespace = kubernetes_namespace.codex_validation_hosts[0].metadata[0].name
  }
  spec {
    pod_selector {
      match_labels = { "app.kubernetes.io/name" = "codex-validation-host" }
    }
    policy_types = ["Egress"]
    egress {
      ports {
        protocol = "UDP"
        port     = "53"
      }
      ports {
        protocol = "TCP"
        port     = "53"
      }
      to {
        namespace_selector {
          match_labels = { "kubernetes.io/metadata.name" = "kube-system" }
        }
        pod_selector {
          match_labels = { "k8s-app" = "kube-dns" }
        }
      }
    }
    egress {
      ports {
        protocol = "TCP"
        port     = "443"
      }
      dynamic "to" {
        for_each = var.codex_validation_api_cidrs
        content {
          ip_block { cidr = to.value }
        }
      }
    }
  }
}
