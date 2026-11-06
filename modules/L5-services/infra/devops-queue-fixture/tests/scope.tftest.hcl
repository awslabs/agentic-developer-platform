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
  mock_resource "aws_sqs_queue" {
    defaults = { arn = "arn:aws:sqs:us-east-1:000000000000:l5-fixture-0000000000000001" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::000000000000:role/l5-fixture-0000000000000001" }
  }
  mock_resource "aws_lambda_function" {
    defaults = { qualified_arn = "arn:aws:lambda:us-east-1:000000000000:function:l5-fixture-0000000000000001:1" }
  }
}

variables {
  aws_account_id       = "000000000000"
  aws_region           = "us-east-1"
  fixture_id           = "0000000000000001"
  source_commit        = "0000000000000000000000000000000000000001"
  worker_zip           = "tests/worker.txt"
  worker_sha256_base64 = filebase64sha256("tests/worker.txt")
}

run "bootstrap_cannot_poll_or_publish" {
  command = apply
  variables { phase = "bootstrap" }
  assert {
    condition     = length(aws_lambda_event_source_mapping.fixture) == 0 && !aws_lambda_function.worker.publish
    error_message = "Network bootstrap must not publish a callable version or create a polling mapping."
  }
  assert {
    condition = one([for s in local.bootstrap_policy.Statement : s.Condition if s.Effect == "Deny" && can(s.Condition)]) == {
      ArnEquals = { "lambda:SourceFunctionArn" = local.lambda_arn }
    }
    error_message = "Temporary ENI permission belongs to Lambda provisioning; function code cannot call network APIs."
  }
  assert {
    condition = one([for s in local.bootstrap_policy.Statement : s.Resource if try(s.Action == ["ec2:CreateNetworkInterface"], false)]) == [
      "arn:aws:ec2:us-east-1:000000000000:network-interface/*", aws_subnet.fixture.arn, aws_security_group.fixture.arn
    ]
    error_message = "ENI creation must require this fixture's exact subnet and security group."
  }
}

run "sealed_queue_only_and_disabled" {
  command = apply
  variables { phase = "sealed" }
  assert {
    condition = jsondecode(aws_iam_role_policy.worker.policy) == {
      Version = "2012-10-17"
      Statement = [
        { Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"], Resource = aws_sqs_queue.fixture.arn },
        { Effect = "Deny", NotAction = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"], Resource = "*" },
        { Effect = "Deny", Action = "*", NotResource = aws_sqs_queue.fixture.arn }
      ]
    } && aws_lambda_function.worker.publish && !aws_lambda_event_source_mapping.fixture[0].enabled
    error_message = "Sealing must remove every setup permission and retain an explicitly disabled mapping to a numeric version."
  }
  assert {
    condition = (length(aws_security_group.fixture.ingress) == 0 && length(aws_security_group.fixture.egress) == 0 &&
      !aws_vpc.fixture.enable_dns_support && !aws_vpc.fixture.enable_dns_hostnames && !aws_subnet.fixture.map_public_ip_on_launch &&
      aws_lambda_function.worker.reserved_concurrent_executions == 2 &&
    aws_lambda_event_source_mapping.fixture[0].function_name == aws_lambda_function.worker.qualified_arn)
    error_message = "The fixture must have no SG network access, public subnet behavior, or mutable mapping target."
  }
}

run "active_preserves_sealed_privilege" {
  command = apply
  variables { phase = "active" }
  assert {
    condition = (aws_iam_role_policy.worker.policy == jsonencode(local.queue_policy) &&
      aws_lambda_event_source_mapping.fixture[0].enabled &&
      aws_lambda_event_source_mapping.fixture[0].batch_size == 5 &&
    toset(aws_lambda_event_source_mapping.fixture[0].function_response_types) == toset(["ReportBatchItemFailures"]))
    error_message = "Explicit activation may only enable the bounded partial-batch mapping while keeping the sealed queue-only role."
  }
}

run "artifact_change_fails_before_deployment" {
  command = plan
  variables { worker_sha256_base64 = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=" }
  expect_failures = [aws_lambda_function.worker]
}
