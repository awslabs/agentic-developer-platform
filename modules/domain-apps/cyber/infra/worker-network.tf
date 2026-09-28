# Gateway endpoints retain AWS service IP addresses; private-subnet CIDRs alone
# do not permit S3 downloads or DynamoDB writes when Auto Mode enforces policies.
data "aws_prefix_list" "worker_gateway_services" {
  for_each = toset(["s3", "dynamodb"])
  name     = "com.amazonaws.${var.aws_region}.${each.key}"
}

resource "kubernetes_network_policy_v1" "cyber_aws_gateway_egress" {
  provider = kubernetes.cyber

  metadata {
    name      = "cyber-aws-gateway-egress"
    namespace = kubernetes_namespace.cyber_workers.metadata[0].name
  }

  spec {
    pod_selector {
      match_expressions {
        key      = "role"
        operator = "In"
        values   = ["triage", "static"]
      }
    }
    policy_types = ["Egress"]

    # The node-local Pod Identity agent issues credentials over HTTP.
    egress {
      ports {
        port     = "80"
        protocol = "TCP"
      }
      to {
        ip_block {
          cidr = "169.254.170.23/32"
        }
      }
    }

    egress {
      ports {
        port     = "443"
        protocol = "TCP"
      }
      dynamic "to" {
        for_each = toset(flatten([
          for service in data.aws_prefix_list.worker_gateway_services : service.cidr_blocks
        ]))
        content {
          ip_block {
            cidr = to.value
          }
        }
      }
    }
  }
}
