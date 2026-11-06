variable "shared_tools_browser_namespace" {
  description = "Existing approved Browser namespace; empty leaves shared release access disabled."
  type        = string
  default     = ""
  validation {
    condition     = var.shared_tools_browser_namespace == "" || can(regex("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", var.shared_tools_browser_namespace))
    error_message = "Select the existing approved Browser namespace."
  }
}

variable "shared_tools_browser_resource_name" {
  description = "Exact reviewed provider name prefix whose Browser resources are pre-created by the state owner."
  type        = string
  default     = ""
  validation {
    condition     = var.shared_tools_browser_resource_name == "" || can(regex("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", var.shared_tools_browser_resource_name))
    error_message = "Select the reviewed Browser resource name."
  }
}

resource "kubernetes_role_v1" "shared_tools_release" {
  count = var.shared_tools_browser_namespace != "" && var.shared_tools_browser_resource_name != "" ? 1 : 0
  metadata {
    name      = "adp-shared-tools-release"
    namespace = var.shared_tools_browser_namespace
  }
  rule {
    api_groups     = [""]
    resources      = ["serviceaccounts"]
    resource_names = ["${var.shared_tools_browser_resource_name}-browser"]
    verbs          = ["get", "update", "patch"]
  }
  rule {
    api_groups     = ["apps"]
    resources      = ["deployments"]
    resource_names = ["${var.shared_tools_browser_resource_name}-browser"]
    verbs          = ["get", "update", "patch", "watch"]
  }
  rule {
    api_groups = [""]
    resources  = ["pods"]
    verbs      = ["get", "list"]
  }
  # The receipt reads the pod's exact controller to verify Deployment ownership.
  rule {
    api_groups = ["apps"]
    resources  = ["replicasets"]
    verbs      = ["get"]
  }
  rule {
    api_groups     = ["networking.k8s.io"]
    resources      = ["networkpolicies"]
    resource_names = ["${var.shared_tools_browser_resource_name}-browser"]
    verbs          = ["get", "update", "patch"]
  }
  depends_on = [module.eks.access_entry_ready]
}

resource "kubernetes_role_binding_v1" "shared_tools_release" {
  count = var.shared_tools_browser_namespace != "" && var.shared_tools_browser_resource_name != "" ? 1 : 0
  metadata {
    name      = "adp-shared-tools-release"
    namespace = var.shared_tools_browser_namespace
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.shared_tools_release[0].metadata[0].name
  }
  subject {
    kind      = "Group"
    name      = "adp:shared-tools-release-${var.environment}"
    api_group = "rbac.authorization.k8s.io"
  }
}

# The tools evaluation identity may observe Browser without inheriting the
# shared release group's patch/update or service-account authority.
resource "kubernetes_role_v1" "tools_browser_observer" {
  count = var.environment == "dev" && var.shared_tools_browser_namespace != "" && var.shared_tools_browser_resource_name != "" ? 1 : 0
  metadata {
    name      = "adp-tools-browser-observer"
    namespace = var.shared_tools_browser_namespace
  }
  rule {
    api_groups     = ["apps"]
    resources      = ["deployments"]
    resource_names = ["${var.shared_tools_browser_resource_name}-browser"]
    verbs          = ["get"]
  }
  rule {
    api_groups = ["apps"]
    resources  = ["replicasets"]
    verbs      = ["get"]
  }
  rule {
    api_groups = [""]
    resources  = ["pods"]
    verbs      = ["list"]
  }
  depends_on = [module.eks.access_entry_ready]
}

resource "kubernetes_role_binding_v1" "tools_browser_observer" {
  count = var.environment == "dev" && var.shared_tools_browser_namespace != "" && var.shared_tools_browser_resource_name != "" ? 1 : 0
  metadata {
    name      = "adp-tools-browser-observer"
    namespace = var.shared_tools_browser_namespace
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.tools_browser_observer[0].metadata[0].name
  }
  subject {
    kind      = "Group"
    name      = "adp:tools-release-${var.environment}"
    api_group = "rbac.authorization.k8s.io"
  }
}
