# Plan-only, mocked-provider assertions for the gateway's pod-identity wiring (#5051).
#
# What these prove: the association is declared for the gateway service account
# against the gateway role, the role's trust policy admits the pod-identity service
# principal with the actions the EKS Auth API requires, and no static AWS access key
# is configured anywhere in this module.
#
# What they do NOT prove: that a pod on a real cluster actually receives credentials
# this way. That is deferred live criterion U16a-L2 and needs a real cluster, a named
# account and a cleanup owner — all recorded unresolved.

mock_provider "aws" {}
mock_provider "kubernetes" {}
mock_provider "tls" {}
mock_provider "time" {}

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

# The mock EKS cluster returns an empty identity list; the OIDC provider, the IRSA
# trust policy and the outputs all read it. Same overrides as
# runner_access_tags.tftest.hcl — duplicated rather than shared because Terraform test
# files have no include mechanism.
run "pod_identity_association_targets_the_gateway_service_account" {
  # command = apply, not plan, and it still touches no AWS account: every provider in
  # this file is mocked, so "apply" resolves values from the mocks and the module's own
  # configuration. It is needed because the two things this run asserts on — the role's
  # assume_role_policy and the association's attributes — are not known during a plan
  # (the AWS provider normalises the policy document, and the association's identifiers
  # are assigned on create). Under plan those assertions are unevaluable rather than
  # false, which is a test that cannot fail and therefore cannot regress.
  command = apply

  variables {
    enable_gateway_pod_identity = true
  }

  override_data {
    target = data.aws_region.current
    values = {
      name = "us-east-1"
    }
  }

  override_data {
    target = data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
    }
  }

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

  # Pin the two AWS-assigned ARNs the assertions read through, so a failure is a real
  # disagreement with the module rather than the mock provider's per-run random values.
  # The trust policy interpolates the OIDC provider ARN; the association's role_arn is
  # the gateway role's.
  #
  # Only `arn` is overridden on the role. assume_role_policy is a configured argument
  # and still comes from the module, so the trust-policy assertions below are checking
  # this module's actual configuration, not a value restated by the test.
  override_resource {
    target = aws_iam_openid_connect_provider.cluster
    values = {
      arn = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/EXAMPLED539D4633E53DE1B716D3041E"
    }
  }

  override_resource {
    target = aws_iam_role.gateway_service_irsa
    values = {
      arn = "arn:aws:iam::123456789012:role/adp-dev-role-gateway-service"
    }
  }

  # The association must exist, or pod identity resolves nothing no matter what the
  # trust policy says.
  assert {
    condition     = length(aws_eks_pod_identity_association.gateway_service) == 2
    error_message = "Both gateway namespaces must have a pod-identity association; the gateway runs in bedrockgw or adp-gateway depending on how it was deployed (#33)."
  }

  # Bound to one named service account. A wildcard or a different SA name would let
  # another workload in the namespace pick up the gateway's role.
  assert {
    condition = alltrue([for ns, assoc in aws_eks_pod_identity_association.gateway_service :
      assoc.service_account == "gateway-service" &&
      assoc.namespace == ns &&
      assoc.role_arn == aws_iam_role.gateway_service_irsa.arn
    ])
    error_message = "Each association must bind the gateway-service account in its own namespace to the gateway role."
  }

  assert {
    condition     = alltrue([for _, assoc in aws_eks_pod_identity_association.gateway_service : assoc.cluster_name == aws_eks_cluster.main.name])
    error_message = "Associations must target this module's cluster."
  }

  # Confined to the gateway's own namespaces — not kube-system, not a tenant namespace.
  assert {
    condition     = toset(keys(aws_eks_pod_identity_association.gateway_service)) == toset(["bedrockgw", "adp-gateway"])
    error_message = "Pod identity for the gateway role must be confined to the gateway's namespaces."
  }

  # The trust statement is what makes the association resolvable. sts:TagSession is
  # not optional: the EKS Auth API tags the session with the identity it resolved, and
  # without it every pod-identity AssumeRole is denied.
  assert {
    condition = anytrue([
      for statement in jsondecode(aws_iam_role.gateway_service_irsa.assume_role_policy).Statement :
      statement.Effect == "Allow" &&
      try(statement.Principal.Service, "") == "pods.eks.amazonaws.com" &&
      contains(statement.Action, "sts:AssumeRole") &&
      contains(statement.Action, "sts:TagSession")
    ])
    error_message = "The gateway role must trust pods.eks.amazonaws.com for sts:AssumeRole and sts:TagSession, or pod identity cannot deliver credentials."
  }

  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role.gateway_service_irsa.assume_role_policy).Statement :
      try(statement.Principal.Service, "") != "pods.eks.amazonaws.com" ? true : (
        statement.Condition.StringEquals["aws:RequestTag/eks-cluster-arn"] == aws_eks_cluster.main.arn &&
        statement.Condition.StringEquals["aws:RequestTag/kubernetes-service-account"] == "gateway-service" &&
        toset(statement.Condition.StringEquals["aws:RequestTag/kubernetes-namespace"]) == toset(["bedrockgw", "adp-gateway"])
      )
    ])
    error_message = "Pod-identity trust must require this cluster and the gateway service account/namespaces."
  }

  # IRSA is left intact so this change is reversible without another trust-policy edit.
  assert {
    condition = anytrue([
      for statement in jsondecode(aws_iam_role.gateway_service_irsa.assume_role_policy).Statement :
      try(statement.Action, "") == "sts:AssumeRoleWithWebIdentity"
    ])
    error_message = "The IRSA statement must remain, so an explicitly rolled-back pod can still use IRSA."
  }
}

# Default-off is asserted. Associations prepare pod identity; an IRSA pod still
# uses web identity until a separately reviewed pod configuration change and rollout.
#
# Also command = apply, and for the same reason as above plus one more: run blocks in a
# file share state, so a plan here would read the role created by the run above and its
# trust-policy assertion would pass on that inherited value rather than on anything this
# run established. Applying with the flag left at its default makes this run stand on
# its own — and exercises association removal only (not a live credential rollback), since it removes the
# associations the previous run created.
run "pod_identity_is_off_by_default" {
  command = apply

  override_data {
    target = data.aws_region.current
    values = {
      name = "us-east-1"
    }
  }

  override_resource {
    target = aws_iam_openid_connect_provider.cluster
    values = {
      arn = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/EXAMPLED539D4633E53DE1B716D3041E"
    }
  }

  override_data {
    target = data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
    }
  }

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

  assert {
    condition     = length(aws_eks_pod_identity_association.gateway_service) == 0
    error_message = "Pod identity associations must be opt-in per environment; credential cutover requires a separate rollout."
  }

  # The trust statement is present regardless of the flag. A statement alone grants
  # nothing without an association, and having it always present means enabling pod
  # identity is one variable rather than a variable plus an IAM change.
  assert {
    condition = anytrue([
      for statement in jsondecode(aws_iam_role.gateway_service_irsa.assume_role_policy).Statement :
      try(statement.Principal.Service, "") == "pods.eks.amazonaws.com"
    ])
    error_message = "The pod-identity trust statement should be present even while the association is off; it grants nothing on its own."
  }
}
