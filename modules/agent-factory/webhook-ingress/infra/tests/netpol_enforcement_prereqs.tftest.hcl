# Issue #4999: these assertions guard the preconditions for turning NetworkPolicy
# ENFORCEMENT on. Until #4999, the cluster had no enforcement agent, so a broken
# or missing policy here was undetectable by any test or probe — the policies were
# never applied to any packet. Once enforcement is live, each of the four facts
# below is the difference between a working namespace and a silent outage:
#
#   1. The ADOT collector has DNS + HTTPS egress. It is selected by
#      default-deny-egress (pod_selector {}) and, before #4999, matched no allow
#      policy. Enforcement without this rule = total loss of agent telemetry with
#      no restart, no CrashLoop and no error surfaced anywhere.
#   2. default-deny-egress is still deny-all. The tempting way to make evaluation
#      #3967's W1-04 probe pass is to loosen isolation; this asserts nobody did.
#   3. The worker allowlist still pins all four paths it needs. Dropping one is a
#      no-op today and breaks every hosted run the moment enforcement starts.
#   4. The control-listener ingress boundary — the actual subject of W1-04 — still
#      admits only gateway pods, and only on the control port.
#
# Plan-only with mocked providers: this proves what the configuration DECLARES.
# It cannot prove the controller enforces it — that requires the live post-apply
# probes in docs/runbooks/network-policy-enforcement.md.

mock_provider "aws" {}
mock_provider "kubernetes" {}
mock_provider "helm" {}

