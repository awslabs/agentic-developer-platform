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
#   - the KMS key the tables use  (aws_kms_key.dynamodb)
#   - the agent-memory and chat-artifacts tables and the artifacts bucket
#     (chat-agent-infra.tf) — the delegated chat memory/artifact stores (#8028)
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

locals {
  gateway_intake_policy = jsonencode({
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
        # Delegated chat (#7899): with ADP_CHAT_DATA_ENABLED the model-root
        # admission transaction retains the registered chat input
        # (PK chat-input#<message_id>, src/agentauth/bootstrap.py) and the session
        # mailbox keeps persistent-session headers and turns (PK session#<id>,
        # src/agentauth/chat_session_mailbox.py) in the chat-context table. Both are
        # conditional writes keyed by the gateway; no other partition is writable.
        # The turn-result and finalization transactions also carry ConditionCheck
        # items on the session header and turn receipt (chat_history_write._unchanged)
        # and read the partition with Query (chat_turn_finalization, chat_history_write);
        # DynamoDB authorizes every transaction item separately, so without
        # ConditionCheckItem the whole TransactWriteItems is AccessDenied (#7983).
        Sid    = "ChatDataPlaneWrite"
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:ConditionCheckItem",
          "dynamodb:Query",
        ]
        Resource = [aws_dynamodb_table.chat_context.arn]
        Condition = {
          "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = ["chat-input#*", "session#*"] }
        }
      },
      {
        # Crew on an EC2 Dev Box (#8223): once the controller has verified the host,
        # GET /crew/target records the owner's lease at PK crew#<tenant>#<user>, SK executor
        # (src/crew/host_lease.py: a conditional PutItem keyed by tenant and owner) and
        # retires it with DeleteItem when the host is replaced or released. GetItem on the
        # partition is covered by IntakeDraftRead. Dev 2026-10-19: the first ready host
        # answered 500 (AccessDeniedException on PutItem) because no statement named it.
        Sid    = "CrewHostLeaseWrite"
        Effect = "Allow"
        Action = ["dynamodb:PutItem", "dynamodb:DeleteItem"]
        Resource = [aws_dynamodb_table.chat_context.arn]
        Condition = {
          "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = ["crew#*"] }
        }
      },
      {
        # Delegated chat memory (#8028): src/agentauth/chat_memory.py keeps each
        # record at PK memory#<id> / SK record, and the owner's index rows
        # (SK mem#<created>#<id>) and write receipts (SK write#<digest>) at
        # PK memory-owner#<tenant>#<user>. Reads are GetItem plus one Query on the
        # owner partition; every write is a TransactWriteItems of conditional Puts
        # on those two partitions, so PutItem is the only write action. No
        # UpdateItem, no DeleteItem (the store never issues them; retention is the
        # table TTL) and no index ARN (the by-id GSI is not read by the gateway).
        Sid    = "ChatMemoryStore"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:Query",
          "dynamodb:PutItem",
        ]
        Resource = [aws_dynamodb_table.agent_memory.arn]
        Condition = {
          "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = ["memory#*", "memory-owner#*"] }
        }
      },
      {
        # Delegated chat artifacts (#8028): src/agentauth/chat_artifact.py keeps
        # the catalog row at PK session#<id> / SK art#<created>#<id> in the
        # chat-artifacts table; the id pointer and write receipt live in the
        # chat-context session partition and are covered by ChatDataPlaneWrite.
        # Reads are GetItem and a Query on the session partition; create is one
        # conditional Put plus a ConditionCheck on the superseded row in the same
        # transaction. No UpdateItem/DeleteItem and no index ARN (by-id / by-org
        # are the sweeper's, not the gateway's).
        Sid    = "ChatArtifactCatalog"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:Query",
          "dynamodb:PutItem",
          "dynamodb:ConditionCheckItem",
        ]
        Resource = [aws_dynamodb_table.chat_artifacts.arn]
        Condition = {
          "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = ["session#*"] }
        }
      },
      {
        # Artifact bytes (#8028): chat_artifact.py writes
        # o/<tenant>/t/<team>/u/<user>/s/<session>/gateway/out/<id> (PutObject with
        # If-None-Match) and reads back only keys under that session prefix, so the
        # resource is the per-session object path the sweeper also uses. No
        # DeleteObject (expiry is the bucket lifecycle and the session sweeper's)
        # and no ListBucket (the catalog table is the index). The bucket is SSE-S3,
        # so there is no KMS grant to pair with this one.
        Sid    = "ChatArtifactObjects"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
        ]
        Resource = ["${aws_s3_bucket.chat_artifacts.arn}/o/*/t/*/u/*/s/*/*"]
      },
      {
        # Every table above (sessions, chat-context, agent-memory, chat-artifacts)
        # is SSE-KMS with the key in this stack. Without Decrypt every read is
        # AccessDenied at the KMS layer, which surfaces as a generic failure rather
        # than as a missing grant — the same trap the ingest Lambda's
        # identity-index precondition check exists to catch.
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

# Existing inline-policy deployments retain their state address and behavior.
moved {
  from = aws_iam_role_policy.gateway_intake_access
  to   = aws_iam_role_policy.gateway_intake_access[0]
}

resource "aws_iam_role_policy" "gateway_intake_access" {
  count  = var.gateway_intake_managed_policy && !var.gateway_intake_cutover_keep_inline ? 0 : 1
  name   = "adp-${var.environment}-policy-gateway-intake"
  role   = "adp-${var.environment}-role-gateway-service"
  policy = var.gateway_intake_cutover_keep_inline ? var.gateway_intake_cutover_inline_policy_json : local.gateway_intake_policy
}

# Same permission document when the gateway has exhausted its inline quota.
resource "aws_iam_policy" "gateway_intake_access" {
  count  = var.gateway_intake_managed_policy ? 1 : 0
  name   = "adp-${var.environment}-policy-gateway-intake"
  policy = local.gateway_intake_policy
}

resource "aws_iam_role_policy_attachment" "gateway_intake_access" {
  count      = var.gateway_intake_managed_policy ? 1 : 0
  role       = "adp-${var.environment}-role-gateway-service"
  policy_arn = aws_iam_policy.gateway_intake_access[0].arn
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
