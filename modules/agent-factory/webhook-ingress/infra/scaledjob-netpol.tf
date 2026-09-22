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

    # The browser broker is the sole URL-analysis browsing boundary. Workers
    # can submit a URL for a guarded capture but cannot access AgentCore itself.
    egress {
      to {
        pod_selector {
          match_labels = {
            "app.kubernetes.io/name" = "url-analysis-browser-broker"
          }
        }
      }
      ports {
        port     = 8765
        protocol = "TCP"
      }
    }

    # NO in-cluster path to the gateway. Deliberate, and load-bearing.
    #
    # Issue #3960: a rule here previously claimed to allow agent pods → gateway on
    # port 8080, selecting the namespace by `app.kubernetes.io/component=gateway`.
    # That rule was DEAD: `namespace_selector` matches labels on the NAMESPACE
    # object, and no namespace carries that label — it is on the gateway's
    # Deployment and pods (modules/gateway/k8s/), not on `adp-gateway` itself. The
    # selector matched zero namespaces, so it permitted zero traffic.
    #
    # It is REMOVED rather than repaired, and that is a security decision, not
    # tidying. Repairing it (selecting `kubernetes.io/metadata.name` so the
    # selector genuinely matches) would open a direct pod → gateway path, and the
    # gateway currently treats the `X-Caller-Identity` header as an authenticated
    # identity assertion with no signature check, because API Gateway is the only
    # component that sanitizes it (`BG_TRUST_APIGW_HEADERS=true`). There is no
    # gateway-side ingress NetworkPolicy. So a compromised agent pod — and agent
    # pods process untrusted repository and issue content by design — could reach
    # `/internal/v1/*` directly and assert a seeded internal-plane identity,
    # escalating out of its own tenant. While the rule is dead, that request dies
    # at the TCP layer instead.
    #
    # Nothing needs the path today: agent pods reach the gateway through the
    # sigv4-proxy → API Gateway, which is public and already covered by the
    # blanket 443 rule above. That is why the rule stayed broken unnoticed.
    #
    # Removing it also answers the original concern that motivated repairing it — a
    # rule that looks like it grants access while granting none misleads the next
    # reader — because this comment is now what they find instead.
    #
    # To open it later, all three are prerequisites, not options:
    #   1. a `pod_selector` in the `to` block so it targets the gateway pods only,
    #      not every pod in that namespace, and only the port genuinely needed;
    #   2. a gateway-namespace INGRESS NetworkPolicy admitting only the API Gateway
    #      VPC Link path;
    #   3. an app-side guard rejecting `X-Caller-Identity` on requests that did not
    #      transit API Gateway — the durable fix, which closes the class rather
    #      than this one instance.

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

# =============================================================================
# NetworkPolicy — Live control listener ingress (Issue #3960)
# =============================================================================
# The worker's control listener binds a port inside the agent pod. This policy is
# what decides who may reach it.
#
# **Why a policy at all, when the listener requires a bearer token.** The token is
# the authentication; this is the reachability boundary, and neither substitutes
# for the other. Without the policy, every pod in the cluster can open a TCP
# connection to the listener and probe it — the token becomes the only thing
# between an arbitrary workload and a running agent, and a token comparison bug
# would then be directly exploitable from anywhere in the cluster rather than only
# from the gateway's namespace. Together with the pod's explicit bind to POD_IP
# (never 0.0.0.0), that is three independent layers, none sufficient alone.
#
# **Why an explicit ingress policy is REQUIRED, not defence in depth.** The
# namespace's `default-deny-egress` above sets `policy_types = ["Egress"]` only,
# so it does not deny ingress. Kubernetes ingress is allow-all until some policy
# selects a pod for Ingress; this resource is what creates that deny. Adding
# ingress rules to the egress policy instead would have been wrong for the same
# reason: it selects ALL pods in the namespace, which would silently restrict
# ingress for the ADOT collector and anything else that lands here later.
#
# **Verb-blind by construction.** The rule names a port, never a path or a method.
# A later story enabling pause or abort — or adding the reserved `/agent/events`
# SSE stream — needs no policy change, so the security boundary is not re-litigated
# per verb (ADR-9, FR-1.10). That is also why it is deployed BEFORE the listener
# exists: a policy that arrives after the port it guards leaves a window in which
# the port is open to the cluster, and `FEATURE_AGENT_CONTROL_ENABLED` gates the
# listener while this resource is unconditional.
#
# **Unconditional on purpose.** It is applied whether the feature flag is on or
# off. A policy guarding a port nothing is listening on costs nothing; a flag
# flipped on in an environment where the policy was skipped is an open port.
resource "kubernetes_network_policy" "agent_control_listener_ingress" {
  metadata {
    name      = "agent-control-listener-ingress"
    namespace = kubernetes_namespace.adp_agents.metadata[0].name

    labels = {
      "app.kubernetes.io/name"       = "agent-scaledjob"
      "app.kubernetes.io/component"  = "agent-control"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }

  spec {
    # Selects the same pods the ScaledJob template labels. Because this selector
    # is non-empty, the deny it creates applies ONLY to agent-worker pods — other
    # pods in the namespace keep whatever ingress they had.
    pod_selector {
      match_labels = {
        "app.kubernetes.io/name" = "agent-scaledjob"
      }
    }

    policy_types = ["Ingress"]

    ingress {
      ports {
        port     = var.agent_control_port
        protocol = "TCP"
      }

      # Namespace-scoped, not cluster-wide. `namespace_selector` and
      # `pod_selector` inside ONE `to`/`from` block are ANDed: this is
      # "pods labelled bedrockgateway, in the gateway namespace". Splitting them
      # into two blocks would OR them, which would additionally admit any pod
      # named bedrockgateway in ANY namespace — a distinction that is easy to get
      # wrong and impossible to see in a diff.
      from {
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = var.gateway_namespace
          }
        }
        pod_selector {
          match_labels = {
            "app" = "bedrockgateway"
          }
        }
      }
    }
  }
}

