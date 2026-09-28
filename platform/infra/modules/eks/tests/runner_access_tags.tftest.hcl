# Plan-only, mocked-provider regression for the shared runner access entry.
# Keep agent-factory tags aligned without suppressing other operator drift.

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

run "runner_tags_align_without_hiding_operator_drift" {
  command = plan

  variables {
    cluster_admin_principal_arns = ["arn:aws:iam::123456789012:role/adp-dev-agent-runner-role", "arn:aws:iam::123456789012:role/Admin"]
  }

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

  assert {
    condition = aws_eks_access_entry.admins["arn:aws:iam::123456789012:role/adp-dev-agent-runner-role"].tags == tomap({
      Module = "agent-factory"
      Name   = "adp-dev-agent-runner-access"
      Owner  = "agent-team"
    })
    error_message = "Shared runner tags must match agent-factory's declared tags."
  }

  assert {
    condition     = length(aws_eks_access_entry.admins["arn:aws:iam::123456789012:role/Admin"].tags) == 0
    error_message = "Unrelated operators must retain the platform's tag management."
  }

  assert {
    condition = alltrue([for arn, entry in aws_eks_access_entry.admins :
      entry.principal_arn == arn && entry.type == "STANDARD"
    ])
    error_message = "Tag alignment must preserve principals and entry types."
  }

  assert {
    condition = alltrue([for arn, association in aws_eks_access_policy_association.admins :
      association.principal_arn == arn &&
      association.policy_arn == "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy" &&
      association.access_scope[0].type == "cluster"
    ])
    error_message = "Both operator and runner must retain cluster-admin access."
  }
}
