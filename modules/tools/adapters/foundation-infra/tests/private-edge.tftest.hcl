mock_provider "aws" {
  mock_resource "aws_kms_key" { defaults = { arn = "arn:aws:kms:us-east-1:123456789012:key/22222222-2222-2222-2222-222222222222" } }
  mock_data "aws_lb" { defaults = { internal = true, vpc_id = "vpc-01234567", dns_name = "internal-fixture-123.us-east-1.elb.amazonaws.com" } }
  mock_data "aws_subnet" { defaults = { vpc_id = "vpc-01234567", map_public_ip_on_launch = false } }
  mock_data "aws_security_group" { defaults = { vpc_id = "vpc-01234567" } }

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
}
mock_provider "kubernetes" {
  mock_resource "kubernetes_ingress_v1" {
    defaults = {
      status = [{ load_balancer = [{ ingress = [{ hostname = "internal-fixture-123.us-east-1.elb.amazonaws.com", ip = "" }] }] }]
    }
  }
}

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

run "foundation_does_not_implicitly_expose_connectors" {
  command = plan
  assert {
    condition     = length(kubernetes_ingress_v1.adapter_private) == 0 && output.private_edge == null
    error_message = "Existing foundation deployments must retain their default private-edge configuration."
  }
}

run "private_connector_ingress" {
  command = apply
  variables { private_edge_enabled = true }
  assert {
    condition = (
      kubernetes_ingress_v1.adapter_private[0].metadata[0].namespace == var.namespace &&
      kubernetes_ingress_v1.adapter_private[0].metadata[0].name == "tool-adapters-private" &&
      kubernetes_ingress_v1.adapter_private[0].spec[0].ingress_class_name == "alb" &&
      kubernetes_ingress_v1.adapter_private[0].metadata[0].annotations["alb.ingress.kubernetes.io/scheme"] == "internal" &&
      kubernetes_ingress_v1.adapter_private[0].metadata[0].annotations["alb.ingress.kubernetes.io/target-type"] == "ip" &&
      kubernetes_ingress_v1.adapter_private[0].metadata[0].annotations["alb.ingress.kubernetes.io/listen-ports"] == jsonencode([{ HTTP = 80 }]) &&
      kubernetes_ingress_v1.adapter_private[0].metadata[0].annotations["alb.ingress.kubernetes.io/healthcheck-path"] == "/readyz" &&
      kubernetes_ingress_v1.adapter_private[0].wait_for_load_balancer
    )
    error_message = "The adapter must get its own internal ALB with the real readiness endpoint and named Service."
  }
  assert {
    condition = (
      toset([for route in kubernetes_ingress_v1.adapter_private[0].spec[0].rule[0].http[0].path : route.path]) == toset([
        "/internal/v1/tools/atlassian/mcp", "/internal/v1/tools/github/mcp",
        "/internal/v1/tools/jira/mcp", "/internal/v1/tools/slack/mcp"
      ]) &&
      length(kubernetes_ingress_v1.adapter_private[0].spec[0].rule[0].http[0].path) == 4 &&
      alltrue([for route in kubernetes_ingress_v1.adapter_private[0].spec[0].rule[0].http[0].path :
        route.path_type == "Exact" && route.backend[0].service[0].name == "tool-adapters" &&
        route.backend[0].service[0].port[0].number == 80
      ])
    )
    error_message = "Only the four exact MCP paths may reach the adapter; no core, wildcard or authorization route is exposed."
  }
  assert {
    condition = (
      output.private_edge.hostname == "internal-fixture-123.us-east-1.elb.amazonaws.com" &&
      output.private_edge.namespace == var.namespace && output.private_edge.ingress == "tool-adapters-private"
    )
    error_message = "Preparation must consume the observed ingress hostname rather than a manually supplied hostname."
  }
}

run "another_namespace_is_supported" {
  command = plan
  variables {
    private_edge_enabled = true
    namespace            = "second-adapters"
  }
  assert {
    condition     = kubernetes_ingress_v1.adapter_private[0].metadata[0].namespace == "second-adapters"
    error_message = "The private ingress must follow the selected adapter namespace."
  }
}

