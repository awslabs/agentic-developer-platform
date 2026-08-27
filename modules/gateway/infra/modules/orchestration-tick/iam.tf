# =============================================================================
# IAM for the Orchestration Tick Lambda (Issue #4203)
# =============================================================================
# Scoping follows `../budget-lambda/iam.tf`: `rds-db:connect` is granted for a
# SINGLE dbuser rather than `*`, and CloudWatch Logs is scoped to this function's
# own log group. The only addition over that precedent is PutMetricData, which
# the tick needs to report nodes examined / transitions effected / rejected /
# errors (R-NF8).
# =============================================================================

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

resource "aws_iam_role" "tick" {
  name = "${local.tick_name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })

  tags = merge(var.common_tags, {
    Name    = "${local.tick_name}-role"
    Service = "lambda"
    Purpose = "orchestration-tick"
  })
}

resource "aws_iam_role_policy" "tick" {
  name = "${local.tick_name}-policy"
  role = aws_iam_role.tick.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # RDS IAM database authentication, scoped to one dbuser. When the resource
      # id is known the ARN is fully qualified; the wildcard fallback still pins
      # the dbuser so this can never become "connect as anyone".
      {
        Sid    = "RDSConnect"
        Effect = "Allow"
        Action = [
          "rds-db:connect"
        ]
        Resource = var.rds_resource_id != "" ? "arn:aws:rds-db:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:dbuser:${var.rds_resource_id}/${var.db_username}" : "arn:aws:rds-db:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:dbuser:*/${var.db_username}"
      },
      # CloudWatch Logs, scoped to this function's own group.
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${local.tick_name}:*"
      },
      # Tick metrics (R-NF8). PutMetricData takes no resource-level condition, so
      # it is constrained by namespace instead.
      {
        Sid    = "PublishTickMetrics"
        Effect = "Allow"
        Action = [
          "cloudwatch:PutMetricData"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = "ADP/Orchestration"
          }
        }
      },
      # VPC ENI management, required for any Lambda with a vpc_config. These
      # actions do not support resource-level permissions.
      {
        Sid    = "VPCExecution"
        Effect = "Allow"
        Action = [
          "ec2:CreateNetworkInterface",
          "ec2:DescribeNetworkInterfaces",
          "ec2:DeleteNetworkInterface",
          "ec2:AssignPrivateIpAddresses",
          "ec2:UnassignPrivateIpAddresses"
        ]
        Resource = "*"
      },
      # X-Ray, matching the tracing_config mode = "Active" on the function.
      {
        Sid    = "XRayTracing"
        Effect = "Allow"
        Action = [
          "xray:PutTraceSegments",
          "xray:PutTelemetryRecords"
        ]
        Resource = "*"
      }
    ]
  })
}
