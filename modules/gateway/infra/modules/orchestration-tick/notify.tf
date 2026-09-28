# =============================================================================
# Orchestration Alert Delivery (Issue #4211)
# =============================================================================
# R-Q9d: detection that nobody hears about is indistinguishable from no detection
# at all. This file is the delivery half of the stall/halt story.
#
# WHY A NEW TOPIC RATHER THAN REUSING AN EXISTING ONE. There is nothing to reuse.
# `modules/gateway/infra/` creates ZERO SNS topics, and the two places that follow
# the repo's metric -> alarm -> `alarm_actions` pattern (`modules/budget-alarms`
# via `budget_alarm_sns_topic_arns`, and `modules/redis` via `sns_topic_arn`) are
# both UNSET in every tfvars — so both currently page nobody. Wiring into an
# unwired precedent would satisfy "reuse what exists" on paper while delivering
# exactly the log line the issue forbids.
#
# WHY THE ENGINE PUBLISHES DIRECTLY RATHER THAN VIA A CLOUDWATCH ALARM:
#   * A stall is a per-node event with an identity (which node, which org, which
#     flow, how long). An alarm fires on an aggregate crossing a threshold and
#     cannot name the node, so the operator would be paged with "something stalled
#     somewhere" — the diagnosis problem this EPIC exists to end.
#   * Notifications must be org-scoped. The org id belongs in a message attribute
#     a subscriber can filter on, not in an alarm dimension whose cardinality grows
#     with tenant count.
#   * "Once per event, not per tick" is a property of the event. An alarm
#     re-notifies every breach period by design.
#
# The topic is created unconditionally when the module is enabled — a topic with no
# subscription still accepts publishes, so the engine never fails for lack of a
# subscriber, and `alert_email_addresses` can be filled in later without a code
# change. `notifications_failed` on the tick's own metrics is what makes an
# unsubscribed topic visible rather than silently fine.
# =============================================================================

resource "aws_sns_topic" "alerts" {
  name = "${local.tick_name}-alerts"

  # Encrypted at rest. `alias/aws/sns` rather than the CloudWatch CMK: SNS requires
  # a key its own service principal can use, and the managed alias is the least
  # surprising choice for a topic whose payload is operational metadata.
  kms_master_key_id = "alias/aws/sns"

  tags = merge(var.common_tags, {
    Name    = "${local.tick_name}-alerts"
    Service = "sns"
    Purpose = "orchestration-stall-halt-alerts"
  })
}

# Enforce TLS in transit. Without this an unauthenticated HTTP publish path is
# permitted by default, which Checkov flags and which there is no reason to allow.
resource "aws_sns_topic_policy" "alerts" {
  arn = aws_sns_topic.alerts.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowOwnerAccountPublishAndSubscribe"
        Effect    = "Allow"
        Principal = { AWS = data.aws_caller_identity.current.account_id }
        Action = [
          "sns:Publish",
          "sns:Subscribe",
          "sns:GetTopicAttributes",
          "sns:SetTopicAttributes",
          "sns:ListSubscriptionsByTopic",
        ]
        Resource = aws_sns_topic.alerts.arn
      },
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.alerts.arn
        Condition = {
          Bool = { "aws:SecureTransport" = "false" }
        }
      },
    ]
  })
}

# Email subscriptions. Each address must be CONFIRMED by its owner before AWS
# delivers to it — that confirmation click is the manual post-apply step, and it is
# also why the smoke test asks for an observed notification rather than a
# successful publish: a publish to a topic whose only subscription is unconfirmed
# reaches nobody.
resource "aws_sns_topic_subscription" "alert_emails" {
  for_each = toset(var.alert_email_addresses)

  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = each.value
}
