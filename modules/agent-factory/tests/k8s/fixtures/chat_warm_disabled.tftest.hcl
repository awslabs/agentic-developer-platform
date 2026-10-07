mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_region" {
    defaults = { name = "us-east-1" }
  }
  mock_data "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/fixture-keda" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
  mock_data "aws_ssm_parameter" {
    defaults = { value = "fixture-agent-registry" }
  }
  mock_data "aws_ssm_parameters_by_path" {
    defaults = { names = [], values = [] }
  }
}
mock_provider "kubernetes" {}
mock_provider "helm" {}
mock_provider "archive" {
  mock_data "archive_file" {
    defaults = { output_base64sha256 = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=" }
  }
}

override_data {
  target = data.terraform_remote_state.platform
  values = {
    outputs = {
      eks_cluster_name           = "fixture-cluster"
      eks_cluster_endpoint       = "https://cluster.example.test"
      eks_cluster_ca_certificate = "Zml4dHVyZQ=="
      eks_oidc_issuer            = "https://oidc.eks.us-east-1.amazonaws.com/id/FIXTURE"
      eks_oidc_provider_arn      = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/FIXTURE"
      vpc_id                     = "vpc-00000000000000000"
      private_subnet_ids         = ["subnet-00000000000000000"]
    }
  }
}

run "warm_disabled" {
  command = plan
}
