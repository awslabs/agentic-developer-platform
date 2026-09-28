# =============================================================================
# EventBridge — Machine/Root-Triggered Agent Transport (Issue #2154)
# =============================================================================
# EventBridge rules target the webhook Lambda natively for machine sources
# (CloudWatch alarms, scheduled rules, CI events). Each rule uses an
# InputTransformer to map the raw event to the adp_trigger schema.
#
# Resources:
#   - Lambda permission: allows events.amazonaws.com to invoke the Lambda
#     (scoped to rules matching adp-${env}-* pattern)
#   - Example rule + target for CloudWatch alarm-state-change events
#
# Additional rules are added per-service by Terraform or CLI. The Lambda
# permission covers all adp-prefixed rules on the default bus.
# =============================================================================

# -----------------------------------------------------------------------------
# Lambda Permission: allow EventBridge to invoke the webhook Lambda
# Scoped to rules matching the adp-${env}-* naming pattern on the default bus.
# -----------------------------------------------------------------------------

resource "aws_lambda_permission" "eventbridge_invoke" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.github_webhook.function_name
  principal     = "events.amazonaws.com"
  source_arn    = "arn:aws:events:${var.aws_region}:${local.account_id}:rule/adp-${var.environment}-*"
}

# -----------------------------------------------------------------------------
# Example: CloudWatch Alarm State Change rule
# Captures alarm-state-change events and maps them to the adp_trigger schema.
# Disabled by default — enable per-alarm by setting the variable.
# -----------------------------------------------------------------------------

resource "aws_cloudwatch_event_rule" "alarm_state_change" {
  count = var.enable_eventbridge_alarm_rule ? 1 : 0

  name        = "adp-${var.environment}-alarm-state-change"
  description = "Route CloudWatch alarm state changes to webhook ingress Lambda for agent triage"

  event_pattern = jsonencode({
    source      = ["aws.cloudwatch"]
    detail-type = ["CloudWatch Alarm State Change"]
    detail = {
      state = {
        value = ["ALARM"]
      }
    }
  })

  tags = {
    Purpose = "agent-triage"
    Issue   = "2154"
  }
}

resource "aws_cloudwatch_event_target" "alarm_to_lambda" {
  count = var.enable_eventbridge_alarm_rule ? 1 : 0

  rule      = aws_cloudwatch_event_rule.alarm_state_change[0].name
  target_id = "webhook-ingress-lambda"
  arn       = aws_lambda_function.github_webhook.arn

  input_transformer {
    input_paths = {
      alarm_name  = "$.detail.alarmName"
      reason      = "$.detail.state.reason"
      source      = "$.source"
      detail_type = "$.detail-type"
    }

    input_template = <<-EOF
      {
        "source": <source>,
        "detail-type": <detail_type>,
        "detail": {
          "adp_trigger": {
            "persona": "${var.eventbridge_alarm_persona}",
            "service_identity": "eventbridge:adp-${var.environment}-alarm-state-change",
            "reason": <reason>,
            "dedup_key": <alarm_name>,
            "target": {
              "repo": "${var.eventbridge_alarm_target_repo}",
              "create_issue": true
            }
          },
          "alarmName": <alarm_name>,
          "state": {
            "reason": <reason>
          }
        }
      }
    EOF
  }
}