# =============================================================================
# NetworkPolicy — ADOT collector egress (Issue #4999)
# =============================================================================
# `default-deny-egress` above selects EVERY pod in this namespace
# (`pod_selector {}`), and until #4999 the ADOT collector matched no allow
# policy at all. That was invisible only because the cluster had no
# network-policy enforcement agent: the deny was never applied, so the missing
# allow never mattered.
#
# #4999 turns enforcement on. The moment it does, this policy is the only thing
# keeping agent telemetry alive — without it the collector's exporters simply
# stop reaching AWS, and they fail the way blocked egress always fails: no pod
# restart, no CrashLoop, no error surfaced to any user, just traces and metrics
# that quietly never arrive. That is why this resource ships and applies BEFORE
# the controller is enabled, never in the same apply and never after.
#
# The collector (`adot-collector-config`, otel-collector.tf) exports via
# `awsxray`, `awsemf` and `awscloudwatchlogs`. Every one of those is an HTTPS
# call to a regional AWS endpoint, and each needs a hostname resolved first and
# IRSA credentials refreshed from STS — hence exactly two allowances:
#   - 53 TCP+UDP for name resolution (both protocols: kube-dns answers over UDP
#     and falls back to TCP for responses that exceed the UDP limit; allowing
#     only UDP produces intermittent resolution failures under load, which is
#     far harder to attribute than a total outage);
#   - 443 TCP for STS, X-Ray and CloudWatch.
#
# Nothing more. No 4317 egress — the collector RECEIVES OTLP on 4317, and
# ingress is not restricted for it (no policy selects it for Ingress, and
# `default-deny-egress` is `policy_types = ["Egress"]` only). No in-cluster
# gateway path: this is a telemetry sink and has no business talking to the
# gateway (see the removal rationale in agent-scaledjob-egress above, #3954).
#
# Unconditional, like agent-control-listener-ingress and for the same reason.
# The collector itself is gated on `var.enable_agent_otel`, so when telemetry is
# off this policy selects zero pods and grants zero traffic — costless. Gating
# it on the same flag would instead create an ordering hazard: a flag flipped on
# in an environment where enforcement was already live would deploy a collector
# whose egress is denied, which is precisely the silent failure above.
resource "kubernetes_network_policy" "adot_collector_egress" {
  metadata {
    name      = "adot-collector-egress"
    namespace = kubernetes_namespace.adp_agents.metadata[0].name

    labels = {
      "app.kubernetes.io/name"       = "adot-collector"
      "app.kubernetes.io/component"  = "observability"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }

  spec {
    # Matches the collector Deployment's pod template labels (otel-collector.tf).
    # Non-empty, so it adds an allow for the collector only and changes nothing
    # for any other pod in the namespace.
    pod_selector {
      match_labels = {
        "app.kubernetes.io/name" = "adot-collector"
      }
    }

    policy_types = ["Egress"]

    # DNS resolution (kube-dns).
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

    # HTTPS to regional AWS endpoints: STS (IRSA credential refresh), X-Ray,
    # CloudWatch Logs, CloudWatch EMF.
    egress {
      ports {
        port     = 443
        protocol = "TCP"
      }
    }
  }
}
