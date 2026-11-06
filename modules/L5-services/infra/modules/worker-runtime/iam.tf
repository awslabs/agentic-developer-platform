data "aws_partition" "current" {}
data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  contribution_suffix = {
    security  = "l5-contributions", devops = "l5-devops-contributions", finops = "l5-cost-contributions",
    transform = "l5-transform-contributions", well_architected = "l5-wa-contributions"
  }[var.service_id]
  vpc_network_actions = ["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses"]
  worker_function_arn = "arn:${data.aws_partition.current.partition}:lambda:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:function:${local.name_prefix}-worker"
  repair_function_arn = "arn:${data.aws_partition.current.partition}:lambda:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:function:${local.name_prefix}-repair"
  lambda_trust_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role" "worker" {
  name                 = "${local.name_prefix}-worker"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy   = local.lambda_trust_policy
}

resource "aws_iam_role" "repair" {
  name                 = "${local.name_prefix}-repair"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy   = local.lambda_trust_policy
}

resource "aws_iam_role_policy" "worker" {
  name = aws_iam_role.worker.name
  role = aws_iam_role.worker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:${data.aws_partition.current.partition}:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:log-group:${aws_cloudwatch_log_group.worker.name}:*"
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility", "sqs:SendMessage"]
        Resource = aws_sqs_queue.work.arn
      },
      {
        Effect   = "Allow"
        Action   = ["rds-db:connect"]
        Resource = var.rds_connect_arn
      },
      {
        Effect   = "Allow"
        Action   = ["execute-api:Invoke"]
        Resource = var.gateway_api_arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion"]
        Resource = "arn:${data.aws_partition.current.partition}:s3:::${var.worker_artifact_bucket}/l5/${var.service_id}/work/*"
      },
      {
        Effect   = "Allow"
        Action   = local.vpc_network_actions
        Resource = "*"
      },
      {
        Effect    = "Deny"
        Action    = local.vpc_network_actions
        Resource  = "*"
        Condition = { ArnEquals = { "lambda:SourceFunctionArn" = local.worker_function_arn } }
      }
    ]
  })
}

resource "aws_iam_role_policy" "repair" {
  name = aws_iam_role.repair.name
  role = aws_iam_role.repair.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:${data.aws_partition.current.partition}:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:log-group:${aws_cloudwatch_log_group.repair.name}:*"
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = aws_sqs_queue.work.arn
      },
      {
        Effect   = "Allow"
        Action   = ["rds-db:connect"]
        Resource = var.rds_connect_arn
      },
      {
        Effect   = "Allow"
        Action   = local.vpc_network_actions
        Resource = "*"
      },
      {
        Effect    = "Deny"
        Action    = local.vpc_network_actions
        Resource  = "*"
        Condition = { ArnEquals = { "lambda:SourceFunctionArn" = local.repair_function_arn } }
      },
      {
        Effect = "Allow"
        Action = ["execute-api:Invoke"]
        Resource = concat(flatten([for consumer in(var.service_id == "security" ? ["context"] : ["context", "insights"]) : [
          "${trimsuffix(var.gateway_api_arn, "/*/*/internal/*")}/*/POST/internal/v1/${consumer}/${local.contribution_suffix}",
          "${trimsuffix(var.gateway_api_arn, "/*/*/internal/*")}/*/POST/internal/v1/${consumer}/${local.contribution_suffix}/*/invalidate",
          "${trimsuffix(var.gateway_api_arn, "/*/*/internal/*")}/*/GET/internal/v1/${consumer}/${local.contribution_suffix}/*"
          ]]), contains(["well_architected", "transform", "security"], var.service_id) ? [
          "${trimsuffix(var.gateway_api_arn, "/*/*/internal/*")}/*/POST/internal/v1/l5/${replace(var.service_id, "_", "-")}/schedule-admissions"
        ] : [])
      }
    ]
  })
}
