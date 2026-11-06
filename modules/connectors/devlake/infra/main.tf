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
variable "api_image" {
  type    = string
  default = ""
  validation {
    condition     = var.api_image == "" || can(regex("^[^[:space:]]+@sha256:[a-f0-9]{64}$", var.api_image))
    error_message = "Supply an immutable API image digest."
  }
}
variable "deployment_role_boundaries" {
  type    = map(string)
  default = {}
  validation {
    condition     = alltrue([for role, boundary in var.deployment_role_boundaries : can(regex("^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9_/-]+$", role)) && can(regex("^arn:aws:iam::[0-9]{12}:policy/[A-Za-z0-9_/-]+$", boundary))])
    error_message = "Supply exact role and policy ARNs without wildcards."
  }
}
variable "provider_egress_cidrs" {
  type    = list(string)
  default = []
  validation {
    condition     = alltrue([for cidr in var.provider_egress_cidrs : can(cidrhost(cidr, 0)) && cidr != "0.0.0.0/0" && cidr != "::/0"])
    error_message = "Provide bounded provider egress CIDRs."
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
  name      = "adp-${var.environment}-devlake-connectors"
  namespace = "adp-devlake-connectors"
  platform  = try(data.terraform_remote_state.platform[0].outputs, {})
}
data "aws_vpc_endpoint" "sts" {
  count        = var.enabled ? 1 : 0
  vpc_id       = local.platform.vpc_id
  service_name = "com.amazonaws.${var.aws_region}.sts"
  lifecycle {
    postcondition {
      condition     = self.vpc_endpoint_type == "Interface" && self.private_dns_enabled && self.state == "available" && length(self.network_interface_ids) > 0
      error_message = "A private-DNS STS interface endpoint is required for workload identity."
    }
  }
}
data "aws_network_interface" "sts" {
  for_each = var.enabled ? toset(data.aws_vpc_endpoint.sts[0].network_interface_ids) : toset([])
  id       = each.value
}
resource "aws_iam_role" "api" {
  count                = var.enabled ? 1 : 0
  name                 = "${local.name}-api"
  permissions_boundary = lookup(var.deployment_role_boundaries, "arn:aws:iam::${var.aws_account_id}:role/${local.name}-api", null)
  lifecycle {
    precondition {
      condition     = var.api_image != "" && toset(keys(var.deployment_role_boundaries)) == toset(["arn:aws:iam::${var.aws_account_id}:role/${local.name}-api"]) && can(regex("^arn:aws:iam::${var.aws_account_id}:policy/", lookup(var.deployment_role_boundaries, "arn:aws:iam::${var.aws_account_id}:role/${local.name}-api", "")))
      error_message = "Enable only with a pinned API image and the exact admitted role boundary."
    }
  }
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = local.platform.eks_oidc_provider_arn }
      Condition = { StringEquals = {
        "${local.platform.eks_oidc_issuer}:sub" = "system:serviceaccount:${local.namespace}:connector-api"
        "${local.platform.eks_oidc_issuer}:aud" = "sts.amazonaws.com"
      } }
    }]
  })
}
resource "kubernetes_namespace_v1" "api" {
  count = var.enabled ? 1 : 0
  metadata {
    name = local.namespace
    labels = {
      "pod-security.kubernetes.io/enforce" = "restricted"
    }
  }
}
resource "kubernetes_service_account_v1" "api" {
  count = var.enabled ? 1 : 0
  metadata {
    name      = "connector-api"
    namespace = kubernetes_namespace_v1.api[0].metadata[0].name
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.api[0].arn
    }
  }
}
resource "kubernetes_network_policy_v1" "deny" {
  count = var.enabled ? 1 : 0
  metadata {
    name      = "deny-all"
    namespace = kubernetes_namespace_v1.api[0].metadata[0].name
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress", "Egress"]
  }
}
resource "kubernetes_network_policy_v1" "allow" {
  count = var.enabled ? 1 : 0
  metadata {
    name      = "gateway-broker-and-provider"
    namespace = kubernetes_namespace_v1.api[0].metadata[0].name
  }
  spec {
    pod_selector { match_labels = { app = "connector-api" } }
    policy_types = ["Ingress", "Egress"]
    ingress {
      from {
        namespace_selector { match_labels = { "kubernetes.io/metadata.name" = "adp-gateway" } }
      }
      ports { port = "8080" }
    }
    egress {
      to {
        namespace_selector { match_labels = { "kubernetes.io/metadata.name" = "kube-system" } }
      }
      ports {
        port     = "53"
        protocol = "UDP"
      }
      ports {
        port     = "53"
        protocol = "TCP"
      }
    }
    egress {
      to {
        namespace_selector { match_labels = { "kubernetes.io/metadata.name" = "adp-gateway" } }
      }
      ports { port = "8080" }
    }
    egress {
      dynamic "to" {
        for_each = data.aws_network_interface.sts
        content {
          ip_block { cidr = "${to.value.private_ip}/32" }
        }
      }
      ports { port = "443" }
    }
    dynamic "egress" {
      for_each = var.provider_egress_cidrs
      content {
        to {
          ip_block { cidr = egress.value }
        }
        ports { port = "443" }
      }
    }
  }
  depends_on = [kubernetes_network_policy_v1.deny]
}
resource "kubernetes_deployment_v1" "api" {
  count = var.enabled ? 1 : 0
  metadata {
    name      = "connector-api"
    namespace = kubernetes_namespace_v1.api[0].metadata[0].name
  }
  spec {
    replicas = 1
    selector { match_labels = { app = "connector-api" } }
    template {
      metadata { labels = { app = "connector-api" } }
      spec {
        service_account_name = kubernetes_service_account_v1.api[0].metadata[0].name
        security_context {
          run_as_non_root = true
          seccomp_profile { type = "RuntimeDefault" }
        }
        container {
          name              = "api"
          image             = var.api_image
          image_pull_policy = "Always"
          env {
            name  = "CONNECTOR_API_LISTEN"
            value = "0.0.0.0:8080"
          }
          env {
            name  = "FEATURE_DEVLAKE_CONNECTORS_ENABLED"
            value = var.enabled ? "true" : "false"
          }
          port { container_port = 8080 }
          liveness_probe {
            http_get {
              path = "/healthz"
              port = 8080
            }
          }
          resources {
            requests = { cpu = "100m", memory = "128Mi" }
            limits   = { cpu = "500m", memory = "512Mi" }
          }
          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            run_as_user                = 10001
            capabilities { drop = ["ALL"] }
          }
        }
      }
    }
  }
  depends_on = [kubernetes_network_policy_v1.allow]
}
resource "kubernetes_service_v1" "api" {
  count = var.enabled ? 1 : 0
  metadata {
    name      = "connector-api"
    namespace = kubernetes_namespace_v1.api[0].metadata[0].name
  }
  spec {
    type     = "ClusterIP"
    selector = { app = "connector-api" }
    port {
      port        = 8080
      target_port = 8080
    }
  }
}
output "api_service" {
  value = var.enabled ? "connector-api.${local.namespace}.svc.cluster.local:8080" : null
}
output "api_role_arn" {
  value = var.enabled ? aws_iam_role.api[0].arn : null
}
