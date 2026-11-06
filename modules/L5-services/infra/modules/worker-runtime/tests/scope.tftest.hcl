mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "000000000000" }
  }
  mock_data "aws_region" {
    defaults = { region = "us-east-1" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::000000000000:role/example-l5-worker" }
  }
  mock_resource "aws_lambda_function" {
    defaults = { arn = "arn:aws:lambda:us-east-1:000000000000:function:adp-dev-l5-worker" }
  }
  mock_resource "aws_cloudwatch_event_rule" {
    defaults = { arn = "arn:aws:events:us-east-1:000000000000:rule/adp-dev-l5-repair" }
  }
  mock_resource "aws_sqs_queue" {
    defaults = {
      arn = "arn:aws:sqs:us-east-1:000000000000:adp-dev-l5-work"
      url = "https://sqs.us-east-1.amazonaws.com/000000000000/adp-dev-l5-work"
    }
  }
}

override_resource {
  target = aws_iam_role.worker
  values = { arn = "arn:aws:iam::000000000000:role/adp-dev-l5-worker" }
}

override_resource {
  target = aws_iam_role.repair
  values = { arn = "arn:aws:iam::000000000000:role/adp-dev-l5-repair" }
}

variables {
  environment                = "dev"
  vpc_id                     = "vpc-00000000000000000"
  private_subnet_ids         = ["subnet-00000000000000000"]
  db_security_group_id       = "sg-00000000000000000"
  endpoint_security_group_id = "sg-00000000000000000"
  db_port                    = 5432
  permissions_boundary_arn   = "arn:aws:iam::000000000000:policy/example-l5-boundary"
  worker_artifact_bucket     = "example-l5-artifacts"
  worker_artifact_key        = "l5/security/example/workers.zip"
  worker_artifact_version    = "version-1"
  worker_artifact_sha256     = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
  worker_factory             = "l5_services.runtime:factory"
  gateway_api_arn            = "arn:aws:execute-api:us-east-1:000000000000:exampleapi/*/*/internal/*"
  rds_connect_arn            = "arn:aws:rds-db:us-east-1:000000000000:dbuser:db-resource/bgadmin"
}

