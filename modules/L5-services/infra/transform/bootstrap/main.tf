variable "executor" {
  type = object({
    cluster_name       = string
    node_name          = string
    node_uid           = string
    provider_namespace = string
    check_namespace    = string
    provider_image     = string
    check_image        = string
    provider_cidrs     = list(string)
  })
  default  = null
  nullable = true
  validation {
    condition = var.executor == null ? true : (
      var.executor.provider_namespace != var.executor.check_namespace &&
      alltrue([for image in [var.executor.provider_image, var.executor.check_image] : can(regex("@sha256:[a-f0-9]{64}$", image))]) &&
      length(var.executor.provider_cidrs) > 0 &&
      alltrue([for cidr in var.executor.provider_cidrs : can(cidrhost(cidr, 0)) && !contains(["0.0.0.0/0", "::/0", "169.254.0.0/16", "169.254.169.254/32"], cidr)])
    )
    error_message = "Pin separate operator-prepared namespaces, immutable images and narrowly scoped provider endpoint CIDRs."
  }
}

locals {
  executor_enabled = var.executor == null ? {} : { selected = var.executor }
  cluster_name     = var.executor != null ? var.executor.cluster_name : try(var.isolated_node_pool.cluster_name, null)
  cluster_enabled  = local.cluster_name == null ? {} : { selected = local.cluster_name }
  namespaces = var.executor == null ? {} : {
    provider = var.executor.provider_namespace
    checks   = var.executor.check_namespace
  }
  executor_group = "adp-${var.environment}-l5-transform-control"
  executor_environment = var.executor == null ? {} : merge({
    L5_TRANSFORM_CLUSTER_NAME       = var.executor.cluster_name
    L5_TRANSFORM_CLUSTER_ENDPOINT   = data.aws_eks_cluster.executor["selected"].endpoint
    L5_TRANSFORM_CLUSTER_CA         = data.aws_eks_cluster.executor["selected"].certificate_authority[0].data
    L5_TRANSFORM_NODE_NAME          = var.executor.node_name
    L5_TRANSFORM_NODE_UID           = var.executor.node_uid
    L5_TRANSFORM_PROVIDER_NAMESPACE = var.executor.provider_namespace
    L5_TRANSFORM_CHECK_NAMESPACE    = var.executor.check_namespace
    L5_TRANSFORM_PROVIDER_IMAGE     = var.executor.provider_image
    L5_TRANSFORM_CHECK_IMAGE        = var.executor.check_image
    L5_TRANSFORM_ARTIFACT_BUCKET    = aws_s3_bucket.executor["selected"].id
  }, var.provider_endpoint == null ? {} : { L5_TRANSFORM_PROVIDER_ENDPOINT_REF = jsonencode(local.provider_endpoint_ref) })
}

data "aws_eks_cluster" "executor" {
  for_each = local.cluster_enabled
  name     = each.value
}

data "aws_eks_cluster_auth" "executor" {
  for_each = local.cluster_enabled
  name     = each.value
}

provider "kubernetes" {
  host                   = local.cluster_name == null ? null : data.aws_eks_cluster.executor["selected"].endpoint
  cluster_ca_certificate = local.cluster_name == null ? null : base64decode(data.aws_eks_cluster.executor["selected"].certificate_authority[0].data)
  token                  = local.cluster_name == null ? null : data.aws_eks_cluster_auth.executor["selected"].token
}

resource "kubernetes_namespace_v1" "executor" {
  for_each = local.namespaces
  metadata {
    name = each.value
    labels = {
      "pod-security.kubernetes.io/enforce"         = "restricted"
      "pod-security.kubernetes.io/enforce-version" = "latest"
    }
  }
}

