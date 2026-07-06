# Issue #2944: the gateway IRSA role must be able to read the deploy-time SSM
# params that register_app_start reads (/adp/<env>/webhook-ingress/endpoint and
# /adp/<env>/gateway/apigw-invoke-url). Without ssm:GetParameter the reads get
# AccessDenied and the #2674 guard returns 422 "Webhook endpoint not configured".
#
# Plan-only test with mocked providers — asserts the policy is attached to the
# gateway IRSA role, grants the two read actions, and is scoped to this
# account's /adp/<env>/* prefix (not ssm:* / not a wildcard resource).

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

run "gateway_ssm_read_policy_is_scoped" {
  command = apply

  # Pin region/account so the ARN interpolation is deterministic instead of the
  # mock provider's random values.
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

  # The mock EKS cluster returns an empty identity list; supply the OIDC issuer
  # the OIDC provider / IRSA trust policy / outputs all depend on.
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

  # The SSM read policy is attached to the gateway IRSA role (the role the pod
  # actually assumes), not the placeholder role in the iam module.
  assert {
    condition     = aws_iam_role_policy.gateway_ssm_read.role == aws_iam_role.gateway_service_irsa.id
    error_message = "SSM read policy must attach to the gateway_service_irsa role"
  }

  # Policy name matches the live-applied inline policy so Terraform converges
  # with no drift / no second policy.
  assert {
    condition     = aws_iam_role_policy.gateway_ssm_read.name == "adp-dev-policy-gateway-ssm-read"
    error_message = "Policy name must be <name_prefix>-policy-gateway-ssm-read"
  }

  # Grants both read actions, and nothing broader (no ssm:*).
  assert {
    condition = alltrue([
      for action in jsondecode(aws_iam_role_policy.gateway_ssm_read.policy).Statement[0].Action :
      contains(["ssm:GetParameter", "ssm:GetParameters"], action)
    ])
    error_message = "Policy must grant only ssm:GetParameter / ssm:GetParameters (no ssm:*)"
  }

  assert {
    condition = contains(
      jsondecode(aws_iam_role_policy.gateway_ssm_read.policy).Statement[0].Action,
      "ssm:GetParameter"
    )
    error_message = "Policy must grant ssm:GetParameter — the action register_app_start needs"
  }

  # Resource is scoped to this account's /adp/<env>/* prefix — covers both
  # params the flow reads, and is not a wildcard.
  assert {
    condition     = jsondecode(aws_iam_role_policy.gateway_ssm_read.policy).Statement[0].Resource == "arn:aws:ssm:us-east-1:123456789012:parameter/adp/dev/*"
    error_message = "SSM resource must be scoped to arn:aws:ssm:<region>:<account>:parameter/adp/<env>/*"
  }
}
