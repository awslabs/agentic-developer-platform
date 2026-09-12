# Issue #4999: NetworkPolicy objects on this Auto Mode cluster were accepted by
# the apiserver but never enforced, because Auto Mode ships the network-policy
# controller disabled and nothing in IaC asked for it. Evaluation #3967's W1-04
# probe measured the consequence: a deny-all ingress policy on the probe target
# still returned HTTP 200, and `kubectl get policyendpoints -A` was empty.
#
# These are plan-only assertions with mocked providers. They prove the module
# declares the documented enablement key, and — just as importantly — that it
# declares nothing when the flag is off, so no environment starts enforcing on
# its next apply as a side effect. They CANNOT prove the controller reconciles
# existing policies into PolicyEndpoints; that is the live post-apply check in
# docs/runbooks/network-policy-enforcement.md.

mock_provider "aws" {}
mock_provider "kubernetes" {}
mock_provider "tls" {}

variables {
  environment             = "dev"
  name_prefix             = "adp-dev"
  vpc_id                  = "vpc-00000000000000000"
  private_subnet_ids      = ["subnet-00000000000000001", "subnet-00000000000000002"]
  eks_security_group_id   = "sg-00000000000000000"
  eks_cluster_role_arn    = "arn:aws:iam::123456789012:role/adp-dev-role-eks-cluster"
  node_group_role_arn     = "arn:aws:iam::123456789012:role/adp-dev-role-eks-node-group"
  eks_public_access_cidrs = ["10.0.0.0/8"]
}

# The mock EKS cluster returns an empty identity list, which the OIDC locals and
# IRSA trust policies index into. Same shim the gateway_ssm_read test uses.
override_resource {
  target = aws_eks_cluster.main
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

override_data {
  target = data.tls_certificate.cluster
  values = {
    certificates = [{
      sha1_fingerprint = "0123456789abcdef0123456789abcdef01234567"
    }]
  }
}

# Default-off is the safety property: an environment that has not audited its
# existing policies for deny-without-allow gaps must not begin enforcing merely
# because it ran a platform apply after this change merged.
run "disabled_by_default_creates_nothing" {
  command = plan

  assert {
    condition     = var.enable_network_policy_controller == false
    error_message = "enable_network_policy_controller must default to false. Enabling enforcement activates every NetworkPolicy already in the cluster at once; a pod selected by a deny with no matching allow silently loses traffic, so this cannot be an opt-out."
  }

  assert {
    condition     = length(kubernetes_config_map.amazon_vpc_cni) == 0
    error_message = "No amazon-vpc-cni ConfigMap may be created while the flag is off — that is what keeps the change reviewable and per-environment."
  }
}

run "enabled_declares_the_documented_key" {
  command = plan

  variables {
    enable_network_policy_controller = true
  }

  assert {
    condition     = length(kubernetes_config_map.amazon_vpc_cni) == 1
    error_message = "Setting enable_network_policy_controller = true must create the amazon-vpc-cni ConfigMap."
  }

  # Name and namespace are not ours to choose — the controller reads exactly
  # kube-system/amazon-vpc-cni. A typo here yields a ConfigMap nothing reads,
  # i.e. a silent no-op that looks applied.
  assert {
    condition     = kubernetes_config_map.amazon_vpc_cni[0].metadata[0].name == "amazon-vpc-cni"
    error_message = "ConfigMap must be named exactly amazon-vpc-cni (AWS reads this specific name; any other name is silently ignored)."
  }

  assert {
    condition     = kubernetes_config_map.amazon_vpc_cni[0].metadata[0].namespace == "kube-system"
    error_message = "ConfigMap must live in kube-system — the namespace the Auto Mode controller reads."
  }

  # The value must be the STRING "true". ConfigMap data is string-typed, and a
  # bool would fail to apply rather than enable anything.
  #
  # HEADS UP — this particular assertion fails as a CRASH, not a clean message.
  # Break it (e.g. expect "false") and Terraform aborts with:
  #
  #   panic: unexpected error marshalling value: value has marks, so it cannot
  #   be serialized as JSON
  #
  # Exit code 11 (mutated) vs 0 (baseline). The panic occurs while rendering the
  # failure diagnostic: something in this module's plan carries a sensitive mark,
  # and the test view cannot serialize a marked value into the diagnostic. So the
  # stack trace IS this guard firing — not a broken test harness, and not a
  # reason to delete or weaken the assertion. Read the assertion above to see
  # what was expected; the panic itself carries no useful detail.
  #
  # KNOWN LIMITATION: writing the value as bare `true` instead of `"true"` in
  # main.tf still PASSES here (verified: exit 0), because Terraform coerces the
  # bool into ConfigMap `data`'s map(string). This assertion therefore guards
  # against a wrong *value*, not a wrong *type*.
  assert {
    condition     = kubernetes_config_map.amazon_vpc_cni[0].data["enable-network-policy-controller"] == "true"
    error_message = "Key enable-network-policy-controller must be the string \"true\" — the documented Auto Mode enablement (https://docs.aws.amazon.com/eks/latest/userguide/auto-net-pol.html)."
  }

  # Auto Mode stays Auto Mode. #4999's non-goals are explicit that neither the
  # cluster's compute config nor the shared NodeClass may be mutated: the
  # NodeClass already reports networkPolicy: DefaultAllow, which is the mode
  # rather than a disablement, so changing it would be a cluster-wide networking
  # change with no bearing on the enforcement gap.
  assert {
    condition     = aws_eks_cluster.main.compute_config[0].enabled == true
    error_message = "compute_config.enabled must remain true — this change must not alter Auto Mode."
  }

  assert {
    condition     = tolist(aws_eks_cluster.main.compute_config[0].node_pools) == tolist(["general-purpose"])
    error_message = "Auto Mode node_pools must be unchanged by the network-policy change."
  }
}