run "narrow_runtime_policy" {
  command = apply

  assert {
    condition = (
      aws_iam_role.worker.permissions_boundary == var.permissions_boundary_arn &&
      aws_iam_role_policy.worker.name == aws_iam_role.worker.name &&
      aws_iam_role_policy.repair.name == aws_iam_role.repair.name &&
      aws_iam_role.repair.permissions_boundary == var.permissions_boundary_arn &&
      jsondecode(aws_iam_role.worker.assume_role_policy).Statement[0].Principal.Service == "lambda.amazonaws.com" &&
      jsondecode(aws_iam_role.repair.assume_role_policy).Statement[0].Principal.Service == "lambda.amazonaws.com" &&
      aws_lambda_function.repair.role == aws_iam_role.repair.arn &&
      aws_lambda_function.worker.role == aws_iam_role.worker.arn
    )
    error_message = "Both Lambda roles need the protected boundary and independent execution identity."
  }

  assert {
    condition = (
      length(jsondecode(aws_iam_role_policy.worker.policy).Statement) == 7 &&
      length(jsondecode(aws_iam_role_policy.repair.policy).Statement) == 6 &&
      sort(jsondecode(aws_iam_role_policy.worker.policy).Statement[1].Action) == sort(["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility", "sqs:SendMessage"]) &&
      jsondecode(aws_iam_role_policy.worker.policy).Statement[1].Resource == aws_sqs_queue.work.arn &&
      jsondecode(aws_iam_role_policy.repair.policy).Statement[1].Action == ["sqs:SendMessage"] &&
      jsondecode(aws_iam_role_policy.repair.policy).Statement[1].Resource == aws_sqs_queue.work.arn
    )
    error_message = "Queue consumer needs only its queue; repair may send but never receive, delete, or use another queue."
  }

  assert {
    condition = (
      sort(jsondecode(aws_iam_role_policy.worker.policy).Statement[0].Action) == sort(["logs:CreateLogStream", "logs:PutLogEvents"]) &&
      sort(jsondecode(aws_iam_role_policy.repair.policy).Statement[0].Action) == sort(["logs:CreateLogStream", "logs:PutLogEvents"]) &&
      jsondecode(aws_iam_role_policy.worker.policy).Statement[0].Resource == "arn:aws:logs:us-east-1:000000000000:log-group:/aws/lambda/adp-dev-l5-worker:*" &&
      jsondecode(aws_iam_role_policy.repair.policy).Statement[0].Resource == "arn:aws:logs:us-east-1:000000000000:log-group:/aws/lambda/adp-dev-l5-repair:*"
    )
    error_message = "Logs must be restricted to each Lambda's dedicated log group."
  }

  assert {
    condition = (
      jsondecode(aws_iam_role_policy.worker.policy).Statement[2].Resource == var.rds_connect_arn &&
      jsondecode(aws_iam_role_policy.repair.policy).Statement[2].Resource == var.rds_connect_arn &&
      aws_lambda_function.worker.environment[0].variables.L5_RDS_CONNECT_ARN == var.rds_connect_arn &&
      aws_lambda_function.repair.environment[0].variables.L5_RDS_CONNECT_ARN == var.rds_connect_arn &&
      jsondecode(aws_iam_role_policy.worker.policy).Statement[3].Resource == var.gateway_api_arn &&
      jsondecode(aws_iam_role_policy.worker.policy).Statement[4].Resource == "arn:aws:s3:::example-l5-artifacts/l5/security/work/*" &&
      aws_vpc_security_group_egress_rule.gateway_api.from_port == 443 &&
      aws_vpc_security_group_egress_rule.gateway_api.to_port == 443
    )
    error_message = "The installed worker needs only its database user, selected Gateway and owned archives."
  }

  assert {
    condition = (
      sort(jsondecode(aws_iam_role_policy.worker.policy).Statement[5].Action) == sort(["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses"]) &&
      jsondecode(aws_iam_role_policy.worker.policy).Statement[5].Effect == "Allow" &&
      jsondecode(aws_iam_role_policy.worker.policy).Statement[5].Resource == "*" &&
      sort(jsondecode(aws_iam_role_policy.repair.policy).Statement[3].Action) == sort(["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses"]) &&
      jsondecode(aws_iam_role_policy.repair.policy).Statement[3].Effect == "Allow" &&
      jsondecode(aws_iam_role_policy.repair.policy).Statement[3].Resource == "*" &&
      jsondecode(aws_iam_role_policy.worker.policy).Statement[6].Effect == "Deny" &&
      jsondecode(aws_iam_role_policy.worker.policy).Statement[6].Resource == "*" &&
      jsondecode(aws_iam_role_policy.worker.policy).Statement[6].Action == jsondecode(aws_iam_role_policy.worker.policy).Statement[5].Action &&
      jsondecode(aws_iam_role_policy.worker.policy).Statement[6].Condition.ArnEquals["lambda:SourceFunctionArn"] == "arn:aws:lambda:us-east-1:000000000000:function:adp-dev-l5-worker" &&
      jsondecode(aws_iam_role_policy.repair.policy).Statement[4].Effect == "Deny" &&
      jsondecode(aws_iam_role_policy.repair.policy).Statement[4].Resource == "*" &&
      jsondecode(aws_iam_role_policy.repair.policy).Statement[4].Action == jsondecode(aws_iam_role_policy.repair.policy).Statement[3].Action &&
      jsondecode(aws_iam_role_policy.repair.policy).Statement[4].Condition.ArnEquals["lambda:SourceFunctionArn"] == "arn:aws:lambda:us-east-1:000000000000:function:adp-dev-l5-repair"
    )
    error_message = "Lambda service ENI permissions may be wildcarded only when function code is explicitly denied them."
  }
}

run "reject_missing_boundary" {
  command = plan
  variables { permissions_boundary_arn = "" }
  expect_failures = [var.permissions_boundary_arn]
}

run "repair_has_only_its_own_consumer_paths" {
  command = apply
  variables { service_id = "finops" }
  assert {
    condition = (
      jsondecode(aws_iam_role_policy.repair.policy).Statement[5].Action == ["execute-api:Invoke"] &&
      toset(jsondecode(aws_iam_role_policy.repair.policy).Statement[5].Resource) == toset([
        "arn:aws:execute-api:us-east-1:000000000000:exampleapi/*/POST/internal/v1/context/l5-cost-contributions",
        "arn:aws:execute-api:us-east-1:000000000000:exampleapi/*/POST/internal/v1/context/l5-cost-contributions/*/invalidate",
        "arn:aws:execute-api:us-east-1:000000000000:exampleapi/*/GET/internal/v1/context/l5-cost-contributions/*",
        "arn:aws:execute-api:us-east-1:000000000000:exampleapi/*/POST/internal/v1/insights/l5-cost-contributions",
        "arn:aws:execute-api:us-east-1:000000000000:exampleapi/*/POST/internal/v1/insights/l5-cost-contributions/*/invalidate",
        "arn:aws:execute-api:us-east-1:000000000000:exampleapi/*/GET/internal/v1/insights/l5-cost-contributions/*"
      ])
    )
    error_message = "Repair cannot call provider credentials, submissions, another service, or arbitrary internal routes."
  }
}

