mock_provider "aws" {
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_region" { defaults = { region = "us-east-1" } }
  mock_data "aws_eks_cluster" {
    defaults = {
      arn      = "arn:aws:eks:us-east-1:123456789012:cluster/reviewed-cluster"
      status   = "ACTIVE"
      identity = [{ oidc = [{ issuer = "https://oidc.eks.us-east-1.amazonaws.com/id/TESTCLUSTER" }] }]
    }
  }
  mock_data "aws_iam_openid_connect_provider" {
    defaults = {
      url            = "oidc.eks.us-east-1.amazonaws.com/id/TESTCLUSTER"
      client_id_list = ["sts.amazonaws.com"]
    }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/reviewed-adapter-runtime" }
  }
  mock_data "aws_secretsmanager_secret" {
    defaults = {
      arn        = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/dev/connections/atlassian-mcp/client-ABC123"
      kms_key_id = ""
    }
  }
  mock_data "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-east-1:123456789012:key/33333333-3333-4333-8333-333333333333" }
  }
}
mock_provider "kubernetes" {}

variables {
  aws_account_id         = "123456789012"
  aws_region             = "us-east-1"
  environment            = "dev"
  cluster_name           = "reviewed-cluster"
  namespace              = "reviewed-adapters"
  oidc_provider_arn      = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/TESTCLUSTER"
  runtime_role_name      = "reviewed-adapter-runtime"
  runtime_verifier_group = "adp:runtime-verifier-review"
}

run "foundation_access" {
  command = apply
  assert {
    condition = (
      length(aws_iam_role_policy.atlassian_mcp_identity) == 0 &&
      length(data.aws_secretsmanager_secret.atlassian_mcp_client) == 0 &&
      kubernetes_namespace_v1.adapter.metadata[0].name == var.namespace &&
      kubernetes_namespace_v1.adapter.metadata[0].labels["pod-security.kubernetes.io/enforce"] == "restricted" &&
      output.namespace == var.namespace &&
      output.runtime_role_arn == aws_iam_role.runtime.arn &&
      output.verified_cluster_arn == data.aws_eks_cluster.target.arn
    )
    error_message = "Renderer outputs and namespace protection must match the reviewed target."
  }
  assert {
    condition = (
      length(jsondecode(aws_iam_role.runtime.assume_role_policy).Statement) == 1 &&
      jsondecode(aws_iam_role.runtime.assume_role_policy).Statement[0].Effect == "Allow" &&
      jsondecode(aws_iam_role.runtime.assume_role_policy).Statement[0].Action == "sts:AssumeRoleWithWebIdentity" &&
      jsondecode(aws_iam_role.runtime.assume_role_policy).Statement[0].Principal.Federated == var.oidc_provider_arn &&
      jsondecode(aws_iam_role.runtime.assume_role_policy).Statement[0].Condition.StringEquals == {
        "oidc.eks.us-east-1.amazonaws.com/id/TESTCLUSTER:aud" = "sts.amazonaws.com"
        "oidc.eks.us-east-1.amazonaws.com/id/TESTCLUSTER:sub" = "system:serviceaccount:reviewed-adapters:tool-adapters"
      }
    )
    error_message = "Only the exact adapter ServiceAccount and STS audience may assume the foundation role."
  }
  assert {
    condition = (
      kubernetes_role_v1.verifier.metadata[0].namespace == var.namespace &&
      kubernetes_role_binding_v1.verifier.metadata[0].namespace == var.namespace &&
      kubernetes_role_binding_v1.verifier.role_ref[0].kind == "Role" &&
      kubernetes_role_binding_v1.verifier.role_ref[0].name == kubernetes_role_v1.verifier.metadata[0].name &&
      length(kubernetes_role_binding_v1.verifier.subject) == 1 &&
      kubernetes_role_binding_v1.verifier.subject[0].kind == "Group" &&
      kubernetes_role_binding_v1.verifier.subject[0].name == var.runtime_verifier_group
    )
    error_message = "Verifier access must remain namespaced and bound to the selected existing group."
  }
  assert {
    condition = (
      length(kubernetes_role_v1.verifier.rule) == 8 &&
      alltrue([for rule in kubernetes_role_v1.verifier.rule :
        length(setsubtract(toset(rule.verbs), toset(["get", "list"]))) == 0
      ]) &&
      toset(flatten([for rule in kubernetes_role_v1.verifier.rule : rule.resources])) == toset([
        "serviceaccounts", "services", "configmaps", "deployments", "poddisruptionbudgets", "pods", "replicasets", "endpointslices", "services/proxy"
      ]) &&
      toset(flatten([for rule in kubernetes_role_v1.verifier.rule : rule.resources if contains(rule.verbs, "list")])) == toset([
        "pods", "replicasets", "endpointslices"
      ]) &&
      alltrue([for rule in kubernetes_role_v1.verifier.rule :
        length(rule.resource_names) > 0 || contains(rule.verbs, "list")
      ])
    )
    error_message = "The verifier needs only canonical reads; secret, exec, token and mutation permissions must be absent."
  }
  assert {
    condition = (
      toset(one([for rule in kubernetes_role_v1.verifier.rule : rule if contains(rule.resources, "serviceaccounts")]).resource_names) == toset(["tool-adapters"]) &&
      toset(one([for rule in kubernetes_role_v1.verifier.rule : rule if contains(rule.resources, "configmaps")]).resource_names) == toset(["tool-adapters-contract"]) &&
      toset(one([for rule in kubernetes_role_v1.verifier.rule : rule if contains(rule.resources, "deployments")]).resource_names) == toset(["tool-adapters"]) &&
      toset(one([for rule in kubernetes_role_v1.verifier.rule : rule if contains(rule.resources, "poddisruptionbudgets")]).resource_names) == toset(["tool-adapters"]) &&
      toset(one([for rule in kubernetes_role_v1.verifier.rule : rule if contains(rule.resources, "services/proxy")]).resource_names) == toset(["http:tool-adapters:80"]) &&
      toset(one([for rule in kubernetes_role_v1.verifier.rule : rule if contains(rule.resources, "services/proxy")]).verbs) == toset(["get"])
    )
    error_message = "Named resource and service-proxy reads must match the actual canonical observer paths."
  }
}

