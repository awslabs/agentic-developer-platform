variable "private_edge_enabled" {
  type        = bool
  default     = false
  description = "Create the adapter's private ALB ingress after its Service exists. VPC-link connectivity and connector activation remain separate release steps."
}

locals {
  adapter_private_paths = [for connector in ["atlassian", "github", "jira", "slack"] :
    "/internal/v1/tools/${connector}/mcp"
  ]
}

# The existing EKS Auto Mode alb class discovers private subnets from the
# platform's internal-elb tags. A distinct Ingress has its own ALB and never
# changes the Gateway ingress or its CloudFront origin.
resource "kubernetes_ingress_v1" "adapter_private" {
  count                  = var.private_edge_enabled ? 1 : 0
  wait_for_load_balancer = true

  metadata {
    name      = "tool-adapters-private"
    namespace = kubernetes_namespace_v1.adapter.metadata[0].name
    labels    = local.labels
    annotations = {
      "alb.ingress.kubernetes.io/scheme"                   = "internal"
      "alb.ingress.kubernetes.io/target-type"              = "ip"
      "alb.ingress.kubernetes.io/listen-ports"             = jsonencode([{ HTTP = 80 }])
      "alb.ingress.kubernetes.io/healthcheck-path"         = "/readyz"
      "alb.ingress.kubernetes.io/success-codes"            = "200"
      "alb.ingress.kubernetes.io/load-balancer-attributes" = "idle_timeout.timeout_seconds=90"
    }
  }

  spec {
    ingress_class_name = "alb"
    rule {
      http {
        dynamic "path" {
          for_each = concat(local.adapter_private_paths, var.observation_runtime == null ? [] : [local.observation_route])
          content {
            path      = path.value
            path_type = "Exact"
            backend {
              service {
                name = "tool-adapters"
                port { number = 80 }
              }
            }
          }
        }
      }
    }
  }
}

output "private_edge" {
  description = "Observed ingress DNS for subsequent AWS ALB/VPC-link readback; not evidence of connectivity, provider access or connector activation."
  value = var.private_edge_enabled ? {
    namespace = kubernetes_namespace_v1.adapter.metadata[0].name
    ingress   = kubernetes_ingress_v1.adapter_private[0].metadata[0].name
    hostname  = try(kubernetes_ingress_v1.adapter_private[0].status[0].load_balancer[0].ingress[0].hostname, null)
    paths     = local.adapter_private_paths
  } : null
}
