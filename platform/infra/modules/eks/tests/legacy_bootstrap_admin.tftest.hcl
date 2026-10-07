# Upgrade preservation: cluster bootstrap access is immutable, whether true or false.
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


run "legacy_cluster" {
  command = apply
  variables {
    bootstrap_cluster_creator_admin_permissions = true
  }
}

run "legacy_cluster_without_creator_admin" {
  command   = apply
  state_key = "without_creator_admin"
  variables {
    bootstrap_cluster_creator_admin_permissions = false
  }
}

run "upgrade_preserves_no_creator_admin" {
  command   = plan
  state_key = "without_creator_admin"
  assert {
    condition     = aws_eks_cluster.main.access_config[0].bootstrap_cluster_creator_admin_permissions == false
    error_message = "Upgrade must not replace a cluster created without bootstrap admin."
  }
  assert {
    condition     = aws_eks_cluster.main.arn == run.legacy_cluster_without_creator_admin.cluster_arn && aws_iam_openid_connect_provider.cluster.arn == run.legacy_cluster_without_creator_admin.oidc_provider_arn
    error_message = "Cluster and OIDC identities must remain known and unchanged."
  }
}

run "upgrade_preserves_creator_admin" {
  command = plan
  assert {
    condition     = aws_eks_cluster.main.access_config[0].bootstrap_cluster_creator_admin_permissions == true
    error_message = "Upgrade must preserve the creation-only bootstrap setting."
  }
  assert {
    condition     = aws_eks_cluster.main.arn == run.legacy_cluster.cluster_arn && aws_iam_openid_connect_provider.cluster.arn == run.legacy_cluster.oidc_provider_arn
    error_message = "Cluster and OIDC identities must remain known and unchanged."
  }
}
