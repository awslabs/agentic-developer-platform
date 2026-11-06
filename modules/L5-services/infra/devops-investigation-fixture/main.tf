# Separate investigation fixture. Never adds logging to the queue-only fixture.
locals {
  name        = "l5-investigation-${var.fixture_id}"
  lambda_arn  = "arn:aws:lambda:${var.aws_region}:${var.aws_account_id}:function:${local.name}"
  log_arn     = "arn:aws:logs:${var.aws_region}:${var.aws_account_id}:log-group:/aws/lambda/${local.name}:*"
  space_arn   = "arn:aws:aidevops:${var.aws_region}:${var.aws_account_id}:agentspace/${var.native_space_id}"
  network_arn = "arn:aws:ec2:${var.aws_region}:${var.aws_account_id}"
  log_actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
  network_actions = [
    "ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets",
    "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses"
  ]
  sealed_policy = {
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = local.log_actions, Resource = local.log_arn },
      { Effect = "Deny", NotAction = local.log_actions, Resource = "*" },
      { Effect = "Deny", Action = "*", NotResource = local.log_arn }
    ]
  }
  bootstrap_policy = {
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = local.log_actions, Resource = local.log_arn },
      { Effect = "Allow", Action = ["ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets"], Resource = "*" },
      { Effect = "Allow", Action = ["ec2:CreateNetworkInterface"], Resource = [
        "${local.network_arn}:network-interface/*", aws_subnet.fixture.arn, aws_security_group.fixture.arn
      ] },
      { Effect = "Allow", Action = ["ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses"],
        Resource = "${local.network_arn}:network-interface/*", Condition = { ArnEquals = {
          "ec2:Subnet" = aws_subnet.fixture.arn, "ec2:Vpc" = aws_vpc.fixture.arn
      } } },
      { Effect = "Deny", NotAction = concat(local.log_actions, local.network_actions), Resource = "*" },
      { Effect = "Deny", Action = local.network_actions, Resource = "*", Condition = {
        ArnEquals = { "lambda:SourceFunctionArn" = local.lambda_arn }
      } }
    ]
  }
}

# No internet gateway, route, NAT, endpoint, peering, DNS, ingress or egress.
resource "aws_vpc" "fixture" {
  cidr_block           = "10.254.241.0/24"
  enable_dns_support   = false
  enable_dns_hostnames = false
}
resource "aws_subnet" "fixture" {
  vpc_id                  = aws_vpc.fixture.id
  cidr_block              = "10.254.241.0/28"
  map_public_ip_on_launch = false
}
resource "aws_security_group" "fixture" {
  name        = local.name
  description = "Synthetic investigation fixture: no network access"
  vpc_id      = aws_vpc.fixture.id
  ingress     = []
  egress      = []
}
resource "aws_cloudwatch_log_group" "fixture" {
  name              = "/aws/lambda/${local.name}"
  retention_in_days = 1
}
resource "aws_iam_role" "worker" {
  name = local.name
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}
resource "aws_iam_role_policy" "worker" {
  name   = "exact-fixture-log-writes"
  role   = aws_iam_role.worker.id
  policy = var.phase == "bootstrap" ? jsonencode(local.bootstrap_policy) : jsonencode(local.sealed_policy)
}
resource "aws_lambda_function" "worker" {
  function_name                  = local.name
  role                           = aws_iam_role.worker.arn
  runtime                        = "nodejs22.x"
  handler                        = "index.handler"
  timeout                        = 5
  memory_size                    = 128
  reserved_concurrent_executions = var.phase == "active" ? 1 : 0
  filename                       = var.worker_zip
  source_code_hash               = var.worker_sha256_base64
  publish                        = var.phase != "bootstrap"
  tags                           = { "adp:source-commit" = var.source_commit, "adp:fixture-purpose" = "synthetic-investigation" }
  vpc_config {
    subnet_ids                  = [aws_subnet.fixture.id]
    security_group_ids          = [aws_security_group.fixture.id]
    ipv6_allowed_for_dual_stack = false
  }
  lifecycle {
    precondition {
      condition     = filebase64sha256(var.worker_zip) == var.worker_sha256_base64
      error_message = "Prepared artifact bytes changed. Rebuild and review the immutable input."
    }
  }
  depends_on = [aws_iam_role_policy.worker, aws_cloudwatch_log_group.fixture]
}
# Distinct from both the Lambda execution role and the ADP setup connection.
resource "aws_iam_role" "resource_reader" {
  name = "${local.name}-reader"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "aidevops.amazonaws.com" },
      Condition = { StringEquals = { "aws:SourceAccount" = var.aws_account_id }, ArnEquals = { "aws:SourceArn" = local.space_arn } }
    }]
  })
}
resource "aws_iam_role_policy" "resource_reader" {
  name = "exact-fixture-resource-reads"
  role = aws_iam_role.resource_reader.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "lambda:GetFunction", Resource = local.lambda_arn },
      { Effect = "Allow", Action = "logs:FilterLogEvents", Resource = local.log_arn }
    ]
  })
}
output "fixture" {
  value = {
    phase                = var.phase
    account_id           = var.aws_account_id
    region               = var.aws_region
    function_arn         = local.lambda_arn
    function_version_arn = var.phase == "bootstrap" ? null : aws_lambda_function.worker.qualified_arn
    log_group_name       = aws_cloudwatch_log_group.fixture.name
    telemetry_arn        = local.log_arn
    resource_role_arn    = aws_iam_role.resource_reader.arn
    execution_role_arn   = aws_iam_role.worker.arn
    native_space_id      = var.native_space_id
    vpc_id               = aws_vpc.fixture.id
    subnet_id            = aws_subnet.fixture.id
    security_group_id    = aws_security_group.fixture.id
    source_commit        = var.source_commit
    code_sha256_base64   = var.worker_sha256_base64
    synthetic            = true
  }
}
