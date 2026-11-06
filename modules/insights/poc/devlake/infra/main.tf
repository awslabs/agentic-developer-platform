terraform {
  required_version = ">= 1.7, < 2.0"
  required_providers {
    aws        = { source = "hashicorp/aws", version = "~> 5.0" }
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 2.23" }
  }
  backend "s3" {}
}

variable "enabled" {
  type     = bool
  default  = false
  nullable = false
}
variable "connectors_enabled" {
  type     = bool
  default  = false
  nullable = false
}
variable "environment" {
  type = string
  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "Only dev and prod are supported."
  }
}
variable "aws_region" { type = string }
variable "aws_account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "Supply the verified AWS account ID."
  }
}
variable "worker_image" {
  type    = string
  default = ""
  validation {
    condition     = var.worker_image == "" || can(regex("^[^[:space:]]+@sha256:[a-f0-9]{64}$", var.worker_image))
    error_message = "Supply a pinned worker image digest."
  }
}
variable "schema_manifest_sha256" {
  type    = string
  default = ""
  validation {
    condition     = var.schema_manifest_sha256 == "" || can(regex("^[a-f0-9]{64}$", var.schema_manifest_sha256))
    error_message = "Supply the pinned schema manifest digest."
  }
}
variable "cell_id" {
  type    = string
  default = ""
  validation {
    condition     = var.cell_id == "" || can(regex("^[a-z][a-z0-9-]{0,30}$", var.cell_id))
    error_message = "Supply the registered cell ID, not an arbitrary database name."
  }
}
variable "existing_db_endpoint" {
  type    = string
  default = ""
  validation {
    condition     = var.existing_db_endpoint == "" || can(regex("^[a-zA-Z0-9.-]+\\.rds\\.amazonaws\\.com$", var.existing_db_endpoint))
    error_message = "Supply the existing verified RDS hostname, without a port or connection string."
  }
}
variable "existing_db_resource_id" {
  type    = string
  default = ""
  validation {
    condition     = var.existing_db_resource_id == "" || can(regex("^(db|cluster)-[A-Za-z0-9]+$", var.existing_db_resource_id))
    error_message = "Supply the existing RDS database or cluster resource ID."
  }
}
variable "ca_configmap_name" {
  type    = string
  default = ""
  validation {
    condition     = var.ca_configmap_name == "" || can(regex("^[a-z][a-z0-9-]{0,61}[a-z0-9]$", var.ca_configmap_name))
    error_message = "Supply the independently verified CA ConfigMap name."
  }
}
variable "worker_role_boundary_arn" {
  type    = string
  default = ""
}
variable "writer_db_username" {
  type    = string
  default = ""
  validation {
    condition     = var.writer_db_username == "" || can(regex("^[A-Za-z0-9+=,.@_-]{1,64}$", var.writer_db_username))
    error_message = "Supply the registered cell writer database username."
  }
}