run "prepare_private_observation_without_activating_release" {
  command = apply
  variables {
    private_edge_enabled = true
    observation_runtime = {
      vpc_id                  = "vpc-01234567"
      subnet_ids              = ["subnet-01234567", "subnet-abcdef01"]
      core_security_group_ids = ["sg-01234567"]
      vpc_link_id             = "link123"
      adapter_alb_arn         = "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/adapter/abc123"
      core_api_id             = "core123"
      core_api_stage          = "dev"
      core_role_arn           = "arn:aws:iam::123456789012:role/core"
      assertion_key_arn       = "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111"
      registry_table          = "reviewed-registry"
      tenant_id               = "org-example"
    }
  }
  assert {
    condition = (
      length(aws_kms_key.observation) == 0 &&
      output.observation_runtime.gateway.assertion_key_arn == var.observation_runtime.assertion_key_arn &&
      length(aws_ssm_parameter.observation_gateway) == 0 &&
      output.observation_runtime.gateway.release == null &&
      length(aws_iam_role_policy.atlassian_mcp_identity) == 0 &&
      !aws_vpc_endpoint.observation["runtime"].private_dns_enabled &&
      aws_api_gateway_rest_api.observation["runtime"].endpoint_configuration[0].types == tolist(["PRIVATE"])
    )
    error_message = "Preparation must not activate a release, retrieve user tokens, expose a public API or shadow the core API DNS."
  }
  assert {
    condition = (
      length(kubernetes_ingress_v1.adapter_private[0].spec[0].rule[0].http[0].path) == 5 &&
      length(output.private_edge.paths) == 4 &&
      keys(jsondecode(aws_api_gateway_rest_api.observation["runtime"].body).paths) == ["/internal/v1/tool-adapters/observe"] &&
      jsondecode(aws_api_gateway_rest_api.observation["runtime"].body).paths["/internal/v1/tool-adapters/observe"].post["x-amazon-apigateway-auth"].type == "AWS_IAM"
    )
    error_message = "Observation must expose one exact IAM-protected path while preserving the four connector handoff paths."
  }
  assert {
    condition = (
      jsondecode(aws_api_gateway_rest_api_policy.observation["runtime"].policy).Statement[0].Effect == "Deny" &&
      jsondecode(aws_api_gateway_rest_api_policy.observation["runtime"].policy).Statement[1].Condition.ArnNotEquals["aws:PrincipalArn"] == var.observation_runtime.core_role_arn &&
      jsondecode(aws_vpc_endpoint_policy.observation["runtime"].policy).Statement[0].Principal.AWS == var.observation_runtime.core_role_arn &&
      jsondecode(aws_iam_role_policy.observation_adapter["runtime"].policy).Statement[0].Action == "kms:Verify" &&
      toset(jsondecode(aws_iam_role_policy.observation_adapter["runtime"].policy).Statement[1].Resource) == toset([
        "arn:aws:execute-api:us-east-1:123456789012:core123/dev/POST/internal/v1/tool-adapters/authorize",
        "arn:aws:execute-api:us-east-1:123456789012:core123/dev/POST/internal/v1/tool-adapters/dispatch",
        "arn:aws:execute-api:us-east-1:123456789012:core123/dev/POST/internal/v1/tool-adapters/catalogue",
        "arn:aws:execute-api:us-east-1:123456789012:core123/dev/POST/internal/v1/tool-adapters/catalogue/legacy"
      ])
    )
    error_message = "Only the selected core role may invoke observation; the adapter only verifies signatures and invokes the four exact core routes."
  }
  assert {
    condition = (
      jsondecode(aws_dynamodb_table_item.observation_adapter["runtime"].item).org_id.S == "org-example" &&
      toset(jsondecode(aws_dynamodb_table_item.observation_adapter["runtime"].item).credential_scopes.SS) == toset(["tools:adapter:observe", "tools:adapter:authorize", "tools:adapter:dispatch"])
    )
    error_message = "The adapter registry entry must be tenant-scoped and must not gain cross-tenant or worker privileges."
  }
}

