mock_provider "aws" {}
mock_provider "time" {}

variables {
  environment           = "dev"
  name_prefix           = "adp-dev"
  aws_region            = "us-east-1"
  image_uri             = "123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-gateway:release"
  vpc_id                = "vpc-00000000000000000"
  private_subnet_ids    = ["subnet-00000000000000001"]
  rds_security_group_id = "sg-00000000000000000"
  db_host               = "db.example.internal"
  db_name               = "gateway"
  db_username           = "bgadmin"
}

override_data {
  target = data.aws_caller_identity.current
  values = { account_id = "123456789012" }
}

override_data {
  target = data.aws_region.current
  values = { name = "us-east-1" }
}

override_resource {
  target          = aws_sns_topic.alerts
  override_during = plan
  values = {
    arn = "arn:aws:sns:us-east-1:123456789012:adp-dev-orchestration-tick-alerts"
  }
}

run "complete_lambda_vpc_permissions" {
  command = plan
  assert {
    condition = alltrue([
      for action in ["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets",
      "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses"] :
      contains(one([for statement in jsondecode(aws_iam_role_policy.tick.policy).Statement : statement.Action if statement.Sid == "VPCExecution"]), action)
    ])
    error_message = "The execution role must include the complete Lambda VPC permission set, including DescribeSubnets."
  }
}

run "product_story_dispatch_defaults_off" {
  command = plan
  assert {
    condition     = aws_lambda_function.tick.environment[0].variables.ADP_PRODUCT_STORY_DISPATCH_ENABLED == "false"
    error_message = "Product-bound dispatch must remain disabled without explicit rollout authority."
  }
}

run "product_story_dispatch_is_an_independent_explicit_rollout" {
  command = plan
  variables {
    product_story_dispatch_enabled = true
  }
  assert {
    condition     = aws_lambda_function.tick.environment[0].variables.ADP_PRODUCT_STORY_DISPATCH_ENABLED == "true" && aws_lambda_function.tick.environment[0].variables.AGENT_AUTHORITY_ENABLED == "false"
    error_message = "The explicit Product dispatch setting must not alter separate authority migration controls."
  }
}

run "shared_reporting_signing_key_is_parameter_reference_only" {
  command = plan
  variables {
    agent_authority_enabled            = false
    shared_run_reporting_enabled       = true
    shared_worker_continuation_enabled = false
    shared_worker_role_arn             = "arn:aws:iam::123456789012:role/existing-worker"
    run_report_key_parameter           = "/adp/dev/gateway/run-report-signing-key"
  }
  assert {
    condition     = aws_lambda_function.tick.environment[0].variables.ADP_SHARED_RUN_REPORTING_ENABLED == "true" && aws_lambda_function.tick.environment[0].variables.AGENT_AUTHORITY_ENABLED == "false" && aws_lambda_function.tick.environment[0].variables.AGENT_WORKER_ROLE_ARN == "arn:aws:iam::123456789012:role/existing-worker"
    error_message = "The tick must enable reporting without enabling protected authority or changing worker roles."
  }
  assert {
    condition     = aws_lambda_function.tick.environment[0].variables.AGENT_RUN_CREDENTIAL_KEY_PARAMETER == "/adp/dev/gateway/run-report-signing-key" && !contains(keys(aws_lambda_function.tick.environment[0].variables), "AGENT_RUN_CREDENTIAL_KEY")
    error_message = "Lambda configuration may carry a parameter reference, never signing-key plaintext."
  }
}

run "redis_disabled_does_not_add_access" {
  command = plan
  assert {
    condition     = length(aws_iam_role_policy.tick_redis) == 0 && length(aws_security_group_rule.redis_from_tick) == 0 && !contains(keys(aws_lambda_function.tick.environment[0].variables), "BG_REDIS_URL")
    error_message = "A deployment without Redis must not create Redis access or empty connection settings."
  }
}

run "reuse_gateway_redis_with_exact_network_and_iam_scope" {
  command = plan
  variables {
    redis_enabled           = true
    redis_host              = "existing.cache.amazonaws.com"
    redis_port              = 6379
    redis_security_group_id = "sg-00000000000000003"
    redis_iam_auth          = true
    redis_username          = "existing-user"
    redis_cache_name        = "existing-cache"
  }
  assert {
    condition     = aws_lambda_function.tick.environment[0].variables.BG_REDIS_URL == "rediss://existing.cache.amazonaws.com:6379/0" && aws_lambda_function.tick.environment[0].variables.BG_REDIS_IAM_AUTH == "true" && aws_lambda_function.tick.environment[0].variables.BG_REDIS_USERNAME == "existing-user" && aws_lambda_function.tick.environment[0].variables.BG_REDIS_CACHE_NAME == "existing-cache"
    error_message = "The tick must use the exact existing gateway Redis endpoint and IAM identity."
  }
  assert {
    condition     = length(aws_security_group.tick.egress) == 3 && length([for rule in aws_security_group.tick.egress : rule if rule.protocol == "tcp" && rule.from_port == 6379 && rule.to_port == 6379 && rule.security_groups == toset(["sg-00000000000000003"]) && length(coalesce(rule.cidr_blocks, [])) == 0]) == 1
    error_message = "Add only one Redis-port egress rule to the existing Redis security group."
  }
  assert {
    condition     = aws_security_group_rule.redis_from_tick[0].security_group_id == "sg-00000000000000003" && aws_security_group_rule.redis_from_tick[0].type == "ingress" && aws_security_group_rule.redis_from_tick[0].from_port == 6379 && aws_security_group_rule.redis_from_tick[0].to_port == 6379 && aws_security_group_rule.redis_from_tick[0].protocol == "tcp"
    error_message = "Reciprocal access must target only the existing Redis port and security group."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.tick_redis[0].policy) == jsondecode(jsonencode({ Version = "2012-10-17", Statement = [{ Sid = "ConnectSharedBudgetRedis", Effect = "Allow", Action = ["elasticache:Connect"], Resource = ["arn:aws:elasticache:us-east-1:123456789012:replicationgroup:existing-cache", "arn:aws:elasticache:us-east-1:123456789012:user:existing-user"] }] }))
    error_message = "Redis IAM scope must contain only Connect for the gateway replication group and existing IAM user."
  }
}

