terraform {
  required_version = ">= 1.9"
  backend "s3" {}
  required_providers { kubernetes = { source = "hashicorp/kubernetes", version = "2.38.0" } }
}
variable "kubeconfig_path" { type = string }
variable "installation_id" { type = string }
variable "namespaces" { type = set(string) }
variable "preflight_namespaces" {
  type = set(string)
  validation {
    condition     = length(var.preflight_namespaces) > 0 && alltrue([for n in var.preflight_namespaces : can(regex("^sp-preflight-[a-f0-9]{16}$", n))])
    error_message = "Use actual run IDs from retained installer receipts; never invent a historical receipt."
  }
}
provider "kubernetes" { config_path = var.kubeconfig_path }
locals {
  name          = "superplane-installer-${var.installation_id}"
  group         = "adp:superplane:installer:${var.installation_id}"
  allowed_names = sort(tolist(setunion(var.namespaces, var.preflight_namespaces)))
}

resource "kubernetes_cluster_role_v1" "namespaces" {
  metadata { name = local.name }
  rule {
    api_groups = [""]
    resources  = ["namespaces"]
    verbs      = ["create"]
  }
  rule {
    api_groups     = [""]
    resources      = ["namespaces"]
    resource_names = local.allowed_names
    verbs          = ["get", "list", "watch", "patch", "update"]
  }
  rule {
    api_groups     = [""]
    resources      = ["namespaces"]
    resource_names = sort(tolist(var.preflight_namespaces))
    verbs          = ["delete"]
  }
}

# Kubernetes RBAC cannot constrain CREATE using resourceNames. This admission
# rule applies only to the app-specific group and closes that otherwise broad grant.
resource "kubernetes_manifest" "namespace_create_policy" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata   = { name = local.name }
    spec = {
      failurePolicy    = "Fail"
      matchConstraints = { resourceRules = [{ apiGroups = [""], apiVersions = ["v1"], operations = ["CREATE"], resources = ["namespaces"], scope = "Cluster" }] }
      matchConditions  = [{ name = "selected-installer", expression = "${jsonencode(local.group)} in request.userInfo.groups" }]
      validations = [
        { expression = "object.metadata.name in ${jsonencode(local.allowed_names)}", message = "Installer may create only its reviewed installation or preflight namespaces." },
        { expression = "has(object.metadata.labels) && 'adp.aws-e.io/installation' in object.metadata.labels && object.metadata.labels['adp.aws-e.io/installation'] == ${jsonencode(var.installation_id)}", message = "Installer Namespace must retain its selected installation owner." }
      ]
    }
  }
}
resource "kubernetes_manifest" "namespace_create_binding" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata   = { name = local.name }
    spec       = { policyName = local.name, validationActions = ["Deny"] }
  }
  depends_on = [kubernetes_manifest.namespace_create_policy]
}
resource "kubernetes_cluster_role_binding_v1" "namespaces" {
  metadata { name = local.name }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role_v1.namespaces.metadata[0].name
  }
  subject {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Group"
    name      = local.group
  }
  depends_on = [kubernetes_manifest.namespace_create_binding]
}
