provider "aws" {
  region              = var.aws_region
  allowed_account_ids = [var.aws_account_id]
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

data "aws_eks_cluster" "target" {
  name = var.cluster_name

  lifecycle {
    postcondition {
      condition = (
        data.aws_caller_identity.current.account_id == var.aws_account_id &&
        data.aws_region.current.region == var.aws_region &&
        self.arn == "arn:aws:eks:${var.aws_region}:${var.aws_account_id}:cluster/${var.cluster_name}" &&
        self.status == "ACTIVE"
      )
      error_message = "Both adapter foundations require the confirmed AWS account, region and active EKS cluster."
    }
    postcondition {
      condition = (
        can(regex("^https://[^/]+$", self.endpoint)) &&
        try(length(base64decode(self.certificate_authority[0].data)) > 0, false)
      )
      error_message = "The reviewed EKS cluster must provide its HTTPS endpoint and certificate authority."
    }
  }
}

locals {
  kubernetes_host                   = data.aws_eks_cluster.target.endpoint
  kubernetes_cluster_ca_certificate = base64decode(data.aws_eks_cluster.target.certificate_authority[0].data)
  kubernetes_exec_args = [
    "eks", "get-token", "--cluster-name", var.cluster_name,
    "--region", var.aws_region, "--output", "json",
  ]
}

# No ambient kubeconfig or manually copied endpoint selects the target. The
# endpoint and CA come from the same described cluster that gates both modules.
# AWS CLI inherits the launcher's credentials, as does the AWS provider above.
provider "kubernetes" {
  host                   = local.kubernetes_host
  cluster_ca_certificate = local.kubernetes_cluster_ca_certificate
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = local.kubernetes_exec_args
  }
}

module "publication" {
  source    = "../infra"
  providers = { aws = aws }

  aws_account_id     = var.aws_account_id
  aws_region         = var.aws_region
  environment        = var.environment
  state_bucket       = var.state_bucket
  source_kms_key_arn = var.source_kms_key_arn

  depends_on = [data.aws_eks_cluster.target, data.aws_eks_access_entry.runtime_verifier]
}

module "foundation" {
  source = "../foundation-infra"
  providers = {
    aws        = aws
    kubernetes = kubernetes
  }

  aws_account_id                 = var.aws_account_id
  aws_region                     = var.aws_region
  environment                    = var.environment
  cluster_name                   = var.cluster_name
  namespace                      = var.namespace
  oidc_provider_arn              = var.oidc_provider_arn
  runtime_role_name              = var.runtime_role_name
  atlassian_mcp_identity_enabled = var.atlassian_mcp_identity_enabled
  private_edge_enabled           = var.private_edge_enabled
  observation_runtime            = var.observation_runtime
  runtime_verifier_group         = var.runtime_verifier_group

  depends_on = [data.aws_eks_cluster.target, data.aws_eks_access_entry.runtime_verifier]
}
