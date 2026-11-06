mock_provider "aws" {
  mock_resource "aws_vpc" {
    defaults = { id = "vpc-00000000000000001", arn = "arn:aws:ec2:us-east-1:000000000000:vpc/vpc-00000000000000001" }
  }
  mock_resource "aws_subnet" {
    defaults = { id = "subnet-00000000000000001", arn = "arn:aws:ec2:us-east-1:000000000000:subnet/subnet-00000000000000001" }
  }
  mock_resource "aws_security_group" {
    defaults = { id = "sg-00000000000000001", arn = "arn:aws:ec2:us-east-1:000000000000:security-group/sg-00000000000000001" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::000000000000:role/l5-investigation-0000000000000001" }
  }
  mock_resource "aws_lambda_function" {
    defaults = { qualified_arn = "arn:aws:lambda:us-east-1:000000000000:function:l5-investigation-0000000000000001:1" }
  }
}
variables {
  aws_account_id       = "000000000000"
  aws_region           = "us-east-1"
  fixture_id           = "0000000000000001"
  native_space_id      = "00000000-0000-0000-0000-000000000001"
  source_commit        = "0000000000000000000000000000000000000001"
  worker_zip           = "tests/worker.txt"
  worker_sha256_base64 = filebase64sha256("tests/worker.txt")
}
run "bootstrap_cannot_invoke_or_publish" {
  command = apply
  variables { phase = "bootstrap" }
  assert {
    condition     = aws_lambda_function.worker.reserved_concurrent_executions == 0 && !aws_lambda_function.worker.publish
    error_message = "Bootstrap must block invocation and avoid publishing a numeric version."
  }
  assert {
    condition = one([for s in local.bootstrap_policy.Statement : s.Condition if s.Effect == "Deny" && can(s.Condition)]) == {
      ArnEquals = { "lambda:SourceFunctionArn" = local.lambda_arn }
    }
    error_message = "Temporary ENI permission must be denied to function code."
  }
}
run "sealed_log_writes_only_and_not_invocable" {
  command = apply
  variables { phase = "sealed" }
  assert {
    condition = jsondecode(aws_iam_role_policy.worker.policy) == {
      Version = "2012-10-17"
      Statement = [
        { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = local.log_arn },
        { Effect = "Deny", NotAction = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "*" },
        { Effect = "Deny", Action = "*", NotResource = local.log_arn }
      ]
    } && aws_lambda_function.worker.reserved_concurrent_executions == 0 && aws_lambda_function.worker.publish
    error_message = "Sealed execution must retain only exact-group log writes and remain uninvocable."
  }
  assert {
    condition = (length(aws_security_group.fixture.ingress) == 0 && length(aws_security_group.fixture.egress) == 0 &&
      !aws_vpc.fixture.enable_dns_support && !aws_vpc.fixture.enable_dns_hostnames && !aws_subnet.fixture.map_public_ip_on_launch &&
      aws_cloudwatch_log_group.fixture.retention_in_days == 1 && aws_lambda_function.worker.timeout == 5 &&
    aws_lambda_function.worker.memory_size == 128)
    error_message = "Fixture must retain isolation and finite compute/log retention."
  }
  assert {
    condition = jsondecode(aws_iam_role.resource_reader.assume_role_policy).Statement == [{
      Effect    = "Allow", Action = "sts:AssumeRole", Principal = { Service = "aidevops.amazonaws.com" },
      Condition = { StringEquals = { "aws:SourceAccount" = var.aws_account_id }, ArnEquals = { "aws:SourceArn" = local.space_arn } }
      }] && jsondecode(aws_iam_role_policy.resource_reader.policy).Statement == [
      { Effect = "Allow", Action = "lambda:GetFunction", Resource = local.lambda_arn },
      { Effect = "Allow", Action = "logs:FilterLogEvents", Resource = local.log_arn }
    ]
    error_message = "Native role must trust only the exact space and read only the selected Lambda/log resources."
  }
}
run "active_keeps_sealed_authority" {
  command = apply
  variables { phase = "active" }
  assert {
    condition = (aws_iam_role_policy.worker.policy == jsonencode(local.sealed_policy) &&
    aws_lambda_function.worker.reserved_concurrent_executions == 1 && aws_lambda_function.worker.publish)
    error_message = "Activation may only permit one concurrent invocation while retaining sealed log-only authority."
  }
}
run "changed_artifact_fails" {
  command = plan
  variables { worker_sha256_base64 = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=" }
  expect_failures = [aws_lambda_function.worker]
}
