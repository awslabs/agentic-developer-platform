variable "enable_tools_handoff_deployment" {
  type    = bool
  default = false
}

variable "tools_handoff_adapter_namespace" {
  type    = string
  default = "adp-tools-adapters"
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,62}$", var.tools_handoff_adapter_namespace))
    error_message = "Select the prepared adapter namespace."
  }
}

resource "kubernetes_role_v1" "tools_handoff" {
  for_each = var.enable_tools_handoff_deployment ? {
    gateway = { namespace = "adp-gateway", deployments = ["bedrockgateway", "tools-facade"] }
    adapter = { namespace = var.tools_handoff_adapter_namespace, deployments = ["tool-adapters"] }
  } : {}
  metadata {
    name      = "adp-tools-handoff"
    namespace = each.value.namespace
  }
  rule {
    api_groups     = ["apps"]
    resources      = ["deployments"]
    resource_names = each.value.deployments
    verbs          = ["get", "patch", "watch"]
  }
  dynamic "rule" {
    for_each = each.key == "gateway" ? [true] : []
    content {
      api_groups     = [""]
      resources      = ["configmaps"]
      resource_names = ["adp-tool-adapter-runtime"]
      verbs          = ["get", "patch"]
    }
  }
  dynamic "rule" {
    for_each = each.key == "adapter" ? [true] : []
    content {
      api_groups     = [""]
      resources      = ["configmaps"]
      resource_names = ["tool-adapters-contract"]
      verbs          = ["get", "patch"]
    }
  }
  dynamic "rule" {
    for_each = each.key == "adapter" ? [true] : []
    content {
      api_groups     = ["networking.k8s.io"]
      resources      = ["ingresses"]
      resource_names = ["tool-adapters-private"]
      verbs          = ["get"]
    }
  }
}

# Terraform owns this object's lifecycle; the fenced connector handoff owns its
# data. Ordinary platform and application deployments preserve the active release.
resource "kubernetes_config_map_v1" "tools_adapter_runtime" {
  count = var.enable_tools_handoff_deployment ? 1 : 0
  metadata {
    name      = "adp-tool-adapter-runtime"
    namespace = "adp-gateway"
  }
  data = {}
  lifecycle {
    ignore_changes = [data]
  }
  depends_on = [null_resource.tools_release_namespace]
}

resource "kubernetes_role_binding_v1" "tools_handoff" {
  for_each = kubernetes_role_v1.tools_handoff
  metadata {
    name      = "adp-tools-handoff"
    namespace = each.value.metadata[0].namespace
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = each.value.metadata[0].name
  }
  subject {
    kind      = "Group"
    name      = "adp:tools-handoff-${var.environment}"
    api_group = "rbac.authorization.k8s.io"
  }
}
