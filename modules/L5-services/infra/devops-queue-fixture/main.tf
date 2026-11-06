# Dedicated customer fixture, never part of an ADP service installation.
# Bootstrap permits Lambda's control plane to provision its private ENI. Seal
# removes that temporary authority before any event source exists or runs.
locals {
  name          = "l5-fixture-${var.fixture_id}"
  lambda_arn    = "arn:aws:lambda:${var.aws_region}:${var.aws_account_id}:function:${local.name}"
  network_arn   = "arn:aws:ec2:${var.aws_region}:${var.aws_account_id}"
  queue_actions = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
  network_actions = [
    "ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets",
    "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses"
  ]
  queue_policy = {
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = local.queue_actions, Resource = aws_sqs_queue.fixture.arn },
      { Effect = "Deny", NotAction = local.queue_actions, Resource = "*" },
      { Effect = "Deny", Action = "*", NotResource = aws_sqs_queue.fixture.arn }
    ]
  }
  bootstrap_policy = {
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = local.queue_actions, Resource = aws_sqs_queue.fixture.arn },
      { Effect = "Allow", Action = ["ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets"], Resource = "*" },
      { Effect = "Allow", Action = ["ec2:CreateNetworkInterface"], Resource = [
        "${local.network_arn}:network-interface/*", aws_subnet.fixture.arn, aws_security_group.fixture.arn
      ] },
      { Effect = "Allow", Action = ["ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses"],
        Resource = "${local.network_arn}:network-interface/*", Condition = { ArnEquals = {
          "ec2:Subnet" = aws_subnet.fixture.arn, "ec2:Vpc" = aws_vpc.fixture.arn
      } } },
      { Effect = "Deny", NotAction = concat(local.queue_actions, local.network_actions), Resource = "*" },
      # Lambda service provisioning is separate from calls by function code.
      { Effect = "Deny", Action = local.network_actions, Resource = "*", Condition = {
        ArnEquals = { "lambda:SourceFunctionArn" = local.lambda_arn }
      } }
    ]
  }
}

# No gateway, NAT, endpoint, peering, or route to any other network is created.
resource "aws_vpc" "fixture" {
  cidr_block           = "10.254.240.0/24"
  enable_dns_support   = false
  enable_dns_hostnames = false
}

resource "aws_subnet" "fixture" {
  vpc_id                  = aws_vpc.fixture.id
  cidr_block              = "10.254.240.0/28"
  map_public_ip_on_launch = false
}

resource "aws_security_group" "fixture" {
  name        = local.name
  description = "Synthetic fixture: no ingress or egress"
  vpc_id      = aws_vpc.fixture.id
  ingress     = []
  egress      = []
}

resource "aws_sqs_queue" "fixture" {
  name                       = local.name
  sqs_managed_sse_enabled    = true
  visibility_timeout_seconds = 180
  message_retention_seconds  = 86400
}

resource "aws_iam_role" "worker" {
  name = local.name
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "worker" {
  name   = "scoped-queue-read"
  role   = aws_iam_role.worker.id
  policy = var.phase == "bootstrap" ? jsonencode(local.bootstrap_policy) : jsonencode(local.queue_policy)
}

resource "aws_lambda_function" "worker" {
  function_name                  = local.name
  role                           = aws_iam_role.worker.arn
  runtime                        = "nodejs22.x"
  handler                        = "index.handler"
  timeout                        = 30
  memory_size                    = 128
  reserved_concurrent_executions = 2
  filename                       = var.worker_zip
  source_code_hash               = var.worker_sha256_base64
  publish                        = var.phase != "bootstrap"
  tags                           = { "adp:source-commit" = var.source_commit }
  vpc_config {
    subnet_ids                  = [aws_subnet.fixture.id]
    security_group_ids          = [aws_security_group.fixture.id]
    ipv6_allowed_for_dual_stack = false
  }
  lifecycle {
    precondition {
      condition     = filebase64sha256(var.worker_zip) == var.worker_sha256_base64
      error_message = "The prepared artifact bytes changed. Rebuild and review the immutable input."
    }
  }
  depends_on = [aws_iam_role_policy.worker]
}

resource "aws_lambda_event_source_mapping" "fixture" {
  count                   = var.phase == "bootstrap" ? 0 : 1
  event_source_arn        = aws_sqs_queue.fixture.arn
  function_name           = aws_lambda_function.worker.qualified_arn
  batch_size              = 5
  function_response_types = ["ReportBatchItemFailures"]
  enabled                 = var.phase == "active"
}

output "fixture" {
  value = {
    phase                = var.phase
    account_id           = var.aws_account_id
    region               = var.aws_region
    function_version_arn = var.phase == "bootstrap" ? null : aws_lambda_function.worker.qualified_arn
    execution_role_arn   = aws_iam_role.worker.arn
    queue_arn            = aws_sqs_queue.fixture.arn
    queue_url            = aws_sqs_queue.fixture.url
    vpc_id               = aws_vpc.fixture.id
    subnet_id            = aws_subnet.fixture.id
    security_group_id    = aws_security_group.fixture.id
    mapping_id           = try(aws_lambda_event_source_mapping.fixture[0].uuid, null)
    source_commit        = var.source_commit
    code_sha256_base64   = var.worker_sha256_base64
  }
}
