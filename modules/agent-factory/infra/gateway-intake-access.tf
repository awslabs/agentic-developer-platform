# =============================================================================
# Gateway operator-plane intake access (#5331, EPIC #4191)
# =============================================================================
# `adp flow start` drives a planning conversation from a terminal. The gateway
# serves that surface (modules/gateway/src/orchestration/intake_*.py), but every
# resource it needs lives HERE:
#
#   - the sessions table          (module.gateway_sessions)      — readback
#   - the chat-context table      (aws_dynamodb_table.chat_context) — the draft
#   - the ingest Lambda           (module.gateway_lambda)        — sending a turn
#   - the KMS key both tables use (aws_kms_key.dynamodb)
#
# So the grants and the discovery parameters are declared in this stack, where all
# four are real in-state references. The alternative — declaring them in the
# gateway stack — would need either hardcoded names or a `terraform_remote_state`
# read of agent-factory, and this repo deliberately does not do the latter
# (see gateway-main.tf's locals: the dependency runs gateway → agent-factory, and
# reversing it for these four would create a cycle).
#
# The gateway's service role is created by platform/infra
# (platform/infra/modules/eks/main.tf -> aws_iam_role.gateway_service_irsa), which
# always applies before this stack, so attaching an inline policy to it by literal
# name is safe and is the established idiom here — see
# aws_iam_role_policy.gateway_activity_read in
# webhook-ingress/infra/iam.tf, which does exactly this for the activity read path.
# PutRolePolicy is an upsert, so this converges without an import.
#
# -----------------------------------------------------------------------------
# What is deliberately NOT granted
# -----------------------------------------------------------------------------
# Legacy intake rows remain read-only. Ingest and the chat worker retain ownership
# of that shape; the gateway cannot PutItem or UpdateItem those session keys.
# The separate Task-backed chat-* journal has static PutItem permission below,
# guarded by its server-owned version and principal. Legacy ingest refuses those
# journal rows, so it cannot attach an independent classifier turn to a Task.
#
# No `dynamodb:Scan`, and no wildcard on the Lambda resource. The readback is
# GetItem by session id plus one Query against the resume GSI; a Scan would make a
# cross-tenant read a matter of forgetting a filter rather than a permission error.
# =============================================================================

resource "aws_iam_role_policy" "gateway_intake_access" {
  name = "adp-${var.environment}-policy-gateway-intake"
  role = "adp-${var.environment}-role-gateway-service"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # The readback: GetItem by session_id, Query against user-workspace-index
        # for `--resume` with no id. The index ARN is separate from the table ARN
        # in DynamoDB's model, so both are required — a policy with only the table
        # makes `GET /intake/sessions/latest` AccessDenied while
        # `GET /intake/sessions/{id}` works, which reads as "resume is broken"
        # rather than as a missing permission.
        Sid    = "IntakeSessionsRead"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:Query",
        ]
        Resource = [
          module.gateway_sessions.table_arn,
          "${module.gateway_sessions.table_arn}/index/*",
        ]
      },
      {
        # Standing permission for Task-backed conversations only (#5640).
        # No role policy is changed per task; session CAS remains server owned.
        Sid      = "HostedTaskChatSessionsWrite"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem"]
        Resource = [module.gateway_sessions.table_arn]
        Condition = {
          "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = ["chat-*"] }
        }
      },
      {
        # The draft, at PK=session#<id>, SK=draft. GetItem only: no Query, because
        # the readback addresses exactly one item and a Query grant would permit
        # walking every row under a session partition.
        Sid      = "IntakeDraftRead"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem"]
        Resource = [aws_dynamodb_table.chat_context.arn]
      },
      {
        # Both tables are SSE-KMS with the key in this stack. Without Decrypt every
        # read is AccessDenied at the KMS layer, which surfaces as a generic
        # failure rather than as a missing grant — the same trap the ingest
        # Lambda's identity-index precondition check exists to catch.
        Sid    = "IntakeTablesKMSDecrypt"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:DescribeKey",
          "kms:GenerateDataKey",
        ]
        Resource = [aws_kms_key.dynamodb.arn]
      },
      {
        # Sending a turn. The gateway invokes the ingest Lambda synchronously so
        # the turn gets the session row, the thread, the transcript and the
        # registered run a browser turn gets — see
        # modules/gateway/src/orchestration/intake_dispatch.py on why writing to
        # the input queue directly produced a turn whose conversation did not
        # exist.
        #
        # THIS GRANT IS THE AUTHENTICATION for the gateway-api channel. That
        # envelope carries its own user_id/org_id, which the ingest Lambda
        # believes, because there is no authorizer on a direct invocation. So the
        # principal list matters: this is the gateway service role and nothing
        # else. `sqs:SendMessage` on the input queue is deliberately absent — the
        # gateway must not be able to reach the worker except through the ingest
        # contract.
        Sid      = "IntakeDispatchInvoke"
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = [module.gateway_lambda.ingest_lambda_arn]
      },
    ]
  })
}

# -----------------------------------------------------------------------------
# Discovery: SSM parameters the gateway deploy reads into pod env
# -----------------------------------------------------------------------------
# gateway-deploy.yml resolves each of these with `get_ssm` and substitutes it into
# k8s/configmap.yaml. Published from this stack because it owns the underlying
# resources, and a name derived on the gateway side would encode a convention it
# does not own — which fails SILENTLY: pointing at a table that does not exist is
# indistinguishable from a user with no sessions.
#
# All three are unconditional (no `count` on gateway_deployed): the resources they
# name exist in this stack regardless of whether the gateway module is deployed,
# and a parameter that disappears would make the gateway fall back to its
# unconfigured path and report `unavailable` — which is honest but would be caused
# by the parameter's absence rather than by anything actually missing.

resource "aws_ssm_parameter" "gateway_intake_sessions_table" {
  name        = "/adp/${var.environment}/agent-gateway/sessions-table"
  description = "Agent-gateway sessions table, read by the gateway's intake readback (#5331)"
  type        = "String"
  value       = module.gateway_sessions.table_name

  tags = { Component = "agent-gateway" }
}

resource "aws_ssm_parameter" "gateway_intake_context_table" {
  name        = "/adp/${var.environment}/agent-gateway/context-table"
  description = "Chat-context table holding the intake draft at PK=session#<id>, SK=draft (#5331)"
  type        = "String"
  value       = aws_dynamodb_table.chat_context.name

  tags = { Component = "agent-gateway" }
}

resource "aws_ssm_parameter" "gateway_intake_ingest_function" {
  name = "/adp/${var.environment}/agent-gateway/ingest-function"
  # A FUNCTION NAME, not a queue URL. The predecessor parameter for this path
  # would have been an SQS URL; it is not published, and the gateway deliberately
  # does not read `BG_INTAKE_QUEUE_URL` as a fallback, so a deployment cannot
  # quietly resume the queue shortcut this replaced.
  description = "Agent-gateway ingest Lambda, invoked by the gateway to send an intake turn (#5331)"
  type        = "String"
  value       = module.gateway_lambda.ingest_lambda_name

  tags = { Component = "agent-gateway" }
}

output "gateway_intake_ingest_function" {
  description = "Ingest Lambda name the gateway invokes to send an intake turn"
  value       = module.gateway_lambda.ingest_lambda_name
}