run "wa_repair_adds_only_the_schedule_admission_path" {
  command = plan
  variables { service_id = "well_architected" }
  assert {
    condition = (
      length(jsondecode(aws_iam_role_policy.repair.policy).Statement[5].Resource) == 7 &&
      contains(jsondecode(aws_iam_role_policy.repair.policy).Statement[5].Resource,
      "arn:aws:execute-api:us-east-1:000000000000:exampleapi/*/POST/internal/v1/l5/well-architected/schedule-admissions") &&
      !contains(jsondecode(aws_iam_role_policy.repair.policy).Statement[5].Resource,
      "arn:aws:execute-api:us-east-1:000000000000:exampleapi/*/*/internal/*")
    )
    error_message = "WA repair may request stored-event admission, without general credential or internal API access."
  }
}

run "transform_repair_adds_only_the_schedule_admission_path" {
  command = plan
  variables { service_id = "transform" }
  assert {
    condition = (
      length(jsondecode(aws_iam_role_policy.repair.policy).Statement[5].Resource) == 7 &&
      contains(jsondecode(aws_iam_role_policy.repair.policy).Statement[5].Resource,
      "arn:aws:execute-api:us-east-1:000000000000:exampleapi/*/POST/internal/v1/l5/transform/schedule-admissions") &&
      !contains(jsondecode(aws_iam_role_policy.repair.policy).Statement[5].Resource,
      "arn:aws:execute-api:us-east-1:000000000000:exampleapi/*/*/internal/*")
    )
    error_message = "Transform repair may request stored-event admission, without general credential or internal API access."
  }
}

run "security_full_repository_resource_bounds" {
  command = plan
  assert {
    condition = (
      aws_lambda_function.worker.timeout == 900 &&
      aws_lambda_function.worker.memory_size == 1536 &&
      aws_lambda_function.worker.ephemeral_storage[0].size == 4096 &&
      aws_lambda_function_recursion_config.worker.function_name == aws_lambda_function.worker.function_name &&
      aws_lambda_function_recursion_config.worker.recursive_loop == "Allow" &&
      aws_sqs_queue.work.visibility_timeout_seconds >= 6 * aws_lambda_function.worker.timeout &&
      aws_lambda_function.repair.timeout == 25 &&
      aws_lambda_event_source_mapping.work.enabled == false
    )
    error_message = "Security full repository preparation needs bounded scratch, memory and matching queue visibility; qualification remains required."
  }
}

run "sibling_worker_resource_bounds_unchanged" {
  command = plan
  variables { service_id = "finops" }
  assert {
    condition = (
      aws_lambda_function.worker.timeout == 25 &&
      aws_lambda_function.worker.memory_size == 512 &&
      aws_lambda_function.worker.ephemeral_storage[0].size == 512 &&
      aws_lambda_function_recursion_config.worker.recursive_loop == "Allow" &&
      aws_sqs_queue.work.visibility_timeout_seconds == 180
    )
    error_message = "Security and DevOps settings must not alter other service workers."
  }
}

run "devops_multi_step_verification_and_collection" {
  command = plan
  variables { service_id = "devops" }
  assert {
    condition = (
      aws_lambda_function.worker.timeout == 120 &&
      aws_lambda_function.worker.timeout < 300 &&
      aws_sqs_queue.work.visibility_timeout_seconds >= 6 * aws_lambda_function.worker.timeout &&
      aws_lambda_function.worker.memory_size == 512 &&
      aws_lambda_function.repair.timeout == 25 &&
      aws_lambda_event_source_mapping.work.enabled == false
    )
    error_message = "DevOps resource verification and report collection need a bounded two-minute invocation, within the five-minute setup lease and with matching queue visibility."
  }
}
