resource "kubernetes_namespace_v1" "insights" {
  metadata {
    name = "adp-insights"
    labels = {
      "pod-security.kubernetes.io/enforce" = "restricted"
      "pod-security.kubernetes.io/warn"    = "restricted"
    }
  }
}

resource "kubernetes_resource_quota_v1" "insights" {
  metadata {
    name      = "insights-budget"
    namespace = kubernetes_namespace_v1.insights.metadata[0].name
  }
  spec {
    hard = {
      pods              = "4"
      "requests.cpu"    = "2"
      "requests.memory" = "4Gi"
      "limits.cpu"      = "4"
      "limits.memory"   = "8Gi"
    }
  }
}

resource "kubernetes_service_account_v1" "service" {
  for_each = aws_iam_role.service
  metadata {
    name      = "insights-${each.key}"
    namespace = kubernetes_namespace_v1.insights.metadata[0].name
    annotations = {
      "eks.amazonaws.com/role-arn"               = each.value.arn
      "eks.amazonaws.com/sts-regional-endpoints" = "true"
    }
  }
  automount_service_account_token = true
}

resource "kubernetes_deployment_v1" "service" {
  for_each = local.images
  metadata {
    name      = "insights-${each.key}"
    namespace = kubernetes_namespace_v1.insights.metadata[0].name
  }
  spec {
    replicas = 0
    selector {
      match_labels = { app = "insights-${each.key}" }
    }
    template {
      metadata {
        labels = { app = "insights-${each.key}" }
      }
      spec {
        service_account_name = kubernetes_service_account_v1.service[each.key].metadata[0].name
        security_context {
          run_as_non_root = true
          seccomp_profile { type = "RuntimeDefault" }
        }
        container {
          name              = each.key
          image             = each.value
          image_pull_policy = "Always"
          port { container_port = 8080 }
          dynamic "env" {
            for_each = each.key == "emitter" ? {
              AWS_REGION                    = var.aws_region
              L5_INSIGHTS_EVIDENCE_BUCKET   = aws_s3_bucket.evidence.id
              L5_INSIGHTS_EVIDENCE_KMS_KEY  = aws_kms_key.evidence.arn
              L5_CONSUMER_VERIFICATION_KEYS = jsonencode(var.l5_consumer_verification_keys)
              L5_AUTHORITY_GATEWAY_URL      = var.l5_authority_gateway_url
            } : {}
            content {
              name  = env.key
              value = env.value
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
  lifecycle {
    ignore_changes = [spec[0].replicas]
  }
  depends_on = [kubernetes_network_policy_v1.deny, kubernetes_network_policy_v1.allowed]
}

resource "kubernetes_service_v1" "l5_emitter" {
  count = contains(keys(local.images), "emitter") ? 1 : 0
  metadata {
    name      = "insights-emitter"
    namespace = kubernetes_namespace_v1.insights.metadata[0].name
  }
  spec {
    type     = "ClusterIP"
    selector = { app = "insights-emitter" }
    port {
      port        = 8080
      target_port = 8080
    }
  }
}

resource "kubernetes_network_policy_v1" "l5_emitter_ingress" {
  metadata {
    name      = "l5-emitter-gateway-only"
    namespace = kubernetes_namespace_v1.insights.metadata[0].name
  }
  spec {
    pod_selector { match_labels = { app = "insights-emitter" } }
    policy_types = ["Ingress"]
    ingress {
      ports {
        port     = "8080"
        protocol = "TCP"
      }
      from {
        namespace_selector { match_labels = { "kubernetes.io/metadata.name" = "adp-gateway" } }
        pod_selector { match_labels = { app = "bedrockgateway" } }
      }
    }
  }
}

resource "kubernetes_network_policy_v1" "deny" {
  metadata {
    name      = "deny-all"
    namespace = kubernetes_namespace_v1.insights.metadata[0].name
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress", "Egress"]
  }
}

resource "kubernetes_network_policy_v1" "allowed" {
  metadata {
    name      = "gateway-s3-github-egress"
    namespace = kubernetes_namespace_v1.insights.metadata[0].name
  }
  spec {
    pod_selector {}
    policy_types = ["Egress"]
    egress {
      ports {
        port     = "53"
        protocol = "UDP"
      }
      ports {
        port     = "53"
        protocol = "TCP"
      }
      to {
        namespace_selector { match_labels = { "kubernetes.io/metadata.name" = "kube-system" } }
      }
    }
    egress {
      ports {
        port     = "8080"
        protocol = "TCP"
      }
      to {
        namespace_selector { match_labels = { "kubernetes.io/metadata.name" = "adp-gateway" } }
        pod_selector { match_labels = { app = "bedrockgateway" } }
      }
    }
    egress {
      ports {
        port     = "443"
        protocol = "TCP"
      }
      dynamic "to" {
        for_each = data.aws_network_interface.sts
        content {
          ip_block { cidr = "${to.value.private_ip}/32" }
        }
      }
    }
    egress {
      ports {
        port     = "443"
        protocol = "TCP"
      }
      dynamic "to" {
        for_each = toset(concat(data.aws_prefix_list.s3.cidr_blocks, var.github_api_cidrs))
        content {
          ip_block { cidr = to.value }
        }
      }
    }
  }
}
