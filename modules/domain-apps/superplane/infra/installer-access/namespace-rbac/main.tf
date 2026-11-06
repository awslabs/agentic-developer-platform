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
variable "lifecycle_storage" {
  description = "Optional app-owned retained EBS class; select the actual owned Bound PVC to grant its exact PV read."
  type = object({
    provisioner = string
    claim = optional(object({
      namespace = string
      name      = string
      uid       = string
    }))
  })
  default = null
  validation {
    condition = var.lifecycle_storage == null ? true : contains([
      "ebs.csi.aws.com", "ebs.csi.eks.amazonaws.com"
    ], var.lifecycle_storage.provisioner)
    error_message = "Select the verified standard or Auto Mode EBS CSI driver."
  }
  validation {
    condition = var.lifecycle_storage == null ? true : (
      var.lifecycle_storage.claim == null ? true : (
        contains(var.namespaces, var.lifecycle_storage.claim.namespace) &&
        can(regex("^[a-z0-9][a-z0-9.-]{0,252}$", var.lifecycle_storage.claim.name)) &&
        can(regex("^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$", var.lifecycle_storage.claim.uid))
      )
    )
    error_message = "Select an actual PVC UID in an installation namespace."
  }
}
provider "kubernetes" { config_path = var.kubeconfig_path }
locals {
  name          = "superplane-installer-${var.installation_id}"
  group         = "adp:superplane:installer:${var.installation_id}"
  allowed_names = sort(tolist(setunion(var.namespaces, var.preflight_namespaces)))
  storage_name  = "superplane-lifecycle-${var.installation_id}"
  storage_claim = var.lifecycle_storage == null ? null : var.lifecycle_storage.claim
}

resource "kubernetes_storage_class_v1" "lifecycle" {
  count = var.lifecycle_storage == null ? 0 : 1
  metadata {
    name   = local.storage_name
    labels = { "adp.aws-e.io/installation" = var.installation_id }
  }
  storage_provisioner    = var.lifecycle_storage.provisioner
  reclaim_policy         = "Retain"
  volume_binding_mode    = "WaitForFirstConsumer"
  allow_volume_expansion = true
  parameters             = { type = "gp3", encrypted = "true" }
  dynamic "allowed_topologies" {
    for_each = var.lifecycle_storage.provisioner == "ebs.csi.eks.amazonaws.com" ? [1] : []
    content {
      match_label_expressions {
        key    = "eks.amazonaws.com/compute-type"
        values = ["auto"]
      }
    }
  }
  lifecycle { prevent_destroy = true }
}

# The installer can already read its namespace's PVC. Resolve the actual bound
# volume there; do not grant cluster-wide PV discovery or guess a volume name.
data "kubernetes_resource" "lifecycle_claim" {
  count       = local.storage_claim == null ? 0 : 1
  api_version = "v1"
  kind        = "PersistentVolumeClaim"
  metadata {
    namespace = local.storage_claim.namespace
    name      = local.storage_claim.name
  }
  lifecycle {
    postcondition {
      condition = (
        try(self.object.metadata.uid == local.storage_claim.uid &&
          self.object.metadata.namespace == local.storage_claim.namespace &&
          self.object.metadata.name == local.storage_claim.name &&
          self.object.metadata.labels["adp.aws-e.io/installation"] == var.installation_id &&
          self.object.spec.storageClassName == local.storage_name &&
          self.object.status.phase == "Bound" &&
        can(regex("^[a-z0-9][a-z0-9.-]{0,252}$", self.object.spec.volumeName)), false)
      )
      error_message = "PV access requires the selected owned PVC UID, retained class and actual bound volume name."
    }
  }
}

resource "kubernetes_cluster_role_v1" "namespaces" {
  metadata { name = local.name }
  dynamic "rule" {
    for_each = kubernetes_storage_class_v1.lifecycle
    content {
      api_groups     = ["storage.k8s.io"]
      resources      = ["storageclasses"]
      resource_names = [rule.value.metadata[0].name]
      verbs          = ["get"]
    }
  }
  dynamic "rule" {
    for_each = data.kubernetes_resource.lifecycle_claim
    content {
      api_groups     = [""]
      resources      = ["persistentvolumes"]
      resource_names = [rule.value.object.spec.volumeName]
      verbs          = ["get"]
    }
  }
  # EKS ViewPolicy does not cover Auto Mode custom resources. The installer
  # reads this exact object to verify configured NetworkPolicy enforcement.
  rule {
    api_groups     = ["eks.amazonaws.com"]
    resources      = ["nodeclasses"]
    resource_names = ["default"]
    verbs          = ["get"]
  }
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

output "lifecycle_storage_class_name" {
  value = var.lifecycle_storage == null ? null : local.storage_name
}
output "lifecycle_persistent_volume_name" {
  value = local.storage_claim == null ? null : data.kubernetes_resource.lifecycle_claim[0].object.spec.volumeName
}
