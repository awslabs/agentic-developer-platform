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
      event_id    = "$.id"
      account     = "$.account"
      alarm_name  = "$.detail.alarmName"
      reason      = "$.detail.state.reason"
      source      = "$.source"
      detail_type = "$.detail-type"
    }

    input_template = <<-EOF
      {
        "id": <event_id>,
        "account": <account>,
        "adp_rule_arn": "${aws_cloudwatch_event_rule.alarm_state_change[0].arn}",
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

# -----------------------------------------------------------------------------
# Nightly security agent — root dispatch (issue #4450 / design note #4559)
# -----------------------------------------------------------------------------
# The nightly security pipeline's ONE root dispatch per night. A GitHub Actions
# job cannot originate an agent chain any other way: `adp-trigger` and
# POST /agent/trigger are both closed to root-minting (a fresh call has no
# lineage; a fabricated one is rejected 422 unknown_chain), and that fail-closed
# behaviour is the control, not a gap. See docs/design-notes/4559-ci-eventbridge-dispatch.md.
#
# Every hop INSIDE the night is an in-run `adp-trigger` dispatch, never a second
# put-events: put-events mints a fresh root at chain_depth=0, so a per-item emit
# would disable the depth cap and the cross-persona loop guard and leave the
# night's runs unlinked in lineage (§1).
#
# `persona`, `service_identity` and `repo` below are TERRAFORM LITERALS and must
# stay that way. `events:PutEvents` cannot be scoped to an event `source` — the
# IAM resource is the bus and `source` is a request-body field with no condition
# key — so the transformer is the primary control on this path (§5). Sourcing any
# of the three from `$.detail` hands a caller the choice of persona, identity or
# target repo. Only `reason`, `run_date` and `issue_number` are caller-supplied,
# and they land in a DynamoDB row and CloudWatch logs, so nothing sensitive
# travels in them.
#
# Default OFF (var.enable_eventbridge_security_agent_rule), like the alarm rule
# above. No new aws_lambda_permission: `eventbridge_invoke` is already bus-wide
# for rule/adp-${var.environment}-*, which this rule name matches.

resource "aws_cloudwatch_event_rule" "security_agent_dispatch" {
  count = var.enable_eventbridge_security_agent_rule ? 1 : 0

  name        = "adp-${var.environment}-security-agent-dispatch"
  description = "Nightly security pipeline -> webhook ingress Lambda (root dispatch, #4559)"

  # `source` is the only match condition the emitter controls; `detail-type`
  # pins the shape the transformer below assumes.
  event_pattern = jsonencode({
    source      = ["adp.security-agent"]
    detail-type = ["ADP Agent Dispatch"]
  })

  tags = {
    Purpose = "agent-dispatch"
    Issue   = "4559"
  }
}

resource "aws_cloudwatch_event_target" "security_agent_to_lambda" {
  count = var.enable_eventbridge_security_agent_rule ? 1 : 0

  rule      = aws_cloudwatch_event_rule.security_agent_dispatch[0].name
  target_id = "webhook-ingress-lambda"
  arn       = aws_lambda_function.github_webhook.arn

  input_transformer {
    input_paths = {
      event_id     = "$.id"
      account      = "$.account"
      source       = "$.source"
      detail_type  = "$.detail-type"
      reason       = "$.detail.reason"
      run_date     = "$.detail.run_date"
      issue_number = "$.detail.issue_number"
    }

    # `create_issue` is deliberately absent: nothing on this path consumes it
    # (§7.2). `issue_number` is REQUIRED instead — it becomes source_ref.issue,
    # and the agent worker runs `gh issue view $ISSUE_NUMBER` at startup, so an
    # empty value kills the run before it does anything. The CI job creates the
    # night's plan issue first and passes its number. <issue_number>
    # interpolates unquoted so it arrives as a JSON number.
    #
    # `dedup_key` = run_date names the correlation channel; it does NOT
    # deduplicate (SQS keys on arrival time, and the near-miss guards are skipped
    # for Service-typed senders — §7.3). Idempotency lives in the CI job.
    #
    # KEY ORDER IS A SECURITY PROPERTY HERE — do not "tidy" it.
    # EventBridge splices substituted values into this template WITHOUT escaping
    # them. A `reason` carrying a quote could therefore close its own string and
    # append its own `"persona"` key; JSON parsers keep the LAST occurrence of a
    # repeated key, so an injected key placed after the literal would win and the
    # emitter would choose its own persona/identity/repo — defeating the control
    # §5 calls primary (`events:PutEvents` cannot be scoped to an event source,
    # so this template is the only thing pinning them).
    #
    # Two independent defences, either sufficient:
    #   1. ops_dispatch.lint_event_field() allowlists `reason` to
    #      [A-Za-z0-9 .,:_-], so no quote or brace can reach this template.
    #   2. every caller-supplied value (<reason>, <run_date>, <issue_number>)
    #      appears BEFORE the pinned literals below, so even if a quote did get
    #      through, its injected duplicate is overridden by the literal that
    #      follows it rather than the other way round.
    input_template = <<-EOF
      {
        "id": <event_id>,
        "account": <account>,
        "adp_rule_arn": "${aws_cloudwatch_event_rule.security_agent_dispatch[0].arn}",
        "source": <source>,
        "detail-type": <detail_type>,
        "detail": {
          "adp_trigger": {
            "reason": <reason>,
            "dedup_key": <run_date>,
            "persona": "operations",
            "service_identity": "eventbridge:adp-${var.environment}-security-agent-dispatch",
            "target": {
              "issue_number": <issue_number>,
              "repo": "${var.eventbridge_security_agent_repo}"
            }
          }
        }
      }
    EOF
  }
}

# -----------------------------------------------------------------------------
# Service identity for the rule above (design note #4559 §3)
# -----------------------------------------------------------------------------
# The rule and its identity row must land together. A rule without its row fails
# closed at 403 unknown_service_identity — safe, but silent, visible only in
# Lambda logs, retried by nothing, and the night then reports nothing. So the row
# is Terraform, not a script.
#
# `allowed_personas` is a security control (a second, server-side ceiling on top
# of the transformer's literal persona), so it belongs in reviewed,
# drift-detected config rather than in a put-item call.
#
# tenant_id == org_id == the GitHub org that owns the pipeline repo. Tenant is a
# property of THIS ROW, never of the event: the handler reads both off the row and
# then resolves the App installation for that org, so a synthetic tenant
# ("system", "platform", null) would 422 no_installation_for_tenant immediately.
# The two are set to the same value because they are synonyms for org-owned
# tenants (#2951); a divergence here produces rows whose tenant and org disagree
# and breaks the tenant-index GSI queries the Activity UI runs.
#
# Two caveats: aws_dynamodb_table_item manages only the attributes it declares and
# will fight any other writer of the same key (acceptable — nothing else writes
# service_account rows), and the table is owned by a DIFFERENT Terraform state
# (modules/gateway/infra), read here via var.identity_index_table_name, so
# gateway-infra must have applied first.

resource "aws_dynamodb_table_item" "security_agent_service_identity" {
  count = var.enable_eventbridge_security_agent_rule ? 1 : 0

  table_name = var.identity_index_table_name
  hash_key   = "identity_type"
  range_key  = "identity_value"

  item = jsonencode({
    identity_type    = { S = "service_account" }
    identity_value   = { S = "eventbridge:adp-${var.environment}-security-agent-dispatch" }
    tenant_id        = { S = var.eventbridge_security_agent_org }
    org_id           = { S = var.eventbridge_security_agent_org }
    repo             = { S = var.eventbridge_security_agent_repo }
    rule_arn         = { S = aws_cloudwatch_event_rule.security_agent_dispatch[0].arn }
    allowed_personas = { L = [{ S = "operations" }] }
    # Child delegation is a separate ceiling; it must not widen root launches.
    allowed_child_personas = { SS = ["operations", "developer", "reviewer"] }
  })
}
