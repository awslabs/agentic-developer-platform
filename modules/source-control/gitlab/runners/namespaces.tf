resource "kubernetes_namespace_v1" "manager" {
  for_each = local.group_bindings
  metadata {
    name = each.value.manager_namespace
    labels = {
      "pod-security.kubernetes.io/enforce" = "restricted"
      "pod-security.kubernetes.io/warn"    = "restricted"
      "adp-group-id"                       = each.key
    }
  }
}

resource "kubernetes_service_account_v1" "manager" {
  for_each = local.group_bindings
  metadata {
    name      = "gitlab-runner"
    namespace = kubernetes_namespace_v1.manager[each.key].metadata[0].name
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.manager[each.key].arn
    }
  }
  automount_service_account_token = true
}

resource "kubernetes_resource_quota_v1" "manager" {
  for_each = local.group_bindings
  metadata {
    name      = "manager-budget"
    namespace = kubernetes_namespace_v1.manager[each.key].metadata[0].name
  }
  spec {
    hard = { pods = "2", "requests.cpu" = "2", "requests.memory" = "4Gi" }
  }
}
