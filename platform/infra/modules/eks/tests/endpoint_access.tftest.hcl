# Upgrade preservation: private-only clusters must stay private.
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


run "retain_private_only_endpoint" {
  command = plan
  variables {
    endpoint_public_access  = false
    endpoint_private_access = true
  }
  assert {
    condition     = aws_eks_cluster.main.vpc_config[0].endpoint_public_access == false
    error_message = "A private-only cluster must not acquire a public endpoint."
  }
  assert {
    condition     = aws_eks_cluster.main.vpc_config[0].endpoint_private_access == true
    error_message = "The private endpoint must remain enabled."
  }
}

run "retain_public_and_private_endpoints" {
  command = plan
  assert {
    condition     = aws_eks_cluster.main.vpc_config[0].endpoint_public_access && aws_eks_cluster.main.vpc_config[0].endpoint_private_access
    error_message = "Existing default deployment behavior must be retained."
  }
}