run "refuse_foreign_core_role" {
  command = plan
  variables {
    private_edge_enabled = true
    observation_runtime = {
      vpc_id                  = "vpc-01234567"
      subnet_ids              = ["subnet-01234567", "subnet-abcdef01"]
      core_security_group_ids = ["sg-01234567"]
      vpc_link_id             = "link123"
      adapter_alb_arn         = "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/adapter/abc123"
      core_api_id             = "core123"
      core_api_stage          = "dev"
      core_role_arn           = "arn:aws:iam::999999999999:role/core"
      assertion_key_arn       = "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111"
      registry_table          = "reviewed-registry"
      tenant_id               = "org-example"
    }
  }
  expect_failures = [var.observation_runtime]
}

run "managed_observation_key_authorizes_only_its_runtime_callers" {
  command = apply
  variables {
    private_edge_enabled = true
    observation_runtime = {
      vpc_id                  = "vpc-01234567"
      subnet_ids              = ["subnet-01234567", "subnet-abcdef01"]
      core_security_group_ids = ["sg-01234567"]
      vpc_link_id             = "link123"
      adapter_alb_arn         = "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/adapter/abc123"
      core_api_id             = "core123"
      core_api_stage          = "dev"
      core_role_arn           = "arn:aws:iam::123456789012:role/core"
      registry_table          = "reviewed-registry"
      tenant_id               = "org-example"
    }
  }
  assert {
    condition = (
      length(aws_kms_key.observation) == 1 &&
      aws_kms_key.observation["runtime"].key_usage == "SIGN_VERIFY" &&
      aws_kms_key.observation["runtime"].customer_master_key_spec == "RSA_2048" &&
      output.observation_runtime.gateway.assertion_key_arn == aws_kms_key.observation["runtime"].arn &&
      jsondecode(aws_iam_role_policy.observation_core["runtime"].policy).Statement[0].Resource == aws_kms_key.observation["runtime"].arn &&
      jsondecode(aws_iam_role_policy.observation_adapter["runtime"].policy).Statement[0].Resource == aws_kms_key.observation["runtime"].arn &&
      length(aws_ssm_parameter.observation_gateway) == 0
    )
    error_message = "Both runtime identities and deployment output must select the dedicated key without activating a release."
  }
  assert {
    condition = (
      length(jsondecode(aws_kms_key.observation["runtime"].policy).Statement) == 3 &&
      !contains(jsondecode(aws_kms_key.observation["runtime"].policy).Statement[0].Action, "kms:Sign") &&
      !contains(jsondecode(aws_kms_key.observation["runtime"].policy).Statement[0].Action, "kms:Verify") &&
      !contains(jsondecode(aws_kms_key.observation["runtime"].policy).Statement[0].Action, "kms:*") &&
      jsondecode(aws_kms_key.observation["runtime"].policy).Statement[1].Principal.AWS == var.observation_runtime.core_role_arn &&
      toset(jsondecode(aws_kms_key.observation["runtime"].policy).Statement[1].Action) == toset(["kms:Sign", "kms:Verify"]) &&
      jsondecode(aws_kms_key.observation["runtime"].policy).Statement[1].Condition.StringEquals["kms:SigningAlgorithm"] == "RSASSA_PSS_SHA_256" &&
      jsondecode(aws_kms_key.observation["runtime"].policy).Statement[2].Principal.AWS == aws_iam_role.runtime.arn &&
      jsondecode(aws_kms_key.observation["runtime"].policy).Statement[2].Action == "kms:Verify" &&
      jsondecode(aws_kms_key.observation["runtime"].policy).Statement[2].Condition.StringEquals["kms:SigningAlgorithm"] == "RSASSA_PSS_SHA_256"
    )
    error_message = "Key policy must authorize only core signing and core/adapter verification with the protocol's exact algorithm; account IAM gets administration only."
  }
}
