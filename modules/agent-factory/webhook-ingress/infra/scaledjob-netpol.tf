# =============================================================================
# NetworkPolicy — Hosted Agent Worker Egress
# =============================================================================
# Pods may egress to:
#   - GitHub API (*.github.com, *.githubusercontent.com)
#   - npm/PyPI registries
#   - Bedrock gateway (in-cluster service)
#   - Customer AWS APIs (for operations persona)
# All other egress is denied by the default-deny policy.
#
# Issue: #346
# =============================================================================

# Default-deny all egress in the namespace. Individual pods must match
# the allow policy below to communicate externally.
resource "kubernetes_network_policy" "default_deny_egress" {
  metadata {
    name      = "default-deny-egress"
    namespace = kubernetes_namespace.adp_agents.metadata[0].name

    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }

  spec {
    pod_selector {}
    policy_types = ["Egress"]
    # No egress rules = deny all
  }
}

# Allow agent pods controlled egress to required services.
resource "kubernetes_network_policy" "agent_scaledjob_egress" {
  metadata {
    name      = "agent-scaledjob-egress"
    namespace = kubernetes_namespace.adp_agents.metadata[0].name

    labels = {
      "app.kubernetes.io/name"       = "agent-scaledjob"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }

  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/name" = "agent-scaledjob"
      }
    }

    policy_types = ["Egress"]

    # DNS resolution (kube-dns)
    egress {
      ports {
        port     = 53
        protocol = "UDP"
      }
      ports {
        port     = 53
        protocol = "TCP"
      }
    }

    # HTTPS egress to external services:
    # GitHub API, npm registry, PyPI, AWS APIs (Bedrock, SQS, STS, SecretsManager)
    egress {
      ports {
        port     = 443
        protocol = "TCP"
      }
    }

    # In-cluster Bedrock gateway service (port 8080)
    egress {
      ports {
        port     = 8080
        protocol = "TCP"
      }
      to {
        namespace_selector {
          match_labels = {
            "app.kubernetes.io/component" = "gateway"
          }
        }
      }
    }

    # SSH for git clone over SSH (some customer repos)
    egress {
      ports {
        port     = 22
        protocol = "TCP"
      }
    }

    # ADOT Collector in-namespace (gRPC on port 4317) — Issue #1630
    # Allows agent-worker pods to export OTel telemetry to the collector.
    egress {
      ports {
        port     = 4317
        protocol = "TCP"
      }
      to {
        pod_selector {
          match_labels = {
            "app.kubernetes.io/name" = "adot-collector"
          }
        }
      }
    }

    # Agent-context MCP server (port 5100) — Issue #3286
    # Allows agent-worker pods to reach the Knowledge Layer MCP endpoint
    # in the agent-context namespace for code intelligence tools.
    egress {
      ports {
        port     = 5100
        protocol = "TCP"
      }
      to {
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = "agent-context"
          }
        }
      }
    }
  }
}
