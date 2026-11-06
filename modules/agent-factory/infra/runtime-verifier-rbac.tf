# Read-only runtime verification of the tools facade by the orchestration engine's
# connection role (EKS access-entry group adp:runtime-verifier-<env>, declared in
# platform/infra/runtime-verifier-access.tf). The verifier reads the
# Deployment, its ReplicaSets, Pods and Service and probes /health through the API
# server's scoped service proxy. Granted live on dev 2026-10-15 (#7870); the
# gateway-deploy workflow cannot apply it (its role may not grant services/proxy),
# so it lives with the other adp-gateway RBAC this stack owns. Adopt the live objects:
#   terraform import kubernetes_role.runtime_verifier adp-gateway/adp-runtime-verifier
#   terraform import kubernetes_role_binding.runtime_verifier adp-gateway/adp-runtime-verifier

resource "kubernetes_role" "runtime_verifier" {
  metadata {
    name      = "adp-runtime-verifier"
    namespace = "adp-gateway"
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "adp-orchestration"
      "app.kubernetes.io/component"  = "runtime-verifier"
    }
  }

  rule {
    api_groups = ["apps"]
    resources  = ["deployments", "replicasets"]
    verbs      = ["get", "list"]
  }

  rule {
    api_groups = [""]
    resources  = ["pods", "services"]
    verbs      = ["get", "list"]
  }

  rule {
    api_groups     = [""]
    resources      = ["services/proxy"]
    resource_names = ["http:tools-facade:8080", "http:tools-facade:80", "tools-facade"]
    verbs          = ["get", "create"]
  }
}

resource "kubernetes_role_binding" "runtime_verifier" {
  metadata {
    name      = "adp-runtime-verifier"
    namespace = "adp-gateway"
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "adp-orchestration"
      "app.kubernetes.io/component"  = "runtime-verifier"
    }
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role.runtime_verifier.metadata[0].name
  }

  subject {
    kind      = "Group"
    name      = "adp:runtime-verifier-${var.environment}"
    api_group = "rbac.authorization.k8s.io"
  }
}
