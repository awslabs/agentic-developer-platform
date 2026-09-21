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
