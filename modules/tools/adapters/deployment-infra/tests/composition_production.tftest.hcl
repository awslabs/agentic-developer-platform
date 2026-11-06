# Separate state from development: this models a different deployment, not an
# account switch against the existing development Terraform state.
mock_provider "aws" {
  mock_data "aws_caller_identity" { defaults = { account_id = "111122223333" } }
  mock_data "aws_region" { defaults = { region = "eu-west-1" } }
  mock_data "aws_iam_role" { defaults = { arn = "arn:aws:iam::111122223333:role/registered-production-verifier" } }
  mock_data "aws_eks_access_entry" { defaults = { type = "STANDARD", kubernetes_groups = ["adp:runtime-verifier-production"] } }
  mock_data "aws_eks_cluster" {
    defaults = {
      arn                   = "arn:aws:eks:eu-west-1:111122223333:cluster/production-cluster"
      status                = "ACTIVE"
      endpoint              = "https://production-cluster.example.invalid"
      certificate_authority = [{ data = "cHJvZHVjdGlvbi1jbHVzdGVyLWNh" }]
      identity              = [{ oidc = [{ issuer = "https://oidc.eks.eu-west-1.amazonaws.com/id/PRODCLUSTER" }] }]
    }
  }
  mock_data "aws_iam_openid_connect_provider" {
    defaults = {
      url            = "oidc.eks.eu-west-1.amazonaws.com/id/PRODCLUSTER"
      client_id_list = ["sts.amazonaws.com"]
    }
  }
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::111122223333:role/mock-generated-role" } }
  mock_resource "aws_ecr_repository" { defaults = { arn = "arn:aws:ecr:eu-west-1:111122223333:repository/adp-tools-prod-adapters" } }
  mock_resource "aws_cloudwatch_log_group" { defaults = { arn = "arn:aws:logs:eu-west-1:111122223333:log-group:/aws/codebuild/adp-tools-prod-adapters-build" } }
}

mock_provider "kubernetes" {}

run "second_account_region_and_environment" {
  command = apply
  variables {
    aws_account_id            = "111122223333"
    aws_region                = "eu-west-1"
    environment               = "prod"
    state_bucket              = "fictional-production-tools-source"
    cluster_name              = "production-cluster"
    namespace                 = "production-adapters"
    oidc_provider_arn         = "arn:aws:iam::111122223333:oidc-provider/oidc.eks.eu-west-1.amazonaws.com/id/PRODCLUSTER"
    runtime_role_name         = "production-adapter-runtime"
    runtime_verifier_group    = "adp:runtime-verifier-production"
    runtime_verifier_role_arn = "arn:aws:iam::111122223333:role/registered-production-verifier"
  }
  assert {
    condition = (
      output.repository_name == "adp-tools-prod-adapters" &&
      output.build_project_name == "adp-tools-prod-adapters-build" &&
      output.foundation_check_project_name == "adp-tools-prod-adapters-foundation-check" &&
      output.foundation_check_source_prefix == "codebuild/src/adp-tools-prod-adapters-foundation-check/" &&
      output.foundation_check_artifact_prefix == "codebuild/artifacts/adp-tools-prod-adapters-foundation-check/" &&
      output.foundation_check_log_group == "/aws/codebuild/adp-tools-prod-adapters-foundation-check" &&
      output.source_prefix == "codebuild/src/adp-tools-prod-adapters-build/" &&
      output.namespace == "production-adapters" &&
      output.runtime_verifier_group == "adp:runtime-verifier-production" &&
      output.runtime_verifier_role_arn == "arn:aws:iam::111122223333:role/registered-production-verifier" &&
      jsondecode(aws_iam_role_policy.runtime_verifier.policy).Statement[0].Resource == "arn:aws:ecr:eu-west-1:111122223333:repository/adp-tools-prod-adapters" &&
      jsondecode(aws_iam_role_policy.runtime_verifier.policy).Statement[1].Resource == "arn:aws:eks:eu-west-1:111122223333:cluster/production-cluster" &&
      jsondecode(aws_iam_role_policy.runtime_verifier.policy).Statement[2].Resource == "arn:aws:codebuild:eu-west-1:111122223333:project/adp-tools-prod-adapters-build" &&
      kubernetes_role_v1.runtime_deployer.metadata[0].namespace == "production-adapters" &&
      kubernetes_role_binding_v1.runtime_deployer.subject[0].name == "adp:tools-release-prod" &&
      output.verified_cluster_arn == "arn:aws:eks:eu-west-1:111122223333:cluster/production-cluster" &&
      local.kubernetes_host == "https://production-cluster.example.invalid" &&
      local.kubernetes_cluster_ca_certificate == "production-cluster-ca" &&
      local.kubernetes_exec_args == ["eks", "get-token", "--cluster-name", "production-cluster", "--region", "eu-west-1", "--output", "json"]
    )
    error_message = "Neither child module nor Kubernetes authentication may remain bound to the development target."
  }
}
