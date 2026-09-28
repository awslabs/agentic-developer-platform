mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_region" {
    defaults = { region = "us-east-1" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws", dns_suffix = "amazonaws.com" }
  }
  mock_data "aws_eks_cluster" {
    defaults = {
      endpoint              = "https://cluster.example"
      certificate_authority = [{ data = "Zml4dHVyZQ==" }]
      vpc_config            = [{ vpc_id = "vpc-fixture", cluster_security_group_id = "sg-cluster", endpoint_private_access = true }]
    }
  }
  mock_data "aws_subnet" {
    defaults = { vpc_id = "vpc-fixture", map_public_ip_on_launch = false }
  }
}
mock_provider "kubernetes" {}
variables {
  agent_registry_table_name = "fixture-registry"
  api_execution_arn         = "arn:aws:execute-api:us-east-1:123456789012:fixture"
  rest_api_id               = "fixture"
  stage_name                = "test"
  tools_parent_resource_id  = "tools"
  authority_endpoint        = "https://fixture.execute-api.us-east-1.amazonaws.com/test/internal/v1/agent/task"
  worker_role_arns          = ["arn:aws:iam::123456789012:role/worker"]
  cluster_name              = "fixture"
  subnet_ids                = ["subnet-fixture"]
  image_uri                 = "123456789012.dkr.ecr.us-east-1.amazonaws.com/validation@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
run "disabled_has_no_runtime_or_grants" {
  command = plan
  assert {
    condition     = length(aws_lambda_function.service) == 0 && length(aws_iam_role_policy.worker) == 0 && length(aws_eks_access_entry.service) == 0
    error_message = "Disabled service must provision no runtime or grants."
  }
}
run "requires_qualified_isolation" {
  command = plan
  variables { enabled = true }
  expect_failures = [aws_lambda_function.service]
}
run "dedicated_iam_and_bounded_runtime" {
  command = plan
  variables {
    enabled             = true
    isolation_qualified = true
  }
  assert {
    condition     = aws_api_gateway_method.service[0].authorization == "AWS_IAM" && aws_lambda_function.service[0].timeout == 180 && aws_lambda_function_event_invoke_config.service[0].maximum_retry_attempts == 0
    error_message = "Validation must require IAM and bound asynchronous execution without blind retries."
  }
  assert {
    condition     = aws_lambda_function.service[0].environment[0].variables["ADP_VALIDATION_SERVICE_ENABLED"] == "false"
    error_message = "Provisioning must not enable the capability."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.worker["arn:aws:iam::123456789012:role/worker"].policy).Statement == [{ Effect = "Allow", Action = "execute-api:Invoke", Resource = "arn:aws:execute-api:us-east-1:123456789012:fixture/test/POST/tools/validation" }]
    error_message = "Workers get exactly one API route, never cluster, Lambda or job-store authority."
  }
  assert {
    condition     = aws_eks_access_entry.service[0].kubernetes_groups == toset(["adp-validation-service"]) && kubernetes_role_binding.service[0].metadata[0].namespace == "adp-codex-validation"
    error_message = "Dedicated service group must be scoped to the validation namespace."
  }
}

run "registry_grants_no_models" {
  command = plan
  override_resource {
    target          = aws_iam_role.service[0]
    override_during = plan
    values          = { arn = "arn:aws:iam::123456789012:role/validation-service" }
  }
  variables {
    enabled             = true
    isolation_qualified = true
  }
  assert {
    condition     = length(aws_dynamodb_table_item.registry) == 1 && jsondecode(aws_dynamodb_table_item.registry[0].item).allowed_models.L == []
    error_message = "Service must provision its gateway identity with the dedicated IAM role."
  }
}

run "reject_cross_account_worker" {
  command = plan
  variables {
    enabled             = true
    isolation_qualified = true
    worker_role_arns    = ["arn:aws:iam::999999999999:role/worker"]
  }
  expect_failures = [aws_lambda_function.service]
}

run "private_cluster_network_binding" {
  command = plan
  variables {
    enabled             = true
    isolation_qualified = true
  }
  assert {
    condition     = aws_security_group.service[0].vpc_id == "vpc-fixture" && aws_vpc_security_group_ingress_rule.eks_api[0].security_group_id == "sg-cluster" && aws_vpc_security_group_ingress_rule.eks_api[0].from_port == 443 && aws_vpc_security_group_ingress_rule.eks_api[0].to_port == 443 && aws_vpc_security_group_ingress_rule.eks_api[0].ip_protocol == "tcp"
    error_message = "EKS must admit only HTTPS from the dedicated service group."
  }
  assert {
    condition     = aws_lambda_function.service[0].environment[0].variables["ADP_VALIDATION_CLUSTER_ENDPOINT"] == "https://cluster.example" && aws_lambda_function.service[0].environment[0].variables["ADP_VALIDATION_CLUSTER_CA"] == "Zml4dHVyZQ=="
    error_message = "EKS endpoint and trust must come from the selected live cluster."
  }
}
run "reject_other_authority_api" {
  command = plan
  variables {
    enabled             = true
    isolation_qualified = true
    authority_endpoint  = "https://other.execute-api.us-east-1.amazonaws.com/test/internal/v1/agent/task"
  }
  expect_failures = [aws_lambda_function.service]
}
run "reject_other_vpc_subnet" {
  command = plan
  variables {
    enabled             = true
    isolation_qualified = true
  }
  override_data {
    target = data.aws_subnet.validation["subnet-fixture"]
    values = { vpc_id = "vpc-other", map_public_ip_on_launch = false }
  }
  expect_failures = [aws_lambda_function.service]
}
run "reject_public_only_cluster" {
  command = plan
  variables {
    enabled             = true
    isolation_qualified = true
  }
  override_data {
    target = data.aws_eks_cluster.validation[0]
    values = { vpc_config = [{ vpc_id = "vpc-fixture", cluster_security_group_id = "sg-cluster", endpoint_private_access = false }] }
  }
  expect_failures = [aws_lambda_function.service]
}