run "qualified_mcp_identity_is_narrow" {
  command = plan
  variables { atlassian_mcp_identity_enabled = true }
  assert {
    condition = (
      length(aws_iam_role_policy.atlassian_mcp_identity) == 1 &&
      aws_iam_role_policy.atlassian_mcp_identity[0].role == aws_iam_role.runtime.id &&
      jsondecode(aws_iam_role_policy.atlassian_mcp_identity[0].policy).Statement == [
        {
          Sid    = "McpUserWorkloadToken"
          Effect = "Allow"
          Action = ["bedrock-agentcore:GetWorkloadAccessTokenForUserId"]
          Resource = [
            "arn:aws:bedrock-agentcore:us-east-1:123456789012:workload-identity-directory/default",
            "arn:aws:bedrock-agentcore:us-east-1:123456789012:workload-identity-directory/default/workload-identity/adp-tools-dev-atlassian-mcp-oauth-user"
          ]
        },
        {
          Sid    = "McpUserProviderToken"
          Effect = "Allow"
          Action = ["bedrock-agentcore:GetResourceOauth2Token"]
          Resource = [
            "arn:aws:bedrock-agentcore:us-east-1:123456789012:workload-identity-directory/default",
            "arn:aws:bedrock-agentcore:us-east-1:123456789012:workload-identity-directory/default/workload-identity/adp-tools-dev-atlassian-mcp-oauth-user",
            "arn:aws:bedrock-agentcore:us-east-1:123456789012:token-vault/default",
            "arn:aws:bedrock-agentcore:us-east-1:123456789012:token-vault/default/oauth2credentialprovider/adp-tools-dev-atlassian-mcp-user"
          ]
        },
        {
          Sid      = "McpExternalClientSecret"
          Effect   = "Allow"
          Action   = ["secretsmanager:GetSecretValue"]
          Resource = ["arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/dev/connections/atlassian-mcp/client-ABC123"]
          Condition = {
            "ForAnyValue:StringEquals" = {
              "aws:CalledVia" = "bedrock-agentcore.amazonaws.com"
            }
          }
        }
      ]
    )
    error_message = "The enabled runtime may retrieve scoped Identity tokens and only its external client secret through AgentCore; direct secret access stays denied."
  }
}

run "client_secret_kms_decryption_is_scoped" {
  command = plan
  variables { atlassian_mcp_identity_enabled = true }
  override_data {
    target = data.aws_secretsmanager_secret.atlassian_mcp_client[0]
    values = {
      arn        = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/dev/connections/atlassian-mcp/client-ABC123"
      kms_key_id = "arn:aws:kms:us-east-1:123456789012:key/33333333-3333-4333-8333-333333333333"
    }
  }
  assert {
    condition = (
      length(jsondecode(aws_iam_role_policy.atlassian_mcp_identity[0].policy).Statement) == 4 &&
      jsondecode(aws_iam_role_policy.atlassian_mcp_identity[0].policy).Statement[3] == {
        Sid      = "DecryptMcpExternalClientSecret"
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = ["arn:aws:kms:us-east-1:123456789012:key/33333333-3333-4333-8333-333333333333"]
        Condition = {
          StringEquals = {
            "kms:ViaService"                  = "secretsmanager.us-east-1.amazonaws.com"
            "kms:EncryptionContext:SecretARN" = ["arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/dev/connections/atlassian-mcp/client-ABC123"]
          }
        }
      }
    )
    error_message = "Customer-key decryption must be limited to this secret through Secrets Manager."
  }
}