run "enforcement_preconditions_hold" {
  command = plan

  # The mocked providers invent random strings for computed data-source
  # attributes, which breaks resources elsewhere in the module that parse them
  # (KMS validates its policy as JSON; the OIDC locals index into the cluster's
  # identity list). None of that is related to NetworkPolicy — these overrides
  # just make the plan reach the assertions below.
  # Pin account/region so interpolated ARNs are valid (the mock's random account
  # id is not 12 digits, which trips ARN validation in eventbridge.tf).
  override_data {
    target = data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
    }
  }

  override_data {
    target = data.aws_region.current
    values = {
      name = "us-east-1"
    }
  }

  override_data {
    target = data.aws_iam_policy_document.dynamodb_kms
    values = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }

  override_data {
    target = data.aws_iam_policy_document.cloudwatch_kms
    values = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }

  override_data {
    target = data.aws_eks_cluster.main
    values = {
      identity = [{
        oidc = [{
          issuer = "https://oidc.eks.us-east-1.amazonaws.com/id/EXAMPLED539D4633E53DE1B716D3041E"
        }]
      }]
      certificate_authority = [{
        data = "TFNUQVJUQ0VSVElGSUNBVEU="
      }]
    }
  }

  # -- 1. ADOT collector egress exists and covers DNS + HTTPS ------------------

  assert {
    condition     = kubernetes_network_policy.adot_collector_egress.spec[0].pod_selector[0].match_labels["app.kubernetes.io/name"] == "adot-collector"
    error_message = "ADOT egress policy must select the collector pods by app.kubernetes.io/name=adot-collector (the label otel-collector.tf puts on the pod template). A selector that matches nothing grants nothing, and the collector stays denied."
  }

  # DNS over UDP *and* TCP. kube-dns answers over UDP and falls back to TCP for
  # oversized responses; allowing only UDP fails intermittently under load, which
  # is much harder to attribute than a clean total failure.
  assert {
    condition = anytrue([
      for rule in kubernetes_network_policy.adot_collector_egress.spec[0].egress :
      length([for p in rule.ports : p if tostring(p.port) == "53" && p.protocol == "UDP"]) > 0
    ])
    error_message = "ADOT egress must allow DNS on 53/UDP, or every exporter fails at name resolution."
  }

  assert {
    condition = anytrue([
      for rule in kubernetes_network_policy.adot_collector_egress.spec[0].egress :
      length([for p in rule.ports : p if tostring(p.port) == "53" && p.protocol == "TCP"]) > 0
    ])
    error_message = "ADOT egress must allow DNS on 53/TCP as well as UDP — TCP is the fallback for responses that exceed the UDP size limit."
  }

  # 443 covers STS (IRSA credential refresh) plus the awsxray, awsemf and
  # awscloudwatchlogs exporter endpoints.
  assert {
    condition = anytrue([
      for rule in kubernetes_network_policy.adot_collector_egress.spec[0].egress :
      length([for p in rule.ports : p if tostring(p.port) == "443" && p.protocol == "TCP"]) > 0
    ])
    error_message = "ADOT egress must allow 443/TCP for STS, X-Ray and CloudWatch, or telemetry export stops once enforcement is enabled."
  }

  assert {
    condition     = kubernetes_network_policy.adot_collector_egress.spec[0].policy_types == tolist(["Egress"])
    error_message = "ADOT policy must be Egress-only. Adding Ingress here would create a deny for the collector's OTLP receiver on 4317 and cut the workers' export path."
  }

  # -- 2. Isolation was not weakened to make a check pass ----------------------

  # An empty `pod_selector {}` selects every pod in the namespace. In state that
  # is match_labels = null (not an empty map), and match_expressions empty — both
  # must hold, since either one being populated narrows the deny.
  assert {
    condition = (
      length(coalesce(kubernetes_network_policy.default_deny_egress.spec[0].pod_selector[0].match_labels, {})) == 0 &&
      length(coalesce(kubernetes_network_policy.default_deny_egress.spec[0].pod_selector[0].match_expressions, [])) == 0
    )
    error_message = "default-deny-egress must keep an EMPTY pod_selector so it covers every pod in adp-agents. Narrowing it would exempt pods from the default deny."
  }

  assert {
    condition     = length(kubernetes_network_policy.default_deny_egress.spec[0].egress) == 0
    error_message = "default-deny-egress must have ZERO egress rules. Any rule added here is a namespace-wide hole, not a targeted allow — targeted allows belong in their own policy."
  }

  # -- 3. Worker allowlist still pins every path it needs ---------------------

  assert {
    condition = alltrue([
      for want in ["53", "443", "22", "4317", "5100"] :
      anytrue([
        for rule in kubernetes_network_policy.agent_scaledjob_egress.spec[0].egress :
        length([for p in rule.ports : p if tostring(p.port) == want]) > 0
      ])
    ])
    error_message = "Worker allowlist must retain DNS 53, HTTPS 443, SSH 22, OTLP 4317 and MCP 5100. Dropping one is invisible while enforcement is off and breaks hosted runs the moment it is on."
  }

  # 4317 must be scoped to the collector, and 5100 to the agent-context
  # namespace. An unscoped port allowance would let a worker reach that port on
  # ANY pod cluster-wide, which is a much wider grant than the audit recorded.
  assert {
    condition = anytrue([
      for rule in kubernetes_network_policy.agent_scaledjob_egress.spec[0].egress :
      length([for p in rule.ports : p if tostring(p.port) == "4317"]) > 0 &&
      anytrue([
        for dest in rule.to :
        try(dest.pod_selector[0].match_labels["app.kubernetes.io/name"], "") == "adot-collector"
      ])
    ])
    error_message = "Worker OTLP 4317 egress must be scoped to the adot-collector pods, not left open to any pod on 4317."
  }

  assert {
    condition = anytrue([
      for rule in kubernetes_network_policy.agent_scaledjob_egress.spec[0].egress :
      length([for p in rule.ports : p if tostring(p.port) == "5100"]) > 0 &&
      anytrue([
        for dest in rule.to :
        try(dest.namespace_selector[0].match_labels["kubernetes.io/metadata.name"], "") == "agent-context"
      ])
    ])
    error_message = "Worker MCP 5100 egress must be scoped to the agent-context namespace."
  }

  # The direct in-cluster pod -> gateway path removed in #3954/#3960 must stay
  # closed. Workers reach the gateway through API Gateway over the 443 rule.
  assert {
    condition = alltrue([
      for rule in kubernetes_network_policy.agent_scaledjob_egress.spec[0].egress :
      alltrue([
        for dest in rule.to :
        try(dest.pod_selector[0].match_labels["app"], "") != "bedrockgateway" &&
        try(dest.namespace_selector[0].match_labels["kubernetes.io/metadata.name"], "") != "adp-gateway"
      ])
    ])
    error_message = "No worker egress rule may target the gateway directly — #3954 removed that path deliberately, because the gateway trusts X-Caller-Identity on the internal plane."
  }

  # -- 4. The W1-04 boundary itself -------------------------------------------

  assert {
    condition     = kubernetes_network_policy.agent_control_listener_ingress.spec[0].policy_types == tolist(["Ingress"])
    error_message = "The control-listener policy must be Ingress-only; it is what creates the deny that W1-04 probes."
  }

  assert {
    condition     = length(kubernetes_network_policy.agent_control_listener_ingress.spec[0].ingress) == 1 && length(kubernetes_network_policy.agent_control_listener_ingress.spec[0].ingress[0].from) == 1
    error_message = "The control listener must admit exactly one source. A second from-block ORs in another caller and would make W1-04 pass for the wrong reason."
  }

  # namespace_selector AND pod_selector inside ONE from-block: "pods labelled
  # bedrockgateway, in the gateway namespace". Split across two blocks they OR,
  # which would additionally admit any pod named bedrockgateway in any namespace.
  assert {
    condition = (
      kubernetes_network_policy.agent_control_listener_ingress.spec[0].ingress[0].from[0].namespace_selector[0].match_labels["kubernetes.io/metadata.name"] == "adp-gateway" &&
      kubernetes_network_policy.agent_control_listener_ingress.spec[0].ingress[0].from[0].pod_selector[0].match_labels["app"] == "bedrockgateway"
    )
    error_message = "Control-listener ingress must AND the gateway namespace with the bedrockgateway pod label in a single from-block."
  }

  assert {
    condition = (
      length(kubernetes_network_policy.agent_control_listener_ingress.spec[0].ingress[0].ports) == 1 &&
      tostring(kubernetes_network_policy.agent_control_listener_ingress.spec[0].ingress[0].ports[0].port) == "8770"
    )
    error_message = "Control-listener ingress must admit exactly the one control port (8770). Widening it re-opens the boundary W1-04 exists to verify."
  }
}