run "operations_default_off" {
  command = plan
  assert {
    condition     = aws_lambda_function.tick.environment[0].variables.ADP_ENGINE_OPERATIONS_EXECUTOR == "false"
    error_message = "Operations must remain disabled by default."
  }
}

run "operations_require_reporting" {
  command = plan
  variables {
    engine_operations_executor_enabled = true
    agent_authority_enabled            = true
    shared_run_reporting_enabled       = false
    webhook_events_table_name          = "webhook-events"
    webhook_events_kms_key_arn         = "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111"
  }
  expect_failures = [var.engine_operations_executor_enabled]
}

run "operations_enable_tick" {
  command = plan
  variables {
    engine_operations_executor_enabled = true
    agent_authority_enabled            = true
    shared_run_reporting_enabled       = true
    webhook_events_table_name          = "webhook-events"
    webhook_events_kms_key_arn         = "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111"
  }
  assert {
    condition     = aws_lambda_function.tick.environment[0].variables.ADP_ENGINE_OPERATIONS_EXECUTOR == "true"
    error_message = "The scheduled engine must receive the same operations opt-in."
  }
}


override_data {
  target = data.aws_partition.current
  values = { partition = "aws" }
}

run "delivery_reconciliation_can_read_tenant_app_without_command_bridge" {
  command = plan
  variables {
    engine_enabled = true
  }
  assert {
    condition = one([for s in jsondecode(aws_iam_role_policy.tick.policy).Statement : s if s.Sid == "EngineCommandAckCredentials"]) == jsondecode(jsonencode({
      Sid      = "EngineCommandAckCredentials", Effect = "Allow", Action = ["secretsmanager:GetSecretValue"],
      Resource = ["arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/dev/tenants/*/github-app-??????"]
    }))
    error_message = "Engine reconciliation needs only tenant App credential reads, independently of command bridge inputs."
  }
}

run "disabled_engine_does_not_gain_secret_access" {
  command = plan
  variables {
    engine_enabled               = false
    agent_authority_enabled      = false
    shared_run_reporting_enabled = false
  }
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.tick.policy).Statement : s if s.Sid == "EngineCommandAckCredentials"]) == 0
    error_message = "An inactive engine must not gain a default secret read grant."
  }
}

run "shared_reporting_has_delivery_credential_access" {
  command = plan
  variables {
    engine_enabled               = false
    shared_run_reporting_enabled = true
  }
  assert {
    condition     = one([for s in jsondecode(aws_iam_role_policy.tick.policy).Statement : s.Resource if s.Sid == "EngineCommandAckCredentials"]) == ["arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/dev/tenants/*/github-app-??????"]
    error_message = "Shared delivery reporting must not depend on optional command bridge configuration."
  }
}

run "explicit_tenant_credential_scope_is_preserved" {
  command = plan
  variables {
    engine_enabled                = true
    github_app_secret_arn_pattern = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/dev/tenants/example/github-app-AbCdEf"
  }
  assert {
    condition     = one([for s in jsondecode(aws_iam_role_policy.tick.policy).Statement : s.Resource if s.Sid == "EngineCommandAckCredentials"]) == [var.github_app_secret_arn_pattern]
    error_message = "Explicit operator scope must not be broadened by the fallback."
  }
}


run "review_artifacts_are_bound_to_configured_bucket" {
  command = plan
  variables {
    environment                = "staging"
    agent_run_logs_bucket_name = "example-staging-agent-run-logs"
  }
  assert {
    condition     = aws_lambda_function.tick.environment[0].variables.ENVIRONMENT == "staging" && aws_lambda_function.tick.environment[0].variables.AGENT_RUN_LOGS_BUCKET == "example-staging-agent-run-logs"
    error_message = "Credential and review-artifact readers must receive the actual environment and bucket reference."
  }
  assert {
    condition = one([for s in jsondecode(aws_iam_role_policy.tick.policy).Statement : s if s.Sid == "ReadVerifiedReviewArtifacts"]) == jsondecode(jsonencode({
      Sid      = "ReadVerifiedReviewArtifacts", Effect = "Allow", Action = ["s3:GetObject"],
      Resource = ["arn:aws:s3:::example-staging-agent-run-logs/runs/*/*/attempt-*/review-result/*.json"]
    }))
    error_message = "The tick may read only review-result objects in the configured bucket, with no writes or transcript reads."
  }
}

run "unconfigured_review_bucket_grants_no_access" {
  command = plan
  assert {
    condition     = !contains(keys(aws_lambda_function.tick.environment[0].variables), "AGENT_RUN_LOGS_BUCKET") && length([for s in jsondecode(aws_iam_role_policy.tick.policy).Statement : s if s.Sid == "ReadVerifiedReviewArtifacts"]) == 0
    error_message = "An unconfigured module must not invent a bucket or grant artifact reads."
  }
}
