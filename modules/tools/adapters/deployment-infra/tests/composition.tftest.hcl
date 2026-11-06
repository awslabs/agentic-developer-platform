mock_provider "aws" {
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_region" { defaults = { region = "us-east-1" } }
  mock_data "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/connections/registered-verifier" } }
  mock_data "aws_eks_access_entry" { defaults = { type = "STANDARD", kubernetes_groups = ["adp:runtime-verifier-review"] } }
  mock_data "aws_eks_cluster" {
    defaults = {
      arn                   = "arn:aws:eks:us-east-1:123456789012:cluster/reviewed-cluster"
      status                = "ACTIVE"
      endpoint              = "https://reviewed-cluster.example.invalid"
      certificate_authority = [{ data = "dGVzdC1jbHVzdGVyLWNh" }]
      identity              = [{ oidc = [{ issuer = "https://oidc.eks.us-east-1.amazonaws.com/id/TESTCLUSTER" }] }]
    }
  }
  mock_data "aws_iam_openid_connect_provider" {
    defaults = {
      url            = "oidc.eks.us-east-1.amazonaws.com/id/TESTCLUSTER"
      client_id_list = ["sts.amazonaws.com"]
    }
  }
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/mock-generated-role" } }
  mock_resource "aws_ecr_repository" { defaults = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/adp-tools-dev-adapters" } }
  mock_resource "aws_cloudwatch_log_group" { defaults = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:/aws/codebuild/adp-tools-dev-adapters-build" } }
}

mock_provider "kubernetes" {}

variables {
  aws_account_id            = "123456789012"
  aws_region                = "us-east-1"
  environment               = "dev"
  state_bucket              = "fictional-private-tools-source"
  cluster_name              = "reviewed-cluster"
  namespace                 = "reviewed-adapters"
  oidc_provider_arn         = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/TESTCLUSTER"
  runtime_role_name         = "reviewed-adapter-runtime"
  runtime_verifier_group    = "adp:runtime-verifier-review"
  runtime_verifier_role_arn = "arn:aws:iam::123456789012:role/connections/registered-verifier"
}

run "composed_foundations" {
  command = apply

  assert {
    condition = (
      data.aws_iam_role.runtime_verifier.name == "registered-verifier" &&
      data.aws_eks_access_entry.runtime_verifier.cluster_name == var.cluster_name &&
      data.aws_eks_access_entry.runtime_verifier.principal_arn == var.runtime_verifier_role_arn &&
      aws_iam_role_policy.runtime_verifier.role == "registered-verifier" &&
      jsondecode(aws_iam_role_policy.runtime_verifier.policy).Statement == [
        {
          Sid      = "ReadPinnedAdapterImage"
          Effect   = "Allow"
          Action   = ["ecr:DescribeRepositories", "ecr:BatchGetImage"]
          Resource = "arn:aws:ecr:us-east-1:123456789012:repository/adp-tools-dev-adapters"
        },
        {
          Sid      = "DescribeReviewedAdapterCluster"
          Effect   = "Allow"
          Action   = "eks:DescribeCluster"
          Resource = "arn:aws:eks:us-east-1:123456789012:cluster/reviewed-cluster"
        },
        {
          Sid      = "ReadAdapterBuildRecovery"
          Effect   = "Allow"
          Action   = ["codebuild:ListBuildsForProject", "codebuild:BatchGetBuilds"]
          Resource = "arn:aws:codebuild:us-east-1:123456789012:project/adp-tools-dev-adapters-build"
        },
      ]
    )
    error_message = "The mapped verifier may only read the exact image, cluster and adapter build history, without mutation permissions."
  }

  assert {
    condition = (
      output.repository_name == "adp-tools-dev-adapters" &&
      output.build_project_name == "adp-tools-dev-adapters-build" &&
      output.foundation_check_project_name == "adp-tools-dev-adapters-foundation-check" &&
      output.foundation_check_source_prefix == "codebuild/src/adp-tools-dev-adapters-foundation-check/" &&
      output.foundation_check_artifact_prefix == "codebuild/artifacts/adp-tools-dev-adapters-foundation-check/" &&
      output.foundation_check_log_group == "/aws/codebuild/adp-tools-dev-adapters-foundation-check" &&
      output.source_prefix == "codebuild/src/adp-tools-dev-adapters-build/" &&
      output.build_log_group == "/aws/codebuild/adp-tools-dev-adapters-build" &&
      output.namespace == var.namespace &&
      output.runtime_verifier_group == var.runtime_verifier_group &&
      output.runtime_verifier_role_arn == var.runtime_verifier_role_arn &&
      output.verified_cluster_arn == "arn:aws:eks:us-east-1:123456789012:cluster/reviewed-cluster"
    )
    error_message = "Build and access foundations must expose the same reviewed environment and target."
  }
  assert {
    condition = (
      local.kubernetes_host == "https://reviewed-cluster.example.invalid" &&
      local.kubernetes_cluster_ca_certificate == "test-cluster-ca" &&
      local.kubernetes_exec_args == ["eks", "get-token", "--cluster-name", "reviewed-cluster", "--region", "us-east-1", "--output", "json"]
    )
    error_message = "The Kubernetes connection must use the reviewed cluster's endpoint, CA and exact token arguments."
  }
  assert {
    condition = (
      kubernetes_role_v1.runtime_deployer.metadata[0].namespace == var.namespace &&
      kubernetes_role_binding_v1.runtime_deployer.metadata[0].namespace == var.namespace &&
      kubernetes_role_binding_v1.runtime_deployer.role_ref[0].kind == "Role" &&
      kubernetes_role_binding_v1.runtime_deployer.role_ref[0].name == kubernetes_role_v1.runtime_deployer.metadata[0].name &&
      length(kubernetes_role_binding_v1.runtime_deployer.subject) == 1 &&
      kubernetes_role_binding_v1.runtime_deployer.subject[0].kind == "Group" &&
      kubernetes_role_binding_v1.runtime_deployer.subject[0].name == "adp:tools-release-dev"
    )
    error_message = "Only the existing protected deployment group may receive namespace-local mutation access."
  }
  assert {
    condition = (
      length(kubernetes_role_v1.runtime_deployer.rule) == 8 &&
      alltrue([for rule in kubernetes_role_v1.runtime_deployer.rule :
        length(setsubtract(toset(rule.verbs), toset(["get", "list", "watch", "create", "patch"]))) == 0
      ]) &&
      toset(flatten([for rule in kubernetes_role_v1.runtime_deployer.rule : rule.resources])) == toset([
        "serviceaccounts", "services", "configmaps", "deployments", "poddisruptionbudgets", "pods", "replicasets", "endpointslices", "services/proxy"
      ]) &&
      alltrue([for rule in kubernetes_role_v1.runtime_deployer.rule :
        (!contains(rule.verbs, "create") && !contains(rule.verbs, "patch")) ||
        (length(rule.resource_names) == 1 && alltrue([for name in rule.resource_names : contains(["tool-adapters", "tool-adapters-contract"], name)]))
      ]) &&
      toset(flatten([for rule in kubernetes_role_v1.runtime_deployer.rule : rule.resources if contains(rule.verbs, "create")])) == toset([
        "serviceaccounts", "services", "configmaps", "deployments", "poddisruptionbudgets"
      ])
    )
    error_message = "Mutation must be restricted to the five named renderer resources, with no secret, exec, token, deletion or RBAC grants."
  }
  assert {
    condition = (
      toset(flatten([for rule in kubernetes_role_v1.runtime_deployer.rule : rule.resources if contains(rule.verbs, "watch")])) == toset(["deployments"]) &&
      toset(flatten([for rule in kubernetes_role_v1.runtime_deployer.rule : rule.resources if contains(rule.verbs, "list")])) == toset(["pods", "replicasets", "endpointslices"]) &&
      toset(one([for rule in kubernetes_role_v1.runtime_deployer.rule : rule if contains(rule.resources, "configmaps")]).resource_names) == toset(["tool-adapters-contract"]) &&
      toset(one([for rule in kubernetes_role_v1.runtime_deployer.rule : rule if contains(rule.resources, "services/proxy")]).resource_names) == toset(["http:tool-adapters:80"])
    )
    error_message = "Observer and rollout permissions must match their actual API paths."
  }
}

run "foreign_verifier_account_refused" {
  command = plan
  variables {
    runtime_verifier_role_arn = "arn:aws:iam::000000000000:role/registered-verifier"
  }
  expect_failures = [var.runtime_verifier_role_arn]
}

run "wildcard_verifier_refused" {
  command = plan
  variables {
    runtime_verifier_role_arn = "arn:aws:iam::123456789012:role/registered-*"
  }
  expect_failures = [var.runtime_verifier_role_arn]
}

run "pod_role_cannot_be_verifier" {
  command = plan
  variables {
    runtime_verifier_role_arn = "arn:aws:iam::123456789012:role/reviewed-adapter-runtime"
  }
  expect_failures = [var.runtime_verifier_role_arn]
}

run "verifier_iam_path_mismatch_refused" {
  command = plan
  override_data {
    target = data.aws_iam_role.runtime_verifier
    values = { arn = "arn:aws:iam::123456789012:role/registered-verifier" }
  }
  expect_failures = [data.aws_iam_role.runtime_verifier]
}

run "verifier_without_expected_group_refused" {
  command = plan
  override_data {
    target = data.aws_eks_access_entry.runtime_verifier
    values = { type = "STANDARD", kubernetes_groups = ["adp:runtime-verifier-other"] }
  }
  expect_failures = [data.aws_eks_access_entry.runtime_verifier]
}

run "non_standard_verifier_mapping_refused" {
  command = plan
  override_data {
    target = data.aws_eks_access_entry.runtime_verifier
    values = { type = "EC2_LINUX", kubernetes_groups = ["adp:runtime-verifier-review"] }
  }
  expect_failures = [data.aws_eks_access_entry.runtime_verifier]
}

run "wrong_provider_account_refused" {
  command = plan
  override_data {
    target = data.aws_caller_identity.current
    values = { account_id = "999988887777" }
  }
  expect_failures = [data.aws_eks_cluster.target]
}

run "wrong_provider_region_refused" {
  command = plan
  override_data {
    target = data.aws_region.current
    values = { region = "eu-west-1" }
  }
  expect_failures = [data.aws_eks_cluster.target]
}

run "different_cluster_refused" {
  command = plan
  override_data {
    target = data.aws_eks_cluster.target
    values = {
      arn                   = "arn:aws:eks:us-east-1:123456789012:cluster/unreviewed-cluster"
      status                = "ACTIVE"
      endpoint              = "https://reviewed-cluster.example.invalid"
      certificate_authority = [{ data = "dGVzdC1jbHVzdGVyLWNh" }]
      identity              = [{ oidc = [{ issuer = "https://oidc.eks.us-east-1.amazonaws.com/id/TESTCLUSTER" }] }]
    }
  }
  expect_failures = [data.aws_eks_cluster.target]
}

run "inactive_cluster_refused" {
  command = plan
  override_data {
    target = data.aws_eks_cluster.target
    values = {
      arn                   = "arn:aws:eks:us-east-1:123456789012:cluster/reviewed-cluster"
      status                = "DELETING"
      endpoint              = "https://reviewed-cluster.example.invalid"
      certificate_authority = [{ data = "dGVzdC1jbHVzdGVyLWNh" }]
      identity              = [{ oidc = [{ issuer = "https://oidc.eks.us-east-1.amazonaws.com/id/TESTCLUSTER" }] }]
    }
  }
  expect_failures = [data.aws_eks_cluster.target]
}

run "non_https_endpoint_refused" {
  command = plan
  override_data {
    target = data.aws_eks_cluster.target
    values = {
      arn                   = "arn:aws:eks:us-east-1:123456789012:cluster/reviewed-cluster"
      status                = "ACTIVE"
      endpoint              = "http://reviewed-cluster.example.invalid"
      certificate_authority = [{ data = "dGVzdC1jbHVzdGVyLWNh" }]
      identity              = [{ oidc = [{ issuer = "https://oidc.eks.us-east-1.amazonaws.com/id/TESTCLUSTER" }] }]
    }
  }
  expect_failures = [data.aws_eks_cluster.target]
}

run "missing_ca_refused" {
  command = plan
  override_data {
    target = data.aws_eks_cluster.target
    values = {
      arn                   = "arn:aws:eks:us-east-1:123456789012:cluster/reviewed-cluster"
      status                = "ACTIVE"
      endpoint              = "https://reviewed-cluster.example.invalid"
      certificate_authority = [{ data = "" }]
      identity              = [{ oidc = [{ issuer = "https://oidc.eks.us-east-1.amazonaws.com/id/TESTCLUSTER" }] }]
    }
  }
  expect_failures = [data.aws_eks_cluster.target]
}