provider "aws" {
  region              = var.aws_region
  allowed_account_ids = var.enabled ? [var.aws_account_id] : null
}
provider "kubernetes" {
  host                   = try(data.terraform_remote_state.platform[0].outputs.eks_cluster_endpoint, "https://localhost")
  cluster_ca_certificate = try(base64decode(data.terraform_remote_state.platform[0].outputs.eks_cluster_ca_certificate), "")
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", try(data.terraform_remote_state.platform[0].outputs.eks_cluster_name, "unconfigured"), "--region", var.aws_region]
  }
}
data "terraform_remote_state" "platform" {
  count   = var.enabled ? 1 : 0
  backend = "s3"
  config = {
    bucket = "adp-terraform-state-${var.aws_account_id}"
    key    = "${var.environment}/platform/terraform.tfstate"
    region = var.aws_region
  }
}
locals {
  name      = "adp-${var.environment}-insights-devlake-${var.cell_id}"
  namespace = "adp-insights-devlake-${var.cell_id}"
  platform  = try(data.terraform_remote_state.platform[0].outputs, {})
}
resource "aws_iam_role" "worker" {
  count                = var.enabled ? 1 : 0
  name                 = "${local.name}-worker"
  permissions_boundary = var.worker_role_boundary_arn
  lifecycle {
    precondition {
      condition = (var.connectors_enabled && var.cell_id != "" && var.worker_image != "" &&
        var.existing_db_endpoint != "" && var.existing_db_resource_id != "" &&
        var.ca_configmap_name != "" && var.writer_db_username != "" && var.schema_manifest_sha256 == filesha256("${path.module}/../schema-manifest.json") &&
      can(regex("^arn:aws:iam::${var.aws_account_id}:policy/[A-Za-z0-9_/-]+$", var.worker_role_boundary_arn)))
      error_message = "Collection requires connectors, a registered cell, an existing DB/CA, approved boundary and the pinned S09 schema."
    }
  }
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = local.platform.eks_oidc_provider_arn }
      Condition = { StringEquals = {
        "${local.platform.eks_oidc_issuer}:sub" = "system:serviceaccount:${local.namespace}:devlake-${var.cell_id}-worker"
        "${local.platform.eks_oidc_issuer}:aud" = "sts.amazonaws.com"
      } }
    }]
  })
}
resource "aws_iam_role_policy" "worker_database" {
  count = var.enabled ? 1 : 0
  name  = "cell-db-connect"
  role  = aws_iam_role.worker[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["rds-db:connect"]
      Resource = "arn:aws:rds-db:${var.aws_region}:${var.aws_account_id}:dbuser:${var.existing_db_resource_id}/${var.writer_db_username}"
    }]
  })
}
resource "kubernetes_namespace_v1" "collection" {
  count = var.enabled ? 1 : 0
  metadata {
    name = local.namespace
    labels = {
      "pod-security.kubernetes.io/enforce" = "restricted"
    }
  }
}
resource "kubernetes_service_account_v1" "worker" {
  count = var.enabled ? 1 : 0
  metadata {
    name      = "devlake-${var.cell_id}-worker"
    namespace = kubernetes_namespace_v1.collection[0].metadata[0].name
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.worker[0].arn
    }
  }
}
resource "kubernetes_network_policy_v1" "deny" {
  count = var.enabled ? 1 : 0
  metadata {
    name      = "deny-all"
    namespace = kubernetes_namespace_v1.collection[0].metadata[0].name
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress", "Egress"]
  }
}
resource "kubernetes_deployment_v1" "worker" {
  count = var.enabled ? 1 : 0
  metadata {
    name      = "devlake-${var.cell_id}-worker"
    namespace = kubernetes_namespace_v1.collection[0].metadata[0].name
  }
  spec {
    replicas = 0
    selector { match_labels = { app = "devlake-${var.cell_id}-worker" } }
    template {
      metadata { labels = { app = "devlake-${var.cell_id}-worker" } }
      spec {
        service_account_name = kubernetes_service_account_v1.worker[0].metadata[0].name
        security_context {
          run_as_non_root = true
          seccomp_profile { type = "RuntimeDefault" }
        }
        container {
          name              = "worker"
          image             = var.worker_image
          image_pull_policy = "Always"
          args              = ["--verify-package"]
          env {
            name  = "DEVLAKE_CELL_ID"
            value = var.cell_id
          }
          env {
            name  = "DEVLAKE_DB_HOST"
            value = var.existing_db_endpoint
          }
          env {
            name  = "DEVLAKE_DB_RESOURCE_ID"
            value = var.existing_db_resource_id
          }
          env {
            name  = "DEVLAKE_DB_AUTH"
            value = "iam"
          }
          env {
            name  = "DEVLAKE_DB_USERNAME"
            value = var.writer_db_username
          }
          env {
            name  = "DEVLAKE_DB_SSLMODE"
            value = "verify-full"
          }
          env {
            name  = "DEVLAKE_DB_CA_PATH"
            value = "/etc/devlake-ca/ca.pem"
          }
          env {
            name  = "DEVLAKE_SCHEMA_MANIFEST_SHA256"
            value = var.schema_manifest_sha256
          }
          env {
            name  = "FEATURE_DEVLAKE_CONNECTORS_ENABLED"
            value = var.connectors_enabled ? "true" : "false"
          }
          env {
            name  = "DEVLAKE_COLLECTION_ENABLED"
            value = var.enabled ? "true" : "false"
          }
          volume_mount {
            name       = "rds-ca"
            mount_path = "/etc/devlake-ca"
            read_only  = true
          }
          resources {
            requests = { cpu = "100m", memory = "128Mi" }
            limits   = { cpu = "500m", memory = "512Mi" }
          }
          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            run_as_user                = 65532
            capabilities { drop = ["ALL"] }
          }
        }
        volume {
          name = "rds-ca"
          config_map {
            name = var.ca_configmap_name
            items {
              key  = "ca.pem"
              path = "ca.pem"
            }
          }
        }
      }
    }
  }
  depends_on = [kubernetes_network_policy_v1.deny]
}
output "cell_worker_role_arn" {
  value = var.enabled ? aws_iam_role.worker[0].arn : null
}