run "repeat_access_is_retained" {
  command = apply
  assert {
    condition     = output.namespace == run.foundation_access.namespace && output.runtime_role_arn == run.foundation_access.runtime_role_arn
    error_message = "Rerunning unchanged foundation inputs must retain the namespace and runtime identity."
  }
}

run "another_environment_is_explicit" {
  command = plan
  variables {
    namespace              = "second-adapters"
    runtime_role_name      = "second-adapter-runtime"
    runtime_verifier_group = "adp:runtime-verifier-second"
  }
  assert {
    condition = (
      kubernetes_namespace_v1.adapter.metadata[0].name == "second-adapters" &&
      aws_iam_role.runtime.name == "second-adapter-runtime" &&
      jsondecode(aws_iam_role.runtime.assume_role_policy).Statement[0].Condition.StringEquals["oidc.eks.us-east-1.amazonaws.com/id/TESTCLUSTER:sub"] == "system:serviceaccount:second-adapters:tool-adapters" &&
      kubernetes_role_binding_v1.verifier.subject[0].name == "adp:runtime-verifier-second"
    )
    error_message = "No namespace, identity or verifier group may be hardcoded to a development environment."
  }
}

run "wrong_account_refused" {
  command = plan
  variables { aws_account_id = "111122223333" }
  expect_failures = [aws_iam_role.runtime]
}

run "wrong_region_refused" {
  command = plan
  variables { aws_region = "eu-west-1" }
  expect_failures = [aws_iam_role.runtime]
}

run "different_cluster_refused" {
  command = plan
  variables { cluster_name = "different-cluster" }
  expect_failures = [aws_iam_role.runtime]
}

run "different_cluster_oidc_refused" {
  command = plan
  variables { oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/OTHERCLUSTER" }
  expect_failures = [aws_iam_role.runtime]
}

run "different_iam_provider_issuer_refused" {
  command = plan
  override_data {
    target = data.aws_iam_openid_connect_provider.target
    values = {
      url            = "oidc.eks.us-east-1.amazonaws.com/id/OTHERCLUSTER"
      client_id_list = ["sts.amazonaws.com"]
    }
  }
  expect_failures = [aws_iam_role.runtime]
}

run "sts_audience_required" {
  command = plan
  override_data {
    target = data.aws_iam_openid_connect_provider.target
    values = {
      url            = "oidc.eks.us-east-1.amazonaws.com/id/TESTCLUSTER"
      client_id_list = ["unrelated.example"]
    }
  }
  expect_failures = [aws_iam_role.runtime]
}

run "inactive_cluster_refused" {
  command = plan
  override_data {
    target = data.aws_eks_cluster.target
    values = {
      arn      = "arn:aws:eks:us-east-1:123456789012:cluster/reviewed-cluster"
      status   = "DELETING"
      identity = [{ oidc = [{ issuer = "https://oidc.eks.us-east-1.amazonaws.com/id/TESTCLUSTER" }] }]
    }
  }
  expect_failures = [aws_iam_role.runtime]
}

run "system_namespace_refused" {
  command = plan
  variables { namespace = "kube-system" }
  expect_failures = [var.namespace]
}

run "system_group_refused" {
  command = plan
  variables { runtime_verifier_group = "system:masters" }
  expect_failures = [var.runtime_verifier_group]
}

run "system_jira_identity_is_separate_and_environment_scoped" {
  command = plan
  variables { atlassian_mcp_identity_enabled = true }
  assert {
    condition = (
      length(aws_iam_role_policy.jira_system_identity) == 1 &&
      jsondecode(aws_iam_role_policy.jira_system_identity[0].policy).Statement[0].Action == ["bedrock-agentcore:GetWorkloadAccessToken"] &&
      jsondecode(aws_iam_role_policy.jira_system_identity[0].policy).Statement[1].Action == ["bedrock-agentcore:GetResourceApiKey"] &&
      jsondecode(aws_iam_role_policy.jira_system_identity[0].policy).Statement[1].Resource == [
        "arn:aws:bedrock-agentcore:us-east-1:123456789012:workload-identity-directory/default",
        "arn:aws:bedrock-agentcore:us-east-1:123456789012:workload-identity-directory/default/workload-identity/adp-tools-dev-atlassian-mcp-system",
        "arn:aws:bedrock-agentcore:us-east-1:123456789012:token-vault/default",
        "arn:aws:bedrock-agentcore:us-east-1:123456789012:token-vault/default/apikeycredentialprovider/adp-tools-dev-atlassian-mcp-system-*"
      ]
    )
    error_message = "System Jira keys require the distinct system workload and environment-scoped API-key provider resources."
  }
}