resource "aws_ecr_repository" "executor" {
  name                 = "adp-${var.environment}-l5-transform-executor"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "kubernetes_service_account_v1" "sandbox" {
  for_each = local.namespaces
  metadata {
    name      = "l5-transform-sandbox"
    namespace = kubernetes_namespace_v1.executor[each.key].metadata[0].name
  }
  automount_service_account_token = false
}

resource "aws_eks_access_entry" "worker" {
  for_each          = local.executor_enabled
  cluster_name      = each.value.cluster_name
  principal_arn     = var.worker_role_arn
  kubernetes_groups = [local.executor_group]
  type              = "STANDARD"
}

resource "kubernetes_role_v1" "worker" {
  for_each = local.namespaces
  metadata {
    name      = "l5-transform-control"
    namespace = kubernetes_namespace_v1.executor[each.key].metadata[0].name
  }
  rule {
    api_groups = [""]
    resources  = ["pods", "configmaps"]
    verbs      = ["get", "create", "delete", "patch"]
  }
  dynamic "rule" {
    for_each = each.key == "provider" ? [1] : []
    content {
      api_groups = [""]
      resources  = ["secrets"]
      verbs      = ["get", "create", "delete"]
    }
  }
  rule {
    api_groups = [""]
    resources  = ["pods/log"]
    verbs      = ["get"]
  }
  rule {
    api_groups     = ["networking.k8s.io"]
    resources      = ["networkpolicies"]
    resource_names = ["deny-all"]
    verbs          = ["get"]
  }
}

resource "kubernetes_role_binding_v1" "worker" {
  for_each = local.namespaces
  metadata {
    name      = "l5-transform-control"
    namespace = kubernetes_namespace_v1.executor[each.key].metadata[0].name
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.worker[each.key].metadata[0].name
  }
  subject {
    kind      = "Group"
    name      = local.executor_group
    api_group = "rbac.authorization.k8s.io"
  }
}

resource "kubernetes_network_policy_v1" "deny" {
  for_each = local.namespaces
  metadata {
    name      = "deny-all"
    namespace = kubernetes_namespace_v1.executor[each.key].metadata[0].name
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress", "Egress"]
  }
}

resource "kubernetes_network_policy_v1" "provider" {
  for_each = local.executor_enabled
  metadata {
    name      = "provider-endpoints"
    namespace = kubernetes_namespace_v1.executor["provider"].metadata[0].name
  }
  spec {
    pod_selector {}
    policy_types = ["Egress"]
    egress {
      ports {
        port     = "443"
        protocol = "TCP"
      }
      dynamic "to" {
        for_each = each.value.provider_cidrs
        content {
          ip_block { cidr = to.value }
        }
      }
    }
    egress {
      ports {
        port     = "53"
        protocol = "UDP"
      }
      to {
        namespace_selector { match_labels = { "kubernetes.io/metadata.name" = "kube-system" } }
        pod_selector { match_labels = { "k8s-app" = "kube-dns" } }
      }
    }
  }
}

resource "kubernetes_resource_quota_v1" "executor" {
  for_each = local.namespaces
  metadata {
    name      = "l5-transform-bound"
    namespace = kubernetes_namespace_v1.executor[each.key].metadata[0].name
  }
  spec {
    hard = { pods = "5", configmaps = "1024", secrets = each.key == "provider" ? "5" : "0",
    "requests.cpu" = "3", "requests.memory" = "5Gi", "limits.cpu" = "10", "limits.memory" = "20Gi" }
  }
}

resource "aws_s3_bucket" "executor" {
  for_each = local.executor_enabled
  bucket   = "adp-${var.environment}-l5-transform-${var.aws_account_id}"
}

resource "aws_s3_bucket_versioning" "executor" {
  for_each = local.executor_enabled
  bucket   = aws_s3_bucket.executor[each.key].id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_public_access_block" "executor" {
  for_each                = local.executor_enabled
  bucket                  = aws_s3_bucket.executor[each.key].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "executor" {
  for_each = local.executor_enabled
  bucket   = aws_s3_bucket.executor[each.key].id
  rule {
    id     = "retained-transform-evidence"
    status = "Enabled"
    filter { prefix = "l5/transform/artifacts/" }
    expiration { days = 8 }
    noncurrent_version_expiration { noncurrent_days = 8 }
  }
}

resource "aws_iam_role_policy" "executor_artifacts" {
  for_each = local.executor_enabled
  name     = "transform-artifacts"
  role     = var.worker_role_name
  policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow",
    Action = ["s3:PutObject", "s3:GetObjectVersion"], Resource = "${aws_s3_bucket.executor[each.key].arn}/l5/transform/artifacts/*" },
    { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.executor[each.key].arn}/l5/transform/artifacts/*/checkpoints/*" },
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.executor[each.key].arn,
  Condition = { StringLike = { "s3:prefix" = "l5/transform/artifacts/*/checkpoints/*" } } }] })
}

resource "aws_iam_role_policy" "gateway_artifact_reads" {
  for_each = local.executor_enabled
  name     = "transform-result-reads"
  role     = var.gateway_role_name
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = ["s3:GetObjectVersion"], Resource = [
      "${aws_s3_bucket.executor[each.key].arn}/l5/transform/qualification/catalog.json",
      "${aws_s3_bucket.executor[each.key].arn}/l5/transform/artifacts/*"
    ]
  }] })
  lifecycle {
    precondition {
      condition     = var.gateway_role_name != null
      error_message = "Executor bootstrap requires the selected Gateway role for versioned qualification and result reads."
    }
  }
}

output "executor_repository_url" { value = aws_ecr_repository.executor.repository_url }
output "executor_worker_group" { value = local.executor_group }
output "executor_artifact_bucket" { value = try(aws_s3_bucket.executor["selected"].id, null) }
output "worker_environment" { value = local.executor_environment }
output "worker_factory" { value = "l5_services.runtime_transform:factory" }

resource "kubernetes_cluster_role_v1" "node_observer" {
  for_each = local.executor_enabled
  metadata { name = local.executor_group }
  rule {
    api_groups     = [""]
    resources      = ["nodes"]
    resource_names = [each.value.node_name]
    verbs          = ["get"]
  }
}

resource "kubernetes_cluster_role_binding_v1" "node_observer" {
  for_each = local.executor_enabled
  metadata { name = local.executor_group }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role_v1.node_observer[each.key].metadata[0].name
  }
  subject {
    kind      = "Group"
    name      = local.executor_group
    api_group = "rbac.authorization.k8s.io"
  }
}
