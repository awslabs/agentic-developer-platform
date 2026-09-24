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
  permissions_boundary = var.automation_permissions_boundary_arn
  name                 = "${local.tick_name}-role"

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
    # `concat` rather than one literal list because the engine-command bridge's
    # three statements are conditional on the webhook-ingress state having been
    # wired (#4527). An unwired environment must get NO statement rather than one
    # with an empty resource, which is an invalid policy — so the bridge being
    # inert is expressible in IAM, not just in code.
    Statement = concat([
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
      # Stall/halt alert delivery (Issue #4211, R-Q9d). Scoped to this module's own
      # topic — the engine can notify operators about its own graph and nothing
      # else. Without this statement the publish fails with AuthorizationError,
      # which `stall.py` records as `notifications_failed` and surfaces as a
      # non-success tick rather than swallowing.
      {
        Sid    = "PublishOrchestrationAlerts"
        Effect = "Allow"
        Action = [
          "sns:Publish"
        ]
        Resource = aws_sns_topic.alerts.arn
      },
      # Engine dispatch onto the agent-submit FIFO queue (Issue #4313, ruling
      # docs/design-notes/4303-engine-genesis-transport.md).
      #
      # This makes the tick a SECOND producer on that queue. The safety argument
      # is that the producer set stays closed to agent pods: `scaledjob-iam.tf`
      # grants ReceiveMessage / DeleteMessage / GetQueueAttributes /
      # ChangeMessageVisibility and NO sqs:SendMessage, so an agent cannot forge
      # an engine dispatch without an IAM change — which is a review moment. The
      # producer set after this change is exactly {webhook Lambda, tick Lambda}.
      #
      # Scoped to the queue ARN, never `Resource = "*"`: a wildcard here would let
      # the tick produce onto any queue in the account, and `tests/orchestration/
      # test_dispatch_pass.py` asserts against that. Follows the shape of
      # `gateway_ingestion_sqs_publish` in ../../main.tf, including passing the ARN
      # in as a variable because the queue lives in a different Terraform state
      # (modules/agent-factory/webhook-ingress/infra/).
      #
      # No KMS grant: the queue is SSE-SQS (`sqs.tf` sets no kms_master_key_id).
      # No networking change: the SQS interface VPC endpoint already exists on the
      # private subnets with private DNS, and the tick SG already egresses 443.
      {
        Sid    = "PublishEngineDispatch"
        Effect = "Allow"
        Action = [
          "sqs:SendMessage",
          "sqs:GetQueueUrl"
        ]
        Resource = var.agent_submit_queue_arn != "" ? var.agent_submit_queue_arn : "arn:aws:sqs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:${var.name_prefix}-agent-submit.fifo"
      },
      # VPC ENI management, required for any Lambda with a vpc_config. These
      # actions do not support resource-level permissions.
      {
        Sid    = "VPCExecution"
        Effect = "Allow"
        Action = [
          "ec2:CreateNetworkInterface",
          "ec2:DescribeNetworkInterfaces",
          "ec2:DescribeSubnets",
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
      ],
      # -----------------------------------------------------------------------
      # GitHub engine-command bridge (Issue #4527)
      # -----------------------------------------------------------------------
      # The tick reads outstanding `@agent-engine` comments from the webhook-events
      # table's sparse `engine-command-index` and conditionally flips each marker to
      # consumed. The table is owned by the webhook-ingress Terraform state, hence
      # the ARN is built from a passed-in NAME rather than a resource reference — the
      # same cross-state pattern as `agent_submit_queue_arn` above.
      #
      # No networking change is needed: the tick SG already egresses 443 to
      # 0.0.0.0/0 for the RDS IAM token and CloudWatch, which covers DynamoDB,
      # Secrets Manager and api.github.com.
      var.webhook_events_table_name != "" ? [
        {
          Sid    = "EngineCommandEventsRead"
          Effect = "Allow"
          Action = [
            # Query only, on the index. NOT Scan and NOT GetItem: the bridge's
            # access pattern is "outstanding commands, oldest first", which the
            # sparse index answers exactly, and a Scan grant would let the tick read
            # 30 days of every tenant's webhook deliveries.
            "dynamodb:Query"
          ]
          Resource = [
            "arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/${var.webhook_events_table_name}/index/engine-command-index"
          ]
        },
        {
          Sid    = "EngineCommandConsume"
          Effect = "Allow"
          Action = [
            # UpdateItem on the base table, for the conditional pending -> consumed
            # flip that makes consumption idempotent. No PutItem and no DeleteItem:
            # the tick must never be able to create a command row or destroy the
            # audit record of one, only mark the one it just applied.
            "dynamodb:UpdateItem"
          ]
          Resource = [
            "arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/${var.webhook_events_table_name}"
          ]
        }
      ] : [],
      # Keep the conditional object in its own list: Terraform cannot unify a
      # heterogeneous tuple (with and without Condition) against an empty list.
      var.webhook_events_table_name != "" ? [
        {
          Sid    = "EngineRuns"
          Effect = "Allow"
          Action = ["dynamodb:PutItem", "dynamodb:GetItem"]
          Resource = [
            "arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/${var.webhook_events_table_name}"
          ]
          Condition = {
            "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = ["orch:*"] }
          }
        }
      ] : [],
      # The table is encrypted with a customer-managed key, so Query and UpdateItem
      # both fail at RUNTIME without this — not at plan time, which is why it is
      # called out rather than assumed. Mirrors `WebhookEventsKMSDecrypt` on the
      # gateway service role in the webhook-ingress state.
      var.webhook_events_kms_key_arn != "" ? [
        {
          Sid    = "EngineCommandEventsKMSDecrypt"
          Effect = "Allow"
          Action = [
            "kms:Decrypt",
            "kms:GenerateDataKey",
            "kms:DescribeKey"
          ]
          Resource = [var.webhook_events_kms_key_arn]
        }
      ] : [],
      # Per-tenant GitHub App credentials, used ONLY to mint an installation token
      # for the acknowledgement comment. Scoped to the environment's tenant prefix,
      # so this cannot reach the platform's own secrets. Without it the tick still
      # applies commands and reports `command_acks_failed`, which forces a
      # non-success tick — visibly degraded rather than silently unacknowledged.
      var.run_report_key_parameter != "" ? [
        {
          Sid      = "ReadRunReportSigningMaterial"
          Effect   = "Allow"
          Action   = ["ssm:GetParameter"]
          Resource = ["arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter${var.run_report_key_parameter}"]
        }
      ] : [],
      var.github_app_secret_arn_pattern != "" ? [
        {
          Sid    = "EngineCommandAckCredentials"
          Effect = "Allow"
          Action = [
            "secretsmanager:GetSecretValue"
          ]
          Resource = [var.github_app_secret_arn_pattern]
        }
      ] : []
    )
  })
}
