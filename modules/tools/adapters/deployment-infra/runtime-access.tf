# This group is created by platform/automation-infra/tools-deployment.tf for
# the protected tools release role. It is separate from the read-only verifier.
# Installing this binding does not create an EKS access entry or an AWS grant.
resource "kubernetes_role_v1" "runtime_deployer" {
  metadata {
    name      = "adp-tool-adapters-release"
    namespace = module.foundation.namespace
    labels = {
      "app.kubernetes.io/name"    = "tool-adapters"
      "app.kubernetes.io/part-of" = "adp"
    }
  }

  # Server-side apply addresses each named resource with PATCH. When it creates
  # a missing object, the API server also authorizes CREATE with that same name.
  # These permissions intentionally do not support unnamed POST creation.
  rule {
    api_groups     = [""]
    resources      = ["serviceaccounts", "services"]
    resource_names = ["tool-adapters"]
    verbs          = ["get", "create", "patch"]
  }
  rule {
    api_groups     = [""]
    resources      = ["configmaps"]
    resource_names = ["tool-adapters-contract"]
    verbs          = ["get", "create", "patch"]
  }
  rule {
    api_groups     = ["apps"]
    resources      = ["deployments"]
    resource_names = ["tool-adapters"]
    verbs          = ["get", "create", "patch", "watch"]
  }
  rule {
    api_groups     = ["policy"]
    resources      = ["poddisruptionbudgets"]
    resource_names = ["tool-adapters"]
    verbs          = ["get", "create", "patch"]
  }
  # The canonical observer discovers controller-owned objects by labels and
  # independently rereads them. List permission remains namespace-confined.
  rule {
    api_groups = [""]
    resources  = ["pods"]
    verbs      = ["get", "list"]
  }
  rule {
    api_groups = ["apps"]
    resources  = ["replicasets"]
    verbs      = ["get", "list"]
  }
  rule {
    api_groups = ["discovery.k8s.io"]
    resources  = ["endpointslices"]
    verbs      = ["get", "list"]
  }
  rule {
    api_groups     = [""]
    resources      = ["services/proxy"]
    resource_names = ["http:tool-adapters:80"]
    verbs          = ["get"]
  }
}

resource "kubernetes_role_binding_v1" "runtime_deployer" {
  metadata {
    name      = "adp-tool-adapters-release"
    namespace = module.foundation.namespace
    labels = {
      "app.kubernetes.io/name"    = "tool-adapters"
      "app.kubernetes.io/part-of" = "adp"
    }
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.runtime_deployer.metadata[0].name
  }
  subject {
    kind      = "Group"
    name      = "adp:tools-release-${var.environment}"
    api_group = "rbac.authorization.k8s.io"
  }
}
